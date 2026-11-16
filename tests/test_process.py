import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from process import process_db
from simulator import generate


def _tmp_db(test: unittest.TestCase) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
    return tmp.name


class ProcessDayTests(unittest.TestCase):
    def _ingest(self, scenario: str, vehicle_id: str | None = None) -> str:
        db = _tmp_db(self)
        kwargs = {} if vehicle_id is None else {"vehicle_id": vehicle_id}
        signals = generate(scenario, **kwargs)
        conn = connect(db)
        try:
            store_signals(conn, signals)
        finally:
            conn.close()
        return db

    def _rows(self, db: str, table: str, vehicle_id: str | None = None):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            if vehicle_id is None:
                return conn.execute(
                    f"SELECT * FROM {table} ORDER BY vehicle_id, start_ts"
                    if table != "tire_readings"
                    else f"SELECT * FROM {table} ORDER BY vehicle_id, ts"
                ).fetchall()
            col = "ts" if table == "tire_readings" else "start_ts"
            return conn.execute(
                f"SELECT * FROM {table} WHERE vehicle_id = ? ORDER BY {col}",
                (vehicle_id,),
            ).fetchall()
        finally:
            conn.close()

    def test_day_trips_charges_tires(self) -> None:
        db = self._ingest("day")
        stats = process_db(db)
        self.assertEqual(stats.ignored_trips, 1)
        trips = self._rows(db, "trips")
        charges = self._rows(db, "charges")
        tires = self._rows(db, "tire_readings")
        self.assertEqual(len(trips), 2)
        self.assertAlmostEqual(trips[0]["km"], 12.0, places=3)
        self.assertAlmostEqual(trips[0]["kwh"], 3.863, places=3)
        self.assertAlmostEqual(trips[0]["wh_per_km"], 321.9, places=1)
        self.assertAlmostEqual(trips[1]["km"], 30.0, places=3)
        self.assertAlmostEqual(trips[1]["kwh"], 10.164, places=3)
        self.assertAlmostEqual(trips[1]["wh_per_km"], 338.8, places=1)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["kind"], "AC")
        self.assertAlmostEqual(charges[0]["kwh_added"], 20.0, places=3)
        self.assertGreater(charges[0]["max_power"], 0)
        phases = {row["phase"] for row in tires}
        self.assertEqual(phases, {"cold", "warm"})
        cold = [row for row in tires if row["phase"] == "cold"]
        warm = [row for row in tires if row["phase"] == "warm"]
        self.assertEqual(len(cold), 1)
        self.assertGreaterEqual(len(warm), 1)
        self.assertLess(cold[0]["ts"], trips[0]["start_ts"])
        self.assertGreaterEqual(warm[0]["ts"], trips[0]["start_ts"])

    def test_dc_day_charge_kind(self) -> None:
        db = self._ingest("dc_day")
        stats = process_db(db)
        self.assertEqual(stats.ignored_trips, 1)
        trips = self._rows(db, "trips")
        charges = self._rows(db, "charges")
        self.assertEqual(len(trips), 2)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["kind"], "DC")
        self.assertAlmostEqual(charges[0]["kwh_added"], 20.0, places=3)

    def test_idempotent(self) -> None:
        db = self._ingest("day")
        process_db(db)
        first_trips = [tuple(r) for r in self._rows(db, "trips")]
        first_charges = [tuple(r) for r in self._rows(db, "charges")]
        first_tires = [tuple(r) for r in self._rows(db, "tire_readings")]
        process_db(db)
        self.assertEqual([tuple(r) for r in self._rows(db, "trips")], first_trips)
        self.assertEqual([tuple(r) for r in self._rows(db, "charges")], first_charges)
        self.assertEqual([tuple(r) for r in self._rows(db, "tire_readings")], first_tires)

    def test_vehicle_ids_do_not_mix(self) -> None:
        db = _tmp_db(self)
        conn = connect(db)
        try:
            store_signals(conn, generate("day", vehicle_id="VINAAA"))
            store_signals(conn, generate("dc_day", vehicle_id="VINBBB"))
        finally:
            conn.close()
        process_db(db)
        a_trips = self._rows(db, "trips", "VINAAA")
        b_trips = self._rows(db, "trips", "VINBBB")
        a_charges = self._rows(db, "charges", "VINAAA")
        b_charges = self._rows(db, "charges", "VINBBB")
        self.assertEqual(len(a_trips), 2)
        self.assertEqual(len(b_trips), 2)
        self.assertTrue(all(r["vehicle_id"] == "VINAAA" for r in a_trips))
        self.assertTrue(all(r["vehicle_id"] == "VINBBB" for r in b_trips))
        self.assertEqual(a_charges[0]["kind"], "AC")
        self.assertEqual(b_charges[0]["kind"], "DC")
        self.assertNotEqual(a_trips[0]["start_ts"], "")
        self.assertEqual({r["vehicle_id"] for r in a_charges}, {"VINAAA"})
        self.assertEqual({r["vehicle_id"] for r in b_charges}, {"VINBBB"})


if __name__ == "__main__":
    unittest.main()
