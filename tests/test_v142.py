"""Service due lines name the days and the action. Stored rows stay as they are."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect
from web.bot_notify import apply_bot_action, flush_pending
from web.notice_view import event_facts, present
from web.notify import _insert, list_notifications
from web.pressure import convert_psi_text
from web.pushcrypto import b64url_encode, decrypt_push, random_ua_key

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


class _Resp:
    def __init__(self, status: int = 201) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ActionCopyTests(unittest.TestCase):
    def _view(self, notice_id: str, title: str, body: str, details: list) -> dict:
        return present({
            "id": notice_id,
            "ts": "2026-11-15T03:00:00Z",
            "kind": "maintenance",
            "title": title,
            "body": body,
            "details": details,
        })

    def test_overdue_and_remaining_name_the_action(self) -> None:
        overdue = self._view(
            "maint:cons-washer:overdue:2026-09-19/",
            "워셔액 점검 기한이 지났어요",
            "-57일 · 2026-09-19",
            [["지난 날", "57일"], ["기한", "9월 19일"]],
        )
        self.assertEqual(overdue["summary"], "57일 지났어요, 워셔액을 점검해 주세요")
        self.assertEqual(overdue["push_body"], overdue["summary"])
        self.assertEqual(dict(overdue["rows"])["지난 날"], "57일")
        self.assertEqual(dict(overdue["rows"])["기한"], "9월 19일")
        self.assertNotIn("미리", overdue["summary"])

        soon = self._view(
            "maint:cons-washer:soon:2026-11-20/",
            "워셔액 점검 시기예요",
            "5일 · 2026-11-20",
            [["남은 날", "5일"], ["기한", "11월 20일"]],
        )
        self.assertEqual(soon["summary"], "5일 남았어요, 미리 워셔액을 점검해 주세요")
        self.assertEqual(soon["push_body"], soon["summary"])
        self.assertEqual(dict(soon["rows"])["남은 날"], "5일")

    def test_battery_and_other_items_pick_their_own_action(self) -> None:
        battery = self._view(
            "maint:cons-battery-full:overdue:2026-10-06/",
            "배터리 완충 기한이 지났어요",
            "-40일 · 2026-10-06",
            [["지난 날", "40일"]],
        )
        self.assertEqual(battery["summary"], "40일 지났어요, 100%까지 한 번 충전해 주세요")
        self.assertEqual(battery["title"], "배터리 완충 기한이 지났어요")

        wiper = self._view(
            "maint:cons-wiper:soon:2026-12-01/",
            "와이퍼 점검 시기예요",
            "16일",
            [["남은 날", "16일"]],
        )
        self.assertEqual(wiper["summary"], "16일 남았어요, 미리 와이퍼를 점검해 주세요")

        fluid = self._view(
            "maint:cons-brake-fluid:overdue:2026-01-01/",
            "브레이크액 기한이 지났어요",
            "-10일",
            [["지난 날", "10일"]],
        )
        self.assertEqual(fluid["summary"], "10일 지났어요, 브레이크액을 점검해 주세요")

        cabin = self._view(
            "maint:cons-cabin-filter:soon:2027-01-01/",
            "에어컨 필터(캐빈) 점검 시기예요",
            "40일",
            [],
        )
        self.assertEqual(cabin["summary"], "40일 남았어요, 미리 에어컨 필터(캐빈)을 점검해 주세요")

        caliper = self._view(
            "maint:cons-caliper:overdue:2026-01-01/",
            "브레이크 캘리퍼 세척·윤활 기한이 지났어요",
            "-3일",
            [],
        )
        self.assertIn("세척·윤활을 점검해 주세요", caliper["summary"])

    def test_old_stored_row_changes_only_on_screen(self) -> None:
        conn = _db(self)
        notice = "maint:cons-washer:overdue:2026-09-19/"
        self.assertTrue(_insert(
            conn, VID, notice, "maintenance", "워셔액 점검 기한이 지났어요", "-57일 · 2026-09-19",
            "Washer", "-57 days", "#consumables", NOW,
            [["지난 날", "57일"], ["기한", "9월 19일"]],
        ))
        before = conn.execute(
            "SELECT title, body, summary, details FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        shown = list_notifications(conn, VID)["notifications"][0]
        self.assertEqual(shown["display_body"], "57일 지났어요, 워셔액을 점검해 주세요")
        self.assertEqual(shown["display_title"], "워셔액 점검 기한이 지났어요")
        after = conn.execute(
            "SELECT title, body, summary, details FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        self.assertEqual(before, after)
        self.assertNotIn("점검해 주세요", before[1])

    def test_fallback_push_uses_the_action_and_leaves_the_row(self) -> None:
        conn = _db(self)
        from web.notify import save_subscription

        notice = "maint:cons-battery-full:overdue:2026-10-06/"
        title = "배터리 완충 기한이 지났어요"
        body = "-40일 · 2026-10-06"
        conn.execute(
            "INSERT INTO notifications "
            "(vehicle_id, id, ts, kind, title, body, title_en, body_en, href, details, delivery) "
            "VALUES (?, ?, ?, 'maintenance', ?, ?, '', '', '#consumables', ?, 'pending')",
            (
                VID, notice, (NOW - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                title, body, json.dumps([["지난 날", "40일"]], ensure_ascii=False),
            ),
        )
        conn.commit()
        ua_priv, ua_pub = random_ua_key()
        auth = b"0123456789abcdef"
        save_subscription(conn, VID, {
            "endpoint": "https://push.example.test/sub",
            "keys": {"p256dh": b64url_encode(ua_pub), "auth": b64url_encode(auth)},
        })
        seen = {}

        def opener(req, timeout=8):
            seen["body"] = req.data
            return _Resp()

        self.assertEqual(flush_pending(conn, VID, NOW, opener), 1)
        plain = json.loads(decrypt_push(seen["body"], ua_priv, auth, ua_pub))
        self.assertEqual(plain["title"], title)
        self.assertEqual(plain["body"], "40일 지났어요, 100%까지 한 번 충전해 주세요")
        stored = conn.execute(
            "SELECT title, body, summary FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        self.assertEqual(stored, (title, body, None))


class ReleaseTests(unittest.TestCase):
    def test_version(self) -> None:
        self.assertIn('const APP_VERSION = "v1.16.2"', HTML)
        self.assertIn("주행 알림: 상세에 배터리, 주행가능거리, 공기압을 보여요", HTML)
        self.assertIn("알림 상세: 중요한 항목을 주행·충전 상세와 같은 순서로 보여요", HTML)
        self.assertIn("공기압 경고: 39 psi보다 낮을 때만 알려요", HTML)
        self.assertIn("차량 알림: 충전구나 차 경고를 짧은 문장으로 알려요", HTML)
        self.assertIn("알림 문장: 제목과 요약이 같은 말을 반복하지 않아요", HTML)
        self.assertIn("알림 문장: 요약이 제목보다 길게 값과 할 일을 말해요", HTML)
        self.assertIn("충전구: 주차 후 5분 안에 충전을 시작하면 알리지 않아요", HTML)
        self.assertIn("openNotice(openNoticeRow)", HTML)
        self.assertIn('fmtFromPsi(lowPsi) + "보다 낮아요"', HTML)
        self.assertNotIn("보다 낮습니다", HTML)
        self.assertEqual(
            convert_psi_text("앞 좌측이 39.0 psi보다 낮아요", "bar"),
            "앞 좌측이 2.69 bar보다 낮아요",
        )
        view = present({
            "id": "tire_low:cold",
            "ts": "2026-11-15T01:00:00Z",
            "kind": "tire",
            "title": "공기압이 낮아요",
            "body": "공기압이 낮아요 · 앞 왼쪽이 39.0 psi보다 낮아요",
            "details": [],
        })
        self.assertEqual(view["summary"], "앞 왼쪽이 39.0 psi보다 낮아요. 공기를 넣어 주세요")
        self.assertEqual(view["push_body"], view["summary"])


class TripEndDetailTests(unittest.TestCase):
    def test_summary_stays_the_distance_and_rows_are_one_value(self) -> None:
        view = present({
            "id": "trip_end:2026-11-15T00:52:00Z",
            "ts": "2026-11-15T01:03:00Z",
            "kind": "driving",
            "title": "주행을 마쳤어요",
            "body": "5.3 km, 회생 0.4kWh, 급가속 1, 급제동 0",
            "details": [
                ["회생", "0.4 kWh"],
                ["급가속", "1회"],
                ["급제동", "0회"],
                ["배터리", "65.2%"],
                ["주행가능거리", "약 282 km"],
                ["앞 왼쪽", "42.1 psi"],
                ["앞 오른쪽", "42.0 psi"],
                ["뒤 오른쪽", "41.8 psi"],
            ],
        })
        self.assertEqual(view["summary"], "5.3km 달렸어요. 배터리 65.2%, 282km 남았어요")
        self.assertEqual(view["push_body"], view["summary"])
        labels = [pair[0] for pair in view["rows"]]
        self.assertEqual(labels, [
            "거리", "배터리", "주행가능", "회생", "급가속", "급제동",
            "앞 왼쪽", "앞 오른쪽", "뒤 오른쪽",
        ])
        self.assertEqual(dict(view["rows"])["거리"], "5.3 km")
        self.assertEqual(dict(view["rows"])["배터리"], "65.2%")
        self.assertEqual(dict(view["rows"])["주행가능"], "약 282 km")
        self.assertNotIn("뒤 왼쪽", labels)
        facts = event_facts(view["summary"], view["rows"], "trip_end:2026-11-15T00:52:00Z")
        stored = event_facts(
            "5.3 km, 회생 0.4kWh, 급가속 1, 급제동 0",
            [
                ["회생", "0.4 kWh"], ["배터리", "65.2%"], ["주행가능거리", "약 282 km"],
                ["앞 왼쪽", "42.1 psi"], ["뒤 오른쪽", "41.8 psi"],
            ],
            "trip_end:2026-11-15T00:52:00Z",
        )
        self.assertEqual(stored["km"], 5.3)
        self.assertEqual(stored["battery_pct"], 65.2)
        self.assertEqual(stored["range_km"], 282)
        self.assertEqual(stored["tires_psi"], {"fl": 42.1, "rr": 41.8})
        charge = present({
            "id": "charge_end:2026-11-15T02:00:00Z",
            "ts": "2026-11-15T03:00:00Z",
            "title": "충전이 완료되었어요",
            "body": "46% → 80%, 12.3kWh",
            "details": [
                ["에너지", "11.0 kWh"],
                ["종류", "교류"],
                ["종료 사유", "충전 완료"],
                ["평균 충전 전력", "7.2 kW"],
                ["충전 중 사용량", "0.4 kWh"],
                ["시간", "2시간 0분"],
                ["충전량", "12.3 kWh"],
            ],
        })
        self.assertEqual([pair[0] for pair in charge["rows"]], [
            "충전량", "시간", "전류", "배터리 저장량", "충전 중 사용량", "평균 충전 전력", "종료 사유",
        ])
        self.assertEqual(
            charge["summary"],
            "46%에서 80%까지 충전했어요. 12.3kWh를 넣었어요. 완속이에요",
        )
        self.assertNotIn("rl", stored["tires_psi"])
        self.assertEqual(facts["km"], 5.3)

    def test_old_notice_uses_stored_signals_without_rewriting_the_row(self) -> None:
        conn = _db(self)
        end = "2026-11-15T01:03:00Z"
        later = "2026-11-15T01:04:00Z"
        too_late = "2026-11-15T01:20:00Z"
        samples = [
            ("BatteryLevel", end, 65.2),
            ("BatteryLevel", later, 90),
            ("RatedRange", end, 400),
            ("EstBatteryRange", end, 281.6),
            ("EstBatteryRange", later, 10),
            ("TpmsPressureFl", end, 2.7),
            ("TpmsPressureFr", later, 2.9),
            ("TpmsPressureRr", too_late, 3.1),
        ]
        for name, ts, value in samples:
            conn.execute(
                "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, ?, ?)",
                (VID, ts, name, json.dumps(value)),
            )
        notice = "trip_end:2026-11-15T00:52:00Z"
        when = datetime(2026, 11, 15, 1, 3, tzinfo=timezone.utc)
        self.assertTrue(_insert(
            conn, VID, notice, "driving", "주행을 마쳤어요", "5.3 km",
            "Trip", "5.3 km", "#trips", when, [["회생", "0.4 kWh"]],
        ))
        before = conn.execute(
            "SELECT title, body, details FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        shown = list_notifications(conn, VID)["notifications"][0]
        self.assertEqual(shown["display_body"], "5.3km 달렸어요. 배터리 65.2%, 282km 남았어요")
        labels = dict(shown["display_details"])
        self.assertEqual(labels["배터리"], "65.2%")
        self.assertEqual(labels["주행가능"], "약 282 km")
        self.assertEqual(labels["앞 왼쪽"], "39.2 psi")
        self.assertEqual(labels["앞 오른쪽"], "42.1 psi")
        self.assertNotIn("뒤 왼쪽", labels)
        self.assertNotIn("뒤 오른쪽", labels)
        after = conn.execute(
            "SELECT title, body, details FROM notifications WHERE id = ?", (notice,)
        ).fetchone()
        self.assertEqual(before, after)
        self.assertNotIn("배터리", before[2])


class CarNoticeTests(unittest.TestCase):
    def test_stored_unicode_and_pipe_ids_are_accepted(self) -> None:
        conn = _db(self)
        import os
        os.environ["BOT_PUSH_TOKEN"] = "bot-token-not-a-vin-value"
        self.addCleanup(os.environ.pop, "BOT_PUSH_TOKEN", None)
        port = "open_after_park:충전구"
        code = "vehicle_alert:CP_a058_acChargeRetryPending|2026-11-15T01:00:00Z|start"
        conn.executemany(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, delivery) "
            "VALUES (?, ?, '2026-11-15T01:00:00Z', 'vehicle', ?, ?, 'pending')",
            [
                (VID, port, "문이 열려 있어요", "주차 후 열림 · 충전구이 열려 있어요"),
                (VID, code, "차량 알림이 있어요", "차량 경고 · CP_a058"),
            ],
        )
        conn.commit()
        skipped = apply_bot_action(conn, VID, {"action": "skip", "id": port}, NOW)
        self.assertEqual(skipped["id"], port)
        sent = apply_bot_action(conn, VID, {
            "action": "skip", "id": code,
        }, NOW)
        self.assertEqual(sent["id"], code)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {"action": "skip", "id": "open_after_park:없는곳"}, NOW)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {"action": "skip", "id": "vehicle_alert:NO|start"}, NOW)
        with self.assertRaises(ValueError):
            apply_bot_action(conn, VID, {"action": "skip", "id": "bad\nid"}, NOW)

    def test_display_names_the_part_and_the_charge_alert(self) -> None:
        parked = {
            "id": "open_after_park:충전구",
            "ts": "2026-11-15T01:00:00Z",
            "kind": "vehicle",
            "title": "문이 열려 있어요",
            "body": "주차 후 열림 · 충전구이 열려 있어요",
            "details": [],
        }
        view = present(parked)
        self.assertEqual(view["title"], "충전구가 열려 있어요")
        self.assertEqual(view["summary"], "주차한 뒤에도 닫히지 않았어요. 확인해 주세요")
        self.assertNotIn("충전구", view["summary"])
        self.assertGreater(len(view["summary"]), len(view["title"]))
        self.assertEqual(view["push_body"], view["summary"])
        self.assertEqual(parked["title"], "문이 열려 있어요")
        self.assertEqual(dict(view["rows"])["열린 곳"], "충전구")
        window = present({
            **parked,
            "id": "open_after_park:운전석 창문",
            "body": "주차 후 열림 · 운전석 창문이 열려 있어요",
        })
        self.assertEqual(window["title"], "운전석 창문이 열려 있어요")
        self.assertGreater(len(window["summary"]), len(window["title"]))
        facts = event_facts(parked["body"], parked["details"], parked["id"])
        self.assertEqual(facts["open"], "충전구")

        code = "vehicle_alert:CP_a058_acChargeRetryPending|2026-11-15T01:00:00Z|start"
        alert = present({
            "id": code,
            "ts": "2026-11-15T01:00:00Z",
            "kind": "vehicle",
            "title": "차량 알림이 있어요",
            "body": "차량 경고 · CP_a058_acChargeRetryPending",
            "details": [],
        })
        self.assertEqual(alert["title"], "완속 충전이 잠시 안 돼요")
        self.assertEqual(
            alert["summary"],
            "잠시 후 다시 시도해요. 안 되면 다른 충전기를 써 보세요",
        )
        self.assertNotIn("CP_a058", alert["summary"])
        self.assertEqual(dict(alert["rows"])["코드"], "CP_a058_acChargeRetryPending")
        unknown = "vehicle_alert:CP_a999_newThing|2026-11-15T01:00:00Z|start"
        other = present({
            "id": unknown, "ts": "2026-11-15T01:00:00Z", "kind": "vehicle",
            "title": "차량 알림이 있어요", "body": "차량 경고 · CP_a999_newThing", "details": [],
        })
        self.assertEqual(other["title"], "차에서 알림이 왔어요")
        self.assertEqual(other["summary"], "충전 관련 알림이에요. 자세한 내용은 앱에서 확인해 주세요")
        self.assertNotEqual(other["summary"], other["title"])
        self.assertNotIn("CP_a999", other["summary"])
        values = event_facts("차량 경고 · CP_a999_newThing", [], unknown)
        self.assertEqual(values["alert"], "CP_a999_newThing")
        self.assertNotIn("CP_a999", values["text"])

    def test_title_and_summary_do_not_repeat(self) -> None:
        samples = [
            {"id": "trip_start:1", "title": "주행", "body": "주행 시작", "kind": "driving"},
            {"id": "trip_end:1", "title": "주행", "body": "주행 종료", "kind": "driving"},
            {"id": "charge_end:1", "title": "충전", "body": "충전 종료", "kind": "charge_done"},
            {"id": "charge_start:1", "title": "충전 시작", "body": "충전 시작 · 배터리 40%", "kind": "charge_start"},
            {"id": "sentry:1", "title": "감시", "body": "감시 경보", "kind": "sentry"},
            {"id": "tire_low:1", "title": "공기압이 낮아요", "body": "앞 왼쪽", "kind": "tire"},
            {"id": "software_update:1", "title": "소프트웨어 업데이트", "body": "2026.1", "kind": "software"},
            {"id": "hv_fault:1", "title": "고전압 계통을 확인해 주세요", "body": "고장", "kind": "vehicle"},
            {"id": "open_after_park:충전구", "title": "문이 열려 있어요", "body": "충전구이", "kind": "vehicle"},
            {"id": "open_after_park:트렁크", "title": "문이 열려 있어요", "body": "", "kind": "vehicle"},
            {"id": "vehicle_alert:CP_a999|t|start", "title": "차량 알림이 있어요", "body": "CP_a999", "kind": "vehicle"},
            {"id": "vehicle_alert:BMS_a1|t|end", "title": "차량 알림이 있어요", "body": "", "kind": "vehicle"},
            {"id": "maint:cons-washer:overdue:2026-09-19/", "kind": "maintenance",
             "title": "워셔액 점검 기한이 지났어요", "body": "-57일 · 2026-09-19",
             "details": [["지난 날", "57일"]]},
            {"id": "charge_full:2026-11", "kind": "charge_full", "title": "한 번 완충할 시기예요",
             "body": "한 달 동안 100% 가까이 충전한 기록이 없어요"},
            {"id": "park14h_1h:2026-11-15T00:00:00Z", "kind": "parking", "title": "충전 자리",
             "body": "완속 충전 자리 14시간 제한: 11/15 18:00까지 차를 옮겨 주세요. 1시간 남았어요"},
            {"id": "tire_leak:fl", "kind": "tire", "title": "공기가 빠지고 있어요",
             "body": "공기압 서서히 빠짐 의심 · 앞 왼쪽 공기가 일주일 새 약 3.2 psi 줄었어요"},
            {"id": "charge_start:full", "kind": "charge_start", "title": "충전을 시작했어요",
             "body": "충전 시작 · 교류(AC) · 6.7 kW · 배터리 65% → 목표 80%"},
        ]
        from web.notice_view import VEHICLE_FALLBACK
        for code in VEHICLE_FALLBACK:
            samples.append({
                "id": f"vehicle_alert:{code}|t|start",
                "title": "차량 알림이 있어요",
                "body": code,
                "kind": "vehicle",
            })
        for row in samples:
            view = present({
                **row, "ts": "2026-11-15T01:00:00Z", "details": row.get("details") or [],
            })
            title, summary = view["title"].strip(), view["summary"].strip()
            self.assertNotEqual(title, summary, row["id"])
            self.assertNotIn(title, summary, row["id"])
            self.assertGreater(len(summary), len(title), f"{row['id']}: {title} / {summary}")
            self.assertFalse(
                title == "차량 알림이 있어요" and summary == "차량 알림이 있어요",
                row["id"],
            )
            self.assertNotIn("차량 알림이 있어요", summary)
        rich = present({
            "id": "charge_start:full", "ts": "2026-11-15T01:00:00Z", "kind": "charge_start",
            "title": "충전을 시작했어요",
            "body": "충전 시작 · 교류(AC) · 6.7 kW · 배터리 65% → 목표 80%",
            "details": [],
        })
        self.assertEqual(rich["summary"], "65%에서 80%까지, 약 6.7kW 완속이에요")


class ChargePortGraceTests(unittest.TestCase):
    def _open(self, extra=None, at=None):
        import ingest
        from tests.test_map import sig

        t = at or datetime(2026, 11, 15, 1, tzinfo=timezone.utc)
        rows = [
            sig(t, "Gear", "P"), sig(t, "Locked", True),
            sig(t, "ChargePortDoorOpen", True),
            sig(t, "DetailedChargeState", "Disconnected"),
        ]
        rows.extend(extra or [])
        conn = _db(self)
        ingest.store_signals(conn, rows)
        return conn, t

    def _check(self, conn, now):
        from process import _load_events
        from process.alerts import check_open_after_park
        from tests.test_map import VID as CAR

        ev = _load_events(conn, CAR)
        sent = []
        check_open_after_park(conn, CAR, ev, now, lambda payload: sent.append(payload) or (True, "ok"))
        try:
            notes = conn.execute("SELECT id, title, body FROM notifications").fetchall()
        except sqlite3.OperationalError:
            notes = []
        return sent, notes

    def test_port_waits_five_minutes_then_sends_once(self) -> None:
        from process.alerts import CHARGE_PORT_GRACE

        conn, t = self._open()
        self.assertEqual(self._check(conn, t)[0], [])
        self.assertEqual(self._check(conn, t + CHARGE_PORT_GRACE - timedelta(seconds=1))[0], [])
        sent, notes = self._check(conn, t + CHARGE_PORT_GRACE)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "open_after_park")
        self.assertIn("충전구", sent[0]["message"])
        self.assertEqual([row[0] for row in notes], ["open_after_park:충전구"])
        again, notes2 = self._check(conn, t + CHARGE_PORT_GRACE + timedelta(minutes=1))
        self.assertEqual(again, [])
        self.assertEqual(len(notes2), 1)

    def test_cable_or_charge_during_the_wait_cancels(self) -> None:
        import ingest
        from process.alerts import CHARGE_PORT_GRACE
        from tests.test_map import sig

        cases = [
            ("DetailedChargeState", "NoPower"),
            ("DetailedChargeState", "Charging"),
            ("ChargePortLatch", "ChargePortLatchEngaged"),
            ("ChargingCableType", "CableTypeIEC"),
            ("ChargeCable", "Connected"),
        ]
        for name, value in cases:
            conn, t = self._open()
            self.assertEqual(self._check(conn, t)[0], [], name)
            ingest.store_signals(conn, [sig(t + timedelta(minutes=2), name, value)])
            sent, notes = self._check(conn, t + CHARGE_PORT_GRACE + timedelta(minutes=1))
            self.assertEqual(sent, [], name)
            self.assertEqual(notes, [], name)

    def test_window_still_sends_immediately(self) -> None:
        from tests.test_map import sig

        conn, t = self._open([
            sig(datetime(2026, 11, 15, 1, tzinfo=timezone.utc), "FdWindow", "PartiallyOpen"),
        ])
        sent, _notes = self._check(conn, t)
        self.assertEqual(len(sent), 1)
        self.assertIn("운전석 창문", sent[0]["message"])
        self.assertNotIn("충전구", sent[0]["message"])

    def test_stored_notice_is_not_rewritten(self) -> None:
        from process.alerts import _set_state
        from tests.test_map import VID as CAR

        conn, t = self._open()
        when = t
        self.assertTrue(_insert(
            conn, CAR, "open_after_park:충전구", "vehicle", "문이 열려 있어요", "예전 문장",
            "Open", "old", "#home", when, [["열린 곳", "충전구"]],
        ))
        _set_state(conn, CAR, "open_after_park", "충전구", t)
        before = conn.execute(
            "SELECT title, body, details FROM notifications WHERE id = 'open_after_park:충전구'"
        ).fetchone()
        sent, notes = self._check(conn, t + timedelta(minutes=10))
        after = conn.execute(
            "SELECT title, body, details FROM notifications WHERE id = 'open_after_park:충전구'"
        ).fetchone()
        self.assertEqual(sent, [])
        self.assertEqual(before, after)
        self.assertEqual(len(notes), 1)

    def test_idle_pass_sends_the_port_when_the_wait_is_over(self) -> None:
        from process import _run_deferred_alerts
        from process.alerts import CHARGE_PORT_GRACE, _set_state
        from tests.test_map import VID as CAR

        conn, t = self._open()
        _set_state(conn, CAR, "open_after_park_port", "hold:" + t.strftime("%Y-%m-%dT%H:%M:%SZ"), t)
        sent = []
        _run_deferred_alerts(conn, [CAR], t + CHARGE_PORT_GRACE, lambda payload: sent.append(payload) or (True, "ok"))
        self.assertEqual(len(sent), 1)
        self.assertIn("충전구", sent[0]["message"])
