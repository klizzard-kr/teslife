"""v1.21.4: home footer version button no longer uses the 48px global min-height."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class HomeFootVersionButtonTests(unittest.TestCase):
    def test_version_button_does_not_keep_global_min_height(self) -> None:
        self.assertIn("min-height: 48px;", HTML)
        css = _between(HTML, "#app-version {", "}")
        self.assertIn("min-height: 0;", css)
        self.assertIn("height: auto;", css)
        self.assertIn("padding: 0;", css)
        self.assertIn("margin: 0;", css)
        self.assertIn("display: inline;", css)
        self.assertIn("-webkit-appearance: none; appearance: none;", css)
        self.assertIn("line-height: inherit;", css)
        # global rule stays for real buttons
        self.assertIn("button, .btn-link {", HTML)
        btn = _between(HTML, "button, .btn-link {", "}")
        self.assertIn("min-height: 48px;", btn)

    def test_footer_lines_stay_tight(self) -> None:
        foot = _between(HTML, ".home-foot {", "}")
        self.assertIn("gap: 2px;", foot)
        self.assertIn("font-size: 11px; line-height: 16px;", foot)
        line = _between(HTML, ".home-foot-line {", "}")
        self.assertIn("margin: 0;", line)
        # the GitHub link already reset min-height; the button now matches
        link = _between(HTML, ".home-foot a {", "}")
        self.assertIn("min-height: 0;", link)
        markup = _between(HTML, 'id="home-foot"', "</div>")
        self.assertIn("<button type=\"button\" id=\"app-version\">", markup)
        self.assertIn('id="last-update"', markup)

    def test_version_notes(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.10"', HTML)
        entry = _between(HTML, 'version: "1.21.4"', 'version: "1.21.3"')
        self.assertIn("아래 차량 수신 줄과 버전 줄이 바로 붙어 있어요", entry)
        self.assertIn("홈: 아래 차량 수신 줄과 버전 줄이 바로 붙어 있어요", I18N)
        self.assertIn("## v1.21.4", CHANGELOG)
        self.assertIn("v1.11.0", CHANGELOG)


if __name__ == "__main__":
    unittest.main()
