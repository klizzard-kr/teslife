"""Group a public charger operator by busiId and show bnm.

busiNm is not the label. Names that share one busiId are one operator.
Older rows stored a busiNm variant. Lookup still finds those rows.
"""

from __future__ import annotations

from typing import Any

# Names that the public catalog has used for one operator. The screen shows bnm.
_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "busi_id": "PW",
        "bnm": "파워큐브",
        "names": (
            "파워큐브",
            "파워큐브(SP)",
            "파워큐브(sp)",
            "파워큐브코리아",
            "파워큐브코리아(주)",
            "powercube",
        ),
    },
)


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "")


def resolve(value: Any) -> dict[str, Any] | None:
    """The busiId group for a code, a bnm, or an older name. None when unknown."""
    fold = _fold(value)
    if not fold:
        return None
    for group in _GROUPS:
        if fold == _fold(group["busi_id"]) or fold == _fold(group["bnm"]):
            return group
        if any(fold == _fold(name) for name in group["names"]):
            return group
    return None


def display_name(busi_id: Any = "", bnm: Any = "", busi_nm: Any = "") -> str:
    """The name on screen. bnm wins. A known alias becomes that bnm."""
    label = str(bnm or "").strip()
    if label:
        found = resolve(label)
        return str(found["bnm"]) if found else label
    found = resolve(busi_id) or resolve(busi_nm)
    if found:
        return str(found["bnm"])
    return str(busi_nm or "").strip()


def busi_code(value: Any) -> str:
    """busiId when this value belongs to a known group, else empty."""
    found = resolve(value)
    return str(found["busi_id"]) if found else ""


def group_keys(value: Any) -> list[str]:
    """Stored operator strings that belong with this value, busiId first."""
    raw = str(value or "").strip()
    if not raw:
        return []
    found = resolve(raw)
    if not found:
        return [raw]
    keys = [str(found["busi_id"]), str(found["bnm"]), *[str(name) for name in found["names"]]]
    out: list[str] = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    if raw not in out:
        out.append(raw)
    return out


def same_operator(left: Any, right: Any) -> bool:
    a = resolve(left)
    b = resolve(right)
    if a and b:
        return a["busi_id"] == b["busi_id"]
    if a:
        return _fold(right) in {_fold(key) for key in group_keys(left)}
    if b:
        return _fold(left) in {_fold(key) for key in group_keys(right)}
    return bool(_fold(left)) and _fold(left) == _fold(right)
