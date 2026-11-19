"""Shape the lines drawn on a trip or charge detail graph.

A sampled line (temperature, power, speed, fan) starts at the window with the
last value from before it only when telemetry was still streaming across that
boundary. The same line is not drawn across a hole longer than STREAM_GAP:
those records are missing, or the car was asleep. A level such as a seat
heater holds until the next report.

STREAM_GAP matches the awake gap used for the day view (15 minutes).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

STREAM_GAP = timedelta(minutes=15)
# A charger rated above this multiple of the observed peak is stronger than
# this session used. The axis then stops at the observed peak so the curve
# fills the chart. A rating at or under the multiple stays the top of the axis.
RATED_HEADROOM = 1.5

Point = tuple[datetime, float]
Span = tuple[datetime, datetime]


def streaming_across(
    before: datetime | None, after: datetime | None, gap: timedelta = STREAM_GAP,
) -> bool:
    """True when a sample before the boundary and one at or after it are within `gap`."""
    if before is None or after is None or after < before:
        return False
    return (after - before) <= gap


def with_start_value(
    samples: list[Point],
    start: datetime,
    prior: Point | None,
    continuous: bool,
    gap: timedelta = STREAM_GAP,
) -> list[Point]:
    """Prepend `start` with the previous value when the stream crossed `start`.

    No prepend when the car was asleep, when this signal's own previous report
    is already a hole, or when a sample already sits on the boundary.
    """
    ordered = sorted((ts, value) for ts, value in samples if ts >= start)
    if prior is None or not continuous:
        return ordered
    if start - prior[0] > gap:
        return ordered
    if ordered and ordered[0][0] <= start:
        return ordered
    if ordered and ordered[0][0] - prior[0] > gap:
        return ordered
    return [(start, prior[1]), *ordered]


def segments(points: list[Point], gap: timedelta = STREAM_GAP) -> list[list[Point]]:
    """Split where two reports are more than `gap` apart. Order is kept."""
    out: list[list[Point]] = []
    cur: list[Point] = []
    for ts, value in points:
        if cur and ts < cur[-1][0]:
            continue
        if cur and ts - cur[-1][0] > gap:
            out.append(cur)
            cur = []
        if cur and ts == cur[-1][0]:
            cur[-1] = (ts, value)
            continue
        cur.append((ts, value))
    if cur:
        out.append(cur)
    return out


def series_segments(
    samples: list[Point],
    start: datetime,
    prior: Point | None,
    continuous: bool,
    gap: timedelta = STREAM_GAP,
) -> list[list[Point]]:
    return segments(with_start_value(samples, start, prior, continuous, gap), gap)


def level_spans(
    samples: list[tuple[datetime, Any]],
    start: datetime,
    end: datetime,
    prior: tuple[datetime, Any] | None,
    continuous: bool,
    level_of: Callable[[Any], float | None],
    gap: timedelta = STREAM_GAP,
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
    pts = with_start_value(numeric, start, prior_point, continuous, gap)
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
    continuous: bool,
) -> list[Point]:
    numeric = [(ts, 1.0 if _on_flag(value) else 0.0) for ts, value in samples if start <= ts]
    prior_point = None
    if prior is not None and prior[0] < start:
        prior_point = (prior[0], 1.0 if _on_flag(prior[1]) else 0.0)
    return with_start_value(numeric, start, prior_point, continuous)


def _held(points: list[Point], t: datetime, gap: timedelta = STREAM_GAP) -> float | None:
    last: Point | None = None
    for ts, value in points:
        if ts <= t:
            last = (ts, value)
        else:
            break
    if last is None or t - last[0] > gap:
        return None
    return last[1]


def setpoint_segments(
    samples: list[tuple[datetime, Any]],
    prior: tuple[datetime, Any] | None,
    on_spans: list[Span],
    start: datetime,
    continuous: bool,
    gap: timedelta = STREAM_GAP,
) -> list[list[Point]]:
    """Set-temperature samples, kept only while HVAC is on, still split on holes."""
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
    pts = with_start_value(numeric, start, prior_point, continuous, gap)
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
        if held is not None and (not inside or inside[0][0] - held[0] <= gap):
            chunk.append((a, held[1]))
        for ts, value in inside:
            if chunk and ts - chunk[-1][0] > gap:
                out.extend(segments(chunk, gap))
                chunk = []
            chunk.append((ts, value))
        if chunk and b > chunk[-1][0] and b - chunk[-1][0] <= gap:
            chunk.append((b, chunk[-1][1]))
        if chunk:
            out.extend(segments(chunk, gap))
    return out


def _differs(driver: list[list[Point]], passenger: list[list[Point]], tol: float = 0.05) -> bool:
    if not passenger:
        return False
    times = sorted({ts for seg in driver for ts, _v in seg} | {ts for seg in passenger for ts, _v in seg})
    flat_d = [p for seg in driver for p in seg]
    flat_p = [p for seg in passenger for p in seg]
    for ts in times:
        left = _held(flat_d, ts, gap=STREAM_GAP)
        right = _held(flat_p, ts, gap=STREAM_GAP)
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


BAR_KEYS = (
    ("seat_left", "SeatHeaterLeft", "level"),
    ("seat_right", "SeatHeaterRight", "level"),
    ("seat_rear_left", "SeatHeaterRearLeft", "level"),
    ("seat_rear_center", "SeatHeaterRearCenter", "level"),
    ("seat_rear_right", "SeatHeaterRearRight", "level"),
    ("wheel", "HvacSteeringWheelHeatLevel", "level"),
    ("defrost", "DefrostMode", "flag"),
    ("rear_defrost", "RearDefrostEnabled", "flag"),
    ("wiper_heat", "WiperHeatEnabled", "flag"),
)


def build_climate(
    series: dict[str, list[tuple[datetime, Any]]],
    priors: dict[str, tuple[datetime, Any]],
    start: datetime,
    end: datetime,
    continuous: bool,
) -> dict[str, Any]:
    """Climate graph series for one trip. Values are not clamped."""
    def samples(name: str) -> list[tuple[datetime, Any]]:
        return list(series.get(name) or [])

    def prior(name: str) -> tuple[datetime, Any] | None:
        return priors.get(name)

    fan_name = "HvacFanSpeed" if samples("HvacFanSpeed") or prior("HvacFanSpeed") else "HvacFanStatus"
    inside = series_segments(_nums(samples("InsideTemp"), start), start, _prior_num(prior("InsideTemp"), start), continuous)
    outside = series_segments(_nums(samples("OutsideTemp"), start), start, _prior_num(prior("OutsideTemp"), start), continuous)
    hvac = level_spans(
        samples("HvacPower"), start, end, prior("HvacPower"), continuous,
        lambda value: 1.0 if _hvac_setpoint_on(value) else 0.0,
    )
    on_spans = [(a, b) for a, b, _level in hvac]
    driver = setpoint_segments(samples("HvacLeftTemperatureRequest"), prior("HvacLeftTemperatureRequest"), on_spans, start, continuous)
    passenger = setpoint_segments(samples("HvacRightTemperatureRequest"), prior("HvacRightTemperatureRequest"), on_spans, start, continuous)
    if not _differs(driver, passenger):
        passenger = []
    fan = series_segments(_nums(samples(fan_name), start), start, _prior_num(prior(fan_name), start), continuous)
    bars: dict[str, list[tuple[datetime, datetime, float]]] = {}
    for key, name, kind in BAR_KEYS:
        if kind == "flag":
            spans = level_spans(
                samples(name), start, end, prior(name), continuous,
                lambda value: 1.0 if _on_flag(value) else 0.0,
            )
        else:
            spans = level_spans(samples(name), start, end, prior(name), continuous, _number)
        if spans:
            bars[key] = spans
    return {
        "inside": inside,
        "outside": outside,
        "set_driver": driver,
        "set_passenger": passenger,
        "fan": fan,
        "auto": _flag_points(samples("HvacAutoMode"), start, prior("HvacAutoMode"), continuous),
        "ac": _flag_points(samples("HvacACEnabled"), start, prior("HvacACEnabled"), continuous),
        "bars": bars,
    }


def build_battery(
    series: dict[str, list[tuple[datetime, Any]]],
    priors: dict[str, tuple[datetime, Any]],
    start: datetime,
    end: datetime,
    continuous: bool,
) -> dict[str, Any]:
    """Battery min and max stay independent. A sample with min above max is kept."""
    def one(name: str) -> list[list[Point]]:
        return series_segments(
            _nums(series.get(name) or [], start), start, _prior_num(priors.get(name), start), continuous,
        )

    heater = level_spans(
        series.get("BatteryHeaterOn") or [], start, end, priors.get("BatteryHeaterOn"), continuous,
        lambda value: 1.0 if _on_flag(value) else 0.0,
    )
    return {"min": one("ModuleTempMin"), "max": one("ModuleTempMax"), "heater": heater}


def charge_axis_top(rated: float | None, observed: float | None) -> float:
    """Top of the charge-power axis, in kW.

    The selected charger's rated output is the top. With no charger, the top is
    the observed peak. A rating more than RATED_HEADROOM times that peak is
    capped at the peak so a much stronger stall does not flatten the curve.
    A peak above the rating is not clipped.
    """
    obs = float(observed) if observed is not None and observed > 0 else 0.0
    if rated is None or rated <= 0:
        return obs if obs > 0 else 1.0
    rate = float(rated)
    if obs <= 0:
        return rate
    if obs > rate:
        return obs
    if rate > obs * RATED_HEADROOM:
        return obs
    return rate


def flag_at(points: list[Point], t: datetime, gap: timedelta = STREAM_GAP) -> bool:
    held = _held(points, t, gap)
    return held is not None and held >= 0.5
