"""v1.17.0: climate graph, gear interval, operator code, and place names."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from process import connect
from process.evcharger import _locked_user, choose_charger, fill_charge_place, pick_station
from process.operators import display_name, group_keys, same_operator
from process.placefill import city_stem, name_from_hits, resolve_place, roads_match, strip_operator_tail
from process.telemetry_delta import apply_delta
from tools.backfill_busi import apply_operators, main as backfill_main, plan_operators
from web.tariff import ensure_schema, unit_price

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class OperatorTests(unittest.TestCase):
    def test_one_code_shows_one_name(self) -> None:
        self.assertEqual(display_name("PW", "", "파워큐브코리아"), "파워큐브")
        self.assertEqual(display_name("", "파워큐브(SP)", ""), "파워큐브")
        self.assertTrue(same_operator("파워큐브코리아", "PW"))
        self.assertIn("파워큐브(SP)", group_keys("파워큐브"))
        self.assertNotEqual(display_name(busi_nm="다른사업자"), "파워큐브")

    def test_price_lookup_follows_the_old_name(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        conn = connect(Path(folder) / "signals.db")
        self.addCleanup(conn.close)
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO tariff_rates (vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
            "VALUES ('', '파워큐브코리아', 'ac', 0, '2026-01-01', 310, 'official')"
        )
        conn.execute(
            "INSERT INTO tariff_member (vehicle_id, operator, member) VALUES ('car', '파워큐브(SP)', 1)"
        )
        conn.commit()
        from web.tariff import _member

        self.assertTrue(_member(conn, "car", "파워큐브"))
        price, learned = unit_price(
            conn, "car", "PW",
            {"kind": "AC", "max_charger_kw": 7, "meta": {"pay_method": "guest"}},
            "2026-06-01",
        )
        self.assertEqual(price, 310)
        self.assertFalse(learned)

    def test_pick_station_prefers_code_and_power_over_the_nearest(self) -> None:
        near = {
            "stat_id": "near", "chger_id": "1", "distance_m": 20,
            "chger_type": "02", "output_kw": 7, "busi_id": "ZZ", "bnm": "다른곳", "busi_nm": "다른곳",
        }
        match = {
            "stat_id": "fit", "chger_id": "2", "distance_m": 80,
            "chger_type": "02", "output_kw": 7, "busi_id": "PW", "bnm": "파워큐브", "busi_nm": "파워큐브코리아",
        }
        chosen, rest = pick_station([near, match], "AC", 7, "PW")
        self.assertEqual(chosen["stat_id"], "fit")
        self.assertEqual(rest, [])
        far = dict(match, stat_id="far", distance_m=250)
        chosen, rest = pick_station([near, far], "AC", 7, "PW")
        self.assertEqual(chosen["stat_id"], "far")
        self.assertIsNone(pick_station([near], "DC", 100, "PW")[0])


class PlaceFillOrderTests(unittest.TestCase):
    def _conn(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        conn = connect(Path(folder) / "signals.db")
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
            "VALUES ('car', 'new', 'new-end', 'AC', 1)"
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES ('car', 'new', 1, 1, 'car')"
        )
        conn.commit()
        return conn

    def test_saved_place_within_150m_comes_before_search(self) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES ('car', 'old', '저장한곳')"
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES ('car', 'old', 1, 1, 'car')"
        )
        conn.commit()
        calls = []
        got = fill_charge_place(
            conn, "car", "new", 1, 1,
            stations=[{"stat_nm": "예시충전소", "addr": "예시시 예시로 1"}],
            search=lambda query: calls.append(query) or [],
            lookup=lambda _a, _b: "역지오",
        )
        self.assertEqual(got, "저장한곳")
        self.assertEqual(calls, [])

    def test_search_then_catalog_name_and_reverse_only_without_a_station(self) -> None:
        conn = self._conn()
        queries = []

        def search(query: str):
            queries.append(query)
            if query == "예시 예시충전소":
                return [{
                    "title": "가게",
                    "category": "음식",
                    "roadAddress": "예시시 예시로 1",
                }]
            if "전기차충전소" in query:
                return [{
                    "title": "예시충전소 파워큐브 2",
                    "category": "교통>전기차충전소",
                    "roadAddress": "경기 예시시 예시로 1",
                }]
            return []

        got = fill_charge_place(
            conn, "car", "new", 1, 1,
            stations=[{"stat_nm": "예시충전소", "addr": "경기도 예시시 예시로 1"}],
            search=search,
            lookup=lambda _a, _b: "역지오",
        )
        self.assertEqual(queries[0], "예시 예시충전소")
        self.assertIn("전기차충전소", queries[1])
        self.assertEqual(got, "예시충전소")
        self.assertEqual(
            conn.execute("SELECT location_text FROM charge_meta WHERE charge_id = 'new'").fetchone()[0],
            "예시충전소",
        )

        conn.execute("DELETE FROM charge_meta WHERE charge_id = 'new'")
        conn.execute("DELETE FROM process_meta")
        conn.commit()
        empty = fill_charge_place(
            conn, "car", "new", 1, 1,
            stations=[{"stat_nm": "예시충전소", "addr": "경기도 예시시 예시로 1"}],
            search=lambda _q: [
                {"title": "가", "category": "전기차충전소", "roadAddress": "경기 예시시 예시로 1"},
                {"title": "나", "category": "전기차충전소", "roadAddress": "경기 예시시 예시로 1"},
            ],
            lookup=lambda _a, _b: "역지오",
        )
        self.assertIsNone(empty)
        self.assertIsNone(conn.execute(
            "SELECT location_text FROM charge_meta WHERE charge_id = 'new'"
        ).fetchone())

        conn.execute("DELETE FROM process_meta")
        conn.commit()
        none = fill_charge_place(
            conn, "car", "new", 1, 1,
            stations=[{"stat_nm": "예시충전소", "addr": "경기도 예시시 예시로 1"}],
            search=lambda _q: [],
            lookup=lambda _a, _b: "역지오",
        )
        self.assertEqual(none, "예시충전소")

        conn.execute("DELETE FROM charge_meta WHERE charge_id = 'new'")
        conn.execute("DELETE FROM process_meta")
        conn.commit()
        reverse = fill_charge_place(
            conn, "car", "new", 1, 1, stations=[], lookup=lambda _a, _b: "역지오",
        )
        self.assertEqual(reverse, "역지오")

    def test_owner_station_is_kept_ahead_of_a_later_match(self) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, busi_id, bnm, stat_nm, addr, updated_ts) "
            "VALUES ('ST-A', '1', 1, 1, '02', 7, '파워큐브코리아', 'PW', '파워큐브', '예시충전소', '예시시 예시로 1', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        got = choose_charger(conn, "car", {"charge_id": "new", "stat_id": "ST-A"})
        self.assertEqual(got["source"], "user")
        self.assertEqual(got["operator"], "파워큐브")
        self.assertTrue(_locked_user(conn, "car", "new"))
        self.assertEqual(tuple(conn.execute("SELECT stat_id, source FROM charge_charger").fetchone()), ("ST-A", "user"))


class PlaceNameTests(unittest.TestCase):
    def test_road_abbreviation_and_operator_tail(self) -> None:
        self.assertEqual(city_stem("경기도 예시시 예시로 10"), "예시")
        self.assertTrue(roads_match("경기도 예시시 예시로 10", "경기 예시시 예시로 10"))
        self.assertEqual(strip_operator_tail("예시충전소 파워큐브 2"), "예시충전소")
        hits = [
            {"title": "예시충전소 파워큐브", "category": "교통>전기차충전소", "roadAddress": "경기도 예시시 예시로 10"},
            {"title": "예시마트", "category": "쇼핑", "roadAddress": "경기도 예시시 예시로 10"},
        ]
        self.assertEqual(name_from_hits(hits, "경기 예시시 예시로 10"), "예시충전소")

    def test_several_names_stay_empty_and_no_hit_uses_the_catalog_name(self) -> None:
        station = {"stat_nm": "예시충전소", "addr": "예시시 예시로 10"}

        def disagree(query: str):
            return [
                {"title": "첫번째", "category": "전기차충전소", "roadAddress": "예시시 예시로 10"},
                {"title": "두번째", "category": "전기차충전소", "roadAddress": "예시시 예시로 10"},
            ]

        self.assertEqual(resolve_place([station], disagree), "")

        def empty(query: str):
            return []

        self.assertEqual(resolve_place([station], empty), "예시충전소")

        def second(query: str):
            if "전기차충전소" not in query:
                return []
            return [{"title": "예시충전소 한국전력", "category": "전기차충전소", "roadAddress": "예시시 예시로 10"}]

        self.assertEqual(resolve_place([station], second), "예시충전소")


class BackfillTests(unittest.TestCase):
    def test_dry_run_counts_and_does_not_write(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        path = Path(folder) / "signals.db"
        conn = connect(path)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
            "VALUES ('car', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 'AC', 1)"
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES ('car', '2026-01-01T00:00:00Z', '파워큐브코리아')"
        )
        conn.commit()
        planned = plan_operators(conn)
        self.assertEqual(planned[0]["to"], "파워큐브")
        self.assertEqual(planned[0]["busi_id"], "PW")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = backfill_main(["--db", str(path)])
        conn.close()
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("dry_run=yes", text)
        self.assertIn("backup=", text)
        self.assertIn("operator_rows=1", text)
        again = sqlite3.connect(path)
        self.assertEqual(again.execute("SELECT operator FROM charge_meta").fetchone()[0], "파워큐브코리아")
        again.close()
        conn = connect(path)
        self.assertEqual(apply_operators(conn, planned), 1)
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "파워큐브")
        conn.close()


class GraphSourceTests(unittest.TestCase):
    def test_value_lines_follow_the_grid_and_legends_match(self) -> None:
        speed = _fn("svgLine")
        self.assertLess(speed.index("axisBaselines("), speed.index("<polyline"))
        battery = _fn("svgBatteryTemp")
        self.assertLess(battery.index("axisBaselines("), battery.index("drawLine("))
        self.assertIn("lineKey(\"#ef8a6b\")", battery)
        self.assertIn("opacity:0.4", battery)
        self.assertNotIn("stationPlace(st) {\n      return [st && st.stat_nm, st && st.addr]", HTML)
        self.assertNotIn('return "— → " + b', _fn("tirePairText"))
        after = apply_delta({"fields": {"Gear": {"interval_seconds": 30, "minimum_delta": 1}, "VehicleSpeed": {"interval_seconds": 1}}})
        self.assertEqual(after["fields"]["Gear"]["interval_seconds"], 1)
        self.assertEqual(after["fields"]["Gear"]["minimum_delta"], 1)
        self.assertIn("AutoSeatClimateLeft", after["fields"])
        self.assertNotIn("SeatHeaterRearCenter", after["fields"])


class VersionTests(unittest.TestCase):
    def test_screen_version(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.1"', HTML)
        self.assertIn("후진", HTML)
        self.assertIn("운전석 열선", HTML)
