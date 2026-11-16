"""Show how stored charge places would read under the charger-name rule.

The default is a dry run: it prints the charge, the date, the current place,
the new place, and the basis (충전소, 건물, or 주소) and does not write.
--apply updates location_text only. Coordinates and every other field stay.
An empty or failed lookup skips that row. One station id is looked up once.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any, Callable

from process.evcharger import _charger_place_near


def _day(ts: str) -> str:
    try:
        from process import KST, parse_ts
        return parse_ts(ts).astimezone(KST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return (ts or "")[:10]


def _propose(
    lat: float, lng: float, name: str, addr: str,
    search: Callable[..., dict[str, Any]] | None,
    reverse: Callable[[float, float], tuple[str, str] | None] | None,
) -> tuple[str, str] | None:
    title = _charger_place_near(lat, lng, name, addr, search)
    if title:
        return title, "충전소"
    if reverse is None:
        from process.evcharger import _reverse_parts
        reverse = _reverse_parts
    try:
        found = reverse(lat, lng)
    except Exception:
        return None
    if not found:
        return None
    text, basis = found
    text = str(text or "").strip()
    if text and basis in ("건물", "주소"):
        return text, basis
    return None


def _rows(conn: sqlite3.Connection, vehicle_id: str | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.vehicle_id, c.start_ts, m.location_text, m.station_name, m.station_addr, "
        "l.lat, l.lon, cc.stat_id "
        "FROM charges c "
        "LEFT JOIN charge_meta m ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts "
        "LEFT JOIN charge_locations l ON l.vehicle_id = c.vehicle_id AND l.charge_id = c.start_ts "
        "LEFT JOIN charge_charger cc ON cc.vehicle_id = c.vehicle_id AND cc.charge_id = c.start_ts"
    )
    args: tuple[Any, ...] = ()
    if vehicle_id:
        sql += " WHERE c.vehicle_id = ?"
        args = (vehicle_id,)
    sql += " ORDER BY c.vehicle_id, c.start_ts"
    return list(conn.execute(sql, args))


def _write_place(conn: sqlite3.Connection, vid: str, charge_id: str, place: str) -> None:
    cur = conn.execute(
        "UPDATE charge_meta SET location_text = ? WHERE vehicle_id = ? AND charge_id = ?",
        (place, vid, charge_id),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, ?, ?)",
            (vid, charge_id, place),
        )


def relabel(
    conn: sqlite3.Connection,
    search: Callable[..., dict[str, Any]] | None = None,
    reverse: Callable[[float, float], tuple[str, str] | None] | None = None,
    apply: bool = False,
    vehicle_id: str | None = None,
) -> list[dict[str, str]]:
    """Planned place changes. apply=False does not write."""
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    cache: dict[str, tuple[str, str] | None] = {}
    planned: list[dict[str, str]] = []
    try:
        return _collect(conn, cache, planned, search, reverse, apply, vehicle_id)
    finally:
        conn.row_factory = previous_factory


def _collect(
    conn: sqlite3.Connection,
    cache: dict[str, tuple[str, str] | None],
    planned: list[dict[str, str]],
    search: Callable[..., dict[str, Any]] | None,
    reverse: Callable[[float, float], tuple[str, str] | None] | None,
    apply: bool,
    vehicle_id: str | None,
) -> list[dict[str, str]]:
    for row in _rows(conn, vehicle_id):
        vid = str(row["vehicle_id"])
        charge_id = str(row["start_ts"])
        if row["lat"] is None or row["lon"] is None:
            continue
        stat_id = str(row["stat_id"] or "").strip()
        name = str(row["station_name"] or "")
        addr = str(row["station_addr"] or "")
        try:
            lat, lng = float(row["lat"]), float(row["lon"])
            if stat_id:
                if stat_id not in cache:
                    cache[stat_id] = _propose(lat, lng, name, addr, search, reverse)
                found = cache[stat_id]
            else:
                found = _propose(lat, lng, name, addr, search, reverse)
        except Exception:
            continue
        if not found:
            continue
        new, basis = found
        current = str(row["location_text"] or "").strip()
        planned.append({
            "vehicle_id": vid,
            "charge_id": charge_id,
            "date": _day(charge_id),
            "current": current,
            "new": new,
            "basis": basis,
        })
        if apply and new != current:
            _write_place(conn, vid, charge_id, new)
    if apply:
        conn.commit()
    return planned


def format_line(row: dict[str, str]) -> str:
    return "\t".join((row["charge_id"], row["date"], row["current"], row["new"], row["basis"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relabel charge places. Dry-run unless --apply.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vehicle", default="")
    args = parser.parse_args(argv)
    from process import connect

    conn = connect(args.db)
    try:
        conn.row_factory = sqlite3.Row
        rows = relabel(conn, apply=args.apply, vehicle_id=args.vehicle.strip() or None)
    finally:
        conn.close()
    print("충전\t날짜\t지금 장소\t새 장소\t근거")
    for row in rows:
        print(format_line(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
