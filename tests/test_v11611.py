"""v1.16.11: a Complete charge is final even while the cable stays in."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from ledger import ensure_schema
from process import (
    PROCESS_VERSION,
    Events,
    _charge_sessions,
    _load_events,
    _meta_set,
    connect,
    ensure_process_schema,
    format_ts,
    process_db,
)
from process.alerts import run_alerts

VID = "car-complete"
START = datetime(2026, 11, 21, 11, 47, tzinfo=timezone.utc)
DONE = datetime(2026, 11, 21, 14, 28, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sig(t: datetime, name: str, value) -> dict:
    return {"vehicle_id": VID, "timestamp": _iso(t), "name": name, "value": value}


def _awake(rows: list[dict], start: datetime, end: datetime) -> None:
    beat = start + timedelta(minutes=4)
    while beat < end:
        rows.append(_sig(beat, "OutsideTemp", 8.0))
        beat += timedelta(minutes=4)


def _complete_session(resume: bool = False) -> list[dict]:
    """Target reached, cable left in, and the car keeps sending."""
    rows = [
        _sig(START - timedelta(minutes=2), "Gear", "P"),
        _sig(START - timedelta(minutes=2), "ChargePortDoorOpen", True),
        _sig(START - timedelta(minutes=2), "FastChargerPresent", False),
        _sig(START, "DetailedChargeState", "Charging"),
        _sig(START, "ChargeState", "Enable"),
        _sig(START, "BatteryLevel", 40.0),
        _sig(START, "ChargeLimitSoc", 80.0),
        _sig(START, "ACChargingEnergyIn", 10.0),
        _sig(START, "ACChargingPower", 7.0),
        _sig(START, "ChargerVoltage", 220.0),
        _sig(START, "ChargeAmps", 32.0),
        _sig(DONE, "DetailedChargeState", "Complete"),
        _sig(DONE, "ChargeState", "Idle"),
        _sig(DONE, "BatteryLevel", 80.0),
        _sig(DONE, "ACChargingEnergyIn", 28.0),
        _sig(DONE, "ACChargingPower", 0.0),
    ]
    _awake(rows, START, NOW)
    if resume:
        again = DONE + timedelta(minutes=20)
        again_end = again + timedelta(minutes=15)
        rows += [
            _sig(again, "DetailedChargeState", "Charging"),
            _sig(again, "ChargeState", "Enable"),
            _sig(again, "ACChargingPower", 7.0),
            _sig(again, "ACChargingEnergyIn", 28.0),
            _sig(again_end, "DetailedChargeState", "Complete"),
            _sig(again_end, "BatteryLevel", 85.0),
            _sig(again_end, "ACChargingEnergyIn", 31.0),
            _sig(again_end, "ACChargingPower", 0.0),
        ]
    return rows


class CompleteChargeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, rows: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, rows)
        conn.close()

    def _rows(self):
        conn = connect(self.db)
        try:
            return conn.execute(
                "SELECT start_ts, end_ts, kind, start_soc, end_soc, kwh_added "
                "FROM charges WHERE vehicle_id = ? ORDER BY start_ts",
                (VID,),
            ).fetchall()
        finally:
            conn.close()

    def test_complete_with_the_port_open_is_stored_at_the_stop(self) -> None:
        self._store(_complete_session())
        conn = connect(self.db)
        ensure_process_schema(conn)
        _meta_set(conn, f"version:{VID}", PROCESS_VERSION)
        _meta_set(conn, f"anchor:{VID}", _iso(START))
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
            "VALUES (?, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', 'AC', 1)"
            ,
            (VID,),
        )
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=NOW)
        rows = [row for row in self._rows() if row[0] != "2026-10-01T00:00:00Z"]
        self.assertEqual(len(rows), 1)
        start, end, kind, soc0, soc1, kwh = rows[0]
        self.assertEqual(start, _iso(START))
        self.assertEqual(end, _iso(DONE))
        self.assertEqual(kind, "AC")
        self.assertAlmostEqual(soc0, 40.0)
        self.assertAlmostEqual(soc1, 80.0)
        self.assertAlmostEqual(kwh, 18.0, places=2)

    def test_charging_again_after_complete_is_a_new_row(self) -> None:
        self._store(_complete_session(resume=True))
        process_db(self.db, seed=False, now=NOW)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], _iso(START))
        self.assertEqual(rows[0][1], _iso(DONE))
        self.assertEqual(rows[1][0], _iso(DONE + timedelta(minutes=20)))
        self.assertEqual(rows[1][1], _iso(DONE + timedelta(minutes=35)))

    def test_a_stopped_pause_still_stays_one_session(self) -> None:
        start = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
        pause = start + timedelta(minutes=20)
        again = start + timedelta(minutes=40)
        end = start + timedelta(minutes=70)
        rows = [
            (start - timedelta(minutes=1), "Gear", "P"),
            (start - timedelta(minutes=1), "ChargePortDoorOpen", True),
            (start, "DetailedChargeState", "Charging"),
            (pause, "DetailedChargeState", "Stopped"),
            (again, "DetailedChargeState", "Charging"),
            (end, "DetailedChargeState", "Complete"),
        ]
        beat = start
        while beat < end:
            rows.append((beat, "OutsideTemp", 8.0))
            beat += timedelta(minutes=4)
        spans, open_start = _charge_sessions(Events(sorted(rows)))
        self.assertIsNone(open_start)
        self.assertEqual(spans, [(start, end)])

    def test_twelve_stored_charges_keep_count_energy_and_amount(self) -> None:
        self._store(_complete_session())
        conn = connect(self.db)
        ensure_process_schema(conn)
        ensure_schema(conn)
        stored = []
        for i in range(12):
            start = f"2026-10-{i + 1:02d}T01:00:00Z"
            kwh = 3.5 + i
            amount = 1000 * (i + 1)
            conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added) "
                "VALUES (?, ?, ?, 'AC', 20, 40, ?)",
                (VID, start, f"2026-10-{i + 1:02d}T03:00:00Z", kwh),
            )
            conn.execute(
                "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw) VALUES (?, ?, ?)",
                (VID, start, amount),
            )
            conn.execute(
                "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
                "VALUES (?, ?, '2026-10-01', 'charging', ?, 'auto')",
                (VID, "auto-charge:" + start, amount),
            )
            stored.append((start, kwh, amount))
        _meta_set(conn, f"version:{VID}", PROCESS_VERSION)
        _meta_set(conn, f"anchor:{VID}", _iso(START))
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=NOW)
        conn = connect(self.db)
        try:
            rows = conn.execute(
                "SELECT start_ts, kwh_added FROM charges WHERE start_ts < ? ORDER BY start_ts",
                (_iso(START),),
            ).fetchall()
            amounts = conn.execute(
                "SELECT id, amount_krw FROM ledger WHERE id LIKE 'auto-charge:2026-10-%' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([(row[0], row[1]) for row in rows], [(item[0], item[1]) for item in stored])
        self.assertEqual(
            [(row[0], row[1]) for row in amounts],
            [("auto-charge:" + item[0], item[2]) for item in stored],
        )
        self.assertEqual(len(rows), 12)

    def test_a_full_rebuild_does_not_rewrite_the_twelve(self) -> None:
        self._store(_complete_session())
        conn = connect(self.db)
        ensure_process_schema(conn)
        ensure_schema(conn)
        for i in range(12):
            start = f"2026-09-{i + 1:02d}T01:00:00Z"
            conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
                "VALUES (?, ?, ?, 'AC', ?)",
                (VID, start, f"2026-09-{i + 1:02d}T02:00:00Z", 2.0 + i),
            )
            conn.execute(
                "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw) VALUES (?, ?, ?)",
                (VID, start, 500 * (i + 1)),
            )
            conn.execute(
                "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, source) "
                "VALUES (?, ?, '2026-09-01', 'charging', ?, 'auto')",
                (VID, "auto-charge:" + start, 500 * (i + 1)),
            )
        _meta_set(conn, f"version:{VID}", "20")
        conn.commit()
        before = conn.execute(
            "SELECT start_ts, kwh_added FROM charges ORDER BY start_ts"
        ).fetchall()
        conn.close()
        process_db(self.db, seed=False, now=NOW)
        conn = connect(self.db)
        try:
            after = conn.execute(
                "SELECT start_ts, kwh_added FROM charges WHERE start_ts < ? ORDER BY start_ts",
                (_iso(START),),
            ).fetchall()
            amounts = [
                row[0] for row in conn.execute(
                    "SELECT amount_krw FROM ledger WHERE id LIKE 'auto-charge:2026-09-%' ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(after, before)
        self.assertEqual(amounts, [500 * (i + 1) for i in range(12)])

    def test_end_notice_is_quiet_after_an_hour(self) -> None:
        self._store(_complete_session())
        conn = connect(self.db)
        ensure_process_schema(conn)
        _meta_set(conn, f"version:{VID}", PROCESS_VERSION)
        _meta_set(conn, f"anchor:{VID}", _iso(START))
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
            "VALUES (?, '2026-10-02T00:00:00Z', '2026-10-02T01:00:00Z', 'AC', 1)",
            (VID,),
        )
        conn.commit()
        conn.close()
        process_db(self.db, seed=False, now=NOW)
        sent: list[dict] = []
        conn = connect(self.db)
        try:
            ev = _load_events(conn, VID)
            run_alerts(
                conn, VID, ev, now=NOW,
                sender=lambda payload: (sent.append(payload) or True, "http 200"),
            )
        finally:
            conn.close()
        self.assertFalse([item for item in sent if item["kind"] == "charge_end"])
