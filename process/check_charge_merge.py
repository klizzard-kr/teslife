#!/usr/bin/env python3
"""Check a database copy before deploy. The file you pass is not modified.

Each pass copies the database, marks the processor version as 20, and runs a
full rebuild. The first pass is 21:45 that evening: charges 13→12, ledger
16→15, and the kept row is 12:00:35Z–12:37:02Z, AC, 63.3%→66.3%, about 2.0 kWh.
Every other charge, charge_meta row, stored location, and other ledger amount
stays. The second pass uses the current time, which is more than 3 hours after
that charge, and must change nothing.

    python -m process.check_charge_merge --db signals.db.bak-20261115-a898c75

After that window, automatic joining does not run. The same merged row is
written by process.merge_split_charge --apply.

`active_s` is an added column. Rows already stored keep their values; a missing
column is created empty and does not rewrite or delete them.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import connect, process_db

KEEP = "2026-11-15T12:00:35Z"
DROP = "2026-11-15T12:26:44Z"
LEDGER = "auto-charge:" + DROP
KEEP_LEDGER = "auto-charge:" + KEEP
# 21:45 KST, after the port closed and inside the 3 h window of the first session.
NOW = datetime(2026, 11, 15, 12, 45, tzinfo=timezone.utc)


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.OperationalError:
        return []


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        "charges": _rows(conn, "SELECT vehicle_id, start_ts, end_ts, kind, start_soc, end_soc, kwh_added FROM charges ORDER BY 1, 2"),
        "meta": _rows(conn, "SELECT vehicle_id, charge_id, operator, total_krw, kwh_billed, discount_krw FROM charge_meta ORDER BY 1, 2"),
        "locations": _rows(conn, "SELECT vehicle_id, charge_id, lat, lon, source FROM charge_locations ORDER BY 1, 2"),
        "ledger": _rows(conn, "SELECT vehicle_id, id, date, category, amount_krw, source FROM ledger ORDER BY 1, 2"),
    }


def _charges_by_start(rows: list[tuple]) -> dict[tuple, tuple]:
    return {(row[0], row[1]): row for row in rows}


MERGED_END = "2026-11-15T12:37:02Z"


def _expects_merge(now: datetime) -> bool:
    from process import parse_ts
    return now - parse_ts(KEEP) <= timedelta(hours=3)


def _force_version_bump(conn: sqlite3.Connection) -> None:
    """Pretend the stored processor is v1.14.2 so this pass reloads every signal."""
    from process import _meta_set
    vids: set[str] = set()
    for sql in (
        "SELECT DISTINCT vehicle_id FROM signals",
        "SELECT DISTINCT vehicle_id FROM charges",
    ):
        try:
            vids.update(row[0] for row in conn.execute(sql))
        except sqlite3.OperationalError:
            pass
    for vid in vids:
        _meta_set(conn, f"version:{vid}", "20")


def audit(source: Path, now: datetime = NOW) -> tuple[bool, list[str]]:
    """Process a copy as a version bump. The source file is not written.

    Inside the 3 h window the two evening rows become one. Later, nothing changes.
    """
    lines: list[str] = []
    if not source.exists():
        return False, [f"database not found: {source}"]
    tmp = Path(tempfile.mkdtemp(prefix="teslife-charge-check-"))
    copy = tmp / "signals.db"
    shutil.copy2(source, copy)
    conn = connect(copy)
    try:
        before = _snapshot(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(charges)")}
        lines.append(f"active_s column before: {'yes' if 'active_s' in cols else 'no (added empty on process)'}")
        _force_version_bump(conn)
        conn.commit()
    finally:
        conn.close()
    process_db(copy, seed=False, now=now)
    conn = connect(copy)
    try:
        after = _snapshot(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(charges)")}
    finally:
        conn.close()
    shutil.rmtree(tmp, ignore_errors=True)

    b_charges = _charges_by_start(before["charges"])
    a_charges = _charges_by_start(after["charges"])
    lines.append(f"charges {len(before['charges'])} → {len(after['charges'])}")
    lines.append(f"ledger {len(before['ledger'])} → {len(after['ledger'])}")
    ok = True
    if not _expects_merge(now):
        loc_before = {(row[0], row[1]): row for row in before["locations"]}
        loc_after = {(row[0], row[1]): row for row in after["locations"]}
        loc_changed = [key for key, row in loc_before.items() if loc_after.get(key) != row]
        if (
            before["charges"] != after["charges"]
            or before["ledger"] != after["ledger"]
            or before["meta"] != after["meta"]
            or loc_changed
        ):
            ok = False
            lines.append("outside the 3 h window a row changed")
        else:
            lines.append("outside the 3 h window nothing changed")
        lines.insert(0, "pass" if ok else "fail")
        return ok, lines
    removed = [key for key in b_charges if key not in a_charges]
    added = [key for key in a_charges if key not in b_charges]
    if removed != [(row[0], DROP) for row in before["charges"] if row[1] == DROP]:
        # Any removal other than the known second session fails.
        extra = [key for key in removed if key[1] != DROP]
        if extra or not removed:
            ok = False
            lines.append(f"unexpected charge removals: {extra or removed or 'none'}")
        else:
            lines.append(f"removed only {DROP}")
    else:
        lines.append(f"removed only {DROP}")
    if added:
        ok = False
        lines.append(f"unexpected new charges: {added}")
    for key, row in b_charges.items():
        if key[1] in (DROP, KEEP) or key not in a_charges:
            continue
        if a_charges[key] != row:
            ok = False
            lines.append(f"charge changed: {key[1]} {row} → {a_charges[key]}")
    if (before["meta"] != after["meta"]) and any(row[1] != DROP for row in before["meta"]):
        kept_meta = [row for row in before["meta"] if row[1] != DROP]
        after_meta = [row for row in after["meta"] if row[1] != DROP]
        if kept_meta != after_meta:
            ok = False
            lines.append("charge_meta changed besides the removed session")
        else:
            lines.append("charge_meta kept except the removed session")
    else:
        lines.append("charge_meta unchanged")
    before_loc = { (row[0], row[1]): row for row in before["locations"] if row[1] != DROP }
    after_loc = { (row[0], row[1]): row for row in after["locations"] if row[1] != DROP }
    changed_loc = [key for key, row in before_loc.items() if after_loc.get(key) != row]
    if changed_loc:
        ok = False
        lines.append(f"charge locations changed: {changed_loc}")
    else:
        lines.append("existing locations unchanged")
    b_led = {(row[0], row[1]): row for row in before["ledger"]}
    a_led = {(row[0], row[1]): row for row in after["ledger"]}
    if LEDGER not in b_led or any(key[1] == LEDGER for key in a_led):
        if any(key[1] == LEDGER for key in b_led) and not any(key[1] == LEDGER for key in a_led):
            lines.append(f"removed ledger {LEDGER}")
        elif not any(key[1] == LEDGER for key in b_led):
            lines.append(f"ledger {LEDGER} was not present")
        else:
            ok = False
            lines.append(f"ledger {LEDGER} is still present")
    for key, row in b_led.items():
        if key[1] in (LEDGER, KEEP_LEDGER):
            continue
        if key not in a_led or a_led[key] != row:
            ok = False
            lines.append(f"ledger changed: {key[1]}")
    merged = next((row for row in after["charges"] if row[1] == KEEP), None)
    if merged is None:
        ok = False
        lines.append(f"merged row {KEEP} is missing")
    else:
        _vid, _start, end, kind, soc0, soc1, kwh = merged
        if end != MERGED_END or kind != "AC":
            ok = False
            lines.append(f"merged row is {end} {kind}, expected {MERGED_END} AC")
        elif soc0 is None or soc1 is None or abs(float(soc0) - 63.3) > 0.05 or abs(float(soc1) - 66.3) > 0.05:
            ok = False
            lines.append(f"merged SoC {soc0}→{soc1}, expected 63.3→66.3")
        elif kwh is None or abs(float(kwh) - 2.0) > 0.25:
            ok = False
            lines.append(f"merged energy {kwh} kWh, expected about 2.0")
        else:
            lines.append(f"merged row {KEEP}–{end} {kind} soc {soc0}→{soc1} kwh {kwh}")
    if "active_s" not in cols:
        ok = False
        lines.append("active_s column missing after process")
    else:
        lines.append("active_s column present; older rows are not deleted to add it")
    expect_charges = len(before["charges"]) - (1 if any(row[1] == DROP for row in before["charges"]) else 0)
    if len(before["charges"]) == 13 and len(after["charges"]) != 12:
        ok = False
        lines.append("expected charges 13 → 12 on the 2026-11-15 backup")
    elif len(before["charges"]) == 13:
        lines.append("charges 13 → 12")
    if len(before["ledger"]) == 16 and len(after["ledger"]) != 15:
        ok = False
        lines.append("expected ledger 16 → 15 on the 2026-11-15 backup")
    elif len(before["ledger"]) == 16:
        lines.append("ledger 16 → 15")
    if len(after["charges"]) != expect_charges and len(before["charges"]) != 13:
        ok = False
        lines.append(f"charge count {len(before['charges'])} → {len(after['charges'])}, expected {expect_charges}")
    lines.insert(0, "pass" if ok else "fail")
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the safe charge-merge on a database copy.")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    path = Path(args.db)
    ok = True
    for label, when in (
        ("version bump at 21:45", NOW),
        ("version bump at now", datetime.now(timezone.utc)),
    ):
        passed, lines = audit(path, when)
        print(f"== {label} ==")
        for line in lines:
            print(line)
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
