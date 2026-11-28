"""v1.21.12: slightly more opaque legend; unified popup body+tail shape."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web/static/naver-map.js").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _block(src: str, start: str, end_mark: str) -> str:
    i = src.index(start)
    j = src.index(end_mark, i)
    return src[i:j]


class VersionTests(unittest.TestCase):
    def test_version_strings(self):
        self.assertIn('const APP_VERSION = "v1.21.12"', HTML)
        self.assertIn("현재 버전은 v1.21.12입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.12`", DESIGN)
        self.assertIn("## v1.21.12", CHANGELOG)
        self.assertIn('{ version: "1.21.12"', HTML)
        self.assertIn("/static/naver-map.js?v=4", HTML)


class LegendStyleTests(unittest.TestCase):
    def test_legend_more_opaque_but_lighter_than_tooltip(self):
        css = _block(HTML, ".trips-map-legend {", ".map-box {")
        self.assertIn("background: rgba(26, 34, 44, 0.50)", css)
        self.assertIn("background: rgba(255, 255, 255, 0.55)", css)
        self.assertNotIn("0.28)", css)
        self.assertNotIn("0.32)", css)
        self.assertGreater(0.78, 0.50)
        self.assertGreater(0.80, 0.55)
        self.assertIn("rgba(38, 49, 62, 0.78)", _block(HTML, ".teslife-map-pop, .ev-pop {", ".map-box .leaflet-popup-content-wrapper"))
        self.assertIn("rgba(255, 255, 255, 0.80)", _block(HTML, ".teslife-map-pop, .ev-pop {", ".map-box .leaflet-popup-content-wrapper"))


class UnifiedPopShapeTests(unittest.TestCase):
    def test_single_svg_path_no_diamond_after(self):
        css = _block(HTML, ".teslife-map-pop, .ev-pop {", ".map-box .leaflet-popup-content-wrapper")
        self.assertNotIn("::after", css)
        self.assertNotIn("rotate(45deg)", css)
        self.assertIn(".teslife-map-pop-shape", css)
        self.assertIn("teslife-map-pop-inner", css)
        self.assertIn("fill: rgba(38, 49, 62, 0.78)", css)
        self.assertIn("fill: rgba(255, 255, 255, 0.80)", css)
        self.assertIn("filter: drop-shadow(", css)
        self.assertIn("background: transparent", css)
        self.assertIn("box-shadow: none", css)
        self.assertIn("border: 0", css)
        self.assertIn("function mapPopPath(", HTML)
        self.assertIn("function ensureMapPopShell(", HTML)
        self.assertIn("function fitMapPopShape(", HTML)
        self.assertIn("hookMapPopShape(map)", HTML)
        self.assertIn("window.fitTeslifeMapPop", HTML)
        self.assertIn("fitTeslifeMapPop", NAVER)
        # one path: line-to tail tip then back, no second filled diamond
        self.assertIn('L" + mid + "," + (body + th)', HTML)
        self.assertIn("createElementNS(\"http://www.w3.org/2000/svg\", \"path\")", HTML)
        self.assertIn("path.setAttribute(\"d\", mapPopPath", HTML)
        # applies to speed, event, charge shells
        self.assertIn('class=\'ev-pop\'', HTML)
        self.assertIn("watchMapPops", HTML)
        self.assertIn("dotTip", HTML)

    def test_docs(self):
        self.assertIn("하나의 SVG 경로", DESIGN)
        self.assertIn("rgba(26, 34, 44, 0.50)", DESIGN)
        self.assertIn("rgba(255, 255, 255, 0.55)", CHANGELOG)


if __name__ == "__main__":
    unittest.main()
