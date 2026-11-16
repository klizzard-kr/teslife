#!/usr/bin/env python3
"""Correct the 2026-11-15 split AC charge by hand. Dry-run unless --apply.

The evening charge was stored twice. Automatic joining only runs for a session
finalized in the last 3 hours. After that window, this command is the way to
make the same correction: row 1 becomes 12:00:35Z–12:37:02Z, AC, 63.3%→66.3%,
and row 2 plus ledger auto-charge:2026-11-15T12:26:44Z are removed.

    python -m process.merge_split_charge --db signals.db
    python -m process.merge_split_charge --db signals.db --apply

It does not wake the car or send a command. Check a copy first with
process.check_charge_merge.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from process import _delete_charge_bundle, connect

KEEP = "2026-11-15T12:00:35Z"  # 21:00:35 KST
DROP = "2026-11-15T12:26:44Z"  # 21:26:44 KST
MERGED_END = "2026-11-15T12:37:02Z"  # 21:37:02 KST
LEDGER = "auto-charge:" + DROP


def _one(conn: sqlite3.Connection, sql: str, args: tuple) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return None


def _count(conn: sqlite3.Connection, sql: str, args: tuple) -> int:
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def plan(conn: sqlite3.Connection, vehicle_id: str | None) -> list[str]:
    where = ""
    args: tuple = (DROP,)
    if vehicle_id:
        where = " AND vehicle_id = ?"
        args = (DROP, vehicle_id)
    charge = _one(conn, f"SELECT vehicle_id, start_ts, end_ts, kind FROM charges WHERE start_ts = ?{where}", args)
    lines = []
    if charge is None:
        lines.append(f"charge {DROP} is not in this database")
    else:
        vid = charge[0]
        lines.append(
            f"would delete charge {vid} {charge[1]}–{charge[2]} kind {charge[3]}"
        )
        lines.append(f"would delete ledger {LEDGER} for {vid} "
                     f"({_count(conn, 'SELECT COUNT(*) FROM ledger WHERE vehicle_id = ? AND id = ?', (vid, LEDGER))} row)")
        for table in ("charge_meta", "charge_locations", "charge_charger"):
            n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE vehicle_id = ? AND charge_id = ?", (vid, DROP))
            lines.append(f"would delete {table} {n} row for {DROP}")
        keep_args = (KEEP, vid)
        keep = _one(conn, "SELECT start_ts, end_ts, kind, start_soc, end_soc FROM charges WHERE start_ts = ? AND vehicle_id = ?", keep_args)
        if keep:
            lines.append(
                f"would correct {vid} {keep[0]} to {MERGED_END} AC "
                f"(now {keep[1]} kind {keep[2]} soc {keep[3]}→{keep[4]})"
            )
        else:
            lines.append(f"kept charge {KEEP} is not in this database")
    return lines


def _correct_keep(conn: sqlite3.Connection, vid: str) -> str:
    """Point row 1 at the real end of the second session and sum the energy."""
    from process import _load_events, charge_energy, parse_ts

    keep = conn.execute(
        "SELECT start_soc, end_soc, kwh_added FROM charges WHERE vehicle_id = ? AND start_ts = ?",
        (vid, KEEP),
    ).fetchone()
    drop = conn.execute(
        "SELECT end_soc, kwh_added FROM charges WHERE vehicle_id = ? AND start_ts = ?",
        (vid, DROP),
    ).fetchone()
    if keep is None:
        return f"kept charge {KEEP} is not in this database"
    start_soc = keep[0] if keep[0] is not None else 63.3
    end_soc = drop[0] if drop is not None and drop[0] is not None else keep[1]
    kwh = None
    try:
        ev = _load_events(conn, vid)
        energy = charge_energy(ev, parse_ts(KEEP), parse_ts(MERGED_END))
        if energy.get("charger_kwh"):
            kwh = energy["charger_kwh"]
    except Exception:
        kwh = None
    if kwh is None:
        kwh = float(keep[2] or 0) + float((drop[1] if drop else 0) or 0)
    conn.execute(
        "UPDATE charges SET end_ts = ?, kind = 'AC', start_soc = ?, end_soc = ?, "
        "kwh_added = ?, charger_kwh = ? WHERE vehicle_id = ? AND start_ts = ?",
        (MERGED_END, start_soc if start_soc is not None else 63.3, end_soc, kwh, kwh, vid, KEEP),
    )
    return (
        f"corrected {vid} {KEEP}–{MERGED_END} AC soc {start_soc}→{end_soc} kwh {kwh}"
    )


def apply(conn: sqlite3.Connection, vehicle_id: str | None) -> list[str]:
    args: tuple = (DROP,)
    sql = "SELECT DISTINCT vehicle_id FROM charges WHERE start_ts = ?"
    if vehicle_id:
        sql += " AND vehicle_id = ?"
        args = (DROP, vehicle_id)
    try:
        vids = [row[0] for row in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        vids = []
    if not vids and vehicle_id:
        vids = [vehicle_id]
    done = []
    with conn:
        for vid in vids:
            done.append(_correct_keep(conn, vid))
            if conn.execute(
                "SELECT 1 FROM charges WHERE vehicle_id = ? AND start_ts = ?", (vid, DROP)
            ).fetchone():
                _delete_charge_bundle(conn, vid, DROP)
                done.append(f"deleted charge bundle {vid} {DROP} and {LEDGER}")
    if not done:
        done.append(f"charge {DROP} is not in this database")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge the 2026-11-15 split AC charge.")
    parser.add_argument("--db", default="signals.db")
    parser.add_argument("--vehicle", default="")
    parser.add_argument("--apply", action="store_true", help="write the database; default is a dry run")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"dry-run: database not found at {path}")
        print(f"would delete charge {DROP}, ledger {LEDGER}, and related charge rows")
        print(f"would keep {KEEP}")
        return 0
    conn = connect(path)
    try:
        vid = args.vehicle or None
        lines = apply(conn, vid) if args.apply else plan(conn, vid)
    finally:
        conn.close()
    prefix = "applied" if args.apply else "dry-run"
    for line in lines:
        print(f"{prefix}: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
