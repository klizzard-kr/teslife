"""Trailing zeros are dropped on every displayed decimal. Geometry stays precise."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from web.formatnum import format_number

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
DISPLAY_PY = (
    "web/notice_view.py",
    "web/notify.py",
    "web/pressure.py",
    "web/formatnum.py",
    "process/alerts.py",
    "process/maintenance.py",
    "ledger/tiresets.py",
)


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class FormatterTests(unittest.TestCase):
    def test_python_formatter_drops_trailing_zeros(self) -> None:
        self.assertEqual(format_number(6.70, 2), "6.7")
        self.assertEqual(format_number(2.17, 2), "2.17")
        self.assertEqual(format_number(2.10, 2), "2.1")
        self.assertEqual(format_number(2.00, 2), "2")
        self.assertEqual(format_number(1057.0, 1), "1057")
        self.assertEqual(format_number(1039.6, 1), "1039.6")
        self.assertEqual(format_number(61.50, 2), "61.5")
        self.assertEqual(format_number(61.00, 2), "61")
        self.assertEqual(format_number(61.25, 2), "61.25")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_screen_formatters_drop_trailing_zeros_and_keep_grouping(self) -> None:
        src = (
            "function uiLang(){ return 'ko'; }\n"
            + _fn("formatNum") + _fn("fmtTrim") + _fn("fmt") + _fn("fmtFixed")
            + _fn("fmtKmGrouped") + _fn("socFixedText")
            + "const out = {\n"
            "  kw: fmtFixed(6.70, 2),\n"
            "  stay: fmtFixed(2.17, 2),\n"
            "  one: fmtFixed(2.10, 2),\n"
            "  whole: fmtFixed(2.00, 2),\n"
            "  odo: fmtKmGrouped(1057.04),\n"
            "  odoKeep: fmtKmGrouped(1039.6),\n"
            "  pct: socFixedText(670),\n"
            "  pctWhole: socFixedText(6500),\n"
            "  pctKeep: socFixedText(8004)\n"
            "};\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        raw = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
        import json
        got = json.loads(raw.stdout)
        self.assertEqual(got["kw"], "6.7")
        self.assertEqual(got["stay"], "2.17")
        self.assertEqual(got["one"], "2.1")
        self.assertEqual(got["whole"], "2")
        self.assertEqual(got["odo"], "1,057 km")
        self.assertEqual(got["odoKeep"], "1,039.6 km")
        self.assertEqual(got["pct"], "6.7")
        self.assertEqual(got["pctWhole"], "65")
        self.assertEqual(got["pctKeep"], "80.04")

    def test_displayed_toFixed_is_only_the_formatter_or_geometry(self) -> None:
        allowed = (
            "v.toFixed(digits",
            "shown[wheel]",
            "opacity",
            "fill-opacity",
            "stroke-opacity",
            "style='left:",
            "%;width:",
            "endLeft.toFixed",
            "transform:rotate",
            "left:",
            "<rect ",
            "<text x=",
        )
        bad = []
        for n, line in enumerate(HTML.splitlines(), 1):
            if ".toFixed(" not in line:
                continue
            if any(bit in line for bit in allowed):
                continue
            bad.append(f"{n}: {line.strip()}")
        self.assertEqual(bad, [])

    def test_display_modules_do_not_pad_one_or_two_decimals(self) -> None:
        bad = []
        for rel in DISPLAY_PY:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if re.search(r":\.[12]f", line):
                    bad.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(bad, [])
        # The one remaining padded decimal builds a float for the 39.0 comparison, not a label.
        tires = (ROOT / "ledger" / "tires.py").read_text(encoding="utf-8")
        self.assertIn('return float(f"{float(bar) * PSI_PER_BAR:.1f}")', tires)
