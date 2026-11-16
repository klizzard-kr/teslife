"""Read telemetry JSON signals and store them in SQLite (Stage 1)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    vehicle_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, ts, name)
);

-- Fleet Telemetry "alerts" records (bridge). notified_*: 0 = not yet, 1 = sent, 2 = skipped.
CREATE TABLE IF NOT EXISTS vehicle_alerts (
    vehicle_id TEXT NOT NULL,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    audiences TEXT,
    received_ts TEXT,
    notified_start INTEGER NOT NULL DEFAULT 0,
    notified_end INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, name, started_at)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def _encode_value(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def iter_signals(source: TextIO) -> Iterator[dict[str, Any]]:
    for line_no, raw in enumerate(source, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {line_no} is not a JSON object")
        yield obj


def normalize(obj: dict[str, Any]) -> tuple[str, str, str, str]:
    vehicle_id = obj.get("vehicle_id")
    ts = obj.get("ts", obj.get("timestamp"))
    name = obj.get("name")
    if "value" not in obj:
        raise ValueError("signal missing value")
    if not vehicle_id or not ts or not name:
        raise ValueError("signal requires vehicle_id, timestamp/ts, and name")
    return str(vehicle_id), str(ts), str(name), _encode_value(obj["value"])


def store_signals(
    conn: sqlite3.Connection, signals: Iterable[dict[str, Any]]
) -> int:
    inserted = 0
    rows = [normalize(obj) for obj in signals]
    with conn:
        for row in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO signals (vehicle_id, ts, name, value)
                VALUES (?, ?, ?, ?)
                """,
                row,
            )
            inserted += cur.rowcount
    return inserted


def ingest_file(db_path: str | Path, source: TextIO) -> int:
    conn = connect(db_path)
    try:
        return store_signals(conn, iter_signals(source))
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest NDJSON telemetry signals into SQLite."
    )
    parser.add_argument(
        "--db",
        default="signals.db",
        help="SQLite database path (default: signals.db)",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="NDJSON file to read (default: stdin)",
    )
    args = parser.parse_args(argv)
    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            inserted = ingest_file(args.db, fh)
    else:
        inserted = ingest_file(args.db, sys.stdin)
    print(f"inserted {inserted} new row(s) into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
