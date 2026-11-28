"""v1.21.1: locate crosshair, hide the car near the home pin, shorter pin. No place coordinates."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
PIN = (ROOT / "web" / "static" / "map" / "home-pin.svg").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = HTML.index("function " + name + "(")
    return HTML[start:HTML.index("\n    }\n", start) + 6]


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class LocateCrosshairTests(unittest.TestCase):
    def test_button_has_ring_ticks_and_the_car(self) -> None:
        locate = _between(HTML, "function addCarLocateControl(", "function movePinToCar(")
        self.assertIn("car-loc-locate-mark", locate)
        self.assertIn("car-loc-cross", locate)
        self.assertIn("stroke='currentColor'", locate)
        self.assertIn("r='11'", locate)
        self.assertIn("M18 1.6v5.2M18 29.2v5.2M1.6 18h5.2M29.2 18h5.2", locate)
        self.assertIn("CAR_ICON_SRC", locate)
        self.assertIn("car-loc-locate-car", locate)
        self.assertIn("width='6'", locate)
        self.assertIn("height='12'", locate)
        self.assertIn("movePinToCar()", locate)
        self.assertNotIn("pearlT1H", locate)
        self.assertIn(".car-loc-cross {\n      width: 26px; height: 26px; display: block; color: var(--loc-btn-fg);", HTML)
        self.assertIn("width: 6px; height: 12px;", HTML)
        self.assertIn('const APP_VERSION = "v1.21.7"', HTML)
        note = _between(HTML, 'version: "1.21.1"', "fixes:")
        self.assertIn("조준 고리", note)


class CarNearPinTests(unittest.TestCase):
    def test_rule_is_wired_to_zoom_and_move(self) -> None:
        self.assertIn("const CAR_NEAR_PIN_PX = 28;", HTML)
        bind = _between(HTML, "function bindCarOverlap(", "function latLngOf(")
        self.assertIn('map.on("zoom", syncCarNearPin)', bind)
        self.assertIn('map.on("move", syncCarNearPin)', bind)
        self.assertIn('map.on("zoomend", syncCarNearPin)', bind)
        self.assertIn('map.on("moveend", syncCarNearPin)', bind)
        sync = _between(HTML, "function syncCarNearPin(", "function bindCarOverlap(")
        self.assertIn("if (homeMarker && carLocMap)", sync)
        self.assertIn("hide = carHiddenNearPin(gap, mapZoomLevel(carLocMap))", sync)
        self.assertIn("setCarVisible(!hide)", sync)
        self.assertIn("bindCarOverlap(carLocMap)", HTML)
        self.assertIn('zoom: "zoom_changed"', NAVER)
        self.assertIn('move: "center_changed"', NAVER)
        self.assertIn("latLngToContainerPoint || map.latLngToLayerPoint", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_threshold_hides_only_under_28px(self) -> None:
        src = (
            "const CAR_NEAR_PIN_PX = 28;\n"
            "const CAR_HIDE_MAX_ZOOM = 15;\n"
            + _fn("screenGap")
            + _fn("carHiddenNearPin")
            + "const out = {\n"
            "  zero: carHiddenNearPin(screenGap({x:0,y:0},{x:0,y:0}), 15),\n"
            "  under: carHiddenNearPin(27.9, 15),\n"
            "  edge: carHiddenNearPin(28, 15),\n"
            "  over: carHiddenNearPin(screenGap({x:0,y:0},{x:20,y:21}), 15),\n"
            "  empty: carHiddenNearPin(screenGap(null, {x:1,y:1}), 15),\n"
            "  bad: carHiddenNearPin(Number.NaN, 15)\n"
            "};\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(
            ["node", "-e", src], capture_output=True, text=True, check=True
        ).stdout)
        self.assertTrue(got["zero"])
        self.assertTrue(got["under"])
        self.assertFalse(got["edge"])
        self.assertFalse(got["over"])
        self.assertFalse(got["empty"])
        self.assertFalse(got["bad"])


class ShortHomePinTests(unittest.TestCase):
    def test_needle_is_shorter_and_the_tip_is_the_anchor(self) -> None:
        self.assertIn('viewBox="0 0 80 98"', PIN)
        self.assertIn("94.4", PIN)
        self.assertIn('cx="40" cy="32" r="22"', PIN)
        self.assertIn("headGrad", PIN)
        self.assertIn('id="headGrad"', PIN)
        old = 123.5 - 46
        new = 94.4 - 46
        cut = 1 - (new / old)
        self.assertGreaterEqual(cut, 0.35)
        self.assertLessEqual(cut, 0.40)
        pin = _between(HTML, "function homePinIcon()", "function screenGap(")
        self.assertIn("iconSize: [40, 49]", pin)
        self.assertIn("iconAnchor: [20, 47.2]", pin)
        self.assertIn("width='40' height='49'", pin)
        self.assertAlmostEqual(94.4 / 98 * 49, 47.2, places=2)
        self.assertIn(".car-loc-pin img { display: block; width: 40px; height: 49px;", HTML)


if __name__ == "__main__":
    unittest.main()
