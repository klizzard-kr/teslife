"""Remove the four motion signals and the old map events, then optionally rebuild.

Does not delete trips, charges, points, or any other signal. Dry-run unless
--apply. Rebuilding events uses VehicleSpeed only and updates hard_accel /
hard_brake / hard_turn on the trip row. Nothing here talks to the car.

    python -m process.drop_legacy_motion --db signals.db
    python -m process.drop_legacy_motion --db signals.db --apply --recompute

Back up the database first. --apply without --recompute leaves the map without
these events until --recompute (or a later process) runs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from process import connect, format_ts, parse_ts
from process.geo import locate
from process.speed_events import speed_events

LEGACY_SIGNALS = (
    "LongitudinalAcceleration",
    "LateralAcceleration",
    "PedalPosition",
    "BrakePedal",
)
LEGACY_KINDS = ("accel", "brake", "turn")


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in LEGACY_SIGNALS:
        row = conn.execute("SELECT COUNT(*) FROM signals WHERE name = ?", (name,)).fetchone()
        out[name] = int(row[0] if row else 0)
    try:
        marks = ",".join("?" * len(LEGACY_KINDS))
        row = conn.execute(
            f"SELECT COUNT(*) FROM trip_events WHERE kind IN ({marks})", LEGACY_KINDS
        ).fetchone()
        out["trip_events"] = int(row[0] if row else 0)
    except sqlite3.OperationalError:
        out["trip_events"] = 0
    out["trips"] = int(conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0])
    return out


def apply_delete(conn: sqlite3.Connection) -> dict[str, int]:
    before = counts(conn)
    marks = ",".join("?" * len(LEGACY_SIGNALS))
    with conn:
        conn.execute(f"DELETE FROM signals WHERE name IN ({marks})", LEGACY_SIGNALS)
        try:
            kmarks = ",".join("?" * len(LEGACY_KINDS))
            conn.execute(f"DELETE FROM trip_events WHERE kind IN ({kmarks})", LEGACY_KINDS)
        except sqlite3.OperationalError:
            pass
    return before


def recompute_events(conn: sqlite3.Connection) -> int:
    """Rewrite accel/brake from VehicleSpeed. Leaves every other trip column alone."""
    trips = conn.execute("SELECT vehicle_id, start_ts, end_ts FROM trips").fetchall()
    n = 0
    for vid, start_s, end_s in trips:
        start, end = parse_ts(start_s), parse_ts(end_s)
        samples = []
        for ts, raw in conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'VehicleSpeed' "
            "AND ts >= ? AND ts <= ? ORDER BY ts",
            (vid, start_s, end_s),
        ):
            try:
                value = json.loads(raw)
                samples.append((parse_ts(ts), float(value)))
            except (TypeError, ValueError):
                continue
        found = speed_events(samples)
        route = [
            (r[0], r[1], parse_ts(r[2])) for r in conn.execute(
                "SELECT lat, lon, ts FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
                (vid, start_s),
            )
        ]
        rows = []
        for kind in ("accel", "brake"):
            for ts, rate in found[kind] or []:
                ll = locate(route, ts)
                rows.append((vid, start_s, format_ts(ts), kind, rate,
                             round(ll[0], 6) if ll else None, round(ll[1], 6) if ll else None))
        with conn:
            conn.execute(
                "DELETE FROM trip_events WHERE vehicle_id = ? AND trip_start_ts = ? AND kind IN ('accel', 'brake', 'turn')",
                (vid, start_s),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO trip_events (vehicle_id, trip_start_ts, ts, kind, g, lat, lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "UPDATE trips SET hard_accel = ?, hard_brake = ?, hard_turn = NULL "
                "WHERE vehicle_id = ? AND start_ts = ?",
                (None if found["accel"] is None else len(found["accel"]),
                 None if found["brake"] is None else len(found["brake"]),
                 vid, start_s),
            )
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete legacy motion signals and map events.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true", help="delete the four signals and accel/brake/turn events")
    parser.add_argument("--recompute", action="store_true", help="rebuild accel/brake from VehicleSpeed")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        plan = counts(conn)
        print(
            "signals "
            + ", ".join(f"{k}={plan[k]}" for k in LEGACY_SIGNALS)
            + f"; trip_events={plan['trip_events']}; trips_kept={plan['trips']}"
        )
        if not args.apply and not args.recompute:
            print("dry-run: pass --apply to delete, --recompute to rebuild events. trips are not deleted.")
            return 0
        if args.apply:
            apply_delete(conn)
            print("deleted legacy signals and accel/brake/turn events")
        if args.recompute:
            n = recompute_events(conn)
            print(f"recomputed events for {n} trip(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
