"""User-facing wording: drives are '주행' everywhere (DB/API names stay 'trip')."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_FACING = ("web/static/index.html", "web/timeline.py", "web/__init__.py",
               "process/alerts.py", "ledger/__init__.py")


class WordingTest(unittest.TestCase):
    def test_no_unhaeng(self):
        for rel in USER_FACING:
            self.assertNotIn("운행", (ROOT / rel).read_text(encoding="utf-8"), rel)

    def test_drive_words(self):
        html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
        for s in ("<h2>주행 기록</h2>", "<h2>충전 기록</h2>"):
            self.assertIn(s, html)
        home = html[html.index('id="tab-home"'):html.index('id="tab-trips"')]
        self.assertNotIn("주행 중", home)
        self.assertNotIn(">정차<", home)
        alerts = (ROOT / "process/alerts.py").read_text(encoding="utf-8")
        self.assertIn('"주행 시작 "', alerts)
        self.assertIn('f"주행 종료 ', alerts)


if __name__ == "__main__":
    unittest.main()


class SentryWordingTest(unittest.TestCase):
    """: on screen and in alerts the sentry mode is '감시'; code names stay 'sentry'."""

    def test_no_sentry_korean_in_user_facing_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in ("web/static/index.html", "web/timeline.py", "process/alerts.py", "web/estimates.py", "web/__init__.py"):
            self.assertNotIn("센트리", (root / rel).read_text(encoding="utf-8"), rel)
        self.assertIn('("sentry", "감시")', (root / "web/timeline.py").read_text(encoding="utf-8"))
        self.assertIn('"감시 모드 "', (root / "process/alerts.py").read_text(encoding="utf-8"))
