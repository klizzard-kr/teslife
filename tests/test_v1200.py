"""v1.20.0: keep GpsHeading only, return it, and save the pin. Example coordinates only."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ingest import connect as ingest_connect
from ingest import store_signals
from process import connect
from web import make_server, serve_in_thread

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
_SPEC = importlib.util.spec_from_file_location("teslife_bridge_v1200", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

VID = "demo-car"
# Example point near Seoul City Hall, and a nearby example pin. Tests only.
CAR_LAT, CAR_LON = 37.5665, 126.9780
PIN_LAT, PIN_LON = 37.5710, 126.9780
OTHER_LAT, OTHER_LON = 37.5680, 126.9800
TS = "2026-11-26T06:05:00Z"
LATER = "2026-11-26T06:40:00Z"


def _line(created_at, **fields):
    data = {"Vin": VID, "CreatedAt": created_at}
    data.update(fields)
    return json.dumps({"msg": "record_payload", "data": data})


def _names(created_at, **fields):
    bridge.GEAR_STATE.clear()
    return {row["name"]: row["value"] for row in bridge.rows_from_line(_line(created_at, **fields))}


class GpsHeadingBridgeTests(unittest.TestCase):
    def test_only_gps_heading_is_kept_with_its_time(self) -> None:
        bridge.GEAR_STATE.clear()
        rows = bridge.rows_from_line(_line(
            TS,
            Gear="ShiftStateD",
            GpsHeading=123.4,
            GpsState="GpsStateNominal",
            OriginLocation={"latitude": CAR_LAT, "longitude": CAR_LON},
            RouteLine="line",
        ))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["GpsHeading"]["value"], 123.4)
        self.assertEqual(by_name["GpsHeading"]["timestamp"], TS)
        for gone in ("GpsState", "OriginLocation", "RouteLine"):
            self.assertNotIn(gone, by_name)
        self.assertIsNone(bridge.heading_degrees("<invalid>"))
        self.assertIsNone(bridge.heading_degrees(True))
        self.assertIsNone(bridge.heading_degrees("nope"))
        self.assertIsNone(bridge.heading_degrees({"latitude": CAR_LAT, "longitude": CAR_LON}))

        parked = _names(TS, Gear="ShiftStateP", GpsHeading=90, GpsFoo=1)
        self.assertEqual(parked["GpsHeading"], 90)
        self.assertNotIn("GpsFoo", parked)
        self.assertNotIn("Location", parked)

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        conn = ingest_connect(tmp.name)
        try:
            store_signals(conn, rows)
            saved = conn.execute(
                "SELECT ts, value FROM signals WHERE name = 'GpsHeading'"
            ).fetchone()
            others = conn.execute(
                "SELECT name FROM signals WHERE name LIKE 'Gps%' AND name != 'GpsHeading'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(saved[0], TS)
        self.assertEqual(json.loads(saved[1]), 123.4)
        self.assertEqual(others, [])

    def test_invalid_heading_is_dropped(self) -> None:
        got = _names(TS, Gear="ShiftStateD", GpsHeading="<invalid>", GpsState=1)
        self.assertNotIn("GpsHeading", got)
        self.assertNotIn("GpsState", got)


class CarLocationCardTests(unittest.TestCase):
    def test_map_pin_and_arrow_markup(self) -> None:
        block = HTML.split("// ---------------------------------------------------------------- car location", 1)[1].split(
            "// ---------------------------------------------------------------- refresh plumbing", 1
        )[0]
        self.assertIn("rawMap(el)", block)
        self.assertIn("mapLib()", block)
        self.assertNotIn("loadLeaflet()", block)
        self.assertNotIn("L.map(", block)
        self.assertIn("car-loc-arrow", block)
        self.assertIn("M18 3.2", block)
        self.assertIn("iconAnchor: [16, 40]", block)
        self.assertIn("movePinToCar", block)
        self.assertIn("savePinHome", block)
        self.assertIn('writeHome({ lat: here.lat, lon: here.lon })', block)
        self.assertNotIn("use_last", block.split("function savePinHome", 1)[1].split("function deleteCarHome", 1)[0])
        self.assertIn("핀 위치를 집으로 저장", HTML)
        self.assertNotIn("carBadgeIcon", HTML)
        self.assertNotIn("여기를 집으로 저장", HTML)
        self.assertNotIn("이 위치로 저장", HTML)
        self.assertIn("setRotationAngle", NAVER)
        self.assertIn("draggable: !!options.draggable", NAVER)
        self.assertIn("function control(", NAVER)
        self.assertIn("disableClickPropagation", NAVER)
        self.assertIn('const APP_VERSION = "v1.20.0"', HTML)
        note = HTML.split('version: "1.20.0"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("화살표", note)


class HeadingAndPinApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._route = os.environ.get("TESLIFE_ROUTE_MATCH")
        self._osrm = os.environ.get("TESLIFE_OSRM_URL")
        os.environ.pop("TESLIFE_ROUTE_MATCH", None)
        os.environ.pop("TESLIFE_OSRM_URL", None)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.path.exists(self.db) and os.remove(self.db))
        conn = connect(self.db)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
            (VID, TS, json.dumps({"lat": CAR_LAT, "lon": CAR_LON})),
        )
        conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m, created_at) "
            "VALUES (?, '예시', ?, ?, 150, ?)",
            (VID, OTHER_LAT, OTHER_LON, TS),
        )
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, ?)",
            (VID, "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", 1.5),
        )
        conn.commit()
        self.kept = {
            "tables": [tuple(r) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )],
            "places_cols": [tuple(r) for r in conn.execute("PRAGMA table_info(places)")],
            "trips": [tuple(r) for r in conn.execute("SELECT vehicle_id, start_ts, km FROM trips")],
            "other": [tuple(r) for r in conn.execute(
                "SELECT name, lat, lon FROM places WHERE TRIM(name) != '집'"
            )],
        }
        conn.close()
        self.server = make_server(self.db, host="127.0.0.1", port=0, process_interval=0, seed=False)
        self.port = self.server.server_address[1]
        serve_in_thread(self.server)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._route is None:
            os.environ.pop("TESLIFE_ROUTE_MATCH", None)
        else:
            os.environ["TESLIFE_ROUTE_MATCH"] = self._route
        if self._osrm is None:
            os.environ.pop("TESLIFE_OSRM_URL", None)
        else:
            os.environ["TESLIFE_OSRM_URL"] = self._osrm

    def get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as res:
            return res.status, json.loads(res.read().decode())

    def put(self, body: dict):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/home",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_heading_absent_then_latest_value(self) -> None:
        status, data = self.get("/api/car/location")
        self.assertEqual(status, 200)
        self.assertIsNone(data["heading"])
        self.assertEqual(data["lat"], CAR_LAT)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'GpsHeading', ?)",
            (VID, TS, json.dumps(90)),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'GpsHeading', ?)",
            (VID, LATER, json.dumps(180)),
        )
        conn.commit()
        conn.close()
        status, data = self.get("/api/car/location")
        self.assertEqual(status, 200)
        self.assertEqual(data["heading"], 180)
        self.assertNotIn("address", data)

    def test_pin_coordinates_save_only_the_home_row(self) -> None:
        status, data = self.put({"lat": PIN_LAT, "lon": PIN_LON})
        self.assertEqual(status, 201)
        self.assertEqual(data["home"]["name"], "집")
        self.assertEqual((data["home"]["lat"], data["home"]["lon"]), (PIN_LAT, PIN_LON))
        status, got = self.get("/api/home")
        self.assertEqual((got["home"]["lat"], got["home"]["lon"]), (PIN_LAT, PIN_LON))
        conn = sqlite3.connect(self.db)
        try:
            tables = [tuple(r) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )]
            cols = [tuple(r) for r in conn.execute("PRAGMA table_info(places)")]
            trips = [tuple(r) for r in conn.execute("SELECT vehicle_id, start_ts, km FROM trips")]
            other = [tuple(r) for r in conn.execute(
                "SELECT name, lat, lon FROM places WHERE TRIM(name) != '집'"
            )]
            homes = conn.execute("SELECT COUNT(*) FROM places WHERE TRIM(name) = '집'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tables, self.kept["tables"])
        self.assertEqual(cols, self.kept["places_cols"])
        self.assertEqual(trips, self.kept["trips"])
        self.assertEqual(other, self.kept["other"])
        self.assertEqual(homes, 1)


if __name__ == "__main__":
    unittest.main()
