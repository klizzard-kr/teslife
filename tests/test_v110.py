"""v1.10.0: sentence notices, sigungu catalogs, and a user choice that stays put."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, format_ts
from process.charge_backfill import apply_station_matches
from process.evcharger import (
    KEEP_M,
    _fetch_info_page,
    ensure_sigungu,
    parse_catalog,
    refresh_catalogs,
    region_from_point,
    stations_near,
)
from process.regions import zscode_at
from web.notify import _notice_parts

VID = "car-a"
NOW = datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


class RegionTests(unittest.TestCase):
    def test_bucheon_and_dangjin_use_the_city_code(self) -> None:
        self.assertEqual(zscode_at(37.503, 126.766), "41190")
        self.assertEqual(region_from_point(37.503, 126.766), ("41", "41190"))
        self.assertEqual(zscode_at(36.80, 126.65), "44270")
        self.assertEqual(region_from_point(37.566, 126.978)[1], "11140")


class CatalogTests(unittest.TestCase):
    def test_json_body_and_every_page(self) -> None:
        body = json.dumps({
            "response": {
                "header": {"resultCode": "00"},
                "body": {"totalCount": 10000, "items": {"item": {"statId": "S", "chgerId": "1", "lat": "36.89", "lng": "126.65", "statNm": "테스트충전소"}}},
            }
        }).encode()
        code, total, items = parse_catalog(body)
        self.assertEqual((code, total, items[0]["statNm"]), ("00", 10000, "테스트충전소"))
        conn = _db(self)
        seen = []

        class Resp:
            def __init__(self, page):
                self.page = page
            def read(self):
                if self.page == "1":
                    item = "<item><statId>S</statId><chgerId>1</chgerId><lat>36.89</lat><lng>126.65</lng></item>"
                    count = 15000
                else:
                    item = "<item><statId>S</statId><chgerId>2</chgerId><lat>36.90</lat><lng>126.65</lng></item>"
                    count = 15000
                return (
                    "<?xml version='1.0'?><response><header><resultCode>00</resultCode></header>"
                    f"<body><items>{item}</items><totalCount>{count}</totalCount></body></response>"
                ).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opener(req, timeout=20):
            page = req.full_url.split("pageNo=")[1].split("&")[0]
            seen.append(page)
            return Resp(page)

        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old) if old else os.environ.pop("EV_CHARGER_API_KEY", None))
        calls = _fetch_info_page(conn, "44", NOW, opener, zscode="44270")
        self.assertEqual(seen, ["1", "2"])
        self.assertEqual(calls, 2)
        done = conn.execute("SELECT done FROM ev_region").fetchone()[0]
        self.assertEqual(done, 1)


class MatchTests(unittest.TestCase):
    def test_user_source_is_not_rematched(self) -> None:
        conn = _db(self)
        start = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 20)",
            (VID, cid, format_ts(start + timedelta(hours=2))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 36.80, 126.65, 'car')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, source, stat_nm, busi_nm, updated_ts) "
            "VALUES (?, ?, 'user', '내가 고른 곳', '파워큐브', 't')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, addr, zcode, updated_ts) "
            "VALUES ('PW', '1', 36.80, 126.65, '02', 7, '테스트충전소', 'ST-TEST-1', '당진', '44', 't')",
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
        stats = apply_station_matches(conn, VID, NOW)
        self.assertEqual(stats["user_skipped"], 1)
        self.assertEqual(conn.execute("SELECT stat_nm, source FROM charge_charger").fetchone(), ("내가 고른 곳", "user"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], before)


def _item(stat_id: str, name: str, lat: float, lng: float, operator: str = "") -> str:
    return (
        "<item><statNm>" + name + "</statNm><statId>" + stat_id + "</statId><chgerId>1</chgerId>"
        "<chgerType>02</chgerType><output>7</output><busiNm>" + operator + "</busiNm>"
        f"<lat>{lat}</lat><lng>{lng}</lng></item>"
    )


def _page(items: str, total: int = 1) -> bytes:
    return (
        "<?xml version='1.0'?><response><header><resultCode>00</resultCode></header>"
        f"<body><items>{items}</items><totalCount>{total}</totalCount></body></response>"
    ).encode()


class WholeDistrictTests(unittest.TestCase):
    def setUp(self) -> None:
        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old) if old else os.environ.pop("EV_CHARGER_API_KEY", None))

    def test_bucheon_car_keeps_dangjin_chargers_for_the_edit_list(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', 'P'), (?, ?, 'Location', ?)",
            (VID, "2026-11-15T00:00:00Z", VID, "2026-11-15T00:00:00Z", '{"lat": 37.503, "lon": 126.766}'),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'c1', 'c2', 'AC', 10)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, 'c1', 36.80, 126.65, 'car')",
            (VID,),
        )
        conn.commit()
        far_lat = 36.80 + (KEEP_M + 4000) / 111_320.0

        def opener(req, timeout=20):
            url = req.full_url
            if "zscode=44270" in url:
                body = _item("ST-TEST-1", "ST-TEST-1", 36.80, 126.65, "테스트충전소") + _item(
                    "FAR", "먼충전소", far_lat, 126.65,
                )
            elif "zscode=41190" in url:
                body = _item("BC", "부천충전소", 37.503, 126.766)
            else:
                body = ""
            raw = _page(body, 1 if body else 0)

            class Resp:
                def read(self):
                    return raw
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Resp()

        refresh_catalogs(conn, VID, NOW, opener)
        names = {row[0] for row in conn.execute("SELECT stat_nm FROM ev_chargers")}
        self.assertIn("ST-TEST-1", names)
        self.assertIn("먼충전소", names)
        stations = stations_near(conn, 36.80, 126.65)
        self.assertIn("ST-TEST-1", [row["stat_nm"] for row in stations])
        # A watched 시·군 is hundreds to a few thousand charger rows, under a megabyte.
        count = conn.execute("SELECT COUNT(*) FROM ev_chargers").fetchone()[0]
        self.assertLess(count * 400, 1_000_000)

    def test_drive_into_a_district_keeps_chargers_away_from_the_entry(self) -> None:
        conn = _db(self)
        entry = (36.80, 126.65)
        other = (36.80 + (KEEP_M + 2500) / 111_320.0, 126.65)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', 'D'), (?, ?, 'Location', ?)",
            (VID, "2026-11-15T01:00:00Z", VID, "2026-11-15T01:00:00Z",
             json.dumps({"lat": entry[0], "lon": entry[1]})),
        )
        conn.commit()

        def opener(req, timeout=20):
            self.assertIn("zscode=44270", req.full_url)
            raw = _page(_item("NEAR", "입구", *entry) + _item("OTHER", "다른곳", *other), 2)

            class Resp:
                def read(self):
                    return raw
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Resp()

        refresh_catalogs(conn, VID, NOW, opener)
        stored = {row[0] for row in conn.execute("SELECT stat_nm FROM ev_chargers")}
        self.assertEqual(stored, {"입구", "다른곳"})

    def test_port_open_refetches_a_done_district_with_nothing_nearby(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO ev_region (zcode, next_page, total_count, done, fetched_ts) VALUES ('44:44270', 2, 1, 1, ?)",
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.commit()
        calls = {"n": 0}

        def opener(req, timeout=20):
            calls["n"] += 1
            raw = _page(_item("ST-TEST-1", "ST-TEST-1", 36.80, 126.65, "테스트충전소"), 1)

            class Resp:
                def read(self):
                    return raw
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Resp()

        self.assertEqual(ensure_sigungu(conn, 36.80, 126.65, NOW, opener), 1)
        self.assertEqual(calls["n"], 1)
        self.assertIn("ST-TEST-1", [row[0] for row in conn.execute("SELECT stat_nm FROM ev_chargers")])
        self.assertEqual(ensure_sigungu(conn, 36.80, 126.65, NOW, opener), 0)
        self.assertEqual(calls["n"], 1)


# dataType=JSON lifts resultCode/totalCount to the outer object. Item fields match
# the portal sample (statNm, statId, chgerId, chgerType, addr, lat, lng, busiNm, zcode, output).
OUTER_JSON = {
    "resultCode": "00",
    "resultMsg": "NORMAL SERVICE.",
    "numOfRows": 9999,
    "pageNo": 1,
    "totalCount": 2159,
    "items": {
        "item": [
            {
                "statNm": "당진 테스트충전소",
                "statId": "ST-TEST-1",
                "chgerId": "01",
                "chgerType": "02",
                "addr": "충청남도 당진시",
                "lat": "36.80",
                "lng": "126.65",
                "useTime": "24시간 이용가능",
                "busiId": "PW",
                "bnm": "테스트충전소",
                "busiNm": "테스트충전소",
                "stat": "2",
                "zcode": "44",
                "zscode": "44270",
                "output": "7",
            }
        ]
    },
}


class OuterJsonTests(unittest.TestCase):
    def test_outer_items_item_list_and_single_object(self) -> None:
        code, total, items = parse_catalog(json.dumps(OUTER_JSON).encode())
        self.assertEqual(code, "00")
        self.assertEqual(total, 2159)
        self.assertEqual(items[0]["statId"], "ST-TEST-1")
        self.assertEqual(items[0]["busiNm"], "테스트충전소")
        single = json.loads(json.dumps(OUTER_JSON))
        single["items"]["item"] = single["items"]["item"][0]
        single["totalCount"] = 1
        _code, one_total, one = parse_catalog(json.dumps(single).encode())
        self.assertEqual(one_total, 1)
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["chgerId"], "01")

    def test_positive_total_with_no_items_is_not_done(self) -> None:
        conn = _db(self)
        raw = json.dumps({
            "resultCode": "00",
            "resultMsg": "NORMAL SERVICE.",
            "numOfRows": 9999,
            "pageNo": 1,
            "totalCount": 5426,
            "items": {},
        }).encode()

        class Resp:
            def read(self):
                return raw
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old) if old else os.environ.pop("EV_CHARGER_API_KEY", None))
        _fetch_info_page(conn, "41", NOW, lambda req, timeout=20: Resp(), zscode="41190")
        done, page, total = conn.execute("SELECT done, next_page, total_count FROM ev_region").fetchone()
        self.assertEqual((done, page, total), (0, 1, 5426))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_chargers").fetchone()[0], 0)

    def test_done_region_with_nothing_stored_is_fetched_once(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO ev_region (zcode, next_page, total_count, done, fetched_ts) VALUES "
            "('41:41190', 2, 5426, 1, '2026-11-14T00:00:00Z'), "
            "('44:44270', 2, 2159, 1, '2026-11-14T00:00:00Z')"
        )
        conn.commit()
        seen: list[str] = []

        def opener(req, timeout=20):
            url = req.full_url
            seen.append(url)
            if "zscode=41190" in url:
                body = json.loads(json.dumps(OUTER_JSON))
                body["totalCount"] = 5426
                body["items"]["item"][0] = {
                    "statNm": "부천충전소", "statId": "BC1", "chgerId": "01", "chgerType": "02",
                    "addr": "경기도 부천시", "lat": "37.503", "lng": "126.766",
                    "busiNm": "환경부", "zcode": "41", "zscode": "41190", "output": "7",
                }
            else:
                body = OUTER_JSON
            raw = json.dumps(body).encode()

            class Resp:
                def read(self):
                    return raw
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Resp()

        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old) if old else os.environ.pop("EV_CHARGER_API_KEY", None))
        from process.evcharger import refresh_catalogs
        refresh_catalogs(conn, VID, NOW, opener)
        stored = {row[0] for row in conn.execute("SELECT stat_id FROM ev_chargers")}
        self.assertIn("ST-TEST-1", stored)
        self.assertIn("BC1", stored)
        first = len(seen)
        self.assertGreaterEqual(first, 2)
        refresh_catalogs(conn, VID, NOW, opener)
        self.assertEqual(len(seen), first)

    def test_short_catalog_with_old_rows_is_fetched_once(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO ev_region (zcode, next_page, total_count, done, fetched_ts) VALUES "
            "('41:41190', 2, 5426, 1, '2026-11-14T00:00:00Z'), "
            "('44:44270', 2, 2159, 1, '2026-11-14T00:00:00Z')"
        )
        old = [("OLD-B%d" % i, 37.503, 126.766) for i in range(3)] + [
            ("OLD-D%d" % i, 36.80, 126.65) for i in range(2)
        ]
        conn.executemany(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, stat_nm, zcode, updated_ts) "
            "VALUES (?, '1', ?, ?, '예전', '44', 't')",
            [(stat, lat, lng) for stat, lat, lng in old],
        )
        conn.commit()
        seen: list[str] = []

        def opener(req, timeout=20):
            seen.append(req.full_url)
            if "zscode=41190" in req.full_url:
                body = json.loads(json.dumps(OUTER_JSON))
                body["totalCount"] = 5426
                body["items"]["item"][0] = {
                    "statNm": "부천충전소", "statId": "BC1", "chgerId": "01", "chgerType": "02",
                    "addr": "경기도 부천시", "lat": "37.504", "lng": "126.766",
                    "busiNm": "환경부", "zcode": "41", "zscode": "41190", "output": "7",
                }
            else:
                body = OUTER_JSON
            raw = json.dumps(body).encode()

            class Resp:
                def read(self):
                    return raw
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Resp()

        old_key = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old_key) if old_key else os.environ.pop("EV_CHARGER_API_KEY", None))
        from process.evcharger import refresh_catalogs
        refresh_catalogs(conn, VID, NOW, opener)
        stored = {row[0] for row in conn.execute("SELECT stat_id FROM ev_chargers")}
        self.assertTrue({"OLD-B0", "OLD-D0", "ST-TEST-1", "BC1"} <= stored)
        self.assertGreaterEqual(len(seen), 2)
        first = len(seen)
        refresh_catalogs(conn, VID, NOW, opener)
        self.assertEqual(len(seen), first)


class NoticeTests(unittest.TestCase):
    def test_sentence_title_and_detail_rows(self) -> None:
        title, _en, body, details = _notice_parts(
            "charge_end", "46% → 80%, 23.4kWh\n충전량|23.4 kWh\n종류|교류",
        )
        self.assertEqual(title, "충전이 완료되었어요")
        self.assertEqual(body, "46% → 80%, 23.4kWh")
        self.assertEqual(details[0], ["충전량", "23.4 kWh"])
        title, _en, body, _details = _notice_parts("consumable:wiper", "와이퍼 점검 시기예요 · 12일 남았어요")
        self.assertEqual(title, "와이퍼 점검 시기예요")
        self.assertEqual(body, "12일 남았어요")
