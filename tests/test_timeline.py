"""Day timeline (stats > 배터리 사용 > 일 선택): lanes per mode, sleep stays empty, hour detail."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
from process import connect as process_connect, process_db
from web.timeline import AWAKE_GAP, LANE_LABELS, awake_spans, day_timeline

VID = "VINTL"
T = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)  # 10:00 KST
NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)


def H(h: float) -> datetime:
    return T + timedelta(hours=h)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def every(a: float, b: float, name: str, f) -> list:
    out, t = [], a
    while t <= b + 1e-9:
        out.append((H(t), name, f(t)))
        t += 5 / 60
    return out


class AwakeSpanTests(unittest.TestCase):
    def test_gap_splits_runs(self) -> None:
        ts = [H(0), H(0.1), H(0.2), H(3), H(3.05)]
        self.assertEqual(awake_spans(ts), [(H(0), H(0.2)), (H(3), H(3.05))])
        self.assertEqual(AWAKE_GAP, timedelta(minutes=15))


class DayTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        rows = [
            (H(0), "Gear", "P"), (H(0), "ClimateKeeperMode", "Party"), (H(0), "HvacPower", "On"),
            (H(0), "SentryMode", "Armed"), (H(0), "EnergyRemaining", 60.0),
            *every(0, 1, "LifetimeEnergyUsed", lambda t: 100.0 + t),          # 1 kW camping
            *every(0, 1, "BatteryLevel", lambda t: 80.0 - t),
            # asleep 11:00-14:00 KST: no signals at all
            (H(4), "ClimateKeeperMode", "Off"), (H(4), "HvacPower", "Off"), (H(4), "SentryMode", "Off"),
            *every(4, 4.5, "LifetimeEnergyUsed", lambda t: 101.05),
            # drive 14:30-15:00, then charging with sentry on
            (H(4.5), "Gear", "D"), *every(4.5, 5, "VehicleSpeed", lambda t: 40.0),
            (H(4.6), "HvacPower", "On"), (H(4.9), "HvacPower", "Off"),     # HVAC while driving
            *every(4.5, 5, "Odometer", lambda t: 1000 + (t - 4.5) * 40), (H(5), "Gear", "P"),
            (H(5.2), "DetailedChargeState", "Charging"), (H(5.2), "SentryMode", "Armed"),
            *every(5.2, 6, "ACChargingEnergyIn", lambda t: (t - 5.2) * 7),
            (H(6), "DetailedChargeState", "Complete"), (H(6.1), "SentryMode", "Off"),
        ]
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(self.db, seed=False, now=H(8))
        self.conn = process_connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.d = day_timeline(self.conn, VID, "2026-10-30", now=NOW)
        self.lanes = {l["key"]: l["bars"] for l in self.d["lanes"]}

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_overlapping_modes_show_in_both_lanes(self) -> None:
        self.assertNotIn("camp", self.lanes)
        self.assertNotIn("dog", self.lanes)
        self.assertNotIn("keep", self.lanes)
        camp = [b for b in self.lanes["parked"] if b.get("detail") == "camp"]
        sentry = self.lanes["sentry"]
        self.assertEqual((camp[0]["start_ts"], camp[0]["end_ts"]), (iso(H(0)), iso(H(1))))
        self.assertEqual((sentry[0]["start_ts"], sentry[0]["end_ts"]), (iso(H(0)), iso(H(1))))
        self.assertEqual(camp[0]["title"], "주차 · 캠핑")
        self.assertAlmostEqual(camp[0]["kwh"] + sentry[0]["kwh"], 1.0, places=2)
        self.assertAlmostEqual(sentry[0]["kwh"], 0.226, places=3)
        self.assertGreaterEqual(camp[0]["kwh"], 0)
        self.assertNotIn("with", camp[0])
        self.assertNotIn("(감시 포함)", camp[0]["title"])

    def test_sleep_stays_empty(self) -> None:
        a, b = H(1), H(4)
        for bars in self.lanes.values():
            for bar in bars:
                self.assertFalse(bar["start_ts"] < iso(b) and bar["end_ts"] > iso(a), bar)
        self.assertNotIn("sleep", self.lanes)
        self.assertEqual(self.d["hours"][12]["states"], [])
        self.assertEqual(self.d["hours"][12]["awake_min"], 0)
        self.assertNotIn("start_pct", self.d["hours"][12])
        # the camping piece runs on through sleep in parked_segments; its bar is cut and
        # the wake jump is not shown inside the empty hours
        plain = [b for b in self.lanes["parked"] if b.get("detail") == "parked"]
        self.assertEqual(plain[0]["start_ts"], iso(H(4)))

    def test_sentry_while_charging_without_energy(self) -> None:
        self.assertNotIn("charge", self.lanes)       # no charge lane; sentry still shows
        late = [b for b in self.lanes["sentry"] if b["start_ts"] >= iso(H(5))]
        self.assertTrue(late)
        self.assertNotIn("kwh", late[0])              # not measured on its own (mixed with the charge)
        # sentry prior (226 W, this car) until enough sentry-only parked time is measured
        mins = late[0]["minutes"]
        self.assertEqual((late[0]["estimate_w"], late[0]["estimate_source"]), (226, "prior"))
        self.assertAlmostEqual(late[0]["kwh_estimate"], 226 * mins / 60 / 1000, places=2)
        self.assertTrue(self.lanes["drive"])

    def test_no_hvac_or_media_lanes(self) -> None:
        # : HVAC (parked or while driving) has no lane; parked HVAC time counts as 주차
        self.assertEqual([l["key"] for l in self.d["lanes"]], ["drive", "parked", "sentry"])
        self.assertFalse([s for h in self.d["hours"] for s in h["states"] if s["lane"] == "charge"])
        for bars in self.lanes.values():
            self.assertFalse([b for b in bars if b.get("driving")])
        self.assertEqual([l["label"] for l in self.d["lanes"] if l["key"] == "parked"], ["주차"])
        cats = {r[0] for r in self.conn.execute("SELECT DISTINCT category FROM parked_segments").fetchall()}
        self.assertLessEqual(cats, {"camp", "dog", "keep", "sentry", "parked"})

    def test_hour_detail(self) -> None:
        h10 = self.d["hours"][10]
        # first and last BatteryLevel inside 10:00-11:00 (the 11:00 sample is the next hour)
        self.assertEqual((h10["start_pct"], h10["end_pct"], h10["start_ts"]), (80.0, 79.1, iso(H(0))))
        self.assertAlmostEqual(h10["delta_pct"], -11 / 12, places=2)
        self.assertAlmostEqual(h10["kwh"], (11 / 12) * 60.0 / (80 - 11 / 12), places=2)
        self.assertTrue(h10["kwh_estimated"])
        self.assertEqual({s["lane"] for s in h10["states"]}, {"parked", "sentry"})
        self.assertEqual(h10["awake_min"], 60.0)
        self.assertEqual(len(self.d["hours"]), 24)
        self.assertIsNone(self.d["now_ts"])

    def test_bad_date(self) -> None:
        with self.assertRaises(ValueError):
            day_timeline(self.conn, VID, "2026-10")


class TimelineUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.web = (ROOT / "web" / "__init__.py").read_text(encoding="utf-8")

    def test_route_and_markup(self) -> None:
        self.assertIn('"/api/battery/timeline"', self.web)
        for needle in ('id="bat-timeline"', 'id="tl-pop"', "function drawTimeline", "function hourPopHtml",
                       "data-bat-hour", 'ev.pointerType === "mouse"'):
            self.assertIn(needle, self.html)
        hour = self.html[self.html.index("function hourPopHtml"):self.html.index("function showTlPop")]
        self.assertNotIn("states", hour)
        self.assertIn('약 "', hour)
        self.assertNotIn("변화", hour)
        self.assertNotIn("기록", hour)
        self.assertNotIn(":00–", hour)

    def test_mode_graph_inside_battery_card(self) -> None:
        # 모드별 사용 is inside the 배터리 사용 card, right under the hourly chart (: no i)
        self.assertNotIn('id="mode-card"', self.html)
        self.assertNotIn('mode-help', self.html)
        bat = self.html[self.html.index('id="battery-card"'):]
        bat = bat[:bat.index("</section>")]
        order = [bat.index(n) for n in ('id="bat-day"', 'class="chart-legend"', 'id="mode-title"', 'id="bat-timeline"', 'id="tl-pop"')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('class="info-btn"', bat)
        self.assertIn("<span><i style=\"background:#3dd68c\"></i>충전</span>", bat)
        self.assertNotIn('id="mode-legend"', bat)

    def test_hourly_bar_selected_ring(self) -> None:
        # : a tapped hourly bar gets the same ring as a tapped mode bar.
        self.assertIn('class="bh-ring"', self.html)
        self.assertIn("#bat-day [data-bat-hour].on .bh-ring { display: inline; }", self.html)
        self.assertIn("#bat-day .bh-ring { display: none; stroke: var(--text); stroke-width: 2px; }", self.html)
        self.assertIn(".tl-bar.on { outline: 2px solid var(--text);", self.html)
        self.assertIn('if (el.classList) el.classList.add("on");', self.html)

    def test_no_charge_lane_in_client(self) -> None:
        self.assertNotIn('lane.key === "charge"', self.html)
        self.assertNotIn('charge: "#3dd68c"', self.html)
        self.assertIn('const MODE_LANES = ["drive", "parked", "sentry"];', self.html)

    def test_weekly_selected_day_ring(self) -> None:
        # : the selected day in the weekly chart has the same ring, redrawn at once on tap.
        self.assertIn('class="bw-ring"', self.html)
        self.assertIn("#bat-week .bw-ring { stroke: var(--text); stroke-width: 2px; }", self.html)
        self.assertIn("drawBatWeek(days); refreshBattery();", self.html)
        self.assertIn('const MODE_LANES = ["drive", "parked", "sentry"];', self.html)
        self.assertIn('const MODE_LABELS = { drive: "주행", parked: "주차", sentry: "감시" };', self.html)
        self.assertIn('rows.push(["사용량", lane.key === "drive" ? driveKwhText(b) : tlKwhPct(b.kwh)]);', self.html)
        self.assertNotIn('repeating-linear-gradient(135deg', self.html)   # no driving stripes
        self.assertNotIn("(주행 중)", self.html)
        for gone in ("그 밖의 공조", "그냥 주차", "미디어(주행"):
            self.assertNotIn(gone, self.html)
        # Home shows the parked overheat state. The mode timeline still has no such lane.
        self.assertIn(">실내 과열 방지<", self.html)
        self.assertNotIn("overheat", self.html[self.html.index("const MODE_LANES"):self.html.index("const MODE_LABELS")])
        self.assertIn('" 모드별 사용"', self.html)
        self.assertIn('Number(batDay.slice(8)) + " 시간별 사용";', self.html)   # 'M/D 시간별 사용', no %

    def test_timeline_tip_stays_in_the_card_when_the_page_scrolls(self) -> None:
        # showTlPop used to set position:fixed and move #tl-pop onto document.body,
        # so a pinned 감시 tip stayed on screen after the card scrolled.
        card = self.html.split('id="battery-card"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="tl-pop"', card)
        self.assertIn(".tl-pop {\n      position: absolute;", self.html)
        fn = self.html[self.html.index("function showTlPop("):self.html.index("function hideTlPop(")]
        self.assertNotIn('position = "fixed"', fn)
        self.assertNotIn("document.body.appendChild", fn)
        self.assertIn('getElementById("battery-card")', fn)
        self.assertIn('pop.style.position = ""', fn)
        for kind in (".cross-tip {", ".speed-tip {"):
            block = self.html[self.html.index(kind):self.html.index(kind) + 80]
            self.assertIn("position: absolute", block)
            self.assertNotIn("position: fixed", block)

    def test_outside_tap_uses_the_graph_dismiss_handler(self) -> None:
        self.assertEqual(self.html.count('document.addEventListener("pointerdown", dismissGraphTip)'), 1)
        bind = self.html[self.html.index("function bindCrosshair("):self.html.index("function crossLine(")]
        self.assertNotIn('document.addEventListener("pointerdown"', bind)
        click = self.html[self.html.index("function bindTlPop("):self.html.index("function fmtMinutes(")]
        self.assertNotIn('document.addEventListener("click"', click)
        src = (
            "let tlPinned = {contains(t){ return t === 'bar'; }};\n"
            "const log = [];\n"
            "function hideCross(){ log.push('cross'); }\n"
            "function hideTlPop(){ log.push('tl'); }\n"
            "const pop = {hidden: false, contains(t){ return t === 'pop'; }};\n"
            "const chart = {contains(t){ return t === 'chart'; }};\n"
            "global.document = {querySelectorAll(){ return [chart]; }, getElementById(){ return pop; }};\n"
            + self.html[self.html.index("function dismissGraphTip("):self.html.index("if (!window.__teslifeCrossDoc)")]
            + "function run(target){ log.length = 0; dismissGraphTip({target: target}); return log.slice(); }\n"
            + "process.stdout.write(JSON.stringify({away: run('away'), pop: run('pop'), bar: run('bar'), chart: run('chart')}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["away"], ["cross", "tl"])
        self.assertEqual(got["pop"], ["cross"])
        self.assertEqual(got["bar"], ["cross"])
        self.assertEqual(got["chart"], ["tl"])
        self.assertNotIn('" 시간별 사용 " + fmt(', self.html)
        self.assertIn("text-anchor=\"middle\">' + fmt(v, 1) + \"%</text>", self.html)   # weekly labels 1 decimal
        self.assertNotIn("시간별 · 사용", self.html)
        self.assertIn("<span class='tl-lab'", self.html)       # colour dot + name per row (back in )
        self.assertIn("width: calc(100% * 76 / 306)", self.html)
        self.assertNotIn("id='tl-legend'", self.html)          # : no legend under 모드별 사용
        # one plot like the hourly chart: no lane backgrounds, same 6/12/18 dividers
        self.assertIn("<div class='tl-plot'>", self.html)
        self.assertIn("[6, 12, 18].map(h => \"<i class='tl-grid'", self.html)
        self.assertIn('<line class="bh-grid"', self.html)
        self.assertIn(".tl-plot .tl-row { position: relative; height: 22px; box-sizing: border-box; border-top: 1px solid var(--line); }", self.html)
        self.assertIn(".tl-bar {\n      position: absolute; top: 3px; bottom: 3px;", self.html)
        # same plot area in both charts: viewBox 390, left 76 / right 8; legends start there too
        self.assertIn("const W = 390, H = 190, L = 76, R = 8,", self.html)
        self.assertIn(".tl-plot, .tl-row.tl-axis { margin: 0 calc(100% * 8 / 390) 0 calc(100% * 76 / 390); }", self.html)
        self.assertIn("#battery-card .chart-legend { margin-left: calc(100% * 76 / 390); }", self.html)
        self.assertNotIn("bh-pct", self.html)
        self.assertNotIn("rgba(255,255,255,0.02)", self.html)

    def test_rising_hour_uses_the_charge_color(self) -> None:
        legend = self.html.split('id="bat-day"', 1)[1].split('id="mode-title"', 1)[0]
        self.assertIn(">충전</span>", legend)
        i = self.html.index("function hourFill(")
        fn = self.html[i:self.html.index("\n    }\n", i) + 6]
        src = (
            "function known(v){ return v !== null && v !== undefined && v !== ''; }\n"
            "function tone(name){ return name; }\n"
            + fn
            + "const out = {\n"
            "  rose: hourFill({driving:true, charging:false}, {start_pct:63.3, end_pct:66.2}),\n"
            "  downDrive: hourFill({driving:true, charging:true}, {start_pct:66.2, end_pct:63.3}),\n"
            "  downCharge: hourFill({driving:false, charging:true}, {start_pct:66.2, end_pct:63.3}),\n"
            "  flat: hourFill({driving:true}, {start_pct:66.2, end_pct:66.2}),\n"
            "  park: hourFill({driving:false, charging:false}, null)\n"
            "};\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["rose"], "chart-charge")
        self.assertEqual(got["downDrive"], "chart-trip")
        self.assertEqual(got["downCharge"], "chart-park")
        self.assertEqual(got["flat"], "chart-trip")
        self.assertEqual(got["park"], "chart-park")

    def test_mode_tooltip_fields_only(self) -> None:
        body = self.html[self.html.index("function barPopHtml"):self.html.index("function hourPopHtml")]
        for needle in ('"시간"', '"사용량"'):
            self.assertIn(needle, body)
        self.assertNotIn('"총 시간"', body)
        self.assertNotIn("쓴 전력", body)
        for gone in ("함께", "평균", "SoC", "거리"):
            self.assertNotIn(gone, body)

    def test_help_only_in_info_tip(self) -> None:
        # : all i tooltips are gone
        self.assertNotIn("tl-help", self.html)
        self.assertNotIn("info-pop", self.html)


class DriveNetTests(unittest.TestCase):
    """Mode-card drive kWh is the pack's net drop, not LifetimeEnergyUsed. Trip rows stay gross."""

    def _run(self, day: str, rows: list) -> tuple[dict, float | None]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "signals.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, rows)
        conn.close()
        end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=15)
        process_db(db, seed=False, now=end)
        conn = process_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            stored = conn.execute("SELECT kwh FROM trips").fetchone()
            timeline = day_timeline(conn, "VINNET", day, now=end + timedelta(hours=1))
        finally:
            conn.close()
        return timeline, (None if stored is None else stored[0])

    def _sig(self, ts: str, name: str, value) -> dict:
        return {"vehicle_id": "VINNET", "timestamp": ts, "name": name, "value": value}

    def test_nov6_uses_remaining_drop_not_lifetime(self) -> None:
        # Gross LifetimeEnergyUsed 13.79 kWh. Pack fell 60 → 49 kWh and 80 → 61.1 %.
        rows = [
            self._sig("2026-11-06T00:00:00Z", "Gear", "P"),
            self._sig("2026-11-06T00:10:00Z", "Gear", "D"),
            self._sig("2026-11-06T00:10:00Z", "VehicleSpeed", 40),
            self._sig("2026-11-06T00:10:00Z", "Odometer", 1000),
            self._sig("2026-11-06T00:10:00Z", "LifetimeEnergyUsed", 100),
            self._sig("2026-11-06T00:10:00Z", "EnergyRemaining", 60),
            self._sig("2026-11-06T00:10:00Z", "BatteryLevel", 80),
            self._sig("2026-11-06T01:00:00Z", "Gear", "P"),
            self._sig("2026-11-06T01:00:00Z", "Odometer", 1020),
            self._sig("2026-11-06T01:00:00Z", "LifetimeEnergyUsed", 113.79),
            self._sig("2026-11-06T01:00:00Z", "EnergyRemaining", 49),
            self._sig("2026-11-06T01:00:00Z", "BatteryLevel", 61.1),
        ]
        timeline, stored = self._run("2026-11-06", rows)
        self.assertAlmostEqual(stored, (80 - 61.1) * (49 / 61.1), places=2)
        drive = next(l for l in timeline["lanes"] if l["key"] == "drive")["bars"][0]
        self.assertEqual(drive["kwh"], 15.16)  # 18.9 %p × (49 kWh / 61.1 %)
        self.assertEqual(drive["soc_drop"], 18.9)
        self.assertTrue(drive["net"])
        self.assertNotIn("estimated", drive)
        self.assertAlmostEqual(timeline["totals"]["kwh"], 15.16, places=2)

    def test_nov7_falls_back_to_lifetime_when_pack_is_unknown(self) -> None:
        rows = [
            self._sig("2026-11-07T00:00:00Z", "Gear", "P"),
            self._sig("2026-11-07T00:00:00Z", "Odometer", 1000),
            self._sig("2026-11-07T00:10:00Z", "LifetimeEnergyUsed", 200),
            self._sig("2026-11-07T00:10:00Z", "Gear", "D"),
            self._sig("2026-11-07T00:10:00Z", "VehicleSpeed", 40),
            self._sig("2026-11-07T01:00:00Z", "Gear", "P"),
            self._sig("2026-11-07T01:00:00Z", "Odometer", 1010),
            self._sig("2026-11-07T01:00:00Z", "LifetimeEnergyUsed", 208),
        ]
        timeline, stored = self._run("2026-11-07", rows)
        self.assertAlmostEqual(stored, 6.0, places=2)  # 8 kWh gross × 0.75, no battery level
        drive = next(l for l in timeline["lanes"] if l["key"] == "drive")["bars"][0]
        self.assertEqual(drive["kwh"], 6.0)
        self.assertTrue(drive["estimated"])
        self.assertNotIn("net", drive)
        self.assertNotIn("soc_drop", drive)

    def test_nov8_level_drop_times_kwh_per_pct(self) -> None:
        rows = [
            self._sig("2026-11-08T00:00:00Z", "Gear", "P"),
            self._sig("2026-11-08T00:10:00Z", "Gear", "D"),
            self._sig("2026-11-08T00:10:00Z", "VehicleSpeed", 40),
            self._sig("2026-11-08T00:10:00Z", "Odometer", 1000),
            self._sig("2026-11-08T00:10:00Z", "LifetimeEnergyUsed", 300),
            self._sig("2026-11-08T00:10:00Z", "BatteryLevel", 50),
            self._sig("2026-11-08T01:00:00Z", "Gear", "P"),
            self._sig("2026-11-08T01:00:00Z", "Odometer", 1012),
            self._sig("2026-11-08T01:00:00Z", "LifetimeEnergyUsed", 309),
            self._sig("2026-11-08T01:00:00Z", "BatteryLevel", 40),
            # After the trip, so the drive has no EnergyRemaining, but the day has a per-percent basis.
            self._sig("2026-11-08T09:00:00Z", "EnergyRemaining", 30),
            self._sig("2026-11-08T09:00:00Z", "BatteryLevel", 50),
        ]
        timeline, stored = self._run("2026-11-08", rows)
        self.assertAlmostEqual(stored, 7.5, places=2)  # 10 %p × (30 kWh / 40 %)
        drive = next(l for l in timeline["lanes"] if l["key"] == "drive")["bars"][0]
        self.assertEqual(drive["kwh"], 7.5)          # 10 %p × (30 kWh / 40 %) at the trip end
        self.assertEqual(drive["soc_drop"], 10.0)
        self.assertTrue(drive["net"])
        self.assertNotIn("estimated", drive)

    def test_a_pack_rise_is_zero(self) -> None:
        rows = [
            self._sig("2026-11-09T00:00:00Z", "Gear", "P"),
            self._sig("2026-11-09T00:10:00Z", "Gear", "D"),
            self._sig("2026-11-09T00:10:00Z", "VehicleSpeed", 30),
            self._sig("2026-11-09T00:10:00Z", "Odometer", 1000),
            self._sig("2026-11-09T00:10:00Z", "LifetimeEnergyUsed", 10),
            self._sig("2026-11-09T00:10:00Z", "EnergyRemaining", 40),
            self._sig("2026-11-09T00:10:00Z", "BatteryLevel", 50),
            self._sig("2026-11-09T01:00:00Z", "Gear", "P"),
            self._sig("2026-11-09T01:00:00Z", "Odometer", 1008),
            self._sig("2026-11-09T01:00:00Z", "LifetimeEnergyUsed", 12),
            self._sig("2026-11-09T01:00:00Z", "EnergyRemaining", 45),
            self._sig("2026-11-09T01:00:00Z", "BatteryLevel", 55),
        ]
        timeline, stored = self._run("2026-11-09", rows)
        self.assertAlmostEqual(stored, 1.5, places=2)  # rise, so 2 kWh gross × 0.75
        drive = next(l for l in timeline["lanes"] if l["key"] == "drive")["bars"][0]
        self.assertEqual(drive["kwh"], 0.0)
        self.assertEqual(drive["soc_drop"], 0.0)


class ChargeShareTests(unittest.TestCase):
    def test_record_energy_splits_on_the_counter_and_sums_exactly(self) -> None:
        from web.timeline import charge_hour_shares

        # 2026-11-15 21:00 KST is 12:00 UTC. The charge stays inside that hour.
        start = datetime(2026, 11, 15, 12, 10, tzinfo=timezone.utc)
        end = datetime(2026, 11, 15, 12, 50, tzinfo=timezone.utc)
        hour = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        hours = [(hour + timedelta(hours=i), hour + timedelta(hours=i + 1)) for i in range(-12, 12)]
        samples = [
            (start, 10.0),
            (start + timedelta(minutes=20), 11.2),
            (end, 11.81),
        ]
        parts = charge_hour_shares(1.81, start, end, samples, hours)
        self.assertAlmostEqual(sum(parts), 1.81, places=4)
        self.assertAlmostEqual(parts[12], 1.81, places=4)
        self.assertEqual(parts[:12] + parts[13:], [0.0] * 23)

        # The same record crosses 22:00, and the counter steps decide the split.
        late = datetime(2026, 11, 15, 12, 40, tzinfo=timezone.utc)
        stop = datetime(2026, 11, 15, 13, 20, tzinfo=timezone.utc)
        stepped = [
            (late, 4.0),
            (datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc), 5.0),
            (stop, 5.81),
        ]
        split = charge_hour_shares(1.81, late, stop, stepped, hours)
        self.assertAlmostEqual(sum(split), 1.81, places=4)
        self.assertGreater(split[12], 0)
        self.assertGreater(split[13], 0)
        self.assertAlmostEqual(split[12] / split[13], 1.0 / 0.81, places=2)

        # No counter samples: clock time, still the record total.
        timed = charge_hour_shares(1.81, late, stop, [], hours)
        self.assertAlmostEqual(sum(timed), 1.81, places=4)
        self.assertAlmostEqual(timed[12], 1.81 * 20 / 40, places=4)
        self.assertAlmostEqual(timed[13], 1.81 * 20 / 40, places=4)
