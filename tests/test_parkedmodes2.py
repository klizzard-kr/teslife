"""parkedmodes2 (after ): five parked categories (주차·반려동물·캠핑·센트리·공조 유지),
`modes` / `outside_temp` on parked_segments, and estimates for parked-mode bars with no
measured kWh of their own (priors -> this car's average)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
import process
from process import Events, parked_modes, parked_segments, parked_state, process_db, connect as process_connect
from web import estimates
from web.estimates import CLIMATE_PRIOR_W, PRIOR_W, estimate, measured_groups, mode_watts, temp_band
from web.timeline import LANE_LABELS, day_timeline

VID = "VINPM2"
T = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)  # 10:00 KST
BUILDER = ROOT / "build_telemetry_config.parkedmodes2.py"


def H(h: float) -> datetime:
    return T + timedelta(hours=h)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(rows) -> Events:
    return Events(sorted(rows, key=lambda r: (r[0], r[1])))


P0 = [(H(-1), "Gear", "P"), (H(-1), "HvacPower", "Off"), (H(-1), "SentryMode", "Off")]


def every(a: float, b: float, name: str, f, step: float = 5 / 60) -> list:
    out, t = [], a
    while t <= b + 1e-9:
        out.append((H(t), name, f(t)))
        t += step
    return out


class CategoryTests(unittest.TestCase):
    def state(self, rows, t=H(0)):
        e = ev([*P0, *rows])
        return parked_state(e, t), parked_modes(e, t)

    def test_five_categories(self) -> None:
        self.assertEqual(process.PROCESS_VERSION, "24")
        self.assertEqual(process.PARKED_CATEGORIES, ("camp", "dog", "keep", "sentry", "parked"))
        self.assertEqual(set(LANE_LABELS), {"drive", "parked", "sentry"})
        self.assertEqual(LANE_LABELS["parked"], "주차")

    def test_other_loads_count_as_parked(self) -> None:
        for rows in ([(H(-0.5), "HvacPower", "On")], [(H(-0.5), "HvacPower", "Precondition")],
                     [(H(-0.5), "HvacPower", "OverheatProtect")], [(H(-0.5), "BatteryHeaterOn", True)],
                     [(H(-0.5), "MediaPlaybackStatus", "Playing")],
                     [(H(-0.1), "SoftwareUpdateDownloadPercentComplete", 40)]):
            self.assertEqual(self.state(rows), (("parked", 0), ()), rows)

    def test_modes_keep_overlap(self) -> None:
        st, modes = self.state([(H(-0.5), "ClimateKeeperMode", "Dog"), (H(-0.5), "SentryMode", "Armed")])
        self.assertEqual((st, modes), (("dog", 1), ("dog", "sentry")))

    def test_segments_store_modes_and_temperature(self) -> None:
        e = ev([*P0, (H(0), "ClimateKeeperMode", "On"), (H(0), "OutsideTemp", 18.0),
                (H(0), "LifetimeEnergyUsed", 10.0), (H(1), "ClimateKeeperMode", "Off"), (H(2), "LifetimeEnergyUsed", 11.0)])
        segs = parked_segments(e, H(0), H(2))
        self.assertEqual([(s["category"], s["modes"], s["outside_temp"]) for s in segs],
                         [("keep", "keep", 18.0), ("parked", "", 18.0)])


def keep_day(day: float, temp: float = 20.0) -> list:
    """An earlier day at 10:00 KST: 45 min climate keep alone at 800 W, then asleep."""
    o = day * 24
    base = 50.0 + (2 + day) * 10
    return [
        (H(o), "Gear", "P"), (H(o), "SentryMode", "Off"), (H(o), "OutsideTemp", temp),
        (H(o), "ClimateKeeperMode", "On"), *every(o, o + 0.75, "LifetimeEnergyUsed", lambda t: base + 0.8 * (t - o)),
        (H(o + 0.75), "ClimateKeeperMode", "Off"),
    ]


class EstimateTests(unittest.TestCase):
    def build(self, days: list[float]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "signals.db"
        rows = [r for d in days for r in keep_day(d)] + [
            # 10/30: charging 10:00-11:00 KST with sentry on (energy mixed with the charge)
            (H(-0.1), "Gear", "P"), (H(-0.1), "OutsideTemp", 20.0),
            (H(0), "DetailedChargeState", "Charging"), (H(0), "SentryMode", "Armed"),
            *every(0, 1, "ACChargingEnergyIn", lambda t: t * 7), *every(0, 1, "BatteryLevel", lambda t: 50 + t * 9),
            (H(1), "DetailedChargeState", "Complete"), (H(1), "SentryMode", "Off"),
        ]
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{"vehicle_id": VID, "timestamp": iso(t), "name": n, "value": v} for t, n, v in rows])
        conn.close()
        process_db(db, seed=False, now=H(3))
        conn = process_connect(db)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_sentry_while_charging_uses_prior(self) -> None:
        conn = self.build([-1, -2])
        d = day_timeline(conn, VID, "2026-10-30", now=H(3))
        sentry = {l["key"]: l["bars"] for l in d["lanes"]}["sentry"][0]
        self.assertNotIn("kwh", sentry)
        self.assertEqual((sentry["estimate_w"], sentry["estimate_source"], sentry["kwh_estimate"]), (226, "prior", 0.23))

    def test_keep_measured_after_two_days(self) -> None:
        conn = self.build([-1, -2])
        g = measured_groups(conn, VID, H(14))
        self.assertEqual((g["keep:15–25℃"]["w"], g["keep:15–25℃"]["days"], g["keep:15–25℃"]["ok"]), (800, 2, True))
        self.assertEqual(mode_watts(g, "keep", 20.0), (800, "measured", "15–25℃"))
        one = measured_groups(self.build([-1]), VID, H(14))
        self.assertFalse(one["keep:15–25℃"]["ok"])
        self.assertEqual(mode_watts(one, "keep", 20.0), (CLIMATE_PRIOR_W["15–25℃"], "prior", "15–25℃"))

    def test_priors_in_one_place(self) -> None:
        self.assertEqual(PRIOR_W, {"sentry": 226})
        self.assertEqual(estimates.CLIMATE_MODES, ("camp", "dog", "keep"))
        self.assertEqual((estimates.MIN_HOURS, estimates.MIN_DAYS), (1.0, 2))
        self.assertIsNone(mode_watts({}, "keep", None))
        self.assertIsNone(mode_watts({}, "hvac", 20.0))          # no HVAC / media estimates any more
        self.assertIsNone(mode_watts({}, "media", None))
        self.assertEqual(estimate({}, "dog", H(0), H(0.5), 30.0)["kwh_estimate"], 0.7)
        self.assertEqual([temp_band(x) for x in (-3, 5, 14.9, 15, 25, None)],
                         ["5℃ 미만", "5–15℃", "5–15℃", "15–25℃", "25℃ 이상", None])


class TelemetryAndUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    @unittest.skipUnless(BUILDER.exists(), "deploy builder not on this machine")
    def test_no_new_telemetry_fields(self) -> None:
        text = BUILDER.read_text(encoding="utf-8")
        for name in ("MediaPlaybackStatus", "CenterDisplay", "PreconditioningEnabled"):
            self.assertNotIn(f'"{name}": f(', text)

    def test_bridge_has_no_media_prefixes(self) -> None:
        self.assertNotIn("MediaStatus", (ROOT / "bridge" / "bridge.py").read_text(encoding="utf-8"))

    def test_estimate_row_reads_약(self) -> None:
        # : estimates read "약 … kWh"; no "(추정)" after values
        self.assertIn('if (!known(b.kwh) && known(b.kwh_estimate)) rows.push(["사용량", tlKwhPct(b.kwh_estimate)]);', self.html)
        self.assertIn('return (est ? "약 " : "") + fmt(v, v < 0.1 ? 3 : 2) + " kWh";', self.html)
        self.assertNotIn('" kWh (추정)"', self.html)
        self.assertNotIn('" (추정)"', self.html)
        self.assertNotIn("추정 사용량", self.html)
        self.assertNotIn("기본값)", self.html)
        self.assertNotIn("estimate_source", self.html)

    def test_grid_line_centred_like_svg_stroke(self) -> None:
        self.assertIn(".tl-grid { position: absolute; top: 0; bottom: 0; width: 1px; margin-left: -0.5px;", self.html)


if __name__ == "__main__":
    unittest.main()
