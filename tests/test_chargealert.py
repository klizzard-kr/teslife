"""charge_start / charge_end alerts, and AC kind when the car also counts DC energy.

Real 2026-10-30 night charge: AC 6.7 kW 20:54 -> 00:54 KST, DetailedChargeState
Charging -> Complete. ACChargingEnergyIn 0 -> 26.83 kWh and DCChargingEnergyIn
0 -> 24.94 kWh both rose, so the old rule saved it as DC.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest
from process import (
    Events, _load_events, charge_curve, connect as process_connect, parse_ts, process_db,
    sentry_minutes,
)
from process.alerts import run_alerts
from web import charges_in_progress, charge_detail_curve, list_charges, live_charge
from ledger import upsert_charge_meta

from tests.test_charge import VID, plug_in, sig


def with_dc(signals: list[dict], start) -> list[dict]:
    """Add the pack-side DC counter the car also sends during AC charging."""
    out = list(signals) + [sig(start - timedelta(seconds=4), "DCChargingEnergyIn", 0.0),
                           sig(start, "TimeToFullCharge", 4.0)]
    for i in range(1, 7):
        out.append(sig(start + timedelta(minutes=10 * i), "DCChargingEnergyIn", round(0.95 * i, 3)))
    return out


def finish(start, state: str = "Complete") -> list[dict]:
    t = start + timedelta(hours=1, minutes=5)
    return [sig(t, "DetailedChargeState", state), sig(t, "ChargeState", "Idle"),
            sig(t, "ACChargingPower", 0), sig(t, "ChargePortDoorOpen", False)]


class ChargeAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)

    def _alerts(self, now) -> list[dict]:
        sent: list[dict] = []
        conn = process_connect(self.db)
        try:
            ev = _load_events(conn, VID)
            run_alerts(conn, VID, ev, now=now, sender=lambda p: (sent.append(p) or True, "http 200"))
        finally:
            conn.close()
        return [p for p in sent if p["kind"].startswith("charge")]

    def test_ac_with_dc_counter_is_ac(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + finish(start))
        conn = sqlite3.connect(str(self.db))
        row = conn.execute("SELECT kind, kwh_added FROM charges WHERE vehicle_id = ?", (VID,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "AC")
        self.assertAlmostEqual(row[1], 6.6, places=3)  # AC counter 100.0 -> 106.6

    def test_dc_only_is_dc(self) -> None:
        s, start = plug_in()
        s = [x for x in s if x["name"] != "ACChargingEnergyIn"]
        self._store(with_dc(s, start) + finish(start))
        conn = sqlite3.connect(str(self.db))
        row = conn.execute("SELECT kind, kwh_added FROM charges WHERE vehicle_id = ?", (VID,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "DC")
        self.assertAlmostEqual(row[1], 5.7, places=3)

    def test_charge_start_once(self) -> None:
        s, start = plug_in()
        early = [x for x in with_dc(s, start) if x["timestamp"] <= (start + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")]
        self._store(early)
        sent = self._alerts(start + timedelta(minutes=1))
        self.assertEqual([p["kind"] for p in sent], ["charge_start"])
        msg = sent[0]["message"]
        self.assertIn("충전 시작 10/30 20:54", msg)
        self.assertIn("교류(AC)", msg)
        self.assertIn("6.7 kW", msg)
        self.assertIn("배터리 40% → 목표 80%", msg)
        self.assertIn("완충까지 약 3시간 42분", msg)
        self.assertEqual(self._alerts(start + timedelta(minutes=3)), [])  # once per charge

    def test_charge_start_waits_for_power(self) -> None:
        s, start = plug_in()
        cut = (start + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._store([x for x in s if x["timestamp"] <= cut])
        self.assertEqual(self._alerts(start + timedelta(seconds=30)), [])
        sent = self._alerts(start + timedelta(minutes=3))  # gives up waiting, sends what it has
        self.assertEqual([p["kind"] for p in sent], ["charge_start"])

    def test_charge_end_once_with_reason(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + finish(start))
        end = start + timedelta(hours=1, minutes=5)
        sent = self._alerts(end + timedelta(minutes=1))
        kinds = [p["kind"] for p in sent]
        self.assertIn("charge_end", kinds)
        self.assertNotIn("charge_start", kinds)  # stale start is marked silently
        msg = next(p for p in sent if p["kind"] == "charge_end")["message"]
        self.assertIn("충전 완료 10/30 21:59", msg)
        self.assertIn("교류(AC)", msg)
        self.assertIn("6.6 kWh", msg)
        self.assertIn("배터리 40→49%", msg)
        self.assertIn("1시간 5분", msg)
        self.assertIn("평균 6.1 kW", msg)
        self.assertEqual(self._alerts(end + timedelta(minutes=2)), [])

    def test_unplugged_and_stale(self) -> None:
        s, start = plug_in()
        self._store(s + finish(start, "Disconnected"))
        end = start + timedelta(hours=1, minutes=5)
        sent = self._alerts(end + timedelta(minutes=2))
        self.assertIn("연결 안 됨 10/30 21:59", next(p for p in sent if p["kind"] == "charge_end")["message"])
        # a charge that ended long ago (e.g. after a restart) is marked, not sent
        tmp2 = Path(self.tmp.name) / "b.db"
        self.db = tmp2
        self._store(s + finish(start, "Stopped"))
        self.assertEqual(self._alerts(end + timedelta(hours=2)), [])


def extras(start) -> list[dict]:
    """Sentry on 30 min into the charge, a settled full-time estimate at +6 min,
    and a stale DCChargingPower 0 from an earlier session."""
    return [sig(start - timedelta(days=1), "DCChargingPower", 0.0),
            sig(start - timedelta(minutes=5), "SentryMode", "Off"),
            sig(start + timedelta(minutes=30), "SentryMode", "Armed"),
            sig(start + timedelta(minutes=6), "TimeToFullCharge", 3.5)]


class ChargeEnergyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self):
        conn = self._conn()
        try:
            return dict(conn.execute("SELECT * FROM charges WHERE vehicle_id = ?", (VID,)).fetchone())
        finally:
            conn.close()

    def test_breakdown_sentry_and_estimate_saved(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + extras(start) + finish(start))
        r = self._row()
        self.assertAlmostEqual(r["charger_kwh"], 6.6, places=3)
        self.assertAlmostEqual(r["kwh_added"], 6.6, places=3)
        self.assertAlmostEqual(r["battery_kwh"], 5.7, places=3)
        self.assertAlmostEqual(r["loss_kwh"], 0.9, places=3)
        self.assertAlmostEqual(r["loss_pct"], 13.64, places=2)
        self.assertAlmostEqual(r["sentry_min"], 35.0, places=1)  # +30 min .. +65 min
        est = start + timedelta(minutes=6, hours=3, seconds=30 * 60)
        self.assertEqual(r["est_full_ts"], est.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_counter_within_grace_extends_the_total(self) -> None:
        s, start = plug_in()
        end = start + timedelta(hours=1, minutes=5)
        late = end + timedelta(seconds=18)
        self._store(with_dc(s, start) + finish(start) + [
            sig(late, "ACChargingEnergyIn", 108.4),
            sig(late, "DCChargingEnergyIn", 7.2),
        ])
        r = self._row()
        self.assertEqual(r["end_ts"], end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(r["kind"], "AC")
        self.assertAlmostEqual(r["charger_kwh"], 8.4, places=3)
        self.assertAlmostEqual(r["kwh_added"], 8.4, places=3)
        self.assertAlmostEqual(r["battery_kwh"], 7.2, places=3)

    def test_counter_after_grace_is_left_out(self) -> None:
        s, start = plug_in()
        end = start + timedelta(hours=1, minutes=5)
        self._store(with_dc(s, start) + finish(start) + [
            sig(end + timedelta(seconds=90), "ACChargingEnergyIn", 120.0),
            sig(end + timedelta(seconds=90), "DCChargingEnergyIn", 20.0),
        ])
        r = self._row()
        self.assertAlmostEqual(r["charger_kwh"], 6.6, places=3)
        self.assertAlmostEqual(r["battery_kwh"], 5.7, places=3)

    def test_grace_stops_before_the_next_charge(self) -> None:
        s, start = plug_in()
        end = start + timedelta(hours=1, minutes=5)
        nxt = end + timedelta(seconds=40)
        done = nxt + timedelta(minutes=20)
        self._store(with_dc(s, start) + finish(start) + [
            sig(end + timedelta(seconds=15), "ACChargingEnergyIn", 108.0),
            sig(end + timedelta(seconds=15), "DCChargingEnergyIn", 6.4),
            sig(nxt, "DetailedChargeState", "Charging"),
            sig(nxt, "ChargeState", "Enable"),
            sig(nxt, "ACChargingEnergyIn", 109.5),
            sig(nxt + timedelta(minutes=10), "ACChargingEnergyIn", 112.0),
            sig(done, "DetailedChargeState", "Complete"),
            sig(done, "ChargeState", "Idle"),
        ])
        conn = self._conn()
        try:
            rows = list(conn.execute(
                "SELECT start_ts, end_ts, charger_kwh, battery_kwh FROM charges ORDER BY start_ts"
            ))
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["end_ts"], end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertAlmostEqual(rows[0]["charger_kwh"], 8.0, places=3)
        self.assertAlmostEqual(rows[0]["battery_kwh"], 6.4, places=3)
        self.assertAlmostEqual(rows[1]["charger_kwh"], 2.5, places=3)

    def test_charge_end_alert_has_car_use_not_sentry(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + extras(start) + finish(start))
        sent: list[dict] = []
        conn = process_connect(self.db)
        try:
            ev = _load_events(conn, VID)
            run_alerts(conn, VID, ev, now=start + timedelta(hours=1, minutes=6),
                       sender=lambda p: (sent.append(p) or True, "http 200"))
        finally:
            conn.close()
        msg = next(p for p in sent if p["kind"] == "charge_end")["message"]
        self.assertTrue(msg.endswith("· 충전 중 차가 쓴 양 0.9 kWh (13.6%)"), msg)
        self.assertNotIn("센트리", msg)
        self.assertNotIn("감시", msg)
        self.assertNotIn("손실", msg)

    def test_sentry_minutes_rules(self) -> None:
        from datetime import datetime, timezone
        t = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        ev = Events([(t - timedelta(minutes=10), "SentryMode", "Aware"),
                     (t + timedelta(minutes=20), "SentryMode", "Off"),
                     (t + timedelta(minutes=40), "SentryMode", "Idle")])
        self.assertEqual(sentry_minutes(ev, t, t + timedelta(minutes=60)), 40.0)
        self.assertEqual(sentry_minutes(Events([]), t, t + timedelta(minutes=5)), 0.0)

    def test_billed_meta_listed_with_car_kwh(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + finish(start))
        conn = self._conn()
        try:
            cid = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            upsert_charge_meta(conn, VID, {"charge_id": cid, "operator": "플러그링크",
                                           "kwh_billed": 6.75, "total_krw": 1971})
            item = list_charges(conn, VID)[0]
        finally:
            conn.close()
        self.assertEqual(item["meta"]["kwh_billed"], 6.75)
        self.assertEqual(item["meta"]["total_krw"], 1971)
        self.assertAlmostEqual(item["meta"]["kwh_billed"] - item["charger_kwh"], 0.15, places=3)


class LiveChargeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        return conn

    def test_in_progress_row_then_gone(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + extras(start))
        now = start + timedelta(minutes=61)
        conn = self._conn()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], 0)
            live = live_charge(conn, VID, now)
            self.assertIsNotNone(live)
            self.assertTrue(live["in_progress"])
            self.assertEqual(live["start_ts"], start.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(live["kind"], "AC")
            self.assertAlmostEqual(live["power_kw"], 6.7, places=3)
            self.assertEqual(live["soc"], 49.0)
            self.assertEqual(live["limit_soc"], 80.0)
            self.assertAlmostEqual(live["charger_kwh"], 6.6, places=3)
            # latest estimate: +6 min sample, 3.5 h
            self.assertEqual(live["est_full_ts"], (start + timedelta(minutes=216)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertIsNotNone(charges_in_progress(conn, VID, month="2026-10", now=now))
            self.assertIsNone(charges_in_progress(conn, VID, month="2026-09", now=now))
            self.assertIsNone(charges_in_progress(conn, VID, offset=20, now=now))
        finally:
            conn.close()
        self._store(finish(start))
        conn = self._conn()
        try:
            self.assertIsNone(live_charge(conn, VID, start + timedelta(hours=2)))
        finally:
            conn.close()


class ChargeCurveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, signals: list[dict]) -> None:
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, signals)
        conn.close()
        process_db(self.db, seed=False)

    def test_ac_curve_uses_ac_power_and_matches_max(self) -> None:
        s, start = plug_in()
        self._store(with_dc(s, start) + extras(start) + finish(start))
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            cid = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            pts = [p["kw"] for p in charge_detail_curve(conn, VID, cid)["points"] if p["kw"] is not None]
            row = conn.execute("SELECT kind, max_charger_kw FROM charges").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["kind"], "AC")
        self.assertTrue(pts)
        self.assertAlmostEqual(max(pts), 6.7, places=3)  # not the stale DC 0
        self.assertAlmostEqual(row["max_charger_kw"], max(pts), places=6)

    def test_volts_amps_fallback_and_no_stale_values(self) -> None:
        from datetime import datetime, timezone
        t = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        ev = Events([
            (t - timedelta(days=1), "ACChargingPower", 0.0),  # earlier session
            (t + timedelta(seconds=10), "ChargerVoltage", 210.0),
            (t + timedelta(seconds=10), "ChargeAmps", 32.0),
            (t + timedelta(minutes=5), "BatteryLevel", 50.0),
        ])
        pts = charge_curve(ev, t, t + timedelta(minutes=10), "AC")
        self.assertTrue(pts)
        self.assertTrue(all(p["kw"] == 6.72 for p in pts))


class ChargeCardUiTests(unittest.TestCase):
    """Static checks of the 충전 기록 card markup in web/static/index.html."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_detail_list_like_trips(self) -> None:
        # one "name … value" per line (kvHtml, same as trip detail); related values in order
        h = self.html
        body = h[h.index("function chargeDetailPairs("):h.index("function openChargeDetail(")]
        order = ['add("시작"', 'add("종료"', 'add("충전기에서 받은 양"', 'add("배터리에 들어간 양"',
                 'add("충전 중 차가 쓴 양"', 'add("사업자"', 'add("청구"', 'add("금액"', 'add("단가"']
        idx = [body.index(k) for k in order]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn("chip(", body)
        live = h[h.index("function liveChargeCard("):h.index("function chargeDetailPairs(")]
        self.assertIn("kvHtml(pairs)", live)
        self.assertIn('add("지금까지 충전량"', live)
        detail = h[h.index("function openChargeDetail("):h.index("function drawMix(")]
        self.assertIn("const pairs = chargeDetailPairs(c);", detail)
        self.assertIn("kvHtml(pairs)", detail)

    def test_car_use_shown_not_loss_or_sentry(self) -> None:
        self.assertIn("충전 중 차가 쓴 양", self.html)
        self.assertNotIn('chip("손실"', self.html)
        self.assertNotIn("sentry_min", self.html)

    def test_unknown_values_hidden(self) -> None:
        self.assertIn('const add = (k, v) => { if (known(v) && v !== "—") pairs.push([k, v]); };', self.html)
        self.assertNotIn("곡선 원본 없음", self.html)

    def test_curve_inline_lazy_no_toggle(self) -> None:
        # : the curve sits in the charge detail sheet, loaded when the sheet opens
        self.assertNotIn("data-charge-curve-toggle", self.html)
        self.assertNotIn("곡선 닫기", self.html)
        self.assertIn("chargeCurveCache", self.html)
        detail = self.html[self.html.index("function openChargeDetail("):self.html.index("function drawMix(")]
        self.assertIn("loadChargeCurve(box, c)", detail)

    def test_finished_cards_collapse_like_trips(self) -> None:
        # : list rows open a detail sheet (same as trips); the in-progress card is not a row
        h = self.html
        rows = h[h.index("function renderChargeRows("):h.index("function openChargeDetail(")]
        self.assertIn("trip-line", rows)
        self.assertNotIn("chargeDetailPairs", rows)
        live = h[h.index("function liveChargeCard("):h.index("function chargeDetailPairs(")]
        self.assertNotIn("toggle", live)


if __name__ == "__main__":
    unittest.main()
