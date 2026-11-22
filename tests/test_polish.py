"""Design polish: ledger missing amounts stay out of the total, and the sheets keep their fields."""
from __future__ import annotations

import unittest
from pathlib import Path

from ledger import connect, ledger_summary

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class LedgerMissingTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "s.db")
        self.addCleanup(self.conn.close)

    def test_null_amount_is_not_a_zero(self) -> None:
        self.conn.execute(
            "INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, memo, source) VALUES "
            "('V', 'z', '2026-11-01', 'wash', 0, '', 'manual'), "
            "('V', 'n', '2026-11-02', 'charging', NULL, '자동 기록 충전', 'auto'), "
            "('V', 'p', '2026-11-03', 'charging', 5600, '', 'manual')"
        )
        self.conn.commit()
        summary = ledger_summary(self.conn, "V", "2026-11-01", "2026-11-30")
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["missing_amount"], 1)
        self.assertEqual(summary["total_krw"], 5600.0)
        charging = next(row for row in summary["by_category"] if row["category"] == "charging")
        self.assertEqual(charging["missing_amount"], 1)
        self.assertEqual(charging["total_krw"], 5600.0)


class PolishHtmlTests(unittest.TestCase):
    def test_sheets_keep_wording_and_theme_tokens(self) -> None:
        self.assertIn("쓴 전력", HTML)
        self.assertIn("충전 정보 입력", HTML)
        self.assertIn('id="theme-select"', HTML)
        self.assertIn("teslife-theme", HTML)
        self.assertIn('data-theme', HTML)
        self.assertIn("--accent-surface", HTML)
        self.assertIn("차량 수신 ", HTML)
        self.assertIn('id="home-foot"', HTML)
        self.assertIn('const APP_VERSION = "v1.17.0"', HTML)
        self.assertIn('href="https://github.com/klizzard-kr/teslife" target="_blank" rel="noopener">GitHub</a>', HTML)
        self.assertIn(">by klizzard<", HTML)
        self.assertIn('"연결 끊김"', HTML)
        self.assertNotIn("서버 연결", HTML)
        self.assertIn("paintTireAlert", HTML)
        self.assertIn("live.tire_alert", HTML)
        self.assertNotIn("권장 42", HTML)
        self.assertNotIn("car-stage", HTML)
        self.assertNotIn('add("SoC"', HTML)
        self.assertIn("금액 입력 후 합계에 반영돼요", HTML)
        self.assertIn("합계에서 제외", HTML)
        self.assertIn("점검 기록", HTML)
        self.assertIn("교체 완료", HTML)
        self.assertIn("tile.openstreetmap.org", HTML)
        self.assertNotIn("basemaps.cartocdn.com", HTML)
        detail = HTML[HTML.index("function openChargeDetail("):HTML.index("function drawMix(")]
        self.assertIn("const pairs = chargeDetailPairs(c);", detail)
        self.assertIn("kvHtml(pairs)", detail)
        self.assertIn("loadChargeCurve(box, c)", detail)
        self.assertIn('c.loc_source === "station" ? "저장한 장소"', detail)


if __name__ == "__main__":
    unittest.main()
