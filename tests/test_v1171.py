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


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClimateFiveCasesTests(unittest.TestCase):
    def test_five_hvac_cases_match_the_confirmed_lines(self) -> None:
        self.assertIn('const SET_OFF = "#c2749e"', HTML)
        self.assertIn('const FAN_COLOR = "#5ec8f0"', HTML)
        self.assertNotIn("운전석 · A/C", HTML)
        self.assertNotIn('const SET_OFF = "#7d8794"', HTML)
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateRows")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("niceTicks") + _fn("tempAxis")
            + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("decimate") + _fn("flagAt") + _fn("crossLine") + _fn("chartSvg")
            + _fn("legendSwatch") + _fn("legendSpan")
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
            + "  const legend = climateLegend(m);\n"
            + "  const strokes = [];\n"
            + "  const re = /<polyline fill='none' stroke='([^']+)' stroke-width='[^']+'(?: stroke-dasharray='([^']*)')?/g;\n"
            + "  let hit;\n"
            + "  const dashed = [];\n"
            + "  while ((hit = re.exec(html))) { strokes.push(hit[1]); if (hit[2]) dashed.push(hit[1]); }\n"
            + "  const ticks = [];\n"
            + "  const tr = /text-anchor=\"start\" dominant-baseline=\"middle\">([^<]+)/g;\n"
            + "  while ((hit = tr.exec(html))) ticks.push(hit[1]);\n"
            + "  return {strokes: strokes, dashed: dashed, ticks: ticks, legend: legend,\n"
            + "    filled: html.indexOf(\"<polygon\") >= 0 || html.indexOf(\"fill='#12c9a0'\") >= 0 || html.indexOf(\"fill='#c2749e'\") >= 0};\n"
            + "}\n"
            + "const out = {}; Object.keys(cases).forEach(k => out[k] = view(cases[k]));\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        inside, outside = "#d5dee8", "#6ba8ef"
        green, pink, sky = "#12c9a0", "#c2749e", "#5ec8f0"

        def colors(name: str) -> set[str]:
            return set(got[name]["strokes"])

        self.assertEqual(colors("off"), {inside, outside})
        self.assertEqual(got["off"]["ticks"], [])
        self.assertNotIn("설정 온도", got["off"]["legend"])
        self.assertNotIn("팬 단계", got["off"]["legend"])

        self.assertEqual(colors("autoOn"), {inside, outside, green})
        self.assertNotIn(pink, colors("autoOn"))
        self.assertNotIn(sky, colors("autoOn"))
        self.assertEqual(got["autoOn"]["ticks"], [])
        self.assertIn("설정 온도: 운전석(실선)/조수석(점선)", got["autoOn"]["legend"])
        self.assertIn("A/C: 켬(초록)/끔(분홍)", got["autoOn"]["legend"])
        self.assertNotIn("팬 단계", got["autoOn"]["legend"])
        self.assertFalse(got["autoOn"]["dashed"])

        self.assertEqual(colors("autoOff"), {inside, outside, pink})
        self.assertNotIn(green, colors("autoOff"))
        self.assertNotIn(sky, colors("autoOff"))

        for name in ("manOn", "manOff"):
            self.assertIn(sky, colors(name))
            self.assertEqual(got[name]["ticks"], ["0", "2", "4", "6", "8", "10"])
            self.assertIn("팬 단계(하늘색, 수동일 때만)", got[name]["legend"])
            self.assertIn("실내", got[name]["legend"])
            self.assertIn("실외", got[name]["legend"])
            self.assertFalse(got[name]["filled"])
        self.assertIn(green, colors("manOn"))
        self.assertNotIn(pink, colors("manOn"))
        self.assertIn(pink, colors("manOff"))
        self.assertNotIn(green, colors("manOff"))
        self.assertIn(green, got["pass"]["dashed"])
        self.assertNotIn("운전석 · A/C", got["pass"]["legend"])
