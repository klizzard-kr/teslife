"""Charge detection: DetailedChargeState decides; ChargeState (Charging/Enable) is a fallback.

Current firmware sends both signals. ChargeState is the charger's internal step
(ClearFaults -> WaitForLineVoltage -> QualifyLineConfig -> Enable) while
DetailedChargeState is Starting -> Charging -> Complete.
"""

from __future__ import annotations

import json
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
from bridge.bridge import rows_from_line
from process import (
    _load_events,
    charge_status,
    connect as process_connect,
    is_charging,
    is_plugged,
    open_charge_start,
    process_db,
)
from web import live_payload

VID = "VINCHG"
T0 = datetime(2026, 10, 30, 11, 52, 26, tzinfo=timezone.utc)  # 20:52:26 KST, parked


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": iso(t), "name": name, "value": value}


def plug_in(with_detailed: bool = True) -> tuple[list[dict], datetime]:
    """Today's real sequence (2026-10-30 20:53-20:55 KST), 6.7 kW AC."""
    s = [
        sig(T0, "Gear", "P"), sig(T0, "BatteryLevel", 40.0), sig(T0, "ACChargingEnergyIn", 100.0),
        sig(T0, "ChargeState", "Idle"), sig(T0, "Odometer", 1000.0),
        sig(T0 + timedelta(seconds=72), "ChargePortDoorOpen", True),
    ]
    if with_detailed:
        s.append(sig(T0, "DetailedChargeState", "Disconnected"))
    c = T0 + timedelta(seconds=136)  # 20:54:42
    s += [sig(c, "ChargeState", "ClearFaults"),
          sig(c + timedelta(seconds=1), "ChargeState", "WaitForLineVoltage"),
          sig(c + timedelta(seconds=2), "ChargeState", "QualifyLineConfig"),
          sig(c + timedelta(seconds=4), "ChargeState", "Enable")]
    if with_detailed:
        s += [sig(c, "DetailedChargeState", "Starting"),
              sig(c + timedelta(seconds=4), "DetailedChargeState", "Charging")]
    start = c + timedelta(seconds=4)
    s += [sig(start + timedelta(seconds=5), "ACChargingPower", 6.7),
          sig(start + timedelta(seconds=3), "ChargeAmps", 32.0),
          sig(start + timedelta(seconds=4), "TimeToFullCharge", 3.7),
          sig(start + timedelta(seconds=20), "ChargeLimitSoc", 80)]
    for i in range(1, 7):
        t = start + timedelta(minutes=10 * i)
        s += [sig(t, "ACChargingEnergyIn", round(100.0 + 1.1 * i, 3)),
              sig(t, "BatteryLevel", round(40.0 + 1.5 * i, 2))]
    return s, start


def unplug(after: datetime, with_detailed: bool = True) -> list[dict]:
    t = after + timedelta(hours=1, minutes=5)
    out = [sig(t, "ChargeState", "Idle"), sig(t, "ChargePortDoorOpen", False)]
    if with_detailed:
        out.append(sig(t, "DetailedChargeState", "Complete"))
    return out


class ChargeRuleTests(unittest.TestCase):
    def test_rule(self) -> None:
        self.assertTrue(is_charging("Charging", "Enable"))
        self.assertTrue(is_charging("Charging", None))
        self.assertFalse(is_charging("Starting", "QualifyLineConfig"))
        self.assertFalse(is_charging("Complete", "Idle"))
        # DetailedChargeState wins over a stale or derived ChargeState
        self.assertFalse(is_charging("Disconnected", "Charging"))
        self.assertFalse(is_charging("Stopped", "Enable"))
        # fallback only when DetailedChargeState is unknown
        self.assertTrue(is_charging(None, "Enable"))
        self.assertTrue(is_charging("Unknown", "Charging"))
        self.assertFalse(is_charging(None, "Idle"))
        self.assertEqual(charge_status("Charging", "Enable"), "Charging")
        self.assertEqual(charge_status(None, "Enable"), "Charging")
        self.assertEqual(charge_status(None, "Complete"), "Complete")
        self.assertEqual(charge_status("Disconnected", "Idle"), "Disconnected")
        self.assertIsNone(charge_status(None, "Idle"))
        self.assertIsNone(charge_status(None, None))
        self.assertTrue(is_plugged("Starting", "ClearFaults"))
        self.assertTrue(is_plugged("Complete", "Idle"))
        self.assertFalse(is_plugged("Disconnected", "Idle"))
        self.assertTrue(is_plugged(None, "Enable"))


class ChargeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()

    def _charges(self) -> list[tuple]:
        conn = process_connect(self.db)
        rows = conn.execute(
            "SELECT start_ts, end_ts, kwh_added, kind, start_soc, end_soc, max_charge_amps "
            "FROM charges WHERE vehicle_id = ? ORDER BY start_ts", (VID,)
        ).fetchall()
        conn.close()
        return rows

    def test_detailed_and_enable_together_open_then_closed(self) -> None:
        s, start = plug_in()
        self._store(s)
        process_db(self.db, seed=False)
        self.assertEqual(self._charges(), [])  # still charging: not closed yet
        conn = process_connect(self.db)
        ev = _load_events(conn, VID, None)
        self.assertEqual(open_charge_start(ev), start)
        conn.row_factory = sqlite3.Row
        live = live_payload(conn, VID, 2.6)
        conn.close()
        self.assertTrue(live["charge_in_progress"])
        self.assertEqual(live["charge_status"], "Charging")
        self.assertEqual(live["signals"]["ChargeState"]["value"], "Enable")

        self._store(unplug(start))
        process_db(self.db, seed=False)
        rows = self._charges()
        self.assertEqual(len(rows), 1)
        st, en, kwh, kind, soc0, soc1, amps = rows[0]
        self.assertEqual(st, iso(start))
        self.assertEqual(en, iso(start + timedelta(hours=1, minutes=5)))
        self.assertAlmostEqual(kwh, 6.6, places=3)
        self.assertEqual(kind, "AC")
        self.assertAlmostEqual(soc0, 40.0)
        self.assertAlmostEqual(soc1, 49.0)
        self.assertEqual(amps, 32.0)
        conn = process_connect(self.db)
        conn.row_factory = sqlite3.Row
        live = live_payload(conn, VID, 2.6)
        conn.close()
        self.assertFalse(live["charge_in_progress"])
        self.assertEqual(live["charge_status"], "Complete")

    def test_enable_only_fallback(self) -> None:
        s, start = plug_in(with_detailed=False)
        self._store(s + unplug(start, with_detailed=False))
        process_db(self.db, seed=False)
        rows = self._charges()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], iso(start))

    def test_detailed_overrides_derived_charge_state(self) -> None:
        # Old bridge rows: ChargeState copied from DetailedChargeState; must stay one session.
        c = T0
        s = [sig(c, "DetailedChargeState", "Charging"), sig(c, "ChargeState", "Charging"),
             sig(c, "ACChargingEnergyIn", 1.0),
             sig(c + timedelta(minutes=30), "ChargeState", "Enable"),
             sig(c + timedelta(hours=1), "ACChargingEnergyIn", 8.0),
             sig(c + timedelta(hours=1), "DetailedChargeState", "Complete"),
             sig(c + timedelta(hours=1), "ChargeState", "Complete")]
        self._store(s)
        process_db(self.db, seed=False)
        rows = self._charges()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1]), (iso(c), iso(c + timedelta(hours=1))))
        self.assertAlmostEqual(rows[0][2], 7.0, places=3)

    def test_recompute_is_idempotent(self) -> None:
        s, start = plug_in()
        self._store(s + unplug(start))
        process_db(self.db, seed=False)
        first = self._charges()
        conn = process_connect(self.db)
        conn.execute("DELETE FROM process_meta")
        conn.commit()
        conn.close()
        process_db(self.db, seed=False)
        self.assertEqual(self._charges(), first)


class BridgeChargeTests(unittest.TestCase):
    def _line(self, **fields) -> str:
        data = {"Vin": VID, "CreatedAt": "2026-10-30T11:54:46Z"}
        data.update(fields)
        return json.dumps({"msg": "record_payload", "data": data})

    def test_both_signals_stored_as_sent(self) -> None:
        rows = rows_from_line(self._line(ChargeState="ChargeStateEnable",
                                         DetailedChargeState="DetailedChargeStateCharging"))
        got = {(r["name"], r["value"]) for r in rows}
        self.assertEqual(got, {("ChargeState", "Enable"), ("DetailedChargeState", "Charging")})

    def test_detailed_only_is_not_copied(self) -> None:
        rows = rows_from_line(self._line(DetailedChargeState="DetailedChargeStateStarting"))
        self.assertEqual([(r["name"], r["value"]) for r in rows], [("DetailedChargeState", "Starting")])

    def test_unknown_dropped(self) -> None:
        rows = rows_from_line(self._line(ChargeState="ChargeStateUnknown",
                                         DetailedChargeState="DetailedChargeStateUnknown"))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
