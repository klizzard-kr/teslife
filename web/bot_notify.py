"""Hold a notice for the bot, then send, skip, merge, or fall back.

BOT_NOTIFY_WEBHOOK_URL empty, or BOT_PUSH_TOKEN empty, keeps the old push.
The webhook body is HMAC-SHA256 of the raw JSON, hex, in X-Teslife-Signature
as sha256=<hex>. The key is BOT_PUSH_TOKEN. If BOT_NOTIFY_WEBHOOK_KEY is set,
that POST also sends Authorization: Bearer <BOT_NOTIFY_WEBHOOK_KEY>. The bot
calls back with Authorization: Bearer <BOT_PUSH_TOKEN>.

The payload has id, kind, event_ts, and values. It has no coordinates, no VIN,
and a place is only the dong and road. Rows are not deleted. New columns are
added beside the old ones.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlparse

PENDING_AFTER = timedelta(minutes=10)
_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,180}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_TEXT = re.compile(
    r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}|\b[A-HJ-NPR-Z0-9]{17}\b"
)
_BANNED = {
    "lat", "lon", "lng", "latitude", "longitude", "vin",
    "endpoint", "p256dh", "auth", "token", "secret",
}
Opener = Callable[..., Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, "redirect", headers, fp)


def webhook_url() -> str:
    return os.environ.get("BOT_NOTIFY_WEBHOOK_URL", "").strip()


def bot_token() -> str:
    return os.environ.get("BOT_PUSH_TOKEN", "").strip()


def webhook_key() -> str:
    return os.environ.get("BOT_NOTIFY_WEBHOOK_KEY", "").strip()


def token_configured() -> bool:
    return bool(bot_token())


def _url_ok(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    return len(url) <= 2000


def holding() -> bool:
    """True when a new notice waits for the bot instead of pushing now."""
    return token_configured() and _url_ok(webhook_url())


def token_matches(header: str) -> bool:
    token = bot_token()
    if not token:
        return False
    scheme, _, given = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not given.strip():
        return False
    try:
        return hmac.compare_digest(given.strip(), token)
    except (TypeError, ValueError):
        return False


def sign_body(raw: bytes, token: str | None = None) -> str:
    key = (token if token is not None else bot_token()).encode("utf-8")
    digest = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def _safe(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _BANNED:
                raise ValueError("unsafe")
            _safe(item)
    elif isinstance(value, list):
        for item in value:
            _safe(item)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise ValueError("unsafe")


def event_payload(notice_id: str, body: str, details: list | None, stored_ts: str) -> dict[str, Any]:
    from web.notice_view import event_facts, present

    view = present({
        "id": notice_id, "ts": stored_ts, "title": "", "body": body, "details": details or [],
    })
    kind = notice_id.split(":", 1)[0] or "notice"
    payload = {
        "id": notice_id,
        "kind": kind,
        "event_ts": view["event_ts"],
        "values": event_facts(body, details, notice_id),
    }
    _safe(payload)
    return payload


def _post(payload: dict[str, Any], opener: Opener | None) -> bool:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "teslife-bot-notify",
        "X-Teslife-Signature": sign_body(raw),
    }
    key = webhook_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        webhook_url(),
        data=raw,
        method="POST",
        headers=headers,
    )
    open_ = opener
    try:
        if open_ is None:
            open_ = urllib.request.build_opener(_NoRedirect).open
        with open_(req, timeout=5) as res:
            code = getattr(res, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    return 200 <= code < 300


def publish_direct(
    kind: str, event_id: str, event_ts: str, values: dict[str, Any], opener: Opener | None = None,
) -> bool:
    """POST one bot event. No inbox row and no app push, even when this fails."""
    if not _url_ok(webhook_url()):
        return False
    payload = {"id": event_id, "kind": kind, "event_ts": event_ts, "values": values}
    try:
        _safe(payload)
    except ValueError:
        return False
    try:
        return _post(payload, opener)
    except Exception as exc:
        print(f"bot webhook: {type(exc).__name__}", flush=True)
        return False


def publish_pending(
    notice_id: str,
    body: str,
    details: list | None,
    stored_ts: str,
    opener: Opener | None = None,
) -> bool:
    """POST the signed event. A failure leaves the row pending for the fallback."""
    try:
        payload = event_payload(notice_id, body, details, stored_ts)
    except ValueError:
        return False
    try:
        return _post(payload, opener)
    except Exception as exc:  # the inbox row must stay either way
        print(f"bot webhook: {type(exc).__name__}", flush=True)
        return False


def _rows(conn: sqlite3.Connection, vehicle_id: str, where: str, args: tuple) -> list[sqlite3.Row]:
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(
            "SELECT id, ts, kind, title, body, title_en, body_en, href, details, summary, "
            "composed, delivery, merged_into FROM notifications "
            f"WHERE vehicle_id = ? AND {where}",
            (vehicle_id, *args),
        ).fetchall())
    finally:
        conn.row_factory = old


def _one(conn: sqlite3.Connection, vehicle_id: str, notice_id: str) -> sqlite3.Row | None:
    found = _rows(conn, vehicle_id, "id = ?", (notice_id,))
    return found[0] if found else None


def list_pending(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, Any]:
    from web.notify import ensure_schema

    ensure_schema(conn)
    items = []
    for row in _rows(conn, vehicle_id, "delivery = 'pending' ORDER BY ts, id", ()):
        details = json.loads(row["details"]) if row["details"] else []
        try:
            items.append(event_payload(row["id"], row["body"] or "", details, row["ts"]))
        except ValueError:
            continue
    return {"pending": items}


def _sentence(value: Any, field: str, required: bool) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if len(text) > 180 or _SECRET_TEXT.search(text) or "\n" in text or "\x00" in text:
        raise ValueError(f"{field} is not allowed")
    return text


def _stored_notice(conn: sqlite3.Connection, vehicle_id: str, notice_id: str) -> bool:
    """True when this exact id is already a notice for the vehicle."""
    try:
        row = conn.execute(
            "SELECT 1 FROM notifications WHERE vehicle_id = ? AND id = ? LIMIT 1",
            (vehicle_id, notice_id),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _notice_id(value: Any, conn: sqlite3.Connection | None = None, vehicle_id: str | None = None) -> str:
    """Allow the usual ids, and any stored notice id even with Korean or '|'.

    A stored id is accepted only by exact match. Length stays capped and control
    characters stay rejected. An unknown unicode id is still refused.
    """
    text = str(value or "").strip()
    if not text or len(text) > 180 or _CONTROL.search(text):
        raise ValueError("id is not allowed")
    plain = _ID.fullmatch(text) and ".." not in text and not text.startswith("/") and "//" not in text
    if plain:
        return text
    if conn is not None and vehicle_id and _stored_notice(conn, vehicle_id, text):
        return text
    raise ValueError("id is not allowed")


def _push_copy(conn, vehicle_id: str, notice_id: str, kind: str, title: str, summary: str, opener, event_ts: str) -> None:
    from web.notice_view import inbox_tab
    from web.notify import push_and_prune

    tab = inbox_tab(kind)
    href = "/?inbox=" + tab if tab else "/?notice=" + quote(notice_id, safe="")
    push_and_prune(
        conn, vehicle_id, title, summary, title, summary, href,
        opener=opener, notice_id=notice_id, event_ts=event_ts,
    )


def _claim(conn, vehicle_id: str, notice_id: str, delivery: str, now: datetime, title: str | None, summary: str | None) -> bool:
    sets = ["delivery = ?", "push_ts = ?"]
    args: list[Any] = [delivery, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
    if title is not None:
        sets.append("title = ?")
        args.append(title)
    if summary is not None:
        sets.append("summary = ?")
        sets.append("composed = 1")
        args.append(summary)
    args.extend([vehicle_id, notice_id])
    cur = conn.execute(
        f"UPDATE notifications SET {', '.join(sets)} "
        "WHERE vehicle_id = ? AND id = ? AND delivery = 'pending'",
        args,
    )
    return cur.rowcount == 1


def apply_bot_action(
    conn: sqlite3.Connection,
    vehicle_id: str,
    body: dict[str, Any],
    now: datetime | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    from web.notify import ensure_schema

    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    action = str(body.get("action") or "").strip()
    if action == "create":
        return _create(conn, vehicle_id, body, now, opener)
    if action not in ("send", "skip", "merge"):
        raise ValueError("action is not allowed")
    notice_id = _notice_id(body.get("id"), conn, vehicle_id)
    row = _one(conn, vehicle_id, notice_id)
    if row is None:
        raise KeyError("notification not found")
    if action == "send" and row["delivery"] == "sent":
        return {"ok": True, "action": "send", "id": notice_id, "already": True}
    if action == "skip" and row["delivery"] == "skipped":
        return {"ok": True, "action": "skip", "id": notice_id, "already": True}
    title = _sentence(body.get("title"), "title", action == "send")
    summary = _sentence(body.get("summary"), "summary", action == "send")
    if action == "merge":
        return _merge(conn, vehicle_id, row, body, title, summary)
    if action == "send":
        from web.notify import enabled

        if not enabled(conn, vehicle_id, row["kind"]):
            raise ValueError("kind is off")
    if row["delivery"] != "pending":
        raise ValueError("not pending")
    with conn:
        claimed = _claim(conn, vehicle_id, notice_id, "sent" if action == "send" else "skipped", now, title, summary)
    if not claimed:
        fresh = _one(conn, vehicle_id, notice_id)
        if fresh is not None and fresh["delivery"] in ("sent", "skipped"):
            return {"ok": True, "action": action, "id": notice_id, "already": True}
        raise ValueError("not pending")
    if action == "send":
        from web.notice_view import present

        details = json.loads(row["details"]) if row["details"] else []
        event_ts = present({
            "id": row["id"], "ts": row["ts"], "kind": row["kind"],
            "title": row["title"], "body": row["body"] or "", "details": details,
        })["event_ts"]
        _push_copy(conn, vehicle_id, notice_id, row["kind"], title or "", summary or "", opener, event_ts)
    return {"ok": True, "action": action, "id": notice_id}


def _merge(conn, vehicle_id: str, row, body: dict, title: str | None, summary: str | None) -> dict[str, Any]:
    into = _notice_id(body.get("into"), conn, vehicle_id)
    if into == row["id"]:
        raise ValueError("into is not allowed")
    if row["delivery"] == "merged" and row["merged_into"] == into:
        return {"ok": True, "action": "merge", "id": row["id"], "into": into, "already": True}
    if row["delivery"] != "pending":
        raise ValueError("not pending")
    target = _one(conn, vehicle_id, into)
    if target is None:
        raise KeyError("notification not found")
    with conn:
        cur = conn.execute(
            "UPDATE notifications SET delivery = 'merged', merged_into = ? "
            "WHERE vehicle_id = ? AND id = ? AND delivery = 'pending'",
            (into, vehicle_id, row["id"]),
        )
        if cur.rowcount != 1:
            raise ValueError("not pending")
        if title is not None or summary is not None:
            sets = []
            args: list[Any] = []
            if title is not None:
                sets.append("title = ?")
                args.append(title)
            if summary is not None:
                sets.append("summary = ?")
                sets.append("composed = 1")
                args.append(summary)
            args.extend([vehicle_id, into])
            conn.execute(
                f"UPDATE notifications SET {', '.join(sets)} WHERE vehicle_id = ? AND id = ?",
                args,
            )
    return {"ok": True, "action": "merge", "id": row["id"], "into": into}


def _create(conn, vehicle_id: str, body: dict, now: datetime, opener) -> dict[str, Any]:
    from web.notify import PREF_TITLE, _href, _now_iso, enabled

    kind = str(body.get("kind") or "").strip()
    if kind not in PREF_TITLE:
        raise ValueError("kind is not allowed")
    if not enabled(conn, vehicle_id, kind):
        raise ValueError("kind is off")
    title = _sentence(body.get("title"), "title", True)
    summary = _sentence(body.get("summary"), "summary", True)
    notice_id = "bot:" + uuid.uuid4().hex[:16]
    when = now
    raw_when = str(body.get("event_ts") or "").strip()
    if raw_when:
        try:
            parsed = datetime.fromisoformat(raw_when.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            when = parsed.astimezone(timezone.utc)
        except ValueError:
            raise ValueError("event_ts is not allowed") from None
    with conn:
        conn.execute(
            "INSERT INTO notifications "
            "(vehicle_id, id, ts, kind, title, body, title_en, body_en, href, details, "
            "summary, composed, delivery, push_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'sent', ?)",
            (
                vehicle_id, notice_id, _now_iso(when), kind, title, summary,
                title, summary, _href(kind), "[]", summary, _now_iso(now),
            ),
        )
    _push_copy(conn, vehicle_id, notice_id, kind, title or "", summary or "", opener, _now_iso(when))
    return {"ok": True, "action": "create", "id": notice_id}


def flush_pending(
    conn: sqlite3.Connection,
    vehicle_id: str,
    now: datetime | None = None,
    opener: Opener | None = None,
) -> int:
    """Push the existing sentence when the bot has not answered for 10 minutes."""
    from web.notify import ensure_schema, _now_iso

    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = _now_iso(now - PENDING_AFTER)
    due = _rows(conn, vehicle_id, "delivery = 'pending' AND ts <= ? ORDER BY ts, id", (cutoff,))
    sent = 0
    from web.notify import enabled

    for row in due:
        if not enabled(conn, vehicle_id, row["kind"]):
            with conn:
                conn.execute(
                    "UPDATE notifications SET delivery = 'skipped', push_ts = ? "
                    "WHERE vehicle_id = ? AND id = ? AND delivery = 'pending'",
                    (_now_iso(now), vehicle_id, row["id"]),
                )
            continue
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET delivery = 'sent', push_ts = ? "
                "WHERE vehicle_id = ? AND id = ? AND delivery = 'pending'",
                (_now_iso(now), vehicle_id, row["id"]),
            )
            claimed = cur.rowcount == 1
        if not claimed:
            continue
        from web.notice_view import present

        details = json.loads(row["details"]) if row["details"] else []
        view = present({
            "id": row["id"], "ts": row["ts"], "kind": row["kind"],
            "title": row["title"], "body": row["body"] or "", "details": details,
        })
        _push_copy(
            conn, vehicle_id, row["id"], row["kind"], view["title"], view["push_body"], opener, view["event_ts"],
        )
        sent += 1
    return sent
