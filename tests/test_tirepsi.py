""": 타이어 종류별 권장 냉간 공기압, 권장 대비 알림, 화면 표시."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from ledger.consumables import ensure_consumable_schema
from ledger.tires import LABEL_BAR, get_tire_settings, low_wheels, rec_text
from process import COLD_PARK, Events, connect as process_connect, format_ts, parse_ts
from process import alerts as A
from process import maintenance as m
from web import dispatch_write, recommended_tire_alert

ROOT = Path(__file__).resolve().parents[1]
VID = "VTIRE"
T0 = parse_ts("2026-10-31T00:00:00Z")   # 09:00 KST


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = process_connect(Path(self.tmp.name) / "signals.db")
        ensure_consumable_schema(self.conn)
        self.sent: list[dict] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def sender(self, p):
        self.sent.append(p)
        return True, "http 200"

    def reading(self, ts, fl, fr, rl, rr, phase="cold", temp=15.0):
        with self.conn:
            self.conn.execute("INSERT INTO tire_readings (vehicle_id, ts, fl, fr, rl, rr, outside_temp, phase) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (VID, format_ts(ts), fl, fr, rl, rr, temp, phase))


class SettingsTests(Base):
    def test_fixed_label_value(self) -> None:
        s = get_tire_settings(self.conn, VID)
        self.assertEqual(s["recommended"], {"front": 2.9, "rear": 2.9})
        self.assertEqual(s["recommended_psi"], {"front": 42, "rear": 42})
        self.assertEqual(rec_text(s), "42 psi")
        with self.conn:   # old saved values are ignored now
            self.conn.execute("INSERT INTO vehicle_settings VALUES (?, 'tire_pressure', ?)", (VID, json.dumps({"front": 3.0, "rear": 3.0})))
        self.assertEqual(get_tire_settings(self.conn, VID)["recommended"]["front"], 2.9)

    def test_settings_api_no_tire_edit(self) -> None:
        with self.assertRaises(ValueError):
            dispatch_write("PUT", "/api/settings", self.conn, VID, {"tires": {"front": 3.0}})

    def test_low_wheels_margin(self) -> None:
        s = get_tire_settings(self.conn, VID)
        self.assertEqual([w for w, *_ in low_wheels({"fl": 2.7, "fr": 2.725, "rl": 2.9, "rr": 2.5}, s)], ["rr"])
        quiet = recommended_tire_alert({"fl": 2.7, "fr": 2.9, "rl": 2.9, "rr": 2.9, "ts": "x", "phase": "cold"}, s)
        self.assertFalse(quiet["alert"])
        a = recommended_tire_alert({"fl": 2.65, "fr": 2.9, "rl": 2.9, "rr": 2.9, "ts": "x", "phase": "cold"}, s)
        self.assertTrue(a["alert"])
        self.assertEqual(a["wheels"], ["fl"])
        self.assertEqual(a["threshold_psi"], 39.0)
        self.assertEqual(a["recommended_psi"], {"front": 42, "rear": 42})
        from ledger.tires import displayed_low_wheels, displayed_psi
        shown_ok = 41.7 / 14.5038
        self.assertEqual(displayed_psi(shown_ok), 41.7)
        self.assertEqual(displayed_low_wheels({"fl": shown_ok, "fr": 2.689, "rl": 2.688, "rr": 2.684}), ["rr"])
        self.assertEqual(displayed_psi(2.689), 39.0)
        self.assertEqual(displayed_psi(2.684), 38.9)


class LowAlertTests(Base):
    def run_at(self, now):
        n = len(self.sent)
        m.check_tire_low(self.conn, VID, None, now, self.sender)
        return [p["message"] for p in self.sent[n:]]

    def test_alert_once_text_and_new_wheel(self) -> None:
        self.reading(T0, 2.65, 2.9, 2.875, 2.9)
        out = self.run_at(T0 + timedelta(minutes=1))
        self.assertEqual(out, ["공기압이 낮아요 · 앞 왼쪽이 39.0 psi보다 낮아요"])
        self.assertEqual(self.run_at(T0 + timedelta(minutes=2)), [])
        self.reading(T0 + timedelta(hours=20), 2.7, 2.675, 2.9, 2.9)
        out = self.run_at(T0 + timedelta(hours=20, minutes=1))
        self.assertEqual(len(out), 1)
        self.assertIn("앞 오른쪽이 39.0 psi보다 낮아요", out[0])
        # back in range -> quiet; low again later -> alerts again
        self.reading(T0 + timedelta(hours=30), 2.9, 2.9, 2.9, 2.9)
        self.assertEqual(self.run_at(T0 + timedelta(hours=30, minutes=1)), [])
        self.reading(T0 + timedelta(hours=50), 2.65, 2.9, 2.9, 2.9)
        self.assertEqual(len(self.run_at(T0 + timedelta(hours=50, minutes=1))), 1)

    def test_warm_and_stale_ignored(self) -> None:
        self.reading(T0, 2.5, 2.5, 2.5, 2.5, phase="warm")
        self.assertEqual(self.run_at(T0 + timedelta(minutes=1)), [])
        self.reading(T0 - timedelta(hours=1), 2.5, 2.5, 2.5, 2.5, phase="cold")
        self.assertEqual(self.run_at(T0 + timedelta(hours=48)), [])

    def test_small_gap_ok(self) -> None:
        self.reading(T0, 2.75, 2.75, 2.75, 2.75)
        self.assertEqual(self.run_at(T0 + timedelta(minutes=1)), [])       # 0.15 bar under: fine

    def test_cold_rule_is_three_hours(self) -> None:
        self.assertEqual(COLD_PARK, timedelta(hours=3))


class OtherAlertTexts(Base):
    def ev(self, **latest) -> Events:
        return Events([(T0, k, v) for k, v in sorted(latest.items())])

    def test_tpms_warning_has_recommended_gap(self) -> None:
        ev = self.ev(TpmsSoftWarnings=True, TpmsPressureFl=2.4, TpmsPressureFr=2.9, TpmsPressureRl=2.9, TpmsPressureRr=2.9)
        A.check_tpms_warning(self.conn, VID, ev, T0, self.sender)
        msg = self.sent[-1]["message"]
        self.assertTrue(msg.startswith("차 공기압 경고 (점검 필요) · 앞 왼쪽 약 34.8 psi · 권장 42 psi보다 약 7.3 psi 낮아요"), msg)
        self.assertNotIn("—", msg)

    def test_tires_text(self) -> None:
        self.assertEqual(A._tires_text({"fl": 2.7, "fr": 2.7, "rl": 2.7, "rr": 2.7}, "42 psi"),
                         "39.2/39.2/39.2/39.2 psi, 권장 42 psi")
        self.assertEqual(A._tires_text({"fl": None, "fr": 2.7}), "39.2 psi")
        self.assertEqual(A._tires_text(None), "")
        self.assertEqual(A._rec(self.conn, VID), "42 psi")

    def test_wired(self) -> None:
        self.assertIn(m.check_tire_low, m.IDLE_CHECKS)
        self.assertIn(m.check_tire_low, m.SIGNAL_CHECKS)


class UiTests(unittest.TestCase):
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_web_shows_no_recommended_or_warning(self) -> None:
        """: alerts live in chat only; the web shows tire values (status) without 권장/warnings."""
        for gone in ('id="tire-form"', 'id="v-fl-rec"', 'id="tire-banner"', "function applyTireRec",
                     "renderTireSettings", "tireThreshold", "점선=경고 기준", '"/api/settings", { tires',
                     "권장 42", 'name="tire_type"'):
            self.assertNotIn(gone, self.html, gone)
        for kept in ('id="v-fl"', 'id="v-rr"', "트레드 깊이"):
            self.assertIn(kept, self.html, kept)
        self.assertNotIn('id="tire-settings-card"', self.html)
        self.assertNotIn("홈 깊이 기록", self.html)
        self.assertNotIn("tireset-alert", self.html)
        self.assertIn("function tireSetChecks", self.html)

    def test_tab_renamed_jeongbi(self) -> None:
        """: the 소모품 tab and card read 정비; only the 차계부 category '소모품' (parts bought) stays."""
        i = self.html.index('id="tab-btn-consumables"')
        self.assertIn("정비", self.html[i:self.html.index("</button>", i)])
        self.assertIn("<h2>정비</h2>", self.html)
        rest = self.html.replace('<option value="supplies">소모품</option>', "").replace('supplies: "소모품"', "")
        self.assertNotIn("소모품", rest)

    def test_add_item_is_a_popup(self) -> None:
        """: '항목 추가' is a button that opens a sheet; no inline form in the card."""
        self.assertIn('id="item-add-open"', self.html)
        ov = self.html[self.html.index('id="item-overlay"'):]
        self.assertLess(ov.index('id="item-form"'), ov.index('id="kind-overlay"'))
        self.assertIn('id="item-cancel"', ov[:ov.index("</form>")])
        self.assertIn('"item-overlay", "period-overlay"', self.html)            # backdrop closes it
        card = self.html[self.html.index('id="consumables-list"'):self.html.index("</section>", self.html.index('id="consumables-list"'))]
        self.assertNotIn("<form", card)

    def test_home_has_no_warning_card(self) -> None:
        """: no 소모품 알림 card or warning band at the top of home."""
        home = self.html[self.html.index('id="tab-home"'):self.html.index('id="live-card"')]
        self.assertEqual(home.count("<section"), 1)   # only the live card's own opening tag
        for gone in ('id="due-card"', 'id="due-list"', "소모품 알림", '"/api/due"', 'class="banner'):
            self.assertNotIn(gone, self.html, gone)


if __name__ == "__main__":
    unittest.main()
