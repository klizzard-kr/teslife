"""Period views for the 주행 / 충전 / 차계부 menus.

Every view takes a KST day range (from / to, both inclusive, either may be missing) or
`recent` (the newest N records regardless of date).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))
_TRIP_KST = "date(start_ts, '+9 hours')"
_CHARGE_TS = "CASE WHEN end_ts IS NULL OR end_ts = '' THEN start_ts ELSE end_ts END"
_CHARGE_KST = f"date({_CHARGE_TS}, '+9 hours')"
SUPERCHARGER = "Supercharger"


def _kst_day(ts: str | None) -> str | None:
    if not ts:
        return None
    s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).date().isoformat()


def _range_sql(expr: str, date_from: str | None, date_to: str | None) -> tuple[str, list[Any]]:
    sql, params = "", []
    if date_from:
        sql += f" AND {expr} >= ?"
        params.append(date_from)
    if date_to:
        sql += f" AND {expr} <= ?"
        params.append(date_to)
    return sql, params


def first_days(conn: sqlite3.Connection, vid: str) -> dict[str, str | None]:
    """First KST day with a record, per menu (the period picker's lower bound)."""
    out: dict[str, str | None] = {}
    for key, sql in (
        ("trips", "SELECT MIN(start_ts) FROM trips WHERE vehicle_id = ?"),
        ("charges", "SELECT MIN(start_ts) FROM charges WHERE vehicle_id = ?"),
    ):
        try:
            out[key] = _kst_day(conn.execute(sql, (vid,)).fetchone()[0])
        except sqlite3.OperationalError:
            out[key] = None
    try:
        out["ledger"] = conn.execute("SELECT MIN(date) FROM ledger WHERE vehicle_id = ?", (vid,)).fetchone()[0]
    except sqlite3.OperationalError:
        out["ledger"] = None
    return out


def record_months(conn: sqlite3.Connection, vid: str) -> dict[str, list[str]]:
    """KST months (YYYY-MM, newest first) that have records, per menu; the period sheet offers
    only these months and their years. Trips count by start, charges by end (like the lists)."""
    out: dict[str, list[str]] = {}
    for key, sql in (
        ("trips", "SELECT DISTINCT strftime('%Y-%m', start_ts, '+9 hours') AS m FROM trips WHERE vehicle_id = ? ORDER BY m DESC"),
        ("charges", f"SELECT DISTINCT strftime('%Y-%m', {_CHARGE_TS}, '+9 hours') AS m FROM charges WHERE vehicle_id = ? ORDER BY m DESC"),
        ("ledger", "SELECT DISTINCT substr(date, 1, 7) AS m FROM ledger WHERE vehicle_id = ? ORDER BY m DESC"),
    ):
        try:
            out[key] = [r[0] for r in conn.execute(sql, (vid,)).fetchall() if r[0]]
        except sqlite3.OperationalError:
            out[key] = []
    return out


def _recent_trip_starts(conn: sqlite3.Connection, vid: str, n: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT start_ts FROM trips WHERE vehicle_id = ? ORDER BY start_ts DESC LIMIT ?", (vid, int(n))).fetchall()]


_TRIP_AGG = """
SELECT COUNT(*),
       COALESCE(SUM(km), 0),
       COALESCE(SUM(duration_s), 0),
       COALESCE(SUM(CASE WHEN kwh IS NOT NULL AND km >= 0.1 THEN kwh END), 0),
       COALESCE(SUM(CASE WHEN kwh IS NOT NULL AND km >= 0.1 THEN km END), 0),
       SUM(CASE WHEN kwh IS NOT NULL AND km >= 0.1 THEN 1 ELSE 0 END),
       SUM(CASE WHEN regen_kwh IS NOT NULL AND kwh IS NOT NULL THEN regen_kwh END),
       SUM(CASE WHEN regen_kwh IS NOT NULL AND kwh IS NOT NULL THEN kwh END)
"""


def _parked_kwh(conn: sqlite3.Connection, vid: str, where: str | None, params: list[Any]) -> float | None:
    if where is None:
        return None
    try:
        return float(conn.execute(
            f"SELECT COALESCE(SUM(kwh), 0) FROM parked_usage WHERE vehicle_id = ?{where}",
            [vid, *params]).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return None


def trips_summary(conn: sqlite3.Connection, vid: str, date_from: str | None = None, date_to: str | None = None,
                  recent: int | None = None) -> dict[str, Any]:
    """횟수, km, 운전 시간, 전비, 쓴 전력, 회생. 전비는 kWh가 있는 0.1 km 이상 주행만.

    주차 중 소모는 여기 두지 않는다. 충전 요약이 같은 기간으로 센다.
    """
    if recent:
        starts = _recent_trip_starts(conn, vid, recent)
        if starts:
            marks = ",".join("?" * len(starts))
            row = conn.execute(
                _TRIP_AGG + f" FROM trips WHERE vehicle_id = ? AND start_ts IN ({marks})",
                [vid, *starts]).fetchone()
            span = (_kst_day(min(starts)), _kst_day(max(starts)))
        else:
            row, span = (0, 0, 0, 0, 0, 0, None, None), (None, None)
    else:
        extra, params = _range_sql(_TRIP_KST, date_from, date_to)
        row = conn.execute(
            _TRIP_AGG + f" FROM trips WHERE vehicle_id = ?{extra}", [vid, *params]).fetchone()
        span = (date_from, date_to)
    n = int(row[0] or 0)
    km = float(row[1] or 0)
    secs = float(row[2] or 0)
    eff_kwh = float(row[3] or 0)
    eff_km = float(row[4] or 0)
    eff_n = int(row[5] or 0)
    regen = row[6]
    regen_base = row[7]
    return {
        "vehicle_id": vid, "from": span[0], "to": span[1], "recent": recent,
        "trip_count": n, "km": round(km, 2),
        "drive_minutes": round(secs / 60.0, 1),
        "kwh": round(eff_kwh, 3) if eff_n else None,
        "km_per_kwh": round(eff_km / eff_kwh, 2) if eff_n and eff_kwh > 0.01 else None,
        "regen_kwh": round(float(regen), 3) if regen is not None else None,
        "regen_ratio": round(float(regen) / float(regen_base), 3) if regen is not None and regen_base and float(regen_base) > 0.01 else None,
    }


def trips_monthly(conn: sqlite3.Connection, vid: str, date_from: str | None = None,
                  date_to: str | None = None) -> dict[str, Any]:
    extra, params = _range_sql(_TRIP_KST, date_from, date_to)
    rows = conn.execute(
        f"SELECT substr({_TRIP_KST}, 1, 7) AS m, COUNT(*), COALESCE(SUM(km), 0), COALESCE(SUM(kwh), 0), "
        f"COALESCE(SUM(CASE WHEN kwh IS NOT NULL AND km >= 0.1 THEN kwh END), 0), "
        f"COALESCE(SUM(CASE WHEN kwh IS NOT NULL AND km >= 0.1 THEN km END), 0) "
        f"FROM trips WHERE vehicle_id = ?{extra} GROUP BY m ORDER BY m DESC", [vid, *params]).fetchall()
    months = [{"month": r[0], "trip_count": int(r[1]), "km": round(float(r[2]), 2), "kwh": round(float(r[3]), 3),
               "km_per_kwh": round(float(r[5]) / float(r[4]), 2) if float(r[4]) > 0.01 else None} for r in rows]
    return {"vehicle_id": vid, "from": date_from, "to": date_to, "months": months}


def trips_trend(conn: sqlite3.Connection, vid: str, date_from: str | None, date_to: str | None,
                recent: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """운전 습관 (summary over the range) and 전비 추이 (per day, week or month by the range length)."""
    from web.driving import _utc, summarize

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(KST).date()
    if recent:
        starts = _recent_trip_starts(conn, vid, recent)
        date_from = _kst_day(min(starts)) if starts else today.isoformat()
        date_to = today.isoformat()
    if not date_from:
        first = first_days(conn, vid).get("trips")
        date_from = first or today.isoformat()
    if not date_to:
        date_to = today.isoformat()
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to) + timedelta(days=1)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT start_ts, end_ts, km, kwh, duration_s, regen_kwh, max_speed, hard_accel, "
            "hard_brake, hard_turn FROM trips WHERE vehicle_id = ? AND start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts", (vid, _utc(start), _utc(end)),
        ).fetchall()
    finally:
        conn.row_factory = None
    if recent:
        keep = set(_recent_trip_starts(conn, vid, recent))
        rows = [r for r in rows if r["start_ts"] in keep]
    days = (end - start).days
    unit = "day" if days <= 31 else ("week" if days <= 120 else "month")
    buckets: dict[str, list[Any]] = {}
    for r in rows:
        d = date.fromisoformat(_kst_day(r["start_ts"]))  # type: ignore[arg-type]
        if unit == "week":
            d = d - timedelta(days=d.weekday())
        elif unit == "month":
            d = d.replace(day=1)
        buckets.setdefault(d.isoformat(), []).append(r)
    trend = []
    for key in sorted(buckets):
        sm = summarize(buckets[key])
        if sm["km_per_kwh"] is None:
            continue
        trend.append({"from": key, "km": sm["km"], "km_per_kwh": sm["km_per_kwh"], "trips": sm["trip_count"]})
    return {"vehicle_id": vid, "from": date_from, "to": date_to, "unit": unit, "days": days,
            **summarize(rows), "trend": trend}


def _charge_rows(conn: sqlite3.Connection, vid: str, date_from: str | None, date_to: str | None,
                 recent: int | None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        if recent:
            return conn.execute(
                "SELECT c.*, m.total_krw AS paid, m.kwh_billed AS billed, m.discount_krw AS discount FROM charges c LEFT JOIN charge_meta m "
                "ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts WHERE c.vehicle_id = ? "
                "ORDER BY c.start_ts DESC LIMIT ?", (vid, int(recent))).fetchall()
        extra, params = _range_sql(_CHARGE_KST.replace("end_ts", "c.end_ts").replace("start_ts", "c.start_ts"),
                                   date_from, date_to)
        return conn.execute(
            "SELECT c.*, m.total_krw AS paid, m.kwh_billed AS billed, m.discount_krw AS discount FROM charges c LEFT JOIN charge_meta m "
            f"ON m.vehicle_id = c.vehicle_id AND m.charge_id = c.start_ts WHERE c.vehicle_id = ?{extra} "
            "ORDER BY c.start_ts DESC", [vid, *params]).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.row_factory = None


def _kind3(row: Any) -> str:
    kind = row["kind"]
    fc = row["fast_charger"] if "fast_charger" in row.keys() else None
    if kind == "DC":
        return "SC" if fc == SUPERCHARGER else "DC"
    return "AC"


def charges_summary(conn: sqlite3.Connection, vid: str, date_from: str | None = None,
                    date_to: str | None = None, recent: int | None = None) -> dict[str, Any]:
    """횟수, 비용, 평균 단가, 충전효율. 방식별 kWh는 by_kind에만 두고 주차 중 소모는 여기 두지 않는다.

    평균 단가는 결제액(total_krw, 0원 포함)과 청구 kWh가 둘 다 있는 충전만
    sum(결제액 + 할인) / sum(청구 kWh). 건별 단가와 같다. 금액이 비면 뺀다.
    충전효율은 charger_kwh·battery_kwh·loss_kwh가 모두 있는 충전만
    sum(battery_kwh) / sum(충전기가 보낸 양). 보낸 양은 청구 kWh, 없으면 차가 받은 양
    에 과거 청구 비율을 곱한 값이다. loss_pct(sum(loss_kwh) / sum(charger_kwh))는
    그대로 둔다. kwh_added만 있는 충전은 분모에 넣지 않는다.
    직류는 이 차이가 저장되지 않아 빠진다. 값이 없는 기간은 0이 아니라 건수 0이다.
    """
    from ledger import charging_cost

    rows = _charge_rows(conn, vid, date_from, date_to, recent)
    out = {"AC": {"count": 0, "kwh": 0.0}, "DC": {"count": 0, "kwh": 0.0}, "SC": {"count": 0, "kwh": 0.0}}
    cost = est = 0.0
    no_cost = 0
    pay_krw = pay_kwh = 0.0
    pay_n = 0
    loss_kwh = loss_base = battery_sum = eff_base = 0.0
    loss_n = 0
    from web.tariff import _ratios, charger_sent, unit_price
    ratios = _ratios(conn, vid)
    metas: dict[str, dict[str, Any]] = {}
    conn.row_factory = sqlite3.Row
    try:
        for m in conn.execute("SELECT * FROM charge_meta WHERE vehicle_id = ?", (vid,)).fetchall():
            metas[m["charge_id"]] = {k: m[k] for k in m.keys()}
    except sqlite3.OperationalError:
        pass
    finally:
        conn.row_factory = None
    for r in rows:
        k = _kind3(r)
        out[k]["count"] += 1
        out[k]["kwh"] += float(r["kwh_added"] or 0)
        meta = metas.get(r["start_ts"])
        try:
            c, e = charging_cost(conn, vid, r["kwh_added"], meta)
        except sqlite3.Error:
            c, e = None, False
        if c is None and meta and meta.get("operator"):
            charge = {col: r[col] for col in r.keys()}
            sent, _sent_est = charger_sent(charge, meta, ratios)
            price, _learned = unit_price(conn, vid, meta.get("operator"), charge, _kst_day(r["end_ts"] or r["start_ts"]))
            if sent is not None and price is not None:
                c, e = round(sent * price), True
        if c is None:
            no_cost += 1
        else:
            cost += float(c)
            if e:
                est += float(c)
        if meta is not None and meta.get("total_krw") is not None and meta.get("kwh_billed") not in (None, ""):
            billed = float(meta["kwh_billed"])
            if billed > 0:
                pay_krw += float(meta["total_krw"]) + float(meta.get("discount_krw") or 0)
                pay_kwh += billed
                pay_n += 1
        keys = r.keys()
        charger = r["charger_kwh"] if "charger_kwh" in keys else None
        battery = r["battery_kwh"] if "battery_kwh" in keys else None
        lost = r["loss_kwh"] if "loss_kwh" in keys else None
        if charger is not None and battery is not None and lost is not None and float(charger) > 0:
            loss_base += float(charger)
            loss_kwh += float(lost)
            battery_sum += float(battery)
            loss_n += 1
            charge = {k: r[k] for k in keys}
            sent, _estimated = charger_sent(charge, meta, ratios)
            eff_base += float(sent) if sent else float(charger)
    for v in out.values():
        v["kwh"] = round(v["kwh"], 2)
    span = (date_from, date_to)
    if recent and rows:
        span = (_kst_day(rows[-1]["start_ts"]), _kst_day(rows[0]["start_ts"]))
    if recent and rows:
        parked_where, parked_params = " AND start_ts >= ?", [min(r["start_ts"] for r in rows)]
    elif recent:
        parked_where, parked_params = None, []
    else:
        parked_where, parked_params = _range_sql("date(start_ts, '+9 hours')", date_from, date_to)
    return {
        "vehicle_id": vid, "from": span[0], "to": span[1], "recent": recent,
        "charge_count": len(rows),
        "kwh_ac": out["AC"]["kwh"], "kwh_dc": round(out["DC"]["kwh"] + out["SC"]["kwh"], 2),
        "kwh": round(sum(v["kwh"] for v in out.values()), 2),
        "by_kind": out,
        "cost_krw": round(cost) if rows and len(rows) > no_cost else None,
        "cost_estimated_krw": round(est),
        "without_cost": no_cost,
        "unit_krw_per_kwh": round(pay_krw / pay_kwh, 1) if pay_n and pay_kwh > 0 else None,
        "unit_n": pay_n,
        "loss_pct": round(loss_kwh / loss_base * 100.0, 1) if loss_n and loss_base > 0 else None,
        "efficiency_pct": round(battery_sum / eff_base * 100.0, 1) if loss_n and eff_base > 0 else None,
        "loss_n": loss_n,
        "parked_kwh": _parked_kwh(conn, vid, parked_where, parked_params),
    }


def charge_trend(conn: sqlite3.Connection, vid: str, date_from: str | None = None,
                 date_to: str | None = None, recent: int | None = None) -> dict[str, Any]:
    """Unit price (원/kWh, paid + discount) per charge, oldest first; only charges with both amount and billed kWh."""
    rows = _charge_rows(conn, vid, date_from, date_to, recent)
    pts = []
    for r in reversed(rows):
        paid, billed = r["paid"], r["billed"]
        if paid is None or not billed or float(billed) <= 0:
            continue
        pts.append({"charge_id": r["start_ts"], "day": _kst_day(r["start_ts"]), "kind": _kind3(r),
                    "krw_per_kwh": round((float(paid) + float(r["discount"] or 0)) / float(billed), 1)})
    return {"vehicle_id": vid, "points": pts}


def charges_monthly(conn: sqlite3.Connection, vid: str, date_from: str | None = None,
                    date_to: str | None = None) -> dict[str, Any]:
    rows = _charge_rows(conn, vid, date_from, date_to, None)
    months: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = r["end_ts"] or r["start_ts"]
        m = (_kst_day(ts) or "")[:7]
        b = months.setdefault(m, {"month": m, "charge_count": 0, "kwh": 0.0, "krw": 0.0})
        b["charge_count"] += 1
        b["kwh"] += float(r["kwh_added"] or 0)
        b["krw"] += float(r["paid"] or 0)
    items = sorted(months.values(), key=lambda b: b["month"], reverse=True)
    for b in items:
        b["kwh"] = round(b["kwh"], 2)
        b["krw"] = round(b["krw"])
    return {"vehicle_id": vid, "from": date_from, "to": date_to, "months": items}


def charge_places(items: list[dict[str, Any]], merge_m: float = 100.0) -> list[dict[str, Any]]:
    """Charge list items (with lat/lon/place) grouped into map dots: one dot per spot with a count."""
    from process.geo import distance_m

    dots: list[dict[str, Any]] = []
    for it in items:
        lat, lon = it.get("lat"), it.get("lon")
        if lat is None or lon is None:
            continue
        kind = "SC" if it.get("kind_label") == "슈퍼차저" else ("DC" if it.get("kind") == "DC" else "AC")
        hit = None
        for d in dots:
            if distance_m((lat, lon), (d["lat"], d["lon"])) <= merge_m:
                hit = d
                break
        if hit is None:
            hit = {"lat": lat, "lon": lon, "count": 0, "kinds": {}, "place": it.get("place"),
                   "charge_ids": [], "estimated": True, "manual": False}
            dots.append(hit)
        hit["count"] += 1
        hit["kinds"][kind] = hit["kinds"].get(kind, 0) + 1
        hit["charge_ids"].append(it["charge_id"])
        if it.get("loc_source") in ("car", "manual", "station"):
            hit["estimated"] = False
        if it.get("loc_source") == "manual":
            hit["manual"] = True
        if not hit.get("place") and it.get("place"):
            hit["place"] = it.get("place")
    for d in dots:
        d["kind"] = max(d["kinds"], key=lambda k: d["kinds"][k])
    return dots
