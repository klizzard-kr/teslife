"""Bridge CreatedAt normalisation. Numeric epochs become UTC ISO before ingest."""

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from process import parse_ts

_SPEC = importlib.util.spec_from_file_location("teslife_bridge", ROOT / "bridge" / "bridge.py")
assert _SPEC and _SPEC.loader
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

# 2026-10-28 10:00:25 UTC
EPOCH = 1793181625
ISO = "2026-10-28T10:00:25Z"


def _line(created_at, **fields):
    data = {"Vin": "TEST-VIN", "CreatedAt": created_at}
    data.update(fields)
    return json.dumps({"msg": "record_payload", "data": data})


class NormTsTests(unittest.TestCase):
    def test_epoch_seconds_millis_and_passthrough(self) -> None:
        self.assertEqual(bridge.norm_ts(EPOCH), ISO)
        self.assertEqual(bridge.norm_ts(float(EPOCH)), ISO)
        self.assertEqual(bridge.norm_ts(str(EPOCH)), ISO)
        self.assertEqual(bridge.norm_ts(EPOCH * 1000), ISO)
        self.assertEqual(bridge.norm_ts(str(EPOCH * 1000)), ISO)
        self.assertEqual(bridge.norm_ts(ISO), ISO)
        self.assertIsNone(bridge.norm_ts(True))
        self.assertIsNone(bridge.norm_ts(False))

    def test_numeric_created_at_is_iso_and_location_is_dropped(self) -> None:
        rows = bridge.rows_from_line(
            _line(
                EPOCH,
                BatteryLevel=55,
                Odometer=10,
                Gear="ShiftStatePark",
                Location={"latitude": 37.5, "longitude": 126.9},
                GpsHeading=90,
                Destination="home",
                LatLon={"latitude": 37.5, "longitude": 126.9},
                ChargeState="<invalid>",
                DestinationName="회사",
                DestinationLocation={"latitude": 37.5, "longitude": 126.9},
                MilesToArrival=3.0,
                MinutesToArrival=7,
                ExpectedEnergyPercentAtTripArrival=55,
            )
        )
        self.assertTrue(rows)
        for gone in ("DestinationName", "DestinationLocation", "MilesToArrival", "MinutesToArrival",
                     "ExpectedEnergyPercentAtTripArrival"):
            self.assertNotIn(gone, {row["name"] for row in rows})
        names = {row["name"] for row in rows}
        self.assertNotIn("Location", names)
        self.assertNotIn("GpsHeading", names)
        self.assertNotIn("Destination", names)
        self.assertNotIn("LatLon", names)
        self.assertNotIn("ChargeState", names)
        self.assertTrue(all(row["timestamp"] == ISO for row in rows))
        by_name = {row["name"]: row["value"] for row in rows}
        self.assertEqual(by_name["BatteryLevel"], 55)
        self.assertEqual(by_name["Gear"], "P")
        self.assertEqual(by_name["Odometer"], round(10 * 1.609344, 3))

        parsed = parse_ts(rows[0]["timestamp"])
        self.assertEqual(parsed, datetime(2026, 10, 28, 10, 0, 25, tzinfo=timezone.utc))
        with self.assertRaises(ValueError) as ctx:
            parse_ts(str(EPOCH))
        self.assertIn("Invalid isoformat", str(ctx.exception))

    def test_millis_line_stored_as_iso(self) -> None:
        rows = bridge.rows_from_line(_line(EPOCH * 1000, BatteryLevel=40))
        self.assertEqual(rows[0]["timestamp"], ISO)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        conn = connect(tmp.name)
        try:
            store_signals(conn, rows)
            saved = conn.execute("SELECT ts, value FROM signals").fetchone()
        finally:
            conn.close()
        self.assertEqual(saved[0], ISO)
        self.assertEqual(json.loads(saved[1]), 40)
        parse_ts(saved[0])


if __name__ == "__main__":
    unittest.main()
