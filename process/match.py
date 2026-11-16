"""Snap finished trips onto roads for drawing (map matching), behind a small provider interface.

Collection stays at 3 s / 20 m (route points thinned to 20 m; 10 s / 50 m before
2026-11-01). Once a trip has ended, the process loop sends its
points to a matcher once and caches the drawn line in `route_match`; `trip_points`
is never changed. Parts that fail to match, or match with low confidence, are drawn
as the original straight lines.

The matcher is chosen with TESLIFE_ROUTE_MATCH and the server with TESLIFE_OSRM_URL:

* "osrm-local" (the default on the server): our own osrm-routed (MLD, car, South
  Korea) on 127.0.0.1. Before matching, isolated jumps are dropped (a point that cannot be
  reached from its neighbours at the speed the car reported). The trip is sent in chunks of
  CHUNK points that overlap by OVERLAP points, with gaps=ignore and steps, so every leg
  between two kept points has its own road path. Each leg is taken from the chunk whose core
  owns it, and a leg is drawn on the road only if the road distance could be driven in the
  time between the two points (GPS gaps, e.g. underpasses, are checked the same way);
  otherwise just that leg stays a straight line. The first and last legs are judged more
  strictly (END_MAX_M, ) and the line is joined to the raw first/last point. If OSRM is down, the trip keeps its raw
  points and is retried later (backoff, many attempts).
* "osrm": the public demo server (router.project-osrm.org). Light use only, and since 2026
  it rejects /match with more than 10 points (TooBig), so it no longer matches our trips.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

DEFAULT_OSRM_URL = "https://router.project-osrm.org"
USER_AGENT = "teslife/9 (personal vehicle log; map matching of own trips)"
MAX_COORDS = 100          # demo server limit per /match request
MIN_INTERVAL_S = 1.1
TIMEOUT_S = 10
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=10)  # times the attempt number
MIN_CONFIDENCE = 0.4
RADIUS_M = 25
TRIPS_PER_CYCLE = 1

LOCAL_OSRM_URL = "http://127.0.0.1:5000"
CHUNK = 300               # points per local /match request (osrm-routed --max-matching-size 500)
OVERLAP = 20              # points shared by neighbouring chunks; each chunk owns the middle of the overlap
LOCAL_RADIUS_M = 20
LOCAL_TIMEOUT_S = 30
LOCAL_MAX_ATTEMPTS = 48
LOCAL_RETRY_MAX = timedelta(hours=2)
LOCAL_TRIPS_PER_CYCLE = 5
V_ABS_MAX = 50.0          # m/s (180 km/h): no leg may need more than this
V_MARGIN = 1.3            # reported speed x this (+ V_SLACK) is the most a leg may need
V_SLACK = 5.0             # m/s
D_SLACK = 150.0           # m, GPS error and turns
DETOUR_MAX = 2.5          # a leg's road path may be at most this x the straight distance (+450 m)
# Trip ends: the first and the last leg start/end where the car parked, often off the road
# network (underground garage), so they are judged more strictly. A first/last leg follows the road
# only when the snapped end is within END_MAX_M of the raw GPS point and its road path is at most
# END_DETOUR_MAX x the straight distance + END_SLACK_M; otherwise it is drawn as the raw straight
# line. The drawn line then always starts and ends on the raw first/last point (short joint).
END_MAX_M = 25.0
END_DETOUR_MAX = 1.5
END_SLACK_M = 40.0

Point = tuple[float, float, datetime]  # lat, lon, ts


def _dist(a: tuple, b: tuple) -> float:
    from process.geo import distance_m
    return distance_m((a[0], a[1]), (b[0], b[1]))


SpeedFn = Callable[[datetime, datetime], "float | None"]  # max reported speed (km/h) in [t0, t1]


def _allowed_m(a: Point, b: Point, speed_max: SpeedFn | None) -> float:
    """Farthest a car could plausibly get from a to b (both directions of error included)."""
    dt = max(1.0, (b[2] - a[2]).total_seconds())
    v = V_ABS_MAX
    if speed_max is not None:
        kmh = speed_max(a[2], b[2])
        if kmh is not None:
            v = min(V_ABS_MAX, kmh / 3.6 * V_MARGIN + V_SLACK)
    return v * dt + D_SLACK


def drop_jumps(points: list[Point], speed_max: SpeedFn | None = None, max_run: int = 3) -> tuple[list[Point], list[int]]:
    """Drop short runs (<= max_run points) that jump away and come back.

    A run i..j is dropped when its first point cannot be reached from the previous kept point
    in time (at the reported speed, else V_ABS_MAX), while the point after the run can be.
    First and last points are kept. Returns the kept points and the dropped indexes.
    """
    if len(points) < 3:
        return list(points), []
    keep = [True] * len(points)
    prev = 0
    i = 1
    while i < len(points) - 1:
        a = points[prev]
        if _dist(a, points[i]) <= _allowed_m(a, points[i], speed_max):
            prev = i
            i += 1
            continue
        dropped = False
        for j in range(i, min(i + max_run, len(points) - 1)):
            b = points[j + 1]
            if _dist(a, b) <= _allowed_m(a, b, speed_max):
                for k in range(i, j + 1):
                    keep[k] = False
                prev = j + 1
                i = j + 2
                dropped = True
                break
        if not dropped:  # the car really moved (or a long wrong stretch): keep it
            prev = i
            i += 1
    return [p for p, k in zip(points, keep) if k], [n for n, k in enumerate(keep) if not k]


def local_chunks(n: int, size: int = CHUNK, overlap: int = OVERLAP) -> list[tuple[int, int, int, int]]:
    """(start, end, own_from, own_to) index ranges: requests of `size` points overlapping by
    `overlap`; a leg starting at index i belongs to the chunk with own_from <= i < own_to."""
    if n <= size:
        return [(0, n, 0, n)]
    out = []
    start = 0
    while True:
        end = min(n, start + size)
        out.append([start, end])
        if end >= n:
            break
        start = end - overlap
    res = []
    for k, (a, b) in enumerate(out):
        own_from = 0 if k == 0 else a + overlap // 2
        own_to = n if k == len(out) - 1 else b - overlap // 2
        res.append((a, b, own_from, own_to))
    return res


def legs_from_osrm(data: dict[str, Any], offset: int, min_conf: float = MIN_CONFIDENCE) -> dict[int, tuple[int, list[list[float]], float]]:
    """{i: (j, [[lat, lon], ...], road_m)} for every matched leg i -> j (indexes into the trip)."""
    out: dict[int, tuple[int, list[list[float]], float]] = {}
    if data.get("code") != "Ok":
        return out
    matchings = data.get("matchings") or []
    by_m: dict[int, list[tuple[int, int]]] = {}
    for n, tp in enumerate(data.get("tracepoints") or []):
        if isinstance(tp, dict) and isinstance(tp.get("matchings_index"), int) and isinstance(tp.get("waypoint_index"), int):
            by_m.setdefault(tp["matchings_index"], []).append((tp["waypoint_index"], n))
    for m, wps in by_m.items():
        if m >= len(matchings) or (matchings[m].get("confidence") or 0) < min_conf:
            continue
        wps.sort()
        legs = matchings[m].get("legs") or []
        for k in range(min(len(legs), len(wps) - 1)):
            coords: list[list[float]] = []
            for st in legs[k].get("steps") or []:
                for c in ((st.get("geometry") or {}).get("coordinates") or []):
                    pt = [round(c[1], 6), round(c[0], 6)]
                    if not coords or coords[-1] != pt:
                        coords.append(pt)
            if len(coords) >= 1:
                out[offset + wps[k][1]] = (offset + wps[k + 1][1], coords, float(legs[k].get("distance") or 0.0))
    return out


class LocalOsrmMatcher:
    """Our own osrm-routed (see the module docstring)."""

    name = "osrm-local"
    max_attempts = LOCAL_MAX_ATTEMPTS
    per_cycle = LOCAL_TRIPS_PER_CYCLE

    def __init__(self, base_url: str = LOCAL_OSRM_URL, opener: Callable[..., Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._open = opener or urllib.request.urlopen
        self.last_info: dict[str, Any] = {}

    def _request(self, chunk: list[Point]) -> dict[str, Any]:
        coords = ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in chunk)
        stamps, prev = [], None
        for p in chunk:
            t = int(p[2].timestamp())
            if prev is not None and t <= prev:
                t = prev + 1
            stamps.append(t)
            prev = t
        q = urllib.parse.urlencode({
            "geometries": "geojson", "overview": "false", "steps": "true", "gaps": "ignore", "tidy": "false",
            "timestamps": ";".join(map(str, stamps)), "radiuses": ";".join([str(LOCAL_RADIUS_M)] * len(chunk)),
        })
        req = urllib.request.Request(f"{self.base_url}/match/v1/driving/{coords}?{q}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with self._open(req, timeout=LOCAL_TIMEOUT_S) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                try:
                    return json.loads(exc.read().decode("utf-8"))
                except (ValueError, OSError):
                    return {"code": "Error"}
            raise MatchError(f"http {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise MatchError(type(exc).__name__) from None
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise MatchError("bad json") from None

    def match(self, points: list[Point], speed_max: SpeedFn | None = None) -> dict[str, Any]:
        kept, dropped = drop_jumps(points, speed_max)
        n = len(kept)
        legs: dict[int, tuple[int, list[list[float]], float]] = {}
        for a, b, own_from, own_to in local_chunks(n):
            if b - a < 2:
                continue
            # no whole-matching confidence cut: with gaps=ignore one GPS gap drags it down; legs are judged one by one
            got = legs_from_osrm(self._request(kept[a:b]), a, min_conf=0.0)
            legs.update({i: v for i, v in got.items() if own_from <= i < own_to})
        segs: list[list[list[float]]] = []
        cur: list[list[float]] = []
        kind = ""
        matched_pts = 0
        straight_legs = 0
        i = 0
        while i < n - 1:
            leg = legs.get(i)
            ok = False
            if leg is not None:
                j, coords, road_m = leg
                ok = (len(coords) >= 2 and road_m <= _allowed_m(kept[i], kept[j], speed_max)
                      and road_m <= DETOUR_MAX * _dist(kept[i], kept[j]) + D_SLACK * 3)
                if ok and (i == 0 or j == n - 1):
                    ok = _end_leg_ok(kept, i, j, coords, road_m)
            if not ok:
                j = leg[0] if leg is not None else i + 1
                coords = [[kept[i][0], kept[i][1]], [kept[j][0], kept[j][1]]]
                straight_legs += 1
            else:
                matched_pts += j - i
            k = "road" if ok else "line"
            if k != kind:
                if len(cur) > 1:
                    segs.append(cur)
                start = cur[-1] if cur else coords[0]  # keep the drawing continuous across kinds
                cur = [start] if start != coords[0] else []
                kind = k
            for c in coords:
                if not cur or cur[-1] != c:
                    cur.append(c)
            i = j
        if len(cur) > 1:
            segs.append(cur)
        if n == 1:
            segs = [[[kept[0][0], kept[0][1]]]]
        elif segs:
            _join_ends(segs, kept)
        ratio = matched_pts / (n - 1) if n > 1 else 0.0
        self.last_info = {"points": len(points), "dropped": len(dropped), "straight_legs": straight_legs,
                          "chunks": len(local_chunks(n)) if n else 0}
        return {"segments": segs, "matched_ratio": min(1.0, ratio), "info": self.last_info}


def _end_leg_ok(kept: list[Point], i: int, j: int, coords: list[list[float]], road_m: float) -> bool:
    """Stricter check for the first and the last leg (see END_MAX_M)."""
    if road_m > END_DETOUR_MAX * _dist(kept[i], kept[j]) + END_SLACK_M:
        return False
    if i == 0 and _dist(coords[0], kept[0]) > END_MAX_M:
        return False
    if j == len(kept) - 1 and _dist(coords[-1], kept[j]) > END_MAX_M:
        return False
    return True


def _join_ends(segs: list[list[list[float]]], kept: list[Point]) -> None:
    """Make the drawn line start on the raw first point and end on the raw last point."""
    first = [kept[0][0], kept[0][1]]
    last = [kept[-1][0], kept[-1][1]]
    if segs[0][0] != first:
        segs[0].insert(0, first)
    if segs[-1][-1] != last:
        segs[-1].append(last)


class MatchError(Exception):
    pass


class Matcher(Protocol):
    name: str

    def match(self, points: list[Point]) -> dict[str, Any]: ...


def points_sig(points: list[tuple[float, float]]) -> str:
    h = hashlib.sha1(";".join(f"{p[0]:.6f},{p[1]:.6f}" for p in points).encode()).hexdigest()
    return f"{len(points)}:{h[:16]}"


def _chunks(points: list[Point]) -> list[list[Point]]:
    """Requests of at most MAX_COORDS points, overlapping by one so lines join."""
    out, i = [], 0
    while i < len(points) - 1 or (i == 0 and points):
        chunk = points[i:i + MAX_COORDS]
        out.append(chunk)
        if i + MAX_COORDS >= len(points):
            break
        i += MAX_COORDS - 1
    return out


def lines_from_osrm(points: list[Point], data: dict[str, Any], min_conf: float = MIN_CONFIDENCE) -> tuple[list[list[list[float]]], int]:
    """Drawn segments for one request and how many points were matched.

    Points in a matching with confidence >= min_conf follow that matching's road
    geometry; the rest keep straight lines between the original points.
    """
    raw = [[p[0], p[1]] for p in points]
    if data.get("code") != "Ok":
        return [raw], 0
    matchings = data.get("matchings") or []
    tps = data.get("tracepoints") or []
    good = [isinstance(m, dict) and (m.get("confidence") or 0) >= min_conf for m in matchings]
    idx: list[int | None] = []
    for i in range(len(points)):
        tp = tps[i] if i < len(tps) else None
        m = tp.get("matchings_index") if isinstance(tp, dict) else None
        idx.append(m if isinstance(m, int) and 0 <= m < len(matchings) and good[m] else None)
    segs: list[list[list[float]]] = []
    i = 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and idx[j + 1] == idx[i]:
            j += 1
        if idx[i] is None:
            a = max(0, i - 1)
            b = min(len(points) - 1, j + 1)
            seg = raw[a:b + 1]
            if len(seg) > 1:
                segs.append(seg)
        else:
            coords = ((matchings[idx[i]].get("geometry") or {}).get("coordinates")) or []
            seg = [[round(c[1], 6), round(c[0], 6)] for c in coords if isinstance(c, (list, tuple)) and len(c) >= 2]
            if len(seg) > 1:
                segs.append(seg)
            else:
                segs.append(raw[max(0, i - 1):j + 2])
        i = j + 1
    matched = sum(1 for m in idx if m is not None)
    return segs, matched


class OsrmMatcher:
    name = "osrm"

    def __init__(self, base_url: str = DEFAULT_OSRM_URL, opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._last = 0.0

    def _request(self, chunk: list[Point]) -> dict[str, Any]:
        coords = ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in chunk)
        stamps = []
        prev = None
        for p in chunk:  # OSRM wants strictly increasing UNIX seconds
            t = int(p[2].timestamp())
            if prev is not None and t <= prev:
                t = prev + 1
            stamps.append(t)
            prev = t
        q = urllib.parse.urlencode({
            "geometries": "geojson", "overview": "full", "gaps": "split", "tidy": "true",
            "timestamps": ";".join(map(str, stamps)),
            "radiuses": ";".join([str(RADIUS_M)] * len(chunk)),
        })
        url = f"{self.base_url}/match/v1/driving/{coords}?{q}"
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with self._open(req, timeout=TIMEOUT_S) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 400:  # e.g. NoMatch / TooBig: answered, just not matchable
                try:
                    return json.loads(exc.read().decode("utf-8"))
                except (ValueError, OSError):
                    return {"code": "Error"}
            raise MatchError(f"http {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise MatchError(type(exc).__name__) from None
        finally:
            self._last = time.monotonic()
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise MatchError("bad json") from None

    def match(self, points: list[Point]) -> dict[str, Any]:
        segs: list[list[list[float]]] = []
        matched = 0
        for chunk in _chunks(points):
            if len(chunk) < 2:
                continue
            s, m = lines_from_osrm(chunk, self._request(chunk))
            segs.extend(s)
            matched += m
        return {"segments": segs, "matched_ratio": min(1.0, matched / len(points)) if points else 0.0}


PROVIDERS: dict[str, Callable[[], Matcher]] = {
    "osrm": lambda: OsrmMatcher(os.environ.get("TESLIFE_OSRM_URL") or DEFAULT_OSRM_URL),
    "osrm-local": lambda: LocalOsrmMatcher(os.environ.get("TESLIFE_OSRM_URL") or LOCAL_OSRM_URL),
}


def _speed_fn(conn: sqlite3.Connection, vid: str, pts: list[Point]) -> SpeedFn | None:
    """Max reported VehicleSpeed (km/h) between two times, from raw signals (kept 31 days)."""
    if not pts:
        return None
    lo = _fmt(pts[0][2] - timedelta(minutes=2))
    hi = _fmt(pts[-1][2] + timedelta(minutes=2))
    try:
        rows = conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'VehicleSpeed' AND ts BETWEEN ? AND ? ORDER BY ts",
            (vid, lo, hi),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    series: list[tuple[datetime, float]] = []
    for ts, v in rows:
        try:
            val = json.loads(v) if isinstance(v, str) else v
            series.append((_parse(ts), float(val)))
        except (TypeError, ValueError):
            continue
    if not series:
        return None

    def speed_max(t0: datetime, t1: datetime) -> float | None:
        # the value in force at t0 plus every sample up to t1
        best = None
        for ts, v in series:
            if ts > t1:
                break
            if ts <= t0:
                best = v
            else:
                best = v if best is None else max(best, v)
        return best

    return speed_max


def from_env() -> Matcher | None:
    name = (os.environ.get("TESLIFE_ROUTE_MATCH") or "").strip().lower()
    if not name or name == "off":
        return None
    factory = PROVIDERS.get(name)
    return factory() if factory else None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _trip_points(conn: sqlite3.Connection, vid: str, start: str) -> list[Point]:
    return [
        (r[0], r[1], _parse(r[2])) for r in conn.execute(
            "SELECT lat, lon, ts FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
            (vid, start),
        ).fetchall()
    ]


def match_pending(conn: sqlite3.Connection, vid: str, matcher: Matcher, now: datetime | None = None,
                  limit: int | None = None, force: set[str] | None = None) -> int:
    """Match up to `limit` finished trips (newest first) whose points have no current cached line.

    Trips in `force` are matched again even when their cached line is current (rematch)."""
    now = now or datetime.now(timezone.utc)
    if limit is None:
        limit = getattr(matcher, "per_cycle", TRIPS_PER_CYCLE)
    max_attempts = getattr(matcher, "max_attempts", MAX_ATTEMPTS)
    starts = [r[0] for r in conn.execute(
        "SELECT DISTINCT trip_start_ts FROM trip_points WHERE vehicle_id = ? ORDER BY trip_start_ts DESC", (vid,)
    ).fetchall()]
    done = 0
    for start in starts:
        if done >= limit:
            break
        pts = _trip_points(conn, vid, start)
        if len(pts) < 2:
            continue
        sig = points_sig([(p[0], p[1]) for p in pts])
        row = conn.execute(
            "SELECT points_sig, status, attempts, fetched_at, provider FROM route_match WHERE vehicle_id = ? AND trip_start_ts = ?",
            (vid, start),
        ).fetchone()
        attempts = 0
        if force is not None and start not in force:
            continue
        if row is not None and row[0] == sig and row[4] == matcher.name and not (force and start in force):
            if row[1] != "error":
                continue
            attempts = row[2] or 0
            if attempts >= max_attempts:
                continue
            last = _parse(row[3]) if row[3] else None
            if last is not None and now - last < min(RETRY_AFTER * attempts, LOCAL_RETRY_MAX if max_attempts > MAX_ATTEMPTS else RETRY_AFTER * attempts):
                continue
        done += 1
        try:
            if isinstance(matcher, LocalOsrmMatcher):
                res = matcher.match(pts, _speed_fn(conn, vid, pts))
            else:
                res = matcher.match(pts)
            if res.get("info"):
                print(f"route match {start}: {res['info']}", flush=True)
            ratio = float(res.get("matched_ratio") or 0.0)
            status = "matched" if ratio >= 0.999 else ("partial" if ratio > 0 else "raw")
            values = (status, json.dumps(res.get("segments") or [], separators=(",", ":")), round(ratio, 3), 0)
        except MatchError as exc:
            print(f"route match {start}: {exc}", flush=True)
            values = ("error", None, None, attempts + 1)
        with conn:
            conn.execute(
                "INSERT INTO route_match (vehicle_id, trip_start_ts, provider, points_sig, status, segments, "
                "matched_ratio, attempts, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(vehicle_id, trip_start_ts) DO UPDATE SET provider = excluded.provider, "
                "points_sig = excluded.points_sig, status = excluded.status, segments = excluded.segments, "
                "matched_ratio = excluded.matched_ratio, attempts = excluded.attempts, fetched_at = excluded.fetched_at",
                (vid, start, matcher.name, sig) + values[:3] + (values[3], _fmt(now)),
            )
    return done


def cached_lines(conn: sqlite3.Connection, vid: str, start: str, points: list[tuple[float, float]]) -> list[list[list[float]]] | None:
    """The cached road-snapped line for a trip, if it is for these exact points and matched at all."""
    try:
        row = conn.execute(
            "SELECT points_sig, status, segments FROM route_match WHERE vehicle_id = ? AND trip_start_ts = ?",
            (vid, start),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[1] not in ("matched", "partial") or not row[2] or row[0] != points_sig(points):
        return None
    try:
        segs = json.loads(row[2])
    except ValueError:
        return None
    return segs or None


def rematch(conn: sqlite3.Connection, vid: str, matcher: Matcher, now: datetime | None = None) -> list[str]:
    """Match again every trip that already has a road-snapped line (after a rule change, ).

    trip_points stay as they are; only route_match rows are replaced. Road usage notices the new
    fetched_at and rebuilds those years on its next update."""
    starts = [r[0] for r in conn.execute(
        "SELECT trip_start_ts FROM route_match WHERE vehicle_id = ? AND status IN ('matched', 'partial')", (vid,)
    ).fetchall()]
    if starts:
        match_pending(conn, vid, matcher, now=now, limit=len(starts), force=set(starts))
    return starts


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Re-match trips that already have a road-snapped line.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--rematch", action="store_true", required=True)
    args = parser.parse_args(argv)
    matcher = from_env() or LocalOsrmMatcher(os.environ.get("TESLIFE_OSRM_URL") or LOCAL_OSRM_URL)
    conn = sqlite3.connect(args.db)
    try:
        vids = [r[0] for r in conn.execute("SELECT DISTINCT vehicle_id FROM route_match").fetchall()]
        for vid in vids:
            print(f"rematched {len(rematch(conn, vid, matcher))} trips")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
