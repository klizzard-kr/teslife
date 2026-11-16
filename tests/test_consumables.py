import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect as ingest_connect, store_signals
from ledger import connect as ledger_connect
from ledger.consumables import (
    AS_NEEDED_LABEL,
    SEED_ITEMS,
    SEED_SOURCE_NOTE,
    UNSET_LABEL,
    compute_item_status,
    delete_replacement,
    due_items,
    list_items,
    list_replacements,
    list_vendors,
    save_replacement,
    seed_consumables,
    update_item,
    update_replacement,
)
from process import process_db
from simulator import generate
from web import make_server, serve_in_thread


def _tmp_db(test: unittest.TestCase) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
    return tmp.name


def _ingest_day(test: unittest.TestCase, vehicle_id: str | None = None) -> str:
    db = _tmp_db(test)
    kwargs = {} if vehicle_id is None else {"vehicle_id": vehicle_id}
    conn = ingest_connect(db)
    try:
        store_signals(conn, generate("day", **kwargs))
    finally:
        conn.close()
    return db


AS_OF = date(2026, 10, 27)


class StatusMathTests(unittest.TestCase):
    def test_km_only_ok_soon_overdue(self) -> None:
        ok = compute_item_status(
            rule="km_only",
            interval_km=10000,
            interval_months=None,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=100,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["next_due_km"], 10000)
        self.assertEqual(ok["remaining_km"], 9900)
        self.assertIsNone(ok["next_due_date"])

        soon = compute_item_status(
            rule="km_only",
            interval_km=10000,
            interval_months=12,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=9600,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(soon["status"], "soon")
        overdue = compute_item_status(
            rule="km_only",
            interval_km=1000,
            interval_months=None,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=1001,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(overdue["status"], "overdue")
        self.assertLess(overdue["remaining_km"], 0)

    def test_months_only_and_baseline_registration(self) -> None:
        # registration 2026-08-19 + 2 months = 2026-10-19; as_of 2026-10-27 overdue
        overdue = compute_item_status(
            rule="months_only",
            interval_km=None,
            interval_months=2,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=42,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(overdue["next_due_date"], "2026-10-19")
        self.assertEqual(overdue["remaining_days"], -8)
        self.assertEqual(overdue["status"], "overdue")
        self.assertIsNone(overdue["next_due_km"])

        soon = compute_item_status(
            rule="months_only",
            interval_km=None,
            interval_months=3,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=0,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(soon["next_due_date"], "2026-11-19")
        self.assertEqual(soon["remaining_days"], 23)
        self.assertEqual(soon["status"], "ok")
        soon2 = compute_item_status(
            rule="months_only",
            interval_km=None,
            interval_months=3,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=0,
            as_of=date(2026, 11, 10),
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(soon2["status"], "soon")

    def test_whichever_first(self) -> None:
        # km overdue even if months ok
        st = compute_item_status(
            rule="km_or_months_whichever_first",
            interval_km=100,
            interval_months=24,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=150,
            as_of=AS_OF,
            warn_before_km=20,
            warn_before_days=14,
        )
        self.assertEqual(st["status"], "overdue")
        # months overdue even if km ok
        st2 = compute_item_status(
            rule="km_or_months_whichever_first",
            interval_km=100000,
            interval_months=1,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=10,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(st2["status"], "overdue")
        self.assertEqual(st2["next_due_date"], "2026-09-19")

    def test_unset(self) -> None:
        st = compute_item_status(
            rule="km_or_months_whichever_first",
            interval_km=None,
            interval_months=None,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=42,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(st["status"], "unset")
        self.assertEqual(st["interval_label"], UNSET_LABEL)

    def test_as_needed_has_no_due(self) -> None:
        st = compute_item_status(
            rule="as_needed",
            interval_km=None,
            interval_months=None,
            last_date="2026-08-19",
            last_odo=0,
            current_odo=42,
            as_of=AS_OF,
            warn_before_km=500,
            warn_before_days=14,
        )
        self.assertEqual(st["status"], "as_needed")
        self.assertEqual(st["interval_label"], AS_NEEDED_LABEL)
        self.assertIsNone(st["next_due_km"])
        self.assertIsNone(st["next_due_date"])

    def test_as_needed_with_saved_interval_uses_that_interval(self) -> None:
        """A stored period is the check interval. No period is still 필요 시."""
        before = compute_item_status(
            rule="as_needed", interval_km=None, interval_months=6,
            last_date="2026-08-19", last_odo=0, current_odo=42, as_of=AS_OF,
            warn_before_km=500, warn_before_days=14,
        )
        self.assertEqual(before["status"], "ok")
        self.assertEqual(before["next_due_date"], "2027-02-19")
        late = compute_item_status(
            rule="as_needed", interval_km=None, interval_months=1,
            last_date="2026-08-19", last_odo=0, current_odo=42, as_of=AS_OF,
            warn_before_km=500, warn_before_days=14,
        )
        self.assertEqual(late["status"], "overdue")
        self.assertEqual(late["next_due_date"], "2026-09-19")


class SeedAndIsolationTests(unittest.TestCase):
    def test_seed_tesla_intervals_and_no_seed(self) -> None:
        db = _ingest_day(self)
        process_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM consumable_items WHERE vehicle_id = ?",
            ("VINTEST0000000001",),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        self.assertEqual({r["name"] for r in rows}, {s["name"] for s in SEED_ITEMS})
        self.assertEqual(by_id["cons-tire-rotation"]["interval_km"], 10000)
        self.assertEqual(by_id["cons-tire-rotation"]["rule"], "km_only")
        self.assertEqual(by_id["cons-cabin-filter"]["interval_months"], 24)
        self.assertEqual(by_id["cons-hepa-filter"]["interval_months"], 36)
        self.assertEqual(by_id["cons-hepa-filter"]["active"], 0)
        self.assertEqual(by_id["cons-brake-fluid"]["interval_months"], 48)
        self.assertEqual(by_id["cons-wiper"]["interval_months"], 12)
        self.assertEqual(by_id["cons-caliper"]["interval_km"], 20000)
        self.assertEqual(by_id["cons-caliper"]["interval_months"], 12)
        self.assertEqual(by_id["cons-washer"]["interval_months"], 1)
        self.assertEqual(by_id["cons-12v-battery"]["rule"], "as_needed")
        self.assertEqual(by_id["cons-alignment"]["name"], "휠 얼라인먼트")
        self.assertIn(SEED_SOURCE_NOTE, by_id["cons-cabin-filter"]["note"])
        conn.close()

        db2 = _ingest_day(self)
        process_db(db2, seed=False)
        conn = sqlite3.connect(db2)
        n = conn.execute("SELECT COUNT(*) FROM consumable_items").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

        conn = ledger_connect(db)
        seed_consumables(conn)
        seed_consumables(conn)
        n = conn.execute(
            "SELECT COUNT(*) FROM consumable_items WHERE vehicle_id = ?",
            ("VINTEST0000000001",),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, len(SEED_ITEMS))

    def test_seed_does_not_overwrite_user_intervals(self) -> None:
        db = _ingest_day(self)
        process_db(db)
        conn = ledger_connect(db)
        conn.execute(
            """
            UPDATE consumable_items
            SET interval_km = 777, interval_months = 9, rule = 'km_or_months_whichever_first'
            WHERE id = 'cons-tire-rotation'
            """
        )
        conn.execute(
            """
            UPDATE consumable_items
            SET interval_km = NULL, interval_months = NULL, rule = 'as_needed', note = 'user note'
            WHERE id = 'cons-wiper'
            """
        )
        conn.commit()
        seed_consumables(conn)
        tire = conn.execute(
            "SELECT interval_km, interval_months, rule FROM consumable_items WHERE id = 'cons-tire-rotation'"
        ).fetchone()
        self.assertEqual(tire["interval_km"], 777)
        self.assertEqual(tire["interval_months"], 9)
        self.assertEqual(tire["rule"], "km_or_months_whichever_first")
        wiper = conn.execute(
            "SELECT interval_km, interval_months, rule, note FROM consumable_items WHERE id = 'cons-wiper'"
        ).fetchone()
        self.assertEqual(wiper["interval_months"], 12)
        self.assertEqual(wiper["rule"], "months_only")
        self.assertEqual(wiper["note"], "user note")
        conn.close()

    def test_fresh_car_due_from_registration(self) -> None:
        db = _ingest_day(self)
        process_db(db)
        conn = ledger_connect(db)
        conn.execute(
            "UPDATE signals SET value = ? WHERE name = 'Odometer'",
            (json.dumps(42.0),),
        )
        conn.commit()
        items = {i["id"]: i for i in list_items(conn, "VINTEST0000000001", as_of=AS_OF)}
        tire = items["cons-tire-rotation"]
        self.assertEqual(tire["last_replaced_date"], "2026-08-19")
        self.assertEqual(tire["last_replaced_odometer_km"], 0)
        self.assertEqual(tire["next_due_km"], 10000)
        self.assertEqual(tire["remaining_km"], 9958)
        self.assertEqual(tire["status"], "ok")
        cabin = items["cons-cabin-filter"]
        self.assertEqual(cabin["next_due_date"], "2028-08-19")
        self.assertEqual(cabin["status"], "ok")
        washer = items["cons-washer"]
        self.assertEqual(washer["next_due_date"], "2026-09-19")
        self.assertEqual(washer["status"], "overdue")
        batt = items["cons-12v-battery"]
        self.assertEqual(batt["status"], "as_needed")
        self.assertIsNone(batt["next_due_date"])
        unset = [i for i in items.values() if i["status"] == "unset"]
        self.assertEqual(unset, [])
        due = due_items(conn, "VINTEST0000000001", as_of=AS_OF)
        self.assertTrue(any(d["id"] == "cons-washer" for d in due))
        self.assertFalse(any(d["status"] == "as_needed" for d in due))
        conn.close()

    def test_vehicle_ids_do_not_mix(self) -> None:
        db = _tmp_db(self)
        conn = ingest_connect(db)
        try:
            store_signals(conn, generate("day", vehicle_id="VINAAA"))
            store_signals(conn, generate("dc_day", vehicle_id="VINBBB"))
        finally:
            conn.close()
        process_db(db)
        wconn = ledger_connect(db)
        save_replacement(
            wconn,
            "VINAAA",
            {
                "item_id": "cons-wiper",
                "date": "2026-10-01",
                "odometer_km": 10,
                "price_krw": 8000,
                "vendor": "쿠팡",
            },
        )
        a_rep = wconn.execute(
            "SELECT vehicle_id FROM replacements WHERE vehicle_id = 'VINAAA'"
        ).fetchall()
        b_rep = wconn.execute(
            "SELECT vehicle_id FROM replacements WHERE vehicle_id = 'VINBBB'"
        ).fetchall()
        a_items = list_items(wconn, "VINAAA")
        b_items = list_items(wconn, "VINBBB")
        wconn.close()
        self.assertEqual(len(a_rep), 1)
        self.assertEqual(len(b_rep), 0)
        self.assertTrue(all(i["vehicle_id"] == "VINAAA" for i in a_items))
        self.assertTrue(all(i["vehicle_id"] == "VINBBB" for i in b_items))


class ReplacementTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _ingest_day(self)
        process_db(self.db)
        self.conn = ledger_connect(self.db)

    def tearDown(self) -> None:
        self.conn.close()

    def test_three_in_one_and_tire_category(self) -> None:
        rep = save_replacement(
            self.conn,
            "VINTEST0000000001",
            {
                "item_id": "cons-tires-season",
                "date": "2026-10-20",
                "odometer_km": 40,
                "price_krw": 100000,
                "quantity": 4,
                "vendor": "T Station 부천소사점",
                "memo": "윈터",
            },
        )
        led = self.conn.execute(
            "SELECT * FROM ledger WHERE id = ?", (rep["ledger_id"],)
        ).fetchone()
        self.assertEqual(led["category"], "tires")
        self.assertEqual(led["amount_krw"], 400000)
        self.assertEqual(led["source"], "manual")
        self.assertIn("타이어", led["memo"])
        self.assertIn("T Station", led["memo"])
        vendors = list_vendors(self.conn, "VINTEST0000000001")
        self.assertEqual(vendors[0]["name"], "T Station 부천소사점")
        items = {i["id"]: i for i in list_items(self.conn, "VINTEST0000000001")}
        self.assertEqual(items["cons-tires-season"]["last_replaced_date"], "2026-10-20")
        self.assertEqual(items["cons-tires-season"]["last_replaced_odometer_km"], 40)

        maint = save_replacement(
            self.conn,
            "VINTEST0000000001",
            {
                "item_id": "cons-cabin-filter",
                "date": "2026-10-21",
                "odometer_km": 41,
                "price_krw": 15000,
                "total_krw": 16000,
                "vendor": "쿠팡",
            },
        )
        led2 = self.conn.execute(
            "SELECT amount_krw, category FROM ledger WHERE id = ?",
            (maint["ledger_id"],),
        ).fetchone()
        self.assertEqual(led2["amount_krw"], 16000)
        self.assertEqual(led2["category"], "maintenance")

    def test_rollback_if_ledger_step_fails(self) -> None:
        before_r = self.conn.execute("SELECT COUNT(*) FROM replacements").fetchone()[0]
        before_l = self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        with mock.patch(
            "ledger.consumables._ensure_vendor",
            side_effect=sqlite3.IntegrityError("boom"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                save_replacement(
                    self.conn,
                    "VINTEST0000000001",
                    {
                        "item_id": "cons-wiper",
                        "date": "2026-10-20",
                        "odometer_km": 10,
                        "price_krw": 1,
                        "vendor": "쿠팡",
                    },
                )
        after_r = self.conn.execute("SELECT COUNT(*) FROM replacements").fetchone()[0]
        after_l = self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.assertEqual(before_r, after_r)
        self.assertEqual(before_l, after_l)

    def test_edit_and_delete_keep_ledger_in_sync(self) -> None:
        rep = save_replacement(
            self.conn,
            "VINTEST0000000001",
            {
                "item_id": "cons-wiper",
                "date": "2026-10-20",
                "odometer_km": 12,
                "price_krw": 9000,
                "vendor": "현대모비스",
            },
        )
        updated = update_replacement(
            self.conn,
            "VINTEST0000000001",
            {"id": rep["id"], "price_krw": 11000, "date": "2026-10-22"},
        )
        led = self.conn.execute(
            "SELECT amount_krw, date FROM ledger WHERE id = ?", (updated["ledger_id"],)
        ).fetchone()
        self.assertEqual(led["amount_krw"], 11000)
        self.assertEqual(led["date"], "2026-10-22")
        lid = updated["ledger_id"]
        delete_replacement(self.conn, "VINTEST0000000001", updated["id"])
        gone_r = self.conn.execute(
            "SELECT 1 FROM replacements WHERE id = ?", (updated["id"],)
        ).fetchone()
        gone_l = self.conn.execute(
            "SELECT 1 FROM ledger WHERE id = ?", (lid,)
        ).fetchone()
        self.assertIsNone(gone_r)
        self.assertIsNone(gone_l)

    def test_interval_edit_recalculates_status(self) -> None:
        item = update_item(
            self.conn,
            "VINTEST0000000001",
            {
                "id": "cons-brake-fluid",
                "interval_km": 50,
                "rule": "km_only",
                "warn_before_km": 10,
            },
        )
        self.assertIn(item["status"], ("ok", "soon", "overdue"))
        self.assertNotEqual(item["status"], "unset")
        due = due_items(self.conn, "VINTEST0000000001")
        ids = {d["id"] for d in due}
        if item["status"] in ("soon", "overdue"):
            self.assertIn("cons-brake-fluid", ids)


class ConsumableApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _ingest_day(self)
        process_db(self.db)
        self.server = make_server(
            self.db, host="127.0.0.1", port=0, process_interval=0
        )
        serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def _get(self, path: str) -> tuple[int, object]:
        url = f"{self.origin}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _write(self, method: str, path: str, body: dict, origin: str | None = None, ctype: str = "application/json"):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": ctype}
        headers["Origin"] = self.origin if origin is None else origin
        req = urllib.request.Request(
            f"{self.origin}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"raw": payload}
            return exc.code, parsed

    def test_due_endpoint_and_crud(self) -> None:
        st, items = self._get("/api/consumables")
        self.assertEqual(st, 200)
        self.assertEqual(len(items["items"]), len(SEED_ITEMS))
        by_id = {i["id"]: i for i in items["items"]}
        self.assertEqual(by_id["cons-tire-rotation"]["interval_km"], 10000)
        self.assertEqual(by_id["cons-12v-battery"]["status"], "as_needed")
        self.assertEqual(by_id["cons-12v-battery"]["interval_label"], AS_NEEDED_LABEL)
        unset = {i["name"] for i in items["items"] if i["status"] == "unset"}
        self.assertEqual(unset, set())

        self._write(
            "PUT",
            "/api/consumables",
            {
                "id": "cons-cabin-filter",
                "interval_km": 1,
                "rule": "km_only",
                "warn_before_km": 1000,
            },
        )
        _, due = self._get("/api/due")
        names = [i["name"] for i in due["items"]]
        self.assertIn("에어컨 필터(캐빈)", names)
        self.assertTrue(all(i["status"] in ("soon", "overdue") for i in due["items"]))

        code, created = self._write(
            "POST",
            "/api/replacements",
            {
                "item_id": "cons-cabin-filter",
                "date": "2026-10-26",
                "odometer_km": 42,
                "price_krw": 20000,
                "vendor": "쿠팡",
            },
        )
        self.assertEqual(code, 201)
        rid = created["replacement"]["id"]
        _, hist = self._get("/api/replacements?item_id=cons-cabin-filter")
        self.assertEqual(hist["total_krw"], 20000)
        self.assertEqual(hist["by_vendor"][0]["vendor"], "쿠팡")
        _, vendors = self._get("/api/vendors")
        self.assertEqual(vendors["vendors"][0]["name"], "쿠팡")

        self._write("PUT", "/api/replacements", {"id": rid, "price_krw": 21000})
        _, ledger = self._get("/api/ledger?category=maintenance")
        amounts = [e["amount_krw"] for e in ledger["entries"] if e.get("id") == created["replacement"]["ledger_id"]]
        self.assertEqual(amounts, [21000])

        st, added = self._write("POST", "/api/consumables", {"name": "실내 방향제", "rule": "months_only"})
        self.assertEqual(st, 201)
        self._write("PUT", "/api/consumables", {"id": added["item"]["id"], "name": "방향제"})
        self._write("DELETE", "/api/consumables", {"id": added["item"]["id"]})
        _, after = self._get("/api/consumables")
        found = [i for i in after["items"] if i["id"] == added["item"]["id"]][0]
        self.assertFalse(found["active"])
        self.assertEqual(found["name"], "방향제")

        self._write("DELETE", "/api/replacements", {"id": rid})
        _, hist2 = self._get("/api/replacements?item_id=cons-cabin-filter")
        self.assertEqual(hist2["replacements"], [])

    def test_validation_and_origin(self) -> None:
        code, _ = self._write(
            "POST",
            "/api/replacements",
            {"item_id": "cons-wiper", "date": "2026-10-01", "odometer_km": 1},
        )
        self.assertEqual(code, 400)
        code, _ = self._write(
            "POST",
            "/api/replacements",
            {
                "item_id": "cons-wiper",
                "date": "2026-13-01",
                "odometer_km": 1,
                "price_krw": 1,
            },
        )
        self.assertEqual(code, 400)
        code, _ = self._write(
            "POST",
            "/api/consumables",
            {"name": "x", "rule": "whenever"},
        )
        self.assertEqual(code, 400)
        code, _ = self._write(
            "POST",
            "/api/replacements",
            {
                "item_id": "cons-wiper",
                "date": "2026-10-01",
                "odometer_km": 1,
                "price_krw": 1,
            },
            origin="http://evil.example",
        )
        self.assertEqual(code, 403)
        code, _ = self._write(
            "POST",
            "/api/consumables",
            {"name": "foo"},
            origin="http://evil.example",
        )
        self.assertEqual(code, 403)
        code, _ = self._write(
            "POST",
            "/api/replacements",
            {
                "item_id": "cons-wiper",
                "date": "2026-10-01",
                "odometer_km": 1,
                "price_krw": 1,
            },
            ctype="text/plain",
        )
        self.assertEqual(code, 415)

    def test_index_has_consumable_labels(self) -> None:
        with urllib.request.urlopen(f"{self.origin}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("교체 완료", html)
        self.assertIn("<h2>정비</h2>", html)


if __name__ == "__main__":
    unittest.main()
