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
            if key in seen or cached(conn, geocoder.name, lat, lon)[0]:
                continue
            pair = getattr(geocoder, "reverse_labels", None)
            cost = 2 if callable(pair) else 1
            if done + cost > limit:
                break
            seen.add(key)
            if callable(pair):
                label, label_en = pair(lat, lon)
            else:
                label, label_en = geocoder.reverse(lat, lon), None
            done += cost
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache (provider, key, label, label_en, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (geocoder.name, key, label, label_en,
                     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
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
