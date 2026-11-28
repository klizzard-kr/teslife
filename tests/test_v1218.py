"""v1.21.8: home-pin delete on tap, value-row align, tooltip chrome, dismiss rules.

Tests use made-up names only (한빛동, 한들도 해솔시 별빛구 …). No real places.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from process import PROCESS_VERSION

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

BANNED = "".join(chr(n) for n in (0xC18C, 0xC0AC, 0xBCF8, 0xB3D9))


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _fn(name: str, nxt: str) -> str:
    start = "function " + name
    end = "function " + nxt
    return start + HTML.split(start, 1)[1].split(end, 1)[0]


def _fn_until(name: str, stopper: str) -> str:
    start = "function " + name
    return start + HTML.split(start, 1)[1].split(stopper, 1)[0]


def _node(src: str) -> str:
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError((out.stdout or "") + "\n" + (out.stderr or ""))
    return out.stdout


class VersionTests(unittest.TestCase):
    def test_version_and_docs(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.8"', HTML)
        self.assertIn("현재 버전은 v1.21.8입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.8`", DESIGN)
        self.assertIn("## v1.21.8", CHANGELOG)
        self.assertIn("집 핀을 눌러 고르면 지우기", CHANGELOG)
        self.assertIn("약 8px", CHANGELOG)
        self.assertIn("약 6px", CHANGELOG)
        self.assertIn("작고 둥근 알약", CHANGELOG)
        self.assertIn("상자와 화살표 밖을 누르거나", CHANGELOG)
        self.assertIn("복사 전에 닫히지 않습니다", CHANGELOG)
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('{ version: "1.21.8"', HTML)
        self.assertIn("집 핀을 눌러 고르면 지우기 단추가 나와요", HTML)
        self.assertIn("위치와 상태 글자 높이를 맞춰요", HTML)
        self.assertIn("복사 단추를 작고 둥글게 줄이고", HTML)
        for text, label in ((HTML, "html"), (I18N, "i18n"), (CHANGELOG, "changelog"), (DESIGN, "design")):
            self.assertNotIn(BANNED, text, msg=label)


class I18nTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_fix_lines(self) -> None:
        def t(ko: str) -> str:
            return _node(
                I18N + "\nTeslifeI18n.setLanguage('en', false);\n"
                "process.stdout.write(TeslifeI18n.t(" + json.dumps(ko) + "));"
            )

        self.assertIn("delete button", t("현재 위치: 집 핀을 눌러 고르면 지우기 단추가 나와요. 손가락으로 눌러도 같아요"))
        self.assertIn("same line", t("현재 위치: 위치와 상태 글자 높이를 맞춰요. 동 이름과 집 아이콘 사이를 조금 더 띄워요"))
        self.assertIn("closes", t("상세 주소 상자: 복사 단추를 작고 둥글게 줄이고, 배경을 카드와 구분해 보여요. 화면을 스크롤하거나 상자 밖을 누르면 닫혀요"))


class LayoutCssTests(unittest.TestCase):
    def test_value_rows_match_live_card_and_icon_gaps(self) -> None:
        area = _between(HTML, "#car-loc-area,\n    #car-loc-state {", "}")
        self.assertIn("align-items: baseline", area)
        self.assertIn("min-height: 0", area)
        main = _between(HTML, "#car-loc-area .car-loc-area-main {", "}")
        self.assertIn("align-items: baseline", main)
        mark = _between(HTML, "#car-loc-area .car-loc-home-mark {", "}")
        self.assertIn("margin-left: 8px", mark)
        self.assertNotIn("0.75px", mark)
        toggle = _between(HTML, "#car-loc-area .car-loc-addr-toggle {", "}")
        self.assertIn("min-height: 0", toggle)
        self.assertIn("-webkit-appearance: none", toggle)
        self.assertIn("appearance: none", toggle)
        self.assertIn("margin: 0 0 0 6px", toggle)
        self.assertNotIn("margin: 0 0 0 -2px", toggle)
        self.assertIn(".metric .value { font-size: 1.25rem; margin-top: 4px;", HTML)

    def test_tooltip_is_compact_and_distinct(self) -> None:
        pop = _between(HTML, ".car-loc-addr-pop {", "}")
        self.assertIn("background: #243140", pop)
        self.assertNotIn("background: var(--card)", pop)
        self.assertIn("border: 1px solid #4a5c70", pop)
        self.assertIn("box-shadow:", pop)
        light = _between(HTML, '[data-theme="light"] .car-loc-addr-pop {', "}")
        self.assertIn("background: #eef4f9", light)
        self.assertIn("border: 1px solid #c3d0db", light)
        row = _between(HTML, ".car-loc-addr-row {", "}")
        self.assertIn("padding: 2px 6px", row)
        copy = _between(HTML, ".car-loc-addr-copy {", "}")
        self.assertIn("border-radius: 999px", copy)
        self.assertIn("min-height: 0", copy)
        self.assertIn("height: auto", copy)
        self.assertIn("padding: 1px 6px", copy)
        self.assertIn("font-size: 0.7rem", copy)
        self.assertIn("-webkit-appearance: none", copy)


class PinTapTests(unittest.TestCase):
    def test_helpers_are_in_the_page(self) -> None:
        self.assertIn("function selectHomePin", HTML)
        self.assertIn("function finishPinDrag", HTML)
        self.assertIn("if (pinAwayFromHome())", HTML)
        self.assertIn("selectHomePin();", HTML)
        self.assertNotIn(
            'marker.on("dragend", () => {\n        pinMoved = true;\n        setPinAction("save");',
            HTML,
        )

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_touch_and_mouse_pin_gestures(self) -> None:
        src = r"""
const btn = {
  hidden: true, dataset: { mode: "" }, innerHTML: "", title: "",
  setAttribute(k, v) { this[k] = v; }
};
const document = { getElementById(id) { return id === "car-loc-pin-action" ? btn : null; } };
let carLocData = { home: { lat: 37.4, lon: 127.1 } };
let homeMarker = {
  _ll: { lat: 37.4, lon: 127.1 },
  getLatLng() { return this._ll; },
  setLatLng(ll) { this._ll = { lat: ll.lat, lon: ll.lon != null ? ll.lon : ll.lng }; }
};
let pinMoved = false;
let pinDragging = false;
let pinActionMode = "";
function syncCarNearPin() {}
const PIN_DELETE_ICON = "DELETE";
""" + _fn("latLngOf", "metresApart") + _fn("metresApart", "pinAwayFromHome") + _fn(
            "pinAwayFromHome", "homeMarkIcon"
        ) + "\n" + _fn("paintPinButton", "setPinAction") + _fn("setPinAction", "onPinActionClick") + _fn(
            "selectHomePin", "finishPinDrag"
        ) + _fn("finishPinDrag", "bindPinDrag") + _fn("bindPinDrag", "fitCarLoc") + r"""
function FakeMarker() { this.handlers = {}; }
FakeMarker.prototype.on = function (name, fn) { this.handlers[name] = fn; return this; };
FakeMarker.prototype.fire = function (name) { if (this.handlers[name]) this.handlers[name](); };

const marker = new FakeMarker();
bindPinDrag(marker);

function reset() {
  pinActionMode = "";
  pinMoved = false;
  pinDragging = false;
  btn.hidden = true;
  btn.dataset.mode = "";
  btn.innerHTML = "";
  homeMarker.setLatLng({ lat: 37.4, lon: 127.1 });
  paintPinButton();
}
function state() {
  return { mode: btn.dataset.mode || "", hidden: !!btn.hidden, html: btn.innerHTML, pinMoved: pinMoved };
}

const out = {};

reset();
marker.fire("dragstart");
marker.fire("dragend");
out.touchSelect = state();

reset();
marker.fire("dragstart");
marker.fire("dragend");
marker.fire("click");
out.touchSelectThenClick = state();

reset();
marker.fire("click");
out.mouseClick = state();

reset();
marker.fire("dragstart");
homeMarker.setLatLng({ lat: 37.401, lon: 127.1 });
marker.fire("dragend");
marker.fire("click");
out.dragAway = state();

process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(_node(src))
        for key in ("touchSelect", "touchSelectThenClick", "mouseClick"):
            self.assertEqual(got[key]["mode"], "delete", msg=key)
            self.assertFalse(got[key]["hidden"], msg=key)
            self.assertTrue(got[key]["html"], msg=key)
            self.assertFalse(got[key]["pinMoved"], msg=key)
        self.assertEqual(got["dragAway"]["mode"], "save")
        self.assertFalse(got["dragAway"]["hidden"])
        self.assertTrue(got["dragAway"]["html"])
        self.assertTrue(got["dragAway"]["pinMoved"])


class TooltipDismissTests(unittest.TestCase):
    def test_listeners_and_copy_do_not_close_first(self) -> None:
        self.assertIn("function locAddrEventInside", HTML)
        self.assertIn("function dismissLocAddr", HTML)
        self.assertIn('document.addEventListener("pointerdown", dismissLocAddr, true)', HTML)
        self.assertIn('document.addEventListener("click", dismissLocAddr, true)', HTML)
        self.assertIn(
            'window.addEventListener("scroll", function () { if (locAddrOpen) closeLocAddr(); }, true)',
            HTML,
        )
        self.assertIn('t.closest("#car-loc-addr-pop")', HTML)
        self.assertIn("ev.stopPropagation();", _between(
            HTML,
            "copy.addEventListener(\"pointerdown\"",
            "copy.addEventListener(\"click\"",
        ))
        self.assertIn("copyLocAddr(text.textContent, copy)", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_outside_scroll_and_copy_behavior(self) -> None:
        block = _fn("locAddrEventInside", "dismissLocAddr") + _fn_until(
            "dismissLocAddr", 'document.addEventListener("pointerdown"'
        )
        src = r"""
function el(id, cls) {
  const node = {
    id: id || "",
    className: cls || "",
    hidden: false,
    parent: null,
    setAttribute(k, v) { this[k] = v; },
    contains(other) {
      let n = other;
      while (n) { if (n === this) return true; n = n.parent; }
      return false;
    },
    closest(sel) {
      let n = this;
      while (n) {
        if (sel === "#car-loc-addr-pop" && n.id === "car-loc-addr-pop") return n;
        if (sel === "#car-loc-area .car-loc-addr-toggle" && (n.className || "").indexOf("car-loc-addr-toggle") >= 0) return n;
        n = n.parent;
      }
      return null;
    }
  };
  return node;
}
const pop = el("car-loc-addr-pop");
const toggle = el("", "car-loc-addr-toggle");
const copy = el("", "car-loc-addr-copy");
copy.parent = pop;
const map = el("car-loc-map");
const document = {
  getElementById(id) { return id === "car-loc-addr-pop" ? pop : null; },
  querySelector(sel) { return sel.indexOf("car-loc-addr-toggle") >= 0 ? toggle : null; }
};
let locAddrOpen = true;
pop.hidden = false;
toggle.setAttribute("aria-expanded", "true");
""" + _fn("closeLocAddr", "positionLocAddr") + block + r"""
const out = {};
dismissLocAddr({ target: map });
out.afterMap = { open: locAddrOpen, hidden: !!pop.hidden };

locAddrOpen = true; pop.hidden = false; toggle.setAttribute("aria-expanded", "true");
dismissLocAddr({ target: copy });
out.afterCopy = { open: locAddrOpen, hidden: !!pop.hidden };

locAddrOpen = true; pop.hidden = false; toggle.setAttribute("aria-expanded", "true");
dismissLocAddr({ target: toggle });
out.afterToggle = { open: locAddrOpen, hidden: !!pop.hidden };

locAddrOpen = true; pop.hidden = false; toggle.setAttribute("aria-expanded", "true");
if (locAddrOpen) closeLocAddr();
out.afterScroll = { open: locAddrOpen, hidden: !!pop.hidden };

process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(_node(src))
        self.assertFalse(got["afterMap"]["open"])
        self.assertTrue(got["afterMap"]["hidden"])
        self.assertTrue(got["afterCopy"]["open"])
        self.assertFalse(got["afterCopy"]["hidden"])
        self.assertTrue(got["afterToggle"]["open"])
        self.assertFalse(got["afterToggle"]["hidden"])
        self.assertFalse(got["afterScroll"]["open"])
        self.assertTrue(got["afterScroll"]["hidden"])


if __name__ == "__main__":
    unittest.main()
