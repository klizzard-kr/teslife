"""Official Tesla maintenance links for the 정비 더보기 sheet.

Korean owner's manual and DIY pages when Tesla publishes them, otherwise the
Korean service manual. Nothing here is a third-party page. A link with
official False would be labeled 비공식; every entry below is official.
"""

from __future__ import annotations

from typing import Any

OWNER = "https://www.tesla.com/ownersmanual/modely/ko_kr/"
DIY = "https://service.tesla.com/docs/Public/diy/modely/ko_kr/"
SERVICE = "https://service.tesla.com/docs/ModelY/ServiceManual/2025/ko-kr/"

SCHEDULE = OWNER + "GUID-E95DAAD9-646E-4249-9930-B109ED7B1D91.html"
TIRES = OWNER + "GUID-94F63B13-EA2C-45D9-83AB-5DCA6295D587.html"
BRAKES = OWNER + "GUID-3DFFB071-C0F6-474D-8A45-17BE1A006365.html"
WIPER_OM = OWNER + "GUID-A5C33F3D-E41D-4671-ADE5-C2FB73203323.html"
WASHER_OM = OWNER + "GUID-197C481B-B4D4-4973-9772-2AF488442CE1.html"


def _link(label: str, href: str, kind: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"label": label, "href": href, "official": True}
    if kind:
        row["kind"] = kind  # inspect or replace: a DIY procedure, not just the schedule
    return row


# Seed item id -> official pages. DIY procedures carry kind inspect or replace.
DOC_LINKS: dict[str, list[dict[str, Any]]] = {
    "cons-tire-rotation": [
        _link("정비 주기", SCHEDULE),
        _link("타이어 관리", TIRES),
        _link("타이어 트레드 확인", DIY + "GUID-B277C6AC-90A2-40DB-A6D9-B467972E051E.html", "inspect"),
        _link("타이어 공기압", DIY + "GUID-DD35EB95-B06A-4F72-8C40-EE98E95A8A8D.html", "inspect"),
    ],
    "cons-cabin-filter": [
        _link("정비 주기", SCHEDULE),
        _link("실내 공기 필터 교체", DIY + "GUID-1C689042-6645-4617-B270-7F2DA9A5624E.html", "replace"),
    ],
    "cons-hepa-filter": [
        _link("정비 주기", SCHEDULE),
        _link("HEPA 필터 교체", DIY + "GUID-80B910A3-D8A6-4F15-BC53-3279B923C43D.html", "replace"),
    ],
    "cons-brake-fluid": [
        _link("정비 주기", SCHEDULE),
        _link("브레이크액 확인", DIY + "GUID-1D888517-65DF-4501-9F8A-90A7E879068F.html", "inspect"),
        _link("브레이크액 점검", SERVICE + "GUID-16F758A2-EC10-4002-BB9A-4BD0C30DD845.html", "inspect"),
        _link("브레이크액 교환", SERVICE + "GUID-B344A192-0FC5-4D88-B71F-2D228FA76B0B.html", "replace"),
    ],
    "cons-wiper": [
        _link("정비 주기", SCHEDULE),
        _link("와이퍼", WIPER_OM),
        _link("와이퍼 블레이드 교체", DIY + "GUID-949BFDA4-8D73-43A6-AAA5-98AE1129AC25.html", "replace"),
    ],
    "cons-caliper": [
        _link("정비 주기", SCHEDULE),
        _link("정비 설명서", SERVICE),
    ],
    "cons-washer": [
        _link("정비 주기", SCHEDULE),
        _link("워셔액", WASHER_OM),
        _link("워셔액 보충", DIY + "GUID-3A80A08E-9DCB-45A4-9796-BCD9153A4A00.html", "inspect"),
    ],
    "cons-battery-full": [
        _link("정비 주기", SCHEDULE),
        _link("충전", OWNER + "GUID-E95DAAD9-646E-4249-9930-B109ED7B1D91.html"),
    ],
    "cons-ac-desiccant": [
        _link("정비 주기", SCHEDULE),
        _link("정비 설명서", SERVICE),
    ],
    "cons-coolant": [
        _link("정비 주기", SCHEDULE),
        _link("냉각수 배출·보충", SERVICE + "GUID-29F552D0-C7B4-40D8-A497-30FE48EE76DE.html"),
    ],
    "cons-tires-season": [
        _link("타이어 관리", TIRES),
        _link("타이어 트레드 확인", DIY + "GUID-B277C6AC-90A2-40DB-A6D9-B467972E051E.html", "inspect"),
    ],
    "cons-12v-battery": [
        _link("저전압 배터리", DIY + "GUID-63C95A3E-7F0B-476D-814B-FF2A39B65A0E.html"),
        _link("점프 시동", DIY + "GUID-3567D5F4-A5F4-4323-8BE0-023D5438FFC6.html"),
    ],
    "cons-brake-pads": [
        _link("제동", BRAKES),
        _link("정비 설명서", SERVICE),
    ],
    "cons-alignment": [
        _link("정비 주기", SCHEDULE),
        _link("정비 설명서", SERVICE),
    ],
}


def doc_links(item_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in DOC_LINKS.get(item_id, ())]
