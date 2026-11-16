"""Consumables schedule, replacements, and due status (Stage 5)."""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ledger import (
    REGISTRATION_DATE,
    decode_value,
    new_id,
    parse_date,
    require_non_negative,
)

RULES = (
    "km_or_months_whichever_first",
    "km_only",
    "months_only",
    "as_needed",
)
UNSET_LABEL = "주기 미설정"
AS_NEEDED_LABEL = "필요 시 교체"
# t583: record kinds. A record restarts the cycle only when its action is in the item's reset set.
ACTIONS = ("replace", "inspect", "service")
ACTION_KO = {"replace": "교체", "inspect": "점검", "service": "작업"}
RESET_ON = {  # item.reset_on -> actions that restart the cycle
    "replace": ("replace",),
    "inspect": ("replace", "inspect", "service"),
    "service": ("replace", "service"),
}
AS_NEEDED_LABELS = {"replace": "필요 시 교체", "inspect": "필요 시 점검", "service": "필요 시"}
# Seed defaults (filled only where the column is still NULL, so owner edits stay).
SEED_ACTIONS = {
    "cons-tire-rotation": ("service", "service"),
    "cons-cabin-filter": ("replace", "replace"),
    "cons-hepa-filter": ("replace", "replace"),
    "cons-brake-fluid": ("inspect", "inspect"),
    "cons-wiper": ("replace", "replace"),
    "cons-caliper": ("service", "service"),
    "cons-washer": ("inspect", "inspect"),
    "cons-battery-full": ("inspect", "inspect"),
    "cons-ac-desiccant": ("replace", "replace"),
    "cons-coolant": ("inspect", "inspect"),
    "cons-tires-season": ("replace", "replace"),
    "cons-12v-battery": ("replace", "replace"),
    "cons-brake-pads": ("inspect", "replace"),
    "cons-alignment": ("service", "service"),
}
SETTING_REGISTRATION_DATE = "registration_date"
DEFAULT_WARN_KM = 500.0
DEFAULT_WARN_DAYS = 14
REGISTRATION_ODOMETER_KM = 0.0
SEED_SOURCE_NOTE = "테슬라 공식 정비 안내 기준, 보통 사용 조건"

# Keep existing ids/names; add missing Tesla items. Intervals fill only when both are NULL.
SEED_ITEMS = (
    {
        "id": "cons-tire-rotation",
        "name": "타이어 로테이션",
        "interval_km": 10000.0,
        "interval_months": None,
        "rule": "km_only",
        "active": 1,
        "note": SEED_SOURCE_NOTE + ". 트레드 깊이 차이가 1.5 mm 이상일 때도 교환",
    },
    {
        "id": "cons-cabin-filter",
        "name": "에어컨 필터(캐빈)",
        "interval_km": None,
        "interval_months": 24.0,
        "rule": "months_only",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-hepa-filter",
        "name": "HEPA·카본 필터",
        "interval_km": None,
        "interval_months": 36.0,
        "rule": "months_only",
        "active": 0,
        "note": SEED_SOURCE_NOTE + ". 트림에 HEPA가 없을 수 있어 기본 비활성",
    },
    {
        "id": "cons-brake-fluid",
        "name": "브레이크액",
        "interval_km": None,
        "interval_months": 48.0,
        "rule": "months_only",
        "active": 1,
        "note": SEED_SOURCE_NOTE + ". 상태 확인, 필요 시 교체",
    },
    {
        "id": "cons-wiper",
        "name": "와이퍼",
        "interval_km": None,
        "interval_months": 12.0,
        "rule": "months_only",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-caliper",
        "name": "브레이크 캘리퍼 세척·윤활",
        "interval_km": 20000.0,
        "interval_months": 12.0,
        "rule": "km_or_months_whichever_first",
        "active": 1,
        "note": SEED_SOURCE_NOTE + ". 겨울철 염화칼슘 지역",
    },
    {
        "id": "cons-washer",
        "name": "워셔액 점검",
        "interval_km": None,
        "interval_months": 1.0,
        "rule": "months_only",
        "active": 1,
        "note": SEED_SOURCE_NOTE + ". 점검을 뜻하며 부품 교체가 아님",
    },
    {
        "id": "cons-battery-full",
        "name": "배터리 완충",
        "interval_km": None,
        "interval_months": 1.0,
        "rule": "months_only",
        "active": 1,
        "note": "한 달에 한 번 99.5% 이상 충전. 그런 충전의 종료일을 마지막 관리일로 씁니다.",
    },
    {
        "id": "cons-ac-desiccant",
        "name": "에어컨 제습제",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-coolant",
        "name": "냉각수",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-tires-season",
        "name": "타이어(썸머/윈터)",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-12v-battery",
        "name": "12V 배터리",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-brake-pads",
        "name": "브레이크 패드 점검",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
    {
        "id": "cons-alignment",
        "name": "휠 얼라인먼트",
        "interval_km": None,
        "interval_months": None,
        "rule": "as_needed",
        "active": 1,
        "note": SEED_SOURCE_NOTE,
    },
)

CONSUMABLE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS consumable_items (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    interval_km REAL,
    interval_months REAL,
    rule TEXT NOT NULL,
    warn_before_km REAL,
    warn_before_days REAL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS replacements (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    date TEXT NOT NULL,
    odometer_km REAL NOT NULL,
    price_krw REAL NOT NULL,
    vendor TEXT,
    quantity REAL,
    memo TEXT,
    receipt_path TEXT,
    ledger_id TEXT,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS vendors (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS vehicle_settings (
    vehicle_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (vehicle_id, key)
);
"""


def ensure_consumable_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CONSUMABLE_SCHEMA_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
    if "replacement_id" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN replacement_id TEXT")
    item_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(consumable_items)").fetchall()
    }
    if "note" not in item_cols:
        conn.execute("ALTER TABLE consumable_items ADD COLUMN note TEXT")
    if "default_action" not in item_cols:
        conn.execute("ALTER TABLE consumable_items ADD COLUMN default_action TEXT")
    if "reset_on" not in item_cols:
        conn.execute("ALTER TABLE consumable_items ADD COLUMN reset_on TEXT")
    rep_cols = {r[1] for r in conn.execute("PRAGMA table_info(replacements)").fetchall()}
    if "action" not in rep_cols:  # existing records were all replacements
        conn.execute("ALTER TABLE replacements ADD COLUMN action TEXT NOT NULL DEFAULT 'replace'")
    from ledger.tiresets import ensure_tireset_schema
    ensure_tireset_schema(conn)
    conn.commit()


def add_months(day: date, months: int) -> date:
    months = int(months)
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def due_label(name: str, action: str) -> str:
    """'에어컨 필터 교체', '워셔액 점검' (no '점검 점검'), '타이어 로테이션' for 작업."""
    name = (name or "").strip()
    if action == "service":
        return name
    word = ACTION_KO.get(action, "교체")
    return name if name.endswith(word) else f"{name} {word}"


def require_action(value: Any, field: str = "action") -> str:
    if value not in ACTIONS:
        raise ValueError(f"unknown {field}: {value}")
    return str(value)


def item_actions(item: Any) -> tuple[str, str]:
    """(default_action, reset_on) with NULL -> seed default or 'replace'."""
    def g(k):
        try:
            return item[k]
        except (KeyError, IndexError, TypeError):
            return None
    seed = SEED_ACTIONS.get(g("id") or "", ("replace", "replace"))
    da = g("default_action") if g("default_action") in ACTIONS else seed[0]
    ro = g("reset_on") if g("reset_on") in RESET_ON else seed[1]
    return da, ro


def require_rule(value: Any) -> str:
    if value not in RULES:
        raise ValueError(f"unknown rule: {value}")
    return str(value)


def ledger_category_for_item(name: str) -> str:
    return "tires" if "타이어" in (name or "") else "maintenance"


def current_odometer(conn: sqlite3.Connection, vehicle_id: str) -> float | None:
    row = conn.execute(
        """
        SELECT value FROM signals
        WHERE vehicle_id = ? AND name = 'Odometer'
        ORDER BY ts DESC
        LIMIT 1
        """,
        (vehicle_id,),
    ).fetchone()
    if not row:
        return None
    raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        return float(decode_value(raw))
    except (TypeError, ValueError, sqlite3.Error):
        return None


def get_registration_date(conn: sqlite3.Connection, vehicle_id: str) -> str:
    try:
        row = conn.execute(
            """
            SELECT value FROM vehicle_settings
            WHERE vehicle_id = ? AND key = ?
            """,
            (vehicle_id, SETTING_REGISTRATION_DATE),
        ).fetchone()
    except sqlite3.OperationalError:
        return REGISTRATION_DATE
    if not row or not row[0]:
        return REGISTRATION_DATE
    return str(row[0])


def set_registration_date(conn: sqlite3.Connection, vehicle_id: str, value: str) -> str:
    day = parse_date(value)
    with conn:
        conn.execute(
            """
            INSERT INTO vehicle_settings (vehicle_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(vehicle_id, key) DO UPDATE SET value = excluded.value
            """,
            (vehicle_id, SETTING_REGISTRATION_DATE, day),
        )
    return day


def seed_consumables(conn: sqlite3.Connection) -> int:
    """Insert Tesla defaults. Never overwrite a non-NULL interval the user set."""
    ensure_consumable_schema(conn)
    vids: list[str] = []
    for sql in (
        "SELECT DISTINCT vehicle_id FROM signals ORDER BY vehicle_id",
        "SELECT DISTINCT vehicle_id FROM ledger ORDER BY vehicle_id",
        "SELECT DISTINCT vehicle_id FROM consumable_items ORDER BY vehicle_id",
    ):
        try:
            vids = [r[0] for r in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            vids = []
        if vids:
            break
    changed = 0
    with conn:
        for vid in vids:
            for spec in SEED_ITEMS:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO consumable_items (
                        vehicle_id, id, name, interval_km, interval_months, rule,
                        warn_before_km, warn_before_days, active, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        vid,
                        spec["id"],
                        spec["name"],
                        spec["interval_km"],
                        spec["interval_months"],
                        spec["rule"],
                        DEFAULT_WARN_KM,
                        DEFAULT_WARN_DAYS,
                        spec["active"],
                        spec["note"],
                    ),
                )
                changed += cur.rowcount
                # Existing rows: fill only when both intervals are still NULL.
                cur = conn.execute(
                    """
                    UPDATE consumable_items SET
                        interval_km = ?,
                        interval_months = ?,
                        rule = ?,
                        note = COALESCE(note, ?)
                    WHERE vehicle_id = ? AND id = ?
                      AND interval_km IS NULL AND interval_months IS NULL
                    """,
                    (
                        spec["interval_km"],
                        spec["interval_months"],
                        spec["rule"],
                        spec["note"],
                        vid,
                        spec["id"],
                    ),
                )
                changed += cur.rowcount
                da, ro = SEED_ACTIONS.get(spec["id"], ("replace", "replace"))
                cur = conn.execute(
                    "UPDATE consumable_items SET default_action = COALESCE(default_action, ?), "
                    "reset_on = COALESCE(reset_on, ?) WHERE vehicle_id = ? AND id = ? "
                    "AND (default_action IS NULL OR reset_on IS NULL)",
                    (da, ro, vid, spec["id"]),
                )
                changed += cur.rowcount
    from ledger.tiresets import migrate_season_item
    for vid in vids:
        changed += migrate_season_item(conn, vid)
    return changed


def compute_item_status(
    *,
    rule: str,
    interval_km: float | None,
    interval_months: float | None,
    last_date: str,
    last_odo: float,
    current_odo: float | None,
    as_of: date,
    warn_before_km: float | None,
    warn_before_days: float | None,
) -> dict[str, Any]:
    """Status math: ok / soon / overdue / unset / as_needed. Baseline last_* is registration if never replaced."""
    require_rule(rule)
    empty = {
        "last_replaced_date": last_date,
        "last_replaced_odometer_km": last_odo,
        "next_due_km": None,
        "next_due_date": None,
        "remaining_km": None,
        "remaining_days": None,
    }
    if rule == "as_needed" and interval_km is None and interval_months is None:
        return {**empty, "status": "as_needed", "interval_label": AS_NEEDED_LABEL}
    use_km = rule in ("km_only", "km_or_months_whichever_first", "as_needed")
    use_months = rule in ("months_only", "km_or_months_whichever_first", "as_needed")
    next_due_km = None
    remaining_km = None
    next_due_date = None
    remaining_days = None
    km_applicable = use_km and interval_km is not None
    months_applicable = use_months and interval_months is not None
    if not km_applicable and not months_applicable:
        return {**empty, "status": "unset", "interval_label": UNSET_LABEL}
    if km_applicable:
        next_due_km = last_odo + float(interval_km)
        odo = 0.0 if current_odo is None else float(current_odo)
        remaining_km = next_due_km - odo
    if months_applicable:
        base = date.fromisoformat(last_date)
        next_due = add_months(base, int(interval_months))
        next_due_date = next_due.isoformat()
        remaining_days = (next_due - as_of).days

    warn_km = float(warn_before_km) if warn_before_km is not None else 0.0
    warn_days = float(warn_before_days) if warn_before_days is not None else 0.0

    overdue = False
    soon = False
    if remaining_km is not None:
        if remaining_km <= 0:
            overdue = True
        elif remaining_km <= warn_km:
            soon = True
    if remaining_days is not None:
        if remaining_days <= 0:
            overdue = True
        elif remaining_days <= warn_days:
            soon = True
    status = "overdue" if overdue else ("soon" if soon else "ok")
    return {
        "last_replaced_date": last_date,
        "last_replaced_odometer_km": last_odo,
        "next_due_km": next_due_km,
        "next_due_date": next_due_date,
        "remaining_km": remaining_km,
        "remaining_days": remaining_days,
        "status": status,
        "interval_label": None,
    }


def _latest_replacement(
    conn: sqlite3.Connection, vehicle_id: str, item_id: str, actions: tuple[str, ...] | None = None
) -> sqlite3.Row | None:
    """Latest record of the item; with `actions`, only records that restart the cycle."""
    sql = "SELECT * FROM replacements WHERE vehicle_id = ? AND item_id = ?"
    params: list[Any] = [vehicle_id, item_id]
    if actions:
        sql += " AND COALESCE(action, 'replace') IN (%s)" % ",".join("?" * len(actions))
        params.extend(actions)
    sql += " ORDER BY date DESC, odometer_km DESC, id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _as_of_date(as_of: str | date | None) -> date:
    if as_of is None:
        return datetime.now(timezone(timedelta(hours=9))).date()
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        return as_of
    return date.fromisoformat(str(as_of)[:10])


FULL_CHARGE_SOC = 99.5


def latest_full_charge_date(conn: sqlite3.Connection, vehicle_id: str) -> str | None:
    """KST date of the latest charge that ended at 99.5% or more. Charges are not written."""
    try:
        row = conn.execute(
            "SELECT end_ts FROM charges WHERE vehicle_id = ? AND end_soc >= ? "
            "AND end_ts IS NOT NULL ORDER BY end_ts DESC LIMIT 1",
            (vehicle_id, FULL_CHARGE_SOC),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    text = str(row[0]).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=9))).date().isoformat()


def item_status_row(
    conn: sqlite3.Connection,
    vehicle_id: str,
    item: sqlite3.Row | dict[str, Any],
    *,
    current_odo: float | None = None,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    def g(key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item[key] if key in item else default
        try:
            return item[key]
        except (KeyError, IndexError):
            return default

    item_id = g("id")
    default_action, reset_on = item_actions(item)
    last = _latest_replacement(conn, vehicle_id, item_id, RESET_ON[reset_on])
    last_any = _latest_replacement(conn, vehicle_id, item_id)
    if last is None:
        last_date = get_registration_date(conn, vehicle_id)
        last_odo = REGISTRATION_ODOMETER_KM
    else:
        last_date = last["date"]
        last_odo = float(last["odometer_km"])
    if item_id == "cons-battery-full":
        charged = latest_full_charge_date(conn, vehicle_id)
        if charged and charged > str(last_date):
            last_date = charged
    if current_odo is None:
        current_odo = current_odometer(conn, vehicle_id)
    basis = None
    if item_id == "cons-tire-rotation" and g("rule") in ("km_only", "km_or_months_whichever_first"):
        from ledger.tiresets import rotation_basis
        basis = rotation_basis(conn, vehicle_id, current_odo)
    if basis is not None:  # : count km of the current tire set since its last rotation
        last_date = basis["last_date"]
        status = compute_item_status(
            rule="km_only",
            interval_km=g("interval_km"),
            interval_months=None,
            last_date=last_date,
            last_odo=basis["set_km_at_last"],
            current_odo=basis["set_km_now"],
            as_of=_as_of_date(as_of),
            warn_before_km=g("warn_before_km"),
            warn_before_days=g("warn_before_days"),
        )
        rem = status["remaining_km"]
        status["last_replaced_odometer_km"] = basis["last_odo"]
        status["next_due_km"] = (float(current_odo) + rem) if (rem is not None and current_odo is not None) else None
        status["basis"] = {"kind": "tire_set", "set_id": basis["set_id"], "set_label": basis["set_label"],
                           "km_since": round(basis["set_km_now"] - basis["set_km_at_last"], 1),
                           "rotated": basis["rotated"]}
    else:
        status = compute_item_status(
            rule=g("rule"),
            interval_km=g("interval_km"),
            interval_months=g("interval_months"),
            last_date=last_date,
            last_odo=last_odo,
            current_odo=current_odo,
            as_of=_as_of_date(as_of),
            warn_before_km=g("warn_before_km"),
            warn_before_days=g("warn_before_days"),
        )
    out = {
        "vehicle_id": vehicle_id,
        "id": item_id,
        "name": g("name"),
        "interval_km": g("interval_km"),
        "interval_months": g("interval_months"),
        "rule": g("rule"),
        "warn_before_km": g("warn_before_km"),
        "warn_before_days": g("warn_before_days"),
        "active": bool(g("active")),
        "note": g("note"),
        "default_action": default_action,
        "reset_on": reset_on,
        "last_action": (last["action"] if last is not None and "action" in last.keys() else None) or ("replace" if last is not None else None),
        "last_record": (
            {"date": last_any["date"], "odometer_km": last_any["odometer_km"],
             "action": (last_any["action"] if "action" in last_any.keys() else None) or "replace"}
            if last_any is not None else None
        ),
        "current_odometer_km": current_odo,
        **status,
    }
    from ledger.manuals import doc_links
    docs = doc_links(item_id)
    if docs:
        out["docs"] = docs
    if out["status"] == "unset":
        out["interval_label"] = UNSET_LABEL
    elif out["status"] == "as_needed":
        out["interval_label"] = AS_NEEDED_LABELS[default_action]
    return out


def list_items(
    conn: sqlite3.Connection,
    vehicle_id: str,
    *,
    include_inactive: bool = True,
    as_of: str | date | None = None,
) -> list[dict[str, Any]]:
    ensure_consumable_schema(conn)
    sql = "SELECT * FROM consumable_items WHERE vehicle_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY name"
    rows = conn.execute(sql, (vehicle_id,)).fetchall()
    odo = current_odometer(conn, vehicle_id)
    return [item_status_row(conn, vehicle_id, r, current_odo=odo, as_of=as_of) for r in rows]


def due_items(
    conn: sqlite3.Connection,
    vehicle_id: str,
    *,
    as_of: str | date | None = None,
) -> list[dict[str, Any]]:
    items = list_items(conn, vehicle_id, include_inactive=False, as_of=as_of)
    # as_needed stays off the alert list even when a saved interval makes the row 정상/점검 필요.
    return [i for i in items if i["status"] in ("soon", "overdue") and i.get("rule") != "as_needed"]


def create_item(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_consumable_schema(conn)
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    item_id = str(body.get("id") or new_id("cons"))
    rule = require_rule(body.get("rule") or "km_or_months_whichever_first")
    interval_km = require_non_negative(body.get("interval_km"), "interval_km")
    interval_months = require_non_negative(body.get("interval_months"), "interval_months")
    warn_km = require_non_negative(
        body["warn_before_km"] if "warn_before_km" in body else DEFAULT_WARN_KM,
        "warn_before_km",
    )
    warn_days = require_non_negative(
        body["warn_before_days"] if "warn_before_days" in body else DEFAULT_WARN_DAYS,
        "warn_before_days",
    )
    active = 0 if body.get("active") in (False, 0, "0") else 1
    note = body.get("note")
    default_action = require_action(body.get("default_action") or "replace", "default_action")
    reset_on = require_action(body.get("reset_on") or default_action, "reset_on")
    with conn:
        conn.execute(
            """
            INSERT INTO consumable_items (
                vehicle_id, id, name, interval_km, interval_months, rule,
                warn_before_km, warn_before_days, active, note, default_action, reset_on
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vehicle_id,
                item_id,
                name,
                interval_km,
                interval_months,
                rule,
                warn_km,
                warn_days,
                active,
                note,
                default_action,
                reset_on,
            ),
        )
    row = conn.execute(
        "SELECT * FROM consumable_items WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, item_id),
    ).fetchone()
    return item_status_row(conn, vehicle_id, row)


def update_item(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_consumable_schema(conn)
    item_id = body.get("id")
    if not item_id:
        raise ValueError("id is required")
    existing = conn.execute(
        "SELECT * FROM consumable_items WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, item_id),
    ).fetchone()
    if existing is None:
        raise KeyError("item not found")
    name = body["name"] if "name" in body else existing["name"]
    if not str(name).strip():
        raise ValueError("name is required")
    rule = require_rule(body["rule"] if "rule" in body else existing["rule"])
    interval_km = require_non_negative(
        body["interval_km"] if "interval_km" in body else existing["interval_km"],
        "interval_km",
    )
    interval_months = require_non_negative(
        body["interval_months"] if "interval_months" in body else existing["interval_months"],
        "interval_months",
    )
    warn_km = require_non_negative(
        body["warn_before_km"] if "warn_before_km" in body else existing["warn_before_km"],
        "warn_before_km",
    )
    warn_days = require_non_negative(
        body["warn_before_days"] if "warn_before_days" in body else existing["warn_before_days"],
        "warn_before_days",
    )
    if "active" in body:
        active = 0 if body.get("active") in (False, 0, "0") else 1
    else:
        active = existing["active"]
    note = body["note"] if "note" in body else existing["note"] if "note" in existing.keys() else None
    cur_da, cur_ro = item_actions(existing)
    default_action = require_action(body["default_action"], "default_action") if "default_action" in body else cur_da
    reset_on = require_action(body["reset_on"], "reset_on") if "reset_on" in body else cur_ro
    with conn:
        conn.execute(
            """
            UPDATE consumable_items SET
                name = ?, interval_km = ?, interval_months = ?, rule = ?,
                warn_before_km = ?, warn_before_days = ?, active = ?, note = ?,
                default_action = ?, reset_on = ?
            WHERE vehicle_id = ? AND id = ?
            """,
            (
                str(name).strip(),
                interval_km,
                interval_months,
                rule,
                warn_km,
                warn_days,
                active,
                note,
                default_action,
                reset_on,
                vehicle_id,
                item_id,
            ),
        )
    row = conn.execute(
        "SELECT * FROM consumable_items WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, item_id),
    ).fetchone()
    return item_status_row(conn, vehicle_id, row)


def deactivate_item(conn: sqlite3.Connection, vehicle_id: str, item_id: str) -> dict[str, Any]:
    return update_item(conn, vehicle_id, {"id": item_id, "active": False})


def list_vendors(conn: sqlite3.Connection, vehicle_id: str) -> list[dict[str, Any]]:
    ensure_consumable_schema(conn)
    rows = conn.execute(
        "SELECT * FROM vendors WHERE vehicle_id = ? ORDER BY name",
        (vehicle_id,),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _ensure_vendor(conn: sqlite3.Connection, vehicle_id: str, name: str | None) -> None:
    if not name or not str(name).strip():
        return
    name = str(name).strip()
    existing = conn.execute(
        "SELECT id FROM vendors WHERE vehicle_id = ? AND name = ?",
        (vehicle_id, name),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO vendors (vehicle_id, id, name) VALUES (?, ?, ?)",
        (vehicle_id, new_id("vnd"), name),
    )


def _replacement_amount(body: dict[str, Any], price: float, quantity: float) -> float:
    if body.get("total_krw") not in (None, ""):
        total = require_non_negative(body.get("total_krw"), "total_krw")
        if total is None:
            raise ValueError("total_krw must be a number")
        return total
    return price * quantity


def _needs_ledger(action: str, amount: float) -> bool:
    """교체 always writes a 차계부 row; 점검·작업 only when they cost something."""
    return action == "replace" or (amount or 0) > 0


def _insert_ledger(conn, vehicle_id, lid, day, category, amount, odo, memo, rid) -> None:
    conn.execute(
        """
        INSERT INTO ledger (
            vehicle_id, id, date, category, amount_krw, odometer_km,
            memo, source, replacement_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)
        """,
        (vehicle_id, lid, day, category, amount, odo, memo, rid),
    )


def _ledger_memo(item_name: str, vendor: str | None, extra: str | None, action: str = "replace") -> str:
    parts = [item_name if action == "replace" else due_label(item_name, action) if action == "inspect" else f"{item_name} {ACTION_KO[action]}"]
    if vendor:
        parts.append(str(vendor))
    if extra:
        parts.append(str(extra))
    return " · ".join(parts)


def _item_or_raise(conn: sqlite3.Connection, vehicle_id: str, item_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM consumable_items WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, item_id),
    ).fetchone()
    if row is None:
        raise KeyError("item not found")
    return row


def save_replacement(
    conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Insert replacement + ledger + vendor in one transaction."""
    ensure_consumable_schema(conn)
    item_id = body.get("item_id")
    if not item_id:
        raise ValueError("item_id is required")
    item = _item_or_raise(conn, vehicle_id, str(item_id))
    day = parse_date(str(body.get("date") or ""))
    odo = require_non_negative(body.get("odometer_km"), "odometer_km")
    if odo is None:
        raise ValueError("odometer_km is required")
    action = require_action(body.get("action") or item_actions(item)[0])
    raw_price = body.get("price_krw")
    if action != "replace" and raw_price in (None, ""):
        raw_price = 0  # 점검·작업: price is optional
    price = require_non_negative(raw_price, "price_krw")
    if price is None:
        raise ValueError("price_krw is required")
    qty = require_non_negative(body.get("quantity"), "quantity")
    if qty is None:
        qty = 1.0
    amount = _replacement_amount(body, price, qty)
    vendor = (body.get("vendor") or "") or None
    if vendor:
        vendor = str(vendor).strip() or None
    memo = body.get("memo")
    rid = str(body.get("id") or new_id("rep"))
    need_ledger = _needs_ledger(action, amount)
    lid = new_id("led") if need_ledger else None
    category = ledger_category_for_item(item["name"])
    ledger_memo = _ledger_memo(item["name"], vendor, memo, action)
    with conn:
        conn.execute(
            """
            INSERT INTO replacements (
                vehicle_id, id, item_id, date, odometer_km, price_krw,
                vendor, quantity, memo, receipt_path, ledger_id, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (vehicle_id, rid, item_id, day, odo, price, vendor, qty, memo, lid, action),
        )
        if need_ledger:
            _insert_ledger(conn, vehicle_id, lid, day, category, amount, odo, ledger_memo, rid)
        for extra in body.get("also") or []:
            extra_id = str(extra.get("item_id") or "")
            if not extra_id or extra_id == item_id:
                continue
            extra_item = _item_or_raise(conn, vehicle_id, extra_id)
            extra_action = require_action(extra.get("action") or "inspect")
            extra_rid = new_id("rep")
            extra_memo = "함께 기록"
            conn.execute(
                """
                INSERT INTO replacements (
                    vehicle_id, id, item_id, date, odometer_km, price_krw,
                    vendor, quantity, memo, receipt_path, ledger_id, action
                ) VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?, NULL, NULL, ?)
                """,
                (vehicle_id, extra_rid, extra_id, day, odo, vendor, extra_memo, extra_action),
            )
            del extra_item
        _ensure_vendor(conn, vehicle_id, vendor)
    return get_replacement(conn, vehicle_id, rid)


def update_replacement(
    conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    ensure_consumable_schema(conn)
    rid = body.get("id")
    if not rid:
        raise ValueError("id is required")
    existing = conn.execute(
        "SELECT * FROM replacements WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, rid),
    ).fetchone()
    if existing is None:
        raise KeyError("replacement not found")
    item_id = body["item_id"] if "item_id" in body else existing["item_id"]
    item = _item_or_raise(conn, vehicle_id, str(item_id))
    day = parse_date(str(body.get("date") or existing["date"]))
    odo = require_non_negative(
        body["odometer_km"] if "odometer_km" in body else existing["odometer_km"],
        "odometer_km",
    )
    if odo is None:
        raise ValueError("odometer_km is required")
    old_action = (existing["action"] if "action" in existing.keys() else None) or "replace"
    action = require_action(body["action"]) if body.get("action") else old_action
    raw_price = body["price_krw"] if "price_krw" in body else existing["price_krw"]
    if action != "replace" and raw_price in (None, ""):
        raw_price = 0
    price = require_non_negative(raw_price, "price_krw")
    if price is None:
        raise ValueError("price_krw is required")
    qty = require_non_negative(
        body["quantity"] if "quantity" in body else existing["quantity"],
        "quantity",
    )
    if qty is None:
        qty = 1.0
    amount = _replacement_amount(body, price, qty)
    vendor = body["vendor"] if "vendor" in body else existing["vendor"]
    if vendor:
        vendor = str(vendor).strip() or None
    memo = body["memo"] if "memo" in body else existing["memo"]
    lid = existing["ledger_id"]
    category = ledger_category_for_item(item["name"])
    ledger_memo = _ledger_memo(item["name"], vendor, memo, action)
    need_ledger = _needs_ledger(action, amount)
    with conn:
        if lid and not need_ledger:  # 점검·작업 now free: drop its 차계부 row
            conn.execute("DELETE FROM ledger WHERE vehicle_id = ? AND id = ?", (vehicle_id, lid))
            lid = None
        elif need_ledger and not lid:
            lid = new_id("led")
            _insert_ledger(conn, vehicle_id, lid, day, category, amount, odo, ledger_memo, rid)
        conn.execute(
            """
            UPDATE replacements SET
                item_id = ?, date = ?, odometer_km = ?, price_krw = ?,
                vendor = ?, quantity = ?, memo = ?, action = ?, ledger_id = ?
            WHERE vehicle_id = ? AND id = ?
            """,
            (item_id, day, odo, price, vendor, qty, memo, action, lid, vehicle_id, rid),
        )
        if lid:
            conn.execute(
                """
                UPDATE ledger SET
                    date = ?, category = ?, amount_krw = ?, odometer_km = ?,
                    memo = ?, replacement_id = ?
                WHERE vehicle_id = ? AND id = ?
                """,
                (day, category, amount, odo, ledger_memo, rid, vehicle_id, lid),
            )
        _ensure_vendor(conn, vehicle_id, vendor)
    return get_replacement(conn, vehicle_id, str(rid))


def delete_replacement(conn: sqlite3.Connection, vehicle_id: str, rid: str) -> None:
    ensure_consumable_schema(conn)
    existing = conn.execute(
        "SELECT ledger_id FROM replacements WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, rid),
    ).fetchone()
    if existing is None:
        raise KeyError("replacement not found")
    with conn:
        if existing["ledger_id"]:
            conn.execute(
                "DELETE FROM ledger WHERE vehicle_id = ? AND id = ?",
                (vehicle_id, existing["ledger_id"]),
            )
        conn.execute(
            "DELETE FROM replacements WHERE vehicle_id = ? AND id = ?",
            (vehicle_id, rid),
        )


def get_replacement(conn: sqlite3.Connection, vehicle_id: str, rid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM replacements WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, rid),
    ).fetchone()
    if row is None:
        raise KeyError("replacement not found")
    return {k: row[k] for k in row.keys()}


def list_replacements(
    conn: sqlite3.Connection,
    vehicle_id: str,
    item_id: str | None = None,
) -> dict[str, Any]:
    ensure_consumable_schema(conn)
    sql = "SELECT * FROM replacements WHERE vehicle_id = ?"
    params: list[Any] = [vehicle_id]
    if item_id:
        sql += " AND item_id = ?"
        params.append(item_id)
    sql += " ORDER BY date DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    items = [{k: r[k] for k in r.keys()} for r in rows]
    total = 0.0
    by_vendor: dict[str, float] = {}
    by_item: dict[str, float] = {}
    for r in items:
        qty = float(r["quantity"] if r["quantity"] is not None else 1)
        spent = float(r["price_krw"]) * qty
        # Prefer linked ledger amount when present (covers owner-entered total).
        if r.get("ledger_id"):
            led = conn.execute(
                "SELECT amount_krw FROM ledger WHERE vehicle_id = ? AND id = ?",
                (vehicle_id, r["ledger_id"]),
            ).fetchone()
            if led is not None and led["amount_krw"] is not None:
                spent = float(led["amount_krw"])
        total += spent
        vname = r["vendor"] or "(없음)"
        by_vendor[vname] = by_vendor.get(vname, 0.0) + spent
        iid = r["item_id"]
        by_item[iid] = by_item.get(iid, 0.0) + spent
    return {
        "vehicle_id": vehicle_id,
        "item_id": item_id,
        "replacements": items,
        "total_krw": total,
        "by_vendor": [
            {"vendor": k, "total_krw": v} for k, v in sorted(by_vendor.items())
        ],
        "by_item": [{"item_id": k, "total_krw": v} for k, v in sorted(by_item.items())],
    }
