"""v1.21.9: pin picture tap, icon/chevron center align, whole value row opens tooltip.

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
NAVER = (ROOT / "web" / "static" / "naver-map.js").read_text(encoding="utf-8")
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
        self.assertIn('const APP_VERSION = "v1.21.12"', HTML)
        self.assertIn("현재 버전은 v1.21.12입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.12`", DESIGN)
        self.assertIn("## v1.21.9", CHANGELOG)
        self.assertIn("핀 그림 자체에서 누름이 끝나도", CHANGELOG)
        self.assertIn("시각적 가운데", CHANGELOG)
        self.assertIn("값 줄 전체가 하나의 단추", CHANGELOG)
        self.assertIn("이름이나 집 아이콘을 눌러도", CHANGELOG)
        self.assertIn("칸 이름은 주소 위 한 줄", CHANGELOG)
        self.assertIn("약 2px", CHANGELOG)
        self.assertIn("세로 가운데", CHANGELOG)
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('{ version: "1.21.9"', HTML)
        self.assertIn("지도 위 핀 그림을 손가락으로 눌러도 같아요", HTML)
        self.assertIn("집 아이콘과 화살표를 동 이름 글자 가운데에 맞춰요", HTML)
        self.assertIn("동 이름, 집 아이콘, 화살표 줄 전체를 누르면", HTML)
        self.assertIn("복사 단추는 그 항목의 세로 가운데에 둬요", HTML)
        self.assertIn("칸 이름은 주소 위 한 줄이고", DESIGN)
        self.assertNotIn("function bindPinElementTap", HTML)
        self.assertIn("hookMarkerDomClick", NAVER)
        self.assertIn("값 줄 전체가 하나의 단추이며", DESIGN)
        self.assertIn("시각적 가운데에 맞춘다", DESIGN)
        for text, label in (
            (HTML, "html"),
            (I18N, "i18n"),
            (CHANGELOG, "changelog"),
            (DESIGN, "design"),
            (NAVER, "naver"),
        ):
            self.assertNotIn(BANNED, text, msg=label)


class I18nTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_fix_lines(self) -> None:
        def t(ko: str) -> str:
            return _node(
                I18N + "\nTeslifeI18n.setLanguage('en', false);\n"
                "process.stdout.write(TeslifeI18n.t(" + json.dumps(ko) + "));"
            )

        self.assertIn("pin picture", t("현재 위치: 집 핀을 눌러 고르면 지우기 단추가 나와요. 지도 위 핀 그림을 손가락으로 눌러도 같아요"))
        self.assertIn("middle", t("현재 위치: 집 아이콘과 화살표를 동 이름 글자 가운데에 맞춰요"))
        self.assertIn("anywhere", t("현재 위치: 동 이름, 집 아이콘, 화살표 줄 전체를 누르면 상세 주소가 열려요"))


class LayoutCssTests(unittest.TestCase):
    def test_value_row_centers_icons_and_resets_button(self) -> None:
        area = _between(HTML, "#car-loc-area,\n    #car-loc-state {", "}")
        self.assertIn("align-items: baseline", area)
        self.assertIn("min-height: 0", area)
        main = _between(HTML, "#car-loc-area .car-loc-area-main {", "}")
        self.assertIn("display: inline-flex", main)
        self.assertIn("align-items: center", main)
        self.assertNotIn("align-items: baseline", main)
        self.assertIn("min-height: 0", main)
        self.assertIn("height: auto", main)
        self.assertIn("padding: 0", main)
        self.assertIn("border: 0", main)
        self.assertIn("background: transparent", main)
        self.assertIn("font: inherit", main)
        self.assertIn("-webkit-appearance: none", main)
        self.assertIn("appearance: none", main)
        mark = _between(HTML, "#car-loc-area .car-loc-home-mark {", "}")
        self.assertIn("margin-left: 8px", mark)
        self.assertIn("align-items: center", mark)
        self.assertIn("width: 1cap", mark)
        self.assertIn("height: 1cap", mark)
        self.assertIn("width: 0.8em", mark)
        icon = _between(HTML, "#car-loc-area .car-loc-home-icon {", "}")
        self.assertIn("width: 1cap", icon)
        self.assertIn("height: 1cap", icon)
        self.assertIn("width: 0.8em", icon)
        toggle = _between(HTML, "#car-loc-area .car-loc-addr-toggle {", "}")
        self.assertIn("min-height: 0", toggle)
        self.assertIn("pointer-events: none", toggle)
        self.assertIn("margin: 0 0 0 6px", toggle)
        self.assertIn("width: 1cap", toggle)
        self.assertIn("height: 1cap", toggle)
        self.assertIn("align-items: center", toggle)
        self.assertIn(
            '#car-loc-area .car-loc-area-main[aria-expanded="true"] .car-loc-addr-toggle svg',
            HTML,
        )

    def test_tooltip_label_gap_and_copy_centered(self) -> None:
        kind = _between(HTML, ".car-loc-addr-kind {", "}")
        self.assertIn("display: block", kind)
        self.assertIn("margin: 0 0 2px", kind)
        self.assertNotIn("margin-bottom: 0", kind)
        text = _between(HTML, ".car-loc-addr-text {", "}")
        self.assertIn("display: block", text)
        self.assertIn("margin: 0", text)
        row = _between(HTML, ".car-loc-addr-row {", "}")
        self.assertIn("align-items: center", row)
        self.assertNotIn("align-items: flex-start", row)
        self.assertIn("gap: 6px", row)
        nxt = _between(HTML, ".car-loc-addr-row + .car-loc-addr-row {", "}")
        self.assertIn("margin-top: 6px", nxt)
        self.assertIn("padding-top: 6px", nxt)
        copy = _between(HTML, ".car-loc-addr-copy {", "}")
        self.assertIn("align-self: center", copy)
        self.assertNotIn("align-self: flex-start", copy)


class ValueRowButtonTests(unittest.TestCase):
    def test_set_loc_area_wraps_row_in_one_button(self) -> None:
        src = _fn("setLocArea", "locAddrEventInside")
        self.assertIn('document.createElement(hasDetail ? "button" : "span")', src)
        self.assertIn('main.addEventListener("click", toggleLocAddr)', src)
        self.assertIn('chev.className = "car-loc-addr-toggle"', src)
        self.assertIn('document.createElement("span")', src)
        self.assertNotIn('className = "car-loc-addr-toggle";\n        btn.setAttribute("aria-expanded"', HTML)
        self.assertIn('t.closest("#car-loc-area .car-loc-area-main")', HTML)
        self.assertIn("function locAddrOpenBtn", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_tapping_name_or_house_opens_tooltip(self) -> None:
        src = r"""
function Node(tag) {
  this.tagName = String(tag || "").toLowerCase();
  this.childNodes = [];
  this.attrs = {};
  this.listeners = {};
  this.className = "";
  this.id = "";
  this.hidden = false;
  this._innerHTML = "";
  this.parent = null;
  this.textContent = "";
  this.style = {};
  Object.defineProperty(this, "innerHTML", {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v);
      if (String(v).indexOf("<svg") >= 0) {
        const svg = new Node("svg");
        svg.className = "car-loc-home-icon";
        this.childNodes = [svg];
        svg.parent = this;
      }
    }
  });
}
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.append = function () {
  for (let i = 0; i < arguments.length; i++) {
    let n = arguments[i];
    if (!(n instanceof Node)) {
      const t = new Node("#text");
      t.textContent = String(n);
      n = t;
    }
    n.parent = this;
    this.childNodes.push(n);
  }
};
Node.prototype.replaceChildren = function () { this.childNodes = []; };
Node.prototype.addEventListener = function (name, fn) {
  (this.listeners[name] = this.listeners[name] || []).push(fn);
};
Node.prototype.querySelector = function (sel) {
  const match = (n) => {
    if (sel === "svg" && n.tagName === "svg") return true;
    if (sel.charAt(0) === "." && (" " + (n.className || "") + " ").indexOf(" " + sel.slice(1) + " ") >= 0) return true;
    return false;
  };
  const walk = (n) => {
    for (const c of n.childNodes || []) {
      if (match(c)) return c;
      const hit = walk(c);
      if (hit) return hit;
    }
    return null;
  };
  return walk(this);
};
Node.prototype.closest = function (sel) {
  let n = this;
  while (n) {
    if (sel === "#car-loc-addr-pop" && n.id === "car-loc-addr-pop") return n;
    if (sel === "#car-loc-area .car-loc-area-main" && (n.className || "").indexOf("car-loc-area-main") >= 0) return n;
    if (sel === "#car-loc-area .car-loc-addr-toggle" && (n.className || "").indexOf("car-loc-addr-toggle") >= 0) return n;
    n = n.parent;
  }
  return null;
};
function fireClick(n) {
  let cur = n;
  while (cur) {
    const fns = (cur.listeners && cur.listeners.click) || [];
    for (const fn of fns) fn({ preventDefault() {}, stopPropagation() {} });
    cur = cur.parent;
  }
}
function texts(n, acc) {
  if (n.tagName === "#text" && n.textContent) acc.push(n.textContent);
  for (const c of n.childNodes || []) texts(c, acc);
  return acc;
}
function findClass(n, cls) {
  if ((" " + (n.className || "") + " ").indexOf(" " + cls + " ") >= 0) return n;
  for (const c of n.childNodes || []) {
    const hit = findClass(c, cls);
    if (hit) return hit;
  }
  return null;
}

const pop = new Node("div");
pop.id = "car-loc-addr-pop";
pop.hidden = true;
const area = new Node("div");
area.id = "car-loc-area";
const document = {
  createElement(tag) { return new Node(tag); },
  createTextNode(s) { const n = new Node("#text"); n.textContent = String(s); return n; },
  getElementById(id) { return id === "car-loc-addr-pop" ? pop : (id === "car-loc-area" ? area : null); },
  querySelector(sel) {
    if (sel.indexOf("button.car-loc-area-main") >= 0) {
      for (const c of area.childNodes) if (c.tagName === "button") return c;
      return null;
    }
    if (sel.indexOf("car-loc-addr-toggle") >= 0) return findClass(area, "car-loc-addr-toggle");
    return null;
  }
};
const window = {};
const ADDR_CHEVRON = "<svg></svg>";
function locAddrLabel(key, fallback) { return fallback; }
let locAddrOpen = false;
let locAddrCopyTimer = 0;
function paintLocAddrPop() {}
function positionLocAddr() {}
""" + _fn("locAddrOpenBtn", "closeLocAddr") + _fn("closeLocAddr", "positionLocAddr") + _fn("toggleLocAddr", "setLocArea") + _fn_until("homeMarkIcon", "const ADDR_CHEVRON") + _fn("setLocArea", "locAddrEventInside") + r"""
setLocArea(area, "한빛동", true, null, {
  road: "한들도 해솔시 별빛구 별빛로 12 (한빛타워)",
  jibun: "한들도 해솔시 별빛구 한빛동 123-4"
});
const main = area.childNodes[0];
const out = {
  tag: main.tagName,
  type: main.type,
  cls: main.className,
  texts: texts(main, []),
  hasHouse: !!findClass(main, "car-loc-home-mark"),
  chevTag: (findClass(main, "car-loc-addr-toggle") || {}).tagName || "",
  nestedButtons: (function () {
    let n = 0;
    const walk = (node) => {
      for (const c of node.childNodes || []) {
        if (c.tagName === "button") n += 1;
        walk(c);
      }
    };
    walk(main);
    return n;
  })()
};
fireClick(main.childNodes[0]);
out.afterName = { open: locAddrOpen, expanded: main.attrs["aria-expanded"], hidden: !!pop.hidden };
locAddrOpen = false; pop.hidden = true; main.setAttribute("aria-expanded", "false");
fireClick(findClass(main, "car-loc-home-mark"));
out.afterHouse = { open: locAddrOpen, expanded: main.attrs["aria-expanded"], hidden: !!pop.hidden };
setLocArea(area, "한빛동", true, null, null);
out.noDetailTag = area.childNodes[0].tagName;
process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(_node(src))
        self.assertEqual(got["tag"], "button")
        self.assertEqual(got["type"], "button")
        self.assertEqual(got["cls"], "car-loc-area-main")
        self.assertIn("한빛동", got["texts"])
        self.assertTrue(got["hasHouse"])
        self.assertEqual(got["chevTag"], "span")
        self.assertEqual(got["nestedButtons"], 0)
        self.assertTrue(got["afterName"]["open"])
        self.assertEqual(got["afterName"]["expanded"], "true")
        self.assertFalse(got["afterName"]["hidden"])
        self.assertTrue(got["afterHouse"]["open"])
        self.assertEqual(got["afterHouse"]["expanded"], "true")
        self.assertEqual(got["noDetailTag"], "span")


class PinElementTapTests(unittest.TestCase):
    def test_helpers_are_in_the_page(self) -> None:
        self.assertNotIn("function bindPinElementTap", HTML)
        self.assertNotIn("function selectHomePin", HTML)
        self.assertNotIn("function deleteCarHome", HTML)
        self.assertIn("function bindPinDrag", HTML)
        self.assertIn("function finishPinDrag", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dom_tap_does_not_show_delete_and_drag_still_saves(self) -> None:
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
""" + _fn("latLngOf", "metresApart") + _fn("metresApart", "pinAwayFromHome") + _fn(
            "pinAwayFromHome", "homeMarkIcon"
        ) + "\n" + _fn("paintPinButton", "setPinAction") + _fn("setPinAction", "onPinActionClick") + _fn(
            "finishPinDrag", "bindPinDrag"
        ) + _fn("bindPinDrag", "fitCarLoc") + r"""
function FakeEl() { this.handlers = {}; }
FakeEl.prototype.addEventListener = function (name, fn, _opts) {
  (this.handlers[name] = this.handlers[name] || []).push(fn);
};
FakeEl.prototype.fire = function (name) {
  (this.handlers[name] || []).forEach(function (fn) { fn({}); });
};
function FakeMarker() { this.handlers = {}; this.el = new FakeEl(); }
FakeMarker.prototype.on = function (name, fn) { this.handlers[name] = fn; return this; };
FakeMarker.prototype.fire = function (name) { if (this.handlers[name]) this.handlers[name](); };
FakeMarker.prototype.getElement = function () { return this.el; };

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
marker.el.fire("pointerdown");
marker.el.fire("pointerup");
out.domPointer = state();

reset();
marker.el.fire("touchstart");
marker.el.fire("touchend");
out.domTouch = state();

reset();
marker.fire("dragstart");
homeMarker.setLatLng({ lat: 37.401, lon: 127.1 });
marker.fire("dragend");
marker.el.fire("pointerup");
out.dragThenDomUp = state();

process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(_node(src))
        for key in ("domPointer", "domTouch"):
            self.assertEqual(got[key]["mode"], "", msg=key)
            self.assertTrue(got[key]["hidden"], msg=key)
            self.assertFalse(got[key]["html"], msg=key)
            self.assertFalse(got[key]["pinMoved"], msg=key)
        self.assertEqual(got["dragThenDomUp"]["mode"], "save")
        self.assertTrue(got["dragThenDomUp"]["pinMoved"])


if __name__ == "__main__":
    unittest.main()
