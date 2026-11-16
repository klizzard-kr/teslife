"""Charging unit price = paid amount / billed kWh; saved prices only estimate (marked)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledger import (
    charging_cost, charging_stats, connect as ledger_connect, csv_export, ensure_schema as ledger_schema, cost_per_km, list_ledger,
    create_station, ownership_total, sync_auto_charges, upsert_charge_meta,
)
from process import ensure_process_schema
from web import list_charges

VID = "VINPRICE"
CID = "2026-10-16T12:14:00Z"


class ChargePriceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "signals.db"
        conn = sqlite3.connect(db)
        ensure_process_schema(conn)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 24.0)",
            (VID, CID, "2026-10-16T16:14:00Z"),
        )
        conn.commit()
        conn.close()
        self.conn = ledger_connect(db)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _auto(self) -> dict:
        return [e for e in list_ledger(self.conn, VID) if e["source"] == "auto"][0]

    def test_unit_is_amount_over_billed_kwh(self) -> None:
        m = upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080,
                                                "unit_price_krw_per_kwh": 999})   # typed unit ignored
        self.assertAlmostEqual(m["unit_price_krw_per_kwh"], 6080 / 24.59, places=6)
        self.assertEqual(m["total_krw"], 6080)
        self.assertEqual(self._auto()["amount_krw"], 6080)
        self.assertFalse(self._auto()["estimated"])

    def test_no_amount_no_unit(self) -> None:
        m = upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59})
        self.assertIsNone(m["unit_price_krw_per_kwh"])
        self.assertIsNone(self._auto()["amount_krw"])
        self.assertEqual(self._auto()["amount_display"], "금액 미입력")

    def test_operator_rate_estimate_only(self) -> None:
        # 장소 (stations) carry no price any more: a saved place price is ignored
        self.conn.execute("INSERT INTO rates (vehicle_id, operator, unit_price_krw_per_kwh) VALUES (?, 'op', 300)", (VID,))
        self.conn.execute("INSERT INTO stations (vehicle_id, id, name, operator, default_unit_price_krw_per_kwh) "
                          "VALUES (?, 's1', '집', 'op', 250)", (VID,))
        self.conn.commit()
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "operator": "op", "location_text": "집", "kwh_billed": 20})
        self.assertEqual(charging_cost(self.conn, VID, 24.0, {"station_name": "집", "operator": "op", "kwh_billed": 20}),
                         (6000.0, True))
        self.assertEqual(self._auto()["amount_krw"], 6000.0)
        self.assertTrue(self._auto()["estimated"])
        # no billed kWh -> operator rate x car kWh
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "operator": "op", "location_text": "회사"})
        self.assertAlmostEqual(self._auto()["amount_krw"], 300 * 24.0)
        self.assertTrue(self._auto()["estimated"])
        self.assertEqual(cost_per_km(self.conn, VID)["estimated_krw"], 300 * 24.0)
        self.assertEqual(ownership_total(self.conn, VID)["estimated_krw"], 300 * 24.0)
        stats = charging_stats(self.conn, VID)
        self.assertIsNone(stats["average_unit_price"])           # estimates are not prices paid
        self.assertIsNone(stats["cheapest"])
        # unknown operator -> nothing to estimate
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "operator": "other"})
        self.assertIsNone(self._auto()["amount_krw"])

    def test_existing_rows_kept(self) -> None:
        # rows saved before this change (unit = total / kWh already) stay as they are
        self.conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, unit_price_krw_per_kwh, total_krw, kwh_billed) "
            "VALUES (?, ?, 247.254981699878, 6080, 24.59)", (VID, CID))
        self.conn.commit()
        sync_auto_charges(self.conn, VID)
        self.assertEqual(self._auto()["amount_krw"], 6080)
        row = self.conn.execute("SELECT unit_price_krw_per_kwh FROM charge_meta").fetchone()
        self.assertAlmostEqual(row[0], 247.254981699878)
        self.assertAlmostEqual(charging_stats(self.conn, VID)["average_unit_price"], 6080 / 24.59, places=6)

    def test_api_charge_cost_fields(self) -> None:
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080})
        item = list_charges(self.conn, VID)[0]
        self.assertEqual(item["cost_krw"], 6080)
        self.assertFalse(item["cost_estimated"])
        self.assertAlmostEqual(item["unit_krw_per_kwh"], 247.25, places=2)

    def test_discount_counts_in_unit_not_in_ledger(self) -> None:
        """: unit price = (paid + discount) / billed kWh; the ledger keeps the cash paid."""
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080, "discount_krw": 1100})
        m = self.conn.execute("SELECT * FROM charge_meta WHERE charge_id = ?", (CID,)).fetchone()
        self.assertAlmostEqual(m["unit_price_krw_per_kwh"], 7180 / 24.59, places=6)
        self.assertEqual(m["discount_krw"], 1100)
        self.assertEqual(self._auto()["amount_krw"], 6080)
        self.assertAlmostEqual(charging_stats(self.conn, VID)["average_unit_price"], 7180 / 24.59, places=6)
        # a caller without the key keeps the discount; the form sends null to clear them
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080})
        self.assertEqual(self.conn.execute("SELECT discount_krw FROM charge_meta WHERE charge_id = ?", (CID,)).fetchone()[0], 1100)
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080, "discount_krw": None})
        m = self.conn.execute("SELECT * FROM charge_meta WHERE charge_id = ?", (CID,)).fetchone()
        self.assertIsNone(m["discount_krw"])
        self.assertAlmostEqual(m["unit_price_krw_per_kwh"], 6080 / 24.59, places=6)
        item = [c for c in list_charges(self.conn, VID) if c["charge_id"] == CID][0]
        self.assertAlmostEqual(item["unit_krw_per_kwh"], 6080 / 24.59, places=6)

    def test_discount_in_csv_and_operator_total_is_paid(self) -> None:
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "kwh_billed": 24.59, "total_krw": 6080,
                                            "discount_krw": 1100, "operator": "환경부"})
        text = csv_export(self.conn, VID).decode("utf-8-sig").splitlines()
        self.assertEqual(text[0], "차량,ID,날짜,분류,금액,할인,주행거리,메모,출처")
        row = [l for l in text if "auto-charge:" in l][0].split(",")
        self.assertEqual((float(row[4]), float(row[5])), (6080, 1100))
        op = charging_stats(self.conn, VID)["by_operator"][0]
        self.assertEqual(op["total_krw"], 6080)
        self.assertAlmostEqual(op["krw_per_kwh"], 7180 / 24.59, places=6)

    def test_migration_adds_or_renames_column(self) -> None:
        """Old DBs get discount_krw (NULL); the short-lived points_krw column is renamed in place."""
        for old in ("", ", points_krw REAL"):
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE charge_meta (vehicle_id TEXT NOT NULL, charge_id TEXT NOT NULL, operator TEXT, "
                         "station_name TEXT, location_text TEXT, unit_price_krw_per_kwh REAL, total_krw REAL, "
                         "kwh_billed REAL" + old + ", PRIMARY KEY (vehicle_id, charge_id))")
            conn.execute("INSERT INTO charge_meta (vehicle_id, charge_id, total_krw) VALUES ('V', 'c', 100)")
            ledger_schema(conn)
            ledger_schema(conn)  # idempotent
            cols = [r[1] for r in conn.execute("PRAGMA table_info(charge_meta)")]
            self.assertIn("discount_krw", cols)
            self.assertNotIn("points_krw", cols)
            self.assertEqual(conn.execute("SELECT total_krw, discount_krw FROM charge_meta").fetchone(), (100, None))
            conn.close()


class ChargePriceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_sheet_has_no_unit_input(self) -> None:
        sheet = self.html[self.html.index('id="charge-meta-form"'):self.html.index("</form>", self.html.index('id="charge-meta-form"'))]
        self.assertNotIn('name="unit_price_krw_per_kwh"', sheet)
        self.assertIn('name="kwh_billed"', sheet)
        self.assertIn('name="total_krw"', sheet)
        self.assertIn('<output name="unit_calc">', sheet)
        self.assertIn('<label>할인·포인트 (원) <input name="discount_krw" type="number"', sheet)
        self.assertNotIn("points_krw", self.html)

    def test_discount_hidden_when_empty(self) -> None:
        self.assertIn('if (known(m.discount_krw) && Number(m.discount_krw) > 0) add("할인", fmtKrw(m.discount_krw));', self.html)
        self.assertIn("unitPrice(form.total_krw.value, form.kwh_billed.value, form.discount_krw.value)", self.html)

    def test_estimates_marked(self) -> None:
        self.assertIn('add("금액", "약 " + fmtKrw(c.cost_krw));', self.html)
        self.assertIn('(row.estimated ? "약 " : "") + fmtKrw(row.amount_krw)', self.html)
        self.assertNotIn('" (추정)"', self.html)
        self.assertNotIn("form.unit_price_krw_per_kwh.value = st.default_unit_price_krw_per_kwh", self.html)


class ChargePlaceTests(unittest.TestCase):
    """장소 = location_text only; station_name stays in the DB and is shown as a fallback."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    setUp = ChargePriceTests.setUp
    tearDown = ChargePriceTests.tearDown

    def test_station_name_kept_when_not_sent(self) -> None:
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "operator": "op", "station_name": "한빛", "total_krw": 100,
                                            "kwh_billed": 1})
        m = upsert_charge_meta(self.conn, VID, {"charge_id": CID, "operator": "op", "location_text": "한빛 2층",
                                                "total_krw": 100, "kwh_billed": 1})
        self.assertEqual(m["station_name"], "한빛")
        self.assertEqual(m["location_text"], "한빛 2층")
        stats = charging_stats(self.conn, VID)
        self.assertEqual(stats["cheapest"]["place"], "한빛 2층")
        # explicit key still clears it (API callers)
        m = upsert_charge_meta(self.conn, VID, {"charge_id": CID, "station_name": None})
        self.assertIsNone(m["station_name"])

    def test_place_falls_back_to_station_name(self) -> None:
        upsert_charge_meta(self.conn, VID, {"charge_id": CID, "station_name": "북측", "total_krw": 100, "kwh_billed": 1})
        self.assertEqual(charging_stats(self.conn, VID)["cheapest"]["place"], "북측")
        self.assertNotIn("form.location_text.value = savedPlace || stationPlace(picked)", self.html)
        self.assertIn("form.location_text.value = String(savedPlace).trim();", self.html)

    def test_saved_place_named_by_text(self) -> None:
        st = create_station(self.conn, VID, {"address_text": "집 차고", "operator": "home"})
        self.assertEqual((st["name"], st["address_text"], st["operator"]), ("집 차고", "집 차고", "home"))
        with self.assertRaises(ValueError):
            create_station(self.conn, VID, {"operator": "home"})

    def test_sheet_and_manage_labels(self) -> None:
        sheet = self.html[self.html.index('id="charge-meta-form"'):self.html.index("</form>", self.html.index('id="charge-meta-form"'))]
        self.assertNotIn('name="station_name"', sheet)
        self.assertIn('name="station_id"', sheet)
        self.assertIn("충전소", sheet)
        self.assertIn('name="location_text"', sheet)
        self.assertIn('id="place-clear"', sheet)
        self.assertLess(sheet.index('name="location_text"'), sheet.index('name="operator"'))
        self.assertLess(sheet.index('name="operator"'), sheet.index('name="pay_method"'))
        self.assertLess(sheet.index('name="pay_method"'), sheet.index('name="kwh_billed"'))
        # : the 단가·장소 관리 section and sheet are gone (APIs stay)
        for gone in ("단가·장소 관리", "단가·충전소 관리", 'id="manage-overlay"', 'id="manage-open"',
                     'id="rate-form"', 'id="station-form"', 'id="rates-list"', 'id="stations-list"'):
            self.assertNotIn(gone, self.html)
        # saved place fills 장소 and its default operator
        self.assertNotIn("form.location_text.value = stationPlace(st);", self.html)
        self.assertIn("if (st.operator) form.operator.value = st.operator;", self.html)

    def test_card_pairs_operator_with_amount(self) -> None:
        self.assertIn('add("사업자", known(m.operator) ? escHtml(m.operator) : null);', self.html)
        self.assertIn('add("장소", place ? escHtml(place) : null);', self.html)
        h = self.html
        self.assertLess(h.index('add("사업자"'), h.index('add("청구"'))
        self.assertLess(h.index('add("금액", fmtKrw(m.total_krw))'), h.index('add("단가"'))


class ChargeNoteRemovalTests(unittest.TestCase):
    """The charge memo is gone from the UI and code; the column goes only via the migration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        conn = sqlite3.connect(self.db)
        ensure_process_schema(conn)
        # an older database: charge_meta still has `note`
        conn.execute("CREATE TABLE charge_meta (vehicle_id TEXT NOT NULL, charge_id TEXT NOT NULL, operator TEXT, "
                     "station_name TEXT, location_text TEXT, unit_price_krw_per_kwh REAL, total_krw REAL, "
                     "kwh_billed REAL, note TEXT, PRIMARY KEY (vehicle_id, charge_id))")
        conn.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 24.0)",
                     (VID, CID, "2026-10-16T16:14:00Z"))
        conn.execute("INSERT INTO charge_meta VALUES (?, ?, 'op', NULL, '집', 250, 6000, 24, 'old memo')", (VID, CID))
        conn.execute("INSERT INTO charge_meta VALUES (?, 'x', 'op', NULL, NULL, NULL, NULL, NULL, '  ')", (VID,))
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cols(self) -> set:
        conn = sqlite3.connect(self.db)
        try:
            return {r[1] for r in conn.execute("PRAGMA table_info(charge_meta)")}
        finally:
            conn.close()

    def test_app_works_before_migration_and_keeps_column(self) -> None:
        conn = ledger_connect(self.db)                       # ensure_schema must not drop it
        m = upsert_charge_meta(conn, VID, {"charge_id": CID, "operator": "op", "location_text": "집",
                                           "total_krw": 6000, "kwh_billed": 24, "note": "ignored"})
        self.assertEqual(m["note"], "old memo")              # not written, not cleared
        conn.close()
        self.assertIn("note", self._cols())

    def test_migration_backs_up_then_drops(self) -> None:
        from ledger.drop_charge_note import drop_charge_note
        backup = Path(self.tmp.name) / "signals.db.bak"
        out = drop_charge_note(self.db, backup)
        self.assertEqual(out, {"dropped": True, "rows": 2, "with_note": 1, "backup": str(backup)})
        self.assertNotIn("note", self._cols())
        b = sqlite3.connect(backup)
        self.assertEqual(b.execute("SELECT note FROM charge_meta WHERE charge_id = ?", (CID,)).fetchone()[0], "old memo")
        b.close()
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        conn = ledger_connect(self.db)
        m = upsert_charge_meta(conn, VID, {"charge_id": CID, "total_krw": 6000, "kwh_billed": 24})
        self.assertNotIn("note", m)
        self.assertEqual(list_charges(conn, VID)[0]["meta"]["location_text"], None)
        conn.close()
        # second run is a no-op; an existing backup path is refused
        self.assertFalse(drop_charge_note(self.db, Path(self.tmp.name) / "again.bak")["dropped"])
        with self.assertRaises(FileExistsError):
            drop_charge_note(self.db, backup)

    def test_new_schema_and_ui_have_no_note(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_process_schema(conn)
        from ledger import ensure_schema
        ensure_schema(conn)
        self.assertNotIn("note", {r[1] for r in conn.execute("PRAGMA table_info(charge_meta)")})
        conn.close()
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        sheet = html[html.index('id="charge-meta-form"'):html.index("</form>", html.index('id="charge-meta-form"'))]
        self.assertNotIn('name="note"', sheet)
        self.assertNotIn("메모", sheet)
        self.assertNotIn("m.note", html)


if __name__ == "__main__":
    unittest.main()
