"""Dismissed service notices, due-date copy, pressure display, and bot ids."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from process import connect, format_ts
from web.bot_notify import apply_bot_action
from ledger.consumables import list_items, save_replacement, seed_consumables
from web.notice_view import event_facts, present
from web.notify import _insert, delete_notices, list_notifications, sync_notices
from web.pressure import convert_psi_text, format_bar_number, format_psi_number

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
VID = "car-a"


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


class CopyTests(unittest.TestCase):
    def test_overdue_copy_and_webhook_keep_the_sign(self) -> None:
        row = {
            "id": "maint:cons-washer:overdue:2026-09-19/",
            "ts": "2026-11-15T03:00:00Z",
            "kind": "maintenance",
            "title": "워셔액 점검 기한이 지났어요",
            "body": "-57일 · 2026-09-19",
            "details": [["남은 날", "-57일"], ["예정일", "2026-09-19"]],
        }
        view = present(row)
        self.assertEqual(view["summary"], "57일 지났어요, 워셔액을 점검해 주세요")
        self.assertEqual(view["push_body"], view["summary"])
        labels = dict(view["rows"])
        self.assertEqual(labels["지난 날"], "57일")
        self.assertEqual(labels["기한"], "9월 19일")
        self.assertNotIn("남은 날", labels)
        self.assertNotIn("-", labels["지난 날"])
        facts = event_facts(row["body"], row["details"], row["id"])
        self.assertEqual(facts["remaining_days"], -57)
        self.assertEqual(facts["status"], "overdue")
        self.assertNotIn("days", facts)

    def test_full_charge_detail_is_one_value_per_row(self) -> None:
        row = {
            "id": "charge_full:2026-11",
            "ts": "2026-11-15T03:00:00Z",
            "kind": "charge_full",
            "title": "한 번 완충할 시기예요",
            "body": "한 달 동안 100% 가까이 충전한 기록이 없어요. 한 달에 한 번은 100%까지 충전하는 것을 권장해요.",
            "details": [],
        }
        view = present(row)
        self.assertEqual(view["summary"], "한 달 동안 완충 기록이 없어요. 100%까지 한 번 채워 주세요")
        self.assertGreater(len(view["summary"]), len(view["title"]))
        labels = [pair[0] for pair in view["rows"]]
        self.assertEqual(labels, ["마지막 100% 충전"])
        self.assertEqual(view["rows"][0][1], "기록 없음")
        self.assertNotIn("권장", labels)
        kept = present({
            **row,
            "details": [
                ["마지막 100% 충전", "2026년 9월 19일"],
                ["최근 30일 최고", "80%"],
                ["권장", "한 달에 한 번은 100%까지 충전하는 것을 권장해요"],
            ],
        })
        self.assertEqual([pair[0] for pair in kept["rows"]], ["마지막 100% 충전", "최근 30일 최고"])
        facts = event_facts(kept["summary"], kept["rows"], row["id"])
        self.assertEqual(facts["last_full"], "2026년 9월 19일")
        self.assertEqual(facts["peak_pct"], 80)
        self.assertNotIn("advice", facts)


class DismissTests(unittest.TestCase):
    def test_deleted_service_and_full_charge_wait_for_a_new_situation(self) -> None:
        conn = _db(self)
        notice = "maint:cons-washer:overdue:2026-09-19/"
        self.assertTrue(_insert(
            conn, VID, notice, "maintenance", "워셔액 점검 기한이 지났어요", "-57일",
            "Washer", "", "#consumables", NOW, [["지난 날", "57일"]],
        ))
        before = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("trips", "charges", "alert_log")
        }
        delete_notices(conn, VID, notice)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertFalse(_insert(
            conn, VID, notice, "maintenance", "워셔액 점검 기한이 지났어요", "-57일",
            "Washer", "", "#consumables", NOW,
        ))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        changed = "maint:cons-washer:soon:2026-12-01/"
        self.assertTrue(_insert(
            conn, VID, changed, "maintenance", "워셔액 점검 시기예요", "12일",
            "Washer", "", "#consumables", NOW,
        ))
        after = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("trips", "charges", "alert_log")
        }
        self.assertEqual(before, after)

    def test_bot_accepts_a_real_service_id(self) -> None:
        conn = _db(self)
        notice = "maint:cons-washer:overdue:2026-09-19/"
        conn.execute(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, delivery) "
            "VALUES (?, ?, ?, 'maintenance', '정비', '-57일', 'pending')",
            (VID, notice, "2026-11-15T03:00:00Z"),
        )
        conn.commit()
        import os
        os.environ["BOT_PUSH_TOKEN"] = "bot-token-not-a-vin-value"
        self.addCleanup(os.environ.pop, "BOT_PUSH_TOKEN", None)
        out = apply_bot_action(conn, VID, {
            "action": "skip", "id": notice, "title": "워셔액 점검을 확인해 주세요", "summary": "57일 지났어요",
        }, NOW)
        self.assertEqual(out["id"], notice)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {"action": "skip", "id": "../secret"}, NOW)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {"action": "skip", "id": "has space"}, NOW)


class BatteryFullTests(unittest.TestCase):
    def test_charge_sets_the_date_without_rewriting_rows(self) -> None:
        conn = _db(self)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', 'P')",
            (VID, format_ts(NOW)),
        )
        conn.commit()
        seed_consumables(conn)
        before_items = conn.execute("SELECT COUNT(*) FROM consumable_items").fetchone()[0]
        before_charges = conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, end_soc) "
            "VALUES (?, ?, ?, 'AC', 99.4)",
            (VID, format_ts(NOW - timedelta(days=40)), format_ts(NOW - timedelta(days=40, hours=-1))),
        )
        conn.commit()
        low = list_items(conn, VID, as_of=NOW.astimezone(timezone(timedelta(hours=9))).date())
        battery = next(item for item in low if item["id"] == "cons-battery-full")
        self.assertEqual(battery["interval_months"], 1)
        self.assertNotEqual(battery["last_replaced_date"], "2026-10-06")
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, end_soc) "
            "VALUES (?, ?, ?, 'AC', 99.6)",
            (VID, "2026-10-06T00:00:00Z", "2026-10-06T02:00:00Z"),
        )
        conn.commit()
        items = list_items(conn, VID, as_of=date(2026, 11, 15))
        battery = next(item for item in items if item["id"] == "cons-battery-full")
        self.assertEqual(battery["last_replaced_date"], "2026-10-06")
        self.assertEqual(battery["status"], "overdue")
        stored = conn.execute(
            "SELECT end_soc FROM charges WHERE end_ts = '2026-10-06T02:00:00Z'"
        ).fetchone()
        self.assertEqual(stored[0], 99.6)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM consumable_items").fetchone()[0], before_items)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], before_charges + 2)
        sync_notices(conn, VID, NOW)
        notes = [n for n in list_notifications(conn, VID)["notifications"] if n["id"].startswith("maint:cons-battery-full:")]
        self.assertEqual(len(notes), 1)
        self.assertIn("지났어요, 100%까지 한 번 충전해 주세요", notes[0]["display_body"])
        self.assertNotIn("charge_full", {n["kind"] for n in list_notifications(conn, VID)["notifications"]})
        notice_id = notes[0]["id"]
        facts = event_facts(notes[0]["body"], notes[0]["details"], notice_id)
        self.assertEqual(facts["status"], "overdue")
        self.assertLess(facts["remaining_days"], 0)
        delete_notices(conn, VID, notice_id)
        sync_notices(conn, VID, NOW)
        again = [n for n in list_notifications(conn, VID)["notifications"] if n["id"] == notice_id]
        self.assertEqual(again, [])
        save_replacement(conn, VID, {
            "item_id": "cons-battery-full", "date": "2026-11-01", "odometer_km": 10, "action": "inspect",
        })
        manual = list_items(conn, VID, as_of=date(2026, 11, 15))
        battery = next(item for item in manual if item["id"] == "cons-battery-full")
        self.assertEqual(battery["last_replaced_date"], "2026-11-01")


class PressureTests(unittest.TestCase):
    def test_toggling_does_not_change_the_stored_number(self) -> None:
        stored = "앞 왼쪽 42.4 psi, 권장 42 psi"
        seen = []
        for _ in range(6):
            seen.append(convert_psi_text(stored, "bar"))
            seen.append(convert_psi_text(stored, "psi"))
        self.assertEqual(seen[0], seen[2])
        self.assertEqual(seen[0], "앞 왼쪽 2.92 bar, 권장 2.90 bar")
        self.assertEqual(seen[1], stored)
        self.assertEqual(convert_psi_text(stored, "psi"), stored)
        self.assertEqual(format_psi_number(42.4, "bar"), "2.92 bar")
        self.assertEqual(format_bar_number(2.9233, "psi"), "42.4 psi")
        self.assertEqual(format_bar_number(2.9233, "bar"), "2.92 bar")
        self.assertIn('id="pressure-unit"', HTML)
        self.assertIn("BAR_PER_PSI = 0.0689476", HTML)
        self.assertIn("gap: 8px", HTML)
        self.assertIn("정비 알림: 기한이 지나면 며칠 지났는지 알려요", HTML)


if __name__ == "__main__":
    unittest.main()
