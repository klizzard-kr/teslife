"""v1.14.3: one AC session across a port-open pause, and composed trip-end rows."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import connect, process_db
from process.merge_split_charge import DROP, KEEP, LEDGER, main as merge_main
from web.notice_view import present
from web.notify import _insert, list_notifications

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-split"
KST = timezone(timedelta(hours=9))


def _utc(hour: int, minute: int, second: int) -> datetime:
    local = datetime(2026, 11, 15, hour, minute, second, tzinfo=KST)
    return local.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": _iso(t), "name": name, "value": value}


def _evening(awake: bool = True) -> list[dict]:
    """21:00:35–21:09:58 then 21:26:44–21:37:02. The port is sampled only when it changes.

    `awake` leaves a signal at least every four minutes through the pause, which is
    what an awake car does. A later Idle and a 66.2% sample must not move the end.
    """
    start = _utc(21, 0, 35)
    pause = _utc(21, 9, 58)
    reset = _utc(21, 10, 46)
    again = _utc(21, 26, 44)
    end = _utc(21, 37, 2)
    closed = _utc(21, 37, 50)
    opened = _utc(20, 59, 43)
    rows = [
        _sig(opened, "Gear", "P"),
        _sig(opened, "ChargePortDoorOpen", True),
        _sig(opened, "FastChargerPresent", False),
        _sig(opened, "Location", {"lat": 37.48, "lon": 126.78}),
        _sig(start, "DetailedChargeState", "Charging"),
        _sig(start, "ChargeState", "Enable"),
        _sig(start, "BatteryLevel", 63.3),
        _sig(start, "ACChargingEnergyIn", 10.0),
        _sig(start, "DCChargingEnergyIn", 5.0),
        _sig(start, "ACChargingPower", 6.7),
        _sig(start, "ChargerVoltage", 220.0),
        _sig(start, "ChargeAmps", 32.0),
        _sig(pause, "ACChargingPower", 6.7),
        _sig(pause, "DetailedChargeState", "Disconnected"),
        _sig(pause, "BatteryLevel", 64.8),
        _sig(pause, "ACChargingEnergyIn", 11.01),
        _sig(pause, "DCChargingEnergyIn", 5.9),
        _sig(reset, "ACChargingEnergyIn", 0.0),
        _sig(reset, "ACChargingPower", 0.0),
        _sig(again, "DetailedChargeState", "Charging"),
        _sig(again, "ACChargingPower", 6.7),
        _sig(again, "ChargerVoltage", 221.0),
        _sig(again, "ChargeAmps", 32.0),
        _sig(again, "FastChargerPresent", False),
        _sig(end, "ACChargingPower", 6.7),
        _sig(end, "DetailedChargeState", "Disconnected"),
        _sig(end, "BatteryLevel", 66.3),
        _sig(end, "ACChargingEnergyIn", 0.99),
        _sig(end, "DCChargingEnergyIn", 6.5),
        _sig(closed, "ChargePortDoorOpen", False),
        _sig(_utc(21, 55, 12), "ChargeState", "Idle"),
        _sig(_utc(21, 55, 12), "BatteryLevel", 66.2),
    ]
    if awake:
        beat = pause + timedelta(minutes=4)
        while beat < again:
            rows.append(_sig(beat, "OutsideTemp", 8.0))
            beat += timedelta(minutes=4)
    return rows


class SplitChargeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening())
        conn.close()

    def _rows(self):
        conn = connect(self.db)
        try:
            return conn.execute(
                "SELECT start_ts, end_ts, kind, start_soc, end_soc, kwh_added FROM charges ORDER BY start_ts"
            ).fetchall()
        finally:
            conn.close()

    def test_pause_with_the_port_open_is_one_ac_charge(self) -> None:
        self._store()
        conn = connect(self.db)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'DC', 64.8, 66.3, 0.5)",
            (VID, DROP, _iso(_utc(21, 37, 2))),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
            "VALUES (?, ?, '2026-11-15', 'charging', 0, 'auto')",
            (VID, LEDGER),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, 'old')",
            (VID, DROP),
        )
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        start, end, kind, soc0, soc1, kwh = rows[0]
        self.assertEqual(start, KEEP)
        self.assertEqual(end, _iso(_utc(21, 37, 2)))
        self.assertEqual(kind, "AC")
        self.assertAlmostEqual(soc0, 63.3)
        self.assertAlmostEqual(soc1, 66.3)
        self.assertAlmostEqual(kwh, 2.0, places=2)
        conn = connect(self.db)
        try:
            self.assertIsNone(conn.execute("SELECT id FROM ledger WHERE id = ?", (LEDGER,)).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT charge_id FROM charge_meta WHERE charge_id = ?", (DROP,)
            ).fetchone())
            anchor = conn.execute("SELECT value FROM process_meta WHERE key = ?", (f"anchor:{VID}",)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(anchor, KEEP)
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        again = self._rows()
        self.assertEqual([(r[0], r[1], r[2]) for r in again], [(start, end, kind)])

    def test_drive_between_pauses_stays_two_sessions(self) -> None:
        rows = _evening()
        mid = _utc(21, 15, 0)
        rows.append(_sig(mid, "Gear", "D"))
        rows.append(_sig(mid + timedelta(minutes=1), "Gear", "P"))
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, rows)
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        got = self._rows()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0][0], KEEP)
        self.assertEqual(got[1][0], _iso(_utc(21, 26, 44)))

    def test_five_minutes_offline_does_not_join(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening(awake=False))
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        self.assertEqual(len(self._rows()), 2)

    def test_a_false_port_between_does_not_join(self) -> None:
        rows = _evening() + [_sig(_utc(21, 15, 0), "ChargePortDoorOpen", False)]
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, rows)
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        self.assertEqual(len(self._rows()), 2)

    def test_existing_rows_keep_their_end_unless_merged(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening())
        from process import ensure_process_schema
        ensure_process_schema(conn)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 63.3, 64.8, 1.01)",
            (VID, KEEP, "2026-11-15T12:09:58Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'DC', 64.8, 66.3, 0.99)",
            (VID, DROP, "2026-11-15T12:37:02Z"),
        )
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        got = self._rows()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], KEEP)
        self.assertEqual(got[0][1], _iso(_utc(21, 37, 2)))
        self.assertEqual(got[0][2], "AC")
        self.assertAlmostEqual(got[0][3], 63.3)
        self.assertAlmostEqual(got[0][4], 66.3)

    def test_after_three_hours_nothing_is_rewritten(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening())
        from process import ensure_process_schema
        ensure_process_schema(conn)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 63.3, 64.8, 1.01)",
            (VID, KEEP, "2026-11-15T12:09:58Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'DC', 64.8, 66.3, 0.99)",
            (VID, DROP, "2026-11-15T12:37:02Z"),
        )
        conn.commit()
        before = conn.execute(
            "SELECT start_ts, end_ts, kind, start_soc, end_soc, kwh_added FROM charges ORDER BY start_ts"
        ).fetchall()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0) + timedelta(hours=6))
        conn = connect(self.db)
        try:
            after = conn.execute(
                "SELECT start_ts, end_ts, kind, start_soc, end_soc, kwh_added FROM charges ORDER BY start_ts"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_full_rebuild_with_trips_between_days_is_not_one_span(self) -> None:
        day1_start = datetime(2026, 10, 30, 11, 54, 46, tzinfo=timezone.utc)
        day1_end = datetime(2026, 10, 30, 13, 0, 0, tzinfo=timezone.utc)
        drive = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone.utc)
        day2_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
        day2_end = datetime(2026, 11, 1, 11, 0, 0, tzinfo=timezone.utc)

        def at(t: datetime, name: str, value) -> dict:
            return {"vehicle_id": VID, "timestamp": _iso(t), "name": name, "value": value}

        rows = [
            at(day1_start - timedelta(minutes=5), "Gear", "P"),
            at(day1_start - timedelta(minutes=5), "ChargePortDoorOpen", True),
            at(day1_start - timedelta(minutes=5), "Location", {"lat": 37.48, "lon": 126.78}),
            at(day1_start, "DetailedChargeState", "Charging"),
            at(day1_start, "BatteryLevel", 40.0),
            at(day1_start, "ACChargingEnergyIn", 1.0),
            at(day1_end, "DetailedChargeState", "Complete"),
            at(day1_end, "ACChargingEnergyIn", 2.0),
            at(drive, "Gear", "D"),
            at(drive, "VehicleSpeed", 40.0),
            at(drive + timedelta(minutes=20), "Location", {"lat": 37.60, "lon": 127.00}),
            at(drive + timedelta(minutes=30), "Gear", "P"),
            at(day2_start, "DetailedChargeState", "Charging"),
            at(day2_start, "BatteryLevel", 30.0),
            at(day2_start, "ACChargingEnergyIn", 3.0),
            at(day2_end, "DetailedChargeState", "Complete"),
            at(day2_end, "ACChargingEnergyIn", 4.0),
        ]
        rows.extend(_evening())
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, rows)
        from process import _charge_sessions, _load_events, _meta_set, ensure_process_schema
        ensure_process_schema(conn)
        ev = _load_events(conn, VID, None)
        spans, open_start = _charge_sessions(ev, conn, VID)
        self.assertIsNone(open_start)
        self.assertEqual(len(spans), 3)
        self.assertEqual(_iso(spans[0][0]), "2026-10-30T11:54:46Z")
        self.assertEqual(_iso(spans[0][1]), "2026-10-30T13:00:00Z")
        self.assertEqual(_iso(spans[1][0]), "2026-11-01T10:00:00Z")
        self.assertEqual(_iso(spans[1][1]), "2026-11-01T11:00:00Z")
        self.assertEqual(_iso(spans[2][0]), KEEP)
        self.assertEqual(_iso(spans[2][1]), "2026-11-15T12:37:02Z")
        for start, end in spans:
            self.assertLess(end - start, timedelta(hours=6))
        for start_ts, end_ts, kind, soc0, soc1, kwh in (
            ("2026-10-30T11:54:46Z", "2026-10-30T13:00:00Z", "AC", 40, 41, 1),
            ("2026-11-01T10:00:00Z", "2026-11-01T11:00:00Z", "AC", 30, 40, 1),
            (KEEP, "2026-11-15T12:09:58Z", "AC", 63.3, 64.8, 1.01),
            (DROP, "2026-11-15T12:37:02Z", "DC", 64.8, 66.3, 0.99),
        ):
            conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (VID, start_ts, end_ts, kind, soc0, soc1, kwh),
            )
        _meta_set(conn, f"version:{VID}", "20")
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        got = self._rows()
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0][0], "2026-10-30T11:54:46Z")
        self.assertEqual(got[0][1], "2026-10-30T13:00:00Z")
        self.assertEqual(got[1][0], "2026-11-01T10:00:00Z")
        self.assertEqual(got[1][1], "2026-11-01T11:00:00Z")
        self.assertEqual(got[2][0], KEEP)
        self.assertEqual(got[2][1], "2026-11-15T12:37:02Z")
        self.assertEqual(got[2][2], "AC")
        self.assertAlmostEqual(got[2][3], 63.3)
        self.assertAlmostEqual(got[2][4], 66.3)

    def test_speed_or_a_far_place_does_not_join(self) -> None:
        for name, value in (
            ("VehicleSpeed", 12.0),
            ("Location", {"lat": 37.55, "lon": 126.98}),
        ):
            with self.subTest(name=name):
                self.tmp.cleanup()
                self.setUp()
                rows = _evening() + [_sig(_utc(21, 15, 0), name, value)]
                conn = ingest.connect(self.db)
                ingest.store_signals(conn, rows)
                conn.close()
                process_db(self.db, seed=False, now=_utc(21, 45, 0))
                self.assertEqual(len(self._rows()), 2)

    def test_a_stored_trip_does_not_join(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening())
        from process import ensure_process_schema
        ensure_process_schema(conn)
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, 1.2)",
            (VID, _iso(_utc(21, 12, 0)), _iso(_utc(21, 20, 0))),
        )
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        self.assertEqual(len(self._rows()), 2)

    def test_old_charges_survive_a_full_rebuild(self) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, _evening())
        from ledger import ensure_schema
        from process import ensure_process_schema
        ensure_process_schema(conn)
        ensure_schema(conn)
        old_a = "2026-11-01T01:00:00Z"
        old_b = "2026-11-01T05:00:00Z"
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 20, 40, 10)",
            (VID, old_a, "2026-11-01T02:00:00Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 40, 55, 8)",
            (VID, old_b, "2026-11-01T06:00:00Z"),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, total_krw) VALUES (?, ?, '그대로', 4000)",
            (VID, old_a),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
            "VALUES (?, ?, '2026-11-01', 'charging', 4000, 'auto')",
            (VID, "auto-charge:" + old_a),
        )
        conn.execute("DELETE FROM process_meta")
        conn.commit()
        before = conn.execute(
            "SELECT start_ts, end_ts, kind, kwh_added FROM charges WHERE start_ts IN (?, ?) ORDER BY start_ts",
            (old_a, old_b),
        ).fetchall()
        conn.close()
        process_db(self.db, seed=False, now=_utc(21, 45, 0))
        conn = connect(self.db)
        try:
            after = conn.execute(
                "SELECT start_ts, end_ts, kind, kwh_added FROM charges WHERE start_ts IN (?, ?) ORDER BY start_ts",
                (old_a, old_b),
            ).fetchall()
            meta = conn.execute(
                "SELECT operator, total_krw FROM charge_meta WHERE charge_id = ?", (old_a,)
            ).fetchone()
            amount = conn.execute(
                "SELECT amount_krw FROM ledger WHERE id = ?", ("auto-charge:" + old_a,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(before, after)
        self.assertEqual(tuple(meta), ("그대로", 4000))
        self.assertEqual(amount[0], 4000)

    def test_active_s_column_does_not_drop_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE charges (vehicle_id TEXT NOT NULL, start_ts TEXT NOT NULL, "
            "end_ts TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY (vehicle_id, start_ts))"
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES ('v', 's', 'e', 'AC')"
        )
        conn.commit()
        from process import ensure_process_schema
        ensure_process_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(charges)")}
        self.assertIn("active_s", cols)
        self.assertEqual(
            conn.execute("SELECT vehicle_id, start_ts, end_ts, kind FROM charges").fetchone(),
            ("v", "s", "e", "AC"),
        )
        self.assertIsNone(conn.execute("SELECT active_s FROM charges").fetchone()[0])


class ComposedTripEndTests(unittest.TestCase):
    def test_composed_trip_end_keeps_the_sentence_and_fills_rows(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        conn = connect(Path(folder) / "signals.db")
        self.addCleanup(conn.close)
        end = "2026-11-15T01:03:00Z"
        notice = "trip_end:2026-11-15T00:52:00Z"
        samples = [
            ("BatteryLevel", 65.2),
            ("EstBatteryRange", 281.6),
            ("TpmsPressureFl", 2.7),
            ("TpmsPressureFr", 2.9),
            ("TpmsPressureRl", 2.8),
            ("TpmsPressureRr", 2.85),
        ]
        for name, value in samples:
            conn.execute(
                "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, ?, ?)",
                (VID, end, name, json.dumps(value)),
            )
        when = datetime(2026, 11, 15, 1, 3, tzinfo=timezone.utc)
        self.assertTrue(_insert(
            conn, VID, notice, "driving", "집에 도착했어요", "5.3 km",
            "Home", "5.3 km", "#trips", when,
            [["회생", "0.4 kWh"], ["급가속", "1회"], ["급제동", "0회"], ["거리", "5.3 km"]],
        ))
        conn.execute(
            "UPDATE notifications SET summary = ?, composed = 1 WHERE id = ?",
            ("가까운 집을 다녀왔어요", notice),
        )
        conn.commit()
        before = conn.execute(
            "SELECT title, body, summary, details, composed FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        shown = list_notifications(conn, VID)["notifications"][0]
        self.assertEqual(shown["display_title"], "집에 도착했어요")
        self.assertEqual(shown["display_body"], "가까운 집을 다녀왔어요")
        labels = [pair[0] for pair in shown["display_details"]]
        self.assertEqual(labels, [
            "거리", "배터리", "주행가능", "회생", "급가속", "급제동",
            "앞 왼쪽", "앞 오른쪽", "뒤 왼쪽", "뒤 오른쪽",
        ])
        after = conn.execute(
            "SELECT title, body, summary, details, composed FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        self.assertEqual(before, after)
        view = present({
            "id": notice, "title": "집에 도착했어요", "summary": "가까운 집을 다녀왔어요",
            "composed": 1, "body": "5.3 km", "ts": end, "details": [],
        })
        self.assertEqual(view["title"], "집에 도착했어요")
        self.assertEqual(view["summary"], "가까운 집을 다녀왔어요")


class ReadTabTests(unittest.TestCase):
    def test_mark_all_read_is_the_open_tab_only(self) -> None:
        self.assertIn("알림 읽음: 모두 읽음은 보고 있는 탭만 읽음으로 표시해요", HTML)
        self.assertIn("주행 알림: 봇이 다듬은 종료 알림에도 배터리, 주행가능거리, 공기압을 보여요", HTML)
        self.assertIn(">충전 멈춤<", HTML)


class BackupCheckTests(unittest.TestCase):
    def test_audit_on_a_copy_joins_only_the_recent_pair(self) -> None:
        from process.check_charge_merge import KEEP, audit

        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        db = Path(folder) / "signals.db"
        conn = ingest.connect(db)
        ingest.store_signals(conn, _evening())
        from process import ensure_process_schema
        from ledger import ensure_schema
        ensure_process_schema(conn)
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 10, 20, 4)",
            (VID, "2026-11-01T01:00:00Z", "2026-11-01T02:00:00Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'AC', 63.3, 64.8, 1.01)",
            (VID, KEEP, "2026-11-15T12:09:58Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
            "VALUES (?, ?, ?, 'DC', 64.8, 66.3, 0.5)",
            (VID, DROP, "2026-11-15T12:37:02Z"),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, total_krw) VALUES (?, ?, '옛', 1000)",
            (VID, "2026-11-01T01:00:00Z"),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, total_krw) VALUES (?, ?, '저녁', 1200)",
            (VID, KEEP),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, total_krw) VALUES (?, ?, '저녁', 800)",
            (VID, DROP),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
            "VALUES (?, ?, '2026-11-01', 'charging', 1000, 'auto')",
            (VID, "auto-charge:2026-11-01T01:00:00Z"),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
            "VALUES (?, ?, '2026-11-15', 'charging', 1200, 'auto')",
            (VID, "auto-charge:" + KEEP),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
            "VALUES (?, ?, '2026-11-15', 'charging', 800, 'auto')",
            (VID, LEDGER),
        )
        conn.commit()
        conn.close()
        ok, lines = audit(db)
        self.assertTrue(ok, "\n".join(lines))
        self.assertIn("removed only " + DROP, "\n".join(lines))
        self.assertIn("12:37:02Z AC", "\n".join(lines))
        late, late_lines = audit(db, _utc(21, 45, 0) + timedelta(hours=6))
        self.assertTrue(late, "\n".join(late_lines))
        self.assertIn("nothing changed", "\n".join(late_lines))
        real_now, real_lines = audit(db, datetime.now(timezone.utc))
        self.assertTrue(real_now, "\n".join(real_lines))
        self.assertIn("nothing changed", "\n".join(real_lines))
        from process.merge_split_charge import main as merge_main
        self.assertEqual(merge_main(["--db", str(db), "--vehicle", VID, "--apply"]), 0)
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT end_ts, kind, start_soc, end_soc, kwh_added FROM charges WHERE start_ts = ?",
                (KEEP,),
            ).fetchone()
            self.assertEqual(row[0], "2026-11-15T12:37:02Z")
            self.assertEqual(row[1], "AC")
            self.assertAlmostEqual(row[2], 63.3)
            self.assertAlmostEqual(row[3], 66.3)
            self.assertAlmostEqual(row[4], 2.0, places=2)
            self.assertIsNone(conn.execute("SELECT 1 FROM charges WHERE start_ts = ?", (DROP,)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT end_ts, kind, kwh_added FROM charges WHERE start_ts = ?",
                ("2026-11-01T01:00:00Z",),
            ).fetchone(), ("2026-11-01T02:00:00Z", "AC", 4))
        finally:
            conn.close()


class MergeScriptTests(unittest.TestCase):
    def test_dry_run_names_the_second_row_and_does_not_write(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        db = Path(folder) / "signals.db"
        conn = connect(db)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc) "
            "VALUES (?, ?, ?, 'AC', 63.3, 64.8)",
            (VID, KEEP, "2026-11-15T12:09:58Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc) "
            "VALUES (?, ?, ?, 'DC', 64.8, 66.3)",
            (VID, DROP, "2026-11-15T12:37:02Z"),
        )
        conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, source) VALUES (?, ?, '2026-11-15', 'charging', 'auto')",
            (VID, LEDGER),
        )
        conn.commit()
        conn.close()
        from io import StringIO
        import contextlib
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = merge_main(["--db", str(db), "--vehicle", VID])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("dry-run", text)
        self.assertIn(DROP, text)
        self.assertIn(LEDGER, text)
        conn = connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], 2)
            self.assertIsNotNone(conn.execute("SELECT id FROM ledger WHERE id = ?", (LEDGER,)).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
