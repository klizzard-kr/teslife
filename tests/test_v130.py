"""Bot-composed notices: signed webhook, bearer API, and the 10-minute fallback."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect
from web import make_server, serve_in_thread
from web.bot_notify import apply_bot_action, flush_pending, list_pending, sign_body
from web.notify import SCHEMA_SQL, _insert, ensure_schema, list_notifications, set_pref
from web.pushcrypto import b64url_encode, decrypt_push, random_ua_key

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
VID = "car-a"
TOKEN = "bot-token-not-a-vin-value"
TRIP = (
    "trip_end:2026-11-15T01:03:00Z",
    "5.5 km\n주행 종료 11/15 10:03 · 5.5 km · 11분 · 회생 0.5kWh",
)
SENTRY = (
    "sentry_alarm:2026-11-10T00:00:00Z",
    "감시 경보 11/10 09:00 · 위치 예시시 가나다구 샘플동 테스트로 (근사) · "
    "35.101010, 129.202020 · SECRET-TOKEN",
)


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


def _env(test: unittest.TestCase, url: str = "https://bot.example.test/hook", token: str = TOKEN) -> None:
    os.environ["BOT_NOTIFY_WEBHOOK_URL"] = url
    os.environ["BOT_PUSH_TOKEN"] = token
    os.environ.pop("BOT_NOTIFY_WEBHOOK_KEY", None)
    test.addCleanup(os.environ.pop, "BOT_NOTIFY_WEBHOOK_URL", None)
    test.addCleanup(os.environ.pop, "BOT_PUSH_TOKEN", None)
    test.addCleanup(os.environ.pop, "BOT_NOTIFY_WEBHOOK_KEY", None)


class _Resp:
    def __init__(self, status: int = 204) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _insert_trip(conn, opener=None, push=None) -> bool:
    return _insert(
        conn, VID, TRIP[0], "driving", "주행", TRIP[1], "Trips", "", "#trips", NOW,
        webhook_opener=opener, push_opener=push,
    )


def _subscribe(conn) -> tuple:
    from web.notify import save_subscription

    ua_priv, ua_pub = random_ua_key()
    auth = b"0123456789abcdef"
    save_subscription(conn, VID, {
        "endpoint": "https://push.example.test/sub",
        "keys": {"p256dh": b64url_encode(ua_pub), "auth": b64url_encode(auth)},
    })
    return ua_priv, ua_pub, auth


class HoldTests(unittest.TestCase):
    def test_empty_webhook_still_pushes(self) -> None:
        conn = _db(self)
        os.environ.pop("BOT_NOTIFY_WEBHOOK_URL", None)
        os.environ["BOT_PUSH_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "BOT_PUSH_TOKEN", None)
        _subscribe(conn)
        pushed = []
        self.assertTrue(_insert_trip(conn, push=lambda *a, **k: pushed.append(1) or _Resp()))
        self.assertEqual(pushed, [1])
        row = conn.execute("SELECT delivery FROM notifications").fetchone()
        self.assertIsNone(row[0])

    def test_webhook_is_signed_and_holds_the_row(self) -> None:
        conn = _db(self)
        _env(self)
        _subscribe(conn)
        seen = {}

        def opener(req, timeout=5):
            seen["raw"] = req.data
            seen["sig"] = req.get_header("X-teslife-signature")
            seen["auth"] = req.get_header("Authorization")
            seen["type"] = req.get_header("Content-type")
            seen["headers"] = str(req.header_items())
            seen["timeout"] = timeout
            return _Resp()

        pushed = []
        self.assertTrue(_insert_trip(conn, opener, lambda *a, **k: pushed.append(1)))
        self.assertEqual(pushed, [])
        self.assertEqual(conn.execute("SELECT delivery FROM notifications").fetchone()[0], "pending")
        self.assertEqual(seen["sig"], sign_body(seen["raw"], TOKEN))
        self.assertIsNone(seen["auth"])
        self.assertNotIn(TOKEN, seen["raw"].decode())
        self.assertNotIn(TOKEN, seen["headers"])
        self.assertEqual(seen["timeout"], 5)
        payload = json.loads(seen["raw"].decode())
        self.assertEqual(set(payload), {"id", "kind", "event_ts", "values"})
        self.assertEqual(payload["id"], TRIP[0])
        self.assertEqual(payload["kind"], "trip_end")
        self.assertEqual(payload["values"]["km"], 5.5)
        self.assertEqual(payload["values"]["minutes"], 11)
        self.assertEqual(payload["values"]["regen_kwh"], 0.5)
        self.assertNotIn("lat", json.dumps(payload))
        self.assertNotIn("vin", json.dumps(payload).lower())

    def test_webhook_key_is_a_bearer_and_not_the_push_token(self) -> None:
        conn = _db(self)
        _env(self)
        os.environ["BOT_NOTIFY_WEBHOOK_KEY"] = "webhook-sender-key"
        self.addCleanup(os.environ.pop, "BOT_NOTIFY_WEBHOOK_KEY", None)
        from web.bot_notify import holding, token_matches

        self.assertTrue(holding())
        self.assertTrue(token_matches("Bearer " + TOKEN))
        self.assertFalse(token_matches("Bearer webhook-sender-key"))
        seen = {}

        def opener(req, timeout=5):
            seen["raw"] = req.data
            seen["sig"] = req.get_header("X-teslife-signature")
            seen["auth"] = req.get_header("Authorization")
            return _Resp()

        self.assertTrue(_insert_trip(conn, opener))
        self.assertEqual(seen["auth"], "Bearer webhook-sender-key")
        self.assertEqual(seen["sig"], sign_body(seen["raw"], TOKEN))
        self.assertNotEqual(seen["sig"], sign_body(seen["raw"], "webhook-sender-key"))
        self.assertNotIn("webhook-sender-key", seen["raw"].decode())
        self.assertEqual(conn.execute("SELECT delivery FROM notifications").fetchone()[0], "pending")

    def test_sentry_place_is_only_the_neighborhood(self) -> None:
        conn = _db(self)
        _env(self)
        seen = {}

        def opener(req, timeout=5):
            seen["raw"] = req.data
            return _Resp()

        _insert(
            conn, VID, SENTRY[0], "sentry", "감시 경보", SENTRY[1], "Sentry", "", "#home", NOW,
            webhook_opener=opener,
        )
        text = seen["raw"].decode()
        payload = json.loads(text)
        self.assertEqual(payload["values"]["place"], "샘플동 테스트로")
        self.assertNotIn("예시시", text)
        self.assertNotIn("35.101010", text)
        self.assertNotIn("129.202020", text)

    def test_failed_webhook_stays_pending(self) -> None:
        conn = _db(self)
        _env(self)

        def opener(req, timeout=5):
            raise urllib.error.HTTPError(req.full_url, 500, "no", hdrs=None, fp=None)

        _insert_trip(conn, opener)
        self.assertEqual(conn.execute("SELECT delivery FROM notifications").fetchone()[0], "pending")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)


class ActionTests(unittest.TestCase):
    def _pending(self) -> sqlite3.Connection:
        conn = _db(self)
        _env(self)
        _insert_trip(conn, lambda req, timeout=5: _Resp())
        return conn

    def test_send_updates_and_pushes_once(self) -> None:
        conn = self._pending()
        ua_priv, ua_pub, auth = _subscribe(conn)
        seen = {}

        def opener(req, timeout=8):
            seen["body"] = req.data
            return _Resp(201)

        out = apply_bot_action(conn, VID, {
            "action": "send", "id": TRIP[0],
            "title": "주행을 마쳤어요", "summary": "5.5km 달렸어요",
        }, NOW, opener)
        self.assertEqual(out["action"], "send")
        self.assertNotIn("already", out)
        plain = json.loads(decrypt_push(seen["body"], ua_priv, auth, ua_pub))
        self.assertEqual(plain["title"], "주행을 마쳤어요")
        self.assertEqual(plain["body"], "5.5km 달렸어요")
        self.assertNotIn(TOKEN, json.dumps(plain))
        listed = list_notifications(conn, VID)["notifications"]
        self.assertEqual(listed[0]["display_title"], "주행을 마쳤어요")
        self.assertEqual(listed[0]["display_body"], "5.5km 달렸어요")
        stored = conn.execute("SELECT body FROM notifications").fetchone()[0]
        self.assertIn("5.5 km", stored)
        again = apply_bot_action(conn, VID, {
            "action": "send", "id": TRIP[0],
            "title": "주행을 마쳤어요", "summary": "5.5km 달렸어요",
        }, NOW, opener)
        self.assertTrue(again["already"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_skip_stays_in_the_list_without_a_push(self) -> None:
        conn = self._pending()

        def opener(req, timeout=8):
            raise AssertionError("skip must not push")

        apply_bot_action(conn, VID, {
            "action": "skip", "id": TRIP[0],
            "title": "짧은 주행이었어요", "summary": "집 근처를 다녀왔어요",
        }, NOW, opener)
        listed = list_notifications(conn, VID)["notifications"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["display_body"], "집 근처를 다녀왔어요")
        self.assertEqual(conn.execute("SELECT delivery FROM notifications").fetchone()[0], "skipped")

    def test_merge_hides_one_row_and_keeps_both(self) -> None:
        conn = self._pending()
        other = "trip_end:2026-11-15T00:10:00Z"
        _insert(
            conn, VID, other, "driving", "주행", "2.0 km", "Trips", "", "#trips",
            NOW - timedelta(hours=1), webhook_opener=lambda req, timeout=5: _Resp(),
        )
        before = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        apply_bot_action(conn, VID, {
            "action": "merge", "id": TRIP[0], "into": other,
            "title": "오전에 다녀왔어요", "summary": "7.5km 달렸어요",
        }, NOW)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], before)
        listed = list_notifications(conn, VID)["notifications"]
        self.assertEqual([item["id"] for item in listed], [other])
        self.assertEqual(listed[0]["display_title"], "오전에 다녀왔어요")
        self.assertEqual(listed[0]["display_body"], "7.5km 달렸어요")
        merged = conn.execute(
            "SELECT delivery, merged_into FROM notifications WHERE id = ?", (TRIP[0],)
        ).fetchone()
        self.assertEqual(merged, ("merged", other))

    def test_create_without_an_id_pushes(self) -> None:
        conn = _db(self)
        _env(self)
        _subscribe(conn)
        seen = {}

        def opener(req, timeout=8):
            seen["url"] = req.full_url
            return _Resp(201)

        out = apply_bot_action(conn, VID, {
            "action": "create", "kind": "trip_end",
            "title": "오늘 주행을 정리했어요", "summary": "32km 달렸어요",
        }, NOW, opener)
        self.assertTrue(out["id"].startswith("bot:"))
        self.assertEqual(seen["url"], "https://push.example.test/sub")
        listed = list_notifications(conn, VID)["notifications"]
        self.assertEqual(listed[0]["display_title"], "오늘 주행을 정리했어요")
        self.assertEqual(listed[0]["display_body"], "32km 달렸어요")
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_create_refuses_a_secret_and_a_disabled_kind(self) -> None:
        conn = _db(self)
        _env(self)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {
                "action": "create", "kind": "trip_end",
                "title": ("5YJ3" + "E1EA7KF" + "123456") + " 이에요", "summary": "알림이에요",
            }, NOW)
        set_pref(conn, VID, "trip_end", False)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {
                "action": "create", "kind": "trip_end",
                "title": "오늘 주행을 정리했어요", "summary": "32km 달렸어요",
            }, NOW)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        started = apply_bot_action(conn, VID, {
            "action": "create", "kind": "trip_start",
            "title": "주행을 시작했어요", "summary": "출발했어요",
        }, NOW)
        self.assertTrue(started["id"].startswith("bot:"))
        self.assertEqual(
            conn.execute("SELECT kind FROM notifications").fetchone()[0], "trip_start",
        )

    def test_fallback_after_ten_minutes_uses_the_old_sentence(self) -> None:
        conn = self._pending()
        ua_priv, ua_pub, auth = _subscribe(conn)
        seen = {}

        def opener(req, timeout=8):
            seen["body"] = req.data
            return _Resp(201)

        conn.execute("UPDATE notifications SET ts = ?", ((NOW - timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.commit()
        self.assertEqual(flush_pending(conn, VID, NOW, opener), 0)
        self.assertNotIn("body", seen)
        conn.execute("UPDATE notifications SET ts = ?", ((NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.commit()
        self.assertEqual(flush_pending(conn, VID, NOW, opener), 1)
        plain = json.loads(decrypt_push(seen["body"], ua_priv, auth, ua_pub))
        self.assertEqual(plain["title"], "주행을 마쳤어요")
        self.assertEqual(plain["body"], "5.5km 달렸어요. 도착 기록을 앱에서 볼 수 있어요")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_columns_are_added_without_touching_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, title_en, body_en, href) "
            "VALUES ('car-a', 'old', '2026-11-01T00:00:00Z', 'driving', '주행', '본문', '', '', '#trips')"
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*), title, body FROM notifications").fetchone()
        ensure_schema(conn)
        after = conn.execute("SELECT COUNT(*), title, body FROM notifications").fetchone()
        self.assertEqual(before, after)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        self.assertTrue({"delivery", "push_ts", "merged_into", "summary", "composed"} <= cols)


class HttpTests(unittest.TestCase):
    def test_bearer_api(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'BatteryLevel', '50')",
            (VID, "2026-11-15T00:00:00Z"),
        )
        conn.commit()
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        _env(self)
        _insert_trip(conn, lambda req, timeout=5: _Resp())
        server = make_server(path, port=0, process_interval=0, seed=False)
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def call(method: str, url_path: str, body: dict | None = None, token: str | None = TOKEN):
            data = None if body is None else json.dumps(body).encode()
            headers = {}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{url_path}", data=data, headers=headers, method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as res:
                    return res.status, json.loads(res.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        status, pending = call("GET", "/api/bot/notify/pending")
        self.assertEqual(status, 200)
        self.assertEqual(pending["pending"][0]["id"], TRIP[0])
        self.assertNotIn("vehicle_id", pending)
        self.assertNotIn(TOKEN, json.dumps(pending))
        self.assertEqual(call("GET", "/api/bot/notify/pending", token="nope")[0], 401)
        self.assertEqual(call("POST", "/api/bot/notify", {"action": "skip", "id": TRIP[0]}, token="nope")[0], 401)
        status, sent = call("POST", "/api/bot/notify", {
            "action": "send", "id": TRIP[0],
            "title": "주행을 마쳤어요", "summary": "5.5km 달렸어요",
        })
        self.assertEqual(status, 200)
        self.assertEqual(sent["action"], "send")
        self.assertNotIn("Origin", json.dumps(sent))
        status, waiting = call("GET", "/api/bot/notify/pending")
        self.assertEqual(waiting["pending"], [])
        os.environ.pop("BOT_PUSH_TOKEN", None)
        self.assertEqual(call("GET", "/api/bot/notify/pending", token=TOKEN)[0], 404)


class PageTests(unittest.TestCase):
    def test_release_note(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.8"', HTML)
        self.assertIn("알림 웹훅: 발신 키가 있으면 그 키로 웹훅을 보내요", HTML)
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("BOT_NOTIFY_WEBHOOK_KEY=", example)
        self.assertNotIn("BOT_NOTIFY_WEBHOOK_KEY=sk", example)
        self.assertIn("알림 정리: 봇이 알림 문장을 다듬어 보내요", HTML)


if __name__ == "__main__":
    unittest.main()
