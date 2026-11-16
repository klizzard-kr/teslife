"""t583: 소모품 기록 종류(교체/점검/작업), 항목별 리셋 규칙, 점검 차계부, 알림 문구."""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ledger import connect as ledger_connect
from ledger.consumables import (
    RESET_ON, SEED_ACTIONS, create_item, due_label, ensure_consumable_schema, list_items,
    list_replacements, save_replacement, seed_consumables, update_item, update_replacement,
)
from process import KST, connect as process_connect, format_ts, parse_ts
from process import maintenance as m

ROOT = Path(__file__).resolve().parents[1]
VID = "VINSP"


def item(conn, iid, as_of="2026-10-31"):
    return next(i for i in list_items(conn, VID, as_of=as_of) if i["id"] == iid)


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        self.conn = process_connect(self.db)
        self.conn.execute("INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Odometer', ?)",
                          (VID, "2026-10-31T00:00:00Z", json.dumps(900.0)))
        self.conn.commit()
        self.conn.row_factory = sqlite3.Row
        seed_consumables(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def ledger_rows(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM ledger WHERE vehicle_id = ?", (VID,)).fetchone()[0]

    def test_companion_work_is_one_ledger_row(self) -> None:
        save_replacement(self.conn, VID, {
            "item_id": "cons-brake-pads",
            "action": "replace",
            "date": "2026-10-31",
            "odometer_km": 900,
            "price_krw": 120000,
            "also": [
                {"item_id": "cons-alignment", "action": "service"},
                {"item_id": "cons-caliper", "action": "service"},
                {"item_id": "cons-brake-pads", "action": "inspect"},
            ],
        })
        reps = self.conn.execute(
            "SELECT item_id, action, price_krw, ledger_id FROM replacements WHERE vehicle_id = ? ORDER BY item_id",
            (VID,),
        ).fetchall()
        self.assertEqual(len(reps), 3)
        priced = [r for r in reps if r["price_krw"]]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["item_id"], "cons-brake-pads")
        self.assertEqual(self.ledger_rows(), 1)
        companions = [r for r in reps if not r["price_krw"]]
        self.assertTrue(all(r["ledger_id"] is None for r in companions))

    def test_seed_defaults(self) -> None:
        want = {"cons-washer": ("inspect", "inspect"), "cons-brake-fluid": ("inspect", "inspect"),
                "cons-tire-rotation": ("service", "service"), "cons-caliper": ("service", "service"),
                "cons-cabin-filter": ("replace", "replace"), "cons-hepa-filter": ("replace", "replace"),
                "cons-wiper": ("replace", "replace"), "cons-brake-pads": ("inspect", "replace"),
                "cons-12v-battery": ("replace", "replace"), "cons-ac-desiccant": ("replace", "replace"),
                "cons-tires-season": ("replace", "replace")}
        for iid, (da, ro) in want.items():
            it = item(self.conn, iid)
            self.assertEqual((it["default_action"], it["reset_on"]), (da, ro), iid)
        self.assertEqual(item(self.conn, "cons-brake-pads")["interval_label"], "필요 시 점검")
        self.assertEqual(item(self.conn, "cons-12v-battery")["interval_label"], "필요 시 교체")

    def test_owner_edit_not_overwritten_by_seed(self) -> None:
        update_item(self.conn, VID, {"id": "cons-wiper", "reset_on": "inspect", "default_action": "inspect"})
        seed_consumables(self.conn)
        it = item(self.conn, "cons-wiper")
        self.assertEqual((it["default_action"], it["reset_on"]), ("inspect", "inspect"))
        with self.assertRaises(ValueError):
            update_item(self.conn, VID, {"id": "cons-wiper", "reset_on": "bogus"})

    def test_inspect_resets_washer_without_ledger_row(self) -> None:
        self.assertEqual(item(self.conn, "cons-washer")["status"], "overdue")
        r = save_replacement(self.conn, VID, {"item_id": "cons-washer", "date": "2026-10-31", "odometer_km": 900})
        self.assertEqual(r["action"], "inspect")          # item default
        self.assertIsNone(r["ledger_id"])                 # no price -> no 차계부 row
        self.assertEqual(r["price_krw"], 0)
        self.assertEqual(self.ledger_rows(), 0)
        it = item(self.conn, "cons-washer")
        self.assertEqual(it["status"], "ok")
        self.assertEqual(it["last_replaced_date"], "2026-10-31")
        self.assertEqual(it["last_action"], "inspect")

    def test_paid_inspect_writes_ledger_with_label(self) -> None:
        r = save_replacement(self.conn, VID, {"item_id": "cons-washer", "date": "2026-10-31",
                                              "odometer_km": 900, "price_krw": 3000})
        self.assertIsNotNone(r["ledger_id"])
        memo = self.conn.execute("SELECT memo FROM ledger WHERE id = ?", (r["ledger_id"],)).fetchone()[0]
        self.assertEqual(memo, "워셔액 점검")             # no '워셔액 점검 점검'
        # price removed later -> ledger row goes away; set again -> comes back
        r2 = update_replacement(self.conn, VID, {"id": r["id"], "price_krw": 0})
        self.assertIsNone(r2["ledger_id"])
        self.assertEqual(self.ledger_rows(), 0)
        r3 = update_replacement(self.conn, VID, {"id": r["id"], "price_krw": 2500})
        self.assertIsNotNone(r3["ledger_id"])
        self.assertEqual(self.ledger_rows(), 1)

    def test_replace_still_requires_price_and_writes_ledger(self) -> None:
        with self.assertRaises(ValueError):
            save_replacement(self.conn, VID, {"item_id": "cons-wiper", "date": "2026-10-31", "odometer_km": 900})
        r = save_replacement(self.conn, VID, {"item_id": "cons-wiper", "date": "2026-10-31",
                                              "odometer_km": 900, "price_krw": 0})
        self.assertEqual(r["action"], "replace")
        self.assertIsNotNone(r["ledger_id"])

    def test_inspect_does_not_reset_replace_only_item(self) -> None:
        save_replacement(self.conn, VID, {"item_id": "cons-wiper", "action": "inspect",
                                          "date": "2026-10-31", "odometer_km": 900})
        it = item(self.conn, "cons-wiper")
        self.assertNotEqual(it["last_replaced_date"], "2026-10-31")   # cycle unchanged
        self.assertEqual(it["last_record"]["action"], "inspect")       # but shown as latest record
        self.assertEqual(it["last_record"]["date"], "2026-10-31")
        # owner switches the item to reset on 점검 -> applies immediately
        update_item(self.conn, VID, {"id": "cons-wiper", "reset_on": "inspect"})
        self.assertEqual(item(self.conn, "cons-wiper")["last_replaced_date"], "2026-10-31")

    def test_service_items(self) -> None:
        save_replacement(self.conn, VID, {"item_id": "cons-tire-rotation", "action": "inspect",
                                          "date": "2026-10-31", "odometer_km": 900})
        self.assertEqual(item(self.conn, "cons-tire-rotation")["last_replaced_odometer_km"], 0)
        r = save_replacement(self.conn, VID, {"item_id": "cons-tire-rotation", "date": "2026-10-31", "odometer_km": 900})
        self.assertEqual(r["action"], "service")
        self.assertIsNone(r["ledger_id"])                 # free rotation: no 차계부 row
        it = item(self.conn, "cons-tire-rotation")
        self.assertEqual(it["last_replaced_odometer_km"], 900)
        self.assertEqual(it["next_due_km"], 10900)
        self.assertEqual(RESET_ON["service"], ("replace", "service"))

    def test_history_has_action(self) -> None:
        save_replacement(self.conn, VID, {"item_id": "cons-washer", "date": "2026-10-31", "odometer_km": 900})
        h = list_replacements(self.conn, VID, item_id="cons-washer")
        self.assertEqual(h["replacements"][0]["action"], "inspect")
        self.assertEqual(h["total_krw"], 0)

    def test_custom_item_actions(self) -> None:
        create_item(self.conn, VID, {"id": "cons-x", "name": "타이어 공기 점검", "rule": "months_only",
                                     "interval_months": 1, "default_action": "inspect"})
        it = item(self.conn, "cons-x")
        self.assertEqual((it["default_action"], it["reset_on"]), ("inspect", "inspect"))
        create_item(self.conn, VID, {"id": "cons-y", "name": "와셔 노즐", "rule": "months_only", "interval_months": 6})
        it = item(self.conn, "cons-y")
        self.assertEqual((it["default_action"], it["reset_on"]), ("replace", "replace"))

    def test_due_label(self) -> None:
        self.assertEqual(due_label("워셔액 점검", "inspect"), "워셔액 점검")
        self.assertEqual(due_label("브레이크액", "inspect"), "브레이크액 점검")
        self.assertEqual(due_label("에어컨 필터(캐빈)", "replace"), "에어컨 필터(캐빈) 교체")
        self.assertEqual(due_label("타이어 로테이션", "service"), "타이어 로테이션")

    def test_alert_texts_by_action(self) -> None:
        sent = []
        self.conn.row_factory = None
        m.check_consumables(self.conn, VID, None, datetime(2026, 10, 31, 18, 0, tzinfo=KST),
                            lambda p: (sent.append(p) or (True, "http 200")))
        msgs = [p["message"] for p in sent]
        self.assertEqual(msgs, [
            "배터리 완충 기한이 지났어요 · 9/19에 지났어요",
            "워셔액 기한이 지났어요 · 9/19에 지났어요",
        ])
        # 작업형 and 교체형 wording
        self.conn.row_factory = sqlite3.Row
        update_item(self.conn, VID, {"id": "cons-tire-rotation", "interval_km": 1200})
        update_item(self.conn, VID, {"id": "cons-wiper", "interval_months": 3})
        self.conn.row_factory = None
        sent.clear()
        m.check_consumables(self.conn, VID, None, datetime(2026, 11, 10, 9, 0, tzinfo=KST),
                            lambda p: (sent.append(p) or (True, "http 200")))
        msgs = sorted(p["message"] for p in sent)
        self.assertFalse(any("로테이션" in s for s in msgs), msgs)
        self.assertTrue(any(s.startswith("와이퍼 점검 시기예요") for s in msgs), msgs)


class MigrationTests(unittest.TestCase):
    def test_old_db_gets_columns_and_old_records_are_replace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "old.db"
            c = sqlite3.connect(db)
            c.executescript("""
                CREATE TABLE replacements (vehicle_id TEXT NOT NULL, id TEXT NOT NULL, item_id TEXT NOT NULL,
                  date TEXT NOT NULL, odometer_km REAL NOT NULL, price_krw REAL NOT NULL, vendor TEXT,
                  quantity REAL, memo TEXT, receipt_path TEXT, ledger_id TEXT, PRIMARY KEY (vehicle_id, id));
                CREATE TABLE consumable_items (vehicle_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                  interval_km REAL, interval_months REAL, rule TEXT NOT NULL, warn_before_km REAL,
                  warn_before_days REAL, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (vehicle_id, id));
                INSERT INTO consumable_items VALUES ('V','cons-washer','워셔액 점검',NULL,1,'months_only',500,14,1);
                INSERT INTO replacements VALUES ('V','r1','cons-washer','2026-10-01',800,0,NULL,1,NULL,NULL,NULL);
            """)
            c.commit(); c.close()
            conn = ledger_connect(db)
            ensure_consumable_schema(conn)
            self.assertEqual(conn.execute("SELECT action FROM replacements").fetchone()[0], "replace")
            seed_consumables(conn)
            row = conn.execute("SELECT default_action, reset_on, name FROM consumable_items WHERE id='cons-washer'").fetchone()
            self.assertEqual(tuple(row), ("inspect", "inspect", "워셔액 점검"))
            conn.close()


class UiTests(unittest.TestCase):
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_record_kind_select_and_buttons(self) -> None:
        self.assertIn('name="action"', self.html)
        self.assertIn("교체 완료", self.html)
        self.assertIn('const ACTION_KO = { replace: "교체", inspect: "점검", service: "작업" };', self.html)
        self.assertIn('name="reset_on"', self.html)
        self.assertIn('name="default_action"', self.html)

    def test_no_dash_placeholders_in_consumables(self) -> None:
        i = self.html.index("async function showHistory")
        block = self.html[i:i + 2500]
        self.assertNotIn('"—"', block)

    def test_help_only_in_tooltip(self) -> None:
        self.assertNotIn('consumable-help', self.html)      # : i tooltips removed


if __name__ == "__main__":
    unittest.main()
