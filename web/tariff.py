"""Operator price history and charge amount estimates.

Official rows (vehicle_id '') apply from their start date. A later manual row for
the same operator, speed and membership replaces them from that date only.
Operators with no official row use the median unit price of this car's billed
charges. Nothing here rewrites trip, charge, route, ledger or service rows.
"""

from __future__ import annotations

import io
import re
import sqlite3
import statistics
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from process.geo import distance_m

KST = timezone(timedelta(hours=9))
PLACE_RADIUS_M = 150.0
OFFICIAL = ""
PLUGLINK = "플러그링크"
# Bands follow the public price table (ev.or.kr), not the older 40/100 kW split.
# ac < 30, mid 30–49, fast50 50–99, fast100 100–199, ultra >= 200.
# PlugLink member prices 292/322/322/345/345 from the 2026-07-31 update.
# The sheet has no 중속 or 초급속 row for PlugLink: those copy the neighbouring
# 급속 row (322 or 345 for members, 528 for guests). Guest 완속 stays 357.
PLUGLINK_FROM = "2026-07-31"
# 기후에너지환경부 row of the public table, from 2026-08-01. One roaming price
# per band; membership of the charge operator is a separate choice.
ENV_CARD = "환경부"
ENV_FROM = "2026-08-01"
ENV_RATES = (
    ("ac", 295.0),
    ("mid", 307.2),
    ("fast50", 325.6),
    ("fast100", 348.4),
    ("ultra", 393.1),
)
SPEEDS = ("ac", "mid", "fast50", "fast100", "ultra")
# Rows written by the first 1.6 seed. Left in place; lookup uses SPEEDS only.
LEGACY_SPEEDS = ("slow", "fast_low", "fast_high", "fast")
SPEED_LABEL = {
    "ac": "완속 (30kW 미만)",
    "mid": "중속 (30~49kW)",
    "fast50": "급속 (50~99kW)",
    "fast100": "급속 (100~199kW)",
    "ultra": "초급속 (200kW 이상)",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tariff_rates (
    vehicle_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    speed TEXT NOT NULL,
    member INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    krw_per_kwh REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, operator, speed, member, effective_from)
);

CREATE TABLE IF NOT EXISTS tariff_member (
    vehicle_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    member INTEGER NOT NULL,
    PRIMARY KEY (vehicle_id, operator)
);

CREATE TABLE IF NOT EXISTS payment_prefs (
    vehicle_id TEXT PRIMARY KEY,
    env_card INTEGER NOT NULL DEFAULT 0
);
"""

_OFFICIAL_SEED = (
    (PLUGLINK, "ac", 1, 292.0),
    (PLUGLINK, "mid", 1, 322.0),
    (PLUGLINK, "fast50", 1, 322.0),
    (PLUGLINK, "fast100", 1, 345.0),
    (PLUGLINK, "ultra", 1, 345.0),
    (PLUGLINK, "ac", 0, 357.0),
    (PLUGLINK, "mid", 0, 528.0),
    (PLUGLINK, "fast50", 0, 528.0),
    (PLUGLINK, "fast100", 0, 528.0),
    (PLUGLINK, "ultra", 0, 528.0),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables and seed official rates. Writable connections only.

    Server start, the process loop, and write APIs call this. A read-only web
    request must not: INSERT fails there and the charge estimates then disappear.
    """
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT OR IGNORE INTO tariff_rates "
        "(vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'official')",
        [(OFFICIAL, op, speed, member, PLUGLINK_FROM, price) for op, speed, member, price in _OFFICIAL_SEED],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO tariff_rates "
        "(vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'official')",
        [(OFFICIAL, ENV_CARD, speed, 0, ENV_FROM, price) for speed, price in ENV_RATES],
    )
    conn.commit()


def canon_operator(name: str | None) -> str:
    text = (name or "").strip()
    key = text.casefold().replace(" ", "")
    if key in {"플러그링크", "pluglink"}:
        return PLUGLINK
    if key in {"환경부", "기후에너지환경부", "기후에너지환경부카드"}:
        return ENV_CARD
    return text


def _day(ts: str | None) -> str | None:
    if not ts:
        return None
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ac_received(charge: dict[str, Any]) -> float | None:
    """ACChargingEnergyIn rise. Older AC rows stored that rise in charger_kwh."""
    stored = _num(charge.get("ac_kwh"))
    if stored is not None:
        return stored
    if charge.get("kind") != "DC":
        return _num(charge.get("charger_kwh"))
    return None


def energy_loss_kwh(
    sent: float | None, stored: float | None, used: float | None,
) -> float | None:
    """충전량 − 배터리 저장량 − 충전 중 사용량, in kWh, never below zero.

    None when any of the three amounts is missing. This is the kWh left over
    after storage and what the car used, not the charging-efficiency percent.
    """
    if sent is None or stored is None or used is None:
        return None
    return max(0.0, sent - stored - used)


def battery_in(charge: dict[str, Any]) -> float | None:
    """DCChargingEnergyIn rise. Older DC rows stored that rise in charger_kwh."""
    stored = _num(charge.get("dc_kwh"))
    if stored is not None:
        return stored
    battery = _num(charge.get("battery_kwh"))
    if battery is not None:
        return battery
    if charge.get("kind") == "DC":
        return _num(charge.get("charger_kwh"))
    return None


SPEED_WORD = {
    "ac": "완속",
    "mid": "중속",
    "fast50": "급속",
    "fast100": "급속",
    "ultra": "초급속",
}


def speed_word(band: str | None) -> str:
    return SPEED_WORD.get(band or "", "")


def _won(value: float | None) -> int | float | None:
    if value is None:
        return None
    if abs(value - round(value)) < 1e-6:
        return int(round(value))
    return round(float(value), 1)


def nearby_operator_rates(
    conn: sqlite3.Connection, chargers: list[dict[str, Any]], day: str,
) -> list[dict[str, Any]]:
    """One row per operator and speed near the car. No coordinates."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in chargers:
        operator = canon_operator(str(item.get("busi_nm") or item.get("operator") or ""))
        if not operator:
            continue
        output = _num(item.get("output_kw"))
        kind = "DC" if output is not None and output >= 50 else "AC"
        band = speed_band({"kind": kind, "max_charger_kw": output}) or "ac"
        dist = _num(item.get("distance_m"))
        key = (operator, band)
        row = grouped.get(key)
        if row is None or (dist is not None and (row["distance_m"] is None or dist < row["distance_m"])):
            grouped[key] = {"operator": operator, "band": band, "distance_m": dist}
    out = []
    for row in grouped.values():
        band = row["band"]
        member, _m = _official_or_manual(conn, OFFICIAL, row["operator"], band, True, day)
        guest, _g = _official_or_manual(conn, OFFICIAL, row["operator"], band, False, day)
        roaming = _price_row(conn, OFFICIAL, ENV_CARD, band, 0, day) if day >= ENV_FROM else None
        distance = row["distance_m"]
        out.append({
            "operator": row["operator"],
            "speed": speed_word(band),
            "member_krw": _won(member),
            "guest_krw": _won(guest),
            "roaming_krw": _won(roaming),
            "distance_m": int(round(distance)) if distance is not None else None,
        })
    out.sort(key=lambda item: (item["distance_m"] is None, item["distance_m"] or 0, item["operator"], item["speed"]))
    return out


def speed_band(charge: dict[str, Any]) -> str | None:
    """Public-table band from charger output.

    AC with no rated output is 완속. DC with no output is unset, so a fast price
    is not chosen for the owner.
    """
    output = _num((charge.get("public_charger") or {}).get("output_kw"))
    if output is None:
        output = _num(charge.get("max_charger_kw"))
    if output is None:
        return "ac" if charge.get("kind") != "DC" else None
    if output < 30:
        return "ac"
    if output < 50:
        return "mid"
    if output < 100:
        return "fast50"
    if output < 200:
        return "fast100"
    return "ultra"


def _median(values: list[float]) -> float:
    if not values:
        return 1.0
    return float(statistics.median(values))


def _ratios(conn: sqlite3.Connection, vehicle_id: str) -> tuple[float, float]:
    """Median billed/received. AC uses the AC counter; DC uses energy into the pack."""
    try:
        rows = conn.execute(
            "SELECT c.kind, c.charger_kwh, c.battery_kwh, c.ac_kwh, c.dc_kwh, m.kwh_billed "
            "FROM charges c JOIN charge_meta m ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts "
            "WHERE c.vehicle_id = ? AND m.kwh_billed IS NOT NULL",
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 1.0, 1.0
    ac_ratios: list[float] = []
    dc_ratios: list[float] = []
    for kind, charger, battery, ac, dc, billed in rows:
        billed_f = _num(billed)
        if billed_f is None or billed_f <= 0:
            continue
        charge = {
            "kind": kind, "charger_kwh": charger, "battery_kwh": battery, "ac_kwh": ac, "dc_kwh": dc,
        }
        received = ac_received(charge)
        if received is not None and received > 0:
            ac_ratios.append(billed_f / received)
            continue
        into = battery_in(charge)
        if into is not None and into > 0:
            dc_ratios.append(billed_f / into)
    return _median(ac_ratios), _median(dc_ratios)


def charger_sent(
    charge: dict[str, Any], meta: dict[str, Any] | None, ratios: tuple[float, float],
) -> tuple[float | None, bool]:
    """kWh the charger delivered, and whether that figure is estimated.

    A billed kWh is used as-is. Otherwise AC received times the AC ratio, or,
    when the AC counter did not move, pack energy times the DC ratio.
    """
    billed = _num((meta or {}).get("kwh_billed"))
    if billed is not None and billed > 0:
        return billed, False
    ac_ratio, dc_ratio = ratios
    received = ac_received(charge)
    if received is not None and received > 0:
        return received * ac_ratio, True
    into = battery_in(charge)
    if into is not None and into > 0:
        return into * dc_ratio, True
    return None, False


def _member(conn: sqlite3.Connection, vehicle_id: str, operator: str) -> bool:
    """True only when this operator is selected. A missing row is not a member."""
    try:
        row = conn.execute(
            "SELECT member FROM tariff_member WHERE vehicle_id = ? AND operator = ?",
            (vehicle_id, operator),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    return bool(row[0])


def _env_card(conn: sqlite3.Connection, vehicle_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT env_card FROM payment_prefs WHERE vehicle_id = ?",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def choose_rate(
    member_price: float | None,
    guest_price: float | None,
    roaming_price: float | None,
    is_member: bool,
    has_env_card: bool,
) -> float | None:
    """Member price, else the 환경부 card rate, else the guest price.

    When the owner is a member and also holds the card, the cheaper of the
    member price and the roaming rate is used.
    """
    if is_member and has_env_card:
        options = [price for price in (member_price, roaming_price) if price is not None]
        if options:
            return min(options)
    if is_member and member_price is not None:
        return member_price
    if has_env_card and roaming_price is not None:
        return roaming_price
    return guest_price


def _has_official(conn: sqlite3.Connection, operator: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM tariff_rates WHERE vehicle_id = ? AND operator = ? AND source = 'official' LIMIT 1",
            (OFFICIAL, operator),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _price_row(
    conn: sqlite3.Connection, vehicle_id: str, operator: str, speed: str, member: int, day: str,
) -> float | None:
    try:
        row = conn.execute(
            "SELECT krw_per_kwh FROM tariff_rates WHERE vehicle_id = ? AND operator = ? AND speed = ? "
            "AND member = ? AND effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
            (vehicle_id, operator, speed, member, day),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return float(row[0])


def _official_or_manual(
    conn: sqlite3.Connection, vehicle_id: str, operator: str, speed: str, member: bool, day: str,
) -> tuple[float | None, bool]:
    """(won per kWh, learned?). Manual history wins over official from its start date."""
    flag = 1 if member else 0
    for vid, learned in ((vehicle_id, False), (OFFICIAL, False)):
        price = _price_row(conn, vid, operator, speed, flag, day)
        if price is not None:
            return price, learned
    return None, False


def _learned_price(
    conn: sqlite3.Connection, vehicle_id: str, operator: str, speed: str | None,
) -> float | None:
    want_dc = speed in ("fast50", "fast100", "ultra")
    try:
        rows = conn.execute(
            "SELECT c.kind, m.total_krw, m.kwh_billed, m.discount_krw FROM charges c "
            "JOIN charge_meta m ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts "
            "WHERE c.vehicle_id = ? AND m.operator = ? AND m.total_krw IS NOT NULL AND m.kwh_billed > 0",
            (vehicle_id, operator),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    prices = []
    for kind, total, billed, discount in rows:
        if want_dc != (kind == "DC"):
            continue
        value = float(total) + float(discount or 0)
        if float(billed) > 0:
            prices.append(value / float(billed))
    if not prices:
        return None
    return float(statistics.median(prices))


def _last_pay_method(conn: sqlite3.Connection, vehicle_id: str, operator: str) -> str | None:
    """Latest payment choice for this operator. Empty when this car has not chosen one."""
    try:
        row = conn.execute(
            "SELECT m.pay_method FROM charge_meta m "
            "LEFT JOIN charges c ON c.vehicle_id = m.vehicle_id AND c.start_ts = m.charge_id "
            "WHERE m.vehicle_id = ? AND m.operator = ? AND m.pay_method IN ('member', 'guest', 'env_card') "
            "ORDER BY COALESCE(c.end_ts, c.start_ts, m.charge_id) DESC LIMIT 1",
            (vehicle_id, operator),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def unit_price(
    conn: sqlite3.Connection, vehicle_id: str, operator: str | None, charge: dict[str, Any], day: str | None,
) -> tuple[float | None, bool]:
    """(won per kWh, learned from bills?). None when there is nothing to apply."""
    name = canon_operator(operator)
    if not name or not day:
        return None, False
    band = speed_band(charge)
    if band is None:
        return None, False
    method = str((charge.get("meta") or {}).get("pay_method") or "")
    if method not in ("member", "guest", "env_card"):
        method = _last_pay_method(conn, vehicle_id, name) or "guest"
    if method == "member":
        member, has_card = True, False
    elif method == "env_card":
        member, has_card = False, True
    else:
        member, has_card = False, False
    member_price, member_learned = _official_or_manual(conn, vehicle_id, name, band, True, day)
    guest_price, guest_learned = _official_or_manual(conn, vehicle_id, name, band, False, day)
    if member_price is None and guest_price is None and not _has_official(conn, name):
        learned = _learned_price(conn, vehicle_id, name, band)
        if learned is not None:
            guest_price, guest_learned = learned, True
            member_price, member_learned = learned, True
    roaming = None
    if has_card and day >= ENV_FROM:
        roaming = _price_row(conn, OFFICIAL, ENV_CARD, band, 0, day)
    picked = choose_rate(member_price, guest_price, roaming, member, has_card)
    if picked is None:
        return None, False
    if roaming is not None and picked == roaming and (member_price is None or roaming <= member_price):
        return picked, False
    if member and member_price is not None and picked == member_price:
        return picked, member_learned
    if guest_price is not None and picked == guest_price:
        return picked, guest_learned
    return picked, False


def _nearby_operators(
    conn: sqlite3.Connection, vehicle_id: str, lat: float, lon: float, charge_id: str,
) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT c.start_ts, m.operator, l.lat, l.lon, COALESCE(c.end_ts, c.start_ts) "
            "FROM charges c JOIN charge_meta m ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts "
            "JOIN charge_locations l ON l.vehicle_id = c.vehicle_id AND l.charge_id = c.start_ts "
            "WHERE c.vehicle_id = ? AND c.start_ts != ? AND m.operator IS NOT NULL AND TRIM(m.operator) != ''",
            (vehicle_id, charge_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    found: list[tuple[str, str]] = []
    for _start, operator, plat, plon, when in rows:
        try:
            if distance_m((lat, lon), (float(plat), float(plon))) > PLACE_RADIUS_M:
                continue
        except (TypeError, ValueError):
            continue
        name = canon_operator(str(operator))
        if name:
            found.append((str(when), name))
    found.sort(reverse=True)
    out: list[str] = []
    for _when, name in found:
        if name not in out:
            out.append(name)
    return out


def _operator_choice(charge: dict[str, Any], nearby: list[str]) -> tuple[str, list[str]]:
    meta = charge.get("meta") or {}
    typed = canon_operator(meta.get("operator"))
    if typed:
        return typed, []
    if len(nearby) == 1:
        return nearby[0], []
    if len(nearby) > 1:
        return "", nearby
    pub = charge.get("public_charger") or {}
    if pub.get("source") == "pending":
        return "", []
    return canon_operator(pub.get("operator")), []


def annotate(conn: sqlite3.Connection, vehicle_id: str, items: list[dict[str, Any]]) -> None:
    """Add display figures. Reads only: a missing tariff table leaves the price unset."""
    if not items:
        return
    ratios = _ratios(conn, vehicle_id)
    for item in items:
        meta = item.get("meta") or {}
        lat, lon = item.get("lat"), item.get("lon")
        nearby: list[str] = []
        if lat is not None and lon is not None:
            nearby = _nearby_operators(conn, vehicle_id, float(lat), float(lon), str(item.get("charge_id") or ""))
        operator, choices = _operator_choice(item, nearby)
        item["operator_choices"] = choices
        item["operator_display"] = operator
        sent, sent_est = charger_sent(item, meta, ratios)
        item["sent_kwh"] = None if sent is None else round(sent, 4)
        item["sent_estimated"] = bool(sent_est and sent is not None)
        paid = _num(meta.get("total_krw"))
        discount = _num(meta.get("discount_krw")) or 0.0
        billed = _num(meta.get("kwh_billed"))
        day = _day(item.get("end_ts") or item.get("start_ts"))
        priced, learned = unit_price(conn, vehicle_id, operator, item, day)
        if paid is not None:
            item["cost_krw"] = paid
            item["cost_estimated"] = False
        elif sent is not None and priced is not None:
            item["cost_krw"] = round(sent * priced)
            item["cost_estimated"] = True
        else:
            item["cost_krw"] = item.get("cost_krw")
            item["cost_estimated"] = bool(item.get("cost_estimated"))
        if paid is not None and billed is not None and billed > 0:
            item["unit_krw_per_kwh"] = (paid + discount) / billed
            item["unit_estimated"] = False
        elif paid is None and priced is not None:
            item["unit_krw_per_kwh"] = priced
            item["unit_estimated"] = learned
        else:
            item["unit_krw_per_kwh"] = None
            item["unit_estimated"] = False
        into = battery_in(item)
        item["battery_in_kwh"] = into
        received = ac_received(item)
        item["ac_received_kwh"] = received
        used = _num(item.get("loss_kwh"))
        item["energy_loss_kwh"] = energy_loss_kwh(item.get("sent_kwh"), into, used)
        item["energy_loss_estimated"] = bool(
            item.get("sent_estimated") and item["energy_loss_kwh"] is not None
        )
        if into is not None and sent is not None and sent > 0:
            item["efficiency_pct"] = into / sent * 100.0
            item["efficiency_estimated"] = bool(item["sent_estimated"])
        else:
            item["efficiency_pct"] = None
            item["efficiency_estimated"] = False


def _empty_tariffs(vehicle_id: str) -> dict[str, Any]:
    return {"vehicle_id": vehicle_id, "rates": [], "members": {}, "operators": [], "env_card": False}


def list_tariffs(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    """Read the price table. Does not create it or insert the official seed."""
    try:
        rows = conn.execute(
            "SELECT vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source "
            "FROM tariff_rates WHERE vehicle_id IN (?, ?) ORDER BY operator, effective_from, speed",
            (OFFICIAL, vehicle_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return _empty_tariffs(vehicle_id)
    rates = [
        {
            "operator": r[1],
            "speed": r[2],
            "member": bool(r[3]),
            "effective_from": r[4],
            "krw_per_kwh": r[5],
            "source": "official" if r[0] == OFFICIAL else r[6],
            "official": r[0] == OFFICIAL,
        }
        for r in rows
    ]
    try:
        member_rows = conn.execute(
            "SELECT operator, member FROM tariff_member WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        member_rows = []
    members = {r[0]: bool(r[1]) for r in member_rows}
    try:
        seen = [
            canon_operator(r[0])
            for r in conn.execute(
                "SELECT DISTINCT operator FROM charge_meta WHERE vehicle_id = ? AND operator IS NOT NULL "
                "AND TRIM(operator) != ''",
                (vehicle_id,),
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        seen = []
    names = []
    for name in [PLUGLINK, *seen, *[r["operator"] for r in rates]]:
        if name and name not in names:
            names.append(name)
    return {
        "vehicle_id": vehicle_id,
        "rates": rates,
        "members": members,
        "operators": names,
        "env_card": _env_card(conn, vehicle_id),
    }


def save_tariff(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    if body.get("op") == "env_card":
        flag = 1 if body.get("env_card") in (True, 1, "1", "true") else 0
        with conn:
            conn.execute(
                "INSERT INTO payment_prefs (vehicle_id, env_card) VALUES (?, ?) "
                "ON CONFLICT(vehicle_id) DO UPDATE SET env_card = excluded.env_card",
                (vehicle_id, flag),
            )
        return list_tariffs(conn, vehicle_id)
    if body.get("op") == "member":
        operator = canon_operator(str(body.get("operator") or ""))
        if not operator:
            raise ValueError("operator is required")
        flag = 1 if body.get("member") in (True, 1, "1", "true") else 0
        with conn:
            conn.execute(
                "INSERT INTO tariff_member (vehicle_id, operator, member) VALUES (?, ?, ?) "
                "ON CONFLICT(vehicle_id, operator) DO UPDATE SET member = excluded.member",
                (vehicle_id, operator, flag),
            )
        return list_tariffs(conn, vehicle_id)
    operator = canon_operator(str(body.get("operator") or ""))
    speed = str(body.get("speed") or "")
    if not operator:
        raise ValueError("operator is required")
    if speed not in SPEEDS:
        raise ValueError("unknown speed")
    day = str(body.get("effective_from") or "")
    datetime.strptime(day, "%Y-%m-%d")
    price = _num(body.get("krw_per_kwh"))
    if price is None or price < 0:
        raise ValueError("krw_per_kwh is required")
    member = 1 if body.get("member") in (True, 1, "1", "true") else 0
    with conn:
        conn.execute(
            "INSERT INTO tariff_rates (vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'manual') "
            "ON CONFLICT(vehicle_id, operator, speed, member, effective_from) DO UPDATE SET "
            "krw_per_kwh = excluded.krw_per_kwh, source = 'manual'",
            (vehicle_id, operator, speed, member, day, price),
        )
    return list_tariffs(conn, vehicle_id)


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_BAND_SUFFIX = re.compile(r"[0-9①-⑳]+$")
_PORTAL_BAND = {
    "완속": "ac",
    "중속": "mid",
    "급속(100kW미만)": "fast50",
    "급속(100kW이상)": "fast100",
    "초급속": "ultra",
}


def portal_band(label: str) -> str | None:
    """Map a 용량구분 cell onto a stored band. Numbered variants (완속①) share the band."""
    text = _BAND_SUFFIX.sub("", str(label or "").strip()).strip()
    return _PORTAL_BAND.get(text)


def _excel_day(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return text.replace("/", "-")
    try:
        serial = float(text)
    except (TypeError, ValueError):
        return None
    if serial < 20000 or serial > 80000:
        return None
    return (datetime(1899, 12, 30) + timedelta(days=int(serial))).strftime("%Y-%m-%d")


def _price_cell(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "없음"):
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    if num < 0:
        return None
    return num


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
    return idx - 1


def _xlsx_grid(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
            for si in root.findall("m:si", _NS):
                shared.append("".join(node.text or "" for node in si.iter(tag)))
        sheet = next(name for name in names if name.startswith("xl/worksheets/sheet"))
        root = ET.fromstring(book.read(sheet))
        grid: list[list[str]] = []
        for row in root.findall("m:sheetData/m:row", _NS):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", _NS):
                ref = cell.get("r") or "A1"
                kind = cell.get("t")
                node = cell.find("m:v", _NS)
                if kind == "inlineStr":
                    text = "".join(
                        (t.text or "") for t in cell.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                    )
                elif node is None or node.text is None:
                    text = ""
                elif kind == "s":
                    text = shared[int(node.text)]
                else:
                    text = node.text
                cells[_col_index(ref)] = text
            if not cells:
                continue
            width = max(cells) + 1
            grid.append([cells.get(i, "") for i in range(width)])
        return grid


def _xls_grid(data: bytes) -> list[list[str]]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError("xls needs xlrd, or save the sheet as xlsx") from exc
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    return [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _portal_grid(source: str | Path | bytes) -> list[list[Any]]:
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    else:
        data = source
    if data[:2] == b"PK":
        return _xlsx_grid(data)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return _xls_grid(data)
    raise ValueError("price file must be xls or xlsx")


def _header_map(grid: list[list[Any]]) -> tuple[int, dict[str, int]]:
    for index, row in enumerate(grid[:8]):
        labels = [str(cell).strip() for cell in row]
        if "사업자명" in labels and "용량구분" in labels and "갱신일자" in labels:
            found = {name: labels.index(name) for name in ("사업자명", "요금유형", "용량구분", "갱신일자")}
            # 회원가 is the price under that header; 비회원가 is the later 충전요금 column.
            member_at = labels.index("회원가") if "회원가" in labels else 4
            guest_at = labels.index("비회원가") if "비회원가" in labels else member_at + 2
            found["회원가"] = member_at
            found["비회원가"] = guest_at
            return index + 1, found
    raise ValueError("price sheet header not found")


def _upsert_official(
    conn: sqlite3.Connection, operator: str, speed: str, member: int, day: str, price: float,
) -> str:
    row = conn.execute(
        "SELECT krw_per_kwh, source FROM tariff_rates WHERE vehicle_id = ? AND operator = ? AND speed = ? "
        "AND member = ? AND effective_from = ?",
        (OFFICIAL, operator, speed, member, day),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO tariff_rates (vehicle_id, operator, speed, member, effective_from, krw_per_kwh, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'official')",
            (OFFICIAL, operator, speed, member, day, price),
        )
        return "inserted"
    if row[1] != "official":
        return "kept"
    if float(row[0]) == float(price):
        return "same"
    conn.execute(
        "UPDATE tariff_rates SET krw_per_kwh = ? WHERE vehicle_id = ? AND operator = ? AND speed = ? "
        "AND member = ? AND effective_from = ? AND source = 'official'",
        (price, OFFICIAL, operator, speed, member, day),
    )
    return "updated"


def import_portal_file(conn: sqlite3.Connection, source: str | Path | bytes) -> dict[str, Any]:
    """Load one 무공해차 통합누리집 price workbook.

    Each band becomes an official row from its 갱신일자. Older rows stay, so a
    charge before that date keeps the previous price. Nothing is fetched from
    the network. A hand-edited row for the same day is left as the owner saved it.
    """
    ensure_schema(conn)
    grid = _portal_grid(source)
    start, cols = _header_map(grid)
    grouped: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    for offset, row in enumerate(grid[start:]):
        def cell(name: str, row: list[Any] = row) -> Any:
            pos = cols.get(name)
            if pos is None or pos >= len(row):
                return ""
            return row[pos]

        operator = canon_operator(str(cell("사업자명") or ""))
        band = portal_band(str(cell("용량구분") or ""))
        day = _excel_day(cell("갱신일자"))
        if not operator or band is None or day is None:
            continue
        label = str(cell("용량구분") or "")
        plan = str(cell("요금유형") or "")
        score = (2 if plan == "대표" else 0) + (1 if label == _BAND_SUFFIX.sub("", label).strip() else 0)
        key = (operator, band, day)
        prev = grouped.get(key)
        payload = {
            "member": _price_cell(cell("회원가")),
            "guest": _price_cell(cell("비회원가")),
        }
        if prev is None or score > prev[0]:
            grouped[key] = (score, payload)
    counts = {"inserted": 0, "updated": 0, "kept": 0, "same": 0}
    with conn:
        for (operator, band, day), (_score, prices) in grouped.items():
            for member, value in ((1, prices["member"]), (0, prices["guest"])):
                if value is None:
                    continue
                kind = _upsert_official(conn, operator, band, member, day, value)
                counts[kind] += 1
    counts["bands"] = len(grouped)
    return counts


def efficiency_sent(
    conn: sqlite3.Connection, vehicle_id: str, charge: dict[str, Any], meta: dict[str, Any] | None,
    ratios: tuple[float, float] | None = None,
) -> float | None:
    sent, _est = charger_sent(charge, meta, ratios or _ratios(conn, vehicle_id))
    return sent
