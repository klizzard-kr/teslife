"""v1.12.0: notice display, tire warning vs the shown psi, chart and place search."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from ledger.tires import displayed_low_wheels, displayed_psi, get_tire_settings
from web.notice_view import present

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class NoticeViewTests(unittest.TestCase):
    def test_old_trip_start_is_a_sentence_without_rewriting(self) -> None:
        row = {
            "id": "trip_start:2026-11-15T00:52:00Z",
            "ts": "2026-11-15T01:05:00Z",
            "title": "주행",
            "body": "주행 시작 11/15 09:52 · 배터리 68% · 주행가능 295 km · 공기압 42.4/42.1/42.8/42.0 psi, 권장 42 psi",
            "details": [],
        }
        view = present(row)
        self.assertEqual(row["title"], "주행")
        self.assertEqual(view["title"], "주행을 시작했어요")
        self.assertEqual(view["summary"], "295km 갈 수 있어요. 배터리 68%에서 출발해요")
        self.assertEqual(view["push_body"], view["summary"])
        self.assertGreater(len(view["summary"]), len(view["title"]))
        self.assertEqual(view["event_ts"], "2026-11-15T00:52:00Z")
        labels = [pair[0] for pair in view["rows"]]
        self.assertEqual(labels[:2], ["배터리", "주행가능"])
        self.assertIn("앞 왼쪽", labels)
        self.assertIn("뒤 오른쪽", labels)
        self.assertNotIn("시작 시각", labels)
        self.assertNotIn("권장 공기압", labels)
        self.assertNotIn("권장", view["push_body"])
        kept = present({
            "id": "trip_end:2026-11-15T00:52:00Z",
            "ts": "2026-11-15T01:03:00Z",
            "title": "주행을 마쳤어요",
            "body": "",
            "details": [["시작 시각", "2026. 11. 15. 09:52"], ["종료 시각", "2026. 11. 15. 10:03"]],
        })
        kept_labels = [pair[0] for pair in kept["rows"]]
        self.assertIn("시작 시각", kept_labels)
        self.assertNotIn("종료 시각", kept_labels)
        sentry = present({
            "id": "sentry_alarm:2026-11-10T00:00:00Z",
            "ts": "2026-11-10T00:00:00Z",
            "kind": "sentry",
            "title": "감시 경보",
            "body": "감시 경보 11/10 09:00 · 위치 예시시 가나다구 샘플동 테스트로 (근사) · 차 주변을 확인해 보세요",
            "details": [],
        })
        self.assertEqual(sentry["summary"], "샘플동 테스트로 근처에서 감지됐어요. 주변을 확인해 주세요")
        self.assertEqual(sentry["push_body"], sentry["summary"])
        self.assertNotIn("차 주변을", sentry["summary"])
        self.assertNotIn("(근사)", sentry["summary"])
        self.assertNotIn("(근사)", sentry["push_body"])
        self.assertNotIn("위치 ", sentry["summary"])

    def test_trip_end_and_charge_summaries(self) -> None:
        end = present({
            "id": "trip_end:2026-11-15T00:52:00Z",
            "ts": "2026-11-15T01:03:00Z",
            "title": "주행",
            "body": "5.5 km\n주행 종료 11/15 10:03 · 5.5 km · 11분",
            "details": [],
        })
        self.assertEqual(end["title"], "주행을 마쳤어요")
        self.assertEqual(end["summary"], "5.5km 달렸어요. 도착 기록을 앱에서 볼 수 있어요")
        self.assertNotIn("11분", end["summary"])
        self.assertEqual(end["push_body"], end["summary"])
        done = present({
            "id": "charge_end:2026-11-15T02:00:00Z",
            "ts": "2026-11-15T03:00:00Z",
            "title": "충전이 완료되었어요",
            "body": "46% → 80%, 12.3kWh",
            "details": [["충전량", "12.3 kWh"]],
        })
        self.assertEqual(done["summary"], "46%에서 80%까지 충전했어요. 12.3kWh를 넣었어요")
        self.assertIn("12.3", done["summary"])
        self.assertEqual(done["push_body"], done["summary"])
        self.assertIn(["충전량", "12.3 kWh"], done["rows"])


class TireDisplayTests(unittest.TestCase):
    def test_shown_psi_above_42_is_not_low(self) -> None:
        settings = get_tire_settings()
        fl = 42.4 / 14.5038
        rl = 42.8 / 14.5038
        self.assertEqual(displayed_psi(fl), 42.4)
        self.assertEqual(displayed_psi(rl), 42.8)
        self.assertEqual(displayed_low_wheels({"fl": fl, "fr": 2.9, "rl": rl, "rr": 2.9}, settings), [])
        self.assertEqual(displayed_low_wheels({"fl": 2.7, "fr": 2.9, "rl": rl, "rr": 2.9}, settings), [])
        self.assertEqual(displayed_low_wheels({"fl": 2.65, "fr": 2.9, "rl": 2.9, "rr": 2.9}, settings), ["fl"])
        self.assertEqual(displayed_psi(2.7), 39.2)


class PageTests(unittest.TestCase):
    def test_notes_inbox_chart_and_place(self) -> None:
        self.assertIn('const APP_VERSION = "v1.16.3"', HTML)
        self.assertIn("notes-label", HTML)
        self.assertNotIn(".notice.unread { background", HTML)
        self.assertIn("notice-head", HTML)
        self.assertIn("noticeWhen", HTML)
        chart = HTML.split("function svgChargeBand", 1)[1].split("function paintChargeTemps", 1)[0]
        self.assertNotIn("#e7c8a4", chart)
        self.assertIn("#ef8a6b", HTML)
        self.assertIn("#6ba8ef", HTML)
        self.assertNotIn("#e07a3d", chart)
        self.assertNotIn("배터리 온도(최저~최고)</text>", chart)
        self.assertNotIn("soc-scale", HTML.split("function socBarHtml", 1)[1].split("function psiNum", 1)[0])
        raw_map = HTML.split("async function rawMap", 1)[1].split("function fitTo", 1)[0]
        self.assertNotIn("addMiniZoom", raw_map)
        self.assertNotIn("map-zoom", HTML)
        self.assertIn("zoomControl: true", raw_map)
        self.assertNotIn("zoomControl: !naverOn", raw_map)
        self.assertIn("지도 확대: 직접 만든 버튼 대신 지도의 기본 확대 버튼으로 바꿨어요", HTML)
        self.assertIn("pickedPlace", HTML)
        dots = HTML.split("function drawChargeDots", 1)[1].split("function renderChargeRows", 1)[0]
        self.assertIn("chargeDotMarker", dots)
        self.assertNotIn("dot-count", dots)
        self.assertIn("box-sizing:border-box", HTML)
        self.assertIn("align-items:center;justify-content:center;line-height:1", HTML)
        naver = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
        self.assertIn("new naver.maps.Size(d, d)", naver)
        self.assertIn("new naver.maps.Point(d / 2, d / 2)", naver)
        self.assertIn("지도 숫자 위치: 충전 위치 원 안의 숫자가 가운데에 오도록 고쳤어요", HTML)
        self.assertIn("설정 화면: 알림을 토글 스위치로 바꿔 더 보기 쉬워졌어요", HTML)
        self.assertIn("알림 시간: 오늘은 오늘 11:00, 어제는 어제 10:00, 그 전은 11/10 09:00이에요", HTML)
        self.assertIn("알림 시각 형식: 상세의 시각이 주행·충전 상세와 같아요", HTML)
        self.assertIn('return "오늘 " + clock', HTML)
        self.assertIn("fmtDetailWhen(whenTs)", HTML)
        self.assertIn("data-notice-open", HTML)
        self.assertIn("notice-pad", HTML)
        self.assertIn("알림 탭: 주행, 충전, 보안, 차량이 밑줄 탭으로 보여요", HTML)
        self.assertIn("주행 알림: 상세와 푸시에서 권장 공기압은 빼요", HTML)
        self.assertIn("알림 상세: 위에 나온 시각과 같은 시각 줄은 빼요", HTML)
        self.assertIn("알림 요약: 목록과 푸시는 주제에 맞는 한 문장만 보여요", HTML)
        self.assertIn("justify-content: space-between; width: 100%", HTML)
        self.assertIn("white-space: nowrap; flex: 0 0 auto", HTML)
        self.assertIn("border-bottom: 3px solid transparent", HTML)
        tabCss = HTML.split(".inbox-tab {", 1)[1].split(".inbox-tab.on", 1)[0]
        self.assertNotIn("999px", tabCss)
        self.assertIn("알림 받기", HTML)
        self.assertNotIn("모든 알림 받기", HTML)
        self.assertIn("ios-switch", HTML)
        self.assertIn('id="settings-screen"', HTML)
        self.assertIn('id="place-map"', HTML)
        self.assertNotIn("차가운 상태 기준", HTML)
        self.assertIn("text-decoration: underline", HTML)


class NoRewriteTests(unittest.TestCase):
    def test_listing_does_not_change_the_stored_title(self) -> None:
        from web.notify import ensure_schema, list_notifications

        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO notifications (vehicle_id, id, ts, kind, title, body, title_en, body_en, href) "
            "VALUES ('car', 'trip_start:2026-11-15T00:52:00Z', '2026-11-15T01:05:00Z', 'driving', '주행', "
            "'주행 시작 11/15 09:52 · 배터리 68% · 주행가능 295 km', 'Trips', '', '#trips')"
        )
        conn.commit()
        listed = list_notifications(conn, "car")
        item = listed["notifications"][0]
        self.assertEqual(item["title"], "주행")
        self.assertEqual(item["display_title"], "주행을 시작했어요")
        stored = conn.execute("SELECT title, body FROM notifications").fetchone()
        self.assertEqual(stored[0], "주행")
        self.assertIn("주행 시작", stored[1])


if __name__ == "__main__":
    unittest.main()
