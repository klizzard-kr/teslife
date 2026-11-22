"""v1.17.1: the wheel-heat auto bar reads the car's field name."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process.telemetry_delta import CLIMATE_FIELDS, apply_delta
from web.graphseries import build_climate

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
REAL = "HvacSteeringWheelHeatAuto"
OLD = "Steering" + "WheelHeatAuto"
START = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=20)


class WheelAutoNameTests(unittest.TestCase):
    def test_the_bar_comes_from_the_car_field(self) -> None:
        built = build_climate({REAL: [(START, True), (START + timedelta(minutes=5), False)]}, {}, START, END)
        self.assertIn("wheel_auto", built["bars"])
        self.assertGreater(built["bars"]["wheel_auto"][0][1], built["bars"]["wheel_auto"][0][0])
        missed = build_climate({OLD: [(START, True)]}, {}, START, END)
        self.assertNotIn("wheel_auto", missed["bars"])
        names = (ROOT / "web" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(f'"{REAL}"', names)
        self.assertNotIn(f'"{OLD}"', names)
        self.assertIn('["wheel_auto", "핸들 열선 자동"', HTML)

    def test_delta_does_not_add_the_wheel_auto_field(self) -> None:
        self.assertNotIn(REAL, CLIMATE_FIELDS)
        self.assertNotIn(OLD, CLIMATE_FIELDS)
        before = {"fields": {"VehicleSpeed": {"interval_seconds": 1}, REAL: {"interval_seconds": 10}}}
        after = apply_delta(before)
        self.assertEqual(after["fields"][REAL], {"interval_seconds": 10})
        self.assertNotIn(OLD, after["fields"])
        fresh = apply_delta({"fields": {"VehicleSpeed": {"interval_seconds": 1}}})
        self.assertNotIn(REAL, fresh["fields"])
        self.assertNotIn(OLD, fresh["fields"])

    def test_override_is_auto_and_front_vent_is_cooling(self) -> None:
        built = build_climate({
            "HvacAutoMode": [(START, "Override"), (START + timedelta(minutes=5), "Off")],
            "ClimateSeatCoolingFrontLeft": [(START, 2)],
            "SeatHeaterRearLeft": [(START, 3)],
            "AutoSeatClimateRight": [(START, True)],
        }, {}, START, END)
        self.assertEqual(built["auto"][0][1], 1.0)
        self.assertEqual(built["auto"][1][1], 0.0)
        self.assertIn("seat_left", built["cool"])
        self.assertNotIn("seat_rear_left", built["cool"])
        self.assertIn("seat_rear_left", built["heat"])
        self.assertIn("auto_seat_right", built["bars"])
        src = (ROOT / "web" / "graphseries.py").read_text(encoding="utf-8")
        self.assertNotIn("SeatHeaterRearCenter", src)
        self.assertNotIn("SeatHeaterRearCenter", (ROOT / "web" / "__init__.py").read_text(encoding="utf-8"))

    def test_defrost_normal_and_autodefog_are_on(self) -> None:
        for raw in ("Normal", "DefrostModeStateNormal", "AutoDefog", "DefrostModeStateAutoDefog", "Max"):
            built = build_climate({"DefrostMode": [(START, raw)]}, {}, START, END)
            self.assertIn("defrost", built["bars"], raw)
        for raw in ("Off", "DefrostModeStateOff", "Unknown"):
            built = build_climate({"DefrostMode": [(START, raw)]}, {}, START, END)
            self.assertNotIn("defrost", built["bars"], raw)

    def test_version(self) -> None:
        self.assertIn('const APP_VERSION = "v1.17.1"', HTML)


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ReverseSpeedTests(unittest.TestCase):
    def test_reverse_speed_is_drawn_below_zero_without_a_band(self) -> None:
        src = (
            "function tone(){ return '#5b9fd6'; }\n"
            "function fmt(n){ return String(Math.round(n * 10) / 10); }\n"
            "function fmtHm(){ return '00:00'; }\n"
            + _fn("niceTicks") + _fn("decimate") + _fn("timeMarks")
            + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("crossLine") + _fn("chartSvg") + _fn("svgLine")
            + "const t0 = Date.parse('2026-11-22T01:00:00Z');\n"
            + "const t1 = t0 + 60000;\n"
            + "const t2 = t0 + 120000;\n"
            + "const pts = [\n"
            + "  {ts: new Date(t0).toISOString(), v: 30},\n"
            + "  {ts: new Date(t1).toISOString(), v: 20},\n"
            + "  {ts: new Date(t2).toISOString(), v: 40}\n"
            + "];\n"
            + "const html = svgLine(pts, {h:150, color:'#5b9fd6', t0: pts[0].ts, t1: pts[2].ts,\n"
            + "  reverse: [{t0: t1 - 1000, t1: t1 + 1000}]});\n"
            + "function ys(color){\n"
            + "  const re = new RegExp('<polyline[^>]*stroke=\"' + color + '\"[^>]*points=\"([^\"]+)\"', 'g');\n"
            + "  const out = []; let m;\n"
            + "  while ((m = re.exec(html))) m[1].split(' ').forEach(p => out.push(Number(p.split(',')[1])));\n"
            + "  return out;\n"
            + "}\n"
            + "function fillYs(color){\n"
            + "  const re = new RegExp('<polygon points=\"([^\"]+)\" fill=\"' + color + '\"', 'g');\n"
            + "  const out = []; let m;\n"
            + "  while ((m = re.exec(html))) m[1].split(' ').forEach(p => out.push(Number(p.split(',')[1])));\n"
            + "  return out;\n"
            + "}\n"
            + "const y0 = Number(html.match(/y=\"([^\"]+)\"[^>]*>0</)[1]);\n"
            + "const plain = svgLine(pts, {h:150, color:'#5b9fd6', t0: pts[0].ts, t1: pts[2].ts});\n"
            + "const regen = svgLine([{ts:pts[0].ts,v:4},{ts:pts[1].ts,v:-3},{ts:pts[2].ts,v:2}], {h:150, color:'#5b9fd6'});\n"
            + "const labels = [];\n"
            + "const re = />([-0-9.]+)</g; let m;\n"
            + "while ((m = re.exec(html))) labels.push(Number(m[1]));\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  labels: labels, y0: y0,\n"
            + "  forward: ys('#5b9fd6'),\n"
            + "  reverse: ys('#e07a5f'),\n"
            + "  reverseFill: fillYs('#e07a5f'),\n"
            + "  forwardFill: fillYs('#5b9fd6'),\n"
            + "  band: html.indexOf('fill=\\\"#e07a5f\\\" fill-opacity=\\\"0.16\\\"') >= 0,\n"
            + "  plainHasReverse: plain.indexOf('#e07a5f') >= 0,\n"
            + "  regenHasReverse: regen.indexOf('#e07a5f') >= 0\n"
            + "}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        y0 = got["y0"]
        self.assertFalse(got["band"])
        self.assertFalse(got["plainHasReverse"])
        self.assertFalse(got["regenHasReverse"])
        self.assertTrue(any(n < 0 for n in got["labels"]))
        self.assertTrue(0 in got["labels"] or any(abs(n) < 1e-6 for n in got["labels"]))
        self.assertTrue(got["reverse"])
        self.assertTrue(all(y >= y0 - 0.6 for y in got["reverse"]))
        self.assertTrue(any(y > y0 + 1 for y in got["reverse"]))
        self.assertTrue(all(y <= y0 + 0.6 for y in got["forward"]))
        self.assertTrue(any(y < y0 - 1 for y in got["forward"]))
        self.assertTrue(any(abs(y - y0) < 0.6 for y in got["reverse"]))
        self.assertTrue(any(abs(y - y0) < 0.6 for y in got["forward"]))
        self.assertTrue(got["reverseFill"])
        self.assertTrue(all(y >= y0 - 0.6 for y in got["reverseFill"]))
        self.assertTrue(all(y <= y0 + 0.6 for y in got["forwardFill"]))

    def test_regen_is_only_below_zero_and_not_the_reverse_color(self) -> None:
        power = _fn("loadTripPower")
        self.assertIn('unit: "kW"', power)
        self.assertNotIn("−는 회생", power)
        self.assertIn(', "전력")', power)
        self.assertIn(', "회생")', power)
        self.assertIn("below: regenColor", power)
        self.assertIn('tone("chart-charge")', power)
        src = (
            "function tone(){ return '#5b9fd6'; }\n"
            "function fmt(n){ return String(Math.round(n * 10) / 10); }\n"
            "function fmtHm(){ return '00:00'; }\n"
            + _fn("niceTicks") + _fn("decimate") + _fn("timeMarks")
            + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("crossLine") + _fn("chartSvg") + _fn("svgLine")
            + "const t0 = Date.parse('2026-11-22T01:00:00Z');\n"
            + "const pts = [\n"
            + "  {ts: new Date(t0).toISOString(), v: 12},\n"
            + "  {ts: new Date(t0 + 60000).toISOString(), v: -8},\n"
            + "  {ts: new Date(t0 + 120000).toISOString(), v: 20}\n"
            + "];\n"
            + "const html = svgLine(pts, {h:150, color:'#5b9fd6', below:'#3dd68c', unit:'kW'});\n"
            + "function ys(color){\n"
            + "  const re = new RegExp('<polyline[^>]*stroke=\"' + color + '\"[^>]*points=\"([^\"]+)\"', 'g');\n"
            + "  const out = []; let m;\n"
            + "  while ((m = re.exec(html))) m[1].split(' ').forEach(p => out.push(Number(p.split(',')[1])));\n"
            + "  return out;\n"
            + "}\n"
            + "const y0 = Number(html.match(/y=\"([^\"]+)\"[^>]*>0</)[1]);\n"
            + "process.stdout.write(JSON.stringify({y0:y0, use:ys('#5b9fd6'), regen:ys('#3dd68c'), orange:html.indexOf('#e07a5f')>=0, unit:html.indexOf('>kW<')>=0}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        y0 = got["y0"]
        self.assertFalse(got["orange"])
        self.assertTrue(got["unit"])
        self.assertTrue(got["regen"])
        self.assertTrue(all(y >= y0 - 0.6 for y in got["regen"]))
        self.assertTrue(any(y > y0 + 1 for y in got["regen"]))
        self.assertTrue(all(y <= y0 + 0.6 for y in got["use"]))
        self.assertTrue(any(y < y0 - 1 for y in got["use"]))
        self.assertTrue(any(abs(y - y0) < 0.6 for y in got["regen"]))
        self.assertTrue(any(abs(y - y0) < 0.6 for y in got["use"]))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClimateFiveCasesTests(unittest.TestCase):
    def test_five_hvac_cases_match_the_confirmed_lines(self) -> None:
        self.assertIn('const SET_OFF = "#c2749e"', HTML)
        self.assertIn('const FAN_COLOR = "#3d9ccc"', HTML)
        self.assertNotIn("운전석 · A/C", HTML)
        self.assertNotIn('const SET_OFF = "#7d8794"', HTML)
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateRows")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("niceTicks") + _fn("tempAxis")
            + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("decimate") + _fn("flagAt") + _fn("crossLine") + _fn("chartSvg")
            + _fn("legendSwatch") + _fn("legendSpan") + _fn("escHtml") + _fn("lanePlotHtml")
            + _fn("climateLaneHtml")
            + chunk
            + "const t0 = Date.parse('2026-11-22T01:00:00Z');\n"
            + "const t1 = t0 + 20*60*1000;\n"
            + "const temps = {inside:[[{t:t0,v:21},{t:t1,v:22}]], outside:[[{t:t0,v:8},{t:t1,v:9}]]};\n"
            + "const set = [[{t:t0,v:22},{t:t1,v:22}]];\n"
            + "const fan = [[{t:t0,v:6},{t:t1,v:6}]];\n"
            + "function model(extra){\n"
            + "  return Object.assign({t0:t0, t1:t1, setDriver:[], setPass:[], fan:[], wheel:[],\n"
            + "    heat:{}, cool:{}, auto:[], ac:[], bars:{}}, temps, extra);\n"
            + "}\n"
            + "const cases = {\n"
            + "  off: model({}),\n"
            + "  autoOn: model({setDriver:set, auto:[{t:t0,v:1}], ac:[{t:t0,v:1}], fan:fan}),\n"
            + "  autoOff: model({setDriver:set, auto:[{t:t0,v:1}], ac:[{t:t0,v:0}], fan:fan}),\n"
            + "  manOn: model({setDriver:set, auto:[{t:t0,v:0}], ac:[{t:t0,v:1}], fan:fan}),\n"
            + "  manOff: model({setDriver:set, auto:[{t:t0,v:0}], ac:[{t:t0,v:0}], fan:fan}),\n"
            + "  pass: model({setDriver:set, setPass:[[{t:t0,v:19},{t:t1,v:19}]], auto:[{t:t0,v:0}], ac:[{t:t0,v:1}], fan:fan})\n"
            + "};\n"
            + "function view(m){\n"
            + "  const html = climateSvg(m);\n"
            + "  const lanes = climateLaneHtml(m);\n"
            + "  const legend = climateLegend(m);\n"
            + "  const strokes = [];\n"
            + "  const re = /<polyline fill='none' stroke='([^']+)' stroke-width='[^']+'(?: stroke-dasharray='([^']*)')?/g;\n"
            + "  let hit;\n"
            + "  const dashed = [];\n"
            + "  while ((hit = re.exec(html))) { strokes.push(hit[1]); if (hit[2]) dashed.push(hit[1]); }\n"
            + "  const ticks = [];\n"
            + "  const tr = /text-anchor=\"start\" dominant-baseline=\"middle\">([^<]+)/g;\n"
            + "  while ((hit = tr.exec(html))) ticks.push(hit[1]);\n"
            + "  const labels = [];\n"
            + "  const lr = /tl-bar-txt'>([^<]+)/g;\n"
            + "  while ((hit = lr.exec(lanes))) labels.push(hit[1]);\n"
            + "  return {strokes: strokes, dashed: dashed, ticks: ticks, legend: legend, labels: labels,\n"
            + "    hatch: lanes.indexOf('tl-hatch') >= 0,\n"
            + "    rows: (lanes.match(/tl-lab/g) || []).length,\n"
            + "    rightAxis: html.indexOf('text-anchor=\"start\" dominant-baseline') >= 0,\n"
            + "    filled: html.indexOf(\"<polygon\") >= 0 || html.indexOf(\"fill='#12c9a0'\") >= 0 || html.indexOf(\"fill='#c2749e'\") >= 0};\n"
            + "}\n"
            + "const out = {}; Object.keys(cases).forEach(k => out[k] = view(cases[k]));\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        inside, outside = "#d5dee8", "#6ba8ef"
        green, pink = "#12c9a0", "#c2749e"

        def colors(name: str) -> set[str]:
            return set(got[name]["strokes"])

        self.assertEqual(colors("off"), {inside, outside})
        self.assertEqual(got["off"]["labels"], [])
        self.assertFalse(got["off"]["rightAxis"])
        self.assertNotIn("설정 온도", got["off"]["legend"])
        self.assertNotIn(">팬<", got["off"]["legend"])

        self.assertEqual(colors("autoOn"), {inside, outside, green})
        self.assertNotIn(pink, colors("autoOn"))
        self.assertFalse(got["autoOn"]["hatch"])
        self.assertEqual(got["autoOn"]["labels"], [])
        self.assertFalse(got["autoOn"]["rightAxis"])
        self.assertIn("설정 온도", got["autoOn"]["legend"])
        self.assertIn("운전석", got["autoOn"]["legend"])
        self.assertIn("조수석", got["autoOn"]["legend"])
        self.assertIn("켜짐", got["autoOn"]["legend"])
        self.assertIn("꺼짐", got["autoOn"]["legend"])
        self.assertIn("#12c9a0", got["autoOn"]["legend"])
        self.assertIn("#c2749e", got["autoOn"]["legend"])
        self.assertNotIn("(실선)", got["autoOn"]["legend"])
        self.assertNotIn(">팬<", got["autoOn"]["legend"])
        self.assertNotIn("시트 열선", got["autoOn"]["legend"])
        self.assertNotIn("좌석", got["autoOn"]["legend"])
        self.assertNotIn("유리", got["autoOn"]["legend"])
        self.assertIn("오토", got["autoOn"]["legend"])
        self.assertIn("수동", got["autoOn"]["legend"])
        self.assertFalse(got["autoOn"]["dashed"])

        self.assertEqual(colors("autoOff"), {inside, outside, pink})
        self.assertNotIn(green, colors("autoOff"))
        self.assertEqual(got["autoOff"]["labels"], [])

        for name in ("manOn", "manOff"):
            self.assertEqual(got[name]["labels"], [])
            self.assertFalse(got[name]["hatch"])
            self.assertFalse(got[name]["rightAxis"])
            self.assertNotIn(">팬<", got[name]["legend"])
            self.assertIn("설정 온도", got[name]["legend"])
            self.assertIn("오토", got[name]["legend"])
            self.assertIn("수동", got[name]["legend"])
            self.assertIn("실내", got[name]["legend"])
            self.assertIn("실외", got[name]["legend"])
            self.assertFalse(got[name]["filled"])
        self.assertIn(green, colors("manOn"))
        self.assertNotIn(pink, colors("manOn"))
        self.assertIn(pink, colors("manOff"))
        self.assertNotIn(green, colors("manOff"))
        self.assertIn(green, got["pass"]["dashed"])
        self.assertNotIn("운전석 · A/C", got["pass"]["legend"])

    def test_seat_row_combines_driver_and_passenger(self) -> None:
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateRows")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + "function known(v){ return v !== null && v !== undefined && v !== '' && isFinite(Number(v)); }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("fmtMin") + _fn("escHtml")
            + _fn("tlSpan") + _fn("popCard") + _fn("climateTip")
            + _fn("niceTicks") + _fn("tempAxis") + _fn("timeMarks")
            + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("decimate") + _fn("flagAt") + _fn("crossLine") + _fn("chartSvg")
            + _fn("legendSwatch") + _fn("legendSpan") + _fn("lanePlotHtml")
            + _fn("climateLaneHtml")
            + chunk
            + "const t0 = Date.parse('2026-11-22T01:00:00Z');\n"
            + "const mid = t0 + 8*60*1000;\n"
            + "const late = t0 + 14*60*1000;\n"
            + "const t1 = t0 + 20*60*1000;\n"
            + "const model = {t0:t0, t1:t1, inside:[], outside:[], setDriver:[], setPass:[], fan:[],\n"
            + "  wheel:[[{t:t0,v:2},{t:late,v:0}]],\n"
            + "  heat:{seat_left:[[{t:t0,v:3},{t:mid,v:0}]],\n"
            + "        seat_right:[[{t:t0,v:1},{t:late,v:0}]],\n"
            + "        seat_rear_left:[[{t:t0,v:2},{t:t1,v:2}]]},\n"
            + "  cool:{seat_left:[[{t:t0,v:2},{t:t1,v:2}]]},\n"
            + "  auto:[], ac:[], bars:{defrost:[{t0:t0,t1:mid,level:1}], wheel_auto:[{t0:late,t1:t1,level:1}],\n"
            + "       auto_seat_left:[{t0:t0,t1:mid,level:1}]}};\n"
            + "const rows = climateRuns(model);\n"
            + "const legend = climateLegend(model);\n"
            + "const lanes = climateLaneHtml(model, rows);\n"
            + "const seat = rows.filter(r => r.label === '좌석')[0];\n"
            + "const glass = rows.filter(r => r.label === '유리')[0];\n"
            + "const fanModel = {t0:t0, t1:t1, inside:[], outside:[], setDriver:[], setPass:[],\n"
            + "  fan:[[{t:t0,v:6},{t:mid,v:3},{t:late,v:3}]], wheel:[], heat:{}, cool:{},\n"
            + "  auto:[{t:t0,v:0},{t:late,v:1}], ac:[{t:t0,v:1},{t:mid,v:0}], bars:{}};\n"
            + "const fanRows = climateRuns(fanModel);\n"
            + "const park = popCard('주차 · 캠핑', '#f0b429', [['시간','10:02–10:14 (12분)'],['사용량','약 0.42 kWh (2.1%)']]);\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  labels: rows.map(r => r.label),\n"
            + "  seatBars: seat.spans.length,\n"
            + "  seatColors: [...new Set(seat.spans.map(s => s.color))],\n"
            + "  tip: climateTip(seat.spans[0]),\n"
            + "  glassTip: climateTip(glass.spans[0]),\n"
            + "  fanBars: fanRows[0].spans.length,\n"
            + "  fanTips: fanRows[0].spans.map(s => climateTip(s)).join('\\n'),\n"
            + "  legend: legend,\n"
            + "  hatch: lanes.indexOf('tl-hatch') >= 0,\n"
            + "  text: lanes.indexOf('tl-bar-txt') >= 0,\n"
            + "  labs: (lanes.match(/<span>[^<]+<\\/span>/g) || []),\n"
            + "  park: park\n"
            + "}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["labels"], ["공조", "좌석", "유리"])
        self.assertGreaterEqual(got["seatBars"], 2)
        self.assertEqual(got["seatColors"], ["#e39a4a"])
        self.assertIn(">좌석<", got["tip"])
        self.assertIn(">시간<", got["tip"])
        self.assertIn("운전석 열선 3단", got["tip"])
        self.assertIn("운전석 통풍 2단", got["tip"])
        self.assertIn("운전석 오토", got["tip"])
        self.assertIn("조수석 열선 1단", got["tip"])
        self.assertIn("핸들 열선 2단", got["tip"])
        self.assertNotIn("꺼짐", got["tip"])
        self.assertIn("뒷좌석 왼쪽 열선 2단", got["tip"])
        self.assertNotIn("뒷좌석 가운데", got["tip"])
        self.assertNotIn("뒷좌석 오른쪽", got["tip"])
        self.assertNotIn("seat_rear_center", HTML)
        self.assertIn("<ul>", got["tip"])
        self.assertIn("<h3>", got["park"])
        self.assertIn("<dl>", got["park"])
        self.assertNotIn("<ul>", got["park"])
        self.assertIn("<h3>", got["tip"])
        self.assertIn("<dl>", got["tip"])
        self.assertEqual(got["fanBars"], 3)
        self.assertIn("팬 6단 · A/C", got["fanTips"])
        self.assertIn("팬 3단", got["fanTips"])
        self.assertNotIn("팬 3단 · A/C", got["fanTips"])
        self.assertIn("오토</li>", got["fanTips"])
        self.assertNotIn("오토 · A/C", got["fanTips"])
        self.assertNotIn("A/C 꺼짐", got["fanTips"])
        self.assertNotIn("켜짐", got["fanTips"])
        self.assertIn(">공조<", got["fanTips"])
        for foreign in ("실내", "실외", "설정", "좌석", "핸들", "°C", "유리"):
            self.assertNotIn(foreign, got["fanTips"])
        for foreign in ("실내", "실외", "설정", ">공조<", "°C", "유리", "팬 "):
            self.assertNotIn(foreign, got["tip"])
        self.assertIn("앞 김서림 제거", got["glassTip"])
        self.assertNotIn("꺼짐", got["glassTip"])
        self.assertNotIn("뒷유리", got["glassTip"])
        self.assertNotIn("와이퍼", got["glassTip"])
        self.assertNotIn("시트 열선", got["legend"])
        self.assertNotIn("핸들 열선", got["legend"])
        self.assertNotIn(">팬<", got["legend"])
        self.assertNotIn("뒷좌석", got["legend"])
        self.assertNotIn("통풍", got["legend"])
        self.assertFalse(got["hatch"])
        self.assertFalse(got["text"])
        self.assertIn(">공조<", "".join(got["labs"]))
        self.assertIn(">좌석<", "".join(got["labs"]))
        self.assertIn(">유리<", "".join(got["labs"]))
        placed = HTML[HTML.index("function placePop("):HTML.index("function showTlPop(")]
        self.assertIn("window.innerWidth", placed)
        self.assertIn("popCard(", HTML[HTML.index("function barPopHtml"):HTML.index("function hourPopHtml")])

    def test_temperature_touch_reads_climate_at_that_moment(self) -> None:
        svg = _fn("climateSvg")
        self.assertIn("crossLine(", svg)
        self.assertIn("class='cross-hit'", svg)
        self.assertIn("bindCrosshair(plot", _fn("mountClimate"))
        self.assertIn("hideCross(", _fn("bindClimateSelect"))
        place = HTML[HTML.index("function placeCross("):HTML.index("function bindCrosshair(")]
        self.assertIn("window.innerWidth", place)
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateRows")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("seriesValueAt") + _fn("flagAt")
            + chunk + _fn("climateRows")
            + "const t0 = Date.parse('2026-11-22T01:00:00Z');\n"
            + "const mid = t0 + 8*60*1000;\n"
            + "const t1 = t0 + 20*60*1000;\n"
            + "const model = {t0:t0, t1:t1,\n"
            + "  inside:[[{t:t0,v:21},{t:t1,v:23}]], outside:[[{t:t0,v:8},{t:t1,v:9}]],\n"
            + "  setDriver:[[{t:t0,v:22},{t:t1,v:22}]], setPass:[[{t:t0,v:19.5},{t:t1,v:19.5}]],\n"
            + "  fan:[[{t:t0,v:6},{t:mid,v:0},{t:t1,v:0}]],\n"
            + "  wheel:[[{t:t0,v:2},{t:mid,v:0}]],\n"
            + "  heat:{seat_left:[[{t:t0,v:3},{t:mid,v:0}]], seat_right:[[{t:t0,v:0}]]},\n"
            + "  auto:[{t:t0,v:0},{t:mid,v:1}], ac:[{t:t0,v:1},{t:mid,v:0}],\n"
            + "  bars:{wheel_auto:[{t0:mid,t1:t1}]}};\n"
            + "const early = climateRows(model, t0 + 60*1000);\n"
            + "const late = climateRows(model, mid + 60*1000);\n"
            + "const text = (rows, label) => (rows.find(r => r.label === label) || {}).text || '';\n"
            + "const sub = (rows, label) => rows.find(r => r.label === label) || {};\n"
            + "process.stdout.write(JSON.stringify({early:early, late:late,\n"
            + "  eSet:text(early,'설정 온도'), eDriver:text(early,'운전석'), ePass:text(early,'조수석'),\n"
            + "  eIndent:!!sub(early,'운전석').indent, eAc:text(early,'A/C'),\n"
            + "  lSet:text(late,'설정 온도'), lAc:text(late,'A/C')}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["eSet"], "")
        self.assertEqual(got["eDriver"], "22°C")
        self.assertEqual(got["ePass"], "19.5°C")
        self.assertTrue(got["eIndent"])
        self.assertNotIn("팬", "".join(r["label"] for r in got["early"]))
        self.assertEqual(got["eAc"], "켜짐")
        self.assertEqual([r["label"] for r in got["early"]], ["실내 온도", "실외 온도", "설정 온도", "운전석", "조수석", "A/C"])
        self.assertEqual([r["label"] for r in got["late"]], ["실내 온도", "실외 온도", "설정 온도", "운전석", "조수석"])
        self.assertNotIn("A/C", [r["label"] for r in got["late"]])
        self.assertEqual(got["lSet"], "")
        self.assertEqual(got["lAc"], "")
