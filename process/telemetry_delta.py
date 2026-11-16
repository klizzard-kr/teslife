"""Change only VehicleSpeed and the four unused motion fields in a telemetry config.

The Fleet API replaces a vehicle's whole telemetry config. This module never
builds that config from a template. It copies a config the caller already
fetched and changes five fields:

- VehicleSpeed interval_seconds becomes 1. minimum_delta and any other keys stay.
- LongitudinalAcceleration, LateralAcceleration, PedalPosition, BrakePedal are removed.

Nothing here connects to the network, calls the Fleet API, or wakes the car. The printed
summary does not include certificates, VINs, hostnames, or tokens.

    python -m process.telemetry_delta --current current.json --out next.json
    python -m process.telemetry_delta --current current.json --verify after.json
    python -m process.telemetry_delta --review-sender send_telemetry_config.mts

next.json is as sensitive as the fetched config. Do not commit or paste it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SPEED_FIELD = "VehicleSpeed"
DROP_FIELDS = (
    "LongitudinalAcceleration",
    "LateralAcceleration",
    "PedalPosition",
    "BrakePedal",
)
SPEED_INTERVAL_S = 1
_SECRET_KEYS = {"vin", "vins", "token", "authorization", "access_token", "private_key"}

_PEM = re.compile(r"-----BEGIN [A-Z ]+-----")
_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


class DeltaError(ValueError):
    pass


def extract_config(doc: Any) -> dict[str, Any]:
    """Accept a GET envelope, a POST body, or the config object itself."""
    if not isinstance(doc, dict):
        raise DeltaError("config JSON must be an object")
    if isinstance(doc.get("response"), dict) and isinstance(doc["response"].get("config"), dict):
        return doc["response"]["config"]
    if isinstance(doc.get("config"), dict) and "fields" in doc["config"]:
        return doc["config"]
    if "fields" in doc:
        return doc
    raise DeltaError("fields not found; pass the fleet_telemetry_config GET body")


def apply_delta(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with only the five approved field differences."""
    if not isinstance(config, dict):
        raise DeltaError("config must be an object")
    fields = config.get("fields")
    if not isinstance(fields, dict):
        raise DeltaError("config.fields must be an object, not a rebuilt list or template")
    out = json.loads(json.dumps(config))
    copied = out["fields"]
    for name in DROP_FIELDS:
        copied.pop(name, None)
    if SPEED_FIELD not in copied:
        copied[SPEED_FIELD] = {"interval_seconds": SPEED_INTERVAL_S}
    else:
        spec = copied[SPEED_FIELD]
        if not isinstance(spec, dict):
            raise DeltaError("VehicleSpeed must be an object")
        spec["interval_seconds"] = SPEED_INTERVAL_S
    for key in list(out):
        if key.lower() in _SECRET_KEYS:
            del out[key]
    return out


def _num(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return False


def same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _num(a, b):
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Names of differing paths. Values are not returned."""
    if same(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in list(before) + [k for k in after if k not in before]:
            nxt = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(nxt)
            else:
                paths.extend(changed_paths(before[key], after[key], nxt))
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        paths = []
        for i, (x, y) in enumerate(zip(before, after)):
            paths.extend(changed_paths(x, y, f"{prefix}[{i}]"))
        return paths
    return [prefix or "$"]


def _interval(spec: Any) -> str:
    if not isinstance(spec, dict) or "interval_seconds" not in spec:
        return "absent"
    return str(spec["interval_seconds"])


def _delta(spec: Any) -> str:
    if not isinstance(spec, dict) or "minimum_delta" not in spec:
        return "absent"
    return str(spec["minimum_delta"])


def _key_state(before: dict, after: dict, key: str) -> str:
    if key not in before and key not in after:
        return "absent"
    return "yes" if same(before.get(key), after.get(key)) else "no"


def public_report(before: dict[str, Any], after: dict[str, Any]) -> str:
    bf = before["fields"]
    af = after["fields"]
    removed = [name for name in DROP_FIELDS if name in bf]
    already = [name for name in DROP_FIELDS if name not in bf]
    lines = [
        f"fields_before={len(bf)}",
        f"fields_after={len(af)}",
        "removed=" + (",".join(removed) if removed else "none"),
        "absent_already=" + (",".join(already) if already else "none"),
        f"vehicle_speed_interval={_interval(bf.get(SPEED_FIELD))}->{_interval(af.get(SPEED_FIELD))}",
        f"vehicle_speed_minimum_delta={_delta(bf.get(SPEED_FIELD))}->{_delta(af.get(SPEED_FIELD))}",
        f"other_field_count={sum(1 for name in af if name != SPEED_FIELD)}",
        f"hostname_unchanged={_key_state(before, after, 'hostname')}",
        f"ca_unchanged={_key_state(before, after, 'ca')}",
        f"port_unchanged={_key_state(before, after, 'port')}",
        f"alert_types_unchanged={_key_state(before, after, 'alert_types')}",
        f"prefer_typed_unchanged={_key_state(before, after, 'prefer_typed')}",
        "network=no",
        "wake=no",
    ]
    text = "\n".join(lines) + "\n"
    if _PEM.search(text) or _VIN.search(text):
        raise DeltaError("summary would have included a certificate or VIN")
    return text


def review_sender(text: str) -> dict[str, str]:
    """Classify a sender script without printing its source.

    passthrough: it posts a JSON body it read, and does not build the field map.
    rebuild: it builds fields (speedf, a fields literal, or telemetry-config.json).
    wake: it mentions a wake call. Do not run it for this change.
    unknown: preservation of the other fields is not shown.
    """
    lowered = text.lower()
    wake = "wake_up" in lowered or "wakeup(" in lowered or "/wake" in lowered
    builds = any(mark in text for mark in (
        "speedf(", "build_telemetry_config", "telemetry-config.json",
    )) or re.search(r"fields\s*[:=]\s*\{", text) is not None
    reads_json = any(mark in text for mark in ("readFile", "JSON.parse", "json.load"))
    posts = "fleet_telemetry_config" in text
    if wake:
        verdict = "wake"
    elif builds:
        verdict = "rebuild"
    elif posts and reads_json:
        verdict = "passthrough"
    else:
        verdict = "unknown"
    return {
        "sender_verdict": verdict,
        "sender_wake": "yes" if wake else "no",
        "sender_builds_fields": "yes" if builds else "no",
    }


def _load(path: str) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeltaError(f"cannot read config JSON: {exc.__class__.__name__}") from exc
    return extract_config(doc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the five-field telemetry delta offline.")
    parser.add_argument("--current", help="GET body or config JSON. Not sent anywhere.")
    parser.add_argument("--out", help="write the patched config object")
    parser.add_argument("--verify", help="GET body fetched after a send; compare with the delta of --current")
    parser.add_argument("--review-sender", help="classify a local sender script without running it")
    args = parser.parse_args(argv)
    try:
        if args.review_sender:
            text = Path(args.review_sender).read_text(encoding="utf-8")
            for key, value in review_sender(text).items():
                print(f"{key}={value}")
            return 0
        if not args.current:
            parser.error("--current is required unless --review-sender is set")
        before = _load(args.current)
        after = apply_delta(before)
        sys.stdout.write(public_report(before, after))
        if args.out:
            Path(args.out).write_text(json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("wrote_out=yes")
        if args.verify:
            got = _load(args.verify)
            paths = changed_paths(after, got)
            if paths:
                print("verify=no")
                for path in paths:
                    print(f"differs={path}")
                return 2
            synced = None
            raw = json.loads(Path(args.verify).read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("response"), dict) and "synced" in raw["response"]:
                synced = raw["response"]["synced"]
            print(f"verify=yes")
            print(f"synced={'yes' if synced is True else 'no' if synced is False else 'absent'}")
            print("synced_means_received=no")
        return 0
    except DeltaError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
