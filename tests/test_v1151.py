"""v1.15.1: regen ratio uses gross lifetime kWh, detail battery has no delta, tire unit toggles."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect
from process import process_db
from web import menu
from web.driving import driving_report
from web.notice_view import format_soc_change, trip_end_summary

HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "VIN1151"


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class RegenRatioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        conn = connect(self.db)
        conn.close()
        process_db(self.db, seed=False)
        self.conn = sqlite3.connect(str(self.db))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _trip(self, start: str, km: float, kwh: float, regen: float,
              life_s: float | None, life_e: float | None) -> None:
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, kwh, duration_s, regen_kwh, "
            "start_lifetime_kwh, end_lifetime_kwh) VALUES (?,?,?,?,?,?,?,?,?)",
            (VID, start, start, km, kwh, 600, regen, life_s, life_e),
        )
        self.conn.commit()

    def test_summary_ratio_uses_gross_not_net(self) -> None:
        # net 2.5, regen 1.26, gross 3.76 -> about 34%, not regen/net (about 50%).
        self._trip("2026-11-16T01:00:00Z", 10.0, 2.5, 1.26, 10.0, 13.76)
        self._trip("2026-11-16T03:00:00Z", 4.0, 1.0, 0.4, 20.0, 21.0)
        # No lifetime rise: kept in 쓴 전력, left out of the ratio.
        self._trip("2026-11-16T05:00:00Z", 4.0, 1.0, 1.0, None, None)
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        report = driving_report(self.conn, VID, "week", "2026-11-16", now=now)
        gross = 3.76 + 1.0
        regen = 1.26 + 0.4
        self.assertAlmostEqual(report["kwh"], 4.5)
        self.assertAlmostEqual(report["km_per_kwh"], round(18.0 / 4.5, 2))
        self.assertAlmostEqual(report["regen_kwh"], 2.66)
        self.assertAlmostEqual(report["regen_ratio"], round(regen / gross, 3))
        self.assertNotAlmostEqual(report["regen_ratio"], round(1.26 / 2.5, 3))
        self.assertAlmostEqual(round(1.26 / 3.76, 3), 0.335)
        summary = menu.trips_summary(self.conn, VID, "2026-11-16", "2026-11-16")
        self.assertAlmostEqual(summary["kwh"], 4.5)
        self.assertAlmostEqual(summary["km_per_kwh"], round(18.0 / 4.5, 2))
        self.assertAlmostEqual(summary["regen_ratio"], round(regen / gross, 3))
        recent = menu.trips_summary(self.conn, VID, recent=5)
        self.assertAlmostEqual(recent["regen_ratio"], round(regen / gross, 3))
        self.assertAlmostEqual(recent["kwh"], 4.5)


class BatteryLineTests(unittest.TestCase):
    def test_one_decimal_without_delta_and_equal_is_one_number(self) -> None:
        self.assertEqual(format_soc_change(64.6, 63.4), "64.6% → 63.4%")
        self.assertEqual(format_soc_change(65.0, 63.4), "65% → 63.4%")
        self.assertEqual(format_soc_change(65.04, 63.0), "65.04% → 63%")
        self.assertEqual(format_soc_change(80.04, 79.96), "80.04% → 79.96%")
        self.assertEqual(format_soc_change(64.6, 64.6), "64.6%")
        self.assertNotIn("%p", format_soc_change(64.6, 63.4))
        summary = trip_end_summary(
            "10 km",
            [["거리", "10 km"], ["배터리", "64.6% → 63.4%"]],
        )
        self.assertIn("배터리 63.4%", summary)
        self.assertNotIn("→", summary)
        self.assertNotIn("%p", summary)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_bar_text_and_no_sliver_when_equal(self) -> None:
        src = (
            _fn("socHundredths") + _fn("socFixedText") + _fn("socDetailText") + _fn("socBarHtml")
            + "const out=["
            "socBarHtml(64.6,63.4),socBarHtml(65,63.4),socBarHtml(65.004,65.001),socBarHtml(64.6,64.6)"
            "]; process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertIn("64.6% → 63.4%", got[0])
        self.assertNotIn("%p", got[0])
        self.assertIn("soc-delta down", got[0])
        self.assertIn("width:1.2%", got[0])
        self.assertIn("65% → 63.4%", got[1])
        self.assertNotIn("%p", got[1])
        self.assertIn(">65%<", got[2])
        self.assertNotIn("soc-delta", got[2])
        self.assertNotIn("→", got[2])
        self.assertIn(">64.6%<", got[3])
        self.assertNotIn("soc-delta", got[3])


class TireUnitTests(unittest.TestCase):
    def test_trip_label_toggles_the_saved_unit(self) -> None:
        self.assertIn('const APP_VERSION = "v1.16.6"', HTML)
        self.assertIn("BAR_PER_PSI = 0.0689476", HTML)
        self.assertIn('class=\'unit-tag tire-unit\'', HTML)
        self.assertIn("주행 전 → 후", HTML)
        self.assertNotIn("주행 전 → 후 · ", HTML)
        self.assertIn("function togglePressureUnit()", HTML)
        self.assertIn('localStorage.setItem(PRESSURE_KEY, next)', HTML)
        self.assertIn('closest(".tire-unit")', HTML)
        self.assertIn("touch-action: manipulation", HTML)
        self.assertIn("-webkit-appearance: none", HTML)


if __name__ == "__main__":
    unittest.main()
