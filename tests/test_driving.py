"""운전 습관 리포트 (/api/driving, web/driving.py) and its stats-tab card."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from process import parse_ts, process_db
from simulator import generate
from web import make_server, serve_in_thread
from web.driving import driving_report, period_bounds, score

HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "VINDRV"


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class DrivingReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        conn = connect(self.db)
        conn.close()
        process_db(self.db, seed=False)  # creates the trips table
        self.conn = sqlite3.connect(str(self.db))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def trip(self, start: str, end: str, km: float, kwh: float | None = None, regen: float | None = None,
             vmax: float | None = None, a: int | None = 0, b: int | None = 0, t: int | None = 0) -> None:
        dur = (parse_ts(end) - parse_ts(start)).total_seconds()
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, kwh, duration_s, regen_kwh, max_speed, "
            "hard_accel, hard_brake, hard_turn) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (VID, start, end, km, kwh, dur, regen, vmax, a, b, t))
        self.conn.commit()

    def test_period_bounds_kst(self) -> None:
        self.assertEqual(period_bounds("week", date(2026, 10, 31)), (date(2026, 10, 26), date(2026, 11, 2)))
        self.assertEqual(period_bounds("month", date(2026, 12, 15)), (date(2026, 12, 1), date(2027, 1, 1)))
        self.assertEqual(period_bounds("year", date(2026, 10, 31)), (date(2026, 1, 1), date(2027, 1, 1)))
        with self.assertRaises(ValueError):
            period_bounds("day", date(2026, 1, 1))

    def test_score_formula(self) -> None:
        self.assertEqual(score({"accel": 10.0, "brake": 3.3, "turn": 0.0}, 30.0), 27)  # 100-50-23.1-0
        self.assertEqual(score({"accel": 30.0, "brake": 10.0, "turn": 5.0}, 30.0), 0)  # floor 0
        self.assertIsNone(score({"accel": 0.0, "brake": 0.0, "turn": 0.0}, 19.9))      # too little distance
        self.assertIsNone(score({"accel": None, "brake": 0.0, "turn": 0.0}, 50.0))     # no data

    def test_week_report(self) -> None:
        # week of 10/26 (Mon) - 11/01 (Sun) KST
        self.trip("2026-10-27T00:00:00Z", "2026-10-27T00:30:00Z", 20.0, kwh=4.0, regen=1.0, vmax=80, a=3, b=1, t=0)
        self.trip("2026-10-29T13:00:00Z", "2026-10-29T13:20:00Z", 10.0, kwh=2.0, regen=0.5, vmax=60, a=0, b=0, t=1)
        self.trip("2026-10-30T00:00:00Z", "2026-10-30T00:05:00Z", 1.0, kwh=0.2, vmax=30, a=None, b=None, t=None)
        # Sunday 23:30 KST belongs to this week, Monday 00:10 KST to the next
        self.trip("2026-11-01T14:30:00Z", "2026-11-01T14:40:00Z", 2.0, kwh=0.4, vmax=50)
        self.trip("2026-11-01T15:10:00Z", "2026-11-01T15:20:00Z", 5.0, kwh=1.0, vmax=90, a=9)
        now = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
        r = driving_report(self.conn, VID, "week", "2026-10-31", now=now)
        self.assertEqual((r["from"], r["to"]), ("2026-10-26", "2026-11-01"))
        self.assertEqual(r["trip_count"], 4)
        self.assertAlmostEqual(r["km"], 33.0)
        self.assertEqual(r["harsh"], {"accel": 3, "brake": 1, "turn": 1})  # the no-data trip is left out
        self.assertEqual(r["harsh_km"], 32.0)
        self.assertEqual(r["harsh_per_100km"], {"accel": 9.4, "brake": 3.1, "turn": 3.1})
        self.assertEqual(r["score"], round(100 - 5 * 9.4 - 7 * 3.1 - 3 * 3.1))
        self.assertEqual(r["max_speed"], 80)
        self.assertAlmostEqual(r["km_per_kwh"], round(33.0 / 6.6, 2))
        self.assertAlmostEqual(r["regen_ratio"], round(1.5 / 6.0, 3))
        self.assertNotIn("night_ratio", r)  # 야간 주행 removed
        self.assertEqual(len(r["trend"]), 8)
        self.assertEqual(r["trend"][-1]["from"], "2026-10-26")
        self.assertIsNone(r["rules"]["speed_limit"])

    def test_year_report_and_first_day(self) -> None:
        self.trip("2026-10-27T00:00:00Z", "2026-10-27T00:30:00Z", 20.0, kwh=4.0)
        self.trip("2026-03-02T00:00:00Z", "2026-03-02T00:30:00Z", 10.0, kwh=2.5)
        self.trip("2025-12-31T16:00:00Z", "2025-12-31T16:20:00Z", 5.0, kwh=1.0)  # 2026-01-01 01:00 KST
        r = driving_report(self.conn, VID, "year", "2026-05-01")
        self.assertEqual((r["from"], r["to"], r["trip_count"]), ("2026-01-01", "2026-12-31", 3))
        self.assertEqual(r["trend_unit"], "month")
        self.assertEqual([t["from"][5:7] for t in r["trend"]], [f"{m:02d}" for m in range(1, 13)])
        self.assertEqual(r["trend"][2]["km"], 10.0)
        self.assertEqual(r["first_day"], "2026-01-01")

    def test_small_distance_hides_rates_and_score(self) -> None:
        self.trip("2026-10-31T02:11:18Z", "2026-10-31T02:23:38Z", 2.25, kwh=0.52, regen=0.14, vmax=46)
        r = driving_report(self.conn, VID, "month", "2026-10-31")
        self.assertEqual(r["harsh"], {"accel": 0, "brake": 0, "turn": 0})
        self.assertEqual(r["harsh_per_100km"], {"accel": None, "brake": None, "turn": None})
        self.assertIsNone(r["score"])

    def test_empty_period(self) -> None:
        r = driving_report(self.conn, VID, "week", "2026-01-07")
        self.assertEqual(r["trip_count"], 0)
        for k in ("km_per_kwh", "regen_ratio", "max_speed", "avg_speed", "score"):
            self.assertIsNone(r[k], k)


class DrivingApiTests(unittest.TestCase):
    def test_api(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "signals.db")
        conn = connect(db)
        store_signals(conn, generate("day"))
        conn.close()
        process_db(db)
        server = make_server(db, host="127.0.0.1", port=0, tire_alert_bar=2.6, process_interval=0)
        serve_in_thread(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/driving?period=month", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body["period"], "month")
        self.assertIn("harsh", body)
        with urllib.request.urlopen(base + "/api/driving?period=year&at=2026-10-31", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read().decode("utf-8"))["from"], "2026-01-01")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/driving?period=day", timeout=5)


class DrivingCardTests(unittest.TestCase):
    def test_card_in_stats_after_summary(self) -> None:
        # : 운전 습관 lives in the 주행 tab, in a collapsible area under the list
        i = HTML.index('id="tab-trips"')
        self.assertLess(HTML.index('id="trips-summary"', i), HTML.index('id="trips-list-card"', i))
        self.assertLess(HTML.index('id="trips-list-card"', i), HTML.index('id="trips-eff"', i))
        self.assertNotIn('id="trips-trend"', HTML)

    def test_same_controls_as_trips_tab(self) -> None:
        # : one period button per menu replaces the per-card pickers
        for gone in ('id="drv-seg"', 'id="drv-week"', 'id="drv-month"', 'id="drv-year"', 'id="activity-seg"'):
            self.assertNotIn(gone, HTML)
        for menu in ("trips", "charges", "ledger"):
            self.assertIn(f'id="{menu}-period-btn"', HTML)
        self.assertIn('id="period-overlay"', HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_period_range(self) -> None:
        """: shared period sheet -> KST day ranges per quick button."""
        src = ("function addDays(day,n){const d=new Date(day+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);}\n"
               "const seoulToday=()=>'2026-11-01';const RECENT_N=5;let firstDays={trips:'2026-10-30'};\n"
               + _fn("lastDayOf") + _fn("mdText") + _fn("periodRange") +
               "const f=(p)=>{const r=periodRange('trips',p);return [r.from||null,r.to||null,r.label,r.view];};"
               "process.stdout.write(JSON.stringify([f({k:'recent'}),f({k:'7d'}),f({k:'month'}),f({k:'month',m:'2026-09'}),"
               "f({k:'year'}),f({k:'all'}),f({k:'range',from:'2026-10-30',to:'2026-10-31'})]));")
        out = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out[0], [None, None, "최근 5건", "list"])
        self.assertEqual(out[1], ["2026-10-26", "2026-11-01", "최근 7일", "list"])
        self.assertEqual(out[2], ["2026-11-01", "2026-11-01", "이번 달", "list"])
        self.assertEqual(out[3], ["2026-09-01", "2026-09-30", "2026년 9월", "list"])
        self.assertEqual(out[4], ["2026-01-01", "2026-11-01", "올해", "months"])
        self.assertEqual(out[5], ["2026-10-30", "2026-11-01", "전체", "months"])
        self.assertEqual(out[6][:2], ["2026-10-30", "2026-10-31"])

    def test_help_only_in_tooltip(self) -> None:
        self.assertNotIn('id="drive-help"', HTML)
        body = _fn("drvItems")
        self.assertNotIn("—", body)
        self.assertNotIn("(추정)", body)
        self.assertNotIn("센트리", body)
        self.assertNotIn("과속", body)
        self.assertNotIn("야간", body)

    def test_same_classes_as_summary(self) -> None:
        # values use the shared metric markup (no own font rules)
        self.assertIn("eff-num", _fn("renderTrend"))
        self.assertNotIn("#drv-grid .value", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_items_hide_unknown(self) -> None:
        src = ("function fmt(v,d){return Number(v).toFixed(d);}\n" + _fn("fmtMinutes") + _fn("drvItems") +
               "const full={score:72,trip_count:3,km:31.5,drive_minutes:75,harsh:{accel:2,brake:1,turn:0},"
               "harsh_per_100km:{accel:6.3,brake:3.2,turn:0},regen_ratio:0.348,km_per_kwh:4.15,max_speed:65,avg_speed:11.1};"
               "const thin={score:null,trip_count:1,km:2.25,drive_minutes:12,harsh:{accel:null,brake:null,turn:null},"
               "harsh_per_100km:{},regen_ratio:null,km_per_kwh:null,max_speed:46,avg_speed:null};"
               "process.stdout.write(JSON.stringify([drvItems(full),drvItems(thin)]));")
        full, thin = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(dict(full), {
            "주행": "3회 · 31.5 km", "운전 시간": "1시간 15분",
            "급가속": "2회", "급제동": "1회", "급회전": "0회",
            "회생제동": "약 35%", "최고 속도": "65 km/h", "평균 속도": "11 km/h"})   # : no 점수/전비 in 운전 패턴
        self.assertEqual(dict(thin), {"주행": "1회 · 2.3 km", "운전 시간": "12분", "최고 속도": "46 km/h"})
