"""v1.18.0: port-price delivery log and away-from-home distance. Fake places only."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import PROCESS_VERSION, Events, connect, format_ts
from process.alerts import (
    BOT_SEND_GAP,
    BOT_SEND_MAX_ATTEMPTS,
    check_port_open_rates,
    check_trip_end_away,
)
from process.evcharger import ensure_schema
from process.geo import distance_m
from web.notify import ensure_schema as ensure_notices

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "car-fake"
# Not a real place. The webhook body must not contain these numbers.
LAT, LON = 1.11, 2.22
END_LAT = 1.16
NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
START = "2026-01-01T00:00:00Z"
END = "2026-01-01T00:20:00Z"


class _Resp:
    status = 200

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _opener(box: list, status: int = 200):
    def open_(req, timeout=5):
        box.append(json.loads(req.data.decode("utf-8")))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "no", {}, None)
        return _Resp(status)

    return open_


def _db() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = connect(tmp.name)
    ensure_schema(conn)
    ensure_notices(conn)
    return conn


def _state(conn, kind: str) -> str:
    row = conn.execute(
        "SELECT state FROM alert_state WHERE vehicle_id = ? AND kind = ?", (VID, kind),
    ).fetchone()
    return row[0] if row else ""


def _park(when: datetime, gear: str = "P", port: bool = True) -> list:
    return [
        (when, "Gear", gear),
        (when, "ChargePortDoorOpen", port),
        (when, "Location", {"lat": LAT, "lon": LON}),
    ]


class PortDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.get("BOT_NOTIFY_WEBHOOK_URL")
        os.environ["BOT_NOTIFY_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
        os.environ.pop("EV_CHARGER_API_KEY", None)
        self.conn = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(self._restore)
        self.conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, updated_ts, addr) "
            "VALUES ('FAKE', '1', ?, ?, '04', 40, '플러그링크', '가짜충전소', ?, '')",
            (LAT + 0.0003, LON, format_ts(NOW)),
        )
        self.conn.commit()

    def _restore(self) -> None:
        if self._env is None:
            os.environ.pop("BOT_NOTIFY_WEBHOOK_URL", None)
        else:
            os.environ["BOT_NOTIFY_WEBHOOK_URL"] = self._env

    def test_success_is_logged_once_without_coordinates(self) -> None:
        sent: list[dict] = []
        app: list[dict] = []
        check_port_open_rates(
            self.conn, VID, Events(_park(NOW)), NOW,
            lambda payload: app.append(payload) or (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(app, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(body["kind"], "port_open_rates")
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("lat", raw)
        self.assertNotIn("lon", raw)
        self.assertNotIn("1.11", raw)
        self.assertNotIn("2.22", raw)
        self.assertEqual(body["values"]["rates"][0]["operator"], "플러그링크")
        self.assertEqual(body["values"]["rates"][0]["member_krw"], 322)
        self.assertTrue(_state(self.conn, "port_open_rates").startswith("sent:"))
        log = self.conn.execute(
            "SELECT status, message, lat, lon FROM alert_log WHERE kind = 'port_open_rates'"
        ).fetchone()
        self.assertEqual(log[0], "ok")
        self.assertEqual(log[1], "ok http 200 attempt 1")
        self.assertIsNone(log[2])
        self.assertIsNone(log[3])
        row = self.conn.execute(
            "SELECT attempt, ok, http_status FROM bot_delivery WHERE kind = 'port_open_rates'"
        ).fetchone()
        self.assertEqual(tuple(row), (1, 1, 200))
        cols = [item[1] for item in self.conn.execute("PRAGMA table_info(bot_delivery)")]
        self.assertNotIn("lat", cols)
        self.assertNotIn("lon", cols)
        check_port_open_rates(
            self.conn, VID, Events(_park(NOW)), NOW + timedelta(minutes=5),
            lambda payload: app.append(payload) or (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(len(sent), 1)

    def test_failures_retry_only_until_the_car_moves(self) -> None:
        sent: list[dict] = []
        parked = Events(_park(NOW))
        check_port_open_rates(self.conn, VID, parked, NOW, lambda p: (True, "ok"), opener=_opener(sent, 503))
        self.assertEqual(len(sent), 1)
        self.assertTrue(_state(self.conn, "port_open_rates").startswith("retry:1:"))
        check_port_open_rates(
            self.conn, VID, parked, NOW + timedelta(seconds=30), lambda p: (True, "ok"), opener=_opener(sent, 503),
        )
        self.assertEqual(len(sent), 1)
        closed = Events(_park(NOW) + [(NOW + timedelta(minutes=3), "ChargePortDoorOpen", False)])
        check_port_open_rates(
            self.conn, VID, closed, NOW + BOT_SEND_GAP, lambda p: (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1]["kind"], "port_open_rates")
        self.assertTrue(_state(self.conn, "port_open_rates").startswith("sent:"))
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                "SELECT attempt FROM bot_delivery WHERE kind = 'port_open_rates' ORDER BY attempt"
            )],
            [1, 2],
        )
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT http_status FROM bot_delivery ORDER BY attempt")],
            [503, 200],
        )

    def test_three_failures_stop_and_movement_stops_sooner(self) -> None:
        sent: list[dict] = []
        when = NOW
        for _ in range(BOT_SEND_MAX_ATTEMPTS):
            check_port_open_rates(
                self.conn, VID, Events(_park(NOW)), when, lambda p: (True, "ok"), opener=_opener(sent, 500),
            )
            when += BOT_SEND_GAP
        self.assertEqual(len(sent), 3)
        self.assertTrue(_state(self.conn, "port_open_rates").startswith("gave_up:"))
        check_port_open_rates(
            self.conn, VID, Events(_park(NOW)), when, lambda p: (True, "ok"), opener=_opener(sent, 500),
        )
        self.assertEqual(len(sent), 3)

        other = _db()
        self.addCleanup(other.close)
        other.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, updated_ts, addr) "
            "VALUES ('FAKE', '1', ?, ?, '02', 7, '플러그링크', '가짜충전소', ?, '')",
            (LAT + 0.0002, LON, format_ts(NOW)),
        )
        other.commit()
        once: list[dict] = []
        check_port_open_rates(other, VID, Events(_park(NOW)), NOW, lambda p: (True, "ok"), opener=_opener(once, 503))
        moved = Events(_park(NOW) + [(NOW + timedelta(minutes=1), "Gear", "D")])
        check_port_open_rates(
            other, VID, moved, NOW + BOT_SEND_GAP, lambda p: (True, "ok"), opener=_opener(once),
        )
        self.assertEqual(len(once), 1)
        self.assertTrue(_state_of(other, "port_open_rates").startswith("moved:"))
        again = Events(_park(NOW) + [
            (NOW + timedelta(minutes=1), "Gear", "D"),
            (NOW + timedelta(minutes=10), "Gear", "P"),
            (NOW + timedelta(minutes=10), "ChargePortDoorOpen", True),
            (NOW + timedelta(minutes=10), "Location", {"lat": LAT, "lon": LON}),
        ])
        check_port_open_rates(
            other, VID, again, NOW + timedelta(minutes=10), lambda p: (True, "ok"), opener=_opener(once),
        )
        self.assertEqual(len(once), 2)
        self.assertTrue(_state_of(other, "port_open_rates").startswith("sent:"))


def _state_of(conn, kind: str) -> str:
    row = conn.execute(
        "SELECT state FROM alert_state WHERE vehicle_id = ? AND kind = ?", (VID, kind),
    ).fetchone()
    return row[0] if row else ""


class AwayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.get("BOT_NOTIFY_WEBHOOK_URL")
        self._match = os.environ.get("TESLIFE_ROUTE_MATCH")
        os.environ["BOT_NOTIFY_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
        os.environ.pop("TESLIFE_ROUTE_MATCH", None)
        os.environ.pop("EV_CHARGER_API_KEY", None)
        self.conn = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._env is None:
            os.environ.pop("BOT_NOTIFY_WEBHOOK_URL", None)
        else:
            os.environ["BOT_NOTIFY_WEBHOOK_URL"] = self._env
        if self._match is None:
            os.environ.pop("TESLIFE_ROUTE_MATCH", None)
        else:
            os.environ["TESLIFE_ROUTE_MATCH"] = self._match

    def _trip(self, end_lat: float = END_LAT) -> None:
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, end_lat, end_lon) VALUES (?, ?, ?, 4.0, ?, ?)",
            (VID, START, END, end_lat, LON),
        )
        self.conn.commit()

    def _ev(self, home, gear: str = "P") -> Events:
        end = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        rows = [
            (end, "Gear", gear),
            (end, "BatteryLevel", 48),
            (end, "EstBatteryRange", 160),
            (end, "OutsideTemp", 8),
        ]
        if home is not None:
            rows.append((end, "LocatedAtHome", home))
        return Events(rows)

    def test_away_sends_distance_and_chargers_without_a_push(self) -> None:
        self._trip()
        self.conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES (?, '집', ?, ?, 150)",
            (VID, LAT, LON),
        )
        self.conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, bnm, busi_nm, stat_nm, updated_ts, addr) "
            "VALUES ('FAKE', '1', ?, ?, '02', 7, '가짜사업자', '가짜사업자', '가짜충전소', ?, '비밀주소')",
            (END_LAT + 0.0004, LON, END),
        )
        self.conn.commit()
        before = self.conn.execute("SELECT km FROM trips").fetchone()[0]
        sent: list[dict] = []
        app: list[dict] = []
        end = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        check_trip_end_away(
            self.conn, VID, self._ev(False), end + timedelta(minutes=1),
            lambda payload: app.append(payload) or (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(app, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT km FROM trips").fetchone()[0], before)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(body["kind"], "trip_end_away")
        self.assertEqual(body["event_ts"], END)
        values = body["values"]
        self.assertEqual(values["battery_pct"], 48.0)
        self.assertEqual(values["range_km"], 160)
        self.assertEqual(values["outside_temp"], 8.0)
        straight = round(distance_m((END_LAT, LON), (LAT, LON)) / 1000.0, 1)
        self.assertEqual(values["home_km"], straight)
        self.assertEqual(values["home_via"], "직선")
        self.assertEqual(values["chargers"], [{
            "name": "가짜충전소", "distance_m": values["chargers"][0]["distance_m"], "operator": "가짜사업자",
        }])
        self.assertLess(values["chargers"][0]["distance_m"], 300)
        raw = json.dumps(body, ensure_ascii=False)
        for banned in ("lat", "lon", "lng", "addr", "1.11", "1.16", "2.22", "집", "비밀주소"):
            self.assertNotIn(banned, raw)
        log = self.conn.execute(
            "SELECT status, message, lat, lon FROM alert_log WHERE kind = 'trip_end_away'"
        ).fetchone()
        self.assertEqual((log[0], log[1], log[2], log[3]), ("ok", "ok http 200 attempt 1", None, None))
        check_trip_end_away(
            self.conn, VID, self._ev(False), end + timedelta(minutes=5),
            lambda payload: app.append(payload) or (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(len(sent), 1)

    def test_home_unknown_and_missing_place(self) -> None:
        self._trip()
        sent: list[dict] = []
        end = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        check_trip_end_away(self.conn, VID, self._ev(True), end, lambda p: (True, "ok"), opener=_opener(sent))
        self.assertEqual(sent, [])
        self.assertTrue(_state(self.conn, "trip_end_away").startswith("home:"))
        self.conn.execute("DELETE FROM alert_state")
        check_trip_end_away(self.conn, VID, self._ev(None), end, lambda p: (True, "ok"), opener=_opener(sent))
        self.assertEqual(sent, [])
        self.assertEqual(_state(self.conn, "trip_end_away"), "")
        check_trip_end_away(
            self.conn, VID, self._ev("maybe"), end + timedelta(minutes=61),
            lambda p: (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(sent, [])
        self.assertTrue(_state(self.conn, "trip_end_away").startswith("skip:"))
        self.conn.execute("DELETE FROM alert_state")
        check_trip_end_away(self.conn, VID, self._ev(False), end, lambda p: (True, "ok"), opener=_opener(sent))
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]["values"]["home_km"])
        self.assertIsNone(sent[0]["values"]["home_via"])
        self.assertEqual(sent[0]["values"]["chargers"], [])

    def test_road_distance_when_osrm_answers(self) -> None:
        self._trip()
        self.conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES (?, '집', ?, ?, 150)",
            (VID, LAT, LON),
        )
        self.conn.commit()
        os.environ["TESLIFE_ROUTE_MATCH"] = "osrm-local"
        os.environ["TESLIFE_OSRM_URL"] = "http://127.0.0.1:9"
        sent: list[dict] = []
        called: list[str] = []

        def route(req, timeout=5):
            called.append(req.full_url)
            return _Resp(200, json.dumps({"code": "Ok", "routes": [{"distance": 20000}]}).encode())

        end = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        check_trip_end_away(
            self.conn, VID, self._ev(False), end, lambda p: (True, "ok"),
            opener=_opener(sent), route_opener=route,
        )
        self.assertEqual(len(called), 1)
        self.assertNotIn(called[0], json.dumps(sent[0]))
        self.assertEqual(sent[0]["values"]["home_km"], 20.0)
        self.assertEqual(sent[0]["values"]["home_via"], "도로")
        self.assertNotIn("1.11", json.dumps(sent[0]))

    def test_a_failed_away_notice_stops_when_the_car_leaves(self) -> None:
        self._trip()
        sent: list[dict] = []
        end = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        check_trip_end_away(
            self.conn, VID, self._ev(False), end, lambda p: (True, "ok"), opener=_opener(sent, 503),
        )
        self.assertEqual(len(sent), 1)
        check_trip_end_away(
            self.conn, VID, self._ev(False, "D"), end + BOT_SEND_GAP,
            lambda p: (True, "ok"), opener=_opener(sent),
        )
        self.assertEqual(len(sent), 1)
        self.assertTrue(_state(self.conn, "trip_end_away").startswith("moved:"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)


class VersionTests(unittest.TestCase):
    def test_screen_version_and_process_version(self) -> None:
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('const APP_VERSION = "v1.21.0"', HTML)
        note = HTML.split('version: "1.18.0"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("집까지 거리", note)


if __name__ == "__main__":
    unittest.main()
