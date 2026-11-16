""": 청구 kWh takes decimals on iPhone; 금액 (원) stays whole won. : 80/40 % guides on the hourly graph."""
from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ledger import connect as ledger_connect, upsert_charge_meta
from process import ensure_process_schema

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _input(name: str) -> str:
    i = HTML.index('id="charge-meta-form"')
    m = re.search(r'<input name="' + name + r'"[^>]*>', HTML[i:])
    assert m, name
    return m.group(0)


class ChargeInputTest(unittest.TestCase):
    def test_kwh_billed_decimal_keypad(self) -> None:
        tag = _input("kwh_billed")
        self.assertIn('inputmode="decimal"', tag)
        self.assertIn('step="any"', tag)
        self.assertNotIn('inputmode="numeric"', tag)

    def test_total_krw_whole_won(self) -> None:
        tag = _input("total_krw")
        self.assertIn('inputmode="numeric"', tag)
        self.assertIn('step="1"', tag)

    def test_submit_keeps_decimals(self) -> None:
        self.assertIn('body[k] = Number(String(body[k]).replace(",", "."))', HTML)

    def test_server_stores_float_kwh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "signals.db"
            c = sqlite3.connect(db)
            ensure_process_schema(c)
            c.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) "
                      "VALUES ('V', '2026-10-30T11:54:46Z', '2026-10-30T15:54:00Z', 'AC', 26.0)")
            c.commit(); c.close()
            conn = ledger_connect(db)
            try:
                m = upsert_charge_meta(conn, "V", {"charge_id": "2026-10-30T11:54:46Z", "kwh_billed": 26.73, "total_krw": 6080})
                self.assertEqual(m["kwh_billed"], 26.73)
                self.assertEqual(m["total_krw"], 6080)
                m = upsert_charge_meta(conn, "V", {"charge_id": "2026-10-30T11:54:46Z", "kwh_billed": "26.73"})
                self.assertEqual(m["kwh_billed"], 26.73)
            finally:
                conn.close()


class Guide80Test(unittest.TestCase):
    """: faint dashed 80 and 40 % guides on the hourly battery graph."""

    def test_guide_line(self) -> None:
        i = HTML.index("function drawBatDay(")
        body = HTML[i:HTML.index("\n    }\n", i)]
        self.assertIn("[80].forEach(v => {", body)
        self.assertNotIn("[80, 40]", body)
        self.assertIn("const yg = T + innerH - (v / 100) * innerH;", body)
        line = body[body.index("class=\"bh-guide bh-guide"):]
        line = line[:line.index("</line>")]
        for attr in ('stroke-dasharray="3 3"', 'stroke-opacity="0.35"', 'stroke-width="1"', 'pointer-events="none"', 'stroke="#8aa0b5"'):
            self.assertIn(attr, line)
        # no label for it: the % labels stay 0/50/100
        self.assertIn("[0, 50, 100].forEach", body)
        self.assertNotIn("80%", body)
        self.assertNotIn("40%", body)
