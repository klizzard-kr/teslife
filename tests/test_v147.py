"""v1.14.7: a nearby EV charger name, then the building, then the road."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from process import PROCESS_VERSION
from process.evcharger import _naver_place, suggest_places
from process.naver_maps import (
    _building_in,
    charger_name_from_items,
    place_name_from_sources,
    strip_charger_place,
)
from tools.relabel_charge_places import format_line, relabel

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-place"
LAT, LON = 37.5, 127.0


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE charges (
            vehicle_id TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT,
            PRIMARY KEY (vehicle_id, start_ts)
        );
        CREATE TABLE charge_meta (
            vehicle_id TEXT NOT NULL, charge_id TEXT NOT NULL,
            station_name TEXT, station_addr TEXT, location_text TEXT,
            PRIMARY KEY (vehicle_id, charge_id)
        );
        CREATE TABLE charge_locations (
            vehicle_id TEXT NOT NULL, charge_id TEXT NOT NULL,
            lat REAL, lon REAL, source TEXT,
            PRIMARY KEY (vehicle_id, charge_id)
        );
        CREATE TABLE charge_charger (
            vehicle_id TEXT NOT NULL, charge_id TEXT NOT NULL, stat_id TEXT,
            PRIMARY KEY (vehicle_id, charge_id)
        );
        CREATE TABLE process_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return conn


def _reverse(building: object = "", road: str = "테헤란로", number: str = "152") -> dict:
    return {"results": [{
        "name": "roadaddr",
        "land": {"name": road, "number1": number, "number2": "", "addition0": building},
    }]}


def _hit(title: str, category: str, lat: float = LAT, lon: float = LON) -> dict:
    return {"title": title, "category": category, "lat": lat, "lon": lon}


class ChargerNameTests(unittest.TestCase):
    def test_the_suffix_comes_off_the_end(self) -> None:
        self.assertEqual(strip_charger_place("한빛센터 전기차충전소"), "한빛센터")
        self.assertEqual(strip_charger_place("  한빛센터   전기차충전소  "), "한빛센터")
        self.assertEqual(strip_charger_place("전기차충전소"), "")

    def test_only_a_nearby_charger_category_is_kept(self) -> None:
        items = [
            _hit("근처 카페", "카페,디저트>카페"),
            _hit("멀리 전기차충전소", "교통>전기차충전소", lat=LAT + 0.01),
            _hit("한빛센터 전기차충전소", "교통,운수>전기차충전소", lat=LAT + 0.001),
        ]
        self.assertEqual(charger_name_from_items(items, (LAT, LON)), "한빛센터")
        self.assertIsNone(charger_name_from_items(
            [_hit("멀리 전기차충전소", "전기차충전소", lat=LAT + 0.01)], (LAT, LON),
        ))

    def test_addition0_object_uses_the_building_value(self) -> None:
        land = {"addition0": {"type": "building", "value": "강남파이낸스센터"}}
        self.assertEqual(_building_in(land), "강남파이낸스센터")
        self.assertNotIn("{", _building_in(land) or "")
        skipped = {"addition0": {"type": "zipcode", "value": "06236"}}
        self.assertIsNone(_building_in(skipped))
        self.assertEqual(place_name_from_sources(_reverse(land["addition0"])), "강남파이낸스센터")
        self.assertEqual(place_name_from_sources(_reverse(skipped["addition0"])), "테헤란로 152")


class SuggestChargerTests(unittest.TestCase):
    def test_empty_query_lists_saved_then_charger_then_building(self) -> None:
        conn = _db()
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, 'old', '우리집')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, 'old', ?, ?, 'car')",
            (VID, LAT, LON),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id) VALUES (?, 'old', 'ST1')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name, station_addr, location_text) "
            "VALUES (?, 'now', '한빛센터', '샘플로 1', '지금 칸')",
            (VID,),
        )
        conn.commit()

        def search(query, lat, lng):
            self.assertIn("한빛", query)
            return {"items": [
                _hit("한빛센터 전기차충전소", "전기차충전소", lat, lng),
                _hit("스타벅스", "카페", lat, lng),
            ]}

        out = suggest_places(
            conn, VID, LAT, LON, "ST1", "", "now",
            lookup=lambda _a, _b: "예시빌딩",
            search=search,
        )
        self.assertEqual(
            [(row["title"], row["tag"]) for row in out["items"]],
            [("우리집", "예전 장소"), ("한빛센터", ""), ("예시빌딩", "건물")],
        )

    def test_a_charger_name_is_not_repeated(self) -> None:
        conn = _db()
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, 'old', '한빛센터')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, 'old', ?, ?, 'car')",
            (VID, LAT, LON),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name) VALUES (?, 'now', '한빛센터')",
            (VID,),
        )

        def search(_q, lat, lng):
            return {"items": [_hit("한빛센터 전기차충전소", "전기차충전소", lat, lng)]}

        out = suggest_places(
            conn, VID, LAT, LON, "ST1", "", "now",
            lookup=lambda _a, _b: "한빛센터",
            search=search,
        )
        self.assertEqual(out["items"], [{"title": "한빛센터", "tag": "예전 장소"}])

    def test_typed_charger_results_drop_the_tag(self) -> None:
        conn = _db()

        def search(_q, lat, lng):
            return {"items": [_hit("한빛센터 전기차충전소", "전기차충전소", lat, lng)]}

        out = suggest_places(conn, VID, LAT, LON, "ST1", "한빛", "now", search=search)
        self.assertEqual(out["items"], [{"title": "한빛센터", "tag": "", "lat": LAT, "lon": LON}])

    def test_place_fill_uses_the_charger_before_the_building(self) -> None:
        def search(_q, lat, lng):
            return {"items": [_hit("한빛센터 전기차충전소", "전기차충전소", lat, lng)]}

        self.assertEqual(
            _naver_place(LAT, LON, "한빛센터", search=search),
            "한빛센터",
        )


class RelabelTests(unittest.TestCase):
    def _seed(self, conn: sqlite3.Connection, charge_id: str, place: str) -> None:
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts) VALUES (?, ?, ?)",
            (VID, charge_id, charge_id),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name, location_text) VALUES (?, ?, ?, ?)",
            (VID, charge_id, "한빛센터", place),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, ?, ?, 'car')",
            (VID, charge_id, LAT, LON),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id) VALUES (?, ?, 'ST1')",
            (VID, charge_id),
        )

    def test_dry_run_does_not_change_the_database(self) -> None:
        conn = _db()
        self._seed(conn, "2026-11-15T12:00:35Z", "예전 이름")
        self._seed(conn, "2026-11-16T01:00:00Z", "다른 이름")
        calls: list[str] = []

        def search(query, lat, lng):
            calls.append(query)
            return {"items": [_hit("한빛센터 전기차충전소", "전기차충전소", lat, lng)]}

        before = conn.execute("SELECT charge_id, location_text, lat FROM charge_meta m "
                              "JOIN charge_locations l USING (vehicle_id, charge_id) ORDER BY charge_id").fetchall()
        rows = relabel(conn, search=search, reverse=lambda _a, _b: (_ for _ in ()).throw(AssertionError("reverse")))
        after = conn.execute("SELECT charge_id, location_text, lat FROM charge_meta m "
                             "JOIN charge_locations l USING (vehicle_id, charge_id) ORDER BY charge_id").fetchall()
        self.assertEqual(after, before)
        self.assertEqual(calls, ["한빛센터"])
        self.assertEqual([row["basis"] for row in rows], ["충전소", "충전소"])
        self.assertEqual(rows[0]["new"], "한빛센터")
        self.assertNotIn("37.5", format_line(rows[0]))
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn("shownPlace", HTML)
        note = HTML.split('version: "1.14.7"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("네이버의 충전소 이름으로 깔끔하게 채워요", note)

    def test_apply_writes_only_the_place_text(self) -> None:
        conn = _db()
        self._seed(conn, "2026-11-15T12:00:35Z", "예전 이름")
        conn.commit()

        def search(_q, lat, lng):
            return {"items": []}

        rows = relabel(conn, search=search, reverse=lambda _a, _b: ("테헤란로 152", "주소"), apply=True)
        self.assertEqual(rows[0]["basis"], "주소")
        stored = conn.execute(
            "SELECT location_text, station_name FROM charge_meta WHERE charge_id = '2026-11-15T12:00:35Z'"
        ).fetchone()
        point = conn.execute(
            "SELECT lat, lon, source FROM charge_locations WHERE charge_id = '2026-11-15T12:00:35Z'"
        ).fetchone()
        self.assertEqual(stored, ("테헤란로 152", "한빛센터"))
        self.assertEqual(point, (LAT, LON, "car"))

    def test_a_failed_lookup_skips_the_row(self) -> None:
        conn = _db()
        self._seed(conn, "2026-11-15T12:00:35Z", "그대로")

        def search(_q, _lat, _lng):
            raise OSError("offline")

        rows = relabel(conn, search=search, reverse=lambda _a, _b: None)
        self.assertEqual(rows, [])
        self.assertEqual(
            conn.execute("SELECT location_text FROM charge_meta").fetchone()[0],
            "그대로",
        )
