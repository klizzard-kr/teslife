"""10/31 11:09-11:25 KST replay: short moves get no trip_start, parking shuffle keeps 감시 quiet.

Real sequence: 11:09:56 D -> 11:10:13 P (17 s, a few metres); 11:11:18 D -> 11:23:38 P (2.3 km);
감시 armed (Idle) at P; 11:24:17 R ... 11:25:03 P (46 s parking shuffle, ~0.02 km), the car
disarms 감시 (Off) at the shift and re-arms (Idle) at P.
Before: trip_start x3 (no end for two), 감시 on/off/on in one minute.
After: one trip_start (11:11 drive) + its trip_end, 감시 켜짐 once.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
from process import _load_events, connect as process_connect, process_db
from process.alerts import run_alerts

VID = "TESTVIN"
Z = datetime(2026, 10, 31, 2, 0, 0, tzinfo=timezone.utc)  # 11:00 KST


def at(m: int, s: int) -> datetime:
    return Z + timedelta(minutes=m, seconds=s)


def sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "name": name, "value": value}


class TripFlapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        self.sent: list[dict] = []
        self.odo = 892.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def feed(self, signals: list[dict], now: datetime) -> list[str]:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)
        before = len(self.sent)
        conn = process_connect(self.db)
        try:
            run_alerts(conn, VID, _load_events(conn, VID), now=now,
                       sender=lambda p: (self.sent.append(p) or True, "http 200"))
        finally:
            conn.close()
        return [p["kind"] + ":" + str(p.get("data", {}).get("sentry", "")) for p in self.sent[before:]
                if p["kind"] in ("trip_start", "trip_end", "sentry")]

    def test_replay(self) -> None:
        got: list[str] = []
        base = [sig(at(9, 0), "Gear", "P"), sig(at(9, 0), "SentryMode", "Off"),
                sig(at(9, 0), "Odometer", 892.0), sig(at(9, 0), "BatteryLevel", 76.0)]
        got += self.feed(base, at(9, 1))
        # 11:09:56 D for 17 s, a few metres
        got += self.feed([sig(at(9, 56), "Gear", "D"), sig(at(9, 58), "VehicleSpeed", 4.0),
                          sig(at(10, 0), "Odometer", 892.005)], at(10, 5))
        got += self.feed([sig(at(10, 13), "Gear", "P")], at(10, 14))
        self.assertEqual(got, [], "17 s move: no trip_start")
        # 11:11:18 real drive 2.3 km
        drive = [sig(at(11, 18), "Gear", "D")]
        for i in range(1, 25):
            drive.append(sig(at(11, 18) + timedelta(seconds=30 * i), "Odometer", round(892.005 + 0.095 * i, 3)))
            drive.append(sig(at(11, 18) + timedelta(seconds=30 * i), "VehicleSpeed", 30.0))
        got += self.feed(drive[:5], at(12, 20))
        self.assertEqual(got, ["trip_start:"], "starts once it moved 0.1 km")
        got += self.feed(drive[5:] + [sig(at(23, 37), "Gear", "R"), sig(at(23, 38), "Gear", "P"),
                                      sig(at(23, 38), "SentryMode", "Idle")], at(24, 2))
        self.assertEqual(got[-1], "sentry:on")
        # 46 s parking shuffle: car disarms 감시 at the shift, re-arms at P
        got += self.feed([sig(at(24, 17), "Gear", "R"), sig(at(24, 17), "SentryMode", "Off"),
                          sig(at(24, 27), "Odometer", 894.3)], at(24, 34))
        got += self.feed([sig(at(24, 29), "Gear", "D"), sig(at(24, 33), "Gear", "R"),
                          sig(at(25, 3), "Gear", "P"), sig(at(25, 3), "SentryMode", "Idle"),
                          sig(at(25, 3), "Odometer", 894.31)], at(25, 6))
        # the shuffle is inside the same trip, so the end is the final P; wait out the tire window
        got += self.feed([sig(at(26, 11), "TpmsPressureFl", 2.65)], at(28, 0))
        self.assertEqual(got.count("trip_start:"), 1)
        self.assertEqual(got.count("trip_end:"), 1)
        self.assertEqual([g for g in got if g.startswith("sentry")], ["sentry:on"])

    def test_sentry_after_real_trip_still_alerts(self) -> None:
        got: list[str] = []
        got += self.feed([sig(at(0, 0), "Gear", "P"), sig(at(0, 0), "SentryMode", "Idle"),
                          sig(at(0, 0), "Odometer", 892.0)], at(0, 1))
        got += self.feed([sig(at(1, 0), "Gear", "D"), sig(at(1, 0), "SentryMode", "Off"),
                          sig(at(3, 0), "Odometer", 893.0)], at(3, 1))
        got += self.feed([sig(at(5, 0), "Gear", "P"), sig(at(5, 1), "SentryMode", "Idle")], at(5, 5))
        self.assertNotIn("sentry:off", got)  # disarm by shifting out of P is quiet
        self.assertIn("sentry:on", got)      # re-armed at a new place after a real trip: tell
        self.assertEqual(got.count("trip_start:"), 1)

    def test_manual_off_while_parked_still_alerts(self) -> None:
        got: list[str] = []
        got += self.feed([sig(at(0, 0), "Gear", "P"), sig(at(0, 0), "SentryMode", "Idle")], at(0, 1))
        got += self.feed([sig(at(2, 0), "SentryMode", "Off")], at(2, 1))
        self.assertEqual(got, ["sentry:off"])
