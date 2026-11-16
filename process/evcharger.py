"""Match a charge to a public charger (Korea Environment Corporation, phase 1).

The catalog is getChargerInfo and the live state is getChargerStatus, both XML.
EV_CHARGER_API_KEY is the portal's already-encoded key and is appended as-is.
With no key the lookup does nothing. Calls stay under 1,000 per Korea day by
caching a region for a week and a charger status for 20 minutes.

A saved choice is stored on the place and reused. Operator unit prices are
out of scope. Trip, route, ledger, and service rows are not modified, and an
operator the owner already typed is not replaced unless they pick a candidate.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from process.geo import distance_m

KST = timezone(timedelta(hours=9))
INFO_URL = "https://apis.data.go.kr/B552584/EvCharger/getChargerInfo"
STATUS_URL = "https://apis.data.go.kr/B552584/EvCharger/getChargerStatus"
DAILY_LIMIT = 1000
NEAR_M = 50.0
FAR_M = 100.0
PICK_M = 300.0  # edit-screen station list; auto match stays at FAR_M
KEEP_M = 1000.0  # catalog rows farther than this from a saved place are not stored
PAGE_ROWS = 9999
HIST_KEEP = timedelta(days=30)
HIST_CAP = 48
REMATCH_LOOKBACK = timedelta(days=30)
# 부천시 구 코드 (2024-01-01 재설치). 옛 통합 코드 41190은 구 조회가 비면 한 번 더 쓴다.
BUCHEON_GU = frozenset({"41192", "41194", "41196"})
BUCHEON_CITY = "41190"
CATALOG_TTL = timedelta(days=7)
STATUS_TTL = timedelta(minutes=20)
STATUS_WINDOW = timedelta(minutes=20)
LOOKBACK = timedelta(days=7)
POLL_RADIUS = timedelta(minutes=3)
POLL_GAP = timedelta(seconds=45)
POLLS_PER_WINDOW = 4
INFER_WINDOW = timedelta(minutes=5)
STAT_WAIT = "2"
STAT_CHARGING = "3"
PLACE_RADIUS_M = 150.0
STATUS_MATCH_TRIES = 3
STATUS_MATCH_GAP = timedelta(minutes=5)
STATUS_MATCH_WINDOW = timedelta(minutes=10)
METERS_PER_DEG = 111_320.0

AC_TYPES = {"02", "07", "08"}
DC_TYPES = {"01", "03", "04", "05", "06", "09", "10"}

# Administrative names only. Longest match wins, so 광주광역시 is not 광주시.
_REGION_NAMES = (
    ("서울특별시", "11"), ("서울", "11"), ("Seoul", "11"),
    ("부산광역시", "26"), ("부산", "26"), ("Busan", "26"),
    ("대구광역시", "27"), ("대구", "27"), ("Daegu", "27"),
    ("인천광역시", "28"), ("인천", "28"), ("Incheon", "28"),
    ("광주광역시", "29"), ("Gwangju", "29"),
    ("대전광역시", "30"), ("대전", "30"), ("Daejeon", "30"),
    ("울산광역시", "31"), ("울산", "31"), ("Ulsan", "31"),
    ("세종특별자치시", "36"), ("세종", "36"), ("Sejong", "36"),
    ("경기도", "41"), ("경기", "41"), ("Gyeonggi", "41"),
    ("강원특별자치도", "42"), ("강원도", "42"), ("강원", "42"),
    ("충청북도", "43"), ("충북", "43"),
    ("충청남도", "44"), ("충남", "44"),
    ("전북특별자치도", "45"), ("전라북도", "45"), ("전북", "45"),
    ("전라남도", "46"), ("전남", "46"),
    ("경상북도", "47"), ("경북", "47"),
    ("경상남도", "48"), ("경남", "48"),
    ("제주특별자치도", "50"), ("제주도", "50"), ("제주", "50"), ("Jeju", "50"),
    ("수원시", "41"), ("성남시", "41"), ("고양시", "41"), ("용인시", "41"),
    ("부천시", "41"), ("안산시", "41"), ("안양시", "41"), ("남양주시", "41"),
    ("화성시", "41"), ("평택시", "41"), ("의정부시", "41"), ("시흥시", "41"),
    ("파주시", "41"), ("김포시", "41"), ("광명시", "41"), ("광주시", "41"),
    ("군포시", "41"), ("하남시", "41"), ("오산시", "41"), ("이천시", "41"),
    ("춘천시", "42"), ("원주시", "42"), ("강릉시", "42"),
    ("청주시", "43"), ("충주시", "43"), ("천안시", "44"), ("아산시", "44"),
    ("전주시", "45"), ("군산시", "45"), ("익산시", "45"),
    ("목포시", "46"), ("여수시", "46"), ("순천시", "46"),
    ("포항시", "47"), ("경주시", "47"), ("구미시", "47"),
    ("창원시", "48"), ("김해시", "48"), ("진주시", "48"), ("양산시", "48"),
    ("제주시", "50"), ("서귀포시", "50"),
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ev_chargers (
    stat_id TEXT NOT NULL,
    chger_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    chger_type TEXT,
    output_kw REAL,
    busi_nm TEXT,
    stat_nm TEXT,
    zcode TEXT,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (stat_id, chger_id)
);
CREATE INDEX IF NOT EXISTS ev_chargers_lat ON ev_chargers(lat);

CREATE TABLE IF NOT EXISTS ev_region (
    zcode TEXT PRIMARY KEY,
    next_page INTEGER NOT NULL,
    total_count INTEGER,
    done INTEGER NOT NULL DEFAULT 0,
    fetched_ts TEXT
);

CREATE TABLE IF NOT EXISTS ev_status (
    stat_id TEXT NOT NULL,
    chger_id TEXT NOT NULL,
    stat TEXT,
    now_tsdt TEXT,
    last_tsdt TEXT,
    last_tedt TEXT,
    fetched_ts TEXT NOT NULL,
    PRIMARY KEY (stat_id, chger_id)
);

CREATE TABLE IF NOT EXISTS ev_api_calls (
    day TEXT PRIMARY KEY,
    n INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ev_status_hist (
    stat_id TEXT NOT NULL,
    chger_id TEXT NOT NULL,
    fetched_ts TEXT NOT NULL,
    stat TEXT,
    now_tsdt TEXT,
    last_tsdt TEXT,
    last_tedt TEXT,
    stat_upd_dt TEXT,
    PRIMARY KEY (stat_id, chger_id, fetched_ts)
);

CREATE TABLE IF NOT EXISTS charge_charger (
    vehicle_id TEXT NOT NULL,
    charge_id TEXT NOT NULL,
    stat_id TEXT,
    chger_id TEXT,
    busi_nm TEXT,
    output_kw REAL,
    chger_type TEXT,
    stat_nm TEXT,
    source TEXT NOT NULL,
    candidates_json TEXT,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, charge_id)
);

CREATE TABLE IF NOT EXISTS charger_place (
    vehicle_id TEXT NOT NULL,
    place_key TEXT NOT NULL,
    stat_id TEXT NOT NULL,
    chger_id TEXT NOT NULL,
    busi_nm TEXT,
    output_kw REAL,
    chger_type TEXT,
    stat_nm TEXT,
    PRIMARY KEY (vehicle_id, place_key)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ev_chargers)").fetchall()}
    if "addr" not in cols:
        conn.execute("ALTER TABLE ev_chargers ADD COLUMN addr TEXT")
    conn.commit()


def api_key() -> str:
    return os.environ.get("EV_CHARGER_API_KEY", "").strip()


def build_url(endpoint: str, key: str, params: dict[str, Any]) -> str:
    """Append the encoded portal key without quoting it again."""
    tail = "&".join(
        f"{name}={urllib.parse.quote(str(value), safe='')}"
        for name, value in params.items() if value is not None and value != ""
    )
    extra = f"&{tail}" if tail else ""
    return f"{endpoint}?serviceKey={key}{extra}"


# Districts that sit against a metropolitan box are listed first. A point in
# 부천 used to fall inside the old Seoul rectangle and the catalog request
# downloaded every Seoul charger.
# (lat0, lat1, lng0, lng1, zcode, zscode)
_DISTRICT_BOXES = (
    (37.450, 37.492, 126.730, 126.855, "41", "41194"),  # 경기 부천시 소사구
    (37.515, 37.560, 126.725, 126.830, "41", "41196"),  # 경기 부천시 오정구
    (37.488, 37.520, 126.730, 126.855, "41", "41192"),  # 경기 부천시 원미구
    (37.400, 37.495, 126.835, 126.920, "41", "41210"),  # 경기 광명시
)
# Cities before the provinces that contain them. Seoul's west edge stays east
# of 부천 (126.80); a miss there falls through to 경기도, not 서울.
_ZCODE_BOXES = (
    (37.43, 37.70, 126.80, 127.18, "11"),  # Seoul
    (35.05, 35.40, 128.90, 129.32, "26"),  # Busan
    (35.75, 36.05, 128.35, 128.80, "27"),  # Daegu
    (37.20, 37.75, 126.30, 126.78, "28"),  # Incheon
    (35.05, 35.25, 126.65, 126.95, "29"),  # Gwangju
    (36.20, 36.50, 127.25, 127.55, "30"),  # Daejeon
    (35.40, 35.75, 129.05, 129.50, "31"),  # Ulsan
    (36.42, 36.75, 127.15, 127.40, "36"),  # Sejong
    (33.10, 33.60, 126.15, 126.97, "50"),  # Jeju
    (37.00, 38.30, 126.40, 127.90, "41"),  # Gyeonggi
    (37.00, 38.65, 127.05, 129.40, "42"),  # Gangwon
    (36.20, 37.25, 127.25, 128.70, "43"),  # Chungbuk
    (35.95, 37.10, 126.10, 127.40, "44"),  # Chungnam
    (35.25, 36.20, 126.40, 127.90, "45"),  # Jeonbuk
    (34.20, 35.55, 125.90, 127.60, "46"),  # Jeonnam
    (35.55, 37.15, 128.00, 129.60, "47"),  # Gyeongbuk
    (34.55, 35.95, 127.50, 129.30, "48"),  # Gyeongnam
)


def _point(lat: float | None, lng: float | None) -> tuple[float, float] | None:
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def region_from_point(lat: float | None, lng: float | None, admin_name: str | None = None,
                      opener: Any | None = None) -> tuple[str, str | None] | None:
    """(zcode, zscode). A 시·군 name is tried first, then the offline boundary file."""
    point = _point(lat, lng)
    if point is None and not admin_name:
        return None
    from process.regions import zscode_at, zscode_from_name

    name = (admin_name or "").strip()
    if not name and opener is not None and point is not None:
        from process.naver_maps import area_name

        name = area_name(point[0], point[1], opener) or ""
    if name:
        named = zscode_from_name(name)
        if named and len(named) >= 5:
            return named[:2], named
    if point is None:
        return None
    code = zscode_at(point[0], point[1])
    if code and len(code) >= 5:
        return code[:2], code
    lat_f, lng_f = point
    for lat0, lat1, lng0, lng1, zcode in _ZCODE_BOXES:
        if lat0 <= lat_f <= lat1 and lng0 <= lng_f <= lng1:
            return zcode, None
    return None


def zcode_from_point(lat: float | None, lng: float | None) -> str | None:
    found = region_from_point(lat, lng)
    return found[0] if found else None


def zcode_from_text(text: str | None) -> str | None:
    if not text:
        return None
    best: tuple[tuple[int, int], str] | None = None
    for name, code in _REGION_NAMES:
        idx = text.find(name)
        if idx < 0:
            continue
        rank = (len(name), -idx)
        if best is None or rank > best[0]:
            best = (rank, code)
    return best[1] if best else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(raw: bytes) -> tuple[str, int, list[dict[str, str]]]:
    """Return (resultCode, totalCount, items). Empty code means the body was not the API."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "", 0, []
    code = ""
    total = 0
    items: list[dict[str, str]] = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "resultCode" and el.text:
            code = el.text.strip()
        elif name == "totalCount" and el.text and el.text.strip().isdigit():
            total = int(el.text.strip())
        elif name == "item":
            row = {}
            for child in list(el):
                row[_local(child.tag)] = (child.text or "").strip()
            if row:
                items.append(row)
    return code, total, items


def _coerce_items(node: Any) -> list[dict[str, Any]] | None:
    """items, items.item, a single object, or a list. None when this node is absent."""
    if node is None:
        return None
    if isinstance(node, list):
        return [row for row in node if isinstance(row, dict)]
    if isinstance(node, dict):
        if "item" in node:
            return _coerce_items(node.get("item")) or []
        if any(key in node for key in ("statId", "statNm", "chgerId")):
            return [node]
        return []
    return []


def parse_catalog(raw: bytes) -> tuple[str, int, list[dict[str, str]]]:
    """XML or JSON from getChargerInfo.

    dataType=JSON puts resultCode, totalCount, and items.item on the outer object.
    The older response.body shape is still accepted. A single item object and a list
    are both read. An error body is not treated as success.
    """
    text = raw.lstrip()
    if text.startswith(b"{") or text.startswith(b"["):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "", 0, []
        if not isinstance(payload, dict):
            return "", 0, []
        header = ((payload.get("response") or {}).get("header") or {}) if isinstance(payload.get("response"), dict) else {}
        body = ((payload.get("response") or {}).get("body") or {}) if isinstance(payload.get("response"), dict) else {}
        if not isinstance(header, dict):
            header = {}
        if not isinstance(body, dict):
            body = {}
        code = str(header.get("resultCode") or body.get("resultCode") or payload.get("resultCode") or "").strip()
        total_raw = body.get("totalCount")
        if total_raw in (None, ""):
            total_raw = payload.get("totalCount")
        try:
            total = int(total_raw or 0)
        except (TypeError, ValueError):
            total = 0
        found: list[dict[str, Any]] | None = None
        for node in (payload.get("items"), body.get("items")):
            rows = _coerce_items(node)
            if rows:
                found = rows
                break
        if found is None:
            found = _coerce_items(payload.get("items")) or _coerce_items(body.get("items")) or []
        out = []
        for item in found:
            if isinstance(item, dict):
                out.append({str(k): "" if v is None else str(v) for k, v in item.items()})
        return code, total, out
    return parse_xml(raw)


def _kw(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _type_code(text: str | None) -> str:
    raw = (text or "").strip()
    if raw.isdigit():
        return raw.zfill(2)
    return raw


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    value = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _day(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y-%m-%d")


def type_label(chger_type: str | None, output_kw: float | None = None) -> str:
    code = _type_code(chger_type)
    if code in DC_TYPES or (output_kw or 0) >= 40:
        return "급속"
    return "완속"


def _as_charger(row: dict[str, str]) -> dict[str, Any] | None:
    if (row.get("delYn") or "").upper() == "Y":
        return None
    stat_id = (row.get("statId") or "").strip()
    chger_id = (row.get("chgerId") or "").strip()
    if not stat_id or not chger_id:
        return None
    lat: float | None
    lng: float | None
    try:
        lat = float(row.get("lat") or "")
        lng = float(row.get("lng") or row.get("lon") or "")
    except ValueError:
        lat, lng = None, None
    if lat is not None and not (-90 <= lat <= 90 and lng is not None and -180 <= lng <= 180):
        return None
    return {
        "stat_id": stat_id,
        "chger_id": chger_id,
        "lat": lat,
        "lng": lng,
        "chger_type": _type_code(row.get("chgerType")),
        "output_kw": _kw(row.get("output")),
        "busi_nm": (row.get("busiNm") or row.get("bnm") or "").strip(),
        "stat_nm": (row.get("statNm") or "").strip(),
        "addr": (row.get("addr") or "").strip(),
        "zcode": (row.get("zcode") or "").strip(),
        "stat": (row.get("stat") or "").strip(),
        "now_ts": _when(row.get("nowTsdt")),
        "last_start": _when(row.get("lastTsdt")),
        "last_end": _when(row.get("lastTedt")),
        "stat_upd": _when(row.get("statUpdDt")),
        "distance_m": None,
    }


def select_band(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer chargers within 50 m. Use up to 100 m only when nothing is closer."""
    near = [item for item in items if item.get("distance_m") is not None and item["distance_m"] <= NEAR_M]
    if near:
        return near
    return [item for item in items if item.get("distance_m") is not None and item["distance_m"] <= FAR_M]


def _type_ok(item: dict[str, Any], kind: str) -> bool:
    code = item.get("chger_type") or ""
    output = item.get("output_kw")
    if kind == "DC":
        return code in DC_TYPES or (output or 0) >= 40
    return code in AC_TYPES or (output is not None and output <= 22)


def _power_ok(item: dict[str, Any], power_kw: float | None) -> bool:
    output = item.get("output_kw")
    if power_kw is None or output is None:
        return True
    return abs(float(output) - float(power_kw)) <= max(10.0, float(power_kw) * 0.4)


def select_type_power(items: list[dict[str, Any]], kind: str, power_kw: float | None) -> list[dict[str, Any]]:
    typed = [item for item in items if _type_ok(item, kind)]
    pool = typed or list(items)
    if power_kw is None:
        return pool
    powered = [item for item in pool if _power_ok(item, power_kw)]
    return powered or pool


def status_hit(item: dict[str, Any], start: datetime) -> bool:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now_ts = item.get("now_ts")
    if item.get("stat") == "3" and isinstance(now_ts, datetime) and abs(now_ts - start) <= STATUS_WINDOW:
        return True
    last_start = item.get("last_start")
    if isinstance(last_start, datetime) and abs(last_start - start) <= STATUS_WINDOW:
        return True
    last_end = item.get("last_end")
    if isinstance(last_start, datetime) and isinstance(last_end, datetime):
        if last_start - timedelta(minutes=5) <= start <= last_end + timedelta(minutes=5):
            return True
    return False


def decide(
    items: list[dict[str, Any]], kind: str, power_kw: float | None, start: datetime,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Narrow by distance, then output and type, then status around the start.

    One remaining charger is the match. Several are returned for the owner to pick.
    """
    pool = select_type_power(select_band(items), kind or "AC", power_kw)
    if not pool:
        return None, []
    informed = [item for item in pool if item.get("stat") or item.get("now_ts") or item.get("last_start")]
    hits = [item for item in pool if status_hit(item, start)]
    if informed and hits:
        pool = hits
    pool = sorted(pool, key=lambda item: item.get("distance_m") if item.get("distance_m") is not None else 1e9)
    if len(pool) == 1:
        return pool[0], []
    return None, pool[:8]


def _calls(conn: sqlite3.Connection, now: datetime) -> int:
    row = conn.execute("SELECT n FROM ev_api_calls WHERE day = ?", (_day(now),)).fetchone()
    return int(row[0]) if row else 0


def _spend(conn: sqlite3.Connection, now: datetime) -> bool:
    """Reserve one call. False when today's budget is already used."""
    if _calls(conn, now) >= DAILY_LIMIT:
        return False
    with conn:
        conn.execute(
            "INSERT INTO ev_api_calls (day, n) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET n = n + 1",
            (_day(now),),
        )
    return True


def _mark_exhausted(conn: sqlite3.Connection, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO ev_api_calls (day, n) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET n = excluded.n",
            (_day(now), DAILY_LIMIT),
        )


def _fetch(url: str, opener: Callable[..., Any] | None) -> bytes:
    """Read one catalog response. SSL drops and 504s are retried. The URL is not logged."""
    req = urllib.request.Request(url, headers={"Accept": "application/json, application/xml"})
    open_ = opener or urllib.request.urlopen
    last: Exception | None = None
    for _try in range(3):
        try:
            with open_(req, timeout=20) as res:
                return res.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    if last is not None:
        raise last
    raise OSError("catalog fetch failed")


def _near_anchors(lat: float, lng: float, anchors: list[tuple[float, float]]) -> bool:
    here = (lat, lng)
    return any(distance_m(here, point) <= KEEP_M for point in anchors)


def _status_tuple(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    def iso(value: datetime | None) -> str:
        return _iso(value) if value else ""
    return (
        item.get("stat") or "",
        iso(item.get("now_ts")),
        iso(item.get("last_start")),
        iso(item.get("last_end")),
        iso(item.get("stat_upd")),
    )


def _history_changed(conn: sqlite3.Connection, stat_id: str, chger_id: str, current: tuple[str, ...]) -> bool:
    try:
        row = conn.execute(
            "SELECT stat, now_tsdt, last_tsdt, last_tedt, stat_upd_dt FROM ev_status_hist "
            "WHERE stat_id = ? AND chger_id = ? ORDER BY fetched_ts DESC LIMIT 1",
            (stat_id, chger_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    if row is None:
        return True
    previous = tuple(value or "" for value in row)
    return previous != current


def _store_items(
    conn: sqlite3.Connection,
    items: list[dict[str, str]],
    now: datetime,
    anchors: list[tuple[float, float]] | None = None,
    history: bool = False,
) -> None:
    """Store catalog rows and, for a station poll, a history row when the state changed.

    anchors is for a province-wide fallback: only chargers within KEEP_M of those
    points are kept. A 시·군 catalog passes anchors=None and keeps every charger
    in that 시·군. History rows are not written for a catalog (history=False).
    """
    ts = _iso(now)
    rows = []
    status_rows = []
    hist_rows = []
    for raw in items:
        item = _as_charger(raw)
        if item is None:
            continue
        if anchors is not None:
            if item["lat"] is None or item["lng"] is None:
                continue
            if not _near_anchors(item["lat"], item["lng"], anchors):
                continue
        if item["lat"] is not None and item["lng"] is not None:
            rows.append((
                item["stat_id"], item["chger_id"], item["lat"], item["lng"], item["chger_type"],
                item["output_kw"], item["busi_nm"], item["stat_nm"], item.get("addr") or "",
                item["zcode"], ts,
            ))
        if item["stat"] or item["now_ts"] or item["last_start"] or item["last_end"] or item.get("stat_upd"):
            status_rows.append((
                item["stat_id"], item["chger_id"], item["stat"],
                _iso(item["now_ts"]) if item["now_ts"] else None,
                _iso(item["last_start"]) if item["last_start"] else None,
                _iso(item["last_end"]) if item["last_end"] else None,
                ts,
            ))
        if history and _history_changed(conn, item["stat_id"], item["chger_id"], _status_tuple(item)):
            hist_rows.append((
                item["stat_id"], item["chger_id"], ts, item["stat"] or None,
                _iso(item["now_ts"]) if item["now_ts"] else None,
                _iso(item["last_start"]) if item["last_start"] else None,
                _iso(item["last_end"]) if item["last_end"] else None,
                _iso(item["stat_upd"]) if item["stat_upd"] else None,
            ))
    if not rows and not status_rows and not hist_rows:
        return
    with conn:
        if rows:
            conn.executemany(
                "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, addr, zcode, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(stat_id, chger_id) DO UPDATE SET "
                "lat = excluded.lat, lng = excluded.lng, chger_type = excluded.chger_type, "
                "output_kw = excluded.output_kw, busi_nm = excluded.busi_nm, stat_nm = excluded.stat_nm, "
                "addr = excluded.addr, zcode = excluded.zcode, updated_ts = excluded.updated_ts",
                rows,
            )
        if status_rows:
            conn.executemany(
                "INSERT INTO ev_status (stat_id, chger_id, stat, now_tsdt, last_tsdt, last_tedt, fetched_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(stat_id, chger_id) DO UPDATE SET "
                "stat = excluded.stat, now_tsdt = excluded.now_tsdt, last_tsdt = excluded.last_tsdt, "
                "last_tedt = excluded.last_tedt, fetched_ts = excluded.fetched_ts",
                status_rows,
            )
        if hist_rows:
            conn.executemany(
                "INSERT INTO ev_status_hist (stat_id, chger_id, fetched_ts, stat, now_tsdt, last_tsdt, last_tedt, stat_upd_dt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(stat_id, chger_id, fetched_ts) DO UPDATE SET "
                "stat = excluded.stat, now_tsdt = excluded.now_tsdt, last_tsdt = excluded.last_tsdt, "
                "last_tedt = excluded.last_tedt, stat_upd_dt = excluded.stat_upd_dt",
                hist_rows,
            )


def _region_row(conn: sqlite3.Connection, zcode: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM ev_region WHERE zcode = ?", (zcode,)).fetchone()


def _region_stale(row: sqlite3.Row | None, now: datetime) -> bool:
    if row is None or not row["done"]:
        return True
    if not row["fetched_ts"]:
        return True
    try:
        return now - _parse_iso(row["fetched_ts"]) > CATALOG_TTL
    except ValueError:
        return True


def _region_key(zcode: str, zscode: str | None) -> str:
    return zcode if not zscode else f"{zcode}:{zscode}"


def _fetch_info_page(
    conn: sqlite3.Connection,
    zcode: str,
    now: datetime,
    opener: Callable[..., Any] | None,
    zscode: str | None = None,
    anchors: list[tuple[float, float]] | None = None,
) -> int:
    """Every page of one 시·군. Done only after the last page, so a large 시·도 does not stop at page 2."""
    key = _region_key(zcode, zscode)
    row = _region_row(conn, key)
    if row is not None and row["done"] and _region_stale(row, now):
        with conn:
            conn.execute("UPDATE ev_region SET next_page = 1, done = 0 WHERE zcode = ?", (key,))
        page = 1
    else:
        page = int(row["next_page"]) if row else 1
    calls = 0
    while _calls(conn, now) < DAILY_LIMIT:
        if not _spend(conn, now):
            return calls
        params: dict[str, Any] = {
            "pageNo": page, "numOfRows": PAGE_ROWS, "zcode": zcode, "dataType": "JSON",
        }
        if zscode:
            params["zscode"] = zscode
        try:
            raw = _fetch(build_url(INFO_URL, api_key(), params), opener)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return calls + 1
        calls += 1
        code, total, items = parse_catalog(raw)
        if code == "22":
            _mark_exhausted(conn, now)
            return calls
        if code != "00":
            return calls
        # A 시·군 response is the whole district. Anchors would drop every charger
        # more than KEEP_M from the car and still mark the district done.
        _store_items(conn, items, now, anchors=None if zscode else anchors, history=False)
        # totalCount can be set while items failed to parse. That must not finish the region.
        if total > 0 and not items:
            with conn:
                conn.execute(
                    "INSERT INTO ev_region (zcode, next_page, total_count, done, fetched_ts) VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(zcode) DO UPDATE SET next_page = excluded.next_page, total_count = excluded.total_count, "
                    "done = 0, fetched_ts = excluded.fetched_ts",
                    (key, page, total, _iso(now)),
                )
            return calls
        done = 1 if not items or page * PAGE_ROWS >= max(total, len(items)) else 0
        with conn:
            conn.execute(
                "INSERT INTO ev_region (zcode, next_page, total_count, done, fetched_ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(zcode) DO UPDATE SET next_page = excluded.next_page, total_count = excluded.total_count, "
                "done = excluded.done, fetched_ts = excluded.fetched_ts",
                (key, page + 1, total, done, _iso(now)),
            )
        if done and zscode:
            _meta_set(conn, _catalog_version_key(key), "1")
        if done:
            return calls
        page += 1
    return calls


def _fetch_status(
    conn: sqlite3.Connection, stat_id: str, now: datetime, opener: Callable[..., Any] | None,
) -> int:
    if not _spend(conn, now):
        return 0
    url = build_url(STATUS_URL, api_key(), {
        "pageNo": 1, "numOfRows": 100, "statId": stat_id, "dataType": "XML",
    })
    try:
        raw = _fetch(url, opener)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 1
    code, _total, items = parse_xml(raw)
    if code == "22":
        _mark_exhausted(conn, now)
        return 1
    if code == "00":
        _store_items(conn, items, now, history=False)
    return 1


def _box(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / METERS_PER_DEG
    scale = max(0.2, math.cos(math.radians(lat)))
    dlng = radius_m / (METERS_PER_DEG * scale)
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def _nearby(conn: sqlite3.Connection, lat: float, lng: float, now: datetime, radius: float = FAR_M) -> list[dict[str, Any]]:
    lo_lat, hi_lat, lo_lng, hi_lng = _box(lat, lng, radius)
    try:
        rows = conn.execute(
            "SELECT c.stat_id, c.chger_id, c.lat, c.lng, c.chger_type, c.output_kw, c.busi_nm, c.stat_nm, c.addr, "
            "s.stat, s.now_tsdt, s.last_tsdt, s.last_tedt, s.fetched_ts "
            "FROM ev_chargers c LEFT JOIN ev_status s ON s.stat_id = c.stat_id AND s.chger_id = c.chger_id "
            "WHERE c.lat BETWEEN ? AND ? AND c.lng BETWEEN ? AND ?",
            (lo_lat, hi_lat, lo_lng, hi_lng),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for stat_id, chger_id, clat, clng, chger_type, output_kw, busi_nm, stat_nm, addr, stat, now_ts, last_ts, last_te, fetched in rows:
        fresh = False
        if fetched:
            try:
                fresh = now - _parse_iso(fetched) <= STATUS_TTL
            except ValueError:
                fresh = False
        item = {
            "stat_id": stat_id,
            "chger_id": chger_id,
            "lat": clat,
            "lng": clng,
            "chger_type": chger_type or "",
            "output_kw": output_kw,
            "busi_nm": busi_nm or "",
            "stat_nm": stat_nm or "",
            "addr": addr or "",
            "distance_m": distance_m((lat, lng), (clat, clng)),
            "stat": stat if fresh else "",
            "now_ts": _parse_iso(now_ts) if fresh and now_ts else None,
            "last_start": _parse_iso(last_ts) if fresh and last_ts else None,
            "last_end": _parse_iso(last_te) if fresh and last_te else None,
        }
        if item["distance_m"] <= radius:
            out.append(item)
    return out


def _candidate_json(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "stat_id": item["stat_id"],
        "chger_id": item["chger_id"],
        "busi_nm": item.get("busi_nm") or "",
        "stat_nm": item.get("stat_nm") or "",
        "output_kw": item.get("output_kw"),
        "chger_type": item.get("chger_type") or "",
        "type_label": type_label(item.get("chger_type"), item.get("output_kw")),
        "distance_m": round(float(item["distance_m"]), 1) if item.get("distance_m") is not None else None,
    }


def _set_operator(conn: sqlite3.Connection, vid: str, charge_id: str, name: str, force: bool) -> None:
    if not name:
        return
    if force:
        sql = (
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET operator = excluded.operator"
        )
    else:
        sql = (
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator) VALUES (?, ?, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET operator = excluded.operator "
            "WHERE charge_meta.operator IS NULL OR TRIM(charge_meta.operator) = ''"
        )
    conn.execute(sql, (vid, charge_id, name))


def _place_keys(conn: sqlite3.Connection, vid: str, lat: float, lng: float) -> list[str]:
    keys = []
    try:
        rows = conn.execute(
            "SELECT id, lat, lon FROM stations WHERE vehicle_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    best = None
    for sid, slat, slng in rows:
        dist = distance_m((lat, lng), (slat, slng))
        if dist <= PLACE_RADIUS_M and (best is None or dist < best[0]):
            best = (dist, sid)
    if best:
        keys.append(f"station:{best[1]}")
    from process.geocode import cache_key
    keys.append("cell:" + cache_key(lat, lng))
    return keys


def _save_place(conn: sqlite3.Connection, vid: str, lat: float, lng: float, item: dict[str, Any]) -> None:
    keys = _place_keys(conn, vid, lat, lng)
    with conn:
        for key in keys:
            conn.execute(
                "INSERT INTO charger_place (vehicle_id, place_key, stat_id, chger_id, busi_nm, output_kw, chger_type, stat_nm) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(vehicle_id, place_key) DO UPDATE SET "
                "stat_id = excluded.stat_id, chger_id = excluded.chger_id, busi_nm = excluded.busi_nm, "
                "output_kw = excluded.output_kw, chger_type = excluded.chger_type, stat_nm = excluded.stat_nm",
                (vid, key, item["stat_id"], item["chger_id"], item.get("busi_nm"), item.get("output_kw"),
                 item.get("chger_type"), item.get("stat_nm")),
            )


def _write_choice(
    conn: sqlite3.Connection, vid: str, charge_id: str, item: dict[str, Any],
    source: str, lat: float, lng: float, force_operator: bool,
) -> None:
    now = _iso(datetime.now(timezone.utc))
    with conn:
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id, chger_id, busi_nm, output_kw, chger_type, "
            "stat_nm, source, candidates_json, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
            "stat_id = excluded.stat_id, chger_id = excluded.chger_id, busi_nm = excluded.busi_nm, "
            "output_kw = excluded.output_kw, chger_type = excluded.chger_type, stat_nm = excluded.stat_nm, "
            "source = excluded.source, candidates_json = NULL, updated_ts = excluded.updated_ts",
            (vid, charge_id, item["stat_id"], item["chger_id"], item.get("busi_nm"), item.get("output_kw"),
             item.get("chger_type"), item.get("stat_nm"), source, now),
        )
        _set_operator(conn, vid, charge_id, item.get("busi_nm") or "", force_operator)
        _remember_station(conn, vid, charge_id, item)


def _remember_station(conn: sqlite3.Connection, vid: str, charge_id: str, item: dict[str, Any]) -> None:
    """Store the matched station name and address. The place text is not touched."""
    name = (item.get("stat_nm") or "").strip()
    addr = (item.get("addr") or "").strip()
    if not name and not addr:
        return
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(charge_meta)").fetchall()}
    except sqlite3.OperationalError:
        return
    if "station_addr" not in cols:
        return
    conn.execute(
        "INSERT INTO charge_meta (vehicle_id, charge_id, station_name, station_addr) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
        "station_name = CASE WHEN charge_meta.station_name IS NULL OR TRIM(charge_meta.station_name) = '' "
        "THEN excluded.station_name ELSE charge_meta.station_name END, "
        "station_addr = CASE WHEN charge_meta.station_addr IS NULL OR TRIM(charge_meta.station_addr) = '' "
        "THEN excluded.station_addr ELSE charge_meta.station_addr END",
        (vid, charge_id, name or None, addr or None),
    )


def _write_pending(conn: sqlite3.Connection, vid: str, charge_id: str, items: list[dict[str, Any]], now: datetime) -> None:
    payload = json.dumps([_candidate_json(item) for item in items], ensure_ascii=False)
    with conn:
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, source, candidates_json, updated_ts) "
            "VALUES (?, ?, 'pending', ?, ?) "
            "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
            "source = 'pending', candidates_json = excluded.candidates_json, updated_ts = excluded.updated_ts, "
            "stat_id = NULL, chger_id = NULL, busi_nm = NULL, output_kw = NULL, chger_type = NULL, stat_nm = NULL",
            (vid, charge_id, payload, _iso(now)),
        )


def _location_text(conn: sqlite3.Connection, vid: str, charge_id: str) -> str:
    try:
        row = conn.execute(
            "SELECT location_text FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0] or "").strip() if row else ""


def _pin_source(conn: sqlite3.Connection, vid: str, charge_id: str) -> str:
    try:
        row = conn.execute(
            "SELECT source FROM charge_locations WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0] or "") if row else ""


def _station_auto_text(name: Any, addr: Any) -> str:
    return " ".join(part for part in (str(name or "").strip(), str(addr or "").strip()) if part)


def _inherit_place(
    conn: sqlite3.Connection, vid: str, charge_id: str, lat: float, lng: float,
    stat_id: str | None,
) -> str | None:
    """A place the owner saved at this spot. A station name plus address is not one."""
    try:
        rows = conn.execute(
            "SELECT m.charge_id, m.location_text, m.station_name, m.station_addr, "
            "l.lat, l.lon, l.source, cc.stat_id "
            "FROM charge_meta m "
            "LEFT JOIN charge_locations l ON l.vehicle_id = m.vehicle_id AND l.charge_id = m.charge_id "
            "LEFT JOIN charge_charger cc ON cc.vehicle_id = m.vehicle_id AND cc.charge_id = m.charge_id "
            "WHERE m.vehicle_id = ? AND m.charge_id != ? "
            "ORDER BY m.charge_id DESC",
            (vid, charge_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for other_id, text, name, addr, olat, olon, source, other_stat in rows:
        place = str(text or "").strip()
        if not place or place == _station_auto_text(name, addr):
            continue
        same_station = bool(stat_id) and str(other_stat or "") == str(stat_id)
        near = False
        if olat is not None and olon is not None:
            near = distance_m((lat, lng), (float(olat), float(olon))) <= PLACE_RADIUS_M
        if same_station or near:
            return place
    return None


def _write_location_text(conn: sqlite3.Connection, vid: str, charge_id: str, place: str) -> None:
    text = place.strip()
    if not text:
        return
    conn.execute(
        "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, ?, ?) "
        "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
        "location_text = CASE WHEN charge_meta.location_text IS NULL OR TRIM(charge_meta.location_text) = '' "
        "THEN excluded.location_text ELSE charge_meta.location_text END",
        (vid, charge_id, text),
    )


def fill_charge_place(
    conn: sqlite3.Connection, vid: str, charge_id: str, lat: float, lng: float,
    stat_id: str | None = None, lookup: Callable[[float, float], str | None] | None = None,
) -> str | None:
    """Fill an empty place once. A typed place, or a pin the owner moved, stays.

    A recent place the owner saved within 150 m, or at the same station, is copied.
    Otherwise one lookup may supply an EV charger name, a building, or a road address.
    """
    if _pin_source(conn, vid, charge_id) == "manual":
        return None
    if _location_text(conn, vid, charge_id):
        return None
    mark = f"place_lookup:{vid}:{charge_id}"
    if _meta_get(conn, mark):
        return None
    inherited = _inherit_place(conn, vid, charge_id, lat, lng, stat_id)
    _meta_set(conn, mark, "1")
    if inherited:
        _write_location_text(conn, vid, charge_id, inherited)
        return inherited
    if lookup is None:
        return None
    try:
        found = lookup(lat, lng)
    except Exception:
        found = None
    if found:
        _write_location_text(conn, vid, charge_id, found)
        return found.strip()
    return None


def saved_place_titles(
    conn: sqlite3.Connection, vid: str, lat: float | None, lng: float | None,
    stat_id: str | None = None, query: str = "", charge_id: str = "",
) -> list[str]:
    """Places the owner saved at this spot, newest first. A station line is skipped."""
    needle = query.strip().casefold()
    try:
        rows = conn.execute(
            "SELECT m.charge_id, m.location_text, m.station_name, m.station_addr, "
            "l.lat, l.lon, cc.stat_id "
            "FROM charge_meta m "
            "LEFT JOIN charge_locations l ON l.vehicle_id = m.vehicle_id AND l.charge_id = m.charge_id "
            "LEFT JOIN charge_charger cc ON cc.vehicle_id = m.vehicle_id AND cc.charge_id = m.charge_id "
            "WHERE m.vehicle_id = ? ORDER BY m.charge_id DESC",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    found: list[str] = []
    for other_id, text, name, addr, olat, olon, other_stat in rows:
        if charge_id and str(other_id) == charge_id:
            continue
        place = str(text or "").strip()
        if not place or place == _station_auto_text(name, addr):
            continue
        if needle and needle not in place.casefold():
            continue
        same_station = bool(stat_id) and str(other_stat or "") == str(stat_id)
        near = False
        if lat is not None and lng is not None and olat is not None and olon is not None:
            near = distance_m((lat, lng), (float(olat), float(olon))) <= PLACE_RADIUS_M
        if not (same_station or near):
            continue
        if place not in found:
            found.append(place)
    return found


def _station_queries(
    conn: sqlite3.Connection, vid: str, charge_id: str = "", stat_id: str | None = None,
) -> list[str]:
    """Station name, then address, for a place search. A missing table yields nothing."""
    name = addr = ""

    def take(row: Any) -> None:
        nonlocal name, addr
        if not row:
            return
        name = name or str(row[0] or "").strip()
        addr = addr or str(row[1] or "").strip()

    def one(sql: str, args: tuple[Any, ...]) -> Any:
        try:
            return conn.execute(sql, args).fetchone()
        except sqlite3.OperationalError:
            return None

    if charge_id:
        take(one(
            "SELECT station_name, station_addr FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ))
    if stat_id and not (name and addr):
        take(one(
            "SELECT m.station_name, m.station_addr FROM charge_meta m "
            "JOIN charge_charger cc ON cc.vehicle_id = m.vehicle_id AND cc.charge_id = m.charge_id "
            "WHERE m.vehicle_id = ? AND cc.stat_id = ? "
            "ORDER BY m.charge_id DESC LIMIT 1",
            (vid, stat_id),
        ))
    if stat_id and not (name or addr):
        take(one(
            "SELECT stat_nm, addr FROM ev_chargers WHERE stat_id = ? LIMIT 1",
            (stat_id,),
        ))
    found: list[str] = []
    for part in (name, addr):
        if part and part not in found:
            found.append(part)
    return found


def _append_hit(items: list[dict[str, Any]], seen: set[str], title: str, tag: str, row: dict[str, Any] | None = None) -> None:
    if not title or title in seen:
        return
    item = {"title": title, "tag": tag}
    if row is not None and row.get("lat") is not None and row.get("lon") is not None:
        item["lat"] = row["lat"]
        item["lon"] = row["lon"]
    items.append(item)
    seen.add(title)


def suggest_places(
    conn: sqlite3.Connection, vid: str, lat: float | None, lng: float | None,
    stat_id: str | None = None, query: str = "", charge_id: str = "",
    lookup: Callable[[float, float], str | None] | None = None,
    search: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only suggestions. Nothing here writes a charge row or prints a key."""
    from process.naver_maps import present_place_hit

    q = (query or "").strip()
    items = [
        {"title": title, "tag": "예전 장소"}
        for title in saved_place_titles(conn, vid, lat, lng, stat_id, q, charge_id)
    ]
    seen = {item["title"] for item in items}
    if search is None:
        from process.naver_maps import place_search
        search = place_search
    if q:
        try:
            found = search(q, lat, lng)
        except Exception:
            found = {"items": []}
        for row in (found or {}).get("items") or []:
            title, tag = present_place_hit(row.get("title"), row.get("category"))
            _append_hit(items, seen, title, tag, row)
            if sum(1 for one in items if one["tag"] != "예전 장소") >= 5:
                break
        return {"items": items}
    if lat is not None and lng is not None:
        for station_query in _station_queries(conn, vid, charge_id, stat_id):
            try:
                found = search(station_query, lat, lng)
            except Exception:
                continue
            from process.naver_maps import charger_name_from_items
            title = charger_name_from_items((found or {}).get("items"), (lat, lng))
            if title:
                _append_hit(items, seen, title, "")
                break
    if lookup is None:
        lookup = _reverse_place
    name = None
    if lat is not None and lng is not None:
        try:
            name = lookup(lat, lng)
        except Exception:
            name = None
    if name and name not in seen:
        items.append({"title": name, "tag": "건물"})
    return {"items": items}


def _saved_place(conn: sqlite3.Connection, vid: str, lat: float, lng: float) -> sqlite3.Row | None:
    keys = _place_keys(conn, vid, lat, lng)
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    return conn.execute(
        f"SELECT * FROM charger_place WHERE vehicle_id = ? AND place_key IN ({marks}) LIMIT 1",
        [vid, *keys],
    ).fetchone()


def _apply_saved(conn: sqlite3.Connection, vid: str, charge_id: str, lat: float, lng: float) -> bool:
    """A previous charger pick is not copied onto the next charge."""
    return False


def _region_text(conn: sqlite3.Connection, vid: str, lat: float, lng: float, charge_id: str) -> str:
    parts: list[str] = []
    try:
        meta = conn.execute(
            "SELECT location_text, station_name FROM charge_meta WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        meta = None
    if meta:
        parts.extend(str(v) for v in meta if v)
    try:
        from process.geocode import cache_key
        label = conn.execute(
            "SELECT label FROM geocode_cache WHERE key = ? AND label IS NOT NULL",
            (cache_key(lat, lng),),
        ).fetchall()
        parts.extend(str(r[0]) for r in label if r[0])
    except sqlite3.OperationalError:
        pass
    try:
        rows = conn.execute(
            "SELECT address_text, name, lat, lon FROM stations WHERE vehicle_id = ?",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for address, name, slat, slng in rows:
        if slat is None or slng is None or distance_m((lat, lng), (slat, slng)) <= PLACE_RADIUS_M:
            if address:
                parts.append(str(address))
            if name:
                parts.append(str(name))
    return " ".join(parts)


def reference_points(conn: sqlite3.Connection, vid: str | None = None) -> list[tuple[float, float]]:
    """Charge locations, saved places, and registered places. Reads only."""
    points: list[tuple[float, float]] = []
    tables = ("charge_locations",)
    for table in tables:
        sql = f"SELECT lat, lon FROM {table} WHERE lat IS NOT NULL AND lon IS NOT NULL"
        params: tuple = ()
        if vid:
            sql = f"SELECT lat, lon FROM {table} WHERE vehicle_id = ? AND lat IS NOT NULL AND lon IS NOT NULL"
            params = (vid,)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            continue
        for lat, lng in rows:
            point = _point(lat, lng)
            if point is not None:
                points.append(point)
    return points


def _pending(conn: sqlite3.Connection, vid: str, now: datetime, lookback: timedelta = LOOKBACK) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    floor = _iso(now - lookback)
    try:
        return list(conn.execute(
            "SELECT c.start_ts, c.end_ts, c.kind, c.max_charger_kw, c.max_power, "
            "l.lat, l.lon, cc.source, cc.updated_ts "
            "FROM charges c JOIN charge_locations l ON l.vehicle_id = c.vehicle_id AND l.charge_id = c.start_ts "
            "LEFT JOIN charge_charger cc ON cc.vehicle_id = c.vehicle_id AND cc.charge_id = c.start_ts "
            "WHERE c.vehicle_id = ? AND c.end_ts >= ? "
            "AND (cc.source IS NULL OR cc.source = 'pending')",
            (vid, floor),
        ).fetchall())
    except sqlite3.OperationalError:
        return []


def _fresh_pending(updated: str | None, now: datetime) -> bool:
    if not updated:
        return False
    try:
        return now - _parse_iso(updated) < STATUS_TTL
    except ValueError:
        return False


def _close(moment: datetime | None, other: datetime | None, window: timedelta = INFER_WINDOW) -> bool:
    if moment is None or other is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if other.tzinfo is None:
        other = other.replace(tzinfo=timezone.utc)
    return abs(moment - other) <= window


def _in_window(ts: datetime, center: datetime | None, window: timedelta = INFER_WINDOW) -> bool:
    if center is None:
        return False
    return _close(ts, center, window)


def infer_charger(
    records: list[dict[str, Any]],
    start: datetime,
    end: datetime | None,
    unplug: datetime | None = None,
) -> str | None:
    """Pick the charger whose public status changes match this charge.

    Start is 대기(2) -> 충전중(3). End is 충전중 -> 완료 (lastTedt near the
    charge end). Unplug is a later change to 대기. One weak timestamp is not
    enough, and a tie stays unset.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        cid = str(rec.get("chger_id") or "")
        if cid:
            grouped.setdefault(cid, []).append(rec)
    scores: dict[str, int] = {}
    for cid, rows in grouped.items():
        rows.sort(key=lambda rec: rec.get("fetched") or datetime.min.replace(tzinfo=timezone.utc))
        score = 0
        saw_wait_then_charge = False
        last_stat = ""
        for rec in rows:
            fetched = rec.get("fetched")
            stat = str(rec.get("stat") or "")
            if fetched is not None and _in_window(fetched, start):
                if last_stat == STAT_WAIT and stat == STAT_CHARGING:
                    saw_wait_then_charge = True
            if stat:
                last_stat = stat
        if saw_wait_then_charge:
            score += 3
        elif any(
            (_close(rec.get("last_start"), start) or (_close(rec.get("now_ts"), start) and str(rec.get("stat") or "") == STAT_CHARGING))
            for rec in rows
        ):
            score += 2
        if end is not None:
            saw_done = False
            prev = ""
            for rec in rows:
                stat = str(rec.get("stat") or "")
                if prev == STAT_CHARGING and _close(rec.get("last_end"), end):
                    saw_done = True
                if stat:
                    prev = stat
            if saw_done:
                score += 3
            elif any(_close(rec.get("last_end"), end) for rec in rows):
                score += 2
        if unplug is not None:
            prev = ""
            became_wait = False
            for rec in rows:
                fetched = rec.get("fetched")
                stat = str(rec.get("stat") or "")
                if fetched is not None and _in_window(fetched, unplug) and prev and prev != STAT_WAIT and stat == STAT_WAIT:
                    became_wait = True
                if stat:
                    prev = stat
            if became_wait:
                score += 2
        if score:
            scores[cid] = score
    if not scores:
        return None
    best = max(scores.values())
    winners = [cid for cid, score in scores.items() if score == best]
    if len(winners) != 1 or best < 3:
        return None
    return winners[0]


def _fetch_station(
    conn: sqlite3.Connection, stat_id: str, now: datetime, opener: Callable[..., Any] | None,
) -> int:
    """One getChargerInfo call for a station, including each charger's status fields."""
    if not _spend(conn, now):
        return 0
    url = build_url(INFO_URL, api_key(), {
        "pageNo": 1, "numOfRows": 100, "statId": stat_id, "dataType": "XML",
    })
    try:
        raw = _fetch(url, opener)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 1
    code, _total, items = parse_xml(raw)
    if code == "22":
        _mark_exhausted(conn, now)
        return 1
    if code == "00":
        _store_items(conn, items, now, history=False)
    return 1


def _last_poll(conn: sqlite3.Connection, stat_id: str) -> datetime | None:
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM ("
            "SELECT MAX(fetched_ts) AS ts FROM ev_status_hist WHERE stat_id = ? "
            "UNION ALL SELECT MAX(fetched_ts) AS ts FROM ev_status WHERE stat_id = ?"
            ")",
            (stat_id, stat_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        return _parse_iso(row[0])
    except ValueError:
        return None


def _polls_around(conn: sqlite3.Connection, stat_id: str, center: datetime) -> int:
    lo, hi = _iso(center - POLL_RADIUS), _iso(center + POLL_RADIUS)
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT fetched_ts) FROM ev_status_hist WHERE stat_id = ? AND fetched_ts >= ? AND fetched_ts <= ?",
            (stat_id, lo, hi),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _should_poll(conn: sqlite3.Connection, stat_id: str, now: datetime, centers: list[datetime]) -> bool:
    last = _last_poll(conn, stat_id)
    if last is not None and now - last < POLL_GAP:
        return False
    for center in centers:
        if abs(now - center) <= POLL_RADIUS and _polls_around(conn, stat_id, center) < POLLS_PER_WINDOW:
            return True
    if last is None and any(now > center + POLL_RADIUS for center in centers):
        return True
    return False


def _history(conn: sqlite3.Connection, stat_ids: list[str]) -> list[dict[str, Any]]:
    if not stat_ids:
        return []
    marks = ",".join("?" * len(stat_ids))
    try:
        rows = conn.execute(
            f"SELECT stat_id, chger_id, fetched_ts, stat, now_tsdt, last_tsdt, last_tedt, stat_upd_dt "
            f"FROM ev_status_hist WHERE stat_id IN ({marks})",
            stat_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for stat_id, chger_id, fetched, stat, now_ts, last_ts, last_te, _upd in rows:
        try:
            fetched_dt = _parse_iso(fetched)
        except ValueError:
            continue

        def _opt(text: str | None) -> datetime | None:
            if not text:
                return None
            try:
                return _parse_iso(text)
            except ValueError:
                return None

        out.append({
            "stat_id": stat_id,
            "chger_id": chger_id,
            "fetched": fetched_dt,
            "stat": stat or "",
            "now_ts": _opt(now_ts),
            "last_start": _opt(last_ts),
            "last_end": _opt(last_te),
        })
    return out


def _signal_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _unplug_time(conn: sqlite3.Connection, vid: str, end: datetime) -> datetime | None:
    """First Disconnected after the charge ends, within two hours. Read only."""
    lo = _iso(end)
    hi = _iso(end + timedelta(hours=2))
    try:
        rows = conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'DetailedChargeState' "
            "AND ts >= ? AND ts <= ? ORDER BY ts",
            (vid, lo, hi),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for ts, raw in rows:
        if _signal_value(raw) == "Disconnected":
            try:
                return _parse_iso(ts)
            except ValueError:
                return None
    return None


def _locked_user(conn: sqlite3.Connection, vid: str, charge_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT source FROM charge_charger WHERE vehicle_id = ? AND charge_id = ?",
            (vid, charge_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row[0] == "user"


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


def prune_status_hist(conn: sqlite3.Connection, now: datetime) -> int:
    """Drop status snapshots older than 30 days, then cap each charger."""
    cutoff = _iso(now - HIST_KEEP)
    removed = 0
    try:
        with conn:
            removed += conn.execute(
                "DELETE FROM ev_status_hist WHERE fetched_ts < ?", (cutoff,),
            ).rowcount
            rows = conn.execute(
                "SELECT stat_id, chger_id, fetched_ts FROM ev_status_hist "
                "ORDER BY stat_id, chger_id, fetched_ts DESC"
            ).fetchall()
            extra: list[tuple[str, str, str]] = []
            current = None
            count = 0
            for stat_id, chger_id, fetched in rows:
                key = (stat_id, chger_id)
                if key != current:
                    current = key
                    count = 0
                count += 1
                if count > HIST_CAP:
                    extra.append((stat_id, chger_id, fetched))
            if extra:
                conn.executemany(
                    "DELETE FROM ev_status_hist WHERE stat_id = ? AND chger_id = ? AND fetched_ts = ?",
                    extra,
                )
                removed += len(extra)
    except sqlite3.OperationalError:
        return 0
    return removed


def _point_from_row(raw: Any) -> tuple[float, float] | None:
    value = raw if not isinstance(raw, str) else _signal_value(raw)
    if isinstance(value, dict):
        lat, lng = value.get("lat"), value.get("lon", value.get("lng"))
        point = _point(lat, lng)
        return point
    return None


def _car_point(conn: sqlite3.Connection, vid: str) -> tuple[float, float] | None:
    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Location' ORDER BY ts DESC LIMIT 1",
            (vid,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        found = _point_from_row(row[0])
        if found:
            return found
    try:
        row = conn.execute(
            "SELECT end_lat, end_lon FROM trips WHERE vehicle_id = ? AND end_lat IS NOT NULL "
            "ORDER BY end_ts DESC LIMIT 1",
            (vid,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0] is not None and row[1] is not None:
        return float(row[0]), float(row[1])
    try:
        row = conn.execute(
            "SELECT lat, lon FROM charge_locations WHERE vehicle_id = ? AND lat IS NOT NULL "
            "ORDER BY charge_id DESC LIMIT 1",
            (vid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    return None


def _charge_regions(conn: sqlite3.Connection, vid: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        rows = conn.execute(
            "SELECT lat, lon FROM charge_locations WHERE vehicle_id = ? AND lat IS NOT NULL",
            (vid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    seen: set[str] = set()
    for lat, lng in rows:
        found = region_from_point(lat, lng)
        if not found or not found[1]:
            text = zcode_from_text(_region_text(conn, vid, float(lat), float(lng), ""))
            found = (text, None) if text else found
        key = found[1] or found[0] if found else None
        if found and key and key not in seen:
            seen.add(key)
            out.append((found[0], found[1] or ""))
    return out


def ensure_sigungu(
    conn: sqlite3.Connection, lat: float, lng: float, now: datetime, opener: Callable[..., Any] | None = None,
) -> int:
    """Fetch this 시·군 for a charge-port opening.

    A finished catalog is used only when a stored charger is already within
    PICK_M of the port. An empty or anchor-trimmed catalog is read again.
    """
    if not api_key():
        return 0
    found = region_from_point(lat, lng)
    if not found or not found[1]:
        return 0
    zcode, zscode = found
    key = _region_key(zcode, zscode)
    row = _region_row(conn, key)
    nearby = _nearby(conn, lat, lng, now, radius=PICK_M)
    if row is not None and row["done"] and not _region_stale(row, now) and nearby:
        return 0
    if row is not None:
        with conn:
            conn.execute("UPDATE ev_region SET next_page = 1, done = 0 WHERE zcode = ?", (key,))
    return _fetch_info_page(conn, zcode, now, opener, zscode=zscode, anchors=None)


# Recorded after a 시·군 catalog is read by this parser. Older done rows have no mark.
CATALOG_VERSION = "v110"


def _catalog_version_key(region_key: str) -> str:
    return f"ev_catalog:{CATALOG_VERSION}:{region_key}"


def _charger_count_in_sigungu(conn: sqlite3.Connection, zscode: str) -> int:
    from process.regions import zscode_at
    try:
        rows = conn.execute(
            "SELECT lat, lng FROM ev_chargers WHERE lat IS NOT NULL AND lng IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    return sum(1 for lat, lng in rows if zscode_at(lat, lng) == zscode)


def refill_empty_done_catalogs(
    conn: sqlite3.Connection, now: datetime, opener: Callable[..., Any] | None = None,
) -> int:
    """One refetch for a finished 시·군 this parser has not recorded.

    A handful of older rows used to hide a short catalog: 부천 had 319 chargers
    against total 5426, 당진 108 against 2159. A region is read again when it is
    done, total_count is positive, and either the saved count is under half of
    that total or this parser has never marked it. The version flag stops a
    second pass. Nothing here deletes charger rows.
    """
    if not api_key():
        return 0
    try:
        rows = conn.execute(
            "SELECT zcode, total_count FROM ev_region WHERE done = 1 AND COALESCE(total_count, 0) > 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    calls = 0
    for key, total in rows:
        if not key or ":" not in key:
            continue
        flag = _catalog_version_key(key)
        if _meta_get(conn, flag) == "1":
            continue
        zcode, zscode = key.split(":", 1)
        if not zscode:
            continue
        try:
            total_n = int(total or 0)
        except (TypeError, ValueError):
            total_n = 0
        stored = _charger_count_in_sigungu(conn, zscode)
        short = total_n > 0 and stored * 2 < total_n
        # No version mark means this parser has not read the region. A short
        # catalog (under half of total_count) is the same one-time case.
        if not short and _meta_get(conn, flag) == "1":
            continue
        with conn:
            conn.execute("UPDATE ev_region SET next_page = 1, done = 0 WHERE zcode = ?", (key,))
        calls += _fetch_info_page(conn, zcode, now, opener, zscode=zscode, anchors=None)
        _meta_set(conn, flag, "1")
    return calls


def refresh_catalogs(
    conn: sqlite3.Connection, vid: str, now: datetime, opener: Callable[..., Any] | None = None,
) -> int:
    """Parked 시·군 refresh once each KST day. A drive into a new 시·군 fetches immediately."""
    if not api_key():
        return 0
    ensure_schema(conn)
    try:
        gear_row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Gear' ORDER BY ts DESC LIMIT 1",
            (vid,),
        ).fetchone()
    except sqlite3.OperationalError:
        gear_row = None
    gear = str(_signal_value(gear_row[0])) if gear_row else ""
    driving = gear in ("D", "R", "N")
    here = _car_point(conn, vid)
    found = region_from_point(here[0], here[1]) if here else None
    day = now.astimezone(KST).strftime("%Y-%m-%d")
    codes = {part for part in (_meta_get(conn, f"ev_watch:{vid}") or "").split(",") if part}
    stored_day = _meta_get(conn, f"ev_watch_day:{vid}")
    targets: list[tuple[str, str]] = []

    def add(pair: tuple[str, str | None] | None) -> None:
        if not pair or not pair[0]:
            return
        token = pair[1] or pair[0]
        if any((code or zone) == token for zone, code in targets):
            return
        targets.append((pair[0], pair[1] or None))

    if driving and found and found[1] not in codes:
        add(found)
    if not driving and stored_day != day:
        add(found)
        for zcode, zscode in _charge_regions(conn, vid):
            add((zcode, zscode or None))
        for code in sorted(codes):
            add((code[:2], code))
    calls = refill_empty_done_catalogs(conn, now, opener)
    force = (not driving) and stored_day != day
    for zcode, zscode in targets:
        key = _region_key(zcode, zscode)
        row = _region_row(conn, key)
        if force and row is not None:
            with conn:
                conn.execute("UPDATE ev_region SET next_page = 1, done = 0 WHERE zcode = ?", (key,))
        elif row is not None and row["done"] and not _region_stale(row, now):
            codes.add(zscode)
            continue
        # 시·군 catalogs keep every charger in that district. A province fallback
        # still keeps only chargers within KEEP_M of the car. One watched 시·군 is
        # a few hundred to a couple of thousand charger rows (well under a megabyte).
        calls += _fetch_info_page(
            conn, zcode, now, opener, zscode=zscode,
            anchors=None if zscode else ([here] if here else None),
        )
        codes.add(zscode or zcode)
    _meta_set(conn, f"ev_watch:{vid}", ",".join(sorted(codes)))
    if not driving:
        _meta_set(conn, f"ev_watch_day:{vid}", day)
    return calls


def fill_vehicle(
    conn: sqlite3.Connection, vid: str, now: datetime | None = None, opener: Callable[..., Any] | None = None,
) -> int:
    """Resolve recent charges. Returns how many portal calls this pass made.

    The first pass after this version looks back 30 days so a charge that the
    wrong region cache missed can be matched again. Later passes stay on the
    short lookback.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ensure_schema(conn)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    key = api_key()
    flag = f"ev_rematch:v171:{vid}"
    wide = _meta_get(conn, flag) != "1"
    try:
        calls = refresh_catalogs(conn, vid, now, opener)
        calls += _fill(conn, vid, now, opener, key, lookback=REMATCH_LOOKBACK if wide else LOOKBACK)
        try:
            calls += retry_status_matches(conn, vid, now, opener)
        except Exception:
            pass
        if wide:
            _meta_set(conn, flag, "1")
        return calls
    finally:
        conn.row_factory = old_factory


def _station_item(pool: list[dict[str, Any]]) -> dict[str, Any]:
    """Nearest charger's station, without a device number."""
    def dist(item: dict[str, Any]) -> float:
        value = item.get("distance_m")
        return float(value) if value is not None else 1e9
    nearest = min(pool, key=dist)
    group = [item for item in pool if item.get("stat_id") == nearest.get("stat_id")] or [nearest]
    stored = dict(min(group, key=dist))
    stored["chger_id"] = None
    return stored


def _station_ids(items: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    ordered = sorted(items, key=lambda item: item.get("distance_m") if item.get("distance_m") is not None else 1e9)
    for item in ordered:
        stat_id = item.get("stat_id")
        if stat_id and stat_id not in seen:
            seen.append(stat_id)
        if len(seen) >= 3:
            break
    return seen


def _charger_place_near(
    lat: float, lng: float, name: str = "", addr: str = "",
    search: Callable[..., dict[str, Any]] | None = None,
) -> str | None:
    """Naver local search for an EV charger, queried by the station name or address."""
    from process.naver_maps import charger_name_from_items, place_search

    if search is None:
        search = place_search
    seen: set[str] = set()
    for query in (str(name or "").strip(), str(addr or "").strip()):
        if not query or query in seen:
            continue
        seen.add(query)
        try:
            found = search(query, lat, lng)
        except Exception:
            continue
        title = charger_name_from_items((found or {}).get("items"), (lat, lng))
        if title:
            return title
    return None


def _reverse_parts(lat: float, lng: float) -> tuple[str, str] | None:
    """Building name as 건물, else the road address as 주소. A miss is None."""
    from process.naver_maps import (
        LookupError, building_name_from_reverse, fetch_reverse, maps_configured, road_address_from_reverse,
    )

    if not maps_configured():
        return None
    try:
        payload = fetch_reverse(lat, lng, orders="roadaddr,addr")
    except LookupError:
        return None
    building = building_name_from_reverse(payload)
    if building:
        return building, "건물"
    road = road_address_from_reverse(payload)
    if road:
        return road, "주소"
    return None


def _reverse_place(lat: float, lng: float) -> str | None:
    """Building name, else the road address. A missing key or a failed call is empty."""
    found = _reverse_parts(lat, lng)
    return found[0] if found else None


def _naver_place(
    lat: float, lng: float, name: str = "", addr: str = "",
    search: Callable[..., dict[str, Any]] | None = None,
) -> str | None:
    """Charger name, else the building, else the road. A missing key leaves it empty."""
    try:
        found = _charger_place_near(lat, lng, name, addr, search)
    except Exception:
        found = None
    if found:
        return found
    return _reverse_place(lat, lng)


def _status_rows(conn: sqlite3.Connection, stat_ids: list[str]) -> list[dict[str, str]]:
    if not stat_ids:
        return []
    marks = ",".join("?" * len(stat_ids))
    try:
        rows = conn.execute(
            f"SELECT stat_id, chger_id, stat FROM ev_status WHERE stat_id IN ({marks})",
            stat_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"stat_id": sid, "chger_id": cid, "stat": stat or ""} for sid, cid, stat in rows]


def changed_to_charging(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Chargers that moved from 충전대기(2) to 충전중(3)."""
    prev = {(str(row.get("stat_id") or ""), str(row.get("chger_id") or "")): str(row.get("stat") or "") for row in before}
    hits: list[tuple[str, str]] = []
    for row in after:
        key = (str(row.get("stat_id") or ""), str(row.get("chger_id") or ""))
        if prev.get(key) == STAT_WAIT and str(row.get("stat") or "") == STAT_CHARGING:
            hits.append(key)
    return hits


def snapshot_port_status(
    conn: sqlite3.Connection, vid: str, lat: float, lng: float, now: datetime,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Read nearby charger status once. A failed call leaves the previous snapshot."""
    try:
        ensure_sigungu(conn, lat, lng, now, opener)
    except Exception:
        pass
    nearby = _nearby(conn, lat, lng, now, FAR_M)
    stat_ids: list[str] = []
    for item in nearby:
        sid = str(item.get("stat_id") or "")
        if sid and sid not in stat_ids:
            stat_ids.append(sid)
        if len(stat_ids) >= 6:
            break
    if api_key():
        for sid in stat_ids:
            try:
                _fetch_station(conn, sid, now, opener)
            except Exception:
                continue
    rows = _status_rows(conn, stat_ids)
    _meta_set(conn, f"port_snap:{vid}", json.dumps({"items": rows}, ensure_ascii=False))
    return rows


def _charger_item(conn: sqlite3.Connection, stat_id: str, chger_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT stat_id, chger_id, busi_nm, output_kw, chger_type, stat_nm, addr, lat, lng "
            "FROM ev_chargers WHERE stat_id = ? AND chger_id = ?",
            (stat_id, chger_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "stat_id": row[0], "chger_id": row[1], "busi_nm": row[2] or "",
        "output_kw": row[3], "chger_type": row[4] or "", "stat_nm": row[5] or "",
        "addr": row[6] or "", "lat": row[7], "lng": row[8],
    }


def match_status_change(
    conn: sqlite3.Connection, vid: str, now: datetime, opener: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """The one charger that left 대기 for 충전중 since the port opened. None if not exactly one."""
    raw = _meta_get(conn, f"port_snap:{vid}")
    if not raw:
        return None
    try:
        before = json.loads(raw).get("items") or []
    except (TypeError, ValueError):
        return None
    stat_ids: list[str] = []
    for row in before:
        sid = str(row.get("stat_id") or "")
        if sid and sid not in stat_ids:
            stat_ids.append(sid)
    if api_key():
        for sid in stat_ids:
            try:
                _fetch_station(conn, sid, now, opener)
            except Exception:
                continue
    hits = changed_to_charging(before, _status_rows(conn, stat_ids))
    if len(hits) != 1:
        return None
    return _charger_item(conn, hits[0][0], hits[0][1])


def _retry_key(vid: str, charge_id: str) -> str:
    return f"status_try:{vid}:{charge_id}"


def _schedule_status_retry(conn: sqlite3.Connection, vid: str, charge_id: str, start: datetime, now: datetime) -> None:
    if now - start > STATUS_MATCH_WINDOW:
        return
    raw = _meta_get(conn, _retry_key(vid, charge_id))
    attempts = 0
    if raw:
        try:
            attempts = int(raw.split("|", 1)[0])
        except ValueError:
            attempts = 0
    if attempts >= STATUS_MATCH_TRIES:
        return
    nxt = _iso(now + STATUS_MATCH_GAP)
    _meta_set(conn, _retry_key(vid, charge_id), f"{attempts + 1}|{nxt}")


def retry_status_matches(
    conn: sqlite3.Connection, vid: str, now: datetime, opener: Callable[..., Any] | None = None,
) -> int:
    """A later poll may still see 대기 become 충전중. Failures leave the stored charge."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM process_meta WHERE key LIKE ?",
            (f"status_try:{vid}:%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    calls = 0
    prefix = f"status_try:{vid}:"
    for key, value in rows:
        charge_id = str(key)[len(prefix):]
        try:
            attempts_s, nxt = str(value).split("|", 1)
            attempts = int(attempts_s)
            due = _parse_iso(nxt)
            start = _parse_iso(charge_id)
        except (ValueError, TypeError):
            conn.execute("DELETE FROM process_meta WHERE key = ?", (str(key),))
            continue
        if attempts >= STATUS_MATCH_TRIES or now - start > STATUS_MATCH_WINDOW:
            conn.execute("DELETE FROM process_meta WHERE key = ?", (str(key),))
            continue
        if now < due:
            continue
        if _locked_user(conn, vid, charge_id):
            conn.execute("DELETE FROM process_meta WHERE key = ?", (str(key),))
            continue
        try:
            found = match_status_change(conn, vid, now, opener)
        except Exception:
            found = None
        calls += 1
        if found is None:
            _schedule_status_retry(conn, vid, charge_id, start, now)
            continue
        try:
            source = conn.execute(
                "SELECT source FROM charge_charger WHERE vehicle_id = ? AND charge_id = ?",
                (vid, charge_id),
            ).fetchone()
        except sqlite3.OperationalError:
            source = None
        previous = source[0] if source else None
        if previous == "user":
            conn.execute("DELETE FROM process_meta WHERE key = ?", (str(key),))
            continue
        _write_choice(
            conn, vid, charge_id, found, "status",
            float(found["lat"]) if found.get("lat") is not None else 0.0,
            float(found["lng"]) if found.get("lng") is not None else 0.0,
            force_operator=previous == "station",
        )
        conn.execute("DELETE FROM process_meta WHERE key = ?", (str(key),))
    return calls


def _fill(
    conn: sqlite3.Connection, vid: str, now: datetime, opener: Callable[..., Any] | None, key: str,
    lookback: timedelta = LOOKBACK,
) -> int:
    calls = 0
    for row in _pending(conn, vid, now, lookback):
        lat, lng = float(row["lat"]), float(row["lon"])
        charge_id = row["start_ts"]
        if _locked_user(conn, vid, charge_id):
            continue
        nearby = _nearby(conn, lat, lng, now)
        kind = row["kind"] or "AC"
        power = row["max_charger_kw"] if row["max_charger_kw"] is not None else row["max_power"]
        pool = select_type_power(select_band(nearby), kind, power) or select_band(nearby)
        chosen = None
        try:
            chosen = match_status_change(conn, vid, now, opener)
        except Exception:
            chosen = None
        if chosen is not None:
            item = chosen
            source = "status"
        else:
            item = _station_item(pool) if pool else None
            source = "station"
            if _meta_get(conn, f"port_snap:{vid}"):
                try:
                    _schedule_status_retry(conn, vid, charge_id, _parse_iso(charge_id), now)
                except (ValueError, TypeError):
                    pass
        point = (lat, lng)
        stat_id = None
        if item is not None:
            _write_choice(
                conn, vid, charge_id, item, source, lat, lng, force_operator=False,
            )
            stat_id = item.get("stat_id")
            if item.get("lat") is not None and item.get("lng") is not None:
                point = (float(item["lat"]), float(item["lng"]))
        station_name = (item.get("stat_nm") or "") if item is not None else ""
        station_addr = (item.get("addr") or "") if item is not None else ""
        fill_charge_place(
            conn, vid, charge_id, point[0], point[1], stat_id,
            lambda la, ln, n=station_name, a=station_addr: _naver_place(la, ln, n, a),
        )
    return calls


def choose_charger(conn: sqlite3.Connection, vid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Store the owner's charger, or clear it. A choice is not reused at the next visit."""
    ensure_schema(conn)
    charge_id = str(body.get("charge_id") or "").strip()
    stat_id = str(body.get("stat_id") or "").strip()
    chger_id = str(body.get("chger_id") or "").strip()
    if not charge_id:
        raise ValueError("charge_id is required")
    conn.row_factory = sqlite3.Row
    loc = conn.execute(
        "SELECT lat, lon FROM charge_locations WHERE vehicle_id = ? AND charge_id = ?",
        (vid, charge_id),
    ).fetchone()
    if loc is None:
        raise KeyError("charge location not found")
    if stat_id and not chger_id:
        row = conn.execute(
            "SELECT stat_id, busi_nm, output_kw, chger_type, stat_nm, addr, lat, lng FROM ev_chargers "
            "WHERE stat_id = ? ORDER BY chger_id LIMIT 1",
            (stat_id,),
        ).fetchone()
        if row is None:
            raise ValueError("not a station near this charge")
        if distance_m((float(loc["lat"]), float(loc["lon"])), (float(row["lat"]), float(row["lng"]))) > PICK_M:
            raise ValueError("not a station near this charge")
        item = {
            "stat_id": row["stat_id"], "chger_id": None, "busi_nm": row["busi_nm"],
            "output_kw": row["output_kw"], "chger_type": row["chger_type"], "stat_nm": row["stat_nm"],
            "addr": row["addr"],
        }
        _write_choice(conn, vid, charge_id, item, "station", float(loc["lat"]), float(loc["lon"]), force_operator=True)
        return {
            "ok": True, "charge_id": charge_id, "stat_id": row["stat_id"],
            "operator": item.get("busi_nm") or "", "output_kw": item.get("output_kw"), "source": "station",
        }
    if not chger_id:
        now = _iso(datetime.now(timezone.utc))
        with conn:
            conn.execute(
                "INSERT INTO charge_charger (vehicle_id, charge_id, source, updated_ts) "
                "VALUES (?, ?, 'user', ?) "
                "ON CONFLICT(vehicle_id, charge_id) DO UPDATE SET "
                "stat_id = NULL, chger_id = NULL, busi_nm = NULL, output_kw = NULL, chger_type = NULL, "
                "stat_nm = NULL, source = 'user', candidates_json = NULL, updated_ts = excluded.updated_ts",
                (vid, charge_id, now),
            )
        return {"ok": True, "charge_id": charge_id, "chger_id": None, "source": "user"}
    row = conn.execute(
        "SELECT stat_id, chger_id, lat, lng, busi_nm, output_kw, chger_type, stat_nm FROM ev_chargers "
        "WHERE stat_id = ? AND chger_id = ?",
        (stat_id, chger_id),
    ).fetchone()
    if row is None:
        raise ValueError("not a charger at this station")
    if distance_m((float(loc["lat"]), float(loc["lon"])), (float(row["lat"]), float(row["lng"]))) > PICK_M:
        raise ValueError("not a charger at this station")
    item = {
        "stat_id": row["stat_id"], "chger_id": row["chger_id"], "busi_nm": row["busi_nm"],
        "output_kw": row["output_kw"], "chger_type": row["chger_type"], "stat_nm": row["stat_nm"],
    }
    _write_choice(conn, vid, charge_id, item, "user", float(loc["lat"]), float(loc["lon"]), force_operator=True)
    return {"ok": True, "charge_id": charge_id, "operator": item.get("busi_nm") or "", "output_kw": item.get("output_kw"), "source": "user"}


def stations_near(conn: sqlite3.Connection, lat: Any, lon: Any) -> list[dict[str, Any]]:
    """Unique stations within the match distance, nearest first. Reads only."""
    if lat is None or lon is None:
        return []
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    grouped: dict[str, dict[str, Any]] = {}
    for item in _nearby(conn, lat_f, lon_f, datetime.now(timezone.utc), radius=PICK_M):
        sid = str(item.get("stat_id") or "")
        if not sid:
            continue
        current = grouped.get(sid)
        dist = item.get("distance_m") if item.get("distance_m") is not None else 1e9
        if current is None or dist < current["distance_m"]:
            grouped[sid] = {
                "stat_id": sid,
                "stat_nm": item.get("stat_nm") or "",
                "addr": item.get("addr") or "",
                "operator": item.get("busi_nm") or "",
                "output_kw": item.get("output_kw"),
                "distance_m": dist,
            }
    return sorted(grouped.values(), key=lambda row: row["distance_m"])


def attach_public_chargers(conn: sqlite3.Connection, vid: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    ids = [item.get("charge_id") for item in items if item.get("charge_id")]
    if not ids:
        return
    found: dict[str, sqlite3.Row] = {}
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM charge_charger WHERE vehicle_id = ? AND charge_id IN ({marks})",
                [vid, *chunk],
            ).fetchall()
            for row in rows:
                found[row["charge_id"]] = row
    except sqlite3.OperationalError:
        found = {}
    finally:
        conn.row_factory = old_factory
    for item in items:
        row = found.get(item.get("charge_id"))
        meta_op = str(((item.get("meta") or {}).get("operator") or item.get("operator_display") or "")).strip()
        operator = (row["busi_nm"] if row is not None and row["busi_nm"] else "") or meta_op
        stat_id = row["stat_id"] if row is not None else None
        source = row["source"] if row is not None else None
        stations = stations_near(conn, item.get("lat"), item.get("lon"))
        if row is None and not stations and item.get("lat") is None:
            continue
        output = row["output_kw"] if row is not None and row["output_kw"] is not None else None
        stat_nm = (row["stat_nm"] if row is not None and row["stat_nm"] else "") or (stations[0]["stat_nm"] if stations else "")
        addr = ""
        if stat_id:
            addr = next((st["addr"] for st in stations if st["stat_id"] == stat_id and st.get("addr")), "")
        if not addr and stations:
            addr = stations[0].get("addr") or ""
        item["public_charger"] = {
            "operator": operator,
            "output_kw": output,
            "type_label": type_label(row["chger_type"], output) if row is not None and (row["chger_type"] or output is not None) else "",
            "stat_nm": stat_nm,
            "addr": addr,
            "stat_id": stat_id or (stations[0]["stat_id"] if stations else None),
            "source": source,
            "stations": stations,
        }


def _same_name(left: str | None, right: str | None) -> bool:
    def norm(text: str | None) -> str:
        return (text or "").strip().casefold().replace(" ", "")
    return bool(norm(left)) and norm(left) == norm(right)


def chargers_at(
    conn: sqlite3.Connection, lat: Any, lon: Any, operator: str | None, stat_id: str | None,
) -> list[dict[str, Any]]:
    """Chargers at this charge's station. Reads only."""
    if lat is None or lon is None:
        return []
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    nearby = _nearby(conn, lat_f, lon_f, datetime.now(timezone.utc))
    pool = nearby
    if stat_id:
        at = [item for item in nearby if item.get("stat_id") == stat_id]
        if at:
            pool = at
    elif operator:
        named = [item for item in nearby if _same_name(item.get("busi_nm"), operator)]
        if named:
            pool = named
    if not pool:
        return []
    if not stat_id:
        best = min(pool, key=lambda item: item.get("distance_m") if item.get("distance_m") is not None else 1e9)
        pool = [item for item in pool if item.get("stat_id") == best.get("stat_id")]
    pool.sort(key=lambda item: str(item.get("chger_id") or ""))
    return [_candidate_json(item) for item in pool]
