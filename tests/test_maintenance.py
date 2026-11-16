""" maintenance alerts: consumables, slow tire leak, vehicle alerts, HV faults,
update schedule, monthly battery report; bridge alerts records and new enum prefixes."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import KST, Events, connect as process_connect, format_ts, parse_ts
from process import maintenance as m
from ledger.consumables import create_item, save_replacement, seed_consumables, update_item

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("teslife_bridge_m", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)  # type: ignore[union-attr]

VID = "VMAINT"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = process_connect(Path(self.tmp.name) / "signals.db")
        self.sent: list[dict] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def sender(self, payload: dict) -> tuple[bool, str]:
        self.sent.append(payload)
        return True, "http 200"

    def sig(self, ts: datetime, name: str, value) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, ?, ?)",
                              (VID, format_ts(ts), name, json.dumps(value)))

    def kinds(self, n0: int) -> list[str]:
        return [p["kind"] for p in self.sent[n0:]]

    def msgs(self, n0: int) -> list[str]:
        return [p["message"] for p in self.sent[n0:]]


class ConsumableTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sig(parse_ts("2026-10-31T00:00:00Z"), "Odometer", 891.0)
        seed_consumables(self.conn)
        self.conn.row_factory = None

    def run_at(self, kst: str) -> list[str]:
        n = len(self.sent)
        t = datetime.fromisoformat(kst).replace(tzinfo=KST)
        m.check_consumables(self.conn, VID, None, t, self.sender)
        self.conn.row_factory = None
        return self.msgs(n)

    def test_overdue_once_and_soon_once(self) -> None:
        # registration default + washer 1 month -> overdue; others ok
        first = self.run_at("2026-10-31 18:00")
        self.assertTrue(any(s.startswith("워셔액 기한이 지났어요") for s in first), first)
        self.assertEqual(self.run_at("2026-10-31 18:01"), [])  # once
        self.assertFalse(any("와이퍼" in s for s in first))

    def test_restart_after_replacement_and_soon_window(self) -> None:
        self.run_at("2026-10-31 18:00")
        self.conn.row_factory = sqlite3.Row
        save_replacement(self.conn, VID, {"item_id": "cons-washer", "date": "2026-10-20", "odometer_km": 880, "price_krw": 0})
        self.conn.row_factory = None
        self.assertEqual(self.run_at("2026-10-31 18:05"), [])          # next due 11/20: ok
        soon = self.run_at("2026-11-08 09:00")                          # 12 days left (< 14)
        self.assertEqual(soon, ["워셔액 점검 시기예요 · 12일 남았어요(11/20까지)"])
        self.assertEqual(self.run_at("2026-11-09 09:00"), [])
        over = self.run_at("2026-11-20 09:00")
        self.assertEqual(over, ["워셔액 기한이 지났어요 · 11/20에 지났어요"])

    def test_inactive_skipped_custom_km_item(self) -> None:
        self.conn.row_factory = sqlite3.Row
        update_item(self.conn, VID, {"id": "cons-washer", "active": False})
        create_item(self.conn, VID, {"id": "cons-my", "name": "내 항목", "rule": "km_only", "interval_km": 1200})
        self.conn.row_factory = None
        out = self.run_at("2026-10-31 18:00")
        self.assertFalse(any("워셔액" in s for s in out))  # disabled: skipped
        self.assertIn("내 항목 점검 시기예요 · 약 309 km 남았어요", out)  # custom item counted

    def test_custom_km_soon_text(self) -> None:
        self.conn.row_factory = sqlite3.Row
        create_item(self.conn, VID, {"id": "cons-my", "name": "내 항목", "rule": "km_only", "interval_km": 1200})
        self.conn.row_factory = None
        out = self.run_at("2026-10-31 18:00")
        self.assertIn("내 항목 점검 시기예요 · 약 309 km 남았어요", out)


class TireTests(Base):
    def reading(self, ts: str, fl, fr, rl, rr, temp=15.0, phase="cold") -> None:
        with self.conn:
            self.conn.execute("INSERT INTO tire_readings (vehicle_id, ts, fl, fr, rl, rr, outside_temp, phase) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (VID, ts, fl, fr, rl, rr, temp, phase))

    def run_at(self, ts: str) -> list[str]:
        n = len(self.sent)
        m.check_tire_leak(self.conn, VID, None, parse_ts(ts), self.sender)
        return self.msgs(n)

    def test_one_tire_low_once(self) -> None:
        self.reading("2026-10-31T00:00:00Z", 2.9, 2.9, 2.9, 2.70)
        out = self.run_at("2026-10-31T00:01:00Z")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0] == "공기압 서서히 빠짐 의심 · 뒤 오른쪽 약 39.2 psi, 다른 바퀴보다 약 2.9 psi 낮아요 · 권장 42 psi보다 약 2.9 psi 낮아요", out)
        self.assertEqual(self.run_at("2026-10-31T00:02:00Z"), [])

    def test_small_gap_ok(self) -> None:
        self.reading("2026-10-31T00:00:00Z", 2.9, 2.85, 2.9, 2.775)
        self.assertEqual(self.run_at("2026-10-31T00:01:00Z"), [])

    def test_weekly_drop_same_temp_only(self) -> None:
        self.reading("2026-10-24T00:00:00Z", 2.9, 2.9, 2.9, 2.9, temp=15.0)
        self.reading("2026-10-31T00:00:00Z", 2.9, 2.9, 2.9, 2.775, temp=16.0)
        out = self.run_at("2026-10-31T00:01:00Z")
        self.assertEqual(out, ["공기압 서서히 빠짐 의심 · 뒤 오른쪽 공기가 일주일 새 약 1.8 psi 줄었어요. 바깥 기온이 비슷할 때 비교한 값이에요 · 권장 42 psi보다 약 1.8 psi 낮아요"])

    def test_weekly_drop_ignored_when_colder(self) -> None:
        self.reading("2026-10-24T00:00:00Z", 2.9, 2.9, 2.9, 2.9, temp=20.0)
        self.reading("2026-10-31T00:00:00Z", 2.9, 2.9, 2.9, 2.775, temp=10.0)
        self.assertEqual(self.run_at("2026-10-31T00:01:00Z"), [])


class VehicleAlertTests(Base):
    def line(self, alerts: list[dict]) -> str:
        return json.dumps({"msg": "record_payload", "data": alerts,
                           "metadata": {"txtype": "alerts", "vin": VID}})

    def store(self, alerts: list[dict]) -> None:
        bridge.store_alerts(self.conn, bridge.alerts_from_line(self.line(alerts)))

    def run_at(self, ts: str) -> list[str]:
        n = len(self.sent)
        m.check_vehicle_alerts(self.conn, VID, None, parse_ts(ts), self.sender)
        return self.msgs(n)

    def test_bridge_parses_and_ignores_other_records(self) -> None:
        rows = bridge.alerts_from_line(self.line([{"Name": "X_a1", "Audiences": ["Customer"],
                                                   "StartedAt": "2026-10-31T09:00:00Z"}]))
        self.assertEqual(rows, [(VID, "X_a1", "2026-10-31T09:00:00Z", None, '["Customer"]')])
        err = json.dumps({"msg": "record_payload", "data": [{"Name": "unsupported_field"}],
                          "metadata": {"txtype": "errors", "vin": VID}})
        self.assertEqual(bridge.alerts_from_line(err), [])
        self.assertEqual(bridge.rows_from_line(err), [])

    def test_known_customer_alert_korean_start_end_once(self) -> None:
        name = next(k for k, v in m.alert_dict().items() if v[0] == "Windshield washer fluid low")
        self.store([{"Name": name, "Audiences": ["Service"], "StartedAt": "2026-10-31T09:00:00Z"}])
        out = self.run_at("2026-10-31T09:01:00Z")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("차량 경고 · 워셔액 부족 (10/31 18:00)"), out)
        self.assertEqual(self.run_at("2026-10-31T09:02:00Z"), [])
        self.store([{"Name": name, "Audiences": ["Service"], "StartedAt": "2026-10-31T09:00:00Z",
                     "EndedAt": "2026-10-31T09:30:00Z"}])
        cleared = self.run_at("2026-10-31T09:31:00Z")
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0].startswith("차량 경고 해제 · 워셔액 부족 (10/31 18:30)"), cleared)
        self.assertEqual(self.run_at("2026-10-31T09:32:00Z"), [])

    def test_unknown_customer_raw_name_and_service_only_skipped(self) -> None:
        self.store([{"Name": "NEW_a999_thing", "Audiences": ["Customer"], "StartedAt": "2026-10-31T09:00:00Z"},
                    {"Name": "SVC_a1_internal", "Audiences": ["Service"], "StartedAt": "2026-10-31T09:00:00Z"}])
        out = self.run_at("2026-10-31T09:01:00Z")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("차량 경고 · NEW_a999_thing (10/31 18:00)"), out)
        self.assertIn("코드|NEW_a999_thing", out[0])

    def test_old_alert_stored_quietly(self) -> None:
        self.store([{"Name": "NEW_a999_thing", "Audiences": ["Customer"], "StartedAt": "2026-10-20T09:00:00Z"}])
        self.assertEqual(self.run_at("2026-10-31T09:01:00Z"), [])


class SignalChecks(Base):
    def ev(self, **latest) -> Events:
        t = parse_ts("2026-10-31T09:00:00Z")
        return Events([(t, k, v) for k, v in sorted(latest.items())])

    def test_hv_fault_and_recovery(self) -> None:
        now = parse_ts("2026-10-31T09:00:00Z")
        m.check_hv_fault(self.conn, VID, self.ev(BMSState="Standby", Hvil="OK", DiStateR="Standby"), now, self.sender)
        self.assertEqual(self.sent, [])
        m.check_hv_fault(self.conn, VID, self.ev(BMSState="Fault", Hvil="OK"), now, self.sender)
        self.assertEqual(self.sent[-1]["message"], "고전압 계통 이상 · 서비스 점검 필요 (배터리 관리 장치(BMS) 고장)")
        m.check_hv_fault(self.conn, VID, self.ev(BMSState="Fault", Hvil="OK"), now, self.sender)
        self.assertEqual(len(self.sent), 1)
        m.check_hv_fault(self.conn, VID, self.ev(BMSState="Standby", Hvil="OK"), now, self.sender)
        self.assertEqual(self.sent[-1]["message"], "고전압 계통 정상으로 돌아왔어요")

    def test_update_scheduled_kst(self) -> None:
        # Tesla quirk: reported instant read as Pacific time = car's wall clock (KST here).
        # 10/30 10:30 KST wall clock -> reported as 10/30 10:30 PDT = 17:30Z
        now = parse_ts("2026-10-30T00:00:00Z")  # 10/30 09:00 KST
        ev = self.ev(SoftwareUpdateScheduledStartTime="2026-10-30T17:30:00Z",
                     SoftwareUpdateExpectedDurationMinutes=25, SoftwareUpdateVersion="2026.38.1",
                     SoftwareUpdateAvailable=True)
        m.check_update_scheduled(self.conn, VID, ev, now, self.sender)
        self.assertEqual(self.sent[-1]["message"], "소프트웨어 업데이트 2026.38.1 · 10:30에 설치 예약, 약 25분")
        m.check_update_scheduled(self.conn, VID, ev, now, self.sender)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(m.scheduled_local(0))

    def test_battery_month_once(self) -> None:
        for i, (hi, lo) in enumerate([(3.310, 3.300), (3.320, 3.305), (3.330, 3.310)]):
            t = parse_ts("2026-10-31T01:00:00Z") + timedelta(minutes=i)
            self.sig(t, "BrickVoltageMax", hi)
            self.sig(t, "BrickVoltageMin", lo)
        self.sig(parse_ts("2026-10-31T01:00:00Z"), "NominalFullPackEnergyKwh", 61.99999)
        early = datetime(2026, 11, 1, 8, 0, tzinfo=KST)
        m.check_battery_month(self.conn, VID, None, early, self.sender)
        self.assertEqual(self.sent, [])
        at = datetime(2026, 11, 1, 9, 5, tzinfo=KST)
        m.check_battery_month(self.conn, VID, None, at, self.sender)
        self.assertEqual(self.sent[-1]["message"],
                         "배터리 건강 월간 리포트 (10월) · 용량 약 62.0 kWh · 셀 전압 차이 보통 약 15 mV, 최대 약 20 mV")
        m.check_battery_month(self.conn, VID, None, at + timedelta(hours=1), self.sender)
        self.assertEqual(len(self.sent), 1)

    def test_bridge_new_prefixes(self) -> None:
        self.assertEqual(bridge.convert("BMSState", "BMSStateFault"), "Fault")
        self.assertEqual(bridge.convert("Hvil", "HvilStatusOK"), "OK")
        self.assertEqual(bridge.convert("DiStateR", "DriveInverterStateFault"), "Fault")
        self.assertEqual(bridge.convert("CabinOverheatProtectionMode", "CabinOverheatProtectionModeStateOn"), "On")
        self.assertEqual(bridge.convert("FastChargerType", "FastChargerCombo"), "Combo")

    def test_wired_into_runners(self) -> None:
        src = (ROOT / "process" / "alerts.py").read_text(encoding="utf-8")
        self.assertIn("*SIGNAL_CHECKS", src)
        self.assertIn("IDLE_CHECKS", (ROOT / "process" / "__init__.py").read_text(encoding="utf-8"))
        for s in [*m.ALERT_KO.values()]:
            self.assertNotIn("센트리", s)


if __name__ == "__main__":
    unittest.main()
