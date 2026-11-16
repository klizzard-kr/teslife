"""One-off migration: remove the charge memo column `charge_meta.note`.

The app no longer reads or writes it, so the column can stay until this runs. Run it
once at deploy time, after the new code is in place; never on import or start-up:

    python3 -m ledger.drop_charge_note \\
        --db signals.db \\
        --backup signals.db.bak-YYYYMMDD-nonote

It first copies the whole database to --backup with SQLite's online backup and checks
the copy (integrity_check, same charge_meta row count). Only then does it drop the
column. It prints counts only, never memo text. Running it again is a no-op.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(charge_meta)").fetchall()}


def drop_charge_note(db: str | Path, backup: str | Path) -> dict[str, Any]:
    db, backup = Path(db), Path(backup)
    if not db.exists():
        raise FileNotFoundError(db)
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        if "note" not in _columns(conn):
            return {"dropped": False, "rows": None, "with_note": 0, "backup": None}
        rows = conn.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0]
        with_note = conn.execute(
            "SELECT COUNT(*) FROM charge_meta WHERE note IS NOT NULL AND trim(note) <> ''"
        ).fetchone()[0]
        dst = sqlite3.connect(str(backup))
        try:
            conn.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            copied = dst.execute("SELECT COUNT(*) FROM charge_meta").fetchone()[0]
            kept = dst.execute(
                "SELECT COUNT(*) FROM charge_meta WHERE note IS NOT NULL AND trim(note) <> ''"
            ).fetchone()[0]
        finally:
            dst.close()
        backup.chmod(0o600)
        if ok != "ok" or copied != rows or kept != with_note:
            raise RuntimeError("backup check failed; nothing was changed")
        with conn:
            conn.execute("ALTER TABLE charge_meta DROP COLUMN note")
        if "note" in _columns(conn):
            raise RuntimeError("column still present")
        return {"dropped": True, "rows": rows, "with_note": with_note, "backup": str(backup)}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--backup", required=True, help="new file; the run stops if it exists")
    args = ap.parse_args(argv)
    print(json.dumps(drop_charge_note(args.db, args.backup), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
