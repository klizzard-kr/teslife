"""시·군 from an offline boundary file.

Polygons are simplified coastlines in ``process/data/sigungu.json``. A city that
only has 일반구 (수원, 성남, 부천, …) is stored under the city code, so 부천 is
41190. 자치구 (서울·부산·…) keep the district code. No network and no extra
packages.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "sigungu.json"
_POLYGONS: list[tuple[str, list[list[tuple[float, float]]]]] | None = None


def _load() -> list[tuple[str, list[list[tuple[float, float]]]]]:
    global _POLYGONS
    if _POLYGONS is None:
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
        _POLYGONS = [
            (item["code"], [[(float(x), float(y)) for x, y in ring] for ring in item["rings"]])
            for item in raw["zscode"]
        ]
    return _POLYGONS


def _inside(lng: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < cross:
                inside = not inside
        j = i
    return inside


# Official 시·군·구 names. A repeated name such as 중구 is kept on every code;
# zscode_from_name uses it only when the text names one of them.
_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "11110": ("종로구", ()), "11140": ("중구", ("서울중구", "서울특별시중구")),
    "11170": ("용산구", ()), "11200": ("성동구", ()), "11215": ("광진구", ()),
    "11230": ("동대문구", ()), "11260": ("중랑구", ()), "11290": ("성북구", ()),
    "11305": ("강북구", ()), "11320": ("도봉구", ()), "11350": ("노원구", ()),
    "11380": ("은평구", ()), "11410": ("서대문구", ()), "11440": ("마포구", ()),
    "11470": ("양천구", ()), "11500": ("강서구", ()), "11530": ("구로구", ()),
    "11545": ("금천구", ()), "11560": ("영등포구", ()), "11590": ("동작구", ()),
    "11620": ("관악구", ()), "11650": ("서초구", ()), "11680": ("강남구", ()),
    "11710": ("송파구", ()), "11740": ("강동구", ()),
    "26110": ("중구", ("부산중구", "부산광역시중구")),
    "26350": ("해운대구", ()),
    "41110": ("수원시", ()), "41130": ("성남시", ()), "41190": ("부천시", ()),
    "44270": ("당진시", ()),
    "50110": ("제주시", ()), "50130": ("서귀포시", ()),
}
_CITY = (
    ("11", "서울"), ("26", "부산"), ("27", "대구"), ("28", "인천"),
    ("29", "광주"), ("30", "대전"), ("31", "울산"), ("36", "세종"),
    ("41", "경기"), ("42", "강원"), ("43", "충북"), ("44", "충남"),
    ("45", "전북"), ("46", "전남"), ("47", "경북"), ("48", "경남"),
    ("50", "제주"), ("51", "강원"), ("52", "전북"),
)


def short_name(official: str) -> str:
    """당진시 → 당진. A one-character stem such as 중구 stays whole."""
    for suffix in ("특별자치시", "시", "군", "구"):
        if official.endswith(suffix) and len(official) - len(suffix) >= 2:
            return official[: -len(suffix)]
    return official


def zscode_from_name(text: str | None) -> str | None:
    """Map a 시·군 name onto one code. An ambiguous name returns None."""
    compact = "".join(str(text or "").split())
    if not compact:
        return None
    alias_hits = [
        code for code, (_official, aliases) in _NAMES.items()
        if any(alias and alias in compact for alias in aliases)
    ]
    if len(set(alias_hits)) == 1:
        return alias_hits[0]
    official_hits = [
        code for code, (official, _aliases) in _NAMES.items()
        if official and official in compact
    ]
    unique = list(dict.fromkeys(official_hits))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        for prefix, token in _CITY:
            if token in compact:
                narrowed = [code for code in unique if code.startswith(prefix)]
                if len(set(narrowed)) == 1:
                    return narrowed[0]
        return None
    short_hits = []
    for code, (official, _aliases) in _NAMES.items():
        short = short_name(official)
        if short != official and len(short) >= 2 and short in compact:
            short_hits.append(code)
    unique_short = list(dict.fromkeys(short_hits))
    if len(unique_short) == 1:
        return unique_short[0]
    return None


def query_prefix(code: str | None) -> str:
    """시·군 words to put in front of a place search. Not a 동 name."""
    item = _NAMES.get(code or "")
    if not item:
        return ""
    official = item[0]
    same = [key for key, (name, _aliases) in _NAMES.items() if name == official]
    if len(same) > 1:
        token = next((city for prefix, city in _CITY if str(code).startswith(prefix)), "")
        return f"{token} {official}".strip()
    return short_name(official)


def zscode_at(lat: float | None, lng: float | None) -> str | None:
    """Administrative code for a point, or None when it is outside every polygon."""
    if lat is None or lng is None:
        return None
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    for code, rings in _load():
        for ring in rings:
            if _inside(lng_f, lat_f, ring):
                return code
    return None
