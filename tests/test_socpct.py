""": battery % next to kWh on the mode-bar popup 사용량 line only."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

from process import ensure_process_schema
from web.timeline import kwh_per_pct

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class KwhPerPctTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = sqlite3.connect(":memory:")
        ensure_process_schema(self.c)

    def add(self, ts: str, name: str, value: str) -> None:
        self.c.execute("INSERT INTO signals (vehicle_id, ts, name, value) VALUES ('V', ?, ?, ?)", (ts, name, value))

    def test_ratio_of_last_values(self) -> None:
        self.add("2026-10-30T10:00:00Z", "EnergyRemaining", "50.0")
        self.add("2026-10-30T10:00:00Z", "BatteryLevel", "80.0")
        self.add("2026-10-31T10:00:00Z", "EnergyRemaining", "30.0")
        self.add("2026-10-31T10:00:00Z", "BatteryLevel", "50.0")
        self.assertEqual(kwh_per_pct(self.c, "V", "2026-10-30T15:00:00Z"), 0.625)
        self.assertEqual(kwh_per_pct(self.c, "V", "2026-10-31T15:00:00Z"), 0.6)
        # before any value: the first one after
        self.assertEqual(kwh_per_pct(self.c, "V", "2026-10-29T00:00:00Z"), 0.625)

    def test_unknown(self) -> None:
        self.assertIsNone(kwh_per_pct(self.c, "V", "2026-10-30T15:00:00Z"))
        self.add("2026-10-30T10:00:00Z", "BatteryLevel", "0")
        self.add("2026-10-30T10:00:00Z", "EnergyRemaining", "1")
        self.assertIsNone(kwh_per_pct(self.c, "V", "2026-10-30T15:00:00Z"))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PopupTextTest(unittest.TestCase):
    def run_js(self, expr: str, timeline: dict | None) -> str:
        src = (_fn("fmtTrim") + _fn("fmt") + "const known=v=>v!==null&&v!==undefined&&v!=='';"
               f"let batTimeline={json.dumps(timeline)};" + _fn("tlSpan") + _fn("tlKwh") + _fn("tlBattShare") + _fn("tlKwhPct") + _fn("modeSumLine") +
               f"process.stdout.write({expr});")
        return subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout

    def test_converted_pct(self) -> None:
        """: one 약 in front for every bar 사용량: '약 X kWh (Y%)'."""
        self.assertEqual(self.run_js("tlKwhPct(0.48)", {"kwh_per_pct": 0.6}), "약 0.48 kWh (0.8%)")
        self.assertEqual(self.run_js("tlKwhPct(12)", {"kwh_per_pct": 0.6}), "약 12 kWh (20%)")
        self.assertEqual(self.run_js("tlKwhPct(0.027)", {"kwh_per_pct": 0.6266}), "약 0.027 kWh (0.1% 미만)")
        self.assertEqual(self.run_js("tlKwhPct(1.15)", {"kwh_per_pct": 0.7667}), "약 1.15 kWh (1.5%)")

    def test_regression_yak_once_in_front(self) -> None:
        """ regression: measured parked bars lost the front 약 in  (label '추정 사용량' →
        '사용량' kept tlKwh(b.kwh, false)); never '약' twice and never a bare kWh."""
        for kwh, per in ((12, 0.6), (0.48, 0.6), (0.027, 0.6266), (3.3, None)):
            out = self.run_js(f"tlKwhPct({kwh})", {"kwh_per_pct": per})
            self.assertTrue(out.startswith("약 "), out)
            self.assertEqual(out.count("약"), 1, out)

    def test_mode_sum_line(self) -> None:
        """Header is '4시간 6분 · 1.2 kWh (1.5%)': no 총/사용량, share like the bars, 약 only when estimated."""
        self.assertEqual(self.run_js("modeSumLine(246, 1.2, false)", {"kwh_per_pct": 0.8}),
                         "4시간 6분 · 1.2 kWh (1.5%)")
        self.assertEqual(self.run_js("modeSumLine(246, 1.2, true)", {"kwh_per_pct": 0.8}),
                         "4시간 6분 · 약 1.2 kWh (1.5%)")
        self.assertEqual(self.run_js("modeSumLine(246, 1.2, false)", {"kwh_per_pct": None}),
                         "4시간 6분 · 1.2 kWh")
        self.assertEqual(self.run_js("modeSumLine(90, null, false)", {"kwh_per_pct": 0.8}), "1시간 30분")
        for expr in ("modeSumLine(246, 1.2, false)", "modeSumLine(246, 1.2, true)"):
            out = self.run_js(expr, {"kwh_per_pct": 0.8})
            self.assertNotIn("총", out)
            self.assertNotIn("사용량", out)

    def test_drive_share_has_no_yak(self) -> None:
        """Drive usage keeps the measured kWh and appends the same battery share, without 약."""
        self.assertEqual(self.run_js("tlKwh(1.2, false) + tlBattShare(1.2)", {"kwh_per_pct": 0.8}),
                         "1.2 kWh (1.5%)")
        self.assertEqual(self.run_js("tlKwh(1.2, false) + tlBattShare(1.2)", {"kwh_per_pct": None}),
                         "1.2 kWh")
        self.assertEqual(self.run_js("tlKwh(1.2, false) + tlBattShare(1.2)", None), "1.2 kWh")
        self.assertEqual(self.run_js("tlKwh(0.027, false) + tlBattShare(0.027)", {"kwh_per_pct": 0.6266}),
                         "0.027 kWh (0.1% 미만)")

    def test_hidden_when_unknown(self) -> None:
        self.assertEqual(self.run_js("tlKwhPct(0.48)", {"kwh_per_pct": None}), "약 0.48 kWh")
        self.assertEqual(self.run_js("tlKwhPct(0.48)", None), "약 0.48 kWh")


class WiringTest(unittest.TestCase):
    def test_popups_use_pct(self) -> None:
        self.assertIn('rows.push(["사용량", tlKwhPct(b.kwh_estimate)])', HTML)
        self.assertIn('tlKwhPct(b.kwh)', HTML)
        self.assertNotIn("(추정)", _fn("tlKwhPct"))

    def test_hour_and_drive_unchanged(self) -> None:
        self.assertIn('"약 "', _fn("hourPopHtml"))
        self.assertNotIn("tlKwhPct", _fn("hourPopHtml"))
        self.assertIn('lane.key === "drive" ? driveKwhText(b) : tlKwhPct(b.kwh)', HTML)
        self.assertIn("function driveKwhText", HTML)
