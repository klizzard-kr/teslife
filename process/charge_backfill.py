"""One-time v1.8.0 charger cleanup and location rematch.

Saved station rows are copied out and then deleted. Existing charges are
matched again from the public catalog to a station (name, address, operator,
rated output). Charge energy and every stored price stay as they were.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PURGE_FLAG = "stations_purge:v180"
BACKFILL_FLAG = "ev_backfill:v180"
_ENERGY = (
    "kwh_added", "charger_kwh", "battery_kwh", "ac_kwh", "dc_kwh",
    "loss_kwh", "loss_pct", "max_charger_kw", "avg_charger_kw",
)
_PRICE = ("unit_price_krw_per_kwh", "total_krw", "kwh_billed", "discount_krw")


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM process_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO process_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone()
    return row is not None


def maybe_purge_stations(conn: sqlite3.Connection, db_path: str | Path) -> dict[str, Any] | None:
    """Copy every saved station out, then delete those rows. Once."""
    if _meta_get(conn, PURGE_FLAG) == "1":
        return None
    if not _table(conn, "stations"):
        _meta_set(conn, PURGE_FLAG, "1")
        return {"backed_up": 0, "deleted": 0, "backup": ""}
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM stations").fetchall()
    payload = [{key: row[key] for key in row.keys()} for row in rows]
    path = Path(db_path).resolve().with_name("stations-backup-v180.json")
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    with conn:
        deleted = conn.execute("DELETE FROM stations").rowcount
    _meta_set(conn, PURGE_FLAG, "1")
    return {"backed_up": len(payload), "deleted": deleted, "backup": str(path)}


def _cost(conn: sqlite3.Connection, vid: str, charge_id: str, kwh: float | None) -> tuple[float | None, bool]:
    from ledger import charging_cost
    row = None
    try:
        row = conn.execute(
            "SELECT operator, station_name, location_text, unit_price_krw_per_kwh, total_krw, "
            "kwh_billed, discount_krw FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    meta = None
    if row is not None:
        meta = {
            "operator": row[0], "station_name": row[1], "location_text": row[2],
            "unit_price_krw_per_kwh": row[3], "total_krw": row[4],
            "kwh_billed": row[5], "discount_krw": row[6],
        }
    return charging_cost(conn, vid, kwh, meta)


def _ledger_row(conn: sqlite3.Connection, vid: str, charge_id: str) -> tuple[Any, Any] | None:
    try:
        row = conn.execute(
            "SELECT amount_krw, estimated FROM ledger WHERE vehicle_id = ? AND id = ?",
            (vid, "auto-charge:" + charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0], row[1]) if row else None


def energy_price_snapshot(conn: sqlite3.Connection, vid: str) -> dict[str, Any]:
    """Energy columns, stored prices, and ledger amounts."""
    charges = []
    try:
        rows = conn.execute(
            f"SELECT start_ts, {', '.join(_ENERGY)} FROM charges WHERE vehicle_id = ?", (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        charges.append({"charge_id": row[0], **{name: row[i + 1] for i, name in enumerate(_ENERGY)}})
    prices = []
    try:
        rows = conn.execute(
            f"SELECT charge_id, {', '.join(_PRICE)} FROM charge_meta WHERE vehicle_id = ?", (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        prices.append({"charge_id": row[0], **{name: row[i + 1] for i, name in enumerate(_PRICE)}})
    ledger = []
    try:
        rows = conn.execute(
            "SELECT id, amount_krw, estimated FROM ledger WHERE vehicle_id = ? AND source = 'auto'",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        ledger.append({"id": row[0], "amount_krw": row[1], "estimated": row[2]})
    return {"charges": charges, "prices": prices, "ledger": ledger}


def _priced_operator(conn: sqlite3.Connection, vid: str, charge_id: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT operator FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def canonical_station_name(conn: sqlite3.Connection, vid: str, name: str | None) -> str | None:
    """Longer station name already stored, or the one in settings. No place is hard-coded."""
    from ledger.station_alias import aliases_for, display_name

    return display_name(aliases_for(conn, vid), name)


def apply_station_matches(conn: sqlite3.Connection, vid: str, now: datetime | None = None) -> dict[str, int]:
    """Write station name, address, operator, and rated output from the local catalog.

    Energy columns and price columns are not updated. The priced operator is
    replaced only when the charging amount, including an estimate, stays the same.
    The matched operator is always stored on the charger row for display.
    """
    from process.evcharger import (
        FAR_M, _nearby, _set_operator, _station_item, _write_choice, ensure_schema, select_type_power,
    )

    now = now or datetime.now(timezone.utc)
    ensure_schema(conn)
    stats = {"matched": 0, "unmatched": 0, "no_location": 0, "operator_kept": 0, "user_skipped": 0}
    if not _table(conn, "charges") or not _table(conn, "charge_locations"):
        return stats
    rows = conn.execute(
        "SELECT c.start_ts, c.kind, c.max_charger_kw, c.max_power, c.kwh_added, l.lat, l.lon "
        "FROM charges c JOIN charge_locations l ON l.vehicle_id = c.vehicle_id AND l.charge_id = c.start_ts "
        "WHERE c.vehicle_id = ? AND l.lat IS NOT NULL AND l.lon IS NOT NULL",
        (vid,),
    ).fetchall()
    known = {row[0] for row in rows}
    all_ids = conn.execute("SELECT start_ts FROM charges WHERE vehicle_id = ?", (vid,)).fetchall()
    stats["no_location"] = sum(1 for (cid,) in all_ids if cid not in known)
    for charge_id, kind, max_kw, max_power, kwh, lat, lng in rows:
        nearby = _nearby(conn, float(lat), float(lng), now)
        pool = [
            item for item in nearby
            if (item.get("distance_m") if item.get("distance_m") is not None else 1e9) <= FAR_M
        ]
        if not pool:
            stats["unmatched"] += 1
            continue
        power = max_kw if max_kw is not None else max_power
        typed = select_type_power(pool, kind or "AC", power)
        stored = _station_item(typed or pool)
        before_cost = _cost(conn, vid, charge_id, kwh)
        before_ledger = _ledger_row(conn, vid, charge_id)
        saved = None
        try:
            saved = conn.execute(
                "SELECT source FROM charge_charger WHERE vehicle_id = ? AND charge_id = ?",
                (vid, charge_id),
            ).fetchone()
        except sqlite3.OperationalError:
            saved = None
        if saved and saved[0] == "user":
            stats["user_skipped"] = stats.get("user_skipped", 0) + 1
            continue
        old_operator = _priced_operator(conn, vid, charge_id)
        from ledger.station_alias import aliases_for, display_name, resolve

        aliases = aliases_for(conn, vid)
        hit = resolve(aliases, stored.get("stat_nm"))
        if hit:
            stored = dict(stored)
            shown = display_name(aliases, stored.get("stat_nm"))
            if shown:
                stored["stat_nm"] = shown
            if hit.get("operator") and not (old_operator or "").strip():
                stored["busi_nm"] = hit["operator"]
        _write_choice(conn, vid, charge_id, stored, "station", float(lat), float(lng), force_operator=False)
        operator = (stored.get("busi_nm") or "").strip()
        # A value the user already saved is left as it is, even when the catalog differs.
        if (old_operator or "").strip():
            if operator and operator != old_operator:
                stats["operator_kept"] += 1
        elif operator:
            _set_operator(conn, vid, charge_id, operator, force=True)
            if _cost(conn, vid, charge_id, kwh) != before_cost or _ledger_row(conn, vid, charge_id) != before_ledger:
                _set_operator(conn, vid, charge_id, "", force=True)
                with conn:
                    conn.execute(
                        "UPDATE charge_meta SET operator = NULL WHERE vehicle_id = ? AND charge_id = ?",
                        (vid, charge_id),
                    )
                stats["operator_kept"] += 1
        stats["matched"] += 1
    return stats


def backfill_charger_info(
    conn: sqlite3.Connection,
    vid: str,
    now: datetime | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, int] | None:
    """Fetch each needed district once, then match every located charge. Once, within the daily cap."""
    from process.evcharger import (
        _calls, _fetch_info_page, _region_key, _region_row, _region_stale, api_key, ensure_schema, reference_points,
        region_from_point,
    )

    if _meta_get(conn, BACKFILL_FLAG) == "1":
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ensure_schema(conn)
    anchors = reference_points(conn, vid)
    regions: set[tuple[str, str | None]] = set()
    for lat, lng in anchors:
        found = region_from_point(lat, lng)
        if found is not None:
            regions.add(found)
    calls = 0
    key = api_key()
    finished_fetch = True
    if key:
        for zcode, zscode in sorted(regions):
            if _calls(conn, now) >= 1000:
                finished_fetch = False
                break
            region_key = _region_key(zcode, zscode)
            if not _region_stale(_region_row(conn, region_key), now):
                continue
            calls += _fetch_info_page(conn, zcode, now, opener, zscode=zscode, anchors=anchors)
            row = _region_row(conn, region_key)
            if row is None or not row["done"]:
                finished_fetch = False
    stats = apply_station_matches(conn, vid, now)
    stats["calls"] = calls
    if finished_fetch:
        _meta_set(conn, BACKFILL_FLAG, "1")
    return stats
