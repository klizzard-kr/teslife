"""Shape the lines drawn on a trip or charge detail graph.

Every line starts at the window with the last value received before it.
While the car sleeps nothing arrives, and the first report after it wakes is
already the current value, so that earlier report is still current at the
start. A value that only arrives when it changes is then held flat until the
window ends, so one report is a flat line across the whole trip or charge.
Inside the window, received values stay connected. A temperature line is the
straight segments between those reports. A level such as fan speed or a seat
heater holds until the next report.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

Point = tuple[datetime, float]
Span = tuple[datetime, datetime]


def with_start_value(
    samples: list[Point],
    start: datetime,
    prior: Point | None,
) -> list[Point]:
    """Put the last value from before `start` on the start, then the reports inside."""
    ordered = sorted((ts, value) for ts, value in samples if ts >= start)
    if prior is None or prior[0] >= start:
        return ordered
    if ordered and ordered[0][0] <= start:
        return ordered
    return [(start, prior[1]), *ordered]


def segments(points: list[Point]) -> list[list[Point]]:
    """One run of reports in time order. A repeated timestamp keeps the later value."""
    cur: list[Point] = []
    for ts, value in points:
        if cur and ts < cur[-1][0]:
            continue
        if cur and ts == cur[-1][0]:
            cur[-1] = (ts, value)
            continue
        cur.append((ts, value))
    return [cur] if cur else []


def hold_through(points: list[Point], start: datetime, end: datetime) -> list[Point]:
    """Hold the last value flat until `end`. One value covers the whole window."""
    if not points or end <= start:
        return points
    if len(points) == 1:
        value = points[0][1]
        return [(start, value), (end, value)]
    if points[-1][0] < end:
        return [*points, (end, points[-1][1])]
    return points


def gate_fan(
    samples: list[Point],
    prior: Point | None,
    on_spans: list[Span],
    start: datetime,
    end: datetime,
    power_known: bool,
) -> list[list[Point]]:
    """Fan speed is drawn only while HvacPower is On or Precondition.

    The last received speed is kept even when it arrived before the window or
    while power was off. Turning power on draws that speed, including when no
    new fan report arrives. Turning power off draws 0. With no HvacPower at
    all, the raw speed is kept.
    """
    if not power_known:
        return series_segments(samples, start, prior, end)

    def on_at(ts: datetime) -> bool:
        return any(a <= ts < b for a, b in on_spans)

    on_at_start = on_at(start)
    remembered = float(prior[1]) if prior is not None else 0.0
    pts: list[Point] = [(start, remembered if on_at_start else 0.0)]
    events: list[tuple[datetime, int, float]] = []
    for ts, value in samples:
        if start <= ts <= end:
            events.append((ts, 1, float(value)))
    for a, b in on_spans:
        if start <= a <= end:
            events.append((a, 0, 1.0))
        # A span that runs to the window end is still on. Only an off inside the window draws 0.
        if start < b < end:
            events.append((b, 2, 0.0))
    events.sort()
    hvac_on = on_at_start
    for ts, kind, value in events:
        if kind == 0:
            hvac_on = True
            pts.append((ts, remembered))
        elif kind == 2:
            hvac_on = False
            pts.append((ts, 0.0))
        else:
            remembered = value
            if hvac_on:
                pts.append((ts, value))
    if not pts or pts[-1][0] < end:
        pts.append((end, remembered if hvac_on else 0.0))
    return segments(pts)


def series_segments(
    samples: list[Point],
    start: datetime,
    prior: Point | None,
    end: datetime | None = None,
) -> list[list[Point]]:
    points = with_start_value(samples, start, prior)
    if end is not None:
        points = hold_through(points, start, end)
    return segments(points)


def flat_average(runs: list[list[Point]], end: datetime | None = None) -> float | None:
    """Time-weighted mean. Each report holds flat until the next one."""
    dedup: list[Point] = []
    for ts, value in sorted(p for run in runs for p in run):
        if dedup and dedup[-1][0] == ts:
            dedup[-1] = (ts, value)
        else:
            dedup.append((ts, value))
    if not dedup:
        return None
    if len(dedup) == 1:
        return dedup[0][1]
    weighted = 0.0
    covered = 0.0
    for i, (ts, value) in enumerate(dedup):
        if i + 1 < len(dedup):
            nxt = dedup[i + 1][0]
        elif end is not None:
            nxt = end
        else:
            continue
        span = (nxt - ts).total_seconds()
        if span <= 0:
            continue
        weighted += value * span
        covered += span
    if covered <= 0:
        return dedup[-1][1]
    return weighted / covered


def series_low(runs: list[list[Point]]) -> float | None:
    vals = [value for run in runs for _ts, value in run]
    return min(vals) if vals else None


def series_high(runs: list[list[Point]]) -> float | None:
    vals = [value for run in runs for _ts, value in run]
    return max(vals) if vals else None


def level_spans(
    samples: list[tuple[datetime, Any]],
    start: datetime,
    end: datetime,
    prior: tuple[datetime, Any] | None,
    level_of: Callable[[Any], float | None],
) -> list[tuple[datetime, datetime, float]]:
    """Hold each non-zero level until the next report, including across a long quiet stretch.

    A zero level draws nothing. Adjacent equal levels are merged.
    """
    numeric: list[Point] = []
    for ts, value in samples:
        level = level_of(value)
        if level is None or ts < start or ts > end:
            continue
        numeric.append((ts, level))
    prior_point = None
    if prior is not None:
        level = level_of(prior[1])
        if level is not None and prior[0] < start:
            prior_point = (prior[0], level)
    pts = with_start_value(numeric, start, prior_point)
    spans: list[tuple[datetime, datetime, float]] = []
    for i, (ts, level) in enumerate(pts):
        if level == 0:
            continue
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        if nxt <= ts:
            continue
        if spans and spans[-1][1] == ts and spans[-1][2] == level:
            spans[-1] = (spans[-1][0], nxt, level)
        else:
            spans.append((ts, nxt, level))
    return spans


def _on_flag(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value == 0 or value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if text in ("Off", "0", "false", "False") or text.endswith("Off"):
            return False
        if text in ("On", "Max", "Auto", "true", "True") or text.endswith(("On", "Max")):
            return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) != 0.0
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _hvac_setpoint_on(value: Any) -> bool:
    """Set-temperature is drawn while HvacPower is On or Precondition."""
    if not isinstance(value, str):
        return value is True
    text = value.strip()
    if text.endswith("Off"):
        return False
    return text in ("On", "Precondition") or text.endswith(("On", "Precondition"))


def _flag_points(
    samples: list[tuple[datetime, Any]],
    start: datetime,
    prior: tuple[datetime, Any] | None,
) -> list[Point]:
    numeric = [(ts, 1.0 if _on_flag(value) else 0.0) for ts, value in samples if start <= ts]
    prior_point = None
    if prior is not None and prior[0] < start:
        prior_point = (prior[0], 1.0 if _on_flag(prior[1]) else 0.0)
    return with_start_value(numeric, start, prior_point)


def _held(points: list[Point], t: datetime) -> float | None:
    last: Point | None = None
    for ts, value in points:
        if ts <= t:
            last = (ts, value)
        else:
            break
    return None if last is None else last[1]


def setpoint_segments(
    samples: list[tuple[datetime, Any]],
    prior: tuple[datetime, Any] | None,
    on_spans: list[Span],
    start: datetime,
) -> list[list[Point]]:
    """Set-temperature samples, kept only while HVAC is on."""
    numeric = []
    for ts, value in samples:
        number = _number(value)
        if number is None or ts < start:
            continue
        numeric.append((ts, number))
    prior_point = None
    if prior is not None and prior[0] < start:
        number = _number(prior[1])
        if number is not None:
            prior_point = (prior[0], number)
    pts = with_start_value(numeric, start, prior_point)
    out: list[list[Point]] = []
    for a, b in on_spans:
        if b <= a:
            continue
        held: Point | None = None
        inside: list[Point] = []
        for ts, value in pts:
            if ts < a:
                held = (ts, value)
                continue
            if ts > b:
                break
            inside.append((ts, value))
        chunk: list[Point] = []
        if held is not None:
            chunk.append((a, held[1]))
        chunk.extend(inside)
        if chunk and b > chunk[-1][0]:
            chunk.append((b, chunk[-1][1]))
        if chunk:
            out.extend(segments(chunk))
    return out


def _differs(driver: list[list[Point]], passenger: list[list[Point]], tol: float = 0.05) -> bool:
    if not passenger:
        return False
    times = sorted({ts for seg in driver for ts, _v in seg} | {ts for seg in passenger for ts, _v in seg})
    flat_d = [p for seg in driver for p in seg]
    flat_p = [p for seg in passenger for p in seg]
    for ts in times:
        left = _held(flat_d, ts)
        right = _held(flat_p, ts)
        if left is None or right is None:
            continue
        if abs(left - right) > tol:
            return True
    return False


def _nums(
    samples: list[tuple[datetime, Any]], start: datetime,
) -> list[Point]:
    out = []
    for ts, value in samples:
        number = _number(value)
        if number is None or ts < start:
            continue
        out.append((ts, number))
    return out


def _prior_num(prior: tuple[datetime, Any] | None, start: datetime) -> Point | None:
    if prior is None or prior[0] >= start:
        return None
    number = _number(prior[1])
    if number is None:
        return None
    return (prior[0], number)


# SeatHeater* is 0 off, 1–3 heat. A negative value is ventilation at that
# magnitude (also 1–3). The steering-wheel heater is 0–3 and does not cool.
SEAT_KEYS = (
    ("seat_left", "SeatHeaterLeft"),
    ("seat_right", "SeatHeaterRight"),
    ("seat_rear_left", "SeatHeaterRearLeft"),
    ("seat_rear_center", "SeatHeaterRearCenter"),
    ("seat_rear_right", "SeatHeaterRearRight"),
)
FLAG_BARS = (
    ("defrost", "DefrostMode"),
    ("rear_defrost", "RearDefrostEnabled"),
    ("wiper_heat", "WiperHeatEnabled"),
    ("auto_seat_left", "AutoSeatClimateLeft"),
    ("auto_seat_right", "AutoSeatClimateRight"),
    ("wheel_auto", "SteeringWheelHeatAuto"),
    ("rear_hvac", "RearDisplayHvacEnabled"),
)


def _heat_level(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return 0.0 if number < 0 else number


def _cool_level(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return abs(number) if number < 0 else 0.0


def build_climate(
    series: dict[str, list[tuple[datetime, Any]]],
    priors: dict[str, tuple[datetime, Any]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Climate graph series for one trip. Values are not clamped."""
    def samples(name: str) -> list[tuple[datetime, Any]]:
        return list(series.get(name) or [])

    def prior(name: str) -> tuple[datetime, Any] | None:
        return priors.get(name)

    fan_name = "HvacFanSpeed" if samples("HvacFanSpeed") or prior("HvacFanSpeed") else "HvacFanStatus"
    inside = series_segments(_nums(samples("InsideTemp"), start), start, _prior_num(prior("InsideTemp"), start), end)
    outside = series_segments(_nums(samples("OutsideTemp"), start), start, _prior_num(prior("OutsideTemp"), start), end)
    hvac = level_spans(
        samples("HvacPower"), start, end, prior("HvacPower"),
        lambda value: 1.0 if _hvac_setpoint_on(value) else 0.0,
    )
    on_spans = [(a, b) for a, b, _level in hvac]
    driver = setpoint_segments(samples("HvacLeftTemperatureRequest"), prior("HvacLeftTemperatureRequest"), on_spans, start)
    passenger = setpoint_segments(samples("HvacRightTemperatureRequest"), prior("HvacRightTemperatureRequest"), on_spans, start)
    if not _differs(driver, passenger):
        passenger = []
    power_known = bool(samples("HvacPower") or prior("HvacPower"))
    fan = gate_fan(
        _nums(samples(fan_name), start),
        _prior_num(prior(fan_name), start),
        on_spans,
        start,
        end,
        power_known,
    )

    def level_series(name: str, level_of: Callable[[Any], float | None]) -> list[list[Point]]:
        numeric: list[Point] = []
        for ts, value in samples(name):
            level = level_of(value)
            if level is None or ts < start:
                continue
            numeric.append((ts, level))
        prior_point = None
        earlier = prior(name)
        if earlier is not None and earlier[0] < start:
            level = level_of(earlier[1])
            if level is not None:
                prior_point = (earlier[0], level)
        segs = series_segments(numeric, start, prior_point, end)
        if any(value > 0 for seg in segs for _ts, value in seg):
            return segs
        return []

    heat: dict[str, list[list[Point]]] = {}
    cool: dict[str, list[list[Point]]] = {}
    for key, name in SEAT_KEYS:
        heated = level_series(name, _heat_level)
        cooled = level_series(name, _cool_level)
        if heated:
            heat[key] = heated
        if cooled:
            cool[key] = cooled
    wheel = level_series("HvacSteeringWheelHeatLevel", _heat_level)
    bars: dict[str, list[tuple[datetime, datetime, float]]] = {}
    for key, name in FLAG_BARS:
        spans = level_spans(
            samples(name), start, end, prior(name),
            lambda value: 1.0 if _on_flag(value) else 0.0,
        )
        if spans:
            bars[key] = spans
    return {
        "inside": inside,
        "outside": outside,
        "set_driver": driver,
        "set_passenger": passenger,
        "fan": fan,
        "heat": heat,
        "cool": cool,
        "wheel": wheel,
        "auto": _flag_points(samples("HvacAutoMode"), start, prior("HvacAutoMode")),
        "ac": _flag_points(samples("HvacACEnabled"), start, prior("HvacACEnabled")),
        "bars": bars,
        "avg_inside": flat_average(inside, end),
        "avg_outside": flat_average(outside, end),
    }


def build_battery(
    series: dict[str, list[tuple[datetime, Any]]],
    priors: dict[str, tuple[datetime, Any]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Battery min and max stay independent. A sample with min above max is kept."""
    def one(name: str) -> list[list[Point]]:
        return series_segments(
            _nums(series.get(name) or [], start), start, _prior_num(priors.get(name), start), end,
        )

    mins = one("ModuleTempMin")
    maxs = one("ModuleTempMax")
    heater = level_spans(
        series.get("BatteryHeaterOn") or [], start, end, priors.get("BatteryHeaterOn"),
        lambda value: 1.0 if _on_flag(value) else 0.0,
    )
    return {
        "min": mins, "max": maxs, "heater": heater,
        "low": series_low(mins), "high": series_high(maxs),
    }


def charge_axis_top(rated: float | None, observed: float | None) -> float:
    """Top of the charge-power axis, in kW.

    A recorded charger rating is always the top, even when this session's peak
    is much lower or a moment went above the rating. With no rating, the top
    is the observed peak.
    """
    if rated is not None and rated > 0:
        return float(rated)
    obs = float(observed) if observed is not None and observed > 0 else 0.0
    return obs if obs > 0 else 1.0


def flag_at(points: list[Point], t: datetime) -> bool:
    held = _held(points, t)
    return held is not None and held >= 0.5
