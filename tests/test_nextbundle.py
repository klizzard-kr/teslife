"""Next-version bundle: three lanes, camping label, admin area, scheduler clock."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import connect as process_connect, process_db
from process.geocode import admin_area
from web.timeline import PARK_DETAIL_KO, day_timeline, park_detail

ROOT = Path(__file__).resolve().parents[1]
VID = "VINNEXT"
T = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)


def H(h: float) -> datetime:
    return T + timedelta(hours=h)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class AdminAreaTests(unittest.TestCase):
    def test_smallest_unit_and_named_place(self) -> None:
        self.assertEqual(admin_area("서울특별시 강남구 역삼1동 테헤란로 152"), "역삼1동")
        self.assertEqual(admin_area("테헤란로 427, 역삼동, 강남구, 서울특별시"), "역삼동")
        self.assertEqual(admin_area("강남구 테헤란로 427"), "강남구")
        self.assertEqual(admin_area("경기도 양평군 서종면"), "서종면")
        self.assertEqual(admin_area("성남시"), "성남시")
        self.assertEqual(admin_area("집"), "집")
        self.assertIsNone(admin_area(None))


class ParkDetailTests(unittest.TestCase):
    def test_climate_overheat_and_unknown(self) -> None:
        self.assertEqual(park_detail(("camp", "sentry"), "On"), "camp")
        self.assertEqual(park_detail(("dog",), None), "pet")
        self.assertEqual(park_detail(("keep",), "Off"), "keep")
        self.assertEqual(park_detail(("parked",), "OverheatProtect"), "overheat")
        self.assertEqual(park_detail(("sentry",), "OverheatProtect"), "overheat")
        self.assertEqual(park_detail(("parked",), None), "unknown")
        self.assertEqual(park_detail(("parked",), "Off"), "parked")
        self.assertIsNone(park_detail(("sentry",), None))
        self.assertIsNone(park_detail(("sentry",), "Off"))
        self.assertNotIn("추정", "".join(PARK_DETAIL_KO.values()))
        self.assertEqual(PARK_DETAIL_KO["camp"], "주차 · 캠핑")


def _pulse(a: float, b: float, rows: list) -> None:
    """Keep the car 'awake' (gaps under 15 min) so the timeline draws bars."""
    t = a
    while t <= b + 1e-9:
        rows.append((H(t), "OutsideTemp", 20.0))
        t += 10 / 60


class OverheatTimelineTests(unittest.TestCase):
    def test_overheat_bar_splits_from_plain_parking(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        rows = [
            (H(0), "Gear", "P"), (H(0), "ClimateKeeperMode", "Off"), (H(0), "SentryMode", "Off"),
            (H(0), "HvacPower", "Off"), (H(0), "LifetimeEnergyUsed", 10.0),
            (H(1), "HvacPower", "OverheatProtect"), (H(1), "LifetimeEnergyUsed", 10.2),
            (H(2), "HvacPower", "Off"), (H(2), "LifetimeEnergyUsed", 10.5),
            (H(3), "LifetimeEnergyUsed", 10.6),
        ]
        _pulse(0, 3, rows)
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(db, seed=False, now=H(4))
        conn = process_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            d = day_timeline(conn, VID, "2026-10-30", now=NOW)
        finally:
            conn.close()
        parked = next(l for l in d["lanes"] if l["key"] == "parked")["bars"]
        details = [(b["detail"], b["title"], b["start_ts"]) for b in parked]
        self.assertEqual(details[0][0], "parked")
        self.assertEqual(details[1][0], "overheat")
        self.assertEqual(details[1][1], "주차 · 과열 방지")
        self.assertEqual(details[1][2], iso(H(1)))
        self.assertEqual(details[2][0], "parked")
        self.assertTrue(all(l["key"] in {"drive", "parked", "sentry"} for l in d["lanes"]))

    def test_missing_hvac_is_unknown(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        rows = [(H(0), "Gear", "P"), (H(0), "SentryMode", "Off"), (H(1), "Gear", "P")]
        _pulse(0, 1, rows)
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(db, seed=False, now=H(2))
        conn = process_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            d = day_timeline(conn, VID, "2026-10-30", now=NOW)
        finally:
            conn.close()
        parked = next(l for l in d["lanes"] if l["key"] == "parked")["bars"]
        self.assertTrue(parked)
        self.assertTrue(all(b["detail"] == "unknown" for b in parked))
        self.assertTrue(all(b["title"] == "주차 · 미확인" for b in parked))


class OverheatHomeTests(unittest.TestCase):
    def test_setting_on_without_cooling_is_not_an_overheat_bar(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        rows = [
            (H(0), "Gear", "P"), (H(0), "SentryMode", "Off"), (H(0), "ClimateKeeperMode", "Off"),
            (H(0), "CabinOverheatProtectionMode", "On"), (H(0), "HvacPower", "Off"),
            (H(0), "LifetimeEnergyUsed", 10.0), (H(1), "LifetimeEnergyUsed", 10.1),
        ]
        _pulse(0, 1, rows)
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(db, seed=False, now=H(2))
        conn = process_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            d = day_timeline(conn, VID, "2026-10-30", now=NOW)
        finally:
            conn.close()
        parked = next(l for l in d["lanes"] if l["key"] == "parked")["bars"]
        self.assertTrue(parked)
        self.assertTrue(all(b["detail"] == "parked" for b in parked))
        self.assertTrue(all(b["title"] == "주차" for b in parked))

    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_home_words(self) -> None:
        import json
        import subprocess

        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("function signalValue(")
        end = html.index("function setNamedValue(")
        src = html[start:end]
        src += r"""
        const cases = [
          [{CabinOverheatProtectionMode:"Off"}, "꺼짐"],
          [{CabinOverheatProtectionMode:"Off", HvacPower:"Off"}, "꺼짐"],
          [{CabinOverheatProtectionMode:"Off", HvacPower:"OverheatProtect"}, "켜짐"],
          [{CabinOverheatProtectionMode:"On", HvacPower:"Off"}, "대기"],
          [{CabinOverheatProtectionMode:"On", HvacPower:"On"}, "대기"],
          [{CabinOverheatProtectionMode:"FanOnly", HvacPower:"Precondition"}, "대기"],
          [{CabinOverheatProtectionMode:"On"}, "미확인"],
          [{CabinOverheatProtectionMode:"FanOnly"}, "미확인"],
          [{}, "미확인"],
          [{HvacPower:"Off"}, "미확인"],
          [{HvacPower:"OverheatProtect"}, "켜짐"],
          [{CabinOverheatProtectionMode:"On", HvacPower:"OverheatProtect"}, "켜짐"]
        ];
        const live = (sig) => ({signals: Object.fromEntries(Object.entries(sig).map(([k,v]) => [k,{value:v}]))});
        process.stdout.write(JSON.stringify(cases.map(([sig, want]) => [overheatWord(live(sig)), want])));
        """
        out = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        for got, want in out:
            self.assertEqual(got, want)


class HomeBootTests(unittest.TestCase):
    def test_first_page_includes_latest_home_values(self) -> None:
        import json
        import urllib.request

        from web import make_server, serve_in_thread

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        when = "2026-11-10T12:00:00Z"
        rows = [
            ("ClimateKeeperMode", "Party"),
            ("CabinOverheatProtectionMode", "On"),
            ("HvacPower", "Off"),
            ("DetailedChargeState", "Disconnected"),
            ("SentryMode", "Armed"),
            ("BatteryLevel", 61.0),
        ]
        conn = ingest.connect(db)
        ingest.store_signals(conn, [
            {"vehicle_id": VID, "timestamp": when, "name": n, "value": v} for n, v in rows
        ])
        conn.close()
        server = make_server(str(db), host="127.0.0.1", port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        self.assertIn('id="v-climate"></div>', body)
        self.assertIn('id="v-overheat"></div>', body)
        self.assertNotIn('id="v-climate">미확인', body)
        self.assertNotIn('id="v-overheat">미확인', body)
        i = body.index("window.__TESLIFE_BOOT__=")
        raw = body[i + len("window.__TESLIFE_BOOT__="):body.index(";</script>", i)]
        boot = json.loads(raw)
        sig = boot["signals"]
        self.assertEqual(sig["ClimateKeeperMode"]["value"], "Party")
        self.assertEqual(sig["CabinOverheatProtectionMode"]["value"], "On")
        self.assertEqual(sig["HvacPower"]["value"], "Off")
        self.assertEqual(sig["DetailedChargeState"]["value"], "Disconnected")
        self.assertEqual(sig["SentryMode"]["value"], "Armed")
        self.assertEqual(boot["charge_status"], "Disconnected")
        static = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        tail = static[static.rindex("tickTimer"):]
        self.assertLess(tail.index("if (window.__TESLIFE_BOOT__) onLive"), tail.index('fetchJson("/api/live")'))

    def test_boot_omits_signals_the_server_does_not_have(self) -> None:
        import json
        import urllib.request

        from web import make_server, serve_in_thread

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": "2026-11-10T12:00:00Z",
            "name": "BatteryLevel", "value": 50.0,
        }])
        conn.close()
        server = make_server(str(db), host="127.0.0.1", port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        i = body.index("window.__TESLIFE_BOOT__=")
        raw = body[i + len("window.__TESLIFE_BOOT__="):body.index(";</script>", i)]
        sig = json.loads(raw)["signals"]
        self.assertIn("BatteryLevel", sig)
        for name in ("ClimateKeeperMode", "CabinOverheatProtectionMode", "HvacPower", "DetailedChargeState"):
            self.assertNotIn(name, sig)


class CopyTests(unittest.TestCase):
    def test_screen_copy(self) -> None:
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("캠핑(추정)", html)
        self.assertNotIn("충전 대기", html)
        self.assertNotIn("전원 없음", html)
        self.assertNotIn("충전 준비", html)
        self.assertNotIn('id="status-icons"', html)
        self.assertNotIn('id="m-camp"', html)
        self.assertIn('id="m-climate"', html)
        self.assertIn("Party: \"캠핑\"", html)
        self.assertIn('id="mode-sum"', html)
        self.assertIn('"주차 · 실내 온도 유지"', html)
        self.assertIn('"주차 · 펫"', html)
        self.assertIn('"주차 · 캠핑"', html)

