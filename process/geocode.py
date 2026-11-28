"""Reverse geocoding for trip start/end labels, behind a small provider interface.

Only the process loop calls this (never the web request path), at most
MAX_PER_CYCLE lookups per cycle and one request per second, and every answer is
cached in `geocode_cache` by rounded coordinates, so a place is asked once.

When NAVER_MAPS_CLIENT_ID and NAVER_MAPS_CLIENT_SECRET are set, Naver is used
first and Nominatim is the fallback. Without those keys, TESLIFE_GEOCODER
selects "nominatim"; unset or "off" disables lookup. Nominatim needs no key;
its usage policy asks for an identifying User-Agent, at most 1 request/s and
caching, which this module follows.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Protocol

CACHE_DIGITS = 3  # ~100 m cells
MAX_PER_CYCLE = 4
MIN_INTERVAL_S = 1.1
USER_AGENT = "teslife/9 (personal vehicle log; reverse geocoding of own trips)"


class Geocoder(Protocol):
    name: str

    def reverse(self, lat: float, lon: float) -> str | None: ...


_DONG = re.compile(r"[가-힣A-Za-z0-9]+(?:동|읍|면)")
_GU = re.compile(r"[가-힣]+구")
_GUN = re.compile(r"[가-힣]+군")
_SI = re.compile(r"[가-힣]+시")
_METRO = re.compile(r"[가-힣]+(?:특별시|광역시|특별자치시|특별자치도)")
# English admin names for Korean places, e.g. "Yeoksam 1-dong", "Yeoksam 1(il)-dong", "Gangnam-gu".
_EN_DONG = re.compile(
    r"[A-Za-z][A-Za-z'’.-]*(?:[\s-][A-Za-z0-9][A-Za-z0-9'’().-]*)*-(?:dong|eup|myeon)\b",
    re.IGNORECASE,
)
_EN_GU = re.compile(r"[A-Za-z][A-Za-z'’-]*-gu\b", re.IGNORECASE)
_EN_GUN = re.compile(r"[A-Za-z][A-Za-z'’-]*-gun\b", re.IGNORECASE)
_EN_SI = re.compile(r"[A-Za-z][A-Za-z'’-]*-si\b", re.IGNORECASE)
_KO_ADMIN_KEYS = ("quarter", "suburb", "neighbourhood", "village", "town", "city_district", "borough", "city", "county")
_LOCALITY_NEAR = ("neighbourhood", "suburb", "quarter")
_LOCALITY_CITY = ("city", "town", "village")


def _has_ko_admin(text: str) -> bool:
    return bool(
        _DONG.search(text) or _GU.search(text) or _GUN.search(text) or _SI.search(text) or _METRO.search(text)
    )


def admin_area(label: str | None) -> str | None:
    """Smallest admin unit in a place string: 동·읍·면, else 시·군·구.

    Road names and building numbers are dropped when an admin unit is present.
    English names of Korean places use the same order (-dong/-eup/-myeon, else -gu/-gun/-si),
    so 'Yeoksam 1-dong, Gangnam-gu, Seoul' becomes 'Yeoksam 1-dong'.
    A name with no admin unit (a saved place such as 집, or an overseas locality already
    shortened) is returned unchanged.
    """
    if label is None:
        return None
    text = str(label).strip()
    if not text:
        return text
    dongs = _DONG.findall(text)
    if dongs:
        return dongs[-1]
    gus = _GU.findall(text)
    if gus:
        return gus[-1]
    guns = _GUN.findall(text)
    if guns:
        return guns[-1]
    sis = [s for s in _SI.findall(text) if not s.endswith(("특별시", "광역시", "자치시"))]
    if sis:
        return sis[-1]
    metro = _METRO.search(text)
    if metro:
        return metro.group(0)
    en_dongs = _EN_DONG.findall(text)
    if en_dongs:
        return en_dongs[-1]
    en_gus = _EN_GU.findall(text)
    if en_gus:
        return en_gus[-1]
    en_guns = _EN_GUN.findall(text)
    if en_guns:
        return en_guns[-1]
    en_sis = _EN_SI.findall(text)
    if en_sis:
        return en_sis[-1]
    return text



@dataclass(frozen=True)
class AdminParts:
    """Administrative units from 동 upward. Empty string means that level is absent."""

    dong: str = ""
    gu: str = ""
    si: str = ""
    sido: str = ""


UP_SEP = " · "
_KO_SIDO = re.compile(r"(특별시|광역시|특별자치시|특별자치도|도)$")
_KO_DONG = re.compile(r"(동|읍|면|리)$")
_KO_SI_GU = re.compile(r"^(.+시)\s+(.+구)$")
_EN_SI_GU = re.compile(r"^(.+?-si)\s+(.+?-gu)$", re.IGNORECASE)
_EN_SIDO = re.compile(
    r"(?:-do|Special City|Metropolitan City|Special Self-Governing (?:City|Province))$",
    re.IGNORECASE,
)
_EN_DONG_END = re.compile(r"-(?:dong|eup|myeon|ri)\b", re.IGNORECASE)
_EN_GU_END = re.compile(r"-gu\b", re.IGNORECASE)
_EN_GUN_END = re.compile(r"-gun\b", re.IGNORECASE)
_EN_SI_END = re.compile(r"-si\b", re.IGNORECASE)


def classify_admin(name: str | None) -> str | None:
    """Return 'dong', 'gu', 'si', or 'sido' for a Korean or English admin name."""
    text = (name or "").strip()
    if not text:
        return None
    if _KO_SIDO.search(text) and not text.endswith("동"):
        return "sido"
    if _KO_DONG.search(text):
        return "dong"
    if text.endswith("구"):
        return "gu"
    if text.endswith("군"):
        return "si"
    if text.endswith("시"):
        return "si"
    if _EN_SIDO.search(text):
        return "sido"
    if _EN_DONG_END.search(text):
        return "dong"
    if _EN_GU_END.search(text):
        return "gu"
    if _EN_GUN_END.search(text) or _EN_SI_END.search(text):
        return "si"
    return None


def split_si_gu(text: str) -> list[str]:
    """Split a combined 시+구 token such as a made-up '해솔시 별빛구'."""
    raw = (text or "").strip()
    if not raw:
        return []
    m = _KO_SI_GU.match(raw)
    if m:
        return [m.group(1), m.group(2)]
    m = _EN_SI_GU.match(raw)
    if m:
        return [m.group(1), m.group(2)]
    return [raw]


def _set_part(bucket: dict[str, str], kind: str | None, name: str) -> None:
    if not kind or not name or bucket.get(kind):
        return
    bucket[kind] = name


def parts_from_area_names(area1: str = "", area2: str = "", area3: str = "",
                          area4: str = "") -> AdminParts:
    """Build parts from Naver-style area names. Road and building strings are not accepted."""
    bucket: dict[str, str] = {"dong": "", "gu": "", "si": "", "sido": ""}
    a1, a2, a3, a4 = (area1 or "").strip(), (area2 or "").strip(), (area3 or "").strip(), (area4 or "").strip()
    if a1:
        _set_part(bucket, classify_admin(a1) or "sido", a1)
    for token in split_si_gu(a2):
        _set_part(bucket, classify_admin(token), token)
    # area3 is 읍·면·동, area4 is 리. Prefer 동/읍/면 when both exist.
    if a3:
        _set_part(bucket, classify_admin(a3) or "dong", a3)
    if a4 and not bucket["dong"]:
        _set_part(bucket, classify_admin(a4) or "dong", a4)
    return AdminParts(**bucket)


def parts_from_nominatim(data: dict[str, Any] | None) -> AdminParts:
    """Admin parts from a Nominatim jsonv2 body. Road and house number are ignored."""
    if not isinstance(data, dict):
        return AdminParts()
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    bucket: dict[str, str] = {"dong": "", "gu": "", "si": "", "sido": ""}
    # Role-based keys, then classify (a city_district may be a 구).
    role_keys = {
        "dong": ("quarter", "suburb", "neighbourhood", "village"),
        "gu": ("city_district", "borough"),
        "si": ("city", "town", "county"),
        "sido": ("state", "province"),
    }
    # Collect every admin-looking value, but skip road/house/postcode.
    skip = {"road", "house_number", "postcode", "country", "country_code",
            "amenity", "building", "shop", "tourism", "leisure", "highway",
            "pedestrian", "residential", "isolated_dwelling"}
    for key, val in addr.items():
        if key in skip or not isinstance(val, str) or not val.strip():
            continue
        name = val.strip()
        kind = classify_admin(name)
        if kind:
            _set_part(bucket, kind, name)
            continue
        for role, keys in role_keys.items():
            if key in keys:
                _set_part(bucket, role, name)
                break
    return AdminParts(**bucket)


def parts_from_naver_payload(payload: dict[str, Any] | None) -> AdminParts:
    if not isinstance(payload, dict):
        return AdminParts()
    from process.naver_maps import region_area_names

    return parts_from_area_names(*region_area_names(payload))


def primary_and_up(parts: AdminParts | None) -> tuple[str | None, str | None]:
    """First unit from 동 (else 구, else 시/군, else 도), and the higher units after it.

    The line below never repeats the first unit. Missing levels are skipped.
    """
    if parts is None:
        return None, None
    order = (parts.dong, parts.gu, parts.si, parts.sido)
    present: list[str] = []
    seen: set[str] = set()
    for name in order:
        text = (name or "").strip()
        if not text or text in seen:
            continue
        present.append(text)
        seen.add(text)
    if not present:
        return None, None
    primary = present[0]
    rest = present[1:]
    return primary, (UP_SEP.join(rest) if rest else None)


def format_road_addr(sido: str = "", sigungu: str = "", road: str = "",
                     number: str = "", building: str = "") -> str:
    """Full road address: '시도 시군구 도로명 건물번호 (건물명)'. Empty if there is no road."""
    if not (road or "").strip() and not (number or "").strip():
        return ""
    text = " ".join(part.strip() for part in (sido, sigungu, road, number) if (part or "").strip())
    bld = (building or "").strip()
    if bld:
        text = f"{text} ({bld})" if text else f"({bld})"
    return text


def format_jibun_addr(sido: str = "", sigungu: str = "", dong: str = "",
                      jibun: str = "") -> str:
    """Full lot address: '시도 시군구 동 지번'. Empty if there is no lot number."""
    if not (jibun or "").strip():
        return ""
    return " ".join(part.strip() for part in (sido, sigungu, dong, jibun) if (part or "").strip())


def addresses_from_naver_payload(payload: dict[str, Any] | None) -> tuple[str, str]:
    from process.naver_maps import detail_address_parts

    road_p, lot_p = detail_address_parts(payload)
    return format_road_addr(**road_p), format_jibun_addr(**lot_p)


def addresses_from_nominatim(data: dict[str, Any] | None) -> tuple[str, str]:
    """Best-effort road line from Nominatim. There is usually no Korean 지번."""
    if not isinstance(data, dict):
        return "", ""
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    sido = str(addr.get("state") or addr.get("province") or "").strip()
    sigungu = " ".join(
        part for part in (
            str(addr.get("city") or addr.get("town") or addr.get("county") or "").strip(),
            str(addr.get("city_district") or addr.get("borough") or "").strip(),
        ) if part
    ).strip()
    road = str(addr.get("road") or "").strip()
    number = str(addr.get("house_number") or "").strip()
    building = str(addr.get("building") or "").strip()
    return format_road_addr(sido, sigungu, road, number, building), ""


def _blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _shortest(addr: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    vals = []
    for key in keys:
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            vals.append(v.strip())
    if not vals:
        return None
    return min(vals, key=len)


def label_from_nominatim(data: dict[str, Any]) -> str | None:
    """Neighbourhood-level label from a Nominatim jsonv2 reply.

    Korean admin names keep the previous priority (quarter, then suburb, …), so
    '역삼1동' still wins over the road and the 구. When no Korean admin suffix
    applies (overseas, and English replies), do not use the road, house number,
    postcode, or the first chunk of display_name. Use the shortest of
    neighbourhood/suburb/quarter, else city/town/village, else state.
    """
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    parts = [v.strip() for v in addr.values() if isinstance(v, str) and v.strip()]
    display = data.get("name") or data.get("display_name") or ""
    display_s = str(display).strip()
    if any(_has_ko_admin(p) for p in parts) or (display_s and _has_ko_admin(display_s)):
        for key in _KO_ADMIN_KEYS:
            v = addr.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        if display_s and _has_ko_admin(display_s):
            return admin_area(display_s)
        return None
    hit = _shortest(addr, _LOCALITY_NEAR) or _shortest(addr, _LOCALITY_CITY)
    if hit:
        return hit
    state = addr.get("state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    return None


class GeocodeLookupError(Exception):
    """Nominatim could not be reached. Callers that can retry should leave the cache row alone."""


class NominatimGeocoder:
    name = "nominatim"
    url = "https://nominatim.openstreetmap.org/reverse"

    def __init__(self, opener: Callable[..., Any] | None = None) -> None:
        self._open = opener or urllib.request.urlopen
        self._last = 0.0

    def _wait(self) -> None:
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _fetch(self, lat: float, lon: float, lang: str) -> dict[str, Any] | None:
        """One reverse request. Raises GeocodeLookupError on transport or parse failure."""
        self._wait()
        q = urllib.parse.urlencode({
            "format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
            "zoom": 17, "addressdetails": 1, "accept-language": lang,
        })
        req = urllib.request.Request(f"{self.url}?{q}", headers={"User-Agent": USER_AGENT})
        try:
            with self._open(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GeocodeLookupError(type(exc).__name__) from exc
        finally:
            self._last = time.monotonic()
        return data if isinstance(data, dict) else None

    def reverse(self, lat: float, lon: float) -> str | None:
        """Korean label. A failed request is a miss (None), same as before."""
        try:
            data = self._fetch(lat, lon, "ko")
        except GeocodeLookupError:
            return None
        return label_from_nominatim(data) if isinstance(data, dict) else None

    def reverse_lang(self, lat: float, lon: float, lang: str) -> str | None:
        """One language. Raises GeocodeLookupError so a backfill can resume later."""
        data = self._fetch(lat, lon, "ko" if lang != "en" else "en")
        label = label_from_nominatim(data) if isinstance(data, dict) else None
        return admin_area(label) if label else None

    def reverse_labels(self, lat: float, lon: float) -> tuple[str | None, str | None]:
        """Korean and English labels. Two requests, each paced by MIN_INTERVAL_S."""
        ko = self.reverse(lat, lon)
        try:
            en = self.reverse_lang(lat, lon, "en")
        except GeocodeLookupError:
            en = None
        return (admin_area(ko) if ko else None), en

    def reverse_admin(self, lat: float, lon: float
                      ) -> tuple[AdminParts | None, AdminParts | None, int, str, str]:
        """Korean and English admin parts, plus a road address when Nominatim has one."""
        ko: AdminParts | None = None
        en: AdminParts | None = None
        road, jibun = "", ""
        calls = 0
        try:
            data = self._fetch(lat, lon, "ko")
            calls += 1
            ko = parts_from_nominatim(data) if isinstance(data, dict) else AdminParts()
            road, jibun = addresses_from_nominatim(data)
        except GeocodeLookupError:
            raise
        try:
            data = self._fetch(lat, lon, "en")
            calls += 1
            en = parts_from_nominatim(data) if isinstance(data, dict) else AdminParts()
        except GeocodeLookupError:
            en = None
        return ko, en, calls, road, jibun

    def search(self, query: str, limit: int = 5, lang: str = "ko") -> list[dict[str, Any]]:
        """Forward search (address or place name) for the charge-location editor; Korea only."""
        self._wait()
        accept = "en" if str(lang).lower().startswith("en") else "ko"
        q = urllib.parse.urlencode({
            "format": "jsonv2", "q": query, "limit": int(limit), "countrycodes": "kr",
            "accept-language": accept,
        })
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}",
                                     headers={"User-Agent": USER_AGENT})
        try:
            with self._open(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return []
        finally:
            self._last = time.monotonic()
        out = []
        for d in data if isinstance(data, list) else []:
            try:
                out.append({"name": str(d.get("display_name") or d.get("name") or ""),
                            "lat": round(float(d["lat"]), 6), "lon": round(float(d["lon"]), 6)})
            except (KeyError, TypeError, ValueError):
                continue
        return out


class NaverGeocoder:
    """Naver reverse geocode. A miss or a transport error falls back to Nominatim."""

    name = "naver"

    def __init__(self, opener: Callable[..., Any] | None = None,
                 fallback: NominatimGeocoder | None = None) -> None:
        self._open = opener
        self._fallback = fallback or NominatimGeocoder(opener=opener)

    def _label(self, lat: float, lon: float) -> str | None:
        from process.naver_maps import LookupError, fetch_reverse, label_from_naver, maps_configured

        if not maps_configured():
            return None
        last: LookupError | None = None
        for _ in range(2):
            try:
                payload = fetch_reverse(lat, lon, self._open)
            except LookupError as exc:
                last = exc
                continue
            label = label_from_naver(payload)
            if label:
                return label
            last = LookupError("empty")
        if last is not None:
            return None
        return None

    def _parts(self, lat: float, lon: float) -> tuple[AdminParts | None, str, str, int]:
        from process.naver_maps import LookupError, fetch_reverse, maps_configured

        if not maps_configured():
            return None, "", "", 0
        last: LookupError | None = None
        calls = 0
        for _ in range(2):
            try:
                payload = fetch_reverse(lat, lon, self._open, orders="roadaddr,addr")
                calls = 1
            except LookupError as exc:
                last = exc
                calls = 1
                continue
            parts = parts_from_naver_payload(payload)
            road, jibun = addresses_from_naver_payload(payload)
            if primary_and_up(parts)[0] or road or jibun:
                return parts, road, jibun, calls
            last = LookupError("empty")
        if last is not None:
            return None, "", "", calls or 1
        return None, "", "", 0

    def reverse_admin(self, lat: float, lon: float
                      ) -> tuple[AdminParts | None, AdminParts | None, int, str, str]:
        parts, road, jibun, calls = self._parts(lat, lon)
        if parts is not None and (primary_and_up(parts)[0] or road or jibun):
            en: AdminParts | None = None
            try:
                data = self._fallback._fetch(lat, lon, "en")
                calls += 1
                en = parts_from_nominatim(data) if isinstance(data, dict) else AdminParts()
            except GeocodeLookupError:
                en = None
            return parts, en, calls, road, jibun
        return self._fallback.reverse_admin(lat, lon)

    def reverse(self, lat: float, lon: float) -> str | None:
        label = self._label(lat, lon)
        if label:
            return admin_area(label)
        return self._fallback.reverse(lat, lon)

    def reverse_lang(self, lat: float, lon: float, lang: str) -> str | None:
        if lang != "en":
            return self.reverse(lat, lon)
        return self._fallback.reverse_lang(lat, lon, lang)

    def reverse_labels(self, lat: float, lon: float) -> tuple[str | None, str | None]:
        label = self._label(lat, lon)
        if label:
            return admin_area(label), None
        return self._fallback.reverse_labels(lat, lon)

    def search(self, query: str, limit: int = 5, lang: str = "ko") -> list[dict[str, Any]]:
        return self._fallback.search(query, limit=limit, lang=lang)


PROVIDERS: dict[str, Callable[[], Geocoder]] = {"nominatim": NominatimGeocoder, "naver": NaverGeocoder}


def from_env() -> Geocoder | None:
    from process.naver_maps import maps_configured

    if maps_configured():
        return NaverGeocoder()
    name = os.environ.get("TESLIFE_GEOCODER", "").strip().lower()
    factory = PROVIDERS.get(name)
    return factory() if factory else None


def cache_key(lat: float, lon: float) -> str:
    return f"{lat:.{CACHE_DIGITS}f},{lon:.{CACHE_DIGITS}f}"


def cached(conn: sqlite3.Connection, provider: str, lat: float, lon: float) -> tuple[bool, str | None]:
    try:
        row = conn.execute(
            "SELECT label FROM geocode_cache WHERE provider = ? AND key = ?", (provider, cache_key(lat, lon))
        ).fetchone()
    except sqlite3.OperationalError:
        return False, None
    return (row is not None), (row[0] if row else None)


def dong_label(conn: sqlite3.Connection, lat: float | None, lon: float | None,
               lang: str = "ko") -> str | None:
    """Cached neighbourhood name (동·읍·면) for a point. Does not read places, and does not call a provider.

    The cache key is rounded to CACHE_DIGITS (~100 m). A saved place name such as 집 is not returned.
    lang="en" uses label_en and does not fall back to the Korean label, so a missing English
    name stays empty. Web requests only read this cache.
    """
    if lat is None or lon is None:
        return None
    try:
        if lang == "en":
            row = conn.execute(
                "SELECT label_en FROM geocode_cache WHERE key = ? AND label_en IS NOT NULL "
                "AND TRIM(label_en) != '' ORDER BY fetched_at DESC LIMIT 1",
                (cache_key(lat, lon),),
            ).fetchone()
            if not row:
                return None
            return admin_area(row[0])
        row = conn.execute(
            "SELECT label FROM geocode_cache WHERE key = ? AND label IS NOT NULL "
            "AND TRIM(label) != '' ORDER BY fetched_at DESC LIMIT 1",
            (cache_key(lat, lon),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return admin_area(row[0]) if row else None


def admin_display(conn: sqlite3.Connection, lat: float | None, lon: float | None,
                  lang: str = "ko") -> tuple[str | None, str | None]:
    """Cached primary unit and the higher-unit line. Does not call a provider.

    The primary value starts at 동·읍·면, then 구, then 시·군, then 도.
    The second value is the remaining units joined with ' · ', or None.
    lang='en' uses the English columns and does not fall back to Korean.
    Older rows that still have only a label return that label and no upper line
    until the process loop refills them.
    """
    if lat is None or lon is None:
        return None, None
    try:
        if lang == "en":
            row = conn.execute(
                "SELECT label_en, gu_en, si_en, sido_en FROM geocode_cache WHERE key = ? "
                "AND (label_en IS NOT NULL OR gu_en IS NOT NULL OR si_en IS NOT NULL "
                "OR sido_en IS NOT NULL) ORDER BY fetched_at DESC LIMIT 1",
                (cache_key(lat, lon),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT label, gu, si, sido FROM geocode_cache WHERE key = ? "
                "AND (label IS NOT NULL OR gu IS NOT NULL OR si IS NOT NULL "
                "OR sido IS NOT NULL) ORDER BY fetched_at DESC LIMIT 1",
                (cache_key(lat, lon),),
            ).fetchone()
    except sqlite3.OperationalError:
        return dong_label(conn, lat, lon, lang), None
    if not row:
        return None, None
    label, gu, si, sido = row
    # Hierarchy has been filled when the 구 column is not NULL ('' counts as filled).
    if gu is not None and si is not None and sido is not None:
        higher = {gu or "", si or "", sido or ""}
        dong = (label or "") if (label and label not in higher) else ""
        parts = AdminParts(dong=dong, gu=gu or "", si=si or "", sido=sido or "")
        return primary_and_up(parts)
    # Not yet refilled: just the smallest unit from the old label.
    return admin_area(label) if label else None, None


def cached_addresses(conn: sqlite3.Connection, lat: float | None, lon: float | None
                     ) -> tuple[str | None, str | None]:
    """Cached full road and 지번 strings. Does not call a provider. Empty means absent."""
    if lat is None or lon is None:
        return None, None
    try:
        row = conn.execute(
            "SELECT road_addr, jibun_addr FROM geocode_cache WHERE key = ? "
            "AND (road_addr IS NOT NULL OR jibun_addr IS NOT NULL) "
            "ORDER BY fetched_at DESC LIMIT 1",
            (cache_key(lat, lon),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if not row:
        return None, None
    road = (row[0] or "").strip() or None
    jibun = (row[1] or "").strip() or None
    return road, jibun


def _latest_location(conn: sqlite3.Connection, vehicle_id: str) -> tuple[float, float] | None:
    """Newest stored Location. Nothing is requested from the car."""
    from process import _latlon, decode_value

    try:
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'Location' "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        point = _latlon(decode_value(row[0]))
    except (TypeError, ValueError):
        return None
    if point is None:
        return None
    return float(point[0]), float(point[1])


def _admin_row(conn: sqlite3.Connection, provider: str, lat: float, lon: float):
    try:
        return conn.execute(
            "SELECT label, label_en, gu, gu_en, si, si_en, sido, sido_en, "
            "road_addr, jibun_addr "
            "FROM geocode_cache WHERE provider = ? AND key = ?",
            (provider, cache_key(lat, lon)),
        ).fetchone()
    except sqlite3.OperationalError:
        try:
            return conn.execute(
                "SELECT label, label_en, gu, gu_en, si, si_en, sido, sido_en, "
                "NULL, NULL "
                "FROM geocode_cache WHERE provider = ? AND key = ?",
                (provider, cache_key(lat, lon)),
            ).fetchone()
        except sqlite3.OperationalError:
            try:
                return conn.execute(
                    "SELECT label, label_en, NULL, NULL, NULL, NULL, NULL, NULL, "
                    "NULL, NULL "
                    "FROM geocode_cache WHERE provider = ? AND key = ?",
                    (provider, cache_key(lat, lon)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None


def _needs_admin(conn: sqlite3.Connection, provider: str, lat: float, lon: float) -> bool:
    """True when this cell has no row, or 구/시/도 or road/지번 columns are still NULL."""
    row = _admin_row(conn, provider, lat, lon)
    if row is None:
        return True
    # gu/si/sido are 2, 4, 6; road_addr/jibun_addr are 8, 9.
    # NULL means not filled; '' means looked up and absent.
    return (
        row[2] is None or row[4] is None or row[6] is None
        or row[8] is None or row[9] is None
    )


def _unpack_admin(result: Any) -> tuple[AdminParts | None, AdminParts | None, int, str, str]:
    """Accept a 3-tuple (older fakes) or a 5-tuple that also has road and 지번 strings."""
    if not isinstance(result, tuple):
        return None, None, 0, "", ""
    if len(result) >= 5:
        ko, en, cost, road, jibun = result[0], result[1], result[2], result[3], result[4]
        return ko, en, int(cost or 0), (road or ""), (jibun or "")
    ko, en, cost = result[0], result[1], result[2]
    return ko, en, int(cost or 0), "", ""


def _lookup_admin(geocoder: Geocoder, lat: float, lon: float
                  ) -> tuple[AdminParts | None, AdminParts | None, int, str, str]:
    fn = getattr(geocoder, "reverse_admin", None)
    if callable(fn):
        return _unpack_admin(fn(lat, lon))
    pair = getattr(geocoder, "reverse_labels", None)
    if callable(pair):
        label, label_en = pair(lat, lon)
        ko = parts_from_area_names("", "", label or "", "") if label else AdminParts()
        en = parts_from_area_names("", "", label_en or "", "") if label_en else AdminParts()
        return ko, en, 2, "", ""
    label = geocoder.reverse(lat, lon)
    ko = parts_from_area_names("", "", label or "", "") if label else AdminParts()
    return ko, AdminParts(), 1, "", ""


def _store_label(conn: sqlite3.Connection, geocoder: Geocoder, lat: float, lon: float) -> int:
    """One cache write for a point, including 구/시/도 when the provider returns them.

    Existing rows that already have the hierarchy columns (even as empty strings) are
    skipped. Rows that only have a dong label are fetched again so the higher units
    can be filled. A transport error leaves the row as it was.
    """
    if not _needs_admin(conn, geocoder.name, lat, lon):
        return 0
    try:
        ko, en, cost, road_addr, jibun_addr = _lookup_admin(geocoder, lat, lon)
    except GeocodeLookupError:
        return 0
    primary, _up = primary_and_up(ko) if ko is not None else (None, None)
    primary_en, _up_en = primary_and_up(en) if en is not None else (None, None)
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    def level(parts: AdminParts | None, name: str) -> str | None:
        if parts is None:
            return None
        return getattr(parts, name) or ""

    with conn:
        conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, label_en, "
            "gu, gu_en, si, si_en, sido, sido_en, road_addr, jibun_addr, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, key) DO UPDATE SET "
            "label = COALESCE(excluded.label, geocode_cache.label), "
            "label_en = COALESCE(excluded.label_en, geocode_cache.label_en), "
            "gu = excluded.gu, gu_en = COALESCE(excluded.gu_en, geocode_cache.gu_en), "
            "si = excluded.si, si_en = COALESCE(excluded.si_en, geocode_cache.si_en), "
            "sido = excluded.sido, sido_en = COALESCE(excluded.sido_en, geocode_cache.sido_en), "
            "road_addr = excluded.road_addr, "
            "jibun_addr = excluded.jibun_addr, "
            "fetched_at = excluded.fetched_at",
            (geocoder.name, cache_key(lat, lon), primary, primary_en,
             level(ko, "gu"), level(en, "gu"),
             level(ko, "si"), level(en, "si"),
             level(ko, "sido"), level(en, "sido"),
             road_addr or "", jibun_addr or "",
             fetched),
        )
    return cost


def fill_car_area_label(conn: sqlite3.Connection, vehicle_id: str,
                        geocoder: Geocoder | None) -> int:
    """Cache a dong name for the newest stored Location when that ~100 m cell is empty."""
    if geocoder is None:
        return 0
    point = _latest_location(conn, vehicle_id)
    if point is None:
        return 0
    try:
        return _store_label(conn, geocoder, point[0], point[1])
    except GeocodeLookupError:
        return 0


def fill_trip_labels(conn: sqlite3.Connection, vehicle_id: str, geocoder: Geocoder | None,
                     limit: int = MAX_PER_CYCLE) -> int:
    """Look up uncached trip start/end coordinates, newest trips first. Returns lookups made."""
    if geocoder is None:
        return 0
    rows = conn.execute(
        "SELECT start_lat, start_lon, end_lat, end_lon FROM trips WHERE vehicle_id = ? "
        "AND (start_lat IS NOT NULL OR end_lat IS NOT NULL) ORDER BY start_ts DESC LIMIT 200",
        (vehicle_id,),
    ).fetchall()
    try:  # charge spots too (an edited spot gets its area name)
        rows = list(conn.execute(
            "SELECT lat, lon, NULL, NULL FROM charge_locations WHERE vehicle_id = ? ORDER BY updated_ts DESC LIMIT 100",
            (vehicle_id,),
        ).fetchall()) + list(rows)
    except sqlite3.OperationalError:
        pass
    done = 0
    seen: set[str] = set()
    for r in rows:
        for lat, lon in ((r[0], r[1]), (r[2], r[3])):
            if lat is None or lon is None or done >= limit:
                continue
            key = cache_key(lat, lon)
            if key in seen or not _needs_admin(conn, geocoder.name, lat, lon):
                continue
            guess = 2 if (hasattr(geocoder, "reverse_admin") or hasattr(geocoder, "reverse_labels")) else 1
            if done + guess > limit:
                break
            seen.add(key)
            used = _store_label(conn, geocoder, lat, lon)
            done += used
            if done >= limit:
                break
    return done


def place_label(conn: sqlite3.Connection, vehicle_id: str, lat: float | None, lon: float | None,
                lang: str = "ko") -> str | None:
    """Name for a spot: the nearest registered place whose radius covers it, else a cached area name.

    A saved place name is returned as stored. `lang="en"` uses label_en and falls back to the
    Korean label. The default stays Korean (webhook copy calls this without a language).
    """
    if lat is None or lon is None:
        return None
    from process.geo import distance_m

    try:
        rows = conn.execute(
            "SELECT name, lat, lon, radius_m FROM places WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    best = None
    for name, plat, plon, radius in rows:
        d = distance_m((lat, lon), (plat, plon))
        if d <= (radius or 0) and (best is None or d < best[0]):
            best = (d, name)
    if best:
        return best[1]
    try:
        if lang == "en":
            row = conn.execute(
                "SELECT label_en, label FROM geocode_cache WHERE key = ? "
                "AND (label_en IS NOT NULL OR label IS NOT NULL) ORDER BY fetched_at DESC LIMIT 1",
                (cache_key(lat, lon),),
            ).fetchone()
            if not row:
                return None
            return row[0] or row[1]
        row = conn.execute(
            "SELECT label FROM geocode_cache WHERE key = ? AND label IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
            (cache_key(lat, lon),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def backfill_english(conn: sqlite3.Connection, geocoder: Any, limit: int | None = None) -> dict[str, int]:
    """Fill label_en for cached places that do not have one yet.

    One request per row, paced by the geocoder. Rows that already have label_en (including an
    empty string when the lookup found no locality) are skipped, so stopping and running again
    is safe. A transport error leaves the row NULL. Only geocode_cache.label_en is written.
    """
    try:
        rows = conn.execute(
            "SELECT provider, key FROM geocode_cache WHERE label IS NOT NULL AND label_en IS NULL "
            "ORDER BY fetched_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"updated": 0, "errors": 0, "pending": 0}
    updated = 0
    errors = 0
    for provider, key in rows:
        if limit is not None and updated + errors >= limit:
            break
        try:
            lat_s, lon_s = str(key).split(",", 1)
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        fetch = getattr(geocoder, "reverse_lang", None)
        try:
            if callable(fetch):
                label = fetch(lat, lon, "en")
            else:
                label = geocoder.reverse(lat, lon)
        except GeocodeLookupError:
            errors += 1
            continue
        stored = "" if not label else admin_area(label) or ""
        with conn:
            conn.execute(
                "UPDATE geocode_cache SET label_en = ? WHERE provider = ? AND key = ? AND label_en IS NULL",
                (stored, provider, key),
            )
        updated += 1
    pending = 0
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM geocode_cache WHERE label IS NOT NULL AND label_en IS NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pending = 0
    return {"updated": updated, "errors": errors, "pending": int(pending or 0)}
