"""Cumulative road usage: how many trips passed each road piece.

Built from the road-snapped lines in route_match (status matched / partial). Every pair of
consecutive points in a snapped line is one edge; the endpoints are rounded to 1e-5 degrees
(about 1 m), so the same road piece from OSRM lines up across trips. A trip counts once per
edge (driving the same road twice in one trip is still one pass).

Incremental: a trip is added once (road_usage_trips keeps which match it was built from). When
a trip's match changes (re-matched) or the trip disappears, its KST year is rebuilt.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

SCALE = 100000
LEVELS = ((1, 1), (2, 3), (4, 9), (10, None))  # pass-count buckets, light to dark

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS road_usage (
    vehicle_id TEXT NOT NULL,
    year TEXT NOT NULL,
    a_lat INTEGER NOT NULL,
    a_lon INTEGER NOT NULL,
    b_lat INTEGER NOT NULL,
    b_lon INTEGER NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (vehicle_id, year, a_lat, a_lon, b_lat, b_lon)
);
CREATE TABLE IF NOT EXISTS road_usage_trips (
    vehicle_id TEXT NOT NULL,
    trip_start_ts TEXT NOT NULL,
    year TEXT NOT NULL,
    sig TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, trip_start_ts)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _kst_year(ts: str) -> str:
    from datetime import datetime, timezone

    s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str((dt.astimezone(timezone.utc) + timedelta(hours=9)).year)


def trip_edges(segments: list[list[list[float]]]) -> set[tuple[int, int, int, int]]:
    out: set[tuple[int, int, int, int]] = set()
    for line in segments or []:
        prev = None
        for p in line:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            cur = (int(round(float(p[0]) * SCALE)), int(round(float(p[1]) * SCALE)))
            if prev is not None and cur != prev:
                a, b = (prev, cur) if prev < cur else (cur, prev)
                out.add((a[0], a[1], b[0], b[1]))
            prev = cur
    return out


def _state(conn: sqlite3.Connection, vid: str) -> str:
    row = conn.execute(
        "SELECT COUNT(*), MAX(fetched_at), MAX(trip_start_ts) FROM route_match WHERE vehicle_id = ? "
        "AND status IN ('matched', 'partial')", (vid,),
    ).fetchone()
    t = conn.execute("SELECT COUNT(*) FROM trips WHERE vehicle_id = ?", (vid,)).fetchone()
    return f"{row[0]}:{row[1]}:{row[2]}:{t[0]}"


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM process_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


EDGE_BATCH = 400


def _add(conn: sqlite3.Connection, vid: str, year: str, edges: set[tuple[int, int, int, int]]) -> None:
    """Write edges in short transactions so a year rebuild cannot hold the db for the whole job."""
    batch: list[tuple] = []
    for e in edges:
        batch.append((vid, year, *e))
        if len(batch) >= EDGE_BATCH:
            with conn:
                conn.executemany(
                    "INSERT INTO road_usage (vehicle_id, year, a_lat, a_lon, b_lat, b_lon, n) VALUES (?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(vehicle_id, year, a_lat, a_lon, b_lat, b_lon) DO UPDATE SET n = n + 1",
                    batch,
                )
            batch = []
    if batch:
        with conn:
            conn.executemany(
                "INSERT INTO road_usage (vehicle_id, year, a_lat, a_lon, b_lat, b_lon, n) VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(vehicle_id, year, a_lat, a_lon, b_lat, b_lon) DO UPDATE SET n = n + 1",
                batch,
            )


def update_road_usage(conn: sqlite3.Connection, vid: str, force: bool = False) -> dict[str, int]:
    """Add newly matched trips; rebuild a year when one of its trips changed. Cheap when nothing changed."""
    ensure_schema(conn)
    key = f"roads:{vid}"
    try:
        state = _state(conn, vid)
    except sqlite3.OperationalError:
        return {"added": 0, "rebuilt_years": 0}
    if not force and _meta(conn, key) == state:
        return {"added": 0, "rebuilt_years": 0}
    rows = conn.execute(
        "SELECT r.trip_start_ts, r.points_sig || ':' || COALESCE(r.fetched_at, ''), r.segments FROM route_match r "
        "JOIN trips t ON t.vehicle_id = r.vehicle_id AND t.start_ts = r.trip_start_ts "
        "WHERE r.vehicle_id = ? AND r.status IN ('matched', 'partial') AND r.segments IS NOT NULL",
        (vid,),
    ).fetchall()
    current = {r[0]: (r[1], r[2]) for r in rows}
    done = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT trip_start_ts, year, sig FROM road_usage_trips WHERE vehicle_id = ?", (vid,)).fetchall()}
    stale_years = {y for start, (y, sig) in done.items() if start not in current or current[start][0] != sig}
    if force:
        stale_years |= {y for y, _s in done.values()}
    added = 0
    with conn:
        for y in stale_years:
            conn.execute("DELETE FROM road_usage WHERE vehicle_id = ? AND year = ?", (vid, y))
            conn.execute("DELETE FROM road_usage_trips WHERE vehicle_id = ? AND year = ?", (vid, y))
    kept = {s for s, (y, _sig) in done.items() if y not in stale_years}
    for start, (sig, seg_json) in sorted(current.items()):
        if start in kept:
            continue
        try:
            segs = json.loads(seg_json)
        except ValueError:
            continue
        year = _kst_year(start)
        _add(conn, vid, year, trip_edges(segs))
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO road_usage_trips (vehicle_id, trip_start_ts, year, sig) VALUES (?, ?, ?, ?)",
                (vid, start, year, sig),
            )
        added += 1
    with conn:
        conn.execute("INSERT OR REPLACE INTO process_meta (key, value) VALUES (?, ?)", (key, state))
    return {"added": added, "rebuilt_years": len(stale_years)}


def _level(n: int) -> int:
    for i, (lo, hi) in enumerate(LEVELS):
        if n >= lo and (hi is None or n <= hi):
            return i
    return len(LEVELS) - 1


def _chain(edges: list[tuple[tuple[int, int], tuple[int, int]]]) -> list[list[tuple[int, int]]]:
    """Join edges that share endpoints into longer polylines (fewer, smaller map objects)."""
    adj: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in enumerate(edges):
        adj.setdefault(a, []).append(i)
        adj.setdefault(b, []).append(i)
    used = [False] * len(edges)

    def walk(node: tuple[int, int], line: list[tuple[int, int]]) -> None:
        while True:
            nxt = [i for i in adj.get(node, ()) if not used[i]]
            if len(adj.get(node, ())) != 2 or not nxt:
                return
            i = nxt[0]
            used[i] = True
            a, b = edges[i]
            node = b if a == node else a
            line.append(node)

    lines = []
    order = sorted(range(len(edges)), key=lambda i: (len(adj[edges[i][0]]) == 2 and len(adj[edges[i][1]]) == 2))
    for i in order:
        if used[i]:
            continue
        used[i] = True
        a, b = edges[i]
        fwd = [a, b]
        walk(b, fwd)
        back = [a]
        walk(a, back)
        lines.append(list(reversed(back[1:])) + fwd)
    return lines


def road_map(conn: sqlite3.Connection, vid: str, year: str | None = None) -> dict[str, Any]:
    """Edges with pass counts for one KST year (or all years), grouped into darkness levels."""
    params: list[Any] = [vid]
    where = "vehicle_id = ?"
    if year:
        where += " AND year = ?"
        params.append(year)
    try:
        rows = conn.execute(
            f"SELECT a_lat, a_lon, b_lat, b_lon, SUM(n) FROM road_usage WHERE {where} "
            f"GROUP BY a_lat, a_lon, b_lat, b_lon", params,
        ).fetchall()
        trips = conn.execute(f"SELECT COUNT(*) FROM road_usage_trips WHERE {where}", params).fetchone()[0]
    except sqlite3.OperationalError:
        rows, trips = [], 0
    by_level: dict[int, list[tuple[tuple[int, int], tuple[int, int]]]] = {}
    max_n = 0
    lat_min = lon_min = lat_max = lon_max = None
    for a_lat, a_lon, b_lat, b_lon, n in rows:
        max_n = max(max_n, int(n))
        by_level.setdefault(_level(int(n)), []).append(((a_lat, a_lon), (b_lat, b_lon)))
        for la, lo in ((a_lat, a_lon), (b_lat, b_lon)):
            lat_min = la if lat_min is None else min(lat_min, la)
            lat_max = la if lat_max is None else max(lat_max, la)
            lon_min = lo if lon_min is None else min(lon_min, lo)
            lon_max = lo if lon_max is None else max(lon_max, lo)
    levels = []
    for i, (lo, hi) in enumerate(LEVELS):
        edges = by_level.get(i, [])
        lines = [[[p[0] / SCALE, p[1] / SCALE] for p in line] for line in _chain(edges)] if edges else []
        levels.append({"min": lo, "max": hi, "edges": len(edges), "lines": lines})
    bounds = None
    if lat_min is not None:
        bounds = [[lat_min / SCALE, lon_min / SCALE], [lat_max / SCALE, lon_max / SCALE]]
    return {"vehicle_id": vid, "year": year, "trips": int(trips), "max_n": max_n, "levels": levels, "bounds": bounds}
