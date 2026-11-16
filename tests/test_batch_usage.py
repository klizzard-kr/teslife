"""Usage-reset batch: brief parks join one trip, window alert skips camping,
Aware is 주의, AC 14 h status, route points carry speed and gear only."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from ledger.consumables import SEED_ITEMS, list_items
from ledger.manuals import DOC_LINKS
from process import _find_trips, _load_events, process_db
from process.alerts import ac_park14h_status, check_open_after_park
from tests.test_map import T0, VID, sig
from tests.test_park14h import Park14hTests
from web import make_server, serve_in_thread


def _ev(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE signals (vehicle_id TEXT, ts TEXT, name TEXT, value TEXT, PRIMARY KEY (vehicle_id, ts, name))")
    ingest.store_signals(conn, rows)
    from process import ensure_process_schema
    ensure_process_schema(conn)
    return _load_events(conn, VID)


class TripMergeTests(unittest.TestCase):
    def _rows(self, pairs):
        # pairs: (minute offset, gear) plus a slow odometer so a real drive is kept
        base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        rows = []
        odo = 100.0
        for minute, gear in pairs:
            t = base + timedelta(minutes=minute)
            if gear != "P":
                odo += 1.0
            rows.append(sig(t, "Gear", gear))
            rows.append(sig(t, "Odometer", odo))
        return rows

    def test_park_of_three_minutes_is_one_trip(self) -> None:
        ev = _ev(self._rows([(0, "P"), (1, "D"), (10, "P"), (13, "D"), (20, "P")]))
        trips, ignored = _find_trips(ev, datetime(2026, 11, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(ignored, 0)
        self.assertEqual(len(trips), 1)
        self.assertEqual((trips[0][1] - trips[0][0]).total_seconds(), 19 * 60)

    def test_park_over_three_minutes_splits(self) -> None:
        ev = _ev(self._rows([(0, "D"), (10, "P"), (14, "D"), (20, "P")]))
        trips, _ = _find_trips(ev, datetime(2026, 11, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(len(trips), 2)

    def test_lock_cycle_does_not_split(self) -> None:
        base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        rows = self._rows([(0, "D"), (30, "P")])
        rows += [sig(base + timedelta(minutes=5), "Locked", False),
                 sig(base + timedelta(minutes=6), "Locked", True),
                 sig(base + timedelta(minutes=7), "DoorState", "DriverFrontOpen"),
                 sig(base + timedelta(minutes=8), "DoorState", "DriverFrontClosed")]
        ev = _ev(rows)
        trips, _ = _find_trips(ev, base + timedelta(hours=2))
        self.assertEqual(len(trips), 1)


class WindowCampTests(unittest.TestCase):
    def _send(self, keeper):
        t = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
        rows = [
            sig(t, "Gear", "P"), sig(t, "Locked", True),
            sig(t, "FdWindow", "PartiallyOpen"), sig(t, "ClimateKeeperMode", keeper),
        ]
        conn = ingest.connect(":memory:")
        ingest.store_signals(conn, rows)
        from process import ensure_process_schema
        ensure_process_schema(conn)
        ev = _load_events(conn, VID)
        sent = []
        check_open_after_park(conn, VID, ev, t, lambda p: sent.append(p) or (True, "ok"))
        conn.close()
        return sent

    def test_camping_skips_open_window_pet_mode_does_not(self) -> None:
        self.assertEqual(self._send("Party"), [])
        sent = self._send("Dog")
        self.assertEqual(len(sent), 1)
        self.assertIn("운전석 창문", sent[0]["message"])
        self.assertNotIn("센트리", sent[0]["message"])


class Park14hApiTests(unittest.TestCase):
    def test_status_active_until_deadline_and_silent_after_move(self) -> None:
        from process.alerts import park_deadline
        from tests.test_park14h import START, VID as PVID
        case = Park14hTests()
        case.setUp()
        try:
            st = ac_park14h_status(case.conn, PVID, START, _load_events(case.conn, PVID))
            self.assertTrue(st["active"])
            self.assertEqual(st["deadline_ts"], park_deadline(START).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertRegex(st["deadline_kst"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            self.assertNotIn("lat", st)
            case.sig(START + timedelta(minutes=10), "Gear", "D")
            st = ac_park14h_status(case.conn, PVID, START + timedelta(minutes=20), _load_events(case.conn, PVID))
            self.assertFalse(st["active"])
            self.assertNotIn("deadline_ts", st)
        finally:
            case.tearDown()


class ManualLinkTests(unittest.TestCase):
    def test_every_seed_item_has_an_official_tesla_page(self) -> None:
        ids = {item["id"] for item in SEED_ITEMS}
        self.assertEqual(set(DOC_LINKS), ids)
        diy = {"cons-cabin-filter", "cons-hepa-filter", "cons-wiper", "cons-washer", "cons-brake-fluid"}
        for iid, links in DOC_LINKS.items():
            self.assertTrue(links)
            for link in links:
                self.assertTrue(link["official"])
                self.assertTrue(link["href"].startswith("https://www.tesla.com/") or link["href"].startswith("https://service.tesla.com/"))
                self.assertNotIn("비공식", link["label"])
            if iid in diy:
                self.assertTrue(any(link.get("kind") in ("inspect", "replace") for link in links))

    def test_api_items_include_docs(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from ledger import ensure_schema
        from ledger.consumables import seed_consumables
        ensure_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS signals (vehicle_id TEXT, ts TEXT, name TEXT, value TEXT)")
        conn.execute("INSERT INTO signals (vehicle_id, ts, name, value) VALUES ('v', '2026-11-01T00:00:00Z', 'Gear', '\"P\"')")
        seed_consumables(conn)
        items = list_items(conn, "v")
        cabin = next(i for i in items if i["id"] == "cons-cabin-filter")
        self.assertTrue(any("필터 교체" in d["label"] for d in cabin["docs"]))
        conn.close()


class RoutePointFieldTests(unittest.TestCase):
    def test_speed_and_gear_stored_autopilot_absent(self) -> None:
        from tests.test_map import drive, scenario
        rows = scenario()
        # a reverse hop and a speed sample on the first drive
        t = T0 + timedelta(minutes=6)
        rows += [sig(t, "Gear", "R"), sig(t, "VehicleSpeed", 12.0),
                 sig(t + timedelta(seconds=20), "Gear", "D"), sig(t + timedelta(seconds=20), "VehicleSpeed", 40.0)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "p.db")
        conn = ingest.connect(db)
        ingest.store_signals(conn, rows)
        conn.close()
        process_db(db, seed=False)
        conn = sqlite3.connect(db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(trip_points)")]
            self.assertIn("speed_kph", cols)
            self.assertIn("gear", cols)
            self.assertNotIn("ap", cols)
            gears = [r[0] for r in conn.execute("SELECT DISTINCT gear FROM trip_points")]
            self.assertIn("R", gears)
            speeds = [r[0] for r in conn.execute("SELECT speed_kph FROM trip_points WHERE speed_kph IS NOT NULL")]
            self.assertTrue(speeds)
        finally:
            conn.close()
        server = make_server(db, port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/map/routes?month=2026-11", timeout=5) as r:
                body = json.loads(r.read())
        finally:
            server.shutdown()
            server.server_close()
        blob = json.dumps(body)
        self.assertNotIn("autopilot", blob)
        self.assertNotIn('"ap"', blob)
        points = body["routes"][-1]["points"]
        self.assertTrue(any(p.get("gear") == "R" for p in points))
        self.assertTrue(any("speed_kph" in p for p in points))
        self.assertTrue(all("ap" not in p for p in points))


class PageCopyTests(unittest.TestCase):
    def test_map_controls_and_no_sentry_word(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for word in ("방향", "이벤트", "기본", "속도", "오토파일럿", "주의"):
            self.assertIn(word, html)
        self.assertNotIn("센트리", html)
        self.assertIn("data-mode=\"ap\"", html)
        self.assertIn("teslife-map-prefs", html)
