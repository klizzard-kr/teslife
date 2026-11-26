#!/usr/bin/env python3
"""teslife-bridge: fleet-telemetry logger output -> teslife `signals` table.

Reads fleet-telemetry's JSON log lines (journald, `-o cat`) on stdin, picks the
`record_payload` entries, and stores one signal row per field using teslife's own
ingest.store_signals (INSERT OR IGNORE, so replays are harmless).

Normalisation (teslife expects its simulator's conventions):
  * Odometer: miles -> km, VehicleSpeed: mph -> km/h, RatedRange/EstBatteryRange: miles -> km
    (Tesla Available Data units).
  * Enum prefixes are stripped: Gear "ShiftStateP" -> "P" (Unknown/Invalid/SNA dropped);
    ChargeState/DetailedChargeState "ChargeStateCharging" -> "Charging"; HvacPower
    "HvacPowerStateOn" -> "On"; SentryMode "SentryModeStateArmed" -> "Armed"; ClimateKeeperMode
    "ClimateKeeperModeStateDog" -> "Dog" (Off/On/Dog/Party); windows
    "WindowStateClosed" -> "Closed".
  * Accelerations (g), pack voltage/current, module temperatures, heater levels, charger
    voltage/amps, TimeToFullCharge (hours), software-update fields and TPMS warnings pass through.
  * ChargeState and DetailedChargeState are stored as sent (prefix stripped), each under its own
    name; nothing is copied between them. teslife decides "charging" from DetailedChargeState
    (Disconnected/NoPower/Starting/Charging/Complete/Stopped) and falls back to ChargeState
    (Charging, or the charger step Enable) only when DetailedChargeState is unknown. "Unknown" is
    dropped for both so it can never end a charge session.
  * Tire pressures (TpmsPressure*) arrive in bar and teslife stores/thresholds in bar (web
    DEFAULT_TIRE_ALERT_BAR = 2.6, simulator ~2.9): passed through unchanged.
  * Location {latitude, longitude} is stored as {"lat", "lon"} (6 decimals) only while driving:
    kept when the gear before or after the payload is not P (so the departure and arrival points
    stay). Parked positions are dropped. Navigation destination fields (DestinationName,
    DestinationLocation, MilesToArrival, MinutesToArrival, ExpectedEnergyPercentAtTripArrival)
    are dropped: teslife no longer uses the navigation destination.
    GpsHeading is kept as degrees, with the payload time, even while parked. Other GPS /
    route fields (OriginLocation, RouteLine, and any other Gps* name) are dropped.
  * Invalid ("<invalid>") values are dropped.
  * New enum prefixes stripped: BMSState "BMSStateFault" -> "Fault", Hvil "HvilStatusOK" -> "OK",
    DiStateR "DriveInverterStateFault" -> "Fault", CabinOverheatProtectionMode
    "CabinOverheatProtectionModeStateOn" -> "On", FastChargerType "FastChargerCombo" -> "Combo".
  * Alerts records (metadata.txtype "alerts", data = list of {Name, Audiences, StartedAt, EndedAt})
    go to the `vehicle_alerts` table (one row per name + start, EndedAt filled in later).
    Errors / connectivity records are ignored.
No secrets, no network access, no Tesla API.
"""
import json, sys, time
import ingest

DB = "signals.db"
MI = 1.609344
SKIP = {"Vin", "CreatedAt", "IsResend", "MinutesToArrival", "ExpectedEnergyPercentAtTripArrival"}
DROP_PREFIX = ("Location", "Gps", "Destination", "OriginLocation", "RouteLine", "MilesToArrival")
KEEP = {"Location", "GpsHeading"}  # kept despite DROP_PREFIX
LATLON = {"Location"}
GEAR_STATE = {}  # vin -> last known gear letter (seeded from the DB in main)
RANGE_MI = {"RatedRange", "EstBatteryRange"}
PREFIXES = {"Gear": "ShiftState", "ChargeState": "ChargeState",
            "DetailedChargeState": "DetailedChargeState", "HvacPower": "HvacPowerState",
            "SentryMode": "SentryModeState", "ClimateKeeperMode": "ClimateKeeperModeState",
            "FdWindow": "WindowState", "FpWindow": "WindowState",
            "RdWindow": "WindowState", "RpWindow": "WindowState",
            "BMSState": "BMSState", "Hvil": "HvilStatus", "DiStateR": "DriveInverterState",
            "CabinOverheatProtectionMode": "CabinOverheatProtectionModeState",
            "FastChargerType": "FastCharger"}
GEARS = {"Park": "P", "Reverse": "R", "Neutral": "N", "Drive": "D", "P": "P", "R": "R", "N": "N", "D": "D"}


def heading_degrees(value):
    """GpsHeading as degrees. Anything else is dropped. The caller stores the time."""
    if isinstance(value, bool) or value is None or value == "<invalid>":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or text == "<invalid>":
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    rounded = round(num, 1)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def convert(name, value):
    if value == "<invalid>" or value is None:
        return None
    if name == "GpsHeading":
        return heading_degrees(value)
    if isinstance(value, dict) and set(value) == {"latitude", "longitude"}:
        if name not in LATLON:
            return None
        try:
            lat, lon = float(value["latitude"]), float(value["longitude"])
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return None
        return {"lat": round(lat, 6), "lon": round(lon, 6)}
    if name == "Odometer" and isinstance(value, (int, float)):
        return round(value * MI, 3)
    if name == "VehicleSpeed" and isinstance(value, (int, float)):
        return round(value * MI, 2)
    if name in RANGE_MI and isinstance(value, (int, float)):
        return round(value * MI, 2)
    p = PREFIXES.get(name)
    if p and isinstance(value, str) and value.startswith(p):
        value = value[len(p):]
    if name == "Gear" and isinstance(value, str):
        # teslife expects single letters (P/R/N/D); Unknown/Invalid/SNA would split trips
        return GEARS.get(value)
    if name in ("SentryMode", "FdWindow", "FpWindow", "RdWindow", "RpWindow") and value == "Unknown":
        return None
    return value


def norm_ts(ts):
    """CreatedAt may be epoch seconds on connectivity records; store ISO UTC like other signals."""
    from datetime import datetime, timezone
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.strip().replace(".", "", 1).isdigit()):
        v = float(ts)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts


def _ci(d, *names):
    for n in names:
        for k, v in d.items():
            if k.lower() == n.lower():
                return v
    return None


def alerts_from_line(line):
    """Fleet Telemetry alerts records -> [(vin, name, started_at, ended_at, audiences_json)]."""
    try:
        o = json.loads(line)
    except ValueError:
        return []
    if o.get("msg") != "record_payload":
        return []
    meta = o.get("metadata") if isinstance(o.get("metadata"), dict) else {}
    d = o.get("data")
    if meta.get("txtype") != "alerts":
        return []
    items = d if isinstance(d, list) else (_ci(d, "alerts") if isinstance(d, dict) else None)
    if not isinstance(items, list):
        return []
    vin = meta.get("vin") or (d.get("Vin") if isinstance(d, dict) else None) or o.get("vin")
    if not vin:
        return []
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        name = _ci(a, "Name")
        started = norm_ts(_ci(a, "StartedAt", "started_at"))
        if isinstance(started, dict):
            started = norm_ts(_ci(started, "seconds"))
        if not name or not started:
            continue
        ended = _ci(a, "EndedAt", "ended_at")
        if isinstance(ended, dict):
            ended = _ci(ended, "seconds")
        ended = norm_ts(ended) if ended not in (None, "", 0, "0") else None
        aud = _ci(a, "Audiences", "audiences") or []
        out.append((vin, str(name), started, ended, json.dumps(aud if isinstance(aud, list) else [aud])))
    return out


def store_alerts(conn, rows):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        for vin, name, started, ended, aud in rows:
            conn.execute(
                "INSERT INTO vehicle_alerts (vehicle_id, name, started_at, ended_at, audiences, received_ts) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(vehicle_id, name, started_at) DO UPDATE SET "
                "ended_at = COALESCE(excluded.ended_at, vehicle_alerts.ended_at), audiences = excluded.audiences",
                (vin, name, started, ended, aud, now),
            )
    return len(rows)


def rows_from_line(line):
    try:
        o = json.loads(line)
    except ValueError:
        return []
    if o.get("msg") != "record_payload":
        return []
    d = o.get("data")
    if not isinstance(d, dict):
        return []
    vin, ts = d.get("Vin") or o.get("vin"), norm_ts(d.get("CreatedAt"))
    if not vin or not ts:
        return []
    out = []
    before = GEAR_STATE.get(vin)
    after = convert("Gear", d["Gear"]) if "Gear" in d else None
    after = after or before
    if after:
        GEAR_STATE[vin] = after
    driving = before not in (None, "P") or after not in (None, "P")
    for k, v in d.items():
        if k in SKIP or (k.startswith(DROP_PREFIX) and k not in KEEP):
            continue
        if k == "Location" and not driving:
            continue
        v = convert(k, v)
        if v is not None:
            if k in ("ChargeState", "DetailedChargeState") and v == "Unknown":
                continue
            out.append({"vehicle_id": vin, "timestamp": ts, "name": k, "value": v})
    return out


def main():
    conn = ingest.connect(DB)
    conn.execute("PRAGMA busy_timeout = 15000")
    for vin, raw in conn.execute(
        "SELECT s.vehicle_id, s.value FROM signals s JOIN (SELECT vehicle_id, MAX(ts) ts FROM signals "
        "WHERE name = 'Gear' GROUP BY vehicle_id) m ON m.vehicle_id = s.vehicle_id AND m.ts = s.ts "
        "WHERE s.name = 'Gear'"
    ).fetchall():
        try:
            GEAR_STATE[vin] = json.loads(raw)
        except ValueError:
            pass
    n = 0
    for line in sys.stdin:
        try:  # a malformed alerts record must never stop signal ingest
            alerts = alerts_from_line(line)
            if alerts:
                store_alerts(conn, alerts)
                print(f"stored {len(alerts)} vehicle alert(s)", flush=True)
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"vehicle alerts skipped: {type(exc).__name__}", flush=True)
        rows = rows_from_line(line)
        if rows:
            n += ingest.store_signals(conn, rows)
            print(f"stored {len(rows)} signal(s) ({n} new total)", flush=True)


if __name__ == "__main__":
    main()
