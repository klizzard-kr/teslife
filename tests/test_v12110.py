"""v1.21.10: no pin-tap delete; toast copy; themed map pops; keep trip selection; legend."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
I18N = (ROOT / "web/static/i18n.js").read_text(encoding="utf-8")
NAVER = (ROOT / "web/static/naver-map.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
BANNED = "".join(chr(c) for c in (0xC18C, 0xC0AC, 0xBCF8, 0xB3D9))


def _fn(start: str, end: str, src: str = HTML) -> str:
    i = src.find("function " + start)
    j = src.find("function " + end, i + 1)
    if i < 0 or j < 0:
        raise AssertionError(start + ".." + end)
    return src[i:j]


def _node(script: str) -> str:
    out = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise AssertionError((out.stdout or "") + "\n" + (out.stderr or ""))
    return out.stdout


class VersionTests(unittest.TestCase):
    def test_version_and_docs(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.10"', HTML)
        self.assertIn("현재 버전은 v1.21.10입니다.", README)
        self.assertIn("화면의 제품 버전은 `v1.21.10`", DESIGN)
        self.assertIn("## v1.21.10", CHANGELOG)
        self.assertIn("집 핀을 눌러 지우기를 고르던 기능을 빼었습니다", CHANGELOG)
        self.assertIn("DELETE /api/home", CHANGELOG)
        self.assertIn("화면에서는 더 이상 쓰지 않습니다", CHANGELOG)
        self.assertIn("도로명 주소를 복사했어요", CHANGELOG)
        self.assertIn("지번 주소를 복사했어요", CHANGELOG)
        self.assertIn("목록 선택은 바뀌지 않습니다", CHANGELOG)
        self.assertIn("횟수와 함께", CHANGELOG)
        self.assertIn("표시 단추 아래 안내 줄", CHANGELOG)
        self.assertIn("집 핀을 눌러도 지우기 단추는 나오지 않는다", DESIGN)
        self.assertIn("도로명 주소를 복사했어요", DESIGN)
        self.assertIn("목록 선택은 바뀌지 않고", DESIGN)
        self.assertIn("집 핀을 눌러도 지우기 단추는 나오지 않아요", HTML)
        self.assertIn('{ version: "1.21.10"', HTML)
        self.assertIn("도로명 주소를 복사했어요", HTML)
        self.assertIn("지번 주소를 복사했어요", HTML)
        for text, label in (
            (HTML, "html"),
            (I18N, "i18n"),
            (CHANGELOG, "changelog"),
            (DESIGN, "design"),
        ):
            self.assertNotIn(BANNED, text, msg=label)


class ToastTests(unittest.TestCase):
    def test_toast_markup_and_copy_messages(self) -> None:
        self.assertIn('id="app-toast"', HTML)
        self.assertIn('aria-live="polite"', HTML)
        self.assertIn("function showToast(message)", HTML)
        self.assertIn("el.classList.add(\"is-on\")", HTML)
        self.assertIn("}, 2000)", HTML)
        self.assertIn("bottom: calc(var(--tab-h) + var(--safe-b) + 12px)", HTML)
        self.assertIn(".app-toast", HTML)
        src = _fn("copyLocAddr", "paintLocAddrPop")
        self.assertIn("function copyLocAddr(text)", src)
        self.assertIn("navigator.clipboard.writeText(shown).catch(fallback)", src)
        paint = _fn("paintLocAddrPop", "toggleLocAddr")
        self.assertIn('copied: "도로명 주소를 복사했어요"', paint)
        self.assertIn('copied: "지번 주소를 복사했어요"', paint)
        self.assertIn("showToast(locAddrLabel(row.copied, row.copied))", paint)
        self.assertIn('copy.textContent = locAddrLabel("복사", "복사")', paint)
        self.assertNotIn("locAddrCopyTimer", HTML)

    def test_new_toast_replaces_old(self) -> None:
        src = _fn("showToast", "copyLocAddr")
        self.assertIn("if (toastTimer) clearTimeout(toastTimer)", src)
        self.assertIn("el.textContent = text", src)


class I18nTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_english_toast_and_map_lines(self) -> None:
        pairs = (
            ("도로명 주소를 복사했어요", "Copied the road address"),
            ("지번 주소를 복사했어요", "Copied the lot address"),
            ("현재 위치: 집 핀을 눌러도 지우기 단추는 나오지 않아요. 핀을 옮겨 집으로 저장하는 것은 그대로예요", "no longer shows a delete button"),
        )
        for ko, en in pairs:
            out = _node(
                I18N
                + "\nTeslifeI18n.setLanguage('en', false);\n"
                + "process.stdout.write(TeslifeI18n.t("
                + json.dumps(ko)
                + "));"
            )
            self.assertIn(en, out)


class MapPopTests(unittest.TestCase):
    def test_popup_uses_app_surface(self) -> None:
        self.assertIn(".teslife-map-pop, .ev-pop", HTML)
        self.assertIn("background: var(--card)", HTML)
        self.assertIn("backgroundColor: \"transparent\"", NAVER)
        self.assertIn("borderWidth: 0", NAVER)
        self.assertIn("anchorSize: new maps.Size(0, 0)", NAVER)
        self.assertIn("function wrapMapPop", NAVER)
        self.assertIn("Overlay.prototype.closePopup", NAVER)
        self.assertIn("closePopup: function () { closeOpenInfo(); }", NAVER)
        self.assertIn("if (openInfo && openInfo !== this._info) closeOpenInfo()", NAVER)

    def test_pops_close_outside_scroll_and_layer_change(self) -> None:
        self.assertIn("function closeTripMapPop", HTML)
        self.assertIn("function holdTripMapPop", HTML)
        self.assertIn('window.addEventListener("scroll", function () { closeTripMapPop(); closeChargeMapPop(); }, true)', HTML)
        self.assertIn(".teslife-map-pop, .ev-pop, .leaflet-popup, .app-toast", HTML)
        self.assertIn("closeTripMapPop();", _fn("paintSelected", "pickRouteAt") if "function pickRouteAt" in _fn("paintSelected", "drawTripRoutes") else HTML)
        self.assertIn("holdTripMapPop();", HTML)
        # switching layer/trip closes via paintSelected
        self.assertIn("function paintSelected", HTML)
        paint = HTML[HTML.index("function paintSelected"):HTML.index("if (!replaced && route)")]
        self.assertIn("closeTripMapPop()", paint)

    def test_map_tap_does_not_change_list_selection(self) -> None:
        pick = _fn("pickRouteAt", "drawTripRoutes")
        self.assertNotIn("toggleTrip", pick)
        self.assertIn("never change the trip list selection", pick)
        click_i = HTML.index('tripsMap.on("click"')
        click = HTML[click_i:HTML.index("syncMapTools();", click_i)]
        self.assertNotIn("toggleTrip", click)
        self.assertNotIn("pickRouteAt", click)
        self.assertIn("closeTripMapPop()", click)
        seg = HTML[HTML.index("const rec = { line: line"):HTML.index("arrowLines.push(latlngs)")]
        self.assertIn("openPiecePop(rec", seg)
        self.assertIn("hit.on(\"click\", onSeg)", seg)
        self.assertNotIn("toggleTrip", seg)
        self.assertIn('toggleTrip(t.start_ts, true)', HTML)


class LegendTests(unittest.TestCase):
    def test_legend_matches_layer_and_event_counts(self) -> None:
        src = _fn("paintTripMapLegend", "closeLayerPops")
        self.assertIn("SPEED_BANDS.forEach", src)
        self.assertIn("30 km/h 이하", HTML)
        self.assertIn("오토파일럿 켜짐", src)
        self.assertIn("오토파일럿 꺼짐", src)
        self.assertIn("오토파일럿 기록 없음", src)
        self.assertIn("EVENT_STYLE[k][0] + \" \" + n[k]", src)
        self.assertIn("if (n[k])", src)
        self.assertIn("e.lat != null", src)
        self.assertIn("pointer-events: none", HTML)
        self.assertIn("trips-map-legend", HTML)
        self.assertNotIn("도로 맞춤 전", HTML)
        self.assertNotIn('id="trips-map-note"', HTML)
        self.assertNotIn('getElementById("trips-map-note")', HTML)
        self.assertIn('id="trips-map-status"', HTML)
        self.assertNotIn('eventLegend(mapPrefs', HTML)
        self.assertNotIn('"<span>선택 "', HTML)



class LineJoinTests(unittest.TestCase):
    def test_trip_polylines_use_round_joins_and_caps(self) -> None:
        self.assertIn('strokeLineJoin: style.lineJoin || "round"', NAVER)
        self.assertIn('strokeLineCap: style.lineCap || "round"', NAVER)
        self.assertIn('lineJoin: "round"', HTML)
        self.assertIn('lineCap: "round"', HTML)
        self.assertIn("__teslifeRoundLines", HTML)
        self.assertIn('Object.assign({ lineJoin: "round", lineCap: "round" }', HTML)


class HitAreaTests(unittest.TestCase):
    def test_wide_hit_polyline_and_nearest_segment(self) -> None:
        self.assertIn("weight: 24", HTML)
        self.assertIn("interactive: true", HTML)
        self.assertIn("function nearestTripPiece", HTML)
        self.assertIn("nearestTripPiece(ev.latlng, 20)", HTML)
        self.assertIn("function openPiecePop", HTML)
        self.assertIn("clickable: style.interactive === true || (style.interactive !== false && style.opacity !== 0)", NAVER)
        self.assertIn("hit.on(\"click\", onSeg)", HTML)


class NoteRemovedTests(unittest.TestCase):
    def test_trips_map_note_is_gone(self) -> None:
        self.assertNotIn('id="trips-map-note"', HTML)
        self.assertNotIn("trips-map-note", HTML)
        self.assertNotIn('"<span>선택 "', HTML)
        self.assertNotIn("도로 맞춤 전", HTML)


class ListToggleTests(unittest.TestCase):
    def test_list_tap_toggles_or_switches(self) -> None:
        tog = _fn("toggleTrip", "highlightTrip")
        self.assertIn("highlightTrip(pickedTrip === startTs ? null : startTs, fit)", tog)
        rows = _fn("renderTripRows", "openTripSheet") if "function openTripSheet" in HTML else HTML
        self.assertIn("toggleTrip(t.start_ts, true)", rows)
        click_i = HTML.index('tripsMap.on("click"')
        click = HTML[click_i:HTML.index("});", click_i) + 3]
        self.assertNotIn("toggleTrip", click)
        self.assertNotIn("highlightTrip", click)
        self.assertIn("openPiecePop", click)

class NoDeleteUiTests(unittest.TestCase):
    def test_pin_tap_delete_is_gone_save_drag_remains(self) -> None:
        self.assertNotIn("function bindPinElementTap", HTML)
        self.assertNotIn("function selectHomePin", HTML)
        self.assertNotIn("function deleteCarHome", HTML)
        self.assertNotIn("PIN_DELETE_ICON", HTML)
        self.assertNotIn("집으로 저장된 위치를 삭제할까요?", HTML)
        self.assertNotIn('setPinAction("delete")', HTML)
        self.assertNotIn('mode === "delete"', HTML)
        self.assertIn("function finishPinDrag", HTML)
        self.assertIn('setPinAction("save")', HTML)
        self.assertIn("선택한 위치를 집으로 저장할까요?", HTML)
        self.assertIn('writeJson("PUT", "/api/home"', HTML)
        self.assertNotIn('writeJson("DELETE", "/api/home"', HTML)
        self.assertIn("function bindPinDrag", HTML)
        self.assertIn('marker.on("dragend"', HTML)
        self.assertIn("if (marker.__pinBound) return;", HTML)

    def test_delete_home_api_remains(self) -> None:
        web = (ROOT / "web/__init__.py").read_text(encoding="utf-8")
        carloc = (ROOT / "web/carloc.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/home" and method == "DELETE":', web)
        self.assertIn('"/api/home"', web)
        self.assertIn("def delete_home", carloc)
        self.assertNotIn(BANNED, web)
        self.assertNotIn(BANNED, carloc)


if __name__ == "__main__":
    unittest.main()
