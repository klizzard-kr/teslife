"""Fix 1: harsh-event units and debounce, estimated regen, trip_end waits for tires."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import Events, _harsh_counts, _load_events, connect as process_connect, process_db
from process.alerts import check_trip_end, trip_end_pending

VID = "VINF1"
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": iso(t), "name": name, "value": value}


def ev_from(rows: list[tuple[float, str, object]]) -> Events:
    out = [(T0 + timedelta(seconds=s), n, v) for s, n, v in rows]
    out.sort(key=lambda r: (r[0], r[1]))
    return Events(out)


def lon(values: list[tuple[float, float]], speed: float = 40.0) -> list[tuple[float, str, object]]:
    rows: list[tuple[float, str, object]] = [(0, "VehicleSpeed", speed)]
    rows += [(s, "LongitudinalAcceleration", v) for s, v in values]
    return rows


def counts(rows) -> tuple:
    return _harsh_counts(ev_from(rows), T0, T0 + timedelta(seconds=600))


class HarshTests(unittest.TestCase):
    def test_speed_step_counts_without_accel_signals(self) -> None:
        rows = [(0, "VehicleSpeed", 30.0), (1, "VehicleSpeed", 46.0), (2, "VehicleSpeed", 46.0)]
        accel, brake, turn = counts(rows)
        self.assertEqual((accel, brake, turn), (1, 0, None))

    def test_small_change_and_long_gap_are_not_events(self) -> None:
        self.assertEqual(counts([(0, "VehicleSpeed", 40.0), (1, "VehicleSpeed", 46.0)])[0], 0)
        self.assertEqual(counts([(0, "VehicleSpeed", 40.0), (5, "VehicleSpeed", 80.0)])[0], 0)

    def test_brake_is_a_drop_and_turn_is_not_inferred(self) -> None:
        rows = [(0, "VehicleSpeed", 80.0), (1, "VehicleSpeed", 64.0)]
        accel, brake, turn = counts(rows)
        self.assertEqual((accel, brake, turn), (0, 1, None))


class DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "f.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def store(self, rows: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, rows)
        conn.close()

    def trip(self) -> dict:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM trips ORDER BY start_ts DESC LIMIT 1").fetchone())
        finally:
            conn.close()


def base_trip(regen_signal: bool) -> tuple[list[dict], datetime]:
    s = [sig(T0, "Gear", "P"), sig(T0, "Odometer", 100.0), sig(T0, "LifetimeEnergyUsed", 50.0),
         sig(T0, "BatteryLevel", 60.0)]
    for n in ("TpmsPressureFl", "TpmsPressureFr", "TpmsPressureRl", "TpmsPressureRr"):
        s.append(sig(T0, n, 2.7))
    if regen_signal:
        s.append(sig(T0, "LifetimeEnergyGainedRegen", 10.0))
    start = T0 + timedelta(minutes=1)
    s.append(sig(start, "Gear", "D"))
    # 10 s steps: drive 300 A for 60 s, regen -100 A for 60 s, at 400 V
    for i in range(13):
        t = start + timedelta(seconds=10 * i)
        s += [sig(t, "PackVoltage", 400.0), sig(t, "PackCurrent", 300.0 if i < 6 else -100.0)]
    end = start + timedelta(minutes=3)
    s += [sig(end, "Odometer", 101.0), sig(end, "LifetimeEnergyUsed", 50.2), sig(end, "BatteryLevel", 59.8)]
    if regen_signal:
        s.append(sig(end, "LifetimeEnergyGainedRegen", 10.05))
    s.append(sig(end + timedelta(seconds=5), "Gear", "P"))
    return s, end + timedelta(seconds=5)


class RegenTests(DbCase):
    def test_estimated_from_pack_power(self) -> None:
        rows, _end = base_trip(regen_signal=False)
        self.store(rows)
        process_db(self.db, seed=False)
        t = self.trip()
        self.assertEqual(t["regen_estimated"], 1)
        # -40 kW for 7 samples x 10 s (last one capped at 10 s) = 280 kJ
        self.assertAlmostEqual(t["regen_kwh"], 40.0 * 70 / 3600.0, places=3)
        gross = t["end_lifetime_kwh"] - t["start_lifetime_kwh"]
        self.assertAlmostEqual(t["regen_ratio"], t["regen_kwh"] / gross, places=4)

    def test_signal_used_when_present(self) -> None:
        rows, _end = base_trip(regen_signal=True)
        self.store(rows)
        process_db(self.db, seed=False)
        t = self.trip()
        self.assertEqual(t["regen_estimated"], 0)
        self.assertAlmostEqual(t["regen_kwh"], 0.05, places=4)


class TripEndWaitTests(DbCase):
    def setUp(self) -> None:
        super().setUp()
        rows, self.end = base_trip(regen_signal=False)
        self.store(rows)
        self.sent: list = []

    def sender(self, payload):
        self.sent.append(payload)
        return True, "http 200"

    def run_at(self, now: datetime) -> None:
        process_db(self.db, seed=False, alerts=True, now=now, sender=self.sender)

    def ends(self) -> list:
        return [p for p in self.sent if p["kind"] == "trip_end"]

    def test_waits_then_uses_new_reading(self) -> None:
        # parked ≤3 min is still the same trip, so nothing is closed and nothing is sent
        self.run_at(self.end + timedelta(seconds=30))
        self.assertEqual(self.ends(), [])
        conn = process_connect(self.db)
        self.assertFalse(trip_end_pending(conn, VID, self.end + timedelta(seconds=30)))
        conn.close()
        t = self.end + timedelta(seconds=70)
        self.store([sig(t, "TpmsPressureFl", 2.65), sig(t, "TpmsPressureFr", 2.65),
                    sig(t, "TpmsPressureRl", 2.675), sig(t, "TpmsPressureRr", 2.65)])
        self.run_at(self.end + timedelta(minutes=2))
        self.assertEqual(self.ends(), [])  # still inside the 3 minute park
        self.run_at(self.end + timedelta(minutes=3, seconds=5))
        ends = self.ends()
        self.assertEqual(len(ends), 1)
        self.assertIn("38.4/38.4/38.8/38.4 psi", ends[0]["message"])
        self.assertIn("배터리|60% → 59.8%", ends[0]["message"])
        self.assertIn("앞 왼쪽|38.4 psi", ends[0]["message"])
        self.assertNotIn("주행가능거리", ends[0]["message"])
        self.assertNotIn("출발 시", ends[0]["message"])
        self.assertEqual(ends[0]["data"]["tire_source"], "after")
        self.assertEqual(ends[0]["data"]["battery_pct"], 59.8)
        self.run_at(self.end + timedelta(minutes=5))
        self.assertEqual(len(self.ends()), 1)

    def test_timeout_sends_departure_value_without_new_signals(self) -> None:
        self.run_at(self.end + timedelta(seconds=30))
        self.assertEqual(self.ends(), [])
        # no new signals: signal_state stays stale during the park, so this pass still closes it
        self.run_at(self.end + timedelta(minutes=3, seconds=10))
        ends = self.ends()
        self.assertEqual(len(ends), 1)
        self.assertIn("39.2/39.2/39.2/39.2 psi, 출발 시 측정값", ends[0]["message"])
        self.assertIn("앞 왼쪽|39.2 psi", ends[0]["message"])
        self.assertEqual(ends[0]["data"]["tire_source"], "departure")
        from web.bot_notify import event_payload
        from web.notify import list_notifications
        conn = process_connect(self.db)
        try:
            shown = next(n for n in list_notifications(conn, VID)["notifications"] if n["id"].startswith("trip_end:"))
        finally:
            conn.close()
        self.assertIn("달렸어요", shown["display_body"])
        self.assertEqual(dict(shown["display_details"])["배터리"], "60% → 59.8%")
        self.assertEqual(dict(shown["display_details"])["앞 왼쪽"], "39.2 psi")
        payload = event_payload(shown["id"], shown["body"], shown["details"], shown["ts"])
        self.assertEqual(payload["values"]["battery_pct"], 59.8)
        self.assertEqual(payload["values"]["tires_psi"]["fl"], 39.2)
        self.assertNotIn("range_km", payload["values"])
        self.run_at(self.end + timedelta(minutes=4))
        self.assertEqual(len(self.ends()), 1)

    def test_failed_send_retries_and_never_duplicates(self) -> None:
        calls: list = []

        def flaky(payload):
            calls.append(payload)
            return (len(calls) > 1), "http 500" if len(calls) == 1 else "http 200"

        now = self.end + timedelta(minutes=3, seconds=5)
        process_db(self.db, seed=False, alerts=True, now=now, sender=flaky)
        process_db(self.db, seed=False, alerts=True, now=now + timedelta(seconds=30), sender=flaky)
        process_db(self.db, seed=False, alerts=True, now=now + timedelta(seconds=60), sender=flaky)
        self.assertEqual(len([c for c in calls if c["kind"] == "trip_end"]), 2)  # 1 failed + 1 ok


if __name__ == "__main__":
    unittest.main()
