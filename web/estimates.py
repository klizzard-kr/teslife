"""Estimated power of one mode, for parked-mode bars with no measured kWh of their own
(e.g. sentry while charging: the car's energy is mixed with the charge).

Every mode starts from a prior (PRIOR_W / CLIMATE_PRIOR_W below, sources in
docs/DESIGN.md) and switches to this car's own parked average once that average has
>= MIN_HOURS on >= MIN_DAYS KST days. The source ("prior" / "measured") is kept in the
API for checking; the screen shows both the same way, as "약 … kWh".

Values are whole-car draw while parked with only that mode on. Change the numbers here only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from process import KST, format_ts, parse_ts

# ---- priors (W, whole car, parked). Sources: docs/DESIGN.md "추정 기본값".
# Vehicle: Model Y Juniper Premium RWD, 19" wheels.
PRIOR_W = {
    "sentry": 226,           # this car, measured 2026-10-30 (LifetimeEnergyUsed)
}
# The car's climate keeper modes (camp / dog / keep), heat pump, steady state, by outside temperature.
CLIMATE_PRIOR_W = {"5℃ 미만": 2000, "5–15℃": 900, "15–25℃": 700, "25℃ 이상": 1400}
CLIMATE_MODES = ("camp", "dog", "keep")
TEMP_BANDS = ((None, 5.0, "5℃ 미만"), (5.0, 15.0, "5–15℃"), (15.0, 25.0, "15–25℃"), (25.0, None, "25℃ 이상"))

# ---- switching to this car's own average
LOOKBACK = timedelta(days=60)
MIN_HOURS = 1.0
MIN_DAYS = 2
MIN_PIECE = timedelta(minutes=5)     # shorter parked pieces are too noisy

_CACHE: dict[tuple, dict[str, Any]] = {}


def temp_band(temp: float | None) -> str | None:
    if temp is None:
        return None
    for lo, hi, label in TEMP_BANDS:
        if (lo is None or temp >= lo) and (hi is None or temp < hi):
            return label
    return None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[tuple]:
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def measured_groups(conn: sqlite3.Connection, vid: str, before: datetime) -> dict[str, dict[str, Any]]:
    """This car's parked averages: key -> {w, hours, days, ok}.

    Keys: "<mode>" or "<climate mode>:<band>" from parked pieces with only that one mode on
    (measured kWh, >= MIN_PIECE).
    """
    lo, hi = format_ts(before - LOOKBACK), format_ts(before)
    pieces = _rows(conn, "SELECT start_ts, end_ts, category, kwh, kwh_estimated, modes, outside_temp "
                   "FROM parked_segments WHERE vehicle_id = ? AND start_ts >= ? AND end_ts <= ? "
                   "AND modes IS NOT NULL ORDER BY start_ts", (vid, lo, hi))
    key = (vid, hi, len(pieces), pieces[-1][1] if pieces else None)
    if key in _CACHE:
        return _CACHE[key]
    acc: dict[str, list] = {}

    def add(k: str, kwh: float, a: datetime, b: datetime) -> None:
        g = acc.setdefault(k, [0.0, 0.0, set()])
        g[0] += kwh
        g[1] += (b - a).total_seconds() / 3600.0
        g[2].add(a.astimezone(KST).date())

    for s, e, cat, kwh, est, modes, temp in pieces:
        a, b = parse_ts(s), parse_ts(e)
        if kwh is None or est or b - a < MIN_PIECE or "," in modes:
            continue
        if modes in CLIMATE_MODES:
            band = temp_band(temp)
            if band:
                add(f"{modes}:{band}", float(kwh), a, b)
        elif modes:
            add(modes, float(kwh), a, b)

    out = {}
    for k, (kwh, hours, days) in acc.items():
        out[k] = {"w": round(kwh / hours * 1000.0) if hours > 0 else None, "hours": round(hours, 2),
                  "days": len(days), "ok": hours >= MIN_HOURS and len(days) >= MIN_DAYS}
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def mode_watts(groups: dict[str, Any], mode: str, temp: float | None = None) -> tuple[float, str, str | None] | None:
    """(W, "measured"|"prior", band) for one mode parked alone, or None if unknown
    (a climate mode without an outside temperature, or a mode with no prior)."""
    if mode in CLIMATE_MODES:
        band = temp_band(temp)
        if not band:
            return None
        g = groups.get(f"{mode}:{band}")
        return (g["w"], "measured", band) if g and g["ok"] else (CLIMATE_PRIOR_W[band], "prior", band)
    g = groups.get(mode)
    if g and g["ok"]:
        return g["w"], "measured", None
    return (PRIOR_W[mode], "prior", None) if mode in PRIOR_W else None


def estimate(groups: dict[str, Any], mode: str, a: datetime, b: datetime, temp: float | None) -> dict[str, Any]:
    """Fields for a bar: kwh_estimate, estimate_w, estimate_source, estimate_band. {} if unknown."""
    m = mode_watts(groups, mode, temp)
    if m is None:
        return {}
    w, src, band = m
    out = {"kwh_estimate": round(w * (b - a).total_seconds() / 3600.0 / 1000.0, 2),
           "estimate_w": round(w), "estimate_source": src}
    if band:
        out["estimate_band"] = band
    return out
