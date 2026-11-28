"""Home card: last stored location and the single place named 집.

Reads telemetry the car already sent. Does not wake the car or send a command.
Distance uses the same home_leg as the away-from-home notice (OSRM, else a straight line).
Saving touches only the places row whose name is 집. Trips, charges, routes, the ledger,
and consumables are left as they are. The places table is not altered.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from process import _latlon, decode_value, ensure_process_schema
from process.alerts import _home_flag, home_leg

HOME_NAME = "집"


def _home_row(conn: sqlite3.Connection, vehicle_id: str) -> sqlite3.Row | tuple | None:
    try:
        return conn.execute(
            "SELECT id, lat, lon, radius_m FROM places "
            "WHERE vehicle_id = ? AND TRIM(name) = ? ORDER BY id LIMIT 1",
            (vehicle_id, HOME_NAME),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def home_point(row: sqlite3.Row | tuple | None) -> tuple[float, float] | None:
    if not row or row[1] is None or row[2] is None:
        return None
    return float(row[1]), float(row[2])


def home_dict(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row[0],
        "name": HOME_NAME,
        "lat": float(row[1]),
        "lon": float(row[2]),
        "radius_m": float(row[3]) if row[3] is not None else 150.0,
    }


def _heading_number(value: Any) -> int | float | None:
    """Degrees already stored, or a numeric string. Missing and junk stay None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    rounded = round(num, 1)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def last_heading(conn: sqlite3.Connection, vehicle_id: str) -> int | float | None:
    """Newest GpsHeading the bridge kept. The row's ts is when it was received.

    The telemetry config does not ask for GpsHeading yet, so None is normal.
    Nothing is requested from the car.
    """
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'GpsHeading' "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row[0] in (None, ""):
        return None
    try:
        return _heading_number(decode_value(row[0]))
    except (TypeError, ValueError):
        return None


def last_fix(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any] | None:
    """The newest Location signal already stored. Nothing is requested from the car."""
    try:
        row = conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'Location' "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[1]:
        return None
    try:
        point = _latlon(decode_value(row[1]))
    except (TypeError, ValueError):
        return None
    if point is None:
        return None
    return {"lat": float(point[0]), "lon": float(point[1]), "received_at": row[0]}


def last_gear(conn: sqlite3.Connection, vehicle_id: str) -> str | None:
    """Newest stored Gear (P/R/N/D). Missing or unreadable stays None.

    Same signal /api/live uses for trip_in_progress. Nothing is requested from the car.
    """
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Gear' "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row[0] in (None, ""):
        return None
    try:
        value = decode_value(row[0])
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def driving_flag(gear: str | None) -> bool | None:
    """True when Gear is present and not P. Same rule as in_progress_flags."""
    if not gear:
        return None
    return gear != "P"


def located_at_home(conn: sqlite3.Connection, vehicle_id: str) -> bool | None:
    """The newest LocatedAtHome sample. Missing or unreadable stays None."""
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'LocatedAtHome' "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row[0] is None or row[0] == "":
        return None
    try:
        return _home_flag(decode_value(row[0]))
    except (TypeError, ValueError):
        return None


def car_location_payload(
    conn: sqlite3.Connection,
    vehicle_id: str,
    route_opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Coordinates, when they arrived, heading, the at-home flag, and kilometres home.

    heading is the newest stored GpsHeading in degrees, or None when the car has not
    sent one. home_km and home_via come from home_leg. Both are empty when either
    point is missing. area is the cached primary admin name (동·읍·면, else 구,
    else 시·군, else 도), or None. area_en is the English primary name, or None.
    road_addr is the full road address ('시도 시군구 도로명 건물번호 (건물명)'), or None.
    jibun_addr is the full lot address ('시도 시군구 동 지번'), or None. driving is True when the newest Gear is present and not P, False when it is P, and None when Gear is missing. That is the same rule /api/live uses for trip_in_progress. This function does not call
    a geocoder; the process loop fills the cache when the last Location moves about
    100 m (a 3-decimal cache cell), and gradually refills older rows that lack 구/시/도 or the two addresses.
    """
    from process.geocode import admin_display, cached_addresses

    fix = last_fix(conn, vehicle_id)
    row = _home_row(conn, vehicle_id)
    origin = (fix["lat"], fix["lon"]) if fix else None
    km, via = home_leg(origin, home_point(row), route_opener)
    area, _unused_up = admin_display(conn, origin[0], origin[1], "ko") if origin else (None, None)
    area_en, _unused_up_en = admin_display(conn, origin[0], origin[1], "en") if origin else (None, None)
    road_addr, jibun_addr = cached_addresses(conn, origin[0], origin[1]) if origin else (None, None)
    return {
        "vehicle_id": vehicle_id,
        "lat": None if fix is None else fix["lat"],
        "lon": None if fix is None else fix["lon"],
        "received_at": None if fix is None else fix["received_at"],
        "located_at_home": located_at_home(conn, vehicle_id),
        "heading": last_heading(conn, vehicle_id),
        "home_km": km,
        "home_via": via,
        "home": home_dict(row),
        "area": area,
        "area_en": area_en,
        "road_addr": road_addr,
        "jibun_addr": jibun_addr,
        "driving": driving_flag(last_gear(conn, vehicle_id)),
    }


def home_payload(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    return {"vehicle_id": vehicle_id, "home": home_dict(_home_row(conn, vehicle_id))}


def _flag(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1"):
        return True
    return False


def _pin(body: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" not in body and "lon" not in body:
        return None
    try:
        lat = float(body.get("lat"))
        lon = float(body.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("위도와 경도가 필요해요") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("위도·경도 범위가 아니에요")
    return round(lat, 6), round(lon, 6)


def save_home(
    conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Create or replace the single 집 row. Returns (home, created).

    A pin (lat, lon) wins when both a pin and use_last are sent. use_last reads the
    stored Location and does not contact the car. Other place names are not written.
    """
    ensure_process_schema(conn)
    pin = _pin(body)
    if pin is None:
        if not _flag(body.get("use_last")):
            raise ValueError("위도와 경도가 필요해요")
        fix = last_fix(conn, vehicle_id)
        if fix is None:
            raise ValueError("마지막 위치가 없어요")
        pin = (round(fix["lat"], 6), round(fix["lon"], 6))
    lat, lon = pin
    existing = _home_row(conn, vehicle_id)
    if existing:
        with conn:
            conn.execute(
                "UPDATE places SET lat = ?, lon = ? WHERE vehicle_id = ? AND id = ?",
                (lat, lon, vehicle_id, existing[0]),
            )
        row = _home_row(conn, vehicle_id)
        saved = home_dict(row)
        if saved is None:
            raise KeyError("저장된 집이 없어요")
        return saved, False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (vehicle_id, HOME_NAME, lat, lon, 150, now),
        )
    saved = home_dict(_home_row(conn, vehicle_id))
    if saved is None:
        raise KeyError("저장된 집이 없어요")
    return saved, True


def delete_home(conn: sqlite3.Connection, vehicle_id: str) -> None:
    """Remove only the 집 row this card uses. Other places stay."""
    ensure_process_schema(conn)
    row = _home_row(conn, vehicle_id)
    if row is None:
        raise KeyError("저장된 집이 없어요")
    with conn:
        conn.execute(
            "DELETE FROM places WHERE vehicle_id = ? AND id = ?",
            (vehicle_id, row[0]),
        )
