"""Calendar bucketing in KST. Stored timestamps stay UTC ISO."""

import json
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import SCHEMA_SQL as SIGNALS_SQL
from ledger import charging_stats, csv_export, ensure_schema, kst_date, list_ledger, sync_auto_charges
from ledger.consumables import _as_of_date
from process import SCHEMA_SQL as PROCESS_SQL
from process import process_db
from ingest import connect as ingest_connect
from ingest import store_signals
from simulator import generate
from web import make_server, month_summary, serve_in_thread

VID = "VINTEST0000000001"
# 8/31 19:44 KST -> 9/1 02:38 KST. UTC dates are both still August 31.
SEP_START = "2026-08-31T10:44:00Z"
SEP_END = "2026-08-31T17:38:00Z"
# 10/16 21:14 KST -> 10/17 01:14 KST. End is after UTC 15:00, so the UTC date is still the 16th.
OCT_START = "2026-10-16T12:14:00Z"
OCT_END = "2026-10-16T16:14:00Z"


def _db(test: unittest.TestCase) -> tuple[str, sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(SIGNALS_SQL)
    conn.executescript(PROCESS_SQL)
    ensure_schema(conn)
    return tmp.name, conn


def _charge(conn: sqlite3.Connection, start: str, end: str, kwh: float, total: float) -> None:
    conn.execute(
        """
        INSERT INTO charges (
            vehicle_id, start_ts, end_ts, kwh_added, kind, start_soc, end_soc, max_power
        ) VALUES (?, ?, ?, ?, 'AC', 20, 80, 7)
        """,
        (VID, start, end, kwh),
    )
    conn.execute(
        """
        INSERT INTO charge_meta (
            vehicle_id, charge_id, operator, station_name,
            unit_price_krw_per_kwh, total_krw, kwh_billed
        ) VALUES (?, ?, 'home', '집', 300, ?, ?)
        """,
        (VID, start, total, kwh),
    )


def _trip(conn: sqlite3.Connection, start: str, end: str, km: float) -> None:
    conn.execute(
        """
        INSERT INTO trips (
            vehicle_id, start_ts, end_ts, km, kwh, wh_per_km,
            start_soc, end_soc, avg_outside_temp
        ) VALUES (?, ?, ?, ?, 1, 100, 80, 70, 20)
        """,
        (VID, start, end, km),
    )


def _between(s, a, b):
    i = s.index(a)
    return s[i:s.index(b, i)]


class KstDateTests(unittest.TestCase):
    def test_utc_15_boundary(self) -> None:
        self.assertEqual(kst_date("2026-10-16T14:59:00Z"), "2026-10-16")
        self.assertEqual(kst_date("2026-10-16T15:00:00Z"), "2026-10-17")
        self.assertEqual(kst_date("2026-08-31T15:00:00Z"), "2026-09-01")
        self.assertEqual(kst_date("2026-10-16T16:14:00+00:00"), "2026-10-17")

    def test_charge_examples(self) -> None:
        self.assertEqual(kst_date(SEP_START), "2026-08-31")
        self.assertEqual(kst_date(SEP_END), "2026-09-01")
        self.assertEqual(kst_date(OCT_START), "2026-10-16")
        self.assertEqual(kst_date(OCT_END), "2026-10-17")


class KstLedgerTests(unittest.TestCase):
    def test_charge_end_buckets_and_trip_start_month(self) -> None:
        _db_path, conn = _db(self)
        _charge(conn, SEP_START, SEP_END, 21.0, 6300.0)
        _charge(conn, OCT_START, OCT_END, 11.0, 3300.0)
        # No end yet: fall back to the start time's KST date (still August).
        _charge(conn, "2026-08-31T10:44:01Z", "", 1.0, 300.0)
        # Trip month follows the start. UTC 15:00 is KST midnight, so the month can change.
        _trip(conn, "2026-08-31T14:30:00Z", "2026-08-31T15:00:00Z", 10.0)
        _trip(conn, "2026-08-31T15:30:00Z", "2026-08-31T16:00:00Z", 20.0)
        _trip(conn, "2026-09-30T14:30:00Z", "2026-09-30T15:00:00Z", 3.0)
        _trip(conn, "2026-09-30T15:30:00Z", "2026-09-30T16:10:00Z", 4.0)
        conn.commit()

        sync_auto_charges(conn, VID)
        dates = {
            row["id"]: row["date"]
            for row in conn.execute("SELECT id, date FROM ledger WHERE source = 'auto'")
        }
        self.assertEqual(dates[f"auto-charge:{SEP_START}"], "2026-09-01")
        self.assertEqual(dates[f"auto-charge:{OCT_START}"], "2026-10-17")
        self.assertEqual(dates["auto-charge:2026-08-31T10:44:01Z"], "2026-08-31")
        stored = conn.execute(
            "SELECT start_ts, end_ts FROM charges WHERE start_ts = ?", (SEP_START,)
        ).fetchone()
        self.assertEqual(stored["start_ts"], SEP_START)
        self.assertEqual(stored["end_ts"], SEP_END)

        stats = charging_stats(conn, VID)
        months = {item["month"]: item["total_krw"] for item in stats["monthly_totals"]}
        self.assertEqual(months["2026-08"], 300.0)
        self.assertEqual(months["2026-09"], 6300.0)
        self.assertEqual(months["2026-10"], 3300.0)

        aug = month_summary(conn, VID, "2026-08")
        sep = month_summary(conn, VID, "2026-09")
        oct_ = month_summary(conn, VID, "2026-10")
        self.assertEqual(aug["charge_count"], 0)
        self.assertEqual(aug["km"], 10.0)
        self.assertEqual(sep["charge_count"], 1)
        self.assertAlmostEqual(sep["kwh_ac"], 21.0, places=3)
        self.assertEqual(sep["km"], 23.0)
        self.assertEqual(oct_["charge_count"], 1)
        self.assertAlmostEqual(oct_["kwh_ac"], 11.0, places=3)
        self.assertEqual(oct_["km"], 4.0)
        conn.close()

    def test_day_scenario_charge_lands_on_kst_end_date(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        conn = ingest_connect(tmp.name)
        try:
            store_signals(conn, generate("day"))
        finally:
            conn.close()
        process_db(tmp.name, seed=False)
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row
        charge = conn.execute("SELECT start_ts, end_ts FROM charges").fetchone()
        ledger = conn.execute(
            "SELECT date FROM ledger WHERE source = 'auto'"
        ).fetchone()
        conn.close()
        self.assertEqual(charge["start_ts"], "2026-10-26T15:10:00Z")
        self.assertEqual(charge["end_ts"], "2026-10-26T17:10:00Z")
        self.assertEqual(ledger["date"], "2026-10-27")


class KstTodayTests(unittest.TestCase):
    def test_default_month_and_consumable_as_of_use_kst(self) -> None:
        # 2026-08-31 16:30Z is 2026-09-01 01:30 KST: the month differs from UTC.
        fixed = datetime(2026, 8, 31, 16, 30, tzinfo=timezone.utc)

        def now(tz=None):
            return fixed.astimezone(tz) if tz is not None else fixed

        with patch("ledger.consumables.datetime") as mocked:
            mocked.now.side_effect = now
            self.assertEqual(_as_of_date(None), datetime(2026, 9, 1).date())

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        server = make_server(tmp.name, host="127.0.0.1", port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]
        with patch("web.datetime") as mocked:
            mocked.now.side_effect = now
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/summary?vehicle_id={VID}", timeout=5
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body["month"], "2026-09")

    def test_page_today_uses_seoul(self) -> None:
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('timeZone: "Asia/Seoul"', html)
        self.assertIn("seoulToday().slice(0, 7)", html)
        self.assertIn("form.date.value = seoulToday()", html)

    def test_ledger_period_defaults(self) -> None:
        # : 차계부 period comes from the shared sheet; default 이번 달 (KST)
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("seoulToday()", _between(html, "function periodRange", "function periodQuery"))
        self.assertIn('ledger: { quick: ["month", "year", "all"], def: { k: "month" }', html)

    def test_period_controls_survive_refresh(self) -> None:
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("지출 목록", html)
        self.assertIn("더 보기", html)
        self.assertIn('"teslife-period-"', html)
        for gone in ('data-activity-mode="month"', 'data-ledger-mode="month"', "/api/activity/yearly", 'id="ledger-year-category"'):
            self.assertNotIn(gone, html)
        body = _between(html, "function refreshAll(", "function onDataRev(")
        self.assertNotIn("savePeriod", body)


def _serve(test: unittest.TestCase, db_path: str) -> int:
    server = make_server(db_path, host="127.0.0.1", port=0, process_interval=0, seed=False)
    serve_in_thread(server)
    test.addCleanup(lambda: (server.shutdown(), server.server_close()))
    return server.server_address[1]


def _http_get(port: int, path: str) -> tuple[int, dict]:
    join = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{port}{path}{join}vehicle_id={VID}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


class PeriodApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, conn = _db(self)
        _charge(conn, SEP_START, SEP_END, 21.0, 6300.0)
        _charge(conn, OCT_START, OCT_END, 11.0, 3300.0)
        _charge(conn, "2026-08-31T10:44:01Z", "", 1.0, 300.0)
        _trip(conn, "2026-08-31T14:30:00Z", "2026-08-31T15:00:00Z", 10.0)
        _trip(conn, "2026-08-31T15:30:00Z", "2026-08-31T16:00:00Z", 20.0)
        _trip(conn, "2026-09-30T14:30:00Z", "2026-09-30T15:00:00Z", 3.0)
        _trip(conn, "2026-09-30T15:30:00Z", "2026-09-30T16:10:00Z", 4.0)
        _trip(conn, "2026-12-31T14:30:00Z", "2026-12-31T15:00:00Z", 5.0)
        _trip(conn, "2026-12-31T15:30:00Z", "2026-12-31T16:10:00Z", 6.0)
        for i in range(20):
            start = f"2024-05-10T01:{i:02d}:00Z"
            _trip(conn, start, start, 1.0)
        _trip(conn, "2024-04-30T15:00:00Z", "2024-04-30T16:00:00Z", 1.0)
        _trip(conn, "2024-04-30T14:59:00Z", "2024-04-30T15:10:00Z", 1.0)
        for i in range(3):
            start = f"2023-03-10T0{i}:00:00Z"
            end = f"2023-03-10T0{i}:30:00Z"
            _charge(conn, start, end, 2.0, 100.0)
        _charge(conn, "2024-05-31T10:44:00Z", "2024-05-31T15:38:00Z", 4.0, 400.0)
        _charge(conn, "2024-05-02T01:00:00Z", "2024-05-02T02:00:00Z", 3.0, 90.0)
        for i in range(25):
            conn.execute(
                """
                INSERT INTO ledger (
                    vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
                ) VALUES (?, ?, '2024-06-15', 'wash', ?, NULL, '', 'manual')
                """,
                (VID, f"led-{i:02d}", 1000 + i),
            )
        conn.execute(
            """
            INSERT INTO ledger (
                vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
            ) VALUES (?, 'led-tax', '2025-01-02', 'tax', 5000, NULL, '', 'manual')
            """,
            (VID,),
        )
        conn.commit()
        conn.close()
        self.port = _serve(self, self.db)

    def _get(self, path: str) -> tuple[int, dict]:
        return _http_get(self.port, path)

    def test_kst_month_boundary_and_paging(self) -> None:
        # 8/31 19:44 KST -> 9/1 02:38 KST belongs to September.
        status, sep_charges = self._get(f"/api/charges?month=2026-09")
        self.assertEqual(status, 200)
        self.assertEqual(sep_charges["total"], 1)
        self.assertEqual(sep_charges["charges"][0]["start_ts"], SEP_START)
        self.assertEqual(sep_charges["charges"][0]["end_ts"], SEP_END)
        self.assertNotIn("limit", sep_charges)

        status, aug_charges = self._get("/api/charges?month=2026-08")
        self.assertEqual(status, 200)
        self.assertEqual(aug_charges["total"], 1)
        self.assertEqual(aug_charges["charges"][0]["end_ts"], "")
        self.assertEqual(aug_charges["charges"][0]["start_ts"], "2026-08-31T10:44:01Z")

        status, oct_charges = self._get("/api/charges?month=2026-10")
        self.assertEqual(status, 200)
        self.assertEqual([c["start_ts"] for c in oct_charges["charges"]], [OCT_START])

        # Trip month follows the start. UTC 15:00 is KST midnight.
        _, sep_trips = self._get("/api/trips?month=2026-09")
        self.assertEqual(sep_trips["total"], 2)
        self.assertEqual(
            {t["start_ts"] for t in sep_trips["trips"]},
            {"2026-08-31T15:30:00Z", "2026-09-30T14:30:00Z"},
        )
        self.assertAlmostEqual(sum(t["km"] for t in sep_trips["trips"]), 23.0, places=3)
        _, aug_trips = self._get("/api/trips?month=2026-08")
        self.assertEqual([t["start_ts"] for t in aug_trips["trips"]], ["2026-08-31T14:30:00Z"])
        _, oct_trips = self._get("/api/trips?month=2026-10")
        self.assertEqual([t["start_ts"] for t in oct_trips["trips"]], ["2026-09-30T15:30:00Z"])

        # Year boundary: 12/31 23:30 KST stays in 2026, 1/1 00:30 KST is 2027.
        _, dec_trips = self._get("/api/trips?month=2026-12")
        self.assertEqual([t["start_ts"] for t in dec_trips["trips"]], ["2026-12-31T14:30:00Z"])
        _, jan_trips = self._get("/api/trips?month=2027-01")
        self.assertEqual([t["start_ts"] for t in jan_trips["trips"]], ["2026-12-31T15:30:00Z"])

        _, year = self._get("/api/activity/yearly?year=2026")
        self.assertEqual(year["year"], "2026")
        by_month = {m["month"]: m for m in year["months"]}
        self.assertEqual(set(by_month), {"2026-08", "2026-09", "2026-10", "2026-12"})
        self.assertEqual(by_month["2026-09"]["trip_count"], 2)
        self.assertAlmostEqual(by_month["2026-09"]["trip_km"], 23.0, places=3)
        self.assertEqual(by_month["2026-09"]["charge_count"], 1)
        self.assertAlmostEqual(by_month["2026-09"]["charge_kwh"], 21.0, places=3)
        self.assertAlmostEqual(by_month["2026-09"]["charge_krw"], 6300.0, places=3)
        self.assertEqual(by_month["2026-08"]["charge_count"], 1)
        self.assertAlmostEqual(by_month["2026-08"]["charge_krw"], 300.0, places=3)
        self.assertEqual(year["totals"]["trip_count"], 5)
        self.assertAlmostEqual(year["totals"]["trip_km"], 42.0, places=3)
        self.assertEqual(year["totals"]["charge_count"], 3)
        self.assertAlmostEqual(year["totals"]["charge_kwh"], 33.0, places=3)
        self.assertAlmostEqual(year["totals"]["charge_krw"], 9900.0, places=3)
        self.assertLessEqual(len(year["months"]), 12)

        _, y2027 = self._get("/api/activity/yearly?year=2027")
        self.assertEqual(y2027["totals"]["trip_count"], 1)
        self.assertAlmostEqual(y2027["totals"]["trip_km"], 6.0, places=3)
        self.assertEqual(y2027["totals"]["charge_count"], 0)

        # In-progress charges stay out of the legacy month summary.
        _, legacy = self._get("/api/summary?month=2026-08")
        self.assertEqual(legacy["charge_count"], 0)
        self.assertAlmostEqual(legacy["km"], 10.0, places=3)

    def test_paging_and_backward_compatible_lists(self) -> None:
        status, page = self._get("/api/trips?month=2024-05&limit=20&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 21)
        self.assertEqual(page["limit"], 20)
        self.assertEqual(page["offset"], 0)
        self.assertEqual(len(page["trips"]), 20)
        self.assertEqual(page["trips"][0]["start_ts"], "2024-05-10T01:19:00Z")
        _, rest = self._get("/api/trips?month=2024-05&limit=20&offset=20")
        self.assertEqual(len(rest["trips"]), 1)
        self.assertEqual(rest["trips"][0]["start_ts"], "2024-04-30T15:00:00Z")
        _, april = self._get("/api/trips?month=2024-04")
        self.assertEqual([t["start_ts"] for t in april["trips"]], ["2024-04-30T14:59:00Z"])

        # Month wins when both period params are present.
        _, both = self._get("/api/trips?month=2026-09&year=2024")
        self.assertEqual(both["month"], "2026-09")
        self.assertNotIn("year", both)
        self.assertEqual(both["total"], 2)

        _, charges = self._get("/api/charges?month=2023-03&limit=1&offset=0")
        self.assertEqual(charges["total"], 3)
        self.assertEqual(len(charges["charges"]), 1)
        self.assertEqual(charges["charges"][0]["start_ts"], "2023-03-10T02:00:00Z")
        _, last = self._get("/api/charges?month=2023-03&limit=1&offset=2")
        self.assertEqual(last["charges"][0]["start_ts"], "2023-03-10T00:00:00Z")
        # 5/31 19:44 KST -> 6/1 00:38 KST is a June charge.
        _, june = self._get("/api/charges?month=2024-06")
        self.assertEqual([c["start_ts"] for c in june["charges"]], ["2024-05-31T10:44:00Z"])
        _, may = self._get("/api/charges?month=2024-05")
        self.assertEqual([c["start_ts"] for c in may["charges"]], ["2024-05-02T01:00:00Z"])

        status, latest = self._get("/api/trips?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(latest["trips"]), 1)
        self.assertEqual(latest["trips"][0]["start_ts"], "2026-12-31T15:30:00Z")
        self.assertEqual(latest["total"], 28)
        _, all_trips = self._get("/api/trips")
        self.assertEqual(len(all_trips["trips"]), 28)
        self.assertNotIn("total", all_trips)
        self.assertNotIn("limit", all_trips)
        self.assertGreaterEqual(
            all_trips["trips"][0]["start_ts"], all_trips["trips"][1]["start_ts"]
        )
        _, all_charges = self._get("/api/charges")
        self.assertEqual(len(all_charges["charges"]), 8)
        self.assertNotIn("total", all_charges)
        _, one_charge = self._get("/api/charges?limit=1")
        self.assertEqual(one_charge["charges"][0]["start_ts"], OCT_START)

        status, bad = self._get("/api/trips?month=2026-13")
        self.assertEqual(status, 400)
        status, bad = self._get("/api/trips?limit=-1")
        self.assertEqual(status, 400)
        status, bad = self._get("/api/activity/yearly")
        self.assertEqual(status, 400)
        status, bad = self._get("/api/activity/yearly?year=26")
        self.assertEqual(status, 400)
        _, empty = self._get("/api/activity/yearly?year=2010")
        self.assertEqual(empty["months"], [])
        self.assertEqual(empty["totals"]["trip_count"], 0)
        self.assertEqual(empty["totals"]["charge_count"], 0)

    def test_ledger_pages_and_year_filter(self) -> None:
        status, page = self._get("/api/ledger?month=2024-06&limit=20&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 25)
        self.assertEqual(len(page["entries"]), 20)
        self.assertEqual(page["entries"][0]["id"], "led-24")
        self.assertEqual(page["entries"][-1]["id"], "led-05")
        _, rest = self._get("/api/ledger?month=2024-06&category=wash&limit=20&offset=20")
        self.assertEqual(rest["total"], 25)
        self.assertEqual(
            [e["id"] for e in rest["entries"]],
            [f"led-{i:02d}" for i in range(4, -1, -1)],
        )
        _, none = self._get("/api/ledger?month=2024-06&category=tax&limit=20")
        self.assertEqual(none["total"], 0)
        self.assertEqual(none["entries"], [])

        _, monthly = self._get("/api/ledger/monthly?year=2024")
        self.assertEqual([i["month"] for i in monthly["items"]], ["2024-06"])
        self.assertAlmostEqual(monthly["items"][0]["total"], 25300.0, places=3)
        self.assertAlmostEqual(monthly["items"][0]["by_category"]["wash"], 25300.0, places=3)
        _, y2025 = self._get("/api/ledger/monthly?year=2025")
        self.assertEqual(y2025["items"][0]["by_category"]["tax"], 5000)
        _, all_months = self._get("/api/ledger/monthly")
        self.assertEqual(
            [i["month"] for i in all_months["items"]],
            ["2024-06", "2025-01"],
        )
        _, yearly = self._get("/api/ledger/yearly")
        self.assertEqual([i["year"] for i in yearly["items"]], ["2024", "2025"])
        _, wash_months = self._get("/api/ledger/monthly?year=2024&category=wash")
        self.assertEqual([i["month"] for i in wash_months["items"]], ["2024-06"])
        self.assertAlmostEqual(wash_months["items"][0]["total"], 25300.0, places=3)
        self.assertEqual(set(wash_months["items"][0]["by_category"]), {"wash"})
        _, no_tax = self._get("/api/ledger/monthly?year=2024&category=tax")
        self.assertEqual(no_tax["items"], [])
        _, wash_years = self._get("/api/ledger/yearly?category=wash")
        self.assertEqual([i["year"] for i in wash_years["items"]], ["2024"])
        self.assertAlmostEqual(wash_years["items"][0]["total"], 25300.0, places=3)
        _, tax_years = self._get("/api/ledger/yearly?category=tax")
        self.assertEqual([i["year"] for i in tax_years["items"]], ["2025"])
        status, _ = self._get("/api/ledger/monthly?category=spaceship")
        self.assertEqual(status, 400)
        _, all_entries = self._get("/api/ledger")
        self.assertEqual(len(all_entries["entries"]), 26)
        self.assertNotIn("total", all_entries)
        self.assertNotIn("limit", all_entries)

        status, _ = self._get("/api/ledger?limit=nope")
        self.assertEqual(status, 400)
        status, _ = self._get("/api/ledger/monthly?year=2024a")
        self.assertEqual(status, 400)

    def test_many_rows_stay_aggregated(self) -> None:
        db, conn = _db(self)
        base = datetime(2014, 1, 1, 3, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(2000):
            start = (base + timedelta(days=i * 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = (base + timedelta(days=i * 2, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append((VID, start, end, 1.0))
        conn.executemany(
            """
            INSERT INTO trips (
                vehicle_id, start_ts, end_ts, km, kwh, wh_per_km,
                start_soc, end_soc, avg_outside_temp
            ) VALUES (?, ?, ?, ?, 1, 100, 80, 70, 20)
            """,
            rows,
        )
        conn.commit()
        conn.close()
        port = _serve(self, db)
        status, year = _http_get(port, "/api/activity/yearly?year=2014")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(year["months"]), 12)
        self.assertGreater(year["totals"]["trip_count"], 100)
        self.assertEqual(
            sum(m["trip_count"] for m in year["months"]),
            year["totals"]["trip_count"],
        )
        status, page = _http_get(port, "/api/trips?month=2014-01&limit=20&offset=0")
        self.assertEqual(status, 200)
        self.assertLess(page["total"], 40)
        self.assertEqual(len(page["trips"]), page["total"])
        self.assertLess(page["total"], 2000)


class SameDayLedgerOrderTests(unittest.TestCase):
    def test_same_day_charges_sort_by_end_then_pages_and_csv_match(self) -> None:
        # 6,080원: KST 10/16 21:14–10/17 01:14. 548원: KST 10/17 08:15–08:52.
        # A third session starts earlier than both but ends later, so end time
        # must win over id / start time.
        early_start = "2026-10-16T12:14:00Z"
        early_end = "2026-10-16T16:14:00Z"
        late_start = "2026-10-16T23:15:00Z"
        late_end = "2026-10-16T23:52:00Z"
        longer_start = "2026-10-16T10:00:00Z"
        longer_end = "2026-10-17T02:00:00Z"
        open_start = "2026-10-16T23:59:00Z"
        _path, conn = _db(self)
        _charge(conn, early_start, early_end, 20.0, 6080.0)
        _charge(conn, late_start, late_end, 2.0, 548.0)
        _charge(conn, longer_start, longer_end, 30.0, 9000.0)
        _charge(conn, open_start, "", 1.0, 100.0)
        conn.execute(
            """
            INSERT INTO ledger (
                vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
            ) VALUES (?, 'manual-a', '2026-10-17', 'wash', 1000, NULL, '', 'manual')
            """,
            (VID,),
        )
        conn.execute(
            """
            INSERT INTO ledger (
                vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
            ) VALUES (?, 'manual-b', '2026-10-17', 'wash', 2000, NULL, '', 'manual')
            """,
            (VID,),
        )
        conn.commit()
        sync_auto_charges(conn, VID)

        rows = list_ledger(conn, VID, month="2026-10")
        self.assertEqual(
            [r["id"] for r in rows],
            [
                f"auto-charge:{longer_start}",
                f"auto-charge:{open_start}",
                f"auto-charge:{late_start}",
                f"auto-charge:{early_start}",
                "manual-b",
                "manual-a",
            ],
        )
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[f"auto-charge:{early_start}"]["date"], "2026-10-17")
        self.assertEqual(by_id[f"auto-charge:{late_start}"]["date"], "2026-10-17")
        self.assertAlmostEqual(by_id[f"auto-charge:{early_start}"]["amount_krw"], 6080.0)
        self.assertAlmostEqual(by_id[f"auto-charge:{late_start}"]["amount_krw"], 548.0)

        page = list_ledger(conn, VID, month="2026-10", limit=1, offset=0)
        nxt = list_ledger(conn, VID, month="2026-10", limit=1, offset=1)
        self.assertEqual(page[0]["id"], rows[0]["id"])
        self.assertEqual(nxt[0]["id"], rows[1]["id"])

        text = csv_export(conn, VID).decode("utf-8-sig")
        csv_ids = [line.split(",")[1] for line in text.splitlines()[1:] if line]
        self.assertEqual(csv_ids, [r["id"] for r in list_ledger(conn, VID)])
        conn.close()


if __name__ == "__main__":
    unittest.main()
