"""v1.7.1: district charger cache, history retention, line loss, detail row order."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, format_ts
from process.evcharger import (
    _store_items,
    ensure_schema,
    fill_vehicle,
    prune_status_hist,
    region_from_point,
)
from process.ev_cache_cleanup import cleanup_public_cache, maybe_cleanup
from process.speed_events import event_details, speed_events
from web.tariff import annotate, energy_loss_kwh

VID = "car-a"
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)
SOSA = (37.53, 126.78)


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    ensure_schema(conn)
    return conn


def _xml(body: str, total: int) -> bytes:
    return (
        "<?xml version='1.0'?><response><header><resultCode>00</resultCode></header>"
        f"<body><items>{body}</items><totalCount>{total}</totalCount></body></response>"
    ).encode()


class RegionTests(unittest.TestCase):
    def test_bucheon_is_a_district_not_seoul(self) -> None:
        self.assertEqual(region_from_point(37.53, 126.78), ("41", "41190"))  # 부천
        self.assertEqual(region_from_point(37.487, 126.753), ("41", "41190"))
        self.assertEqual(region_from_point(37.503, 126.766), ("41", "41190"))
        self.assertEqual(region_from_point(37.53, 126.79), ("41", "41190"))
        self.assertEqual(region_from_point(37.566, 126.978), ("11", "11140"))
        self.assertEqual(region_from_point(37.5, 127.0), ("11", "11650"))
        self.assertIsNone(region_from_point(0, 0))

    def test_coordinates_request_the_district_ahead_of_a_seoul_label(self) -> None:
        conn = _db(self)
        start = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 5)",
            (VID, cid, format_ts(start + timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, ?, ?, 'car')",
            (VID, cid, SOSA[0], SOSA[1]),
        )
        conn.execute(
            "INSERT INTO stations (vehicle_id, id, name, address_text) VALUES (?, 's', 'label', '서울특별시')",
            (VID,),
        )
        conn.commit()
        seen: list[str] = []

        class Resp:
            def read(self):
                return _xml("", 0)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opener(req, timeout=20):
            seen.append(req.full_url)
            return Resp()

        old = os.environ.get("EV_CHARGER_API_KEY")
        os.environ["EV_CHARGER_API_KEY"] = "TESTKEY"
        self.addCleanup(lambda: os.environ.__setitem__("EV_CHARGER_API_KEY", old) if old else os.environ.pop("EV_CHARGER_API_KEY", None))
        calls = fill_vehicle(conn, VID, start + timedelta(minutes=30), opener)
        self.assertEqual(calls, 1)
        self.assertIn("zcode=41", seen[0])
        self.assertIn("zscode=41190", seen[0])
        self.assertNotIn("zcode=11", seen[0])

    def test_catalog_keeps_nearby_rows_and_skips_history(self) -> None:
        conn = _db(self)
        near = {
            "statId": "NEAR", "chgerId": "01", "lat": "37.53", "lng": "126.78",
            "stat": "2", "statNm": "near", "chgerType": "02",
        }
        far = dict(near, statId="FAR", chgerId="09", lat="37.566", lng="126.978", statNm="far")
        _store_items(conn, [near, far], NOW, anchors=[SOSA], history=False)
        kept = [row[0] for row in conn.execute("SELECT stat_id FROM ev_chargers ORDER BY stat_id")]
        self.assertEqual(kept, ["NEAR"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_status_hist").fetchone()[0], 0)
        _store_items(conn, [near], NOW, history=True)
        _store_items(conn, [near], NOW + timedelta(minutes=5), history=True)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_status_hist").fetchone()[0], 1)
        changed = dict(near, stat="3")
        _store_items(conn, [changed], NOW + timedelta(minutes=10), history=True)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_status_hist").fetchone()[0], 2)

    def test_history_drops_old_rows_and_caps_each_charger(self) -> None:
        conn = _db(self)
        old = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO ev_status_hist (stat_id, chger_id, fetched_ts, stat) VALUES ('S', '1', ?, '2')",
            (old,),
        )
        for i in range(50):
            ts = (NOW - timedelta(minutes=50 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO ev_status_hist (stat_id, chger_id, fetched_ts, stat) VALUES ('S', '1', ?, '2')",
                (ts,),
            )
        conn.commit()
        prune_status_hist(conn, NOW)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_status_hist").fetchone()[0], 48)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM ev_status_hist WHERE fetched_ts = ?", (old,),
        ).fetchone())


class CleanupTests(unittest.TestCase):
    def test_missing_anchor_tables_are_left_alone(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ev_chargers (stat_id TEXT, chger_id TEXT, lat REAL, lng REAL)"
        )
        conn.execute("INSERT INTO ev_chargers VALUES ('S', '1', 37.5, 127.0)")
        stats = cleanup_public_cache(conn, vacuum=False)
        self.assertEqual(stats["chargers"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_chargers").fetchone()[0], 1)

    def test_far_public_rows_go_and_user_picks_trips_and_charges_stay(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, 't0', 't1', 3)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'c0', 'c1', 'AC', 4)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, 'c0', ?, ?, 'car')",
            (VID, SOSA[0], SOSA[1]),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, 'c0', 'kept')",
            (VID,),
        )
        rows = [
            ("NEAR", "1", SOSA[0], SOSA[1]),
            ("FAR", "1", 37.566, 126.978),
            ("PICK", "7", 37.566, 126.978),
        ]
        conn.executemany(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, updated_ts) VALUES (?, ?, ?, ?, 't')",
            rows,
        )
        conn.executemany(
            "INSERT INTO ev_status (stat_id, chger_id, stat, fetched_ts) VALUES (?, ?, '2', 't')",
            [("NEAR", "1"), ("FAR", "1"), ("PICK", "7")],
        )
        conn.executemany(
            "INSERT INTO ev_status_hist (stat_id, chger_id, fetched_ts, stat) VALUES (?, ?, 't', '2')",
            [("NEAR", "1"), ("FAR", "1"), ("PICK", "7")],
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id, chger_id, source, updated_ts) "
            "VALUES (?, 'c0', 'PICK', '7', 'user', 't')",
            (VID,),
        )
        conn.execute("INSERT INTO ev_region (zcode, next_page, done) VALUES ('11', 2, 0)")
        conn.commit()
        stats = cleanup_public_cache(conn, vacuum=False)
        self.assertEqual(stats["chargers"], 1)
        self.assertEqual(stats["regions"], 1)
        kept = {row[0] for row in conn.execute("SELECT stat_id FROM ev_chargers")}
        self.assertEqual(kept, {"NEAR", "PICK"})
        self.assertIsNone(conn.execute("SELECT 1 FROM ev_status WHERE stat_id = 'FAR'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM ev_status WHERE stat_id = 'PICK'").fetchone())
        self.assertEqual(conn.execute("SELECT km FROM trips").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT kwh_added FROM charges").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT operator FROM charge_meta").fetchone()[0], "kept")
        self.assertEqual(conn.execute("SELECT source FROM charge_charger").fetchone()[0], "user")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ev_region").fetchone()[0], 0)
        again = maybe_cleanup(conn)
        self.assertIsNotNone(again)
        self.assertIsNone(maybe_cleanup(conn))


class DetailTests(unittest.TestCase):
    def test_energy_loss_is_sent_minus_stored_minus_used(self) -> None:
        self.assertAlmostEqual(energy_loss_kwh(10, 7, 2), 1.0)
        self.assertEqual(energy_loss_kwh(10, 8, 3), 0.0)
        self.assertIsNone(energy_loss_kwh(10, None, 1))
        self.assertIsNone(energy_loss_kwh(None, 7, 2))
        conn = _db(self)
        item = {
            "vehicle_id": VID,
            "charge_id": "c1",
            "kind": "AC",
            "battery_kwh": 7,
            "loss_kwh": 2,
            "start_ts": "2026-11-13T00:00:00Z",
            "meta": {"kwh_billed": 10},
        }
        annotate(conn, VID, [item])
        self.assertAlmostEqual(item["sent_kwh"], 10.0)
        self.assertAlmostEqual(item["energy_loss_kwh"], 1.0)
        self.assertFalse(item["energy_loss_estimated"])
        guessed = {
            "kind": "AC", "ac_kwh": 9, "battery_kwh": 7, "loss_kwh": 2,
            "meta": {}, "start_ts": "2026-11-13T00:00:00Z",
        }
        annotate(conn, VID, [guessed])
        self.assertAlmostEqual(guessed["energy_loss_kwh"], 0.0)
        self.assertTrue(guessed["energy_loss_estimated"])
        missing = {"kind": "AC", "battery_kwh": 7, "meta": {"kwh_billed": 10}, "start_ts": "2026-11-13T00:00:00Z"}
        annotate(conn, VID, [missing])
        self.assertIsNone(missing["energy_loss_kwh"])

    def test_event_detail_shape(self) -> None:
        t0 = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
        samples = [(t0, 32.0), (t0 + timedelta(seconds=1), 58.0)]
        grouped = speed_events(samples)
        self.assertEqual(set(grouped), {"accel", "brake", "turn"})
        self.assertIsNone(grouped["turn"])
        details = event_details(samples)
        self.assertEqual(len(details), 1)
        row = details[0]
        self.assertEqual(row["kind"], "accel")
        self.assertEqual(row["ts"], t0)
        self.assertEqual(row["speed_before"], 32.0)
        self.assertEqual(row["speed_after"], 58.0)
        self.assertEqual(row["delta_kph"], 26.0)
        self.assertEqual(row["duration_s"], 1.0)
        self.assertEqual(row["rate_kmh_s"], 26.0)
        self.assertEqual(event_details([(t0, 10.0)]), [])

    def test_trip_speed_serializes_the_detail(self) -> None:
        conn = _db(self)
        start = "2026-11-13T00:00:00Z"
        end = "2026-11-13T00:10:00Z"
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, 1)",
            (VID, start, end),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, '2026-11-13T00:01:00Z', 'VehicleSpeed', '32')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, '2026-11-13T00:01:01Z', 'VehicleSpeed', '58')",
            (VID,),
        )
        conn.commit()
        from web import trip_speed
        payload = trip_speed(conn, VID, start)
        self.assertEqual(len(payload["events"]), 1)
        event = payload["events"][0]
        self.assertEqual(event["kind"], "accel")
        self.assertEqual(event["speed_before"], 32.0)
        self.assertEqual(event["speed_after"], 58.0)
        self.assertEqual(event["delta_kph"], 26.0)
        self.assertIn("duration_s", event)
        self.assertIn("rate_kmh_s", event)
        self.assertNotIn("한국시간", str(event))

    def test_detail_row_order(self) -> None:
        self.assertIn('const APP_VERSION = "v1.15.2"', HTML)
        trip = HTML[HTML.index("function tripSheetHtml"):HTML.index("function openTripSheet")]
        self.assertLess(trip.index('oneRow("평균 속도"'), trip.index('oneRow("최고 속도"'))
        self.assertLess(trip.index('oneRow("최고 속도"'), trip.index('oneRow("급가속"'))
        self.assertLess(trip.index('oneRow("급가속"'), trip.index('oneRow("급제동"'))
        self.assertIn(
            "kvBlock(patternHead) + \"<div class='curve-box' id='trip-sheet-speed'></div>\" + kvBlock(patternTail)",
            trip,
        )
        self.assertLess(trip.index('oneRow("최대 출력"'), trip.index('oneRow("최대 회생"'))
        self.assertLess(trip.index('oneRow("최대 회생"'), trip.index('oneRow("실내 온도"'))
        self.assertIn(
            "kvBlock(energyHead) + \"<div class='curve-box' id='trip-sheet-power'></div>\" + kvBlock(energyTail)",
            trip,
        )
        self.assertIn('foldHtml("운전 패턴"', trip)
        charge = HTML[HTML.index("function chargeSheetHtml"):HTML.index("function openChargeDetail")]
        energy = charge[charge.index('oneRow("충전량"'):]
        labels = ["충전량", "배터리 저장량", "충전 중 사용량", "손실률", "충전효율"]
        positions = [energy.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("차가 받은 양", charge)
        self.assertIn("손실률", charge)
        self.assertNotIn("충전기가 보낸 양", charge)
        self.assertLess(charge.index('keyCell("충전량"'), charge.index('keyCell("금액"'))
        self.assertLess(charge.index("평균 충전 전력"), charge.index("최대 충전 전력"))
        self.assertLess(charge.index("최대 충전 전력"), charge.index("charge-sheet-curve"))
        self.assertNotIn("chargeFlowHtml", charge)
        self.assertNotIn("손실량", charge)
        self.assertIn('foldHtml("충전기"', charge)
        self.assertNotIn("chargerNumberRow", charge)
        self.assertNotIn("id='charger-number'", HTML)
        self.assertNotIn('name="charger_pick"', HTML)
        self.assertNotIn("id='charger-number'", HTML)
        self.assertIn("선택 안 함", HTML)
        self.assertIn('"/api/charges/charger"', HTML)
        self.assertIn("speed-mark", HTML)
        self.assertIn("변화 시간", HTML)
        self.assertIn('timeZone: "Asia/Seoul"', HTML)
        self.assertNotIn("한국시간", HTML)

    def test_charge_flow_diagram_is_gone(self) -> None:
        self.assertNotIn("function chargeFlowModel", HTML)
        self.assertNotIn("class='charge-flow'", HTML)
        self.assertNotIn("id=\"tariff-open\"", HTML)


if __name__ == "__main__":
    unittest.main()
