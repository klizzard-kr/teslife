"""Display numbers. A trailing .0 is dropped; other decimals stay.

292 -> 292, 307.2 -> 307.2, 10.50 -> 10.5. The screen uses the same rule in fmt().
"""

from __future__ import annotations


def format_number(value: float, digits: int | None = None) -> str:
    number = float(value)
    if digits is None:
        text = str(number)
        if text.endswith(".0"):
            return text[:-2]
        return text
    text = f"{number:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
