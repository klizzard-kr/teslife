"""Stage 9: route points (bridge -> trip_points), simplification, route and map APIs."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import _store_route, connect as process_connect, process_db
from process.geo import distance_m, simplify, thin
from web import make_server, serve_in_thread

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("teslife_bridge_map", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

VID = "VINMAP"
T0 = datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc)  # 11/03 09:00 KST
HOME = (37.5000, 127.0000)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": iso(t), "name": name, "value": value}


def loc(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon}


def drive(start: datetime, origin: tuple[float, float], steps: int, odo0: float, used0: float,
          dlat: float = 0.0009) -> tuple[list[dict], datetime, tuple[float, float]]:
    """Drive north: one Location every 10 s, ~100 m apart, plus a jittery ~10 m point (under ROUTE_MIN_M 20 m)."""
    rows = [sig(start, "Gear", "D")]
    lat, lon = origin
    for i in range(1, steps + 1):
        t = start + timedelta(seconds=10 * i)
        lat += dlat
        rows.append(sig(t, "Location", loc(round(lat, 6), lon)))
        if i == 2:
            rows.append(sig(t + timedelta(seconds=5), "Location", loc(round(lat + 0.00009, 6), lon)))  # ~10 m
    end = start + timedelta(seconds=10 * steps + 5)
    km = steps * 0.1
    rows += [sig(start, "Odometer", odo0), sig(end, "Odometer", round(odo0 + km, 3)),
             sig(start, "LifetimeEnergyUsed", used0), sig(end, "LifetimeEnergyUsed", used0 + km * 0.15),
             sig(end, "Gear", "P")]
    return rows, end, (lat, lon)


def scenario() -> list[dict]:
    rows = [sig(T0, "Gear", "P"), sig(T0, "Odometer", 500.0), sig(T0, "LifetimeEnergyUsed", 100.0),
            sig(T0 - timedelta(hours=10), "Location", loc(*HOME))]
    a, end1, pos = drive(T0 + timedelta(minutes=5), HOME, 20, 500.0, 100.0)
    rows += a
    b, end2, pos2 = drive(end1 + timedelta(minutes=3), pos, 10, 502.0, 100.3)
    rows += b
    t3 = end2 + timedelta(days=1)
    # navigation set while parked, shortly before the third trip
    rows += [sig(t3 - timedelta(minutes=2), "DestinationName", "회사"),
             sig(t3 - timedelta(minutes=2), "DestinationLocation", loc(37.5050, 127.0)),
             sig(t3 + timedelta(seconds=20), "MilesToArrival", 1.4),
             sig(t3 + timedelta(seconds=20), "ExpectedEnergyPercentAtTripArrival", 71)]
    c, _end3, _ = drive(t3, pos2, 15, 503.0, 100.45, dlat=-0.0009)
    rows += c
    return rows



def _status(port: int, method: str, path: str, body: dict) -> int:
    import json as _j, urllib.error as _e, urllib.request as _r
    req = _r.Request(f"http://127.0.0.1:{port}{path}", method=method, data=_j.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    try:
        with _r.urlopen(req, timeout=5) as r:
            return r.status
    except _e.HTTPError as e:
        return e.code

class GeoTests(unittest.TestCase):
    def test_distance_and_thin(self) -> None:
        self.assertAlmostEqual(distance_m((37.5, 127.0), (37.5009, 127.0)), 100.1, delta=0.5)
        pts = [(37.5, 127.0), (37.5001, 127.0), (37.5009, 127.0), (37.50095, 127.0)]
        out = thin(pts, 50)
        self.assertEqual(out[0], pts[0])
        self.assertEqual(out[-1], pts[-1])  # last point always kept
        self.assertNotIn(pts[1], out)  # 11 m from the start

    def test_simplify_straight_line(self) -> None:
        line = [(37.5 + i * 0.0001, 127.0) for i in range(50)]
        self.assertEqual(len(simplify(line, 5)), 2)
        bend = line + [(line[-1][0], 127.0 + i * 0.0001) for i in range(1, 30)]
        self.assertEqual(len(simplify(bend, 5)), 3)


class BridgeLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        bridge.GEAR_STATE.clear()

    def line(self, **fields) -> str:
        data = {"Vin": VID, "CreatedAt": "2026-11-03T00:00:00Z"}
        data.update(fields)
        return json.dumps({"msg": "record_payload", "data": data})

    def names(self, **fields) -> dict:
        return {r["name"]: r["value"] for r in bridge.rows_from_line(self.line(**fields))}

    def test_only_while_driving(self) -> None:
        where = {"latitude": 37.123456789, "longitude": 127.987654321}
        self.assertNotIn("Location", self.names(Gear="ShiftStateP", Location=where))
        got = self.names(Gear="ShiftStateD", Location=where)  # departure payload
        self.assertEqual(got["Location"], {"lat": 37.123457, "lon": 127.987654})
        self.assertIn("Location", self.names(Location=where))  # still driving
        self.assertIn("Location", self.names(Gear="ShiftStateP", Location=where))  # arrival point
        self.assertNotIn("Location", self.names(Location=where))  # parked afterwards
        self.assertNotIn("GpsHeading", self.names(Gear="ShiftStateD", GpsHeading=90))

    def test_bad_location_dropped(self) -> None:
        self.names(Gear="ShiftStateD")
        self.assertNotIn("Location", self.names(Location={"latitude": 0, "longitude": 0}))
        self.assertNotIn("Location", self.names(Location={"latitude": "x", "longitude": 1}))


class MapBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "m.db")
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, scenario())
        conn.close()
        process_db(self.db, seed=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def q(self, sql: str, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TripPointTests(MapBase):
    def test_points_per_trip(self) -> None:
        trips = [r[0] for r in self.q("SELECT start_ts FROM trips ORDER BY start_ts")]
        # the second drive starts 3 minutes after the first parks, so they are one trip
        self.assertEqual(len(trips), 2)
        pts = self.q("SELECT lat, lon FROM trip_points WHERE trip_start_ts = ? ORDER BY seq", (trips[0],))
        self.assertEqual(tuple(pts[0]), HOME)  # parked position is the start
        self.assertEqual(len(pts), 31)  # 20 + 10 moves + start; the ~10 m jitter point is thinned
        for a, b in zip(pts, pts[1:]):
            self.assertGreaterEqual(distance_m(a, b), 20)

    def test_route_kept_when_raw_is_gone(self) -> None:
        start = self.q("SELECT start_ts FROM trips ORDER BY start_ts LIMIT 1")[0][0]
        n = self.q("SELECT COUNT(*) FROM trip_points WHERE trip_start_ts = ?", (start,))[0][0]
        conn = process_connect(self.db)
        with conn:
            _store_route(conn, VID, datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), [])
        conn.close()
        self.assertEqual(self.q("SELECT COUNT(*) FROM trip_points WHERE trip_start_ts = ?", (start,))[0][0], n)

    def test_rerun_is_stable(self) -> None:
        before = self.q("SELECT * FROM trip_points ORDER BY trip_start_ts, seq")
        process_db(self.db, seed=False)
        conn = process_connect(self.db)
        with conn:
            conn.execute("DELETE FROM process_meta")
        conn.close()
        process_db(self.db, seed=False)
        self.assertEqual(self.q("SELECT * FROM trip_points ORDER BY trip_start_ts, seq"), before)


class PlaceAndDestinationTests(MapBase):
    def test_trip_coordinates_no_destination(self) -> None:
        # : the navigation destination is neither stored nor read
        cols = {r[1] for r in self.q("PRAGMA table_info(trips)")}
        self.assertFalse({c for c in cols if c.startswith("dest_")})
        rows = self.q("SELECT start_lat, start_lon, end_lat FROM trips ORDER BY start_ts")
        self.assertEqual(rows[0][:2], HOME)
        # the next drive is 3 minutes later, so this row is both drives (20 + 10 steps)
        self.assertAlmostEqual(rows[0][2], 37.5 + 30 * 0.0009, places=5)

    def test_trip_start_alert_has_no_destination(self) -> None:
        from process import _load_events
        from process.alerts import check_trip_start
        conn = ingest.connect(self.db)
        t = T0 + timedelta(days=5)
        ingest.store_signals(conn, [
            sig(t - timedelta(minutes=1), "DestinationName", "부천 T스테이션"),
            sig(t, "Gear", "D"), sig(t, "BatteryLevel", 60.0),
            sig(t + timedelta(seconds=3), "MilesToArrival", 12.31),
            sig(t + timedelta(seconds=3), "ExpectedEnergyPercentAtTripArrival", 55),
        ])
        conn.close()
        conn = process_connect(self.db)
        sent: list = []
        try:
            check_trip_start(conn, VID, _load_events(conn, VID), t + timedelta(seconds=70),
                             lambda p: (sent.append(p) or True, "http 200"))
        finally:
            conn.close()
        self.assertEqual(len(sent), 1)
        self.assertNotIn("목적지", sent[0]["message"])
        self.assertNotIn("부천", sent[0]["message"])
        self.assertNotIn("destination", sent[0]["data"])

    def test_drop_trip_dest_migration(self) -> None:
        from process.drop_trip_dest import drop_trip_dest
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN dest_name TEXT")
            conn.execute("ALTER TABLE trips ADD COLUMN dest_km REAL")
            conn.execute("UPDATE trips SET dest_name = '회사', dest_km = 1.4 WHERE start_ts = (SELECT MAX(start_ts) FROM trips)")
        conn.close()
        before = self.q("SELECT start_ts, start_lat, start_lon, end_lat, end_lon FROM trips ORDER BY start_ts")
        backup = Path(self.tmp.name) / "nodest.bak"
        out = drop_trip_dest(self.db, backup)
        self.assertTrue(out["changed"])
        self.assertEqual(out["columns"], ["dest_name", "dest_km"])
        self.assertEqual(out["trips_with_dest"], 1)
        self.assertEqual(out["signals_deleted"], 4)   # DestinationName, DestinationLocation, MilesToArrival, ExpectedEnergyPercentAtTripArrival
        self.assertEqual(self.q("SELECT start_ts, start_lat, start_lon, end_lat, end_lon FROM trips ORDER BY start_ts"), before)
        self.assertEqual(sqlite3.connect(backup).execute("SELECT COUNT(*) FROM trips WHERE dest_name = '회사'").fetchone()[0], 1)
        self.assertFalse(drop_trip_dest(self.db, Path(self.tmp.name) / "again.bak")["changed"])

    def test_reverse_geocode_cache_and_labels(self) -> None:
        from process.geocode import fill_trip_labels, label_from_nominatim
        from web import label_trips, trip_entries

        calls: list = []

        class Fake:
            name = "fake"

            def reverse(self, lat, lon):
                calls.append((lat, lon))
                return "역삼1동" if lat < 37.51 else "삼성동"

        conn = process_connect(self.db)
        try:
            self.assertEqual(fill_trip_labels(conn, VID, Fake(), limit=2), 2)
            fill_trip_labels(conn, VID, Fake(), limit=10)
            n = len(calls)
            self.assertEqual(fill_trip_labels(conn, VID, Fake(), limit=10), 0)  # all cached
            self.assertEqual(len(calls), n)
            conn.row_factory = sqlite3.Row
            entries = trip_entries(conn, VID)
            self.assertEqual(entries[-1]["from_label"], "역삼1동")
            self.assertEqual(entries[0]["to_label"], "삼성동")  # reverse-geocoded area (no navigation name)
        finally:
            conn.close()
        self.assertEqual(label_from_nominatim({"address": {"road": "테헤란로", "suburb": "역삼1동", "borough": "강남구"}}),
                         "역삼1동")
        self.assertIsNone(label_from_nominatim({}))

    def test_nominatim_request_policy(self) -> None:
        import process.geocode as gc

        seen: list = []

        class Res:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"address": {"suburb": "역삼1동"}}).encode()

        def opener(req, timeout=0):
            seen.append(req)
            return Res()

        saved = gc.MIN_INTERVAL_S
        gc.MIN_INTERVAL_S = 0.05
        try:
            g = gc.NominatimGeocoder(opener)
            self.assertEqual(g.reverse(37.5, 127.03), "역삼1동")
            self.assertEqual(g.reverse(37.5, 127.03), "역삼1동")
        finally:
            gc.MIN_INTERVAL_S = saved
        self.assertIn("teslife", seen[0].get_header("User-agent"))
        self.assertIn("accept-language=ko", seen[0].full_url)


class MapApiTests(MapBase):
    def setUp(self) -> None:
        super().setUp()
        self.server = make_server(self.db, port=0, process_interval=0, seed=False)
        self.port = self.server.server_address[1]
        serve_in_thread(self.server)
        self.trips = [r[0] for r in self.q("SELECT start_ts FROM trips ORDER BY start_ts")]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                body = r.read()
                return r.status, (json.loads(body) if path.startswith("/api/") else body)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def test_trip_route(self) -> None:
        status, data = self.get("/api/trips/" + self.trips[0] + "/route")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["segments"]), 1)
        seg = data["segments"][0]
        self.assertEqual(seg[0], list(HOME))
        self.assertEqual(len(seg), 2)  # straight line simplifies to its ends
        self.assertEqual(data["bounds"][0][0], HOME[0])
        status, _ = self.get("/api/trips/2020-01-01T00:00:00Z/route")
        self.assertEqual(status, 404)

    def test_no_merge_api_routes_per_trip(self) -> None:
        # merging was removed: no endpoint; a leftover trip_merges row is kept but ignored
        self.assertEqual(_status(self.port, "POST", "/api/trip-merges", {"start_ts": self.trips[:2]}), 405)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.executemany("INSERT INTO trip_merges (vehicle_id, merge_id, start_ts, created_at) VALUES (?, ?, ?, ?)",
                             [(r[0], "m-x", r[1], "2026-11-01T00:00:00Z") for r in
                              conn.execute("SELECT vehicle_id, start_ts FROM trips ORDER BY start_ts LIMIT 2").fetchall()])
        conn.close()
        status, data = self.get("/api/trips/" + self.trips[1] + "/route")
        self.assertEqual(status, 200)
        self.assertNotIn("merged", data)
        self.assertEqual(data["start_ts"], self.trips[1])
        self.assertEqual(len(data["segments"]), 1)
        status, data = self.get("/api/map/routes?month=2026-11")
        self.assertEqual(status, 200)
        self.assertEqual([r["start_ts"] for r in data["routes"]], self.trips[::-1])  # newest first, one each
        self.assertTrue(all("merged" not in r for r in data["routes"]))
        status, _ = self.get("/api/map/routes?starts=" + self.trips[0])
        self.assertEqual(status, 400)  # picking trips from the list is gone
        status, _ = self.get("/api/map/routes")
        self.assertEqual(status, 400)
        self.assertEqual(self.q("SELECT COUNT(*) FROM trip_merges")[0][0], 2)  # table untouched

    def test_cumulative(self) -> None:
        status, data = self.get("/api/map/cumulative")
        self.assertEqual(status, 200)
        self.assertEqual(data["trips"], 2)
        self.assertAlmostEqual(data["km"], 4.5, places=3)
        status, data = self.get("/api/map/cumulative?year=2025")
        self.assertEqual((status, data["trips"], data["bounds"]), (200, 0, None))

    def send(self, method: str, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_places_label_trips(self) -> None:
        status, data = self.send("POST", "/api/places", {"name": "집", "lat": HOME[0], "lon": HOME[1], "radius_m": 200})
        self.assertEqual(status, 201)
        pid = data["place"]["id"]
        status, _ = self.send("POST", "/api/places", {"name": "", "lat": 1, "lon": 1})
        self.assertEqual(status, 400)
        status, _ = self.send("POST", "/api/places", {"name": "x", "lat": 1, "lon": 1, "radius_m": 5})
        self.assertEqual(status, 400)
        status, data = self.get("/api/places")
        self.assertEqual([p["name"] for p in data["places"]], ["집"])
        status, data = self.get("/api/trips?month=2026-11")
        oldest = data["trips"][-1]
        self.assertNotEqual(oldest["from_label"], "집")
        self.assertNotEqual(data["trips"][0]["to_label"], "회사")  # no navigation destination label
        status, data = self.send("PUT", "/api/places", {"id": pid, "name": "우리집"})
        self.assertEqual((status, data["place"]["name"]), (200, "우리집"))
        status, _ = self.send("DELETE", "/api/places", {"id": pid})
        self.assertEqual(status, 200)
        status, _ = self.send("DELETE", "/api/places", {"id": pid})
        self.assertEqual(status, 404)

    def test_map_config_lists_only_implemented(self) -> None:
        import os
        os.environ["TESLIFE_MAP_KAKAO_KEY"] = "dummy"
        try:
            status, data = self.get("/api/map/config")
        finally:
            os.environ.pop("TESLIFE_MAP_KAKAO_KEY", None)
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in data["providers"]], ["osm"])
        self.assertNotIn("dummy", json.dumps(data))

    def test_live_hides_location_and_static_served(self) -> None:
        status, data = self.get("/api/live")
        self.assertEqual(status, 200)
        self.assertNotIn("Location", data["signals"])
        status, body = self.get("/static/leaflet.js")
        self.assertEqual(status, 200)
        self.assertIn(b"Leaflet", body[:400])
        status, body = self.get("/static/leaflet.css")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
