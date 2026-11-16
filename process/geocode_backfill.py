"""Fill English labels for places already cached in geocode_cache.

One Nominatim request per row, at most about one per second. Safe to stop and run again:
a row is skipped once label_en is set (including an empty string when there was no locality).
A failed request leaves the row empty so the next run retries it.

Only geocode_cache.label_en is written. Trips, charges, routes, the ledger, and consumables
are not changed.

    python -m process.geocode_backfill --db PATH

About one second per place that still needs an English label. One hundred places take
about two minutes. The command prints counts only.
"""

from __future__ import annotations

import argparse

from process import connect
from process.geocode import NominatimGeocoder, backfill_english


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill English geocode labels (label_en).")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=0, help="Stop after this many rows (0 = all)")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM geocode_cache WHERE label IS NOT NULL AND label_en IS NULL"
        ).fetchone()[0]
        result = backfill_english(conn, NominatimGeocoder(), limit=args.limit or None)
    finally:
        conn.close()
    print(
        f"pending_before={int(pending or 0)} updated={result['updated']} "
        f"errors={result['errors']} pending={result['pending']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
