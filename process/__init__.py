"""Segment raw signals into trips, charges, tire readings, parked usage and
hourly battery, then hand state changes to the alert step.

Trips are split by gear only: P -> D/R/N starts one, back to P closes it.
A park of PARK_MERGE or less stays in the same trip (a brief stop, not a new
drive). Lock changes alone do not split a trip. The old web merge table
`trip_merges` is unused.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from process.geo import distance_m, locate, thin
from ledger import ensure_schema, seed_insurance, sync_auto_charges
from ledger.consumables import seed_consumables

PROCESS_VERSION = "25"  # 25: fan speed carries across HVAC off->on. 24: port-open joins adjacent bouts only
# : tire_readings is a short working window, not a history. Keep TIRE_KEEP before the
# newest reading: enough for tire_low (latest cold, 48 h), the slow-leak check (cold value
# 6-8 days earlier) and the trip alerts. Older rows are deleted on every process run.
TIRE_KEEP = timedelta(days=10)
KST = timezone(timedelta(hours=9))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trips (
    vehicle_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    km REAL NOT NULL,
    kwh REAL,
    wh_per_km REAL,
    start_soc REAL,
    end_soc REAL,
    avg_outside_temp REAL,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS charges (
    vehicle_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    kwh_added REAL,
    kind TEXT NOT NULL,
    start_soc REAL,
    end_soc REAL,
    max_power REAL,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS tire_readings (
    vehicle_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    fl REAL,
    fr REAL,
    rl REAL,
    rr REAL,
    outside_temp REAL,
    phase TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, ts)
);

CREATE TABLE IF NOT EXISTS trip_merges (
    vehicle_id TEXT NOT NULL,
    merge_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS parked_segments (
    vehicle_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    category TEXT NOT NULL,
    sentry INTEGER NOT NULL DEFAULT 0,
    kwh REAL,
    kwh_estimated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS parked_usage (
    vehicle_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    hours REAL,
    kwh REAL,
    start_soc REAL,
    end_soc REAL,
    soc_drop REAL,
    charged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS battery_hourly (
    vehicle_id TEXT NOT NULL,
    hour TEXT NOT NULL,
    last_pct REAL,
    min_pct REAL,
    max_pct REAL,
    used_pct REAL NOT NULL DEFAULT 0,
    charged_pct REAL NOT NULL DEFAULT 0,
    samples INTEGER NOT NULL DEFAULT 0,
    driving INTEGER NOT NULL DEFAULT 0,
    charging INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, hour)
);

CREATE TABLE IF NOT EXISTS vehicle_alerts (
    vehicle_id TEXT NOT NULL,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    audiences TEXT,
    received_ts TEXT,
    notified_start INTEGER NOT NULL DEFAULT 0,
    notified_end INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, name, started_at)
);

CREATE TABLE IF NOT EXISTS alert_state (
    vehicle_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT,
    updated_ts TEXT,
    PRIMARY KEY (vehicle_id, kind)
);

CREATE TABLE IF NOT EXISTS alert_log (
    vehicle_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS trip_points (
    vehicle_id TEXT NOT NULL,
    trip_start_ts TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    PRIMARY KEY (vehicle_id, trip_start_ts, seq)
);

CREATE TABLE IF NOT EXISTS trip_events (
    vehicle_id TEXT NOT NULL,
    trip_start_ts TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    g REAL,
    lat REAL,
    lon REAL,
    PRIMARY KEY (vehicle_id, trip_start_ts, ts, kind)
);

CREATE TABLE IF NOT EXISTS route_match (
    vehicle_id TEXT NOT NULL,
    trip_start_ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    points_sig TEXT NOT NULL,
    status TEXT NOT NULL,
    segments TEXT,
    matched_ratio REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    PRIMARY KEY (vehicle_id, trip_start_ts)
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 150,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    provider TEXT NOT NULL,
    key TEXT NOT NULL,
    label TEXT,
    label_en TEXT,
    fetched_at TEXT,
    PRIMARY KEY (provider, key)
);

CREATE TABLE IF NOT EXISTS sentry_alarms (
    vehicle_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    last_ts TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    lat REAL,
    lon REAL,
    place TEXT,
    notified_ts TEXT,
    notify_status TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, start_ts)
);

CREATE TABLE IF NOT EXISTS process_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS signals_by_name ON signals (vehicle_id, name, ts);
"""

# Columns added to the stage-2 tables. Older databases get them with ALTER TABLE.
TRIP_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("duration_s", "REAL"),
    ("km_per_kwh", "REAL"),
    ("kwh_estimated", "INTEGER"),
    ("start_odometer", "REAL"),
    ("end_odometer", "REAL"),
    ("start_lifetime_kwh", "REAL"),
    ("end_lifetime_kwh", "REAL"),
    ("regen_kwh", "REAL"),
    ("regen_ratio", "REAL"),
    ("regen_estimated", "INTEGER"),
    ("max_power_kw", "REAL"),
    ("max_regen_kw", "REAL"),
    ("start_range_km", "REAL"),
    ("end_range_km", "REAL"),
    ("start_est_range_km", "REAL"),
    ("end_est_range_km", "REAL"),
    ("avg_speed", "REAL"),
    ("max_speed", "REAL"),
    ("avg_inside_temp", "REAL"),
    ("battery_temp_min", "REAL"),
    ("battery_temp_max", "REAL"),
    ("battery_heater_on", "INTEGER"),
    ("hvac_on", "INTEGER"),
    ("seat_heat_left_max", "INTEGER"),
    ("seat_heat_right_max", "INTEGER"),
    ("wheel_heat_max", "INTEGER"),
    ("fan_max", "INTEGER"),
    ("fan_avg", "REAL"),
    ("hard_accel", "INTEGER"),
    ("hard_brake", "INTEGER"),
    ("hard_turn", "INTEGER"),
    ("tire_before", "TEXT"),
    ("tire_after", "TEXT"),
    ("start_gear", "TEXT"),
    ("start_lat", "REAL"),
    ("start_lon", "REAL"),
    ("end_lat", "REAL"),
    ("end_lon", "REAL"),
    ("km_source", "TEXT"),  # odometer, speed, or gps; null on rows from before this column
)
CHARGE_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("max_charger_voltage", "REAL"),
    ("max_charge_amps", "REAL"),
    ("max_charger_kw", "REAL"),
    ("time_to_full_h", "REAL"),
    ("charger_kwh", "REAL"),
    ("battery_kwh", "REAL"),
    ("loss_kwh", "REAL"),
    ("loss_pct", "REAL"),
    ("sentry_min", "REAL"),
    ("est_full_ts", "TEXT"),
    ("fast_charger", "TEXT"),  # DC only: FastChargerType during the charge (Supercharger, Combo, ...)
    ("ac_kwh", "REAL"),  # ACChargingEnergyIn rise; null on rows processed before the column existed
    ("dc_kwh", "REAL"),  # DCChargingEnergyIn rise
    ("avg_charger_kw", "REAL"),  # time-weighted mean of the same samples as max_charger_kw
    ("active_s", "REAL"),  # charging time with the pauses taken out; null when it cannot be told
)

TPMS_NAMES = (
    "TpmsPressureFl",
    "TpmsPressureFr",
    "TpmsPressureRl",
    "TpmsPressureRr",
)
WHEELS = ("fl", "fr", "rl", "rr")
DRIVE_GEARS = ("D", "R", "N")
COLD_PARK = timedelta(hours=3)  # Tesla: check tires 'cold', after the car has been stationary for over three hours
WARM_AFTER = timedelta(minutes=30)
TIRE_BEFORE_MAX_AGE = timedelta(hours=12)
MIN_TRIP_KM = 0.1
ODO_STEP_KM = 0.161  # one odometer step is about 0.1 mile; a smaller delta is not a real reading
MIN_TRIP_SECONDS = 60.0
PARK_MERGE = timedelta(minutes=3)  # parked this long or less: same trip, not a new one
WINDOW_LOOKBACK = timedelta(hours=3)
PLACE_MOVE_M = 100.0
ASLEEP_GAP = timedelta(minutes=5)
RAW_KEEP_DAYS = 31
ALERT_LOG_KEEP_DAYS = 90
G = 9.80665
HARD_ACCEL_G = 0.30
HARD_BRAKE_G = 0.35
HARD_TURN_G = 0.40
PEDAL_MIN_PCT = 10.0
HARSH_MIN_HOLD_S = 2.0  # must stay over the threshold this long
HARSH_COOLDOWN_S = 5.0  # after one event, ignore new ones for this long
HARSH_MIN_SPEED_KMH = 10.0
HARSH_SAMPLE_HOLD_S = 1.0  # one sample counts for at most this long (gaps are not "held")
REGEN_MAX_GAP_S = 10.0
# VehicleSpeed gaps of one to two minutes still count as moving. A longer hole is
# capped so a dropped sample does not turn a stop into driving time.
MOVING_GAP_S = 120.0
ROUTE_MIN_M = 20.0  # keep a route point only after moving this far (car sends Location every 3 s at >= 20 m since ; was 50)
ROUTE_START_MAX_M = 2000.0  # the parked position before a trip is its start only if this close  # cap per-sample span when integrating regen power


@dataclass
class ProcessStats:
    trips: int = 0
    charges: int = 0
    tire_readings: int = 0
    ignored_trips: int = 0
    skipped: bool = False
    park_grace: bool = False  # a trip is parked but not closed yet (≤ PARK_MERGE)

    def add(self, other: "ProcessStats") -> None:
        self.trips += other.trips
        self.charges += other.charges
        self.tire_readings += other.tire_readings
        self.ignored_trips += other.ignored_trips
        self.park_grace = self.park_grace or other.park_grace


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: Iterable[tuple[str, str]]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, typ in cols:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def ensure_process_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS signals (
            vehicle_id TEXT NOT NULL, ts TEXT NOT NULL, name TEXT NOT NULL,
            value TEXT NOT NULL, PRIMARY KEY (vehicle_id, ts, name))"""
    )
    conn.executescript(SCHEMA_SQL)
    _ensure_columns(conn, "trips", TRIP_EXTRA_COLUMNS)
    _ensure_columns(conn, "trip_points", (("speed_kph", "REAL"), ("gear", "TEXT")))
    _ensure_columns(conn, "charges", CHARGE_EXTRA_COLUMNS)
    _ensure_columns(conn, "sentry_alarms", (("stage", "TEXT"),))
    _ensure_columns(conn, "alert_log", (("lat", "REAL"), ("lon", "REAL")))
    _ensure_columns(conn, "parked_segments", (("modes", "TEXT"), ("outside_temp", "REAL")))
    _ensure_columns(conn, "geocode_cache", (("label_en", "TEXT"),))
    from process.chargeloc import ensure_schema as _chargeloc_schema
    from process.roads import ensure_schema as _roads_schema

    _chargeloc_schema(conn)
    _roads_schema(conn)
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    ensure_process_schema(conn)
    ensure_schema(conn)
    return conn


def parse_ts(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kst_hour(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%dT%H")


def decode_value(raw: str) -> Any:
    return json.loads(raw)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "none", "unknown")
    return False


def _seconds(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


class Events:
    """Signals indexed by name for fast carry-forward lookups."""

    def __init__(self, rows: list[tuple[datetime, str, Any]]) -> None:
        self.rows = rows
        by: dict[str, tuple[list[datetime], list[Any]]] = {}
        for ts, name, value in rows:
            slot = by.get(name)
            if slot is None:
                slot = ([], [])
                by[name] = slot
            slot[0].append(ts)
            slot[1].append(value)
        self.by = by

    def __bool__(self) -> bool:
        return bool(self.rows)

    @property
    def last_ts(self) -> datetime | None:
        return self.rows[-1][0] if self.rows else None

    def at(self, name: str, t: datetime) -> Any | None:
        slot = self.by.get(name)
        if not slot:
            return None
        i = bisect_right(slot[0], t)
        return slot[1][i - 1] if i else None

    def at_ts(self, name: str, t: datetime) -> tuple[datetime, Any] | None:
        slot = self.by.get(name)
        if not slot:
            return None
        i = bisect_right(slot[0], t)
        return (slot[0][i - 1], slot[1][i - 1]) if i else None

    def latest(self, name: str) -> Any | None:
        slot = self.by.get(name)
        return slot[1][-1] if slot else None

    def series(self, name: str, start: datetime | None = None, end: datetime | None = None) -> list[tuple[datetime, Any]]:
        slot = self.by.get(name)
        if not slot:
            return []
        lo = 0 if start is None else bisect_left(slot[0], start)
        hi = len(slot[0]) if end is None else bisect_right(slot[0], end)
        return list(zip(slot[0][lo:hi], slot[1][lo:hi]))

    def nums(self, name: str, start: datetime, end: datetime, include_start: bool = True) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        if include_start:
            first = self.at_ts(name, start)
            if first is not None and first[0] < start:
                num = _as_float(first[1])
                if num is not None:
                    out.append((start, num))
        for ts, v in self.series(name, start, end):
            num = _as_float(v)
            if num is not None:
                out.append((ts, num))
        return out


def value_at(events: Any, name: str, t: datetime) -> Any | None:
    """Last known value of signal `name` at time `t` (carry-forward)."""
    if isinstance(events, Events):
        return events.at(name, t)
    last: Any | None = None
    for ts, n, v in events:
        if ts > t:
            break
        if n == name:
            last = v
    return last


def _load_events(
    conn: sqlite3.Connection, vehicle_id: str, since: datetime | None = None
) -> Events:
    params: list[Any] = [vehicle_id]
    rows: list[tuple[datetime, str, Any]] = []
    if since is None:
        raw = conn.execute(
            "SELECT ts, name, value FROM signals WHERE vehicle_id = ? ORDER BY ts, name",
            params,
        ).fetchall()
    else:
        cut = format_ts(since)
        seed = conn.execute(
            """
            SELECT s.ts, s.name, s.value FROM signals s
            JOIN (SELECT name, MAX(ts) AS ts FROM signals
                  WHERE vehicle_id = ? AND ts < ? GROUP BY name) m
              ON m.name = s.name AND m.ts = s.ts
            WHERE s.vehicle_id = ?
            """,
            (vehicle_id, cut, vehicle_id),
        ).fetchall()
        recent = conn.execute(
            "SELECT ts, name, value FROM signals WHERE vehicle_id = ? AND ts >= ? ORDER BY ts, name",
            (vehicle_id, cut),
        ).fetchall()
        raw = sorted(seed, key=lambda r: (r[0], r[1])) + list(recent)
    for ts_s, name, val in raw:
        try:
            rows.append((parse_ts(ts_s), name, decode_value(val)))
        except (ValueError, TypeError):
            continue
    rows.sort(key=lambda r: (r[0], r[1]))
    return Events(rows)


# ---------------------------------------------------------------- segmentation

def _gear_series(ev: Events) -> list[tuple[datetime, str]]:
    return [(ts, v) for ts, v in ev.series("Gear") if v in ("P",) + DRIVE_GEARS]


def _gear_walk(ev: Events) -> tuple[list[tuple[datetime, datetime]], datetime | None, datetime | None]:
    """Closed gear spans, plus the open span's start and the time it entered P (if it has).

    A return to D/R/N within PARK_MERGE of entering P continues the same span.
    Unlock and relock are not consulted: only gear and the park length matter.
    """
    closed: list[tuple[datetime, datetime]] = []
    gear: Any | None = None
    trip_start: datetime | None = None
    park_at: datetime | None = None
    for ts, value in _gear_series(ev):
        prev = gear
        gear = value
        if gear == "P":
            if prev != "P" and trip_start is not None:
                park_at = ts
            continue
        if trip_start is None:
            trip_start = ts
        elif park_at is not None:
            if ts - park_at > PARK_MERGE:
                closed.append((trip_start, park_at))
                trip_start = ts
            park_at = None
    return closed, trip_start, park_at


def _keep_trips(ev: Events, spans: list[tuple[datetime, datetime]]) -> tuple[list[tuple[datetime, datetime]], int]:
    kept: list[tuple[datetime, datetime]] = []
    ignored = 0
    for start, end in spans:
        odo_s = _as_float(ev.at("Odometer", start))
        odo_e = _as_float(ev.at("Odometer", end))
        km = (odo_e - odo_s) if odo_s is not None and odo_e is not None else 0.0
        if km < MIN_TRIP_KM and _seconds(start, end) < MIN_TRIP_SECONDS:
            ignored += 1
            continue
        kept.append((start, end))
    return kept, ignored


def _find_trips(ev: Events, now: datetime | None = None) -> tuple[list[tuple[datetime, datetime]], int]:
    """Finished trips. The current park is closed only after it has lasted longer than PARK_MERGE,
    so a brief stop is not its own trip and a trip_end is not emitted while the driver may return.
    """
    closed, trip_start, park_at = _gear_walk(ev)
    now = now or datetime.now(timezone.utc)
    if trip_start is not None and park_at is not None and now - park_at > PARK_MERGE:
        closed.append((trip_start, park_at))
    return _keep_trips(ev, closed)


def open_trip_start(ev: Events, now: datetime | None = None) -> datetime | None:
    """Start of the trip still open: in gear, or parked for PARK_MERGE or less. Else None."""
    _closed, trip_start, park_at = _gear_walk(ev)
    if trip_start is None:
        return None
    if park_at is None:
        return trip_start
    now = now or datetime.now(timezone.utc)
    if now - park_at <= PARK_MERGE:
        return trip_start
    return None


# The car reports two charge signals. `DetailedChargeState` is the user-facing state
# (Disconnected/NoPower/Starting/Charging/Complete/Stopped). `ChargeState` is the charger's
# internal step on current firmware (Idle/ClearFaults/WaitForLineVoltage/QualifyLineConfig/
# Enable/...), where `Enable` means power is flowing; the simulator and older data use
# `Charging`. DetailedChargeState decides whenever it is known; ChargeState is a fallback.
RAW_CHARGING_STATES = ("Charging", "Enable")
DETAILED_CHARGE_STATES = ("Disconnected", "NoPower", "Starting", "Charging", "Complete", "Stopped")
# One Korean wording for DetailedChargeState across the app (web CHARGE_KO uses the same words).
# Five words on every screen: 충전 중, 연결됨, 충전 완료, 연결 안 됨, 미확인.
# Plugged but not charging (NoPower, Starting, Stopped) is 연결됨. Missing is 미확인.
CHARGE_STATE_KO = {"Disconnected": "연결 안 됨", "NoPower": "연결됨", "Starting": "연결됨",
                   "Charging": "충전 중", "Complete": "충전 완료", "Stopped": "연결됨"}


def _known_detailed(detailed: Any) -> bool:
    return isinstance(detailed, str) and detailed not in ("", "Unknown")


def charge_status(detailed: Any, raw: Any) -> str | None:
    """User-facing charge state: DetailedChargeState, else ChargeState mapped onto that set."""
    if _known_detailed(detailed):
        return detailed
    if raw in RAW_CHARGING_STATES:
        return "Charging"
    if raw in DETAILED_CHARGE_STATES:
        return raw
    return None


def is_charging(detailed: Any, raw: Any) -> bool:
    """True while charging: DetailedChargeState == Charging; if it is unknown,
    ChargeState Charging or Enable."""
    if _known_detailed(detailed):
        return detailed == "Charging"
    return raw in RAW_CHARGING_STATES


def is_plugged(detailed: Any, raw: Any) -> bool:
    """A cable is connected (DetailedChargeState other than Disconnected), or charging."""
    if _known_detailed(detailed):
        return detailed != "Disconnected"
    return raw in RAW_CHARGING_STATES


def _charge_steps(ev: Events) -> list[tuple[datetime, bool]]:
    stamps = sorted({ts for n in ("DetailedChargeState", "ChargeState") for ts, _ in ev.series(n)})
    return [(ts, is_charging(ev.at("DetailedChargeState", ts), ev.at("ChargeState", ts))) for ts in stamps]


def _port_still_open(ev: Events, start: datetime, end: datetime) -> bool:
    """The port stays open on its last known value until something breaks that.

    ChargePortDoorOpen is sent only when it changes, so a true with no later
    sample is still open. A false, an unknown value, or no history is not open.
    A drive, a trip, movement, or five minutes with no signal also clears it;
    those are checked separately and must be false before a pause can join.
    """
    if end <= start:
        return False
    at = ev.at_ts("ChargePortDoorOpen", start)
    if at is None or not _true_flag(at[1]):
        return False
    for ts, value in ev.series("ChargePortDoorOpen", start, end):
        if ts <= start:
            continue
        if not _true_flag(value):
            return False
    return True


def _asleep_or_offline(ev: Events, start: datetime, end: datetime) -> bool:
    """Five minutes with no signal at all means the car was asleep or offline."""
    if end - start >= ASLEEP_GAP and not ev.rows:
        return True
    mids = [ts for ts, _name, _value in ev.rows if start < ts < end]
    bounds = [start, *mids, end]
    return any(b - a >= ASLEEP_GAP for a, b in zip(bounds, bounds[1:]))


def _moved_place(ev: Events, start: datetime, end: datetime) -> bool:
    points: list[tuple[float, float]] = []
    base = _latlon(ev.at("Location", start))
    if base is not None:
        points.append(base)
    for ts, value in ev.series("Location", start, end):
        if not (start < ts < end):
            continue
        point = _latlon(value)
        if point is None:
            return True
        points.append(point)
    for left, right in zip(points, points[1:]):
        if distance_m(left, right) > PLACE_MOVE_M:
            return True
    return False


def _trip_between(conn: sqlite3.Connection | None, vehicle_id: str | None, start: datetime, end: datetime) -> bool:
    if conn is None or not vehicle_id:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM trips WHERE vehicle_id = ? AND start_ts < ? AND end_ts > ? LIMIT 1",
            (vehicle_id, format_ts(end), format_ts(start)),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _event_trip_between(
    trips: list[tuple[datetime, datetime]] | None, start: datetime, end: datetime,
) -> bool:
    if not trips:
        return False
    return any(s < end and e > start for s, e in trips)


def _gap_blocked(
    ev: Events, start: datetime, end: datetime,
    conn: sqlite3.Connection | None = None, vehicle_id: str | None = None,
    trips: list[tuple[datetime, datetime]] | None = None,
) -> bool:
    """A drive, a trip, a place change, or five minutes offline: the port state breaks."""
    for ts, gear in ev.series("Gear", start, end):
        if start < ts < end and gear in ("D", "R"):
            return True
    for ts, speed in ev.nums("VehicleSpeed", start, end):
        if start < ts < end and speed > 0:
            return True
    if _moved_place(ev, start, end):
        return True
    if _asleep_or_offline(ev, start, end):
        return True
    if _event_trip_between(trips, start, end):
        return True
    return _trip_between(conn, vehicle_id, start, end)


def _ended_complete(ev: Events, stopped: datetime) -> bool:
    """Charging stopped because the car said Complete (the target was reached).

    Stopped, and the other plugged-but-waiting states, are not Complete. Those
    pauses still join while the port stays open.
    """
    return charge_status(
        ev.at("DetailedChargeState", stopped),
        ev.at("ChargeState", stopped),
    ) == "Complete"


def _gap_is_one_session(
    ev: Events, stopped: datetime, resumed: datetime,
    conn: sqlite3.Connection | None = None, vehicle_id: str | None = None,
    trips: list[tuple[datetime, datetime]] | None = None,
) -> bool:
    """Join only the next bout, and only while the port's last value is still open.

    A bout that ended Complete is already finished. The next bout on the same
    cable is a new charge.
    """
    if _ended_complete(ev, stopped):
        return False
    if not _port_still_open(ev, stopped, resumed):
        return False
    return not _gap_blocked(ev, stopped, resumed, conn, vehicle_id, trips)


def _session_still_open(
    ev: Events, stopped: datetime,
    conn: sqlite3.Connection | None = None, vehicle_id: str | None = None,
    trips: list[tuple[datetime, datetime]] | None = None,
) -> bool:
    """The port is still open and nothing has broken that, so the session is not final.

    Complete is final at the moment charging stopped, even while the port stays open.
    """
    if _ended_complete(ev, stopped):
        return False
    end = ev.last_ts or stopped
    if end <= stopped:
        return False
    if not _port_still_open(ev, stopped, end):
        return False
    return not _gap_blocked(ev, stopped, end, conn, vehicle_id, trips)


def _atomic_bouts(ev: Events) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """Charging periods. The end is the first sample that is no longer charging.

    A later Complete, Disconnected, Idle, or port sample does not move that end.
    """
    bouts: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    stopped: datetime | None = None
    for ts, on in _charge_steps(ev):
        if on and start is None:
            start = ts
            stopped = None
        elif on and stopped is not None:
            bouts.append((start, stopped))
            start = ts
            stopped = None
        elif not on and start is not None and stopped is None:
            stopped = ts
    if start is not None and stopped is not None:
        bouts.append((start, stopped))
        return bouts, None
    return bouts, start


def _charge_sessions(
    ev: Events, conn: sqlite3.Connection | None = None, vehicle_id: str | None = None,
    trips: list[tuple[datetime, datetime]] | None = None,
) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """Closed (start, end) spans, and the start of a session that is still open.

    Only two adjacent bouts can join, and only when the port's last signal is
    still open, the car stayed put, and the gap has no trip, drive, or five
    minutes offline. A bout that ended Complete does not join and does not stay
    open. A joined span cannot reach across a later bout. The end is the moment
    charging stopped, not a later signal.
    """
    if trips is None:
        trips, _ignored = _find_trips(ev)
    bouts, trailing = _atomic_bouts(ev)
    spans: list[tuple[datetime, datetime]] = []
    for start, end in bouts:
        if spans and _gap_is_one_session(ev, spans[-1][1], start, conn, vehicle_id, trips):
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    open_start: datetime | None = None
    if trailing is not None:
        if spans and _gap_is_one_session(ev, spans[-1][1], trailing, conn, vehicle_id, trips):
            open_start = spans[-1][0]
            spans.pop()
        else:
            open_start = trailing
    elif spans and _session_still_open(ev, spans[-1][1], conn, vehicle_id, trips):
        open_start = spans[-1][0]
        spans.pop()
    return spans, open_start


def _find_charges(ev: Events) -> list[tuple[datetime, datetime]]:
    return _charge_sessions(ev)[0]


def open_charge_start(ev: Events) -> datetime | None:
    """Start of the charge in progress (still charging at the latest step), else None."""
    start: datetime | None = None
    for ts, on in _charge_steps(ev):
        if on and start is None:
            start = ts
        elif not on:
            start = None
    return start


def _kwh_used(ev: Events, start: datetime, end: datetime) -> float | None:
    life_s = _as_float(ev.at("LifetimeEnergyUsed", start))
    life_e = _as_float(ev.at("LifetimeEnergyUsed", end))
    if life_s is not None and life_e is not None:
        return life_e - life_s
    rem_s = _as_float(ev.at("EnergyRemaining", start))
    rem_e = _as_float(ev.at("EnergyRemaining", end))
    if rem_s is not None and rem_e is not None:
        return rem_s - rem_e
    return None


def _delta(ev: Events, name: str, start: datetime, end: datetime) -> float | None:
    a = _as_float(ev.at(name, start))
    b = _as_float(ev.at(name, end))
    if a is None or b is None:
        return None
    return max(0.0, b - a)


def _time_weighted(ev: Events, name: str, start: datetime, end: datetime) -> float | None:
    points = ev.nums(name, start, end)
    duration = _seconds(start, end)
    if not points:
        return None
    if duration <= 0:
        return points[0][1]
    # A value exactly at start replaces the carried-forward one.
    dedup: list[tuple[datetime, float]] = []
    for ts, v in points:
        if dedup and dedup[-1][0] == ts:
            dedup[-1] = (ts, v)
        else:
            dedup.append((ts, v))
    weighted = 0.0
    covered = 0.0
    for i, (ts, val) in enumerate(dedup):
        nxt = dedup[i + 1][0] if i + 1 < len(dedup) else end
        span = max(0.0, _seconds(ts, nxt))
        weighted += val * span
        covered += span
    if covered <= 0:
        return dedup[-1][1]
    return weighted / covered


def _time_weighted_temp(ev: Events, start: datetime, end: datetime) -> float | None:
    return _time_weighted(ev, "OutsideTemp", start, end)


def _max_num(ev: Events, name: str, start: datetime, end: datetime) -> float | None:
    vals = [v for _ts, v in ev.nums(name, start, end)]
    return max(vals) if vals else None


def _min_num(ev: Events, name: str, start: datetime, end: datetime) -> float | None:
    vals = [v for _ts, v in ev.nums(name, start, end)]
    return min(vals) if vals else None


def _any_true(ev: Events, name: str, start: datetime, end: datetime) -> int | None:
    vals = [ev.at(name, start)] + [v for _t, v in ev.series(name, start, end)]
    known = [v for v in vals if v is not None]
    if not known:
        return None
    return 1 if any(_truthy(v) for v in known) else 0


def moving_seconds(samples: list[tuple[datetime, float]], end: datetime) -> float | None:
    """Seconds the car was moving (VehicleSpeed > 0).

    A sample holds until the next one. Gaps of 1–2 minutes between moving
    samples still count. A longer hole is capped at MOVING_GAP_S. When the
    next sample is stopped, only half of that gap counts: the stop happened
    somewhere in between.
    """
    if not samples:
        return None
    ordered = sorted(samples, key=lambda item: item[0])
    total = 0.0
    for i, (ts, speed) in enumerate(ordered):
        if speed <= 0:
            continue
        if i + 1 < len(ordered):
            nxt, nxt_speed = ordered[i + 1]
        else:
            nxt, nxt_speed = end, speed
        span = min(MOVING_GAP_S, max(0.0, _seconds(ts, nxt)))
        if nxt_speed <= 0:
            span *= 0.5
        total += span
    return total


def series_avg_max(
    samples: list[tuple[datetime, float]], end: datetime | None = None,
) -> tuple[float | None, float | None]:
    """Time-weighted average and maximum of one series. The average is never above the max."""
    vals = [(ts, value) for ts, value in samples if value is not None]
    if not vals:
        return None, None
    peak = max(value for _ts, value in vals)
    weighted = 0.0
    covered = 0.0
    for i, (ts, value) in enumerate(vals):
        if i + 1 < len(vals):
            nxt = vals[i + 1][0]
        elif end is not None and end > ts:
            nxt = end
        else:
            continue
        span = max(0.0, _seconds(ts, nxt))
        weighted += value * span
        covered += span
    avg = (weighted / covered) if covered > 0 else vals[-1][1]
    if avg > peak:
        avg = peak
    return avg, peak


def _max_power_kw(samples: list[tuple[datetime, float]]) -> float | None:
    if len(samples) < 2:
        return None
    best: float | None = None
    for (t0, e0), (t1, e1) in zip(samples, samples[1:]):
        gap = _seconds(t0, t1)
        if gap <= 0:
            continue
        power = (e1 - e0) * 3600.0 / gap
        if best is None or power > best:
            best = power
    return best


def power_series(ev: Events, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Pack power in kW (consumption positive, regen negative) from PackVoltage x PackCurrent."""
    stamps = sorted(
        {ts for ts, _ in ev.series("PackCurrent", start, end)}
        | {ts for ts, _ in ev.series("PackVoltage", start, end)}
    )
    out: list[tuple[datetime, float]] = []
    for ts in stamps:
        v = _as_float(ev.at("PackVoltage", ts))
        i = _as_float(ev.at("PackCurrent", ts))
        if v is None or i is None:
            continue
        out.append((ts, v * i / 1000.0))
    if out:
        # Sign convention differs by firmware; over a drive, consumption dominates.
        if sum(p for _t, p in out) < 0:
            out = [(t, -p) for t, p in out]
    return out


def _accel_samples(ev: Events, name: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Acceleration samples in g. The car sends m/s^2."""
    return [(ts, v / G) for ts, v in ev.nums(name, start, end, include_start=False)]


def _episode_list(
    samples: list[tuple[datetime, float]], hit: Callable[[datetime, float], bool]
) -> list[tuple[datetime, float]]:
    """Runs of `hit` samples that last >= HARSH_MIN_HOLD_S, with a cooldown: (start, peak |g|).

    A sample holds until the next sample, but never longer than HARSH_SAMPLE_HOLD_S,
    so a single 1 Hz spike (1 s) does not count and gaps are not stretched.
    """
    found: list[tuple[datetime, float]] = []
    run_start: datetime | None = None
    run_end: datetime | None = None
    run_peak = 0.0
    last_event_end: datetime | None = None

    def close() -> None:
        nonlocal last_event_end
        if run_start is None or run_end is None:
            return
        if _seconds(run_start, run_end) < HARSH_MIN_HOLD_S:
            return
        if last_event_end is not None and _seconds(last_event_end, run_start) < HARSH_COOLDOWN_S:
            return
        found.append((run_start, run_peak))
        last_event_end = run_end

    for i, (ts, v) in enumerate(samples):
        nxt = samples[i + 1][0] if i + 1 < len(samples) else None
        hold = HARSH_SAMPLE_HOLD_S if nxt is None else min(HARSH_SAMPLE_HOLD_S, max(0.0, _seconds(ts, nxt)))
        if hit(ts, v):
            if run_start is None or (run_end is not None and run_end < ts):
                close()
                run_start = ts
                run_peak = 0.0
            run_peak = max(run_peak, abs(v))
            run_end = ts + timedelta(seconds=hold)
        else:
            close()
            run_start = run_end = None
    close()
    return found


def _episodes(samples: list[tuple[datetime, float]], hit: Callable[[datetime, float], bool]) -> int:
    return len(_episode_list(samples, hit))


def harsh_events(ev: Events, start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]] | None]:
    """Accel / brake from adjacent VehicleSpeed samples. Lateral acceleration is not used.

    The value stored with each event is the peak |km/h per second|, not g.
    """
    from process.speed_events import speed_events

    samples = ev.nums("VehicleSpeed", start, end, include_start=False)
    return speed_events(samples)


def _harsh_counts(ev: Events, start: datetime, end: datetime) -> tuple[int | None, int | None, int | None]:
    found = harsh_events(ev, start, end)
    counts = tuple(None if found[k] is None else len(found[k]) for k in ("accel", "brake", "turn"))
    return counts  # type: ignore[return-value]


def regen_from_power(power: list[tuple[datetime, float]], end: datetime | None = None) -> float | None:
    """Regen kWh estimated from pack power (regen negative), each sample held up to REGEN_MAX_GAP_S."""
    if not power:
        return None
    total = 0.0
    for i, (ts, p) in enumerate(power):
        nxt = power[i + 1][0] if i + 1 < len(power) else end
        if nxt is None:
            continue
        span = min(REGEN_MAX_GAP_S, max(0.0, _seconds(ts, nxt)))
        if p < 0:
            total += -p * span / 3600.0
    return total


def _tpms_candidates(ev: Events) -> list[tuple[datetime, float, float, float, float]]:
    stamps = sorted({ts for n in TPMS_NAMES for ts, _ in ev.series(n)})
    out: list[tuple[datetime, float, float, float, float]] = []
    for ts in stamps:
        vals = [_as_float(ev.at(n, ts)) for n in TPMS_NAMES]
        if all(v is not None for v in vals):
            out.append((ts, vals[0], vals[1], vals[2], vals[3]))  # type: ignore[arg-type]
    return out


def _tire_json(c: tuple[datetime, float, float, float, float] | None) -> str | None:
    if c is None:
        return None
    return json.dumps(
        {"ts": format_ts(c[0]), "fl": c[1], "fr": c[2], "rl": c[3], "rr": c[4]},
        separators=(",", ":"),
    )


def _tires_around(
    candidates: list[tuple[datetime, float, float, float, float]],
    start: datetime,
    end: datetime,
) -> tuple[str | None, str | None]:
    before = [c for c in candidates if start - TIRE_BEFORE_MAX_AGE <= c[0] <= start]
    after = [c for c in candidates if start < c[0] <= end + WARM_AFTER]
    if not before:
        # first reading right after shifting out of P is still "before driving"
        early = [c for c in candidates if start <= c[0] <= start + timedelta(minutes=2)]
        before = early[:1]
    return _tire_json(before[-1] if before else None), _tire_json(after[-1] if after else None)


def speed_distance_km(ev: Events, start: datetime, end: datetime) -> float | None:
    """Trapezoid of VehicleSpeed (km/h) over the trip. A long gap is capped.

    One sample is not enough. The speed at the end is treated as stopped when
    the last sample is earlier than the end.
    """
    samples = ev.nums("VehicleSpeed", start, end, include_start=False)
    if len(samples) < 2:
        return None
    ordered = sorted(samples, key=lambda item: item[0])
    if end > ordered[-1][0]:
        ordered.append((end, 0.0))
    total_km = 0.0
    spanned = False
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:]):
        gap = _seconds(t0, t1)
        if gap <= 0:
            continue
        gap = min(MOVING_GAP_S, gap)
        total_km += (max(0.0, v0) + max(0.0, v1)) / 2.0 * (gap / 3600.0)
        spanned = True
    if not spanned or total_km <= 0:
        return None
    return total_km


def gps_distance_km(points: list[tuple[float, float, datetime]]) -> float | None:
    """Path length of the trip points. Two points are enough."""
    if len(points) < 2:
        return None
    total_m = 0.0
    for (lat0, lon0, _t0), (lat1, lon1, _t1) in zip(points, points[1:]):
        total_m += distance_m((lat0, lon0), (lat1, lon1))
    if total_m <= 0:
        return None
    return total_m / 1000.0


def trip_distance_km(
    ev: Events, start: datetime, end: datetime,
) -> tuple[float, str]:
    """Odometer when it moved a full step; otherwise speed, then the GPS path."""
    odo_s = _as_float(ev.at("Odometer", start))
    odo_e = _as_float(ev.at("Odometer", end))
    odo_km = (odo_e - odo_s) if odo_s is not None and odo_e is not None else None
    if odo_km is not None and odo_km >= ODO_STEP_KM:
        return odo_km, "odometer"
    by_speed = speed_distance_km(ev, start, end)
    if by_speed is not None:
        return by_speed, "speed"
    by_gps = gps_distance_km(route_points(ev, start, end))
    if by_gps is not None:
        return by_gps, "gps"
    return (odo_km if odo_km is not None else 0.0), "odometer"


def _fan_while_hvac(ev: Events, start: datetime, end: datetime) -> tuple[float | None, float | None]:
    """Fan max and average, counting speed only while HvacPower is On or Precondition.

    With no HvacPower at all, the raw fan speed is used. A speed received before
    the window or while power is off is drawn again when power turns on. The off
    stretch itself stays 0. Version 25 recomputes stored rows on the next run.
    """
    from web.graphseries import _hvac_setpoint_on, flat_average, gate_fan, level_spans, series_high

    power = ev.series("HvacPower", start, end)
    hit = ev.at_ts("HvacPower", start - timedelta(microseconds=1))
    prior = hit if hit is not None and hit[0] < start else None
    if not power and prior is None:
        return _max_num(ev, "HvacFanSpeed", start, end), _time_weighted(ev, "HvacFanSpeed", start, end)
    spans = level_spans(
        power, start, end, prior,
        lambda value: 1.0 if _hvac_setpoint_on(value) else 0.0,
    )
    on_spans = [(a, b) for a, b, _level in spans]
    fan_rows = []
    for ts, value in ev.nums("HvacFanSpeed", start, end, include_start=False):
        fan_rows.append((ts, float(value)))
    fan_hit = ev.at_ts("HvacFanSpeed", start - timedelta(microseconds=1))
    fan_prior = None
    if fan_hit is not None and fan_hit[0] < start:
        try:
            fan_prior = (fan_hit[0], float(fan_hit[1]))
        except (TypeError, ValueError):
            fan_prior = None
    segs = gate_fan(fan_rows, fan_prior, on_spans, start, end, True)
    return series_high(segs), flat_average(segs, end)


def _trip_row(
    vehicle_id: str, ev: Events, start: datetime, end: datetime,
    candidates: list[tuple[datetime, float, float, float, float]],
) -> dict[str, Any]:
    odo_s = _as_float(ev.at("Odometer", start))
    odo_e = _as_float(ev.at("Odometer", end))
    km, km_source = trip_distance_km(ev, start, end)
    km = round(km, 3)
    kwh = _kwh_used(ev, start, end)
    wh_per_km = (kwh * 1000.0 / km) if kwh is not None and km >= MIN_TRIP_KM else None
    km_per_kwh = (km / kwh) if kwh is not None and kwh > 0.01 and km >= MIN_TRIP_KM else None
    regen = _delta(ev, "LifetimeEnergyGainedRegen", start, end)
    duration = _seconds(start, end)
    power = power_series(ev, start, end)
    regen_estimated: int | None = 0 if regen is not None else None
    if regen is None:
        regen = regen_from_power(power, end)
        regen_estimated = 1 if regen is not None else None
    max_power = max((p for _t, p in power), default=None)
    max_regen = max((-p for _t, p in power if p < 0), default=None)
    speed_samples = ev.nums("VehicleSpeed", start, end, include_start=False)
    speeds = [v for _t, v in speed_samples]
    moving = moving_seconds(speed_samples, end)
    hard_accel, hard_brake, hard_turn = _harsh_counts(ev, start, end)
    tire_before, tire_after = _tires_around(candidates, start, end)
    hvac = ev.series("HvacPower", start, end)
    hvac_vals = [ev.at("HvacPower", start)] + [v for _t, v in hvac]
    hvac_known = [v for v in hvac_vals if v is not None]
    seat_l = _max_num(ev, "SeatHeaterLeft", start, end)
    seat_r = _max_num(ev, "SeatHeaterRight", start, end)
    wheel = _max_num(ev, "HvacSteeringWheelHeatLevel", start, end)
    fan_max, fan_avg = _fan_while_hvac(ev, start, end)
    return {
        "vehicle_id": vehicle_id,
        "start_ts": format_ts(start),
        "end_ts": format_ts(end),
        "km": km,
        "km_source": km_source,
        "kwh": kwh,
        "wh_per_km": wh_per_km,
        "start_soc": _as_float(ev.at("BatteryLevel", start)),
        "end_soc": _as_float(ev.at("BatteryLevel", end)),
        "avg_outside_temp": _time_weighted_temp(ev, start, end),
        "duration_s": duration,
        "km_per_kwh": km_per_kwh,
        "start_odometer": odo_s,
        "end_odometer": odo_e,
        "start_lifetime_kwh": _as_float(ev.at("LifetimeEnergyUsed", start)),
        "end_lifetime_kwh": _as_float(ev.at("LifetimeEnergyUsed", end)),
        "regen_kwh": regen,
        "regen_ratio": (regen / kwh) if regen is not None and kwh and kwh > 0.01 else None,
        "regen_estimated": regen_estimated,
        "max_power_kw": max_power,
        "max_regen_kw": max_regen,
        "start_range_km": _as_float(ev.at("RatedRange", start)),
        "end_range_km": _as_float(ev.at("RatedRange", end)),
        "start_est_range_km": _as_float(ev.at("EstBatteryRange", start)),
        "end_est_range_km": _as_float(ev.at("EstBatteryRange", end)),
        "avg_speed": (km / (moving / 3600.0)) if moving and moving > 0 and km >= MIN_TRIP_KM else None,
        "max_speed": max(speeds) if speeds else None,
        "avg_inside_temp": _time_weighted(ev, "InsideTemp", start, end),
        "battery_temp_min": _min_num(ev, "ModuleTempMin", start, end),
        "battery_temp_max": _max_num(ev, "ModuleTempMax", start, end),
        "battery_heater_on": _any_true(ev, "BatteryHeaterOn", start, end),
        "hvac_on": (1 if any(v in ("On", "Precondition") for v in hvac_known) else 0) if hvac_known else None,
        "seat_heat_left_max": int(seat_l) if seat_l is not None else None,
        "seat_heat_right_max": int(seat_r) if seat_r is not None else None,
        "wheel_heat_max": int(wheel) if wheel is not None else None,
        "fan_max": int(fan_max) if fan_max is not None else None,
        "fan_avg": fan_avg,
        "hard_accel": hard_accel,
        "hard_brake": hard_brake,
        "hard_turn": hard_turn,
        "tire_before": tire_before,
        "tire_after": tire_after,
        "start_gear": next((v for t, v in _gear_series(ev) if t == start), None),
    }


def _latlon(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lat, lon = _as_float(value.get("lat")), _as_float(value.get("lon"))
        if lat is None or lon is None:
            lat, lon = _as_float(value.get("latitude")), _as_float(value.get("longitude"))
        if lat is not None and lon is not None:
            return lat, lon
    return None


def route_points(ev: Events, start: datetime, end: datetime) -> list[tuple[float, float, datetime]]:
    """In-trip Location points (lat, lon, ts), thinned to ROUTE_MIN_M.

    The last position known at the start (where the car was parked) is the first point when it
    is close to the first in-trip point.
    """
    pts: list[tuple[float, float, datetime]] = []
    for ts, v in ev.series("Location", start, end):
        ll = _latlon(v)
        if ll is not None:
            pts.append((ll[0], ll[1], ts))
    before = ev.at_ts("Location", start)
    if before is not None and before[0] < start:
        ll = _latlon(before[1])
        if ll is not None and (not pts or distance_m(ll, pts[0]) <= ROUTE_START_MAX_M):
            pts.insert(0, (ll[0], ll[1], start))
    return thin(pts, ROUTE_MIN_M) if len(pts) > 1 else pts


def trip_place_fields(ev: Events, start: datetime, end: datetime,
                      pts: list[tuple[float, float, datetime]]) -> dict[str, Any]:
    first = pts[0] if pts else None
    last = pts[-1] if pts else None
    if first is None:
        ll = _latlon(ev.at("Location", start))
        first = (ll[0], ll[1], start) if ll else None
    if last is None:
        ll = _latlon(ev.at("Location", end))
        last = (ll[0], ll[1], end) if ll else None
    out = {
        "start_lat": first[0] if first else None, "start_lon": first[1] if first else None,
        "end_lat": last[0] if last else None, "end_lon": last[1] if last else None,
    }
    return out


def _store_route(
    conn: sqlite3.Connection, vehicle_id: str, start: datetime,
    pts: list[tuple[float, float, datetime]], ev: Events | None = None,
) -> None:
    """Replace a trip's points. Never clears an existing route when raw signals are gone.

    speed_kph and gear are copied from the signals at each point when those signals
    exist. Autopilot is not stored: no autopilot field is ingested. Old rows stay
    NULL and the map hides what was not stored.
    """
    if not pts:
        return
    key = format_ts(start)
    conn.execute("DELETE FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ?", (vehicle_id, key))
    rows = []
    for i, (lat, lon, ts) in enumerate(pts):
        speed = gear = None
        if ev is not None:
            speed = _as_float(ev.at("VehicleSpeed", ts))
            g = ev.at("Gear", ts)
            if g in ("P", "R", "N", "D"):
                gear = g
        rows.append((vehicle_id, key, i, format_ts(ts), round(lat, 6), round(lon, 6), speed, gear))
    conn.executemany(
        "INSERT INTO trip_points (vehicle_id, trip_start_ts, seq, ts, lat, lon, speed_kph, gear) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _store_events(
    conn: sqlite3.Connection, vehicle_id: str, start: datetime,
    found: dict[str, list[tuple[datetime, float]] | None], pts: list[tuple[float, float, datetime]],
) -> None:
    """Replace a trip's harsh events, each placed at its time on the route (approximate).

    Kept as they are when the raw acceleration samples are gone.
    """
    if all(v is None for v in found.values()):
        return
    key = format_ts(start)
    route = pts or [
        (r[0], r[1], parse_ts(r[2])) for r in conn.execute(
            "SELECT lat, lon, ts FROM trip_points WHERE vehicle_id = ? AND trip_start_ts = ? ORDER BY seq",
            (vehicle_id, key),
        ).fetchall()
    ]
    rows = []
    for kind, items in found.items():
        for ts, g in items or []:
            ll = locate(route, ts)
            rows.append((vehicle_id, key, format_ts(ts), kind, round(g, 3),
                         round(ll[0], 6) if ll else None, round(ll[1], 6) if ll else None))
    conn.execute("DELETE FROM trip_events WHERE vehicle_id = ? AND trip_start_ts = ?", (vehicle_id, key))
    conn.executemany(
        "INSERT OR REPLACE INTO trip_events (vehicle_id, trip_start_ts, ts, kind, g, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


EST_SETTLE = timedelta(minutes=5)  # first full-time estimate taken after power has settled


def sentry_on(value: Any) -> bool:
    """SentryMode is on unless Off/Unknown (Idle, Armed, Aware, Panic, Quiet are on)."""
    if isinstance(value, bool):
        return value
    return value is not None and str(value) not in ("Off", "Unknown", "")


def sentry_minutes(ev: Events, start: datetime, end: datetime) -> float:
    """Minutes SentryMode was on between start and end (carry-forward)."""
    if end <= start:
        return 0.0
    pts: list[tuple[datetime, Any]] = []
    first = ev.at_ts("SentryMode", start)
    if first is not None:
        pts.append((start, first[1]))
    pts += [(t, v) for t, v in ev.series("SentryMode", start, end) if t > start]
    total = 0.0
    for i, (t, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        if sentry_on(v):
            total += max(0.0, _seconds(t, min(nxt, end)))
    return round(total / 60.0, 1)


def estimated_full(ev: Events, start: datetime, end: datetime) -> datetime | None:
    """Full-charge time as estimated at the start: the first TimeToFullCharge sample
    at least EST_SETTLE after the start (power has ramped up), else the first one."""
    samples = [(t, v) for t, v in ev.nums("TimeToFullCharge", start, end, include_start=False) if v > 0]
    if not samples:
        return None
    settled = [x for x in samples if x[0] >= start + EST_SETTLE]
    t, h = (settled or samples)[0]
    return t + timedelta(hours=h)


# The car often posts the last ACChargingEnergyIn / DCChargingEnergyIn sample
# 5–24 s after DetailedChargeState leaves Charging. The session still ends at
# that state change; only the counter read looks forward, and never into the
# next charge.
CHARGE_ENERGY_GRACE = timedelta(seconds=60)


def _energy_limit(end: datetime, next_start: datetime | None) -> datetime:
    limit = end + CHARGE_ENERGY_GRACE
    if next_start is not None and next_start < limit:
        return end if next_start <= end else next_start - timedelta(microseconds=1)
    return limit


def _counter_rise(ev: Events, name: str, start: datetime, limit: datetime) -> float | None:
    """Sum of upward steps. A reset to a lower number starts again and is not the end value."""
    samples = ev.nums(name, start, limit, include_start=True)
    if not samples:
        return None
    total = 0.0
    prev = samples[0][1]
    for _ts, value in samples[1:]:
        if value + 1e-6 < prev:
            prev = value
            continue
        total += value - prev
        prev = value
    return total


def _false_flag(value: Any) -> bool:
    if value is False or value == 0:
        return True
    return isinstance(value, str) and value.strip().lower() in ("false", "0", "no")


def _true_flag(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")


# A fast charger is decided from signals only. Place and "home" are not used.
DC_POWER_MIN_KW = 15.0
DC_VOLTAGE_MIN = 300.0


def _fast_type_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    name = value[len("FastCharger"):] if value.startswith("FastCharger") else value
    if name in ("Unknown", "SNA", ""):
        return None
    return name


def _dc_certain(ev: Events, start: datetime, end: datetime) -> bool:
    """A fast-charge signal sampled during this session, not a value carried from earlier."""
    if any(_true_flag(v) for _t, v in ev.series("FastChargerPresent", start, end)):
        return True
    if any(_fast_type_name(v) for _t, v in ev.series("FastChargerType", start, end)):
        return True
    power = ev.nums("DCChargingPower", start, end, include_start=False)
    if any(v >= DC_POWER_MIN_KW for _t, v in power):
        return True
    volts = ev.nums("ChargerVoltage", start, end, include_start=False)
    return any(v >= DC_VOLTAGE_MIN for _t, v in volts)


def _ac_wall_evidence(ev: Events, start: datetime, end: datetime) -> bool:
    """Wall AC: about 200 V, AC power, current, and FastChargerPresent false."""
    power = any(v > 0.2 for _t, v in ev.nums("ACChargingPower", start, end, include_start=False))
    amps = any(v > 0 for _t, v in ev.nums("ChargeAmps", start, end, include_start=False))
    volts = any(180 <= v < 300 for _t, v in ev.nums("ChargerVoltage", start, end, include_start=False))
    if not (power and amps and volts):
        return False
    flags = [v for _t, v in ev.series("FastChargerPresent", start, end)]
    at = ev.at("FastChargerPresent", start)
    if at is not None:
        flags.append(at)
    if not flags or any(_true_flag(v) for v in flags):
        return False
    return any(_false_flag(v) for v in flags)


def _energy_counter_at(ev: Events, name: str, end: datetime, next_start: datetime | None) -> float | None:
    """Last counter at `end`, or a later sample within CHARGE_ENERGY_GRACE before the next charge."""
    return _as_float(ev.at(name, _energy_limit(end, next_start)))


def _dc_energy(dc_delta: float | None, counters: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "DC", "charger_kwh": dc_delta, "battery_kwh": None,
            "loss_kwh": None, "loss_pct": None, **counters}


def _ac_energy(ac_delta: float | None, dc_delta: float | None, counters: dict[str, Any]) -> dict[str, Any]:
    charger = ac_delta if ac_delta is not None else 0.0
    battery = dc_delta if dc_delta is not None and dc_delta > 0 else None
    loss = (charger - battery) if battery is not None and charger > 0 else None
    pct = (loss / charger * 100.0) if loss is not None and charger > 0 else None
    return {"kind": "AC", "charger_kwh": charger, "battery_kwh": battery,
            "loss_kwh": round(loss, 4) if loss is not None else None,
            "loss_pct": round(pct, 2) if pct is not None else None, **counters}


def charge_energy(
    ev: Events, start: datetime, end: datetime, next_start: datetime | None = None,
) -> dict[str, Any]:
    """Charger-side and battery-side kWh, loss, and kind for [start, end].

    A fast signal during the session decides DC first: FastChargerPresent true,
    a fast FastChargerType, DCChargingPower at 15 kW or more, or ChargerVoltage
    at 300 V or more. Otherwise AC energy rising, or wall power near 200 V with
    current and FastChargerPresent false, is AC. DC energy rising alone is DC.
    Place is not used. A counter that drops is a reset: the rise before it and
    the rise after it are added.

    Samples up to 60 seconds after `end` count, and stop before `next_start`.
    """
    limit = _energy_limit(end, next_start)
    dc_delta = _counter_rise(ev, "DCChargingEnergyIn", start, limit)
    ac_delta = _counter_rise(ev, "ACChargingEnergyIn", start, limit)
    counters = {
        "ac_kwh": round(ac_delta, 4) if ac_delta is not None else None,
        "dc_kwh": round(dc_delta, 4) if dc_delta is not None else None,
    }
    if _dc_certain(ev, start, end):
        return _dc_energy(dc_delta, counters)
    if (ac_delta or 0) > 0 or _ac_wall_evidence(ev, start, end):
        return _ac_energy(ac_delta, dc_delta, counters)
    if (dc_delta or 0) > 0:
        return _dc_energy(dc_delta, counters)
    return _ac_energy(ac_delta, dc_delta, counters)


def _charge_row(
    vehicle_id: str, ev: Events, start: datetime, end: datetime, next_start: datetime | None = None,
) -> dict[str, Any]:
    energy = charge_energy(ev, start, end, next_start)
    kind = energy["kind"]
    kwh_added = energy["charger_kwh"]
    if kind == "DC":
        samples = ev.nums("DCChargingEnergyIn", start, end, include_start=False)
    else:
        samples = ev.nums("ACChargingEnergyIn", start, end, include_start=False)
    est = estimated_full(ev, start, end)
    ttf = [v for _t, v in ev.nums("TimeToFullCharge", start, end, include_start=False) if v > 0]
    # Average and max come from one series, so the shown max is never below the average.
    curve = charge_curve(ev, start, end, kind)
    power_samples = [(parse_ts(p["ts"]), float(p["kw"])) for p in curve if p["kw"] is not None]
    if not power_samples and len(samples) >= 2:
        power_samples = []
        for (t0, e0), (t1, e1) in zip(samples, samples[1:]):
            gap = _seconds(t0, t1)
            if gap > 0:
                power_samples.append((t1, (e1 - e0) * 3600.0 / gap))
    avg_kw, max_kw = series_avg_max(power_samples, end)
    active = _active_seconds(curve)
    return {
        "vehicle_id": vehicle_id,
        "start_ts": format_ts(start),
        "end_ts": format_ts(end),
        "kwh_added": kwh_added,
        "kind": kind,
        "start_soc": _as_float(ev.at("BatteryLevel", start)),
        "end_soc": _as_float(ev.at("BatteryLevel", end)),
        "max_power": _max_power_kw(samples),
        "max_charger_voltage": max((v for _t, v in ev.nums("ChargerVoltage", start, end, include_start=False)), default=None),
        "max_charge_amps": max((v for _t, v in ev.nums("ChargeAmps", start, end, include_start=False)), default=None),
        "max_charger_kw": max_kw,
        "avg_charger_kw": avg_kw,
        "time_to_full_h": ttf[0] if ttf else None,
        "charger_kwh": energy["charger_kwh"],
        "battery_kwh": energy["battery_kwh"],
        "ac_kwh": energy.get("ac_kwh"),
        "dc_kwh": energy.get("dc_kwh"),
        "loss_kwh": energy["loss_kwh"],
        "loss_pct": energy["loss_pct"],
        "sentry_min": sentry_minutes(ev, start, end),
        "est_full_ts": format_ts(est) if est else None,
        "fast_charger": _fast_charger(ev, start, end) if kind == "DC" else None,
        "active_s": active,
    }


def _active_seconds(curve: list[dict[str, Any]]) -> float | None:
    """Seconds between power samples that are both above a trickle. Pauses drop out."""
    pts = [(parse_ts(p["ts"]), float(p["kw"])) for p in curve if p.get("kw") is not None]
    total = 0.0
    counted = False
    for (t0, k0), (t1, k1) in zip(pts, pts[1:]):
        if k0 > 0.2 and k1 > 0.2:
            total += max(0.0, (t1 - t0).total_seconds())
            counted = True
    return round(total, 1) if counted else None


def _fast_charger(ev: Events, start: datetime, end: datetime) -> str | None:
    """Most frequent FastChargerType during a DC charge ('Supercharger', 'Combo', ...); None if the
    car did not send one. The bridge strips the 'FastCharger' prefix; strip it here too."""
    seen: dict[str, int] = {}
    vals = [v for _t, v in ev.series("FastChargerType", start, end)]
    at = ev.at("FastChargerType", start)
    if at is not None:
        vals.append(at)
    for v in vals:
        name = _fast_type_name(v)
        if name is None:
            continue
        seen[name] = seen.get(name, 0) + 1
    return max(seen, key=lambda k: seen[k]) if seen else None


CURVE_STALE = timedelta(minutes=2)  # ignore values left over from before the charge


def _fresh(ev: Events, name: str, ts: datetime, start: datetime) -> float | None:
    hit = ev.at_ts(name, ts)
    if hit is None or hit[0] < start - CURVE_STALE:
        return None
    return _as_float(hit[1])


def charge_curve(ev: Events, start: datetime, end: datetime, kind: str) -> list[dict[str, Any]]:
    """Charging power over the charge. AC uses ACChargingPower, DC uses DCChargingPower;
    when that signal is missing, charger voltage x current (kW). Values sent before the
    charge started (e.g. a 0 from an earlier session) are not carried in."""
    power_name = "DCChargingPower" if kind == "DC" else "ACChargingPower"
    names = (power_name, "ChargerVoltage", "ChargeAmps", "BatteryLevel")
    stamps = sorted({ts for n in names for ts, _ in ev.series(n, start, end)})
    out = []
    for ts in stamps:
        volts = _fresh(ev, "ChargerVoltage", ts, start)
        amps = _fresh(ev, "ChargeAmps", ts, start)
        kw = _fresh(ev, power_name, ts, start)
        if kw is None and volts is not None and amps is not None:
            kw = round(volts * amps / 1000.0, 2)
        out.append({
            "ts": format_ts(ts),
            "kw": kw,
            "voltage": volts,
            "amps": amps,
            "soc": _as_float(ev.at("BatteryLevel", ts)),
        })
    return out


def _parked_in_p_since(ev: Events, t: datetime) -> timedelta | None:
    gears = [(ts, v) for ts, v in _gear_series(ev) if ts <= t]
    if not gears or gears[-1][1] != "P":
        return None
    origin = gears[0][0]
    for i in range(len(gears) - 1, -1, -1):
        if gears[i][1] != "P":
            origin = gears[i + 1][0]
            break
    return t - origin


def _tire_rows(
    vehicle_id: str, ev: Events, trips: list[tuple[datetime, datetime]],
    candidates: list[tuple[datetime, float, float, float, float]],
) -> list[tuple]:
    cold_ts: set[datetime] = set()
    for start, _end in trips:
        before = [c for c in candidates if c[0] < start]
        if not before:
            continue
        latest = before[-1]
        parked = _parked_in_p_since(ev, latest[0])
        if parked is not None and parked >= COLD_PARK:
            cold_ts.add(latest[0])
    warm_ts: set[datetime] = set()
    for ts, *_rest in candidates:
        for start, end in trips:
            if start <= ts <= end + WARM_AFTER:
                warm_ts.add(ts)
                break
    rows: list[tuple] = []
    seen: set[datetime] = set()
    for ts, fl, fr, rl, rr in candidates:
        if ts in warm_ts:
            phase = "warm"
        elif ts in cold_ts:
            phase = "cold"
        else:
            continue
        if ts in seen:
            continue
        seen.add(ts)
        rows.append((vehicle_id, format_ts(ts), fl, fr, rl, rr,
                     _as_float(ev.at("OutsideTemp", ts)), phase))
    return rows


def _upsert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], keys: tuple[str, ...]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    marks = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks}) "
        f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


# ---------------------------------------------------------------- meta helpers

def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM process_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO process_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _window_start(conn: sqlite3.Connection, vehicle_id: str) -> datetime | None:
    if _meta_get(conn, f"version:{vehicle_id}") != PROCESS_VERSION:
        return None
    anchor = _meta_get(conn, f"anchor:{vehicle_id}")
    return parse_ts(anchor) if anchor else None


def prune_tire_readings(conn: sqlite3.Connection, vehicle_id: str) -> int:
    """Delete tire readings older than TIRE_KEEP before this car's newest reading."""
    try:
        row = conn.execute("SELECT MAX(ts) FROM tire_readings WHERE vehicle_id = ?", (vehicle_id,)).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row[0]:
        return 0
    cutoff = format_ts(parse_ts(row[0]) - TIRE_KEEP)
    return conn.execute("DELETE FROM tire_readings WHERE vehicle_id = ? AND ts < ?",
                        (vehicle_id, cutoff)).rowcount

def _new_anchor(
    trips: list[tuple[datetime, datetime]], charges: list[tuple[datetime, datetime]],
    previous: datetime | None,
    open_start: datetime | None = None,
) -> datetime | None:
    cands: list[datetime] = []
    if trips:
        cands.append(trips[-1][0] - WINDOW_LOOKBACK)
    if charges:
        cands.append(charges[-1][0])
    if open_start is not None:
        cands.append(open_start)
    if not cands:
        return previous
    anchor = min(cands)
    # Never move into the middle of an earlier trip: start at that trip instead.
    for s, e in trips:
        if s <= anchor <= e:
            anchor = s
    for s, e in charges:
        if s <= anchor <= e:
            anchor = s
    if previous is not None and anchor < previous:
        return previous
    return anchor


# ---------------------------------------------------------------- derived tables

def update_parked_usage(conn: sqlite3.Connection, vehicle_id: str) -> int:
    last = conn.execute(
        "SELECT MAX(start_ts) FROM parked_usage WHERE vehicle_id = ?", (vehicle_id,)
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT start_ts, end_ts, start_soc, end_soc, start_lifetime_kwh, end_lifetime_kwh
        FROM trips WHERE vehicle_id = ? ORDER BY start_ts
        """,
        (vehicle_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for a, b in zip(rows, rows[1:]):
        if last and a[1] < last:
            continue
        start, end = a[1], b[0]
        if end <= start:
            continue
        hours = _seconds(parse_ts(start), parse_ts(end)) / 3600.0
        kwh = None
        if a[5] is not None and b[4] is not None:
            kwh = max(0.0, float(b[4]) - float(a[5]))
        charged = conn.execute(
            "SELECT COUNT(*) FROM charges WHERE vehicle_id = ? AND start_ts >= ? AND start_ts < ?",
            (vehicle_id, start, end),
        ).fetchone()[0]
        soc_drop = (a[3] - b[2]) if a[3] is not None and b[2] is not None else None
        out.append({
            "vehicle_id": vehicle_id, "start_ts": start, "end_ts": end, "hours": hours,
            "kwh": kwh, "start_soc": a[3], "end_soc": b[2], "soc_drop": soc_drop,
            "charged": 1 if charged else 0,
        })
    _upsert(conn, "parked_usage", out, ("vehicle_id", "start_ts"))
    return len(out)


# ---------------------------------------------------------------- parked breakdown
#
# Parked time (gear P or unknown, not charging) is cut wherever the climate keeper,
# HVAC, sentry, gear or charge state changes, and at KST midnight. Each piece gets the
# highest category that applies; sentry under a higher category is kept as a flag.
# Energy is the rise of LifetimeEnergyUsed (linear between samples, so the jump that
# arrives when the car wakes is spread over the sleep before it); without it, the
# BatteryLevel drop x kWh-per-% (EnergyRemaining / BatteryLevel) is used and marked.

PARKED_CATEGORIES = ("camp", "dog", "keep", "sentry", "parked")
CLIMATE_KEEPER_CATEGORY = {"Party": "camp", "Dog": "dog", "On": "keep"}
HVAC_ON_STATES = ("On", "Precondition", "OverheatProtect")
SENTRY_ACTIVE = ("Armed", "Aware", "Panic", "Quiet")  # Idle: enabled but standing by
# Plain HVAC, preconditioning, overheat protection, battery heater, updates and media are
# not their own categories: that time counts as 주차 (parked).
PARKED_STATE_NAMES = ("ClimateKeeperMode", "SentryMode", "Gear", "DetailedChargeState", "ChargeState")


def sentry_active(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return value is not None and str(value) in SENTRY_ACTIVE


def parked_modes(ev: Events, t: datetime) -> tuple[str, ...]:
    """Every parked mode on at t (camp / dog / keep, sentry), in PARKED_CATEGORIES order."""
    on = set()
    clim = CLIMATE_KEEPER_CATEGORY.get(str(ev.at("ClimateKeeperMode", t)))
    if clim:
        on.add(clim)
    if sentry_active(ev.at("SentryMode", t)):
        on.add("sentry")
    return tuple(c for c in PARKED_CATEGORIES if c in on)


def parked_state(ev: Events, t: datetime) -> tuple[str, int] | None:
    """(category, sentry flag) at t, or None while driving or charging. The category is the
    first of PARKED_CATEGORIES that is on; sentry under a higher one is kept as a flag."""
    gear = ev.at("Gear", t)
    if gear in DRIVE_GEARS:
        return None
    if is_charging(ev.at("DetailedChargeState", t), ev.at("ChargeState", t)):
        return None
    modes = parked_modes(ev, t)
    sentry = 1 if "sentry" in modes else 0
    return (modes[0], sentry) if modes else ("parked", 0)


def _interp(series: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Linear value at t between samples; held after the last one, unknown before the first."""
    if not series or t < series[0][0]:
        return None
    if t >= series[-1][0]:
        return series[-1][1]
    i = bisect_right([p[0] for p in series], t)
    (t0, v0), (t1, v1) = series[i - 1], series[i]
    span = _seconds(t0, t1)
    return v0 if span <= 0 else v0 + (v1 - v0) * _seconds(t0, t) / span


def _num_series(ev: Events, name: str) -> list[tuple[datetime, float]]:
    out = []
    for ts, v in ev.series(name):
        num = _as_float(v)
        if num is not None:
            out.append((ts, num))
    return out


def parked_segments(ev: Events, start: datetime, end: datetime) -> list[dict[str, Any]]:
    if end <= start:
        return []
    cuts = {start, end}
    for name in PARKED_STATE_NAMES:
        cuts.update(ts for ts, _ in ev.series(name, start, end))
    day = start.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while day < end:
        cuts.add(day.astimezone(timezone.utc))
        day += timedelta(days=1)
    pts = sorted(c for c in cuts if start <= c <= end)
    pieces: list[list[Any]] = []
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        st = parked_state(ev, a)
        if st is None:
            continue
        modes = ",".join(parked_modes(ev, a))
        last = pieces[-1] if pieces else None
        if (last and last[1] == a and (last[2], last[3], last[4]) == (st[0], st[1], modes)
                and last[0].astimezone(KST).date() == a.astimezone(KST).date()):
            last[1] = b
        else:
            pieces.append([a, b, st[0], st[1], modes])
    life = _num_series(ev, "LifetimeEnergyUsed")
    level = _num_series(ev, "BatteryLevel")
    out = []
    for a, b, cat, sentry, modes in pieces:
        kwh: float | None = None
        est = 0
        e0, e1 = _interp(life, a), _interp(life, b)
        if e0 is not None and e1 is not None:
            kwh = max(0.0, e1 - e0)
        else:
            p0, p1 = _interp(level, a), _interp(level, b)
            er, bl = _as_float(ev.at("EnergyRemaining", a)), _as_float(ev.at("BatteryLevel", a))
            if p0 is not None and p1 is not None and er and bl:
                kwh = max(0.0, (p0 - p1) * er / bl)
                est = 1
        out.append({
            "start_ts": format_ts(a), "end_ts": format_ts(b), "category": cat, "sentry": sentry,
            "kwh": round(kwh, 4) if kwh is not None else None, "kwh_estimated": est,
            "modes": modes, "outside_temp": _as_float(ev.at("OutsideTemp", a)),
        })
    return out


def update_parked_segments(conn: sqlite3.Connection, vehicle_id: str, ev: Events, since: datetime | None) -> int:
    """Recompute parked pieces from `since` (or the first raw signal) to the latest signal."""
    if not ev:
        return 0
    start = since or ev.rows[0][0]
    end = ev.last_ts
    cut = format_ts(start)
    row = conn.execute(
        "SELECT start_ts, end_ts, kwh FROM parked_segments WHERE vehicle_id = ? AND start_ts < ? AND end_ts > ?",
        (vehicle_id, cut, cut),
    ).fetchone()
    if row:  # keep the part before the window, energy scaled by time
        s0, e0 = parse_ts(row[0]), parse_ts(row[1])
        frac = _seconds(s0, start) / max(1.0, _seconds(s0, e0))
        conn.execute(
            "UPDATE parked_segments SET end_ts = ?, kwh = CASE WHEN kwh IS NULL THEN NULL ELSE kwh * ? END "
            "WHERE vehicle_id = ? AND start_ts = ?",
            (cut, frac, vehicle_id, row[0]),
        )
    conn.execute("DELETE FROM parked_segments WHERE vehicle_id = ? AND start_ts >= ?", (vehicle_id, cut))
    segs = parked_segments(ev, start, end)
    for seg in segs:
        seg["vehicle_id"] = vehicle_id
    _upsert(conn, "parked_segments", segs, ("vehicle_id", "start_ts"))
    return len(segs)


def update_battery_hourly(conn: sqlite3.Connection, vehicle_id: str, now: datetime | None = None) -> int:
    key = f"battery_hour:{vehicle_id}"
    mark = _meta_get(conn, key)
    since: datetime | None = None
    if mark:
        since = parse_ts(mark) - timedelta(hours=1)
        since = since.replace(minute=0, second=0, microsecond=0)
    params: list[Any] = [vehicle_id]
    prev_val: float | None = None
    if since is not None:
        cut = format_ts(since)
        row = conn.execute(
            "SELECT value FROM signals WHERE vehicle_id = ? AND name = 'BatteryLevel' AND ts < ? "
            "ORDER BY ts DESC LIMIT 1",
            (vehicle_id, cut),
        ).fetchone()
        if row:
            prev_val = _as_float(decode_value(row[0]))
        sig = conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'BatteryLevel' AND ts >= ? ORDER BY ts",
            (vehicle_id, cut),
        ).fetchall()
    else:
        sig = conn.execute(
            "SELECT ts, value FROM signals WHERE vehicle_id = ? AND name = 'BatteryLevel' ORDER BY ts",
            params,
        ).fetchall()
    hours: dict[str, dict[str, Any]] = {}
    last_dt: datetime | None = None
    for ts_s, raw in sig:
        val = _as_float(decode_value(raw))
        if val is None:
            continue
        dt = parse_ts(ts_s)
        last_dt = dt
        h = kst_hour(dt)
        b = hours.setdefault(h, {"last": None, "min": None, "max": None, "used": 0.0, "charged": 0.0, "n": 0})
        if prev_val is not None:
            d = val - prev_val
            if d < 0:
                b["used"] += -d
            else:
                b["charged"] += d
        prev_val = val
        b["last"] = val
        b["min"] = val if b["min"] is None else min(b["min"], val)
        b["max"] = val if b["max"] is None else max(b["max"], val)
        b["n"] += 1
    rows: list[dict[str, Any]] = []
    for h, b in hours.items():
        h_start = datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=KST)
        lo, hi = format_ts(h_start), format_ts(h_start + timedelta(hours=1))
        driving = conn.execute(
            "SELECT COUNT(*) FROM trips WHERE vehicle_id = ? AND start_ts < ? AND end_ts > ?",
            (vehicle_id, hi, lo),
        ).fetchone()[0]
        charging = conn.execute(
            "SELECT COUNT(*) FROM charges WHERE vehicle_id = ? AND start_ts < ? AND end_ts > ?",
            (vehicle_id, hi, lo),
        ).fetchone()[0]
        rows.append({
            "vehicle_id": vehicle_id, "hour": h, "last_pct": b["last"], "min_pct": b["min"],
            "max_pct": b["max"], "used_pct": round(b["used"], 4), "charged_pct": round(b["charged"], 4),
            "samples": b["n"], "driving": 1 if driving else 0, "charging": 1 if charging else 0,
        })
    _upsert(conn, "battery_hourly", rows, ("vehicle_id", "hour"))
    if last_dt is not None:
        _meta_set(conn, key, format_ts(last_dt))
    return len(rows)


def refresh_hour_flags(conn: sqlite3.Connection, vehicle_id: str, since: str) -> None:
    """Mark battery hours that overlap trips/charges that closed after `since` (UTC ISO)."""
    spans = conn.execute(
        "SELECT start_ts, end_ts, 'driving' FROM trips WHERE vehicle_id = ? AND end_ts >= ? "
        "UNION ALL SELECT start_ts, end_ts, 'charging' FROM charges WHERE vehicle_id = ? AND end_ts >= ?",
        (vehicle_id, since, vehicle_id, since),
    ).fetchall()
    for s, e, col in spans:
        t = parse_ts(s).astimezone(KST).replace(minute=0, second=0, microsecond=0)
        end = parse_ts(e)
        n = 0
        while t < end and n < 72:
            conn.execute(
                f"UPDATE battery_hourly SET {col} = 1 WHERE vehicle_id = ? AND hour = ?",
                (vehicle_id, t.strftime("%Y-%m-%dT%H")),
            )
            t += timedelta(hours=1)
            n += 1


def prune_raw(conn: sqlite3.Connection, now: datetime | None = None, keep_days: int = RAW_KEEP_DAYS) -> int:
    """Delete raw signals older than `keep_days`, keeping each signal's latest row."""
    now = now or datetime.now(timezone.utc)
    cut = format_ts(now - timedelta(days=keep_days))
    cur = conn.execute(
        """
        DELETE FROM signals
        WHERE ts < ?
          AND EXISTS (
            SELECT 1 FROM signals s2
            WHERE s2.vehicle_id = signals.vehicle_id AND s2.name = signals.name AND s2.ts > signals.ts
          )
        """,
        (cut,),
    )
    conn.execute(
        "DELETE FROM alert_log WHERE ts < ?",
        (format_ts(now - timedelta(days=ALERT_LOG_KEEP_DAYS)),),
    )
    return cur.rowcount


def _maybe_prune(conn: sqlite3.Connection, now: datetime) -> int:
    today = now.astimezone(KST).strftime("%Y-%m-%d")
    if _meta_get(conn, "pruned_on") == today:
        return 0
    with conn:
        n = prune_raw(conn, now)
        _meta_set(conn, "pruned_on", today)
    if n:
        print(f"pruned {n} raw signal row(s) older than {RAW_KEEP_DAYS} days", flush=True)
    return n


def _delete_charge_bundle(conn: sqlite3.Connection, vehicle_id: str, start_ts: str) -> None:
    """Remove one charge and the rows that point at it. Trips and consumables stay."""
    conn.execute("DELETE FROM charges WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start_ts))
    for table, column in (
        ("charge_meta", "charge_id"),
        ("charge_locations", "charge_id"),
        ("charge_charger", "charge_id"),
    ):
        try:
            conn.execute(
                f"DELETE FROM {table} WHERE vehicle_id = ? AND {column} = ?",
                (vehicle_id, start_ts),
            )
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(
            "DELETE FROM ledger WHERE vehicle_id = ? AND id = ?",
            (vehicle_id, "auto-charge:" + start_ts),
        )
    except sqlite3.OperationalError:
        pass


def _charge_rewrite_floor(
    conn: sqlite3.Connection, vehicle_id: str, now: datetime,
) -> datetime | None:
    """None while this car has no stored charges, so a first build can use every signal.

    After that, sessions older than 3 hours are frozen.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM charges WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or int(row[0]) == 0:
        return None
    return now - WINDOW_LOOKBACK


def _stored_charge_ends(conn: sqlite3.Connection, vehicle_id: str) -> dict[str, datetime]:
    try:
        rows = conn.execute(
            "SELECT start_ts, end_ts FROM charges WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {start: parse_ts(end) for start, end in rows}


def _spans_keeping_stored_rows(
    conn: sqlite3.Connection, vehicle_id: str, spans: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Write a new charge, or the survivor of a merge. Do not rewrite any other row.

    A merge absorbs only the next stored row. A span that covers a trip, or more
    than that one neighbor, is not written. The end stays the earlier of the
    detected stop and the ends already stored.
    """
    stored = _stored_charge_ends(conn, vehicle_id)
    if not stored:
        return spans
    chosen: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if _trip_between(conn, vehicle_id, start, end):
            continue
        key = format_ts(start)
        covered = [stamp for stamp in stored if key < stamp < format_ts(end)]
        if len(covered) > 1:
            continue
        if key not in stored:
            if covered:
                chosen.append((start, min(end, stored[covered[0]])))
            else:
                chosen.append((start, end))
            continue
        if not covered:
            continue
        cap = stored[key]
        if stored[covered[0]] > cap:
            cap = stored[covered[0]]
        chosen.append((start, min(end, cap)))
    return chosen


def _drop_merged_away_charges(
    conn: sqlite3.Connection, vehicle_id: str, spans: list[tuple[datetime, datetime]],
) -> None:
    """Delete only rows that sit inside a charge this pass just wrote.

    A finalized charge that started before the 3 h window is never removed, including
    on a version bump or a full rebuild.
    """
    try:
        conn.execute("SELECT 1 FROM charges LIMIT 1")
    except sqlite3.OperationalError:
        return
    for start, end in spans:
        rows = conn.execute(
            "SELECT start_ts FROM charges WHERE vehicle_id = ? AND start_ts > ? AND start_ts < ?",
            (vehicle_id, format_ts(start), format_ts(end)),
        ).fetchall()
        for (stamp,) in rows:
            _delete_charge_bundle(conn, vehicle_id, stamp)


def _drop_replaced_trips(
    conn: sqlite3.Connection, vehicle_id: str, ev: Events, kept: list[tuple[datetime, datetime]],
) -> None:
    """Remove trip rows inside the loaded gear window that this pass no longer produces.

    A brief park that used to be its own trip is deleted when it joins the neighbours.
    Trips that started before the remaining signals are left alone.
    """
    gears = _gear_series(ev)
    if not gears:
        return
    lo = format_ts(gears[0][0])
    kept_starts = {format_ts(s) for s, _e in kept}
    kept_ends = {format_ts(e) for _s, e in kept}
    try:
        rows = conn.execute(
            "SELECT start_ts FROM trips WHERE vehicle_id = ? AND start_ts >= ?", (vehicle_id, lo)
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for (start,) in rows:
        if start in kept_starts:
            continue
        conn.execute("DELETE FROM trips WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start))
        for table, col in (("trip_points", "trip_start_ts"), ("trip_events", "trip_start_ts"),
                           ("route_match", "trip_start_ts")):
            conn.execute(f"DELETE FROM {table} WHERE vehicle_id = ? AND {col} = ?", (vehicle_id, start))
    try:
        usage = conn.execute(
            "SELECT start_ts FROM parked_usage WHERE vehicle_id = ? AND start_ts >= ?", (vehicle_id, lo)
        ).fetchall()
    except sqlite3.OperationalError:
        usage = []
    for (start,) in usage:
        if start not in kept_ends:
            conn.execute("DELETE FROM parked_usage WHERE vehicle_id = ? AND start_ts = ?", (vehicle_id, start))


# ---------------------------------------------------------------- main entry

def process_vehicle(
    conn: sqlite3.Connection,
    vehicle_id: str,
    alerts: bool = False,
    now: datetime | None = None,
    sender: Any = None,
) -> ProcessStats:
    now = now or datetime.now(timezone.utc)
    stats = ProcessStats()
    since = _window_start(conn, vehicle_id)
    ev = _load_events(conn, vehicle_id, since)
    if not ev:
        return stats
    trips, ignored = _find_trips(ev, now)
    stats.ignored_trips = ignored
    _closed, open_start, park_at = _gear_walk(ev)
    if open_start is not None and park_at is not None and now - park_at <= PARK_MERGE:
        stats.park_grace = True  # next cycle must run even if no new signal arrives
    candidates = _tpms_candidates(ev)
    trip_rows = [_trip_row(vehicle_id, ev, s, e, candidates) for s, e in trips]
    from process.net_energy import apply_trip_net, recent_net_ratios
    # A version bump rebuilds every trip still in the signals, so the ratio is learned
    # inside that pass. Later passes only see the lookback window and reuse stored ratios.
    prior = [] if since is None else recent_net_ratios(conn, vehicle_id, {r["start_ts"] for r in trip_rows})
    apply_trip_net(ev, trip_rows, prior)
    routes = [route_points(ev, s, e) for s, e in trips]
    for row, (s, e), pts in zip(trip_rows, trips, routes):
        row.update(trip_place_fields(ev, s, e, pts))
    charge_floor = _charge_rewrite_floor(conn, vehicle_id, now)
    charge_spans, open_charge = _charge_sessions(ev, conn, vehicle_id, trips)
    # Once any charge is stored, only a session that started inside the last
    # 3 hours may be written or may absorb a sibling. A version bump or a full
    # rebuild leaves older rows alone.
    recent_spans = [(s, e) for s, e in charge_spans if charge_floor is None or s >= charge_floor]
    # An incremental pass can still be sitting on a charge that reached Complete
    # while the port stayed open. That row was never stored. Write it even when
    # the start is older than the 3 hour floor. A full rebuild does not, so
    # already stored charges are not split or rewritten.
    if since is not None:
        seen = set(recent_spans)
        for span in charge_spans:
            if span in seen or not _ended_complete(ev, span[1]):
                continue
            recent_spans.append(span)
            seen.add(span)
    recent_spans.sort(key=lambda span: span[0])
    if open_charge is not None and charge_floor is not None and open_charge < charge_floor:
        open_charge = None
    write_spans = _spans_keeping_stored_rows(conn, vehicle_id, recent_spans)
    charge_rows = [
        _charge_row(
            vehicle_id, ev, s, e,
            write_spans[i + 1][0] if i + 1 < len(write_spans) else None,
        )
        for i, (s, e) in enumerate(write_spans)
    ]
    tire_rows = _tire_rows(vehicle_id, ev, trips, candidates)
    # Short transactions. A full recompute used to keep one write open for every
    # trip, which on the rollback journal blocks the bridge until it commits.
    # Each commit releases the lock. An exception rolls that piece back only.
    with conn:
        _drop_replaced_trips(conn, vehicle_id, ev, trips)
        _upsert(conn, "trips", trip_rows, ("vehicle_id", "start_ts"))
    for (s, e), pts in zip(trips, routes):
        with conn:
            _store_route(conn, vehicle_id, s, pts, ev)
            _store_events(conn, vehicle_id, s, harsh_events(ev, s, e), pts)
    with conn:
        _upsert(conn, "charges", charge_rows, ("vehicle_id", "start_ts"))
        _drop_merged_away_charges(conn, vehicle_id, write_spans)
        conn.executemany(
            """
            INSERT OR IGNORE INTO tire_readings (
                vehicle_id, ts, fl, fr, rl, rr, outside_temp, phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tire_rows,
        )
        prune_tire_readings(conn, vehicle_id)
        update_parked_usage(conn, vehicle_id)
        update_parked_segments(conn, vehicle_id, ev, since)
        update_battery_hourly(conn, vehicle_id, now)
        refresh_hour_flags(conn, vehicle_id, format_ts(since) if since else "")
        anchor = _new_anchor(trips, write_spans or recent_spans, since, open_charge)
        _meta_set(conn, f"anchor:{vehicle_id}", format_ts(anchor) if anchor else None)
        _meta_set(conn, f"version:{vehicle_id}", PROCESS_VERSION)
    stats.trips = len(trip_rows)
    stats.charges = len(charge_rows)
    stats.tire_readings = len(tire_rows)
    sync_auto_charges(conn, vehicle_id)
    if alerts:
        from process.alerts import run_alerts

        try:
            run_alerts(conn, vehicle_id, ev, now=now, sender=sender)
        except Exception as exc:  # alerts must never break processing
            print(f"alerts error: {type(exc).__name__}: {exc}", flush=True)
    return stats


def update_charge_places(conn: sqlite3.Connection, vehicle_id: str) -> None:
    """Charges get a point (manual > saved place > car > previous trip end); saved places without
    coordinates take the representative point of their charges; then those charges move onto it."""
    from process.chargeloc import fill_charge_locations, station_coords_from_charges

    try:
        fill_charge_locations(conn, vehicle_id)
        if station_coords_from_charges(conn, vehicle_id):
            fill_charge_locations(conn, vehicle_id)
    except sqlite3.Error as exc:
        print(f"charge location error: {type(exc).__name__}", flush=True)


def _run_deferred_alerts(conn: sqlite3.Connection, vehicle_ids: list[str], now: datetime, sender: Any) -> None:
    """With no new signals, still finish a trip_end alert that is waiting for tires, and run
    the 14 h AC-spot check (the car sleeps while parked, so no new signals come)."""
    from process.alerts import (
        charge_port_hold_due, check_open_after_park, check_park14h, check_trip_end, trip_end_pending,
    )
    from process.maintenance import IDLE_CHECKS

    from process.alerts import send_pending_sentry_alarms

    for vid in vehicle_ids:
        try:
            send_pending_sentry_alarms(conn, vid, now, sender or _default_sender())
        except Exception as exc:  # alerts must never break processing
            print(f"alerts error: {type(exc).__name__}: {exc}", flush=True)
        for check in (check_park14h, *IDLE_CHECKS):
            try:
                check(conn, vid, None, now, sender or _default_sender())
            except Exception as exc:  # alerts must never break processing
                print(f"alerts error: {type(exc).__name__}: {exc}", flush=True)
        try:
            if charge_port_hold_due(conn, vid, now):
                ev = _load_events(conn, vid, _window_start(conn, vid))
                check_open_after_park(conn, vid, ev, now, sender or _default_sender())
        except Exception as exc:  # alerts must never break processing
            print(f"alerts error: {type(exc).__name__}: {exc}", flush=True)
        try:
            if not trip_end_pending(conn, vid, now):
                continue
            ev = _load_events(conn, vid, _window_start(conn, vid))
            check_trip_end(conn, vid, ev, now, sender or _default_sender())
        except Exception as exc:  # alerts must never break processing
            print(f"alerts error: {type(exc).__name__}: {exc}", flush=True)


def _default_sender() -> Any:
    from process.alerts import webhook_sender

    return webhook_sender


def _signal_state(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(rowid), COUNT(*) FROM signals").fetchone()
    return f"{row[0]}:{row[1]}"


def process_db(
    db_path: str | Path,
    seed: bool = True,
    alerts: bool = False,
    now: datetime | None = None,
    sender: Any = None,
    geocoder: Any = None,
    matcher: Any = None,
) -> ProcessStats:
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        total = ProcessStats()
        state = _signal_state(conn)
        vehicle_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT vehicle_id FROM signals ORDER BY vehicle_id"
            ).fetchall()
        ]
        up_to_date = (
            _meta_get(conn, "signal_state") == state
            and all(_meta_get(conn, f"version:{v}") == PROCESS_VERSION for v in vehicle_ids)
        )
        if up_to_date:
            total.skipped = True
            for vid in vehicle_ids:
                sync_auto_charges(conn, vid)
            if alerts:
                _run_deferred_alerts(conn, vehicle_ids, now, sender)
        else:
            for vid in vehicle_ids:
                total.add(process_vehicle(conn, vid, alerts=alerts, now=now, sender=sender))
            # A brief park is not a finished trip yet. Leave signal_state stale so the next
            # cycle, even with no new signal, closes it once the park has lasted past PARK_MERGE.
            if not total.park_grace:
                with conn:
                    _meta_set(conn, "signal_state", _signal_state(conn))
        if geocoder is not None:
            from process.geocode import fill_trip_labels

            for vid in vehicle_ids:
                try:
                    fill_trip_labels(conn, vid, geocoder)
                except sqlite3.Error as exc:
                    print(f"geocode error: {type(exc).__name__}", flush=True)
        if matcher is not None:
            from process.match import match_pending

            for vid in vehicle_ids:
                try:
                    match_pending(conn, vid, matcher, now=now)
                except sqlite3.Error as exc:
                    print(f"route match error: {type(exc).__name__}", flush=True)
        from process.roads import update_road_usage

        for vid in vehicle_ids:
            update_charge_places(conn, vid)
            try:
                update_road_usage(conn, vid)
            except sqlite3.Error as exc:
                print(f"road usage error: {type(exc).__name__}", flush=True)
        _maybe_prune(conn, now)
        if seed:
            seed_insurance(conn)
            seed_consumables(conn)
        try:
            from process.charge_backfill import maybe_purge_stations
            purged = maybe_purge_stations(conn, db_path)
            if purged is not None and purged.get("deleted"):
                print(
                    f"saved stations removed {purged['deleted']}, backup {purged['backup']}",
                    flush=True,
                )
        except Exception as exc:  # station purge must never break processing
            print(f"station purge error: {type(exc).__name__}", flush=True)
        for vid in vehicle_ids:
            try:
                from web.notify import sync_notices
                sync_notices(conn, vid, now)
            except Exception as exc:  # notices must never break processing
                print(f"notice error: {type(exc).__name__}", flush=True)
            try:
                from process.charge_backfill import backfill_charger_info
                filled = backfill_charger_info(conn, vid, now=now)
                if filled is not None:
                    print(
                        "charger rematch "
                        f"matched {filled['matched']}, unmatched {filled['unmatched']}, "
                        f"no_location {filled['no_location']}, calls {filled['calls']}, "
                        f"operator_kept {filled['operator_kept']}",
                        flush=True,
                    )
            except Exception as exc:  # rematch must never break processing
                print(f"charger rematch error: {type(exc).__name__}", flush=True)
            try:
                from process.ev_cache_cleanup import maybe_cleanup
                maybe_cleanup(conn)
            except Exception as exc:  # cache cleanup must never break processing
                print(f"charger cache cleanup error: {type(exc).__name__}", flush=True)
            try:
                from process.evcharger import fill_vehicle
                fill_vehicle(conn, vid, now=now)
            except Exception as exc:  # charger lookup must never break processing
                print(f"charger lookup error: {type(exc).__name__}", flush=True)
        return total
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build trips, charges, tire readings, parked usage and hourly battery from signals."
    )
    parser.add_argument("--db", default="signals.db", help="SQLite database path (default: signals.db)")
    parser.add_argument("--no-seed", action="store_true", help="skip insurance and consumable item seeds")
    parser.add_argument("--full", action="store_true", help="recompute from all raw signals")
    args = parser.parse_args(argv)
    if args.full:
        conn = connect(args.db)
        with conn:
            conn.execute("DELETE FROM process_meta WHERE key LIKE 'version:%' OR key LIKE 'anchor:%' "
                         "OR key LIKE 'battery_hour:%' OR key = 'signal_state'")
        conn.close()
    stats = process_db(args.db, seed=not args.no_seed)
    print(
        f"trips {stats.trips}, charges {stats.charges}, "
        f"tire_readings {stats.tire_readings}, "
        f"ignored {stats.ignored_trips} short trip(s) in {args.db}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
