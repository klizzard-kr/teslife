"""Maintenance / consumable alerts.

All checks only read what is already in the DB (no car wakes, no Fleet API calls) and send
through process.alerts._emit, so they land in alert_log and the same webhook as the other
alerts. Each one is deduplicated in alert_state (or vehicle_alerts.notified_*), so a state is
announced once and survives restarts.

1. consumable:<item>  소모품 시기 (soon / overdue), from the 정비 tab (was 소모품) settings.
2. tire_leak          공기압 서서히 빠짐 (one tire low vs the others, or a weekly drop).
3. vehicle_alert      차량 경고 (Fleet Telemetry Alerts records, customer-facing ones).
4. hv_fault           고전압 계통 이상 (BMSState / Hvil / DiStateR fault values).
5. update_scheduled   업데이트 설치 예약 (SoftwareUpdateScheduledStartTime, in KST).
6. battery_month      배터리 건강 월간 리포트 (1st of the month, from 09:00 KST).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from process import KST, WHEELS, _as_float, decode_value, format_ts, parse_ts

PSI_PER_BAR = 14.5038
WHEEL_KO = {"fl": "앞 왼쪽", "fr": "앞 오른쪽", "rl": "뒤 왼쪽", "rr": "뒤 오른쪽"}

# 2. tires
TIRE_LOW_BAR = 0.15       # one tire this much below the average of the other three
TIRE_DROP_BAR = 0.10      # drop over about a week
TIRE_WEEK = (timedelta(days=6), timedelta(days=8))
TIRE_TEMP_C = 3.0         # "comparable outside temperature"

# 3. vehicle alerts
VALERT_FRESH = timedelta(hours=24)   # older starts (first enable, replays) are stored quietly
_DICT_PATH = Path(__file__).with_name("alert_dict.json")
_DICT: dict[str, list[str]] | None = None
# Korean for Tesla's customer-facing messages (CustomerFacingMessage1), where we have one.
ALERT_KO = {
    "Low voltage battery disconnected": "저전압(12V) 배터리 연결 끊김",
    "Low voltage battery configuration error detected": "저전압(12V) 배터리 설정 오류",
    "Schedule service to replace low voltage battery": "저전압(12V) 배터리 교체 필요 - 서비스 예약",
    "Low voltage electrical system issue detected": "저전압 전기 계통 이상",
    "Low voltage battery replacement detected": "저전압(12V) 배터리 교체 감지",
    "Connector on low voltage battery is not secured": "저전압(12V) 배터리 커넥터가 제대로 안 끼워짐",
    "Vehicle may not restart - Service is required": "다시 시동이 안 걸릴 수 있음 - 서비스 필요",
    "Brake fluid low": "브레이크액 부족",
    "Brake fluid level sensor service required": "브레이크액 센서 점검 필요",
    "Coolant low - Schedule service": "냉각수 부족 - 서비스 예약",
    "Windshield washer fluid low": "워셔액 부족",
    "Windshield wipers are unavailable": "와이퍼 사용 불가",
    "Air pressure below recommendation for tires": "타이어 공기압이 권장값보다 낮음",
    "Air pressure in tires very low": "타이어 공기압 매우 낮음",
    "Air pressure over recommendation for tires": "타이어 공기압이 권장값보다 높음",
    "Air pressure exceeds recommendation for tires": "타이어 공기압이 권장값보다 높음",
    "Tire pressure monitoring system fault": "타이어 공기압 감지 장치 고장",
    "Rear tire tread depth low - Schedule service": "뒤 타이어 마모 - 서비스 예약",
    "Steering alignment needs adjustment": "휠 얼라인먼트 조정 필요",
    "Steering alignment service recommended": "휠 얼라인먼트 점검 권장",
    "Cabin climate control system requires service": "공조 장치 서비스 필요",
    "High voltage battery requires service": "고전압 배터리 서비스 필요",
    "High voltage battery performance limited": "고전압 배터리 성능 제한",
    "Maximum battery charge level reduced": "최대 충전량 제한됨",
    "Battery power output very low": "배터리 출력 매우 낮음",
    "Battery charge level low": "배터리 잔량 낮음",
    "Powertrain issue detected - Schedule service": "구동계 이상 - 서비스 예약",
    "Powertrain requires service - OK to drive": "구동계 서비스 필요 - 운행은 가능",
    "Powertrain requires service": "구동계 서비스 필요",
    "Unable to drive - Service is required": "주행 불가 - 서비스 필요",
    "Gearbox fluid service required": "감속기 오일 서비스 필요",
    "Safety restraint system issue - Service required": "에어백·안전벨트 계통 이상 - 서비스 필요",
    "Pedestrian safety system issue - Service required": "보행자 경고 장치 이상 - 서비스 필요",
    "Parking brake functions degraded": "주차 브레이크 기능 저하",
    "Parking brake did not release": "주차 브레이크가 풀리지 않음",
    "Low brake stiffness detected": "브레이크 페달 감 이상",
    "Brakes too hot - Braking performance reduced": "브레이크 과열 - 제동력 저하",
    "Brake pedal regenerative braking unavailable": "브레이크 페달 회생제동 사용 불가",
    "Left brake light condition detected": "왼쪽 브레이크등 이상",
    "Right brake light condition detected": "오른쪽 브레이크등 이상",
    "Center brake light condition detected": "가운데 브레이크등 이상",
    "Steering assist reduced": "조향 보조 약해짐",
    "Steering is unavailable": "조향 불가",
    "Camera system requires service": "카메라 서비스 필요",
    "Cameras unavailable": "카메라 사용 불가",
    "Hood latch sensor requires service": "보닛 잠금 센서 서비스 필요",
    "Charge port requires service": "충전구 서비스 필요",
    "Charge port door sensor fault": "충전구 덮개 센서 고장",
    "Unable to AC charge - System will retry shortly": "완속 충전이 잠시 안 돼요",
    "Disconnect and retry or use different equipment": "케이블을 뺐다 꼽거나 다른 충전기를 써 보세요",
    "Charge port error detected - Service is required": "충전구 오류 - 서비스 필요",
    "Charge port error detected - Unable to charge": "충전구 오류 - 충전 불가",
    "Charge port latch fault - AC charging reduced": "충전구 잠금 고장 - 완속 충전 속도 감소",
    "Unable to charge": "충전 불가",
    "Unable to charge - Service is required": "충전 불가 - 서비스 필요",
    "Unable to charge - Vehicle connection issue": "충전 불가 - 차 연결 문제",
    "Unable to DC Fast Charge": "급속 충전 불가",
    "Unable to AC charge": "완속 충전 불가",
    "Charge rate reduced - Unexpected voltage drop": "충전 속도 감소 - 전압 강하",
    "Power Conversion System requires service": "충전 변환 장치 서비스 필요",
    "Software update required": "소프트웨어 업데이트 필요",
    "Key fob battery low": "키 배터리 부족",
    "Service is required": "서비스 필요",
}

# 4. HV faults
HV_FAULTS = {
    "BMSState": ({"Fault"}, {"Fault": "배터리 관리 장치(BMS) 고장"}),
    "Hvil": ({"Fault"}, {"Fault": "고전압 연결 회로(HVIL) 이상"}),
    "DiStateR": ({"Fault"}, {"Fault": "뒤 모터 인버터 고장"}),
}

# 6. battery report
BATTERY_REPORT_HOUR = 9  # KST, on the 1st


def _psi(bar: float) -> str:
    return f"{bar * PSI_PER_BAR:.1f}"


def _alerts():
    from process import alerts  # late import: process.alerts imports this module

    return alerts


def _emit(conn, vid, kind, state, msg, data, now, sender) -> bool:
    return _alerts()._emit(conn, vid, kind, state, msg, data, now, sender)


def _get(conn, vid, kind):
    return _alerts()._get_state(conn, vid, kind)


def _set(conn, vid, kind, state, now):
    _alerts()._set_state(conn, vid, kind, state, now)


def _md(d: str | date) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.month}/{d.day}"


# ---------------------------------------------------------------- 1. consumables
def check_tire_sets(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    """Tire set notices for tread and warranty only. DOT stays on the set and does not alert.
    One message per alert level; clearing is quiet."""
    from ledger.tiresets import list_sets

    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        sets = list_sets(conn, vid, today=now.astimezone(KST).date())
    finally:
        conn.row_factory = old
    for st in sets:
        if st.get("retired"):
            continue
        active = {a["code"]: a for a in st["alerts"]}
        for code in ("tread", "age", "warranty"):
            kind = f"tireset:{st['id']}:{code}"
            prev = _get(conn, vid, kind)
            a = active.get(code)
            if a is None:
                if prev is not None and prev != "ok":
                    _set(conn, vid, kind, "ok", now)
                continue
            if prev == a["level"]:
                continue
            where = "장착 중" if st["mounted"] else "보관 중"
            msg = f"타이어 {'교체' if a['level'] == 'overdue' else '점검'} · {st['label']}({where}) · {a['text']}"
            data = {"set_id": st["id"], "code": code, "level": a["level"], "km_total": st["km_total"],
                    "age_years": st["age_years"], "tread": st["tread"]}
            _emit(conn, vid, kind, a["level"], msg, data, now, sender)


def check_consumables(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    """Reads the 정비 tab as saved (own intervals and warn windows, custom items, inactive
    items skipped, last replacement as the restart point), so web edits apply on the next run."""
    from ledger.consumables import due_label, list_items

    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        items = list_items(conn, vid, include_inactive=False, as_of=now.astimezone(KST).date())
    finally:
        conn.row_factory = old
    for it in items:
        # Rotation is part of the one tire item. A seasonal swap counts there, and this
        # item does not send its own inspection alert or grow a ledger row.
        if it["id"] == "cons-tire-rotation" or it.get("rule") == "as_needed":
            continue
        kind = f"consumable:{it['id']}"
        status = it.get("status")
        # one cycle = one due point; a new replacement (or edited interval) starts a new cycle
        cycle = f"{it.get('next_due_date') or ''}/{round(it['next_due_km']) if it.get('next_due_km') is not None else ''}"
        prev = _get(conn, vid, kind)
        if status not in ("soon", "overdue"):
            if prev is not None and prev != f"{cycle}|ok":
                _set(conn, vid, kind, f"{cycle}|ok", now)
            continue
        state = f"{cycle}|{status}"
        if prev == state:
            continue
        name = it.get("name") or it["id"]
        plain = name[:-2].strip() if str(name).endswith("점검") else str(name)
        label = due_label(name, it.get("default_action") or "replace")
        rk, rd = it.get("remaining_km"), it.get("remaining_days")
        parts = []
        if status == "soon":
            if rd is not None and rd > 0 and rd <= (it.get("warn_before_days") or 0):
                parts.append(f"{int(rd)}일 남았어요({_md(it['next_due_date'])}까지)")
            if rk is not None and rk > 0 and rk <= (it.get("warn_before_km") or 0):
                parts.append(f"약 {max(1, round(rk))} km 남았어요")
            msg = f"{plain} 점검 시기예요"
            if parts:
                msg += " · " + ", ".join(parts)
        else:
            if rd is not None and rd <= 0:
                parts.append(f"{_md(it['next_due_date'])}에 지났어요")
            if rk is not None and rk <= 0:
                parts.append(f"약 {max(1, round(-rk))} km 지났어요")
            msg = f"{plain} 기한이 지났어요"
            if parts:
                msg += " · " + ", ".join(parts)
        data = {k: it.get(k) for k in ("id", "name", "status", "next_due_date", "next_due_km",
                                       "remaining_days", "remaining_km", "current_odometer_km",
                                       "default_action")}
        _emit(conn, vid, kind, state, msg, data, now, sender)


# ---------------------------------------------------------------- 2. tire slow leak
def _tire_rows(conn, vid, since: datetime) -> list[tuple]:
    return conn.execute(
        "SELECT ts, fl, fr, rl, rr, outside_temp, phase FROM tire_readings "
        "WHERE vehicle_id = ? AND ts >= ? ORDER BY ts",
        (vid, format_ts(since)),
    ).fetchall()


def tire_findings(rows: list[tuple], now: datetime) -> list[tuple[str, str, float]]:
    """[(code, wheel, bar)] code 'low' (vs other three) or 'drop' (weekly, same phase/temp)."""
    if not rows:
        return []
    last = rows[-1]
    vals = dict(zip(WHEELS, last[1:5]))
    out: list[tuple[str, str, float]] = []
    if all(v is not None for v in vals.values()):
        for w, v in vals.items():
            others = [vals[o] for o in WHEELS if o != w]
            gap = sum(others) / 3 - v
            if gap > TIRE_LOW_BAR + 1e-9:
                out.append(("low", w, gap))
    t_last = parse_ts(last[0])
    temp, phase = _as_float(last[5]), last[6]
    if temp is not None:
        old = [r for r in rows
               if TIRE_WEEK[0] <= t_last - parse_ts(r[0]) <= TIRE_WEEK[1]
               and r[6] == phase and _as_float(r[5]) is not None
               and abs(_as_float(r[5]) - temp) <= TIRE_TEMP_C]
        if old:
            for i, w in enumerate(WHEELS):
                past = [r[1 + i] for r in old if r[1 + i] is not None]
                if past and vals[w] is not None:
                    drop = median(past) - vals[w]
                    if drop > TIRE_DROP_BAR + 1e-9:
                        out.append(("drop", w, drop))
    return out


def check_tire_leak(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    rows = _tire_rows(conn, vid, now - TIRE_WEEK[1] - timedelta(days=1))
    if not rows:
        return
    found = tire_findings(rows, now)
    keys = sorted({f"{c}:{w}" for c, w, _ in found})
    state = ",".join(keys) if keys else "none"
    prev = _get(conn, vid, "tire_leak")
    if prev == state:
        return
    new = [f for f in found if f"{f[0]}:{f[1]}" not in set((prev or "").split(","))]
    if not new:
        _set(conn, vid, "tire_leak", state, now)  # cleared or shrank: remember quietly
        return
    last = rows[-1]
    vals = dict(zip(WHEELS, last[1:5]))
    bits = []
    ts_ = _tire_settings(conn, vid)
    for code, w, bar in new:
        if code == "low":
            bit = f"{WHEEL_KO[w]} 약 {_psi(vals[w])} psi, 다른 바퀴보다 약 {_psi(bar)} psi 낮아요"
        else:
            bit = f"{WHEEL_KO[w]} 공기가 일주일 새 약 {_psi(bar)} psi 줄었어요. 바깥 기온이 비슷할 때 비교한 값이에요"
        bit += _vs_rec(ts_, w, vals[w])
        bits.append(bit)
    msg = "공기압 서서히 빠짐 의심 · " + " / ".join(bits)
    data = {"findings": [{"type": c, "wheel": w, "bar": round(b, 3)} for c, w, b in new],
            "tires_psi": {w: (_psi(v) if v is not None else None) for w, v in vals.items()},
            "reading_ts": last[0]}
    _emit(conn, vid, "tire_leak", state, msg, data, now, sender)


def _tire_settings(conn, vid) -> dict:
    from ledger.tires import get_tire_settings
    return get_tire_settings(conn, vid)


def _vs_rec(settings: dict, w: str, bar: Any) -> str:
    """' · 권장 42 psi보다 약 2.8 psi 낮아요' (only when below by ≥ 0.05 bar; never for missing values)."""
    from ledger.tires import recommended_for
    v = _as_float(bar)
    if v is None:
        return ""
    rec = recommended_for(settings, w)
    below = rec - v
    if below < 0.05:
        return ""
    return f" · 권장 {round(rec * PSI_PER_BAR)} psi보다 약 {below * PSI_PER_BAR:.1f} psi 낮아요"


# ---------------------------------------------------------------- 2b. below the recommended cold pressure
TIRE_LOW_FRESH = timedelta(hours=48)


def check_tire_low(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    """Latest COLD reading (parked ≥ 3 h before a drive, Tesla's 'cold' rule).

    A wheel is low when the psi printed from that bar is strictly under 39.0.
    The comparison is in psi. Warm values never count. The 42 psi label is not
    the warning line. One alert per low episode; a newly low wheel alerts again;
    all back at 39.0 or above clears quietly.
    """
    from ledger.tires import LOW_PSI, low_wheels
    try:
        row = conn.execute(
            "SELECT ts, fl, fr, rl, rr, outside_temp FROM tire_readings "
            "WHERE vehicle_id = ? AND phase = 'cold' ORDER BY ts DESC LIMIT 1", (vid,)).fetchone()
    except sqlite3.OperationalError:
        return
    if row is None or now - parse_ts(row[0]) > TIRE_LOW_FRESH:
        return
    settings = _tire_settings(conn, vid)
    reading = dict(zip(WHEELS, row[1:5]))
    low = low_wheels(reading, settings)
    state = "low:" + ",".join(w for w, _v, _b in low) if low else "ok"
    prev = _get(conn, vid, "tire_low")
    if prev == state:
        return
    prev_set = set((prev or "").replace("low:", "").split(",")) if prev and prev.startswith("low:") else set()
    if not low or {w for w, _v, _b in low} <= prev_set:
        _set(conn, vid, "tire_low", state, now)
        return
    line = f"{LOW_PSI:.1f} psi"
    names = ", ".join(WHEEL_KO[w] for w, _v, _b in low)
    bits = [f"{names}이 {line}보다 낮아요"]
    msg = "공기압이 낮아요 · " + ", ".join(bits)
    data = {"recommended_bar": settings["recommended"],
            "reading_ts": row[0], "outside_temp": row[5],
            "low": [{"wheel": w, "bar": round(v, 3), "below_bar": round(b, 3)} for w, v, b in low]}
    _emit(conn, vid, "tire_low", state, msg, data, now, sender)


# ---------------------------------------------------------------- 3. vehicle alerts
def alert_dict() -> dict[str, list[str]]:
    global _DICT
    if _DICT is None:
        try:
            _DICT = json.loads(_DICT_PATH.read_text(encoding="utf-8"))["alerts"]
        except (OSError, ValueError, KeyError):
            _DICT = {}
    return _DICT


def alert_text(name: str, lang: str = "ko") -> str:
    """Customer-facing alert name. Korean by default. The code itself is not the sentence."""
    if lang != "en":
        from web.notice_view import VEHICLE_FALLBACK

        known = VEHICLE_FALLBACK.get(name)
        if known:
            return known[0]
    m = alert_dict().get(name)
    if m and m[0]:
        if lang == "en":
            return m[0]
        ko = ALERT_KO.get(m[0])
        return ko if ko else m[0]
    return name


def is_customer(name: str, audiences: Any) -> bool:
    if name in alert_dict():
        return True
    aud = audiences if isinstance(audiences, list) else []
    return any(str(a).lower() in ("customer", "1") for a in aud)


def check_vehicle_alerts(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    try:
        rows = conn.execute(
            "SELECT name, started_at, ended_at, audiences, notified_start, notified_end "
            "FROM vehicle_alerts WHERE vehicle_id = ? AND (notified_start = 0 OR "
            "(notified_start = 1 AND notified_end = 0 AND ended_at IS NOT NULL)) ORDER BY started_at",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for name, started, ended, aud_raw, n_start, n_end in rows:
        try:
            aud = json.loads(aud_raw) if aud_raw else []
        except ValueError:
            aud = []
        key = (vid, name, started)
        if not is_customer(name, aud):
            with conn:
                conn.execute("UPDATE vehicle_alerts SET notified_start = 2, notified_end = 2 "
                             "WHERE vehicle_id = ? AND name = ? AND started_at = ?", key)
            continue
        text = alert_text(name)
        if n_start == 0:
            st = parse_ts(started) if started else now
            if now - st > VALERT_FRESH:
                with conn:  # old (first enable / replay): store, do not announce
                    conn.execute("UPDATE vehicle_alerts SET notified_start = 2, notified_end = 2 "
                                 "WHERE vehicle_id = ? AND name = ? AND started_at = ?", key)
                continue
            if ended:
                with conn:  # started and cleared before we looked: one line is enough
                    conn.execute("UPDATE vehicle_alerts SET notified_end = 2 "
                                 "WHERE vehicle_id = ? AND name = ? AND started_at = ?", key)
            msg = f"차량 경고 · {text} ({_alerts()._kst(st)})"
            m = alert_dict().get(name)
            if m and m[1]:
                advice = ALERT_KO.get(m[1], m[1])
                if advice != text:
                    msg += f" · {advice}"
            msg += f"\n알림|{text}\n코드|{name}"
            if _emit(conn, vid, "vehicle_alert", f"{name}|{started}|start", msg,
                     {"name": name, "text": text, "started_at": started, "ended_at": ended,
                      "audiences": aud}, now, sender):
                with conn:
                    conn.execute("UPDATE vehicle_alerts SET notified_start = 1 "
                                 "WHERE vehicle_id = ? AND name = ? AND started_at = ?", key)
        elif n_start == 1 and n_end == 0 and ended:
            msg = f"차량 경고 해제 · {text} ({_alerts()._kst(parse_ts(ended))})"
            msg += f"\n알림|{text}\n코드|{name}"
            if _emit(conn, vid, "vehicle_alert", f"{name}|{started}|end", msg,
                     {"name": name, "text": text, "started_at": started, "ended_at": ended}, now, sender):
                with conn:
                    conn.execute("UPDATE vehicle_alerts SET notified_end = 1 "
                                 "WHERE vehicle_id = ? AND name = ? AND started_at = ?", key)


# ---------------------------------------------------------------- 4. HV faults
def hv_faults(ev: Any) -> list[str]:
    out = []
    for field, (bad, ko) in HV_FAULTS.items():
        v = ev.latest(field) if ev else None
        if isinstance(v, str) and v in bad:
            out.append(f"{field}:{v}")
    return out


def check_hv_fault(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    if not ev or all(ev.latest(f) is None for f in HV_FAULTS):
        return
    faults = hv_faults(ev)
    state = ",".join(sorted(faults)) if faults else "ok"
    prev = _get(conn, vid, "hv_fault")
    if prev == state or (prev is None and state == "ok"):
        if prev is None:
            _set(conn, vid, "hv_fault", state, now)
        return
    if not faults:
        _emit(conn, vid, "hv_fault", state, "고전압 계통 정상으로 돌아왔어요", {"faults": []}, now, sender)
        return
    names = [HV_FAULTS[f.split(":")[0]][1][f.split(":")[1]] for f in sorted(faults)]
    msg = "고전압 계통 이상 · 서비스 점검 필요 (" + ", ".join(names) + ")"
    _emit(conn, vid, "hv_fault", state, msg, {"faults": sorted(faults)}, now, sender)


# ---------------------------------------------------------------- 5. update scheduled
LA = ZoneInfo("America/Los_Angeles")


def scheduled_local(value: Any) -> datetime | None:
    """Tesla: timestamp fields report incorrectly; read as Pacific time they give the car's
    local wall clock. The car is in Korea, so that wall clock is KST."""
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, dict):
            value = value.get("seconds") or value.get("Seconds")
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
            v = float(value)
            if v > 1e11:
                v /= 1000.0
            if v <= 0:
                return None
            t = datetime.fromtimestamp(v, timezone.utc)
        else:
            t = parse_ts(str(value))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if t.year < 2020:
        return None
    return t.astimezone(LA).replace(tzinfo=KST)


def check_update_scheduled(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    if not ev:
        return
    at = scheduled_local(ev.latest("SoftwareUpdateScheduledStartTime"))
    ver = ev.latest("SoftwareUpdateVersion")
    ver = ver.strip() if isinstance(ver, str) else ""
    if at is None or at < now - timedelta(minutes=5):
        return
    state = f"{ver}|{at.strftime('%Y-%m-%dT%H:%M')}"
    if _get(conn, vid, "update_scheduled") == state:
        return
    mins = _as_float(ev.latest("SoftwareUpdateExpectedDurationMinutes"))
    when = at.strftime("%H:%M") if at.date() == now.astimezone(KST).date() else at.strftime("%m/%d %H:%M")
    msg = "소프트웨어 업데이트" + (f" {ver}" if ver else "") + f" · {when}에 설치 예약"
    if mins and mins > 0:
        msg += f", 약 {round(mins)}분"
    _emit(conn, vid, "update_scheduled", state, msg,
          {"version": ver or None, "scheduled_kst": at.strftime("%Y-%m-%d %H:%M"),
           "expected_minutes": mins, "available": ev.latest("SoftwareUpdateAvailable")}, now, sender)


# ---------------------------------------------------------------- 6. battery monthly report
def _series(conn, vid, name, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out = []
    for ts, raw in conn.execute(
        "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (vid, name, format_ts(start), format_ts(end)),
    ):
        v = _as_float(decode_value(raw))
        if v is not None:
            out.append((parse_ts(ts), v))
    return out


def cell_spreads_mv(vmax: list[tuple[datetime, float]], vmin: list[tuple[datetime, float]]) -> list[float]:
    """Spread at each max/min sample pair (carry the other forward up to 10 minutes)."""
    out = []
    j = 0
    for t, hi in vmax:
        while j + 1 < len(vmin) and vmin[j + 1][0] <= t:
            j += 1
        if vmin and vmin[j][0] <= t and t - vmin[j][0] <= timedelta(minutes=10):
            out.append(max(0.0, (hi - vmin[j][1]) * 1000))
    return out


def battery_report(conn, vid, month_start: date, month_end: date) -> dict[str, Any] | None:
    s = datetime(month_start.year, month_start.month, 1, tzinfo=KST)
    e = datetime(month_end.year, month_end.month, 1, tzinfo=KST)
    cap = _series(conn, vid, "NominalFullPackEnergyKwh", s, e)
    spreads = cell_spreads_mv(_series(conn, vid, "BrickVoltageMax", s, e), _series(conn, vid, "BrickVoltageMin", s, e))
    if not cap and not spreads:
        return None
    r: dict[str, Any] = {"month": month_start.strftime("%Y-%m")}
    if cap:
        r["capacity_kwh"] = round(cap[-1][1], 1)
        r["capacity_first_in_month_kwh"] = round(cap[0][1], 1)
    if spreads:
        r["cell_spread_mv_median"] = round(median(spreads))
        r["cell_spread_mv_max"] = round(max(spreads))
        r["cell_spread_samples"] = len(spreads)
    return r


def check_battery_month(conn: sqlite3.Connection, vid: str, ev: Any, now: datetime, sender) -> None:
    k = now.astimezone(KST)
    if k.day != 1 or k.hour < BATTERY_REPORT_HOUR:
        return
    this_month = date(k.year, k.month, 1)
    prev_month = (this_month - timedelta(days=1)).replace(day=1)
    label = prev_month.strftime("%Y-%m")
    if _get(conn, vid, "battery_month") == label:
        return
    r = battery_report(conn, vid, prev_month, this_month)
    if r is None:
        _set(conn, vid, "battery_month", label, now)  # nothing measured: skip quietly
        return
    # capacity history survives signal pruning: one alert_state row per month
    before = _get(conn, vid, f"battery_cap:{(prev_month - timedelta(days=1)).strftime('%Y-%m')}")
    first = conn.execute(
        "SELECT state FROM alert_state WHERE vehicle_id = ? AND kind LIKE 'battery_cap:%' ORDER BY kind LIMIT 1",
        (vid,),
    ).fetchone()
    parts = []
    if "capacity_kwh" in r:
        cap = r["capacity_kwh"]
        _set(conn, vid, f"battery_cap:{label}", f"{cap}", now)
        txt = f"용량 약 {cap:.1f} kWh"
        if before and _as_float(before) is not None:
            d = cap - float(before)
            txt += " (지난달과 같아요)" if abs(d) < 0.05 else f" (지난달보다 {abs(d):.1f} kWh {'줄었' if d < 0 else '늘었'}어요)"
            r["capacity_prev_month_kwh"] = float(before)
        if first and _as_float(first[0]) is not None and first[0] != f"{cap}" and before != first[0]:
            r["capacity_first_kwh"] = float(first[0])
            txt += f", 처음 기록 {float(first[0]):.1f} kWh"
        parts.append(txt)
    if "cell_spread_mv_median" in r:
        parts.append(f"셀 전압 차이 보통 약 {r['cell_spread_mv_median']} mV, 최대 약 {r['cell_spread_mv_max']} mV")
    msg = f"배터리 건강 월간 리포트 ({prev_month.month}월) · " + " · ".join(parts)
    _emit(conn, vid, "battery_month", label, msg, r, now, sender)


# checks that need no new signals (run from the idle loop too)
IDLE_CHECKS = (check_tire_low, check_consumables, check_tire_sets, check_vehicle_alerts, check_battery_month)
SIGNAL_CHECKS = (check_consumables, check_tire_sets, check_tire_leak, check_tire_low, check_vehicle_alerts, check_hv_fault,
                 check_update_scheduled, check_battery_month)
