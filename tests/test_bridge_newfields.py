"""2026-10-31 telemetry experiment: fields teslife does not use yet are stored as-is and never
break the bridge or processing (drive inverter, battery bricks, charging detail, climate, flags)."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from process import process_db

_SPEC = importlib.util.spec_from_file_location("teslife_bridge_nf", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

NEW = {
    "DiTorqueActualR": 123.5, "DiMotorCurrentR": 88, "DiAxleSpeedR": 1500.0, "DiStatorTempR": 41.0,
    "DiInverterTR": 39.5, "DiHeatsinkTR": 35, "LifetimeEnergyUsedDrive": 1234.567,
    "BrickVoltageMax": 3.912, "BrickVoltageMin": 3.905, "BMSState": "BMSStateDrive",
    "IsolationResistance": 2850, "NominalFullPackEnergyKwh": 62.3, "LifetimeEnergyChargedKwh": 4321.0,
    "DCDCEnable": True, "ChargerPhases": 1, "ChargingCableType": "CableTypeIEC",
    "FastChargerPresent": False, "FastChargerType": "FastChargerUnknown", "ChargeCurrentRequest": 32,
    "ChargeCurrentRequestMax": 32, "ChargeEnableRequest": True, "EstimatedHoursToChargeTermination": 3.5,
    "ChargeRateMilePerHour": 24, "ChargePortLatch": "ChargePortLatchEngaged",
    "ChargePortColdWeatherMode": False, "HvacACEnabled": True, "HvacAutoMode": "HvacAutoModeStateOn",
    "HvacFanStatus": 3, "HvacLeftTemperatureRequest": 22.0, "HvacRightTemperatureRequest": 22.0,
    "CabinOverheatProtectionMode": "CabinOverheatProtectionModeStateOn", "DefrostMode": "DefrostModeStateOff",
    "SeatVentEnabled": False, "DriverSeatOccupied": True, "DriverSeatBelt": "BuckleStatusLatched",
    "LocatedAtHome": True, "LocatedAtWork": False, "LocatedAtFavorite": False,
    "SomeFutureField": {"nested": [1, 2]}, "BadOne": "<invalid>",
}


class NewFieldsTest(unittest.TestCase):
    def test_rows_and_store(self) -> None:
        line = json.dumps({"msg": "record_payload", "data": {"Vin": "TEST-VIN",
                                                             "CreatedAt": "2026-10-31T01:00:00Z", **NEW}})
        rows = bridge.rows_from_line(line)
        names = {r["name"] for r in rows}
        self.assertNotIn("BadOne", names)
        self.assertEqual(len(rows), len(NEW) - 1)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "signals.db"
            conn = connect(db)
            self.assertEqual(store_signals(conn, rows), len(rows))
            conn.close()
            stats = process_db(db, seed=False)   # unknown names are ignored by processing
            self.assertEqual(stats.trips, 0)

    def test_mixed_with_known_fields(self) -> None:
        line = json.dumps({"msg": "record_payload", "data": {"Vin": "TEST-VIN",
                           "CreatedAt": "2026-10-31T01:00:01Z", "Gear": "ShiftStateP", "VehicleSpeed": 0,
                           "DiTorqueActualR": 0, "LocatedAtHome": True}})
        rows = {r["name"]: r["value"] for r in bridge.rows_from_line(line)}
        self.assertEqual(rows["Gear"], "P")
        self.assertIs(rows["LocatedAtHome"], True)
        self.assertEqual(rows["DiTorqueActualR"], 0)
