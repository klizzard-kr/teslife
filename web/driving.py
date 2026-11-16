"""운전 습관 리포트 (week or month, KST) built only from the stored `trips` table.

Everything here comes from fields the normal 62-field telemetry config fills: harsh accel /
brake / turn counts (LongitudinalAcceleration / LateralAcceleration at 1 s, with speed, pedal
and brake checks), regen kWh (estimated from pack power), kWh, km, duration, max speed.
There is no speed-limit signal, so nothing here talks about speeding.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from process import HARD_ACCEL_G, HARD_BRAKE_G, HARD_TURN_G, KST, MIN_TRIP_KM, parse_ts

RATE_MIN_KM = 10.0   # per-100 km rates only once the period has this much distance
SCORE_MIN_KM = 20.0  # the score only once the period has this much distance
SCORE_WEIGHTS = {"accel": 5.0, "brake": 7.0, "turn": 3.0}  # points per event per 100 km
TREND_PERIODS = 8


def period_bounds(period: str, at: date) -> tuple[date, date]:
    """[start, end) dates in KST: a week starts on Monday, a month on the 1st."""
    if period == "week":
        start = at - timedelta(days=at.weekday())
        return start, start + timedelta(days=7)
    if period == "month":
        start = at.replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, nxt
    if period == "year":
        return date(at.year, 1, 1), date(at.year + 1, 1, 1)
    raise ValueError("period must be week, month or year")


def _utc(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=KST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(conn: sqlite3.Connection, vid: str, start: date, end: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT start_ts, end_ts, km, kwh, duration_s, regen_kwh, max_speed, hard_accel, "
            "hard_brake, hard_turn FROM trips WHERE vehicle_id = ? AND start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts", (vid, _utc(start), _utc(end)),
        ).fetchall()
    finally:
        conn.row_factory = None


def _per100(n: int, km: float) -> float | None:
    return round(n * 100.0 / km, 1) if km >= RATE_MIN_KM else None


def score(rates: dict[str, float | None], km: float) -> int | None:
    """100 - 5 x 급가속 - 7 x 급제동 - 3 x 급회전 (each per 100 km), floor 0. None below SCORE_MIN_KM."""
    if km < SCORE_MIN_KM or any(rates.get(k) is None for k in SCORE_WEIGHTS):
        return None
    lost = sum(SCORE_WEIGHTS[k] * float(rates[k]) for k in SCORE_WEIGHTS)  # type: ignore[arg-type]
    return max(0, round(100 - lost))


def summarize(rows: list[sqlite3.Row]) -> dict[str, Any]:
    km = sum(r["km"] or 0.0 for r in rows)
    secs = sum(r["duration_s"] or 0.0 for r in rows)
    kwh_rows = [r for r in rows if r["kwh"] is not None and r["km"] >= MIN_TRIP_KM]
    kwh = sum(r["kwh"] for r in kwh_rows)
    kwh_km = sum(r["km"] for r in kwh_rows)
    regen_rows = [r for r in rows if r["regen_kwh"] is not None and r["kwh"] is not None]
    regen = sum(r["regen_kwh"] for r in regen_rows)
    regen_base = sum(r["kwh"] for r in regen_rows)
    # harsh counts only from trips that had acceleration samples (None = no data, not zero)
    harsh_rows = [r for r in rows if r["hard_accel"] is not None and r["hard_brake"] is not None]
    turn_rows = [r for r in rows if r["hard_turn"] is not None]
    harsh_km = sum(r["km"] for r in harsh_rows)
    turn_km = sum(r["km"] for r in turn_rows)
    counts = {
        "accel": sum(r["hard_accel"] for r in harsh_rows) if harsh_rows else None,
        "brake": sum(r["hard_brake"] for r in harsh_rows) if harsh_rows else None,
        "turn": sum(r["hard_turn"] for r in turn_rows) if turn_rows else None,
    }
    rates = {
        "accel": _per100(counts["accel"], harsh_km) if counts["accel"] is not None else None,
        "brake": _per100(counts["brake"], harsh_km) if counts["brake"] is not None else None,
        "turn": _per100(counts["turn"], turn_km) if counts["turn"] is not None else None,
    }
    speeds = [r["max_speed"] for r in rows if r["max_speed"] is not None]
    rate_km = min(harsh_km, turn_km) if turn_rows else harsh_km
    return {
        "trip_count": len(rows),
        "km": round(km, 2),
        "drive_minutes": round(secs / 60.0, 1),
        "kwh": round(kwh, 3) if kwh_rows else None,
        "km_per_kwh": round(kwh_km / kwh, 2) if kwh_rows and kwh > 0.01 else None,
        "regen_kwh": round(regen, 3) if regen_rows else None,
        "regen_ratio": round(regen / regen_base, 3) if regen_rows and regen_base > 0.01 else None,
        "max_speed": max(speeds) if speeds else None,
        "avg_speed": round(km / (secs / 3600.0), 1) if secs > 0 and km >= MIN_TRIP_KM else None,
        "harsh": counts,
        "harsh_per_100km": rates,
        "harsh_km": round(harsh_km, 2),
        "score": score(rates, rate_km),
    }


def driving_report(conn: sqlite3.Connection, vid: str, period: str = "week",
                   at: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(at) if at else now.astimezone(KST).date()
    start, end = period_bounds(period, day)
    first = conn.execute("SELECT MIN(start_ts) FROM trips WHERE vehicle_id = ?", (vid,)).fetchone()[0]
    out = {"period": period, "from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
           "first_day": parse_ts(first).astimezone(KST).date().isoformat() if first else None,
           **summarize(_rows(conn, vid, start, end))}
    # trend: the 8 periods up to this one; for a year, its 12 months
    unit = "month" if period == "year" else period
    steps = []
    if period == "year":
        steps = [date(start.year, m, 1) for m in range(1, 13)]
    else:
        s = start
        for _ in range(TREND_PERIODS):
            steps.append(s)
            s = period_bounds(period, s - timedelta(days=1))[0]
        steps.reverse()
    trend = []
    for s in steps:
        e = period_bounds(unit, s)[1]
        sm = summarize(_rows(conn, vid, s, e))
        trend.append({"from": s.isoformat(), "km": sm["km"], "km_per_kwh": sm["km_per_kwh"],
                      "harsh": (sum(v for v in sm["harsh"].values() if v is not None)
                                if any(v is not None for v in sm["harsh"].values()) else None)})
    out["trend"] = trend
    out["trend_unit"] = unit
    out["rules"] = {
        "hard_accel_g": HARD_ACCEL_G, "hard_brake_g": HARD_BRAKE_G, "hard_turn_g": HARD_TURN_G,
        "rate_min_km": RATE_MIN_KM,
        "score_min_km": SCORE_MIN_KM, "score_weights": SCORE_WEIGHTS, "speed_limit": None,
    }
    return out
