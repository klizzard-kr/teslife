"""Point stored charge operators at busiId and bnm. Default is a dry run.

Back up the database file before --apply. This does not run unless --apply
is passed, and it does not wake a car or call the network.

    python -m tools.backfill_busi --db signals.db
    python -m tools.backfill_busi --db signals.db --apply

One charge can be pointed at one catalog station. Pass the ids yourself.
This file does not contain a charge id or a station id.

    python -m tools.backfill_busi --db signals.db --vehicle VEHICLE --charge CHARGE_ID --station STAT_ID
    python -m tools.backfill_busi --db signals.db --vehicle VEHICLE --charge CHARGE_ID --station STAT_ID --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from process.evcharger import ensure_schema
from process.operators import busi_code, display_name, resolve


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def plan_operators(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Rows whose operator label would change to the group's bnm."""
    try:
        rows = conn.execute(
            "SELECT vehicle_id, charge_id, operator FROM charge_meta "
            "WHERE operator IS NOT NULL AND TRIM(operator) != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    changes = []
    for row in rows:
        current = str(row[2] or "").strip()
        found = resolve(current)
        if not found:
            continue
        label = str(found["bnm"])
        if current == label:
            continue
        changes.append({
            "vehicle_id": str(row[0]),
            "charge_id": str(row[1]),
            "from": current,
            "to": label,
            "busi_id": str(found["busi_id"]),
        })
    return changes


def apply_operators(conn: sqlite3.Connection, changes: list[dict[str, str]]) -> int:
    with conn:
        for row in changes:
            conn.execute(
                "UPDATE charge_meta SET operator = ? WHERE vehicle_id = ? AND charge_id = ?",
                (row["to"], row["vehicle_id"], row["charge_id"]),
            )
            try:
                conn.execute(
                    "UPDATE charge_charger SET busi_id = ?, bnm = ?, busi_nm = ? "
                    "WHERE vehicle_id = ? AND charge_id = ? AND (busi_id IS NULL OR TRIM(busi_id) = '')",
                    (row["busi_id"], row["to"], row["to"], row["vehicle_id"], row["charge_id"]),
                )
            except sqlite3.OperationalError:
                continue
    return len(changes)


def plan_station(conn: sqlite3.Connection, vehicle_id: str, charge_id: str, stat_id: str) -> dict[str, str]:
    """What one assignment would write. Missing rows say so. No coordinates."""
    charge = conn.execute(
        "SELECT start_ts FROM charges WHERE vehicle_id = ? AND start_ts = ?",
        (vehicle_id, charge_id),
    ).fetchone()
    station = conn.execute(
        "SELECT stat_id, stat_nm, busi_id, bnm, busi_nm, output_kw, chger_type "
        "FROM ev_chargers WHERE stat_id = ? LIMIT 1",
        (stat_id,),
    ).fetchone()
    out = {
        "charge": "yes" if charge else "no",
        "station": "yes" if station else "no",
        "operator": "",
        "busi_id": "",
        "stat_nm": "",
    }
    if station is None:
        return out
    label = display_name(station["busi_id"], station["bnm"], station["busi_nm"])
    out["operator"] = label
    out["busi_id"] = str(station["busi_id"] or "") or busi_code(label)
    out["stat_nm"] = str(station["stat_nm"] or "")
    return out


def apply_station(conn: sqlite3.Connection, vehicle_id: str, charge_id: str, stat_id: str) -> dict[str, str]:
    planned = plan_station(conn, vehicle_id, charge_id, stat_id)
    if planned["charge"] != "yes" or planned["station"] != "yes":
        return planned
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id, chger_id, busi_id, bnm, busi_nm, "
            "output_kw, chger_type, stat_nm, source, candidates_json, updated_ts) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, 'user', NULL, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
            "stat_id = excluded.stat_id, chger_id = NULL, busi_id = excluded.busi_id, "
            "bnm = excluded.bnm, busi_nm = excluded.busi_nm, stat_nm = excluded.stat_nm, source = 'user', "
            "candidates_json = NULL, updated_ts = excluded.updated_ts",
            (vehicle_id, charge_id, stat_id, planned["busi_id"], planned["operator"], planned["operator"],
             planned["stat_nm"], now),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, station_name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET operator = excluded.operator, "
            "station_name = excluded.station_name",
            (vehicle_id, charge_id, planned["operator"], planned["stat_nm"] or None),
        )
    planned["wrote"] = "yes"
    return planned


def _report(changes: list[dict[str, str]], *, apply: bool) -> str:
    labels = sorted({row["from"] + " -> " + row["to"] for row in changes})
    lines = [
        "backup=copy the database file before --apply",
        "dry_run=" + ("no" if apply else "yes"),
        "apply=" + ("yes" if apply else "no"),
        "network=no",
        f"operator_rows={len(changes)}",
        "name_changes=" + (", ".join(labels) if labels else "none"),
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run operator backfill. Write only with --apply.")
    parser.add_argument("--db", required=True, help="SQLite file. Not modified unless --apply.")
    parser.add_argument("--apply", action="store_true", help="Write. Default is a dry run.")
    parser.add_argument("--vehicle", help="Vehicle id for --charge.")
    parser.add_argument("--charge", help="Charge id to point at --station.")
    parser.add_argument("--station", help="Catalog stat id. Not stored in this repository.")
    args = parser.parse_args(argv)
    if bool(args.charge) != bool(args.station):
        print("error=charge and station are used together", file=sys.stderr)
        return 1
    if args.charge and not args.vehicle:
        print("error=vehicle is required with charge", file=sys.stderr)
        return 1
    conn = _open(args.db)
    try:
        if args.charge:
            if args.apply:
                result = apply_station(conn, args.vehicle, args.charge, args.station)
                result["dry_run"] = "no"
            else:
                result = plan_station(conn, args.vehicle, args.charge, args.station)
                result["dry_run"] = "yes"
            print("backup=copy the database file before --apply")
            print("network=no")
            for key in ("dry_run", "charge", "station", "busi_id", "operator", "stat_nm"):
                if key in result:
                    print(f"{key}={result[key]}")
            if result.get("charge") != "yes" or result.get("station") != "yes":
                return 2
            return 0
        changes = plan_operators(conn)
        if args.apply:
            apply_operators(conn, changes)
        sys.stdout.write(_report(changes, apply=bool(args.apply)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
