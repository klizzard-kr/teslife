"""Car ledger (차계부) core: tables, auto charging entries, reports (Stage 4)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CATEGORIES = (
    "charging",
    "maintenance",
    "tires",
    "insurance",
    "tax",
    "wash",
    "supplies",
    "other",
)
SOURCES = ("manual", "auto")
# Stored timestamps stay UTC ISO; calendar dates/months are computed in KST (UTC+9, no DST).
KST = timezone(timedelta(hours=9))
REGISTRATION_DATE = "2026-08-19"
INSURANCE_AMOUNT = 479850
INSURANCE_MEMO = "KB다이렉트 개인용자동차보험 2026-08-19~2027-08-19"
INSURANCE_ID = "seed-kb-direct-2026"
CSV_HEADERS = ("차량", "ID", "날짜", "분류", "금액", "할인", "주행거리", "메모", "출처")

# charge_meta.charge_id is charges.start_ts (Stage 2 primary key besides vehicle_id).
CHARGE_ID_FIELD = "start_ts"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_krw REAL,
    odometer_km REAL,
    memo TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS charge_meta (
    vehicle_id TEXT NOT NULL,
    charge_id TEXT NOT NULL,
    operator TEXT,
    station_name TEXT,
    location_text TEXT,
    unit_price_krw_per_kwh REAL,
    total_krw REAL,
    kwh_billed REAL,
    PRIMARY KEY (vehicle_id, charge_id)
);

CREATE TABLE IF NOT EXISTS stations (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    operator TEXT,
    address_text TEXT,
    default_unit_price_krw_per_kwh REAL,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS rates (
    vehicle_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    unit_price_krw_per_kwh REAL NOT NULL,
    PRIMARY KEY (vehicle_id, operator)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
    if "estimated" not in have:  # 1 = auto charging amount estimated from a saved price
        conn.execute("ALTER TABLE ledger ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0")
    meta_cols = {r[1] for r in conn.execute("PRAGMA table_info(charge_meta)").fetchall()}
    if "station_addr" not in meta_cols:
        conn.execute("ALTER TABLE charge_meta ADD COLUMN station_addr TEXT")
    if "pay_method" not in meta_cols:
        conn.execute("ALTER TABLE charge_meta ADD COLUMN pay_method TEXT")
    if "discount_krw" not in meta_cols:  # : discount (points, coupon, membership) on top of the paid amount
        if "points_krw" in meta_cols:  # the short-lived  name (always NULL): rename in place
            conn.execute("ALTER TABLE charge_meta RENAME COLUMN points_krw TO discount_krw")
        else:
            conn.execute("ALTER TABLE charge_meta ADD COLUMN discount_krw REAL")
    from ledger.consumables import ensure_consumable_schema

    ensure_consumable_schema(conn)
    from process.chargeloc import ensure_schema as ensure_chargeloc_schema

    ensure_chargeloc_schema(conn)  # stations.lat / lon
    migrate_parking_toll(conn)
    from web.notify import ensure_schema as ensure_notify_schema

    ensure_notify_schema(conn)
    from process.evcharger import ensure_schema as ensure_ev_schema

    ensure_ev_schema(conn)
    from web.tariff import ensure_schema as ensure_tariff_schema

    ensure_tariff_schema(conn)
    migrate_v190(conn)
    conn.commit()


def migrate_v190(conn: sqlite3.Connection) -> None:
    """Plug Link charges with no payment choice become 회원.

    A shorter station name becomes the longer name already stored for that
    vehicle, or the name in vehicle_settings `station_aliases`. Only UPDATE.
    Existing row counts do not change. A payment choice or a station name the
    user already saved stays.
    """
    try:
        conn.execute(
            "UPDATE charge_meta SET pay_method = 'member' "
            "WHERE (pay_method IS NULL OR TRIM(pay_method) = '') AND ("
            "IFNULL(operator, '') LIKE '%플러그%' OR lower(IFNULL(operator, '')) LIKE '%plug%' "
            "OR charge_id IN (SELECT charge_id FROM charge_charger "
            "WHERE vehicle_id = charge_meta.vehicle_id AND ("
            "IFNULL(busi_nm, '') LIKE '%플러그%' OR lower(IFNULL(busi_nm, '')) LIKE '%plug%')))"
        )
    except sqlite3.OperationalError:
        pass
    from ledger.station_alias import apply_station_aliases

    try:
        apply_station_aliases(conn)
    except sqlite3.OperationalError:
        pass


def migrate_parking_toll(conn: sqlite3.Connection) -> int:
    """주차·통행 was dropped as a category: its rows move to 기타 with the old name kept in the memo."""
    cur = conn.execute(
        "UPDATE ledger SET category = 'other', "
        "memo = CASE WHEN memo IS NULL OR memo = '' THEN '주차·통행' ELSE '주차·통행 · ' || memo END "
        "WHERE category = 'parking_toll'"
    )
    return cur.rowcount


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def decode_value(raw: str) -> Any:
    return json.loads(raw)


def auto_ledger_id(charge_id: str) -> str:
    return f"auto-charge:{charge_id}"


def kst_date(ts: str) -> str:
    """UTC ISO timestamp (e.g. 2026-10-16T23:15:00Z) -> KST calendar date YYYY-MM-DD."""
    s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def parse_date(value: str) -> str:
    if not value or not isinstance(value, str):
        raise ValueError("date is required (YYYY-MM-DD)")
    date.fromisoformat(value)
    return value


def require_category(value: Any) -> str:
    if value not in CATEGORIES:
        raise ValueError(f"unknown category: {value}")
    return str(value)


def require_non_negative(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if num < 0:
        raise ValueError(f"{name} must be non-negative")
    return num


def compute_unit_and_total(
    unit_price: float | None, total: float | None, kwh: float | None, discount: float | None = None
) -> tuple[float | None, float | None, float | None]:
    """The unit price is never typed: it is (paid amount + discount) / billed kWh, i.e. the
    value of the charge, not only the cash part.

    `unit_price` is accepted for old callers and ignored. Without both amount and
    billed kWh there is no unit price."""
    if total is not None and kwh is not None and kwh != 0:
        return (total + (discount or 0)) / kwh, total, kwh
    return None, total, kwh


def charge_value(meta: Any) -> float | None:
    """Paid amount + discount (None without a paid amount): what the energy was worth."""
    if not meta or meta["total_krw"] is None:
        return None
    try:
        disc = meta["discount_krw"]
    except (KeyError, IndexError):
        pts = None
    return float(meta["total_krw"]) + float(disc or 0)


def _col(row: Any, name: str, index: int) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[name]
    if isinstance(row, dict):
        return row[name]
    return row[index]


def _odometer_at(conn: sqlite3.Connection, vehicle_id: str, ts: str) -> float | None:
    row = conn.execute(
        """
        SELECT value FROM signals
        WHERE vehicle_id = ? AND name = 'Odometer' AND ts <= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (vehicle_id, ts),
    ).fetchone()
    if not row:
        return None
    val = decode_value(_col(row, "value", 0))
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _rate_for(
    conn: sqlite3.Connection, vehicle_id: str, operator: str | None
) -> float | None:
    if not operator:
        return None
    row = conn.execute(
        """
        SELECT unit_price_krw_per_kwh FROM rates
        WHERE vehicle_id = ? AND operator = ?
        """,
        (vehicle_id, operator),
    ).fetchone()
    if not row:
        return None
    return float(_col(row, "unit_price_krw_per_kwh", 0))


def charging_cost(
    conn: sqlite3.Connection,
    vehicle_id: str,
    kwh_added: float | None,
    meta: dict[str, Any] | None,
) -> tuple[float | None, bool]:
    """(amount, estimated). The paid amount when entered; otherwise an estimate from the
    operator's saved rate times billed kWh (else car kWh). Places carry no price."""
    meta = meta or {}
    if meta.get("total_krw") is not None:
        return float(meta["total_krw"]), False
    kwh = meta.get("kwh_billed")
    if kwh is None:
        kwh = kwh_added
    rate = _rate_for(conn, vehicle_id, meta.get("operator"))
    if rate is not None and kwh is not None:
        return float(kwh) * rate, True
    return None, False


def charging_amount(
    conn: sqlite3.Connection,
    vehicle_id: str,
    kwh_added: float | None,
    meta: dict[str, Any] | None,
) -> float | None:
    return charging_cost(conn, vehicle_id, kwh_added, meta)[0]


_META_KEYS = (
    "operator",
    "station_name",
    "location_text",
    "unit_price_krw_per_kwh",
    "total_krw",
    "kwh_billed",
    "discount_krw",
)


def _meta_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return {k: row[k] for k in _META_KEYS}
    return {
        "operator": row[0],
        "station_name": row[1],
        "location_text": row[2],
        "unit_price_krw_per_kwh": row[3],
        "total_krw": row[4],
        "kwh_billed": row[5],
        "discount_krw": row[6] if len(row) > 6 else None,
    }


def sync_auto_charges(conn: sqlite3.Connection, vehicle_id: str) -> int:
    """Create or update auto ledger rows for finished charges. Idempotent."""
    ensure_schema(conn)
    try:
        charges = conn.execute(
            """
            SELECT start_ts, kwh_added, end_ts FROM charges
            WHERE vehicle_id = ?
            """,
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    updated = 0
    with conn:
        for ch in charges:
            charge_id = _col(ch, "start_ts", 0)
            meta_row = conn.execute(
                """
                SELECT operator, station_name, location_text,
                       unit_price_krw_per_kwh, total_krw, kwh_billed, discount_krw
                FROM charge_meta
                WHERE vehicle_id = ? AND charge_id = ?
                """,
                (vehicle_id, charge_id),
            ).fetchone()
            meta = _meta_dict(meta_row)
            amount, estimated = charging_cost(
                conn, vehicle_id, _col(ch, "kwh_added", 1), meta
            )
            odo = _odometer_at(conn, vehicle_id, charge_id)
            # Bucket by charge END (payment time), KST; fall back to start if no end yet.
            day = kst_date(_col(ch, "end_ts", 2) or charge_id)
            lid = auto_ledger_id(charge_id)
            conn.execute(
                """
                INSERT INTO ledger (
                    vehicle_id, id, date, category, amount_krw, odometer_km, memo, source,
                    estimated
                ) VALUES (?, ?, ?, 'charging', ?, ?, ?, 'auto', ?)
                ON CONFLICT(vehicle_id, id) DO UPDATE SET
                    date = excluded.date,
                    amount_krw = excluded.amount_krw,
                    odometer_km = excluded.odometer_km,
                    memo = excluded.memo,
                    estimated = excluded.estimated
                """,
                (
                    vehicle_id,
                    lid,
                    day,
                    amount,
                    odo,
                    "자동 기록 충전",
                    1 if estimated else 0,
                ),
            )
            updated += 1
    return updated


def sync_all_auto_charges(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    n = 0
    try:
        vids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT vehicle_id FROM charges ORDER BY vehicle_id"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return 0
    for vid in vids:
        n += sync_auto_charges(conn, vid)
    return n


def seed_insurance(conn: sqlite3.Connection) -> int:
    """Idempotent KB Direct insurance row for each vehicle_id in signals."""
    ensure_schema(conn)
    try:
        vids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT vehicle_id FROM signals ORDER BY vehicle_id"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        vids = []
    if not vids:
        try:
            vids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT vehicle_id FROM ledger ORDER BY vehicle_id"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            vids = []
    inserted = 0
    with conn:
        for vid in vids:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO ledger (
                    vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
                ) VALUES (?, ?, ?, 'insurance', ?, NULL, ?, 'manual')
                """,
                (
                    vid,
                    INSURANCE_ID,
                    REGISTRATION_DATE,
                    INSURANCE_AMOUNT,
                    INSURANCE_MEMO,
                ),
            )
            inserted += cur.rowcount
    return inserted


def parse_day_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """KST calendar days (YYYY-MM-DD, both inclusive). Either may be missing."""
    for v in (date_from, date_to):
        if v is not None:
            try:
                date.fromisoformat(v)
            except (TypeError, ValueError):
                raise ValueError("from / to must be YYYY-MM-DD") from None
            if len(v) != 10:
                raise ValueError("from / to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise ValueError("from must not be after to")
    return date_from, date_to


def _ledger_where(
    vehicle_id: str,
    month: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list[Any]]:
    sql = "WHERE ledger.vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    if month:
        sql += " AND ledger.date LIKE ?"
        params.append(month + "%")
    date_from, date_to = parse_day_range(date_from, date_to)
    if date_from:
        sql += " AND ledger.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND ledger.date <= ?"
        params.append(date_to)
    if category:
        require_category(category)
        sql += " AND ledger.category = ?"
        params.append(category)
    return sql, params


# Same calendar day: latest charge end first (start if the session has not ended),
# then id DESC so manual rows on that day are newest-id first. CSV uses this too.
_LEDGER_ORDER = """
ORDER BY ledger.date DESC,
         COALESCE(NULLIF(charges.end_ts, ''), charges.start_ts, '') DESC,
         ledger.id DESC
"""


def list_ledger(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _ledger_where(vehicle_id, month, category, date_from, date_to)
    sql = f"""
        SELECT ledger.* FROM ledger
        LEFT JOIN charges
          ON charges.vehicle_id = ledger.vehicle_id
         AND ledger.source = 'auto'
         AND charges.start_ts = substr(ledger.id, length('auto-charge:') + 1)
        {where}
        {_LEDGER_ORDER}
    """
    page_params = list(params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        page_params.extend([int(limit), int(offset)])
    try:
        rows = conn.execute(sql, page_params).fetchall()
    except sqlite3.OperationalError:
        sql = f"SELECT * FROM ledger AS ledger {where} ORDER BY ledger.date DESC, ledger.id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
        try:
            rows = conn.execute(sql, page_params).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_ledger_row(r) for r in rows]


def count_ledger(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where, params = _ledger_where(vehicle_id, month, category, date_from, date_to)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ledger AS ledger {where}", params
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row["n"] is None:
        return 0
    return int(row["n"])


def _ledger_row(row: sqlite3.Row) -> dict[str, Any]:
    amount = row["amount_krw"]
    estimated = bool(row["estimated"]) if "estimated" in row.keys() else False
    return {
        "vehicle_id": row["vehicle_id"],
        "id": row["id"],
        "date": row["date"],
        "category": row["category"],
        "amount_krw": amount,
        "amount_display": "금액 미입력" if amount is None else amount,
        "estimated": estimated and amount is not None,
        "odometer_km": row["odometer_km"],
        "memo": row["memo"],
        "source": row["source"],
        "editable": row["source"] == "manual",
    }


def create_manual_entry(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    day = parse_date(str(body.get("date") or ""))
    category = require_category(body.get("category"))
    amount = require_non_negative(body.get("amount_krw"), "amount_krw")
    if amount is None:
        raise ValueError("amount_krw is required")
    odo = require_non_negative(body.get("odometer_km"), "odometer_km")
    memo = body.get("memo")
    lid = str(body.get("id") or new_id("led"))
    with conn:
        conn.execute(
            """
            INSERT INTO ledger (
                vehicle_id, id, date, category, amount_krw, odometer_km, memo, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
            """,
            (vehicle_id, lid, day, category, amount, odo, memo),
        )
    row = conn.execute(
        "SELECT * FROM ledger WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, lid),
    ).fetchone()
    return _ledger_row(row)


def update_manual_entry(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    lid = body.get("id")
    if not lid:
        raise ValueError("id is required")
    existing = conn.execute(
        "SELECT * FROM ledger WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, lid),
    ).fetchone()
    if existing is None:
        raise KeyError("ledger entry not found")
    if existing["source"] != "manual":
        raise ValueError("auto ledger entries cannot be edited")
    day = parse_date(str(body.get("date") or existing["date"]))
    category = require_category(body.get("category") or existing["category"])
    amount = require_non_negative(
        body["amount_krw"] if "amount_krw" in body else existing["amount_krw"],
        "amount_krw",
    )
    odo = require_non_negative(
        body["odometer_km"] if "odometer_km" in body else existing["odometer_km"],
        "odometer_km",
    )
    memo = body["memo"] if "memo" in body else existing["memo"]
    with conn:
        conn.execute(
            """
            UPDATE ledger SET date = ?, category = ?, amount_krw = ?,
                odometer_km = ?, memo = ?
            WHERE vehicle_id = ? AND id = ? AND source = 'manual'
            """,
            (day, category, amount, odo, memo, vehicle_id, lid),
        )
    row = conn.execute(
        "SELECT * FROM ledger WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, lid),
    ).fetchone()
    return _ledger_row(row)


def delete_manual_entry(conn: sqlite3.Connection, vehicle_id: str, lid: str) -> None:
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT source FROM ledger WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, lid),
    ).fetchone()
    if existing is None:
        raise KeyError("ledger entry not found")
    if existing["source"] != "manual":
        raise ValueError("auto ledger entries cannot be deleted")
    with conn:
        conn.execute(
            "DELETE FROM ledger WHERE vehicle_id = ? AND id = ? AND source = 'manual'",
            (vehicle_id, lid),
        )


def upsert_charge_meta(
    conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    ensure_schema(conn)
    charge_id = body.get("charge_id") or body.get("start_ts")
    if not charge_id:
        raise ValueError("charge_id (charges.start_ts) is required")
    unit = require_non_negative(body.get("unit_price_krw_per_kwh"), "unit_price_krw_per_kwh")
    total = require_non_negative(body.get("total_krw"), "total_krw")
    kwh = require_non_negative(body.get("kwh_billed"), "kwh_billed")
    # 할인: the form always sends it (null = none); other callers keep the stored value
    if "discount_krw" in body:
        discount = require_non_negative(body.get("discount_krw"), "discount_krw")
    else:
        try:
            prev = conn.execute("SELECT discount_krw FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
                                (vehicle_id, charge_id)).fetchone()
        except sqlite3.OperationalError:
            prev = None
        discount = prev[0] if prev else None
    if discount == 0:
        discount = None
    unit, total, kwh = compute_unit_and_total(unit, total, kwh, discount)
    operator = body.get("operator")
    location_text = body.get("location_text")
    pay_method = body.get("pay_method") if "pay_method" in body else None
    if "pay_method" not in body:
        try:
            prev_pay = conn.execute(
                "SELECT pay_method FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
                (vehicle_id, charge_id),
            ).fetchone()
        except sqlite3.OperationalError:
            prev_pay = None
        pay_method = prev_pay[0] if prev_pay else None
    elif pay_method not in ("member", "env_card", "guest"):
        pay_method = None
    # The form no longer has a station name (장소 = location_text only). Keep a stored
    # station_name unless the caller sends the key explicitly.
    if "station_name" in body:
        station_name = body.get("station_name")
    else:
        try:
            old = conn.execute(
                "SELECT station_name FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
                (vehicle_id, charge_id),
            ).fetchone()
        except sqlite3.OperationalError:
            old = None
        station_name = old[0] if old else None
    with conn:
        conn.execute(
            """
            INSERT INTO charge_meta (
                vehicle_id, charge_id, operator, station_name, location_text,
                unit_price_krw_per_kwh, total_krw, kwh_billed, discount_krw, pay_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET
                operator = excluded.operator,
                station_name = excluded.station_name,
                location_text = excluded.location_text,
                unit_price_krw_per_kwh = excluded.unit_price_krw_per_kwh,
                total_krw = excluded.total_krw,
                kwh_billed = excluded.kwh_billed,
                discount_krw = excluded.discount_krw,
                pay_method = excluded.pay_method
            """,
            (
                vehicle_id,
                charge_id,
                operator,
                station_name,
                location_text,
                unit,
                total,
                kwh,
                discount,
                pay_method,
            ),
        )
    sync_auto_charges(conn, vehicle_id)
    row = conn.execute(
        "SELECT * FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
        (vehicle_id, charge_id),
    ).fetchone()
    return {k: row[k] for k in row.keys()}


def assign_operator(conn: sqlite3.Connection, vehicle_id: str, charge_id: str, operator: str) -> dict[str, Any]:
    """Set only the operator. Payment fields already stored stay as they are."""
    ensure_schema(conn)
    name = (operator or "").strip()
    if not charge_id or not name:
        raise ValueError("charge_id and operator are required")
    with conn:
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET operator = excluded.operator",
            (vehicle_id, charge_id, name),
        )
    row = conn.execute(
        "SELECT * FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
        (vehicle_id, charge_id),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else {"charge_id": charge_id, "operator": name}


def list_charge_meta(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
        "SELECT * FROM charge_meta WHERE vehicle_id = ? ORDER BY charge_id",
        (vehicle_id,),
    ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{k: r[k] for k in r.keys()} for r in rows]


def _station_latlon(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    if lat in (None, "") and lon in (None, ""):
        return None, None
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("lat and lon must be numbers") from None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise ValueError("lat/lon out of range")
    return round(la, 6), round(lo, 6)


def list_stations(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
        "SELECT * FROM stations WHERE vehicle_id = ? ORDER BY name",
        (vehicle_id,),
    ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{k: r[k] for k in r.keys()} for r in rows]


def create_station(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    name = body.get("name") or body.get("address_text")  # a saved 장소 is named by its text
    if not name:
        raise ValueError("address_text (장소) is required")
    sid = str(body.get("id") or new_id("st"))
    price = require_non_negative(
        body.get("default_unit_price_krw_per_kwh"), "default_unit_price_krw_per_kwh"
    )
    with conn:
        conn.execute(
            """
            INSERT INTO stations (
                vehicle_id, id, name, operator, address_text, default_unit_price_krw_per_kwh, lat, lon
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vehicle_id,
                sid,
                name,
                body.get("operator"),
                body.get("address_text"),
                price,
                *_station_latlon(body.get("lat"), body.get("lon")),
            ),
        )
    row = conn.execute(
        "SELECT * FROM stations WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, sid),
    ).fetchone()
    return {k: row[k] for k in row.keys()}


def update_station(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    sid = body.get("id")
    if not sid:
        raise ValueError("id is required")
    existing = conn.execute(
        "SELECT * FROM stations WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, sid),
    ).fetchone()
    if existing is None:
        raise KeyError("station not found")
    name = body.get("name") or existing["name"]
    price = require_non_negative(
        body["default_unit_price_krw_per_kwh"]
        if "default_unit_price_krw_per_kwh" in body
        else existing["default_unit_price_krw_per_kwh"],
        "default_unit_price_krw_per_kwh",
    )
    operator = body["operator"] if "operator" in body else existing["operator"]
    address = body["address_text"] if "address_text" in body else existing["address_text"]
    lat, lon = (_station_latlon(body.get("lat"), body.get("lon")) if "lat" in body or "lon" in body
                else (existing["lat"], existing["lon"]))
    with conn:
        conn.execute(
            """
            UPDATE stations SET name = ?, operator = ?, address_text = ?,
                default_unit_price_krw_per_kwh = ?, lat = ?, lon = ?
            WHERE vehicle_id = ? AND id = ?
            """,
            (name, operator, address, price, lat, lon, vehicle_id, sid),
        )
    row = conn.execute(
        "SELECT * FROM stations WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, sid),
    ).fetchone()
    return {k: row[k] for k in row.keys()}


def delete_station(conn: sqlite3.Connection, vehicle_id: str, sid: str) -> None:
    ensure_schema(conn)
    with conn:
        cur = conn.execute(
            "DELETE FROM stations WHERE vehicle_id = ? AND id = ?",
            (vehicle_id, sid),
        )
        if cur.rowcount == 0:
            raise KeyError("station not found")


def list_rates(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
        "SELECT * FROM rates WHERE vehicle_id = ? ORDER BY operator",
        (vehicle_id,),
    ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{k: r[k] for k in r.keys()} for r in rows]


def upsert_rate(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    operator = body.get("operator")
    if not operator:
        raise ValueError("operator is required")
    price = require_non_negative(body.get("unit_price_krw_per_kwh"), "unit_price_krw_per_kwh")
    if price is None:
        raise ValueError("unit_price_krw_per_kwh is required")
    with conn:
        conn.execute(
            """
            INSERT INTO rates (vehicle_id, operator, unit_price_krw_per_kwh)
            VALUES (?, ?, ?)
            ON CONFLICT(vehicle_id, operator) DO UPDATE SET
                unit_price_krw_per_kwh = excluded.unit_price_krw_per_kwh
            """,
            (vehicle_id, operator, price),
        )
    sync_auto_charges(conn, vehicle_id)
    row = conn.execute(
        "SELECT * FROM rates WHERE vehicle_id = ? AND operator = ?",
        (vehicle_id, operator),
    ).fetchone()
    return {k: row[k] for k in row.keys()}


def delete_rate(conn: sqlite3.Connection, vehicle_id: str, operator: str) -> None:
    ensure_schema(conn)
    with conn:
        cur = conn.execute(
            "DELETE FROM rates WHERE vehicle_id = ? AND operator = ?",
            (vehicle_id, operator),
        )
        if cur.rowcount == 0:
            raise KeyError("rate not found")
    sync_auto_charges(conn, vehicle_id)


def _period_filter(column: str, month: str | None, year: str | None) -> tuple[str, list[Any]]:
    if month:
        return f" AND {column} LIKE ?", [month + "%"]
    if year:
        return f" AND {column} LIKE ?", [year + "%"]
    return "", []


def report_by_period(
    conn: sqlite3.Connection,
    vehicle_id: str,
    grain: str,
    year: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    if year is not None and (len(year) != 4 or not year.isdigit()):
        raise ValueError("year must be YYYY")
    if category:
        require_category(category)
    width = 7 if grain == "month" else 4
    key_name = "month" if grain == "month" else "year"
    sql = f"""
        SELECT substr(date, 1, {width}) AS period, category,
               SUM(amount_krw) AS total
        FROM ledger
        WHERE vehicle_id = ?
    """
    params: list[Any] = [vehicle_id]
    if year:
        sql += " AND date LIKE ?"
        params.append(year + "%")
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " GROUP BY period, category ORDER BY period, category"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        period = row["period"]
        bucket = buckets.setdefault(
            period, {key_name: period, "by_category": {}, "total": 0.0}
        )
        cat_total = float(row["total"] or 0)
        bucket["by_category"][row["category"]] = cat_total
        bucket["total"] += cat_total
    return {"vehicle_id": vehicle_id, "items": list(buckets.values())}


def _trip_km(conn: sqlite3.Connection, vehicle_id: str, prefix: str | None) -> float:
    sql = "SELECT COALESCE(SUM(km), 0) AS km FROM trips WHERE vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    if prefix:
        sql += " AND date(start_ts, '+9 hours') LIKE ?"
        params.append(prefix + "%")
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(row["km"] or 0)


def _ledger_sum(
    conn: sqlite3.Connection,
    vehicle_id: str,
    prefix: str | None = None,
    since: str | None = None,
    estimated_only: bool = False,
) -> float:
    sql = "SELECT COALESCE(SUM(amount_krw), 0) AS total FROM ledger WHERE vehicle_id = ?"
    if estimated_only:
        sql += " AND estimated = 1"
    params: list[Any] = [vehicle_id]
    if prefix:
        sql += " AND date LIKE ?"
        params.append(prefix + "%")
    if since:
        sql += " AND date >= ?"
        params.append(since)
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(row["total"] or 0)


def cost_per_km(
    conn: sqlite3.Connection,
    vehicle_id: str,
    month: str | None = None,
    year: str | None = None,
) -> dict[str, Any]:
    prefix = month or year
    total = _ledger_sum(conn, vehicle_id, prefix=prefix)
    km = _trip_km(conn, vehicle_id, prefix)
    per = (total / km) if km > 0 else None
    return {
        "vehicle_id": vehicle_id,
        "month": month,
        "year": year,
        "ledger_total_krw": total,
        "estimated_krw": _ledger_sum(conn, vehicle_id, prefix=prefix, estimated_only=True),
        "km": km,
        "krw_per_km": per,
    }


def ownership_total(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    total = _ledger_sum(conn, vehicle_id, since=REGISTRATION_DATE)
    return {
        "vehicle_id": vehicle_id,
        "since": REGISTRATION_DATE,
        "total_krw": total,
        "estimated_krw": _ledger_sum(conn, vehicle_id, since=REGISTRATION_DATE, estimated_only=True),
    }


def charging_stats(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    try:
        metas = conn.execute(
        """
        SELECT * FROM charge_meta WHERE vehicle_id = ?
        """,
        (vehicle_id,),
    ).fetchall()
    except sqlite3.OperationalError:
        metas = []
    try:
        end_by_id = {
            r["start_ts"]: r["end_ts"]
            for r in conn.execute(
                "SELECT start_ts, end_ts FROM charges WHERE vehicle_id = ?",
                (vehicle_id,),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        end_by_id = {}

    def group(key: str) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, float]] = {}
        for m in metas:
            name = m[key]
            if not name:
                continue
            value = charge_value(m)  # paid + discount, for the unit price
            kwh = m["kwh_billed"]
            if value is None or kwh is None or float(kwh) == 0:
                continue
            b = buckets.setdefault(name, {"cost": 0.0, "value": 0.0, "kwh": 0.0})
            b["cost"] += float(m["total_krw"])  # money spent = paid only
            b["value"] += float(value)
            b["kwh"] += float(kwh)
        out = []
        for name, b in sorted(buckets.items()):
            out.append(
                {
                    key: name,
                    "total_krw": b["cost"],
                    "kwh": b["kwh"],
                    "krw_per_kwh": b["value"] / b["kwh"],
                }
            )
        return out

    # Average unit price = (paid + discount) / billed kWh, over charges that have both.
    paid = [
        (charge_value(m), float(m["kwh_billed"]))
        for m in metas
        if m["total_krw"] is not None and m["kwh_billed"] is not None and float(m["kwh_billed"]) > 0
    ]
    paid_kwh = sum(k for _, k in paid)
    avg_unit = (sum(t for t, _ in paid) / paid_kwh) if paid_kwh > 0 else None
    monthly: dict[str, float] = {}
    stops: list[dict[str, Any]] = []
    for m in metas:
        if m["total_krw"] is None:
            continue
        cid = m["charge_id"]
        month = kst_date(end_by_id.get(cid) or cid)[:7] if cid else ""
        if month:
            monthly[month] = monthly.get(month, 0.0) + float(m["total_krw"])
        stops.append(
            {
                "charge_id": m["charge_id"],
                "operator": m["operator"],
                "station_name": m["station_name"],
                "location_text": m["location_text"],
                "place": m["location_text"] or m["station_name"],
                "total_krw": float(m["total_krw"]),
            }
        )
    cheapest = min(stops, key=lambda s: s["total_krw"]) if stops else None
    dearest = max(stops, key=lambda s: s["total_krw"]) if stops else None
    return {
        "vehicle_id": vehicle_id,
        "by_operator": group("operator"),
        "by_station": group("station_name"),
        "average_unit_price": avg_unit,
        "monthly_totals": [
            {"month": k, "total_krw": v} for k, v in sorted(monthly.items())
        ],
        "cheapest": cheapest,
        "most_expensive": dearest,
    }


def ledger_summary(
    conn: sqlite3.Connection,
    vehicle_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Totals for a KST day range: sum, rows, per category (estimated amounts counted and flagged)."""
    where, params = _ledger_where(vehicle_id, None, category, date_from, date_to)
    try:
        rows = conn.execute(
            f"SELECT category, COUNT(*) AS n, COALESCE(SUM(amount_krw), 0) AS total, "
            f"COALESCE(SUM(CASE WHEN estimated = 1 THEN amount_krw ELSE 0 END), 0) AS est, "
            f"COALESCE(SUM(CASE WHEN amount_krw IS NULL THEN 1 ELSE 0 END), 0) AS missing "
            f"FROM ledger AS ledger {where} GROUP BY category ORDER BY total DESC",
            params,
        ).fetchall()
        first = conn.execute("SELECT MIN(date) FROM ledger WHERE vehicle_id = ?", (vehicle_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        rows, first = [], None
    by = [{"category": r["category"], "count": int(r["n"]), "total_krw": float(r["total"]),
           "estimated_krw": float(r["est"]), "missing_amount": int(r["missing"] or 0)} for r in rows]
    return {
        "vehicle_id": vehicle_id, "from": date_from, "to": date_to,
        "count": sum(b["count"] for b in by),
        "total_krw": sum(b["total_krw"] for b in by),
        "estimated_krw": sum(b["estimated_krw"] for b in by),
        "missing_amount": sum(b["missing_amount"] for b in by),
        "by_category": by,
        "first_day": first,
    }


def csv_export(conn: sqlite3.Connection, vehicle_id: str, date_from: str | None = None,
               date_to: str | None = None) -> bytes:
    rows = list_ledger(conn, vehicle_id, date_from=date_from, date_to=date_to)
    # 할인 column: only auto charge rows can have one (charge_meta.discount_krw, )
    discounts = {
        auto_ledger_id(cid): d
        for cid, d in conn.execute(
            "SELECT charge_id, discount_krw FROM charge_meta WHERE vehicle_id = ? AND discount_krw > 0",
            (vehicle_id,),
        ).fetchall()
    }
    lines = [",".join(CSV_HEADERS)]
    for r in rows:
        disc = discounts.get(r["id"])
        cells = [
            r["vehicle_id"],
            r["id"],
            r["date"],
            r["category"],
            "" if r["amount_krw"] is None else str(r["amount_krw"]),
            "" if disc is None else str(int(disc) if float(disc).is_integer() else disc),
            "" if r["odometer_km"] is None else str(r["odometer_km"]),
            (r["memo"] or "").replace('"', '""'),
            r["source"],
        ]
        escaped = []
        for c in cells:
            s = str(c)
            if any(ch in s for ch in ',\"\n'):
                escaped.append('"' + s + '"')
            else:
                escaped.append(s)
        lines.append(",".join(escaped))
    text = "\n".join(lines) + "\n"
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teslife ledger seed and auto-charge sync.")
    parser.add_argument("--db", default="signals.db")
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        sync_all_auto_charges(conn)
        if not args.no_seed:
            seed_insurance(conn)
            from ledger.consumables import seed_consumables

            seed_consumables(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
