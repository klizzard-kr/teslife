"""v1.21.2: hide the car only when zoomed out; smaller, lighter locate button. No place coordinates."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = HTML.index("function " + name + "(")
    return HTML[start:HTML.index("\n    }\n", start) + 6]


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class HideOnlyWhenZoomedOutTests(unittest.TestCase):
    def test_constants_and_wiring(self) -> None:
        self.assertIn("const CAR_NEAR_PIN_PX = 28;", HTML)
        self.assertIn("const CAR_HIDE_MAX_ZOOM = 15;", HTML)
        self.assertIn("function mapZoomLevel(map)", HTML)
        self.assertIn("typeof map.getZoom !== \"function\"", HTML)
        sync = _between(HTML, "function syncCarNearPin(", "function bindCarOverlap(")
        self.assertIn("hide = carHiddenNearPin(gap, mapZoomLevel(carLocMap))", sync)
        bind = _between(HTML, "function bindCarOverlap(", "function latLngOf(")
        self.assertIn('map.on("zoom", syncCarNearPin)', bind)
        self.assertIn('map.on("zoomend", syncCarNearPin)', bind)
        self.assertIn('map.on("move", syncCarNearPin)', bind)
        self.assertIn('map.on("moveend", syncCarNearPin)', bind)
        self.assertIn("getZoom: function () { return raw.getZoom(); }", NAVER)
        self.assertIn('zoom: "zoom_changed"', NAVER)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_hides_only_at_zoom_15_or_lower(self) -> None:
        src = (
            "const CAR_NEAR_PIN_PX = 28;\n"
            "const CAR_HIDE_MAX_ZOOM = 15;\n"
            + _fn("mapZoomLevel")
            + _fn("screenGap")
            + _fn("carHiddenNearPin")
            + "const overlap = screenGap({x:0,y:0},{x:0,y:0});\n"
            "const almost = 27.9;\n"
            "const edge = 28;\n"
            "const far = screenGap({x:0,y:0},{x:20,y:21});\n"
            "const map16 = { getZoom() { return 16; } };\n"
            "const map18 = { getZoom() { return 18; } };\n"
            "const map15 = { getZoom() { return 15; } };\n"
            "const map14 = { getZoom() { return 14; } };\n"
            "const mapBad = { getZoom() { return Number.NaN; } };\n"
            "const out = {\n"
            "  zoom16: carHiddenNearPin(overlap, mapZoomLevel(map16)),\n"
            "  zoom18: carHiddenNearPin(almost, mapZoomLevel(map18)),\n"
            "  zoom15hide: carHiddenNearPin(almost, mapZoomLevel(map15)),\n"
            "  zoom15edge: carHiddenNearPin(edge, mapZoomLevel(map15)),\n"
            "  zoom14hide: carHiddenNearPin(overlap, mapZoomLevel(map14)),\n"
            "  zoom14far: carHiddenNearPin(far, mapZoomLevel(map14)),\n"
            "  noZoom: carHiddenNearPin(overlap, mapZoomLevel({})),\n"
            "  missing: carHiddenNearPin(overlap, mapZoomLevel(null)),\n"
            "  nanZoom: carHiddenNearPin(overlap, mapZoomLevel(mapBad)),\n"
            "  rawUndefined: carHiddenNearPin(overlap, undefined),\n"
            "  zoom15empty: carHiddenNearPin(screenGap(null, {x:1,y:1}), 15)\n"
            "};\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(
            ["node", "-e", src], capture_output=True, text=True, check=True
        ).stdout)
        self.assertFalse(got["zoom16"])
        self.assertFalse(got["zoom18"])
        self.assertTrue(got["zoom15hide"])
        self.assertFalse(got["zoom15edge"])
        self.assertTrue(got["zoom14hide"])
        self.assertFalse(got["zoom14far"])
        self.assertFalse(got["noZoom"])
        self.assertFalse(got["missing"])
        self.assertFalse(got["nanZoom"])
        self.assertFalse(got["rawUndefined"])
        self.assertFalse(got["zoom15empty"])


class LocateButtonSizeAndThemeTests(unittest.TestCase):
    def test_button_is_32px_with_scaled_mark(self) -> None:
        locate = _between(HTML, "function addCarLocateControl(", "function movePinToCar(")
        self.assertIn("car-loc-cross", locate)
        self.assertIn("width='6'", locate)
        self.assertIn("height='12'", locate)
        self.assertIn("width: 32px; height: 32px;", HTML)
        self.assertNotIn("width: 44px; height: 44px; margin: 0 0 8px; padding: 0; border-radius: 50%;", HTML)
        self.assertIn("width: 26px; height: 26px; display: block; color: var(--loc-btn-fg);", HTML)
        self.assertIn("width: 6px; height: 12px;", HTML)
        self.assertNotIn("background: var(--card); color: var(--text);", _between(
            HTML, "#car-loc-map .car-loc-locate,", "#car-loc-map .car-loc-locate:focus-visible"
        ))

    def test_two_background_variants_exist(self) -> None:
        self.assertIn('data-locate-btn="b"', HTML)
        self.assertIn('#car-loc-map[data-locate-btn="a"]', HTML)
        self.assertIn('#car-loc-map[data-locate-btn="b"]', HTML)
        self.assertIn("rgba(255, 255, 255, 0.90)", HTML)
        self.assertIn("rgba(255, 255, 255, 0.55)", HTML)
        self.assertIn("#3a4550", HTML)
        self.assertNotIn("backdrop-filter: blur(8px);", HTML)
        block = _between(HTML, 'id="car-loc-map"', "hidden></div>")
        self.assertIn('data-locate-btn="b"', block)
        self.assertNotIn('data-locate-btn="a"', block)

    def test_version_notes_and_copy(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.8"', HTML)
        note = _between(HTML, 'version: "1.21.2"', "fixes:")
        self.assertIn("지도를 많이 줄였을 때만", note)
        self.assertIn("크게 보면 차와 핀을 같이", note)
        self.assertIn("줌 버튼과 비슷한 크기", note)
        self.assertIn("배경을 더 옅게", note)
        self.assertIn("지도를 많이 줄였을 때만, 차가 집 핀에 붙으면 핀만 보여요. 크게 보면 차와 핀을 같이 보여요", I18N)
        self.assertIn("위치 버튼: 줌 버튼과 비슷한 크기이고, 배경을 더 옅게 했어요", I18N)
        self.assertIn("## v1.21.2", CHANGELOG)
        self.assertIn("CAR_HIDE_MAX_ZOOM", (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
