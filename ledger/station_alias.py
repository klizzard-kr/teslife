"""Station display names come from settings or from names already stored.

Nothing here names a real place. A shorter catalog name matches a stored name
when the stored name starts with it. An explicit `vehicle_settings` value
`station_aliases` is used as written and is not inferred.
"""

from __future__ import annotations

import json
import sqlite3

ALIAS_KEY = "station_aliases"
MIN_PREFIX = 6


def compact(name: str | None) -> str:
    return "".join(str(name or "").split())


def _explicit(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, str]] | None:
    try:
        row = conn.execute(
            "SELECT value FROM vehicle_settings WHERE vehicle_id = ? AND key = ?",
            (vehicle_id, ALIAS_KEY),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None or str(row[0]).strip() == "":
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "operator": str(item.get("operator") or "").strip(),
            "prefix": str(item.get("prefix") or "").strip(),
        })
    return out


def _inferred(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def add(name: str | None, operator: str | None) -> None:
        text = str(name or "").strip()
        key = compact(text)
        if len(key) < MIN_PREFIX:
            return
        slot = found.setdefault(key, {"name": text, "operator": "", "prefix": ""})
        op = str(operator or "").strip()
        if op and not slot["operator"]:
            slot["operator"] = op

    try:
        rows = conn.execute(
            "SELECT station_name, operator FROM charge_meta WHERE vehicle_id = ?",
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for name, operator in rows:
        add(name, operator)
    try:
        rows = conn.execute(
            "SELECT stat_nm, busi_nm FROM charge_charger WHERE vehicle_id = ?",
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for name, operator in rows:
        add(name, operator)
    return list(found.values())


def aliases_for(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, str]]:
    explicit = _explicit(conn, vehicle_id)
    if explicit is not None:
        return explicit
    return _inferred(conn, vehicle_id)


def resolve(aliases: list[dict[str, str]], name: str | None) -> dict[str, str] | None:
    """The stored name this catalog name should show, or the name itself."""
    folded = compact(name)
    if not folded:
        return None
    exact = None
    longer: list[dict[str, str]] = []
    for alias in aliases:
        canon = compact(alias.get("name"))
        prefix = compact(alias.get("prefix") or "")
        if not canon:
            continue
        if prefix:
            if prefix in folded or folded.startswith(prefix):
                if canon == folded:
                    exact = alias
                else:
                    longer.append(alias)
            continue
        if canon == folded:
            exact = alias
        elif canon.startswith(folded) and len(folded) >= MIN_PREFIX:
            longer.append(alias)
    if longer:
        longer.sort(key=lambda item: len(compact(item.get("name"))))
        return longer[0]
    return exact


def display_name(aliases: list[dict[str, str]], name: str | None) -> str | None:
    """Canonical name when `name` is a shorter form of one stored name."""
    current = str(name or "").strip()
    hit = resolve(aliases, current)
    if hit is None:
        return None
    if compact(current) == compact(hit.get("name")):
        return current or None
    return str(hit.get("name") or "").strip() or None


def apply_station_aliases(conn: sqlite3.Connection) -> None:
    """Rewrite shorter stored names to the name already in the database.

    Only UPDATE. A non-empty operator or business name is left as saved.
    An empty one is filled from the matched station. No table is added.
    """
    ids: set[str] = set()
    for table in ("charge_meta", "charge_charger"):
        try:
            for (vid,) in conn.execute(f"SELECT DISTINCT vehicle_id FROM {table}"):
                if vid:
                    ids.add(str(vid))
        except sqlite3.OperationalError:
            continue
    for vid in ids:
        aliases = aliases_for(conn, vid)
        if not aliases:
            continue
        _apply_charger(conn, vid, aliases)
        _apply_meta(conn, vid, aliases)


def _fill_operator(current: str | None, hit: dict[str, str] | None) -> str | None:
    if str(current or "").strip():
        return current
    if hit and hit.get("operator"):
        return hit["operator"]
    return current


def _apply_charger(conn: sqlite3.Connection, vid: str, aliases: list[dict[str, str]]) -> None:
    try:
        rows = conn.execute(
            "SELECT charge_id, stat_nm, busi_nm FROM charge_charger WHERE vehicle_id = ?",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for charge_id, stat_nm, busi_nm in rows:
        hit = resolve(aliases, stat_nm)
        if hit is None:
            continue
        new_name = display_name(aliases, stat_nm) or stat_nm
        new_op = _fill_operator(busi_nm, hit)
        if new_name == stat_nm and new_op == busi_nm:
            continue
        conn.execute(
            "UPDATE charge_charger SET stat_nm = ?, busi_nm = ? WHERE vehicle_id = ? AND charge_id = ?",
            (new_name, new_op, vid, charge_id),
        )


def _apply_meta(conn: sqlite3.Connection, vid: str, aliases: list[dict[str, str]]) -> None:
    try:
        rows = conn.execute(
            "SELECT charge_id, station_name, operator FROM charge_meta WHERE vehicle_id = ?",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    charger: dict[str, str] = {}
    try:
        for charge_id, stat_nm in conn.execute(
            "SELECT charge_id, stat_nm FROM charge_charger WHERE vehicle_id = ?",
            (vid,),
        ):
            charger[str(charge_id)] = str(stat_nm or "")
    except sqlite3.OperationalError:
        charger = {}
    for charge_id, station_name, operator in rows:
        source = station_name if str(station_name or "").strip() else charger.get(str(charge_id))
        hit = resolve(aliases, source)
        if hit is None:
            continue
        new_name = display_name(aliases, source) or station_name
        if not str(station_name or "").strip() and new_name:
            pass
        elif str(station_name or "").strip() and display_name(aliases, station_name) is None and compact(station_name) != compact(hit.get("name")):
            new_name = station_name
        new_op = _fill_operator(operator, hit)
        if new_name == station_name and new_op == operator:
            continue
        conn.execute(
            "UPDATE charge_meta SET station_name = ?, operator = ? WHERE vehicle_id = ? AND charge_id = ?",
            (new_name, new_op, vid, charge_id),
        )
