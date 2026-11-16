"""Naver map, reverse geocode, and local search. Secrets stay in the environment.

Reverse geocoding uses NAVER_MAPS_CLIENT_ID and NAVER_MAPS_CLIENT_SECRET.
Place search uses NAVER_SEARCH_CLIENT_ID and NAVER_SEARCH_CLIENT_SECRET.
Neither secret is returned to the browser. A failed reverse lookup is retried,
then Nominatim. Results are cached in this process. Nothing here prints a key,
a coordinate, or a response body.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from process.geo import distance_m

REVERSE_URL = "https://maps.apigw.ntruss.com/map-reversegeocode/v2/gc"
SEARCH_URL = "https://naverapihub.apigw.ntruss.com/search/v1/local"
MAPS_ID = "NAVER_MAPS_CLIENT_ID"
MAPS_SECRET = "NAVER_MAPS_CLIENT_SECRET"
SEARCH_ID = "NAVER_SEARCH_CLIENT_ID"
SEARCH_SECRET = "NAVER_SEARCH_CLIENT_SECRET"

Opener = Callable[..., Any]
_AREA_CACHE: dict[str, str] = {}
_TAG = re.compile(r"<[^>]+>")


class LookupError(Exception):
    """The provider did not return a usable body. The message has no URL and no key."""


def maps_keys() -> tuple[str, str]:
    return os.environ.get(MAPS_ID, "").strip(), os.environ.get(MAPS_SECRET, "").strip()


def search_keys() -> tuple[str, str]:
    return os.environ.get(SEARCH_ID, "").strip(), os.environ.get(SEARCH_SECRET, "").strip()


def maps_configured() -> bool:
    kid, secret = maps_keys()
    return bool(kid and secret)


def search_configured() -> bool:
    kid, secret = search_keys()
    return bool(kid and secret)


def _opener() -> Opener:
    return urllib.request.urlopen


def _read(req: urllib.request.Request, opener: Opener) -> dict[str, Any]:
    try:
        with opener(req, timeout=8) as res:
            status = getattr(res, "status", 200) or 200
            body = res.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LookupError(type(exc).__name__) from exc
    if int(status) >= 400:
        raise LookupError("http")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LookupError("json") from exc
    if not isinstance(data, dict):
        raise LookupError("shape")
    return data


def _status_ok(payload: dict[str, Any]) -> bool:
    status = payload.get("status")
    if not isinstance(status, dict) or "code" not in status:
        return True
    try:
        return int(status["code"]) == 0
    except (TypeError, ValueError):
        return False


def _region(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, list):
        return {}
    for item in results:
        if not isinstance(item, dict):
            continue
        region = item.get("region")
        if isinstance(region, dict) and region:
            return region
    return {}


def sigungu_name(payload: dict[str, Any]) -> str | None:
    """시·군·구 from a reverse-geocode body (area2). An empty name is a miss."""
    area = _region(payload).get("area2")
    if not isinstance(area, dict):
        return None
    name = str(area.get("name") or "").strip()
    return name or None


_ADMIN_NAME = re.compile(r".+(동|리|읍|면|가)$")
_BUILDING_KEYS = ("buildingName", "bldNm", "building", "addition0")
CHARGER_CATEGORY = "전기차충전소"
CHARGER_PLACE_M = 300.0


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _named_results(payload: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    wanted = set(names)
    return [item for item in _results(payload) if item.get("name") in wanted]


def _building_in(land: dict[str, Any]) -> str | None:
    for key in _BUILDING_KEYS:
        value = _land_text(key, land.get(key))
        if value:
            return value
    return None


def _land_text(key: str, raw: Any) -> str | None:
    """A building string. addition0 may be {type, value}; only type building counts."""
    if isinstance(raw, dict):
        if key != "addition0" or str(raw.get("type") or "").strip().casefold() != "building":
            return None
        raw = raw.get("value")
    text = str(raw or "").strip()
    if not text:
        return None
    if key == "addition0" and _ADMIN_NAME.fullmatch(text):
        return None
    return text


def strip_charger_place(title: Any) -> str:
    """Drop a trailing 전기차충전소, including the spaces around it."""
    text = " ".join(str(title or "").split())
    if text.endswith(CHARGER_CATEGORY):
        text = text[: -len(CHARGER_CATEGORY)].strip()
    return text


def is_ev_charger_category(category: Any) -> bool:
    return CHARGER_CATEGORY in str(category or "")


def charger_name_from_items(
    items: list[dict[str, Any]] | None, origin: tuple[float, float] | None,
    limit_m: float = CHARGER_PLACE_M,
) -> str | None:
    """Nearest EV charger title within limit_m. A shop, or a far hit, is ignored."""
    if origin is None:
        return None
    best: tuple[float, str] | None = None
    for item in items or []:
        if not isinstance(item, dict) or not is_ev_charger_category(
            item.get("category") or item.get("categoryName")
        ):
            continue
        title = strip_charger_place(strip_title(item.get("title")))
        if not title:
            continue
        point = None
        if item.get("lat") is not None and item.get("lon") is not None:
            try:
                point = (float(item["lat"]), float(item["lon"]))
            except (TypeError, ValueError):
                point = None
        if point is None:
            point = _point(item)
        if point is None:
            continue
        dist = distance_m(origin, point)
        if dist > limit_m:
            continue
        if best is None or dist < best[0]:
            best = (dist, title)
    return best[1] if best else None


def present_place_hit(title: Any, category: Any) -> tuple[str, str]:
    """Shown title and tag. An EV charger loses its suffix and its tag."""
    text = strip_title(title)
    raw = str(category or "").strip()
    last = raw.split(">")[-1].strip() if raw else ""
    if is_ev_charger_category(raw):
        return strip_charger_place(text), ""
    return text, last


def building_name_from_reverse(payload: dict[str, Any]) -> str | None:
    """Building name from a road or lot reverse-geocode result. A dong name is not one."""
    for item in _named_results(payload, "roadaddr", "addr"):
        land = item.get("land")
        if not isinstance(land, dict):
            continue
        name = _building_in(land)
        if name:
            return name
    return None


def road_address_from_reverse(payload: dict[str, Any]) -> str | None:
    """Road name and number from roadaddr. No coordinate is included."""
    for item in _named_results(payload, "roadaddr"):
        land = item.get("land")
        if not isinstance(land, dict):
            continue
        road = str(land.get("name") or "").strip()
        main = str(land.get("number1") or "").strip()
        sub = str(land.get("number2") or "").strip()
        if road and main and sub and sub != "0":
            return f"{road} {main}-{sub}"
        if road and main:
            return f"{road} {main}"
        if road:
            return road
    return None


def is_shop_place(item: dict[str, Any]) -> bool:
    """A local-search row with a business category is a shop inside a building."""
    category = str(item.get("category") or item.get("categoryName") or "").strip()
    return bool(category)


def place_name_from_sources(
    reverse_payload: dict[str, Any] | None, search_items: list[dict[str, Any]] | None = None,
) -> str | None:
    """Building name, else the road address. A shop from place search is never the place."""
    for item in search_items or []:
        if is_shop_place(item):
            continue
    payload = reverse_payload or {}
    building = building_name_from_reverse(payload)
    if building:
        return building
    return road_address_from_reverse(payload)


def label_from_naver(payload: dict[str, Any]) -> str | None:
    """Administrative label, broadest area first. 'kr' is dropped."""
    region = _region(payload)
    parts: list[str] = []
    for key in ("area1", "area2", "area3", "area4"):
        area = region.get(key)
        if not isinstance(area, dict):
            continue
        name = str(area.get("name") or "").strip()
        if name and name.lower() != "kr":
            parts.append(name)
    return " ".join(parts) or None


def _cache_key(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"


def fetch_reverse(lat: float, lon: float, opener: Opener | None = None,
                  orders: str = "admcode,legalcode") -> dict[str, Any]:
    kid, secret = maps_keys()
    if not kid or not secret:
        raise LookupError("maps key missing")
    query = urllib.parse.urlencode({
        "coords": f"{float(lon):.6f},{float(lat):.6f}",
        "output": "json",
        "orders": orders,
    })
    req = urllib.request.Request(
        f"{REVERSE_URL}?{query}",
        headers={
            "x-ncp-apigw-api-key-id": kid,
            "x-ncp-apigw-api-key": secret,
        },
    )
    payload = _read(req, opener or _opener())
    if not _status_ok(payload):
        raise LookupError("status")
    return payload


def _nominatim_area(lat: float, lon: float, opener: Opener) -> str | None:
    from process.geocode import GeocodeLookupError, NominatimGeocoder

    try:
        data = NominatimGeocoder(opener=opener)._fetch(lat, lon, "ko")
    except GeocodeLookupError:
        return None
    addr = data.get("address") if isinstance(data, dict) else None
    if not isinstance(addr, dict):
        return None
    for key in ("city", "county", "town", "state_district"):
        value = addr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def area_name(lat: float, lon: float, opener: Opener | None = None, attempts: int = 2) -> str | None:
    """시·군·구 name. Naver is tried twice, then Nominatim. A hit is cached."""
    key = _cache_key(lat, lon)
    if key in _AREA_CACHE:
        return _AREA_CACHE[key] or None
    open_fn = opener or _opener()
    name: str | None = None
    if maps_configured():
        for _ in range(max(1, attempts)):
            try:
                name = sigungu_name(fetch_reverse(lat, lon, open_fn))
            except LookupError:
                name = None
                continue
            break
    if not name:
        name = _nominatim_area(lat, lon, open_fn)
    if name:
        _AREA_CACHE[key] = name
    return name


def clear_area_cache() -> None:
    _AREA_CACHE.clear()


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("items")
    if not isinstance(raw, list):
        result = payload.get("result")
        raw = result.get("items") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _point(item: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lon = int(str(item.get("mapx"))) / 1e7
        lat = int(str(item.get("mapy"))) / 1e7
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def strip_title(value: Any) -> str:
    return _TAG.sub("", str(value or "")).replace("&amp;", "&").strip()


def fetch_search(query: str, opener: Opener | None = None, attempts: int = 2) -> list[dict[str, Any]]:
    kid, secret = search_keys()
    if not kid or not secret:
        raise LookupError("search key missing")
    params = urllib.parse.urlencode({"query": query, "display": 5, "format": "json"})
    req = urllib.request.Request(
        f"{SEARCH_URL}?{params}",
        headers={
            "X-NCP-APIGW-API-KEY-ID": kid,
            "X-NCP-APIGW-API-KEY": secret,
        },
    )
    open_fn = opener or _opener()
    last: LookupError | None = None
    for _ in range(max(1, attempts)):
        try:
            return _items(_read(req, open_fn))
        except LookupError as exc:
            last = exc
    raise last or LookupError("search")


def place_search(query: str, lat: float | None, lon: float | None,
                 opener: Opener | None = None) -> dict[str, Any]:
    """Up to five place titles, nearest to the charge first. No keys and no coordinates."""
    text = " ".join(str(query or "").split())
    if not search_configured():
        return {"available": False, "items": []}
    prefix = ""
    origin: tuple[float, float] | None = None
    if lat is not None and lon is not None:
        try:
            origin = (float(lat), float(lon))
        except (TypeError, ValueError):
            origin = None
    if origin is not None:
        from process.regions import query_prefix, zscode_at

        prefix = query_prefix(zscode_at(origin[0], origin[1]))
    full = text if not prefix or prefix in text else f"{prefix} {text}"
    try:
        rows = fetch_search(full, opener)
    except LookupError:
        return {"available": True, "items": []}
    ranked: list[tuple[float, str, str, tuple[float, float] | None]] = []
    for item in rows:
        title = strip_title(item.get("title"))
        if not title:
            continue
        category = str(item.get("category") or "").strip()
        category = category.split(">")[-1].strip() if category else ""
        point = _point(item)
        if origin is not None and point is not None:
            dist = distance_m(origin, point)
        else:
            dist = float(len(ranked))
        ranked.append((dist, title, category, point))
    ranked.sort(key=lambda row: row[0])
    items = []
    for _dist, title, category, point in ranked[:5]:
        row = {"title": title}
        if category:
            row["category"] = category
        if point is not None:
            row["lat"] = round(point[0], 6)
            row["lon"] = round(point[1], 6)
        items.append(row)
    return {"available": True, "items": items}
