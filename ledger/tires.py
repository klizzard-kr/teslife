"""Recommended cold tire pressure: fixed 42 psi.

Official source: Tesla says to keep the cold pressures on the Tire and Loading Information label
(driver door pillar), checked after the car has been stationary for over three hours. For this car
(Model Y Premium RWD 2025+, option $WY19P 19" Crossflow, 255/45R19) the label value is 42 psi /
290 kPa (2.9 bar) front and rear. Tesla gives no separate seasonal or tire-type value and the
evidence for seasonal +/- rules is weak, so it is a code constant used only for chat
alerts (tire_low, slow leak, TPMS warning, trip texts). The web no longer edits or shows it.
"""
from __future__ import annotations

import sqlite3
from typing import Any

PSI_PER_BAR = 14.5038
LABEL_BAR = 2.9            # 290 kPa / 42 psi on the door label (19" 255/45R19)
LOW_PSI = 39.0             # warn only when the printed psi is strictly under this
LOW_MARGIN_BAR = 0.2       # kept for older callers; the warning line is LOW_PSI
WHEEL = {"code": "WY19P", "name": "19인치 Crossflow", "size": "255/45R19"}


def get_tire_settings(conn: sqlite3.Connection | None = None, vid: str | None = None) -> dict[str, Any]:
    """Constant settings (conn/vid kept for callers; nothing is read)."""
    rec = {"front": LABEL_BAR, "rear": LABEL_BAR}
    return {
        "wheel": dict(WHEEL),
        "recommended": rec,
        "recommended_psi": {ax: round(v * PSI_PER_BAR) for ax, v in rec.items()},
        "low_margin_bar": LOW_MARGIN_BAR,
        "low_psi": LOW_PSI,
        "label_bar": LABEL_BAR,
    }


def recommended_for(settings: dict[str, Any], wheel: str) -> float:
    return settings["recommended"]["front" if wheel in ("fl", "fr") else "rear"]


def displayed_psi(bar: float) -> float:
    """Same 1-decimal psi the home tile prints (``toFixed(1)``)."""
    return float(f"{float(bar) * PSI_PER_BAR:.1f}")


def displayed_low_wheels(pressures: dict[str, Any], settings: dict[str, Any] | None = None) -> list[str]:
    """Wheels whose printed psi is strictly under 39.0.

    The comparison is that one-decimal psi, not the bar figure and not the
    42 psi label. A tile that reads 39.0 or 41.7 is not low. 38.9 is.
    """
    del settings
    low = []
    for wheel in ("fl", "fr", "rl", "rr"):
        raw = (pressures or {}).get(wheel)
        if not isinstance(raw, (int, float)):
            continue
        if displayed_psi(raw) < LOW_PSI:
            low.append(wheel)
    return low


def low_wheels(reading: dict[str, Any] | None, settings: dict[str, Any]) -> list[tuple[str, float, float]]:
    """[(wheel, value_bar, below_recommended_bar)] when the printed psi is under 39.0."""
    out = []
    if not reading:
        return out
    for w in ("fl", "fr", "rl", "rr"):
        v = reading.get(w)
        if not isinstance(v, (int, float)):
            continue
        if displayed_psi(v) < LOW_PSI:
            below = recommended_for(settings, w) - float(v)
            out.append((w, float(v), below))
    return out


def psi_text(bar: float, digits: int = 1) -> str:
    return f"{bar * PSI_PER_BAR:.{digits}f}"


def rec_text(settings: dict[str, Any]) -> str:
    """'42 psi' when front and rear match, else '앞 42 · 뒤 44 psi'."""
    f, r = settings["recommended_psi"]["front"], settings["recommended_psi"]["rear"]
    return f"{f} psi" if f == r else f"앞 {f} · 뒤 {r} psi"
