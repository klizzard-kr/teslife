"""v1.21.11: five speed bands, smaller translucent legend, slightly lighter opaque pops."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web/static/i18n.js").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _block(src: str, start: str, end_mark: str) -> str:
    i = src.index(start)
    j = src.index(end_mark, i)
    return src[i:j]


def speed_band(n):
    """Mirror of speedBand: n < 20 / 50 / 80 / 100, else 100 and over."""
    bands = [
        (20, "20 km/h 미만"),
        (50, "20–50 km/h"),
        (80, "50–80 km/h"),
        (100, "80–100 km/h"),
        (None, "100 km/h 이상"),
    ]
    for mx, lab in bands:
        if mx is None or n < mx:
            return lab
    return bands[-1][1]


class VersionTests(unittest.TestCase):
    def test_version_strings(self):
        self.assertIn('const APP_VERSION = "v1.21.12"', HTML)
        self.assertIn("현재 버전은 v1.21.12입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.12`", DESIGN)
        self.assertIn("## v1.21.11", CHANGELOG)
        self.assertIn('{ version: "1.21.11"', HTML)
        self.assertIn("/static/i18n.js?v=12", HTML)


class SpeedBandTests(unittest.TestCase):
    def test_five_buckets_and_colors(self):
        m = re.search(r"const SPEED_BANDS = \[(.*?)\];", HTML, re.S)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertIn('label: "20 km/h 미만"', block)
        self.assertIn('label: "20–50 km/h"', block)
        self.assertIn('label: "50–80 km/h"', block)
        self.assertIn('label: "80–100 km/h"', block)
        self.assertIn('label: "100 km/h 이상"', block)
        self.assertNotIn("30 km/h 이하", HTML)
        self.assertNotIn("30–60 km/h", HTML)
        self.assertNotIn("60–80 km/h", HTML)
        self.assertNotIn("100 km/h 초과", HTML)
        self.assertIn('color: "#3dd68c"', block)
        self.assertIn('color: "#5b9fd6"', block)
        self.assertIn('color: "#f0b429"', block)
        self.assertIn('color: "#f97316"', block)
        self.assertIn('color: "#ef4444"', block)
        self.assertEqual(block.count("label:"), 5)

    def test_boundary_rule_in_source(self):
        fn = _block(HTML, "function speedBand(v)", "function havem")
        self.assertIn("b.max === Infinity || n < b.max", fn)
        self.assertNotIn("n <= b.max", fn)

    def test_boundaries(self):
        self.assertEqual(speed_band(0), "20 km/h 미만")
        self.assertEqual(speed_band(19.9), "20 km/h 미만")
        self.assertEqual(speed_band(20), "20–50 km/h")
        self.assertEqual(speed_band(49.9), "20–50 km/h")
        self.assertEqual(speed_band(50), "50–80 km/h")
        self.assertEqual(speed_band(79.9), "50–80 km/h")
        self.assertEqual(speed_band(80), "80–100 km/h")
        self.assertEqual(speed_band(99.9), "80–100 km/h")
        self.assertEqual(speed_band(100), "100 km/h 이상")
        self.assertEqual(speed_band(140), "100 km/h 이상")

    def test_docs_and_i18n(self):
        self.assertIn("20 km/h 미만", DESIGN)
        self.assertIn("정확히 20은 20–50", DESIGN)
        self.assertIn("정확히 100은 100 이상", DESIGN)
        self.assertIn('"20 km/h 미만": "under 20 km/h"', I18N)
        self.assertIn('"20–50 km/h": "20–50 km/h"', I18N)
        self.assertIn('"50–80 km/h": "50–80 km/h"', I18N)
        self.assertIn('"80–100 km/h": "80–100 km/h"', I18N)
        self.assertIn('"100 km/h 이상": "100 km/h and over"', I18N)
        self.assertIn("20 km/h 미만", CHANGELOG)


class LegendAndPopStyleTests(unittest.TestCase):
    def test_legend_third_version_no_border(self):
        css = _block(HTML, ".trips-map-legend {", ".map-box {")
        self.assertIn("font-size: 0.62rem", css)
        self.assertIn("padding: 4px 6px", css)
        # 바탕 농도는 v1.21.12에서 다시 맞춤. 여기서는 테마별 반투명만 확인한다.
        self.assertIn("rgba(26, 34, 44,", css)
        self.assertIn("rgba(255, 255, 255,", css)
        self.assertIn("color: #f4f8fc", css)
        self.assertIn("color: #12181e", css)
        self.assertIn("text-shadow: 0 1px 2px rgba(0, 0, 0, 0.7)", css)
        self.assertIn("text-shadow: 0 1px 1px rgba(255, 255, 255, 0.9)", css)
        self.assertIn("border: 0", css)
        self.assertNotIn("1px solid var(--line)", css)
        self.assertNotIn("rgba(18, 24, 30, 0.14)", css)
        self.assertIn("width: 6px; height: 6px", css)
        self.assertIn("gap: 3px", css)
        line = [ln for ln in HTML.splitlines() if ".trips-map-legend i.line" in ln][0]
        self.assertIn("width: 11px; height: 3px", line)

    def test_pop_third_version_no_border(self):
        css = _block(HTML, ".teslife-map-pop, .ev-pop {", ".map-box .leaflet-popup-content-wrapper")
        self.assertIn("font-size: 0.72rem", css)
        self.assertIn("padding: 6px 9px 8px", css)
        self.assertIn("min-width: 132px", css)
        self.assertIn("max-width: 220px", css)
        self.assertIn("rgba(38, 49, 62, 0.78)", css)
        self.assertIn("rgba(255, 255, 255, 0.80)", css)
        self.assertIn("color: #f4f8fc", css)
        self.assertIn("color: #12181e", css)
        self.assertIn("color: #c8d6e4", css)
        self.assertIn("color: #3a4a58", css)
        self.assertIn("text-shadow: 0 1px 2px rgba(0, 0, 0, 0.7)", css)
        self.assertIn("text-shadow: 0 1px 1px rgba(255, 255, 255, 0.9)", css)
        self.assertIn("border: 0", css)
        self.assertIn("border: 0", css)
        self.assertNotIn("border-right:", css)
        self.assertNotIn("border-bottom:", css)
        self.assertNotIn("1px solid var(--line)", css)
        self.assertNotIn("rgba(18, 24, 30, 0.14)", css)
        self.assertGreater(0.78, 0.28)
        self.assertGreater(0.80, 0.32)
        self.assertNotIn("background: var(--card)", css)
        bands = _block(HTML, "const SPEED_BANDS = [", "function speedBand")
        for color in ("#3dd68c", "#5b9fd6", "#f0b429", "#f97316", "#ef4444"):
            self.assertIn(color, bands)
        naver = (ROOT / "web/static/naver-map.js").read_text(encoding="utf-8")
        self.assertIn('borderColor: "transparent"', naver)
        self.assertIn("borderWidth: 0", naver)
        self.assertIn('backgroundColor: "transparent"', naver)
        self.assertIn("anchorSize: new maps.Size(0, 0)", naver)

if __name__ == "__main__":
    unittest.main()
