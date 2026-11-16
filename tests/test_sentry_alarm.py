"""감시 경보: SentryMode Panic -> one webhook alert with time and place, grouped,
recorded in sentry_alarms, shown on the 감시 lane only (no card)."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path

import ingest
from process import _load_events, connect as process_connect, process_db
from process.alerts import (ALARM_MAX_ATTEMPTS, _set_state, address_from_nominatim, alarm_message,
                            check_sentry_alarm, send_pending_sentry_alarms)
from tests.test_map import T0, VID, scenario, sig

T = T0 + timedelta(days=3)          # 11/06 09:00 KST, parked after the scenario's trips
NOMI = {"address": {"road": "테스트로", "quarter": "샘플동", "borough": "가나다구", "city": "예시시",
                    "province": "경기도", "country": "대한민국"}}


def fake_opener(calls):
    def opener(req, timeout=0):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps(NOMI).encode())
    return opener


class Sent:
    def __init__(self, ok=True):
        self.items, self.ok = [], ok

    def __call__(self, payload):
        self.items.append(payload)
        return (self.ok, "http 200" if self.ok else "http 500")


class AlarmBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "s.db")
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, scenario() + [sig(T - timedelta(minutes=30), "SentryMode", "Armed")])
        conn.close()
        process_db(self.db, seed=False)
        with sqlite3.connect(self.db) as c:
            end = c.execute("SELECT end_lat, end_lon FROM trips ORDER BY start_ts DESC LIMIT 1").fetchone()
            c.execute("INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES (?, '집', ?, ?, 100)",
                      (VID, end[0], end[1]))
        self.calls: list = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, *rows) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, [sig(T + timedelta(seconds=s), "SentryMode", v) for s, v in rows])
        conn.close()

    def check(self, at_s: float, sender) -> None:
        conn = process_connect(self.db)
        try:
            ev = _load_events(conn, VID)
            check_sentry_alarm(conn, VID, ev, T + timedelta(seconds=at_s), sender, opener=fake_opener(self.calls))
        finally:
            conn.close()

    def q(self, sql, params=()):
        with sqlite3.connect(self.db) as c:
            return c.execute(sql, params).fetchall()


class AlarmTests(AlarmBase):
    def test_panic_sends_once_with_time_and_place(self) -> None:
        sent = Sent()
        self.check(-60, sent)                      # nothing yet: quiet
        self.assertEqual(sent.items, [])
        self.add((0, "Aware"), (20, "Panic"), (50, "Armed"))
        self.check(60, sent)
        self.check(90, sent)                       # next cycle: no repeat
        self.assertEqual(len(sent.items), 2)       # Aware(주의) and Panic(감시 경보)
        aware = next(p for p in sent.items if p["message"].startswith("주의"))
        panic = next(p for p in sent.items if p["message"].startswith("감시 경보"))
        self.assertTrue(aware["message"].startswith("주의 "))
        self.assertNotIn("센트리", aware["message"])
        p = panic
        self.assertEqual(set(p), {"kind", "ts", "message", "data"})   # same shape as other alerts
        self.assertEqual(p["kind"], "sentry_alarm")
        self.assertEqual(p["message"], "감시 경보 11/06 09:00 · 근처 집, 예시시 가나다구 샘플동 테스트로 · 차 주변을 확인해 보세요")
        self.assertEqual(p["data"]["raw"], "Panic")
        self.assertTrue(p["data"]["location"]["approximate"])
        rows = self.q("SELECT count, place, notify_status, stage FROM sentry_alarms ORDER BY start_ts")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0:3], (1, "집, 예시시 가나다구 샘플동 테스트로", "sent"))
        self.assertEqual(rows[1][3], "Panic")
        self.assertEqual(len(self.calls), 1)       # one address lookup, then cached
        self.assertEqual(self.q("SELECT COUNT(*) FROM alert_log WHERE kind = 'sentry_alarm'")[0][0], 2)

    def test_aware_notifies_as_caution_and_groups(self) -> None:
        sent = Sent()
        self.add((0, "Aware"), (30, "Armed"), (60, "Aware"), (90, "Armed"))
        self.check(100, sent)
        self.assertEqual(len(sent.items), 1)
        self.assertTrue(sent.items[0]["message"].startswith("주의 "))
        self.assertNotIn("센트리", sent.items[0]["message"])
        self.assertEqual(sent.items[0]["data"]["raw"], "Aware")
        self.assertEqual(sent.items[0]["data"]["label"], "주의")
        self.assertEqual(self.q("SELECT count, stage FROM sentry_alarms"), [(2, "Aware")])

    def test_repeats_within_10_min_join_one_record(self) -> None:
        sent = Sent()
        self.add((0, "Panic"), (30, "Armed"))
        self.check(40, sent)
        self.add((120, "Panic"), (150, "Aware"), (400, "SentryModeStatePanic"), (430, "Armed"))
        self.check(440, sent)
        # Panic stays one grouped record; Aware is its own 주의 notice
        self.assertEqual(len(sent.items), 2)
        self.assertTrue(any(p["message"].startswith("주의 ") for p in sent.items))
        self.assertEqual(self.q("SELECT stage, count, notify_status FROM sentry_alarms ORDER BY start_ts"),
                         [("Panic", 3, "sent"), ("Aware", 1, "sent")])
        # quiet for more than 10 minutes, then a new alarm: a new record and a new alert
        self.add((430 + 11 * 60, "Panic"))
        self.check(430 + 11 * 60 + 10, sent)
        self.assertEqual(len(sent.items), 3)
        self.assertEqual(self.q("SELECT COUNT(*) FROM sentry_alarms")[0][0], 3)

    def test_burst_in_one_cycle_says_count(self) -> None:
        sent = Sent()
        self.add((0, "Panic"), (10, "Armed"), (20, "Panic"), (30, "Armed"))
        self.check(35, sent)
        self.assertEqual(len(sent.items), 1)
        self.assertIn("10분 안에 2번", sent.items[0]["message"])

    def test_old_alarm_is_recorded_not_sent(self) -> None:
        sent = Sent()
        _set_state(process_connect(self.db), VID, "sentry_alarm_seen", "2026-11-05T00:00:00Z", T)
        self.add((0, "Panic"), (30, "Armed"))
        self.check(30 * 60, sent)                  # server was down for 30 minutes
        self.assertEqual(sent.items, [])
        self.assertEqual(self.q("SELECT notify_status FROM sentry_alarms"), [("stale",)])

    def test_failed_send_retries_then_gives_up(self) -> None:
        bad, good = Sent(ok=False), Sent()
        self.add((0, "Panic"))
        self.check(10, bad)
        self.assertEqual(self.q("SELECT notify_status, attempts FROM sentry_alarms"), [("retry", 1)])
        conn = process_connect(self.db)
        send_pending_sentry_alarms(conn, VID, T + timedelta(seconds=40), good)   # idle-cycle retry
        conn.close()
        self.assertEqual(len(good.items), 1)
        self.assertEqual(self.q("SELECT notify_status FROM sentry_alarms"), [("sent",)])
        # a second alarm whose webhook keeps failing stops after ALARM_MAX_ATTEMPTS
        self.add((20 * 60, "Armed"), (31 * 60, "Panic"))
        for k in range(ALARM_MAX_ATTEMPTS + 2):
            self.check(31 * 60 + 5 + k * 30, bad)
        self.assertEqual(len(bad.items), 1 + ALARM_MAX_ATTEMPTS)
        self.assertEqual(self.q("SELECT notify_status FROM sentry_alarms ORDER BY start_ts")[-1], ("gave_up",))

    def test_process_loop_sends_through_webhook_path(self) -> None:
        sent = Sent()
        process_db(self.db, seed=False, alerts=True, now=T - timedelta(seconds=1), sender=sent)
        self.add((0, "Panic"), (25, "Armed"))
        process_db(self.db, seed=False, alerts=True, now=T + timedelta(seconds=30), sender=sent)
        process_db(self.db, seed=False, alerts=True, now=T + timedelta(seconds=60), sender=sent)
        alarms = [p for p in sent.items if p["kind"] == "sentry_alarm"]
        self.assertEqual(len(alarms), 1)
        self.assertIn("감시 경보 11/06 09:00", alarms[0]["message"])

    def test_message_and_address_helpers(self) -> None:
        self.assertEqual(address_from_nominatim(NOMI), "예시시 가나다구 샘플동 테스트로")
        self.assertEqual(address_from_nominatim({}), None)
        self.assertIn("위치는 아직 몰라요", alarm_message("2026-11-06T00:00:00Z", 1, None, None, None))


class AlarmWebTests(AlarmBase):
    def test_timeline_marks_and_api(self) -> None:
        from web import make_server, serve_in_thread
        from web.timeline import day_timeline

        self.add((0, "Panic"), (30, "Armed"))
        self.check(40, Sent())
        conn = sqlite3.connect(self.db)
        try:
            d = day_timeline(conn, VID, "2026-11-06", now=T + timedelta(hours=1))
        finally:
            conn.close()
        self.assertEqual(len(d["alarms"]), 1)
        self.assertEqual(d["alarms"][0]["place"], "집, 예시시 가나다구 샘플동 테스트로")
        self.assertIn("sentry", [l["key"] for l in d["lanes"]])
        self.assertNotIn("lat", d["alarms"][0])
        server = make_server(self.db, port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/sentry/alarms", timeout=5) as r:
                body = json.loads(r.read())
            self.assertEqual(body["alarms"][0]["count"], 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_web_has_no_alarm_card(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("tl-alarm", html)
        self.assertIn("감시 경보", html)
        self.assertNotIn("alarm-card", html)


if __name__ == "__main__":
    unittest.main()
