"""Place text for a charge, from a public station name and a local search.

Order for one station: city stem plus the catalog name, then city plus the
road plus 전기차충전소. A hit counts only when it is an EV charger and the
road address matches the catalog (경기 and 경기도 are the same). Several
different names are left empty. No hit uses the catalog name.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from process.naver_maps import is_ev_charger_category, strip_charger_place, strip_title

# Longer tails first so a short name is not cut out of a longer one.
_TAILS = (
    "LG유플러스 볼트업",
    "나이스차저(NICE인프라)",
    "나이스차저",
    "SK일렉링크",
    "플러그링크",
    "한국전력",
    "파워큐브",
    "차지비",
    "에버온",
    "채비",
)
_NUMBER = re.compile(r"[\s\-·]*\d+$")
_CITY = re.compile(r"[가-힣]{1,12}시")
# Full province and city forms, longest first. Short forms are the common key.
_FOLDS = (
    ("서울특별시", "서울"),
    ("부산광역시", "부산"),
    ("대구광역시", "대구"),
    ("인천광역시", "인천"),
    ("광주광역시", "광주"),
    ("대전광역시", "대전"),
    ("울산광역시", "울산"),
    ("세종특별자치시", "세종"),
    ("강원특별자치도", "강원"),
    ("전북특별자치도", "전북"),
    ("제주특별자치도", "제주"),
    ("충청북도", "충북"),
    ("충청남도", "충남"),
    ("전라북도", "전북"),
    ("전라남도", "전남"),
    ("경상북도", "경북"),
    ("경상남도", "경남"),
    ("경기도", "경기"),
    ("강원도", "강원"),
    ("제주도", "제주"),
)


def city_stem(addr: Any) -> str:
    """시 name with the trailing 시 removed. 서울특별시 becomes 서울."""
    text = str(addr or "")
    for token in _CITY.findall(text):
        if token.endswith(("특별시", "광역시", "자치시")):
            continue
        return token[:-1]
    if "서울" in text:
        return "서울"
    return ""


def fold_road(addr: Any) -> str:
    """Road address with spaces and province abbreviations folded together."""
    text = re.sub(r"\s+", "", str(addr or ""))
    for src, dst in _FOLDS:
        text = text.replace(src, dst)
    return text


def roads_match(left: Any, right: Any) -> bool:
    a = fold_road(left)
    b = fold_road(right)
    return bool(a) and a == b


def strip_operator_tail(title: Any) -> str:
    """Drop a trailing operator label and a trailing number."""
    text = strip_charger_place(strip_title(title))
    changed = True
    while changed and text:
        changed = False
        text = _NUMBER.sub("", text).strip(" -·")
        for tail in _TAILS:
            if text.endswith(tail):
                text = text[: -len(tail)].strip(" -·")
                changed = True
                break
    return text


def _road_of(item: dict[str, Any]) -> str:
    for key in ("roadAddress", "roadAddr", "address"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    return ""


def name_from_hits(items: list[dict[str, Any]] | None, ministry_addr: str) -> str | None:
    """One cleaned title, '' when the hits disagree, None when none qualify."""
    names: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if not is_ev_charger_category(item.get("category") or item.get("categoryName")):
            continue
        if not roads_match(_road_of(item), ministry_addr):
            continue
        title = strip_operator_tail(item.get("title"))
        if title and title not in names:
            names.append(title)
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    return ""


def resolve_place(
    stations: list[dict[str, Any]] | None,
    search: Callable[[str], list[dict[str, Any]] | None],
) -> str | None:
    """A place name, '' when it cannot be one name, None when there is no station.

    None leaves the caller free to try a reverse geocode. '' means stop.
    """
    rows = [row for row in (stations or []) if isinstance(row, dict)]
    usable = [row for row in rows if str(row.get("stat_nm") or "").strip() or str(row.get("addr") or "").strip()]
    if not usable:
        return None
    chosen: list[str] = []
    saw_hit = False
    for row in usable:
        name = str(row.get("stat_nm") or "").strip()
        addr = str(row.get("addr") or "").strip()
        city = city_stem(addr)
        queries = []
        first = " ".join(part for part in (city, name) if part)
        second = " ".join(part for part in (city, addr, "전기차충전소") if part)
        if first:
            queries.append(first)
        if second and second not in queries:
            queries.append(second)
        picked: str | None = None
        for query in queries:
            try:
                hits = search(query)
            except Exception:
                hits = []
            got = name_from_hits(list(hits or []), addr)
            if got is None:
                continue
            saw_hit = True
            if got == "":
                return ""
            picked = got
            break
        if picked and picked not in chosen:
            chosen.append(picked)
    if len(chosen) == 1:
        return chosen[0]
    if len(chosen) > 1:
        return ""
    if saw_hit:
        return ""
    ministry = []
    for row in usable:
        name = str(row.get("stat_nm") or "").strip()
        if name and name not in ministry:
            ministry.append(name)
    if len(ministry) == 1:
        return ministry[0]
    return ""
