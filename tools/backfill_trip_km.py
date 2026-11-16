"""Fill short trip distances from speed, then the saved path.

The default is a dry run. It prints the trip, the date, the stored distance,
the new distance, and the source. It does not write. --apply updates km and
km_source, and clears efficiency when the distance stays under 0.1 km.
Existing rows are not touched unless --apply is passed.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from typing import Any

from process import ODO_STEP_KM, MIN_TRIP_KM, connect, parse_ts
from process.evcharger import KST
from process.geo import distance_m


def _day(ts: str) -> str:
    try:
        return parse_ts(ts).astimezone(KST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return (ts or "")[:10]


def _speeds(conn: sqlite3.Connection, vid: str, start: str, end: str) -> list[tuple[datetime, float]]:
    rows = conn.execute(
        "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'VehicleSpeed' "
        "AND ts >= ? AND ts <= ? ORDER BY ts",
        (vid, start, end),
    ).fetchall()
    out = []
    for ts, raw in rows:
        try:
            out.append((parse_ts(ts), float(raw)))
        except (TypeError, ValueError):
            continue
    return out


def _speed_km(samples: list[tuple[datetime, float]], end: datetime) -> float | None:
    from process import MOVING_GAP_S

    if len(samples) < 2:
        return None
    ordered = sorted(samples, key=lambda item: item[0])
    if end > ordered[-1][0]:
        ordered.append((end, 0.0))
    total = 0.0
    spanned = False
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:]):
        gap = (t1 - t0).total_seconds()
        if gap <= 0:
            continue
        gap = min(MOVING_GAP_S, gap)
        total += (max(0.0, v0) + max(0.0, v1)) / 2.0 * (gap / 3600.0)
        spanned = True
    if not spanned or total <= 0:
        return None
    return total


def _path_km(conn: sqlite3.Connection, vid: str, start: str) -> float | None:
    rows = conn.execute(
        "SELECT lat, lon FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
        (vid, start),
    ).fetchall()
    if len(rows) < 2:
        return None
    total = 0.0
    for (lat0, lon0), (lat1, lon1) in zip(rows, rows[1:]):
        total += distance_m((lat0, lon0), (lat1, lon1))
    if total <= 0:
        return None
    return total / 1000.0


def propose(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[float, str] | None:
    vid, start, end = row["vehicle_id"], row["start_ts"], row["end_ts"]
    try:
        end_dt = parse_ts(end)
    except (TypeError, ValueError):
        return None
    try:
        sped = _speed_km(_speeds(conn, vid, start, end), end_dt)
    except sqlite3.OperationalError:
        sped = None
    if sped is not None:
        return round(sped, 3), "speed"
    try:
        path = _path_km(conn, vid, start)
    except sqlite3.OperationalError:
        path = None
    if path is not None:
        return round(path, 3), "gps"
    return None


def backfill(conn: sqlite3.Connection, apply: bool = False, vehicle_id: str | None = None) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    sql = "SELECT vehicle_id, start_ts, end_ts, km, kwh FROM trips WHERE km IS NULL OR km < ?"
    args: list[Any] = [ODO_STEP_KM]
    if vehicle_id:
        sql += " AND vehicle_id = ?"
        args.append(vehicle_id)
    sql += " ORDER BY vehicle_id, start_ts"
    planned = []
    for row in conn.execute(sql, args):
        found = propose(conn, row)
        if not found:
            continue
        new, source = found
        old = row["km"]
        if old is not None and abs(float(old) - new) < 0.0005:
            continue
        item = {
            "vehicle_id": row["vehicle_id"],
            "start_ts": row["start_ts"],
            "date": _day(row["start_ts"]),
            "old_km": None if old is None else float(old),
            "new_km": new,
            "source": source,
        }
        planned.append(item)
        if not apply:
            continue
        eff_km = new if new >= MIN_TRIP_KM else None
        kwh = row["kwh"]
        per = (new / float(kwh)) if eff_km is not None and kwh is not None and float(kwh) > 0.01 else None
        wh = (float(kwh) * 1000.0 / new) if per is not None else None
        conn.execute(
            "UPDATE trips SET km = ?, km_source = ?, km_per_kwh = ?, wh_per_km = ? "
            "WHERE vehicle_id = ? AND start_ts = ?",
            (new, source, per, wh, row["vehicle_id"], row["start_ts"]),
        )
    if apply:
        conn.commit()
    return planned


def format_line(row: dict[str, Any]) -> str:
    old = "" if row["old_km"] is None else f"{row['old_km']:.3f}"
    return "\t".join((row["start_ts"], row["date"], old, f"{row['new_km']:.3f}", row["source"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill short trip distances. Dry-run unless --apply.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vehicle", default="")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        rows = backfill(conn, apply=args.apply, vehicle_id=args.vehicle.strip() or None)
    finally:
        conn.close()
    print("주행\t날짜\t지금 거리\t새 거리\t근거")
    for row in rows:
        print(format_line(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
