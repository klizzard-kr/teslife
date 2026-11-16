"""Public charger matching. The portal is not called; responses are fixtures."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, format_ts
from process.evcharger import (
    DAILY_LIMIT,
    build_url,
    choose_charger,
    decide,
    fill_vehicle,
    parse_xml,
    zcode_from_text,
)

VID = "car-a"
NOW = datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc)
START = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
KEY = "TEST%2BKEY%3D"
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


def _shift(north_m: float, east_m: float = 0.0) -> tuple[float, float]:
    return north_m / 111_320.0, east_m / 111_320.0


def _charge(conn, start: datetime, kwh: float = 10.0, operator: str | None = None) -> str:
    end = start + timedelta(hours=1)
    cid = format_ts(start)
    conn.execute(
        "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, max_charger_kw) "
        "VALUES (?, ?, ?, 'AC', ?, 7)",
        (VID, cid, format_ts(end), kwh),
    )
    conn.execute(
        "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 0, 0, 'car')",
        (VID, cid),
    )
    if operator:
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?)",
            (VID, cid, operator),
        )
    conn.execute(
        "INSERT INTO stations (vehicle_id, id, name, address_text) VALUES (?, 'st1', 'place', '서울특별시')",
        (VID,),
    )
    conn.commit()
    return cid


def _item(north_m: float, east_m: float, chger_id: str, stat: str, when: str) -> str:
    lat, lng = _shift(north_m, east_m)
    return (
        "<item><statNm>Station</statNm><statId>ST</statId>"
        f"<chgerId>{chger_id}</chgerId><chgerType>02</chgerType><output>7</output>"
        f"<busiNm>Operator {chger_id}</busiNm><zcode>11</zcode>"
        f"<lat>{lat}</lat><lng>{lng}</lng><stat>{stat}</stat><nowTsdt>{when}</nowTsdt></item>"
    )


def _xml(items: str, total: int = 2) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?><response><header><resultCode>00</resultCode>"
        f"<resultMsg>NORMAL SERVICE.</resultMsg></header><body><items>{items}</items>"
        f"<totalCount>{total}</totalCount></body></response>"
    ).encode("utf-8")


class PureTests(unittest.TestCase):
    def test_key_is_not_encoded_twice(self) -> None:
        url = build_url("https://apis.data.go.kr/B552584/EvCharger/getChargerInfo", KEY, {"zcode": "11", "dataType": "XML"})
        self.assertIn(f"serviceKey={KEY}", url)
        self.assertNotIn("TEST%252B", url)
        self.assertEqual(zcode_from_text("광주광역시 어딘가"), "29")
        self.assertEqual(zcode_from_text("경기도 광주시"), "41")

    def test_distance_then_status(self) -> None:
        near = {"stat_id": "a", "chger_id": "1", "chger_type": "02", "output_kw": 7, "distance_m": 30,
                "stat": "3", "now_ts": START, "last_start": None, "last_end": None, "busi_nm": "A"}
        other = dict(near, chger_id="2", distance_m=40, stat="2", now_ts=None, busi_nm="B")
        far = dict(near, chger_id="3", distance_m=180, busi_nm="C")
        chosen, rest = decide([near, other, far], "AC", 7, START)
        self.assertEqual(chosen["chger_id"], "1")
        self.assertEqual(rest, [])
        both = [dict(near, stat="", now_ts=None), dict(other, stat="", now_ts=None)]
        chosen, rest = decide(both, "AC", 7, START)
        self.assertIsNone(chosen)
        self.assertEqual(len(rest), 2)

    def test_parse_result_code(self) -> None:
        code, total, items = parse_xml(_xml(_item(10, 0, "01", "2", ""), 1))
        self.assertEqual(code, "00")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["chgerId"], "01")


class FillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = KEY

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("EV_CHARGER_API_KEY", None)
        else:
            os.environ["EV_CHARGER_API_KEY"] = self._old

    def test_missing_key_makes_no_calls_and_keeps_rows(self) -> None:
        os.environ.pop("EV_CHARGER_API_KEY", None)
        conn = _db(self)
        cid = _charge(conn, START, operator="kept")
        called = []

        def opener(req, timeout=20):
            called.append(req.full_url)
            raise AssertionError("portal called")

        self.assertEqual(fill_vehicle(conn, VID, NOW, opener), 0)
        self.assertEqual(called, [])
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "kept")
        self.assertEqual(conn.execute("SELECT kwh_added FROM charges").fetchone()[0], 10.0)
        self.assertIsNotNone(cid)

    def test_one_match_fills_empty_operator_only(self) -> None:
        conn = _db(self)
        cid = _charge(conn, START, operator="kept")
        seen = []

        class Resp:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        body = _xml(_item(30, 0, "01", "3", "2026-11-13 09:00:00"), 1)

        def opener(req, timeout=20):
            seen.append(req.full_url)
            return Resp(body)

        calls = fill_vehicle(conn, VID, NOW, opener)
        self.assertEqual(calls, 1)
        self.assertIn(f"serviceKey={KEY}", seen[0])
        self.assertNotIn("TEST%252B", seen[0])
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "kept")
        self.assertEqual(conn.execute("SELECT kwh_added FROM charges").fetchone()[0], 10.0)
        # A station match stores no device number.
        self.assertIsNone(conn.execute("SELECT chger_id FROM charge_charger").fetchone()[0])
        self.assertTrue(cid)

    def test_several_candidates_then_a_saved_place(self) -> None:
        conn = _db(self)
        first = _charge(conn, START)
        seen = []

        class Resp:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        body = _xml(_item(30, 0, "01", "", "") + _item(0, 40, "02", "", ""))

        def opener(req, timeout=20):
            seen.append(req.full_url)
            return Resp(body)

        fill_vehicle(conn, VID, NOW, opener)
        self.assertIsNone(conn.execute(
            "SELECT chger_id FROM charge_charger WHERE chger_id IS NOT NULL"
        ).fetchone())
        choose_charger(conn, VID, {"charge_id": first, "stat_id": "ST", "chger_id": "02"})
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "Operator 02")
        self.assertEqual(conn.execute("SELECT source FROM charge_charger").fetchone()[0], "user")
        second_start = START + timedelta(days=1)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, max_charger_kw) VALUES (?, ?, ?, 'AC', 4, 7)",
            (VID, format_ts(second_start), format_ts(second_start + timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 0, 0, 'car')",
            (VID, format_ts(second_start)),
        )
        conn.commit()
        before = len(seen)
        fill_vehicle(conn, VID, NOW + timedelta(days=1, hours=2), opener)
        self.assertEqual(len(seen), before + 1)  # the next KST day refreshes the parked 시·군
        saved = conn.execute(
            "SELECT source, chger_id FROM charge_charger WHERE charge_id = ?",
            (format_ts(second_start),),
        ).fetchone()
        if saved is not None:
            self.assertNotEqual(saved[0], "user")
            self.assertIsNone(saved[1])

    def test_daily_budget_stops_the_portal(self) -> None:
        conn = _db(self)
        _charge(conn, START)
        from process.evcharger import ensure_schema
        ensure_schema(conn)
        conn.execute("INSERT INTO ev_api_calls (day, n) VALUES ('2026-11-13', ?)", (DAILY_LIMIT,))
        conn.commit()
        called = []

        def opener(req, timeout=20):
            called.append(1)
            raise AssertionError("over budget")

        self.assertEqual(fill_vehicle(conn, VID, NOW, opener), 0)
        self.assertEqual(called, [])


class SheetTests(unittest.TestCase):
    def test_detail_shows_operator_output_and_candidates(self) -> None:
        self.assertIn('foldHtml("충전기"', HTML)
        self.assertNotIn('keyCell("충전기 출력"', HTML)
        self.assertNotIn("충전기 번호", HTML)
        self.assertIn("선택 안 함", HTML)
        self.assertNotIn("충전기 후보", HTML)
        self.assertNotIn("출처: 한국환경공단", HTML)
        self.assertIn('"/api/charges/charger"', HTML)
        self.assertLess(HTML.index("trip-place"), HTML.index("class='tag kind-"))
        if shutil.which("node"):
            i18n = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
            expr = (
                'TeslifeI18n.setLanguage("en", false), '
                '[TeslifeI18n.t("결제 방식"), TeslifeI18n.t("환경부 카드"), '
                'TeslifeI18n.t("에너지 상세")].join("|")'
            )
            out = subprocess.run(
                ["node", "-e", i18n + "\nprocess.stdout.write(String((" + expr + ")));"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(out.stdout, "Payment method|Environment card|Energy detail")


if __name__ == "__main__":
    unittest.main()
