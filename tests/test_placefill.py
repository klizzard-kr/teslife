"""A charge place is a saved name or a building, never the station line or a shop."""

from __future__ import annotations

import sqlite3
import unittest

from process.evcharger import _remember_station, fill_charge_place, suggest_places
from process.naver_maps import place_name_from_sources

VID = "car-place"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
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


def _reverse(building: str = "", road: str = "테헤란로", number: str = "152") -> dict:
    return {"results": [{
        "name": "roadaddr",
        "land": {"name": road, "number1": number, "number2": "", "addition0": building},
    }]}


SHOP = {"title": "스타벅스 강남점", "category": "카페,디저트>카페"}


class BuildingNameTests(unittest.TestCase):
    def test_a_shop_is_not_used_instead_of_the_building(self) -> None:
        self.assertEqual(
            place_name_from_sources(_reverse("강남파이낸스센터"), [SHOP]),
            "강남파이낸스센터",
        )

    def test_road_address_when_there_is_no_building(self) -> None:
        self.assertEqual(place_name_from_sources(_reverse(""), [SHOP]), "테헤란로 152")

    def test_empty_when_only_a_shop_is_known(self) -> None:
        self.assertIsNone(place_name_from_sources({"results": []}, [SHOP]))

    def test_a_dong_name_is_not_a_building(self) -> None:
        self.assertEqual(place_name_from_sources(_reverse("삼성동")), "테헤란로 152")


class FillTests(unittest.TestCase):
    def test_station_match_does_not_write_the_place(self) -> None:
        conn = _db()
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, ?, ?)",
            (VID, "new", "내 장소"),
        )
        _remember_station(conn, VID, "new", {"stat_nm": "강남충전소", "addr": "테헤란로 152"})
        row = conn.execute(
            "SELECT station_name, station_addr, location_text FROM charge_meta WHERE charge_id = 'new'"
        ).fetchone()
        self.assertEqual(row, ("강남충전소", "테헤란로 152", "내 장소"))

    def test_a_saved_place_nearby_is_copied_and_the_station_line_is_not(self) -> None:
        conn = _db()
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name, station_addr, location_text) "
            "VALUES (?, 'old-auto', '강남충전소', '테헤란로 152', '강남충전소 테헤란로 152')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES (?, 'old-auto', 37.5, 127.0, 'car')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, 'old-user', '우리집')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES (?, 'old-user', 37.5, 127.0, 'car')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES (?, 'new', 37.5, 127.0, 'car')",
            (VID,),
        )
        calls = []
        got = fill_charge_place(conn, VID, "new", 37.5, 127.0, lookup=lambda _a, _b: calls.append(1))
        self.assertEqual(got, "우리집")
        self.assertEqual(calls, [])

    def test_a_manual_pin_is_not_replaced(self) -> None:
        conn = _db()
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES (?, 'new', 37.5, 127.0, 'manual')",
            (VID,),
        )
        calls = []
        self.assertIsNone(fill_charge_place(
            conn, VID, "new", 37.5, 127.0, lookup=lambda _a, _b: calls.append("x") or "건물",
        ))
        self.assertEqual(calls, [])
        self.assertIsNone(conn.execute(
            "SELECT location_text FROM charge_meta WHERE charge_id = 'new'"
        ).fetchone())

    def test_the_lookup_runs_once(self) -> None:
        conn = _db()
        calls = []

        def lookup(_lat, _lng):
            calls.append(1)
            return "강남파이낸스센터"

        self.assertEqual(fill_charge_place(conn, VID, "new", 37.5, 127.0, lookup=lookup), "강남파이낸스센터")
        self.assertIsNone(fill_charge_place(conn, VID, "new", 37.5, 127.0, lookup=lookup))
        self.assertEqual(calls, [1])


class SuggestTests(unittest.TestCase):
    def _near(self, conn: sqlite3.Connection, charge_id: str, place: str, stat_id: str = "ST1") -> None:
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, ?, ?)",
            (VID, charge_id, place),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5, 127.0, 'manual')",
            (VID, charge_id),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id) VALUES (?, ?, ?)",
            (VID, charge_id, stat_id),
        )

    def test_an_empty_field_lists_a_saved_place_then_the_building(self) -> None:
        conn = _db()
        self._near(conn, "old", "우리집")
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name, station_addr, location_text) "
            "VALUES (?, 'auto', '강남충전소', '테헤란로 152', '강남충전소 테헤란로 152')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, 'auto', 37.5, 127.0, 'car')",
            (VID,),
        )
        before = conn.execute("SELECT location_text FROM charge_meta ORDER BY charge_id").fetchall()
        out = suggest_places(
            conn, VID, 37.5, 127.0, "ST1", "", "new",
            lookup=lambda _a, _b: "강남파이낸스센터",
        )
        self.assertEqual(
            [(row["title"], row["tag"]) for row in out["items"]],
            [("우리집", "예전 장소"), ("강남파이낸스센터", "건물")],
        )
        self.assertEqual(conn.execute("SELECT location_text FROM charge_meta ORDER BY charge_id").fetchall(), before)

    def test_typing_puts_a_match_first_and_keeps_the_category(self) -> None:
        conn = _db()
        self._near(conn, "old", "우리집 주차장")

        def search(_q, _lat, _lng):
            return {"items": [
                {"title": "집앞 편의점", "category": "편의점", "lat": 37.5, "lon": 127.0},
                {"title": "건너 카페", "category": "카페"},
            ]}

        out = suggest_places(conn, VID, 37.5, 127.0, "ST1", "집", "new", search=search)
        self.assertEqual(out["items"][0], {"title": "우리집 주차장", "tag": "예전 장소"})
        self.assertEqual(out["items"][1]["title"], "집앞 편의점")
        self.assertEqual(out["items"][1]["tag"], "편의점")
        self.assertEqual(out["items"][2]["tag"], "카페")

    def test_a_failed_lookup_adds_nothing(self) -> None:
        conn = _db()

        def lookup(_lat, _lng):
            raise OSError("offline")

        self.assertEqual(suggest_places(conn, VID, 37.5, 127.0, lookup=lookup), {"items": []})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
