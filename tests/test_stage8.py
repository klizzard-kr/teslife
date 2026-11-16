"""Stage 8: per-trip records, parked usage, hourly battery, merges, alerts, retention."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from bridge.bridge import convert, rows_from_line
from process import process_db, prune_raw
from process.alerts import run_alerts
from process import _load_events, connect as process_connect
from web import make_server, serve_in_thread

VID = "VIN8"
T0 = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)  # 11/03 07:00 KST


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": iso(t), "name": name, "value": value}


def drive(start: datetime, minutes: int, odo0: float, used0: float, regen0: float, soc0: float, km: float, kwh: float):
    out = [sig(start, "Gear", "D")]
    steps = minutes
    for i in range(1, steps + 1):
        t = start + timedelta(minutes=i)
        out += [
            sig(t, "Odometer", round(odo0 + km * i / steps, 3)),
            sig(t, "LifetimeEnergyUsed", round(used0 + kwh * i / steps, 4)),
            sig(t, "LifetimeEnergyGainedRegen", round(regen0 + 0.1 * kwh * i / steps, 4)),
            sig(t, "BatteryLevel", round(soc0 - 4.0 * i / steps, 3)),
            sig(t, "VehicleSpeed", 40.0 + i),
            sig(t, "PackVoltage", 390.0),
            sig(t, "PackCurrent", 30.0 if i % 3 else -20.0),
            sig(t, "HvacFanSpeed", 3),
            sig(t, "SeatHeaterLeft", 2 if i == 2 else 0),
        ]
    # one hard acceleration, one hard brake, one hard turn (m/s^2, held 3 s at 1 Hz)
    for k in range(3):
        out += [
            sig(start + timedelta(seconds=30 + k), "LongitudinalAcceleration", 4.2),
            sig(start + timedelta(seconds=90 + k), "LongitudinalAcceleration", -4.5),
            sig(start + timedelta(seconds=120 + k), "LateralAcceleration", 5.0),
        ]
    out += [
        sig(start + timedelta(seconds=30), "PedalPosition", 60.0),
        sig(start + timedelta(seconds=33), "LongitudinalAcceleration", 0.5),
        sig(start + timedelta(seconds=90), "BrakePedal", True),
        sig(start + timedelta(seconds=93), "LongitudinalAcceleration", 0.0),
        sig(start + timedelta(seconds=123), "LateralAcceleration", 1.0),
    ]
    end = start + timedelta(minutes=minutes, seconds=10)
    out.append(sig(end, "Gear", "P"))
    return out, end


def scenario() -> list[dict]:
    s = []
    s += [
        sig(T0, "Gear", "P"), sig(T0, "Odometer", 1000.0), sig(T0, "LifetimeEnergyUsed", 300.0),
        sig(T0, "LifetimeEnergyGainedRegen", 50.0), sig(T0, "BatteryLevel", 80.0),
        sig(T0, "RatedRange", 400.0), sig(T0, "OutsideTemp", 5.0), sig(T0, "InsideTemp", 12.0),
        sig(T0, "ModuleTempMin", 8), sig(T0, "ModuleTempMax", 11), sig(T0, "BatteryHeaterOn", False),
        sig(T0, "ChargeState", "Disconnected"), sig(T0, "Locked", True),
    ]
    for n, v in (("TpmsPressureFl", 2.7), ("TpmsPressureFr", 2.7), ("TpmsPressureRl", 2.65), ("TpmsPressureRr", 2.7)):
        s.append(sig(T0 + timedelta(hours=2, minutes=50), n, v))
    a, end1 = drive(T0 + timedelta(hours=3), 20, 1000.0, 300.0, 50.0, 80.0, 12.0, 2.0)
    s += a
    # parked in P with camping/HVAC: energy counter rises 0.5 kWh, battery drops 1%
    p1 = end1 + timedelta(minutes=30)
    s += [sig(p1, "LifetimeEnergyUsed", 302.5), sig(p1, "BatteryLevel", 75.0)]
    b, end2 = drive(end1 + timedelta(minutes=60), 30, 1012.0, 302.5, 50.2, 75.0, 20.0, 3.0)
    s += b
    # charge in the evening
    c0 = end2 + timedelta(hours=8)
    s += [
        sig(c0, "ChargeState", "Charging"), sig(c0, "ACChargingEnergyIn", 0.0),
        sig(c0, "ChargerVoltage", 221.0), sig(c0, "ChargeAmps", 31.0), sig(c0, "ACChargingPower", 6.8),
        sig(c0 + timedelta(minutes=1), "TimeToFullCharge", 3.5),
    ]
    for i in range(1, 7):
        t = c0 + timedelta(minutes=30 * i)
        s += [sig(t, "ACChargingEnergyIn", 3.4 * i), sig(t, "BatteryLevel", 71.0 + 4 * i), sig(t, "ChargerVoltage", 222.0 + i)]
    s.append(sig(c0 + timedelta(hours=3, minutes=5), "ChargeState", "Complete"))
    return s



def _status(port: int, method: str, path: str, body: dict) -> int:
    import json as _j, urllib.error as _e, urllib.request as _r
    req = _r.Request(f"http://127.0.0.1:{port}{path}", method=method, data=_j.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    try:
        with _r.urlopen(req, timeout=5) as r:
            return r.status
    except _e.HTTPError as e:
        return e.code

class Stage8Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "s.db")
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, scenario())
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def rows(self, sql: str, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class TripRecordTests(Stage8Base):
    def test_trip_fields(self) -> None:
        process_db(self.db, seed=False)
        trips = self.rows("SELECT * FROM trips ORDER BY start_ts")
        self.assertEqual(len(trips), 2)
        t = trips[0]
        self.assertAlmostEqual(t["km"], 12.0, places=3)
        # No net battery drop in this fixture, so 쓴 전력 is 0.75 × LifetimeEnergyUsed.
        self.assertAlmostEqual(t["kwh"], 1.5, places=3)
        self.assertAlmostEqual(t["km_per_kwh"], 8.0, places=3)
        self.assertEqual(t["kwh_estimated"], 1)
        self.assertAlmostEqual(t["regen_kwh"], 0.2, places=3)
        self.assertAlmostEqual(t["regen_ratio"], 0.1, places=3)
        self.assertEqual(t["hard_accel"], 0)
        self.assertEqual(t["hard_brake"], 0)
        self.assertIsNone(t["hard_turn"])
        self.assertEqual(t["seat_heat_left_max"], 2)
        self.assertEqual(t["fan_max"], 3)
        self.assertEqual(t["battery_temp_min"], 8)
        self.assertEqual(t["battery_temp_max"], 11)
        self.assertEqual(t["battery_heater_on"], 0)
        self.assertAlmostEqual(t["max_power_kw"], 390 * 30 / 1000.0, places=2)
        self.assertAlmostEqual(t["max_regen_kw"], 390 * 20 / 1000.0, places=2)
        self.assertAlmostEqual(t["max_speed"], 60.0, places=1)
        self.assertEqual(t["start_gear"], "D")
        before = json.loads(t["tire_before"])
        self.assertAlmostEqual(before["rl"], 2.65, places=3)
        self.assertAlmostEqual(t["start_range_km"], 400.0)

    def test_parked_usage_separate_from_trip_energy(self) -> None:
        process_db(self.db, seed=False)
        parked = self.rows("SELECT * FROM parked_usage")
        self.assertEqual(len(parked), 1)
        self.assertAlmostEqual(parked[0]["kwh"], 0.5, places=3)
        self.assertEqual(parked[0]["charged"], 0)
        trips = self.rows("SELECT * FROM trips ORDER BY start_ts")
        self.assertAlmostEqual(trips[1]["kwh"], 2.25, places=3)

    def test_charge_extras(self) -> None:
        process_db(self.db, seed=False)
        c = self.rows("SELECT * FROM charges")[0]
        self.assertAlmostEqual(c["kwh_added"], 20.4, places=3)
        self.assertAlmostEqual(c["max_charger_voltage"], 228.0)
        self.assertAlmostEqual(c["max_charge_amps"], 31.0)
        self.assertAlmostEqual(c["time_to_full_h"], 3.5)
        self.assertAlmostEqual(c["max_charger_kw"], 6.8)

    def test_battery_hourly_kst(self) -> None:
        process_db(self.db, seed=False)
        hours = self.rows("SELECT * FROM battery_hourly ORDER BY hour")
        self.assertTrue(hours)
        self.assertEqual(hours[0]["hour"], "2026-11-03T07")  # 22:00Z = 07:00 KST
        drive_hours = [h for h in hours if h["driving"]]
        self.assertTrue(drive_hours)
        charge_hours = [h for h in hours if h["charging"]]
        self.assertTrue(charge_hours)
        used = sum(h["used_pct"] for h in hours)
        self.assertAlmostEqual(used, 4.0 + 1.0 + 4.0, places=2)
        charged = sum(h["charged_pct"] for h in hours)
        self.assertAlmostEqual(charged, 24.0, places=2)

    def test_incremental_rerun_matches_full(self) -> None:
        process_db(self.db, seed=False)
        first = self.rows("SELECT * FROM trips ORDER BY start_ts")
        stats = process_db(self.db, seed=False)
        self.assertTrue(stats.skipped)
        conn = ingest.connect(self.db)
        more, _ = drive(T0 + timedelta(days=1), 10, 1032.0, 305.5, 50.5, 95.0, 5.0, 1.0)
        ingest.store_signals(conn, more)
        conn.close()
        process_db(self.db, seed=False)
        again = self.rows("SELECT * FROM trips ORDER BY start_ts")
        self.assertEqual(len(again), 3)
        for a, b in zip(first, again[:2]):
            self.assertEqual(a, b)
        self.assertEqual(len(self.rows("SELECT * FROM parked_usage")), 2)

    def test_gear_only_no_auto_merge(self) -> None:
        process_db(self.db, seed=False)
        self.assertEqual(len(self.rows("SELECT * FROM trips")), 2)
        self.assertEqual(self.rows("SELECT * FROM trip_merges"), [])

    def test_retention_keeps_latest_per_signal(self) -> None:
        process_db(self.db, seed=False)
        conn = process_connect(self.db)
        n = prune_raw(conn, now=T0 + timedelta(days=40))
        conn.commit()
        conn.close()
        self.assertGreater(n, 0)
        left = self.rows("SELECT name, COUNT(*) AS n FROM signals GROUP BY name")
        self.assertTrue(all(r["n"] == 1 for r in left))
        self.assertEqual(len(self.rows("SELECT * FROM trips")), 2)
        process_db(self.db, seed=False)
        self.assertEqual(len(self.rows("SELECT * FROM trips")), 2)


class BridgeStage8Tests(unittest.TestCase):
    def test_enum_and_units(self) -> None:
        self.assertEqual(convert("SentryMode", "SentryModeStateArmed"), "Armed")
        self.assertEqual(convert("FdWindow", "WindowStateClosed"), "Closed")
        self.assertIsNone(convert("Gear", "ShiftStateSNA"))
        self.assertIsNone(convert("Gear", "ShiftStateInvalid"))
        self.assertEqual(convert("Gear", "ShiftStateD"), "D")
        self.assertAlmostEqual(convert("RatedRange", 100.0), 160.93, places=2)
        self.assertAlmostEqual(convert("EstBatteryRange", 10.0), 16.09, places=2)
        self.assertEqual(convert("PackCurrent", -12.5), -12.5)
        line = json.dumps({"msg": "record_payload", "data": {
            "Vin": VID, "CreatedAt": "2026-11-01T00:00:00Z", "Location": {"latitude": 1, "longitude": 2},
            "TpmsHardWarnings": False, "ChargePortDoorOpen": True}})
        names = {r["name"] for r in rows_from_line(line)}
        self.assertEqual(names, {"TpmsHardWarnings", "ChargePortDoorOpen"})


class AlertTests(Stage8Base):
    def run_alerts_at(self, now: datetime, sent: list):
        conn = process_connect(self.db)
        try:
            ev = _load_events(conn, VID)
            run_alerts(conn, VID, ev, now=now, sender=lambda p: (sent.append(p) or True, "http 200"))
        finally:
            conn.close()

    def test_trip_end_once_and_stale_silent(self) -> None:
        process_db(self.db, seed=False)
        trips = self.rows("SELECT * FROM trips ORDER BY start_ts")
        end = datetime.strptime(trips[-1]["end_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        sent: list = []
        self.run_alerts_at(end + timedelta(minutes=5), sent)
        kinds = [p["kind"] for p in sent]
        self.assertIn("trip_end", kinds)
        msg = next(p for p in sent if p["kind"] == "trip_end")["message"]
        self.assertIn("km/kWh", msg)
        self.assertIn("psi", msg)
        sent2: list = []
        self.run_alerts_at(end + timedelta(minutes=6), sent2)
        self.assertNotIn("trip_end", [p["kind"] for p in sent2])

    def test_trip_start_and_states(self) -> None:
        conn = ingest.connect(self.db)
        t = T0 + timedelta(days=2)
        ingest.store_signals(conn, [
            sig(t, "SentryMode", "Armed"), sig(t, "Gear", "P"), sig(t, "FdWindow", "Closed"),
        ])
        conn.close()
        sent: list = []
        self.run_alerts_at(t + timedelta(minutes=1), sent)
        self.assertEqual([p for p in sent if p["kind"] == "sentry"], [])  # first sighting silent
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, [
            sig(t + timedelta(minutes=2), "SentryMode", "Off"),
            sig(t + timedelta(minutes=2), "FdWindow", "PartiallyOpen"),
            sig(t + timedelta(minutes=2), "TpmsSoftWarnings", True),
            sig(t + timedelta(minutes=2), "SoftwareUpdateVersion", "2026.44.1"),
        ])
        conn.close()
        self.run_alerts_at(t + timedelta(minutes=3), sent)
        kinds = [p["kind"] for p in sent]
        for k in ("sentry", "open_after_park", "tpms_warning", "software_update"):
            self.assertIn(k, kinds)
        n = len(sent)
        self.run_alerts_at(t + timedelta(minutes=4), sent)
        self.assertEqual(len(sent), n)
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, [sig(t + timedelta(minutes=5), "Gear", "D")])
        conn.close()
        self.run_alerts_at(t + timedelta(minutes=6), sent)
        start = [p for p in sent if p["kind"] == "trip_start"]
        self.assertEqual(len(start), 1)
        self.assertIn("psi", start[0]["message"])

    def test_no_webhook_is_skipped(self) -> None:
        import os
        from process.alerts import webhook_sender
        saved = {k: os.environ.pop(k, None) for k in ("TESLIFE_ALERT_WEBHOOK_URL", "TESLIFE_ALERT_WEBHOOK_KEY")}
        try:
            ok, status = webhook_sender({"kind": "test"})
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertTrue(ok)
        self.assertIn("skipped", status)


class Stage8ApiTests(Stage8Base):
    def setUp(self) -> None:
        super().setUp()
        process_db(self.db, seed=False)
        self.server = make_server(self.db, port=0, process_interval=0, seed=False)
        self.port = self.server.server_address[1]
        serve_in_thread(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def write(self, method: str, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_no_merge_or_split(self) -> None:
        # merging/splitting is gone from the API; trips are always listed one by one
        _, page = self.get("/api/trips?month=2026-11&limit=20&offset=0")
        self.assertEqual(page["total"], 2)
        starts = [t["start_ts"] for t in page["trips"]]
        self.assertGreater(starts[0], starts[1])  # newest first
        self.assertTrue(all("merged" not in t for t in page["trips"]))
        self.assertEqual(_status(self.port, "POST", "/api/trip-merges", {"start_ts": starts}), 405)
        self.assertEqual(_status(self.port, "DELETE", "/api/trip-merges", {"merge_id": "m-" + starts[1]}), 405)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.executemany("INSERT INTO trip_merges (vehicle_id, merge_id, start_ts, created_at) VALUES (?, ?, ?, ?)",
                             [(r[0], "m-x", r[1], "2026-11-01T00:00:00Z") for r in
                              conn.execute("SELECT vehicle_id, start_ts FROM trips").fetchall()])
        conn.close()
        _, page = self.get("/api/trips?month=2026-11")
        self.assertEqual(page["total"], 2)  # a leftover merge row is ignored
        self.assertEqual(len(self.rows("SELECT * FROM trips")), 2)

    def test_power_curve_battery_and_parked(self) -> None:
        _, page = self.get("/api/trips")
        ts = page["trips"][-1]["start_ts"]
        status, power = self.get(f"/api/trips/{ts}/power")
        self.assertEqual(status, 200)
        self.assertTrue(power["points"])
        self.assertTrue(any(p["kw"] < 0 for p in power["points"]))
        status, _ = self.get("/api/trips/2000-01-01T00:00:00Z/power")
        self.assertEqual(status, 404)
        _, ch = self.get("/api/charges")
        cid = ch["charges"][0]["charge_id"]
        status, curve = self.get(f"/api/charges/{cid}/curve")
        self.assertEqual(status, 200)
        self.assertTrue(any(p["voltage"] for p in curve["points"]))
        status, daily = self.get("/api/battery/daily?from=2026-11-01&to=2026-11-07")
        self.assertEqual(status, 200)
        self.assertEqual(len(daily["days"]), 7)
        day = next(d for d in daily["days"] if d["date"] == "2026-11-03")
        self.assertAlmostEqual(day["used_pct"], 9.0, places=2)
        _, month = self.get("/api/battery/daily?month=2026-11")
        self.assertIsNotNone(month["avg_used_pct"])
        _, year = self.get("/api/battery/daily?year=2026")
        self.assertEqual(year["months"][0]["month"], "2026-11")
        status, hourly = self.get("/api/battery/hourly?date=2026-11-03")
        self.assertEqual(status, 200)
        self.assertEqual(len(hourly["hours"]), 24)
        self.assertEqual(hourly["hours"][7]["pct"], 80.0)
        status, _ = self.get("/api/battery/hourly?date=bad")
        self.assertEqual(status, 400)
        _, parked = self.get("/api/parked?month=2026-11")
        self.assertAlmostEqual(parked["total_kwh"], 0.5, places=3)
        _, summary = self.get("/api/summary?month=2026-11")
        self.assertAlmostEqual(summary["avg_km_per_kwh"], 8.533, places=3)
        self.assertAlmostEqual(summary["parked_kwh"], 0.5, places=3)


if __name__ == "__main__":
    unittest.main()
