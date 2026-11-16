"""v1.9.0: midnight charge lookup, payment defaults, inbox, tires, and the home tiles."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledger import ensure_schema
from ledger import tiresets as T
from process import connect, format_ts
from process.alerts import _emit
from process.charge_backfill import apply_station_matches, canonical_station_name
from web import list_charges
from web.notify import _insert, remember_alert
from web.tariff import unit_price

VID = "car-a"
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
SW = (ROOT / "web" / "static" / "sw.js").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)


def _db(test: unittest.TestCase):
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    path = Path(folder) / "signals.db"
    conn = connect(path)
    conn.row_factory = sqlite3.Row
    test.addCleanup(conn.close)
    ensure_schema(conn)
    return conn


class MidnightTests(unittest.TestCase):
    def test_start_id_finds_a_charge_the_start_day_filter_misses(self) -> None:
        conn = _db(self)
        start = datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)  # 23:30 KST
        end = start + timedelta(hours=1)  # 00:30 KST the next day
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 4)",
            (VID, cid, format_ts(end)),
        )
        conn.commit()
        missed = list_charges(conn, VID, date_from="2026-11-01", date_to="2026-11-01")
        self.assertEqual(missed, [])
        found = list_charges(conn, VID, start_ts=cid)
        self.assertEqual([row["charge_id"] for row in found], [cid])
        self.assertIn("start: cid", HTML)


class PaymentTests(unittest.TestCase):
    def test_empty_plug_link_becomes_member_without_changing_row_counts(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, pay_method) VALUES (?, 'c1', '플러그링크', NULL)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, pay_method) VALUES (?, 'c2', '플러그링크', 'guest')",
            (VID,),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0]
        ensure_schema(conn)
        ensure_schema(conn)
        rows = dict(conn.execute("SELECT charge_id, pay_method FROM charge_meta").fetchall())
        self.assertEqual(rows["c1"], "member")
        self.assertEqual(rows["c2"], "guest")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0], before)

    def test_empty_method_uses_the_last_choice_then_guest(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, max_charger_kw) "
            "VALUES (?, 'old', 'old2', 'AC', 10, 7)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, pay_method) VALUES (?, 'old', '플러그링크', 'member')",
            (VID,),
        )
        conn.commit()
        member_price, _learned = unit_price(
            conn, VID, "플러그링크", {"kind": "AC", "max_charger_kw": 7, "meta": {}}, "2026-11-01",
        )
        explicit, _learned = unit_price(
            conn, VID, "플러그링크",
            {"kind": "AC", "max_charger_kw": 7, "meta": {"pay_method": "guest"}}, "2026-11-01",
        )
        self.assertIsNotNone(member_price)
        self.assertIsNotNone(explicit)
        self.assertNotEqual(member_price, explicit)
        self.assertNotIn('id="env-card"', HTML)

    def test_stored_name_rewrites_a_shorter_one_without_a_migration(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'canon', 'canon2', 'AC', 5)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, station_name) "
            "VALUES (?, 'canon', '한빛전력', '북측한빛캐슬아파트')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'd1', 'd2', 'AC', 5)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, station_name) "
            "VALUES (?, 'd1', '내가 고른 곳', '북측 한빛캐슬')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_nm, busi_nm, source, updated_ts) "
            "VALUES (?, 'd1', '북측 한빛캐슬', NULL, 'station', 't')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'd2', 'd3', 'AC', 5)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name) VALUES (?, 'd2', '북측 한빛캐슬')",
            (VID,),
        )
        before = conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0]
        ensure_schema(conn)
        name, operator = conn.execute(
            "SELECT station_name, operator FROM charge_meta WHERE charge_id = 'd1'"
        ).fetchone()
        stat, busi = conn.execute(
            "SELECT stat_nm, busi_nm FROM charge_charger WHERE charge_id = 'd1'"
        ).fetchone()
        empty_name, empty_op = conn.execute(
            "SELECT station_name, operator FROM charge_meta WHERE charge_id = 'd2'"
        ).fetchone()
        self.assertEqual(canonical_station_name(conn, VID, "북측 한빛캐슬"), "북측한빛캐슬아파트")
        self.assertEqual(name, "북측한빛캐슬아파트")
        self.assertEqual(operator, "내가 고른 곳")
        self.assertEqual(stat, "북측한빛캐슬아파트")
        self.assertEqual(busi, "한빛전력")
        self.assertEqual(empty_name, "북측한빛캐슬아파트")
        self.assertEqual(empty_op, "한빛전력")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0], before)

    def test_alias_setting_renames_when_nothing_is_stored_yet(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO vehicle_settings (vehicle_id, key, value) VALUES (?, 'station_aliases', ?)",
            (VID, '[{"name": "북측한빛캐슬아파트", "operator": "한빛전력", "prefix": "북측한빛캐슬"}]'),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'd1', 'd2', 'AC', 5)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, station_name) VALUES (?, 'd1', '북측 한빛캐슬')",
            (VID,),
        )
        ensure_schema(conn)
        name, operator = conn.execute(
            "SELECT station_name, operator FROM charge_meta WHERE charge_id = 'd1'"
        ).fetchone()
        self.assertEqual(name, "북측한빛캐슬아파트")
        self.assertEqual(operator, "한빛전력")


class InboxTests(unittest.TestCase):
    def test_webhook_failure_still_stores_the_inbox_row(self) -> None:
        conn = _db(self)

        def fail(_payload):
            return False, "down"

        ok = _emit(conn, VID, "tire_low", "fl", "앞왼쪽\n공기압|30 psi", {}, NOW, fail)
        self.assertFalse(ok)
        row = conn.execute("SELECT title, body, kind FROM notifications").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "앞왼쪽")
        self.assertIn("30 psi", conn.execute("SELECT details FROM notifications").fetchone()[0])
        again = remember_alert(conn, VID, "tire_low", "fl", "앞왼쪽", NOW)
        self.assertFalse(again)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_push_uses_the_first_line_and_a_notice_link(self) -> None:
        conn = _db(self)
        _insert(
            conn, VID, "charge_end:abc", "charge_done", "충전 완료",
            "46% → 80%, 23.4kWh\n충전량|23.4 kWh",
            "Charging finished", "46% → 80%, 23.4kWh\nEnergy|23.4 kWh",
            "#charges", NOW,
        )
        body = conn.execute("SELECT body FROM notifications").fetchone()[0]
        self.assertTrue(body.startswith("46% → 80%, 23.4kWh"))
        self.assertNotIn('title || "Teslife"', SW)
        self.assertIn("from\\s+", SW)
        self.assertIn("navigate", SW)
        self.assertIn("inbox-tabs", HTML)
        self.assertIn("주행", HTML)
        self.assertNotIn(">전체<", HTML[HTML.index('id="inbox-tabs"'):HTML.index('id="inbox-list"')])


class TireLedgerTests(unittest.TestCase):
    def test_saving_a_set_leaves_ledger_rows_byte_identical(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, odometer_km, memo, source) "
            "VALUES (?, 'led1', '2026-08-01', 'tires', 1000, 10, '그대로', 'manual')",
            (VID,),
        )
        conn.commit()
        before = conn.execute("SELECT * FROM ledger").fetchall()
        st = T.create_set(conn, VID, {"kind": "summer", "price_krw": 900000, "purchase_date": "2026-08-10"})
        conn.execute("UPDATE tire_sets SET ledger_id = 'led1' WHERE id = ?", (st["id"],))
        conn.commit()
        T.update_set(conn, VID, {"id": st["id"], "price_krw": 1, "memo": "바꿈"})
        T.update_set(conn, VID, {"id": st["id"], "price_krw": ""})
        after = conn.execute("SELECT * FROM ledger").fetchall()
        self.assertEqual(after, before)
        T.delete_set(conn, VID, st["id"])
        self.assertEqual(conn.execute("SELECT * FROM ledger").fetchall(), before)

    def test_open_interval_blocks_a_new_mount(self) -> None:
        conn = _db(self)
        first = T.create_set(conn, VID, {"kind": "summer"})
        T.mount_set(conn, VID, {"set_id": first["id"], "date": "2026-01-01", "odometer_km": 1000})
        other = T.create_set(conn, VID, {"kind": "winter"})
        conn.execute(
            "INSERT INTO tire_mounts (vehicle_id, id, set_id, mounted_date, mounted_odo) "
            "VALUES (?, 'open2', ?, '2026-06-01', 5000)",
            (VID, other["id"]),
        )
        conn.commit()
        with self.assertRaises(ValueError):
            T.mount_set(conn, VID, {"set_id": first["id"], "date": "2026-07-01", "odometer_km": 6000})


class ScreenTests(unittest.TestCase):
    def test_home_and_copy(self) -> None:
        self.assertIn('id="m-software"', HTML)
        self.assertIn('id="m-odo"', HTML)
        self.assertIn("배터리 최저 온도", HTML)
        self.assertIn("배터리 최고 온도", HTML)
        self.assertIn("평균 충전 전력", HTML)
        self.assertIn("최대 충전 전력", HTML)
        self.assertIn(">타이어 교체<", HTML)
        self.assertIn(">지금 달린 세트 등록<", HTML)
        self.assertIn("edit_mount", HTML)
        self.assertIn("MIT", README)
        self.assertIn("Leaflet", README)
        self.assertIn("v1.9.0", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertNotIn("한국시간", HTML)


class BackfillGuardTests(unittest.TestCase):
    def test_saved_operator_is_not_replaced(self) -> None:
        conn = _db(self)
        start = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, max_charger_kw) "
            "VALUES (?, ?, ?, 'AC', 5, 7)",
            (VID, cid, format_ts(start + timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5, 127.0, 'car')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, location_text) VALUES (?, ?, '내 사업자', '우리집')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, addr, zcode, updated_ts) "
            "VALUES ('ST', '1', 37.5, 127.0, '02', 7, '카탈로그', '다른이름', '주소', '11', 't')",
        )
        conn.commit()
        stats = apply_station_matches(conn, VID, NOW)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "내 사업자")
        self.assertEqual(conn.execute("SELECT location_text FROM charge_meta").fetchone()[0], "우리집")


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class SoftwareSignalTests(unittest.TestCase):
    """Production car: Version 2026.38.3.2, a blank update version, download 0, install 1, available false."""

    VALUES = {
        "Version": "2026.38.3.2",
        "SoftwareUpdateVersion": " ",
        "SoftwareUpdateDownloadPercentComplete": 0,
        "SoftwareUpdateInstallationPercentComplete": 1,
        "SoftwareUpdateAvailable": False,
    }

    def test_blank_version_does_not_push(self) -> None:
        from process import Events
        from process.alerts import check_software_update, install_in_progress

        self.assertFalse(install_in_progress(0, 1, False))
        conn = _db(self)
        t = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)
        ev = Events([(t, name, value) for name, value in self.VALUES.items()])
        sent: list = []

        def sender(payload):
            sent.append(payload)
            return True, "ok"

        check_software_update(conn, VID, ev, t, sender)
        self.assertEqual(sent, [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_stray_install_percent_is_not_in_progress(self) -> None:
        from process import Events
        from process.alerts import check_software_update

        conn = _db(self)
        t = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)
        offered = dict(self.VALUES, SoftwareUpdateVersion="2026.44.1")
        ev = Events([(t, name, value) for name, value in offered.items()])
        sent: list = []

        def sender(payload):
            sent.append(payload)
            return True, "ok"

        check_software_update(conn, VID, ev, t, sender)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["data"]["phase"], "available")
        self.assertNotIn("설치", sent[0]["message"])
        self.assertEqual(sent[0]["message"], "2026.38.3.2 → 2026.44.1")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_home_tile_has_no_arrow_for_a_blank_update_version(self) -> None:
        src = _fn("softwareText") + _fn("softwareUpcoming")
        src += 'process.stdout.write(softwareUpcoming("2026.38.3.2", " ") + "|" + softwareUpcoming("2026.38.3.2", "2026.44.1"));'
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, "|2026.44.1")


if __name__ == "__main__":
    unittest.main()
