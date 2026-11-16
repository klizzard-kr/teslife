import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from simulator import SIGNAL_NAMES, generate


class ScenarioIngestTests(unittest.TestCase):
    def _run(self, scenario: str) -> sqlite3.Connection:
        signals = generate(scenario)
        self.assertGreater(len(signals), 0)
        for row in signals:
            self.assertEqual(set(row), {"vehicle_id", "timestamp", "name", "value"})
            self.assertTrue(row["vehicle_id"])
            self.assertIn(row["name"], SIGNAL_NAMES)
            self.assertNotIn("lat", row)
            self.assertNotIn("lon", row)
            self.assertNotIn("Location", row["name"])
            self.assertNotIn("Gps", row["name"])
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        conn = connect(tmp.name)
        self.addCleanup(conn.close)
        inserted = store_signals(conn, signals)
        self.assertEqual(inserted, len(signals))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(tables, {"signals", "vehicle_alerts"})
        return conn

    def _values(self, conn: sqlite3.Connection, name: str) -> list:
        import json

        rows = conn.execute(
            "SELECT ts, value FROM signals WHERE name = ? ORDER BY ts",
            (name,),
        ).fetchall()
        return [json.loads(v) for _, v in rows]

    def test_parked(self) -> None:
        conn = self._run("parked")
        gears = self._values(conn, "Gear")
        speeds = self._values(conn, "VehicleSpeed")
        odo = self._values(conn, "Odometer")
        self.assertTrue(gears)
        self.assertTrue(all(g == "P" for g in gears))
        self.assertTrue(all(s == 0.0 for s in speeds))
        self.assertEqual(len(set(odo)), 1)
        n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(n % len(SIGNAL_NAMES), 0)

    def test_driving(self) -> None:
        conn = self._run("driving")
        gears = self._values(conn, "Gear")
        speeds = self._values(conn, "VehicleSpeed")
        odo = self._values(conn, "Odometer")
        battery = self._values(conn, "BatteryLevel")
        self.assertIn("D", gears)
        self.assertEqual(gears[0], "D")
        self.assertEqual(gears[-1], "P")
        self.assertGreater(max(speeds), 0)
        self.assertEqual(speeds[-1], 0.0)
        self.assertGreater(odo[-1], odo[0])
        self.assertLess(battery[-1], battery[0])

    def test_charging(self) -> None:
        conn = self._run("charging")
        states = self._values(conn, "ChargeState")
        energy = self._values(conn, "ACChargingEnergyIn")
        battery = self._values(conn, "BatteryLevel")
        self.assertIn("Charging", states)
        self.assertGreater(energy[-1], energy[0])
        self.assertGreater(battery[-1], battery[0])

    def test_tire_pressure(self) -> None:
        conn = self._run("tire_pressure")
        fl = self._values(conn, "TpmsPressureFl")
        fr = self._values(conn, "TpmsPressureFr")
        rl = self._values(conn, "TpmsPressureRl")
        rr = self._values(conn, "TpmsPressureRr")
        self.assertGreaterEqual(len(fl), 2)
        self.assertNotEqual(fl[0], fl[-1])
        self.assertEqual(len(fl), len(fr))
        self.assertEqual(len(fr), len(rl))
        self.assertEqual(len(rl), len(rr))
        for name in (
            "TpmsPressureFl",
            "TpmsPressureFr",
            "TpmsPressureRl",
            "TpmsPressureRr",
        ):
            count = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE name = ?", (name,)
            ).fetchone()[0]
            self.assertGreaterEqual(count, 2)

    def test_duplicate_ignored(self) -> None:
        signals = generate("parked")
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        conn = connect(tmp.name)
        self.addCleanup(conn.close)
        first = store_signals(conn, signals)
        second = store_signals(conn, signals)
        self.assertEqual(first, len(signals))
        self.assertEqual(second, 0)
        n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(n, len(signals))


if __name__ == "__main__":
    unittest.main()
