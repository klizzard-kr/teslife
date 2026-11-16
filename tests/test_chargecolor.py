"""Battery chart colours for a trip / charge that has not ended yet.

battery_hourly flags come from finished rows in trips / charges, so tonight's
charge (2026-10-30 20:54 KST, still running) showed the 21시 bar in the park
colour. The API now also marks the hours of the open session from live signals.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
from process import process_db
from web import battery_daily, battery_hourly, open_span_hours

from tests.test_charge import T0, VID, plug_in, sig, unplug


class ChargeColourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        return conn

    def _hours(self, now) -> dict[int, dict]:
        conn = self._conn()
        try:
            out = battery_hourly(conn, VID, "2026-10-30", now=now)
        finally:
            conn.close()
        return {h["hour"]: h for h in out["hours"]}

    def test_open_charge_hours_are_charging(self) -> None:
        s, start = plug_in()  # charging from 20:54:46 KST, samples until 21:54
        self._store(s)
        now = start + timedelta(hours=1, minutes=2)  # 21:56 KST, still charging
        conn = self._conn()
        try:
            stored = {r["hour"]: r["charging"] for r in conn.execute(
                "SELECT hour, charging FROM battery_hourly WHERE vehicle_id = ?", (VID,))}
            live = open_span_hours(conn, VID, now)
        finally:
            conn.close()
        # the stored table alone says "not charging" (the bug)
        self.assertEqual(stored.get("2026-10-30T21"), 0)
        self.assertEqual(live["charging"], {"2026-10-30T20", "2026-10-30T21"})
        self.assertEqual(live["driving"], set())
        hours = self._hours(now)
        self.assertTrue(hours[20]["charging"])
        self.assertTrue(hours[21]["charging"])
        self.assertFalse(hours[21]["driving"])
        self.assertFalse(hours[22]["charging"])  # no data yet -> no bar

    def test_closed_charge_comes_from_table(self) -> None:
        s, start = plug_in()
        self._store(s + unplug(start))
        now = start + timedelta(hours=3)
        conn = self._conn()
        try:
            live = open_span_hours(conn, VID, now)
        finally:
            conn.close()
        self.assertEqual(live["charging"], set())
        hours = self._hours(now)
        self.assertTrue(hours[20]["charging"])
        self.assertTrue(hours[21]["charging"])

    def test_before_plug_in_is_parked(self) -> None:
        s, _ = plug_in()
        self._store([x for x in s if x["timestamp"] <= "2026-10-30T11:53:00Z"])
        conn = self._conn()
        try:
            live = open_span_hours(conn, VID, T0 + timedelta(minutes=1))
        finally:
            conn.close()
        self.assertEqual(live, {"driving": set(), "charging": set()})
        self.assertFalse(self._hours(T0 + timedelta(minutes=1))[20]["charging"])

    def test_open_trip_hours_are_driving(self) -> None:
        t = T0 - timedelta(hours=2)  # 18:52 KST
        s = [sig(t, "Gear", "P"), sig(t, "BatteryLevel", 50.0), sig(t, "Odometer", 900.0),
             sig(t + timedelta(minutes=1), "Gear", "D")]
        for i in range(1, 5):
            s += [sig(t + timedelta(minutes=10 * i), "BatteryLevel", 50.0 - i),
                  sig(t + timedelta(minutes=10 * i), "Odometer", 900.0 + 3 * i)]
        self._store(s)
        now = t + timedelta(minutes=45)  # 19:37 KST, still in D
        hours = self._hours(now)
        self.assertTrue(hours[18]["driving"])
        self.assertTrue(hours[19]["driving"])
        conn = self._conn()
        try:
            week = battery_daily(conn, VID, "2026-10-24", "2026-10-30", now=now)
        finally:
            conn.close()
        self.assertTrue(week["days"][-1]["driving"])
        self.assertFalse(week["days"][-1]["charging"])

    def test_daily_flag_for_open_charge(self) -> None:
        s, start = plug_in()
        self._store(s)
        conn = self._conn()
        try:
            week = battery_daily(conn, VID, "2026-10-24", "2026-10-30", now=start + timedelta(hours=1))
        finally:
            conn.close()
        self.assertTrue(week["days"][-1]["charging"])


if __name__ == "__main__":
    unittest.main()
