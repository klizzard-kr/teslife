"""Map events from adjacent VehicleSpeed samples.

The rate is (v2 - v1) / dt in km/h per second. The timestamps are the samples'
own times (the car's CreatedAt, stored as signals.ts). This does not read the
pedal, the brake, or acceleration, and it does not claim a collision.

A single interval is enough. The old acceleration rule required the value to
stay over a threshold for 2 seconds; a speed change is already the change
across the gap, so that hold is not copied.

Samples farther apart than MAX_DT_S are a gap, not a slow event. Samples closer
than MIN_DT_S are skipped because a 1 mph step (about 1.6 km/h) over a very
short gap can look like a large rate. Same-sign hits that start within MERGE_S
of the previous hit's end are one event, keeping the peak rate.
"""

from __future__ import annotations

from datetime import datetime

# Strictly past ±15 km/h/s. Exactly +15 and −15 are not events. A 1 mph step
# (~1.6 km/h) over a short gap is still dropped by MIN_DT_S.
SPEED_RATE_KMH_S = 15.0
MIN_DT_S = 0.4
MAX_DT_S = 2.5
MERGE_S = 3.0


def _rate(v1: float, v2: float, dt: float) -> float | None:
    if dt < MIN_DT_S or dt > MAX_DT_S:
        return None
    return (v2 - v1) / dt


def speed_events(samples: list[tuple[datetime, float]]) -> dict[str, list[tuple[datetime, float]] | None]:
    """{'accel': [(start, peak km/h/s), ...], 'brake': [...], 'turn': None}.

    turn is always None: lateral acceleration is not used. accel/brake are None
    when there are no speed samples, so a later pass can keep stored rows.
    An empty list means speeds were read and nothing crossed the rate.
    """
    if len(samples) < 2:
        return {"accel": None, "brake": None, "turn": None}
    ordered = sorted(samples, key=lambda p: p[0])
    hits: list[tuple[str, datetime, datetime, float]] = []
    for (t1, v1), (t2, v2) in zip(ordered, ordered[1:]):
        dt = (t2 - t1).total_seconds()
        if dt <= 0:
            continue
        rate = _rate(v1, v2, dt)
        # Strict: exactly ±15 is kept out. Accel is above +15, brake is below -15.
        if rate is None or abs(rate) <= SPEED_RATE_KMH_S:
            continue
        kind = "accel" if rate > 0 else "brake"
        hits.append((kind, t1, t2, abs(rate)))
    grouped: dict[str, list[tuple[datetime, float]]] = {"accel": [], "brake": []}
    open_kind: str | None = None
    open_start: datetime | None = None
    open_end: datetime | None = None
    open_peak = 0.0

    def close() -> None:
        nonlocal open_kind, open_start, open_end, open_peak
        if open_kind is None or open_start is None:
            return
        grouped[open_kind].append((open_start, round(open_peak, 3)))
        open_kind = open_start = open_end = None
        open_peak = 0.0

    for kind, t1, t2, peak in hits:
        if open_kind == kind and open_end is not None and (t1 - open_end).total_seconds() <= MERGE_S:
            open_end = t2
            open_peak = max(open_peak, peak)
            continue
        close()
        open_kind, open_start, open_end, open_peak = kind, t1, t2, peak
    close()
    return {"accel": grouped["accel"], "brake": grouped["brake"], "turn": None}


def event_details(samples: list[tuple[datetime, float]]) -> list[dict]:
    """One dict per harsh speed change, for the trip graph popover.

    speed_before/after are the speeds at the ends of the (possibly merged)
    run. duration_s is that span. rate_kmh_s is the peak |km/h per second|
    that crossed the detection line. Empty when there are too few samples.
    """
    if len(samples) < 2:
        return []
    ordered = sorted(samples, key=lambda p: p[0])
    hits: list[tuple[str, datetime, datetime, float, float, float]] = []
    for (t1, v1), (t2, v2) in zip(ordered, ordered[1:]):
        dt = (t2 - t1).total_seconds()
        if dt <= 0:
            continue
        rate = _rate(v1, v2, dt)
        if rate is None or abs(rate) <= SPEED_RATE_KMH_S:
            continue
        kind = "accel" if rate > 0 else "brake"
        hits.append((kind, t1, t2, v1, v2, abs(rate)))
    out: list[dict] = []
    open_kind: str | None = None
    open_start: datetime | None = None
    open_end: datetime | None = None
    open_v1 = 0.0
    open_v2 = 0.0
    open_peak = 0.0

    def close() -> None:
        nonlocal open_kind, open_start, open_end, open_peak
        if open_kind is None or open_start is None or open_end is None:
            return
        delta = open_v2 - open_v1
        out.append({
            "kind": open_kind,
            "ts": open_start,
            "speed_before": round(open_v1, 1),
            "speed_after": round(open_v2, 1),
            "delta_kph": round(delta, 1),
            "duration_s": round((open_end - open_start).total_seconds(), 3),
            "rate_kmh_s": round(open_peak, 3),
        })
        open_kind = open_start = open_end = None
        open_peak = 0.0

    for kind, t1, t2, v1, v2, peak in hits:
        if open_kind == kind and open_end is not None and (t1 - open_end).total_seconds() <= MERGE_S:
            open_end = t2
            open_v2 = v2
            open_peak = max(open_peak, peak)
            continue
        close()
        open_kind, open_start, open_end = kind, t1, t2
        open_v1, open_v2, open_peak = v1, v2, peak
    close()
    return out
