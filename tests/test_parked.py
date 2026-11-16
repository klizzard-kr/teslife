"""Parked time split into camping / dog / climate keep / other HVAC / sentry / plain parked.

Real 2026-10-30 night: sentry Armed while parked used LifetimeEnergyUsed +0.05 kWh about
every 12.9 min (~235 W); asleep overnight 10/29 used 0.036 kWh in 8.75 h (~4 W).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
from process import Events, parked_segments, parked_state, process_db, connect as process_connect

_SPEC = importlib.util.spec_from_file_location("teslife_bridge_parked", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

VID = "VINPARK"
T = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)  # 10:00 KST


def H(h: float) -> datetime:
    return T + timedelta(hours=h)


def ev(rows: list[tuple[datetime, str, object]]) -> Events:
    return Events(sorted(rows, key=lambda r: (r[0], r[1])))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def summary(segs):
    return [(s["category"], s["sentry"], round((datetime.fromisoformat(s["end_ts"].replace("Z", "+00:00"))
             - datetime.fromisoformat(s["start_ts"].replace("Z", "+00:00"))).total_seconds() / 3600, 2)) for s in segs]


class ParkedSegmentTests(unittest.TestCase):
    def test_priority_and_sentry_flag(self) -> None:
        e = ev([
            (H(0), "Gear", "P"), (H(0), "SentryMode", "Armed"), (H(0), "ClimateKeeperMode", "Off"),
            (H(0), "HvacPower", "Off"),
            (H(1), "ClimateKeeperMode", "Party"), (H(1), "HvacPower", "On"),
            (H(2), "ClimateKeeperMode", "Off"),            # HVAC still on -> no lane of its own
            (H(2.5), "HvacPower", "Off"),
            (H(3), "SentryMode", "Off"),
        ])
        self.assertEqual(summary(parked_segments(e, H(0), H(4))), [
            ("sentry", 1, 1.0), ("camp", 1, 1.0), ("sentry", 1, 1.0), ("parked", 0, 1.0),
        ])

    def test_dog_keep_and_idle_sentry(self) -> None:
        e = ev([(H(0), "Gear", "P"), (H(0), "ClimateKeeperMode", "Dog"), (H(0), "SentryMode", "Idle"),
                (H(1), "ClimateKeeperMode", "On"), (H(2), "ClimateKeeperMode", "Off")])
        self.assertEqual(summary(parked_segments(e, H(0), H(3))),
                         [("dog", 0, 1.0), ("keep", 0, 1.0), ("parked", 0, 1.0)])
        self.assertEqual(parked_state(e, H(2.5)), ("parked", 0))

    def test_driving_and_charging_excluded(self) -> None:
        e = ev([(H(0), "Gear", "P"), (H(1), "Gear", "D"), (H(1.5), "Gear", "P"),
                (H(2), "DetailedChargeState", "Charging"), (H(3), "DetailedChargeState", "Complete")])
        self.assertEqual(summary(parked_segments(e, H(0), H(4))),
                         [("parked", 0, 1.0), ("parked", 0, 0.5), ("parked", 0, 1.0)])

    def test_energy_linear_and_wake_jump_goes_to_sleep(self) -> None:
        e = ev([(H(0), "Gear", "P"), (H(0), "SentryMode", "Armed"), (H(0), "LifetimeEnergyUsed", 100.0),
                (H(1), "LifetimeEnergyUsed", 100.24), (H(1), "SentryMode", "Off"),
                (H(9), "LifetimeEnergyUsed", 100.28)])            # first value after waking
        segs = parked_segments(e, H(0), H(9))
        self.assertEqual([s["category"] for s in segs], ["sentry", "parked"])
        self.assertAlmostEqual(segs[0]["kwh"], 0.24, places=3)
        self.assertAlmostEqual(segs[1]["kwh"], 0.04, places=3)
        self.assertEqual([s["kwh_estimated"] for s in segs], [0, 0])

    def test_battery_level_fallback_is_marked(self) -> None:
        e = ev([(H(0), "Gear", "P"), (H(0), "BatteryLevel", 80.0), (H(0), "EnergyRemaining", 50.0),
                (H(2), "BatteryLevel", 79.0)])
        seg = parked_segments(e, H(0), H(2))[0]
        self.assertAlmostEqual(seg["kwh"], 0.625, places=3)
        self.assertEqual(seg["kwh_estimated"], 1)

    def test_cut_at_kst_midnight(self) -> None:
        start = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)   # 23:00 KST
        e = ev([(start, "Gear", "P")])
        segs = parked_segments(e, start, start + timedelta(hours=2))
        self.assertEqual([s["start_ts"] for s in segs], ["2026-10-30T14:00:00Z", "2026-10-30T15:00:00Z"])


class ParkedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, rows) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(self.db, seed=False, now=H(10))

    def test_store_breakdown_and_incremental(self) -> None:
        self._store([(H(0), "Gear", "P"), (H(0), "SentryMode", "Armed"), (H(0), "LifetimeEnergyUsed", 100.0),
                     (H(2), "LifetimeEnergyUsed", 100.47)])
        self._store([(H(2), "SentryMode", "Off"), (H(2), "ClimateKeeperMode", "On"), (H(2), "HvacPower", "On"),
                     (H(3), "LifetimeEnergyUsed", 101.47), (H(3), "ClimateKeeperMode", "Off"),
                     (H(3), "HvacPower", "Off"), (H(4), "LifetimeEnergyUsed", 101.48)])
        conn = process_connect(self.db)
        rows = conn.execute("SELECT start_ts, end_ts FROM parked_segments WHERE vehicle_id = ? ORDER BY start_ts",
                            (VID,)).fetchall()
        for a, b in zip(rows, rows[1:]):
            self.assertLessEqual(a[1], b[0])          # no overlaps after re-processing
        # the stats card is gone; parked_segments stays for the mode timeline
        cats = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT category, SUM(kwh), SUM((julianday(end_ts) - julianday(start_ts)) * 24), MAX(sentry) "
            "FROM parked_segments WHERE vehicle_id = ? GROUP BY category", (VID,)).fetchall()}
        conn.close()
        self.assertEqual(set(cats), {"keep", "sentry", "parked"})
        self.assertAlmostEqual(cats["sentry"][0], 0.47, places=2)
        self.assertEqual(round(cats["sentry"][0] * 1000 / cats["sentry"][1]), 235)
        self.assertAlmostEqual(cats["keep"][0], 1.0, places=2)
        self.assertEqual(cats["keep"][2], 0)

    def test_camp_label_is_estimate(self) -> None:
        self._store([(H(0), "Gear", "P"), (H(0), "ClimateKeeperMode", "Party"), (H(1), "ClimateKeeperMode", "Off")])
        conn = process_connect(self.db)
        rows = conn.execute("SELECT category, kwh FROM parked_segments WHERE vehicle_id = ? AND category = 'camp'",
                            (VID,)).fetchall()
        conn.close()
        self.assertTrue(rows)
        self.assertIsNone(rows[0][1])    # unknown energy stays unknown


class BridgeClimateKeeperTests(unittest.TestCase):
    def test_prefix_stripped(self) -> None:
        for raw, want in (("ClimateKeeperModeStateOff", "Off"), ("ClimateKeeperModeStateOn", "On"),
                          ("ClimateKeeperModeStateDog", "Dog"), ("ClimateKeeperModeStateParty", "Party")):
            self.assertEqual(bridge.convert("ClimateKeeperMode", raw), want)


class ParkedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_stats_card_removed(self) -> None:
        # : no '주차 중 소모' stats card and no /api/parked/breakdown
        h = self.html
        for gone in ('id="parked-card"', "parked-help", "/api/parked/breakdown", "pk-bar", "function parkedHtml(",
                     "refreshParked", "<h2>주차 중 소모</h2>"):
            self.assertNotIn(gone, h)
        import web
        self.assertFalse(hasattr(web, "parked_breakdown"))


if __name__ == "__main__":
    unittest.main()
