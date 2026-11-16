"""Where each charge happened, and names for charge spots.

charge_locations keeps one point per charge, apart from the charges table so that
reprocessing never overwrites it. A row that already exists, including source
'station' or 'manual', is never deleted and its source and coordinates never
change, even after the saved station rows are gone. Only a charge with no row
yet is filled. Order for that new row:

  source 'manual'     moved by hand in the charge sheet; never replaced automatically.
  source 'station'    the charge was saved with the name of a stored place (station) that has
                      coordinates: those coordinates. Not an estimate.
  source 'car'        the last Location the car sent before the charge started
                      (the bridge keeps Location only while driving, so this is where it parked)
  source 'estimated'  no Location in the raw signals (older charges): the end of the last
                      trip before the charge. Shown with '약'.

'station' comes before 'car' on purpose: 'car' is in practice the end of the last trip (the
bridge drops Location while parked), whereas a saved place is what the owner said.

A station without coordinates takes the representative point of the charges saved under its
name (the spot where most of them lie, 100 m clusters). A charge with no saved place text gets
the name of the nearest station within STATION_RADIUS_M.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from process.geo import distance_m

STATION_RADIUS_M = 150.0
SOURCES = ("manual", "station", "car", "estimated")
CLUSTER_M = 100.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS charge_locations (
    vehicle_id TEXT NOT NULL,
    charge_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    source TEXT NOT NULL,
    updated_ts TEXT,
    PRIMARY KEY (vehicle_id, charge_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(stations)").fetchall()}
    except sqlite3.OperationalError:
        have = set()
    if have:
        for col in ("lat", "lon"):
            if col not in have:
                conn.execute(f"ALTER TABLE stations ADD COLUMN {col} REAL")


def _latlon(raw: Any) -> tuple[float, float] | None:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if isinstance(v, dict):
        try:
            lat, lon = float(v.get("lat", v.get("latitude"))), float(v.get("lon", v.get("longitude")))
        except (TypeError, ValueError):
            return None
        return lat, lon
    return None


def _last_location(conn: sqlite3.Connection, vid: str, ts: str) -> tuple[float, float] | None:
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Location' AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1", (vid, ts),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return _latlon(row[0]) if row else None


def _first_signal_ts(conn: sqlite3.Connection, vid: str) -> str | None:
    try:
        row = conn.execute("SELECT MIN(ts) FROM signals WHERE vehicle_id = ? AND name = 'Location'", (vid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _trip_end_before(conn: sqlite3.Connection, vid: str, ts: str) -> tuple[float, float] | None:
    try:
        row = conn.execute(
            "SELECT end_lat, end_lon FROM trips WHERE vehicle_id = ? AND end_ts <= ? "
            "AND end_lat IS NOT NULL AND end_lon IS NOT NULL ORDER BY end_ts DESC LIMIT 1", (vid, ts),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0], row[1]) if row else None


def _station_coords_by_name(conn: sqlite3.Connection, vid: str) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    try:
        rows = conn.execute(
            "SELECT name, address_text, lat, lon FROM stations WHERE vehicle_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for name, addr, lat, lon in rows:
        for key in (name, addr):
            if key and key.strip():
                out.setdefault(key.strip(), (lat, lon))
    return out


def _meta_places(conn: sqlite3.Connection, vid: str) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT charge_id, location_text, station_name FROM charge_meta WHERE vehicle_id = ?", (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: (r[1] or r[2] or "").strip() for r in rows if (r[1] or r[2] or "").strip()}


def _auto_point(conn: sqlite3.Connection, vid: str, start: str, first_loc: str | None):
    """car (raw Location at the start) > estimated (previous trip end) > None."""
    if first_loc and first_loc <= start:
        pos = _last_location(conn, vid, start)
        if pos:
            return pos, "car"
    pos = _trip_end_before(conn, vid, start)
    return (pos, "estimated") if pos else (None, None)


def fill_charge_locations(conn: sqlite3.Connection, vid: str) -> dict[str, int]:
    """Add a point only for charges that do not have one.

    Existing rows stay as stored: source and coordinates are not updated, and
    nothing is deleted. A new row still prefers a saved place with coordinates
    ('station'), then the car, then the previous trip end. Returns how many
    rows were written per source, plus 'none' for charges that still have no point.
    """
    ensure_schema(conn)
    counts = {"station": 0, "car": 0, "estimated": 0, "none": 0}
    try:
        charges = [r[0] for r in conn.execute(
            "SELECT start_ts FROM charges WHERE vehicle_id = ? ORDER BY start_ts", (vid,)).fetchall()]
    except sqlite3.OperationalError:
        return counts
    if not charges:
        return counts
    have = {r[0] for r in conn.execute(
        "SELECT charge_id FROM charge_locations WHERE vehicle_id = ?", (vid,)).fetchall()}
    names = _meta_places(conn, vid)
    stations = _station_coords_by_name(conn, vid)
    first_loc = None
    first_loc_read = False
    upserts: list[tuple] = []
    for start in charges:
        if start in have:
            continue
        st = stations.get(names.get(start, ""))
        if st:
            pos = (round(st[0], 6), round(st[1], 6))
            upserts.append((vid, start, pos[0], pos[1], "station", _now()))
            counts["station"] += 1
            continue
        if not first_loc_read:
            first_loc, first_loc_read = _first_signal_ts(conn, vid), True
        pos, source = _auto_point(conn, vid, start, first_loc)
        if pos is None:
            counts["none"] += 1
            continue
        upserts.append((vid, start, round(pos[0], 6), round(pos[1], 6), source, _now()))
        counts[source] += 1
    if upserts:
        with conn:
            conn.executemany(
                "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source, updated_ts) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(vehicle_id, charge_id) DO NOTHING",
                upserts,
            )
    return counts


def locations_for(conn: sqlite3.Connection, vid: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not ids:
        return out
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT charge_id, lat, lon, source FROM charge_locations WHERE vehicle_id = ? AND charge_id IN ({marks})",
                [vid, *chunk],
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        for cid, lat, lon, src in rows:
            out[cid] = {"lat": lat, "lon": lon, "source": src}
    return out


def set_location(conn: sqlite3.Connection, vid: str, body: dict[str, Any]) -> dict[str, Any]:
    """PUT /api/charges/location: {charge_id, lat, lon} moves the point (manual);
    {charge_id, reset: true} drops a manual point so the automatic one comes back."""
    ensure_schema(conn)
    cid = str(body.get("charge_id") or "").strip()
    if not cid:
        raise ValueError("charge_id is required")
    if conn.execute("SELECT 1 FROM charges WHERE vehicle_id = ? AND start_ts = ?", (vid, cid)).fetchone() is None:
        raise KeyError(f"charge not found: {cid}")
    if body.get("reset"):
        with conn:
            conn.execute("DELETE FROM charge_locations WHERE vehicle_id = ? AND charge_id = ?", (vid, cid))
        fill_charge_locations(conn, vid)
        return {"charge_id": cid, **(locations_for(conn, vid, [cid]).get(cid) or {"lat": None, "lon": None, "source": None})}
    try:
        lat, lon = float(body.get("lat")), float(body.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("lat and lon must be numbers") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    with conn:
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source, updated_ts) VALUES (?, ?, ?, ?, 'manual', ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET lat = excluded.lat, lon = excluded.lon, "
            "source = 'manual', updated_ts = excluded.updated_ts",
            (vid, cid, round(lat, 6), round(lon, 6), _now()),
        )
    return {"charge_id": cid, "lat": round(lat, 6), "lon": round(lon, 6), "source": "manual"}


def stations_with_coords(conn: sqlite3.Connection, vid: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, name, operator, address_text, lat, lon FROM stations WHERE vehicle_id = ? "
            "AND lat IS NOT NULL AND lon IS NOT NULL", (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": r[0], "name": r[1], "operator": r[2], "address_text": r[3], "lat": r[4], "lon": r[5]} for r in rows]


def nearest_station(stations: list[dict[str, Any]], lat: Any, lon: Any) -> dict[str, Any] | None:
    if lat is None or lon is None:
        return None
    best = None
    for s in stations:
        d = distance_m((lat, lon), (s["lat"], s["lon"]))
        if d <= STATION_RADIUS_M and (best is None or d < best[0]):
            best = (d, s)
    return best[1] if best else None


def representative_point(points: list[tuple[float, float]], radius_m: float = CLUSTER_M) -> tuple[float, float] | None:
    """The spot where most points lie: the point with the most neighbours within radius_m
    (earliest wins a tie), then the mean of that neighbourhood."""
    if not points:
        return None
    best = None
    for p in points:
        near = [q for q in points if distance_m(p, q) <= radius_m]
        if best is None or len(near) > len(best):
            best = near
    return (round(sum(q[0] for q in best) / len(best), 6), round(sum(q[1] for q in best) / len(best), 6))


def station_coords_from_charges(conn: sqlite3.Connection, vid: str) -> int:
    """Stations without coordinates take the representative point of the charges saved under
    their name (manual, car or estimated points). Returns stations updated."""
    ensure_schema(conn)
    try:
        rows = conn.execute(
            "SELECT id, name, address_text FROM stations WHERE vehicle_id = ? AND (lat IS NULL OR lon IS NULL)",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    names = _meta_places(conn, vid)
    pts = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT charge_id, lat, lon FROM charge_locations WHERE vehicle_id = ? AND source IN ('manual', 'car', 'estimated')",
        (vid,)).fetchall()}
    n = 0
    for sid, name, addr in rows:
        keys = {x.strip() for x in (name, addr) if x and x.strip()}
        if not keys:
            continue
        points = [pts[cid] for cid, nm in sorted(names.items()) if nm in keys and cid in pts]
        rep = representative_point(points)
        if rep:
            with conn:
                conn.execute("UPDATE stations SET lat = ?, lon = ? WHERE vehicle_id = ? AND id = ?", (rep[0], rep[1], vid, sid))
            n += 1
    return n
