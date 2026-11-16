""" menu overhaul: charge locations, road usage, period APIs, ledger ranges, 주차·통행 migration."""

from __future__ import annotations

import json
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals  # noqa: E402
from ledger import connect as ledger_connect, csv_export, ledger_summary, list_ledger, migrate_parking_toll  # noqa: E402
from process import _fast_charger, ensure_process_schema, process_db, update_charge_places  # noqa: E402
from process import chargeloc, roads  # noqa: E402
from simulator import generate  # noqa: E402
from web import make_server, menu, serve_in_thread  # noqa: E402

VID = "VINMENU"


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "signals.db"
        conn = sqlite3.connect(self.db)
        ensure_process_schema(conn)
        conn.close()
        self.conn = ledger_connect(self.db)
        self.addCleanup(self.conn.close)

    def charge(self, start: str, end: str, kind: str = "AC", kwh: float = 10.0) -> None:
        self.conn.execute("INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, ?, ?)",
                          (VID, start, end, kind, kwh))
        self.conn.commit()

    def trip(self, start: str, end: str, lat: float, lon: float, km: float = 5.0) -> None:
        self.conn.execute("INSERT INTO trips (vehicle_id, start_ts, end_ts, km, end_lat, end_lon) VALUES (?, ?, ?, ?, ?, ?)",
                          (VID, start, end, km, lat, lon))
        self.conn.commit()


class ChargeLocationTests(_Tmp):
    def test_backfill_order_and_manual_edit(self) -> None:
        self.trip("2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", 37.48, 126.80)
        self.charge("2026-10-01T01:00:00Z", "2026-10-01T03:00:00Z")          # previous trip end -> estimated
        self.charge("2026-09-01T01:00:00Z", "2026-09-01T03:00:00Z")          # no trip, no place -> none
        counts = chargeloc.fill_charge_locations(self.conn, VID)
        self.assertEqual(counts, {"station": 0, "car": 0, "estimated": 1, "none": 1})
        loc = chargeloc.locations_for(self.conn, VID, ["2026-10-01T01:00:00Z"])["2026-10-01T01:00:00Z"]
        self.assertEqual((loc["lat"], loc["lon"], loc["source"]), (37.48, 126.80, "estimated"))
        # manual edit wins and survives another fill
        chargeloc.set_location(self.conn, VID, {"charge_id": "2026-10-01T01:00:00Z", "lat": 37.5, "lon": 126.9})
        chargeloc.fill_charge_locations(self.conn, VID)
        loc = chargeloc.locations_for(self.conn, VID, ["2026-10-01T01:00:00Z"])["2026-10-01T01:00:00Z"]
        self.assertEqual(loc["source"], "manual")
        # reset brings the automatic one back
        out = chargeloc.set_location(self.conn, VID, {"charge_id": "2026-10-01T01:00:00Z", "reset": True})
        self.assertEqual(out["source"], "estimated")
        with self.assertRaises(ValueError):
            chargeloc.set_location(self.conn, VID, {"charge_id": "2026-10-01T01:00:00Z", "lat": 99, "lon": 0})
        with self.assertRaises(KeyError):
            chargeloc.set_location(self.conn, VID, {"charge_id": "nope", "lat": 1, "lon": 1})

    def test_car_location_when_signals_cover_the_start(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS signals (vehicle_id TEXT, ts TEXT, name TEXT, value TEXT)")
        self.conn.execute("INSERT INTO signals VALUES (?, '2026-10-30T10:00:00Z', 'Location', ?)",
                          (VID, json.dumps({"lat": 35.1112, "lon": 129.2011})))
        self.conn.commit()
        self.charge("2026-10-30T11:00:00Z", "2026-10-30T14:00:00Z")
        self.charge("2026-10-29T11:00:00Z", "2026-10-29T14:00:00Z")          # before Location data -> not 'car'
        counts = chargeloc.fill_charge_locations(self.conn, VID)
        self.assertEqual(counts["car"], 1)
        self.assertEqual(counts["none"], 1)

    def _named(self, cid: str, name: str) -> None:
        self.charge(cid, cid.replace("T11", "T13"))
        self.conn.execute("INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, ?, ?)", (VID, cid, name))
        self.conn.commit()

    def test_saved_place_coords_win_over_car_and_trip_end(self) -> None:
        """: manual > saved place > car > previous trip end; place points are not estimates."""
        self.conn.execute("CREATE TABLE IF NOT EXISTS signals (vehicle_id TEXT, ts TEXT, name TEXT, value TEXT)")
        self.conn.execute("INSERT INTO signals VALUES (?, '2026-10-30T10:00:00Z', 'Location', ?)",
                          (VID, json.dumps({"lat": 37.40, "lon": 126.70})))
        self.conn.execute("INSERT INTO stations (vehicle_id, id, name, lat, lon) VALUES (?, 's1', '집', 35.1112, 129.2011)", (VID,))
        self.conn.commit()
        self._named("2026-10-30T11:00:00Z", "집")      # car point exists, place wins
        self._named("2026-10-31T11:00:00Z", "회사")    # unknown place -> car
        update_charge_places(self.conn, VID)
        locs = chargeloc.locations_for(self.conn, VID, ["2026-10-30T11:00:00Z", "2026-10-31T11:00:00Z"])
        self.assertEqual(locs["2026-10-30T11:00:00Z"], {"lat": 35.1112, "lon": 129.2011, "source": "station"})
        self.assertEqual(locs["2026-10-31T11:00:00Z"]["source"], "car")
        # a hand-set point is never replaced
        chargeloc.set_location(self.conn, VID, {"charge_id": "2026-10-30T11:00:00Z", "lat": 37.5, "lon": 126.9})
        update_charge_places(self.conn, VID)
        self.assertEqual(chargeloc.locations_for(self.conn, VID, ["2026-10-30T11:00:00Z"])["2026-10-30T11:00:00Z"]["source"], "manual")
        # reset goes back to the place, and the name removed -> back to the car point
        chargeloc.set_location(self.conn, VID, {"charge_id": "2026-10-30T11:00:00Z", "reset": True})
        self.assertEqual(chargeloc.locations_for(self.conn, VID, ["2026-10-30T11:00:00Z"])["2026-10-30T11:00:00Z"]["source"], "station")
        self.conn.execute("UPDATE charge_meta SET location_text = '' WHERE charge_id = '2026-10-30T11:00:00Z'")
        self.conn.commit()
        update_charge_places(self.conn, VID)
        kept = chargeloc.locations_for(self.conn, VID, ["2026-10-30T11:00:00Z"])["2026-10-30T11:00:00Z"]
        self.assertEqual(kept, {"lat": 35.1112, "lon": 129.2011, "source": "station"})

    def test_place_without_coords_takes_the_busiest_spot(self) -> None:
        self.conn.execute("INSERT INTO stations (vehicle_id, id, name) VALUES (?, 's1', '한빛')", (VID,))
        self.conn.commit()
        spots = [(35.1112, 129.2011), (35.1113, 129.2012), (35.1111, 129.2010), (37.60, 127.00)]
        for i, (la, lo) in enumerate(spots):
            day = f"2026-10-{10 + i:02d}"
            self.trip(f"{day}T09:00:00Z", f"{day}T10:00:00Z", la, lo)
            self._named(f"{day}T11:00:00Z", "한빛")
        update_charge_places(self.conn, VID)
        st = chargeloc.stations_with_coords(self.conn, VID)
        self.assertEqual(len(st), 1)
        self.assertAlmostEqual(st[0]["lat"], 35.1112, places=4)
        self.assertAlmostEqual(st[0]["lon"], 129.2011, places=4)
        srcs = {v["source"] for v in chargeloc.locations_for(
            self.conn, VID, [f"2026-10-{10 + i:02d}T11:00:00Z" for i in range(4)]).values()}
        self.assertEqual(srcs, {"estimated"})
        self.assertIsNotNone(chargeloc.nearest_station(st, 35.1113, 129.2012))
        self.assertIsNone(chargeloc.nearest_station(st, 35.20, 129.30))
        self.assertEqual(chargeloc.representative_point([]), None)


class RoadUsageTests(_Tmp):
    def _match(self, start: str, segs, sig: str = "a") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO route_match (vehicle_id, trip_start_ts, provider, points_sig, status, segments, fetched_at) "
            "VALUES (?, ?, 'osrm-local', ?, 'matched', ?, ?)", (VID, start, sig, json.dumps(segs), "2026-10-31T00:00:0" + sig[-1:] + "Z"))
        self.conn.commit()

    def test_incremental_counts_and_levels(self) -> None:
        line = [[37.0, 127.0], [37.001, 127.0], [37.002, 127.0]]
        for i, start in enumerate(("2026-10-30T01:00:00Z", "2026-10-30T02:00:00Z")):
            self.trip(start, start.replace(":00:00Z", ":20:00Z"), 37.0, 127.0)
            self._match(start, [line] if i == 0 else [line[:2]], sig=str(i))
        self.assertEqual(roads.update_road_usage(self.conn, VID)["added"], 2)
        self.assertEqual(roads.update_road_usage(self.conn, VID)["added"], 0)   # no change -> skip
        m = roads.road_map(self.conn, VID, "2026")
        self.assertEqual(m["trips"], 2)
        self.assertEqual(m["max_n"], 2)
        self.assertEqual([lv["edges"] for lv in m["levels"]], [1, 1, 0, 0])
        # a re-matched trip rebuilds its year
        self._match("2026-10-30T02:00:00Z", [line], sig="9")
        out = roads.update_road_usage(self.conn, VID)
        self.assertEqual(out["rebuilt_years"], 1)
        self.assertEqual([lv["edges"] for lv in roads.road_map(self.conn, VID, "2026")["levels"]], [0, 2, 0, 0])
        self.assertEqual(roads.road_map(self.conn, VID, "2025")["trips"], 0)

    def test_each_edge_once_per_trip(self) -> None:
        edges = roads.trip_edges([[[37.0, 127.0], [37.001, 127.0], [37.0, 127.0]]])
        self.assertEqual(len(edges), 1)


class LedgerRangeTests(_Tmp):
    def test_parking_toll_moves_to_other(self) -> None:
        self.conn.execute("INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, memo, source) "
                          "VALUES (?, 'p1', '2026-10-01', 'parking_toll', 3000, '', 'manual'), "
                          "(?, 'p2', '2026-10-02', 'parking_toll', 1500, '하이패스', 'manual')", (VID, VID))
        self.conn.commit()
        self.assertEqual(migrate_parking_toll(self.conn), 2)
        rows = {r["id"]: r for r in list_ledger(self.conn, VID)}
        self.assertEqual((rows["p1"]["category"], rows["p1"]["memo"]), ("other", "주차·통행"))
        self.assertEqual(rows["p2"]["memo"], "주차·통행 · 하이패스")
        self.assertEqual(migrate_parking_toll(self.conn), 0)

    def test_summary_list_and_csv_by_range(self) -> None:
        self.conn.execute("INSERT INTO ledger (vehicle_id, id, date, category, amount_krw, memo, source) VALUES "
                          "(?, 'a', '2026-09-30', 'other', 1000, '', 'manual'), (?, 'b', '2026-10-01', 'other', 2000, '', 'manual'), "
                          "(?, 'c', '2026-10-31', 'insurance', 4000, '', 'manual')", (VID, VID, VID))
        self.conn.commit()
        s = ledger_summary(self.conn, VID, "2026-10-01", "2026-10-31")
        self.assertEqual((s["count"], s["total_krw"], s["first_day"]), (2, 6000.0, "2026-09-30"))
        self.assertEqual({r["id"] for r in list_ledger(self.conn, VID, date_from="2026-10-01", date_to="2026-10-31")}, {"b", "c"})
        csv = csv_export(self.conn, VID, "2026-09-01", "2026-09-30").decode("utf-8-sig")
        self.assertEqual(len(csv.strip().splitlines()), 2)


class FastChargerTests(unittest.TestCase):
    class Ev:
        def __init__(self, vals):
            self.vals = vals

        def series(self, _name, _a, _b):
            return [(None, v) for v in self.vals]

        def at(self, _name, _t):
            return None

    def test_prefix_stripped_and_unknown_ignored(self) -> None:
        self.assertEqual(_fast_charger(self.Ev(["FastChargerSupercharger", "Supercharger", "Unknown"]), None, None), "Supercharger")
        self.assertIsNone(_fast_charger(self.Ev(["Unknown", "SNA"]), None, None))
        self.assertEqual(menu.SUPERCHARGER, "Supercharger")

    def test_charge_places_group_nearby(self) -> None:
        items = [{"charge_id": "a", "lat": 37.0, "lon": 127.0, "kind_label": "완속", "kind": "AC", "loc_source": "car"},
                 {"charge_id": "b", "lat": 37.0003, "lon": 127.0, "kind_label": "완속", "kind": "AC", "loc_source": "estimated"},
                 {"charge_id": "c", "lat": 37.1, "lon": 127.0, "kind_label": "슈퍼차저", "kind": "DC", "loc_source": "manual"}]
        out = menu.charge_places(items)
        self.assertEqual(sorted(p["count"] for p in out), [1, 2])


class MenuApiTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "signals.db"
        conn = connect(self.db)
        try:
            store_signals(conn, generate("day"))
        finally:
            conn.close()
        process_db(self.db)
        self.server = make_server(self.db, host="127.0.0.1", port=0, tire_alert_bar=2.6, process_interval=0)
        serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw)

    def test_period_endpoints(self) -> None:
        _, p = self._get("/api/periods")
        self.assertIn("trips", p["first"])
        for key in ("trips", "charges", "ledger"):              # : months with records
            self.assertIsInstance(p["months"][key], list)
        self.assertTrue(p["months"]["trips"])
        self.assertEqual(p["months"]["trips"], sorted(p["months"]["trips"], reverse=True))
        _, s = self._get("/api/trips/summary?recent=10")
        self.assertEqual(s["trip_count"], 2)
        _, s = self._get("/api/trips/summary?from=2000-01-01&to=2000-01-02")
        self.assertEqual(s["trip_count"], 0)
        _, m = self._get("/api/trips/monthly")
        self.assertEqual(sum(r["trip_count"] for r in m["months"]), 2)
        _, c = self._get("/api/charges/summary?recent=10")
        self.assertEqual(c["charge_count"], 1)
        _, pl = self._get("/api/charges/places")
        self.assertIn("places", pl)
        _, r = self._get("/api/map/roads?year=2026")
        self.assertIn("levels", r)
        _, l = self._get("/api/ledger/summary?from=2000-01-01&to=2000-12-31")
        self.assertEqual(l["count"], 0)
        status, csv = self._get("/api/ledger.csv?from=2000-01-01&to=2000-12-31")
        self.assertEqual(status, 200)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self._get("/api/geocode/search?q=a")
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self._get("/api/trips/summary?from=2026-13-01")

    def test_put_charge_location(self) -> None:
        _, ch = self._get("/api/charges")
        cid = ch["charges"][0]["charge_id"]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/charges/location", method="PUT",
                                     data=json.dumps({"charge_id": cid, "lat": 37.5, "lon": 127.0}).encode(),
                                     headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["location"]["source"], "manual")
        _, ch = self._get("/api/charges")
        self.assertEqual(ch["charges"][0]["loc_source"], "manual")


class LayoutOrderTests(unittest.TestCase):
    def test_list_above_map(self) -> None:
        """: period, summary, months table / list, map, trend."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for menu, tail in (("trips", ('id="trips-eff"',)), ("charges", ('id="charges-mix-card"',))):
            i = html.index(f'id="tab-{menu}"')
            order = [html.index(k, i) for k in (f'id="{menu}-period-btn"', f'id="{menu}-summary"', f'id="{menu}-months-card"',
                                                f'id="{menu}-list-card"', f'id="{menu}-map"', *tail)]
            self.assertEqual(order, sorted(order), menu)

    def test_tab_icons(self) -> None:
        """: 주행 = steering wheel, 정비 = wrench (inline SVG, same stroke style)."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        trips = html[html.index('id="tab-btn-trips"'):html.index("</button>", html.index('id="tab-btn-trips"'))]
        self.assertIn('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>', trips)
        cons = html[html.index('id="tab-btn-consumables"'):html.index("</button>", html.index('id="tab-btn-consumables"'))]
        self.assertIn('<path d="M14.7 6.3', cons)


class RowAlignTests(unittest.TestCase):
    def test_rows_keep_columns_without_place(self) -> None:
        """: rows without a route/place keep the swatch, place line and distance column."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        trips = html[html.index("function renderTripRows("):html.index("function openTripSheet(")]
        self.assertIn('"<span class=\'trip-route-label\'>" + escHtml(tripLabel(t) || "") + "</span></div>"', trips)
        self.assertNotIn("tripRoutes.length > 8", trips)
        self.assertIn("tripRoutes.findIndex(r => r.start_ts === t.start_ts)", trips)
        self.assertIn("pal[ri % pal.length]", trips)
        self.assertIn('"<i class=\'sw\'"', trips)
        self.assertIn('tripRoutes.length > 8 ? " · 누르면 강조"', html)
        charges = html[html.index("function renderChargeRows("):html.index("function openChargeDetail(")]
        self.assertIn('"<span class=\'trip-place\'><span class=\'trip-route-label\'>" + escHtml(place || "") + "</span>"', charges)
        self.assertIn("trip-place", charges)
        self.assertIn('.trip-route-label:empty::before { content: "\\00a0"; }', html)
        self.assertIn(".trip-eff { flex: 0 0 auto; min-width: 4.6em; text-align: right;", html)
        self.assertNotIn('|| "—"', trips + charges)


class ChargePlaceColorTests(unittest.TestCase):
    def test_place_colour_bar_and_dots(self) -> None:
        """: one fixed colour per place on the row bar and the dot; kind on the dot's border."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function placeColor(name)", html)
        self.assertIn('const NO_PLACE_COLOR = "#6b7785";', html)
        self.assertIn("return ROUTE_COLORS[h % ROUTE_COLORS.length];", html)
        rows = html[html.index("function renderChargeRows("):html.index("function openChargeDetail(")]
        self.assertIn("\"<i class='sw' style='background:\" + placeColor(place) + \"'></i>\"", rows)
        dot = html[html.index("function dotStyle("):html.index("function highlightCharge(")]
        self.assertIn("const fill = placeColor(d.place);", dot)
        self.assertIn("fillColor: fill", dot)
        self.assertNotIn("KIND_RING", html)            # : no kind borders
        self.assertIn('const KIND_BADGE = { DC: { cls: "dc", text: "⚡" }, SC: { cls: "sc", text: "T" } };', html)
        self.assertNotIn("흰 테두리 = 직접 지정", html)
        # same hash in JS and here: a name always maps to the same palette slot
        def py_hash(s):
            h = 0
            for ch in s:
                h = (h * 31 + ord(ch)) & 0xFFFFFFFF
            return h % 8
        self.assertEqual(py_hash("한빛센터"), py_hash("한빛센터"))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_badges_and_tip_per_kind(self) -> None:
        """: 완속 only = no badge; 급속 bolt, 슈퍼차저 T, side by side; tip counts each kind."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

        def fn(name):
            i = html.index("function " + name + "(")
            return html[i:html.index("\n    }\n", i) + 6]
        k = html.index("    const CHARGE_KIND = {")
        consts = html[k:html.index("    function placeColor(", k)]
        kb = html.index("    const KIND_BADGE")
        src = (consts + html[kb:html.index("\n", kb)] + "\nconst escHtml = s => s;\n" + fn("dotKinds") + fn("dotBadges") + fn("dotTip") +
               "const ds=[{place:'집',kinds:{AC:3},count:3},{place:'휴게소',kinds:{AC:2,DC:1},count:3},"
               "{place:'',kinds:{DC:1,SC:2},count:3,estimated:true},{place:'x',kinds:{AC:1,DC:1,SC:1},count:3}];"
               "process.stdout.write(JSON.stringify(ds.map(d=>[dotBadges(d).map(b=>b.text),dotTip(d)])));")
        out = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out[0], [[], "집 · 완속 3회"])
        self.assertEqual(out[1], [["⚡"], "휴게소 · 완속 2회 · 급속 1회"])
        self.assertEqual(out[2], [["⚡", "T"], "급속 1회 · 슈퍼차저 2회 · 위치 약"])
        self.assertEqual(out[3], [["⚡", "T"], "x · 완속 1회 · 급속 1회 · 슈퍼차저 1회"])

    def test_mixed_kinds_one_dot(self) -> None:
        """: 완속 and 급속 at the same spot stay one dot with counts per kind."""
        items = [
            {"charge_id": "a", "lat": 37.5, "lon": 127.0, "kind": "AC", "place": "휴게소", "loc_source": "station"},
            {"charge_id": "b", "lat": 37.5002, "lon": 127.0, "kind": "DC", "place": "휴게소", "loc_source": "station"},
            {"charge_id": "c", "lat": 37.5001, "lon": 127.0, "kind": "DC", "kind_label": "슈퍼차저", "place": "휴게소", "loc_source": "car"},
            {"charge_id": "d", "lat": 37.5, "lon": 127.0001, "kind": "AC", "place": "휴게소", "loc_source": "car"},
        ]
        dots = menu.charge_places(items)
        self.assertEqual(len(dots), 1)
        self.assertEqual(dots[0]["kinds"], {"AC": 2, "DC": 1, "SC": 1})
        self.assertEqual(dots[0]["count"], 4)


class NoUnitTrendTests(unittest.TestCase):
    def test_unit_trend_section_gone(self) -> None:
        """: no 단가 추이 chart on the charges menu; mix and battery cards stay; the API stays."""
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for gone in ('id="unit-trend"', 'id="unit-trend-title"', "drawUnitTrend", "/api/charges/trend", "단가 추이"):
            self.assertNotIn(gone, html)
        self.assertIn('id="charges-mix-card"', html)
        self.assertIn('id="battery-card"', html)
        self.assertIn("/api/charges/trend", (ROOT / "web" / "__init__.py").read_text(encoding="utf-8"))


class SelectionUiTests(unittest.TestCase):
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def _fn(self, name: str) -> str:
        i = self.html.index("function " + name + "(")
        return self.html[i:self.html.index("\n    }\n", i) + 6]

    def test_row_tap_selects_detail_button_opens(self) -> None:
        """: tapping a row toggles one selection; only the detail icon opens the sheet."""
        for rows, toggle, detail, opener in (("renderTripRows", "toggleTrip(t.start_ts, true)", "data-trip-detail", "openTripSheet("),
                                             ("renderChargeRows", "toggleCharge(c.charge_id, true)", "data-charge-detail", "openChargeDetail(")):
            body = self._fn(rows)
            self.assertIn(toggle, body)
            self.assertIn(f"class='detail-btn' {detail}=", body)
            self.assertIn("aria-label='상세'", body)
            self.assertIn("ev.stopPropagation();", body)
            go = body[body.index("const go = "):body.index("el.addEventListener(\"click\", go)")]
            self.assertNotIn(opener, go)
        self.assertIn("highlightTrip(pickedTrip === startTs ? null : startTs, fit)", self._fn("toggleTrip"))
        self.assertNotIn("highlightTrip", self._fn("openTripSheet"))
        self.assertNotIn("highlightCharge", self._fn("openChargeDetail"))
        self.assertIn("toggleTrip(hits[0].start_ts, false)", self._fn("pickRouteAt"))
        self.assertIn("highlightCharge(null, false)", self._fn("drawChargeDots"))
        self.assertIn(".detail-btn { flex: none; width: 44px; height: 44px; min-height: 44px; min-width: 44px; margin: -8px", self.html)

    def test_no_location_tag_in_charge_rows(self) -> None:
        """: estimated / manual location only in the detail sheet."""
        body = self._fn("renderChargeRows")
        for gone in ("locTag", "위치 약", "직접 지정", "loc_source"):
            self.assertNotIn(gone, body)
        self.assertIn('c.loc_source === "station" ? "저장한 장소"', self._fn("openChargeDetail"))


class TrendSectionsTests(unittest.TestCase):
    def test_eff_card_stays_open_and_pattern_is_gone(self) -> None:
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        eff = html[html.index('id="trips-eff"'):html.index("</section>", html.index('id="trips-eff"'))]
        self.assertIn("<h2>전비</h2>", eff)
        self.assertIn('id="eff-num"', eff)
        self.assertIn('id="drv-trend"', eff)
        self.assertNotIn('id="trips-trend"', html)
        self.assertNotIn("<h2>운전 패턴</h2>", html)
        self.assertIn('<section class="card" id="trips-eff">', html)


class PeriodSheetTests(unittest.TestCase):
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_one_apply_button_and_x(self) -> None:
        """: no per-row 보기 buttons, no '직접 선택' text, X closes, 보기 applies."""
        i = self.html.index('<div id="period-overlay"')
        sheet = self.html[i:self.html.index("</div>\n  </div>", i)]
        for gone in ("period-month-go", "period-year-go", "period-range-go", "period-cancel", "직접 선택", 'type="month"'):
            self.assertNotIn(gone, sheet)
        self.assertIn('id="period-x" aria-label="닫기">×</button>', sheet)
        self.assertIn('id="period-apply">보기</button>', sheet)
        self.assertIn('<select id="period-month"', sheet)
        self.assertIn('<select id="period-year"', sheet)
        self.assertIn('"period-x").addEventListener("click", closePeriodSheet)', self.html)
        self.assertIn('"period-apply").addEventListener("click", applyPeriodSheet)', self.html)
        self.assertIn('day: "오늘"', self.html)
        self.assertNotIn('"하루"', self.html)

    def test_draft_fills_dates_and_record_months_only(self) -> None:
        j = self.html.index("function paintPeriodSheet(")
        paint = self.html[j:self.html.index("\n    }\n", j)]
        self.assertIn("f.disabled = t.disabled = off;", paint)
        o = self.html.index("function openPeriodSheet(")
        open_ = self.html[o:self.html.index("\n    }\n", o)]
        self.assertIn("recordMonths[menu]", open_)
        self.assertIn("new Set(months.map(m => m.slice(0, 4)))", open_)
        self.assertIn('periodDraft = { k: "range", from: f, to: t };', self.html)
        self.assertIn('ledger: { quick: ["month", "year", "all"]', self.html)

    def test_selects_match_and_centre(self) -> None:
        self.assertIn("text-align: center; text-align-last: center;", self.html)
        self.assertIn(".range-inputs { display: grid; grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1fr);", self.html)


class EfficiencyGapTests(_Tmp):
    """A trip with km 0 and a little kWh stays in the trip list and drops out of 전비."""

    def test_zero_km_trip_does_not_invent_efficiency(self) -> None:
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, kwh) VALUES (?,?,?,?,?)",
            (VID, "2026-11-01T23:41:54Z", "2026-11-01T23:43:08Z", 0.0, 0.03),
        )
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, kwh) VALUES (?,?,?,?,?)",
            (VID, "2026-11-03T01:00:00Z", "2026-11-03T02:00:00Z", 10.0, 2.0),
        )
        self.conn.commit()
        month = menu.trips_monthly(self.conn, VID, "2026-11-01", "2026-11-30")["months"][0]
        self.assertEqual(month["trip_count"], 2)
        self.assertEqual(month["km"], 10.0)
        self.assertEqual(month["km_per_kwh"], 5.0)
        trend = menu.trips_trend(self.conn, VID, "2026-11-01", "2026-11-07")
        self.assertEqual([t["from"] for t in trend["trend"]], ["2026-11-03"])
        self.assertIsNone(menu.trips_summary(self.conn, VID, "2026-11-02", "2026-11-02")["km_per_kwh"])
        self.assertEqual(menu.trips_summary(self.conn, VID, "2026-11-02", "2026-11-02")["trip_count"], 1)


class FeedbackHtmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_privacy_and_line_art_gone_and_maintenance_labels(self) -> None:
        # The old privacy sheet is gone. v1.7 settings is language, alerts, and payment.
        for gone in ("privacyOn", "가리기", "예시 지도", "car-stage", "등록일 기준"):
            self.assertNotIn(gone, self.html, gone)
        self.assertNotIn('id="v-speed"', self.html)
        self.assertNotIn('id="v-gear"', self.html)
        self.assertIn("앞 좌측", self.html)
        self.assertIn('id="m-sentry"', self.html)
        self.assertNotIn("캠핑(추정)", self.html)
        self.assertNotIn('id="m-camp"', self.html)
        self.assertNotIn('id="status-icons"', self.html)
        self.assertIn('id="m-climate"', self.html)
        self.assertIn(">공조 모드<", self.html)
        self.assertIn('if (status === "overdue") return "점검 필요"', self.html)
        self.assertIn("다음 점검", self.html)
        self.assertNotIn("알림 꺼짐", self.html)
        self.assertNotIn("항목 비활성화", self.html)
        self.assertNotIn("기존 기록은 남아요", self.html)
        self.assertIn('id="charges-summary"', self.html)
        i = self.html.index('getElementById("charges-summary").innerHTML')
        block = self.html[i:i + 700]
        self.assertIn('["횟수"', block)
        self.assertIn('["비용"', block)
        self.assertIn('["평균 단가"', block)
        self.assertIn('["충전효율"', block)
        self.assertNotIn("건 기준", self.html)
        self.assertIn('["충전효율", eff]', block)
        self.assertNotIn('["손실률"', block)
        self.assertNotIn("주차 중 소모", block)
        self.assertNotIn('["AC"', block)


class ChargeSummaryRateTests(_Tmp):
    def test_unit_price_and_loss_use_complete_rows_only(self) -> None:
        rows = [
            ("2026-10-30T11:54:00Z", "2026-10-30T13:00:00Z", 26.73, 26.73, 24.94, 1.79, 6.7, 6000, 26.73, 500),
            ("2026-11-01T12:38:00Z", "2026-11-01T13:00:00Z", 7.98, 7.98, 7.50, 0.48, 6.0, 0, 7.98, None),
            ("2026-11-02T01:00:00Z", "2026-11-02T02:00:00Z", 20.0, None, None, None, None, None, None, None),
        ]
        for start, end, kwh, charger, battery, loss, pct, paid, billed, discount in rows:
            self.conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, charger_kwh, "
                "battery_kwh, loss_kwh, loss_pct) VALUES (?,?,?,?,?,?,?,?,?)",
                (VID, start, end, "AC", kwh, charger, battery, loss, pct),
            )
            if paid is not None or billed is not None:
                self.conn.execute(
                    "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw, kwh_billed, discount_krw) "
                    "VALUES (?,?,?,?,?)",
                    (VID, start, paid, billed, discount),
                )
        self.conn.commit()
        got = menu.charges_summary(self.conn, VID, "2026-10-01", "2026-11-30")
        self.assertEqual(got["charge_count"], 3)
        self.assertEqual(got["loss_n"], 2)
        self.assertAlmostEqual(got["loss_pct"], 6.5, places=1)
        self.assertAlmostEqual(got["efficiency_pct"], 93.5, places=1)
        self.assertEqual(got["unit_n"], 2)
        self.assertAlmostEqual(got["unit_krw_per_kwh"], round((6000 + 500) / (26.73 + 7.98), 1), places=1)
        empty = menu.charges_summary(self.conn, VID, "2026-11-02", "2026-11-02")
        self.assertEqual(empty["loss_n"], 0)
        self.assertIsNone(empty["loss_pct"])
        self.assertIsNone(empty["efficiency_pct"])
        self.assertIsNone(empty["unit_krw_per_kwh"])

    def test_recent_without_discount_stays_while_the_year_includes_it(self) -> None:
        for i in range(5):
            start = f"2026-11-{10 + i:02d}T01:00:00Z"
            self.conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?,?,?,?,?)",
                (VID, start, start, "AC", 10),
            )
            self.conn.execute(
                "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw, kwh_billed, discount_krw) VALUES (?,?,?,?,?)",
                (VID, start, 2920, 10, 0),
            )
        old = "2026-01-15T01:00:00Z"
        self.conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?,?,?,?,?)",
            (VID, old, old, "AC", 10),
        )
        self.conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, total_krw, kwh_billed, discount_krw) VALUES (?,?,?,?,?)",
            (VID, old, 2500, 10, 500),
        )
        self.conn.commit()
        recent = menu.charges_summary(self.conn, VID, recent=5)
        year = menu.charges_summary(self.conn, VID, "2026-01-01", "2026-12-31")
        self.assertEqual(recent["unit_krw_per_kwh"], 292.0)
        self.assertEqual(year["unit_krw_per_kwh"], round((2920 * 5 + 2500 + 500) / 60, 1))
        self.assertGreater(year["unit_krw_per_kwh"], round((2920 * 5 + 2500) / 60, 1))


if __name__ == "__main__":
    unittest.main()
