""": tire_readings is a short working window (TIRE_KEEP), not a history."""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from process import ensure_process_schema, TIRE_KEEP, format_ts, prune_tire_readings

VID = "v1"
T0 = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        ensure_process_schema(self.conn)

    def add(self, vid: str, ts: datetime, phase: str = "cold") -> None:
        self.conn.execute("INSERT INTO tire_readings (vehicle_id, ts, fl, fr, rl, rr, outside_temp, phase) "
                          "VALUES (?, ?, 2.7, 2.7, 2.7, 2.7, 10, ?)", (vid, format_ts(ts), phase))

    def test_keeps_window_for_alerts(self) -> None:
        self.assertGreaterEqual(TIRE_KEEP, timedelta(days=9))   # slow leak looks back up to 9 days
        for d in (0, 1, 7, 9, 11, 30, 120):
            self.add(VID, T0 - timedelta(days=d))
        self.add("other", T0 - timedelta(days=60))
        self.assertEqual(prune_tire_readings(self.conn, VID), 3)
        left = [r[0] for r in self.conn.execute("SELECT ts FROM tire_readings WHERE vehicle_id = ? ORDER BY ts", (VID,))]
        self.assertEqual(left, [format_ts(T0 - timedelta(days=d)) for d in (9, 7, 1, 0)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tire_readings WHERE vehicle_id = 'other'").fetchone()[0], 1)

    def test_empty_and_missing_table(self) -> None:
        self.assertEqual(prune_tire_readings(self.conn, VID), 0)
        self.assertEqual(prune_tire_readings(sqlite3.connect(":memory:"), VID), 0)


if __name__ == "__main__":
    unittest.main()
