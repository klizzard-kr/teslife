"""Inbox rows, preference toggles, and Web Push request shape. No real push service."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, format_ts
from web.notify import (
    delete_subscription,
    list_notifications,
    list_prefs,
    mark_read,
    push_and_prune,
    save_subscription,
    set_pref,
    sync_notices,
)
from web.pushcrypto import b64url_encode, decrypt_push, random_ua_key

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)
VID = "car-a"


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


def _charge(conn, start: datetime, end: datetime, soc_end: float = 70.0) -> None:
    conn.execute(
        "INSERT INTO charges (vehicle_id, start_ts, end_ts, kwh_added, kind, start_soc, end_soc, charger_kwh) "
        "VALUES (?, ?, ?, ?, 'AC', 20, ?, 11.5)",
        (VID, format_ts(start), format_ts(end), 11.5, soc_end),
    )
    conn.commit()


class InboxTests(unittest.TestCase):
    def test_recent_charge_is_stored_once_and_can_be_marked_read(self) -> None:
        conn = _db(self)
        _charge(conn, NOW - timedelta(hours=2), NOW - timedelta(hours=1))
        _charge(conn, NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(hours=1))
        sync_notices(conn, VID, NOW)
        sync_notices(conn, VID, NOW)
        payload = list_notifications(conn, VID)
        done = [n for n in payload["notifications"] if n["kind"] == "charge_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["title"], "충전이 완료되었어요")
        self.assertIn("11.5kWh", done[0]["body"])
        self.assertTrue(any(row[0] == "충전량" for row in done[0]["details"]))
        self.assertFalse(done[0]["read"])
        self.assertEqual(payload["unread"] >= 1, True)
        mark_read(conn, VID, done[0]["id"], False, NOW)
        again = list_notifications(conn, VID)
        row = next(n for n in again["notifications"] if n["id"] == done[0]["id"])
        self.assertTrue(row["read"])

    def test_full_charge_reminder_respects_the_toggle(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'BatteryLevel', '40')",
            (VID, format_ts(NOW - timedelta(days=1))),
        )
        conn.commit()
        sync_notices(conn, VID, NOW)
        kinds = {n["kind"] for n in list_notifications(conn, VID)["notifications"]}
        self.assertNotIn("charge_full", kinds)
        set_pref(conn, VID, "charge_full", False)
        prefs = {k["id"]: k["enabled"] for k in list_prefs(conn, VID)["kinds"]}
        self.assertFalse(prefs["charge_full"])
        self.assertTrue(prefs["charge_done"])

    def test_reaching_full_skips_the_reminder(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'BatteryLevel', '100')",
            (VID, format_ts(NOW - timedelta(days=2))),
        )
        conn.commit()
        sync_notices(conn, VID, NOW)
        kinds = {n["kind"] for n in list_notifications(conn, VID)["notifications"]}
        self.assertNotIn("charge_full", kinds)

    def test_old_charge_rows_are_not_rewritten(self) -> None:
        conn = _db(self)
        _charge(conn, NOW - timedelta(days=10), NOW - timedelta(days=10) + timedelta(hours=2), 40)
        before = conn.execute("SELECT kwh_added, end_soc FROM charges").fetchone()
        sync_notices(conn, VID, NOW)
        after = conn.execute("SELECT kwh_added, end_soc FROM charges").fetchone()
        self.assertEqual(tuple(before), tuple(after))
        done = [n for n in list_notifications(conn, VID)["notifications"] if n["kind"] == "charge_done"]
        self.assertEqual(done, [])


class PushTests(unittest.TestCase):
    def test_subscription_must_be_https_and_push_uses_vapid(self) -> None:
        conn = _db(self)
        with self.assertRaises(ValueError):
            save_subscription(conn, VID, {"endpoint": "http://push.example.test/s", "keys": {}})
        ua_priv, ua_pub = random_ua_key()
        auth = b"0123456789abcdef"
        save_subscription(conn, VID, {
            "endpoint": "https://push.example.test/sub",
            "keys": {"p256dh": b64url_encode(ua_pub), "auth": b64url_encode(auth)},
            "lang": "en",
        })
        seen = {}

        class Resp:
            status = 201
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opener(req, timeout=8):
            seen["url"] = req.full_url
            seen["encoding"] = req.get_header("Content-encoding")
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = req.data
            seen["timeout"] = timeout
            return Resp()

        os.environ["VAPID_SUBJECT"] = "mailto:teslife@localhost"
        self.addCleanup(os.environ.pop, "VAPID_SUBJECT", None)
        sent = push_and_prune(
            conn, VID, "충전 완료", "본문", "Charging finished", "Body", "#charges", opener,
        )
        self.assertEqual(sent, 1)
        self.assertEqual(seen["url"], "https://push.example.test/sub")
        self.assertEqual(seen["encoding"], "aes128gcm")
        self.assertTrue(seen["auth"].startswith("vapid t="))
        self.assertNotIn("VAPID_PRIVATE", seen["auth"])
        plain = decrypt_push(seen["body"], ua_priv, auth, ua_pub)
        self.assertEqual(json.loads(plain)["title"], "Charging finished")
        delete_subscription(conn, VID, "https://push.example.test/sub")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)

    def test_gone_subscription_is_removed(self) -> None:
        conn = _db(self)
        ua_priv, ua_pub = random_ua_key()
        save_subscription(conn, VID, {
            "endpoint": "https://push.example.test/gone",
            "keys": {"p256dh": b64url_encode(ua_pub), "auth": b64url_encode(b"0123456789abcdef")},
        })

        def opener(req, timeout=8):
            raise urllib.error.HTTPError(req.full_url, 410, "gone", hdrs=None, fp=None)

        sent = push_and_prune(conn, VID, "알림", "본문", "Notifications", "Body", "#home", opener)
        self.assertEqual(sent, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)
        self.assertIsNotNone(ua_priv)


class InboxUiTests(unittest.TestCase):
    def test_permission_is_requested_only_from_the_button(self) -> None:
        self.assertEqual(HTML.count("requestPermission"), 1)
        start = HTML.index("async function enablePush")
        end = HTML.index("document.getElementById(\"inbox-open\")")
        self.assertIn("requestPermission", HTML[start:end])
        self.assertIn("serviceWorker.register(\"/sw.js\")", HTML[start:end])
        self.assertIn("isIosDevice() && !isStandaloneApp()", HTML[start:end])
        self.assertNotIn('id="push-enable"', HTML)
        self.assertIn('aria-label="알림 받기"', HTML)
        self.assertNotIn("모든 알림 받기", HTML)
        self.assertIn('id="push-master"', HTML)
        self.assertIn("홈 화면에 추가", HTML)
        self.assertIn("아이폰 설정 > 알림 > Teslife에서 알림을 허용해 주세요.", HTML)
        self.assertIn("Chrome 설정 > 사이트 설정 > 알림", HTML)
        self.assertIn("overscroll-behavior: contain", HTML)
        manifest = (ROOT / "web" / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
        self.assertIn('"display": "standalone"', manifest)
        sw = (ROOT / "web" / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("showNotification", sw)
        self.assertNotIn("wake", sw.lower())


if __name__ == "__main__":
    unittest.main()
