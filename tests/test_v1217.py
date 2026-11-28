"""v1.21.7: retapping the current tab button smoothly scrolls that tab to the top."""

from __future__ import annotations

import unittest
from pathlib import Path

from process import PROCESS_VERSION

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class RetapScrollTests(unittest.TestCase):
    def test_version(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.7"', HTML)
        self.assertIn("현재 버전은 v1.21.7입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.7`", DESIGN)
        self.assertIn("## v1.21.7", CHANGELOG)
        self.assertIn("맨 위로 부드럽게", CHANGELOG)
        self.assertIn("다섯 탭", CHANGELOG)
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('{ version: "1.21.7"', HTML)
        self.assertIn(
            "메뉴: 이미 보고 있는 탭의 단추를 다시 누르면, 그 화면이 맨 위로 부드럽게 올라가요. 다섯 탭 모두 같아요",
            HTML,
        )
        self.assertIn(
            "Menu: tapping the tab you are already on smoothly scrolls that screen to the top. All five tabs work the same way",
            I18N,
        )

    def test_scroll_helper_is_smooth_and_respects_reduced_motion(self) -> None:
        self.assertIn("function scrollTabToTop", HTML)
        body = _between(HTML, "function scrollTabToTop()", "document.querySelectorAll(\".tab-btn\")")
        self.assertIn('behavior: reduce ? "auto" : "smooth"', body)
        self.assertIn("prefers-reduced-motion: reduce", body)
        self.assertIn("document.scrollingElement || document.documentElement", body)
        self.assertIn("se.scrollTo(opts)", body)

    def test_retap_of_current_tab_scrolls_instead_of_showtab(self) -> None:
        click = _between(
            HTML,
            "document.querySelectorAll(\".tab-btn\").forEach(btn => {",
            "window.addEventListener(\"hashchange\"",
        )
        self.assertIn("const name = btn.getAttribute(\"data-tab\")", click)
        self.assertIn("if (name === activeTab)", click)
        self.assertIn("scrollTabToTop()", click)
        self.assertIn("showTab(name, true)", click)
        # same-tab path returns before showTab
        same = _between(click, "if (name === activeTab)", "showTab(name, true)")
        self.assertIn("return;", same)
        self.assertIn("scrollTabToTop()", same)

    def test_all_five_tabs_share_the_handler_and_hash_does_not_scroll(self) -> None:
        self.assertIn('const TABS = ["home", "trips", "charges", "ledger", "consumables"]', HTML)
        for name in ("home", "trips", "charges", "ledger", "consumables"):
            self.assertIn('id="tab-btn-%s"' % name, HTML)
            self.assertIn('data-tab="%s"' % name, HTML)
        # hash/back navigation still only calls showTab, not the retap scroller
        self.assertIn(
            'window.addEventListener("hashchange", () => showTab(location.hash.replace("#", ""), false));',
            HTML,
        )
        self.assertIn(
            'window.addEventListener("popstate", () => showTab(location.hash.replace("#", ""), false));',
            HTML,
        )
        self.assertNotIn("scrollTabToTop(location.hash", HTML)
        self.assertIn("이미 보고 있는 탭의 아래 메뉴 단추를 다시 누르면 그 화면이 맨 위로 부드럽게 올라간다", DESIGN)
        self.assertIn("다른 탭으로 바꿀 때는 이 스크롤을 쓰지 않는다", DESIGN)


if __name__ == "__main__":
    unittest.main()
