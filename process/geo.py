"""Small geometry helpers for route points (no dependencies)."""

from __future__ import annotations

import math
from typing import Any, Sequence

EARTH_M = 6371008.8


def distance_m(a: Sequence[float], b: Sequence[float]) -> float:
    """Great-circle distance in meters between (lat, lon) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def thin(points: list[tuple], min_m: float) -> list[tuple]:
    """Drop points closer than `min_m` to the last kept one; always keep the last point.

    Each point starts with (lat, lon, ...).
    """
    out: list[tuple] = []
    for p in points:
        if not out or distance_m(out[-1], p) >= min_m:
            out.append(p)
    if points and out[-1] is not points[-1]:
        if len(out) > 1 and distance_m(out[-2], points[-1]) < min_m:
            out[-1] = points[-1]
        else:
            out.append(points[-1])
    return out


def _xy(p: Sequence[float], lat0: float) -> tuple[float, float]:
    k = math.pi / 180 * EARTH_M
    return p[1] * k * math.cos(math.radians(lat0)), p[0] * k


def simplify(points: list[Sequence[float]], tolerance_m: float) -> list[Sequence[float]]:
    """Douglas-Peucker on (lat, lon, ...) points with a tolerance in meters (iterative)."""
    n = len(points)
    if n < 3 or tolerance_m <= 0:
        return list(points)
    lat0 = sum(p[0] for p in points) / n
    xy = [_xy(p, lat0) for p in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            if seg == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
                d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
            if d > best:
                best, idx = d, k
        if best > tolerance_m and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]


def locate(points: Sequence[tuple], ts: Any) -> tuple[float, float] | None:
    """Position at time `ts` interpolated between route points (lat, lon, ts, ...).

    Before the first or after the last point it is that point. Points are about
    3 s / 20 m apart (10 s / 50 m before 2026-11-01), so the result is an approximation.
    """
    if not points:
        return None
    if ts <= points[0][2]:
        return points[0][0], points[0][1]
    for a, b in zip(points, points[1:]):
        if a[2] <= ts <= b[2]:
            span = (b[2] - a[2]).total_seconds()
            f = 0.0 if span <= 0 else (ts - a[2]).total_seconds() / span
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
    return points[-1][0], points[-1][1]
