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
        self.assertIn("seat_left", built["bars"])
        self.assertNotIn("seat_right", built["bars"])
        self.assertEqual(built["bars"]["seat_left"], [(at(60), at(400), 2.0)])
        self.assertIn("wheel", built["bars"])

    def test_fan_auto_and_ac_flags_follow_the_samples(self) -> None:
        series = {
            "HvacFanSpeed": [(at(0), 3), (at(120), 6), (at(240), 0)],
            "HvacAutoMode": [(at(0), "On"), (at(120), "Off")],
            "HvacACEnabled": [(at(120), True)],
        }
        built = self._built(series, {})
        self.assertEqual([v for seg in built["fan"] for _t, v in seg], [3.0, 6.0, 0.0])
        self.assertEqual(built["auto"][0][1], 1.0)
        self.assertEqual(built["auto"][-1][1], 0.0)
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
        self.assertEqual(format_soc_change(64.6, 63.4), "64.60% → 63.40%")
        self.assertEqual(format_soc_change(65.0, 63.4), "65.00% → 63.40%")
        self.assertEqual(format_soc_change(65.004, 65.001), "65.00%")
        self.assertEqual(format_soc_change(80.04, 79.96), "80.04% → 79.96%")
        self.assertEqual(format_soc_change(64.6, 64.6), "64.60%")
        self.assertNotIn("%p", format_soc_change(64.6, 63.4))


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PageGraphTests(unittest.TestCase):
    def test_labels_tabs_and_crosshair_hooks(self) -> None:
        self.assertIn('const APP_VERSION = "v1.16.1"', HTML)
        self.assertIn("cross-xlabel", HTML)
        self.assertIn('label: "시각"', HTML)
        self.assertIn("배터리 히터", HTML)
        self.assertNotIn('oneRow("히터"', HTML)
        self.assertIn("시트·열선", HTML)
        self.assertIn("function seriesValueAt(", HTML)
        self.assertIn("touch-action: pan-y", HTML)
        self.assertIn("#ef8a6b", HTML)
        self.assertIn("#6ba8ef", HTML)
        self.assertNotIn("배터리 온도(최저~최고)", HTML.split("function svgChargeBand", 1)[1].split("function paintChargeTemps", 1)[0])

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
        self.assertIn("64.60% → 63.40%", got[0])
        self.assertIn("soc-delta down", got[0])
        self.assertIn(">65.00%<", got[1])
        self.assertNotIn("soc-delta", got[1])
        self.assertIn("80.04% → 79.96%", got[2])
        self.assertIn("soc-delta down", got[2])
        self.assertIn("width:0.1%", got[2])


if __name__ == "__main__":
    unittest.main()
