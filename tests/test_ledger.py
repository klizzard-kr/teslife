import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect as ingest_connect, store_signals
from ledger import (
    CSV_HEADERS,
    INSURANCE_AMOUNT,
    INSURANCE_MEMO,
    compute_unit_and_total,
    connect as ledger_connect,
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


class ComputeMetaTests(unittest.TestCase):
    def test_typed_unit_is_ignored(self) -> None:
        # the unit price is never typed; without a paid amount there is none
        unit, total, kwh = compute_unit_and_total(350.0, None, 20.0)
        self.assertIsNone(unit)
        self.assertIsNone(total)
        self.assertEqual(kwh, 20.0)

    def test_total_and_kwh_fill_unit(self) -> None:
        unit, total, kwh = compute_unit_and_total(None, 8000.0, 20.0)
        self.assertEqual(unit, 400.0)
        self.assertEqual(total, 8000.0)
        self.assertEqual(kwh, 20.0)


class AutoLedgerTests(unittest.TestCase):
    def test_auto_entry_null_until_rate_or_meta(self) -> None:
        db = _ingest_day(self)
        process_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        auto = conn.execute(
            "SELECT * FROM ledger WHERE source = 'auto' AND category = 'charging'"
        ).fetchall()
        self.assertEqual(len(auto), 1)
        self.assertIsNone(auto[0]["amount_krw"])
        self.assertEqual(auto[0]["memo"], "자동 기록 충전")
        self.assertTrue(str(auto[0]["id"]).startswith("auto-charge:"))
        self.assertGreater(auto[0]["odometer_km"], 0)
        insurance = conn.execute(
            "SELECT * FROM ledger WHERE id = 'seed-kb-direct-2026'"
        ).fetchone()
        self.assertEqual(insurance["amount_krw"], INSURANCE_AMOUNT)
        self.assertEqual(insurance["memo"], INSURANCE_MEMO)
        conn.close()

        process_db(db)
        conn = sqlite3.connect(db)
        n_auto = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE source = 'auto'"
        ).fetchone()[0]
        n_ins = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE id = 'seed-kb-direct-2026'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n_auto, 1)
        self.assertEqual(n_ins, 1)

    def test_recalc_after_rate_and_meta(self) -> None:
        db = _ingest_day(self)
        process_db(db)
        conn = ledger_connect(db)
        charge = conn.execute("SELECT start_ts, kwh_added FROM charges").fetchone()
        cid = charge["start_ts"]
        conn.execute(
            "INSERT INTO rates (vehicle_id, operator, unit_price_krw_per_kwh) VALUES (?, ?, ?)",
            ("VINTEST0000000001", "home", 100.0),
        )
        conn.commit()
        from ledger import sync_auto_charges, upsert_charge_meta

        sync_auto_charges(conn, "VINTEST0000000001")
        row = conn.execute(
            "SELECT amount_krw FROM ledger WHERE source = 'auto'"
        ).fetchone()
        self.assertIsNone(row["amount_krw"])

        upsert_charge_meta(
            conn,
            "VINTEST0000000001",
            {"charge_id": cid, "operator": "home", "kwh_billed": 20.0},
        )
        row = conn.execute(
            "SELECT amount_krw FROM ledger WHERE source = 'auto'"
        ).fetchone()
        self.assertAlmostEqual(row["amount_krw"], 2000.0, places=3)

        upsert_charge_meta(
            conn,
            "VINTEST0000000001",
            {"charge_id": cid, "operator": "home", "total_krw": 5555, "kwh_billed": 20},
        )
        row = conn.execute(
            "SELECT amount_krw FROM ledger WHERE source = 'auto'"
        ).fetchone()
        self.assertAlmostEqual(row["amount_krw"], 5555.0, places=3)
        conn.close()

    def test_no_seed(self) -> None:
        db = _ingest_day(self)
        process_db(db, seed=False)
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE id = 'seed-kb-direct-2026'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_vehicle_ids_do_not_mix(self) -> None:
        db = _tmp_db(self)
        conn = ingest_connect(db)
        try:
            store_signals(conn, generate("day", vehicle_id="VINAAA"))
            store_signals(conn, generate("dc_day", vehicle_id="VINBBB"))
        finally:
            conn.close()
        process_db(db)
        conn = sqlite3.connect(db)
        a = conn.execute(
            "SELECT * FROM ledger WHERE vehicle_id = 'VINAAA' AND source = 'auto'"
        ).fetchall()
        b = conn.execute(
            "SELECT * FROM ledger WHERE vehicle_id = 'VINBBB' AND source = 'auto'"
        ).fetchall()
        ins = conn.execute(
            "SELECT vehicle_id FROM ledger WHERE id = 'seed-kb-direct-2026' ORDER BY vehicle_id"
        ).fetchall()
        conn.close()
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual([r[0] for r in ins], ["VINAAA", "VINBBB"])


class LedgerApiTests(unittest.TestCase):
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
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "csv" in ctype:
                return resp.status, raw
            return resp.status, json.loads(raw.decode("utf-8"))

    def _write(self, method: str, path: str, body: dict, origin: str | None = None, ctype: str = "application/json"):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": ctype}
        if origin is not None:
            headers["Origin"] = origin
        else:
            headers["Origin"] = self.origin
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

    def test_manual_crud_and_reports(self) -> None:
        status, created = self._write(
            "POST",
            "/api/ledger",
            {
                "date": "2026-10-20",
                "category": "wash",
                "amount_krw": 15000,
                "memo": "세차",
            },
        )
        self.assertEqual(status, 201)
        lid = created["entry"]["id"]
        _, listing = self._get("/api/ledger")
        ids = [e["id"] for e in listing["entries"]]
        self.assertIn(lid, ids)
        self.assertIn("seed-kb-direct-2026", ids)
        auto = [e for e in listing["entries"] if e["source"] == "auto"][0]
        self.assertEqual(auto["amount_display"], "금액 미입력")

        status, updated = self._write(
            "PUT",
            "/api/ledger",
            {"id": lid, "date": "2026-10-21", "category": "wash", "amount_krw": 16000},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["entry"]["amount_krw"], 16000)

        _, monthly = self._get("/api/ledger/monthly")
        months = {i["month"]: i for i in monthly["items"]}
        self.assertEqual(months["2026-08"]["by_category"]["insurance"], INSURANCE_AMOUNT)
        self.assertEqual(months["2026-10"]["by_category"]["wash"], 16000)

        _, yearly = self._get("/api/ledger/yearly")
        self.assertEqual(yearly["items"][0]["year"], "2026")

        _, cpk = self._get("/api/ledger/cost-per-km?month=2026-10")
        self.assertAlmostEqual(cpk["km"], 42.0, places=3)
        expected = (16000) / 42.0
        # October ledger: wash + auto charging (null ignored) ; insurance is August
        self.assertAlmostEqual(cpk["krw_per_km"], expected, places=3)

        _, own = self._get("/api/ledger/ownership")
        self.assertEqual(own["since"], "2026-08-19")
        self.assertEqual(own["total_krw"], INSURANCE_AMOUNT + 16000)

        status, _ = self._write("DELETE", "/api/ledger", {"id": lid})
        self.assertEqual(status, 200)

    def test_validation_and_origin(self) -> None:
        code, body = self._write(
            "POST",
            "/api/ledger",
            {"date": "2026-13-01", "category": "wash", "amount_krw": 1},
        )
        self.assertEqual(code, 400)
        code, body = self._write(
            "POST",
            "/api/ledger",
            {"date": "2026-10-01", "category": "spaceship", "amount_krw": 1},
        )
        self.assertEqual(code, 400)
        code, body = self._write(
            "POST",
            "/api/ledger",
            {"date": "2026-10-01", "category": "wash", "amount_krw": -3},
        )
        self.assertEqual(code, 400)
        code, body = self._write(
            "POST",
            "/api/ledger",
            {"date": "2026-10-01", "category": "wash", "amount_krw": 1},
            origin="http://evil.example",
        )
        self.assertEqual(code, 403)
        code, body = self._write(
            "POST",
            "/api/ledger",
            {"date": "2026-10-01", "category": "wash", "amount_krw": 1},
            ctype="text/plain",
        )
        self.assertEqual(code, 415)
        req = urllib.request.Request(
            f"{self.origin}/api/live",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)

    def test_stations_rates_charge_meta_csv(self) -> None:
        st, _station = self._write(
            "POST",
            "/api/stations",
            {
                "name": "집",
                "operator": "home",
                "address_text": "차고",
                "default_unit_price_krw_per_kwh": 120,
            },
        )
        self.assertEqual(st, 405)
        self._write("PUT", "/api/rates", {"operator": "home", "unit_price_krw_per_kwh": 90})
        _, charges = self._get("/api/charges")
        cid = charges["charges"][0]["charge_id"]
        self._write(
            "PUT",
            "/api/charge-meta",
            {
                "charge_id": cid,
                "operator": "home",
                "station_name": "집충전기",
                "unit_price_krw_per_kwh": 90,
                "kwh_billed": 20,
            },
        )
        # no paid amount: estimate from the operator rate only (90); places carry no price
        _, ledger = self._get("/api/ledger?category=charging")
        self.assertAlmostEqual(ledger["entries"][0]["amount_krw"], 1800.0, places=3)
        self.assertTrue(ledger["entries"][0]["estimated"])
        self._write(
            "PUT",
            "/api/charge-meta",
            {"charge_id": cid, "operator": "home", "location_text": "차고",
             "kwh_billed": 20, "total_krw": 1800},
        )
        _, ledger = self._get("/api/ledger?category=charging")
        self.assertAlmostEqual(ledger["entries"][0]["amount_krw"], 1800.0, places=3)
        self.assertFalse(ledger["entries"][0]["estimated"])
        _, stats = self._get("/api/ledger/charging-stats")
        self.assertEqual(stats["by_operator"][0]["operator"], "home")
        self.assertAlmostEqual(stats["average_unit_price"], 90.0, places=3)
        self.assertEqual(stats["cheapest"]["station_name"], "집충전기")   # kept: key not sent
        self.assertEqual(stats["cheapest"]["place"], "차고")
        status, raw = self._get("/api/ledger.csv")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        header = text.splitlines()[0]
        self.assertEqual(header, ",".join(CSV_HEADERS))
        self.assertIn("보험", INSURANCE_MEMO)
        self.assertIn("charging", text)
        gone, _ = self._write("DELETE", "/api/stations", {"id": "unused"})
        self.assertEqual(gone, 405)
        self._write("DELETE", "/api/rates", {"operator": "home"})

    def test_index_has_ledger_labels(self) -> None:
        url = f"{self.origin}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("차계부", html)
        self.assertIn("지출 추가", html)


if __name__ == "__main__":
    unittest.main()
