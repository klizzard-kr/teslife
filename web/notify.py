"""In-app notification inbox and Web Push delivery.

The server writes a row when it decides something is worth telling the owner
(a finished charge, a service item that is due or overdue, a monthly reminder
to charge to 100%, and the other alerts the process loop already creates).
Push is sent only to browsers that subscribed, and only for kinds the owner
left on. VAPID keys come from the environment or from one file next to the
database. They are never written into the repository.

Nothing here wakes the car or changes telemetry settings.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from web.formatnum import format_number
from web.pushcrypto import (
    b64url_decode,
    dump_public_raw,
    encrypt_push,
    generate_vapid,
    vapid_jwt,
    vapid_keys_match,
)

KST = timezone(timedelta(hours=9))
CHARGE_NOTICE_WINDOW = timedelta(hours=6)
PUSH_TTL = "86400"

# Preference ids the settings sheet shows, in order. Missing rows mean on.
PREF_GROUPS = (
    ("주행", "Trips", ("trip_start", "trip_end")),
    ("충전", "Charging", ("charge_start", "charge_done", "parking", "charge_full")),
    ("보안", "Security", ("sentry",)),
    ("차량", "Vehicle", ("vehicle", "tire", "maintenance", "software")),
)
# The old single driving switch. Its stored value is copied onto the two new ones.
PREF_ORDER = tuple(kind for _ko, _en, kinds in PREF_GROUPS for kind in kinds)
PREF_TITLE = {
    "trip_start": ("주행 시작", "Trip start"),
    "trip_end": ("주행 종료", "Trip end"),
    "charge_start": ("충전 시작", "Charge start"),
    "charge_done": ("충전 완료", "Charging finished"),
    "parking": ("충전 자리", "Charging spot"),
    "charge_full": ("완충 권장", "Charge to 100%"),
    "sentry": ("감시 경보", "Sentry alarm"),
    "vehicle": ("차량 알림", "Vehicle alert"),
    "tire": ("공기압", "Tire pressure"),
    "maintenance": ("정비 시기", "Service due"),
    "software": ("소프트웨어 업데이트", "Software update"),
}
PREF_GROUP_OF = {kind: (ko, en) for ko, en, kinds in PREF_GROUPS for kind in kinds}
# Sample of the body the owner actually receives. Kinds are only these seven.
PREF_EXAMPLE = {
    "trip_start": ("주행 시작", "Trip start"),
    "trip_end": ("주행 종료", "Trip end"),
    "charge_start": ("충전을 시작하면 알려요", "Charging starting"),
    "charge_done": ("충전이 끝나면 알려요", "Charging finishing"),
    "parking": ("충전 자리를 비워야 할 때 알려요", "When a charging spot must be freed"),
    "charge_full": ("한 달에 한 번 완충을 권할 때 알려요", "The monthly full-charge reminder"),
    "sentry": ("감시 경보가 울리면 알려요", "A sentry alarm"),
    "vehicle": ("차에서 온 경고를 알려요", "A warning from the car"),
    "tire": ("공기압이 낮거나 빠지면 알려요", "Low or leaking tire pressure"),
    "maintenance": ("정비 시기가 되면 알려요", "When service is due"),
    "software": ("새 소프트웨어가 있으면 알려요", "When a software update is waiting"),
}
SENTENCE = {
    "trip_start": ("주행을 시작했어요", "A trip started"),
    "trip_end": ("주행을 마쳤어요", "A trip ended"),
    "charge_start": ("충전을 시작했어요", "Charging started"),
    "charge_end": ("충전이 완료되었어요", "Charging finished"),
    "software_update": ("소프트웨어 업데이트", "Software update"),
    "tire_low": ("공기압이 낮아요", "Tire pressure is low"),
    "tire_leak": ("공기가 빠지고 있어요", "A tire is losing air"),
    "sentry": ("감시 경보가 울렸어요", "Sentry alarm"),
    "sentry_alarm": ("감시 경보가 울렸어요", "Sentry alarm"),
    "open_after_park": ("문이 열려 있어요", "Something is open"),
    "vehicle_alert": ("차량 알림이 있어요", "Vehicle alert"),
    "hv_fault": ("고전압 계통을 확인해 주세요", "High-voltage system"),
}
MASTER_KIND = "master"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notifications (
    vehicle_id TEXT NOT NULL,
    id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    title_en TEXT,
    body_en TEXT,
    href TEXT,
    read_ts TEXT,
    PRIMARY KEY (vehicle_id, id)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    vehicle_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    lang TEXT,
    created_ts TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, endpoint)
);

CREATE TABLE IF NOT EXISTS notification_prefs (
    vehicle_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (vehicle_id, kind)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
    if "details" not in cols:
        conn.execute("ALTER TABLE notifications ADD COLUMN details TEXT")
    # Additive only. Old rows stay, with these columns empty.
    for name, decl in (
        ("delivery", "TEXT"),
        ("push_ts", "TEXT"),
        ("merged_into", "TEXT"),
        ("summary", "TEXT"),
        ("composed", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE notifications ADD COLUMN {name} {decl}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notice_dismissed ("
        "vehicle_id TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (vehicle_id, id))"
    )
    conn.commit()


def _now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    text = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pref_for_alert(alert_kind: str) -> str | None:
    """Map a process alert kind onto a toggle. None means the inbox ignores it."""
    if alert_kind == "charge_end":
        return "charge_done"
    if alert_kind == "charge_full":
        return "charge_full"
    if alert_kind.startswith("consumable:") or alert_kind.startswith("tireset:"):
        return "maintenance"
    if alert_kind in ("tire_low", "tire_leak", "tpms_warning"):
        return "tire"
    if alert_kind == "sentry" or alert_kind.startswith("sentry"):
        return "sentry"
    if alert_kind in ("trip_start", "trip_end"):
        return alert_kind
    if alert_kind.startswith("park14h") or alert_kind.startswith("parkdc"):
        return "parking"
    if alert_kind == "charge_start":
        return "charge_start"
    if alert_kind in ("software_update", "update_scheduled"):
        return "software"
    if alert_kind in (
        "vehicle_alert", "hv_fault", "open_after_park", "battery_month",
    ):
        return "vehicle"
    return None


def _pref_row(conn: sqlite3.Connection, vehicle_id: str, kind: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT enabled FROM notification_prefs WHERE vehicle_id = ? AND kind = ?",
            (vehicle_id, kind),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return int(row[0])


def master_enabled(conn: sqlite3.Connection, vehicle_id: str) -> bool:
    """No saved row stays on, same as the kind toggles. Off is stored as 0."""
    saved = _pref_row(conn, vehicle_id, MASTER_KIND)
    if saved is None:
        return True
    return bool(saved)


def _legacy_driving(conn: sqlite3.Connection, vehicle_id: str) -> int | None:
    return _pref_row(conn, vehicle_id, "driving")


def _persist_driving_split(conn: sqlite3.Connection, vehicle_id: str) -> None:
    """Copy a stored driving switch onto trip start and trip end, once each.

    A missing new row keeps that old value. A row the owner already set is left alone.
    A read-only connection cannot write, and the same value is still reported.
    """
    legacy = _legacy_driving(conn, vehicle_id)
    if legacy is None:
        return
    missing = [kind for kind in ("trip_start", "trip_end") if _pref_row(conn, vehicle_id, kind) is None]
    if not missing:
        return
    try:
        for kind in missing:
            conn.execute(
                "INSERT OR IGNORE INTO notification_prefs (vehicle_id, kind, enabled) VALUES (?, ?, ?)",
                (vehicle_id, kind, legacy),
            )
        conn.commit()
    except sqlite3.OperationalError:
        return


def _kind_on(conn: sqlite3.Connection, vehicle_id: str, kind: str) -> bool:
    saved = _pref_row(conn, vehicle_id, kind)
    if saved is None and kind in ("trip_start", "trip_end"):
        legacy = _legacy_driving(conn, vehicle_id)
        if legacy is not None:
            return bool(legacy)
    if saved is None:
        return True
    return bool(saved)


def enabled(conn: sqlite3.Connection, vehicle_id: str, kind: str) -> bool:
    """Whether a new notice of this kind may be stored. A missing master row stays on."""
    _persist_driving_split(conn, vehicle_id)
    if kind != MASTER_KIND and _pref_row(conn, vehicle_id, MASTER_KIND) == 0:
        return False
    return _kind_on(conn, vehicle_id, kind)


def list_prefs(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    _persist_driving_split(conn, vehicle_id)
    kinds = []
    for kind in PREF_ORDER:
        ko, en = PREF_TITLE[kind]
        ex_ko, ex_en = PREF_EXAMPLE[kind]
        kinds.append({
            "id": kind,
            "title": ko,
            "title_en": en,
            "group": PREF_GROUP_OF[kind][0],
            "group_en": PREF_GROUP_OF[kind][1],
            "example": ex_ko,
            "example_en": ex_en,
            "enabled": _kind_on(conn, vehicle_id, kind),
        })
    return {"vehicle_id": vehicle_id, "master": master_enabled(conn, vehicle_id), "kinds": kinds}


def set_pref(conn: sqlite3.Connection, vehicle_id: str, kind: str, on: bool) -> dict[str, Any]:
    if kind != MASTER_KIND and kind not in PREF_TITLE:
        raise ValueError("unknown notification kind")
    ensure_schema(conn)
    _persist_driving_split(conn, vehicle_id)
    with conn:
        conn.execute(
            "INSERT INTO notification_prefs (vehicle_id, kind, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(vehicle_id, kind) DO UPDATE SET enabled = excluded.enabled",
            (vehicle_id, kind, 1 if on else 0),
        )
    return list_prefs(conn, vehicle_id)


def _as_number(raw: Any) -> float | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _latest_number(
    conn: sqlite3.Connection,
    vehicle_id: str,
    name: str,
    end_ts: str,
    after_ts: str | None = None,
) -> float | None:
    """Latest stored sample at or before end_ts. after_ts limits the window to (after, end]."""
    try:
        if after_ts:
            row = conn.execute(
                "SELECT value FROM signals WHERE vehicle_id = ? AND name = ? AND ts > ? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (vehicle_id, name, after_ts, end_ts),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT value FROM signals WHERE vehicle_id = ? AND name = ? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (vehicle_id, name, end_ts),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return _as_number(row[0])


def trip_end_signal_rows(conn: sqlite3.Connection, vehicle_id: str, end_ts: str) -> list[list[str]]:
    """Battery, range, and tire rows from signals stored at the trip end. Missing values are left out."""
    from web.notice_view import format_range_km, format_soc
    from web.pressure import format_bar_number

    if not end_ts:
        return []
    rows: list[list[str]] = []
    soc = _latest_number(conn, vehicle_id, "BatteryLevel", end_ts)
    if soc is not None:
        rows.append(["배터리", format_soc(soc)])
    estimated = _latest_number(conn, vehicle_id, "EstBatteryRange", end_ts)
    rated = _latest_number(conn, vehicle_id, "RatedRange", end_ts)
    distance = estimated if estimated is not None and estimated > 0 else rated
    if distance is not None and distance > 0:
        rows.append(["주행가능", format_range_km(distance)])
    try:
        after = _now_iso(_parse(end_ts) + timedelta(minutes=2))
    except ValueError:
        after = ""
    for label, name in (
        ("앞 왼쪽", "TpmsPressureFl"),
        ("앞 오른쪽", "TpmsPressureFr"),
        ("뒤 왼쪽", "TpmsPressureRl"),
        ("뒤 오른쪽", "TpmsPressureRr"),
    ):
        bar = _latest_number(conn, vehicle_id, name, after, end_ts) if after else None
        if bar is None:
            bar = _latest_number(conn, vehicle_id, name, end_ts)
        if bar is None or bar <= 0:
            continue
        rows.append([label, format_bar_number(bar, "psi")])
    return rows


def _fill_trip_end_rows(
    conn: sqlite3.Connection, vehicle_id: str, end_ts: str, rows: list
) -> list:
    """Add trip-end rows for an older notice. The stored notification row is not written."""
    have = {str(pair[0]) for pair in rows if pair}
    extra = [pair for pair in trip_end_signal_rows(conn, vehicle_id, end_ts) if pair[0] not in have]
    return list(rows) + extra


def list_notifications(conn: sqlite3.Connection, vehicle_id: str, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    ensure_schema(conn)
    visible = "IFNULL(delivery, '') != 'merged'"
    try:
        rows = conn.execute(
            "SELECT id, ts, kind, title, body, title_en, body_en, href, read_ts, details, summary, composed "
            "FROM notifications WHERE vehicle_id = ? AND " + visible +
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (vehicle_id, limit),
        ).fetchall()
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE vehicle_id = ? AND read_ts IS NULL AND " + visible,
            (vehicle_id,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return {"vehicle_id": vehicle_id, "notifications": [], "unread": 0}
    items = [
        {
            "id": r[0],
            "ts": r[1],
            "kind": r[2],
            "title": r[3],
            "body": r[4],
            "title_en": r[5],
            "body_en": r[6],
            "href": r[7],
            "read": r[8] is not None,
            "details": json.loads(r[9]) if r[9] else [],
            "_summary": r[10],
            "_composed": r[11],
        }
        for r in rows
    ]
    from web.notice_view import present

    for item in items:
        composed = item.pop("_composed")
        view = present({**item, "summary": item.pop("_summary"), "composed": composed})
        item["display_title"] = view["title"]
        item["display_body"] = view["summary"]
        item["event_ts"] = view["event_ts"]
        rows = view["rows"]
        if str(item["id"]).startswith("trip_end:"):
            rows = _fill_trip_end_rows(conn, vehicle_id, str(item.get("ts") or ""), rows)
            from web.notice_view import _align_detail_rows

            rows = _align_detail_rows(rows, str(item["id"]), str(item.get("body") or ""))
            if not composed:
                from web.notice_view import trip_end_summary

                item["display_body"] = trip_end_summary(str(item.get("body") or ""), rows)
        item["display_details"] = rows
    items.sort(key=lambda item: (item.get("event_ts") or item.get("ts") or "", item.get("id") or ""), reverse=True)
    return {"vehicle_id": vehicle_id, "notifications": items, "unread": int(unread)}


TAB_KINDS = {
    "drive": ("driving", "trip_start", "trip_end"),
    "charge": ("charge_done", "charge_start", "parking", "charge_full"),
    "security": ("sentry",),
    "car": ("vehicle", "maintenance", "tire", "software"),
}


def _tab_kinds(tab: str) -> tuple[str, ...]:
    kinds = TAB_KINDS.get(str(tab or ""))
    if not kinds:
        raise ValueError("tab is not allowed")
    return kinds


def mark_read(
    conn: sqlite3.Connection,
    vehicle_id: str,
    notice_id: str | None,
    all_rows: bool,
    now: datetime | None = None,
    tab: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    ts = _now_iso(now)
    with conn:
        if tab:
            kinds = _tab_kinds(tab)
            marks = ",".join("?" * len(kinds))
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM notifications WHERE vehicle_id = ? AND kind IN ({marks})",
                    (vehicle_id, *kinds),
                ).fetchall()
            ]
            conn.execute(
                f"UPDATE notifications SET read_ts = ? WHERE vehicle_id = ? AND read_ts IS NULL "
                f"AND kind IN ({marks})",
                (ts, vehicle_id, *kinds),
            )
            if ids:
                child = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE notifications SET read_ts = ? WHERE vehicle_id = ? AND read_ts IS NULL "
                    f"AND merged_into IN ({child})",
                    (ts, vehicle_id, *ids),
                )
        elif all_rows:
            conn.execute(
                "UPDATE notifications SET read_ts = ? WHERE vehicle_id = ? AND read_ts IS NULL",
                (ts, vehicle_id),
            )
        else:
            if not notice_id:
                raise ValueError("id is required")
            cur = conn.execute(
                "UPDATE notifications SET read_ts = ? WHERE vehicle_id = ? AND id = ? AND read_ts IS NULL",
                (ts, vehicle_id, notice_id),
            )
            if cur.rowcount == 0:
                found = conn.execute(
                    "SELECT 1 FROM notifications WHERE vehicle_id = ? AND id = ?",
                    (vehicle_id, notice_id),
                ).fetchone()
                if found is None:
                    raise KeyError("notification not found")
    return list_notifications(conn, vehicle_id)


_DISMISS_PREFIXES = ("maint:", "charge_full:")


def _dismissed(conn: sqlite3.Connection, vehicle_id: str, notice_id: str) -> bool:
    if not str(notice_id).startswith(_DISMISS_PREFIXES):
        return False
    row = conn.execute(
        "SELECT 1 FROM notice_dismissed WHERE vehicle_id = ? AND id = ?",
        (vehicle_id, notice_id),
    ).fetchone()
    return row is not None


def _remember_dismissed(conn: sqlite3.Connection, vehicle_id: str, notice_ids: list[str]) -> None:
    for notice_id in notice_ids:
        if str(notice_id).startswith(_DISMISS_PREFIXES):
            conn.execute(
                "INSERT OR IGNORE INTO notice_dismissed (vehicle_id, id) VALUES (?, ?)",
                (vehicle_id, notice_id),
            )


def delete_notices(
    conn: sqlite3.Connection,
    vehicle_id: str,
    notice_id: str | None = None,
    tab: str | None = None,
) -> dict[str, Any]:
    """Remove inbox rows only. Other tables stay. Merged children go with the parent.

    A deleted pending row is gone, so the 10-minute fallback cannot send it.
    """
    ensure_schema(conn)
    with conn:
        if notice_id:
            notice_id = str(notice_id).strip()
            if not notice_id or len(notice_id) > 180 or ".." in notice_id:
                raise ValueError("id is not allowed")
            gone = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM notifications WHERE vehicle_id = ? AND (id = ? OR merged_into = ?)",
                    (vehicle_id, notice_id, notice_id),
                ).fetchall()
            ]
            if not gone:
                raise KeyError("notification not found")
            _remember_dismissed(conn, vehicle_id, gone)
            conn.execute(
                "DELETE FROM notifications WHERE vehicle_id = ? AND (id = ? OR merged_into = ?)",
                (vehicle_id, notice_id, notice_id),
            )
        elif tab:
            kinds = _tab_kinds(tab)
            marks = ",".join("?" * len(kinds))
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM notifications WHERE vehicle_id = ? AND kind IN ({marks})",
                    (vehicle_id, *kinds),
                ).fetchall()
            ]
            children = []
            if ids:
                child = ",".join("?" * len(ids))
                children = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT id FROM notifications WHERE vehicle_id = ? AND merged_into IN ({child})",
                        (vehicle_id, *ids),
                    ).fetchall()
                ]
            _remember_dismissed(conn, vehicle_id, ids + children)
            conn.execute(
                f"DELETE FROM notifications WHERE vehicle_id = ? AND kind IN ({marks})",
                (vehicle_id, *kinds),
            )
            if ids:
                child = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM notifications WHERE vehicle_id = ? AND merged_into IN ({child})",
                    (vehicle_id, *ids),
                )
        else:
            raise ValueError("id is required")
    return list_notifications(conn, vehicle_id)


def _insert(
    conn: sqlite3.Connection,
    vehicle_id: str,
    notice_id: str,
    kind: str,
    title: str,
    body: str,
    title_en: str,
    body_en: str,
    href: str,
    now: datetime,
    details: list | None = None,
    webhook_opener: Callable[..., Any] | None = None,
    push_opener: Callable[..., Any] | None = None,
) -> bool:
    """Insert one unread notice. False when that id is already stored or the kind is off.

    A configured bot webhook stores the row as pending and does not push yet.
    """
    if kind not in set(PREF_TITLE) | {"driving"} or not enabled(conn, vehicle_id, kind):
        return False
    ensure_schema(conn)
    if _dismissed(conn, vehicle_id, notice_id):
        return False
    from web.bot_notify import holding, publish_pending

    hold = holding()
    detail_text = json.dumps(details or [], ensure_ascii=False)
    stored_ts = _now_iso(now)
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications "
            "(vehicle_id, id, ts, kind, title, body, title_en, body_en, href, details, delivery) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vehicle_id, notice_id, stored_ts, kind, title, body, title_en, body_en, href,
                detail_text, "pending" if hold else None,
            ),
        )
        inserted = cur.rowcount == 1
    if inserted:
        try:
            if hold:
                publish_pending(notice_id, body, details or [], stored_ts, webhook_opener)
            else:
                from web.notice_view import present

                view = present({
                    "id": notice_id, "ts": stored_ts, "kind": kind,
                    "title": title, "body": body, "details": details or [],
                })
                from web.notice_view import inbox_tab

                tab = inbox_tab(kind)
                deep = "/?inbox=" + tab if tab else "/?notice=" + quote(notice_id, safe="")
                from web.pressure import convert_psi_text, get_pressure_unit

                unit = get_pressure_unit(conn, vehicle_id)
                push_body = convert_psi_text(view["push_body"], unit)
                push_and_prune(
                    conn, vehicle_id, view["title"], push_body,
                    view["title"], push_body,
                    deep,
                    opener=push_opener,
                    notice_id=notice_id, event_ts=view["event_ts"],
                )
        except Exception:
            # A push failure must not undo the inbox row or stop processing.
            pass
    return inserted


def _first_line(body: str) -> str:
    return (body or "").split("\n", 1)[0].strip()


def _plain_title(title: str) -> str:
    """Drop a 'from teslife' prefix when this app added one. The browser may still show the site name."""
    text = (title or "").strip()
    lower = text.lower()
    for prefix in ("from teslife", "teslife:", "teslife ·", "teslife -"):
        if lower.startswith(prefix):
            return text[len(prefix):].strip() or text
    return text


def remember_alert(
    conn: sqlite3.Connection,
    vehicle_id: str,
    alert_kind: str,
    state: str,
    message: str,
    now: datetime,
) -> bool:
    """Copy a webhook alert into the inbox. Charge, service, and the 100% reminder
    are written by sync_notices so they exist even when webhook alerts are off."""
    pref = pref_for_alert(alert_kind)
    if pref in (None, "charge_done", "charge_full"):
        return False
    if pref == "maintenance" and not alert_kind.startswith("tireset:"):
        return False
    title, title_en, body, details = _notice_parts(alert_kind, message)
    notice_id = f"{alert_kind}:{state}"[:180]
    return _insert(
        conn, vehicle_id, notice_id, pref, title, body, title_en, title_en, _href(pref), now, details,
    )


def _notice_parts(alert_kind: str, message: str) -> tuple[str, str, str, list]:
    """Sentence title, one or two lines of body, and one label/value row per stored detail."""
    lines = [line for line in (message or "").split("\n") if line.strip()]
    prose = lines[0] if lines else ""
    details = []
    for line in lines[1:]:
        if "|" in line:
            label, value = line.split("|", 1)
            details.append([label.strip(), value.strip()])
    sentence = SENTENCE.get(alert_kind)
    if alert_kind.startswith("consumable:") or alert_kind.startswith("tireset:"):
        if " · " in prose:
            title, body = prose.split(" · ", 1)
        else:
            title, body = prose, ""
        return title, title, body, details
    if sentence:
        return sentence[0], sentence[1], prose, details
    title, title_en = PREF_TITLE.get(pref_for_alert(alert_kind) or "", (prose, prose))
    return title, title_en, prose, details


def _href(kind: str) -> str:
    if kind in ("charge_done", "charge_full", "parking", "charge_start"):
        return "#charges"
    if kind in ("maintenance", "tire"):
        return "#consumables"
    if kind in ("driving", "trip_start", "trip_end"):
        return "#trips"
    return "#home"


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM process_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO process_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _charge_detail_lines(conn, vehicle_id, start_ts, end_ts, kwh) -> list[str]:
    """One label|value line per fact the charge-complete popup shows."""
    try:
        row = conn.execute(
            "SELECT kind, battery_kwh, loss_kwh, avg_charger_kw, start_ts, end_ts FROM charges "
            "WHERE vehicle_id = ? AND start_ts = ?",
            (vehicle_id, start_ts),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    kind = battery = used = avg = None
    start = end = None
    if row:
        kind, battery, used, avg, start, end = row
    lines = []
    if kwh is not None:
        lines.append(f"충전량|{format_number(float(kwh), 1)} kWh")
    if start and end:
        try:
            hours = (_parse(end) - _parse(start)).total_seconds() / 3600.0
            h, m = divmod(round(hours * 60), 60)
            lines.append("시간|" + (f"{h}시간 {m}분" if h else f"{m}분"))
        except ValueError:
            pass
    if kind:
        lines.append("전류|" + ("교류(AC)" if kind == "AC" else "직류(DC)"))
    if battery is not None:
        lines.append(f"배터리 저장량|{format_number(float(battery), 1)} kWh")
    if used is not None:
        lines.append(f"충전 중 사용량|{format_number(float(used), 1)} kWh")
    if avg is not None:
        lines.append(f"평균 충전 전력|{format_number(float(avg), 1)} kW")
    reason = _charge_reason(conn, vehicle_id, end or end_ts)
    if reason:
        lines.append(f"종료 사유|{reason}")
    return lines


def _charge_reason(conn, vehicle_id, end_ts) -> str | None:
    if not end_ts:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'DetailedChargeState' AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id, end_ts),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    raw = str(row[0])
    return {
        "Complete": "완료",
        "Stopped": "중단",
        "Disconnected": "연결 안 됨",
        "NoPower": "전력 없음",
    }.get(raw)


def _charge_notices(conn: sqlite3.Connection, vehicle_id: str, now: datetime) -> None:
    """One notice per charge that finished inside the recent window. Older rows are left alone."""
    key = f"notice_charge_after:{vehicle_id}"
    cursor = _meta_get(conn, key)
    floor = now - CHARGE_NOTICE_WINDOW
    if cursor:
        try:
            seen = _parse(cursor)
            if seen > floor:
                floor = seen
        except ValueError:
            pass
    try:
        rows = conn.execute(
            "SELECT start_ts, end_ts, kwh_added, charger_kwh, start_soc, end_soc "
            "FROM charges WHERE vehicle_id = ? AND end_ts > ? ORDER BY end_ts",
            (vehicle_id, _now_iso(floor)),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    newest = floor
    for start_ts, end_ts, kwh_added, charger_kwh, start_soc, end_soc in rows:
        try:
            end = _parse(end_ts)
        except ValueError:
            continue
        if end > newest:
            newest = end
        kwh = charger_kwh if charger_kwh is not None else kwh_added
        soc = ""
        if start_soc is not None and end_soc is not None:
            soc = f"{float(start_soc):.0f}% → {float(end_soc):.0f}%"
        kwh_bit = f"{format_number(float(kwh), 1)}kWh" if kwh is not None else ""
        line = ", ".join(bit for bit in (soc, kwh_bit) if bit) or "충전이 끝났어요"
        line_en = ", ".join(bit for bit in (soc, kwh_bit) if bit) or "Charging finished"
        rows = _charge_detail_lines(conn, vehicle_id, start_ts, end_ts, kwh)
        details = []
        for detail in rows:
            if "|" in detail:
                label, value = detail.split("|", 1)
                details.append([label, value])
        _insert(
            conn, vehicle_id, f"charge_end:{start_ts}", "charge_done",
            "충전이 완료되었어요", line, "Charging finished", line_en, "#charges", end, details,
        )
    with conn:
        _meta_set(conn, key, _now_iso(newest if newest > floor else now))


def _maintenance_notices(conn: sqlite3.Connection, vehicle_id: str, now: datetime) -> None:
    from ledger.consumables import list_items

    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        items = list_items(conn, vehicle_id, include_inactive=False, as_of=now.astimezone(KST).date())
    except sqlite3.Error:
        return
    finally:
        conn.row_factory = old
    for it in items:
        if it.get("id") == "cons-tire-rotation" or it.get("rule") == "as_needed":
            continue
        status = it.get("status")
        if status not in ("soon", "overdue"):
            continue
        name = str(it.get("name") or "정비")
        if status == "overdue":
            title, title_en = f"{name} 기한이 지났어요", f"{name} is overdue"
        else:
            title, title_en = f"{name} 점검 시기예요", f"{name} is due"
        days = it.get("remaining_days")
        km = it.get("remaining_km")
        due = it.get("next_due_date")
        signed = int(days) if days is not None else None
        if signed is not None and status == "overdue" and signed > 0:
            signed = -signed
        bits = []
        if signed is not None:
            bits.append(f"{signed}일")
        if km is not None:
            bits.append(f"{float(km):.0f} km")
        if due:
            bits.append(str(due))
        line = " · ".join(bits) if bits else "시기를 확인해 주세요"
        body = line
        body_en = line
        details = []
        if signed is not None:
            label = "지난 날" if signed < 0 or status == "overdue" else "남은 날"
            details.append([label, f"{abs(signed)}일"])
        if km is not None:
            km_n = float(km)
            details.append(["지난 거리" if km_n < 0 else "남은 거리", f"{abs(km_n):.0f} km"])
        if due:
            from web.notice_view import _due_phrase
            details.append(["기한", _due_phrase(str(due)) or str(due)])
        when = now
        cycle = f"{it.get('next_due_date') or ''}/{it.get('next_due_km') or ''}"
        _insert(
            conn, vehicle_id, f"maint:{it.get('id')}:{status}:{cycle}"[:180], "maintenance",
            title, body, title_en, body_en, "#consumables", when, details,
        )


def _full_charge_notice(conn: sqlite3.Connection, vehicle_id: str, now: datetime) -> None:
    """The monthly full charge is the cons-battery-full service item, not a separate notice."""
    return


def sync_notices(conn: sqlite3.Connection, vehicle_id: str, now: datetime | None = None) -> None:
    """Write inbox rows for charges, service, and the monthly 100% reminder."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ensure_schema(conn)
    _charge_notices(conn, vehicle_id, now)
    _maintenance_notices(conn, vehicle_id, now)
    _full_charge_notice(conn, vehicle_id, now)
    from web.bot_notify import flush_pending

    flush_pending(conn, vehicle_id, now)


# ---------------------------------------------------------------- Web Push

def _db_dir(conn: sqlite3.Connection) -> Path | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    path = row[2] if row and len(row) > 2 else None
    if not path:
        return None
    return Path(path).resolve().parent


def vapid_subject() -> str:
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    if subject.startswith("mailto:") or subject.startswith("https://"):
        return subject
    return "mailto:teslife@localhost"


def load_vapid(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Public and private keys (base64url). Env wins. Otherwise the file beside the database."""
    public = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    private = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if public and private:
        if not vapid_keys_match(public, private):
            return None
        return public, private
    folder = _db_dir(conn)
    if folder is None:
        return None
    path = folder / "vapid.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            public, private = str(data.get("public") or ""), str(data.get("private") or "")
        except (OSError, ValueError):
            return None
        if public and private and vapid_keys_match(public, private):
            return public, private
        return None
    public, private = generate_vapid()
    try:
        path.write_text(json.dumps({"public": public, "private": private}), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return public, private
    return public, private


def vapid_public(conn: sqlite3.Connection) -> dict[str, Any]:
    keys = load_vapid(conn)
    if keys is None:
        return {"enabled": False, "publicKey": None}
    return {"enabled": True, "publicKey": keys[0]}


def _valid_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc) and len(url) <= 2000


def save_subscription(conn: sqlite3.Connection, vehicle_id: str, body: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    endpoint = str(body.get("endpoint") or "").strip()
    keys = body.get("keys") if isinstance(body.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or body.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or body.get("auth") or "").strip()
    lang = "en" if str(body.get("lang") or "") == "en" else "ko"
    if not _valid_endpoint(endpoint):
        raise ValueError("endpoint must be an https URL")
    try:
        dump_public_raw(p256dh)
        secret = b64url_decode(auth)
        if len(secret) != 16:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid push subscription keys") from None
    ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO push_subscriptions (vehicle_id, endpoint, p256dh, auth, lang, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vehicle_id, endpoint) DO UPDATE SET "
            "p256dh = excluded.p256dh, auth = excluded.auth, lang = excluded.lang",
            (vehicle_id, endpoint, p256dh, auth, lang, _now_iso(now)),
        )
    return {"ok": True}


def delete_subscription(conn: sqlite3.Connection, vehicle_id: str, endpoint: str) -> dict[str, Any]:
    if not endpoint:
        raise ValueError("endpoint is required")
    ensure_schema(conn)
    with conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE vehicle_id = ? AND endpoint = ?",
            (vehicle_id, endpoint),
        )
    return {"ok": True}


Opener = Callable[..., Any]


def push_and_prune(
    conn: sqlite3.Connection,
    vehicle_id: str,
    title: str,
    body: str,
    title_en: str,
    body_en: str,
    href: str,
    opener: Opener | None = None,
    notice_id: str = "",
    event_ts: str = "",
) -> int:
    """Send and drop subscriptions the push service says are gone."""
    try:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth, lang FROM push_subscriptions WHERE vehicle_id = ?",
            (vehicle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    keys = load_vapid(conn)
    if keys is None:
        return 0
    public, private = keys
    sent = 0
    for endpoint, p256dh, auth, lang in rows:
        use_en = lang == "en"
        payload = json.dumps({
            "title": title_en if use_en else title,
            "body": body_en if use_en else body,
            "href": href or "/",
            "id": notice_id,
            "ts": event_ts,
        }, ensure_ascii=False).encode("utf-8")
        gone = _deliver(conn, vehicle_id, endpoint, p256dh, auth, payload, public, private, opener)
        if gone is True:
            sent += 1
    return sent


def _deliver(
    conn: sqlite3.Connection,
    vehicle_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    payload: bytes,
    public: str,
    private: str,
    opener: Opener | None,
) -> bool:
    try:
        ua_public = b64url_decode(p256dh)
        auth_secret = b64url_decode(auth)
        body, _local = encrypt_push(payload, ua_public, auth_secret)
    except (ValueError, TypeError):
        return False
    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"
    exp = int(datetime.now(timezone.utc).timestamp()) + 12 * 3600
    token = vapid_jwt(private, aud, vapid_subject(), exp)
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL": PUSH_TTL,
            "Authorization": f"vapid t={token}, k={public}",
        },
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=8) as res:
            code = getattr(res, "status", 201) or 201
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    if code in (404, 410):
        with conn:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE vehicle_id = ? AND endpoint = ?",
                (vehicle_id, endpoint),
            )
        return False
    return 200 <= code < 300
