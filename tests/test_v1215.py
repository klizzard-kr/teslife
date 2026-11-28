"""v1.21.5: vehicle-location card, map received time, pin icons, temps, footer.

Tests use a made-up neighbourhood name only. No real place names.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from process import connect
from process.geocode import (
    cache_key,
    dong_label,
    fill_car_area_label,
    fill_trip_labels,
    place_label,
)
from web.carloc import car_location_payload

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")

VID = "demo-car"
# Made-up point for cache-cell tests only. Not a real place.
LAT, LON = 37.4000, 127.1000
NEAR_LAT, NEAR_LON = 37.4004, 127.1000  # same 3-decimal cell
FAR_LAT, FAR_LON = 37.4015, 127.1000  # next cell
TS = "2026-11-27T12:26:00Z"
DONG = "한빛동"
DONG_EN = "Hanbit-dong"


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _node(expr: str) -> str:
    src = I18N + "\nprocess.stdout.write(String((" + expr + ")));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return out.stdout


def _db() -> tuple[sqlite3.Connection, str]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = connect(tmp.name)
    return conn, tmp.name


def _put_location(conn: sqlite3.Connection, lat: float, lon: float, ts: str = TS) -> None:
    conn.execute(
        "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
        (VID, ts, json.dumps({"lat": lat, "lon": lon})),
    )
    conn.commit()


def _put_home(conn: sqlite3.Connection, lat: float, lon: float, radius: int = 150) -> None:
    conn.execute(
        "INSERT INTO places (vehicle_id, name, lat, lon, radius_m, created_at) "
        "VALUES (?, '집', ?, ?, ?, ?)",
        (VID, lat, lon, radius, TS),
    )
    conn.commit()


def _put_cache(conn: sqlite3.Connection, lat: float, lon: float,
               label: str | None, label_en: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (provider, key, label, label_en, fetched_at) "
        "VALUES ('fake', ?, ?, ?, ?)",
        (cache_key(lat, lon), label, label_en, TS),
    )
    conn.commit()


class CardMarkupTests(unittest.TestCase):
    def test_card_matches_vehicle_card_and_stays_open(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.7"', HTML)
        loc = HTML[HTML.index('id="car-loc-card"'):HTML.index('id="home-foot"')]
        self.assertIn("<h2>현재 위치</h2>", loc)
        self.assertIn('class="live-grid home-hero"', loc)
        self.assertIn('<div class="label">위치</div>', loc)
        self.assertIn('class="value" id="car-loc-area"', loc)
        self.assertIn('<div class="label">상태</div>', loc)
        self.assertIn('class="value" id="car-loc-state"', loc)
        self.assertIn('id="car-loc-place"', loc)
        self.assertIn('id="car-loc-drive"', loc)
        self.assertNotIn("<details", loc)
        self.assertNotIn("car-loc-fold", HTML)
        self.assertNotIn("car-loc-title", HTML)
        self.assertNotIn(".car-loc-status {", HTML)
        self.assertNotIn("car-loc-clock", HTML)
        self.assertNotIn("car-loc-when", HTML)
        self.assertNotIn("#car-loc-card > details > .mode-row { grid-template-columns: 1fr; }", HTML)
        self.assertIn("#car-loc-card .mode-row > * { grid-column: 1 / -1; }", HTML)
        title = _between(HTML, "#tab-home h2 {", "}")
        self.assertIn("font-size: 1.125rem", title)
        self.assertIn("font-weight: 600", title)
        value = _between(HTML, ".metric .value {", "}")
        self.assertIn("font-size: 1.25rem", value)
        hero = _between(HTML, ".home-hero {", "}")
        self.assertIn("grid-template-columns: 1fr 1fr", hero)
        self.assertIn("Date.now() - carLocAt < 30000", HTML)
        self.assertIn("function setLocArea", HTML)
        self.assertIn("function homeMarkIcon", HTML)
        self.assertIn('"주행 중"', HTML)
        self.assertIn('"주차 중"', HTML)
        self.assertIn("data.driving === true", HTML)
        self.assertIn('class="mode-row"', loc)
        self.assertIn("showCarLocMap();", HTML[HTML.index("function paintCarLoc"):HTML.index("function refreshCarLoc")])

    def test_map_time_fades_and_avoids_naver_zoom(self) -> None:
        heard = _between(HTML, "#car-loc-heard {", "}")
        self.assertIn("top: 10px", heard)
        self.assertIn("right: 10px", heard)
        self.assertIn("font-size: 11px", heard)
        self.assertIn("rgba(255, 255, 255, 0.72)", heard)
        self.assertIn("opacity: 0", heard)
        self.assertIn("#car-loc-heard.is-on { opacity: 1; }", HTML)
        self.assertNotIn(".car-loc-map-host.is-naver #car-loc-heard", HTML)
        self.assertIn("LOC_HEARD_MS = 3000", HTML)
        self.assertIn("function showLocHeard", HTML)
        self.assertIn("function bindLocHeard", HTML)
        self.assertIn("function fmtTodayOrAgo", HTML)
        self.assertIn("fmtTodayOrAgo(data.received_at)", HTML)
        self.assertIn('"위치 수신 " + label', HTML)
        self.assertNotIn("fmtHm(data.received_at)", HTML)
        self.assertIn("map.on(\"click\", reveal)", HTML)
        self.assertIn("map.on(\"zoom\", reveal)", HTML)
        self.assertIn("map.on(\"move\", reveal)", HTML)
        self.assertIn("pointerdown", HTML)
        self.assertIn("touchstart", HTML)

    def test_pin_icons_and_confirms(self) -> None:
        self.assertIn("car-loc-pin-action", HTML)
        self.assertIn("선택한 위치를 집으로 저장할까요?", HTML)
        self.assertIn("집으로 저장된 위치를 삭제할까요?", HTML)
        self.assertIn('writeJson("PUT", "/api/home"', HTML)
        self.assertIn('writeJson("DELETE", "/api/home"', HTML)
        block = HTML[HTML.index("#car-loc-map .car-loc-pin-action,"):HTML.index("#car-loc-map .car-loc-pin-action svg")]
        self.assertIn("min-height: 36px", block)
        self.assertIn("padding: 0", block)
        self.assertIn("-webkit-appearance: none; appearance: none", block)
        self.assertIn("width: 36px", block)
        self.assertNotIn('id="car-loc-save-pin"', HTML)
        self.assertNotIn('id="car-loc-delete"', HTML)

    def test_temps_stay_in_their_own_card(self) -> None:
        live = HTML.index('id="live-card"')
        tires = HTML.index('id="tire-card"')
        temp = HTML.index('id="temp-card"')
        loc = HTML.index('id="car-loc-card"')
        self.assertLess(live, tires)
        self.assertLess(tires, temp)
        self.assertLess(temp, loc)
        self.assertNotIn('id="temp-row"', HTML)
        chunk = HTML[temp:loc]
        self.assertIn('id="m-inside"', chunk)
        self.assertIn('id="m-outside"', chunk)
        self.assertIn("실내 온도", chunk)
        self.assertIn("실외 온도", chunk)
        self.assertNotIn('id="m-inside"', HTML[live:tires])

    def test_footer_uses_vehicle_received_24h(self) -> None:
        ago = _between(HTML, "function fmtTodayOrAgo(", "function fillTable(")
        self.assertIn("hour12: false", ago)
        self.assertIn("fmtAgo(ts)", ago)
        self.assertIn('"차량 수신 " + fmtTodayOrAgo(lastTs)', ago)
        self.assertIn("fmtTodayOrAgo(data.received_at)", HTML)
        self.assertNotIn("fmtAmpmKo(lastTs)", ago)

    def test_docs_use_the_made_up_name_only(self) -> None:
        self.assertIn("## v1.21.5", CHANGELOG)
        self.assertIn("현재 위치", CHANGELOG)
        self.assertIn("위치 수신", CHANGELOG)
        self.assertIn("차량 수신", CHANGELOG)
        self.assertIn("주차 중", CHANGELOG)
        self.assertIn("현재 위치", DESIGN)
        self.assertIn("주차 중", DESIGN)
        banned = "".join(chr(n) for n in (0xC18C, 0xC0AC, 0xBCF8, 0xB3D9))
        for path in (ROOT / "web", ROOT / "docs", ROOT / "process", ROOT / "CHANGELOG.md", ROOT / "README.md"):
            paths = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
            for item in paths:
                if item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2"}:
                    continue
                try:
                    text = item.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                self.assertNotIn(banned, text, msg=str(item.relative_to(ROOT)))


class I18nTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_lines(self) -> None:
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("현재 위치")'),
            "Current location",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("위치")'),
            "Location",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("상태")'),
            "Status",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("주차 중")'),
            "Parked",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("주행 중")'),
            "Driving",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("한빛동 (집)")'),
            "한빛동 (Home)",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("집")'),
            "Home",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("위치 수신 21:26")'),
            "Location received 21:26",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("차량 수신 21:26")'),
            "Updated 21:26",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("선택한 위치를 집으로 저장할까요?")'),
            "Save the selected spot as home?",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("집으로 저장된 위치를 삭제할까요?")'),
            "Delete the saved home location?",
        )


class AreaCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.db = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: os.path.exists(self.db) and os.remove(self.db))

    def test_dong_label_reads_cache_and_ignores_home_place(self) -> None:
        _put_home(self.conn, LAT, LON)
        self.assertIsNone(dong_label(self.conn, LAT, LON, "ko"))
        self.assertEqual(place_label(self.conn, VID, LAT, LON), "집")
        _put_cache(self.conn, LAT, LON, "가상시 한빛동", "Hanbit-dong")
        self.assertEqual(dong_label(self.conn, LAT, LON, "ko"), DONG)
        self.assertEqual(dong_label(self.conn, LAT, LON, "en"), DONG_EN)
        self.assertEqual(dong_label(self.conn, NEAR_LAT, NEAR_LON, "ko"), DONG)
        self.assertIsNone(dong_label(self.conn, FAR_LAT, FAR_LON, "ko"))
        self.assertEqual(place_label(self.conn, VID, LAT, LON), "집")

    def test_driving_follows_the_latest_gear(self) -> None:
        _put_location(self.conn, LAT, LON)
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertIsNone(data["driving"])
        self.conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', ?)",
            (VID, TS, json.dumps("P")),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertFalse(data["driving"])
        self.conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', ?)",
            (VID, "2026-11-27T12:30:00Z", json.dumps("D")),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertTrue(data["driving"])

    def test_payload_is_cache_read_only(self) -> None:

        _put_location(self.conn, LAT, LON)
        _put_home(self.conn, LAT, LON)
        self.conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'LocatedAtHome', ?)",
            (VID, TS, "true"),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertIsNone(data["area"])
        self.assertIsNone(data["area_en"])
        self.assertIsNone(data["driving"])
        self.assertNotIn("address", data)
        _put_cache(self.conn, LAT, LON, DONG, DONG_EN)
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertEqual(data["area"], DONG)
        self.assertEqual(data["area_en"], DONG_EN)
        self.assertTrue(data["located_at_home"])

    def test_fill_uses_the_latest_location_once_per_cell(self) -> None:
        _put_location(self.conn, LAT, LON, "2026-11-27T12:00:00Z")
        _put_location(self.conn, NEAR_LAT, NEAR_LON, TS)
        calls: list[tuple[float, float]] = []

        class Fake:
            name = "fake"

            def reverse_labels(self, lat, lon):
                calls.append((lat, lon))
                return DONG, DONG_EN

        self.assertEqual(fill_car_area_label(self.conn, VID, Fake()), 2)
        self.assertEqual(calls, [(NEAR_LAT, NEAR_LON)])
        self.assertEqual(fill_car_area_label(self.conn, VID, Fake()), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(dong_label(self.conn, LAT, LON, "ko"), DONG)
        self.assertEqual(dong_label(self.conn, LAT, LON, "en"), DONG_EN)

        # A later point in a new cell is a new lookup. Nearby points stay cached.
        _put_location(self.conn, FAR_LAT, FAR_LON, "2026-11-27T12:30:00Z")
        self.assertEqual(fill_car_area_label(self.conn, VID, Fake()), 2)
        self.assertEqual(calls[-1], (FAR_LAT, FAR_LON))
        self.assertEqual(len(calls), 2)

    def test_trip_fill_keeps_its_own_budget_when_car_label_is_already_cached(self) -> None:
        _put_location(self.conn, LAT, LON)
        _put_cache(self.conn, LAT, LON, DONG, DONG_EN)
        # A fully filled hierarchy (empty higher units) is not fetched again,
        # so the leftover budget goes to the trip point.
        self.conn.execute(
            "UPDATE geocode_cache SET gu='', gu_en='', si='', si_en='', sido='', sido_en='', "
            "road_addr='', jibun_addr='' WHERE key = ?",
            ("37.400,127.100",),
        )
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, start_lat, start_lon) "
            "VALUES (?, '2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', 1.2, 37.4100, 127.1100)",
            (VID,),
        )
        self.conn.commit()
        calls: list[tuple[float, float]] = []

        class Fake:
            name = "fake"

            def reverse(self, lat, lon):
                calls.append((lat, lon))
                return "달빛동"

        used = fill_car_area_label(self.conn, VID, Fake())
        self.assertEqual(used, 0)
        self.assertEqual(fill_trip_labels(self.conn, VID, Fake(), limit=4 - used), 1)
        self.assertEqual(calls, [(37.4100, 127.1100)])
        self.assertEqual(dong_label(self.conn, 37.4100, 127.1100, "ko"), "달빛동")


if __name__ == "__main__":
    unittest.main()
