"""v1.14.9: short trips use speed or the path, and the screen shows meters."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import (
    MIN_TRIP_KM,
    ODO_STEP_KM,
    PROCESS_VERSION,
    Events,
    connect,
    format_ts,
    process_db,
    trip_distance_km,
)
from web.notice_view import format_trip_distance, present, trip_end_summary
from tools.backfill_trip_km import backfill, format_line

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
T0 = datetime(2026, 11, 16, 8, 46, 28, tzinfo=timezone.utc)


def _ev(rows):
    return Events(rows)


class DistanceTests(unittest.TestCase):
    def test_a_full_odometer_step_is_kept(self) -> None:
        end = T0 + timedelta(minutes=20)
        rows = [
            (T0, "Odometer", 10.0),
            (T0, "VehicleSpeed", 40.0),
            (end, "Odometer", 15.0),
            (end, "VehicleSpeed", 0.0),
        ]
        km, source = trip_distance_km(_ev(rows), T0, end)
        self.assertEqual(source, "odometer")
        self.assertAlmostEqual(km, 5.0, places=3)
        self.assertGreaterEqual(5.0, ODO_STEP_KM)

    def test_speed_fills_a_zero_odometer(self) -> None:
        end = T0 + timedelta(seconds=80)
        rows = [
            (T0, "Odometer", 100.0),
            (T0, "VehicleSpeed", 0.0),
            (T0 + timedelta(seconds=10), "VehicleSpeed", 11.0),
            (T0 + timedelta(seconds=70), "VehicleSpeed", 11.0),
            (end, "Odometer", 100.0),
            (end, "VehicleSpeed", 0.0),
        ]
        km, source = trip_distance_km(_ev(rows), T0, end)
        self.assertEqual(source, "speed")
        self.assertAlmostEqual(km, 0.214, delta=0.02)

    def test_a_long_gap_is_capped(self) -> None:
        end = T0 + timedelta(minutes=30)
        rows = [
            (T0, "Odometer", 1.0),
            (T0, "VehicleSpeed", 100.0),
            (end, "Odometer", 1.0),
            (end, "VehicleSpeed", 100.0),
        ]
        km, source = trip_distance_km(_ev(rows), T0, end)
        self.assertEqual(source, "speed")
        self.assertLess(km, 100.0 * 120.0 / 3600.0 + 0.01)

    def test_gps_is_used_when_speed_is_missing(self) -> None:
        end = T0 + timedelta(seconds=80)
        rows = [
            (T0, "Odometer", 100.0),
            (T0, "Location", {"lat": 37.5, "lon": 127.0}),
            (T0 + timedelta(seconds=40), "Location", {"lat": 37.5003, "lon": 127.0}),
            (end, "Odometer", 100.0),
            (end, "Location", {"lat": 37.5006, "lon": 127.0}),
        ]
        km, source = trip_distance_km(_ev(rows), T0, end)
        self.assertEqual(source, "gps")
        self.assertGreater(km, 0.04)
        self.assertLess(km, 0.1)

    def test_a_stored_short_trip_uses_speed_and_hides_efficiency(self) -> None:
        import tempfile

        start = datetime(2026, 11, 1, 8, 46, 28, tzinfo=timezone.utc)
        end = start + timedelta(seconds=80)
        vid = "car-short"
        tmp = tempfile.NamedTemporaryFile(suffix=".db")
        conn = connect(tmp.name)
        signals = [
            (start - timedelta(seconds=30), "Gear", "P"),
            (start, "Gear", "D"),
            (start, "Odometer", 100.0),
            (start, "VehicleSpeed", 0.0),
            (start, "LifetimeEnergyUsed", 10.0),
            (start + timedelta(seconds=10), "VehicleSpeed", 4.0),
            (start + timedelta(seconds=70), "VehicleSpeed", 4.0),
            (end, "Gear", "P"),
            (end, "Odometer", 100.0),
            (end, "VehicleSpeed", 0.0),
            (end, "LifetimeEnergyUsed", 10.02),
        ]
        for ts, name, value in signals:
            conn.execute(
                "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, ?, ?)",
                (vid, format_ts(ts), name, __import__("json").dumps(value)),
            )
        conn.commit()
        conn.close()
        process_db(tmp.name, seed=False)
        conn = connect(tmp.name)
        row = conn.execute("SELECT km, km_source, km_per_kwh FROM trips").fetchone()
        conn.close()
        self.assertEqual(row[1], "speed")
        self.assertGreater(row[0], 0.05)
        self.assertLess(row[0], MIN_TRIP_KM)
        self.assertIsNone(row[2])
        self.assertEqual(PROCESS_VERSION, "25")


class DisplayTests(unittest.TestCase):
    def test_under_one_kilometre_is_meters(self) -> None:
        self.assertEqual(format_trip_distance(0.066), "66m")
        self.assertEqual(format_trip_distance(5.5), "5.5 km")
        summary = trip_end_summary(
            "66m",
            [["거리", "66m"], ["시간", "1분"], ["배터리", "40%"]],
        )
        self.assertIn("66m 이동했어요", summary)
        self.assertNotIn("달렸어요", summary)
        self.assertNotIn("자리를 옮겼어요", summary)
        view = present({
            "id": "trip_end:1",
            "title": "주행을 마쳤어요",
            "body": "66m",
            "details": [["거리", "66m"], ["시간", "1분"]],
        })
        self.assertEqual(view["title"], "자리를 옮겼어요")
        self.assertNotIn(view["title"], view["summary"])
        long = trip_end_summary("5.5 km", [["거리", "5.5 km"]])
        self.assertEqual(long, "5.5km 달렸어요. 도착 기록을 앱에서 볼 수 있어요")
        self.assertIn("function fmtTripDistance", HTML)
        self.assertIn('version: "1.14.9"', HTML)


class BackfillTests(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".db")
        conn = connect(tmp.name)
        vid = "car-short"
        start = format_ts(T0)
        end = format_ts(T0 + timedelta(seconds=80))
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, kwh) VALUES (?, ?, ?, 0, 0.02)",
            (vid, start, end),
        )
        for offset, speed in ((10, 11.0), (40, 11.0), (70, 11.0)):
            conn.execute(
                "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'VehicleSpeed', ?)",
                (vid, format_ts(T0 + timedelta(seconds=offset)), __import__("json").dumps(speed)),
            )
        conn.commit()
        before = tuple(conn.execute("SELECT km, km_source FROM trips").fetchone())
        rows = backfill(conn, apply=False)
        conn.row_factory = None
        after = tuple(conn.execute("SELECT km, km_source FROM trips").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(rows[0]["source"], "speed")
        self.assertNotIn("37.", format_line(rows[0]))
        conn.close()
