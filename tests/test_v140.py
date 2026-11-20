"""Delete one notice, delete a tab, and mark a tab read. Other tables stay."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect
from web.bot_notify import flush_pending
from web.notify import delete_notices, list_notifications, mark_read

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
VID = "car-a"


def _db(test: unittest.TestCase) -> sqlite3.Connection:
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    conn = connect(Path(folder) / "signals.db")
    test.addCleanup(conn.close)
    return conn


def _notice(conn, notice_id: str, kind: str, delivery: str | None = None, merged_into: str | None = None) -> None:
    conn.execute(
        "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, title_en, body_en, href, delivery, merged_into) "
        "VALUES (?, ?, ?, ?, ?, '본문', '', '', '#home', ?, ?)",
        (VID, notice_id, "2026-11-15T01:00:00Z", kind, kind, delivery, merged_into),
    )
    conn.commit()


class DeleteTests(unittest.TestCase):
    def test_one_delete_removes_merged_children_and_leaves_other_tables(self) -> None:
        conn = _db(self)
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES (?, ?, ?, 5.5)",
            (VID, "2026-11-15T00:00:00Z", "2026-11-15T00:20:00Z"),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind) VALUES (?, ?, ?, 'AC')",
            (VID, "2026-11-15T02:00:00Z", "2026-11-15T03:00:00Z"),
        )
        conn.execute(
            "INSERT INTO alert_log (vehicle_id, ts, kind, status, message) VALUES (?, ?, 'sentry', 'sent', 'ok')",
            (VID, "2026-11-15T01:00:00Z"),
        )
        conn.commit()
        _notice(conn, "sentry_alarm:1", "sentry")
        _notice(conn, "merged-child", "driving", "merged", "sentry_alarm:1")
        _notice(conn, "trip_end:1", "driving")
        before = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("trips", "charges", "alert_log")
        }
        listed = delete_notices(conn, VID, "sentry_alarm:1")
        ids = [item["id"] for item in listed["notifications"]]
        self.assertEqual(ids, ["trip_end:1"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        after = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("trips", "charges", "alert_log")
        }
        self.assertEqual(before, after)
        with self.assertRaises(KeyError):
            delete_notices(conn, VID, "missing")

    def test_tab_delete_and_tab_read_stay_on_that_tab(self) -> None:
        conn = _db(self)
        _notice(conn, "drive-1", "driving")
        _notice(conn, "charge-1", "charge_done")
        _notice(conn, "sentry-1", "sentry")
        _notice(conn, "merged-into-drive", "sentry", "merged", "drive-1")
        read = mark_read(conn, VID, None, False, NOW, tab="security")
        by_id = {item["id"]: item["read"] for item in read["notifications"]}
        self.assertTrue(by_id["sentry-1"])
        self.assertFalse(by_id["drive-1"])
        self.assertFalse(by_id["charge-1"])
        left = delete_notices(conn, VID, tab="drive")
        ids = sorted(item["id"] for item in left["notifications"])
        self.assertEqual(ids, ["charge-1", "sentry-1"])
        self.assertIsNone(conn.execute(
            "SELECT id FROM notifications WHERE id = 'merged-into-drive'"
        ).fetchone())
        with self.assertRaises(ValueError):
            delete_notices(conn, VID, tab="nope")

    def test_deleted_pending_is_not_pushed_later(self) -> None:
        conn = _db(self)
        old = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, delivery) "
            "VALUES (?, 'trip_end:old', ?, 'driving', '주행', '5.5 km', 'pending')",
            (VID, old),
        )
        conn.commit()
        delete_notices(conn, VID, "trip_end:old")
        self.assertEqual(flush_pending(conn, VID, NOW), 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(list_notifications(conn, VID)["notifications"], [])


class PageTests(unittest.TestCase):
    def test_trash_buttons_and_tab_actions(self) -> None:
        self.assertIn('const APP_VERSION = "v1.16.5"', HTML)
        self.assertIn("알림 삭제: 보안과 차량 알림은 줄의 휴지통으로 지울 수 있어요", HTML)
        self.assertIn("이 탭의 알림을 모두 지울까요?", HTML)
        self.assertIn('id="notice-delete"', HTML)
        self.assertIn('id="inbox-delete-tab"', HTML)
        self.assertIn('id="inbox-read-tab"', HTML)
        self.assertIn("data-notice-delete", HTML)
        self.assertIn("TRASH_ICON", HTML)
        self.assertIn("inbox-delete-all", HTML)
        side = HTML.split("const side = detail", 1)[1].split("return \"<div class='notice", 1)[0]
        self.assertIn("data-notice-delete", side)
        self.assertNotIn("notice-pad", side)


if __name__ == "__main__":
    unittest.main()
