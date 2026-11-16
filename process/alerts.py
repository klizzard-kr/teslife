"""State-change alerts -> 내차 Bot webhook.

The server only reads telemetry the awake car already sent; nothing here wakes the
car. Each alert kind remembers the last state it announced in `alert_state`, so a
state is sent once. The webhook URL and key come from the environment
(TESLIFE_ALERT_WEBHOOK_URL, TESLIFE_ALERT_WEBHOOK_KEY), which systemd loads from a
root-only file. Without them alerts are logged and skipped.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from process import (
    CHARGE_STATE_KO,
    MIN_TRIP_KM,
    MIN_TRIP_SECONDS,
    PARK_MERGE,
    charge_status,
    is_plugged,
    open_charge_start,
    KST,
    Events,
    WHEELS,
    _as_float,
    _latlon,
    _truthy,
    decode_value,
    format_ts,
    open_trip_start,
    parse_ts,
)

PSI_PER_BAR = 14.5038
TRIP_START_FRESH = timedelta(minutes=15)
TRIP_END_FRESH = timedelta(minutes=60)
TRIP_END_TIRE_WAIT = timedelta(minutes=2)  # wait this long for a TPMS reading after the trip
CHARGE_PORT_GRACE = timedelta(minutes=5)  # open charge port may mean a charge is about to start
PORT_PART = "충전구"
PORT_HOLD_KIND = "open_after_park_port"
CHARGE_START_FRESH = timedelta(minutes=15)
CHARGE_START_WAIT = timedelta(minutes=2)  # wait this long for power / limit / time-to-full
CHARGE_END_FRESH = timedelta(minutes=60)
WINDOWS = {"FdWindow": "운전석 창문", "FpWindow": "조수석 창문", "RdWindow": "뒷좌석 왼쪽 창문", "RpWindow": "뒷좌석 오른쪽 창문"}
TPMS = ("TpmsPressureFl", "TpmsPressureFr", "TpmsPressureRl", "TpmsPressureRr")
WHEEL_KO = {"fl": "앞 왼쪽", "fr": "앞 오른쪽", "rl": "뒤 왼쪽", "rr": "뒤 오른쪽"}

Sender = Callable[[dict[str, Any]], tuple[bool, str]]


def psi(bar: Any) -> str:
    v = _as_float(bar)
    return "" if v is None else f"{v * PSI_PER_BAR:.1f}"


def _tires_text(values: dict[str, Any] | None, rec: str = "") -> str:
    """'39.2/39.2/38.8/39.2 psi, 권장 42 psi'; missing wheels are left out, '' when none."""
    vals = [psi((values or {}).get(w)) for w in WHEELS]
    if not any(vals):
        return ""
    text = "/".join(v for v in vals if v) + " psi"
    return text + (f", 권장 {rec}" if rec else "")


def _rec(conn, vid) -> str:
    try:
        from ledger.tires import get_tire_settings, rec_text
        return rec_text(get_tire_settings(conn, vid))
    except Exception:  # noqa: BLE001 - settings must never block an alert
        return ""


def _kst(ts: str | datetime) -> str:
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def webhook_sender(payload: dict[str, Any]) -> tuple[bool, str]:
    url = os.environ.get("TESLIFE_ALERT_WEBHOOK_URL", "").strip()
    key = os.environ.get("TESLIFE_ALERT_WEBHOOK_KEY", "").strip()
    if not url or not key:
        return True, "skipped (no webhook configured)"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            code = res.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"error {type(exc).__name__}"
    return 200 <= code < 300, f"http {code}"


def _get_state(conn: sqlite3.Connection, vid: str, kind: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM alert_state WHERE vehicle_id = ? AND kind = ?", (vid, kind)
    ).fetchone()
    return row[0] if row else None


def _has_state(conn: sqlite3.Connection, vid: str, kind: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM alert_state WHERE vehicle_id = ? AND kind = ?", (vid, kind)
    ).fetchone() is not None


def _set_state(conn: sqlite3.Connection, vid: str, kind: str, state: str | None, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO alert_state (vehicle_id, kind, state, updated_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(vehicle_id, kind) DO UPDATE SET state = excluded.state, updated_ts = excluded.updated_ts",
            (vid, kind, state, format_ts(now)),
        )


def _emit(
    conn: sqlite3.Connection, vid: str, kind: str, state: str, message: str,
    data: dict[str, Any], now: datetime, sender: Sender,
    where: tuple[float, float] | None = None,
    event_ts: datetime | None = None,
) -> bool:
    """Inbox and app push run even when the webhook sender fails.

    The webhook state is still stored only after a successful send, so a failed
    webhook is retried. The inbox insert is idempotent, so the retry does not
    add a second row or a second push.
    """
    when = event_ts or now
    try:
        from web.notify import remember_alert
        remember_alert(conn, vid, kind, state, message, when)
    except Exception:
        pass
    payload = {"kind": kind, "ts": format_ts(when), "message": message, "data": data}
    try:
        ok, status = sender(payload)
    except Exception:
        ok, status = False, "error"
    with conn:
        conn.execute(
            "INSERT INTO alert_log (vehicle_id, ts, kind, status, message, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vid, format_ts(when), kind, status, message,
             where[0] if where else None, where[1] if where else None),
        )
    print(f"alert {kind}: {status}", flush=True)
    if ok:
        _set_state(conn, vid, kind, state, now)
    return ok


def _latest_tires(ev: Events) -> dict[str, Any] | None:
    vals = {w: ev.latest(n) for w, n in zip(WHEELS, TPMS)}
    return vals if any(v is not None for v in vals.values()) else None


def _range_km(ev: Events) -> float | None:
    est = _as_float(ev.latest("EstBatteryRange"))
    return est if est is not None else _as_float(ev.latest("RatedRange"))


def _trip_is_real(ev: Events, start: datetime, now: datetime) -> bool:
    """Same bar as the trip store (process.MIN_TRIP_KM / MIN_TRIP_SECONDS): a shift out of P
    counts once it has lasted a minute or moved 0.1 km. Shorter moves (a few metres in the
    car park, R/D shuffles while parking) are dropped at close, so they get no start alert
    either, otherwise a start would arrive with no matching end."""
    if now - start >= timedelta(seconds=MIN_TRIP_SECONDS):
        return True
    odo0 = _as_float(ev.at("Odometer", start))
    odo1 = _as_float(ev.latest("Odometer"))
    return odo0 is not None and odo1 is not None and odo1 - odo0 >= MIN_TRIP_KM


def check_trip_start(conn, vid, ev, now, sender) -> None:
    start = open_trip_start(ev, now)
    if start is None:
        return
    state = format_ts(start)
    if _get_state(conn, vid, "trip_start") == state:
        return
    if now - start > TRIP_START_FRESH:
        _set_state(conn, vid, "trip_start", state, now)
        return
    if not _trip_is_real(ev, start, now):
        return  # check again with the next signals (the car streams while in gear)
    soc = _as_float(ev.at("BatteryLevel", start + timedelta(seconds=5)))
    rng = _as_float(ev.at("EstBatteryRange", start + timedelta(seconds=5)))
    if rng is None:
        rng = _as_float(ev.at("RatedRange", start + timedelta(seconds=5)))
    tires = {w: ev.at(n, start + timedelta(seconds=5)) for w, n in zip(WHEELS, TPMS)}
    parts = ["주행 시작 " + _kst(start)]
    if soc is not None:
        parts.append(f"배터리 {soc:.0f}%")
    if rng is not None:
        parts.append(f"주행가능 {rng:.0f} km")
    tt = _tires_text(tires, _rec(conn, vid))
    if tt:
        parts.append("공기압 " + tt)
    from web.notice_view import format_soc
    from web.pressure import format_bar_number

    detail = []
    if soc is not None:
        detail.append(f"배터리|{format_soc(soc)}")
    if rng is not None and rng > 0:
        detail.append(f"주행가능|{rng:.0f} km")
    for wheel in WHEELS:
        bar = _as_float(tires.get(wheel))
        if bar is None or bar <= 0:
            continue
        detail.append(f"{WHEEL_KO[wheel]}|{format_bar_number(bar, 'psi')}")
    message = " · ".join(parts)
    if detail:
        message += "\n" + "\n".join(detail)
    data = {"start_ts": state, "battery_pct": soc, "range_km": rng,
            "tires_psi": {w: (round(_as_float(v) * PSI_PER_BAR, 1) if _as_float(v) is not None else None) for w, v in tires.items()}}
    _emit(conn, vid, "trip_start", state, message, data, now, sender, event_ts=start)


def _last_trip(conn: sqlite3.Connection, vid: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM trips WHERE vehicle_id = ? ORDER BY start_ts DESC LIMIT 1", (vid,)
        ).fetchone()
    finally:
        conn.row_factory = None


def trip_end_pending(conn: sqlite3.Connection, vid: str, now: datetime) -> bool:
    """True while the latest closed trip still needs its trip_end alert."""
    row = _last_trip(conn, vid)
    if row is None or _get_state(conn, vid, "trip_end") == row["start_ts"]:
        return False
    return now - parse_ts(row["end_ts"]) <= TRIP_END_FRESH


def _trip_end_tires(
    ev: Events, start: datetime, end: datetime, now: datetime
) -> tuple[dict[str, Any], str, datetime | None] | None:
    """Tires for the trip_end alert, or None to keep waiting.

    Prefer a reading newer than the trip end on all four wheels. Until
    TRIP_END_TIRE_WAIT has passed, wait for it; after that use the last values.
    """
    after: dict[str, tuple[datetime, Any]] = {}
    for w, n in zip(WHEELS, TPMS):
        newer = [(t, v) for t, v in ev.series(n, end) if t > end and _as_float(v) is not None]
        if newer:
            after[w] = newer[-1]
    if len(after) == len(WHEELS):
        return {w: after[w][1] for w in WHEELS}, "after", max(t for t, _v in after.values())
    if now - end < TRIP_END_TIRE_WAIT:
        return None
    tires: dict[str, Any] = {}
    stamps: list[datetime] = []
    for w, n in zip(WHEELS, TPMS):
        if w in after:
            tires[w] = after[w][1]
            stamps.append(after[w][0])
            continue
        hit = ev.at_ts(n, end)
        tires[w] = hit[1] if hit else None
        if hit:
            stamps.append(hit[0])
    if after:
        source = "partial"
    elif stamps and max(stamps) <= start + timedelta(minutes=2):
        source = "departure"
    else:
        source = "before_end"
    return tires, source, (max(stamps) if stamps else None)


def check_trip_end(conn, vid, ev, now, sender, tire_alert_bar: float = 2.6) -> None:
    row = _last_trip(conn, vid)
    if row is None:
        return
    state = row["start_ts"]
    if _get_state(conn, vid, "trip_end") == state:
        return
    end = parse_ts(row["end_ts"])
    if now - end > TRIP_END_FRESH:
        _set_state(conn, vid, "trip_end", state, now)
        return
    picked = _trip_end_tires(ev, parse_ts(row["start_ts"]), end, now)
    if picked is None:
        # deferred: nothing stored besides the trip itself, so a restart just re-checks
        if _get_state(conn, vid, "trip_end_pending") != state:
            _set_state(conn, vid, "trip_end_pending", state, now)
        return
    tires, source, measured = picked
    minutes = (row["duration_s"] or 0) / 60.0
    from web.notice_view import format_trip_distance
    distance = format_trip_distance(float(row["km"] or 0))
    parts = [f"주행 종료 {_kst(row['end_ts'])}", distance, f"{minutes:.0f}분"]
    try:
        estimated = row["kwh_estimated"]
    except (KeyError, IndexError):
        estimated = None
    approx = "약 " if estimated else ""
    if row["km_per_kwh"] is not None:
        parts.append(f"전비 {approx}{row['km_per_kwh']:.1f} km/kWh")
    if row["kwh"] is not None:
        parts.append(f"{approx}{row['kwh']:.2f} kWh")
    if row["start_soc"] is not None and row["end_soc"] is not None:
        parts.append(f"배터리 {row['start_soc']:.0f}→{row['end_soc']:.0f}%")
    hard = _truthy(ev.latest("TpmsHardWarnings")) or _truthy(ev.latest("TpmsSoftWarnings"))
    tire_status = "차 경고 있음" if hard else "정상"
    note = {"departure": ", 출발 시 측정값", "partial": ", 일부 출발 시 측정값",
            "before_end": f", {_kst(measured)} 측정값" if measured else ""}.get(source, "")
    tt = _tires_text(tires)
    rec = _rec(conn, vid)
    parts.append(f"공기압 {tire_status} ({tt}{note}" + (f", 권장 {rec}" if rec else "") + ")"
                 if tt else f"공기압 {tire_status}")
    data = {k: row[k] for k in row.keys() if k not in ("vehicle_id", "tire_before", "tire_after")}
    data["tire_status"] = tire_status
    data["tires_psi"] = {w: (round(_as_float(v) * PSI_PER_BAR, 1) if _as_float(v) is not None else None)
                         for w, v in tires.items()}
    data["tire_source"] = source
    data["tire_measured_ts"] = format_ts(measured) if measured else None
    regen = row["regen_kwh"] if "regen_kwh" in row.keys() else None
    accel = row["hard_accel"] if "hard_accel" in row.keys() else None
    brake = row["hard_brake"] if "hard_brake" in row.keys() else None
    short_bits = [distance]
    if regen is not None:
        short_bits.append(f"회생 {float(regen):.1f}kWh")
    if accel is not None:
        short_bits.append(f"급가속 {int(accel)}")
    if brake is not None:
        short_bits.append(f"급제동 {int(brake)}")
    from web.notice_view import format_range_km, format_soc
    from web.pressure import format_bar_number

    detail = [f"거리|{distance}"]
    if row["duration_s"]:
        whole = int(round(float(row["duration_s"]) / 60.0))
        hours, mins = divmod(whole, 60)
        detail.append("시간|" + (f"{hours}시간 {mins}분" if hours else f"{mins}분"))
    if row["kwh"] is not None:
        detail.append(f"쓴 전력|{approx}{float(row['kwh']):.2f} kWh")
    if row["km_per_kwh"] is not None:
        detail.append(f"전비|{approx}{float(row['km_per_kwh']):.1f} km/kWh")
    soc = _as_float(ev.at("BatteryLevel", end))
    if soc is not None:
        detail.append(f"배터리|{format_soc(soc)}")
        data["battery_pct"] = round(float(soc), 1)
    estimated_range = _as_float(ev.at("EstBatteryRange", end))
    rated = _as_float(ev.at("RatedRange", end))
    distance = estimated_range if estimated_range is not None and estimated_range > 0 else rated
    if distance is not None and distance > 0:
        detail.append(f"주행가능|{format_range_km(distance)}")
        data["range_km"] = int(float(distance) + 0.5)
    if regen is not None:
        detail.append(f"회생|{float(regen):.1f} kWh")
    if accel is not None:
        detail.append(f"급가속|{int(accel)}회")
    if brake is not None:
        detail.append(f"급제동|{int(brake)}회")
    for wheel in WHEELS:
        bar = _as_float(tires.get(wheel))
        if bar is None or bar <= 0:
            continue
        detail.append(f"{WHEEL_KO[wheel]}|{format_bar_number(bar, 'psi')}")
    message = ", ".join(short_bits) + "\n" + " · ".join(parts)
    if detail:
        message += "\n" + "\n".join(detail)
    _emit(conn, vid, "trip_end", state, message, data, now, sender, event_ts=end)


def _charge_kind(ev: Events, start: datetime, at: datetime) -> str:
    """Same kind as the stored charge: a clear fast signal is DC before AC energy."""
    from process import charge_energy

    return charge_energy(ev, start, at)["kind"]


def _park_session_id(ev: Events) -> str | None:
    """When this park started. A later drive starts a new session."""
    if ev.latest("Gear") != "P":
        return None
    started = None
    prev = None
    for ts, value in ev.series("Gear"):
        if value == "P" and prev != "P":
            started = ts
        prev = value
    if started is None:
        return None
    return format_ts(started)


def check_port_open_rates(conn, vid, ev, now, sender, opener=None) -> None:
    """Tell the bot the nearby public prices. No app notice and no app push.

    One send per parking session. The open-port warning still waits five minutes.
    `sender` is the app webhook and is not used.
    """
    del sender
    if ev.latest("Gear") != "P" or ev.latest("ChargePortDoorOpen") is not True:
        return
    session = _park_session_id(ev)
    if not session:
        return
    state = _get_state(conn, vid, "port_open_rates") or ""
    if state.endswith(":" + session):
        return
    from process.evcharger import _meta_get, _meta_set
    from web.bot_notify import _url_ok, publish_direct, webhook_url

    waiting = state == "wait:" + session
    rates = None
    if waiting:
        try:
            rates = json.loads(_meta_get(conn, f"port_rates:{vid}") or "")
        except (TypeError, ValueError):
            rates = None
    if not isinstance(rates, list):
        where = parked_location(conn, vid, ev)
        if where is None:
            _set_state(conn, vid, "port_open_rates", "empty:" + session, now)
            return
        try:
            from process.evcharger import FAR_M, _nearby, snapshot_port_status
            from web.tariff import ensure_schema, nearby_operator_rates

            snapshot_port_status(conn, vid, where[0], where[1], now)
            chargers = _nearby(conn, where[0], where[1], now, FAR_M)
            ensure_schema(conn)
            rates = nearby_operator_rates(conn, chargers, now.astimezone(KST).date().isoformat())
        except Exception:
            return
        if not rates:
            _set_state(conn, vid, "port_open_rates", "empty:" + session, now)
            return
        _meta_set(conn, f"port_rates:{vid}", json.dumps(rates, ensure_ascii=False))
        _set_state(conn, vid, "port_open_rates", "wait:" + session, now)
    if not _url_ok(webhook_url()):
        _set_state(conn, vid, "port_open_rates", "nobot:" + session, now)
        return
    ok = publish_direct(
        "port_open_rates",
        f"port_open_rates:{session}",
        format_ts(now),
        {"rates": rates},
        opener,
    )
    if ok:
        _set_state(conn, vid, "port_open_rates", "sent:" + session, now)


def check_charge_start(conn, vid, ev, now, sender) -> None:
    if ev.latest("ChargePortDoorOpen") is False and _get_state(conn, vid, "charge_start_session"):
        _set_state(conn, vid, "charge_start_session", "", now)
    start = open_charge_start(ev)
    if start is None:
        return
    state = format_ts(start)
    session = _park_session_id(ev)
    if session and _get_state(conn, vid, "charge_start_session") == session:
        _set_state(conn, vid, "charge_start", state, now)
        return
    if _get_state(conn, vid, "charge_start") == state:
        return
    if now - start > CHARGE_START_FRESH:
        _set_state(conn, vid, "charge_start", state, now)
        if session:
            _set_state(conn, vid, "charge_start_session", session, now)
        return
    at = ev.last_ts or start
    kind = _charge_kind(ev, start, at)
    kw = _as_float(ev.at("DCChargingPower" if kind == "DC" else "ACChargingPower", at))
    if (kw is None or kw <= 0.5) and now - start < CHARGE_START_WAIT:
        return  # power still ramping; check again next round
    soc = _as_float(ev.at("BatteryLevel", at))
    limit = _as_float(ev.at("ChargeLimitSoc", at))
    ttf = _as_float(ev.at("TimeToFullCharge", at))
    parts = [f"충전 시작 {_kst(start)}", "교류(AC)" if kind == "AC" else "직류(DC)"]
    if kw is not None and kw > 0:
        parts.append(f"{kw:.1f} kW")
    if soc is not None:
        parts.append(f"배터리 {soc:.0f}%" + (f" → 목표 {limit:.0f}%" if limit is not None else ""))
    if ttf is not None and ttf > 0:
        h, m = divmod(round(ttf * 60), 60)
        parts.append("완충까지 약 " + (f"{h}시간 {m}분" if h else f"{m}분"))
    data = {"start_ts": state, "kind": kind, "power_kw": kw, "battery_pct": soc,
            "limit_pct": limit, "time_to_full_h": ttf}
    message = " · ".join(parts)
    detail = [f"전류|{'교류(AC)' if kind == 'AC' else '직류(DC)'}"]
    if soc is not None:
        detail.insert(0, f"배터리|{soc:.0f}%")
    if kw is not None and kw > 0:
        detail.append(f"충전 전력|{kw:.1f} kW")
    if detail:
        message += "\n" + "\n".join(detail)
    sent = _emit(conn, vid, "charge_start", state, message, data, now, sender, event_ts=start)
    if sent and session:
        _set_state(conn, vid, "charge_start_session", session, now)


def _last_charge(conn: sqlite3.Connection, vid: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM charges WHERE vehicle_id = ? ORDER BY start_ts DESC LIMIT 1", (vid,)
        ).fetchone()
    finally:
        conn.row_factory = None


def check_charge_end(conn, vid, ev, now, sender) -> None:
    row = _last_charge(conn, vid)
    if row is None:
        return
    state = row["start_ts"]
    if _get_state(conn, vid, "charge_end") == state:
        return
    end = parse_ts(row["end_ts"])
    if now - end > CHARGE_END_FRESH:
        _set_state(conn, vid, "charge_end", state, now)
        return
    status = charge_status(ev.at("DetailedChargeState", end), ev.at("ChargeState", end))
    label = CHARGE_STATE_KO.get(status or "")
    hours = (end - parse_ts(row["start_ts"])).total_seconds() / 3600.0
    kwh = row["kwh_added"]
    # Same five words as the app. The headline is that word plus the time.
    if label:
        head = f"{label} {_kst(end)}"
    else:
        head = f"충전 종료 {_kst(end)}"
    parts = [head, "교류(AC)" if row["kind"] == "AC" else "직류(DC)"]
    if kwh is not None:
        parts.append(f"{kwh:.1f} kWh")
    if row["start_soc"] is not None and row["end_soc"] is not None:
        parts.append(f"배터리 {row['start_soc']:.0f}→{row['end_soc']:.0f}%")
    h, m = divmod(round(hours * 60), 60)
    parts.append(f"{h}시간 {m}분" if h else f"{m}분")
    avg = (kwh / hours) if kwh and hours > 0 else None
    if avg is not None:
        parts.append(f"평균 {avg:.1f} kW")
    if row["loss_kwh"] is not None and row["loss_pct"] is not None:
        # charger-side minus battery-side: conversion plus whatever the car ran meanwhile
        parts.append(f"충전 중 차가 쓴 양 {row['loss_kwh']:.1f} kWh ({row['loss_pct']:.1f}%)")
    data = {k: row[k] for k in row.keys() if k != "vehicle_id"}
    data.update({"reason": status, "duration_h": round(hours, 3),
                 "avg_kw": round(avg, 2) if avg is not None else None})
    soc_bit = ""
    if row["start_soc"] is not None and row["end_soc"] is not None:
        soc_bit = f"{float(row['start_soc']):.0f}% → {float(row['end_soc']):.0f}%"
    kwh_bit = f"{float(kwh):.1f}kWh" if kwh is not None else ""
    short = ", ".join(bit for bit in (soc_bit, kwh_bit) if bit) or head
    rows = []
    if kwh is not None:
        rows.append(f"충전량|{float(kwh):.1f} kWh")
    rows.append("시간|" + (f"{h}시간 {m}분" if h else f"{m}분"))
    rows.append("전류|" + ("교류(AC)" if row["kind"] == "AC" else "직류(DC)"))
    battery = row["battery_kwh"] if "battery_kwh" in row.keys() else None
    if battery is not None:
        rows.append(f"배터리 저장량|{float(battery):.1f} kWh")
    if row["loss_kwh"] is not None:
        rows.append(f"충전 중 사용량|{float(row['loss_kwh']):.1f} kWh")
    if avg is not None:
        rows.append(f"평균 충전 전력|{avg:.1f} kW")
    if label:
        rows.append(f"종료 사유|{label}")
    message = short
    if rows:
        message += "\n" + "\n".join(rows)
    message += "\n" + " · ".join(parts)
    _emit(conn, vid, "charge_end", state, message, data, now, sender, event_ts=end)


# Korean law: a car may not occupy an AC (완속) charging spot for more than 14 hours.
PARK_LIMIT = timedelta(hours=14)
PARK_LEAD = timedelta(minutes=5)       # parking starts ~5 min before the charge starts (user's rule)
PARK_WARN_1H = timedelta(hours=1)
PARK_WARN_30M = timedelta(minutes=30)
PARK_OVER_GRACE = timedelta(hours=3)   # after this past the deadline, stay silent (old sessions)
PARK_PREFIX = "완속 충전 자리 14시간 제한"


def _park_session(conn: sqlite3.Connection, vid: str, ev: Events | None) -> tuple[datetime, str] | None:
    """The latest charge session (start, AC/DC): the one in progress, else the last stored one.

    A session in progress is remembered in alert_state ("park14h_session") so the check keeps
    working while the car sleeps and no new signals (no Events) arrive.
    """
    remembered = None
    if ev is not None:
        start = open_charge_start(ev)
        if start is not None:
            kind = _charge_kind(ev, start, ev.last_ts or start)
            state = f"{format_ts(start)}|{kind}"
            if _get_state(conn, vid, "park14h_session") != state:
                _set_state(conn, vid, "park14h_session", state, ev.last_ts or start)
            remembered = (start, kind)
    if remembered is None:
        raw = _get_state(conn, vid, "park14h_session")
        if raw and "|" in raw:
            ts, kind = raw.split("|", 1)
            remembered = (parse_ts(ts), kind)
    row = _last_charge(conn, vid)
    stored = (parse_ts(row["start_ts"]), row["kind"] or "AC") if row is not None else None
    cands = [c for c in (remembered, stored) if c is not None]
    if not cands:
        return None
    # the stored row wins for the same start (its kind is final)
    return max(cands, key=lambda c: (c[0], c is stored))


def moved_since(conn: sqlite3.Connection, vid: str, since: datetime) -> bool:
    """True once the car left P after `since` (any D/R/N, even briefly) or a trip started."""
    lo = format_ts(since)
    try:
        if conn.execute("SELECT 1 FROM trips WHERE vehicle_id = ? AND start_ts > ? LIMIT 1", (vid, lo)).fetchone():
            return True
        for (raw,) in conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Gear' AND ts > ?", (vid, lo)
        ):
            try:
                if decode_value(raw) in ("D", "R", "N"):
                    return True
            except ValueError:
                continue
    except sqlite3.OperationalError:
        return False
    return False


def _hm(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%H:%M")


# Per charge type: (limit, first warning, second warning, passed-notice grace, alert kinds, prefix)
PARK_RULES = {
    "AC": {"limit": PARK_LIMIT, "warn": (PARK_WARN_1H, PARK_WARN_30M), "grace": PARK_OVER_GRACE,
           "kinds": ("park14h_1h", "park14h_30m", "park14h_over"), "prefix": PARK_PREFIX,
           "left": ("1시간", "30분"), "span": "14시간"},
    # DC (급속) spots allow 1 hour of parking
    "DC": {"limit": timedelta(hours=1), "warn": (timedelta(minutes=15), timedelta(minutes=5)),
           "grace": timedelta(minutes=30),
           "kinds": ("parkdc_15m", "parkdc_5m", "parkdc_over"), "prefix": "급속 충전 자리 1시간 제한",
           "left": ("15분", "5분"), "span": "1시간"},
}


def park_deadline(start: datetime, kind: str = "AC") -> datetime:
    return start - PARK_LEAD + PARK_RULES[kind]["limit"]


def park_texts(deadline: datetime, kind: str = "AC") -> dict[str, str]:
    r = PARK_RULES[kind]
    hm = _hm(deadline)
    first, second, over = r["kinds"]
    return {
        first: f"{r['prefix']}: {hm}까지 차를 옮겨 주세요. {r['left'][0]} 남았어요",
        second: f"{r['prefix']}: {hm}까지 차를 옮겨 주세요. {r['left'][1]} 남았어요",
        over: f"{r['prefix']}: {hm}에 {r['span']}이 지났어요. 얼른 차를 옮겨 주세요",
    }


def park14h_texts(deadline: datetime) -> dict[str, str]:
    return park_texts(deadline, "AC")


def check_park14h(conn, vid, ev, now, sender) -> None:
    """Warn before the parking limit of the charging spot; stop once the car moves.

    AC (완속): 14 h, warnings 1 h and 30 min before. DC (급속): 1 h, warnings 15 and 5 min
    before. Parking start = charge start - 5 min. Charging ending does not cancel (the car
    still holds the spot); leaving P does. Each warning is sent once per session (alert_state
    keeps the session start per kind), so restarts do not repeat it. Reads only stored telemetry.
    """
    sess = _park_session(conn, vid, ev)
    if sess is None:
        return
    start, kind = sess
    rule = PARK_RULES.get(kind)
    if rule is None:
        return
    deadline = park_deadline(start, kind)
    w1, w2 = rule["warn"]
    first, second, over = rule["kinds"]
    if now < deadline - w1:
        return
    state = format_ts(start)
    sent = {k: _get_state(conn, vid, k) == state for k in rule["kinds"]}
    if now >= deadline - w2:
        due = second if now < deadline else over
    else:
        due = first
    if sent[due] or (due == over and (sent[first] or sent[second])):
        return
    if now >= deadline + rule["grace"]:
        return  # long past: an old session, nothing useful to say
    if moved_since(conn, vid, start):
        return
    texts = park_texts(deadline, kind)
    data = {"charge_start_ts": state, "charge_kind": kind, "parking_start_ts": format_ts(start - PARK_LEAD),
            "deadline_ts": format_ts(deadline), "deadline_kst": _hm(deadline), "warning": due}
    if _emit(conn, vid, due, state, texts[due], data, now, sender) and due == second and not sent[first]:
        _set_state(conn, vid, first, state, now)  # the first one is moot now


def ac_park14h_status(conn, vid: str, now: datetime | None = None, ev: Events | None = None) -> dict[str, Any]:
    """Whether an AC charging-spot 14 h countdown is still running, and its deadline.

    Active means the latest session is AC, the car has not left P since it started, and
    now is before the deadline. Reads only: the 1 h / 30 min alerts stay in check_park14h.
    No position is included.
    """
    now = now or datetime.now(timezone.utc)
    empty: dict[str, Any] = {"active": False}
    remembered = None
    if ev is not None:
        start = open_charge_start(ev)
        if start is not None:
            remembered = (start, _charge_kind(ev, start, ev.last_ts or start))
    if remembered is None:
        raw = _get_state(conn, vid, "park14h_session")
        if raw and "|" in raw:
            ts, kind = raw.split("|", 1)
            remembered = (parse_ts(ts), kind)
    row = _last_charge(conn, vid)
    stored = (parse_ts(row["start_ts"]), row["kind"] or "AC") if row is not None else None
    cands = [c for c in (remembered, stored) if c is not None]
    if not cands:
        return empty
    start, kind = max(cands, key=lambda c: (c[0], c is stored))
    if kind != "AC" or moved_since(conn, vid, start):
        return empty
    deadline = park_deadline(start, "AC")
    if now >= deadline + PARK_OVER_GRACE:
        return empty
    return {
        "active": now < deadline,
        "kind": "AC",
        "charge_start_ts": format_ts(start),
        "parking_start_ts": format_ts(start - PARK_LEAD),
        "deadline_ts": format_ts(deadline),
        "deadline_kst": deadline.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
    }


def _version_text(value: Any) -> str:
    """A blank or whitespace-only software version is no version."""
    if value is None:
        return ""
    return str(value).strip()


def _update_unavailable(value: Any) -> bool:
    if value is False or value == 0:
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0"):
        return True
    return False


def install_in_progress(download: float | None, install: float | None, available: Any) -> bool:
    """Ignore a stray install percent when nothing is downloading or no update is offered."""
    if install is None or install <= 0:
        return False
    if download == 0:
        return False
    if _update_unavailable(available):
        return False
    return True


def check_software_update(conn, vid, ev, now, sender) -> None:
    ver = _version_text(ev.latest("SoftwareUpdateVersion"))
    cur = _version_text(ev.latest("Version"))
    if not ver or ver == cur:
        if _get_state(conn, vid, "software_update") not in (None, "none"):
            _set_state(conn, vid, "software_update", "none", now)
        return
    dl = _as_float(ev.latest("SoftwareUpdateDownloadPercentComplete"))
    inst = _as_float(ev.latest("SoftwareUpdateInstallationPercentComplete"))
    available = ev.latest("SoftwareUpdateAvailable")
    if install_in_progress(dl, inst, available):
        phase, text = "installing", f"설치 중 {inst:.0f}%"
    elif dl is not None and dl >= 100:
        phase, text = "downloaded", "다운로드 완료, 설치 대기"
    else:
        phase, text = "available", "새 업데이트" + (f" (다운로드 {dl:.0f}%)" if dl else "")
    prev = _get_state(conn, vid, "software_update") or ""
    if prev == ver or prev.startswith(ver + "|"):
        if prev != ver:
            _set_state(conn, vid, "software_update", ver, now)
        return
    # Once per new version. Phase changes of the same version do not send again.
    body = f"{cur} → {ver}" if cur else ver
    _emit(conn, vid, "software_update", ver, body,
          {"version": ver, "current": cur, "phase": phase, "download_pct": dl, "install_pct": inst}, now, sender)


def _latch_engaged(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and "Disengaged" not in text and text.endswith("Engaged")


def _cable_value(value: Any) -> bool:
    """True for a stored cable signal that names a connected cable."""
    if value is True:
        return True
    if value is None or value is False:
        return False
    text = str(value).strip()
    if not text:
        return False
    low = text.lower()
    if low in {"none", "unknown", "disconnected", "sna", "invalid", "0", "false"}:
        return False
    if low.endswith("none") or low.endswith("unknown") or "disconnected" in low:
        return False
    return True


def _connected_at(ev: Events, ts: datetime | None) -> bool:
    """Cable or charge at one moment. A known Disconnected state wins over ChargeState."""
    detailed = ev.at("DetailedChargeState", ts) if ts is not None else ev.latest("DetailedChargeState")
    raw = ev.at("ChargeState", ts) if ts is not None else ev.latest("ChargeState")
    if isinstance(detailed, str) and detailed not in ("", "Unknown"):
        if detailed != "Disconnected":
            return True
    elif raw in ("Charging", "Enable"):
        return True
    latch = ev.at("ChargePortLatch", ts) if ts is not None else ev.latest("ChargePortLatch")
    if _latch_engaged(latch):
        return True
    for name in ("ChargeCable", "ChargingCableType"):
        value = ev.at(name, ts) if ts is not None else ev.latest(name)
        if _cable_value(value):
            return True
    return False


def port_cable_connected(ev: Events, since: datetime | None = None) -> bool:
    """True when stored signals show a cable or a charge now, or since `since`."""
    if _connected_at(ev, None):
        return True
    if since is None:
        return False
    stamps: set[datetime] = set()
    for name in ("DetailedChargeState", "ChargeState", "ChargePortLatch", "ChargeCable", "ChargingCableType"):
        for ts, _value in ev.series(name, since):
            stamps.add(ts)
    return any(_connected_at(ev, ts) for ts in stamps)


def _without_port(state: str) -> str:
    parts = [part for part in (state or "").split(",") if part and part != PORT_PART]
    return ",".join(parts) if parts else "closed"


def _open_message(parts: list[str]) -> str:
    from web.notice_view import _iga

    if len(parts) == 1:
        tail = parts[0] + _iga(parts[0])
    else:
        tail = ", ".join(parts[:-1] + [parts[-1] + _iga(parts[-1])])
    msg = f"주차 후 열림 · {tail} 열려 있어요"
    return msg + "\n" + "\n".join(f"열린 곳|{part}" for part in parts)


def _announce_others(conn, vid, others: list[str], now, sender) -> None:
    """Windows and other non-port parts. A stale charge-port token is only stripped."""
    prev = _get_state(conn, vid, "open_after_park") or ""
    target = ",".join(sorted(others)) if others else "closed"
    if _without_port(prev) == target and PORT_PART not in prev.split(","):
        return
    if not others:
        if prev != target:
            _set_state(conn, vid, "open_after_park", target, now)
        return
    if prev == target:
        return
    _emit(conn, vid, "open_after_park", target, _open_message(others), {"open": others}, now, sender)


def _end_port_hold(conn, vid, now) -> None:
    """Drop a pending or finished hold. Stored notices are not deleted."""
    cur = _get_state(conn, vid, PORT_HOLD_KIND)
    if cur and cur != "clear":
        _set_state(conn, vid, PORT_HOLD_KIND, "clear", now)


def charge_port_hold_due(conn: sqlite3.Connection, vid: str, now: datetime) -> bool:
    """True when a charge-port hold has waited out CHARGE_PORT_GRACE."""
    state = _get_state(conn, vid, PORT_HOLD_KIND) or ""
    if not state.startswith("hold:"):
        return False
    try:
        started = parse_ts(state.split(":", 1)[1])
    except ValueError:
        return True
    return now - started >= CHARGE_PORT_GRACE


def _open_parts(ev: Events) -> list[str] | None:
    if ev.latest("Gear") not in ("P", None):
        return None
    if ev.latest("Locked") is not True:
        return None
    # Camping (ClimateKeeper Party) often leaves a window open on purpose. Pet mode (Dog) still alerts.
    camping = str(ev.latest("ClimateKeeperMode") or "") == "Party"
    opened: list[str] = []
    if not camping:
        for name, ko in WINDOWS.items():
            v = ev.latest(name)
            if isinstance(v, str) and v in ("PartiallyOpen", "Opened", "Open"):
                opened.append(ko)
    port = ev.latest("ChargePortDoorOpen")
    plugged = is_plugged(ev.latest("DetailedChargeState"), ev.latest("ChargeState"))
    if port is True and not plugged:
        opened.append("충전구")
    return opened


def check_open_after_park(conn, vid, ev, now, sender) -> None:
    """Open windows now. An open charge port waits CHARGE_PORT_GRACE.

    A cable or a charge during that wait cancels the port notice. Nothing here
    deletes or rewrites a notice that was already stored.
    """
    opened = _open_parts(ev)
    if opened is None:
        _end_port_hold(conn, vid, now)
        return
    others = [part for part in opened if part != PORT_PART]
    port_open = PORT_PART in opened and not port_cable_connected(ev)
    prev = _get_state(conn, vid, "open_after_park") or ""
    already = PORT_PART in [part for part in prev.split(",") if part]
    _announce_others(conn, vid, others, now, sender)
    if not port_open:
        _end_port_hold(conn, vid, now)
        return
    hold = _get_state(conn, vid, PORT_HOLD_KIND) or ""
    if hold == "sent" or (already and not hold.startswith("hold:")):
        if hold != "sent":
            _set_state(conn, vid, PORT_HOLD_KIND, "sent", now)
        return
    if not hold.startswith("hold:"):
        _set_state(conn, vid, PORT_HOLD_KIND, "hold:" + format_ts(now), now)
        return
    try:
        started = parse_ts(hold.split(":", 1)[1])
    except ValueError:
        _set_state(conn, vid, PORT_HOLD_KIND, "hold:" + format_ts(now), now)
        return
    if port_cable_connected(ev, started):
        _end_port_hold(conn, vid, now)
        return
    if now - started < CHARGE_PORT_GRACE:
        return
    ok = _emit(
        conn, vid, "open_after_park", PORT_PART, _open_message([PORT_PART]),
        {"open": [PORT_PART]}, now, sender,
    )
    if ok:
        _set_state(conn, vid, PORT_HOLD_KIND, "sent", now)
        target = ",".join(sorted(others)) if others else "closed"
        _set_state(conn, vid, "open_after_park", target, now)


def parked_location(conn: sqlite3.Connection, vid: str, ev: Events) -> tuple[float, float] | None:
    """Where the car is standing: the last position it reported (kept from the end of the last trip)."""
    ll = _latlon(ev.latest("Location"))
    if ll:
        return ll
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Location' ORDER BY ts DESC LIMIT 1", (vid,)
        ).fetchone()
        if row:
            ll = _latlon(decode_value(row[0]))
            if ll:
                return ll
        row = conn.execute(
            "SELECT end_lat, end_lon FROM trips WHERE vehicle_id = ? AND end_lat IS NOT NULL ORDER BY start_ts DESC LIMIT 1",
            (vid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0], row[1]) if row else None


def check_sentry(conn, vid, ev, now, sender) -> None:
    v = ev.latest("SentryMode")
    if v is None:
        return
    on = "on" if str(v) not in ("Off", "Unknown") else "off"
    if not _has_state(conn, vid, "sentry"):
        _set_state(conn, vid, "sentry", on, now)  # first observation: remember silently
        return
    prev = _get_state(conn, vid, "sentry") or ""
    if prev == on or (on == "off" and prev.startswith("off")):
        return
    seen = ev.series("SentryMode")
    at = seen[-1][0] if seen else now
    if on == "off" and str(ev.at("Gear", at) or "P") != "P":
        # the car disarms 감시 by itself when shifted out of P: not news, remember quietly
        _set_state(conn, vid, "sentry", "off_drive:" + format_ts(at), now)
        return
    if on == "on" and prev.startswith("off_drive:"):
        since = prev.split(":", 1)[1]
        since_dt = parse_ts(since)
        row = conn.execute(
            "SELECT start_ts FROM trips WHERE vehicle_id = ? AND end_ts >= ? ORDER BY end_ts DESC LIMIT 1",
            (vid, since),
        ).fetchone()
        # Quiet when nothing completed after the disarm, and when the only trip was already
        # underway (a brief stop inside the same drive). A trip that started with the disarm
        # is a real departure, so re-arming at the new place is still announced.
        if row is None or parse_ts(row[0]) < since_dt - PARK_MERGE:
            _set_state(conn, vid, "sentry", on, now)
            return
    msg = "감시 모드 " + ("켜짐" if on == "on" else "꺼짐")
    where = parked_location(conn, vid, ev)
    data: dict[str, Any] = {"sentry": on, "raw": v}
    if where:
        from process.geocode import place_label

        label = place_label(conn, vid, where[0], where[1])
        if label:
            msg += f" · 근처 {label}"
        data["location"] = {"lat": where[0], "lon": where[1], "label": label, "approximate": True}
    _emit(conn, vid, "sentry", on, msg, data, now, sender, where=where)


# ------------------------------------------------------------- 감시 경보 / 주의
# SentryMode is already streamed (interval 5 s). Panic (proto SentryModeStatePanic, stored
# "Panic") is a 감시 경보. Aware is the same kind of notice, grouped the same way, labeled
# 주의. Each stage groups on its own for ALARM_MERGE. Records stay in `sentry_alarms`.
ALARM_VALUE = "Panic"
ALARM_STAGES = {"Panic": "감시 경보", "Aware": "주의"}
ALARM_MERGE = timedelta(minutes=10)
ALARM_FRESH = timedelta(minutes=15)   # older alarms (server was down) are recorded, not sent
ALARM_MAX_ATTEMPTS = 5


def _sentry_value(v: Any) -> str:
    text = "" if v is None else str(v)
    return text[len("SentryModeState"):] if text.startswith("SentryModeState") else text


def address_from_nominatim(data: dict[str, Any]) -> str | None:
    """'예시시 가나다구 샘플동 테스트로' from a Nominatim jsonv2 reply (city, district, dong, road)."""
    addr = data.get("address") or {}
    parts: list[str] = []
    for keys in (("city", "county", "town"), ("borough", "city_district"),
                 ("quarter", "suburb", "neighbourhood", "village"), ("road",)):
        for k in keys:
            v = addr.get(k)
            if isinstance(v, str) and v.strip() and v.strip() not in parts:
                parts.append(v.strip())
                break
    return " ".join(parts) or None


def _registered_place(conn, vid: str, lat: float, lon: float) -> str | None:
    from process.geo import distance_m
    try:
        rows = conn.execute("SELECT name, lat, lon, radius_m FROM places WHERE vehicle_id = ?", (vid,)).fetchall()
    except sqlite3.OperationalError:
        return None
    best = None
    for name, plat, plon, radius in rows:
        d = distance_m((lat, lon), (plat, plon))
        if d <= (radius or 0) and (best is None or d < best[0]):
            best = (d, name)
    return best[1] if best else None


def _address(conn, lat: float, lon: float, opener: Callable[..., Any] | None = None) -> str | None:
    """Address-level name: cached, else one Nominatim lookup when TESLIFE_GEOCODER=nominatim."""
    from process.geocode import USER_AGENT, cache_key
    key = cache_key(lat, lon)
    try:
        row = conn.execute("SELECT label FROM geocode_cache WHERE provider = 'nominatim-addr' AND key = ?",
                           (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0]:
        return row[0]
    label = None
    if os.environ.get("TESLIFE_GEOCODER", "").strip().lower() == "nominatim" or opener is not None:
        import urllib.parse
        q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                    "zoom": 17, "addressdetails": 1, "accept-language": "ko"})
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{q}",
                                     headers={"User-Agent": USER_AGENT})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
            label = address_from_nominatim(data) if isinstance(data, dict) else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            label = None
        if label:
            with conn:
                conn.execute("INSERT OR REPLACE INTO geocode_cache (provider, key, label, fetched_at) VALUES (?, ?, ?, ?)",
                             ("nominatim-addr", key, label, format_ts(datetime.now(timezone.utc))))
    if label:
        return label
    try:
        row = conn.execute("SELECT label FROM geocode_cache WHERE key = ? AND label IS NOT NULL "
                           "ORDER BY fetched_at DESC LIMIT 1", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    return row[0] if row else None


def alarm_place(conn, vid: str, lat: float, lon: float, opener: Callable[..., Any] | None = None) -> str | None:
    """'집, 예시시 가나다구 샘플동 테스트로', or just the address / registered name."""
    name = _registered_place(conn, vid, lat, lon)
    addr = _address(conn, lat, lon, opener)
    if name and addr:
        return f"{name}, {addr}"
    return name or addr


def _alarm_open(conn, vid: str, ev: Events, t: datetime, opener=None, stage: str = ALARM_VALUE) -> None:
    row = conn.execute(
        "SELECT start_ts, last_ts, end_ts FROM sentry_alarms WHERE vehicle_id = ? "
        "AND COALESCE(stage, 'Panic') = ? ORDER BY start_ts DESC LIMIT 1", (vid, stage)
    ).fetchone()
    if row:
        ref = parse_ts(row[2]) if row[2] and row[2] > row[1] else parse_ts(row[1])
        if t - ref <= ALARM_MERGE:
            with conn:
                conn.execute("UPDATE sentry_alarms SET count = count + 1, last_ts = ?, end_ts = NULL "
                             "WHERE vehicle_id = ? AND start_ts = ?", (format_ts(t), vid, row[0]))
            return
    where = parked_location(conn, vid, ev)
    place = alarm_place(conn, vid, where[0], where[1], opener) if where else None
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO sentry_alarms (vehicle_id, start_ts, last_ts, lat, lon, place, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vid, format_ts(t), format_ts(t), where[0] if where else None,
             where[1] if where else None, place, stage))


def _alarm_close(conn, vid: str, t: datetime, stage: str = ALARM_VALUE) -> None:
    with conn:
        conn.execute(
            "UPDATE sentry_alarms SET end_ts = ? WHERE vehicle_id = ? AND COALESCE(stage, 'Panic') = ? "
            "AND end_ts IS NULL",
            (format_ts(t), vid, stage))


def alarm_message(start_ts: str, count: int, place: str | None, lat, lon, label: str = "감시 경보") -> str:
    msg = f"{label} {_kst(start_ts)}"
    if place:
        msg += f" · 근처 {place}"
    else:
        msg += " · 위치는 아직 몰라요"
    if count > 1:
        msg += f" · {int(ALARM_MERGE.total_seconds() // 60)}분 안에 {count}번"
    return msg + " · 차 주변을 확인해 보세요"


def send_pending_sentry_alarms(conn, vid: str, now: datetime, sender: Sender) -> None:
    """Send each unsent alarm record once (retry a few times while it is fresh)."""
    try:
        rows = conn.execute(
            "SELECT start_ts, count, place, lat, lon, attempts, last_ts, end_ts, COALESCE(stage, 'Panic') "
            "FROM sentry_alarms WHERE vehicle_id = ? AND notified_ts IS NULL "
            "AND (notify_status IS NULL OR notify_status NOT IN ('stale', 'gave_up'))",
            (vid,)).fetchall()
    except sqlite3.OperationalError:
        return
    for start_ts, count, place, lat, lon, attempts, last_ts, end_ts, stage in rows:
        if now - parse_ts(start_ts) > ALARM_FRESH:
            with conn:
                conn.execute("UPDATE sentry_alarms SET notify_status = 'stale' WHERE vehicle_id = ? AND start_ts = ?",
                             (vid, start_ts))
            continue
        label = ALARM_STAGES.get(stage, "감시 경보")
        msg = alarm_message(start_ts, count, place, lat, lon, label)
        data: dict[str, Any] = {"sentry": "alarm", "raw": stage, "stage": stage, "label": label,
                                "start_ts": start_ts, "last_ts": last_ts, "end_ts": end_ts, "count": count}
        where = (lat, lon) if lat is not None and lon is not None else None
        if where:
            data["location"] = {"lat": lat, "lon": lon, "label": place, "approximate": True}
        ok = _emit(conn, vid, "sentry_alarm", start_ts, msg, data, now, sender, where=where)
        with conn:
            if ok:
                conn.execute("UPDATE sentry_alarms SET notified_ts = ?, notify_status = 'sent', attempts = attempts + 1 "
                             "WHERE vehicle_id = ? AND start_ts = ?", (format_ts(now), vid, start_ts))
            else:
                st = "gave_up" if attempts + 1 >= ALARM_MAX_ATTEMPTS else "retry"
                conn.execute("UPDATE sentry_alarms SET notify_status = ?, attempts = attempts + 1 "
                             "WHERE vehicle_id = ? AND start_ts = ?", (st, vid, start_ts))


def check_sentry_alarm(conn, vid, ev, now, sender, opener=None) -> None:
    """New SentryMode values since the last look: alarm starts/ends -> sentry_alarms, then send."""
    seen = ev.series("SentryMode")
    if seen:
        cur = _get_state(conn, vid, "sentry_alarm_seen")
        cursor = parse_ts(cur) if cur else now - ALARM_FRESH
        new = [(t, _sentry_value(v)) for t, v in seen if t > cursor]
        if new:
            prev = _sentry_value(ev.at("SentryMode", cursor))
            for t, v in new:
                if v in ALARM_STAGES and v != prev:
                    if prev in ALARM_STAGES:
                        _alarm_close(conn, vid, t, prev)
                    _alarm_open(conn, vid, ev, t, opener, v)
                elif v not in ALARM_STAGES and prev in ALARM_STAGES:
                    _alarm_close(conn, vid, t, prev)
                prev = v
            _set_state(conn, vid, "sentry_alarm_seen", format_ts(new[-1][0]), now)
    send_pending_sentry_alarms(conn, vid, now, sender)


def check_tpms_warning(conn, vid, ev, now, sender) -> None:
    hard = ev.latest("TpmsHardWarnings")
    soft = ev.latest("TpmsSoftWarnings")
    if hard is None and soft is None:
        return
    active = []
    if _truthy(hard):
        active.append(f"hard:{hard}")
    if _truthy(soft):
        active.append(f"soft:{soft}")
    state = "|".join(active) if active else "none"
    prev = _get_state(conn, vid, "tpms_warning")
    if prev == state:
        return
    if not active:
        _set_state(conn, vid, "tpms_warning", state, now)
        return
    level = "심한 이상" if _truthy(hard) else "점검 필요"
    from process.maintenance import _tire_settings, _vs_rec
    try:
        ts_ = _tire_settings(conn, vid)
    except Exception:  # noqa: BLE001
        ts_ = None
    bits = []
    for w, v in (_latest_tires(ev) or {}).items():
        if psi(v):
            bits.append(f"{WHEEL_KO[w]} 약 {psi(v)} psi" + (_vs_rec(ts_, w, v) if ts_ else ""))
    msg = f"차 공기압 경고 ({level})" + (" · " + ", ".join(bits) if bits else "")
    _emit(conn, vid, "tpms_warning", state, msg,
          {"hard": hard, "soft": soft, "tires_psi": {w: psi(v) for w, v in (_latest_tires(ev) or {}).items()}},
          now, sender)


def run_alerts(
    conn: sqlite3.Connection, vehicle_id: str, ev: Events,
    now: datetime | None = None, sender: Sender | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    sender = sender or webhook_sender
    from process.maintenance import SIGNAL_CHECKS

    for check in (check_trip_start, check_trip_end, check_charge_start, check_charge_end,
                  check_park14h, check_software_update, check_port_open_rates,
                  check_open_after_park, check_sentry, check_sentry_alarm, check_tpms_warning, *SIGNAL_CHECKS):
        try:
            check(conn, vehicle_id, ev, now, sender)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            print(f"alert check {check.__name__} failed: {type(exc).__name__}", flush=True)


def send_test(message: str = "teslife 알림 연결 테스트") -> str:
    ok, status = webhook_sender({"kind": "test", "message": message})
    return status


if __name__ == "__main__":
    print(send_test())
