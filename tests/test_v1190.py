"""v1.19.0: home card location and the single saved home. Example coordinates only."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from process import PROCESS_VERSION, connect
from process.alerts import home_leg
from web import make_server, serve_in_thread
from web.carloc import car_location_payload

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
VID = "demo-car"
# Example point near Seoul City Hall, and a nearby example pin. Tests only.
CAR_LAT, CAR_LON = 37.5665, 126.9780
PIN_LAT, PIN_LON = 37.5710, 126.9780
OTHER_LAT, OTHER_LON = 37.5680, 126.9800
TS = "2026-11-26T06:05:00Z"
OLDER = "2026-11-26T05:00:00Z"


def _node(expr: str) -> str:
    src = I18N + "\nprocess.stdout.write(String((" + expr + ")));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return out.stdout


class HomeCardMarkupTests(unittest.TestCase):
    def test_card_sits_under_the_temperature_card(self) -> None:
        temp = HTML.index('id="temp-card"')
        card = HTML.index('id="car-loc-card"')
        foot = HTML.index('id="home-foot"')
        self.assertLess(temp, card)
        self.assertLess(card, foot)
        block = HTML[card:foot]
        for text in (
            "차 위치",
            "지금 차 위치를 집으로 저장",
            "핀 위치를 집으로 저장",
            "집 삭제",
            'id="car-loc-map" class="map-box"',
        ):
            self.assertIn(text, block)
        self.assertNotIn("여기를 집으로 저장", HTML)
        self.assertNotIn("이 위치로 저장", HTML)
        self.assertNotIn("+ 장소 추가", HTML)
        self.assertNotIn("37.566", HTML)
        self.assertNotIn("126.978", HTML)

    def test_version_note(self) -> None:
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('const APP_VERSION = "v1.20.0"', HTML)
        note = HTML.split('version: "1.19.0"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("차 위치", note)
        self.assertIn("집 한 곳", note)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_lines(self) -> None:
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("집에 있음")'),
            "At home",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("집에서 1.2km")'),
            "1.2 km from home",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("마지막 위치 · 오후 3:05")'),
            "Last location · 3:05 PM",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("ko", false), TeslifeI18n.t("차 위치")'),
            "차 위치",
        )


class HomeApiTests(unittest.TestCase):
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
        self._kept = self._seed(conn)
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

    def _seed(self, conn: sqlite3.Connection) -> dict:
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
            (VID, OLDER, json.dumps({"lat": OTHER_LAT, "lon": OTHER_LON})),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
            (VID, TS, json.dumps({"lat": CAR_LAT, "lon": CAR_LON})),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'LocatedAtHome', ?)",
            (VID, TS, json.dumps(False)),
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
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?, ?, ?, ?)",
            (VID, "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z", "AC"),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) VALUES (?, ?, ?, ?, ?, ?)",
            (VID, "row-1", "2026-01-03", "other", 1000, "manual"),
        )
        conn.execute(
            "INSERT INTO route_match (vehicle_id, trip_start_ts, provider, points_sig, status) "
            "VALUES (?, ?, 'test', 'sig', 'raw')",
            (VID, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        return self._snapshot(conn)

    def _snapshot(self, conn: sqlite3.Connection) -> dict:
        def rows(sql: str):
            return [tuple(r) for r in conn.execute(sql).fetchall()]

        return {
            "tables": rows("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"),
            "places_cols": rows("PRAGMA table_info(places)"),
            "trips": rows("SELECT vehicle_id, start_ts, end_ts, km FROM trips"),
            "charges": rows("SELECT vehicle_id, start_ts, end_ts, kind FROM charges"),
            "ledger": rows("SELECT vehicle_id, id, date, category, amount_krw, source FROM ledger"),
            "routes": rows("SELECT vehicle_id, trip_start_ts, provider, points_sig, status FROM route_match"),
            "other": rows("SELECT name, lat, lon FROM places WHERE TRIM(name) != '집' ORDER BY id"),
        }

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def send(self, method: str, path: str, body: dict | None, origin: str | None = "self"):
        data = b"" if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if origin == "self":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read().decode() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                payload = json.loads(raw.decode())
            except json.JSONDecodeError:
                payload = {"error": raw.decode("utf-8", "replace")}
            return exc.code, payload

    def test_no_home_uses_the_latest_fix_and_leaves_other_rows(self) -> None:
        status, data = self.get("/api/home")
        self.assertEqual(status, 200)
        self.assertIsNone(data["home"])
        status, data = self.get("/api/car/location")
        self.assertEqual(status, 200)
        self.assertEqual(data["lat"], CAR_LAT)
        self.assertEqual(data["lon"], CAR_LON)
        self.assertEqual(data["received_at"], TS)
        self.assertIs(data["located_at_home"], False)
        self.assertIsNone(data["home"])
        self.assertIsNone(data["home_km"])
        self.assertIsNone(data["home_via"])
        self.assertNotIn("address", data)
        self.assertNotIn("label", data)

    def test_save_overwrite_and_delete_only_the_home_row(self) -> None:
        status, data = self.send("PUT", "/api/home", {"use_last": True})
        self.assertEqual(status, 201)
        self.assertEqual(data["home"]["name"], "집")
        self.assertEqual((data["home"]["lat"], data["home"]["lon"]), (CAR_LAT, CAR_LON))
        home_id = data["home"]["id"]
        status, got = self.get("/api/home")
        self.assertEqual(got["home"]["id"], home_id)
        status, loc = self.get("/api/car/location")
        km, via = home_leg((CAR_LAT, CAR_LON), (CAR_LAT, CAR_LON))
        self.assertEqual((loc["home_km"], loc["home_via"]), (km, via))
        self.assertEqual(loc["home_via"], "직선")
        self.assertEqual(loc["home"]["id"], home_id)

        status, data = self.send("PUT", "/api/home", {"lat": PIN_LAT, "lon": PIN_LON, "name": "다른이름"})
        self.assertEqual(status, 200)
        self.assertEqual(data["home"]["id"], home_id)
        self.assertEqual(data["home"]["name"], "집")
        self.assertEqual((data["home"]["lat"], data["home"]["lon"]), (PIN_LAT, PIN_LON))
        conn = self._open()
        homes = conn.execute("SELECT id, name FROM places WHERE TRIM(name) = '집'").fetchall()
        self.assertEqual(homes, [(home_id, "집")])
        status, loc = self.get("/api/car/location")
        km, via = home_leg((CAR_LAT, CAR_LON), (PIN_LAT, PIN_LON))
        self.assertEqual((loc["home_km"], loc["home_via"]), (km, via))
        self.assertGreater(loc["home_km"], 0)

        status, _ = self.send("DELETE", "/api/home", {})
        self.assertEqual(status, 200)
        status, got = self.get("/api/home")
        self.assertIsNone(got["home"])
        status, loc = self.get("/api/car/location")
        self.assertIsNone(loc["home_km"])
        self.assertIsNone(loc["home_via"])
        self.assertEqual(loc["lat"], CAR_LAT)
        status, err = self.send("DELETE", "/api/home", {})
        self.assertEqual(status, 404)

        conn = self._open()
        after = self._snapshot(conn)
        self.assertEqual(after["tables"], self._kept["tables"])
        self.assertEqual(after["places_cols"], self._kept["places_cols"])
        self.assertEqual(after["trips"], self._kept["trips"])
        self.assertEqual(after["charges"], self._kept["charges"])
        self.assertEqual(after["ledger"], self._kept["ledger"])
        self.assertEqual(after["routes"], self._kept["routes"])
        self.assertEqual(after["other"], self._kept["other"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM places WHERE TRIM(name) = '집'").fetchone()[0], 0)

    def test_missing_location_and_origin(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM signals WHERE name = 'Location'")
        conn.commit()
        conn.close()
        status, data = self.get("/api/car/location")
        self.assertEqual(status, 200)
        self.assertIsNone(data["lat"])
        self.assertIsNone(data["lon"])
        self.assertIsNone(data["received_at"])
        self.assertIsNone(data["home_km"])
        self.assertIs(data["located_at_home"], False)
        status, err = self.send("PUT", "/api/home", {"use_last": True})
        self.assertEqual(status, 400)
        self.assertIn("마지막 위치", err["error"])
        status, made = self.send("PUT", "/api/home", {"lat": PIN_LAT, "lon": PIN_LON})
        self.assertEqual(status, 201)
        self.assertEqual((made["home"]["lat"], made["home"]["lon"]), (PIN_LAT, PIN_LON))
        status, denied = self.send("PUT", "/api/home", {"lat": PIN_LAT, "lon": PIN_LON}, origin=None)
        self.assertEqual(status, 403)
        status, denied = self.send(
            "DELETE", "/api/home", {}, origin="http://127.0.0.1:1",
        )
        self.assertEqual(status, 403)
        status, still = self.get("/api/home")
        self.assertIsNotNone(still["home"])
        status, err = self.send("PUT", "/api/home", {"lat": 91, "lon": 0})
        self.assertEqual(status, 400)

    def test_road_distance_reuses_home_leg(self) -> None:
        conn = connect(self.db)
        conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES (?, '집', ?, ?, 150)",
            (VID, PIN_LAT, PIN_LON),
        )
        conn.commit()
        os.environ["TESLIFE_ROUTE_MATCH"] = "osrm-local"
        os.environ["TESLIFE_OSRM_URL"] = "http://127.0.0.1:9"
        called: list[str] = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"routes": [{"distance": 12345}]}).encode()

        def route(req, timeout=5):
            called.append(req.full_url)
            return _Resp()

        data = car_location_payload(conn, VID, route_opener=route)
        conn.close()
        self.assertEqual(len(called), 1)
        self.assertTrue(called[0].startswith("http://127.0.0.1:9/"))
        self.assertNotIn(called[0], json.dumps(data))
        self.assertEqual(data["home_km"], 12.3)
        self.assertEqual(data["home_via"], "도로")
        self.assertEqual(data["located_at_home"], False)


if __name__ == "__main__":
    unittest.main()
