"""v1.14.4: notification switches sit at the right end of each settings row."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _block(src: str, start: str, end: str) -> str:
    return src.split(start, 1)[1].split(end, 1)[0]


class AlertToggleLayoutTests(unittest.TestCase):
    def test_version_and_note(self) -> None:
        self.assertIn('const APP_VERSION = "v1.19.0"', HTML)
        self.assertIn("알림 설정: 스위치는 각 항목의 오른쪽 끝에 있어요", HTML)
        self.assertIn("알림 설정: 알림 받기 스위치는 제목 오른쪽에 있어요", HTML)
        self.assertIn("알림 안내: 알림이 막히면 쓰는 브라우저에 맞는 설정 방법을 알려요", HTML)
        self.assertIn("알림 설정: 알림 받기를 끄면 아래 항목은 흐리게 남아요", HTML)
        self.assertIn("주행 알림: 시작과 종료를 따로 켜고 끌 수 있어요", HTML)
        self.assertIn('aria-label="알림 받기"', HTML)
        self.assertNotIn("끄면 아래 설정과 관계없이 알림이 오지 않아요", HTML)
        self.assertNotIn("모든 알림 받기", HTML)

    def test_switch_is_at_the_right_of_every_row(self) -> None:
        row = _block(HTML, ".sheet-body label.pref-row {", "}")
        self.assertIn("display: flex", row)
        self.assertIn("flex-direction: row", row)
        self.assertIn("flex-wrap: nowrap", row)
        self.assertIn("align-items: center", row)
        self.assertIn("justify-content: space-between", row)
        self.assertIn("width: 100%", row)
        name = _block(HTML, ".sheet-body label.pref-row > span:not(.ios-switch) {", "}")
        self.assertIn("flex: 1 1 auto", name)
        self.assertIn("text-align: left", name)
        switch = _block(HTML, ".sheet-body label.pref-row > .ios-switch {", "}")
        self.assertIn("margin-left: auto", switch)
        self.assertIn("align-self: center", switch)
        head = _block(HTML, "#settings-notify .notify-head {", "}")
        self.assertIn("display: flex", head)
        self.assertIn("align-items: center", head)
        self.assertIn("justify-content: space-between", head)
        self.assertIn("border-top: 1px solid var(--line)", head)
        self.assertIn("id=\"push-master\"", HTML)
        self.assertLess(HTML.index(">알림</h3>"), HTML.index('id="push-master"'))
        self.assertIn("border: 0", _block(HTML, "#settings-notify .pref-row {", "}"))
        group = _block(HTML, "#push-prefs .section-label {", "}")
        self.assertIn("border-top: 1px solid var(--line)", group)
        self.assertIn("margin: 16px 0 0", group)
        self.assertIn("padding: 14px 0 0", group)
        self.assertIn("font-size: 17px", group)
        self.assertIn("font-weight: 700", group)
        self.assertIn("color: var(--text)", group)
        plain = ".section-label { margin: 8px 0 0; color: var(--text-secondary); font-size: 13px; font-weight: 600; }"
        self.assertIn(plain, HTML)
        self.assertEqual(HTML.count(plain), 1)
        self.assertNotIn(".section-label:first-child", HTML)
        dim = _block(HTML, "function setKindSwitches", "function renderPrefs")
        self.assertIn("is-off", dim)
        self.assertIn("disabled", dim)
        self.assertNotIn("hidden", dim)
        rendered = _block(HTML, "function renderPrefs()", "function urlBase64ToBytes")
        self.assertIn("class='pref-row'", rendered)
        self.assertIn("class='ios-switch'", rendered)
        self.assertLess(rendered.index("pref-text"), rendered.index("ios-switch"))

    def test_blocked_hint_follows_the_browser(self) -> None:
        self.assertIn("아이폰 설정 > 알림 > Teslife에서 알림을 허용해 주세요.", HTML)
        self.assertIn("아이패드 설정 > 알림 > Teslife에서 알림을 허용해 주세요.", HTML)
        self.assertIn("홈 화면에 추가해야 알림을 받을 수 있어요.", HTML)
        self.assertIn("Chrome 설정 > 사이트 설정 > 알림에서 이 사이트를 허용해 주세요.", HTML)
        self.assertIn("Safari 설정 > 웹사이트 > 알림에서 이 사이트를 허용해 주세요.", HTML)
        self.assertIn("Edge 설정 > 쿠키 및 사이트 권한 > 알림에서 이 사이트를 허용해 주세요.", HTML)
        self.assertIn("Firefox 설정 > 개인 정보 및 보안 > 알림에서 이 사이트를 허용해 주세요.", HTML)
        self.assertIn("삼성 인터넷 설정 > 사이트 및 다운로드 > 알림에서 이 사이트를 허용해 주세요.", HTML)
        self.assertIn("브라우저 설정에서 이 사이트의 알림을 허용해 주세요.", HTML)
        self.assertIn("Allow notifications in iPhone Settings > Notifications > Teslife.", HTML)
        self.assertIn("Allow this site's notifications in your browser settings.", HTML)
        src = HTML.split("function detectPushClient", 1)[1].split("function pushHelpText", 1)[0]
        script = "function detectPushClient" + src + r"""
const samples = [
  ["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1", "iPhone", 5, false],
  ["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1", "iPhone", 5, true],
  ["Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1", "iPad", 5, true],
  ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15", "MacIntel", 5, true],
  ["Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36", "Linux armv8l", 5, false],
  ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", "Win32", 0, false],
  ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15", "MacIntel", 0, false],
  ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "Win32", 0, false],
  ["Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0", "Win32", 0, false],
  ["Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36", "Linux armv8l", 5, false],
  ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Version/4.0 Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0", "Linux x86_64", 0, false],
];
const out = samples.map(function (row) {
  const client = detectPushClient(row[0], row[1], row[2], row[3]);
  const situation = (client.kind === "iphone" || client.kind === "ipad") && !client.standalone ? "install" : "blocked";
  return [client.kind, pushHelpKey(client.kind, situation)];
});
process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got, [
            ["iphone", "iphone-install"],
            ["iphone", "iphone-blocked"],
            ["ipad", "ipad-blocked"],
            ["ipad", "ipad-blocked"],
            ["chrome", "chrome-blocked"],
            ["chrome", "chrome-blocked"],
            ["safari", "safari-blocked"],
            ["edge", "edge-blocked"],
            ["firefox", "firefox-blocked"],
            ["samsung", "samsung-blocked"],
            ["other", "other-blocked"],
        ])


if __name__ == "__main__":
    unittest.main()
