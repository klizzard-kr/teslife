"""v1.21.6: no map divider, heard label top-right, house icon, admin hierarchy.

Tests use made-up place names only (한빛동, 별빛구, 해솔시, 한들도). No real places.
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
    AdminParts,
    admin_display,
    fill_car_area_label,
    parts_from_area_names,
    parts_from_naver_payload,
    parts_from_nominatim,
    primary_and_up,
    format_road_addr,
    format_jibun_addr,
    addresses_from_naver_payload,
    cached_addresses,
)
from web import label_trips
from web.carloc import car_location_payload

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _node(expr: str) -> str:
    src = I18N + "\nprocess.stdout.write(String((" + expr + ")));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return out.stdout


class CardTests(unittest.TestCase):
    def test_version_and_no_divider(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.8"', HTML)
        loc_css = _between(HTML, "#car-loc-card .mode-row {", "}")
        self.assertIn("border-top: none", loc_css)
        self.assertIn("padding-top: 0", loc_css)
        self.assertNotIn("border-top: 1px", loc_css)
        self.assertIn("## v1.21.6", CHANGELOG)
        self.assertIn("구분선", CHANGELOG)
        self.assertIn("구분선이 없다", DESIGN)

    def test_heard_stays_top_right_on_both_maps(self) -> None:
        heard = _between(HTML, "#car-loc-heard {", "}")
        self.assertIn("top: 10px", heard)
        self.assertIn("right: 10px", heard)
        self.assertNotIn("left: 10px", heard)
        self.assertNotIn(".car-loc-map-host.is-naver #car-loc-heard", HTML)
        self.assertIn("zoomControlOptions: { position: maps.Position.TOP_LEFT }", NAVER)
        self.assertIn("Position.TOP_LEFT", DESIGN)

    def test_home_icon_replaces_home_suffix(self) -> None:
        self.assertIn("function homeMarkIcon", HTML)
        self.assertIn("function setLocArea", HTML)
        self.assertNotIn('area + " (집)"', HTML)
        self.assertIn("car-loc-home-icon", HTML)
        icon = _between(HTML, "#car-loc-area .car-loc-home-icon {", "}")
        self.assertIn("width: 1em", icon)
        self.assertIn("height: 1em", icon)
        # same path as the pin save house
        self.assertIn("M4 11.5 12 4.5l8 7V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z", HTML)
        self.assertGreaterEqual(HTML.count("M4 11.5 12 4.5l8 7V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z"), 2)
        self.assertIn('svg.setAttribute("aria-label", label)', HTML)
        self.assertIn('TeslifeI18n.t("집")', HTML)
        # named area: icon after the name, no extra 집 word
        self.assertIn("setLocArea(areaEl, area, atHome, false, detail)", HTML)
        # no area at home: icon plus the word 집, not icon alone
        self.assertIn('setLocArea(areaEl, "", true, true, detail)', HTML)
        self.assertIn("집 아이콘", CHANGELOG)
        self.assertIn("집 아이콘", DESIGN)


class I18nTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_aria_and_notes(self) -> None:
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("집")'),
            "Home",
        )
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("현재 위치: 지도 위에 구분선이 없어요. 집이면 동 이름 뒤에 집 아이콘이 붙어요")'),
            "Current location: there is no divider above the map. At home, a house icon follows the neighbourhood name",
        )


DONG, GU, SI, SIDO = "한빛동", "별빛구", "해솔시", "한들도"
DONG_EN, GU_EN, SI_EN, SIDO_EN = "Hanbit-dong", "Byeollit-gu", "Haesol-si", "Handeul-do"
UP = "별빛구 · 해솔시 · 한들도"
UP_EN = "Byeollit-gu · Haesol-si · Handeul-do"
ROAD_FULL = "한들도 해솔시 별빛구 별빛로 12 (한빛타워)"
JIBUN_FULL = "한들도 해솔시 별빛구 한빛동 123-4"
VID = "demo-car"
LAT, LON = 37.4000, 127.1000  # made-up point for cache cells only
TS = "2026-11-27T12:26:00Z"


def _db() -> tuple[sqlite3.Connection, str]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = connect(tmp.name)
    return conn, tmp.name


class HierarchyParseTests(unittest.TestCase):
    def test_primary_starts_at_dong_then_gu_then_si(self) -> None:
        full = parts_from_area_names(SIDO, f"{SI} {GU}", DONG, "")
        self.assertEqual(full, AdminParts(dong=DONG, gu=GU, si=SI, sido=SIDO))
        self.assertEqual(primary_and_up(full), (DONG, UP))

        no_dong = parts_from_area_names(SIDO, f"{SI} {GU}", "", "")
        self.assertEqual(primary_and_up(no_dong), (GU, f"{SI} · {SIDO}"))

        no_gu = parts_from_area_names(SIDO, SI, "", "")
        self.assertEqual(primary_and_up(no_gu), (SI, SIDO))

        only_do = parts_from_area_names(SIDO, "", "", "")
        self.assertEqual(primary_and_up(only_do), (SIDO, None))

        myeon = parts_from_area_names(SIDO, "가람군", "한빛면", "")
        self.assertEqual(primary_and_up(myeon), ("한빛면", f"가람군 · {SIDO}"))

    def test_higher_line_skips_duplicates(self) -> None:
        parts = AdminParts(dong=DONG, gu=DONG, si=SI, sido=SIDO)
        self.assertEqual(primary_and_up(parts), (DONG, f"{SI} · {SIDO}"))
        parts = AdminParts(dong="", gu=GU, si=GU, sido=SIDO)
        self.assertEqual(primary_and_up(parts), (GU, SIDO))

    def test_naver_region_ignores_road_and_building(self) -> None:
        payload = {
            "results": [{
                "name": "roadaddr",
                "region": {
                    "area1": {"name": SIDO},
                    "area2": {"name": f"{SI} {GU}"},
                    "area3": {"name": DONG},
                    "area4": {"name": ""},
                },
                "land": {"name": "가상로", "number1": "12", "addition0": {"type": "building", "value": "가상빌딩"}},
            }]
        }
        parts = parts_from_naver_payload(payload)
        self.assertEqual(parts, AdminParts(dong=DONG, gu=GU, si=SI, sido=SIDO))
        self.assertEqual(primary_and_up(parts), (DONG, UP))

    def test_nominatim_ignores_road_and_house_number(self) -> None:
        parts = parts_from_nominatim({
            "address": {
                "suburb": DONG,
                "city_district": GU,
                "city": SI,
                "state": SIDO,
                "road": "가상로",
                "house_number": "12",
            }
        })
        self.assertEqual(parts, AdminParts(dong=DONG, gu=GU, si=SI, sido=SIDO))



class AddressFormatTests(unittest.TestCase):
    def test_full_road_and_jibun_are_not_shortened(self) -> None:
        self.assertEqual(
            format_road_addr(SIDO, f"{SI} {GU}", "별빛로", "12", "한빛타워"),
            ROAD_FULL,
        )
        self.assertEqual(
            format_road_addr(SIDO, f"{SI} {GU}", "별빛로", "12", ""),
            "한들도 해솔시 별빛구 별빛로 12",
        )
        self.assertEqual(
            format_jibun_addr(SIDO, f"{SI} {GU}", DONG, "123-4"),
            JIBUN_FULL,
        )
        self.assertEqual(format_road_addr(SIDO, f"{SI} {GU}", "", "", "한빛타워"), "")
        self.assertEqual(format_jibun_addr(SIDO, f"{SI} {GU}", DONG, ""), "")

    def test_naver_payload_uses_the_full_strings(self) -> None:
        payload = {
            "results": [
                {
                    "name": "roadaddr",
                    "region": {
                        "area1": {"name": SIDO},
                        "area2": {"name": f"{SI} {GU}"},
                        "area3": {"name": DONG},
                        "area4": {"name": ""},
                    },
                    "land": {
                        "name": "별빛로",
                        "number1": "12",
                        "number2": "0",
                        "addition0": {"type": "building", "value": "한빛타워"},
                    },
                },
                {
                    "name": "addr",
                    "region": {
                        "area1": {"name": SIDO},
                        "area2": {"name": f"{SI} {GU}"},
                        "area3": {"name": DONG},
                        "area4": {"name": ""},
                    },
                    "land": {"number1": "123", "number2": "4"},
                },
            ]
        }
        road, jibun = addresses_from_naver_payload(payload)
        self.assertEqual(road, ROAD_FULL)
        self.assertEqual(jibun, JIBUN_FULL)
        self.assertNotEqual(road, "별빛로 12")
        self.assertNotEqual(jibun, "123-4")


class CacheAndPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.db = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: os.path.exists(self.db) and os.remove(self.db))

    def test_schema_adds_admin_columns_without_dropping_old_rows(self) -> None:
        raw_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.addCleanup(lambda: os.path.exists(raw_path) and os.remove(raw_path))
        raw = sqlite3.connect(raw_path)
        raw.execute(
            "CREATE TABLE geocode_cache ("
            "provider TEXT NOT NULL, key TEXT NOT NULL, label TEXT, label_en TEXT, "
            "fetched_at TEXT, PRIMARY KEY (provider, key))"
        )
        raw.execute(
            "INSERT INTO geocode_cache VALUES ('naver', '37.400,127.100', ?, ?, '2026-11-01T00:00:00Z')",
            (DONG, DONG_EN),
        )
        raw.commit()
        raw.close()
        conn = connect(raw_path)
        self.addCleanup(conn.close)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(geocode_cache)")}
        self.assertTrue({"gu", "gu_en", "si", "si_en", "sido", "sido_en", "road_addr", "jibun_addr"} <= cols)
        row = conn.execute("SELECT label, label_en, gu, si, sido FROM geocode_cache").fetchone()
        self.assertEqual(row[0], DONG)
        self.assertEqual(row[1], DONG_EN)
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])

    def test_old_row_is_refilled_then_skipped(self) -> None:
        self.conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, label_en, fetched_at) "
            "VALUES ('fake', '37.400,127.100', ?, ?, ?)",
            (DONG, DONG_EN, TS),
        )
        self.conn.commit()
        calls: list[tuple[float, float]] = []

        class Fake:
            name = "fake"

            def reverse_admin(self, lat, lon):
                calls.append((lat, lon))
                ko = AdminParts(dong=DONG, gu=GU, si=SI, sido=SIDO)
                en = AdminParts(dong=DONG_EN, gu=GU_EN, si=SI_EN, sido=SIDO_EN)
                return ko, en, 1

        self.conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
            (VID, TS, json.dumps({"lat": LAT, "lon": LON})),
        )
        self.conn.commit()
        self.assertEqual(fill_car_area_label(self.conn, VID, Fake()), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fill_car_area_label(self.conn, VID, Fake()), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(admin_display(self.conn, LAT, LON, "ko"), (DONG, UP))
        self.assertEqual(admin_display(self.conn, LAT, LON, "en"), (DONG_EN, UP_EN))
        self.assertEqual(cached_addresses(self.conn, LAT, LON), (None, None))

    def test_payload_reads_cache_only_and_falls_back(self) -> None:
        self.conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Location', ?)",
            (VID, TS, json.dumps({"lat": LAT, "lon": LON})),
        )
        self.conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m, created_at) "
            "VALUES (?, '집', ?, ?, 150, ?)",
            (VID, LAT, LON, TS),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertIsNone(data["area"])
        self.assertIsNone(data["road_addr"])
        self.assertIsNone(data["jibun_addr"])
        self.assertNotIn("area_up", data)
        self.conn.execute(
            "INSERT INTO geocode_cache "
            "(provider, key, label, label_en, gu, gu_en, si, si_en, sido, sido_en, "
            "road_addr, jibun_addr, fetched_at) "
            "VALUES ('fake', '37.400,127.100', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (DONG, DONG_EN, GU, GU_EN, SI, SI_EN, SIDO, SIDO_EN, ROAD_FULL, JIBUN_FULL, TS),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertEqual(data["area"], DONG)
        self.assertEqual(data["area_en"], DONG_EN)
        self.assertEqual(data["road_addr"], ROAD_FULL)
        self.assertEqual(data["jibun_addr"], JIBUN_FULL)
        self.assertNotIn("address", data)
        self.assertNotIn("area_up", data)
        # no 동: value starts at 구, still a single name
        self.conn.execute(
            "UPDATE geocode_cache SET label=?, label_en=?, gu=?, gu_en=? "
            "WHERE key='37.400,127.100'",
            (GU, GU_EN, GU, GU_EN),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertEqual(data["area"], GU)
        self.assertEqual(data["area_en"], GU_EN)
        self.assertEqual(data["road_addr"], ROAD_FULL)
        # no 동 no 구
        self.conn.execute(
            "UPDATE geocode_cache SET label=?, label_en=?, gu='', gu_en='' "
            "WHERE key='37.400,127.100'",
            (SI, SI_EN),
        )
        self.conn.commit()
        data = car_location_payload(self.conn, VID, route_opener=lambda *_a, **_k: None)
        self.assertEqual(data["area"], SI)
        self.assertEqual(data["area_en"], SI_EN)

    def test_trip_list_still_shows_only_the_smallest_unit(self) -> None:
        self.conn.execute(
            "INSERT INTO geocode_cache "
            "(provider, key, label, label_en, gu, gu_en, si, si_en, sido, sido_en, fetched_at) "
            "VALUES ('fake', '37.400,127.100', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (DONG, DONG_EN, GU, GU_EN, SI, SI_EN, SIDO, SIDO_EN, TS),
        )
        self.conn.commit()
        entries = [{"start_lat": LAT, "start_lon": LON, "end_lat": LAT, "end_lon": LON}]
        label_trips(self.conn, VID, entries)
        self.assertEqual(entries[0]["from_label"], DONG)
        self.assertEqual(entries[0]["to_label"], DONG)
        self.assertNotIn(GU, entries[0]["from_label"])
        self.assertNotIn(" · ", entries[0]["from_label"] or "")


class CardHierarchyTests(unittest.TestCase):
    def test_markup_and_docs(self) -> None:
        self.assertNotIn("car-loc-area-up", HTML)
        self.assertNotIn("data.area_up", HTML)
        self.assertIn("function setLocArea(el, text, atHome, homeWord, detail)", HTML)
        self.assertIn("car-loc-addr-toggle", HTML)
        self.assertIn("car-loc-addr-pop", HTML)
        self.assertIn("car-loc-addr-copy", HTML)
        self.assertIn("navigator.clipboard.writeText", HTML)
        self.assertIn("copyLocAddr(text.textContent, copy)", HTML)
        text_css = _between(HTML, ".car-loc-addr-text {", "}")
        self.assertIn("overflow-wrap: anywhere", text_css)
        self.assertIn("word-break: break-word", text_css)
        self.assertIn("font-size: 0.8rem", text_css)
        kind_css = _between(HTML, ".car-loc-addr-kind {", "}")
        self.assertIn("font-size: 0.7rem", kind_css)
        copy_css = _between(HTML, ".car-loc-addr-copy {", "}")
        self.assertIn("font-size: 0.7rem", copy_css)
        self.assertIn("border-radius: 999px", copy_css)
        self.assertIn("min-height: 0", copy_css)
        row_css = _between(HTML, ".car-loc-addr-row {", "}")
        self.assertIn("gap: 6px", row_css)
        self.assertIn("padding: 2px 6px", row_css)
        pop_css = _between(HTML, ".car-loc-addr-pop {", "}")
        self.assertIn("padding: 4px", pop_css)
        self.assertIn("background: #243140", pop_css)
        main_css = _between(HTML, "#car-loc-area .car-loc-area-main {", "}")
        self.assertIn("gap: 0", main_css)
        mark_css = _between(HTML, "#car-loc-area .car-loc-home-mark {", "}")
        self.assertIn("margin-left: 8px", mark_css)
        toggle_css = _between(HTML, "#car-loc-area .car-loc-addr-toggle {", "}")
        self.assertIn("font-size: inherit", toggle_css)
        self.assertIn("min-height: 0", toggle_css)
        self.assertIn("margin: 0 0 0 6px", toggle_css)
        self.assertIn("ALTER TABLE", CHANGELOG)
        self.assertIn("화살표를 누르면 열립니다", CHANGELOG)
        self.assertIn("줄여 쓰지 않으며", CHANGELOG)
        self.assertIn("구 · 시 · 도를 이은 줄은 넣지 않는다", DESIGN)
        self.assertIn("화살표를 누르면 열려요", HTML)


class I18nHierarchyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_hierarchy_note_english(self) -> None:
        self.assertEqual(
            _node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("현재 위치: 위치 값은 동 이름 하나예요. 동이 없으면 구, 구가 없으면 시·군, 그것도 없으면 도를 보여 줘요. 자세한 도로명·지번 주소는 화살표를 누르면 열려요")'),
            "Current location: the value is a single name, starting at the neighbourhood, then the district, then the city or county, then the province. Tap the chevron for the full road and lot addresses",
        )
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("도로명")'), "Road")
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("지번")'), "Lot")
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("복사")'), "Copy")
