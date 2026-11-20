"""v1.6.0: notification master switch, charge estimates, tariff history.

Stored trip, charge, route, ledger and service rows are not rewritten.
Estimates are computed when the charge is read.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ledger import assign_operator, connect as ledger_connect, upsert_charge_meta
from process import PROCESS_VERSION, ensure_process_schema
from web.notify import PREF_ORDER, list_prefs, set_pref, sync_notices
from web.tariff import PLUGLINK, SCHEMA_SQL, annotate, list_tariffs, save_tariff

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-160"


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / "signals.db"
    conn = sqlite3.connect(path)
    ensure_process_schema(conn)
    conn.close()
    conn = ledger_connect(path)
    test.addCleanup(conn.close)
    return conn


def _charge(conn, cid: str, kind: str = "AC", kwh: float = 10.0, ac: float | None = 10.0,
            dc: float | None = 9.0, end: str = "2026-08-15T03:00:00Z", kw: float | None = 7.0) -> None:
    conn.execute(
        "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, charger_kwh, battery_kwh, "
        "ac_kwh, dc_kwh, max_charger_kw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (VID, cid, end, kind, kwh, ac if kind != "DC" else dc, dc if kind != "DC" else None, ac, dc, kw),
    )


class TariffTests(unittest.TestCase):
    def test_pluglink_bands_and_history(self) -> None:
        """Member 292/322/322/345/345 from 2026-07-31. Guest 완속 357, every faster band 528."""
        conn = _db(self)
        self.assertEqual(PROCESS_VERSION, "24")
        conn.execute(
            "INSERT INTO tariff_rates (vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
            "VALUES ('', '플러그링크', 'fast_low', 1, '2026-08-01', 322, 'official')"
        )
        from web.tariff import ensure_schema
        ensure_schema(conn)
        legacy = conn.execute(
            "SELECT krw_per_kwh FROM tariff_rates WHERE speed = 'fast_low'"
        ).fetchone()[0]
        self.assertEqual(legacy, 322)
        rates = {
            (r["speed"], r["member"]): r["krw_per_kwh"]
            for r in list_tariffs(conn, VID)["rates"]
            if r["official"] and r["operator"] == PLUGLINK
        }
        self.assertEqual(rates[("ac", True)], 292)
        self.assertEqual(rates[("mid", True)], 322)
        self.assertEqual(rates[("fast50", True)], 322)
        self.assertEqual(rates[("fast100", True)], 345)
        self.assertEqual(rates[("ultra", True)], 345)
        self.assertEqual(rates[("ac", False)], 357)
        self.assertEqual(rates[("mid", False)], 528)
        self.assertEqual(rates[("fast50", False)], 528)
        self.assertEqual(rates[("ultra", False)], 528)
        slow = "2026-08-02T01:00:00Z"
        fast = "2026-08-02T02:00:00Z"
        hyper = "2026-08-02T03:00:00Z"
        before = "2026-07-20T01:00:00Z"
        _charge(conn, slow, kw=7)
        _charge(conn, fast, kind="DC", kwh=20, ac=0, dc=20, kw=50, end="2026-08-15T04:00:00Z")
        _charge(conn, hyper, kind="DC", kwh=30, ac=0, dc=30, kw=150, end="2026-08-15T05:00:00Z")
        _charge(conn, "2026-08-02T06:00:00Z", kind="DC", kwh=40, ac=0, dc=40, kw=None, end="2026-08-15T06:00:00Z")
        _charge(conn, before, end="2026-07-20T02:00:00Z")
        for cid, op in ((slow, PLUGLINK), (fast, "PlugLink"), (hyper, PLUGLINK), (before, PLUGLINK)):
            conn.execute(
                "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?)",
                (VID, cid, op),
            )
        conn.commit()
        items = [
            {"charge_id": slow, "start_ts": slow, "end_ts": "2026-08-15T03:00:00Z", "kind": "AC",
             "kwh_added": 10, "charger_kwh": 10, "ac_kwh": 10, "dc_kwh": 9, "battery_kwh": 9,
             "max_charger_kw": 7,              "meta": {"operator": PLUGLINK, "pay_method": "member"}},
            {"charge_id": fast, "start_ts": fast, "end_ts": "2026-08-15T04:00:00Z", "kind": "DC",
             "kwh_added": 20, "charger_kwh": 20, "ac_kwh": 0, "dc_kwh": 20, "max_charger_kw": 50,
             "meta": {"operator": "PlugLink", "pay_method": "member"}},
            {"charge_id": hyper, "start_ts": hyper, "end_ts": "2026-08-15T05:00:00Z", "kind": "DC",
             "kwh_added": 30, "charger_kwh": 30, "ac_kwh": 0, "dc_kwh": 30, "max_charger_kw": 150,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
            {"charge_id": before, "start_ts": before, "end_ts": "2026-07-20T02:00:00Z", "kind": "AC",
             "kwh_added": 10, "charger_kwh": 10, "ac_kwh": 10, "dc_kwh": 9, "max_charger_kw": 7,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
            {"charge_id": "2026-08-02T06:00:00Z", "start_ts": "2026-08-02T06:00:00Z", "end_ts": "2026-08-15T06:00:00Z",
             "kind": "DC", "kwh_added": 40, "charger_kwh": 40, "ac_kwh": 0, "dc_kwh": 40,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
            {"charge_id": "edge", "end_ts": "2026-08-15T07:00:00Z", "kind": "DC", "max_charger_kw": 30,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
            {"charge_id": "ultra", "end_ts": "2026-08-15T08:00:00Z", "kind": "DC", "max_charger_kw": 200,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
        ]
        before_kwh = conn.execute("SELECT kwh_added FROM charges WHERE start_ts = ?", (slow,)).fetchone()[0]
        save_tariff(conn, VID, {"op": "member", "operator": PLUGLINK, "member": True})
        annotate(conn, VID, items)
        self.assertEqual(conn.execute("SELECT kwh_added FROM charges WHERE start_ts = ?", (slow,)).fetchone()[0], before_kwh)
        by_id = {item["charge_id"]: item for item in items}
        self.assertEqual(by_id[slow]["unit_krw_per_kwh"], 292)
        self.assertFalse(by_id[slow]["unit_estimated"])
        self.assertEqual(by_id[fast]["unit_krw_per_kwh"], 322)
        self.assertEqual(by_id[hyper]["unit_krw_per_kwh"], 345)
        self.assertIsNone(by_id[before]["unit_krw_per_kwh"])
        self.assertIsNone(by_id["2026-08-02T06:00:00Z"]["unit_krw_per_kwh"])
        self.assertEqual(by_id["edge"]["unit_krw_per_kwh"], 322)
        self.assertEqual(by_id["ultra"]["unit_krw_per_kwh"], 345)
        save_tariff(conn, VID, {
            "operator": PLUGLINK, "speed": "ac", "member": True,
            "krw_per_kwh": 200, "effective_from": "2026-09-01",
        })
        later = dict(items[0])
        later["end_ts"] = "2026-09-02T03:00:00Z"
        later.pop("unit_krw_per_kwh", None)
        annotate(conn, VID, [later, items[0]])
        self.assertEqual(items[0]["unit_krw_per_kwh"], 292)
        self.assertEqual(later["unit_krw_per_kwh"], 200)
        guest = dict(items[0])
        guest["meta"] = {"operator": PLUGLINK, "pay_method": "guest"}
        guest.pop("unit_krw_per_kwh", None)
        annotate(conn, VID, [guest])
        self.assertEqual(guest["unit_krw_per_kwh"], 357)

    def test_sent_kwh_and_amount_use_about_only_until_the_bill_is_typed(self) -> None:
        conn = _db(self)
        billed = "2026-08-10T00:00:00Z"
        fresh = "2026-08-20T00:00:00Z"
        _charge(conn, billed, ac=10, dc=9)
        _charge(conn, fresh, ac=20, dc=18, end="2026-08-20T04:00:00Z")
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, kwh_billed) VALUES (?, ?, ?, 11)",
            (VID, billed, PLUGLINK),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?)",
            (VID, fresh, PLUGLINK),
        )
        conn.commit()
        save_tariff(conn, VID, {"op": "member", "operator": PLUGLINK, "member": True})
        items = [
            {"charge_id": fresh, "start_ts": fresh, "end_ts": "2026-08-20T04:00:00Z", "kind": "AC",
             "charger_kwh": 20, "ac_kwh": 20, "dc_kwh": 18, "battery_kwh": 18, "max_charger_kw": 7,
             "meta": {"operator": PLUGLINK, "pay_method": "member"}},
        ]
        annotate(conn, VID, items)
        self.assertAlmostEqual(items[0]["sent_kwh"], 22.0)  # 20 * (11/10)
        self.assertTrue(items[0]["sent_estimated"])
        self.assertAlmostEqual(items[0]["cost_krw"], round(22 * 292))
        self.assertTrue(items[0]["cost_estimated"])
        self.assertAlmostEqual(items[0]["efficiency_pct"], 18 / 22 * 100)
        self.assertTrue(items[0]["efficiency_estimated"])
        upsert_charge_meta(conn, VID, {"charge_id": fresh, "operator": PLUGLINK, "kwh_billed": 21, "total_krw": 5000, "discount_krw": 1000})
        items[0]["meta"] = {"operator": PLUGLINK, "kwh_billed": 21, "total_krw": 5000, "discount_krw": 1000}
        annotate(conn, VID, items)
        self.assertEqual(items[0]["sent_kwh"], 21)
        self.assertFalse(items[0]["sent_estimated"])
        self.assertEqual(items[0]["cost_krw"], 5000)
        self.assertFalse(items[0]["cost_estimated"])
        self.assertAlmostEqual(items[0]["unit_krw_per_kwh"], 6000 / 21)

    def test_nearby_operator_is_not_guessed_when_several_match(self) -> None:
        conn = _db(self)
        here = "2026-08-21T00:00:00Z"
        _charge(conn, here, ac=5, dc=4)
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5, 127.0, 'car')",
            (VID, here),
        )
        for idx, name in (("2026-08-01T00:00:00Z", "갑"), ("2026-08-02T00:00:00Z", "을")):
            _charge(conn, idx, end=idx)
            conn.execute(
                "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?)",
                (VID, idx, name),
            )
            conn.execute(
                "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5001, 127.0001, 'car')",
                (VID, idx),
            )
        conn.commit()
        item = {"charge_id": here, "start_ts": here, "end_ts": here, "kind": "AC", "lat": 37.5, "lon": 127.0,
                "ac_kwh": 5, "dc_kwh": 4, "charger_kwh": 5, "meta": {}}
        annotate(conn, VID, [item])
        self.assertEqual(item["operator_display"], "")
        self.assertEqual(item["operator_choices"], ["을", "갑"])
        self.assertIsNone(conn.execute("SELECT operator FROM charge_meta WHERE charge_id = ?", (here,)).fetchone())
        assign_operator(conn, VID, here, "을")
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta WHERE charge_id = ?", (here,)).fetchone()[0], "을")

    def test_one_nearby_operator_is_used_for_the_price(self) -> None:
        conn = _db(self)
        here = "2026-09-02T00:00:00Z"
        old = "2026-08-11T00:00:00Z"
        _charge(conn, here, end="2026-09-02T02:00:00Z")
        _charge(conn, old)
        conn.execute("INSERT INTO charge_meta (vehicle_id, charge_id, operator, kwh_billed, total_krw) VALUES (?, ?, ?, 10, 3000)",
                     (VID, old, "우리집"))
        conn.execute("INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.1, 127.1, 'car')",
                     (VID, old))
        conn.execute("INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.1002, 127.1002, 'car')",
                     (VID, here))
        conn.commit()
        item = {"charge_id": here, "start_ts": here, "end_ts": "2026-09-02T02:00:00Z", "kind": "AC",
                "lat": 37.1, "lon": 127.1, "ac_kwh": 10, "charger_kwh": 10, "dc_kwh": 9, "battery_kwh": 9,
                "max_charger_kw": 7, "meta": {}}
        annotate(conn, VID, [item])
        self.assertEqual(item["operator_display"], "우리집")
        self.assertEqual(item["operator_choices"], [])
        self.assertTrue(item["unit_estimated"])
        self.assertAlmostEqual(item["unit_krw_per_kwh"], 300)


    def test_portal_file_applies_from_its_date_only(self) -> None:
        import io
        import zipfile
        from web.tariff import import_portal_file, portal_band

        self.assertEqual(portal_band("급속(100kW미만)③"), "fast50")
        self.assertEqual(portal_band("완속①"), "ac")
        self.assertEqual(portal_band("초급속"), "ultra")
        conn = _db(self)
        rows = [
            ["순번", "사업자명", "요금유형", "용량구분", "회원가", "", "비회원가", "갱신일자"],
            ["", "", "", "", "충전요금(원)", "비고", "충전요금(원)", ""],
            ["1", "테스트충전", "대표", "완속", "100", "", "180", "2026-11-01"],
            ["2", "테스트충전", "대표", "급속(100kW미만)③", "110", "", "-", "2026-11-01"],
        ]
        body = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, val in enumerate(row):
                col = chr(ord("A") + c)
                cells.append(f'<c r="{col}{r}" t="inlineStr"><is><t>{val}</t></is></c>')
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheet = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(body)}</sheetData></worksheet>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as book:
            book.writestr("xl/worksheets/sheet1.xml", sheet)
        save_tariff(conn, VID, {
            "operator": "테스트충전", "speed": "ac", "member": True,
            "krw_per_kwh": 50, "effective_from": "2026-11-01",
        })
        counts = import_portal_file(conn, buf.getvalue())
        self.assertGreaterEqual(counts["inserted"], 1)
        kept = conn.execute(
            "SELECT krw_per_kwh, source FROM tariff_rates WHERE vehicle_id = ? AND operator = '테스트충전' "
            "AND speed = 'ac' AND member = 1 AND effective_from = '2026-11-01'",
            (VID,),
        ).fetchone()
        self.assertEqual(tuple(kept), (50, "manual"))
        guest = conn.execute(
            "SELECT krw_per_kwh FROM tariff_rates WHERE vehicle_id = '' AND operator = '테스트충전' AND speed = 'ac' "
            "AND member = 0 AND effective_from = '2026-11-01'"
        ).fetchone()[0]
        self.assertEqual(guest, 180)
        fast = conn.execute(
            "SELECT krw_per_kwh FROM tariff_rates WHERE operator = '테스트충전' AND speed = 'fast50' AND member = 1"
        ).fetchone()[0]
        self.assertEqual(fast, 110)
        save_tariff(conn, VID, {"op": "member", "operator": "테스트충전", "member": True})
        save_tariff(conn, VID, {"op": "member", "operator": PLUGLINK, "member": True})
        early = {"end_ts": "2026-10-01T00:00:00Z", "kind": "AC", "max_charger_kw": 7, "meta": {"operator": "테스트충전", "pay_method": "member"}}
        late = {"end_ts": "2026-11-02T00:00:00Z", "kind": "AC", "max_charger_kw": 7, "meta": {"operator": "테스트충전", "pay_method": "member"}}
        annotate(conn, VID, [early, late])
        self.assertIsNone(early["unit_krw_per_kwh"])
        self.assertEqual(late["unit_krw_per_kwh"], 50)
        old = {"end_ts": "2026-08-15T00:00:00Z", "kind": "AC", "max_charger_kw": 7, "meta": {"operator": PLUGLINK, "pay_method": "member"}}
        annotate(conn, VID, [old])
        self.assertEqual(old["unit_krw_per_kwh"], 292)


class NotifyMasterTests(unittest.TestCase):
    def test_seven_kinds_and_master_switch(self) -> None:
        self.assertEqual(list(PREF_ORDER), [
            "trip_start", "trip_end", "charge_start", "charge_done", "parking", "charge_full",
            "sentry", "vehicle", "tire", "maintenance", "software",
        ])
        conn = _db(self)
        prefs = list_prefs(conn, VID)
        self.assertTrue(prefs["master"])
        self.assertEqual([k["id"] for k in prefs["kinds"]], list(PREF_ORDER))
        self.assertTrue(all(k["example"] and k["example_en"] for k in prefs["kinds"]))
        turned = set_pref(conn, VID, "master", True)
        self.assertTrue(turned["master"])
        set_pref(conn, VID, "master", False)
        from datetime import datetime, timezone
        sync_notices(conn, VID, datetime(2026, 11, 13, tzinfo=timezone.utc))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_a_saved_driving_switch_splits_into_start_and_end(self) -> None:
        from datetime import datetime, timezone
        from web.notify import enabled, ensure_schema, remember_alert

        conn = _db(self)
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO notification_prefs (vehicle_id, kind, enabled) VALUES (?, 'driving', 0)",
            (VID,),
        )
        conn.commit()
        prefs = list_prefs(conn, VID)
        by_id = {item["id"]: item["enabled"] for item in prefs["kinds"]}
        self.assertFalse(by_id["trip_start"])
        self.assertFalse(by_id["trip_end"])
        now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        self.assertFalse(remember_alert(conn, VID, "trip_start", "s", "출발", now))
        set_pref(conn, VID, "trip_end", True)
        self.assertTrue(remember_alert(conn, VID, "trip_end", "e", "도착", now))
        self.assertFalse(remember_alert(conn, VID, "trip_start", "s2", "또 출발", now))
        self.assertEqual(
            [row[0] for row in conn.execute("SELECT kind FROM notifications").fetchall()],
            ["trip_end"],
        )
        self.assertFalse(enabled(conn, VID, "trip_start"))

    def test_an_on_driving_switch_leaves_both_on(self) -> None:
        from web.notify import ensure_schema

        conn = _db(self)
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO notification_prefs (vehicle_id, kind, enabled) VALUES (?, 'driving', 1)",
            (VID,),
        )
        conn.commit()
        by_id = {item["id"]: item["enabled"] for item in list_prefs(conn, VID)["kinds"]}
        self.assertTrue(by_id["trip_start"])
        self.assertTrue(by_id["trip_end"])

    def test_master_off_keeps_each_saved_switch(self) -> None:
        from web.notify import enabled

        conn = _db(self)
        set_pref(conn, VID, "trip_start", False)
        set_pref(conn, VID, "master", False)
        prefs = list_prefs(conn, VID)
        by_id = {item["id"]: item["enabled"] for item in prefs["kinds"]}
        self.assertFalse(prefs["master"])
        self.assertFalse(by_id["trip_start"])
        self.assertTrue(by_id["trip_end"])
        self.assertFalse(enabled(conn, VID, "trip_end"))
        set_pref(conn, VID, "master", True)
        self.assertFalse(enabled(conn, VID, "trip_start"))
        self.assertTrue(enabled(conn, VID, "trip_end"))


class ReadonlyTests(unittest.TestCase):
    """GET uses open_readonly. Those calls must not CREATE or INSERT."""

    def test_reads_on_a_readonly_connection(self) -> None:
        from web import list_charges, menu, open_readonly

        conn = _db(self)
        cid = "2026-08-16T01:00:00Z"
        _charge(conn, cid, ac=10, dc=9, end="2026-08-16T05:00:00Z", kw=7)
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, pay_method) VALUES (?, ?, ?, 'member')",
            (VID, cid, PLUGLINK),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'BatteryLevel', '80')",
            (VID, cid),
        )
        save_tariff(conn, VID, {"op": "member", "operator": PLUGLINK, "member": True})
        set_pref(conn, VID, "charge_done", False)
        seeded = conn.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0]
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        conn.close()

        ro = open_readonly(path)
        self.addCleanup(ro.close)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO tariff_rates (vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
                "VALUES ('', 'x', 'slow', 1, '2026-08-01', 1, 'official')"
            )
        tariffs = list_tariffs(ro, VID)
        self.assertGreaterEqual(len(tariffs["rates"]), 5)
        self.assertEqual(tariffs["rates"][0]["krw_per_kwh"] in {292, 322, 345, 357, 528}, True)
        item = list_charges(ro, VID)[0]
        self.assertTrue(item["sent_estimated"])
        self.assertAlmostEqual(item["sent_kwh"], 10.0)
        self.assertTrue(item["cost_estimated"])
        self.assertEqual(item["cost_krw"], 2920)
        self.assertAlmostEqual(item["efficiency_pct"], 90.0)
        summary = menu.charges_summary(ro, VID, "2026-08-01", "2026-08-31")
        self.assertEqual(summary["cost_krw"], 2920)
        self.assertEqual(summary["cost_estimated_krw"], 2920)
        prefs = list_prefs(ro, VID)
        self.assertTrue(prefs["master"])
        self.assertEqual(len(prefs["kinds"]), len(PREF_ORDER))
        done = next(k for k in prefs["kinds"] if k["id"] == "charge_done")
        self.assertFalse(done["enabled"])
        ro.close()

        check = sqlite3.connect(path)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], seeded)
        check.close()

    def test_missing_tables_stay_missing(self) -> None:
        from web import list_charges, open_readonly

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "empty.db"
        sqlite3.connect(path).close()
        ro = open_readonly(path)
        self.addCleanup(ro.close)
        self.assertEqual(list_tariffs(ro, VID), {
            "vehicle_id": VID, "rates": [], "members": {}, "operators": [], "env_card": False,
        })
        annotate(ro, VID, [{
            "charge_id": "c", "kind": "AC", "ac_kwh": 10, "dc_kwh": 9,
            "end_ts": "2026-08-16T05:00:00Z", "meta": {"operator": PLUGLINK},
        }])
        prefs = list_prefs(ro, VID)
        self.assertEqual(len(prefs["kinds"]), len(PREF_ORDER))
        self.assertEqual(list_charges(ro, VID), [])
        ro.close()
        names = sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        self.assertEqual(names, [])

        bare = Path(folder.name) / "bare.db"
        raw = sqlite3.connect(bare)
        raw.executescript(SCHEMA_SQL)
        raw.commit()
        raw.close()
        ro2 = open_readonly(bare)
        self.addCleanup(ro2.close)
        self.assertEqual(list_tariffs(ro2, VID)["rates"], [])
        ro2.close()
        self.assertEqual(
            sqlite3.connect(bare).execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], 0,
        )


class ScreenTests(unittest.TestCase):
    def test_layout_and_wording(self) -> None:
        self.assertNotIn("한국시간", HTML)
        self.assertIn('aria-label="알림 받기"', HTML)
        self.assertNotIn(">알림 받기<", HTML)
        self.assertNotIn("모든 알림 받기", HTML)
        self.assertIn('id="push-master"', HTML)
        self.assertIn("overscroll-behavior: contain", HTML)
        grid = HTML[HTML.index("function tripSheetHtml"):HTML.index("function openTripSheet")]
        self.assertLess(grid.index('keyCell("거리"'), grid.index('keyCell("시간"'))
        self.assertLess(grid.index('keyCell("시간"'), grid.index('keyCell("쓴 전력"'))
        self.assertLess(grid.index('keyCell("쓴 전력"'), grid.index('keyCell("전비"'))
        self.assertIn('foldHtml("에너지 상세", ""', HTML)
        self.assertIn('foldHtml("운전 패턴"', HTML)
        self.assertIn('seatPhrase("운전석"', HTML)
        self.assertIn('" 열선 "', HTML)
        self.assertIn("핸들 열선", HTML)
        self.assertIn('if (step >= 5.5) return "최대"', HTML)
        self.assertIn('hvacOn === "Off"', HTML)
        self.assertIn('foldHtml("에너지 상세"', HTML)
        charge = HTML[HTML.index("function chargeSheetHtml"):HTML.index("function openChargeDetail")]
        energy = charge[charge.index('oneRow("충전량"'):]
        for label in ("충전량", "배터리 저장량", "충전 중 사용량", "손실률", "충전효율"):
            self.assertIn(label, energy)
        self.assertNotIn("차가 받은 양", charge)
        self.assertNotIn("충전기가 보낸 양", charge)
        self.assertNotIn("출처: 한국환경공단", HTML)
        self.assertNotIn('" (추정)"', HTML)
        self.assertNotIn("예상보다", HTML)
        self.assertIn("할인·포인트 (원)", HTML)
        self.assertIn('const APP_VERSION = "v1.16.5"', HTML)


if __name__ == "__main__":
    unittest.main()
