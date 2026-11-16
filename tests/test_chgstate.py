"""One Korean wording for DetailedChargeState: five words, unknown is 미확인."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from process import CHARGE_STATE_KO

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
WANT = {"Disconnected": "연결 안 됨", "NoPower": "연결됨", "Starting": "연결됨",
        "Charging": "충전 중", "Complete": "충전 완료", "Stopped": "연결됨"}


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class ChargeStateWordingTest(unittest.TestCase):
    def test_server_mapping(self) -> None:
        self.assertEqual(CHARGE_STATE_KO, WANT)

    def test_web_mapping_same(self) -> None:
        i = HTML.index("const CHARGE_KO = {")
        body = HTML[i:HTML.index("};", i)]
        got = dict(re.findall(r'(\w+): "([^"]+)"', body))
        self.assertEqual(got, WANT)

    def test_old_words_gone(self) -> None:
        for old in ("케이블 없음", "충전 중단", "충전 시작 중", "전력 없음", "케이블 분리", "전원 끊김"):
            self.assertNotIn(old, HTML, old)
            for f in ("process/alerts.py", "process/__init__.py", "web/__init__.py"):
                self.assertNotIn(old, (ROOT / f).read_text(encoding="utf-8"), (f, old))

    def test_unknown_stays_visible(self) -> None:
        self.assertIn('document.getElementById("m-charge").classList.remove("hidden");', HTML)
        self.assertIn('id="m-charge"', HTML)
        self.assertIn('return "미확인"', _fn("fmtChargeState"))
        self.assertNotIn('"—"', _fn("fmtChargeState"))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_fmt_values(self) -> None:
        src = _fn("fmtChargeState").join([HTML[HTML.index("const CHARGE_KO"):HTML.index("};", HTML.index("const CHARGE_KO")) + 2] + "\n", ""])
        src += "process.stdout.write(JSON.stringify(['Disconnected','NoPower','Starting','Charging','Complete','Stopped','Unknown',null,''].map(fmtChargeState)));"
        out = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, list(WANT.values()) + ["미확인", "미확인", "미확인"])
