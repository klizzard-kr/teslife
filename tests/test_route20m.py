""": route points are thinned at 20 m (the car sends Location every 3 s at >= 20 m)."""
import unittest
from datetime import datetime, timedelta, timezone

import process
from process import Events, ROUTE_MIN_M, route_points
from process.geo import distance_m
from process.match import MAX_COORDS, _chunks


def _loc(lat, lon):
    return {"latitude": lat, "longitude": lon}


class Route20mTests(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)

    def _points(self, step_deg, n, every_s=3):
        rows = [(self.t0 + timedelta(seconds=i * every_s), "Location", _loc(37.5 + i * step_deg, 127.0)) for i in range(n)]
        return Events(rows), self.t0, self.t0 + timedelta(seconds=n * every_s)

    def test_threshold_is_20m(self):
        self.assertEqual(ROUTE_MIN_M, 20.0)

    def test_keeps_points_25m_apart(self):
        ev, s, e = self._points(0.000225, 20)  # ~25 m apart
        pts = route_points(ev, s, e)
        self.assertEqual(len(pts), 20)

    def test_drops_points_under_20m(self):
        ev, s, e = self._points(0.0001, 21)  # ~11 m apart -> every 2nd point (~22 m) survives
        pts = route_points(ev, s, e)
        self.assertLess(len(pts), 21)
        self.assertGreaterEqual(len(pts), 10)
        for a, b in zip(pts, pts[1:-1]):
            self.assertGreaterEqual(distance_m(a, b), 20.0)
        self.assertEqual(pts[-1][2], s + timedelta(seconds=60))  # last point always kept

    def test_old_50m_routes_unchanged(self):
        ev, s, e = self._points(0.00045, 10, every_s=10)  # ~50 m apart, 10 s (old config)
        self.assertEqual(len(route_points(ev, s, e)), 10)

    def test_long_trip_chunks_overlap(self):
        # 20 km at 20 m -> ~1000 points -> 11 requests of <= 100 points, overlapping by one
        pts = [(37.5 + i * 0.00018, 127.0, self.t0 + timedelta(seconds=3 * i)) for i in range(1000)]
        ch = _chunks(pts)
        self.assertEqual(len(ch), 11)
        self.assertTrue(all(len(c) <= MAX_COORDS for c in ch))
        self.assertEqual(ch[0][-1], ch[1][0])


if __name__ == "__main__":
    unittest.main()
