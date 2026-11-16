"""Speed-rate map events and the legacy-motion migration. No car, no prod db."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, process_db
from process.drop_legacy_motion import LEGACY_SIGNALS, apply_delete, counts, main as drop_main, recompute_events
from process.speed_events import MAX_DT_S, MIN_DT_S, SPEED_RATE_KMH_S, speed_events

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def at(sec: float, kmh: float):
    return (T0 + timedelta(seconds=sec), kmh)


class SpeedEventTests(unittest.TestCase):
    def test_one_second_jump_counts_without_a_two_second_hold(self) -> None:
        # 16 km/h in 1.0 s is 16 km/h/s. The old rule needed 2 s above a g threshold.
        found = speed_events([at(0, 30), at(1, 46), at(2, 46)])
        self.assertEqual(len(found["accel"]), 1)
        self.assertEqual(found["accel"][0][0], T0)
        self.assertAlmostEqual(found["accel"][0][1], 16.0, places=3)
        self.assertEqual(found["turn"], None)

    def test_exact_fifteen_is_excluded_and_past_fifteen_counts(self) -> None:
        """Exactly ±15 km/h/s is not an event. Just past it is."""
        self.assertEqual(SPEED_RATE_KMH_S, 15.0)
        exact_accel = speed_events([at(0, 0), at(1, 15)])
        exact_brake = speed_events([at(0, 30), at(1, 15)])
        under = speed_events([at(0, 0), at(1, 14.99)])
        over_accel = speed_events([at(0, 0), at(1, 15.5)])
        over_brake = speed_events([at(0, 30), at(1, 14.5)])
        self.assertEqual(exact_accel["accel"], [])
        self.assertEqual(exact_accel["brake"], [])
        self.assertEqual(exact_brake["brake"], [])
        self.assertEqual(exact_brake["accel"], [])
        self.assertEqual(under["accel"], [])
        self.assertEqual(len(over_accel["accel"]), 1)
        self.assertAlmostEqual(over_accel["accel"][0][1], 15.5, places=3)
        self.assertEqual(over_accel["brake"], [])
        self.assertEqual(len(over_brake["brake"]), 1)
        self.assertAlmostEqual(over_brake["brake"][0][1], 15.5, places=3)
        self.assertEqual(over_brake["accel"], [])

    def test_short_gap_and_long_gap_and_duplicate_time(self) -> None:
        # 1 mph (1.609 km/h) over 0.2 s would be ~8 km/h/s; over 0.15 s it exceeds 10.
        # MIN_DT drops that quantum. A 3 s gap is not filled in.
        self.assertGreaterEqual(MIN_DT_S, 0.4)
        self.assertLessEqual(MAX_DT_S, 2.5)
        quantum = speed_events([at(0, 30), at(0.15, 31.609)])
        self.assertEqual(quantum["accel"], [])
        gap = speed_events([at(0, 30), at(3, 60)])
        self.assertEqual(gap["accel"], [])
        dup = speed_events([at(0, 30), at(0, 80), at(1, 80)])
        self.assertEqual(dup["accel"], [])

    def test_brake_merge_and_opposite_sign_stay_apart(self) -> None:
        samples = [at(0, 80), at(1, 60), at(2, 40), at(6, 40), at(7, 56)]
        found = speed_events(samples)
        self.assertEqual(len(found["brake"]), 1)
        self.assertAlmostEqual(found["brake"][0][1], 20.0, places=3)
        self.assertEqual(len(found["accel"]), 1)

    def test_no_samples_is_none_so_stored_rows_can_stay(self) -> None:
        self.assertEqual(speed_events([at(0, 10)])["accel"], None)


class MigrationTests(unittest.TestCase):
    def test_delete_is_scoped_and_recompute_uses_speed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "s.db")
        conn = connect(db)
        vid = "V"
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, 1)",
            (vid, "2026-11-01T00:00:00Z", "2026-11-01T00:05:00Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?, ?, ?, 'AC')",
            (vid, "2026-11-01T02:00:00Z", "2026-11-01T03:00:00Z"),
        )
        conn.execute(
            "INSERT INTO trip_points (vehicle_id, trip_start_ts, seq, ts, lat, lon) VALUES (?,?,?,?,?,?)",
            (vid, "2026-11-01T00:00:00Z", 0, "2026-11-01T00:00:00Z", 37.5, 127.0),
        )
        conn.execute(
            "INSERT INTO trip_events (vehicle_id, trip_start_ts, ts, kind, g, lat, lon) VALUES (?,?,?,?,?,?,?)",
            (vid, "2026-11-01T00:00:00Z", "2026-11-01T00:01:00Z", "turn", 0.4, 37.5, 127.0),
        )
        for name in LEGACY_SIGNALS:
            conn.execute(
                "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?,?,?,?)",
                (vid, "2026-11-01T00:00:10Z", name, "1"),
            )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?,?,?,?)",
            (vid, "2026-11-01T00:00:00Z", "VehicleSpeed", "30"),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?,?,?,?)",
            (vid, "2026-11-01T00:00:01Z", "VehicleSpeed", "46"),
        )
        conn.commit()
        plan = counts(conn)
        self.assertEqual(plan["trip_events"], 1)
        self.assertEqual(plan["trips"], 1)
        code = drop_main(["--db", db])
        self.assertEqual(code, 0)
        self.assertEqual(counts(conn)["LongitudinalAcceleration"], 1)  # dry-run
        apply_delete(conn)
        self.assertEqual(counts(conn)["PedalPosition"], 0)
        self.assertEqual(counts(conn)["trip_events"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM signals WHERE name = 'VehicleSpeed'").fetchone()[0], 2)
        recompute_events(conn)
        kinds = [r[0] for r in conn.execute("SELECT kind FROM trip_events")]
        self.assertEqual(kinds, ["accel"])
        self.assertNotIn("turn", kinds)
        conn.close()


class RoadBatchLockTests(unittest.TestCase):
    def test_edge_batches_commit_and_release(self) -> None:
        import process.roads as roads

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "roads.db")
        conn = sqlite3.connect(path, timeout=1)
        conn.execute(
            "CREATE TABLE road_usage (vehicle_id TEXT, year TEXT, a_lat INT, a_lon INT, "
            "b_lat INT, b_lon INT, n INT, PRIMARY KEY (vehicle_id, year, a_lat, a_lon, b_lat, b_lon))"
        )
        conn.commit()
        sqls: list[str] = []
        conn.set_trace_callback(sqls.append)
        old = roads.EDGE_BATCH
        roads.EDGE_BATCH = 2
        try:
            roads._add(conn, "V", "2026", {(i, 0, i, 1) for i in range(5)})
        finally:
            roads.EDGE_BATCH = old
        commits = [s for s in sqls if s.strip().upper() == "COMMIT"]
        self.assertGreaterEqual(len(commits), 3)
        self.assertFalse(conn.in_transaction)
        other = sqlite3.connect(path, timeout=0.5)
        other.execute("INSERT INTO road_usage VALUES ('V','2026',9,9,8,8,1)")
        other.commit()
        self.assertEqual(other.execute("SELECT COUNT(*) FROM road_usage").fetchone()[0], 6)
        other.close()
        conn.close()


class LockReleaseTests(unittest.TestCase):
    def test_failed_write_rolls_back_and_another_connection_can_write(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "l.db")
        conn = sqlite3.connect(path, timeout=1)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        try:
            with conn:
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        self.assertFalse(conn.in_transaction)
        other = sqlite3.connect(path, timeout=0.5)
        other.execute("INSERT INTO t VALUES (2)")
        other.commit()
        self.assertEqual(other.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
        other.close()
        conn.close()
