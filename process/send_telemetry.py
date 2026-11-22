"""Apply the telemetry delta and, only with --send, post that copy.

The default is a dry run: read a config the caller already has, keep every
field that is not part of the delta, and print the difference. Nothing is
posted and the car is not contacted. --send is the only flag that posts, and
it posts the patched config to the URL the caller passes. A URL that mentions
a wake call is refused.

    python -m process.send_telemetry
    python -m process.send_telemetry --current current.json
    python -m process.send_telemetry --current current.json --send --url https://example/fleet_telemetry_config

The printed lines are counts and field names. Certificates, tokens, and VINs
are not printed. Do not commit a fetched config.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

from process.telemetry_delta import (
    DeltaError,
    apply_delta,
    changed_paths,
    extract_config,
    public_report,
)

SAMPLE: dict[str, Any] = {
    "hostname": "telemetry.example",
    "port": 443,
    "ca": "kept",
    "alert_types": ["service"],
    "prefer_typed": True,
    "fields": {
        "VehicleSpeed": {"interval_seconds": 10, "minimum_delta": 0.1},
        "LongitudinalAcceleration": {"interval_seconds": 1},
        "LateralAcceleration": {"interval_seconds": 1},
        "PedalPosition": {"interval_seconds": 1},
        "BrakePedal": {"interval_seconds": 1},
        "BatteryLevel": {"interval_seconds": 60},
    },
}


class SendError(DeltaError):
    pass


def _load_current(path: str | None) -> dict[str, Any]:
    if not path:
        return json.loads(json.dumps(SAMPLE))
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SendError(f"cannot read config JSON: {exc.__class__.__name__}") from exc


def _http_get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def _http_post(url: str, body: dict[str, Any]) -> None:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20):
        return None


def prepare(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    before = extract_config(current)
    after = apply_delta(before)
    report = public_report(before, after)
    return before, after, report


def _lines(before: dict[str, Any], after: dict[str, Any], report: str, *,
           dry_run: bool, sent: bool, fetched: bool) -> str:
    extra = [
        "dry_run=" + ("yes" if dry_run else "no"),
        "sent=" + ("yes" if sent else "no"),
        "fetched=" + ("yes" if fetched else "no"),
        "network=" + ("post" if sent else "get" if fetched else "no"),
        "wake=no",
    ]
    for path in changed_paths(before, after):
        extra.append(f"changed={path}")
    text = report + "\n".join(extra) + "\n"
    return text


def main(argv: list[str] | None = None, transport: dict[str, Callable[..., Any]] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run the telemetry delta. Post only with --send.")
    parser.add_argument("--current", help="GET body already on disk. No network.")
    parser.add_argument("--fetch", action="store_true", help="GET --url. Still does not post.")
    parser.add_argument("--send", action="store_true", help="POST the delta to --url. This is the only send.")
    parser.add_argument("--url", help="fleet_telemetry_config URL. Required for --fetch and --send.")
    args = parser.parse_args(argv)
    get = (transport or {}).get("get", _http_get)
    post = (transport or {}).get("post", _http_post)
    try:
        if args.url and "wake" in args.url.lower():
            raise SendError("url is not a config endpoint")
        if (args.fetch or args.send) and not args.url:
            raise SendError("--url is required to fetch or send")
        if args.send and args.fetch and not args.url:
            raise SendError("--url is required to fetch or send")
        fetched = False
        if args.fetch:
            current = get(args.url)
            fetched = True
        else:
            current = _load_current(args.current)
        before, after, report = prepare(current)
        if not args.send:
            sys.stdout.write(_lines(before, after, report, dry_run=True, sent=False, fetched=fetched))
            return 0
        post(args.url, after)
        sys.stdout.write(_lines(before, after, report, dry_run=False, sent=True, fetched=fetched))
        return 0
    except (SendError, DeltaError) as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
