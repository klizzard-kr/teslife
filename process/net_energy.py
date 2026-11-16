"""Net energy of one drive: battery-percent drop times kWh in one percent.

The stored trip kWh used to be the rise of LifetimeEnergyUsed (gross, regen still
inside it). Screens that show 쓴 전력 and 전비 use this net figure instead.
LifetimeEnergyUsed, EnergyRemaining and BatteryLevel samples are left as recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MIN_DROP_PCT = 1.0
MIN_PLAIN_KM = 5.0
DEFAULT_NET_RATIO = 0.75
RATIO_KEEP = 10


def _points(ev: Any, name: str) -> list[tuple[datetime, float]]:
    from process import _as_float

    out = []
    for ts, value in ev.series(name):
        num = _as_float(value)
        if num is not None:
            out.append((ts, num))
    return out


def boundary_level(points: list[tuple[datetime, float]], t: datetime, *, at_end: bool) -> float | None:
    """Level at t. Interpolate when a sample sits on each side.

    The start side otherwise keeps the last sample at or before t (else the first
    after). The end side otherwise keeps the first sample at or after t (else the
    last before).
    """
    if not points:
        return None
    before = after = None
    for ts, value in points:
        if ts < t:
            before = (ts, value)
        elif ts == t:
            return value
        else:
            after = (ts, value)
            break
    if before and after:
        span = (after[0] - before[0]).total_seconds()
        if span > 0:
            weight = (t - before[0]).total_seconds() / span
            return before[1] + (after[1] - before[1]) * weight
    if at_end:
        return after[1] if after else (before[1] if before else None)
    return before[1] if before else (after[1] if after else None)


def value_near(points: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Last sample at or before t, else the first after. Matches timeline kwh_per_pct."""
    last = None
    for ts, value in points:
        if ts <= t:
            last = value
        elif last is None:
            return value
        else:
            return last
    return last


def kwh_per_pct(energy: float | None, level: float | None) -> float | None:
    if energy is None or level is None or level <= 0 or energy <= 0:
        return None
    return energy / level


def recent_net_ratios(conn: Any, vehicle_id: str, skip_starts: set[str]) -> list[float]:
    """Net/gross of recent long trips already stored. Skips rows this pass will rewrite.

    Only rows marked kwh_estimated = 0 count. A version bump leaves older rows NULL
    until they are rebuilt, and those still hold the gross figure.
    """
    try:
        found = conn.execute(
            """
            SELECT start_ts, kwh, start_lifetime_kwh, end_lifetime_kwh
            FROM trips
            WHERE vehicle_id = ? AND kwh_estimated = 0 AND kwh > 0.01
              AND start_lifetime_kwh IS NOT NULL
              AND end_lifetime_kwh > start_lifetime_kwh
            ORDER BY start_ts DESC
            """,
            (vehicle_id,),
        ).fetchall()
    except Exception:
        return []
    picked: list[float] = []
    for start, kwh, a, b in found:
        if start in skip_starts:
            continue
        picked.append(float(kwh) / (float(b) - float(a)))
        if len(picked) >= RATIO_KEEP:
            break
    picked.reverse()
    return picked


def pack_kwh(drop: float | None, per: float | None) -> float | None:
    """Battery drop times kWh in one percent. A rise is 0. None when either input is missing."""
    if drop is None or per is None or per <= 0:
        return None
    return max(0.0, drop) * per


def apply_trip_net(ev: Any, rows: list[dict[str, Any]], prior_ratios: list[float] | None = None) -> None:
    """Rewrite each row's kWh and km/kWh. Regen stays on the gross lifetime figure.

    Rows must already carry the gross kWh from LifetimeEnergyUsed (or the
    EnergyRemaining fallback) plus start/end lifetime and km. A version bump
    rebuilds these rows from the remaining signals; it does not delete trips.
    """
    from process import MIN_TRIP_KM, parse_ts

    levels = _points(ev, "BatteryLevel") or _points(ev, "Soc")
    remaining = _points(ev, "EnergyRemaining")
    ratios: list[float] = list(prior_ratios or [])
    ordered = sorted(rows, key=lambda row: row["start_ts"])
    for row in ordered:
        start, end = parse_ts(row["start_ts"]), parse_ts(row["end_ts"])
        gross = row.get("kwh")
        life_s, life_e = row.get("start_lifetime_kwh"), row.get("end_lifetime_kwh")
        lifetime = (life_e - life_s) if life_s is not None and life_e is not None else None
        per = kwh_per_pct(value_near(remaining, end), value_near(levels, end))
        start_level = boundary_level(levels, start, at_end=False)
        end_level = boundary_level(levels, end, at_end=True)
        drop = (start_level - end_level) if start_level is not None and end_level is not None else None
        km = row.get("km")
        direct = pack_kwh(drop, per) if drop is not None and drop > 0 else None
        kwh, forced = (direct, False) if direct is not None else _choose(None, None, lifetime, gross, ratios)
        row["kwh"] = round(kwh, 4) if kwh is not None else None
        show_eff = kwh is not None and kwh > 0.01 and km is not None and km >= MIN_TRIP_KM
        row["km_per_kwh"] = (km / kwh) if show_eff else None
        row["wh_per_km"] = (kwh * 1000.0 / km) if show_eff else None
        approx = forced or drop is None or drop < MIN_DROP_PCT or km is None or km < MIN_PLAIN_KM
        row["kwh_estimated"] = (1 if approx else 0) if kwh is not None else None
        regen = row.get("regen_kwh")
        base = lifetime if lifetime is not None and lifetime > 0.01 else (gross if gross and gross > 0.01 else None)
        row["regen_ratio"] = (regen / base) if regen is not None and base else None
        if (
            drop is not None and drop >= MIN_DROP_PCT and per and per > 0
            and lifetime is not None and lifetime > 0
            and km is not None and km >= MIN_PLAIN_KM
        ):
            ratios.append((drop * per) / lifetime)
            del ratios[:-RATIO_KEEP]


def _choose(
    drop: float | None,
    per: float | None,
    lifetime: float | None,
    gross: float | None,
    ratios: list[float],
) -> tuple[float | None, bool]:
    """(kWh, forced-estimate). forced-estimate is the ratio/legacy path, not a small drop."""
    if drop is not None and drop > 0 and per and per > 0:
        return drop * per, False
    ratio = (sum(ratios) / len(ratios)) if ratios else DEFAULT_NET_RATIO
    if lifetime is not None and lifetime > 0:
        return lifetime * ratio, True
    if gross is not None and gross > 0:
        return gross, True
    if gross is not None:
        return max(0.0, gross), True
    return None, False
