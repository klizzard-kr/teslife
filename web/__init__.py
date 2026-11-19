"""Web dashboard for teslife (Stage 3 reads + Stage 4 ledger writes)."""

from __future__ import annotations

import argparse
import json
import re
import select
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ledger import (
    charging_stats,
    connect as open_writable,
    cost_per_km,
    charging_cost,
    csv_export,
    create_manual_entry,
    create_station,
    delete_manual_entry,
    delete_rate,
    delete_station,
    list_charge_meta,
    list_ledger,
    list_rates,
    list_stations,
    ownership_total,
    count_ledger,
    report_by_period,
    seed_insurance,
    update_manual_entry,
    update_station,
    upsert_charge_meta,
    upsert_rate,
    ledger_summary,
    parse_day_range,
)
from ledger.consumables import (
    create_item,
    deactivate_item,
    delete_replacement,
    due_items,
    get_registration_date,
    list_items,
    list_replacements,
    list_vendors,
    save_replacement,
    seed_consumables,
    set_registration_date,
    update_item,
    update_replacement,
)
from process import (
    charge_status,
    is_charging,
    KST,
    Events,
    open_charge_start,
    open_trip_start,
    charge_energy,
    estimated_full,
    sentry_minutes,
    charge_curve,
    series_avg_max,
    decode_value as _decode_signal,
    ensure_process_schema,
    format_ts,
    parse_ts,
    power_series,
    process_db,
    _as_float,
)
from process.geo import distance_m, simplify
from .graphseries import build_battery, build_climate, charge_axis_top, series_segments
from process.geocode import cache_key
from process.match import cached_lines

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Vendored Leaflet 1.9.4 (BSD-2-Clause) for the route map; tiles come from OpenStreetMap.
# Brand art is in static/brand. Root paths match what browsers request without a tag.
STATIC_FILES = {
    "/static/i18n.js": ("i18n.js", "application/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/static/leaflet.js": ("vendor/leaflet/leaflet.js", "application/javascript; charset=utf-8"),
    "/static/naver-map.js": ("naver-map.js", "application/javascript; charset=utf-8"),
    "/static/leaflet.css": ("vendor/leaflet/leaflet.css", "text/css; charset=utf-8"),
    "/favicon.ico": ("brand/favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("brand/apple-touch-icon.png", "image/png"),
    "/static/favicon.ico": ("brand/favicon.ico", "image/x-icon"),
    "/static/favicon-16.png": ("brand/favicon-16.png", "image/png"),
    "/static/favicon-32.png": ("brand/favicon-32.png", "image/png"),
    "/static/apple-touch-icon.png": ("brand/apple-touch-icon.png", "image/png"),
    "/static/icon-192.png": ("brand/icon-192.png", "image/png"),
    "/static/icon-512.png": ("brand/icon-512.png", "image/png"),
    "/static/logo-64.png": ("brand/logo-64.png", "image/png"),
    "/static/logo-128.png": ("brand/logo-128.png", "image/png"),
    "/static/teslife-logo-1024.png": ("brand/teslife-logo-1024.png", "image/png"),
}
DEFAULT_TIRE_ALERT_BAR = 2.6
DEFAULT_PROCESS_INTERVAL = 30.0
WHEEL_KEYS = ("fl", "fr", "rl", "rr")
WHEEL_SIGNALS = {
    "fl": "TpmsPressureFl",
    "fr": "TpmsPressureFr",
    "rl": "TpmsPressureRl",
    "rr": "TpmsPressureRr",
}


def decode_value(raw: str) -> Any:
    return json.loads(raw)


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error:
        pass
    return conn


def cold_tire_alert(
    reading: dict[str, Any] | None, threshold: float
) -> dict[str, Any]:
    """Warn only from the latest COLD reading. Warm values never trigger."""
    if not reading:
        return {"alert": False, "wheels": [], "threshold": threshold}
    low: list[str] = []
    for wheel in WHEEL_KEYS:
        val = reading.get(wheel)
        if isinstance(val, (int, float)) and val < threshold:
            low.append(wheel)
    return {
        "alert": bool(low),
        "wheels": low,
        "threshold": threshold,
        "ts": reading.get("ts"),
        "phase": reading.get("phase"),
    }


def recommended_tire_alert(
    reading: dict[str, Any] | None, settings: dict[str, Any]
) -> dict[str, Any]:
    """Latest COLD reading. A wheel is low when its printed psi is under 39.0.

    Warm values never trigger. The 42 psi label stays on the payload as information.
    """
    from ledger.tires import LOW_PSI, PSI_PER_BAR, low_wheels

    rec = settings["recommended"]
    low = low_wheels(reading, settings)
    return {
        "alert": bool(low),
        "wheels": [w for w, _v, _b in low],
        "below_bar": {w: round(b, 3) for w, _v, b in low},
        "threshold": round(LOW_PSI / PSI_PER_BAR, 3),
        "threshold_psi": LOW_PSI,
        "recommended": rec,
        "recommended_psi": settings["recommended_psi"],
        "ts": (reading or {}).get("ts"),
        "phase": (reading or {}).get("phase"),
    }


def _distinct_vehicles(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT vehicle_id FROM signals ORDER BY vehicle_id"
        ).fetchall()
    ]


def resolve_vehicle_id(conn: sqlite3.Connection, requested: str | None) -> str | None:
    if requested:
        return requested
    ids = _distinct_vehicles(conn)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    row = conn.execute(
        "SELECT vehicle_id FROM signals ORDER BY ts DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else ids[0]


def latest_signals(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.name, s.ts, s.value
        FROM signals s
        INNER JOIN (
            SELECT name, MAX(ts) AS ts
            FROM signals
            WHERE vehicle_id = ?
            GROUP BY name
        ) latest ON latest.name = s.name AND latest.ts = s.ts
        WHERE s.vehicle_id = ?
        """,
        (vehicle_id, vehicle_id),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["name"]] = {"ts": row["ts"], "value": decode_value(row["value"])}
    return out


def max_signal_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(rowid) FROM signals").fetchone()
    val = row[0] if row else None
    return int(val) if val is not None else 0


def in_progress_flags(signals: dict[str, dict[str, Any]]) -> tuple[bool, bool]:
    gear = (signals.get("Gear") or {}).get("value")
    detailed = (signals.get("DetailedChargeState") or {}).get("value")
    raw = (signals.get("ChargeState") or {}).get("value")
    trip = gear is not None and gear != "P"
    charging = is_charging(detailed, raw)
    return trip, charging


def live_payload(
    conn: sqlite3.Connection, vehicle_id: str, tire_alert_bar: float
) -> dict[str, Any]:
    signals = latest_signals(conn, vehicle_id)
    signals.pop("Location", None)  # position is shown only as trip routes
    trip, charging = in_progress_flags(signals)
    last_ts = max((s["ts"] for s in signals.values()), default=None)
    return {
        "vehicle_id": vehicle_id,
        "signals": signals,
        "last_ts": last_ts,
        "trip_in_progress": trip,
        "charge_in_progress": charging,
        "charge_status": charge_status(
            (signals.get("DetailedChargeState") or {}).get("value"),
            (signals.get("ChargeState") or {}).get("value"),
        ),
        "tire_alert": _displayed_tire_alert(signals),
    }


def _displayed_tire_alert(signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Home warning from the same psi the tiles show, not a colder stored reading."""
    from ledger.tires import displayed_low_wheels, get_tire_settings

    settings = get_tire_settings()
    pressures = {}
    for wheel, name in WHEEL_SIGNALS.items():
        raw = (signals.get(name) or {}).get("value")
        if isinstance(raw, (int, float)):
            pressures[wheel] = raw
    low = displayed_low_wheels(pressures, settings)
    return {
        "alert": bool(low),
        "wheels": low,
        "threshold_psi": settings["low_psi"],
        "recommended_psi": settings["recommended_psi"],
        "basis": "displayed",
    }


def _tire_alert(conn, vehicle_id, cold, tire_alert_bar) -> dict[str, Any]:
    try:
        from ledger.tires import get_tire_settings
        return recommended_tire_alert(cold, get_tire_settings(conn, vehicle_id))
    except Exception:  # noqa: BLE001 - fall back to the fixed --tire-alert-bar line
        return cold_tire_alert(cold, tire_alert_bar)


def _row_to_trip(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for key in ("tire_before", "tire_after"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except ValueError:
                out[key] = None
    if out.get("km_per_kwh") is None and out.get("kwh") and out.get("km") is not None:
        try:
            from process import MIN_TRIP_KM
            if float(out["kwh"]) > 0.01 and float(out["km"]) >= MIN_TRIP_KM:
                out["km_per_kwh"] = float(out["km"]) / float(out["kwh"])
        except (TypeError, ValueError):
            pass
    return out


def _row_to_charge(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    out["charge_id"] = row["start_ts"]
    return out


def _row_to_tire(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "vehicle_id": row["vehicle_id"],
        "ts": row["ts"],
        "fl": row["fl"],
        "fr": row["fr"],
        "rl": row["rl"],
        "rr": row["rr"],
        "outside_temp": row["outside_temp"],
        "phase": row["phase"],
    }


def latest_cold_reading(
    conn: sqlite3.Connection, vehicle_id: str
) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            """
            SELECT * FROM tire_readings
            WHERE vehicle_id = ? AND phase = 'cold'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return _row_to_tire(row) if row else None


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_YEAR_RE = re.compile(r"^\d{4}$")
_PAGE_MAX = 5000
# Stored timestamps are UTC. Calendar months are KST (UTC+9, no DST).
_TRIP_KST = "date(start_ts, '+9 hours')"
# Charges bucket on the end time, or the start time when the session has not ended.
_CHARGE_TS = "CASE WHEN end_ts IS NULL OR end_ts = '' THEN start_ts ELSE end_ts END"
_CHARGE_KST = f"date({_CHARGE_TS}, '+9 hours')"
_CHARGE_TS_C = "CASE WHEN c.end_ts IS NULL OR c.end_ts = '' THEN c.start_ts ELSE c.end_ts END"
_CHARGE_KST_C = f"date({_CHARGE_TS_C}, '+9 hours')"


def parse_month(value: str | None) -> str | None:
    if value is None:
        return None
    if not _MONTH_RE.fullmatch(value):
        raise ValueError("month must be YYYY-MM")
    return value


def parse_year(value: str | None) -> str | None:
    if value is None:
        return None
    if not _YEAR_RE.fullmatch(value):
        raise ValueError("year must be YYYY")
    return value


def parse_page_int(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d+", value):
        raise ValueError(f"{name} must be a non-negative integer")
    num = int(value)
    if name == "limit" and num > _PAGE_MAX:
        raise ValueError(f"limit must be <= {_PAGE_MAX}")
    return num


def _q1(query: dict[str, list[str]], key: str) -> str | None:
    vals = query.get(key)
    if not vals or vals[0] is None or vals[0] == "":
        return None
    return vals[0]


def _period_like(month: str | None, year: str | None) -> str | None:
    """LIKE prefix for a KST calendar period. Month wins when both are set."""
    month = parse_month(month)
    year = parse_year(year)
    if month:
        return month + "%"
    if year:
        return year + "%"
    return None


def _day_where(expr: str, date_from: str | None, date_to: str | None) -> tuple[str, list[Any]]:
    """SQL for a KST day range on a 'YYYY-MM-DD' expression (both ends inclusive)."""
    date_from, date_to = parse_day_range(date_from, date_to)
    sql, params = "", []
    if date_from:
        sql += f" AND {expr} >= ?"
        params.append(date_from)
    if date_to:
        sql += f" AND {expr} <= ?"
        params.append(date_to)
    return sql, params


def _count_where(conn: sqlite3.Connection, sql: str, params: list[Any]) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def trip_entries(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Trips for a KST period, newest first. Every trip row is its own entry (no merging)."""
    where = "vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    like = _period_like(month, year)
    if like:
        where += f" AND {_TRIP_KST} LIKE ?"
        params.append(like)
    extra, extra_params = _day_where(_TRIP_KST, date_from, date_to)
    where += extra
    params += extra_params
    page = ""
    if limit is not None:
        page = " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    try:
        rows = conn.execute(f"SELECT * FROM trips WHERE {where} ORDER BY start_ts DESC{page}", params).fetchall()
    except sqlite3.OperationalError:
        return []
    entries = [_row_to_trip(r) for r in rows]
    label_trips(conn, vehicle_id, entries)
    return entries


# ---------------------------------------------------------------- places and labels



def list_places(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, name, lat, lon, radius_m, created_at FROM places WHERE vehicle_id = ? ORDER BY name, id",
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": r[0], "name": r[1], "lat": r[2], "lon": r[3], "radius_m": r[4], "created_at": r[5]} for r in rows]


def _place_body(body: dict[str, Any]) -> tuple[str, float, float, float]:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ValueError("name is required (40 characters or fewer)")
    try:
        lat, lon = float(body.get("lat")), float(body.get("lon"))
        radius = float(body.get("radius_m") if body.get("radius_m") not in (None, "") else 150)
    except (TypeError, ValueError):
        raise ValueError("lat, lon and radius_m must be numbers") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    if not (30 <= radius <= 5000):
        raise ValueError("radius_m must be between 30 and 5000")
    return name, round(lat, 6), round(lon, 6), radius


def create_place(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_process_schema(conn)
    name, lat, lon, radius = _place_body(body)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        cur = conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (vehicle_id, name, lat, lon, radius, now),
        )
    return {"id": cur.lastrowid, "name": name, "lat": lat, "lon": lon, "radius_m": radius, "created_at": now}


def update_place(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_process_schema(conn)
    pid = body.get("id")
    row = conn.execute("SELECT name, lat, lon, radius_m FROM places WHERE vehicle_id = ? AND id = ?",
                       (vehicle_id, pid)).fetchone()
    if row is None:
        raise KeyError(f"place not found: {pid}")
    merged = {"name": row[0], "lat": row[1], "lon": row[2], "radius_m": row[3]}
    merged.update({k: v for k, v in body.items() if k in merged and v is not None})
    name, lat, lon, radius = _place_body(merged)
    with conn:
        conn.execute("UPDATE places SET name = ?, lat = ?, lon = ?, radius_m = ? WHERE vehicle_id = ? AND id = ?",
                     (name, lat, lon, radius, vehicle_id, pid))
    return {"id": pid, "name": name, "lat": lat, "lon": lon, "radius_m": radius}


def delete_place(conn: sqlite3.Connection, vehicle_id: str, pid: Any) -> None:
    ensure_process_schema(conn)
    with conn:
        cur = conn.execute("DELETE FROM places WHERE vehicle_id = ? AND id = ?", (vehicle_id, pid))
    if cur.rowcount == 0:
        raise KeyError(f"place not found: {pid}")


def _nearest_place(places: list[dict[str, Any]], lat: Any, lon: Any) -> str | None:
    if lat is None or lon is None:
        return None
    best: tuple[float, str] | None = None
    for p in places:
        d = distance_m((lat, lon), (p["lat"], p["lon"]))
        if d <= p["radius_m"] and (best is None or d < best[0]):
            best = (d, p["name"])
    return best[1] if best else None


def _geo_label(conn: sqlite3.Connection, lat: Any, lon: Any, memo: dict[str, str | None]) -> str | None:
    if lat is None or lon is None:
        return None
    from web.l10n import get_lang

    lang = get_lang()
    key = cache_key(lat, lon)
    memo_key = lang + "\0" + key
    if memo_key not in memo:
        row = None
        try:
            row = conn.execute(
                "SELECT label, label_en FROM geocode_cache WHERE key = ? "
                "AND (label IS NOT NULL OR label_en IS NOT NULL) ORDER BY fetched_at DESC LIMIT 1",
                (key,),
            ).fetchone()
        except sqlite3.OperationalError:
            try:
                row = conn.execute(
                    "SELECT label, NULL FROM geocode_cache WHERE key = ? AND label IS NOT NULL "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (key,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
        if not row:
            memo[memo_key] = None
        elif lang == "en" and row[1]:
            memo[memo_key] = row[1]
        else:
            memo[memo_key] = row[0]
    return memo[memo_key]


def _spot_address(conn: sqlite3.Connection, lat: Any, lon: Any, memo: dict[str, str | None]) -> str | None:
    """Address for a trip end. A cached street address wins; otherwise the stored area name."""
    if lat is None or lon is None:
        return None
    from process.geocode import cache_key
    key = "addr\0" + cache_key(lat, lon)
    if key not in memo:
        row = None
        try:
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE provider = 'nominatim-addr' AND key = ? AND label IS NOT NULL",
                (cache_key(lat, lon),),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        memo[key] = row[0] if row and row[0] else None
    if memo[key]:
        return memo[key]
    return _geo_label(conn, lat, lon, memo)


def label_trips(conn: sqlite3.Connection, vehicle_id: str, entries: list[dict[str, Any]]) -> None:
    """Origin and destination at 동 level.

    Uses the nominatim area cache (`admin_area` of that label). Street-level
    `nominatim-addr` rows and saved place names are not used.
    """
    from process.geocode import admin_area

    memo: dict[str, str | None] = {}
    for e in entries:
        e["from_label"] = admin_area(_geo_label(conn, e.get("start_lat"), e.get("start_lon"), memo))
        e["to_label"] = admin_area(_geo_label(conn, e.get("end_lat"), e.get("end_lon"), memo))


def charge_spot(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> tuple[Any, Any]:
    """Where the car stood for a charge: the end of the last trip that ended by its start.

    Location is only kept while driving, so this is the same point the car last reported.
    Trip end coordinates are kept for good, so old charges get a place too (no new column).
    """
    try:
        row = conn.execute(
            "SELECT end_lat, end_lon FROM trips WHERE vehicle_id = ? AND end_ts <= ? "
            "AND end_lat IS NOT NULL AND end_lon IS NOT NULL ORDER BY end_ts DESC LIMIT 1",
            (vehicle_id, start_ts),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def label_charges(conn: sqlite3.Connection, vehicle_id: str, items: list[dict[str, Any]],
                  meta_by_id: dict[str, dict[str, Any]]) -> None:
    """Add place_auto (registered place > reverse-geocoded area, like trips) and place.

    place is what the list header shows: the saved place text (charge_meta.location_text,
    older rows: station_name) when the user saved one, else place_auto.
    """
    from process.chargeloc import locations_for

    locs = locations_for(conn, vehicle_id, [item["start_ts"] for item in items])
    memo: dict[str, str | None] = {}
    for item in items:
        loc = locs.get(item["start_ts"])
        if loc:
            lat, lon = loc["lat"], loc["lon"]
        else:  # not filled yet (the process loop fills it): the previous trip's end
            lat, lon = charge_spot(conn, vehicle_id, item["start_ts"])
            loc = {"lat": lat, "lon": lon, "source": "estimated"} if lat is not None else None
        item["lat"] = loc["lat"] if loc else None
        item["lon"] = loc["lon"] if loc else None
        item["loc_source"] = loc["source"] if loc else None
        auto = _spot_address(conn, lat, lon, memo)
        meta = meta_by_id.get(item["charge_id"]) or {}
        station = " ".join(
            part for part in ((meta.get("station_name") or "").strip(), (meta.get("station_addr") or "").strip()) if part
        )
        saved = (meta.get("location_text") or "").strip()
        item["place_auto"] = station or auto
        item["station_auto"] = None
        item["place"] = saved or station or auto


def list_trips(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    return trip_entries(conn, vehicle_id, month=month, year=year, date_from=date_from, date_to=date_to,
                        limit=limit, offset=offset)


def count_trips(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where = "vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    like = _period_like(month, year)
    if like:
        where += f" AND {_TRIP_KST} LIKE ?"
        params.append(like)
    extra, extra_params = _day_where(_TRIP_KST, date_from, date_to)
    return _count_where(conn, f"SELECT COUNT(*) FROM trips WHERE {where}{extra}", params + extra_params)


def _events_between(
    conn: sqlite3.Connection, vehicle_id: str, start: str, end: str, names: tuple[str, ...]
) -> Events:
    marks = ",".join("?" * len(names))
    seed = conn.execute(
        f"""
        SELECT s.ts, s.name, s.value FROM signals s
        JOIN (SELECT name, MAX(ts) AS ts FROM signals
              WHERE vehicle_id = ? AND ts < ? AND name IN ({marks}) GROUP BY name) m
          ON m.name = s.name AND m.ts = s.ts
        WHERE s.vehicle_id = ?
        """,
        [vehicle_id, start, *names, vehicle_id],
    ).fetchall()
    rows = conn.execute(
        f"SELECT ts, name, value FROM signals WHERE vehicle_id = ? AND ts >= ? AND ts <= ? AND name IN ({marks})",
        [vehicle_id, start, end, *names],
    ).fetchall()
    parsed = []
    for ts, name, raw in list(seed) + list(rows):
        try:
            parsed.append((parse_ts(ts), name, _decode_signal(raw)))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda r: (r[0], r[1]))
    return Events(parsed)


def _downsample(points: list[Any], limit: int = 400) -> list[Any]:
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    return [points[int(i * step)] for i in range(limit)] + [points[-1]]


def _window_parts(ev: Any, name: str, start: datetime, end: datetime):
    samples = [(ts, value) for ts, value in ev.series(name, start, end)]
    hit = ev.at_ts(name, start - timedelta(microseconds=1))
    prior = hit if hit is not None and hit[0] < start else None
    return samples, prior


def _pack_segments(segs: list, key: str) -> list[list[dict[str, Any]]]:
    return [
        [{key: round(float(value), 3), "ts": format_ts(ts)} for ts, value in seg]
        for seg in segs if seg
    ]


def _pack_battery(built: dict[str, Any]) -> dict[str, Any]:
    return {
        "min": _pack_segments(built["min"], "v"),
        "max": _pack_segments(built["max"], "v"),
        "heater": [
            {"start": format_ts(a), "end": format_ts(b), "level": level}
            for a, b, level in built["heater"]
        ],
    }


_CLIMATE_NAMES = (
    "InsideTemp", "OutsideTemp",
    "HvacLeftTemperatureRequest", "HvacRightTemperatureRequest",
    "HvacFanSpeed", "HvacFanStatus",
    "HvacAutoMode", "HvacACEnabled", "HvacPower",
    "SeatHeaterLeft", "SeatHeaterRight",
    "SeatHeaterRearLeft", "SeatHeaterRearRight", "SeatHeaterRearCenter",
    "HvacSteeringWheelHeatLevel",
    "DefrostMode", "RearDefrostEnabled", "HvacRearDefrost",
    "WiperHeatEnabled", "HvacWiperHeat",
    "ModuleTempMin", "ModuleTempMax", "BatteryHeaterOn",
)


def _alias_series(series: dict, priors: dict, target: str, source: str) -> None:
    if series.get(target) or priors.get(target):
        return
    if series.get(source) or priors.get(source):
        series[target] = series.get(source) or []
        if source in priors:
            priors[target] = priors[source]


def _rated_kw(conn: sqlite3.Connection, vehicle_id: str, charge_id: str) -> float | None:
    try:
        row = conn.execute(
            "SELECT output_kw FROM charge_charger WHERE vehicle_id = ? AND charge_id = ?",
            (vehicle_id, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def trip_power(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT start_ts, end_ts FROM trips WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts)
    ).fetchone()
    if row is None:
        raise KeyError(f"trip not found: {start_ts}")
    ev = _events_between(conn, vehicle_id, row[0], row[1], ("PackVoltage", "PackCurrent", "VehicleSpeed"))
    s, e = parse_ts(row[0]), parse_ts(row[1])
    samples = power_series(ev, s, e)
    flipped = False
    if samples:
        ts0, kw0 = samples[0]
        volts = _as_float(ev.at("PackVoltage", ts0))
        amps = _as_float(ev.at("PackCurrent", ts0))
        raw = None if volts is None or amps is None else volts * amps / 1000.0
        if raw is not None and abs(raw + kw0) + 1e-9 < abs(raw - kw0):
            flipped = True
    prior = None
    hit_v = ev.at_ts("PackVoltage", s - timedelta(microseconds=1))
    hit_i = ev.at_ts("PackCurrent", s - timedelta(microseconds=1))
    if hit_v and hit_i and hit_v[0] < s and hit_i[0] < s:
        when = max(hit_v[0], hit_i[0])
        volts = _as_float(ev.at("PackVoltage", when))
        amps = _as_float(ev.at("PackCurrent", when))
        if volts is not None and amps is not None:
            kw = volts * amps / 1000.0
            prior = (when, -kw if flipped else kw)
    segs = series_segments(samples, s, prior)
    flat = [point for seg in segs for point in seg]
    pts = [{"ts": format_ts(t), "kw": round(p, 2)} for t, p in flat]
    return {
        "vehicle_id": vehicle_id, "start_ts": row[0], "end_ts": row[1],
        "points": pts, "segments": _pack_segments(segs, "kw"),
    }


def trip_speed(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> dict[str, Any]:
    """VehicleSpeed samples and hard accel/brake times. Reads only."""
    row = conn.execute(
        "SELECT start_ts, end_ts FROM trips WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts)
    ).fetchone()
    if row is None:
        raise KeyError(f"trip not found: {start_ts}")
    ev = _events_between(conn, vehicle_id, row[0], row[1], ("VehicleSpeed",))
    s, e = parse_ts(row[0]), parse_ts(row[1])
    samples = ev.nums("VehicleSpeed", s, e, include_start=False)
    hit = ev.at_ts("VehicleSpeed", s - timedelta(microseconds=1))
    prior = None
    if hit and hit[0] < s:
        speed = _as_float(hit[1])
        if speed is not None:
            prior = (hit[0], speed)
    segs = series_segments(samples, s, prior)
    flat = [point for seg in segs for point in seg]
    pts = [{"ts": format_ts(t), "kph": round(v, 1)} for t, v in flat]
    from process.speed_events import event_details
    details = event_details(samples)
    if details:
        marks = [
            {
                "kind": item["kind"],
                "ts": format_ts(item["ts"]),
                "speed_before": item["speed_before"],
                "speed_after": item["speed_after"],
                "delta_kph": item["delta_kph"],
                "duration_s": item["duration_s"],
                "rate_kmh_s": item["rate_kmh_s"],
            }
            for item in details
        ]
    else:
        marks = [
            {
                "ts": event["ts"],
                "kind": event["kind"],
                "rate_kmh_s": event["g"],
            }
            for event in trip_events(conn, vehicle_id, [row[0]])
            if event["kind"] in ("accel", "brake")
        ]
    return {
        "vehicle_id": vehicle_id,
        "start_ts": row[0],
        "end_ts": row[1],
        "points": pts,
        "segments": _pack_segments(segs, "kph"),
        "events": marks,
    }


def trip_climate(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> dict[str, Any]:
    """Climate lines, heater bars, and the battery-temperature card for one trip."""
    row = conn.execute(
        "SELECT start_ts, end_ts FROM trips WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts)
    ).fetchone()
    if row is None:
        raise KeyError(f"trip not found: {start_ts}")
    ev = _events_between(conn, vehicle_id, row[0], row[1], _CLIMATE_NAMES)
    start, end = parse_ts(row[0]), parse_ts(row[1])
    series: dict[str, list] = {}
    priors: dict[str, tuple] = {}
    for name in _CLIMATE_NAMES:
        samples, prior = _window_parts(ev, name, start, end)
        if samples:
            series[name] = samples
        if prior is not None:
            priors[name] = prior
    _alias_series(series, priors, "RearDefrostEnabled", "HvacRearDefrost")
    _alias_series(series, priors, "WiperHeatEnabled", "HvacWiperHeat")
    climate = build_climate(series, priors, start, end)
    battery = build_battery(series, priors, start, end)
    bars = {
        key: [
            {"start": format_ts(a), "end": format_ts(b), "level": level}
            for a, b, level in spans
        ]
        for key, spans in climate["bars"].items()
    }
    return {
        "vehicle_id": vehicle_id, "start_ts": row[0], "end_ts": row[1],
        "inside": _pack_segments(climate["inside"], "v"),
        "outside": _pack_segments(climate["outside"], "v"),
        "set_driver": _pack_segments(climate["set_driver"], "v"),
        "set_passenger": _pack_segments(climate["set_passenger"], "v"),
        "fan": _pack_segments(climate["fan"], "v"),
        "auto": [{"ts": format_ts(t), "v": v} for t, v in climate["auto"]],
        "ac": [{"ts": format_ts(t), "v": v} for t, v in climate["ac"]],
        "bars": bars,
        "battery": _pack_battery(battery),
    }


ROUTE_TOLERANCE_M = 8.0  # per-trip and overlay routes
CUMULATIVE_TOLERANCE_M = 25.0  # cumulative map
MAX_OVERLAY_ROUTES = 100
MAX_CUMULATIVE_TRIPS = 5000


def _trip_lines(
    conn: sqlite3.Connection, vehicle_id: str, start: str, tolerance_m: float, digits: int = 6
) -> tuple[list[list[list[float]]], bool]:
    """Simplified [[lat, lon], ...] polylines of one trip and whether they are road-snapped.

    Uses the cached map-matched line when it is for the current points; otherwise the
    straight lines between the recorded points.
    """
    try:
        rows = conn.execute(
            "SELECT lat, lon FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
            (vehicle_id, start),
        ).fetchall()
    except sqlite3.OperationalError:
        return [], False
    if not rows:
        return [], False
    raw = [(r[0], r[1]) for r in rows]
    snapped = cached_lines(conn, vehicle_id, start, raw)
    lines = snapped if snapped else [raw]
    out = []
    for line in lines:
        pts = simplify([(p[0], p[1]) for p in line], tolerance_m)
        if len(pts) >= 1:
            out.append([[round(p[0], digits), round(p[1], digits)] for p in pts])
    return out, bool(snapped)


def _route_segments(
    conn: sqlite3.Connection, vehicle_id: str, starts: list[str], tolerance_m: float, digits: int = 6
) -> list[list[list[float]]]:
    """Polylines of the given trips in order (trips without points are skipped)."""
    out: list[list[list[float]]] = []
    for start in starts:
        out.extend(_trip_lines(conn, vehicle_id, start, tolerance_m, digits)[0])
    return out


def _matched_any(conn: sqlite3.Connection, vehicle_id: str, starts: list[str]) -> bool:
    return any(_trip_lines(conn, vehicle_id, s, ROUTE_TOLERANCE_M)[1] for s in starts)


EVENT_KINDS = ("accel", "brake", "turn")


def trip_events(conn: sqlite3.Connection, vehicle_id: str, starts: list[str]) -> list[dict[str, Any]]:
    """Harsh events of the given trips with their approximate position, in time order."""
    if not starts:
        return []
    marks = ",".join("?" * len(starts))
    try:
        rows = conn.execute(
            f"SELECT trip_start_ts, ts, kind, g, lat, lon FROM trip_events WHERE vehicle_id = ? "
            f"AND trip_start_ts IN ({marks}) ORDER BY ts",
            [vehicle_id] + list(starts),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"trip_start_ts": r[0], "ts": r[1], "kind": r[2], "g": r[3], "lat": r[4], "lon": r[5]} for r in rows]


def _bounds(segments: list[list[list[float]]]) -> list[list[float]] | None:
    pts = [p for seg in segments for p in seg]
    if not pts:
        return None
    return [[min(p[0] for p in pts), min(p[1] for p in pts)], [max(p[0] for p in pts), max(p[1] for p in pts)]]


def trip_trace(conn: sqlite3.Connection, vehicle_id: str, start: str) -> list[dict[str, Any]]:
    """Ordered route points. speed_kph and gear are included only when stored.

    Autopilot is never added: route points do not store it.
    """
    try:
        rows = conn.execute(
            "SELECT lat, lon, ts, speed_kph, gear FROM trip_points "
            "WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
            (vehicle_id, start),
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(r[0], r[1], r[2], None, None) for r in conn.execute(
                "SELECT lat, lon, ts FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
                (vehicle_id, start),
            ).fetchall()]
        except sqlite3.OperationalError:
            return []
    out: list[dict[str, Any]] = []
    for lat, lon, ts, speed, gear in rows:
        item: dict[str, Any] = {"lat": lat, "lon": lon, "ts": ts}
        if speed is not None:
            item["speed_kph"] = round(float(speed), 1)
        if gear:
            item["gear"] = gear
        out.append(item)
    return out


def trip_route(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> dict[str, Any]:
    """Route of one trip."""
    row = conn.execute(
        "SELECT start_ts, end_ts FROM trips WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts)
    ).fetchone()
    if row is None:
        raise KeyError(f"trip not found: {start_ts}")
    members = [start_ts]
    segments = _route_segments(conn, vehicle_id, members, ROUTE_TOLERANCE_M)
    return {
        "vehicle_id": vehicle_id,
        "start_ts": start_ts,
        "segments": segments,
        "bounds": _bounds(segments),
        "matched": _matched_any(conn, vehicle_id, members),
        "events": trip_events(conn, vehicle_id, members),
        "points": trip_trace(conn, vehicle_id, start_ts),
    }


def map_routes(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    recent: int | None = None,
) -> dict[str, Any]:
    """Routes to overlay: every trip in a KST month/year/day range, or the newest `recent`. Newest first."""
    if not parse_month(month) and not parse_year(year) and not date_from and not date_to and not recent:
        raise ValueError("month, year, from/to or recent is required")
    entries = trip_entries(conn, vehicle_id, month=month, year=year, date_from=date_from, date_to=date_to,
                           limit=recent if recent else MAX_OVERLAY_ROUTES + 1)
    routes = []
    for e in entries[:MAX_OVERLAY_ROUTES]:
        members = [e["start_ts"]]
        segments = _route_segments(conn, vehicle_id, members, ROUTE_TOLERANCE_M)
        routes.append({
            "start_ts": e["start_ts"], "end_ts": e["end_ts"], "km": e.get("km"),
            "from_label": e.get("from_label"), "to_label": e.get("to_label"),
            "km_per_kwh": e.get("km_per_kwh"), "segments": segments,
            "matched": _matched_any(conn, vehicle_id, members),
            "events": trip_events(conn, vehicle_id, members),
            "points": trip_trace(conn, vehicle_id, e["start_ts"]),
        })
    all_segments = [seg for r in routes for seg in r["segments"]]
    return {
        "vehicle_id": vehicle_id,
        "month": parse_month(month),
        "year": None if parse_month(month) else parse_year(year),
        "routes": routes,
        "truncated": len(entries) > MAX_OVERLAY_ROUTES,
        "bounds": _bounds(all_segments),
    }


MAP_PROVIDERS = (
    # id, label, env var holding the key (None = no key).
    # Naver Dynamic Map is drawn when NAVER_MAPS_CLIENT_ID is set. OSM stays when it is absent.
    ("osm", "OSM", None),
    ("kakao", "카카오", "TESLIFE_MAP_KAKAO_KEY"),
    ("naver", "네이버", "TESLIFE_MAP_NAVER_KEY"),
)
MAP_IMPLEMENTED = {"osm"}


def map_config() -> dict[str, Any]:
    """Map providers the page may offer.

    OSM needs no key. Naver Dynamic Map needs NAVER_MAPS_CLIENT_ID in the page
    as ncpKeyId; that id is the public map key. The map secret and both search
    keys are never included.
    """
    import os

    providers = []
    for pid, label, env in MAP_PROVIDERS:
        if pid not in MAP_IMPLEMENTED:
            continue
        if env and not os.environ.get(env, "").strip():
            continue
        providers.append({"id": pid, "label": label})
    client = os.environ.get("NAVER_MAPS_CLIENT_ID", "").strip()
    out: dict[str, Any] = {"default": "osm", "providers": providers}
    if client:
        providers.insert(0, {"id": "naver", "label": "네이버"})
        out["default"] = "naver"
        out["ncpKeyId"] = client
    return out


def map_cumulative(conn: sqlite3.Connection, vehicle_id: str, year: str | None = None) -> dict[str, Any]:
    """Every recorded route (optionally one KST year), simplified for one map."""
    params: list[Any] = [vehicle_id]
    where = "vehicle_id = ?"
    y = parse_year(year)
    if y:
        where += f" AND {_TRIP_KST} LIKE ?"
        params.append(y + "%")
    try:
        starts = [r[0] for r in conn.execute(
            f"SELECT start_ts FROM trips WHERE {where} AND start_ts IN "
            "(SELECT DISTINCT trip_start_ts FROM trip_points WHERE vehicle_id = ?) "
            "ORDER BY start_ts DESC LIMIT ?",
            params + [vehicle_id, MAX_CUMULATIVE_TRIPS],
        ).fetchall()]
    except sqlite3.OperationalError:
        starts = []
    segments: list[list[list[float]]] = []
    with_lines = 0
    for start in starts:
        lines = _trip_lines(conn, vehicle_id, start, CUMULATIVE_TOLERANCE_M, digits=5)[0]
        if lines:
            with_lines += 1
            segments.extend(lines)
    km = 0.0
    if starts:
        marks = ",".join("?" * len(starts))
        km = conn.execute(
            f"SELECT COALESCE(SUM(km), 0) FROM trips WHERE vehicle_id = ? AND start_ts IN ({marks})",
            [vehicle_id] + starts,
        ).fetchone()[0]
    return {
        "vehicle_id": vehicle_id, "year": y, "trips": with_lines, "km": km,
        "segments": segments, "bounds": _bounds(segments),
    }


def charge_detail_curve(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT start_ts, end_ts, kind FROM charges WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts)
    ).fetchone()
    if row is None:
        raise KeyError(f"charge not found: {start_ts}")
    names = (
        "ACChargingPower", "DCChargingPower", "ChargerVoltage", "ChargeAmps", "BatteryLevel",
        "ModuleTempMin", "ModuleTempMax", "BatteryHeaterOn",
    )
    ev = _events_between(conn, vehicle_id, row[0], row[1], names)
    start, end = parse_ts(row[0]), parse_ts(row[1])
    pts = charge_curve(ev, start, end, row[2])
    samples = [(parse_ts(p["ts"]), float(p["kw"])) for p in pts if p["kw"] is not None]
    avg_kw, max_kw = series_avg_max(samples, end)
    power_name = "DCChargingPower" if row[2] == "DC" else "ACChargingPower"
    _power_samples, power_prior = _window_parts(ev, power_name, start, end)
    prior_kw = None
    if power_prior is not None:
        number = _as_float(power_prior[1])
        if number is not None:
            prior_kw = (power_prior[0], number)
    if prior_kw is None:
        volts = ev.at_ts("ChargerVoltage", start - timedelta(microseconds=1))
        amps = ev.at_ts("ChargeAmps", start - timedelta(microseconds=1))
        if volts and amps and volts[0] < start and amps[0] < start:
            when = max(volts[0], amps[0])
            v_num = _as_float(ev.at("ChargerVoltage", when))
            a_num = _as_float(ev.at("ChargeAmps", when))
            if v_num is not None and a_num is not None:
                prior_kw = (when, round(v_num * a_num / 1000.0, 3))
    segs = series_segments(samples, start, prior_kw)
    observed_vals = [p for seg in segs for _t, p in seg]
    observed = max(observed_vals) if observed_vals else None
    rated = _rated_kw(conn, vehicle_id, row[0])
    temps = _charge_temps(ev, start, end)
    tmin = [p["min"] for p in temps if p.get("min") is not None]
    tmax = [p["max"] for p in temps if p.get("max") is not None]
    series: dict[str, list] = {}
    priors: dict[str, tuple] = {}
    for name in ("ModuleTempMin", "ModuleTempMax", "BatteryHeaterOn"):
        got, prior = _window_parts(ev, name, start, end)
        if got:
            series[name] = got
        if prior is not None:
            priors[name] = prior
    return {
        "vehicle_id": vehicle_id, "start_ts": row[0], "end_ts": row[1], "kind": row[2],
        "points": pts, "segments": _pack_segments(segs, "kw"),
        "avg_kw": avg_kw, "max_kw": max_kw, "temps": temps,
        "rated_kw": rated, "axis_kw": charge_axis_top(rated, observed),
        "battery_temp_min": min(tmin) if tmin else None,
        "battery_temp_max": max(tmax) if tmax else None,
        "battery": _pack_battery(build_battery(series, priors, start, end)),
    }


def _charge_temps(ev: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Battery min/max samples. Only timestamps that actually arrived."""
    mins = {}
    maxs = {}
    for ts, value in ev.series("ModuleTempMin", start, end):
        try:
            mins[format_ts(ts)] = round(float(value), 1)
        except (TypeError, ValueError):
            continue
    for ts, value in ev.series("ModuleTempMax", start, end):
        try:
            maxs[format_ts(ts)] = round(float(value), 1)
        except (TypeError, ValueError):
            continue
    out = []
    for ts in sorted(set(mins) | set(maxs)):
        item: dict[str, Any] = {"ts": ts}
        if ts in mins:
            item["min"] = mins[ts]
        if ts in maxs:
            item["max"] = maxs[ts]
        out.append(item)
    return out


def list_parked(
    conn: sqlite3.Connection, vehicle_id: str, month: str | None = None, year: str | None = None
) -> dict[str, Any]:
    where = "vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    like = _period_like(month, year)
    if like:
        where += " AND date(start_ts, '+9 hours') LIKE ?"
        params.append(like)
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM parked_usage WHERE {where} ORDER BY start_ts DESC", params
        ).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    total = sum(float(r["kwh"] or 0) for r in rows)
    return {"vehicle_id": vehicle_id, "items": rows, "total_kwh": total, "count": len(rows)}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def battery_daily(
    conn: sqlite3.Connection, vehicle_id: str, start: str | None, end: str | None,
    month: str | None = None, year: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    def q(where: str, params: list[Any]) -> list[sqlite3.Row]:
        try:
            return conn.execute(
                f"""
                SELECT substr(hour, 1, 10) AS day, SUM(used_pct) AS used, SUM(charged_pct) AS charged,
                       MAX(driving) AS driving, MAX(charging) AS charging, COUNT(*) AS hours
                FROM battery_hourly WHERE vehicle_id = ? AND {where}
                GROUP BY day ORDER BY day
                """,
                [vehicle_id, *params],
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    if month or year:
        month = parse_month(month)
        year = parse_year(year)
        prefix = month or year
        rows = q("hour LIKE ?", [prefix + "%"])
        used = [float(r["used"] or 0) for r in rows]
        out: dict[str, Any] = {
            "vehicle_id": vehicle_id,
            "days_with_data": len(rows),
            "avg_used_pct": (sum(used) / len(used)) if used else None,
            "total_used_pct": sum(used),
        }
        if month:
            out["month"] = month
        else:
            out["year"] = year
            months: dict[str, list[float]] = {}
            for r in rows:
                months.setdefault(r["day"][:7], []).append(float(r["used"] or 0))
            out["months"] = [
                {"month": m, "avg_used_pct": sum(v) / len(v), "days_with_data": len(v)}
                for m, v in sorted(months.items())
            ]
        return out
    if not start or not end or not _DATE_RE.fullmatch(start) or not _DATE_RE.fullmatch(end):
        raise ValueError("from and to must be YYYY-MM-DD (or give month / year)")
    rows = q("hour >= ? AND hour < ?", [start + "T00", end + "T99"])
    by = {r["day"]: r for r in rows}
    live = open_span_hours(conn, vehicle_id, now)
    live_days = {k: {h[:10] for h in v} for k, v in live.items()}
    days = []
    d = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    n = 0
    while d <= last and n < 400:
        key = d.strftime("%Y-%m-%d")
        r = by.get(key)
        days.append({
            "date": key,
            "used_pct": float(r["used"] or 0) if r else None,
            "charged_pct": float(r["charged"] or 0) if r else None,
            "driving": (bool(r["driving"]) or key in live_days["driving"]) if r else False,
            "charging": (bool(r["charging"]) or key in live_days["charging"]) if r else False,
        })
        d += timedelta(days=1)
        n += 1
    return {"vehicle_id": vehicle_id, "from": start, "to": end, "days": days}


OPEN_SPAN_LOOKBACK = timedelta(hours=72)


def _recent_events(
    conn: sqlite3.Connection, vehicle_id: str, names: tuple[str, ...], now: datetime
) -> Events | None:
    """Signals `names` for the last OPEN_SPAN_LOOKBACK, plus each one's value before it."""
    cut = format_ts(now - OPEN_SPAN_LOOKBACK)
    mark = ",".join("?" for _ in names)
    try:
        seed = conn.execute(
            f"""
            SELECT s.ts, s.name, s.value FROM signals s
            JOIN (SELECT name, MAX(ts) AS ts FROM signals
                  WHERE vehicle_id = ? AND name IN ({mark}) AND ts < ? GROUP BY name) m
              ON m.name = s.name AND m.ts = s.ts
            WHERE s.vehicle_id = ?
            """,
            (vehicle_id, *names, cut, vehicle_id),
        ).fetchall()
        recent = conn.execute(
            f"SELECT ts, name, value FROM signals WHERE vehicle_id = ? AND name IN ({mark}) "
            "AND ts >= ? AND ts <= ? ORDER BY ts, name",
            (vehicle_id, *names, cut, format_ts(now)),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    rows = [(parse_ts(ts), name, _decode_signal(v)) for ts, name, v in [*seed, *recent]]
    rows.sort(key=lambda r: (r[0], r[1]))
    return Events(rows)


def open_span_hours(
    conn: sqlite3.Connection, vehicle_id: str, now: datetime | None = None
) -> dict[str, set[str]]:
    """KST hours ("YYYY-MM-DDTHH") covered by the trip / charge still in progress.

    battery_hourly flags come from finished trips and charges only, so the hours of a
    session that has not ended yet would show as parked. This reads the latest
    Gear / DetailedChargeState / ChargeState (last 72 h) with the same rules as the
    processor and returns the hours from the open session's start to now.
    """
    now = now or datetime.now(timezone.utc)
    ev = _recent_events(conn, vehicle_id, ("Gear", "DetailedChargeState", "ChargeState"), now)
    out: dict[str, set[str]] = {"driving": set(), "charging": set()}
    if ev is None:
        return out
    for key, start in (("driving", open_trip_start(ev, now)), ("charging", open_charge_start(ev))):
        if start is None:
            continue
        t = max(start, now - OPEN_SPAN_LOOKBACK).astimezone(KST).replace(minute=0, second=0, microsecond=0)
        end = max(now, start)
        while t <= end:
            out[key].add(t.strftime("%Y-%m-%dT%H"))
            t += timedelta(hours=1)
    return out


LIVE_CHARGE_SIGNALS = (
    "DetailedChargeState", "ChargeState", "ACChargingPower", "DCChargingPower",
    "ACChargingEnergyIn", "DCChargingEnergyIn", "ChargerVoltage", "ChargeAmps",
    "BatteryLevel", "ChargeLimitSoc", "TimeToFullCharge", "SentryMode",
)


def live_charge(
    conn: sqlite3.Connection, vehicle_id: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The charge in progress, computed from signals (nothing is stored until it ends)."""
    now = now or datetime.now(timezone.utc)
    ev = _recent_events(conn, vehicle_id, LIVE_CHARGE_SIGNALS, now)
    if ev is None:
        return None
    start = open_charge_start(ev)
    if start is None:
        return None
    at = max(now, start)
    energy = charge_energy(ev, start, at)
    kind = energy["kind"]
    curve = charge_curve(ev, start, at, kind)
    kw = next((p["kw"] for p in reversed(curve) if p["kw"] is not None), None)
    ttf = ev.at_ts("TimeToFullCharge", at)
    est_now = (ttf[0] + timedelta(hours=_num(ttf[1]))) if ttf and ttf[0] >= start and _num(ttf[1]) > 0 else None
    est_start = estimated_full(ev, start, at)
    return {
        "in_progress": True,
        "charge_id": format_ts(start),
        "start_ts": format_ts(start),
        "kind": kind,
        "power_kw": kw,
        "start_soc": _as_float_or_none(ev.at("BatteryLevel", start)),
        "soc": _as_float_or_none(ev.at("BatteryLevel", at)),
        "limit_soc": _as_float_or_none(ev.at("ChargeLimitSoc", at)),
        "charger_kwh": energy["charger_kwh"],
        "battery_kwh": energy["battery_kwh"],
        "ac_kwh": energy.get("ac_kwh"),
        "dc_kwh": energy.get("dc_kwh"),
        "loss_kwh": energy["loss_kwh"],
        "loss_pct": energy["loss_pct"],
        "sentry_min": sentry_minutes(ev, start, at),
        "est_full_ts": format_ts(est_now) if est_now else (format_ts(est_start) if est_start else None),
    }


def charges_in_progress(
    conn: sqlite3.Connection, vehicle_id: str, month: str | None = None,
    year: str | None = None, offset: int = 0, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Live charge for the top of the 충전 기록 list: first page only, and only when the
    selected month/year contains today (KST)."""
    if offset:
        return None
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(KST).strftime("%Y-%m-%d")
    if month and not today.startswith(month):
        return None
    if year and not month and not today.startswith(year):
        return None
    return live_charge(conn, vehicle_id, now)


def _as_float_or_none(v: Any) -> float | None:
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def battery_hourly(
    conn: sqlite3.Connection, vehicle_id: str, day: str | None, now: datetime | None = None
) -> dict[str, Any]:
    if not day or not _DATE_RE.fullmatch(day):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        rows = {r["hour"]: r for r in conn.execute(
            "SELECT * FROM battery_hourly WHERE vehicle_id = ? AND hour LIKE ? ORDER BY hour",
            (vehicle_id, day + "T%"),
        ).fetchall()}
    except sqlite3.OperationalError:
        rows = {}
    live = open_span_hours(conn, vehicle_id, now)
    hours = []
    for h in range(24):
        key = f"{day}T{h:02d}"
        r = rows.get(key)
        hours.append({
            "hour": h,
            "pct": r["last_pct"] if r else None,
            "min_pct": r["min_pct"] if r else None,
            "max_pct": r["max_pct"] if r else None,
            "used_pct": r["used_pct"] if r else None,
            "charged_pct": r["charged_pct"] if r else None,
            "driving": (bool(r["driving"]) or key in live["driving"]) if r else False,
            "charging": (bool(r["charging"]) or key in live["charging"]) if r else False,
        })
    used = sum(float(h["used_pct"] or 0) for h in hours)
    return {"vehicle_id": vehicle_id, "date": day, "hours": hours, "used_pct": used}


def list_charges(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    start_ts: str | None = None,
) -> list[dict[str, Any]]:
    where = "vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    like = _period_like(month, year)
    extra, extra_params = _day_where(_CHARGE_KST, date_from, date_to)
    # A charge id is the start timestamp. Day filters use the end (KST), so a
    # session that crosses midnight is missing from the start day's list.
    if start_ts:
        where += " AND start_ts = ?"
        params.append(start_ts)
        like, extra, extra_params = None, "", []
    if like or extra:
        if like:
            where += f" AND {_CHARGE_KST} LIKE ?"
            params.append(like)
        where += extra
        params += extra_params
        order = f"{_CHARGE_TS} DESC, start_ts DESC"
    else:
        order = "start_ts DESC"
    sql = f"SELECT * FROM charges WHERE {where} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    items = [_row_to_charge(r) for r in rows]
    for item in items:
        item["kind_label"] = charge_kind_label(item.get("kind"), item.get("fast_charger"))
    meta_by_id = _meta_for_charges(conn, vehicle_id, [item["charge_id"] for item in items])
    label_charges(conn, vehicle_id, items, meta_by_id)
    for item in items:
        meta = meta_by_id.get(item["charge_id"])
        item["meta"] = meta
        # Paid amount, or an estimate from the saved station price / operator rate (marked).
        try:
            cost, estimated = charging_cost(conn, vehicle_id, item.get("kwh_added"), meta)
        except sqlite3.Error:
            cost, estimated = None, False
        item["cost_krw"] = cost
        item["cost_estimated"] = estimated
        paid, billed = (meta or {}).get("total_krw"), (meta or {}).get("kwh_billed")
        value = None if paid is None else float(paid) + float((meta or {}).get("discount_krw") or 0)
        item["unit_krw_per_kwh"] = (   # (paid + discount) / billed kWh
            value / float(billed) if value is not None and billed and float(billed) > 0 else None
        )
    try:
        from process.evcharger import attach_public_chargers
        attach_public_chargers(conn, vehicle_id, items)
    except sqlite3.Error:
        pass
    try:
        from web.tariff import annotate
        annotate(conn, vehicle_id, items)
    except sqlite3.Error:
        pass
    return items


def charge_kind_label(kind: Any, fast_charger: Any) -> str:
    """완속 (AC) / 급속 (DC) / 슈퍼차저 (DC with FastChargerType Supercharger)."""
    if kind == "DC":
        return "슈퍼차저" if fast_charger == "Supercharger" else "급속"
    return "완속"


def count_charges(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where = "vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    like = _period_like(month, year)
    if like:
        where += f" AND {_CHARGE_KST} LIKE ?"
        params.append(like)
    extra, extra_params = _day_where(_CHARGE_KST, date_from, date_to)
    return _count_where(conn, f"SELECT COUNT(*) FROM charges WHERE {where}{extra}", params + extra_params)


def _meta_for_charges(
    conn: sqlite3.Connection, vehicle_id: str, ids: list[str]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not ids:
        return out
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT * FROM charge_meta WHERE vehicle_id = ? AND charge_id IN ({marks})",
                [vehicle_id, *chunk],
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        for row in rows:
            out[row["charge_id"]] = {k: row[k] for k in row.keys()}
    return out


def _num(value: Any) -> float:
    return float(value or 0)


def activity_year_summary(
    conn: sqlite3.Connection, vehicle_id: str, year: str
) -> dict[str, Any]:
    """Monthly trip and charge totals for one KST year, aggregated in SQL."""
    parsed = parse_year(year)
    if not parsed:
        raise ValueError("year is required (YYYY)")
    year = parsed
    like = year + "%"
    try:
        trip_rows = conn.execute(
            f"""
            SELECT substr({_TRIP_KST}, 1, 7) AS month,
                   COUNT(*) AS n,
                   COALESCE(SUM(km), 0) AS km,
                   COALESCE(SUM(kwh), 0) AS kwh
            FROM trips
            WHERE vehicle_id = ? AND {_TRIP_KST} LIKE ?
            GROUP BY month
            """,
            (vehicle_id, like),
        ).fetchall()
    except sqlite3.OperationalError:
        trip_rows = []
    charge_sql = f"""
        SELECT substr({_CHARGE_KST_C}, 1, 7) AS month,
               COUNT(*) AS n,
               COALESCE(SUM(c.kwh_added), 0) AS kwh,
               COALESCE(SUM(m.total_krw), 0) AS krw
        FROM charges c
        LEFT JOIN charge_meta m
          ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts
        WHERE c.vehicle_id = ? AND {_CHARGE_KST_C} LIKE ?
        GROUP BY month
    """
    try:
        charge_rows = conn.execute(charge_sql, (vehicle_id, like)).fetchall()
    except sqlite3.OperationalError:
        try:
            charge_rows = conn.execute(
                f"""
                SELECT substr({_CHARGE_KST}, 1, 7) AS month,
                       COUNT(*) AS n,
                       COALESCE(SUM(kwh_added), 0) AS kwh,
                       0 AS krw
                FROM charges
                WHERE vehicle_id = ? AND {_CHARGE_KST} LIKE ?
                GROUP BY month
                """,
                (vehicle_id, like),
            ).fetchall()
        except sqlite3.OperationalError:
            charge_rows = []
    trip_by = {row["month"]: row for row in trip_rows if row["month"]}
    charge_by = {row["month"]: row for row in charge_rows if row["month"]}
    totals = {
        "trip_count": 0,
        "trip_km": 0.0,
        "trip_kwh": 0.0,
        "charge_count": 0,
        "charge_kwh": 0.0,
        "charge_krw": 0.0,
    }
    months: list[dict[str, Any]] = []
    for key in sorted(set(trip_by) | set(charge_by)):
        trip = trip_by.get(key)
        charge = charge_by.get(key)
        item = {
            "month": key,
            "trip_count": int(trip["n"]) if trip else 0,
            "trip_km": _num(trip["km"]) if trip else 0.0,
            "trip_kwh": _num(trip["kwh"]) if trip else 0.0,
            "charge_count": int(charge["n"]) if charge else 0,
            "charge_kwh": _num(charge["kwh"]) if charge else 0.0,
            "charge_krw": _num(charge["krw"]) if charge else 0.0,
        }
        months.append(item)
        for field in totals:
            totals[field] += item[field]
    return {"vehicle_id": vehicle_id, "year": year, "months": months, "totals": totals}


def _page_fields(
    payload: dict[str, Any],
    *,
    total: int,
    month: str | None,
    year: str | None,
    limit: int | None,
    offset: int,
) -> dict[str, Any]:
    if month is None and year is None and limit is None:
        return payload
    payload["total"] = total
    if month:
        payload["month"] = month
    elif year:
        payload["year"] = year
    if limit is not None:
        payload["limit"] = limit
        payload["offset"] = offset
    return payload


def month_summary(
    conn: sqlite3.Connection, vehicle_id: str, month: str
) -> dict[str, Any]:
    prefix = month + "%"
    try:
        trips = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(km), 0) AS km,
                   COALESCE(SUM(kwh), 0) AS kwh
            FROM trips
            WHERE vehicle_id = ? AND date(start_ts, '+9 hours') LIKE ?
            """,
            (vehicle_id, prefix),
        ).fetchone()
        charges = conn.execute(
            """
            SELECT kind, COALESCE(SUM(kwh_added), 0) AS kwh, COUNT(*) AS n
            FROM charges
            WHERE vehicle_id = ? AND date(end_ts, '+9 hours') LIKE ?
            GROUP BY kind
            """,
            (vehicle_id, prefix),
        ).fetchall()
    except sqlite3.OperationalError:
        trips = {"n": 0, "km": 0, "kwh": 0}
        charges = []
    km = float(trips["km"] or 0)
    kwh = float(trips["kwh"] or 0)
    avg = (kwh * 1000.0 / km) if km > 0 else None
    try:
        parked_row = conn.execute(
            "SELECT COALESCE(SUM(kwh), 0), COUNT(*) FROM parked_usage "
            "WHERE vehicle_id = ? AND date(start_ts, '+9 hours') LIKE ?",
            (vehicle_id, prefix),
        ).fetchone()
        parked_kwh = float(parked_row[0] or 0)
    except sqlite3.OperationalError:
        parked_kwh = 0.0
    kwh_ac = 0.0
    kwh_dc = 0.0
    charge_count = 0
    for row in charges:
        charge_count += int(row["n"])
        if row["kind"] == "AC":
            kwh_ac = float(row["kwh"])
        elif row["kind"] == "DC":
            kwh_dc = float(row["kwh"])
    return {
        "vehicle_id": vehicle_id,
        "month": month,
        "trip_count": int(trips["n"] or 0),
        "km": km,
        "kwh": kwh,
        "avg_wh_per_km": avg,
        "avg_km_per_kwh": (km / kwh) if kwh > 0.01 else None,
        "parked_kwh": parked_kwh,
        "charge_count": charge_count,
        "kwh_ac": kwh_ac,
        "kwh_dc": kwh_dc,
    }


def stream_snapshot(
    conn: sqlite3.Connection, vehicle_id: str, tire_alert_bar: float
) -> dict[str, Any]:
    live = live_payload(conn, vehicle_id, tire_alert_bar)
    return {
        "live": live,
        "data_rev": data_rev(conn, vehicle_id),
        "trip_in_progress": live["trip_in_progress"],
        "charge_in_progress": live["charge_in_progress"],
        "signals": live["signals"],
        "last_ts": live["last_ts"],
        "vehicle_id": vehicle_id,
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        db_path: str,
        tire_alert_bar: float,
        process_interval: float,
        stop_event: threading.Event | None = None,
        seed: bool = True,
        alerts: bool = False,
    ) -> None:
        super().__init__(server_address, DashboardHandler)
        self.alerts = alerts
        # reverse geocoding of trip start/end, only when TESLIFE_GEOCODER is set (server env)
        from process.geocode import from_env as _geocoder_from_env

        self.geocoder = _geocoder_from_env() if process_interval > 0 else None
        from process.match import from_env as _matcher_from_env

        self.matcher = _matcher_from_env() if process_interval > 0 else None
        self.db_path = db_path
        self.tire_alert_bar = tire_alert_bar
        self.process_interval = process_interval
        self.seed = seed
        self.stop_event = stop_event or threading.Event()
        self.process_thread: threading.Thread | None = None
        # Schema and the official price seed need a writable connection. GET uses
        # open_readonly and must not be the place that creates them.
        try:
            conn = open_writable(db_path)
            try:
                if seed:
                    seed_insurance(conn)
                    seed_consumables(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        if process_interval > 0:
            self.process_thread = threading.Thread(
                target=self._process_loop, name="process-loop", daemon=True
            )
            self.process_thread.start()

    def _process_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                process_db(self.db_path, seed=self.seed, alerts=self.alerts, geocoder=self.geocoder,
                           matcher=self.matcher)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive; process_db closes its connection
                print(f"process error: {type(exc).__name__}: {exc}", flush=True)
            if self.stop_event.wait(self.process_interval):
                break

    def shutdown(self) -> None:
        self.stop_event.set()
        super().shutdown()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        self._handle_write("POST")

    def do_PUT(self) -> None:
        self._handle_write("PUT")

    def do_DELETE(self) -> None:
        self._handle_write("DELETE")

    def do_PATCH(self) -> None:
        self._reject_write()

    def _reject_write(self) -> None:
        self.send_error(405, "API is read-only")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return False
        parsed = urlparse(origin)
        return parsed.netloc == host

    def _read_json_body(self) -> tuple[dict[str, Any] | None, str | None]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, "JSON body required"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid JSON"
        if not isinstance(data, dict):
            return None, "JSON object required"
        return data, None

    def _bot_gate(self) -> bool:
        from web.bot_notify import token_configured, token_matches

        if not token_configured():
            self._json(404, {"error": "not found"})
            return False
        if not token_matches(self.headers.get("Authorization") or ""):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _bot_vehicle(self, conn: sqlite3.Connection) -> str | None:
        return resolve_vehicle_id(conn, None)

    def _bot_notify_pending(self) -> None:
        if not self._bot_gate():
            return
        conn = self._open()
        try:
            vehicle_id = self._bot_vehicle(conn)
            if vehicle_id is None:
                self._json(400, {"error": "no vehicles"})
                return
            from web.bot_notify import list_pending
            self._json(200, list_pending(conn, vehicle_id))
        except sqlite3.Error as exc:
            self._json(500, {"error": str(exc)})
        finally:
            conn.close()

    def _bot_notify_post(self) -> None:
        if not self._bot_gate():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return
        if length > 8000:
            self._json(413, {"error": "body too large"})
            return
        body, err = self._read_json_body()
        if err:
            self._json(415 if err == "JSON body required" else 400, {"error": err})
            return
        assert body is not None
        wconn = open_writable(self.server.db_path)
        try:
            vehicle_id = self._bot_vehicle(wconn)
            if vehicle_id is None:
                self._json(400, {"error": "no vehicles"})
                return
            from web.bot_notify import apply_bot_action
            self._json(200, apply_bot_action(wconn, vehicle_id, body))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "notification not found"})
        except sqlite3.Error as exc:
            self._json(500, {"error": str(exc)})
        finally:
            wconn.close()

    def _handle_write(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/bot/notify" and method == "POST":
            self._bot_notify_post()
            return
        if path not in WRITE_PATHS.get(method, ()):
            self._reject_write()
            return
        if not self._origin_ok():
            self._json(403, {"error": "origin mismatch"})
            return
        body, err = self._read_json_body()
        if err:
            self._json(415 if err == "JSON body required" else 400, {"error": err})
            return
        assert body is not None
        from web.l10n import bind_lang, error_text

        wconn = open_writable(self.server.db_path)
        try:
            with bind_lang(query):
                vehicle_id = self._vehicle(wconn, query)
                if vehicle_id is None and body.get("vehicle_id"):
                    vehicle_id = str(body["vehicle_id"])
                if vehicle_id is None:
                    self._json(400, {"error": "no vehicles"})
                    return
                payload, status = dispatch_write(method, path, wconn, vehicle_id, body)
                self._json(status, payload)
        except ValueError as exc:
            self._json(400, {"error": error_text(str(exc), (query.get("lang") or ["ko"])[0])})
        except KeyError as exc:
            self._json(404, {"error": error_text(str(exc).strip("'"), (query.get("lang") or ["ko"])[0])})
        except sqlite3.Error as exc:
            self._json(500, {"error": str(exc)})
        finally:
            wconn.close()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_index()
            return
        if path in STATIC_FILES:
            self._serve_static(path)
            return
        if path == "/api/stream":
            self._serve_stream(query)
            return
        if path == "/api/bot/notify/pending":
            self._bot_notify_pending()
            return
        if path == "/api/ledger.csv":
            self._serve_ledger_csv(query)
            return
        if not path.startswith("/api/"):
            self.send_error(404)
            return
        from web.l10n import bind_lang, error_text

        ui_lang = (query.get("lang") or ["ko"])[0]
        try:
            with bind_lang(query):
                payload = self._api_payload(path, query)
        except ValueError as exc:
            self._json(400, {"error": error_text(str(exc), ui_lang)})
            return
        except KeyError as exc:
            self._json(404, {"error": error_text(str(exc).strip("'"), ui_lang)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": str(exc)})
            return
        if payload is None:
            self.send_error(404)
            return
        self._json(200, payload)

    def _serve_static(self, path: str) -> None:
        name, ctype = STATIC_FILES[path]
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        cache = "no-cache" if path == "/sw.js" else "public, max-age=604800"
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        boot = "null"
        try:
            conn = self._open()
            try:
                parsed = urlparse(self.path)
                vid = self._vehicle(conn, parse_qs(parsed.query))
                if vid:
                    payload = live_payload(conn, vid, self.server.tire_alert_bar)
                    payload["data_rev"] = data_rev(conn, vid)
                    boot = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
            finally:
                conn.close()
        except sqlite3.Error:
            boot = "null"
        snippet = f"<script>window.__TESLIFE_BOOT__={boot};</script>\n"
        marker = "  <script>\n"
        if marker in html:
            html = html.replace(marker, snippet + marker, 1)
        else:
            html = html.replace("</body>", snippet + "</body>", 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _open(self) -> sqlite3.Connection:
        return open_readonly(self.server.db_path)

    def _vehicle(self, conn: sqlite3.Connection, query: dict[str, list[str]]) -> str | None:
        requested = query.get("vehicle_id", [None])[0]
        return resolve_vehicle_id(conn, requested)

    def _period_list(
        self,
        conn: sqlite3.Connection,
        vehicle_id: str,
        query: dict[str, list[str]],
        key: str,
        lister: Any,
        counter: Any,
    ) -> dict[str, Any]:
        month = parse_month(_q1(query, "month"))
        year = parse_year(_q1(query, "year"))
        limit = parse_page_int(_q1(query, "limit"), "limit")
        offset = parse_page_int(_q1(query, "offset"), "offset") or 0
        date_from, date_to = parse_day_range(_q1(query, "from"), _q1(query, "to"))
        rows = lister(
            conn,
            vehicle_id,
            month=month,
            year=year,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        )
        payload = {"vehicle_id": vehicle_id, key: rows}
        applied_year = None if month else year
        total = 0
        if month or applied_year or limit is not None or date_from or date_to:
            total = counter(conn, vehicle_id, month=month, year=year, date_from=date_from, date_to=date_to)
        if date_from or date_to:
            payload["total"] = total
            payload["from"], payload["to"] = date_from, date_to
        return _page_fields(
            payload,
            total=total,
            month=month,
            year=applied_year,
            limit=limit,
            offset=offset,
        )

    def _menu_payload(self, conn: sqlite3.Connection, vid: str, path: str,
                      query: dict[str, list[str]]) -> Any | None:
        """The period views of the 주행 / 충전 / 차계부 menus (web/menu.py)."""
        from web import menu

        if path not in MENU_PATHS:
            return None
        date_from, date_to = parse_day_range(_q1(query, "from"), _q1(query, "to"))
        recent = parse_page_int(_q1(query, "recent"), "limit")
        if path == "/api/periods":
            return {"vehicle_id": vid, "first": menu.first_days(conn, vid), "months": menu.record_months(conn, vid)}
        if path == "/api/trips/summary":
            return menu.trips_summary(conn, vid, date_from, date_to, recent)
        if path == "/api/trips/monthly":
            return menu.trips_monthly(conn, vid, date_from, date_to)
        if path == "/api/trips/trend":
            return menu.trips_trend(conn, vid, date_from, date_to, recent)
        if path == "/api/charges/summary":
            return menu.charges_summary(conn, vid, date_from, date_to, recent)
        if path == "/api/charges/monthly":
            return menu.charges_monthly(conn, vid, date_from, date_to)
        if path == "/api/charges/trend":
            return menu.charge_trend(conn, vid, date_from, date_to, recent)
        if path == "/api/charges/places":
            items = list_charges(conn, vid, date_from=date_from, date_to=date_to,
                                 limit=recent if recent else _PAGE_MAX)
            return {"vehicle_id": vid, "places": menu.charge_places(items)}
        if path == "/api/map/roads":
            from process.roads import road_map
            return road_map(conn, vid, parse_year(_q1(query, "year")))
        if path == "/api/ledger/summary":
            f, t = _ledger_range(query)
            return ledger_summary(conn, vid, f, t, _q1(query, "category"))
        if path == "/api/geocode/search":
            q = (_q1(query, "q") or "").strip()
            if len(q) < 2 or len(q) > 100:
                raise ValueError("q must be 2-100 characters")
            geocoder = getattr(self.server, "geocoder", None)
            if geocoder is None or not hasattr(geocoder, "search"):
                from process.geocode import from_env
                geocoder = from_env()
            if geocoder is None or not hasattr(geocoder, "search"):
                return {"results": [], "error": "geocoder off"}
            from web.l10n import get_lang
            with _SEARCH_LOCK:
                return {"results": geocoder.search(q, lang=get_lang())}
        if path == "/api/place-search":
            q = (_q1(query, "q") or "").strip()
            if not q or len(q) > 80:
                raise ValueError("q must be 1-80 characters")
            from process.naver_maps import place_search

            def _coord(key: str) -> float | None:
                raw = _q1(query, key)
                if raw is None or raw == "":
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            return place_search(q, _coord("lat"), _coord("lon"))
        if path == "/api/place-suggest":
            from process.evcharger import suggest_places

            def _coord(key: str) -> float | None:
                raw = _q1(query, key)
                if raw is None or raw == "":
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            return suggest_places(
                conn, vid, _coord("lat"), _coord("lon"),
                (_q1(query, "stat_id") or "").strip() or None,
                _q1(query, "q") or "",
                (_q1(query, "charge_id") or "").strip(),
            )
        if path == "/api/chargers/near":
            from process.evcharger import stations_near

            def _coord(key: str) -> float | None:
                raw = _q1(query, key)
                if raw is None or raw == "":
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            rows = stations_near(conn, _coord("lat"), _coord("lon"))
            return {"stations": [
                {key: row.get(key) for key in ("stat_id", "stat_nm", "addr", "operator", "output_kw")}
                for row in rows
            ]}
        return None

    def _api_payload(self, path: str, query: dict[str, list[str]]) -> Any | None:
        conn = self._open()
        try:
            vehicle_id = self._vehicle(conn, query)
            if vehicle_id is None:
                return {"error": "no vehicles"}
            bar = self.server.tire_alert_bar
            if path == "/api/live":
                out = live_payload(conn, vehicle_id, bar)
                out["data_rev"] = data_rev(conn, vehicle_id)
                return out
            menu = self._menu_payload(conn, vehicle_id, path, query)
            if menu is not None:
                return menu
            if path == "/api/trips":
                return self._period_list(
                    conn, vehicle_id, query, "trips", list_trips, count_trips
                )
            if path == "/api/charges":
                start_id = _q1(query, "start")
                if start_id:
                    rows = list_charges(conn, vehicle_id, start_ts=start_id, limit=5)
                    return {"vehicle_id": vehicle_id, "charges": rows, "total": len(rows)}
                payload = self._period_list(
                    conn, vehicle_id, query, "charges", list_charges, count_charges
                )
                payload["in_progress"] = charges_in_progress(
                    conn, vehicle_id, month=parse_month(_q1(query, "month")),
                    year=parse_year(_q1(query, "year")),
                    offset=parse_page_int(_q1(query, "offset"), "offset") or 0,
                )
                return payload
            m = _TRIP_POWER_RE.fullmatch(path)
            if m:
                return trip_power(conn, vehicle_id, unquote(m.group(1)))
            m = _TRIP_SPEED_RE.fullmatch(path)
            if m:
                return trip_speed(conn, vehicle_id, unquote(m.group(1)))
            m = _TRIP_CLIMATE_RE.fullmatch(path)
            if m:
                return trip_climate(conn, vehicle_id, unquote(m.group(1)))
            m = _TRIP_ROUTE_RE.fullmatch(path)
            if m:
                return trip_route(conn, vehicle_id, unquote(m.group(1)))
            if path == "/api/map/routes":
                date_from, date_to = parse_day_range(_q1(query, "from"), _q1(query, "to"))
                return map_routes(conn, vehicle_id, month=_q1(query, "month"), year=_q1(query, "year"),
                                  date_from=date_from, date_to=date_to,
                                  recent=parse_page_int(_q1(query, "recent"), "limit"))
            if path == "/api/map/cumulative":
                return map_cumulative(conn, vehicle_id, year=_q1(query, "year"))
            if path == "/api/map/config":
                return map_config()
            if path == "/api/places":
                return {"vehicle_id": vehicle_id, "places": list_places(conn, vehicle_id)}
            m = _CHARGE_CURVE_RE.fullmatch(path)
            if m:
                return charge_detail_curve(conn, vehicle_id, unquote(m.group(1)))
            if path == "/api/parked":
                return list_parked(
                    conn, vehicle_id, month=_q1(query, "month"), year=_q1(query, "year")
                )
            if path == "/api/battery/daily":
                return battery_daily(
                    conn, vehicle_id, _q1(query, "from"), _q1(query, "to"),
                    month=_q1(query, "month"), year=_q1(query, "year"),
                )
            if path == "/api/battery/hourly":
                return battery_hourly(conn, vehicle_id, _q1(query, "date"))
            if path == "/api/sentry/alarms":
                from web.timeline import sentry_alarms
                return {"vehicle_id": vehicle_id, "alarms": sentry_alarms(conn, vehicle_id)}
            if path == "/api/park14h":
                from process import _load_events
                from process.alerts import ac_park14h_status
                return ac_park14h_status(conn, vehicle_id, ev=_load_events(conn, vehicle_id))
            if path == "/api/battery/timeline":
                from web.timeline import day_timeline
                return day_timeline(conn, vehicle_id, _q1(query, "date"))
            if path == "/api/activity/yearly":
                year = parse_year(_q1(query, "year"))
                if not year:
                    raise ValueError("year is required (YYYY)")
                return activity_year_summary(conn, vehicle_id, year)
            if path == "/api/driving":
                from web.driving import driving_report
                period = _q1(query, "period") or "week"
                if period not in ("week", "month", "year"):
                    raise ValueError("period must be week, month or year")
                at = _q1(query, "at")
                if at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", at):
                    raise ValueError("at must be YYYY-MM-DD")
                return driving_report(conn, vehicle_id, period, at)
            if path == "/api/summary":
                month = query.get("month", [datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m")])[0]
                return month_summary(conn, vehicle_id, month)
            if path == "/api/ledger":
                month = parse_month(_q1(query, "month"))
                category = _q1(query, "category")
                limit = parse_page_int(_q1(query, "limit"), "limit")
                offset = parse_page_int(_q1(query, "offset"), "offset") or 0
                date_from, date_to = _ledger_range(query)
                entries = list_ledger(
                    conn,
                    vehicle_id,
                    month=month,
                    category=category,
                    limit=limit,
                    offset=offset,
                    date_from=date_from,
                    date_to=date_to,
                )
                payload: dict[str, Any] = {"vehicle_id": vehicle_id, "entries": entries}
                if limit is not None:
                    payload["total"] = count_ledger(
                        conn, vehicle_id, month=month, category=category,
                        date_from=date_from, date_to=date_to,
                    )
                    payload["limit"] = limit
                    payload["offset"] = offset
                return payload
            if path == "/api/ledger/monthly":
                return report_by_period(
                    conn,
                    vehicle_id,
                    "month",
                    year=parse_year(_q1(query, "year")),
                    category=_q1(query, "category"),
                )
            if path == "/api/ledger/yearly":
                return report_by_period(
                    conn, vehicle_id, "year", category=_q1(query, "category")
                )
            if path == "/api/ledger/cost-per-km":
                return cost_per_km(
                    conn,
                    vehicle_id,
                    month=query.get("month", [None])[0],
                    year=query.get("year", [None])[0],
                )
            if path == "/api/ledger/ownership":
                return ownership_total(conn, vehicle_id)
            if path == "/api/ledger/charging-stats":
                return charging_stats(conn, vehicle_id)
            if path == "/api/stations":
                return {"vehicle_id": vehicle_id, "stations": list_stations(conn, vehicle_id)}
            if path == "/api/rates":
                return {"vehicle_id": vehicle_id, "rates": list_rates(conn, vehicle_id)}
            if path == "/api/tariffs":
                from web.tariff import list_tariffs
                return list_tariffs(conn, vehicle_id)
            if path == "/api/charge-meta":
                return {"vehicle_id": vehicle_id, "meta": list_charge_meta(conn, vehicle_id)}
            if path == "/api/consumables":
                return {
                    "vehicle_id": vehicle_id,
                    "items": list_items(conn, vehicle_id),
                    "registration_date": get_registration_date(conn, vehicle_id),
                }
            if path == "/api/due":
                return {"vehicle_id": vehicle_id, "items": due_items(conn, vehicle_id)}
            if path == "/api/vendors":
                return {"vehicle_id": vehicle_id, "vendors": list_vendors(conn, vehicle_id)}
            if path == "/api/replacements":
                item_id = query.get("item_id", [None])[0]
                payload = list_replacements(conn, vehicle_id, item_id=item_id)
                return payload
            if path == "/api/tire-sets":
                from ledger.tiresets import list_sets, season_due
                from web.l10n import get_lang
                return {
                    "vehicle_id": vehicle_id,
                    "sets": list_sets(conn, vehicle_id, lang=get_lang()),
                    "season_due": season_due(conn, vehicle_id),
                }
            if path == "/api/settings":
                from web.pressure import get_pressure_unit
                return {
                    "vehicle_id": vehicle_id,
                    "registration_date": get_registration_date(conn, vehicle_id),
                    "pressure_unit": get_pressure_unit(conn, vehicle_id),
                }
            if path == "/api/notifications":
                from web.notify import list_notifications
                return list_notifications(conn, vehicle_id)
            if path == "/api/notification-prefs":
                from web.notify import list_prefs
                return list_prefs(conn, vehicle_id)
            if path == "/api/push/vapid":
                from web.notify import vapid_public
                return vapid_public(conn)
            return None
        finally:
            conn.close()

    def _peer_closed(self) -> bool:
        """True when the browser has hung up. A quiet socket is still open."""
        sock = self.connection
        try:
            flags = socket.MSG_PEEK
            if hasattr(socket, "MSG_DONTWAIT"):
                flags |= socket.MSG_DONTWAIT
            data = sock.recv(1, flags)
        except BlockingIOError:
            return False
        except OSError:
            return True
        return data == b""

    def _stream_idle(self, seconds: float = 1.0) -> bool:
        """Wait out the snapshot gap. True if the server is stopping or the peer is gone.

        A closed browser leaves the socket readable with no bytes. Waiting only on
        the stop flag never sees that, so the handler kept the thread and the
        CLOSE_WAIT socket and rebuilt a snapshot every second.
        """
        sock = self.connection
        end = time.monotonic() + seconds
        while True:
            if self.server.stop_event.is_set():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            try:
                readable, _, _ = select.select([sock], [], [], min(left, 0.25))
            except (OSError, ValueError):
                return True
            if not readable:
                continue
            if self._peer_closed():
                return True
            if self.server.stop_event.wait(max(0.0, end - time.monotonic())):
                return True
            return False

    def _serve_stream(self, query: dict[str, list[str]]) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_rowid = -1
        last_state: tuple[bool, bool] | None = None
        try:
            while not self.server.stop_event.is_set():
                conn = self._open()
                try:
                    vehicle_id = self._vehicle(conn, query)
                    if vehicle_id is None:
                        payload = {"error": "no vehicles"}
                        rowid = max_signal_rowid(conn)
                        state = (False, False)
                    else:
                        payload = stream_snapshot(
                            conn, vehicle_id, self.server.tire_alert_bar
                        )
                        rowid = max_signal_rowid(conn)
                        state = (
                            payload["trip_in_progress"],
                            payload["charge_in_progress"],
                        )
                finally:
                    conn.close()
                changed = rowid != last_rowid or state != last_state
                if changed:
                    last_rowid = rowid
                    last_state = state
                    data = json.dumps(payload, ensure_ascii=False)
                    chunk = (
                        f"event: update\ndata: {data}\n\n"
                        f"data: {data}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if self._stream_idle(1.0):
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _serve_ledger_csv(self, query: dict[str, list[str]]) -> None:
        conn = self._open()
        try:
            vehicle_id = self._vehicle(conn, query)
            if vehicle_id is None:
                self._json(400, {"error": "no vehicles"})
                return
            date_from, date_to = _ledger_range(query)
            body = csv_export(conn, vehicle_id, date_from, date_to)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": str(exc)})
            return
        finally:
            conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        tag = ""
        try:
            f, t = _ledger_range(query)
            tag = "-" + "_".join(x for x in (f, t) if x) if (f or t) else ""
        except ValueError:
            pass
        self.send_header(
            "Content-Disposition", f'attachment; filename="ledger{tag}.csv"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


MENU_PATHS = {
    "/api/periods", "/api/trips/summary", "/api/trips/monthly", "/api/trips/trend",
    "/api/charges/summary", "/api/charges/monthly", "/api/charges/trend", "/api/charges/places",
    "/api/map/roads", "/api/ledger/summary", "/api/geocode/search", "/api/place-search",
    "/api/place-suggest", "/api/chargers/near",
}
_SEARCH_LOCK = threading.Lock()


def _ledger_range(query: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """Ledger rows by KST day range; month / year still work (converted to a range)."""
    f, t = parse_day_range(_q1(query, "from"), _q1(query, "to"))
    if f or t:
        return f, t
    year = parse_year(_q1(query, "year"))
    if year and not _q1(query, "month"):
        return f"{year}-01-01", f"{year}-12-31"
    return None, None


def data_rev(conn: sqlite3.Connection, vehicle_id: str) -> str:
    """Changes when a trip or charge is added or finished, or the ledger changes: the page then
    reloads the open menu (live values alone do not)."""
    parts = []
    for sql in (
        "SELECT COUNT(*), MAX(end_ts) FROM trips WHERE vehicle_id = ?",
        "SELECT COUNT(*), MAX(end_ts) FROM charges WHERE vehicle_id = ?",
        "SELECT COUNT(*), SUM(amount_krw) FROM ledger WHERE vehicle_id = ?",
        "SELECT COUNT(*), MAX(fetched_at) FROM route_match WHERE vehicle_id = ?",
        "SELECT COUNT(*), MAX(ts) FROM notifications WHERE vehicle_id = ?",
    ):
        try:
            r = conn.execute(sql, (vehicle_id,)).fetchone()
            parts.append(f"{r[0]}:{r[1]}")
        except sqlite3.OperationalError:
            parts.append("-")
    return "|".join(parts)


_TRIP_POWER_RE = re.compile(r"/api/trips/([^/]+)/power")
_TRIP_SPEED_RE = re.compile(r"/api/trips/([^/]+)/speed")
_TRIP_CLIMATE_RE = re.compile(r"/api/trips/([^/]+)/climate")
_TRIP_ROUTE_RE = re.compile(r"/api/trips/([^/]+)/route")
_CHARGE_CURVE_RE = re.compile(r"/api/charges/([^/]+)/curve")

WRITE_PATHS = {
    "POST": {
        "/api/places",
        "/api/ledger",
        "/api/charge-meta",
        "/api/stations",
        "/api/rates",
        "/api/consumables",
        "/api/replacements",
        "/api/settings",
        "/api/tire-sets",
        "/api/notifications/read",
        "/api/notifications/delete",
        "/api/push/subscribe",
        "/api/charges/charger",
        "/api/charges/operator",
        "/api/tariffs",
    },
    "PUT": {
        "/api/charges/location",
        "/api/places",
        "/api/ledger",
        "/api/charge-meta",
        "/api/stations",
        "/api/rates",
        "/api/consumables",
        "/api/replacements",
        "/api/settings",
        "/api/tire-sets",
        "/api/notification-prefs",
        "/api/tariffs",
    },
    "DELETE": {
        "/api/places",
        "/api/ledger",
        "/api/stations",
        "/api/rates",
        "/api/consumables",
        "/api/replacements",
        "/api/tire-sets",
        "/api/push/subscribe",
    },
}


def dispatch_write(
    method: str,
    path: str,
    conn: sqlite3.Connection,
    vehicle_id: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    if path == "/api/places" and method == "POST":
        return {"place": create_place(conn, vehicle_id, body)}, 201
    if path == "/api/places" and method == "PUT":
        return {"place": update_place(conn, vehicle_id, body)}, 200
    if path == "/api/places" and method == "DELETE":
        if body.get("id") in (None, ""):
            raise ValueError("id is required")
        delete_place(conn, vehicle_id, body.get("id"))
        return {"ok": True}, 200
    if path == "/api/ledger" and method == "POST":
        return {"entry": create_manual_entry(conn, vehicle_id, body)}, 201
    if path == "/api/ledger" and method == "PUT":
        return {"entry": update_manual_entry(conn, vehicle_id, body)}, 200
    if path == "/api/ledger" and method == "DELETE":
        lid = body.get("id")
        if not lid:
            raise ValueError("id is required")
        delete_manual_entry(conn, vehicle_id, str(lid))
        return {"ok": True}, 200
    if path == "/api/charge-meta" and method in ("POST", "PUT"):
        return {"meta": upsert_charge_meta(conn, vehicle_id, body)}, 200
    if path == "/api/charges/location" and method == "PUT":
        from process.chargeloc import set_location
        return {"location": set_location(conn, vehicle_id, body)}, 200
    if path == "/api/stations" and method in ("POST", "PUT", "DELETE"):
        return {"error": "saved stations are not used"}, 405
    if path == "/api/rates" and method in ("POST", "PUT"):
        return {"rate": upsert_rate(conn, vehicle_id, body)}, 200
    if path == "/api/rates" and method == "DELETE":
        operator = body.get("operator")
        if not operator:
            raise ValueError("operator is required")
        delete_rate(conn, vehicle_id, str(operator))
        return {"ok": True}, 200
    if path == "/api/consumables" and method == "POST":
        return {"item": create_item(conn, vehicle_id, body)}, 201
    if path == "/api/consumables" and method == "PUT":
        return {"item": update_item(conn, vehicle_id, body)}, 200
    if path == "/api/consumables" and method == "DELETE":
        item_id = body.get("id")
        if not item_id:
            raise ValueError("id is required")
        return {"item": deactivate_item(conn, vehicle_id, str(item_id))}, 200
    if path == "/api/replacements" and method == "POST":
        return {"replacement": save_replacement(conn, vehicle_id, body)}, 201
    if path == "/api/replacements" and method == "PUT":
        return {"replacement": update_replacement(conn, vehicle_id, body)}, 200
    if path == "/api/replacements" and method == "DELETE":
        rid = body.get("id")
        if not rid:
            raise ValueError("id is required")
        delete_replacement(conn, vehicle_id, str(rid))
        return {"ok": True}, 200
    if path == "/api/tire-sets":
        from ledger import tiresets as T
        op = body.get("op") or ("create" if method == "POST" else "update")
        if method == "DELETE":
            if not body.get("id"):
                raise ValueError("id is required")
            T.delete_set(conn, vehicle_id, str(body["id"]))
            return {"ok": True}, 200
        if op == "create":
            return {"set": T.create_set(conn, vehicle_id, body)}, 201
        if op == "update":
            return {"set": T.update_set(conn, vehicle_id, body)}, 200
        if op == "mount":
            return T.mount_set(conn, vehicle_id, body), 200
        if op == "edit_mount":
            return T.update_mount(conn, vehicle_id, body), 200
        if op == "remove":
            return T.remove_current(conn, vehicle_id, body), 200
        if op == "tread":
            return T.add_tread(conn, vehicle_id, body), 201
        raise ValueError("unknown op")
    if path == "/api/charges/charger" and method == "POST":
        from process.evcharger import choose_charger
        return choose_charger(conn, vehicle_id, body), 200
    if path == "/api/charges/operator" and method == "POST":
        from ledger import assign_operator
        return {"meta": assign_operator(conn, vehicle_id, str(body.get("charge_id") or ""), str(body.get("operator") or ""))}, 200
    if path == "/api/tariffs" and method in ("POST", "PUT"):
        from web.tariff import save_tariff
        return save_tariff(conn, vehicle_id, body), 200
    if path == "/api/notifications/read" and method == "POST":
        from web.notify import mark_read
        tab = body.get("tab")
        return mark_read(
            conn, vehicle_id, body.get("id"), bool(body.get("all")),
            tab=str(tab) if tab else None,
        ), 200
    if path == "/api/notifications/delete" and method == "POST":
        from web.notify import delete_notices
        tab = body.get("tab")
        notice_id = body.get("id")
        return delete_notices(
            conn, vehicle_id,
            str(notice_id) if notice_id else None,
            str(tab) if tab and not notice_id else None,
        ), 200
    if path == "/api/notification-prefs" and method == "PUT":
        from web.notify import set_pref
        kind = str(body.get("kind") or "")
        if "enabled" not in body:
            raise ValueError("enabled is required")
        return set_pref(conn, vehicle_id, kind, bool(body.get("enabled"))), 200
    if path == "/api/push/subscribe" and method == "POST":
        from web.notify import save_subscription
        return save_subscription(conn, vehicle_id, body), 200
    if path == "/api/push/subscribe" and method == "DELETE":
        from web.notify import delete_subscription
        return delete_subscription(conn, vehicle_id, str(body.get("endpoint") or "")), 200
    if path == "/api/settings" and method in ("POST", "PUT") and "tires" in body:
        raise ValueError("권장 공기압은 42 psi(문기둥 라벨 값) 고정이라 바꿀 수 없어요")
    if path == "/api/settings" and method in ("POST", "PUT") and "pressure_unit" in body and "registration_date" not in body:
        from web.pressure import set_pressure_unit
        return {"pressure_unit": set_pressure_unit(conn, vehicle_id, str(body.get("pressure_unit") or ""))}, 200
    if path == "/api/settings" and method in ("POST", "PUT"):
        day = body.get("registration_date")
        if not day:
            raise ValueError("registration_date is required")
        return {
            "registration_date": set_registration_date(conn, vehicle_id, str(day))
        }, 200
    raise ValueError("unsupported write")


def make_server(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    tire_alert_bar: float = DEFAULT_TIRE_ALERT_BAR,
    process_interval: float = DEFAULT_PROCESS_INTERVAL,
    seed: bool = True,
    alerts: bool = False,
) -> DashboardServer:
    return DashboardServer(
        (host, port),
        db_path=db_path,
        tire_alert_bar=tire_alert_bar,
        process_interval=process_interval,
        seed=seed,
        alerts=alerts,
    )


def serve_in_thread(server: DashboardServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teslife read-only dashboard.")
    parser.add_argument("--db", default="signals.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--tire-alert-bar",
        type=float,
        default=DEFAULT_TIRE_ALERT_BAR,
        help="warn when latest cold tire pressure is below this (bar)",
    )
    parser.add_argument(
        "--process-interval",
        type=float,
        default=DEFAULT_PROCESS_INTERVAL,
        help="seconds between process() runs (0 disables)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="skip insurance and consumable item seeds",
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="send state-change alerts to TESLIFE_ALERT_WEBHOOK_URL (env); skipped when unset",
    )
    args = parser.parse_args(argv)
    httpd = make_server(
        db_path=args.db,
        host=args.host,
        port=args.port,
        tire_alert_bar=args.tire_alert_bar,
        process_interval=args.process_interval,
        seed=not args.no_seed,
        alerts=args.alerts,
    )
    print(
        f"dashboard http://{args.host}:{httpd.server_address[1]}/  db={args.db}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
