""": own osrm-routed matcher: jump filter, overlapping chunks, leg feasibility, retry when down."""
import json
import sqlite3
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from process import ensure_process_schema
from process.geo import distance_m
from process import match as pm

T0 = datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)
VID = "v"


def line(n, step_deg=0.00018, every_s=3, lat0=37.5, lon0=127.0):
    """n points going north ~20 m apart every 3 s (~24 km/h)."""
    return [(lat0 + i * step_deg, lon0, T0 + timedelta(seconds=every_s * i)) for i in range(n)]


class FakeOsrm:
    """Answers /match with one matching; each leg's road path is the straight line between
    its points, unless `detour` maps a (lat, lon) leg start to an extra road distance."""

    def __init__(self, detour=None, down=False, snap=None):
        self.calls = []
        self.detour = detour or {}
        self.down = down
        self.snap = snap or {}  # (lat, lon) of a point -> lat offset where OSRM puts it on the road

    def __call__(self, req, timeout=None):
        if self.down:
            raise urllib.error.URLError("refused")
        u = urllib.parse.urlsplit(req.full_url)
        coords = [tuple(map(float, c.split(","))) for c in u.path.rsplit("/", 1)[1].split(";")]
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((len(coords), q))
        legs = []
        for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
            d = distance_m((la1, lo1), (la2, lo2)) + self.detour.get((round(la1, 6), round(lo1, 6)), 0.0)
            s1 = la1 + self.snap.get((round(la1, 6), round(lo1, 6)), 0.0)
            s2 = la2 + self.snap.get((round(la2, 6), round(lo2, 6)), 0.0)
            legs.append({"distance": d, "steps": [{"geometry": {"coordinates": [[lo1, s1], [lo2, s2]]}}]})
        body = {"code": "Ok", "matchings": [{"confidence": 0.9, "legs": legs}],
                "tracepoints": [{"matchings_index": 0, "waypoint_index": i} for i in range(len(coords))]}
        return _Resp(json.dumps(body).encode())


class _Resp:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class JumpTests(unittest.TestCase):
    def test_single_spike_dropped(self):
        pts = line(10)
        lat, lon, ts = pts[5]
        pts[5] = (lat + 0.01, lon, ts)  # ~1.1 km off for 3 s
        kept, dropped = pm.drop_jumps(pts)
        self.assertEqual(dropped, [5])
        self.assertEqual(len(kept), 9)

    def test_reported_speed_tightens_limit(self):
        pts = line(10, every_s=10)
        lat, lon, ts = pts[4]
        pts[4] = (lat + 0.0036, lon, ts)  # ~400 m sideways jump in 10 s
        self.assertEqual(pm.drop_jumps(pts)[1], [])                     # 180 km/h cap allows it
        self.assertEqual(pm.drop_jumps(pts, lambda a, b: 30.0)[1], [4])  # car said 30 km/h

    def test_real_move_after_gap_is_kept(self):
        pts = line(5) + [(37.52, 127.0, T0 + timedelta(minutes=5))]  # 2 km in 5 min: possible
        self.assertEqual(pm.drop_jumps(pts)[1], [])


class ChunkTests(unittest.TestCase):
    def test_overlap_and_ownership(self):
        n = 650
        ch = pm.local_chunks(n)
        self.assertTrue(all(b - a <= pm.CHUNK for a, b, _, _ in ch))
        for (a1, b1, _, _), (a2, b2, _, _) in zip(ch, ch[1:]):
            self.assertEqual(b1 - a2, pm.OVERLAP)
        owned = [i for _, _, f, t in ch for i in range(f, t)]
        self.assertEqual(owned, list(range(n)))  # every leg start owned exactly once

    def test_long_trip_continuous(self):
        fake = FakeOsrm()
        m = pm.LocalOsrmMatcher("http://127.0.0.1:5000", opener=fake)
        pts = line(650)
        res = m.match(pts)
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(n <= pm.CHUNK for n, _ in fake.calls))
        self.assertEqual(fake.calls[0][1]["gaps"], ["ignore"])
        self.assertAlmostEqual(res["matched_ratio"], 1.0)
        self.assertEqual(len(res["segments"]), 1)
        self.assertEqual(len(res["segments"][0]), 650)  # each point once, seams not doubled

    def test_impossible_gap_leg_stays_straight(self):
        pts = line(20)[:10] + [(p[0], p[1], p[2] + timedelta(seconds=60)) for p in line(20)[10:]]  # 60 s GPS gap
        start = (round(pts[9][0], 6), round(pts[9][1], 6))
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(detour={start: 20000.0}))  # road says 20 km in ~63 s
        res = m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 1)
        self.assertEqual(len(res["segments"]), 3)  # road, straight gap, road
        self.assertAlmostEqual(res["matched_ratio"], 18 / 19)

    def test_possible_gap_leg_follows_road(self):
        # 60 s without GPS (underpass) while the car moved ~600 m; the road there is 300 m longer
        pts = line(20)[:10] + [(p[0] + 0.0054, p[1], p[2] + timedelta(seconds=60)) for p in line(20)[10:]]
        start = (round(pts[9][0], 6), round(pts[9][1], 6))
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(detour={start: 300.0}))
        res = m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 0)
        self.assertEqual(len(res["segments"]), 1)


class EndLegTests(unittest.TestCase):
    """: trip ends are judged strictly and joined to the raw first/last point."""

    def key(self, p):
        return (round(p[0], 6), round(p[1], 6))

    def test_end_snapped_far_stays_raw(self):
        pts = line(10)
        # OSRM puts the last point ~59 m away on a side road (underground garage)
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(snap={self.key(pts[-1]): 0.00053}))
        res = m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 1)
        end = res["segments"][-1][-1]
        self.assertLess(distance_m(tuple(end), pts[-1]), 0.5)
        self.assertEqual(res["segments"][-1][-2:], [[pts[-2][0], pts[-2][1]], [pts[-1][0], pts[-1][1]]])

    def test_start_snapped_far_stays_raw(self):
        pts = line(10)
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(snap={self.key(pts[0]): -0.0004}))
        res = m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 1)
        self.assertEqual(res["segments"][0][0], [pts[0][0], pts[0][1]])

    def test_short_end_leg_detour_rejected(self):
        # a 20 m last leg whose road path is 240 m long (10/31: wandered 120 m away and back)
        pts = line(10)
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(detour={self.key(pts[-2]): 220.0}))
        m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 1)
        # the same detour in the middle passes the ordinary (looser) check
        m2 = pm.LocalOsrmMatcher(opener=FakeOsrm(detour={self.key(pts[4]): 220.0}))
        m2.match(pts)
        self.assertEqual(m2.last_info["straight_legs"], 0)

    def test_small_snap_kept_and_joined(self):
        pts = line(10)
        # snapped 10 m off at both ends: the ends follow the road and get a short joint to the raw points
        m = pm.LocalOsrmMatcher(opener=FakeOsrm(snap={self.key(pts[0]): 0.00009, self.key(pts[-1]): 0.00009}))
        res = m.match(pts)
        self.assertEqual(m.last_info["straight_legs"], 0)
        self.assertEqual(len(res["segments"]), 1)
        seg = res["segments"][0]
        self.assertEqual(seg[0], [pts[0][0], pts[0][1]])
        self.assertEqual(seg[-1], [pts[-1][0], pts[-1][1]])
        self.assertAlmostEqual(distance_m(tuple(seg[-2]), pts[-1]), 10.0, delta=0.5)


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        ensure_process_schema(self.c)
        for i, (lat, lon, ts) in enumerate(line(30)):
            self.c.execute("INSERT INTO trip_points (vehicle_id, trip_start_ts, seq, ts, lat, lon) VALUES (?,?,?,?,?,?)",
                           (VID, "2026-10-31T01:00:00Z", i, ts.strftime("%Y-%m-%dT%H:%M:%SZ"), lat, lon))
        # an old demo-server answer for the same points: the local matcher must redo it
        sig = pm.points_sig([(p[0], p[1]) for p in line(30)])
        self.c.execute("INSERT INTO route_match (vehicle_id, trip_start_ts, provider, points_sig, status, attempts) "
                       "VALUES (?, '2026-10-31T01:00:00Z', 'osrm', ?, 'raw', 0)", (VID, sig))
        self.c.commit()

    def row(self):
        return self.c.execute("SELECT provider, status, attempts, matched_ratio FROM route_match").fetchone()

    def test_down_then_retry(self):
        down = pm.LocalOsrmMatcher(opener=FakeOsrm(down=True))
        self.assertEqual(pm.match_pending(self.c, VID, down, now=T0 + timedelta(hours=1)), 1)
        self.assertEqual(self.row()[:3], ("osrm-local", "error", 1))
        self.assertIsNone(pm.cached_lines(self.c, VID, "2026-10-31T01:00:00Z", [(p[0], p[1]) for p in line(30)]))
        # too soon: no retry
        self.assertEqual(pm.match_pending(self.c, VID, down, now=T0 + timedelta(hours=1, minutes=5)), 0)
        up = pm.LocalOsrmMatcher(opener=FakeOsrm())
        self.assertEqual(pm.match_pending(self.c, VID, up, now=T0 + timedelta(hours=2)), 1)
        self.assertEqual(self.row()[:2], ("osrm-local", "matched"))
        self.assertIsNotNone(pm.cached_lines(self.c, VID, "2026-10-31T01:00:00Z", [(p[0], p[1]) for p in line(30)]))

    def test_many_attempts_for_local(self):
        self.assertGreater(pm.LocalOsrmMatcher.max_attempts, pm.MAX_ATTEMPTS)

    def test_from_env(self):
        import os
        old = dict(os.environ)
        try:
            os.environ["TESLIFE_ROUTE_MATCH"] = "osrm-local"
            os.environ.pop("TESLIFE_OSRM_URL", None)
            m = pm.from_env()
            self.assertEqual((m.name, m.base_url), ("osrm-local", "http://127.0.0.1:5000"))
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()


class RematchTests(unittest.TestCase):
    def test_rematch_replaces_cached_lines_only(self):
        conn = sqlite3.connect(":memory:")
        ensure_process_schema(conn)
        pts = line(10)
        conn.executemany("INSERT INTO trip_points (vehicle_id, trip_start_ts, seq, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
                         [(VID, "s1", i, pm._fmt(p[2]), p[0], p[1]) for i, p in enumerate(pts)])
        first = FakeOsrm(snap={(round(pts[-1][0], 6), round(pts[-1][1], 6)): 0.00053})
        old = pm.LocalOsrmMatcher(opener=first)
        old_segs = json.dumps([[[p[0], p[1]] for p in pts[:-1]] + [[pts[-1][0] + 0.00053, pts[-1][1]]]])
        conn.execute("INSERT INTO route_match (vehicle_id, trip_start_ts, provider, points_sig, status, segments, matched_ratio, attempts, fetched_at) "
                     "VALUES (?, 's1', 'osrm-local', ?, 'matched', ?, 1.0, 0, '2026-10-31T00:00:00Z')",
                     (VID, pm.points_sig([(p[0], p[1]) for p in pts]), old_segs))
        before = conn.execute("SELECT * FROM trip_points ORDER BY seq").fetchall()
        self.assertEqual(pm.match_pending(conn, VID, old), 0)  # cache is current: nothing to do
        self.assertEqual(pm.rematch(conn, VID, old), ["s1"])
        segs = json.loads(conn.execute("SELECT segments FROM route_match").fetchone()[0])
        self.assertLess(distance_m(tuple(segs[-1][-1]), pts[-1]), 0.5)
        self.assertEqual(conn.execute("SELECT * FROM trip_points ORDER BY seq").fetchall(), before)
        self.assertNotEqual(conn.execute("SELECT fetched_at FROM route_match").fetchone()[0], "2026-10-31T00:00:00Z")

