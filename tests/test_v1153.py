"""v1.15.3: pressure unit on one value, hour tooltip, hour boundaries, pause legend, temp sliver."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PSI_PER_BAR = 14.5038


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


def _node(src: str):
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class VersionTests(unittest.TestCase):
    def test_notes_stay_in_order(self) -> None:
        self.assertIn('const APP_VERSION = "v1.17.1"', HTML)
        self.assertLess(CHANGELOG.index("## v1.16.12"), CHANGELOG.index("## v1.16.11"))
        self.assertLess(CHANGELOG.index("## v1.16.11"), CHANGELOG.index("## v1.16.10"))
        self.assertLess(CHANGELOG.index("## v1.16.10"), CHANGELOG.index("## v1.16.9"))
        self.assertLess(CHANGELOG.index("## v1.16.9"), CHANGELOG.index("## v1.16.8"))
        self.assertLess(CHANGELOG.index("## v1.16.8"), CHANGELOG.index("## v1.16.7"))
        self.assertLess(CHANGELOG.index("## v1.16.7"), CHANGELOG.index("## v1.16.6"))
        self.assertLess(CHANGELOG.index("## v1.16.6"), CHANGELOG.index("## v1.16.5"))
        self.assertLess(CHANGELOG.index("## v1.16.5"), CHANGELOG.index("## v1.16.4"))
        self.assertLess(CHANGELOG.index("## v1.16.4"), CHANGELOG.index("## v1.16.3"))
        self.assertLess(CHANGELOG.index("## v1.16.3"), CHANGELOG.index("## v1.16.2"))
        self.assertLess(CHANGELOG.index("## v1.16.2"), CHANGELOG.index("## v1.16.1\n"))
        self.assertLess(CHANGELOG.index("## v1.16.1"), CHANGELOG.index("## v1.16.0"))
        self.assertLess(CHANGELOG.index("## v1.16.0"), CHANGELOG.index("## v1.15.3"))
        self.assertLess(HTML.index('version: "1.16.12"'), HTML.index('version: "1.16.11"'))
        self.assertLess(HTML.index('version: "1.16.11"'), HTML.index('version: "1.16.10"'))
        self.assertLess(HTML.index('version: "1.16.10"'), HTML.index('version: "1.16.9"'))
        self.assertLess(HTML.index('version: "1.16.9"'), HTML.index('version: "1.16.8"'))
        self.assertLess(HTML.index('version: "1.16.8"'), HTML.index('version: "1.16.7"'))
        self.assertLess(HTML.index('version: "1.16.7"'), HTML.index('version: "1.16.6"'))
        self.assertLess(HTML.index('version: "1.16.6"'), HTML.index('version: "1.16.5"'))
        self.assertLess(HTML.index('version: "1.16.5"'), HTML.index('version: "1.16.4"'))
        self.assertLess(HTML.index('version: "1.16.4"'), HTML.index('version: "1.16.3"'))
        self.assertLess(HTML.index('version: "1.16.3"'), HTML.index('version: "1.16.2"'))
        self.assertLess(HTML.index('version: "1.16.2"'), HTML.index('version: "1.16.1"'))
        self.assertLess(HTML.index('version: "1.16.1"'), HTML.index('version: "1.16.0"'))
        self.assertLess(CHANGELOG.index("## v1.15.3"), CHANGELOG.index("## v1.15.2"))
        self.assertLess(HTML.index('version: "1.15.3"'), HTML.index('version: "1.15.2"'))
        self.assertIn("숫자 뒤에 지금 고른 단위", CHANGELOG)
        self.assertIn("증감량", CHANGELOG)
        self.assertIn("정각 직전", CHANGELOG)
        self.assertIn("범례", CHANGELOG)
        self.assertIn("2분", CHANGELOG)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TireUnitOnSingleValueTests(unittest.TestCase):
    def test_one_number_uses_the_home_unit_text(self) -> None:
        src = (
            "let pressureUnit = 'psi'; const PSI_PER_BAR = 14.5038;\n"
            "function carTopSvg(){ return ''; }\n"
            + _fn("fmtTrim") + _fn("fmtFromBar") + _fn("psiNum") + _fn("tirePairText") + _fn("tireCompareHtml")
            + "const before = {fl:2.875, fr:2.88, rl:2.87, rr:2.90};\n"
            "const out = {\n"
            "  home: fmtFromBar(2.875),\n"
            "  one: tirePairText(2.875, null),\n"
            "  card: tireCompareHtml(before, {}),\n"
            "  both: tirePairText(2.87, 2.90),\n"
            "  after: tirePairText(null, 2.90),\n"
            "  dash: tirePairText(null, null)\n"
            "};\n"
            "pressureUnit = 'bar';\n"
            "out.homeBar = fmtFromBar(2.88);\n"
            "out.oneBar = tirePairText(2.88, null);\n"
            "out.bothBar = tirePairText(2.87, 2.90);\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = _node(src)
        one = f"{2.875 * PSI_PER_BAR:.1f}"
        self.assertEqual(one, "41.7")
        self.assertEqual(got["home"], "41.7 psi")
        self.assertEqual(got["one"], got["home"])
        self.assertIn(">41.7 psi<", got["card"])
        self.assertNotIn("→", got["card"])
        both = f"{2.87 * PSI_PER_BAR:.1f} → {2.90 * PSI_PER_BAR:.1f}"
        self.assertEqual(got["both"], both)
        self.assertNotIn("psi", got["both"])
        self.assertEqual(got["after"], f"{2.90 * PSI_PER_BAR:.1f} psi")
        self.assertEqual(got["dash"], "—")
        self.assertEqual(got["homeBar"], "2.88 bar")
        self.assertEqual(got["oneBar"], "2.88 bar")
        self.assertNotIn("bar", got["bothBar"])
        rule = HTML.split(".tire-cell .tv {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: 18px", rule)
        self.assertIn("font-weight: 600", rule)
        self.assertIn('class="value tv"', HTML)
        self.assertIn("class='tv'>", HTML)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HourTooltipTests(unittest.TestCase):
    def test_battery_line_matches_detail_and_keeps_usage(self) -> None:
        src = (
            "function fmt(n, digits) {\n"
            "  if (n === null || n === undefined || n === '') return '—';\n"
            "  let s = Number(n).toFixed(digits == null ? 0 : digits);\n"
            "  if (s.indexOf('.') >= 0) s = s.replace(/0+$/, '').replace(/\\.$/, '');\n"
            "  return s;\n"
            "}\n"
            "const known = v => v !== null && v !== undefined && v !== '';\n"
            "let batTimeline = {hours: [\n"
            "  {start_pct: 61.9, end_pct: 61.8, use_kwh: 0.05, use_estimated: true},\n"
            "  {start_pct: 65.0, end_pct: 63.4, use_kwh: 1.2, use_estimated: true},\n"
            "  {start_pct: 80.04, end_pct: 79.96, use_kwh: 0.04, use_estimated: true},\n"
            "  {start_pct: 64.6, end_pct: 64.6, use_kwh: 0},\n"
            "  {start_pct: 70, end_pct: 72.5, charge_kwh: 1.81, use_kwh: 0.4},\n"
            "  {start_pct: 50, charge_kwh: 1.5}\n"
            "]};\n"
            + _fn("socTenths") + _fn("socTenthsText") + _fn("socChangeText") + _fn("hourPopHtml")
            + "const out = [0,1,2,3,4,5].map(i => hourPopHtml(i, null));\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        got = _node(src)
        self.assertIn(">61.9% → 61.8%<", got[0])
        self.assertNotIn("%p", got[0])
        self.assertIn(">사용량<", got[0])
        self.assertIn("약 0.05kWh", got[0])
        self.assertIn(">65% → 63.4%<", got[1])
        self.assertNotIn("%p", got[1])
        self.assertIn("약 1.2kWh", got[1])
        self.assertIn(">80%<", got[2])
        self.assertNotIn("→", got[2])
        self.assertNotIn("%p", got[2])
        self.assertIn("약 0.04kWh", got[2])
        self.assertIn(">64.6%<", got[3])
        self.assertNotIn("→", got[3])
        self.assertNotIn("사용량", got[3])
        self.assertIn(">70% → 72.5%<", got[4])
        self.assertIn(">충전량<", got[4])
        self.assertIn(">1.81kWh<", got[4])
        self.assertNotIn("약 1.81", got[4])
        self.assertIn(">사용량<", got[4])
        self.assertIn(">50%<", got[5])
        self.assertIn(">충전량<", got[5])
        self.assertIn(">1.5kWh<", got[5])
        self.assertNotIn("사용량", got[5])
        self.assertNotIn("%p", "".join(got))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ChargeChartTests(unittest.TestCase):
    def _src(self, body: str) -> str:
        return (
            "function fmt(n, d){ const s = Number(n).toFixed(d == null ? 0 : d);"
            " return s.replace(/0+$/, '').replace(/\\.$/, ''); }\n"
            "function fmtHm(){ return '00:00'; }\n"
            "function tone(name){ return name === 'chart-axis' ? '#8aa0b5' : '#3dd68c'; }\n"
            + _fn("decimate") + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines") + _fn("crossLine") + _fn("chartSvg") + _fn("legendSwatch") + _fn("svgChargeBand")
            + body
        )

    def test_charge_power_fills_under_the_line_and_has_no_pause_mark(self) -> None:
        src = self._src(
            "const power = [\n"
            "  {ts:'2026-11-13T12:38:00Z', v:7},\n"
            "  {ts:'2026-11-13T12:50:00Z', v:0},\n"
            "  {ts:'2026-11-13T13:10:00Z', v:6}\n"
            "];\n"
            "const html = svgChargeBand(power, [], '2026-11-13T13:51:00Z');\n"
            "process.stdout.write(JSON.stringify({html: html}));"
        )
        got = _node(src)
        self.assertNotIn("충전 멈춤", got["html"])
        self.assertNotIn("fill-opacity=\"0.18\"", got["html"])
        self.assertIn("<polygon", got["html"])
        self.assertIn('fill-opacity="0.15"', got["html"])
        self.assertIn("충전 전력", got["html"])
        legend = got["html"].split("chart-legend", 1)[1]
        self.assertIn("height='8'", legend)
        self.assertIn("y1='2'", legend)
        self.assertNotIn("y1='5'", legend)

    def test_area_swatch_line_sits_on_the_top_edge(self) -> None:
        src = (
            _fn("legendSwatch")
            + "const area = legendSwatch({line:'#3dd68c', fill:'#3dd68c', fillOpacity:0.15});\n"
            + "const line = legendSwatch({line:'#9aa6b2', dash:'4 3'});\n"
            + "const fill = legendSwatch({fill:'#7c6ae0', fillOpacity:1});\n"
            + "process.stdout.write(JSON.stringify({area:area, line:line, fill:fill}));"
        )
        got = _node(src)
        self.assertIn("width='8'", got["area"])
        self.assertIn("height='8'", got["area"])
        self.assertIn("y1='2'", got["area"])
        self.assertIn("y2='2'", got["area"])
        self.assertNotIn("y1='5'", got["area"])
        self.assertIn("y1='5'", got["line"])
        self.assertIn("stroke-dasharray='4 3'", got["line"])
        self.assertNotIn("<line", got["fill"])
        charge = _fn("svgChargeBand")
        self.assertIn("legendSwatch({line: chargeColor, fill: chargeColor, fillOpacity: 0.15})", charge)
        speed = _fn("loadTripSpeed")
        power = _fn("loadTripPower")
        self.assertIn('unit: "km/h (-는 후진)"', speed)
        self.assertNotIn("chart-legend", speed)
        self.assertNotIn("legendSpan(", speed)
        self.assertIn('unit: "kW (-는 회생)"', power)
        self.assertNotIn("chart-legend", power)
        self.assertNotIn("legendSpan(", power)
        self.assertIn("below: regenColor", power)
        battery = _fn("svgBatteryTemp")
        self.assertIn("legendSwatch({line: color})", battery)
        self.assertIn("legendSwatch({fill: \"#e39a4a\", fillOpacity: 0.4})", battery)

    def test_power_chart_has_no_temperature_band(self) -> None:
        src = self._src(
            "const end = '2026-11-13T13:51:00Z';\n"
            "const power = [{ts:'2026-11-13T12:38:00Z', v:7},{ts:end, v:6}];\n"
            "const html = svgChargeBand(power, [{ts:'2026-11-13T13:13:00Z', min:20, max:40}], end, 11);\n"
            "process.stdout.write(JSON.stringify({html: html}));"
        )
        got = _node(src)
        self.assertNotIn("#e7c8a4", got["html"])
        self.assertIn("<polygon", got["html"])
        self.assertIn('fill-opacity="0.15"', got["html"])
        self.assertIn(">11<", got["html"])
        self.assertIn("충전 전력", got["html"])


class HourBoundaryTests(unittest.TestCase):
    def test_edges_meet_except_on_a_gap_or_a_charge(self) -> None:
        import ingest
        from process import KST, ensure_process_schema, format_ts
        from web.timeline import _hour_bounds, _hours

        def at(hour: int, minute: int) -> datetime:
            return datetime(2026, 11, 13, hour, minute, tzinfo=KST).astimezone(timezone.utc)

        d0 = datetime(2026, 11, 13, 0, 0, tzinfo=KST).astimezone(timezone.utc)
        level = [
            (at(10, 0), 80.0),
            (at(14, 5), 70.0),
            (at(15, 50), 50.0),
            (at(16, 10), 55.0),
            (at(16, 50), 60.0),
            (at(18, 50), 74.3),
            (at(18, 59), 73.4),
            (at(19, 1), 73.3),
            (at(19, 40), 72.2),
        ]
        h18 = _hour_bounds(level, at(18, 0), at(19, 0), False)
        h19 = _hour_bounds(level, at(19, 0), at(20, 0), False)
        self.assertIsNotNone(h18)
        self.assertIsNotNone(h19)
        assert h18 is not None and h19 is not None
        self.assertEqual(h18[1][1], h19[0][1])
        self.assertEqual(round(h18[1][1], 1), 73.4)
        self.assertEqual(round(h19[1][1], 1), 72.2)
        gap = _hour_bounds(level, at(14, 0), at(15, 0), False)
        assert gap is not None
        self.assertEqual(gap[0][1], 70.0)
        self.assertEqual(gap[1][1], 70.0)
        charging = _hour_bounds(level, at(16, 0), at(17, 0), True)
        assert charging is not None
        self.assertEqual(charging[0][1], 55.0)
        self.assertEqual(charging[1][1], 60.0)

        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "signals.db"
        rows = []
        for t, v in level:
            rows.append({
                "vehicle_id": "V1153", "timestamp": format_ts(t),
                "name": "BatteryLevel", "value": v,
            })
        rows.append({
            "vehicle_id": "V1153", "timestamp": format_ts(at(19, 40)),
            "name": "EnergyRemaining", "value": 60.0,
        })
        conn = ingest.connect(db)
        ingest.store_signals(conn, rows)
        conn.close()
        conn = sqlite3.connect(str(db))
        ensure_process_schema(conn)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?,?,?,?)",
            ("V1153", format_ts(at(16, 0)), format_ts(at(17, 0)), "AC"),
        )
        conn.commit()
        d1 = d0 + timedelta(days=1)
        hours = _hours(conn, "V1153", d0, d1, [], [])
        conn.close()
        by = {h["hour"]: h for h in hours}
        self.assertEqual(by[18]["end_pct"], by[19]["start_pct"])
        self.assertEqual(by[19]["end_pct"], 72.2)
        self.assertAlmostEqual(by[19]["delta_pct"], -1.2, places=2)
        self.assertAlmostEqual(by[19]["kwh"], round(1.2 * 60.0 / 72.2, 2))
        self.assertEqual(by[14]["start_pct"], 70.0)
        self.assertEqual(by[14]["end_pct"], 70.0)
        self.assertNotIn("delta_pct", by[14])
        self.assertEqual(by[16]["start_pct"], 55.0)
        self.assertEqual(by[16]["end_pct"], 60.0)


if __name__ == "__main__":
    unittest.main()
