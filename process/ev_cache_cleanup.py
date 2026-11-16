"""One-time removal of the public-charger cache that is far from this car.

v1.7.0 stored a whole region list (Seoul, 76k chargers) and a history row for
every charger on each fetch. This deletes catalog, status, and history rows
that are not within KEEP_M of a charge location or saved place, and clears the
region-page cache. User-picked chargers, trips, charges, routes, signals,
charge_meta, the ledger, and service rows are left in place.

Run once from the process loop. `python -m process.ev_cache_cleanup --db PATH`
does the same pass, including VACUUM when something was deleted.
"""

from __future__ import annotations

import argparse
import sqlite3

from process.evcharger import KEEP_M, reference_points
from process.geo import distance_m

FLAG = "ev_cache_cleanup:v171"
_PROTECTED_SOURCES = ("user", "picked")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone()
    return row is not None


def cleanup_public_cache(conn: sqlite3.Connection, vacuum: bool = True) -> dict[str, int]:
    """Delete far public-charger cache rows. Returns counts only."""
    stats = {"chargers": 0, "status": 0, "history": 0, "regions": 0}
    if not any(_table_exists(conn, name) for name in ("charge_locations", "stations", "places")):
        return stats
    anchors = reference_points(conn)
    protected: set[tuple[str, str]] = set()
    if _table_exists(conn, "charge_charger"):
        marks = ",".join("?" * len(_PROTECTED_SOURCES))
        try:
            rows = conn.execute(
                f"SELECT stat_id, chger_id FROM charge_charger "
                f"WHERE source IN ({marks}) AND stat_id IS NOT NULL AND chger_id IS NOT NULL",
                _PROTECTED_SOURCES,
            ).fetchall()
            protected = {(str(stat_id), str(chger_id)) for stat_id, chger_id in rows}
        except sqlite3.OperationalError:
            protected = set()
    if not _table_exists(conn, "ev_chargers"):
        with conn:
            _clear_regions(conn, stats)
        return stats
    keep: list[tuple[str, str]] = []
    drop: list[tuple[str, str]] = []
    for stat_id, chger_id, lat, lng in conn.execute(
        "SELECT stat_id, chger_id, lat, lng FROM ev_chargers"
    ):
        key = (str(stat_id), str(chger_id))
        near = False
        if lat is not None and lng is not None and anchors:
            near = any(distance_m((float(lat), float(lng)), point) <= KEEP_M for point in anchors)
        if key in protected or near:
            keep.append(key)
        else:
            drop.append(key)
    with conn:
        if drop:
            conn.executemany(
                "DELETE FROM ev_chargers WHERE stat_id = ? AND chger_id = ?", drop,
            )
            stats["chargers"] = len(drop)
        if _table_exists(conn, "ev_status"):
            stats["status"] = _delete_orphans(conn, "ev_status")
        if _table_exists(conn, "ev_status_hist"):
            stats["history"] = _delete_orphans(conn, "ev_status_hist")
        _clear_regions(conn, stats)
    if vacuum and any(stats.values()):
        conn.execute("VACUUM")
    return stats


def _delete_orphans(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(
        f"DELETE FROM {table} WHERE NOT EXISTS ("
        "SELECT 1 FROM ev_chargers c WHERE c.stat_id = "
        f"{table}.stat_id AND c.chger_id = {table}.chger_id)"
    ).rowcount


def _clear_regions(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    if not _table_exists(conn, "ev_region"):
        return
    stats["regions"] = conn.execute("DELETE FROM ev_region").rowcount


def maybe_cleanup(conn: sqlite3.Connection) -> dict[str, int] | None:
    """Run the cleanup once. Later calls do nothing."""
    try:
        row = conn.execute(
            "SELECT value FROM process_meta WHERE key = ?", (FLAG,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and row[0] == "1":
        return None
    stats = cleanup_public_cache(conn, vacuum=True)
    try:
        with conn:
            conn.execute(
                "INSERT INTO process_meta (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (FLAG,),
            )
    except sqlite3.OperationalError:
        pass
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drop far public-charger cache rows and VACUUM.")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        stats = cleanup_public_cache(conn, vacuum=True)
    finally:
        conn.close()
    print(
        "chargers {chargers}, status {status}, history {history}, regions {regions}".format(**stats)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
