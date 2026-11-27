"""v1.21.0: car picture and home pin on the location map. No place coordinates."""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from process import connect
from web import STATIC_DIR, STATIC_FILES, make_server, serve_in_thread

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
CAR_URL = "/static/map/car-t1h.svg?v=1.21.0"
PIN_URL = "/static/map/home-pin.svg?v=1.21.0"


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


TPMS_URL = "/static/tire/tpms-car.svg?v=1.21.0"
TIRE_IDS = ("tire-fl", "tire-fr", "tire-rl", "tire-rr")


class CarMapArtTests(unittest.TestCase):
    def test_asset_files_exist_and_are_referenced(self) -> None:
        car = ROOT / "web" / "static" / "map" / "car-t1h.svg"
        pin = ROOT / "web" / "static" / "map" / "home-pin.svg"
        self.assertTrue(car.is_file())
        self.assertTrue(pin.is_file())
        car_svg = car.read_text(encoding="utf-8")
        pin_svg = pin.read_text(encoding="utf-8")
        self.assertIn('viewBox="-10 -8 112 224"', car_svg)
        self.assertIn('viewBox="0 0 80 128"', pin_svg)
        self.assertIn("123.5", pin_svg)
        self.assertEqual(HTML.count(f'"{CAR_URL}"'), 1)
        self.assertEqual(HTML.count(f'"{PIN_URL}"'), 1)
        self.assertIn(f'const CAR_ICON_SRC = "{CAR_URL}"', HTML)
        self.assertIn(f'const HOME_PIN_SRC = "{PIN_URL}"', HTML)
        self.assertNotIn("pearlT1H", HTML)
        self.assertNotIn("CAR_ARROW_SVG", HTML)
        self.assertNotIn("LOCATE_SVG", HTML)
        self.assertNotIn("HOME_PIN_SVG", HTML)
        self.assertNotIn("M18 3.2", HTML)
        self.assertEqual(STATIC_FILES["/static/map/car-t1h.svg"], ("map/car-t1h.svg", "image/svg+xml"))
        self.assertEqual(STATIC_FILES["/static/map/home-pin.svg"], ("map/home-pin.svg", "image/svg+xml"))
        self.assertEqual(STATIC_DIR / "map" / "car-t1h.svg", car)
        self.assertTrue((STATIC_DIR / "map" / "home-pin.svg").is_file())

    def test_locate_button_uses_the_same_car_image(self) -> None:
        marker = _between(HTML, "function carArrowIcon()", "function pointCar(")
        locate = _between(HTML, "function addCarLocateControl(", "function movePinToCar(")
        self.assertIn("<img", marker)
        self.assertIn("CAR_ICON_SRC", marker)
        self.assertIn("width='20'", marker)
        self.assertIn("height='40'", marker)
        self.assertIn("car-loc-arrow", marker)
        self.assertIn("<img", locate)
        self.assertIn("CAR_ICON_SRC", locate)
        self.assertIn("width='14'", locate)
        self.assertIn("height='28'", locate)
        self.assertNotIn("<svg", marker)
        self.assertNotIn("<svg", locate)
        self.assertIn("#car-loc-map .car-loc-locate img { width: 14px; height: 28px;", HTML)

    def test_pin_and_car_anchors(self) -> None:
        pin = _between(HTML, "function homePinIcon()", "function latLngOf(")
        marker = _between(HTML, "function carArrowIcon()", "function pointCar(")
        self.assertIn("iconSize: [40, 64]", pin)
        self.assertIn("iconAnchor: [20, 61.75]", pin)
        self.assertIn("HOME_PIN_SRC", pin)
        self.assertIn("iconSize: [20, 40]", marker)
        self.assertIn("iconAnchor: [10, 19.3]", marker)
        self.assertNotIn("iconAnchor: [16, 40]", HTML)
        block = _between(
            HTML,
            "// ---------------------------------------------------------------- car location",
            "// ---------------------------------------------------------------- refresh plumbing",
        )
        self.assertIn("draggable: true, icon: homePinIcon()", block)

    def test_null_heading_stays_unrotated(self) -> None:
        heading = _between(HTML, "function headingDeg(", "function carArrowIcon(")
        self.assertIn("data.heading == null", heading)
        self.assertIn('data.heading === ""', heading)
        self.assertIn("return Number.isFinite(n) ? n : null", heading)
        point = _between(HTML, "function pointCar(", "function homePinIcon(")
        self.assertIn("setRotationAngle(deg == null ? 0 : deg)", point)
        self.assertIn('node.style.transform = deg == null ? "" : ("rotate(" + deg + "deg)")', point)
        self.assertIn('el.querySelector(".car-loc-arrow")', point)
        paint = _between(HTML, "function paintCarMarkers()", "function addCarLocateControl(")
        self.assertIn("const aim = headingDeg(data)", paint)
        self.assertIn("pointCar(carMarker, aim)", paint)

    def test_card_still_uses_raw_map_and_map_lib(self) -> None:
        block = _between(
            HTML,
            "// ---------------------------------------------------------------- car location",
            "// ---------------------------------------------------------------- refresh plumbing",
        )
        self.assertIn("rawMap(el)", block)
        self.assertIn("mapLib()", block)
        self.assertNotIn("loadLeaflet()", block)
        self.assertNotIn("L.map(", block)
        self.assertIn(".leaflet-div-icon.car-loc-car, .car-loc-car { overflow: visible; }", HTML)
        self.assertNotIn("#2f7dcc", HTML)
        self.assertNotIn("#145a9c", HTML)
        self.assertIn('const APP_VERSION = "v1.21.0"', HTML)
        note = _between(HTML, 'version: "1.21.0"', "fixes:")
        self.assertIn("위에서 본 차 그림", note)
        self.assertIn("파란 압정", note)
        old = _between(HTML, 'version: "1.20.0"', "fixes:")
        self.assertIn("화살표", old)

    def test_static_routes_serve_the_files(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        conn = connect(tmp.name)
        conn.close()
        server = make_server(tmp.name, host="127.0.0.1", port=0, process_interval=0, seed=False)
        port = server.server_address[1]
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        for path, needle in (
            (CAR_URL, b'viewBox="-10 -8 112 224"'),
            (PIN_URL, b'viewBox="0 0 80 128"'),
        ):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as res:
                self.assertEqual(res.status, 200, path)
                self.assertEqual(res.headers.get("Content-Type"), "image/svg+xml", path)
                self.assertIn(needle, res.read())


class TirePressureCarTests(unittest.TestCase):
    def test_asset_has_the_four_tire_ids(self) -> None:
        path = ROOT / "web" / "static" / "tire" / "tpms-car.svg"
        self.assertTrue(path.is_file())
        svg = path.read_text(encoding="utf-8")
        self.assertIn('viewBox="-5 -4 101.4 208"', svg)
        self.assertIn('stroke="currentColor"', svg)
        self.assertIn('id="seam-windshield"', svg)
        self.assertIn('id="seam-rear-glass"', svg)
        self.assertIn('id="glass"', svg)
        for tire_id in TIRE_IDS:
            self.assertIn(f'id="{tire_id}" class="tire"', svg)
        self.assertEqual(STATIC_FILES["/static/tire/tpms-car.svg"], ("tire/tpms-car.svg", "image/svg+xml"))
        self.assertIn(f'const TPMS_CAR_URL = "{TPMS_URL}"', HTML)
        self.assertEqual(HTML.count(TPMS_URL), 1)

    def test_warning_class_hook_colors_the_tire_shape(self) -> None:
        prep = _between(HTML, "function prepareTpmsCar(", "function carTopSvg(")
        for wheel in ("fl", "fr", "rl", "rr"):
            self.assertIn(f'id="tire-\' + wheel + \'"', prep)
            self.assertIn('data-wheel="\' + wheel + \'"', prep)
        hook = _between(HTML, "function paintTireAlert(", "function updateAgo(")
        self.assertIn('"#home-tires [data-wheel]"', hook)
        self.assertIn('el.classList.toggle("is-low"', hook)
        self.assertIn("shown[wheel] < lowPsi", hook)
        load = _between(HTML, "function loadTpmsCar(", "function fmtTripDistance(")
        self.assertIn("home.innerHTML = tpmsCarSvg", load)
        self.assertIn("paintTireAlert(lastPayload)", load)
        self.assertIn(".model-y .tire.is-low { stroke: var(--tire-warn); fill: var(--tire-warn); fill-opacity: 0.28; }", HTML)
        self.assertIn("--tire-warn: #FF6B5E;", HTML)
        self.assertIn("--tire-warn: #D93025;", HTML)
        self.assertIn(".tire-cell.is-low .tv, .tire-cell.is-low .tw { color: var(--tire-warn); }", HTML)
        board = _between(HTML, 'id="home-tires"', 'id="tire-note"')
        self.assertLess(board.index('id="m-fl"'), board.index('id="home-car"'))
        self.assertLess(board.index('id="m-fr"'), board.index('id="home-car"'))
        self.assertLess(board.index('id="home-car"'), board.index('id="m-rl"'))
        self.assertLess(board.index('id="home-car"'), board.index('id="m-rr"'))
        self.assertIn('id="v-fl"', board)
        self.assertIn('id="v-rr"', board)
        note = _between(HTML, 'version: "1.21.0"', "fixes:")
        self.assertIn("낮은 바퀴는 붉은 선", note)

    def test_tpms_car_is_served(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        conn = connect(tmp.name)
        conn.close()
        server = make_server(tmp.name, host="127.0.0.1", port=0, process_interval=0, seed=False)
        port = server.server_address[1]
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{TPMS_URL}", timeout=5) as res:
            self.assertEqual(res.status, 200)
            self.assertEqual(res.headers.get("Content-Type"), "image/svg+xml")
            body = res.read().decode("utf-8")
        for tire_id in TIRE_IDS:
            self.assertIn(f'id="{tire_id}"', body)


if __name__ == "__main__":
    unittest.main()
