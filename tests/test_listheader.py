"""List item headers: trips show time + distance, charges show time + kWh, place below."""
from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path

from ledger import upsert_charge_meta
from process import ensure_process_schema
from process.geocode import cache_key
from web import create_place, list_charges

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
VID = "V1"


def _fn(name: str) -> str:
    i = HTML.index("function " + name + "(")
    j = HTML.index("\n    }\n", i)
    return HTML[i:j]


class ChargePlaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_process_schema(self.conn)
        c = self.conn
        c.execute("INSERT INTO trips (vehicle_id, start_ts, end_ts, km, end_lat, end_lon) VALUES (?,?,?,?,?,?)",
                  (VID, "2026-10-30T09:00:00Z", "2026-10-30T09:30:00Z", 10.0, 37.4901, 126.7801))
        c.execute("INSERT INTO trips (vehicle_id, start_ts, end_ts, km, end_lat, end_lon) VALUES (?,?,?,?,?,?)",
                  (VID, "2026-10-30T12:00:00Z", "2026-10-30T12:30:00Z", 5.0, 37.5101, 127.0601))
        c.execute("INSERT INTO geocode_cache (provider, key, label) VALUES ('nominatim', ?, '예시동')",
                  (cache_key(37.4901, 126.7801),))
        c.execute("INSERT INTO geocode_cache (provider, key, label) VALUES ('nominatim', ?, '삼성동')",
                  (cache_key(37.5101, 127.0601),))
        for start, end in (("2026-10-30T10:00:00Z", "2026-10-30T11:00:00Z"),
                           ("2026-10-30T13:00:00Z", "2026-10-30T14:00:00Z")):
            c.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kwh_added, kind) VALUES (?,?,?,?,?)",
                      (VID, start, end, 20.0, "AC"))
        c.commit()

    def _by_start(self) -> dict:
        return {x["start_ts"]: x for x in list_charges(self.conn, VID)}

    def test_auto_place_from_last_trip_end(self) -> None:
        got = self._by_start()
        self.assertEqual(got["2026-10-30T10:00:00Z"]["place_auto"], "예시동")
        self.assertEqual(got["2026-10-30T13:00:00Z"]["place_auto"], "삼성동")
        self.assertEqual(got["2026-10-30T10:00:00Z"]["place"], "예시동")

    def test_favorite_place_name_wins(self) -> None:
        create_place(self.conn, VID, {"name": "집", "lat": 37.4901, "lon": 126.7801, "radius_m": 150})
        self.assertEqual(self._by_start()["2026-10-30T10:00:00Z"]["place"], "예시동")

    def test_saved_place_replaces_auto(self) -> None:
        upsert_charge_meta(self.conn, VID, {"charge_id": "2026-10-30T10:00:00Z", "location_text": "회사 주차장 B2"})
        got = self._by_start()["2026-10-30T10:00:00Z"]
        self.assertEqual(got["place"], "회사 주차장 B2")
        self.assertEqual(got["place_auto"], "예시동")

    def test_no_trip_before_no_place(self) -> None:
        self.conn.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kwh_added, kind) VALUES (?,?,?,?,?)",
                          (VID, "2026-10-29T10:00:00Z", "2026-10-29T11:00:00Z", 5.0, "AC"))
        got = self._by_start()["2026-10-29T10:00:00Z"]
        self.assertIsNone(got["place_auto"])
        self.assertIsNone(got["place"])

    def test_no_schema_change(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(charges)")}
        self.assertFalse({"lat", "lon", "place", "place_auto"} & cols)


class HeaderHtmlTest(unittest.TestCase):
    def test_trip_header_time_distance_route(self) -> None:
        body = _fn("renderTripRows")
        self.assertIn("fmtTripDistance(t.km)", body)
        self.assertIn("tripListEnergy(t)", body)
        self.assertIn("tripLabel(t)", body)

    def test_list_efficiency_hides_under_min_trip(self) -> None:
        body = _fn("tripListEnergy")
        self.assertIn("km_per_kwh", body)
        self.assertIn("Number(t.km) < 0.1", body)
        self.assertIn("fmt(t.kwh, 2)", body)

    def test_efficiency_in_trip_detail(self) -> None:
        self.assertIn('add("전비"', _fn("tripDetailPairs"))

    def test_charge_header_time_kwh_place(self) -> None:
        body = _fn("renderChargeRows")
        self.assertIn("chargePlace(c)", body)
        self.assertIn("trip-route-label", body)
        self.assertIn('fmt(charger, 1) + " kWh"', body)
        for gone in ("start_soc", "trip-num", "fmtKrw"):
            self.assertNotIn(gone, body)

    def test_charge_detail_place_and_edit(self) -> None:
        self.assertIn('add("장소"', _fn("chargeDetailPairs"))
        self.assertIn("chargePlace(c)", _fn("chargeDetailPairs"))
        self.assertIn("fillChargeStations(form, c)", _fn("openChargeSheet"))
        self.assertIn('id="charge-sheet-edit"', HTML)
        self.assertIn('id="place-map"', HTML)
        self.assertNotIn('id="charge-sheet-loc"', HTML)

    def test_place_line_wraps_below(self) -> None:
        self.assertIn(".trip-route-label { display: block; flex: 0 0 100%;", HTML)


if __name__ == "__main__":
    unittest.main()
