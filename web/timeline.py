"""One KST day as three lanes (stats > 배터리 사용 > 일 선택): 주행, 주차, 감시.

Parking holds pet, climate keep, camping (ClimateKeeperMode Party), overheat protection
while it is actually running, and every other parked state. Sentry is its own lane.
Where those two overlap, parking usage loses this car's sentry watts times the overlap
(226 W until the car's own average is known) and the sentry bar keeps that share, so
the three lanes add up. Bars are cut to the time the car was sending data. A run of
signals with gaps up to AWAKE_GAP counts as awake; longer gaps (sleep) stay empty.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from web.estimates import estimate, measured_groups, mode_watts
from process import (
    KST,
    _as_float,
    format_ts,
    open_trip_start,
    parse_ts,
    sentry_active,
    _interp,
)

AWAKE_GAP = timedelta(minutes=15)
LANES = (("drive", "주행"), ("parked", "주차"), ("sentry", "감시"))
# No charge lane: charging adds energy, it is not use. Sentry while charging still
# shows on the sentry lane.
LANE_LABELS = dict(LANES)
# ClimateKeeperMode Party is camping. The screen says 캠핑.
PARK_DETAIL_KO = {
    "pet": "주차 · 펫",
    "keep": "주차 · 실내 온도 유지",
    "camp": "주차 · 캠핑",
    "overheat": "주차 · 과열 방지",
    "unknown": "주차 · 미확인",
    "parked": "주차",
}
_CLIMATE_DETAIL = {"camp": "camp", "dog": "pet", "keep": "keep"}

# Estimates for bars without their own measured kWh: see web/estimates.py.

Span = tuple[datetime, datetime]


def _sec(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def _intersect(spans: list[Span], cover: list[Span]) -> list[Span]:
    out: list[Span] = []
    for a, b in spans:
        for c, d in cover:
            s, e = max(a, c), min(b, d)
            if e > s:
                out.append((s, e))
    return out


def _merge(spans: list[Span], join: float = 1.0) -> list[Span]:
    out: list[list[datetime]] = []
    for a, b in sorted(spans):
        if out and _sec(out[-1][1], a) <= join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def awake_spans(times: list[datetime], gap: timedelta = AWAKE_GAP) -> list[Span]:
    """Runs of signal timestamps no more than `gap` apart. A lone sample is a point."""
    out: list[list[datetime]] = []
    for t in sorted(times):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b) for a, b in out]


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[tuple]:
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def _series(conn: sqlite3.Connection, vid: str, name: str, lo: str, hi: str) -> list[tuple[datetime, Any]]:
    """Values of `name` in [lo, hi), plus the last one before lo."""
    from process import decode_value

    seed = _rows(conn, "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = ? AND ts < ? "
                 "ORDER BY ts DESC LIMIT 1", (vid, name, lo))
    rows = _rows(conn, "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = ? AND ts >= ? AND ts < ? "
                 "ORDER BY ts", (vid, name, lo, hi))
    return [(parse_ts(t), decode_value(v)) for t, v in [*seed, *rows]]


def kwh_per_pct(conn: sqlite3.Connection, vid: str, at: str) -> float | None:
    """kWh in 1 % of battery: EnergyRemaining / BatteryLevel, the same basis as the hourly kWh.

    Uses the last pair at or before `at` (else the first after it; raw signals are pruned
    after 31 days but the newest row of each name is kept). None when unknown.
    """
    from process import decode_value

    def pick(name: str) -> float | None:
        for sql in ("SELECT value FROM signals WHERE vehicle_id = ? AND name = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                    "SELECT value FROM signals WHERE vehicle_id = ? AND name = ? AND ts > ? ORDER BY ts LIMIT 1"):
            rows = _rows(conn, sql, (vid, name, at))
            if rows and (v := _as_float(decode_value(rows[0][0]))) is not None:
                return v
        return None

    er, bl = pick("EnergyRemaining"), pick("BatteryLevel")
    if er is None or not bl or bl <= 0 or er <= 0:
        return None
    return round(er / bl, 4)


def _num_series(series: list[tuple[datetime, Any]]) -> list[tuple[datetime, float]]:
    return [(t, f) for t, v in series if (f := _as_float(v)) is not None]


def _apply_drive_net(
    bars: list[dict[str, Any]],
    er: list[tuple[datetime, float]],
    level: list[tuple[datetime, float]],
    life: list[tuple[datetime, float]],
    ratios: list[float],
) -> None:
    """Drive-bar kWh is that bar's battery drop times kWh in one percent.

    The same boundary and per-percent helpers as the stored trip figure, so the
    % and the kWh are one drop. A rise is 0. Without a level or a per-percent
    basis, LifetimeEnergyUsed across the bar times the recent net/gross ratio
    (else 0.75) is marked estimated. The stored trip kWh is already net, so it
    is not the fallback.
    """
    from process.net_energy import DEFAULT_NET_RATIO, boundary_level, kwh_per_pct, pack_kwh, value_near

    ratio = (sum(ratios) / len(ratios)) if ratios else DEFAULT_NET_RATIO
    for bar in bars:
        a, b = parse_ts(bar["start_ts"]), parse_ts(bar["end_ts"])
        start_level = boundary_level(level, a, at_end=False)
        end_level = boundary_level(level, b, at_end=True)
        drop = (start_level - end_level) if start_level is not None and end_level is not None else None
        per = kwh_per_pct(value_near(er, b), value_near(level, b))
        direct = pack_kwh(drop, per)
        bar.pop("estimated", None)
        bar.pop("net", None)
        bar.pop("soc_drop", None)
        if direct is not None and per:
            shown = round(max(0.0, drop or 0.0), 1)
            bar["kwh"] = round(shown * per, 2)
            bar["soc_drop"] = shown
            bar["net"] = True
            continue
        e0, e1 = _interp(life, a), _interp(life, b)
        if e0 is not None and e1 is not None and e1 > e0:
            bar["kwh"] = round((e1 - e0) * ratio, 2)
            bar["estimated"] = True
        elif bar.get("kwh") is not None:
            bar["kwh"] = round(max(0.0, float(bar["kwh"])), 2)
            bar["estimated"] = True


def _bar(lane: str, a: datetime, b: datetime, **extra: Any) -> dict[str, Any]:
    out = {"lane": lane, "start_ts": format_ts(a), "end_ts": format_ts(b), "minutes": round(_sec(a, b) / 60.0, 1)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _energy(span: Span, segs: list[tuple[datetime, datetime, float | None, int]],
            life: list[tuple[datetime, float]] | None = None) -> tuple[float | None, bool]:
    """kWh in span: rise of LifetimeEnergyUsed when it is known at both ends, else the
    parked pieces scaled by overlap time (marked). None if parked pieces cover < 90%
    of the span (e.g. sentry while charging: the energy is mixed with the charge)."""
    a, b = span
    if life:
        e0, e1 = _interp(life, a), _interp(life, b)
        if e0 is not None and e1 is not None and life[0][0] <= a and b <= life[-1][0]:
            pc = sum(_sec(max(a, s), min(b, e)) for s, e, *_ in segs if min(b, e) > max(a, s))
            if _sec(a, b) > 0 and pc >= 0.9 * _sec(a, b):
                return max(0.0, e1 - e0), False
            return None, False
    total, covered, est = 0.0, 0.0, False
    for s, e, kwh, flag in segs:
        lo, hi = max(a, s), min(b, e)
        if hi <= lo:
            continue
        covered += _sec(lo, hi)
        if kwh is None:
            return None, False
        frac = _sec(lo, hi) / max(1.0, _sec(s, e))
        total += kwh * frac
        est = est or bool(flag) or frac < 0.999
    if _sec(a, b) <= 0 or covered < 0.9 * _sec(a, b):
        return None, False
    return total, est


def _with_energy(lane: str, span: Span, segs, life=None) -> dict[str, Any]:
    kwh, est = _energy(span, segs, life)
    hours = _sec(*span) / 3600.0
    return _bar(lane, *span, kwh=round(kwh, 3) if kwh is not None else None,
                avg_w=round(kwh / hours * 1000.0) if kwh is not None and hours >= 0.05 else None,
                estimated=est if kwh is not None else None)


def _modes(category: str, modes: str | None) -> tuple[str, ...]:
    """Modes on in a parked piece; rows from before the modes column hold only the category."""
    if modes is None:
        return (category,) if category else ()
    return tuple(m for m in modes.split(",") if m) + (("parked",) if category == "parked" else ())


def _segment_rows(conn: sqlite3.Connection, vid: str, lo: str, hi: str) -> list[tuple]:
    sql = ("SELECT start_ts, end_ts, category, sentry, kwh, kwh_estimated, {m} FROM parked_segments "
           "WHERE vehicle_id = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts")
    rows = _rows(conn, sql.format(m="modes"), (vid, lo, hi))
    return rows or _rows(conn, sql.format(m="NULL"), (vid, lo, hi))


def _value_at(series: list[tuple[datetime, float]], t: datetime) -> float | None:
    last = None
    for ts, v in series:
        if ts > t:
            break
        last = v
    return last if last is not None else (series[0][1] if series else None)


def _held(series: list[tuple[datetime, Any]], t: datetime) -> Any:
    """Last sample at or before t. None when the signal has not been seen yet."""
    last = None
    seen = False
    for ts, v in series:
        if ts > t:
            break
        last, seen = v, True
    return last if seen else None


def _cuts(series: list[tuple[datetime, Any]], a: datetime, b: datetime) -> list[datetime]:
    pts = [a]
    for ts, _v in series:
        if a < ts < b:
            pts.append(ts)
    pts.append(b)
    return pts


def park_detail(modes: tuple[str, ...] | list[str], hvac: Any) -> str | None:
    """Parking-bar detail, or None when the piece is sentry alone (no parking bar).

    Climate keeper wins over HVAC. Overheat counts only while HvacPower is
    OverheatProtect. A general parked piece with no HvacPower sample is unknown.
    """
    have = set(modes)
    for key, detail in _CLIMATE_DETAIL.items():
        if key in have:
            return detail
    if str(hvac or "") == "OverheatProtect":
        return "overheat"
    if have <= {"sentry"}:
        return None
    if hvac is None:
        return "unknown"
    return "parked"


def _overlap_h(a: datetime, b: datetime, spans: list[Span]) -> float:
    sec = 0.0
    for s, e in spans:
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            sec += _sec(lo, hi)
    return sec / 3600.0


def _share_sentry(bars: dict[str, list[dict[str, Any]]], watts: float) -> None:
    """Take sentry average power × overlap out of the parking bar and give that share to sentry.

    The two numbers add back to the parking interval, so a header that sums the three
    lanes does not count the same energy twice. The floor is 0.
    """
    park = [(parse_ts(b["start_ts"]), parse_ts(b["end_ts"])) for b in bars["parked"]]
    sent = [(parse_ts(b["start_ts"]), parse_ts(b["end_ts"])) for b in bars["sentry"]]
    for bar in bars["parked"]:
        a, b = parse_ts(bar["start_ts"]), parse_ts(bar["end_ts"])
        hours = _overlap_h(a, b, sent)
        if hours <= 0:
            continue
        share = watts * hours / 1000.0
        dur_h = _sec(a, b) / 3600.0
        if bar.get("kwh") is not None:
            bar["kwh"] = round(max(0.0, float(bar["kwh"]) - share), 3)
            bar["estimated"] = True
            if dur_h >= 0.05:
                bar["avg_w"] = round(bar["kwh"] / dur_h * 1000.0)
        elif bar.get("kwh_estimate") is not None:
            bar["kwh_estimate"] = round(max(0.0, float(bar["kwh_estimate"]) - share), 2)
    for bar in bars["sentry"]:
        a, b = parse_ts(bar["start_ts"]), parse_ts(bar["end_ts"])
        dur = _sec(a, b) / 3600.0
        hours = _overlap_h(a, b, park)
        if hours <= 0 or dur <= 0:
            continue
        rest = max(0.0, dur - hours)
        extra = 0.0
        if rest > 1e-9:
            if bar.get("kwh") is None and bar.get("kwh_estimate") is not None:
                extra = float(bar["kwh_estimate"]) * (rest / dur)
            else:
                extra = watts * rest / 1000.0
        bar["kwh"] = round(max(0.0, watts * hours / 1000.0 + extra), 3)
        bar.pop("kwh_estimate", None)
        bar["estimated"] = True
        if dur >= 0.05:
            bar["avg_w"] = round(bar["kwh"] / dur * 1000.0)


def _shown_kwh(bar: dict[str, Any]) -> float | None:
    if bar.get("kwh") is not None:
        return float(bar["kwh"])
    if bar.get("kwh_estimate") is not None:
        return float(bar["kwh_estimate"])
    return None


def day_timeline(
    conn: sqlite3.Connection, vehicle_id: str, day: str, now: datetime | None = None
) -> dict[str, Any]:
    from web import _DATE_RE, _recent_events

    if not day or not _DATE_RE.fullmatch(day):
        raise ValueError("date must be YYYY-MM-DD")
    now = now or datetime.now(timezone.utc)
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST).astimezone(timezone.utc)
    d1 = d0 + timedelta(days=1)
    end = min(d1, now)
    lo, hi = format_ts(d0), format_ts(d1)
    vid = vehicle_id

    times = [parse_ts(t) for (t,) in _rows(
        conn, "SELECT DISTINCT ts FROM signals WHERE vehicle_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (vid, format_ts(d0 - AWAKE_GAP), hi))]
    cover = [(max(a, d0), min(b, end)) for a, b in awake_spans(times) if b > d0 and a < end]

    segs = [(parse_ts(s), parse_ts(e), _modes(cat, modes), sentry, kwh, est)
            for s, e, cat, sentry, kwh, est, modes in _segment_rows(conn, vid, lo, hi)]
    energy = [(s, e, kwh, est) for s, e, _c, _x, kwh, est in segs]
    life = [(t, f) for t, v in _series(conn, vid, "LifetimeEnergyUsed", lo, hi)
            if (f := _as_float(v)) is not None]

    bars: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in LANES}
    hvac = _series(conn, vid, "HvacPower", lo, hi)
    grouped: dict[str, list[Span]] = {}
    for s, e, modes, *_rest in segs:
        a0, b0 = max(s, d0), min(e, end)
        if b0 <= a0:
            continue
        climate = next((detail for key, detail in _CLIMATE_DETAIL.items() if key in modes), None)
        if climate:
            grouped.setdefault(climate, []).append((a0, b0))
            continue
        pts = _cuts(hvac, a0, b0)
        for a, b in zip(pts, pts[1:]):
            detail = park_detail(modes, _held(hvac, a))
            if detail:
                grouped.setdefault(detail, []).append((a, b))
    for detail, spans in grouped.items():
        for span in _merge(_intersect(spans, cover)):
            bar = _with_energy("parked", span, energy, life)
            bar["detail"] = detail
            bar["title"] = PARK_DETAIL_KO[detail]
            bars["parked"].append(bar)
    bars["parked"].sort(key=lambda b: b["start_ts"])

    # Sentry from the signal itself, so it also shows while charging (not only parked).
    on: list[Span] = []
    start: datetime | None = None
    for t, v in _series(conn, vid, "SentryMode", lo, hi):
        if sentry_active(v) and start is None:
            start = max(t, d0)
        elif not sentry_active(v) and start is not None:
            on.append((start, min(max(t, d0), end)))
            start = None
    if start is not None:
        on.append((start, end))
    for span in _merge(_intersect([s for s in on if s[1] > s[0]], cover)):
        bars["sentry"].append(_with_energy("sentry", span, energy, life))

    for s, e, km, kwh, s0, s1 in _rows(
            conn, "SELECT start_ts, end_ts, km, kwh, start_soc, end_soc FROM trips "
            "WHERE vehicle_id = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts", (vid, lo, hi)):
        a, b = parse_ts(s), parse_ts(e)
        bars["drive"].append(_bar("drive", max(a, d0), min(b, d1), session_start=s, session_end=e,
                                  km=round(km, 1) if km is not None else None,
                                  kwh=round(kwh, 2) if kwh is not None else None,
                                  start_soc=s0, end_soc=s1))

    if d0 <= now < d1 + timedelta(hours=72):
        ev = _recent_events(conn, vid, ("Gear",), now)
        if ev is not None:
            st = open_trip_start(ev, now)
            if st is not None and st < end and now > d0:
                bars["drive"].append(_bar("drive", max(st, d0), end, session_start=format_ts(st), in_progress=True))

    er = _num_series(_series(conn, vid, "EnergyRemaining", lo, hi))
    bl = _num_series(_series(conn, vid, "BatteryLevel", lo, hi))
    if not bl:
        bl = _num_series(_series(conn, vid, "Soc", lo, hi))
    from process.net_energy import recent_net_ratios
    _apply_drive_net(bars["drive"], er, bl, life, recent_net_ratios(conn, vid, set()))

    groups = measured_groups(conn, vid, d1)
    temps = [(t, f) for t, v in _series(conn, vid, "OutsideTemp", lo, hi) if (f := _as_float(v)) is not None]
    # Sentry while charging has no kWh of its own: the mode's whole-car draw.
    for bar in bars["sentry"]:
        if "kwh" not in bar:
            a, b = parse_ts(bar["start_ts"]), parse_ts(bar["end_ts"])
            bar.update(estimate(groups, "sentry", a, b, _value_at(temps, a)))
    rated = mode_watts(groups, "sentry")
    _share_sentry(bars, rated[0] if rated else 226.0)

    # 감시 경보 records of the day: Panic marks on the 감시 lane. Aware (주의) stays
    # in the alert log and is not drawn on this screen.
    alarms = sentry_alarms(conn, vid, lo, hi)
    drawn = [a for a in alarms if a.get("stage") != "Aware"]
    lanes = [{"key": k, "label": label, "bars": bars[k]} for k, label in LANES
             if bars[k] or (k == "sentry" and drawn)]
    shown = [b for k, _label in LANES for b in bars[k]]
    values = [v for b in shown if (v := _shown_kwh(b)) is not None]
    totals = {
        "minutes": round(sum(b["minutes"] for b in shown), 1),
        "kwh": round(sum(values), 3) if values else None,
    }
    return {
        "vehicle_id": vid, "date": day, "start_ts": lo, "end_ts": hi,
        "now_ts": format_ts(now) if d0 <= now < d1 else None,
        "awake": [{"start_ts": format_ts(a), "end_ts": format_ts(b)} for a, b in cover],
        "kwh_per_pct": kwh_per_pct(conn, vid, format_ts(min(end, d1))),
        "lanes": lanes,
        "totals": totals,
        "alarms": alarms,
        "hours": _hours(conn, vid, d0, end, lanes, cover),
    }


def sentry_alarms(conn, vid: str, lo: str | None = None, hi: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
    """감시 경보 records (start, last alarm, end, count, place), newest first; [] before the table exists."""
    where = " WHERE vehicle_id = ?"
    args: list[Any] = [vid]
    if lo:
        where += " AND COALESCE(end_ts, last_ts) >= ?"
        args.append(lo)
    if hi:
        where += " AND start_ts < ?"
        args.append(hi)
    tail = " ORDER BY start_ts DESC LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(
            "SELECT start_ts, last_ts, end_ts, count, place, COALESCE(stage, 'Panic') FROM sentry_alarms"
            + where + tail, args).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(a, b, c, n, p, "Panic") for a, b, c, n, p in conn.execute(
                "SELECT start_ts, last_ts, end_ts, count, place FROM sentry_alarms" + where + tail, args).fetchall()]
        except sqlite3.OperationalError:
            return []
    out = []
    for a, b, c, n, p, stage in rows:
        label = "주의" if stage == "Aware" else "감시 경보"
        out.append({"start_ts": a, "last_ts": b, "end_ts": c, "count": n, "place": p,
                    "stage": stage, "label": label})
    return out


def _charge_spans(conn, vid: str, d0: datetime, d1: datetime, end: datetime) -> list[Span]:
    """Finished charges overlapping the day, plus a charge still running on today."""
    spans: list[Span] = []
    for s, e in _rows(
        conn,
        "SELECT start_ts, end_ts FROM charges WHERE vehicle_id = ? AND end_ts > ? AND start_ts < ?",
        (vid, format_ts(d0), format_ts(d1)),
    ):
        if s and e:
            spans.append((parse_ts(s), parse_ts(e)))
    if end < d1:
        from process import open_charge_start
        from web import _recent_events

        ev = _recent_events(conn, vid, ("DetailedChargeState", "ChargeState"), end)
        if ev is not None:
            started = open_charge_start(ev)
            if started is not None:
                spans.append((started, end))
    return spans


def _hour_bounds(
    level: list[tuple[datetime, float]], a: datetime, b: datetime, charging: bool,
    gap: timedelta = AWAKE_GAP,
) -> tuple[tuple[datetime, float], tuple[datetime, float]] | None:
    """Start and end battery samples for [a, b).

    Normally the start is the last sample before a, and the end is the last
    sample before b, so one hour's end is the next hour's start. A charge,
    or a gap longer than `gap` with no sample, keeps the first and last
    sample inside the hour. An hour with no sample stays empty.
    """
    inside = [(t, v) for t, v in level if a <= t < b]
    if not inside:
        return None
    prev = None
    for t, v in level:
        if t < a:
            prev = (t, v)
        else:
            break
    stale = prev is None or (a - prev[0]) > gap
    if charging or stale:
        return inside[0], inside[-1]
    return prev, inside[-1]


def _hours(conn, vid: str, d0: datetime, end: datetime, lanes, cover: list[Span]) -> list[dict[str, Any]]:
    lo, hi = format_ts(d0), format_ts(d0 + timedelta(days=1))
    # Keep the sample from before midnight. The old filter dropped it, so hour 0
    # could not start from the last value before 00:00.
    level = [(t, _as_float(v)) for t, v in _series(conn, vid, "BatteryLevel", lo, hi)]
    level = sorted(((t, v) for t, v in level if v is not None), key=lambda p: p[0])
    er = [(t, _as_float(v)) for t, v in _series(conn, vid, "EnergyRemaining", lo, hi)]
    er = [(t, v) for t, v in er if v is not None]
    charges = _charge_spans(conn, vid, d0, d0 + timedelta(days=1), end)
    out = []
    for h in range(24):
        a, b = d0 + timedelta(hours=h), d0 + timedelta(hours=h + 1)
        if a >= end:
            break
        states = []
        for lane in lanes:
            for bar in lane["bars"]:
                s, e = max(parse_ts(bar["start_ts"]), a), min(parse_ts(bar["end_ts"]), b)
                if e > s:
                    st = {"lane": lane["key"], "label": lane["label"], "start_ts": format_ts(s),
                          "end_ts": format_ts(e), "minutes": round(_sec(s, e) / 60.0, 1)}
                    states.append(st)
        states.sort(key=lambda x: x["start_ts"])
        awake = sum(_sec(max(c, a), min(d, b)) for c, d in cover if min(d, b) > max(c, a))
        row: dict[str, Any] = {"hour": h, "states": states, "awake_min": round(awake / 60.0, 1)}
        charging = any(s < b and e > a for s, e in charges)
        bounds = _hour_bounds(level, a, b, charging)
        if bounds:
            start, stop = bounds
            row.update(start_pct=round(start[1], 1), start_ts=format_ts(start[0]),
                       end_pct=round(stop[1], 1), end_ts=format_ts(stop[0]))
            if start[0] != stop[0]:
                delta = stop[1] - start[1]
                row["delta_pct"] = round(delta, 2)
                ref = [v for t, v in er if t <= stop[0]]
                if ref and stop[1] > 0 and abs(delta) > 0:
                    row["kwh"] = round(abs(delta) * ref[-1] / stop[1], 2)
                    row["kwh_estimated"] = True
        out.append(row)
    return out
