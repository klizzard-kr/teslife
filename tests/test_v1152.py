"""v1.15.2: trip pressure omits a missing after-value, unit chips, gray disabled alerts."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PSI_PER_BAR = 14.5038


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


def _psi(bar: float) -> str:
    return f"{bar * PSI_PER_BAR:.1f}"


def _button(html: str) -> str:
    start = html.index("<button")
    return html[start:html.index("</button>", start)]


class TireDisplayTests(unittest.TestCase):
    def test_chips_keep_the_saved_unit(self) -> None:
        self.assertIn('const APP_VERSION = "v1.19.0"', HTML)
        self.assertIn('id="pressure-unit" class="unit-tag tire-unit"', HTML)
        self.assertLess(CHANGELOG.index("## v1.15.2"), CHANGELOG.index("## v1.15.1"))
        chip = HTML.split("button.unit-tag.tire-unit {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 999px", chip)
        self.assertIn("text-decoration: none", chip)
        self.assertIn("color: var(--text-secondary)", chip)
        self.assertNotIn("underline", chip)
        self.assertIn("function togglePressureUnit()", HTML)
        self.assertIn('localStorage.setItem(PRESSURE_KEY, next)', HTML)
        self.assertIn('writeJson("PUT", "/api/settings", { pressure_unit: next })', HTML)
        self.assertIn('closest(".tire-unit")', HTML)
        settings = HTML.split('id="settings-overlay"', 1)[1].split('id="period-overlay"', 1)[0]
        self.assertNotIn("타이어 공기압 단위", settings)
        self.assertNotIn("pressure-unit", settings)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_missing_after_is_one_number_and_the_phase_stays_outside(self) -> None:
        src = (
            "let pressureUnit = 'psi'; const PSI_PER_BAR = 14.5038;\n"
            "function carTopSvg(){ return ''; }\n"
            + _fn("fmtTrim") + _fn("psiNum") + _fn("tirePairText") + _fn("tireCompareHtml")
            + "const before = {fl:2.87, fr:2.90, rl:2.87, rr:2.90};\n"
            "const out = {\n"
            "  none: tireCompareHtml(before, {}),\n"
            "  mixed: tireCompareHtml(before, {fl:2.90, rl:2.90, rr:2.90}),\n"
            "  both: tireCompareHtml(before, {fl:2.90, fr:2.91, rl:2.90, rr:2.90}),\n"
            "  empty: tireCompareHtml({}, {}),\n"
            "  onlyAfter: tirePairText(null, 2.90)\n"
            "};\n"
            "pressureUnit = 'bar';\n"
            "out.bars = tireCompareHtml(before, {});\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(
            ["node", "-e", src], capture_output=True, text=True, check=True
        ).stdout)
        fl, fr = _psi(2.87), _psi(2.90)
        self.assertEqual(fl, "41.6")
        self.assertEqual(fr, "42.1")
        self.assertNotIn("주행 전 → 후", got["none"])
        self.assertNotIn("→", got["none"])
        self.assertIn(">" + fl + " psi<", got["none"])
        self.assertIn(">" + fr + " psi<", got["none"])
        self.assertEqual(_button(got["none"]), "<button type='button' class='unit-tag tire-unit' aria-label='공기압 단위'>psi")
        self.assertLess(got["mixed"].index("주행 전 → 후"), got["mixed"].index("<button"))
        self.assertNotIn("주행 전", _button(got["mixed"]))
        self.assertIn(">" + fr + " psi<", got["mixed"])
        self.assertIn(">" + fl + " → " + fr + "<", got["mixed"])
        self.assertIn(">" + fr + " → " + _psi(2.91) + "<", got["both"])
        self.assertEqual(got["empty"], "")
        self.assertEqual(got["onlyAfter"], fr + " psi")
        self.assertNotIn("주행 전 → 후", got["bars"])
        self.assertIn(">2.87 bar<", got["bars"])
        self.assertIn(">2.9 bar<", got["bars"])
        self.assertTrue(_button(got["bars"]).endswith(">bar"))


class AlertSwitchTests(unittest.TestCase):
    def test_disabled_switches_are_gray_without_a_native_check(self) -> None:
        track = HTML.split("#push-prefs.is-off .ios-switch input:checked + .knob {", 1)[1].split("}", 1)[0]
        self.assertIn("background: #c7c7cc", track)
        self.assertNotIn("#34c759", track)
        hidden = HTML.split(".pref-row .ios-switch input:disabled {", 1)[1].split("}", 1)[0]
        self.assertIn("visibility: hidden", hidden)
        self.assertIn("opacity: 0", hidden)
        self.assertIn("-webkit-appearance: none", hidden)
        body = HTML.split("function setKindSwitches", 1)[1].split("function renderPrefs", 1)[0]
        self.assertIn("box.disabled = !on", body)
        self.assertNotIn(".checked", body)
        self.assertNotIn("enabled:", body)


if __name__ == "__main__":
    unittest.main()
