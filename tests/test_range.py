""": 주행가능거리 cell next to 주행 in the home 현재 상태 card; hidden when unknown."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class RangeCellTest(unittest.TestCase):
    def test_cell_right_after_trip_and_last(self) -> None:
        home = HTML[HTML.index('id="tab-home"'):HTML.index('id="tab-trips"')]
        labels = [s.split("</div>")[0] for s in home.split('<div class="label">')[1:]]
        tires = [s.split("</div>")[0] for s in home.split('<div class="tw">')[1:]]
        self.assertEqual(labels[:2], ["배터리", "주행가능거리"])
        self.assertEqual(tires[:4], ["앞 좌측", "앞 우측", "뒤 좌측", "뒤 우측"])
        self.assertIn("공조 모드", labels)
        self.assertNotIn("펫", labels)
        self.assertNotIn("캠핑", labels)
        self.assertNotIn("속도", labels)
        self.assertNotIn("기어", labels)
        self.assertNotIn("주행", labels)
        self.assertIn('id="v-fl"', home)
        self.assertLess(home.index("앞 좌측"), home.index('id="v-fl"'))
        self.assertLess(home.index("앞 우측"), home.index('id="v-fr"'))
        self.assertIn('id="m-range"', home)
        self.assertIn('id="m-software"', home)
        self.assertIn('id="m-odo"', home)
        self.assertNotIn('id="v-range">—', home)

    def test_hidden_when_unknown_and_help_in_tip(self) -> None:
        self.assertNotIn('document.getElementById("m-range").classList.toggle("hidden", !rangeText);', HTML)
        self.assertNotIn("—", _fn("fmtRange"))
        self.assertNotIn('id="live-help"', HTML)

    def test_title_is_a_normal_reload_and_live_is_not_gated(self) -> None:
        click = HTML[HTML.rindex('getElementById("brand-home")'):HTML.rindex('getElementById("brand-home")') + 180]
        self.assertIn('location.hash = "home"', click)
        self.assertIn("location.reload()", click)
        for extra in ("wake", "caches", "--full", "reload(true)"):
            self.assertNotIn(extra, click)
        tail = HTML[HTML.rindex("tickTimer = setInterval"):]
        self.assertLess(tail.index('fetchJson("/api/live")'), tail.index("refreshShared()"))
        self.assertLess(tail.index("startStream()"), tail.index("refreshShared()"))
        self.assertIn("불러오는 중", _fn("updateAgo"))
        self.assertIn("불러오지 못했어요", _fn("updateAgo"))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_fmt_values(self) -> None:
        src = "function signalValue(live, name){const s=live.signals&&live.signals[name];return s?s.value:null;}\n" + _fn("fmtRange")
        cases = [
            {"EstBatteryRange": 332.4, "RatedRange": 340},
            {"RatedRange": 331.02},
            {"EstBatteryRange": None, "RatedRange": 0},
            {},
        ]
        src += "const C=" + json.dumps(cases) + ";process.stdout.write(JSON.stringify(C.map(c=>{const sig={};for(const k in c) sig[k]={value:c[k]};return fmtRange({signals:sig});})));"
        out = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, ["약 332 km", "약 331 km", "", ""])
