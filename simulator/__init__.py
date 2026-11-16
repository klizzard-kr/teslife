"""Tesla Fleet Telemetry-like JSON signal generator (Stage 1)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

# Starting set from DESIGN.md "받을 항목", using Tesla Available Data names.
# Location / GPS fields are never generated.
SIGNAL_NAMES = (
    "Odometer",
    "LifetimeEnergyUsed",
    "BatteryLevel",
    "EnergyRemaining",
    "ChargeState",
    "ACChargingEnergyIn",
    "DCChargingEnergyIn",
    "ChargeLimitSoc",
    "TpmsPressureFl",
    "TpmsPressureFr",
    "TpmsPressureRl",
    "TpmsPressureRr",
    "Gear",
    "VehicleSpeed",
    "Locked",
    "DoorState",
    "InsideTemp",
    "OutsideTemp",
    "HvacPower",
)

DEFAULT_VEHICLE_ID = "VINTEST0000000001"
SCENARIOS = ("parked", "driving", "charging", "tire_pressure", "day", "dc_day")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signal(vehicle_id: str, ts: datetime, name: str, value: Any) -> dict[str, Any]:
    return {
        "vehicle_id": vehicle_id,
        "timestamp": _iso(ts),
        "name": name,
        "value": value,
    }


def _snapshot(
    vehicle_id: str,
    ts: datetime,
    *,
    odometer: float,
    lifetime_energy: float,
    battery_level: float,
    energy_remaining: float,
    charge_state: str,
    ac_energy: float,
    dc_energy: float,
    charge_limit: int,
    tpms: tuple[float, float, float, float],
    gear: str,
    speed: float,
    locked: bool,
    door_state: str,
    inside_temp: float,
    outside_temp: float,
    hvac_power: str,
) -> list[dict[str, Any]]:
    fl, fr, rl, rr = tpms
    values: dict[str, Any] = {
        "Odometer": odometer,
        "LifetimeEnergyUsed": lifetime_energy,
        "BatteryLevel": battery_level,
        "EnergyRemaining": energy_remaining,
        "ChargeState": charge_state,
        "ACChargingEnergyIn": ac_energy,
        "DCChargingEnergyIn": dc_energy,
        "ChargeLimitSoc": charge_limit,
        "TpmsPressureFl": fl,
        "TpmsPressureFr": fr,
        "TpmsPressureRl": rl,
        "TpmsPressureRr": rr,
        "Gear": gear,
        "VehicleSpeed": speed,
        "Locked": locked,
        "DoorState": door_state,
        "InsideTemp": inside_temp,
        "OutsideTemp": outside_temp,
        "HvacPower": hvac_power,
    }
    return [signal(vehicle_id, ts, name, values[name]) for name in SIGNAL_NAMES]


def scenario_parked(
    vehicle_id: str, start: datetime
) -> Iterator[dict[str, Any]]:
    tpms = (2.9, 2.9, 2.85, 2.85)
    for i in range(4):
        ts = start + timedelta(seconds=60 * i)
        yield from _snapshot(
            vehicle_id,
            ts,
            odometer=12345.6,
            lifetime_energy=4120.4,
            battery_level=72.0,
            energy_remaining=54.0,
            charge_state="Idle",
            ac_energy=0.0,
            dc_energy=0.0,
            charge_limit=80,
            tpms=tpms,
            gear="P",
            speed=0.0,
            locked=True,
            door_state="Closed",
            inside_temp=21.5,
            outside_temp=14.0,
            hvac_power="Off",
        )


def scenario_driving(
    vehicle_id: str, start: datetime
) -> Iterator[dict[str, Any]]:
    odometer = 12350.0
    battery = 70.0
    energy_rem = 52.5
    lifetime = 4130.0
    tpms = (3.05, 3.04, 3.02, 3.01)
    steps = [
        ("D", 12.0),
        ("D", 38.0),
        ("D", 62.0),
        ("D", 45.0),
        ("P", 0.0),
    ]
    for i, (gear, speed) in enumerate(steps):
        ts = start + timedelta(seconds=15 * i)
        if i > 0 and gear == "D":
            odometer += 0.4
            battery -= 0.3
            energy_rem -= 0.22
            lifetime += 0.18
        if gear == "P":
            speed = 0.0
        yield from _snapshot(
            vehicle_id,
            ts,
            odometer=round(odometer, 2),
            lifetime_energy=round(lifetime, 3),
            battery_level=round(battery, 2),
            energy_remaining=round(energy_rem, 2),
            charge_state="Idle",
            ac_energy=0.0,
            dc_energy=0.0,
            charge_limit=80,
            tpms=tpms,
            gear=gear,
            speed=speed,
            locked=True,
            door_state="Closed",
            inside_temp=22.0,
            outside_temp=13.5,
            hvac_power="On",
        )


def scenario_charging(
    vehicle_id: str, start: datetime
) -> Iterator[dict[str, Any]]:
    battery = 40.0
    energy_rem = 30.0
    ac_energy = 0.0
    for i in range(5):
        ts = start + timedelta(seconds=60 * i)
        charge_state = "Charging" if i < 4 else "Complete"
        if i > 0 and charge_state == "Charging":
            battery += 4.0
            energy_rem += 3.0
            ac_energy += 3.2
        if charge_state == "Complete":
            battery = 80.0
            energy_rem = 60.0
        yield from _snapshot(
            vehicle_id,
            ts,
            odometer=12400.0,
            lifetime_energy=4200.0,
            battery_level=round(battery, 2),
            energy_remaining=round(energy_rem, 2),
            charge_state=charge_state,
            ac_energy=round(ac_energy, 2),
            dc_energy=0.0,
            charge_limit=80,
            tpms=(2.9, 2.9, 2.85, 2.85),
            gear="P",
            speed=0.0,
            locked=True,
            door_state="Closed",
            inside_temp=20.0,
            outside_temp=12.0,
            hvac_power="Off",
        )


def scenario_tire_pressure(
    vehicle_id: str, start: datetime
) -> Iterator[dict[str, Any]]:
    pressures = [
        (2.90, 2.90, 2.85, 2.85),
        (2.88, 2.89, 2.84, 2.85),
        (2.70, 2.89, 2.84, 2.85),
        (2.55, 2.88, 2.83, 2.84),
    ]
    for i, tpms in enumerate(pressures):
        ts = start + timedelta(seconds=300 * i)
        yield from _snapshot(
            vehicle_id,
            ts,
            odometer=12410.0,
            lifetime_energy=4205.0,
            battery_level=68.0,
            energy_remaining=51.0,
            charge_state="Idle",
            ac_energy=0.0,
            dc_energy=0.0,
            charge_limit=80,
            tpms=tpms,
            gear="P",
            speed=0.0,
            locked=True,
            door_state="Closed",
            inside_temp=18.0,
            outside_temp=8.0,
            hvac_power="Off",
        )


def _emit(vehicle_id: str, ts: datetime, **fields: Any) -> Iterator[dict[str, Any]]:
    for name, value in fields.items():
        yield signal(vehicle_id, ts, name, value)


def scenario_day(
    vehicle_id: str,
    start: datetime,
    *,
    charge_kind: str = "AC",
) -> Iterator[dict[str, Any]]:
    """One vehicle, one day: overnight park, gear bump, two trips, then a charge."""
    odo = 10000.0
    lifetime = 5000.0
    soc = 65.0
    energy_rem = 48.75
    ac_energy = 10.0
    dc_energy = 4.0
    cold_tpms = (2.90, 2.91, 2.88, 2.87)
    warm_tpms = (3.10, 3.11, 3.08, 3.07)
    outside = 8.0

    def parked_fields(**extra: Any) -> dict[str, Any]:
        fl, fr, rl, rr = extra.pop("tpms", None) or (None, None, None, None)
        fields: dict[str, Any] = {
            "Odometer": round(odo, 3),
            "LifetimeEnergyUsed": round(lifetime, 3),
            "BatteryLevel": round(soc, 2),
            "EnergyRemaining": round(energy_rem, 3),
            "ChargeState": "Idle",
            "ACChargingEnergyIn": round(ac_energy, 3),
            "DCChargingEnergyIn": round(dc_energy, 3),
            "ChargeLimitSoc": 80,
            "Gear": "P",
            "VehicleSpeed": 0.0,
            "Locked": True,
            "DoorState": "Closed",
            "InsideTemp": 18.0,
            "OutsideTemp": outside,
            "HvacPower": "Off",
        }
        if fl is not None:
            fields["TpmsPressureFl"] = fl
            fields["TpmsPressureFr"] = fr
            fields["TpmsPressureRl"] = rl
            fields["TpmsPressureRr"] = rr
        fields.update(extra)
        return fields

    # Overnight park (8 h), full snapshot then a later still-parked tick.
    yield from _snapshot(
        vehicle_id,
        start,
        odometer=odo,
        lifetime_energy=lifetime,
        battery_level=soc,
        energy_remaining=energy_rem,
        charge_state="Idle",
        ac_energy=ac_energy,
        dc_energy=dc_energy,
        charge_limit=80,
        tpms=cold_tpms,
        gear="P",
        speed=0.0,
        locked=True,
        door_state="Closed",
        inside_temp=18.0,
        outside_temp=outside,
        hvac_power="Off",
    )
    yield from _emit(
        vehicle_id,
        start + timedelta(hours=4),
        **parked_fields(),
    )

    # Cold tire reading after >= 2 h in P, before the first real trip.
    t_cold = start + timedelta(hours=7, minutes=50)
    yield from _emit(
        vehicle_id,
        t_cold,
        **parked_fields(tpms=cold_tpms),
    )

    # 0.05 km gear bump (< 60 s) — must be ignored as a trip.
    t_bump = start + timedelta(hours=8)
    odo += 0.05
    lifetime += 0.01
    energy_rem -= 0.01
    soc -= 0.02
    yield from _emit(
        vehicle_id,
        t_bump,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        Gear="D",
        VehicleSpeed=5.0,
    )
    yield from _emit(
        vehicle_id,
        t_bump + timedelta(seconds=10),
        Gear="P",
        VehicleSpeed=0.0,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
    )

    # Morning trip ~12 km.
    t_m0 = start + timedelta(hours=8, minutes=5)
    t_m1 = start + timedelta(hours=8, minutes=17)
    t_m2 = start + timedelta(hours=8, minutes=30)
    outside = 10.0
    yield from _emit(
        vehicle_id,
        t_m0,
        Gear="D",
        VehicleSpeed=40.0,
        OutsideTemp=outside,
        HvacPower="On",
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        ChargeState="Idle",
    )
    odo += 6.0
    lifetime += 1.2
    energy_rem -= 1.2
    soc -= 2.5
    yield from _emit(
        vehicle_id,
        t_m1,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        VehicleSpeed=55.0,
        TpmsPressureFl=warm_tpms[0],
        TpmsPressureFr=warm_tpms[1],
        TpmsPressureRl=warm_tpms[2],
        TpmsPressureRr=warm_tpms[3],
    )
    odo += 6.0
    lifetime += 1.2
    energy_rem -= 1.2
    soc -= 2.5
    yield from _emit(
        vehicle_id,
        t_m2,
        Gear="P",
        VehicleSpeed=0.0,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        TpmsPressureFl=warm_tpms[0],
        TpmsPressureFr=warm_tpms[1],
        TpmsPressureRl=warm_tpms[2],
        TpmsPressureRr=warm_tpms[3],
    )

    # Warm reading within 30 minutes after the morning trip.
    yield from _emit(
        vehicle_id,
        t_m2 + timedelta(minutes=10),
        Gear="P",
        TpmsPressureFl=warm_tpms[0],
        TpmsPressureFr=warm_tpms[1],
        TpmsPressureRl=warm_tpms[2],
        TpmsPressureRr=warm_tpms[3],
        OutsideTemp=12.0,
    )

    # Afternoon trip ~30 km after a parked gap.
    t_a0 = start + timedelta(hours=14)
    t_a1 = start + timedelta(hours=14, minutes=25)
    t_a2 = start + timedelta(hours=14, minutes=50)
    outside = 16.0
    yield from _emit(
        vehicle_id,
        t_a0,
        Gear="D",
        VehicleSpeed=50.0,
        OutsideTemp=outside,
        HvacPower="On",
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
    )
    odo += 15.0
    lifetime += 2.85
    energy_rem -= 2.85
    soc -= 6.0
    yield from _emit(
        vehicle_id,
        t_a1,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        VehicleSpeed=70.0,
        TpmsPressureFl=warm_tpms[0],
        TpmsPressureFr=warm_tpms[1],
        TpmsPressureRl=warm_tpms[2],
        TpmsPressureRr=warm_tpms[3],
    )
    odo += 15.0
    lifetime += 2.85
    energy_rem -= 2.85
    soc -= 6.0
    yield from _emit(
        vehicle_id,
        t_a2,
        Gear="P",
        VehicleSpeed=0.0,
        Odometer=round(odo, 3),
        LifetimeEnergyUsed=round(lifetime, 3),
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
    )

    # Charge ~20 kWh (AC or DC depending on variant).
    t_c0 = start + timedelta(hours=15, minutes=10)
    charge_name = "DCChargingEnergyIn" if charge_kind == "DC" else "ACChargingEnergyIn"
    energy = dc_energy if charge_kind == "DC" else ac_energy
    yield from _emit(
        vehicle_id,
        t_c0,
        ChargeState="Charging",
        Gear="P",
        VehicleSpeed=0.0,
        BatteryLevel=round(soc, 2),
        EnergyRemaining=round(energy_rem, 3),
        ACChargingEnergyIn=round(ac_energy, 3),
        DCChargingEnergyIn=round(dc_energy, 3),
    )
    for i in range(1, 5):
        ts = t_c0 + timedelta(minutes=30 * i)
        energy += 5.0
        soc += 7.5
        energy_rem += 5.0
        payload: dict[str, Any] = {
            "ChargeState": "Charging" if i < 4 else "Complete",
            "BatteryLevel": round(soc, 2),
            "EnergyRemaining": round(energy_rem, 3),
            charge_name: round(energy, 3),
        }
        yield from _emit(vehicle_id, ts, **payload)


def scenario_dc_day(vehicle_id: str, start: datetime) -> Iterator[dict[str, Any]]:
    yield from scenario_day(vehicle_id, start, charge_kind="DC")


GENERATORS = {
    "parked": scenario_parked,
    "driving": scenario_driving,
    "charging": scenario_charging,
    "tire_pressure": scenario_tire_pressure,
    "day": scenario_day,
    "dc_day": scenario_dc_day,
}


def generate(
    scenario: str,
    vehicle_id: str = DEFAULT_VEHICLE_ID,
    start: datetime | None = None,
) -> list[dict[str, Any]]:
    if scenario not in GENERATORS:
        raise ValueError(f"unknown scenario: {scenario}")
    if start is None:
        if scenario in ("day", "dc_day"):
            start = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
        else:
            start = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    return list(GENERATORS[scenario](vehicle_id, start))


def write_signals(signals: Iterable[dict[str, Any]], out) -> None:
    for row in signals:
        out.write(json.dumps(row, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Tesla Fleet Telemetry-like JSON signals (NDJSON)."
    )
    parser.add_argument(
        "scenario",
        choices=SCENARIOS,
        help="parked | driving | charging | tire_pressure | day | dc_day",
    )
    parser.add_argument(
        "--vehicle-id",
        default=DEFAULT_VEHICLE_ID,
        help="vehicle_id stored on every signal",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="write NDJSON to this file (default: stdout)",
    )
    args = parser.parse_args(argv)
    signals = generate(args.scenario, args.vehicle_id)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            write_signals(signals, fh)
    else:
        write_signals(signals, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
