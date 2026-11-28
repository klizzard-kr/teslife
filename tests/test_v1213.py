"""v1.21.3: locate button stays a 32px circle inside the map, dark ring on a light map."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class LocateButtonCircleTests(unittest.TestCase):
    def test_overrides_global_button_min_height(self) -> None:
        self.assertIn("min-height: 48px;", HTML)
        css = _between(HTML, "#car-loc-map .car-loc-locate,", "#car-loc-map .car-loc-locate:focus-visible")
        self.assertIn("width: 32px; height: 32px;", css)
        self.assertIn("min-width: 32px; min-height: 32px;", css)
        self.assertIn("max-width: 32px; max-height: 32px;", css)
        self.assertIn("box-sizing: border-box;", css)
        self.assertIn("padding: 0;", css)
        self.assertIn("margin: 0;", css)
        self.assertIn("-webkit-appearance: none; appearance: none;", css)
        self.assertIn("overflow: hidden;", css)
        self.assertIn("flex: none;", css)
        self.assertNotIn("margin: 0 0 8px;", css)
        self.assertNotIn("backdrop-filter", css)

    def test_sits_10px_inside_the_map(self) -> None:
        self.assertIn('div.style[bottom ? "bottom" : "top"] = "10px";', NAVER)
        self.assertNotIn('bottom ? "36px" : "10px"', NAVER)
        self.assertIn('#car-loc-map .teslife-map-corner[data-teslife-corner="bottomright"] {\n      bottom: 10px; right: 10px;', HTML)
        self.assertIn("#car-loc-map .leaflet-bottom.leaflet-right {\n      margin: 0; bottom: 10px; right: 10px;", HTML)

    def test_style_b_is_readable_on_a_light_map(self) -> None:
        block = _between(HTML, 'id="car-loc-map"', "hidden></div>")
        self.assertIn('data-locate-btn="b"', block)
        b = _between(HTML, '/* locate-btn-b:', "*/")
        # values live in the rule after the comment
        rule = _between(HTML, '#car-loc-map[data-locate-btn="b"] {', "}")
        self.assertIn("rgba(255, 255, 255, 0.55)", rule)
        self.assertIn("#3a4550", rule)
        self.assertIn("rgba(15, 20, 25, 0.14)", rule)
        self.assertIn("0 1px 3px rgba(15, 20, 25, 0.18)", rule)
        self.assertNotIn("#e8eef4", rule)
        self.assertNotIn("rgba(232, 238, 244, 0.28)", rule)

    def test_version_notes(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.8"', HTML)
        note = _between(HTML, 'version: "1.21.3"', "fixes:")
        # features array is empty; the text is in fixes, still inside this slice? 
        # _between to "fixes:" would be the features array only. Check the whole entry.
        entry = _between(HTML, 'version: "1.21.3"', "version: \"1.21.2\"")
        self.assertIn("지도 안쪽에 32픽셀 원으로 보여요", entry)
        self.assertIn("고리는 진한 회색이라 밝은 지도에서도 보여요", entry)
        self.assertIn("위치 버튼: 지도 안쪽에 32픽셀 원으로 보여요. 고리는 진한 회색이라 밝은 지도에서도 보여요", I18N)
        self.assertIn("## v1.21.3", CHANGELOG)
        self.assertIn("min-height: 48px", CHANGELOG)


if __name__ == "__main__":
    unittest.main()
