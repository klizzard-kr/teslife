"""v1.14.8: bot-only port prices, status matching, one charge-start per park."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import PROCESS_VERSION, Events, connect, format_ts
from process.alerts import check_charge_start, check_port_open_rates
from process.evcharger import (
    _fill,
    changed_to_charging,
    ensure_schema,
    match_status_change,
    snapshot_port_status,
)
from web.notify import ensure_schema as ensure_notices
from web.tariff import ensure_schema as ensure_tariff

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-port"
LAT, LON = 37.5, 127.0
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


def _opener(box: list):
    def open_(req, timeout=5):
        box.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    return open_


def _db() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = connect(tmp.name)
    ensure_schema(conn)
    ensure_tariff(conn)
    ensure_notices(conn)
    return conn


def _charger(conn, stat_id, chger_id, lat, kw, name):
    conn.execute(
        "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, updated_ts, addr) "
        "VALUES (?, ?, ?, ?, '04', ?, ?, ?, ?, '')",
        (stat_id, chger_id, lat, LON, kw, name, name, format_ts(NOW)),
    )
    conn.execute(
        "INSERT INTO ev_status (stat_id, chger_id, stat, fetched_ts) VALUES (?, ?, '2', ?)",
        (stat_id, chger_id, format_ts(NOW)),
    )


class PortRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.get("BOT_NOTIFY_WEBHOOK_URL")
        os.environ["BOT_NOTIFY_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
        self.conn = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._env is None:
            os.environ.pop("BOT_NOTIFY_WEBHOOK_URL", None)
        else:
            os.environ["BOT_NOTIFY_WEBHOOK_URL"] = self._env

    def _ev(self, parked: bool = True) -> Events:
        gear = "P" if parked else "D"
        return Events([
            (NOW, "Gear", gear),
            (NOW, "ChargePortDoorOpen", True),
            (NOW, "Location", {"lat": LAT, "lon": LON}),
        ])

    def test_port_open_posts_rates_without_an_app_notice(self) -> None:
        _charger(self.conn, "NEAR", "1", LAT + 0.0003, 40, "플러그링크")
        _charger(self.conn, "FAR", "1", LAT + 0.02, 7, "다른곳")
        self.conn.commit()
        sent: list[dict] = []
        app: list[dict] = []
        check_port_open_rates(
            self.conn, VID, self._ev(), NOW, lambda payload: app.append(payload) or (True, "ok"),
            opener=_opener(sent),
        )
        self.assertEqual(app, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(body["kind"], "port_open_rates")
        self.assertNotIn("lat", json.dumps(body))
        rates = body["values"]["rates"]
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0]["operator"], "플러그링크")
        self.assertEqual(rates[0]["speed"], "중속")
        self.assertEqual(rates[0]["member_krw"], 322)
        self.assertEqual(rates[0]["guest_krw"], 528)
        self.assertEqual(rates[0]["roaming_krw"], 307.2)
        self.assertLess(rates[0]["distance_m"], 100)
        check_port_open_rates(
            self.conn, VID, self._ev(), NOW + timedelta(minutes=1),
            lambda payload: app.append(payload) or (True, "ok"),
            opener=_opener(sent),
        )
        self.assertEqual(len(sent), 1)

    def test_no_nearby_charger_does_not_post(self) -> None:
        sent: list[dict] = []
        check_port_open_rates(self.conn, VID, self._ev(), NOW, lambda p: (True, "ok"), opener=_opener(sent))
        self.assertEqual(sent, [])
        self.assertTrue((_get(self.conn, "port_open_rates") or "").startswith("empty:"))

    def test_a_missing_bot_does_not_push(self) -> None:
        os.environ.pop("BOT_NOTIFY_WEBHOOK_URL", None)
        _charger(self.conn, "NEAR", "1", LAT + 0.0002, 7, "플러그링크")
        self.conn.commit()
        app: list[dict] = []
        check_port_open_rates(self.conn, VID, self._ev(), NOW, lambda p: app.append(p) or (True, "ok"))
        self.assertEqual(app, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertTrue((_get(self.conn, "port_open_rates") or "").startswith("nobot:"))


def _get(conn, kind: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM alert_state WHERE vehicle_id = ? AND kind = ?", (VID, kind),
    ).fetchone()
    return row[0] if row else None


class StatusMatchTests(unittest.TestCase):
    def test_only_a_single_wait_to_charge_change_is_chosen(self) -> None:
        before = [
            {"stat_id": "A", "chger_id": "1", "stat": "2"},
            {"stat_id": "B", "chger_id": "1", "stat": "2"},
        ]
        self.assertEqual(
            changed_to_charging(before, [
                {"stat_id": "A", "chger_id": "1", "stat": "3"},
                {"stat_id": "B", "chger_id": "1", "stat": "2"},
            ]),
            [("A", "1")],
        )
        self.assertEqual(
            changed_to_charging(before, [
                {"stat_id": "A", "chger_id": "1", "stat": "3"},
                {"stat_id": "B", "chger_id": "1", "stat": "3"},
            ]),
            [("A", "1"), ("B", "1")],
        )
        conn = _db()
        self.addCleanup(conn.close)
        _charger(conn, "NEAR", "1", LAT + 0.0001, 7, "가까운곳")
        _charger(conn, "FAR", "1", LAT + 0.0005, 50, "먼곳")
        conn.execute("UPDATE ev_status SET stat = '3' WHERE stat_id = 'FAR'")
        conn.execute(
            "INSERT INTO process_meta (key, value) VALUES (?, ?)",
            (f"port_snap:{VID}", json.dumps({"items": [
                {"stat_id": "NEAR", "chger_id": "1", "stat": "2"},
                {"stat_id": "FAR", "chger_id": "1", "stat": "2"},
            ]})),
        )
        conn.commit()
        os.environ.pop("EV_CHARGER_API_KEY", None)
        found = match_status_change(conn, VID, NOW)
        self.assertEqual(found["stat_id"], "FAR")
        start = format_ts(NOW - timedelta(minutes=5))
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?, ?, ?, 'DC')",
            (VID, start, format_ts(NOW)),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, ?, ?, 'car')",
            (VID, start, LAT, LON),
        )
        conn.commit()
        _fill(conn, VID, NOW, None, "")
        stored = conn.execute(
            "SELECT stat_id, source FROM charge_charger WHERE charge_id = ?", (start,),
        ).fetchone()
        self.assertEqual(tuple(stored), ("FAR", "status"))

    def test_a_failed_lookup_still_records_the_nearest(self) -> None:
        conn = _db()
        self.addCleanup(conn.close)
        _charger(conn, "NEAR", "1", LAT + 0.0001, 7, "가까운곳")
        start = format_ts(NOW - timedelta(minutes=5))
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?, ?, ?, 'AC')",
            (VID, start, format_ts(NOW)),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, ?, ?, 'car')",
            (VID, start, LAT, LON),
        )
        conn.execute(
            "INSERT INTO process_meta (key, value) VALUES (?, ?)",
            (f"port_snap:{VID}", "{"),
        )
        conn.commit()

        def boom(*_args, **_kwargs):
            raise OSError("offline")

        from process import evcharger
        original = evcharger.match_status_change
        evcharger.match_status_change = boom
        try:
            _fill(conn, VID, NOW, None, "")
        finally:
            evcharger.match_status_change = original
        stored = conn.execute("SELECT stat_id, source FROM charge_charger").fetchone()
        self.assertEqual(tuple(stored), ("NEAR", "station"))


class ChargeStartOnceTests(unittest.TestCase):
    def test_a_pause_in_the_same_park_sends_one_start(self) -> None:
        conn = _db()
        self.addCleanup(conn.close)
        start = NOW
        sent: list[dict] = []

        def sender(payload):
            sent.append(payload)
            return True, "ok"

        first = Events([
            (start, "Gear", "P"),
            (start, "ChargePortDoorOpen", True),
            (start, "DetailedChargeState", "Charging"),
            (start, "ACChargingPower", 6.7),
            (start, "BatteryLevel", 40.0),
            (start, "ChargeLimitSoc", 80.0),
        ])
        check_charge_start(conn, VID, first, start + timedelta(minutes=1), sender)
        self.assertEqual([row["kind"] for row in sent], ["charge_start"])
        self.assertTrue(sent[0]["message"].startswith("충전 시작"))
        self.assertNotIn("원", sent[0]["message"])
        paused = Events(first.rows + [
            (start + timedelta(minutes=20), "DetailedChargeState", "Stopped"),
            (start + timedelta(minutes=25), "DetailedChargeState", "Charging"),
            (start + timedelta(minutes=25), "ACChargingPower", 6.7),
        ])
        check_charge_start(conn, VID, paused, start + timedelta(minutes=26), sender)
        self.assertEqual(len(sent), 1)
        self.assertEqual(PROCESS_VERSION, "25")
        note = HTML.split('version: "1.14.8"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("봇 대화로 알려요", note)
        self.assertIn("시작 알림은 한 번만 와요", note)


if __name__ == "__main__":
    unittest.main()
