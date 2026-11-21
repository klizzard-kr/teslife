"""v1.7.0: payment rate, number formatting, charger inference, moving-time speed, inbox read."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import (
    PROCESS_VERSION,
    Events,
    _charge_row,
    connect,
    format_ts,
    moving_seconds,
    process_db,
    series_avg_max,
)
from process.evcharger import fill_vehicle, infer_charger, zcode_from_point
from web.formatnum import format_number
from web.notify import list_notifications, mark_read
from web.tariff import ENV_CARD, ENV_RATES, PLUGLINK, annotate, choose_rate, ensure_schema, save_tariff

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-170"


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    return HTML[i:HTML.index("\n    }\n", i) + 6]


class FormatTests(unittest.TestCase):
    def test_trailing_zero_is_dropped(self) -> None:
        self.assertEqual(format_number(292, 1), "292")
        self.assertEqual(format_number(307.2, 1), "307.2")
        self.assertEqual(format_number(10.0, 2), "10")
        self.assertEqual(format_number(10.5, 2), "10.5")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_screen_fmt_matches(self) -> None:
        src = _fn("fmtTrim") + _fn("fmt") + "process.stdout.write([fmt(292,1),fmt(307.2,1),fmt(10,2)].join(','));"
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, "292,307.2,10")


class RateTests(unittest.TestCase):
    def test_member_card_and_guest(self) -> None:
        self.assertEqual(choose_rate(292, 357, 295, True, False), 292)
        self.assertEqual(choose_rate(292, 357, 295, False, True), 295)
        self.assertEqual(choose_rate(292, 357, 295, False, False), 357)
        self.assertEqual(choose_rate(400, 528, 295, True, True), 295)
        self.assertEqual(choose_rate(280, 528, 295, True, True), 280)
        self.assertIsNone(choose_rate(None, None, None, False, False))

    def test_env_card_uses_the_cheaper_official_rate(self) -> None:
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertEqual([row[1] for row in ENV_RATES], [295.0, 307.2, 325.6, 348.4, 393.1])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        conn = connect(Path(folder.name) / "s.db")
        self.addCleanup(conn.close)
        ensure_schema(conn)
        member = {
            "charge_id": "a", "end_ts": "2026-08-15T03:00:00Z", "kind": "AC",
            "ac_kwh": 10, "dc_kwh": 9, "max_charger_kw": 7,
            "meta": {"operator": PLUGLINK, "pay_method": "member"},
        }
        # PlugLink member 292 is cheaper than the card's 295.
        annotate(conn, VID, [member])
        self.assertEqual(member["unit_krw_per_kwh"], 292)
        self.assertFalse(member["unit_estimated"])
        card = dict(member, charge_id="b", meta={"operator": PLUGLINK, "pay_method": "env_card"})
        annotate(conn, VID, [card])
        self.assertEqual(card["unit_krw_per_kwh"], 295)
        self.assertTrue(card["cost_estimated"])
        self.assertEqual(card["cost_krw"], round(10 * 295))
        billed = dict(card, meta={"operator": PLUGLINK, "pay_method": "env_card", "kwh_billed": 10, "total_krw": 5000})
        annotate(conn, VID, [billed])
        self.assertEqual(billed["cost_krw"], 5000)
        self.assertFalse(billed["cost_estimated"])
        self.assertEqual(billed["unit_krw_per_kwh"], 500)
        # No saved choice: 비회원. Before the card rates start, that is the guest price.
        plain = dict(member, charge_id="c", meta={"operator": PLUGLINK})
        annotate(conn, VID, [plain])
        self.assertEqual(plain["unit_krw_per_kwh"], 357)
        early = dict(plain, end_ts="2026-07-31T12:00:00Z")
        early.pop("unit_krw_per_kwh", None)
        annotate(conn, VID, [early])
        self.assertEqual(early["unit_krw_per_kwh"], 357)
        roaming = dict(member, charge_id="d", meta={"operator": "다른충전", "pay_method": "env_card"})
        annotate(conn, VID, [roaming])
        self.assertEqual(roaming["unit_krw_per_kwh"], 295)
        unknown = dict(member, charge_id="e", meta={"operator": "다른충전"})
        annotate(conn, VID, [unknown])
        self.assertIsNone(unknown["unit_krw_per_kwh"])
        self.assertEqual(ENV_CARD, "환경부")


class InferTests(unittest.TestCase):
    def test_transitions_pick_one_charger_or_none(self) -> None:
        start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        unplug = end + timedelta(minutes=4)

        def rec(cid, minutes, stat, last_end=None):
            return {
                "chger_id": cid,
                "fetched": start + timedelta(minutes=minutes),
                "stat": stat,
                "now_ts": None,
                "last_start": None,
                "last_end": last_end,
            }

        chosen = infer_charger([
            rec("01", -2, "2"),
            rec("01", 2, "3"),
            rec("02", -2, "2"),
            rec("02", 2, "2"),
        ], start, end, unplug)
        self.assertEqual(chosen, "01")
        tie = infer_charger([
            rec("01", -2, "2"), rec("01", 2, "3"),
            rec("02", -2, "2"), rec("02", 2, "3"),
        ], start, end, unplug)
        self.assertIsNone(tie)
        weak = infer_charger([
            {"chger_id": "01", "fetched": start, "stat": "3", "now_ts": start,
             "last_start": None, "last_end": None},
        ], start, end, None)
        self.assertIsNone(weak)
        both = infer_charger([
            {"chger_id": "03", "fetched": end, "stat": "3", "now_ts": None,
             "last_start": start, "last_end": end},
        ], start, end, None)
        self.assertEqual(both, "03")


class SpeedTests(unittest.TestCase):
    def test_moving_time_ignores_stops_and_caps_long_gaps(self) -> None:
        t0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        short = [(t0, 20.0), (t0 + timedelta(seconds=90), 20.0)]
        self.assertEqual(moving_seconds(short, short[-1][0]), 90)
        long = [(t0, 20.0), (t0 + timedelta(minutes=5), 20.0)]
        self.assertEqual(moving_seconds(long, long[-1][0]), 120)
        stopped = [
            (t0, 30.0),
            (t0 + timedelta(minutes=10), 0.0),
            (t0 + timedelta(minutes=20), 0.0),
        ]
        # The moving sample runs into a stop, so only half of the capped gap counts.
        self.assertEqual(moving_seconds(stopped, stopped[-1][0]), 60)

    def test_trip_average_uses_moving_time_and_keeps_edits(self) -> None:
        t0 = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
        end = t0 + timedelta(minutes=70)
        rows = [
            (t0 - timedelta(minutes=1), "Gear", "P"),
            (t0, "Gear", "D"),
            (t0, "Odometer", 1000),
            (t0, "VehicleSpeed", 30),
            (t0 + timedelta(minutes=10), "VehicleSpeed", 30),
            (t0 + timedelta(minutes=20), "VehicleSpeed", 0),
            (t0 + timedelta(minutes=40), "VehicleSpeed", 0),
            (t0 + timedelta(minutes=50), "VehicleSpeed", 40),
            (t0 + timedelta(minutes=60), "VehicleSpeed", 40),
            (end, "Gear", "P"),
            (end, "Odometer", 1010),
            (end, "VehicleSpeed", 40),
        ]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = Path(folder.name) / "s.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "name": name, "value": value,
        } for ts, name, value in rows])
        conn.close()
        process_db(db, seed=False, now=end + timedelta(minutes=5))
        conn = connect(db)
        trip = conn.execute("SELECT start_ts, km, avg_speed, max_speed, duration_s FROM trips").fetchone()
        self.assertAlmostEqual(trip[1], 10.0, places=3)
        self.assertAlmostEqual(trip[3], 40.0, places=3)
        wall = trip[1] / (trip[4] / 3600.0)
        self.assertGreater(trip[2], wall)
        speeds = [(ts, float(value)) for ts, name, value in rows if name == "VehicleSpeed" and ts <= end]
        expect = trip[1] / (moving_seconds(speeds, end) / 3600.0)
        self.assertAlmostEqual(trip[2], expect, places=3)
        charge_id = "2026-11-02T03:00:00Z"
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 1)",
            (VID, charge_id, "2026-11-02T04:00:00Z"),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw, operator) VALUES (?, ?, 4321, 'kept')",
            (VID, charge_id),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id, chger_id, source, updated_ts) "
            "VALUES (?, ?, 'ST', '07', 'user', '2026-11-02T04:00:00Z')",
            (VID, charge_id),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, memo, source) "
            "VALUES (?, 'manual-1', '2026-11-02', 'wash', 1000, '세차', 'manual')",
            (VID,),
        )
        conn.execute(
            "UPDATE process_meta SET value = '19' WHERE key = ?",
            (f"version:{VID}",),
        )
        conn.commit()
        conn.close()
        process_db(db, seed=False, now=end + timedelta(minutes=5))
        conn = connect(db)
        self.assertEqual(conn.execute("SELECT value FROM process_meta WHERE key = ?", (f"version:{VID}",)).fetchone()[0], "25")
        self.assertEqual(conn.execute("SELECT total_krw, operator FROM charge_meta").fetchone(), (4321, "kept"))
        self.assertEqual(conn.execute("SELECT chger_id, source FROM charge_charger").fetchone(), ("07", "user"))
        self.assertEqual(conn.execute("SELECT amount_krw, memo FROM ledger").fetchone(), (1000, "세차"))
        again = conn.execute("SELECT avg_speed FROM trips").fetchone()[0]
        self.assertAlmostEqual(again, expect, places=3)
        conn.close()


class ChargePowerTests(unittest.TestCase):
    def test_average_and_max_share_the_power_series(self) -> None:
        t0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        end = t0 + timedelta(hours=1)
        rows = [
            (t0, "DetailedChargeState", "Charging"),
            (t0, "ACChargingEnergyIn", 0),
            (end, "ACChargingEnergyIn", 10),
            (t0, "ACChargingPower", 6.7),
            (t0 + timedelta(minutes=30), "ACChargingPower", 6.8),
            (end, "DetailedChargeState", "Complete"),
        ]
        ev = Events(sorted(rows, key=lambda item: (item[0], item[1])))
        row = _charge_row(VID, ev, t0, end)
        self.assertAlmostEqual(row["max_charger_kw"], 6.8)
        self.assertAlmostEqual(row["avg_charger_kw"], 6.75)
        self.assertGreaterEqual(row["max_charger_kw"], row["avg_charger_kw"])
        # 10 kWh over one hour would show 10 kW, above the samples. That figure is not the average.
        self.assertLess(row["avg_charger_kw"], 8)
        avg, peak = series_avg_max([(t0, 6.75), (end, 6.74)], end)
        self.assertGreaterEqual(peak, avg)


class ChargerCallTests(unittest.TestCase):
    def test_coordinates_fetch_a_catalog_when_the_label_has_no_region(self) -> None:
        self.assertEqual(zcode_from_point(37.5, 127.0), "11")
        self.assertEqual(zcode_from_point(33.4, 126.5), "50")
        self.assertIsNone(zcode_from_point(0, 0))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        conn = connect(Path(folder.name) / "s.db")
        self.addCleanup(conn.close)
        start = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 5)",
            (VID, cid, format_ts(start + timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5, 127.0, 'car')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO stations (vehicle_id, id, name, address_text) VALUES (?, 's', '예시동', '예시동')",
            (VID,),
        )
        conn.commit()
        seen = []

        class Resp:
            def read(self):
                return b"<?xml version='1.0'?><response><header><resultCode>00</resultCode></header><body><items></items><totalCount>0</totalCount></body></response>"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opener(req, timeout=20):
            seen.append(req.full_url)
            return Resp()

        import os
        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.pop("EV_CHARGER_API_KEY", None) if old is None else os.environ.__setitem__("EV_CHARGER_API_KEY", old))
        calls = fill_vehicle(conn, VID, start + timedelta(minutes=30), opener)
        self.assertEqual(calls, 1)
        self.assertIn("zcode=11", seen[0])
        self.assertNotIn("예시동", seen[0])

    def test_process_calls_the_matcher_even_when_signals_are_unchanged(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = Path(folder.name) / "s.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, [{
            "vehicle_id": VID, "timestamp": "2026-11-01T00:00:00Z", "name": "Gear", "value": "P",
        }])
        conn.close()
        import process.evcharger as ev
        called = []
        original = ev.fill_vehicle

        def fake(conn, vid, now=None, opener=None):
            called.append(vid)
            return 0

        ev.fill_vehicle = fake
        self.addCleanup(lambda: setattr(ev, "fill_vehicle", original))
        now = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
        process_db(db, seed=False, now=now)
        process_db(db, seed=False, now=now)
        self.assertEqual(called, [VID, VID])


class InboxTests(unittest.TestCase):
    def test_opening_marks_every_row_and_the_dot_slot_stays(self) -> None:
        header = HTML[HTML.index("<header>"):HTML.index("</header>")]
        self.assertLess(header.index('id="inbox-open"'), header.index('id="settings-open"'))
        self.assertNotIn("lang-toggle", header)
        self.assertNotIn("theme-toggle", header)
        self.assertNotIn('id="settings-pay"', HTML)
        self.assertNotIn('id="env-card"', HTML)
        self.assertNotIn('id="inbox-read-all"', HTML)
        self.assertIn('id="inbox-read-tab"', HTML)
        self.assertIn(">모두 읽음<", HTML)
        self.assertNotIn("notice-dot", HTML)
        self.assertNotIn("한국시간", HTML)
        self.assertNotIn(">KST<", HTML)
        handler = HTML[HTML.index('getElementById("inbox-open")'):HTML.index('getElementById("inbox-x")')]
        self.assertLess(handler.index("badge.hidden = true"), handler.index("renderInbox"))
        self.assertNotIn("all: true", handler)
        read = HTML[HTML.index('getElementById("inbox-read-tab")'):HTML.index('getElementById("inbox-delete-tab")')]
        self.assertIn("tab: inboxTab", read)
        self.assertNotIn("all: true", read)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        conn = connect(Path(folder.name) / "s.db")
        self.addCleanup(conn.close)
        from web.notify import ensure_schema
        ensure_schema(conn)
        conn.executemany(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body) VALUES (?, ?, ?, 'charge_done', ?, ?)",
            [(VID, "a", "2026-11-01T00:00:00Z", "하나", "본문"), (VID, "b", "2026-11-01T01:00:00Z", "둘", "본문")],
        )
        conn.commit()
        self.assertEqual(list_notifications(conn, VID)["unread"], 2)
        listed = mark_read(conn, VID, None, True)
        self.assertEqual(listed["unread"], 0)
        self.assertEqual(len(listed["notifications"]), 2)
        self.assertTrue(all(item["read"] for item in listed["notifications"]))


if __name__ == "__main__":
    unittest.main()
