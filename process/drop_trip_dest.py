"""One-off migration: delete the navigation destination data teslife no longer uses.

Removes the `trips` columns dest_name, dest_lat, dest_lon, dest_km, dest_arrival_pct and the
raw `signals` rows DestinationName, DestinationLocation, MilesToArrival, MinutesToArrival,
ExpectedEnergyPercentAtTripArrival. Trip start/end coordinates, the reverse-geocoded area
names (geocode_cache) and favourite places are kept. Run once at deploy time, after the new
code (which neither reads nor writes these) is running:

    python3 -m process.drop_trip_dest \\
        --db signals.db \\
        --backup signals.db.bak-YYYYMMDD-nodest

It first copies the whole database to --backup with SQLite's online backup and checks the
copy, then deletes. It prints counts only, never names or coordinates. Running it again is a
no-op.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

DEST_COLUMNS = ("dest_name", "dest_lat", "dest_lon", "dest_km", "dest_arrival_pct")
DEST_SIGNALS = ("DestinationName", "DestinationLocation", "MilesToArrival", "MinutesToArrival",
                "ExpectedEnergyPercentAtTripArrival")


def _trip_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(trips)").fetchall()}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _counts(conn: sqlite3.Connection) -> tuple[list[str], int, int, int]:
    cols = [c for c in DEST_COLUMNS if c in _trip_columns(conn)] if _has_table(conn, "trips") else []
    with_dest = 0
    if cols:
        cond = " OR ".join(f"{c} IS NOT NULL" for c in cols)
        with_dest = conn.execute(f"SELECT COUNT(*) FROM trips WHERE {cond}").fetchone()[0]
    trips = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] if _has_table(conn, "trips") else 0
    sig = 0
    if _has_table(conn, "signals"):
        marks = ",".join("?" * len(DEST_SIGNALS))
        sig = conn.execute(f"SELECT COUNT(*) FROM signals WHERE name IN ({marks})", DEST_SIGNALS).fetchone()[0]
    return cols, with_dest, trips, sig


def drop_trip_dest(db: str | Path, backup: str | Path) -> dict[str, Any]:
    db, backup = Path(db), Path(backup)
    if not db.exists():
        raise FileNotFoundError(db)
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        cols, with_dest, trips, sig = _counts(conn)
        if not cols and not sig:
            return {"changed": False, "columns": [], "trips_with_dest": 0, "signals_deleted": 0, "backup": None}
        if backup.exists():
            raise FileExistsError(f"backup already exists: {backup}")
        dst = sqlite3.connect(str(backup))
        try:
            conn.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            copied = _counts(dst)
        finally:
            dst.close()
        backup.chmod(0o600)
        if ok != "ok" or copied[1:] != (with_dest, trips, sig):
            raise RuntimeError("backup check failed; nothing was changed")
        with conn:
            for c in cols:
                conn.execute(f"ALTER TABLE trips DROP COLUMN {c}")
            if sig:
                marks = ",".join("?" * len(DEST_SIGNALS))
                conn.execute(f"DELETE FROM signals WHERE name IN ({marks})", DEST_SIGNALS)
        left = _counts(conn)
        if left[0] or left[3] or left[2] != trips:
            raise RuntimeError("destination data still present")
        return {"changed": True, "columns": cols, "trips_with_dest": with_dest, "trips": trips,
                "signals_deleted": sig, "backup": str(backup)}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--backup", required=True, help="new file; the run stops if it exists")
    args = ap.parse_args(argv)
    print(json.dumps(drop_trip_dest(args.db, args.backup), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
