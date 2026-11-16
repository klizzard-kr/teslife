"""Trip 쓴 전력 and 전비 follow the battery drop, not LifetimeEnergyUsed."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import PROCESS_VERSION, connect, process_db

VID = "VINNET"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class NetTripTests(unittest.TestCase):
    def store(self, rows: list[tuple[datetime, str, object]]) -> list[dict]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": iso(ts), "name": name, "value": value,
        } for ts, name, value in rows])
        conn.close()
        process_db(db, seed=False, now=datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc))
        conn = connect(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT start_ts, km, kwh, km_per_kwh, kwh_estimated, regen_ratio, "
                "start_lifetime_kwh, end_lifetime_kwh FROM trips ORDER BY start_ts"
            ).fetchall()]
        finally:
            conn.close()

    def test_version(self) -> None:
        self.assertEqual(PROCESS_VERSION, "24")

    def test_long_trip_is_level_drop_times_per(self) -> None:
        # 40.7 km shape: gross 13.79, level 80 → 61.1, remaining at the end 49.
        t0 = datetime(2026, 11, 6, 0, 10, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=50)
        rows = [
            (t0 - timedelta(minutes=1), "Gear", "P"),
            (t0, "Gear", "D"), (t0, "VehicleSpeed", 40), (t0, "Odometer", 1000),
            (t0, "LifetimeEnergyUsed", 100), (t0, "BatteryLevel", 80), (t0, "EnergyRemaining", 60),
            (t1, "Gear", "P"), (t1, "Odometer", 1040.7), (t1, "LifetimeEnergyUsed", 113.79),
            (t1, "BatteryLevel", 61.1), (t1, "EnergyRemaining", 49),
        ]
        trip = self.store(rows)[0]
        per = 49 / 61.1
        net = (80 - 61.1) * per
        self.assertAlmostEqual(trip["end_lifetime_kwh"] - trip["start_lifetime_kwh"], 13.79, places=2)
        self.assertAlmostEqual(trip["kwh"], net, places=2)
        self.assertAlmostEqual(trip["km_per_kwh"], 40.7 / net, places=2)
        self.assertEqual(trip["kwh_estimated"], 0)

    def test_short_and_small_drop_are_marked_and_tiny_hides_efficiency(self) -> None:
        t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=20)
        rows = [
            (t0 - timedelta(minutes=1), "Gear", "P"),
            (t0, "Gear", "D"), (t0, "VehicleSpeed", 30), (t0, "Odometer", 2000),
            (t0, "LifetimeEnergyUsed", 50), (t0, "BatteryLevel", 70), (t0, "EnergyRemaining", 42),
            (t1, "Gear", "P"), (t1, "Odometer", 2003), (t1, "LifetimeEnergyUsed", 51),
            (t1, "BatteryLevel", 69.4), (t1, "EnergyRemaining", 41.6),
        ]
        trip = self.store(rows)[0]
        self.assertEqual(trip["kwh_estimated"], 1)  # 3 km and 0.6 %p
        self.assertIsNotNone(trip["km_per_kwh"])
        tiny0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        tiny1 = tiny0 + timedelta(minutes=3)
        tiny = [
            (tiny0 - timedelta(minutes=1), "Gear", "P"),
            (tiny0, "Gear", "D"), (tiny0, "VehicleSpeed", 5), (tiny0, "Odometer", 3000),
            (tiny0, "LifetimeEnergyUsed", 80), (tiny0, "BatteryLevel", 60), (tiny0, "EnergyRemaining", 36),
            (tiny1, "Gear", "P"), (tiny1, "Odometer", 3000.05), (tiny1, "LifetimeEnergyUsed", 80.05),
            (tiny1, "BatteryLevel", 59.9), (tiny1, "EnergyRemaining", 35.9),
        ]
        short = self.store(tiny)[0]
        self.assertLess(short["km"], 0.1)
        self.assertIsNone(short["km_per_kwh"])
        self.assertEqual(short["kwh_estimated"], 1)

    def test_no_drop_uses_recent_ratio_then_default(self) -> None:
        # First a long trip so a ratio exists, then a rise (regen downhill).
        a0 = datetime(2026, 11, 7, 0, 10, tzinfo=timezone.utc)
        a1 = a0 + timedelta(minutes=40)
        b0 = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
        b1 = b0 + timedelta(minutes=30)
        rows = [
            (a0 - timedelta(minutes=1), "Gear", "P"),
            (a0, "Gear", "D"), (a0, "VehicleSpeed", 40), (a0, "Odometer", 1000),
            (a0, "LifetimeEnergyUsed", 10), (a0, "BatteryLevel", 80), (a0, "EnergyRemaining", 48),
            (a1, "Gear", "P"), (a1, "Odometer", 1020), (a1, "LifetimeEnergyUsed", 20),
            (a1, "BatteryLevel", 70), (a1, "EnergyRemaining", 42),
            (b0, "Gear", "D"), (b0, "VehicleSpeed", 40), (b0, "Odometer", 1020),
            (b0, "LifetimeEnergyUsed", 20), (b0, "BatteryLevel", 70), (b0, "EnergyRemaining", 42),
            (b1, "Gear", "P"), (b1, "Odometer", 1035), (b1, "LifetimeEnergyUsed", 24),
            (b1, "BatteryLevel", 72), (b1, "EnergyRemaining", 43),
        ]
        first, second = self.store(rows)
        net = (80 - 70) * (42 / 70)
        ratio = net / 10
        self.assertAlmostEqual(first["kwh"], net, places=2)
        self.assertEqual(first["kwh_estimated"], 0)
        self.assertAlmostEqual(second["kwh"], 4 * ratio, places=2)
        self.assertEqual(second["kwh_estimated"], 1)
        # No history and no drop: 0.75 times the lifetime rise.
        c0 = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
        c1 = c0 + timedelta(minutes=20)
        alone = self.store([
            (c0 - timedelta(minutes=1), "Gear", "P"),
            (c0, "Gear", "D"), (c0, "VehicleSpeed", 30), (c0, "Odometer", 5000),
            (c0, "LifetimeEnergyUsed", 5),
            (c1, "Gear", "P"), (c1, "Odometer", 5012), (c1, "LifetimeEnergyUsed", 9),
        ])[0]
        self.assertAlmostEqual(alone["kwh"], 4 * 0.75, places=2)
        self.assertEqual(alone["kwh_estimated"], 1)

    def test_later_pass_reuses_a_stored_ratio(self) -> None:
        a0 = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
        a1 = a0 + timedelta(minutes=40)
        first = [
            (a0 - timedelta(minutes=1), "Gear", "P"),
            (a0, "Gear", "D"), (a0, "VehicleSpeed", 40), (a0, "Odometer", 1000),
            (a0, "LifetimeEnergyUsed", 10), (a0, "BatteryLevel", 80), (a0, "EnergyRemaining", 48),
            (a1, "Gear", "P"), (a1, "Odometer", 1020), (a1, "LifetimeEnergyUsed", 20),
            (a1, "BatteryLevel", 70), (a1, "EnergyRemaining", 42),
        ]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "s.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": iso(ts), "name": name, "value": value,
        } for ts, name, value in first])
        conn.close()
        process_db(db, seed=False, now=datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))
        b0 = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
        b1 = b0 + timedelta(minutes=30)
        second = [
            (b0 - timedelta(minutes=1), "Gear", "P"),
            (b0, "Gear", "D"), (b0, "VehicleSpeed", 40), (b0, "Odometer", 1020),
            (b0, "LifetimeEnergyUsed", 20),
            (b1, "Gear", "P"), (b1, "Odometer", 1035), (b1, "LifetimeEnergyUsed", 24),
        ]
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": iso(ts), "name": name, "value": value,
        } for ts, name, value in second])
        # The lookback would still reload the first trip. Move the anchor past it
        # so this pass has to read the stored ratio.
        conn.execute(
            "UPDATE process_meta SET value = ? WHERE key = ?",
            (iso(b0 - timedelta(hours=1)), f"anchor:{VID}"),
        )
        conn.commit()
        conn.close()
        process_db(db, seed=False, now=datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))
        conn = connect(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT kwh, kwh_estimated, start_lifetime_kwh, end_lifetime_kwh FROM trips ORDER BY start_ts"
            ).fetchall()]
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        ratio = rows[0]["kwh"] / (rows[0]["end_lifetime_kwh"] - rows[0]["start_lifetime_kwh"])
        self.assertAlmostEqual(rows[1]["kwh"], 4 * ratio, places=2)
        self.assertNotAlmostEqual(rows[1]["kwh"], 4 * 0.75, places=2)
        self.assertEqual(rows[1]["kwh_estimated"], 1)
