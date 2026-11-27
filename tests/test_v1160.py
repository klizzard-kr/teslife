"""v1.16.0: climate and battery graph series, charge axis, two-decimal battery."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from web.graphseries import (
    build_battery,
    build_climate,
    charge_axis_top,
    flat_average,
    gate_fan,
    series_segments,
)
from web.notice_view import format_soc_change

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
T0 = datetime(2026, 11, 19, 8, 49, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class StreamFillTests(unittest.TestCase):
    def test_start_uses_the_last_value_from_before(self) -> None:
        segs = series_segments(
            [(at(10), 21.0), (at(20), 21.4)],
            T0,
            (at(-8), 20.6),
        )
        self.assertEqual(segs, [[(T0, 20.6), (at(10), 21.0), (at(20), 21.4)]])

    def test_an_old_value_still_fills_the_start(self) -> None:
        segs = series_segments(
            [(at(300), 18.0), (at(310), 18.2)],
            T0,
            (at(-3 * 3600), 14.0),
        )
        self.assertEqual(segs, [[(T0, 14.0), (at(300), 18.0), (at(310), 18.2)]])

    def test_a_long_quiet_stretch_stays_one_line(self) -> None:
        segs = series_segments(
            [(at(0), 10.0), (at(20 * 60), 12.0)],
            T0,
            None,
        )
        self.assertEqual(segs, [[(at(0), 10.0), (at(20 * 60), 12.0)]])

    def test_no_prior_starts_at_the_first_report(self) -> None:
        segs = series_segments([(at(10), 19.0)], T0, None)
        self.assertEqual(segs, [[(at(10), 19.0)]])

    def test_one_value_is_flat_across_the_window(self) -> None:
        segs = series_segments([(at(10), 19.0)], T0, None, at(100))
        self.assertEqual(segs, [[(T0, 19.0), (at(100), 19.0)]])

    def test_the_last_value_holds_flat_to_the_end(self) -> None:
        segs = series_segments(
            [(at(10), 21.0), (at(20), 21.4)],
            T0,
            (at(-8), 20.6),
            at(100),
        )
        self.assertEqual(
            segs,
            [[(T0, 20.6), (at(10), 21.0), (at(20), 21.4), (at(100), 21.4)]],
        )


class ClimateSeriesTests(unittest.TestCase):
    def _built(self, series, priors):
        return build_climate(series, priors, T0, at(26 * 60))

    def test_setpoint_is_only_while_hvac_is_on_and_passenger_hides_when_equal(self) -> None:
        series = {
            "HvacPower": [(at(0), "On"), (at(600), "Off")],
            "HvacLeftTemperatureRequest": [(at(0), 22.0), (at(300), 22.0)],
            "HvacRightTemperatureRequest": [(at(0), 22.0)],
            "InsideTemp": [(at(0), 18.0), (at(600), 21.0)],
        }
        built = self._built(series, {})
        self.assertTrue(built["set_driver"])
        self.assertFalse(built["set_passenger"])
        flat = [ts for seg in built["set_driver"] for ts, _v in seg]
        self.assertLessEqual(max(flat), at(600))
        self.assertGreaterEqual(min(flat), T0)

    def test_passenger_setpoint_stays_when_it_differs(self) -> None:
        series = {
            "HvacPower": [(at(0), "On"), (at(600), "Off")],
            "HvacLeftTemperatureRequest": [(at(0), 22.0)],
            "HvacRightTemperatureRequest": [(at(0), 20.0)],
        }
        built = self._built(series, {})
        self.assertTrue(built["set_passenger"])
        self.assertEqual(built["set_passenger"][0][0][1], 20.0)

    def test_unused_seat_is_omitted_and_used_seat_keeps_its_level(self) -> None:
        series = {
            "SeatHeaterLeft": [(at(0), 0), (at(60), 2), (at(400), 0)],
            "SeatHeaterRight": [(at(0), 0)],
            "HvacSteeringWheelHeatLevel": [(at(30), 1), (at(200), 0)],
        }
        built = self._built(series, {})
        self.assertIn("seat_left", built["heat"])
        self.assertNotIn("seat_right", built["heat"])
        self.assertNotIn("seat_left", built["bars"])
        vals = [v for seg in built["heat"]["seat_left"] for _t, v in seg]
        self.assertEqual(vals[:3], [0.0, 2.0, 0.0])
        self.assertEqual(vals[-1], 0.0)
        self.assertEqual(built["heat"]["seat_left"][0][-1][0], at(26 * 60))
        wheel = [v for seg in built["wheel"] for _t, v in seg]
        self.assertIn(1.0, wheel)
        self.assertEqual(wheel[-1], 0.0)

    def test_seat_cooling_is_a_level_line(self) -> None:
        built = self._built({"SeatHeaterLeft": [(at(10), -3), (at(40), -1), (at(80), 0)]}, {})
        self.assertNotIn("seat_left", built["heat"])
        self.assertNotIn("cool_seat_left", built["bars"])
        vals = [v for seg in built["cool"]["seat_left"] for _t, v in seg]
        self.assertEqual(vals[0], 3.0)
        self.assertIn(1.0, vals)
        self.assertEqual(vals[-1], 0.0)

    def test_one_inside_reading_spans_the_trip(self) -> None:
        built = self._built({}, {"InsideTemp": (at(-5), 19.5)})
        self.assertEqual(built["inside"], [[(T0, 19.5), (at(26 * 60), 19.5)]])

    def test_fan_auto_and_ac_flags_follow_the_samples(self) -> None:
        series = {
            "HvacFanSpeed": [(at(0), 3), (at(120), 6), (at(240), 0)],
            "HvacAutoMode": [(at(0), "On"), (at(120), "Off")],
            "HvacACEnabled": [(at(120), True)],
        }
        built = self._built(series, {})
        self.assertEqual([v for seg in built["fan"] for _t, v in seg], [3.0, 6.0, 0.0, 0.0])
        self.assertEqual(built["fan"][0][-1][0], at(26 * 60))
        self.assertEqual(built["auto"][0][1], 1.0)
        self.assertEqual(built["auto"][-1][1], 0.0)
        self.assertEqual(built["ac"][-1][1], 1.0)

    def test_cabin_average_holds_the_last_report(self) -> None:
        built = self._built({"InsideTemp": [(at(0), 10.0), (at(600), 20.0)]}, {})
        self.assertAlmostEqual(built["avg_inside"], (10 * 600 + 20 * (26 * 60 - 600)) / (26 * 60))
        self.assertIsNone(built["avg_outside"])

    def test_fan_stays_zero_while_hvac_is_off(self) -> None:
        built = self._built({
            "HvacPower": [(at(0), "Off")],
            "HvacFanSpeed": [(at(0), 1), (at(60), 1)],
            "HvacAutoMode": [(at(0), "Off")],
            "HvacACEnabled": [(at(0), False)],
        }, {"HvacPower": (at(-9000), "Off"), "HvacFanSpeed": (at(-8000), 1)})
        vals = [v for seg in built["fan"] for _t, v in seg]
        self.assertTrue(vals)
        self.assertTrue(all(v == 0.0 for v in vals))
        self.assertFalse(built["set_driver"])

    def test_fan_keeps_the_speed_from_before_power_turns_on(self) -> None:
        built = self._built({
            "HvacPower": [(at(0), "Off"), (at(300), "On")],
            "HvacAutoMode": [(at(300), "On")],
            "HvacACEnabled": [(at(400), True), (at(500), False)],
        }, {"HvacPower": (at(-9000), "Off"), "HvacFanSpeed": (at(-8000), 1)})
        vals = [(ts, v) for seg in built["fan"] for ts, v in seg]
        self.assertTrue(vals)
        self.assertTrue(all(v == 0.0 for ts, v in vals if ts < at(300)))
        self.assertTrue(all(v == 1.0 for ts, v in vals if ts >= at(300)))
        self.assertEqual(built["auto"][-1][1], 1.0)
        self.assertTrue(any(v >= 0.5 for _ts, v in built["ac"]))
        self.assertEqual(built["ac"][-1][1], 0.0)

    def test_fan_uses_a_speed_received_while_power_is_off(self) -> None:
        built = self._built({
            "HvacPower": [(at(0), "Off"), (at(200), "On")],
            "HvacFanSpeed": [(at(40), 4)],
        }, {"HvacPower": (at(-10), "Off")})
        vals = [(ts, v) for seg in built["fan"] for ts, v in seg]
        self.assertTrue(all(v == 0.0 for ts, v in vals if ts < at(200)))
        self.assertTrue(all(v == 4.0 for ts, v in vals if ts >= at(200)))

    def test_fan_is_zero_after_power_turns_off(self) -> None:
        built = self._built({
            "HvacPower": [(at(0), "On"), (at(180), "Off")],
            "HvacFanSpeed": [(at(0), 2)],
        }, {})
        vals = [(ts, v) for seg in built["fan"] for ts, v in seg]
        self.assertTrue(any(v == 2.0 for _ts, v in vals))
        self.assertTrue(all(v == 0.0 for ts, v in vals if ts >= at(180)))
        self.assertEqual(vals[-1], (at(26 * 60), 0.0))

    def test_auto_or_ac_without_a_fan_sample_keeps_the_flags(self) -> None:
        built = self._built({
            "HvacPower": [(at(0), "Off"), (at(60), "On")],
            "HvacAutoMode": [(at(60), "On")],
            "HvacACEnabled": [(at(60), True)],
        }, {})
        self.assertTrue(built["fan"])
        self.assertTrue(all(v == 0.0 for seg in built["fan"] for _ts, v in seg))
        self.assertEqual(built["auto"][-1][1], 1.0)
        self.assertEqual(built["ac"][-1][1], 1.0)


class BatterySeriesTests(unittest.TestCase):
    def test_min_above_max_is_kept(self) -> None:
        series = {
            "ModuleTempMin": [(at(0), 17.0), (at(600), 18.0)],
            "ModuleTempMax": [(at(0), 16.0), (at(600), 16.5)],
            "BatteryHeaterOn": [(at(30), True), (at(200), False)],
        }
        built = build_battery(series, {}, T0, at(900))
        self.assertEqual(built["min"][0][0][1], 17.0)
        self.assertEqual(built["max"][0][0][1], 16.0)
        self.assertGreater(built["min"][0][0][1], built["max"][0][0][1])
        self.assertEqual(built["heater"], [(at(30), at(200), 1.0)])
        self.assertEqual(built["low"], 17.0)
        self.assertEqual(built["high"], 16.5)

    def test_flat_average_holds_each_report(self) -> None:
        end = at(40)
        runs = [[(T0, 10.0), (at(10), 20.0), (end, 20.0)]]
        self.assertAlmostEqual(flat_average(runs, end), 17.5)
        self.assertEqual(flat_average([[(T0, 19.5), (at(100), 19.5)]], at(100)), 19.5)
        self.assertIsNone(flat_average([], at(10)))

    def test_no_temperature_samples_leave_the_lines_empty(self) -> None:
        built = build_battery({"BatteryHeaterOn": [(at(0), True)]}, {}, T0, at(60))
        self.assertEqual(built["min"], [])
        self.assertEqual(built["max"], [])


class ChargeAxisTests(unittest.TestCase):
    def test_rated_output_is_always_the_top(self) -> None:
        self.assertEqual(charge_axis_top(7.0, 6.6), 7.0)
        self.assertEqual(charge_axis_top(50.0, 47.0), 50.0)
        self.assertEqual(charge_axis_top(350.0, 80.0), 350.0)
        self.assertEqual(charge_axis_top(7.0, 7.4), 7.0)
        self.assertEqual(charge_axis_top(11.0, 0), 11.0)

    def test_missing_charger_uses_the_observed_peak(self) -> None:
        self.assertEqual(charge_axis_top(None, 11.2), 11.2)
        self.assertEqual(charge_axis_top(0, 11.2), 11.2)
        self.assertEqual(charge_axis_top(None, None), 1.0)


class SocDisplayTests(unittest.TestCase):
    def test_detail_battery_uses_two_decimals_and_equal_shown_values_collapse(self) -> None:
        self.assertEqual(format_soc_change(64.6, 63.4), "64.6% → 63.4%")
        self.assertEqual(format_soc_change(65.0, 63.4), "65% → 63.4%")
        self.assertEqual(format_soc_change(65.004, 65.001), "65%")
        self.assertEqual(format_soc_change(80.04, 79.96), "80.04% → 79.96%")
        self.assertEqual(format_soc_change(64.6, 64.6), "64.6%")
        self.assertNotIn("%p", format_soc_change(64.6, 63.4))


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PageGraphTests(unittest.TestCase):
    def test_labels_tabs_and_crosshair_hooks(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.0"', HTML)
        self.assertNotIn("function climateTabs(", HTML)
        self.assertNotIn("<strong>공조</strong>", _fn("mountClimate"))
        self.assertIn("climateSvg(model)", _fn("mountClimate"))
        self.assertIn("climateLaneHtml(model, runs)", _fn("mountClimate"))
        self.assertIn("lanePlotHtml(", _fn("climateLaneHtml"))
        i18n = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
        self.assertIn('"온도": "Temp"', i18n)
        self.assertIn('"시트·핸들": "Seats and wheel"', i18n)
        self.assertIn('"유리·와이퍼": "Glass and wipers"', i18n)
        self.assertIn('"A/C 켬": "A/C on"', i18n)
        self.assertIn('"성에 제거": "Defrost"', i18n)
        fan = _fn("fanPieces")
        self.assertIn("flagAt(model.auto", fan)
        self.assertIn("fanBlue(level)", fan)
        legend = _fn("climateLegend")
        self.assertNotIn("켜짐", legend)
        self.assertNotIn("꺼짐", legend)
        self.assertNotIn(">A/C<", legend)
        self.assertNotIn("오토", legend)
        self.assertNotIn("수동", legend)
        self.assertNotIn("function climateBarLegend(", HTML)
        mount = _fn("mountClimate")
        self.assertLess(mount.index("clim-legend-temp"), mount.index("clim-band"))
        self.assertNotIn("clim-legend-bars", mount)
        hvac = _fn("hvacPieces")
        self.assertIn("AUTO_COLOR", hvac)
        self.assertIn("FAN_COLOR", hvac)
        self.assertIn("SET_COLOR", legend)
        self.assertIn("설정 온도", legend)
        self.assertIn("운전석", legend)
        self.assertIn("조수석", legend)
        self.assertNotIn("(실선)", legend)
        self.assertNotIn("(점선)", legend)
        self.assertNotIn("켬(초록)", legend)
        self.assertNotIn("팬 단계(하늘색", legend)
        self.assertNotIn("운전석 · A/C", legend)
        self.assertNotIn("legendSpan({fill: FAN_COLOR", legend)
        self.assertNotIn("시트 열선", legend)
        self.assertNotIn("핸들 열선", legend)
        self.assertIn("leg-sep", legend)
        clim_css = HTML[HTML.index(".clim-legend {") : HTML.index(".clim-legend > span")]
        self.assertIn("margin-left: calc(100% * 76 / 390)", clim_css)
        self.assertIn("bat-temp-legend", _fn("svgBatteryTemp"))
        self.assertIn(".bat-temp-legend { margin-left: calc(100% * 40 / 390);", HTML)
        self.assertIn("fitLegendDividers", _fn("mountClimate"))
        self.assertNotIn('label: "팬"', _fn("climateRows"))
        tip = _fn("climateTip")
        self.assertIn("popCard(", tip)
        self.assertIn("시간", tip)
        self.assertIn("bindClimateSelect(", _fn("mountClimate"))
        self.assertIn("bindCrosshair(plot", _fn("mountClimate"))
        self.assertIn("climateRows(model, ms)", _fn("mountClimate"))
        self.assertNotIn("시트·열선", legend)
        self.assertNotIn("<em>", legend)
        svg = _fn("climateSvg")
        self.assertIn("const W = 390, L = 76, R = 8", svg)
        self.assertNotIn("R = 44", svg)
        self.assertNotIn("L = 104", svg)
        self.assertNotIn("yTickText(L + innerW + 4", svg)
        self.assertNotIn("L + innerW - 4", svg)
        self.assertNotIn("barStrip", svg)
        self.assertIn("class='clim-range'", svg)
        self.assertNotIn("polygon", svg)
        self.assertIn("polyline fill='none'", svg)
        self.assertNotIn("cross-xlabel", HTML)
        self.assertIn('label: "시각"', HTML)
        self.assertIn("평균 실내 온도", HTML)
        self.assertIn("평균 실외 온도", HTML)
        self.assertIn("최저 배터리 온도", HTML)
        self.assertIn("최고 배터리 온도", HTML)
        batt = _fn("svgBatteryTemp")
        self.assertIn("const W = 390, H = 180, L = 40, R = 16", batt)
        self.assertNotIn("fmt(last.v", batt)
        self.assertIn("fill-opacity='0.16'", batt)
        self.assertNotIn("laneY", batt)
        self.assertIn("opacity:0.4", batt)
        self.assertNotIn('oneRow("히터"', HTML)
        self.assertIn("운전석 열선", HTML)
        self.assertIn("function seriesValueAt(", HTML)
        self.assertIn("touch-action: pan-y", HTML)
        self.assertIn("#ef8a6b", HTML)
        self.assertIn("#6ba8ef", HTML)
        self.assertNotIn("배터리 온도(최저~최고)", HTML.split("function svgChargeBand", 1)[1].split("function paintChargeTemps", 1)[0])

    def test_fan_tab_opens_for_auto_or_ac_without_fan_speed(self) -> None:
        src = (
            "const SEATS = []; const CLIM_BARS = []; const FAN_COLOR = '#3d9ccc';\n"
            + _fn("flagAt") + _fn("flagCuts") + _fn("coverWindow")
            + _fn("fanBlue") + _fn("mergeSpan") + _fn("onSpans") + _fn("fanPieces")
            + "const t0 = 0, t1 = 60000;\n"
            + "const base = {t0:t0, t1:t1, fan:[[{t:t0,v:0},{t:t1,v:0}]], auto:[], ac:[]};\n"
            + "const n = (extra) => fanPieces(Object.assign({}, base, extra)).length;\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  off: n({}),\n"
            + "  fan: n({fan:[[{t:t0,v:3},{t:t1,v:3}]]}),\n"
            + "  auto: n({fan:[[{t:t0,v:3},{t:t1,v:3}]], auto:[{t:t0,v:1}]}),\n"
            + "  manual: n({fan:[[{t:t0,v:3},{t:t1,v:3}]], auto:[{t:t0,v:0}]})\n"
            + "}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["off"], 0)
        self.assertGreater(got["fan"], 0)
        self.assertGreater(got["auto"], 0)
        self.assertGreater(got["manual"], 0)

    def test_fan_line_follows_auto_and_ac_changes(self) -> None:
        src = (
            "const FAN_COLOR = '#3d9ccc';\n"
            + _fn("flagAt") + _fn("flagCuts") + _fn("coverWindow")
            + _fn("fanBlue") + _fn("mergeSpan") + _fn("onSpans") + _fn("fanPieces")
            + "const t0 = Date.parse('2026-11-21T08:00:00Z');\n"
            + "const mid = t0 + 30*60*1000;\n"
            + "const acAt = t0 + 50*60*1000;\n"
            + "const t1 = t0 + 60*60*1000;\n"
            + "const model = {t0:t0, t1:t1, fan:[[{t:t0, v:1},{t:t1, v:1}]],\n"
            + "  auto:[{t:t0, v:0},{t:mid, v:1}], ac:[{t:t0, v:0},{t:acAt, v:1}]};\n"
            + "const pieces = fanPieces(model);\n"
            + "function at(t){\n"
            + "  const piece = pieces.find(p => p.hold[0].t <= t && t < p.hold[1].t);\n"
            + "  const auto = flagAt(model.auto, t);\n"
            + "  const ac = flagAt(model.ac, t);\n"
            + "  return {dash:piece.dash, color:piece.color,\n"
            + "    lineAuto:piece.dash === '', tipAuto:auto,\n"
            + "    lineAc:piece.color === FAN_ON, tipAc:ac};\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify({n:pieces.length, kinds:pieces.map(p => p.kind), levels:pieces.map(p => p.level)}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["kinds"], ["fan", "auto"])
        self.assertEqual(got["levels"], [1, None])

    def test_level_labels_stay_inside_the_shared_right_edge(self) -> None:
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateLegend")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("niceTicks") + _fn("tempAxis")
            + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines")
            + _fn("decimate") + _fn("flagAt") + _fn("crossLine") + _fn("chartSvg")
            + chunk
            + "const t0 = Date.parse('2026-11-15T11:40:00Z');\n"
            + "const t1 = Date.parse('2026-11-15T11:58:00Z');\n"
            + "const model = {t0:t0, t1:t1,\n"
            + "  inside:[[{t:t0,v:22},{t:t1,v:23}]], outside:[], setDriver:[], setPass:[],\n"
            + "  fan:[[{t:t0,v:11},{t:t1,v:6}]], wheel:[],\n"
            + "  heat:{seat_left:[[{t:t0,v:2},{t:t1,v:3}]]}, cool:{}, auto:[], ac:[], bars:{}};\n"
            + "function labels(html){\n"
            + "  const out=[];\n"
            + "  const re=/<text x=\"([^\"]+)\" y=\"([^\"]+)\" fill=\"[^\"]+\" font-size=\"11\" text-anchor=\"start\" dominant-baseline=\"middle\"(?: opacity='([^']+)')?>([^<]+)/g;\n"
            + "  let m;\n"
            + "  while((m=re.exec(html))){\n"
            + "    const op = m[3] == null ? 1 : Number(m[3]);\n"
            + "    if (op > 0.5) out.push({x:Number(m[1]), y:Number(m[2]), text:m[4]});\n"
            + "  }\n"
            + "  return out;\n"
            + "}\n"
            + "function plot(html){ return Number(html.match(/data-plot='([^']+)'/)[1]); }\n"
            + "const cabin=climateSvg(model,'cabin','cabin',1);\n"
            + "const fan=climateSvg(model,'fan','fan',1);\n"
            + "const seats=climateSvg(model,'seats','seats',1);\n"
            + "const glass=climateSvg(model,'glass','glass',1);\n"
            + "function deg(html){\n"
            + "  const out=[]; const re=/>([-0-9.]+)°C</g; let m;\n"
            + "  while((m=re.exec(html))) out.push(m[1]);\n"
            + "  return out;\n"
            + "}\n"
            + "function vlines(html){\n"
            + "  const re = /<line x1=\"([^\"]+)\" y1=\"([^\"]+)\" x2=\"([^\"]+)\"/g;\n"
            + "  let n = 0, m;\n"
            + "  while ((m = re.exec(html))) if (m[1] === m[3]) n++;\n"
            + "  return n;\n"
            + "}\n"
            + "function edges(html){\n"
            + "  const h = Number(html.match(/viewBox='0 0 390 ([0-9.]+)'/)[1]);\n"
            + "  const top = Number(html.match(/data-top='([^']+)'/)[1]);\n"
            + "  const ih = Number(html.match(/data-ih='([^']+)'/)[1]);\n"
            + "  const L = Number(html.match(/data-l='([^']+)'/)[1]);\n"
            + "  const plot = Number(html.match(/data-plot='([^']+)'/)[1]);\n"
            + "  const re = /<line x1=\"([^\"]+)\" y1=\"([^\"]+)\" x2=\"([^\"]+)\" y2=\"([^\"]+)\" stroke=\"[^\"]+\" stroke-width=\"1\" vector-effect=\"non-scaling-stroke\"><\\/line>/g;\n"
            + "  const lines = []; let m;\n"
            + "  while ((m = re.exec(html))) if (m[2] === m[4]) lines.push({x1:Number(m[1]), x2:Number(m[3]), y:Number(m[2])});\n"
            + "  return {h:h, top:top, ih:ih, L:L, plot:plot, lines:lines};\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  plot:[plot(cabin),plot(fan),plot(seats),plot(glass)],\n"
            + "  fan:labels(fan).map(t => t.text), seats:labels(seats).map(t => t.text), cabin:labels(cabin).map(t => t.text),\n"
            + "  fanX:labels(fan).map(t => t.x), seatX:labels(seats).map(t => t.x),\n"
            + "  fanY:labels(fan).map(t => t.y),\n"
            + "  high: fan.indexOf(',-'),\n"
            + "  deg:deg(cabin), vlines:[vlines(cabin),vlines(fan),vlines(glass)],\n"
            + "  edges:[edges(cabin),edges(fan),edges(seats),edges(glass)]\n"
            + "}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["plot"], [306, 306, 306, 306])
        self.assertEqual(got["fan"], [])
        self.assertEqual(got["seats"], [])
        self.assertEqual(got["cabin"], [])
        self.assertEqual(76 + got["plot"][1], 382)
        self.assertEqual(got["deg"], ["20", "22.5", "25"])
        self.assertEqual(got["vlines"], [2, 2, 2])
        for edge in got["edges"]:
            ys = sorted(line["y"] for line in edge["lines"])
            self.assertEqual(ys[0], edge["top"])
            self.assertEqual(ys[-1], edge["top"] + edge["ih"])
            self.assertIn(edge["top"] + edge["ih"] / 2, ys)
            for line in edge["lines"]:
                self.assertGreater(line["y"], 0)
                self.assertLess(line["y"], edge["h"])
                self.assertEqual(line["x1"], edge["L"])
                self.assertEqual(line["x2"], edge["L"] + edge["plot"])
        self.assertIn("function timeMarks(", HTML)
        marks = _fn("timeMarks")
        self.assertIn("const marks = [t0, t1]", marks)
        self.assertNotIn("(t1 - t0) / 2", marks)
        for name in ("svgLine", "svgChargeBand", "svgBatteryTemp", "climateSvg"):
            self.assertIn("timeMarks(", _fn(name))
            self.assertIn("axisBaselines(", _fn(name))
        self.assertIn("axisBaselines(", _fn("drawBatDay"))
        self.assertIn("function axisBaselines(", HTML)
        self.assertIn("overflow='visible'", _fn("chartSvg"))

    def test_temperature_steps_and_baselines_on_every_graph(self) -> None:
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("niceTicks") + _fn("tempAxis")
            + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines") + _fn("decimate") + _fn("coverWindow")
            + _fn("yOn") + _fn("crossLine") + _fn("chartSvg") + _fn("legendSwatch") + _fn("svgBatteryTemp")
            + _fn("svgLine") + _fn("svgChargeBand")
            + "function pack(lo, hi){ const a = tempAxis(lo, hi);"
            + " return {min:a.min, max:a.max, ticks:a.ticks, labels:a.ticks.map(v => fmt(v, 1))}; }\n"
            + "function deg(html){ const out=[]; const re=/>([-0-9.]+)°C</g; let m;"
            + " while((m=re.exec(html))) out.push(m[1]); return out; }\n"
            + "function edges(html){\n"
            + "  const h = Number(html.match(/viewBox='0 0 390 ([0-9.]+)'/)[1]);\n"
            + "  const top = Number(html.match(/data-top='([^']+)'/)[1]);\n"
            + "  const ih = Number(html.match(/data-ih='([^']+)'/)[1]);\n"
            + "  const L = Number(html.match(/data-l='([^']+)'/)[1]);\n"
            + "  const plot = Number(html.match(/data-plot='([^']+)'/)[1]);\n"
            + "  const re = /<line x1=\"([^\"]+)\" y1=\"([^\"]+)\" x2=\"([^\"]+)\" y2=\"([^\"]+)\" stroke=\"[^\"]+\" stroke-width=\"1\" vector-effect=\"non-scaling-stroke\"><\\/line>/g;\n"
            + "  const lines = []; let m;\n"
            + "  while ((m = re.exec(html))) if (m[2] === m[4]) lines.push({x1:Number(m[1]), x2:Number(m[3]), y:Number(m[2])});\n"
            + "  return {h:h, top:top, bot:top+ih, L:L, plot:plot, lines:lines};\n"
            + "}\n"
            + "const t0 = Date.parse('2026-11-15T11:40:00Z');\n"
            + "const t1 = Date.parse('2026-11-15T12:10:00Z');\n"
            + "const narrow = {min:[[{t:t0,v:21},{t:t1,v:22}]], max:[[{t:t0,v:24},{t:t1,v:24}]], heater:[], t0:t0, t1:t1};\n"
            + "const wide = {min:[[{t:t0,v:12},{t:t1,v:18}]], max:[[{t:t0,v:28},{t:t1,v:33}]], heater:[], t0:t0, t1:t1};\n"
            + "const speed = svgLine([{ts:'2026-11-15T11:40:00Z', v:12},{ts:'2026-11-15T12:10:00Z', v:48}],"
            + " {h:150, unit:'km/h', t0:'2026-11-15T11:40:00Z', t1:'2026-11-15T12:10:00Z'});\n"
            + "const power = svgLine([{ts:'2026-11-15T11:40:00Z', v:-20},{ts:'2026-11-15T12:10:00Z', v:40}],"
            + " {h:150, unit:'kW', t0:'2026-11-15T11:40:00Z', t1:'2026-11-15T12:10:00Z'});\n"
            + "const charge = svgChargeBand([{ts:'2026-11-15T11:40:00Z', v:3},{ts:'2026-11-15T12:10:00Z', v:7}],"
            + " [], '2026-11-15T12:10:00Z', 11, null, '2026-11-15T11:40:00Z');\n"
            + "const bn = svgBatteryTemp(narrow), bw = svgBatteryTemp(wide);\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  narrow: pack(22, 23), wide: pack(12, 33), exact: pack(20, 25),\n"
            + "  bn: deg(bn), bw: deg(bw),\n"
            + "  edges: {bn:edges(bn), bw:edges(bw), speed:edges(speed), power:edges(power), charge:edges(charge)}\n"
            + "}));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["narrow"], {"min": 20, "max": 25, "ticks": [20, 22.5, 25], "labels": ["20", "22.5", "25"]})
        self.assertEqual(got["exact"], got["narrow"])
        self.assertEqual(got["wide"]["min"], 10)
        self.assertEqual(got["wide"]["max"], 35)
        self.assertEqual(got["wide"]["ticks"], [10, 15, 20, 25, 30, 35])
        self.assertEqual(got["wide"]["labels"], ["10", "15", "20", "25", "30", "35"])
        self.assertTrue(all(abs(b - a - 5) < 1e-9 for a, b in zip(got["wide"]["ticks"], got["wide"]["ticks"][1:])))
        self.assertEqual(got["bn"], ["20", "22.5", "25"])
        self.assertEqual(got["bw"], ["10", "15", "20", "25", "30", "35"])
        self.assertLessEqual(got["wide"]["min"], 12)
        self.assertGreaterEqual(got["wide"]["max"], 33)
        for edge in got["edges"].values():
            ys = sorted(line["y"] for line in edge["lines"])
            self.assertEqual(ys[0], edge["top"])
            self.assertEqual(ys[-1], edge["bot"])
            self.assertGreaterEqual(len(ys), 3)
            for line in edge["lines"]:
                self.assertGreater(line["y"], 0)
                self.assertLess(line["y"], edge["h"])
                self.assertEqual([line["x1"], line["x2"]], [edge["L"], edge["L"] + edge["plot"]])

    def test_every_labeled_y_tick_has_a_gridline(self) -> None:
        start = HTML.index("    const SEATS = [")
        chunk = HTML[start:HTML.index("    function climateLegend")]
        src = (
            "function tone(){ return '#8aa0b5'; }\n"
            + _fn("fmtTrim") + _fn("fmt") + _fn("fmtHm") + _fn("niceTicks") + _fn("tempAxis")
            + _fn("timeMarks") + _fn("yTickLine") + _fn("yTickText") + _fn("axisBaselines") + _fn("decimate")
            + _fn("coverWindow") + _fn("yOn") + _fn("flagAt") + _fn("crossLine") + _fn("chartSvg") + _fn("legendSwatch")
            + _fn("svgLine") + _fn("svgChargeBand") + _fn("svgBatteryTemp")
            + chunk
            + "function hlines(html){\n"
            + "  const re=/<line x1=\"([^\"]+)\" y1=\"([^\"]+)\" x2=\"([^\"]+)\" y2=\"([^\"]+)\" stroke=\"[^\"]+\" stroke-width=\"1\" vector-effect=\"non-scaling-stroke\"><\\/line>/g;\n"
            + "  const out=[]; let m; while((m=re.exec(html))) if(m[2]===m[4]) out.push(Number(m[2]));\n"
            + "  return out;\n"
            + "}\n"
            + "function covered(lines, ys){ return ys.every(y => lines.some(v => Math.abs(v-y)<0.05)); }\n"
            + "const t0 = Date.parse('2026-11-15T11:40:00Z');\n"
            + "const t1 = Date.parse('2026-11-15T12:10:00Z');\n"
            + "const model = {t0:t0, t1:t1,\n"
            + "  inside:[[{t:t0,v:22},{t:t1,v:23}]], outside:[], setDriver:[], setPass:[],\n"
            + "  fan:[[{t:t0,v:6},{t:t1,v:4}]], wheel:[], heat:{}, cool:{}, auto:[], ac:[], bars:{}};\n"
            + "const batt = {min:[[{t:t0,v:21},{t:t1,v:22}]], max:[[{t:t0,v:24},{t:t1,v:24}]], heater:[], t0:t0, t1:t1};\n"
            + "const speed = svgLine([{ts:'2026-11-15T11:40:00Z', v:12},{ts:'2026-11-15T12:10:00Z', v:48}],"
            + " {h:150, unit:'km/h', t0:'2026-11-15T11:40:00Z', t1:'2026-11-15T12:10:00Z'});\n"
            + "const power = svgLine([{ts:'2026-11-15T11:40:00Z', v:-20},{ts:'2026-11-15T12:10:00Z', v:40}],"
            + " {h:150, unit:'kW', t0:'2026-11-15T11:40:00Z', t1:'2026-11-15T12:10:00Z'});\n"
            + "const charge = svgChargeBand([{ts:'2026-11-15T11:40:00Z', v:3.5},{ts:'2026-11-15T12:10:00Z', v:7}],"
            + " [], '2026-11-15T12:10:00Z', 7, null, '2026-11-15T11:40:00Z');\n"
            + "const cabin = climateSvg(model,'cabin','cabin',1);\n"
            + "const fan = climateSvg(model,'fan','fan',1);\n"
            + "const seats = climateSvg(model,'seats','seats',1);\n"
            + "const glass = climateSvg(model,'glass','glass',1);\n"
            + "const bn = svgBatteryTemp(batt);\n"
            + "function ysOf(lo, hi, n){ const out=[]; for(let i=0;i<=n;i++) out.push(lo+(hi-lo)*i/n); return out; }\n"
            + "function labels(html, size, anchor){\n"
            + "  const re = new RegExp('<text x=\"[^\"]+\" y=\"([^\"]+)\" fill=\"[^\"]+\" font-size=\"' + size + '\" text-anchor=\"' + (anchor || 'end') + '\" dominant-baseline=\"middle\"', 'g');\n"
            + "  const out=[]; let m; while((m=re.exec(html))) out.push(Number(m[1])); return out;\n"
            + "}\n"
            + "function anyH(html){\n"
            + "  const out=[]; const re=/<line\\b[^>]*>/g; let m;\n"
            + "  while((m=re.exec(html))){\n"
            + "    const y1=m[0].match(/\\by1=(['\"])([^'\"]+)\\1/), y2=m[0].match(/\\by2=(['\"])([^'\"]+)\\1/);\n"
            + "    if(y1 && y2 && y1[2]===y2[2]) out.push(Number(y1[2]));\n"
            + "  }\n"
            + "  return out;\n"
            + "}\n"
            + "function centered(html, size, anchor){\n"
            + "  const labs=labels(html, size, anchor), lines=anyH(html);\n"
            + "  const h=Number(html.match(/viewBox='0 0 390 ([0-9.]+)'/)[1]);\n"
            + "  return labs.length>0 && labs.every(y => lines.some(v => Math.abs(v-y)<0.05) && y-6>0 && y+6<h);\n"
            + "}\n"
            + "function ends(html){\n"
            + "  const labs=labels(html,'11').slice().sort((a,b)=>a-b);\n"
            + "  const top=Number(html.match(/data-top='([^']+)'/)[1]);\n"
            + "  const ih=Number(html.match(/data-ih='([^']+)'/)[1]);\n"
            + "  return Math.abs(labs[0]-top)<0.05 && Math.abs(labs[labs.length-1]-(top+ih))<0.05;\n"
            + "}\n"
            + "const graphs={charge:charge, batt:bn, cabin:cabin, fan:fan, seats:seats, glass:glass, speed:speed, power:power};\n"
            + "function edgeOf(html){\n"
            + "  const L=Number(html.match(/data-l='([^']+)'/)[1]);\n"
            + "  const plot=Number(html.match(/data-plot='([^']+)'/)[1]);\n"
            + "  return {L:L, plot:plot, right:L+plot};\n"
            + "}\n"
            + "const center={}; const end={};\n"
            + "Object.keys(graphs).forEach(k => { center[k]=centered(graphs[k],'11'); end[k]=ends(graphs[k]); });\n"
            + "center.fanLevels=centered(fan,'11','start'); center.seatLevels=centered(seats,'11','start');\n"
            + "process.stdout.write(JSON.stringify({\n"
            + "  charge: covered(hlines(charge), ysOf(12, 134, 2)),\n"
            + "  batt: covered(hlines(bn), ysOf(16, 134, 2)),\n"
            + "  cabin: covered(hlines(cabin), ysOf(12, 188, 2)),\n"
            + "  fan: covered(hlines(fan), ysOf(12, 188, 2)),\n"
            + "  seats: covered(hlines(seats), ysOf(12, 188, 2)),\n"
            + "  glass: covered(hlines(glass), ysOf(12, 188, 2)),\n"
            + "  speed: covered(hlines(speed), ysOf(12, 116, 3)),\n"
            + "  power: covered(hlines(power), ysOf(12, 116, 3)),\n"
            + "  center: center, end: end,\n"
            + "  edges:{fan:edgeOf(fan), batt:edgeOf(bn), power:edgeOf(power), speed:edgeOf(speed)}\n"
            + "}));"
        )
        # The renderer names are checked from source so a new graph cannot skip the helper.
        for name in ("svgLine", "svgChargeBand", "svgBatteryTemp", "climateSvg", "drawBatDay"):
            self.assertIn("yTickLine(", _fn(name))
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        for key in ("charge", "batt", "cabin", "fan", "seats", "glass", "speed", "power"):
            self.assertTrue(got[key], key)
            self.assertTrue(got["center"][key], key)
            self.assertTrue(got["end"][key], key)
        self.assertFalse(got["center"]["fanLevels"])
        self.assertFalse(got["center"]["seatLevels"])
        self.assertEqual(got["edges"]["batt"], got["edges"]["power"])
        self.assertEqual(got["edges"]["batt"], {"L": 40, "plot": 334, "right": 374})
        self.assertEqual(got["edges"]["fan"], {"L": 76, "plot": 306, "right": 382})
        self.assertNotIn("T + 9", _fn("climateSvg"))
        self.assertNotIn("innerH - 2", _fn("svgBatteryTemp"))

    def test_charge_power_touch_survives_reopen(self) -> None:
        rows = _fn("chargePowerRows")
        self.assertIn('label: "충전 전력"', rows)
        self.assertIn(" kW", rows)
        self.assertIn("bindCrosshair(box", _fn("showChargeCurve"))
        self.assertIn("showChargeCurve(box, cached.html, cached.segs)", _fn("openChargeDetail"))
        self.assertIn("cross-hit", _fn("svgChargeBand"))
        self.assertIn("crossLine(", _fn("svgChargeBand"))
        self.assertIn("bindCrosshair(el", _fn("loadTripPower"))
        self.assertIn("bindCrosshair(plot", _fn("mountBattery"))
        self.assertIn("bindCrosshair(el", _fn("loadTripSpeed"))

    def test_crosshair_reads_real_samples_and_skips_a_gap(self) -> None:
        src = (
            _fn("seriesValueAt")
            + "const seg=[[{t:0,v:10},{t:1000,v:20}],[{t:5000,v:30}]];\n"
            + "const out={mid:seriesValueAt(seg,500,'line'),step:seriesValueAt(seg,500,'step'),"
            + "gap:seriesValueAt(seg,3000,'line'),late:seriesValueAt(seg,5000,'line')};\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["mid"], 15)
        self.assertEqual(got["step"], 10)
        self.assertIsNone(got["gap"])
        self.assertEqual(got["late"], 30)

    def test_crosshair_time_is_seoul_hms(self) -> None:
        src = (
            _fn("fmtSeoulTime")
            + "process.stdout.write(fmtSeoulTime('2026-11-19T05:32:05Z'));"
        )
        got = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout
        self.assertEqual(got, "14:32:05")

    def test_detail_bar_uses_the_shown_hundredths(self) -> None:
        src = (
            _fn("socHundredths") + _fn("socFixedText") + _fn("socDetailText") + _fn("socBarHtml")
            + "const out=[socBarHtml(64.6,63.4),socBarHtml(65.004,65.001),socBarHtml(80.04,79.96)];\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        got = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertIn("64.6% → 63.4%", got[0])
        self.assertIn("soc-delta down", got[0])
        self.assertIn(">65%<", got[1])
        self.assertNotIn("soc-delta", got[1])
        self.assertIn("80.04% → 79.96%", got[2])
        self.assertIn("soc-delta down", got[2])
        self.assertIn("width:0.1%", got[2])


if __name__ == "__main__":
    unittest.main()
