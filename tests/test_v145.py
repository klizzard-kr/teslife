"""v1.14.5: a clear fast signal is DC before AC energy. Place is not used.

PROCESS_VERSION stays 24, so the twelve stored charges are not rebuilt.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import PROCESS_VERSION, Events, charge_energy, process_db
from process.alerts import _charge_kind
from tests.test_charge import plug_in, sig, unplug

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
END = T0 + timedelta(hours=1)


def _kind(rows: list[tuple]) -> str:
    energy = charge_energy(Events(rows), T0, END)
    return energy["kind"]


def _wall(**over) -> list[tuple]:
    """AC energy up, pack energy up, and a full wall-AC signal."""
    rows = {
        "ac0": (T0, "ACChargingEnergyIn", 10.0),
        "ac1": (END, "ACChargingEnergyIn", 16.0),
        "dc0": (T0, "DCChargingEnergyIn", 1.0),
        "dc1": (END, "DCChargingEnergyIn", 6.5),
        "power": (T0 + timedelta(minutes=1), "ACChargingPower", 6.7),
        "amps": (T0 + timedelta(minutes=1), "ChargeAmps", 32.0),
        "volts": (T0 + timedelta(minutes=1), "ChargerVoltage", 220.0),
        "present": (T0 + timedelta(minutes=1), "FastChargerPresent", False),
    }
    rows.update(over)
    return list(rows.values())


class KindOrderTests(unittest.TestCase):
    def test_version_is_unchanged_so_stored_rows_stay(self) -> None:
        self.assertEqual(PROCESS_VERSION, "25")
        notes = HTML.split('version: "1.14.5"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("급속 신호가 분명하면 급속으로 먼저 판단해요", notes)
        old = HTML.split('version: "1.14.3"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("벽면 완속 신호가 잡히면 급속으로 잘못 적지 않아요", old)
        self.assertNotIn("집 충전으로 보이면 급속으로 적지 않아요", HTML)

    def test_twelve_wall_patterns_stay_ac(self) -> None:
        stale = T0 - timedelta(hours=3)
        patterns = [
            _wall(),
            [(T0, "ACChargingEnergyIn", 1.0), (END, "ACChargingEnergyIn", 4.0)],
            [
                (T0 + timedelta(minutes=1), "ACChargingPower", 6.7),
                (T0 + timedelta(minutes=1), "ChargeAmps", 16.0),
                (T0 + timedelta(minutes=1), "ChargerVoltage", 221.0),
                (T0 + timedelta(minutes=1), "FastChargerPresent", False),
                (T0, "DCChargingEnergyIn", 0.0),
                (END, "DCChargingEnergyIn", 5.0),
            ],
            _wall(kind=(T0 + timedelta(minutes=2), "FastChargerType", "FastChargerUnknown")),
            _wall(pack=(T0 + timedelta(minutes=2), "DCChargingPower", 7.0)),
            _wall(volts=(T0 + timedelta(minutes=2), "ChargerVoltage", 299.0)),
            _wall() + [(stale, "FastChargerPresent", True)],
            _wall() + [(stale, "DCChargingPower", 80.0)],
            _wall() + [(stale, "ChargerVoltage", 400.0)],
            _wall() + [(stale, "FastChargerType", "FastChargerSupercharger")],
            [
                (T0 + timedelta(minutes=1), "ACChargingPower", 3.3),
                (T0 + timedelta(minutes=1), "ChargeAmps", 16.0),
                (T0 + timedelta(minutes=1), "ChargerVoltage", 200.0),
                (T0 + timedelta(minutes=1), "FastChargerPresent", False),
            ],
            _wall(where=(T0, "Location", {"lat": 37.0, "lon": 127.0})),
        ]
        self.assertEqual(len(patterns), 12)
        self.assertEqual([_kind(rows) for rows in patterns], ["AC"] * 12)

    def test_fast_present_is_dc_even_with_some_wall_signals(self) -> None:
        rows = _wall(present=(T0 + timedelta(minutes=2), "FastChargerPresent", True))
        rows.append((T0 + timedelta(minutes=3), "FastChargerPresent", False))
        rows.append((T0, "Location", {"lat": 37.0, "lon": 127.0}))
        energy = charge_energy(Events(rows), T0, END)
        self.assertEqual(energy["kind"], "DC")
        self.assertAlmostEqual(energy["charger_kwh"], 5.5, places=3)
        self.assertEqual(_charge_kind(Events(rows), T0, END), "DC")

    def test_fast_type_high_power_or_voltage_is_dc_first(self) -> None:
        self.assertEqual(_kind(_wall(kind=(T0 + timedelta(minutes=2), "FastChargerType", "Combo"))), "DC")
        self.assertEqual(_kind(_wall(pack=(T0 + timedelta(minutes=2), "DCChargingPower", 15.0))), "DC")
        self.assertEqual(_kind(_wall(volts=(T0 + timedelta(minutes=2), "ChargerVoltage", 300.0))), "DC")
        self.assertEqual(_kind(_wall(pack=(T0 + timedelta(minutes=2), "DCChargingPower", 14.9))), "AC")

    def test_dc_energy_alone_is_still_dc(self) -> None:
        rows = [
            (T0, "DCChargingEnergyIn", 0.0),
            (END, "DCChargingEnergyIn", 20.0),
            (T0, "Location", {"lat": 37.0, "lon": 127.0}),
        ]
        energy = charge_energy(Events(rows), T0, END)
        self.assertEqual(energy["kind"], "DC")
        self.assertAlmostEqual(energy["charger_kwh"], 20.0, places=3)


class StoredKindTests(unittest.TestCase):
    def test_a_fast_session_with_wall_samples_is_stored_as_dc(self) -> None:
        import tempfile

        s, start = plug_in()
        s += [
            sig(start + timedelta(minutes=1), "ChargerVoltage", 218.0),
            sig(start + timedelta(minutes=1), "FastChargerPresent", True),
            sig(start + timedelta(minutes=2), "DCChargingPower", 48.0),
            sig(start, "DCChargingEnergyIn", 0.0),
            sig(start + timedelta(minutes=30), "DCChargingEnergyIn", 12.0),
        ]
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "signals.db"
            conn = ingest.connect(db)
            ingest.store_signals(conn, s + unplug(start))
            conn.close()
            process_db(db, seed=False)
            conn = ingest.connect(db)
            kind = conn.execute("SELECT kind FROM charges").fetchone()[0]
            conn.close()
        finally:
            tmp.cleanup()
        self.assertEqual(kind, "DC")
