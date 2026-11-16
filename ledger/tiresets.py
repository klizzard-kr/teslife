"""Tire sets: register sets, mount/remove them, tread records, per-set km / age / alerts.

A set is one group of four tires (여름용 / 겨울용 / 사계절). Mount records say when a set went on
and came off the car (date + odometer); the open mount is the current set. Per-set km is the sum
of the odometer spans while mounted, so a stored winter set does not age in km. Rotation for the
current set counts km since the later of a real rotation record and a seasonal swap. A swap is
assumed, not written as a replacement or a ledger row. DOT stays on the set and is not its own alert.

Replacement guidance (shown in the UI 'i' tooltip):
- tread: 3 mm 점검·교체 권장 (한국타이어), 1.6 mm 법정 한계, 겨울용 4 mm (Continental).
- age from the DOT week/year: NHTSA says some makers recommend replacing at 6-10 years;
  Michelin: yearly inspection after 5 years, replace at 10 years from manufacture.
- maker treadwear warranty km: notice at 90 %.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ledger import new_id, parse_date, require_non_negative

KINDS = ("summer", "winter", "all_season")
KIND_KO = {"summer": "여름용", "winter": "겨울용", "all_season": "사계절"}
ROTATION_ITEM = "cons-tire-rotation"
SEASON_ITEM = "cons-tires-season"
DEFAULT_ROTATION_KM = 10000.0
TREAD_LEGAL_MM = 1.6
TREAD_CHECK_MM = 3.0
TREAD_WINTER_MM = 4.0
AGE_CAUTION_Y = 6
AGE_REPLACE_Y = 10
WARRANTY_RATIO = 0.9
MIGRATED_KEY = "tireset_migrated"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tire_sets (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    size TEXT,
    load_speed TEXT,
    dot TEXT,
    purchase_date TEXT,
    price_krw REAL,
    vendor TEXT,
    warranty_km REAL,
    new_tread_mm REAL,
    rec_front_bar REAL,  -- unused since  (fixed 42 psi)
    rec_rear_bar REAL,
    memo TEXT,
    retired INTEGER NOT NULL DEFAULT 0,
    ledger_id TEXT,
    created_at TEXT,
    PRIMARY KEY (vehicle_id, id)
);
CREATE TABLE IF NOT EXISTS tire_mounts (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    set_id TEXT NOT NULL,
    mounted_date TEXT NOT NULL,
    mounted_odo REAL NOT NULL,
    removed_date TEXT,
    removed_odo REAL,
    price_krw REAL,
    memo TEXT,
    ledger_id TEXT,
    PRIMARY KEY (vehicle_id, id)
);
CREATE TABLE IF NOT EXISTS tire_treads (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    set_id TEXT NOT NULL,
    date TEXT NOT NULL,
    odometer_km REAL,
    fl REAL, fr REAL, rl REAL, rr REAL,
    memo TEXT,
    PRIMARY KEY (vehicle_id, id)
);
"""


def ensure_tireset_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _today() -> date:
    return datetime.now(timezone(timedelta(hours=9))).date()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clean(v: Any) -> str | None:
    s = (str(v).strip() if v is not None else "")
    return s or None


# ---- DOT ----------------------------------------------------------------------------------

def parse_dot(text: str | None) -> list[date]:
    """Production weeks from DOT codes. Each code's last 4 digits are WWYY (e.g. 2525 = week 25 of 2025)."""
    out = []
    for code in re.split(r"[,/;]+", text or ""):
        toks = code.split()
        if not toks:
            continue
        digits = re.sub(r"\D", "", toks[-1])  # a full TIN ends with the WWYY group
        if len(digits) < 4:
            continue
        ww, yy = int(digits[-4:-2]), int(digits[-2:])
        if not 1 <= ww <= 53:
            continue
        year = 2000 + yy
        try:
            out.append(date.fromisocalendar(year, ww, 1))
        except ValueError:  # week 53 in a 52-week year
            out.append(date.fromisocalendar(year, 52, 1))
    return out


def validate_dot(text: Any) -> str | None:
    t = _clean(text)
    if t is None:
        return None
    if not parse_dot(t):
        raise ValueError("DOT는 생산 주차·연도 네 자리(예: 2525)로 적어 주세요")
    if max(parse_dot(t)) > _today() + timedelta(days=60):
        raise ValueError("DOT 생산 시기가 미래예요. 주차 두 자리 + 연도 두 자리(예: 2525)예요")
    return t


# ---- km / mounts ---------------------------------------------------------------------------

def _mounts(conn, vid, set_id=None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tire_mounts WHERE vehicle_id = ?"
    params: tuple = (vid,)
    if set_id:
        sql += " AND set_id = ?"
        params = (vid, set_id)
    return _rows(conn, sql + " ORDER BY mounted_odo, mounted_date, id", params)


def current_mount(conn, vid) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM tire_mounts WHERE vehicle_id = ? AND removed_date IS NULL "
                       "ORDER BY mounted_date DESC, mounted_odo DESC LIMIT 1", (vid,))
    return rows[0] if rows else None


def get_set(conn, vid, set_id) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM tire_sets WHERE vehicle_id = ? AND id = ?", (vid, set_id))
    return rows[0] if rows else None


def current_set(conn, vid) -> dict[str, Any] | None:
    m = current_mount(conn, vid)
    return get_set(conn, vid, m["set_id"]) if m else None


def set_km_at(mounts: list[dict[str, Any]], odo: float) -> float:
    """km this set had run when the car's odometer read `odo`."""
    total = 0.0
    for m in mounts:
        start = float(m["mounted_odo"])
        end = m["removed_odo"]
        end = odo if end is None else min(float(end), odo)
        if end > start:
            total += end - start
    return total


KIND_EN = {"summer": "Summer", "winter": "Winter", "all_season": "All-season"}


def kind_name(kind: str, lang: str = "ko") -> str:
    if lang == "en":
        return KIND_EN.get(kind, kind)
    return KIND_KO.get(kind, kind)


def set_label(s: dict[str, Any], lang: str = "ko") -> str:
    name = " ".join(x for x in (s.get("brand"), s.get("model")) if x)
    return " · ".join(x for x in (kind_name(s["kind"], lang), name) if x)


def _rotation_records(conn, vid) -> list[dict[str, Any]]:
    from ledger.consumables import RESET_ON, item_actions
    items = _rows(conn, "SELECT * FROM consumable_items WHERE vehicle_id = ? AND id = ?", (vid, ROTATION_ITEM))
    reset = RESET_ON[item_actions(items[0])[1]] if items else ("service", "replace")
    recs = _rows(conn, "SELECT * FROM replacements WHERE vehicle_id = ? AND item_id = ? "
                       "ORDER BY odometer_km, date", (vid, ROTATION_ITEM))
    return [r for r in recs if (r.get("action") or "replace") in reset]


def _in_mounts(mounts, odo: float) -> bool:
    for m in mounts:
        end = m["removed_odo"]
        if float(m["mounted_odo"]) <= odo and (end is None or odo <= float(end)):
            return True
    return False


def _season_swap(conn, vid, open_m) -> bool:
    """True when this mount took over from a different set at the same date and odometer."""
    if not open_m:
        return False
    rows = _rows(
        conn,
        "SELECT 1 AS ok FROM tire_mounts WHERE vehicle_id = ? AND set_id != ? "
        "AND removed_date = ? AND removed_odo IS NOT NULL AND ABS(removed_odo - ?) < 0.05 LIMIT 1",
        (vid, open_m["set_id"], open_m["mounted_date"], float(open_m["mounted_odo"])),
    )
    return bool(rows)


def rotation_baseline(conn, vid, mounts) -> dict[str, Any]:
    """Latest rotation point for status. Does not insert or edit a record.

    The open mount counts when another set came off there (a seasonal swap).
    A real rotation record at or after that point still wins. Older records stay as stored.
    The first mount of a set is not a swap.
    """
    open_m = next((m for m in mounts if m["removed_date"] is None), None)
    odo = None
    day = None
    source = None
    if open_m and _season_swap(conn, vid, open_m):
        odo = float(open_m["mounted_odo"])
        day = open_m["mounted_date"]
        source = "season"
    for r in _rotation_records(conn, vid):
        rodo = float(r["odometer_km"])
        if not mounts or not _in_mounts(mounts, rodo):
            continue
        if odo is not None and rodo + 1e-6 < odo:
            continue
        odo = rodo
        day = r["date"]
        source = "record"
    return {"odo": odo, "date": day, "source": source}


def _rotation_warn_km(conn, vid) -> float:
    rows = _rows(conn, "SELECT warn_before_km FROM consumable_items WHERE vehicle_id = ? AND id = ?",
                 (vid, ROTATION_ITEM))
    v = rows[0]["warn_before_km"] if rows else None
    return float(v) if v else 500.0


def rotation_interval(conn, vid) -> float:
    rows = _rows(conn, "SELECT interval_km FROM consumable_items WHERE vehicle_id = ? AND id = ?",
                 (vid, ROTATION_ITEM))
    v = rows[0]["interval_km"] if rows else None
    return float(v) if v else DEFAULT_ROTATION_KM


def _latest_tread(conn, vid, set_id) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM tire_treads WHERE vehicle_id = ? AND set_id = ? "
                       "ORDER BY date DESC, id DESC LIMIT 1", (vid, set_id))
    return rows[0] if rows else None


def set_status(conn, vid, s: dict[str, Any], current_odo: float | None,
               today: date | None = None, lang: str = "ko") -> dict[str, Any]:
    today = today or _today()
    mounts = _mounts(conn, vid, s["id"])
    open_m = next((m for m in mounts if m["removed_date"] is None), None)
    last_odo = max([float(m["removed_odo"]) for m in mounts if m["removed_odo"] is not None]
                   + [float(m["mounted_odo"]) for m in mounts], default=0.0)
    odo_now = current_odo if current_odo is not None and current_odo >= last_odo else last_odo
    km_total = set_km_at(mounts, odo_now)
    days = 0
    for m in mounts:
        d0 = date.fromisoformat(m["mounted_date"])
        d1 = date.fromisoformat(m["removed_date"]) if m["removed_date"] else today
        days += max(0, (d1 - d0).days)
    # rotation: km of this set since a real record, or since the seasonal swap that mounted it
    base = rotation_baseline(conn, vid, mounts)
    km_at_rot = set_km_at(mounts, float(base["odo"])) if base["odo"] is not None else 0.0
    since_rot = max(0.0, km_total - km_at_rot)
    interval = rotation_interval(conn, vid)
    tread = _latest_tread(conn, vid, s["id"])
    tread_min = None
    if tread:
        vals = [tread[w] for w in ("fl", "fr", "rl", "rr") if tread[w] is not None]
        tread_min = min(vals) if vals else None
    made = parse_dot(s.get("dot"))
    oldest = min(made) if made else None
    age_years = ((today - oldest).days / 365.25) if oldest else None
    en = lang == "en"
    alerts = []
    if tread_min is not None:
        if tread_min <= TREAD_LEGAL_MM + 1e-9:
            text = (f"Tread about {tread_min:.1f} mm · at the 1.6 mm legal limit, replace now" if en else
                    f"홈 깊이 약 {tread_min:.1f} mm · 법정 한계(1.6 mm)라 바로 교체")
            alerts.append({"code": "tread", "level": "overdue", "text": text})
        elif s["kind"] == "winter" and tread_min <= TREAD_WINTER_MM + 1e-9:
            text = (f"Tread about {tread_min:.1f} mm · winter tires lose snow grip from 4 mm" if en else
                    f"홈 깊이 약 {tread_min:.1f} mm · 겨울용은 4 mm부터 눈길 성능이 떨어져요")
            alerts.append({"code": "tread", "level": "soon", "text": text})
        elif tread_min <= TREAD_CHECK_MM + 1e-9:
            text = (f"Tread about {tread_min:.1f} mm · 3 mm or less, plan a replacement" if en else
                    f"홈 깊이 약 {tread_min:.1f} mm · 3 mm 이하라 교체를 준비할 때예요")
            alerts.append({"code": "tread", "level": "soon", "text": text})
    wk = s.get("warranty_km")
    if wk and km_total >= WARRANTY_RATIO * float(wk):
        pct = km_total / float(wk) * 100
        text = (f"About {km_total:,.0f} km · about {pct:.0f}% of the {float(wk):,.0f} km treadwear warranty" if en else
                f"주행 약 {km_total:,.0f} km · 마모 보증 {float(wk):,.0f} km의 약 {pct:.0f}%")
        alerts.append({"code": "warranty", "level": "soon", "text": text})
    # : per-item status for the web (same words as the consumables list); unknown items are left out
    lv = {x["code"]: x["level"] for x in alerts}
    checks = []
    if tread_min is not None:
        checks.append({"code": "tread", "name": "Tread" if en else "홈 깊이", "status": lv.get("tread", "ok")})
    if wk:
        checks.append({"code": "warranty", "name": "Treadwear warranty" if en else "마모 보증",
                       "status": lv.get("warranty", "ok")})
    rot_status = None
    if open_m:
        warn = _rotation_warn_km(conn, vid)
        if since_rot + 1e-9 >= interval:
            rot_status = "overdue"
        elif interval - since_rot <= warn + 1e-9:
            rot_status = "soon"
        else:
            rot_status = "ok"
    return {
        **s,
        "kind_ko": kind_name(s["kind"], lang),
        "label": set_label(s, lang),
        "mounted": open_m is not None,
        "mounted_since": ({"date": open_m["mounted_date"], "odometer_km": open_m["mounted_odo"]}
                          if open_m else None),
        "km_total": round(km_total, 1),
        "days_total": days,
        "months_total": round(days / 30.44, 1),
        "rotation": {
            "last_date": base["date"],
            "last_odo": base["odo"],
            "km_since": round(since_rot, 1),
            "interval_km": interval,
            "source": base["source"],
            "status": rot_status,
        },
        "tread": ({"date": tread["date"], "min_mm": tread_min,
                   "values": {w: tread[w] for w in ("fl", "fr", "rl", "rr")}} if tread else None),
        "made": oldest.isoformat() if oldest else None,
        "age_years": round(age_years, 1) if age_years is not None else None,
        "alerts": alerts,
        "checks": checks,
        "mounts": [{"id": m["id"], "mounted_date": m["mounted_date"], "mounted_odo": m["mounted_odo"],
                    "removed_date": m["removed_date"], "removed_odo": m["removed_odo"]} for m in mounts],
    }


def next_season_due(
    today: date,
    mounts: list[tuple[str, date]],
    *,
    has_seasonal: bool,
    not_before: date | None = None,
) -> date | None:
    """Next summer (March 1) or winter (December 1) swap that is not already mounted.

    The year is not stored. A real mount of that season after the previous anchor
    and before the following one completes the anchor, including an early swap.
    Buying or editing a set does not count: only mount dates do. Anchors before
    `not_before` (the registration day, when there is one) are skipped.
    """
    if not has_seasonal:
        return None
    floor = not_before or (today - timedelta(days=366))
    cands: list[tuple[date, str]] = []
    for year in range(floor.year - 1, today.year + 3):
        for month, day, kind in ((3, 1, "summer"), (12, 1, "winter")):
            anchor = date(year, month, day)
            if floor <= anchor <= today + timedelta(days=400):
                cands.append((anchor, kind))
    cands.sort()
    for i, (anchor, kind) in enumerate(cands):
        prev = cands[i - 1][0] if i else floor - timedelta(days=1)
        nxt = cands[i + 1][0] if i + 1 < len(cands) else anchor + timedelta(days=200)
        if any(mk == kind and prev < md <= nxt for mk, md in mounts):
            continue
        return anchor
    return None


def season_due(conn, vid, today: date | None = None) -> str | None:
    """ISO date of the next seasonal tire swap, or None when no summer/winter set exists."""
    today = today or _today()
    sets = _rows(conn, "SELECT id, kind, retired FROM tire_sets WHERE vehicle_id = ?", (vid,))
    if not any(s["kind"] in ("summer", "winter") and not s["retired"] for s in sets):
        return None
    mounts: list[tuple[str, date]] = []
    for s in sets:
        if s["kind"] not in ("summer", "winter"):
            continue
        for m in _mounts(conn, vid, s["id"]):
            mounts.append((s["kind"], date.fromisoformat(m["mounted_date"])))
    from ledger.consumables import get_registration_date
    raw = get_registration_date(conn, vid)
    floor = date.fromisoformat(raw) if raw else None
    due = next_season_due(today, mounts, has_seasonal=True, not_before=floor)
    return due.isoformat() if due else None


def list_sets(conn, vid, current_odo: float | None = None, today: date | None = None,
              lang: str = "ko") -> list[dict[str, Any]]:
    if current_odo is None:
        from ledger.consumables import current_odometer
        current_odo = current_odometer(conn, vid)
    sets = _rows(conn, "SELECT * FROM tire_sets WHERE vehicle_id = ? ORDER BY retired, created_at, id", (vid,))
    out = [set_status(conn, vid, s, current_odo, today, lang=lang) for s in sets]
    out.sort(key=lambda x: (not x["mounted"], x["retired"], x.get("created_at") or ""))
    return out


def rotation_basis(conn, vid, current_odo: float | None) -> dict[str, Any] | None:
    """For the rotation item: km of the current set at its last rotation and now."""
    m = current_mount(conn, vid)
    if not m:
        return None
    s = get_set(conn, vid, m["set_id"])
    if not s:
        return None
    st = set_status(conn, vid, s, current_odo)
    rot = st["rotation"]
    return {
        "set_id": s["id"],
        "set_label": st["label"],
        "set_km_now": st["km_total"],
        "set_km_at_last": round(st["km_total"] - rot["km_since"], 1),
        "last_date": rot["last_date"] or m["mounted_date"],
        "last_odo": float(rot["last_odo"]) if rot["last_odo"] is not None else float(m["mounted_odo"]),
        "rotated": rot["source"] == "record",
    }


# ---- writes --------------------------------------------------------------------------------

def _ledger(conn, vid, day, amount, odo, memo, ref) -> str | None:
    """Tire prices stay on the tire set. They do not create 차계부 rows."""
    return None


def _sync_ledger(conn, vid, lid, day, amount, odo, memo, ref) -> str | None:
    """Leave any existing 차계부 row byte-identical. Price edits do not write the ledger."""
    return lid


SET_FIELDS = ("kind", "brand", "model", "size", "load_speed", "dot", "purchase_date", "price_krw",
              "vendor", "warranty_km", "new_tread_mm", "memo")


def _set_values(body: dict[str, Any], cur: dict[str, Any] | None = None) -> dict[str, Any]:
    v = dict(cur or {})
    for k in SET_FIELDS:
        if k not in body:
            continue
        val = body[k]
        if k == "kind":
            if val not in KINDS:
                raise ValueError("kind must be summer, winter or all_season")
            v[k] = val
        elif k in ("price_krw", "warranty_km", "new_tread_mm"):
            v[k] = require_non_negative(val, k)
        elif k == "purchase_date":
            v[k] = parse_date(val) if _clean(val) else None
        elif k == "dot":
            v[k] = validate_dot(val)
        else:
            v[k] = _clean(val)
    if v.get("new_tread_mm") is not None and v["new_tread_mm"] > 20:
        raise ValueError("new_tread_mm looks wrong (mm)")
    v.setdefault("kind", "summer")
    return v


def _purchase_memo(v: dict[str, Any]) -> str:
    name = " ".join(x for x in (v.get("brand"), v.get("model")) if x)
    return " · ".join(x for x in ("타이어 구매", KIND_KO[v["kind"]], name, v.get("vendor")) if x)


def create_set(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    v = _set_values(body)
    sid = new_id("tset")
    from ledger.consumables import current_odometer
    day = v.get("purchase_date") or _today().isoformat()
    with conn:
        lid = _ledger(conn, vid, day, v.get("price_krw"), None, _purchase_memo(v), sid)
        conn.execute(
            "INSERT INTO tire_sets (vehicle_id, id, kind, brand, model, size, load_speed, dot, purchase_date, "
            "price_krw, vendor, warranty_km, new_tread_mm, memo, ledger_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, sid, v["kind"], v.get("brand"), v.get("model"), v.get("size"), v.get("load_speed"),
             v.get("dot"), v.get("purchase_date"), v.get("price_krw"), v.get("vendor"), v.get("warranty_km"),
             v.get("new_tread_mm"), v.get("memo"), lid, datetime.now(timezone.utc).isoformat()),
        )
    mount = body.get("mount")
    if isinstance(mount, dict):
        mount_set(conn, vid, {**mount, "set_id": sid})
    return set_status(conn, vid, get_set(conn, vid, sid), current_odometer(conn, vid))


def update_set(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    sid = body.get("id")
    cur = get_set(conn, vid, sid) if sid else None
    if not cur:
        raise KeyError("tire set not found")
    v = _set_values(body, cur)
    if "retired" in body:
        v["retired"] = 1 if body["retired"] in (True, 1, "1") else 0
    day = v.get("purchase_date") or (cur.get("created_at") or _today().isoformat())[:10]
    with conn:
        lid = _sync_ledger(conn, vid, cur.get("ledger_id"), day, v.get("price_krw"), None, _purchase_memo(v), sid)
        conn.execute(
            "UPDATE tire_sets SET kind = ?, brand = ?, model = ?, size = ?, load_speed = ?, dot = ?, "
            "purchase_date = ?, price_krw = ?, vendor = ?, warranty_km = ?, new_tread_mm = ?, memo = ?, "
            "retired = ?, ledger_id = ? WHERE vehicle_id = ? AND id = ?",
            (v["kind"], v.get("brand"), v.get("model"), v.get("size"), v.get("load_speed"), v.get("dot"),
             v.get("purchase_date"), v.get("price_krw"), v.get("vendor"), v.get("warranty_km"),
             v.get("new_tread_mm"), v.get("memo"), v.get("retired", 0), lid, vid, sid),
        )
    from ledger.consumables import current_odometer
    return set_status(conn, vid, get_set(conn, vid, sid), current_odometer(conn, vid))


def delete_set(conn, vid, sid: str) -> None:
    s = get_set(conn, vid, sid)
    if not s:
        raise KeyError("tire set not found")
    with conn:
        for t in ("tire_mounts", "tire_treads"):
            conn.execute(f"DELETE FROM {t} WHERE vehicle_id = ? AND set_id = ?", (vid, sid))
        conn.execute("DELETE FROM tire_sets WHERE vehicle_id = ? AND id = ?", (vid, sid))


def _day_odo(body: dict[str, Any]) -> tuple[str, float]:
    day = parse_date(body.get("date") or _today().isoformat())
    odo = require_non_negative(body.get("odometer_km"), "odometer_km")
    if odo is None:
        raise ValueError("odometer_km is required")
    return day, odo


def _span_hit(start, end, other_start, other_end) -> bool:
    """Half-open intervals. A missing end is still open."""
    if isinstance(start, str):
        end_v = end if end is not None else "\uffff"
        other_end_v = other_end if other_end is not None else "\uffff"
    else:
        end_v = end if end is not None else float("inf")
        other_end_v = other_end if other_end is not None else float("inf")
    return start < other_end_v and other_start < end_v


def _reject_overlap(conn, vid, day, odo, end_day, end_odo, ignore_id: str | None = None) -> None:
    """Block a mount that crosses any other interval, including one still on the car."""
    for m in _mounts(conn, vid):
        if ignore_id and m["id"] == ignore_id:
            continue
        removed_odo = float(m["removed_odo"]) if m["removed_odo"] is not None else None
        if _span_hit(float(m["mounted_odo"]), removed_odo, float(odo), end_odo):
            raise ValueError("이 주행거리에는 이미 다른 세트가 장착돼 있었어요")
        if _span_hit(m["mounted_date"], m["removed_date"], day, end_day):
            raise ValueError("이 날짜에는 이미 다른 세트가 장착돼 있었어요")


def update_mount(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    """Edit one mount record's date and odometer. The 차계부 is not touched."""
    mid = body.get("id")
    cur = next((m for m in _mounts(conn, vid) if m["id"] == mid), None)
    if not cur:
        raise KeyError("mount not found")
    day = parse_date(body.get("mounted_date") or cur["mounted_date"])
    odo = require_non_negative(
        body["mounted_odo"] if "mounted_odo" in body else cur["mounted_odo"], "mounted_odo",
    )
    if odo is None:
        raise ValueError("mounted_odo is required")
    end_day = cur["removed_date"]
    end_odo = float(cur["removed_odo"]) if cur["removed_odo"] is not None else None
    if "removed_date" in body:
        end_day = parse_date(body["removed_date"]) if _clean(body.get("removed_date")) else None
    if "removed_odo" in body:
        raw = body.get("removed_odo")
        end_odo = require_non_negative(raw, "removed_odo") if _clean(raw) else None
    _reject_overlap(conn, vid, day, odo, end_day, end_odo, ignore_id=str(mid))
    with conn:
        conn.execute(
            "UPDATE tire_mounts SET mounted_date = ?, mounted_odo = ?, removed_date = ?, removed_odo = ? "
            "WHERE vehicle_id = ? AND id = ?",
            (day, odo, end_day, end_odo, vid, mid),
        )
    return {"id": mid, "mounted_date": day, "mounted_odo": odo}


def mount_set(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    """Put a set on the car. The set that was on comes off at the same date/odometer."""
    sid = body.get("set_id")
    s = get_set(conn, vid, sid) if sid else None
    if not s:
        raise KeyError("tire set not found")
    day, odo = _day_odo(body)
    cur = current_mount(conn, vid)
    if cur and cur["set_id"] == sid:
        raise ValueError("이미 장착된 세트예요")
    if cur and (odo < float(cur["mounted_odo"]) or day < cur["mounted_date"]):
        raise ValueError("장착 날짜·주행거리가 지금 세트 장착보다 앞서요")
    _reject_overlap(conn, vid, day, odo, None, None, ignore_id=(cur["id"] if cur else None))
    mid = new_id("tmnt")
    price = require_non_negative(body.get("price_krw"), "price_krw")
    memo = _clean(body.get("memo"))
    with conn:
        if cur:
            conn.execute("UPDATE tire_mounts SET removed_date = ?, removed_odo = ? WHERE vehicle_id = ? AND id = ?",
                         (day, odo, vid, cur["id"]))
        lid = _ledger(conn, vid, day, price, odo,
                      " · ".join(x for x in ("타이어 장착", set_label(s), memo) if x), mid)
        conn.execute("INSERT INTO tire_mounts (vehicle_id, id, set_id, mounted_date, mounted_odo, price_krw, memo, ledger_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (vid, mid, sid, day, odo, price, memo, lid))
    return {"mount_id": mid, "set_id": sid}


def remove_current(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    cur = current_mount(conn, vid)
    if not cur:
        raise ValueError("장착된 세트가 없어요")
    day, odo = _day_odo(body)
    if odo < float(cur["mounted_odo"]):
        raise ValueError("탈착 주행거리가 장착보다 작아요")
    with conn:
        conn.execute("UPDATE tire_mounts SET removed_date = ?, removed_odo = ? WHERE vehicle_id = ? AND id = ?",
                     (day, odo, vid, cur["id"]))
    return {"set_id": cur["set_id"]}


def add_tread(conn, vid, body: dict[str, Any]) -> dict[str, Any]:
    sid = body.get("set_id")
    if not sid or not get_set(conn, vid, sid):
        raise KeyError("tire set not found")
    day = parse_date(body.get("date") or _today().isoformat())
    odo = require_non_negative(body.get("odometer_km"), "odometer_km")
    vals = {}
    for w in ("fl", "fr", "rl", "rr"):
        v = require_non_negative(body.get(w), w)
        if v is not None and v > 20:
            raise ValueError("홈 깊이는 mm로 적어 주세요")
        vals[w] = v
    if all(v is None for v in vals.values()):
        raise ValueError("홈 깊이를 하나 이상 적어 주세요")
    tid = new_id("ttrd")
    with conn:
        conn.execute("INSERT INTO tire_treads (vehicle_id, id, set_id, date, odometer_km, fl, fr, rl, rr, memo) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (vid, tid, sid, day, odo, vals["fl"], vals["fr"], vals["rl"], vals["rr"], _clean(body.get("memo"))))
    return {"id": tid}


# ---- migration -----------------------------------------------------------------------------

def migrate_season_item(conn, vid) -> int:
    """Once per car: old '타이어(썸머/윈터)' replacement records become sets; the item is turned off.

    Each old record becomes a set (summer, purchase = record date, price/vendor kept, the existing
    차계부 row is linked rather than duplicated) mounted at the record's date/odometer.
    """
    try:
        done = conn.execute("SELECT value FROM vehicle_settings WHERE vehicle_id = ? AND key = ?",
                            (vid, MIGRATED_KEY)).fetchone()
    except sqlite3.OperationalError:
        return 0
    if done:
        return 0
    recs = _rows(conn, "SELECT * FROM replacements WHERE vehicle_id = ? AND item_id = ? "
                       "ORDER BY date, odometer_km", (vid, SEASON_ITEM))
    n = 0
    with conn:
        prev = None
        for r in recs:
            sid = new_id("tset")
            conn.execute(
                "INSERT INTO tire_sets (vehicle_id, id, kind, purchase_date, price_krw, vendor, memo, ledger_id, created_at) "
                "VALUES (?, ?, 'summer', ?, ?, ?, ?, ?, ?)",
                (vid, sid, r["date"], r["price_krw"], r["vendor"], r["memo"], r["ledger_id"],
                 datetime.now(timezone.utc).isoformat()))
            if prev:
                conn.execute("UPDATE tire_mounts SET removed_date = ?, removed_odo = ? WHERE vehicle_id = ? AND id = ?",
                             (r["date"], r["odometer_km"], vid, prev))
            prev = new_id("tmnt")
            conn.execute("INSERT INTO tire_mounts (vehicle_id, id, set_id, mounted_date, mounted_odo) VALUES (?, ?, ?, ?, ?)",
                         (vid, prev, sid, r["date"], r["odometer_km"]))
            n += 1
        conn.execute("UPDATE consumable_items SET active = 0 WHERE vehicle_id = ? AND id = ?", (vid, SEASON_ITEM))
        conn.execute("INSERT OR REPLACE INTO vehicle_settings (vehicle_id, key, value) VALUES (?, ?, '1')",
                     (vid, MIGRATED_KEY))
    return n
