"""Language for text the dashboard shows. Webhook messages stay Korean."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_LANG: ContextVar[str] = ContextVar("teslife_ui_lang", default="ko")

# Korean sentences the forms show. Unknown messages (already English, or user data) pass through.
ERRORS = {
    "DOT는 생산 주차·연도 네 자리(예: 2525)로 적어 주세요":
        "Enter the DOT week and year as four digits (for example 2525).",
    "DOT 생산 시기가 미래예요. 주차 두 자리 + 연도 두 자리(예: 2525)예요":
        "That DOT date is in the future. Use two digits for the week and two for the year (for example 2525).",
    "이미 장착된 세트예요": "That set is already mounted.",
    "장착 날짜·주행거리가 지금 세트 장착보다 앞서요": "The mount date or odometer is before the current set.",
    "이 주행거리에는 이미 다른 세트가 장착돼 있었어요": "Another set was already mounted at that odometer.",
    "장착된 세트가 없어요": "No set is mounted.",
    "탈착 주행거리가 장착보다 작아요": "The removal odometer is less than the mount odometer.",
    "홈 깊이는 mm로 적어 주세요": "Enter tread depth in mm.",
    "홈 깊이를 하나 이상 적어 주세요": "Enter tread depth for at least one tire.",
    "권장 공기압은 42 psi(문기둥 라벨 값) 고정이라 바꿀 수 없어요":
        "Recommended pressure stays at 42 psi (the door-jamb label) and cannot be changed.",
    "address_text (장소) is required": "A place name is required.",
    "위도와 경도가 필요해요": "Latitude and longitude are required.",
    "위도·경도 범위가 아니에요": "That latitude or longitude is out of range.",
    "마지막 위치가 없어요": "There is no last location.",
    "저장된 집이 없어요": "No home is saved.",
}


def parse_lang(value: str | None) -> str:
    v = (value or "").strip().lower().replace("_", "-")
    if v == "en" or v.startswith("en-"):
        return "en"
    return "ko"


def get_lang() -> str:
    return _LANG.get()


def error_text(message: str, lang: str | None = None) -> str:
    lang = get_lang() if lang is None else parse_lang(lang)
    if lang != "en":
        return message
    return ERRORS.get(message, message)


@contextmanager
def bind_lang(query: dict | None) -> Iterator[str]:
    raw = None
    if query:
        vals = query.get("lang") or []
        raw = vals[0] if vals else None
    lang = parse_lang(raw)
    token = _LANG.set(lang)
    try:
        yield lang
    finally:
        _LANG.reset(token)
