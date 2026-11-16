""": tire sets (mount/remove, per-set km and rotation, tread / DOT / warranty alerts, ledger)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ledger import ensure_schema as ensure_ledger_schema
from ledger.consumables import list_items, save_replacement, seed_consumables
from ledger.tires import get_tire_settings
from ledger import tiresets as T
from process import connect as process_connect, format_ts
from process import maintenance as m
from web import dispatch_write

ROOT = Path(__file__).resolve().parents[1]
VID = "VSET"
TODAY = date(2026, 10, 31)
NOW = datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = process_connect(Path(self.tmp.name) / "signals.db")
        ensure_ledger_schema(self.conn)
        self.conn.row_factory = sqlite3.Row
        self.odo(8000)
        seed_consumables(self.conn)
        self.sent: list[str] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def odo(self, km: float) -> None:
        with self.conn:
            n = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            self.conn.execute("INSERT INTO signals VALUES (?, ?, 'Odometer', ?)",
                              (VID, f"2026-10-30T{n // 60:02d}:{n % 60:02d}:00Z", json.dumps(km)))

    def sender(self, p):
        self.sent.append(p["message"])
        return True, "http 200"

    def summer(self, **kw):
        body = {"kind": "summer", "brand": "한국타이어", "model": "iON evo", "size": "255/45R19",
                "mount": {"date": "2026-08-19", "odometer_km": 0}, **kw}
        return T.create_set(self.conn, VID, body)

    def ledger_rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM ledger WHERE category = 'tires' ORDER BY date")]

    def sets(self):
        return {s["kind"]: s for s in T.list_sets(self.conn, VID, today=TODAY)}


class SetTests(Base):
    def test_season_due_follows_mounts_not_purchase(self) -> None:
        bare = T.create_set(self.conn, VID, {"kind": "summer", "price_krw": 1000, "purchase_date": "2026-11-01"})
        self.assertEqual(T.season_due(self.conn, VID, date(2026, 11, 7)), "2026-12-01")
        T.update_set(self.conn, VID, {"id": bare["id"], "memo": "메모만"})
        self.assertEqual(T.season_due(self.conn, VID, date(2026, 11, 7)), "2026-12-01")
        winter = T.create_set(self.conn, VID, {"kind": "winter"})
        T.mount_set(self.conn, VID, {"set_id": winter["id"], "date": "2026-11-02", "odometer_km": 8000})
        self.assertEqual(T.season_due(self.conn, VID, date(2026, 11, 7)), "2027-03-01")
        self.assertEqual(T.season_due(self.conn, VID, date(2027, 1, 10)), "2027-03-01")
        only = T.next_season_due(date(2026, 11, 7), [], has_seasonal=False)
        self.assertIsNone(only)

    def test_first_set_counts_from_mount(self) -> None:
        st = self.summer()
        self.assertTrue(st["mounted"])
        self.assertEqual(st["label"], "여름용 · 한국타이어 iON evo")
        self.assertEqual(st["km_total"], 8000)
        self.assertEqual(st["days_total"], (T._today() - date(2026, 8, 19)).days)
        self.assertEqual(st["alerts"], [])
        self.assertEqual(self.ledger_rows(), [])          # no price -> no 차계부 row

    def test_second_summer_is_a_new_set_and_keeps_the_old_one(self) -> None:
        old = self.summer()
        self.odo(9000)
        T.add_tread(self.conn, VID, {"set_id": old["id"], "date": "2026-10-30",
                                     "fl": 4.0, "fr": 4.0, "rl": 4.0, "rr": 4.0})
        new = T.create_set(self.conn, VID, {
            "kind": "summer", "brand": "한국타이어", "model": "iON evo", "size": "255/45R19",
            "purchase_date": "2026-11-01",
        })
        self.assertNotEqual(new["id"], old["id"])
        self.assertFalse(new["mounted"])
        listed = T.list_sets(self.conn, VID, today=TODAY)
        summers = [s for s in listed if s["kind"] == "summer"]
        self.assertEqual(len(summers), 2)
        old_row = next(s for s in summers if s["id"] == old["id"])
        self.assertEqual(old_row["km_total"], 9000)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM tire_treads WHERE set_id = ?", (old["id"],)).fetchone()[0], 1)
        T.mount_set(self.conn, VID, {"set_id": new["id"], "date": "2026-11-02", "odometer_km": 9000})
        again = {s["id"]: s for s in T.list_sets(self.conn, VID, current_odo=9500, today=TODAY)}
        self.assertTrue(again[new["id"]]["mounted"])
        self.assertFalse(again[old["id"]]["mounted"])
        self.assertEqual(again[old["id"]]["km_total"], 9000)
        self.assertEqual(again[new["id"]]["km_total"], 500)

    def test_swap_keeps_km_per_set(self) -> None:
        s = self.summer()
        w = T.create_set(self.conn, VID, {"kind": "winter", "price_krw": 1200000, "purchase_date": "2026-10-01"})
        self.assertFalse(w["mounted"])
        self.odo(9000)
        T.mount_set(self.conn, VID, {"set_id": w["id"], "date": "2026-11-20", "odometer_km": 9000, "price_krw": 40000})
        self.odo(12000)
        sets = self.sets()
        self.assertEqual((sets["summer"]["km_total"], sets["winter"]["km_total"]), (9000, 3000))
        self.assertTrue(sets["winter"]["mounted"])
        T.mount_set(self.conn, VID, {"set_id": s["id"], "date": "2027-03-20", "odometer_km": 12000})
        self.odo(12500)
        sets = self.sets()
        self.assertEqual((sets["summer"]["km_total"], sets["winter"]["km_total"]), (9500, 3000))
        self.assertEqual(self.ledger_rows(), [])   # tire prices stay off the 차계부

    def test_validation(self) -> None:
        s = self.summer()
        with self.assertRaises(ValueError):
            T.mount_set(self.conn, VID, {"set_id": s["id"], "date": "2026-10-31", "odometer_km": 8000})
        w = T.create_set(self.conn, VID, {"kind": "winter"})
        with self.assertRaises(ValueError):   # earlier than the current mount
            T.mount_set(self.conn, VID, {"set_id": w["id"], "date": "2026-08-01", "odometer_km": 0})
        for bad in ({"kind": "spring"}, {"dot": "25"}, {"dot": "2599"}, {"new_tread_mm": 80}, {"price_krw": -1}):
            with self.assertRaises(ValueError):
                T.create_set(self.conn, VID, bad)

    def test_price_edit_leaves_ledger_untouched(self) -> None:
        st = T.create_set(self.conn, VID, {"kind": "summer", "price_krw": 900000, "purchase_date": "2026-08-10"})
        self.assertEqual(self.ledger_rows(), [])
        T.update_set(self.conn, VID, {"id": st["id"], "price_krw": 950000})
        T.update_set(self.conn, VID, {"id": st["id"], "price_krw": ""})
        T.update_set(self.conn, VID, {"id": st["id"], "price_krw": 100})
        T.delete_set(self.conn, VID, st["id"])
        self.assertEqual(self.ledger_rows(), [])


class AlertTests(Base):
    def test_dot_age(self) -> None:
        self.assertEqual(T.parse_dot("DOT ABCD 1234 2518"), [date(2018, 6, 18)])
        self.assertEqual(len(T.parse_dot("2518, 3019")), 2)
        self.assertEqual(T.parse_dot("DOT 4B 2518 / 3019"), [date(2018, 6, 18), date(2019, 7, 22)])
        st = self.summer(dot="2518")
        shown = self.sets()["summer"]
        self.assertGreater(shown["age_years"], 6)
        self.assertEqual(shown["dot"], "2518")
        self.assertNotIn("age", [a["code"] for a in shown["alerts"]])
        self.assertNotIn("age", [c["code"] for c in shown["checks"]])
        T.update_set(self.conn, VID, {"id": st["id"], "dot": "2515"})
        kept = self.sets()["summer"]
        self.assertEqual(kept["dot"], "2515")
        self.assertGreater(kept["age_years"], 10)
        self.assertEqual(kept["alerts"], [])

    def test_tread_and_warranty(self) -> None:
        st = self.summer(warranty_km=8500)
        a = self.sets()["summer"]["alerts"]
        self.assertEqual([(x["code"], x["level"]) for x in a], [("warranty", "soon")])
        self.assertIn("약 94%", a[0]["text"])
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-10-30", "fl": 5, "fr": 2.8, "rl": 5, "rr": 5})
        a = {x["code"]: x for x in self.sets()["summer"]["alerts"]}
        self.assertEqual(a["tread"]["level"], "soon")
        self.assertIn("약 2.8 mm", a["tread"]["text"])
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-10-31", "fl": 1.5})
        self.assertEqual({x["code"]: x["level"] for x in self.sets()["summer"]["alerts"]}["tread"], "overdue")
        w = T.create_set(self.conn, VID, {"kind": "winter"})
        T.add_tread(self.conn, VID, {"set_id": w["id"], "date": "2026-10-31", "fl": 3.9, "fr": 4.5})
        self.assertIn("겨울용은 4 mm", self.sets()["winter"]["alerts"][0]["text"])
        with self.assertRaises(ValueError):
            T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-10-31"})

    def test_checks_simple_status_known_only(self) -> None:
        """: web rows are status words only, and unknown items are left out."""
        st = T.create_set(self.conn, VID, {"kind": "summer", "size": "255/45R19",
                                           "mount": {"date": "2026-08-19", "odometer_km": 0}})
        s1 = self.sets()["summer"]
        self.assertEqual(s1["checks"], [])
        self.assertEqual(s1["label"], "여름용")
        T.update_set(self.conn, VID, {"id": st["id"], "dot": "2518", "warranty_km": 50000})
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-10-31", "fl": 7, "fr": 7, "rl": 7, "rr": 7})
        c = {x["code"]: x["status"] for x in self.sets()["summer"]["checks"]}
        self.assertEqual(c, {"tread": "ok", "warranty": "ok"})
        self.assertNotIn("age", c)
        self.assertGreater(self.sets()["summer"]["age_years"], 6)
        self.assertTrue(all(set(x) == {"code", "name", "status"} for x in self.sets()["summer"]["checks"]))

    def test_check_tire_sets_once_per_level(self) -> None:
        st = self.summer()
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-10-31", "fl": 2.8, "fr": 5, "rl": 5, "rr": 5})
        m.check_tire_sets(self.conn, VID, None, NOW, self.sender)
        m.check_tire_sets(self.conn, VID, None, NOW, self.sender)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("홈 깊이", self.sent[0])
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-11-01", "fl": 1.5})
        m.check_tire_sets(self.conn, VID, None, NOW, self.sender)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1].startswith("타이어 교체 · "))
        T.add_tread(self.conn, VID, {"set_id": st["id"], "date": "2026-11-02", "fl": 6, "fr": 6, "rl": 6, "rr": 6})
        m.check_tire_sets(self.conn, VID, None, NOW, self.sender)
        self.assertEqual(len(self.sent), 2)                # clearing is quiet
        T.update_set(self.conn, VID, {"id": st["id"], "dot": "2514"})
        m.check_tire_sets(self.conn, VID, None, NOW, self.sender)
        self.assertEqual(len(self.sent), 2)                # production age is not its own alert
        self.assertIn(m.check_tire_sets, m.IDLE_CHECKS)
        self.assertIn(m.check_tire_sets, m.SIGNAL_CHECKS)
        for msg in self.sent:
            self.assertNotIn("—", msg)
            self.assertNotIn("생산", msg)


class RotationAndPressure(Base):
    def rot_item(self):
        return next(i for i in list_items(self.conn, VID, as_of=TODAY) if i["id"] == "cons-tire-rotation")

    def test_rotation_counts_current_set_km(self) -> None:
        self.assertNotIn("basis", self.rot_item())        # no set yet: car odometer as before
        s = self.summer()
        it = self.rot_item()
        self.assertEqual(it["basis"]["km_since"], 8000)
        self.assertFalse(it["basis"]["rotated"])
        self.assertEqual(it["remaining_km"], 2000)
        save_replacement(self.conn, VID, {"item_id": "cons-tire-rotation", "date": "2026-10-01",
                                          "odometer_km": 5000, "price_krw": 0, "action": "service"})
        it = self.rot_item()
        self.assertEqual((it["basis"]["km_since"], it["remaining_km"]), (3000, 7000))
        self.assertEqual(it["next_due_km"], 15000)
        w = T.create_set(self.conn, VID, {"kind": "winter"})
        T.mount_set(self.conn, VID, {"set_id": w["id"], "date": "2026-11-20", "odometer_km": 8000})
        self.odo(9500)
        it = self.rot_item()
        self.assertEqual((it["basis"]["km_since"], it["remaining_km"]), (1500, 8500))   # winter set only
        n_rep = self.conn.execute(
            "SELECT COUNT(*) FROM replacements WHERE vehicle_id = ? AND item_id = ?",
            (VID, "cons-tire-rotation")).fetchone()[0]
        n_led = len(self.ledger_rows())
        T.mount_set(self.conn, VID, {"set_id": s["id"], "date": "2027-03-20", "odometer_km": 9500})
        self.odo(10000)
        it = self.rot_item()
        self.assertEqual(it["basis"]["km_since"], 500)          # seasonal remount counts as rotation
        self.assertFalse(it["basis"]["rotated"])                # assumption, not a written service
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM replacements WHERE vehicle_id = ? AND item_id = ?",
            (VID, "cons-tire-rotation")).fetchone()[0], n_rep)
        self.assertEqual(len(self.ledger_rows()), n_led)
        row = self.conn.execute(
            "SELECT date, odometer_km, price_krw FROM replacements WHERE vehicle_id = ? AND item_id = ?",
            (VID, "cons-tire-rotation")).fetchone()
        self.assertEqual((row["date"], row["odometer_km"], row["price_krw"]), ("2026-10-01", 5000, 0))
        save_replacement(self.conn, VID, {"item_id": "cons-tire-rotation", "date": "2027-04-01",
                                          "odometer_km": 9800, "price_krw": 0, "action": "service"})
        later = self.rot_item()
        self.assertEqual(later["basis"]["km_since"], 200)       # a newer real record still wins
        self.assertTrue(later["basis"]["rotated"])

    def test_recommended_is_fixed_whatever_set(self) -> None:
        self.summer()
        w = T.create_set(self.conn, VID, {"kind": "winter"})
        T.mount_set(self.conn, VID, {"set_id": w["id"], "date": "2026-11-20", "odometer_km": 8000})
        self.assertEqual(get_tire_settings(self.conn, VID)["recommended_psi"], {"front": 42, "rear": 42})


class MigrationAndApi(Base):
    def test_old_season_records_become_sets(self) -> None:
        conn = self.conn
        with conn:
            conn.execute("DELETE FROM vehicle_settings WHERE key = ?", (T.MIGRATED_KEY,))
            conn.execute("UPDATE consumable_items SET active = 1 WHERE id = ?", (T.SEASON_ITEM,))
        save_replacement(conn, VID, {"item_id": T.SEASON_ITEM, "date": "2026-09-01", "odometer_km": 3000,
                                     "price_krw": 800000, "vendor": "T Station"})
        n_ledger = len(self.ledger_rows())
        self.assertEqual(T.migrate_season_item(conn, VID), 1)
        self.assertEqual(T.migrate_season_item(conn, VID), 0)
        st = T.list_sets(conn, VID, today=TODAY)
        self.assertEqual(len(st), 1)
        self.assertEqual((st[0]["km_total"], st[0]["price_krw"], st[0]["vendor"]), (5000, 800000, "T Station"))
        self.assertEqual(len(self.ledger_rows()), n_ledger)          # linked, not duplicated
        active = conn.execute("SELECT active FROM consumable_items WHERE id = ?", (T.SEASON_ITEM,)).fetchone()[0]
        self.assertEqual(active, 0)
        seed_consumables(conn)                                        # seed does not turn it back on
        self.assertEqual(conn.execute("SELECT active FROM consumable_items WHERE id = ?", (T.SEASON_ITEM,)).fetchone()[0], 0)

    def test_api(self) -> None:
        out, code = dispatch_write("POST", "/api/tire-sets", self.conn, VID,
                                   {"kind": "summer", "mount": {"date": "2026-08-19", "odometer_km": 0}})
        self.assertEqual((code, out["set"]["mounted"]), (201, True))
        sid = out["set"]["id"]
        out, _ = dispatch_write("POST", "/api/tire-sets", self.conn, VID, {"kind": "winter"})
        wid = out["set"]["id"]
        dispatch_write("POST", "/api/tire-sets", self.conn, VID, {"op": "mount", "set_id": wid, "date": "2026-11-20", "odometer_km": 8000})
        dispatch_write("POST", "/api/tire-sets", self.conn, VID, {"op": "tread", "set_id": sid, "date": "2026-11-20", "fl": 6})
        dispatch_write("PUT", "/api/tire-sets", self.conn, VID, {"id": sid, "brand": "미쉐린", "retired": True})
        dispatch_write("POST", "/api/tire-sets", self.conn, VID, {"op": "remove", "date": "2026-12-01", "odometer_km": 8000})
        st = {s["id"]: s for s in T.list_sets(self.conn, VID, today=TODAY)}
        self.assertEqual(st[sid]["retired"], 1)
        self.assertEqual(st[sid]["tread"]["min_mm"], 6)
        self.assertFalse(any(s["mounted"] for s in st.values()))
        dispatch_write("DELETE", "/api/tire-sets", self.conn, VID, {"id": wid})
        self.assertEqual(len(T.list_sets(self.conn, VID)), 1)
        with self.assertRaises(ValueError):
            dispatch_write("POST", "/api/tire-sets", self.conn, VID, {"op": "x"})
        with self.assertRaises(KeyError):
            dispatch_write("PUT", "/api/tire-sets", self.conn, VID, {"id": "nope"})


class UiTests(unittest.TestCase):
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_markers(self) -> None:
        for t in ('id="tireset-form"', 'id="mount-form"', 'id="tread-form"',
                  "지금 세트 만들기", '"/api/tire-sets"', 'it.id !== "cons-tires-season"', "지금 세트 기준",
                  'name="dot"', 'name="warranty_km"', 'name="new_tread_mm"', "tireset-overlay", "떼기만",
                  "트레드 깊이", "sheet-docs", "정비 주기", "정비 설명서",
                  "id=\"item-add-open\"", "aria-label=\"항목 추가\"", "aria-label='상세'",
                  "시즌 교체는 로테이션으로 계산합니다"):
            self.assertIn(t, self.html, t)
        self.assertNotIn("icon-btn", self.html)
        self.assertNotIn(">⋯<", self.html)
        self.assertNotIn("guide-toggle", self.html)
        self.assertNotIn("기록이 없으면 등록일", self.html)
        head = self.html[self.html.index('id="tab-consumables"'):self.html.index('id="consumables-list"')]
        self.assertIn('class="card-head"', head)
        self.assertIn('id="item-add-open"', head)
        self.assertIn('class="detail-btn"', head)
        self.assertNotIn("항목 추가</button>", head)
        expense = self.html[self.html.index('id="expense-open"') - 40:self.html.index('id="expense-open"') + 80]
        self.assertIn("detail-btn", expense)
        i = self.html.index("function renderTireSets")
        self.assertNotIn("—", self.html[i:self.html.index("function openTread", i)])


if __name__ == "__main__":
    unittest.main()
