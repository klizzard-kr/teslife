""": 14 h limit in an AC (완속) charging spot -> warnings 1 h and 30 min before.

Parking start = charge start - 5 min, deadline = + 14 h. Real case: AC 2026-10-30 20:54:46 KST
-> deadline 10/31 10:49:46 KST.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect as process_connect, format_ts, parse_ts, process_db
from process.alerts import check_park14h, moved_since, park_deadline, park14h_texts, park_texts

VID = "VPARK"
START = parse_ts("2026-10-30T11:54:46Z")          # 20:54:46 KST
DEADLINE = parse_ts("2026-10-31T01:49:46Z")       # 10:49:46 KST


class Park14hTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        self.conn = process_connect(self.db)
        self.sent: list[dict] = []
        self.add_charge(START, "AC")
        self.sig(START - timedelta(minutes=3), "Gear", "P")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def sender(self, payload: dict) -> tuple[bool, str]:
        self.sent.append(payload)
        return True, "http 200"

    def add_charge(self, start: datetime, kind: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, ?, 26.7)",
                              (VID, format_ts(start), format_ts(start + timedelta(hours=4)), kind))

    def sig(self, ts: datetime, name: str, value) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, ?, ?)",
                              (VID, format_ts(ts), name, json.dumps(value)))

    def run_at(self, t: datetime) -> list[str]:
        n = len(self.sent)
        check_park14h(self.conn, VID, None, t, self.sender)
        return [p["kind"] for p in self.sent[n:]]

    def test_deadline_and_texts(self) -> None:
        self.assertEqual(park_deadline(START), DEADLINE)
        t = park14h_texts(DEADLINE)
        self.assertEqual(t["park14h_1h"], "완속 충전 자리 14시간 제한: 10:49까지 차를 옮겨 주세요. 1시간 남았어요")
        self.assertEqual(t["park14h_30m"], "완속 충전 자리 14시간 제한: 10:49까지 차를 옮겨 주세요. 30분 남았어요")
        self.assertEqual(t["park14h_over"], "완속 충전 자리 14시간 제한: 10:49에 14시간이 지났어요. 얼른 차를 옮겨 주세요")
        for s in t.values():
            self.assertNotIn("센트리", s)

    def test_two_warnings_once_each(self) -> None:
        self.assertEqual(self.run_at(DEADLINE - timedelta(hours=1, minutes=1)), [])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=59)), ["park14h_1h"])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=40)), [])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=29)), ["park14h_30m"])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=10)), [])
        self.assertEqual(self.run_at(DEADLINE + timedelta(minutes=5)), [])  # warned already: no extra
        p = self.sent[0]
        self.assertEqual(p["data"]["deadline_ts"], "2026-10-31T01:49:46Z")
        self.assertEqual(p["data"]["parking_start_ts"], "2026-10-30T11:49:46Z")
        self.assertIn("10:49까지", p["message"])

    def test_restart_does_not_repeat(self) -> None:
        self.run_at(DEADLINE - timedelta(minutes=50))
        self.conn.close()
        self.conn = process_connect(self.db)  # state is in alert_state, not memory
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=45)), [])

    def test_late_start_skips_to_30m(self) -> None:
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=20)), ["park14h_30m"])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=5)), [])

    def test_past_deadline_once_then_quiet(self) -> None:
        self.assertEqual(self.run_at(DEADLINE + timedelta(minutes=10)), ["park14h_over"])
        self.assertEqual(self.run_at(DEADLINE + timedelta(minutes=20)), [])

    def test_long_past_is_silent(self) -> None:
        self.assertEqual(self.run_at(DEADLINE + timedelta(hours=4)), [])

    def test_drive_cancels_even_briefly(self) -> None:
        self.sig(START + timedelta(hours=6), "Gear", "R")
        self.sig(START + timedelta(hours=6, seconds=20), "Gear", "P")
        self.assertTrue(moved_since(self.conn, VID, START))
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=59)), [])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=29)), [])

    def test_new_trip_cancels(self) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, 0.3)",
                              (VID, format_ts(START + timedelta(hours=8)), format_ts(START + timedelta(hours=8, minutes=10))))
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=59)), [])

    def test_charge_end_alone_does_not_cancel(self) -> None:
        self.sig(START + timedelta(hours=4), "DetailedChargeState", "Complete")
        self.sig(START + timedelta(hours=5), "Gear", "P")
        self.assertFalse(moved_since(self.conn, VID, START))
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=59)), ["park14h_1h"])

    def test_gear_before_charge_does_not_count(self) -> None:
        self.sig(START - timedelta(minutes=4), "Gear", "D")
        self.assertFalse(moved_since(self.conn, VID, START))

    def test_dc_one_hour_rule(self) -> None:
        """: DC (급속) spots: 1 h, warnings 15 and 5 min before."""
        later = START + timedelta(days=1)                 # 10/31 20:54:46 KST
        self.add_charge(later, "DC")
        dl = park_deadline(later, "DC")
        self.assertEqual(dl, later - timedelta(minutes=5) + timedelta(hours=1))   # 21:49:46 KST
        self.assertEqual(self.run_at(dl - timedelta(minutes=16)), [])
        self.assertEqual(self.run_at(dl - timedelta(minutes=14)), ["parkdc_15m"])
        self.assertEqual(self.run_at(dl - timedelta(minutes=10)), [])
        self.assertEqual(self.run_at(dl - timedelta(minutes=4)), ["parkdc_5m"])
        self.assertEqual(self.run_at(dl + timedelta(minutes=2)), [])
        msgs = [p["message"] for p in self.sent]
        self.assertEqual(msgs, ["급속 충전 자리 1시간 제한: 21:49까지 차를 옮겨 주세요. 15분 남았어요",
                                "급속 충전 자리 1시간 제한: 21:49까지 차를 옮겨 주세요. 5분 남았어요"])
        self.assertEqual(self.sent[0]["data"]["charge_kind"], "DC")

    def test_dc_texts_and_passed(self) -> None:
        later = START + timedelta(days=1)
        self.add_charge(later, "DC")
        dl = park_deadline(later, "DC")
        self.assertEqual(park_texts(dl, "DC")["parkdc_over"], "급속 충전 자리 1시간 제한: 21:49에 1시간이 지났어요. 얼른 차를 옮겨 주세요")
        self.assertEqual(self.run_at(dl + timedelta(minutes=10)), ["parkdc_over"])
        self.assertEqual(self.run_at(dl + timedelta(minutes=15)), [])

    def test_dc_long_past_silent_and_cancel(self) -> None:
        later = START + timedelta(days=1)
        self.add_charge(later, "DC")
        dl = park_deadline(later, "DC")
        self.assertEqual(self.run_at(dl + timedelta(minutes=31)), [])
        self.sig(later + timedelta(minutes=30), "Gear", "D")
        self.assertEqual(self.run_at(dl - timedelta(minutes=14)), [])

    def test_ac_states_unchanged_by_dc_rule(self) -> None:
        """Redeploy must not resend an AC warning already sent (same alert_state keys)."""
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=59)), ["park14h_1h"])
        self.assertEqual(self.run_at(DEADLINE - timedelta(minutes=50)), [])

    def test_open_session_remembered_for_sleep(self) -> None:
        """A charge still in progress (no charges row yet) is remembered in alert_state."""
        later = START + timedelta(days=2)
        with self.conn:
            self.conn.execute("INSERT INTO alert_state (vehicle_id, kind, state, updated_ts) VALUES (?, 'park14h_session', ?, ?)",
                              (VID, format_ts(later) + "|AC", format_ts(later)))
        self.assertEqual(self.run_at(park_deadline(later) - timedelta(minutes=59)), ["park14h_1h"])

    def test_deferred_path_runs_without_new_signals(self) -> None:
        self.conn.close()
        process_db(self.db, seed=False)  # process once so the next run is "up to date"
        sent: list[dict] = []
        process_db(self.db, seed=False, alerts=True, now=DEADLINE - timedelta(minutes=55),
                   sender=lambda p: (sent.append(p) or True, "http 200"))
        self.conn = process_connect(self.db)
        self.assertEqual([p["kind"] for p in sent if p["kind"].startswith("park14h")], ["park14h_1h"])


if __name__ == "__main__":
    unittest.main()
