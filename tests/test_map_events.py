"""Stage 9: harsh events placed on the route, sentry location, and road matching (OSRM)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
from process import connect as process_connect, process_db
from process.geo import locate
from tests.test_map import HOME, T0, VID, loc, scenario, sig

TRIP1 = T0 + timedelta(minutes=5)


def harsh_rows() -> list[dict]:
    """A 16 km/h rise in 1 s at 65 s into the first trip. Exactly 15 is not an event."""
    t = TRIP1 + timedelta(seconds=65)
    return [sig(t, "VehicleSpeed", 20.0), sig(t + timedelta(seconds=1), "VehicleSpeed", 36.0)]


class Base(unittest.TestCase):
    extra: list[dict] = []

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "e.db")
        conn = ingest.connect(self.db)
        ingest.store_signals(conn, scenario() + harsh_rows())
        conn.close()
        process_db(self.db, seed=False)
        self.trips = [r[0] for r in self.q("SELECT start_ts FROM trips ORDER BY start_ts")]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def q(self, sql: str, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class EventLocationTests(Base):
    def test_locate_interpolates(self) -> None:
        pts = [(37.0, 127.0, T0), (37.001, 127.0, T0 + timedelta(seconds=10))]
        self.assertAlmostEqual(locate(pts, T0 + timedelta(seconds=5))[0], 37.0005)
        self.assertEqual(locate(pts, T0 - timedelta(seconds=5)), (37.0, 127.0))
        self.assertEqual(locate(pts, T0 + timedelta(minutes=5)), (37.001, 127.0))
        self.assertIsNone(locate([], T0))

    def test_harsh_event_is_placed_on_route(self) -> None:
        rows = self.q("SELECT trip_start_ts, ts, kind, g, lat, lon FROM trip_events")
        self.assertEqual(len(rows), 1)
        start, ts, kind, g, lat, lon = rows[0]
        self.assertEqual(start, self.trips[0])
        self.assertEqual(kind, "accel")
        self.assertAlmostEqual(g, 16.0, places=3)
        # 65 s in: halfway between the 6th and 7th point (0.0009 deg apart)
        self.assertAlmostEqual(lat, HOME[0] + 0.0009 * 6.5, places=5)
        self.assertAlmostEqual(lon, HOME[1], places=6)
        self.assertEqual(self.q("SELECT hard_accel FROM trips WHERE start_ts = ?", (start,))[0][0], 1)

    def test_events_kept_when_raw_is_gone_and_rerun_stable(self) -> None:
        process_db(self.db, seed=False)
        self.assertEqual(len(self.q("SELECT * FROM trip_events")), 1)
        with sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM signals WHERE name = 'VehicleSpeed'")
            c.execute("DELETE FROM process_meta")
        process_db(self.db, seed=False)
        self.assertEqual(len(self.q("SELECT * FROM trip_events")), 1)

    def test_sentry_alert_has_parked_location(self) -> None:
        from process import _load_events
        from process.alerts import check_sentry

        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES (?, '집', ?, ?, 300)",
                      (VID, HOME[0], HOME[1]))
        last = self.q("SELECT end_lat, end_lon FROM trips ORDER BY start_ts DESC LIMIT 1")[0]
        conn = ingest.connect(self.db)
        t = T0 + timedelta(days=3)
        ingest.store_signals(conn, [sig(t, "SentryMode", "SentryModeStateOff"),
                                    sig(t + timedelta(minutes=1), "SentryMode", "SentryModeStateArmed")])
        conn.close()
        sent: list = []
        conn = process_connect(self.db)
        try:
            ev = _load_events(conn, VID)
            # first observation is silent; a change is sent
            check_sentry(conn, VID, ev, t, lambda p: (True, "ok"))
            from process.alerts import _set_state
            _set_state(conn, VID, "sentry", "off", t)
            check_sentry(conn, VID, ev, t + timedelta(minutes=1), lambda p: (sent.append(p) or True, "http 200"))
        finally:
            conn.close()
        self.assertEqual(len(sent), 1)
        self.assertIn("감시 모드 켜짐", sent[0]["message"])
        self.assertNotIn("(근사)", sent[0]["message"])
        self.assertNotRegex(sent[0]["message"], r"\d+\.\d{3}")
        where = sent[0]["data"]["location"]
        self.assertAlmostEqual(where["lat"], last[0], places=5)
        self.assertTrue(where["approximate"])
        log = self.q("SELECT lat, lon FROM alert_log WHERE kind = 'sentry'")
        self.assertAlmostEqual(log[-1][0], last[0], places=5)


def osrm_reply(points, conf=0.9, unmatched=()):
    """A fake OSRM /match answer: one matching that bends each line 0.00005 deg east."""
    coords = [[p[1] + 0.00005, p[0]] for p in points]
    tps = [None if i in unmatched else {"matchings_index": 0, "waypoint_index": i} for i in range(len(points))]
    return {"code": "Ok", "matchings": [{"confidence": conf, "geometry": {"coordinates": coords}}], "tracepoints": tps}


class FakeOpener:
    def __init__(self, conf=0.9, fail=False):
        self.calls: list = []
        self.conf = conf
        self.fail = fail

    def __call__(self, req, timeout=0):
        self.calls.append((req, timeout))
        if self.fail:
            raise urllib.error.URLError("down")
        path = req.full_url.split("/match/v1/driving/")[1].split("?")[0]
        pts = [(float(c.split(",")[1]), float(c.split(",")[0])) for c in path.split(";")]
        body = json.dumps(osrm_reply(pts, self.conf)).encode()

        class Res:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return body
        return Res()


class MatchTests(Base):
    def matcher(self, opener):
        from process.match import OsrmMatcher
        return OsrmMatcher("http://osrm.local:5000/", opener=opener, sleep=lambda s: None)

    def test_lines_from_osrm_fallbacks(self) -> None:
        from process.match import lines_from_osrm
        pts = [(37.0 + i * 0.001, 127.0, T0 + timedelta(seconds=10 * i)) for i in range(5)]
        segs, n = lines_from_osrm(pts, osrm_reply([(p[0], p[1]) for p in pts]))
        self.assertEqual((len(segs), n), (1, 5))
        self.assertAlmostEqual(segs[0][0][1], 127.00005)
        segs, n = lines_from_osrm(pts, osrm_reply([(p[0], p[1]) for p in pts], conf=0.1))
        self.assertEqual(n, 0)
        self.assertEqual(segs, [[[p[0], p[1]] for p in pts]])  # low confidence: straight lines
        segs, n = lines_from_osrm(pts, {"code": "NoMatch"})
        self.assertEqual((segs, n), ([[[p[0], p[1]] for p in pts]], 0))
        segs, n = lines_from_osrm(pts, osrm_reply([(p[0], p[1]) for p in pts], unmatched=(3, 4)))
        self.assertEqual(n, 3)
        self.assertEqual(segs[-1], [[pts[2][0], pts[2][1]], [pts[3][0], pts[3][1]], [pts[4][0], pts[4][1]]])

    def test_request_shape_and_chunks(self) -> None:
        import process.match as pm
        opener = FakeOpener()
        pts = [(37.0 + i * 0.0005, 127.0, T0 + timedelta(seconds=10 * i)) for i in range(150)]
        res = self.matcher(opener).match(pts)
        self.assertEqual(len(opener.calls), 2)  # 100 + 51 (one shared point)
        req, timeout = opener.calls[0]
        self.assertEqual(timeout, pm.TIMEOUT_S)
        self.assertTrue(req.full_url.startswith("http://osrm.local:5000/match/v1/driving/127.000000,37.000000;"))
        self.assertIn("radiuses=25%3B25", req.full_url)
        self.assertIn("gaps=split", req.full_url)
        self.assertIn("teslife", req.get_header("User-agent"))
        self.assertEqual(res["matched_ratio"], 1.0)
        self.assertEqual(len(res["segments"]), 2)

    def test_match_once_cache_and_route_api(self) -> None:
        from process.match import match_pending
        opener = FakeOpener()
        m = self.matcher(opener)
        conn = process_connect(self.db)
        try:
            n = sum(match_pending(conn, VID, m, now=T0 + timedelta(days=2)) for _ in range(5))
            self.assertEqual(n, 2)  # one trip per call, each once (two drives 3 min apart are one trip)
            self.assertEqual(len(opener.calls), 2)
        finally:
            conn.close()
        self.assertEqual({r[0] for r in self.q("SELECT status FROM route_match")}, {"matched"})
        # points unchanged on rerun: still cached
        process_db(self.db, seed=False, matcher=m, now=T0 + timedelta(days=2))
        self.assertEqual(len(opener.calls), 2)
        from web import trip_route
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            data = trip_route(conn, VID, self.trips[0])
        finally:
            conn.close()
        self.assertTrue(data["matched"])
        self.assertAlmostEqual(data["segments"][0][0][1], HOME[1] + 0.00005, places=6)
        self.assertEqual([e["kind"] for e in data["events"]], ["accel"])
        # trip_points stay the original points
        self.assertEqual(self.q("SELECT lat, lon FROM trip_points WHERE trip_start_ts = ? AND seq = 0",
                                (self.trips[0],))[0], HOME)

    def test_failures_retry_limited(self) -> None:
        import process.match as pm
        opener = FakeOpener(fail=True)
        m = self.matcher(opener)
        conn = process_connect(self.db)
        now = T0 + timedelta(days=2)
        try:
            for i in range(12):
                pm.match_pending(conn, VID, m, now=now + timedelta(hours=i), limit=3)
        finally:
            conn.close()
        rows = self.q("SELECT status, attempts FROM route_match")
        self.assertEqual({r[0] for r in rows}, {"error"})
        self.assertEqual({r[1] for r in rows}, {pm.MAX_ATTEMPTS})
        self.assertEqual(len(opener.calls), 2 * pm.MAX_ATTEMPTS)
        conn = process_connect(self.db)
        try:
            calls = len(opener.calls)
            pm.match_pending(conn, VID, m, now=now + timedelta(seconds=30), limit=3)  # capped: no more requests
            self.assertEqual(len(opener.calls), calls)
        finally:
            conn.close()

    def test_from_env(self) -> None:
        import os
        import process.match as pm
        saved = {k: os.environ.get(k) for k in ("TESLIFE_ROUTE_MATCH", "TESLIFE_OSRM_URL")}
        try:
            os.environ.pop("TESLIFE_ROUTE_MATCH", None)
            self.assertIsNone(pm.from_env())
            os.environ["TESLIFE_ROUTE_MATCH"] = "osrm"
            os.environ.pop("TESLIFE_OSRM_URL", None)
            self.assertEqual(pm.from_env().base_url, pm.DEFAULT_OSRM_URL)
            os.environ["TESLIFE_OSRM_URL"] = "http://127.0.0.1:5000"
            self.assertEqual(pm.from_env().base_url, "http://127.0.0.1:5000")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
