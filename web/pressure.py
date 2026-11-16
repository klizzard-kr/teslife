"""Show tire pressure in psi or bar. Stored values stay in their original unit.

psi on a notice is a stored number. bar on a live tire is a stored bar.
Display converts from that stored number once. It never converts a converted value.
"""

from __future__ import annotations

import re
import sqlite3

BAR_PER_PSI = 0.0689476
PSI_PER_BAR = 14.5038
_PSI = re.compile(r"(\d+(?:\.\d+)?)\s*psi", re.I)


def normalize_unit(value: str | None) -> str:
    return "bar" if str(value or "").strip().lower() == "bar" else "psi"


def get_pressure_unit(conn: sqlite3.Connection, vehicle_id: str) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM process_meta WHERE key = ?",
            (f"pressure_unit:{vehicle_id}",),
        ).fetchone()
    except sqlite3.Error:
        return "psi"
    if not row:
        return "psi"
    return normalize_unit(row[0])


def set_pressure_unit(conn: sqlite3.Connection, vehicle_id: str, unit: str) -> str:
    chosen = normalize_unit(unit)
    if str(unit or "").strip().lower() not in ("psi", "bar"):
        raise ValueError("pressure unit is not allowed")
    with conn:
        conn.execute(
            "INSERT INTO process_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"pressure_unit:{vehicle_id}", chosen),
        )
    return chosen


def format_psi_number(psi: float, unit: str) -> str:
    """Format one stored psi. The caller keeps the original number."""
    if normalize_unit(unit) == "bar":
        return f"{float(psi) * BAR_PER_PSI:.2f} bar"
    return f"{float(psi):.1f} psi"


def format_bar_number(bar: float, unit: str) -> str:
    """Format one stored bar. psi display uses the same factor as the home tiles."""
    if normalize_unit(unit) == "bar":
        return f"{float(bar):.2f} bar"
    return f"{float(bar) * PSI_PER_BAR:.1f} psi"


def convert_psi_text(text: str, unit: str) -> str:
    """Replace stored '42.4 psi' phrases. The input string is not modified by the caller."""
    if normalize_unit(unit) != "bar":
        return text

    def repl(match: re.Match[str]) -> str:
        return format_psi_number(float(match.group(1)), "bar")

    return _PSI.sub(repl, text or "")
