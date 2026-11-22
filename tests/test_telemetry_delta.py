"""Offline telemetry delta: five fields only, no network, no secrets in the summary."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from process.telemetry_delta import (
    CLIMATE_FIELDS,
    DROP_FIELDS,
    apply_delta,
    extract_config,
    main,
    public_report,
    review_sender,
)

CA = "-----BEGIN CERTIFICATE-----\nFAKECERTDATA\n-----END CERTIFICATE-----\n"
VIN = "VINTEST0000000001"
HOST = "telemetry.example.test"


def live_config(*, speed_interval: int, with_motion: bool = True) -> dict:
    fields = {
        "Odometer": {"interval_seconds": 60, "minimum_delta": 0.1},
        "Gear": {"interval_seconds": 1},
        "VehicleSpeed": {"interval_seconds": speed_interval, "minimum_delta": 1, "resend_interval_seconds": 30},
        "Location": {"interval_seconds": 10, "minimum_delta": 50},
        "BatteryLevel": {"interval_seconds": 60},
        "SentryMode": {"interval_seconds": 10},
    }
    if with_motion:
        fields["LongitudinalAcceleration"] = {"interval_seconds": 1, "minimum_delta": 0.1}
        fields["LateralAcceleration"] = {"interval_seconds": 1, "minimum_delta": 0.1}
        fields["PedalPosition"] = {"interval_seconds": 1}
        fields["BrakePedal"] = {"interval_seconds": 1}
    return {
        "hostname": HOST,
        "port": 443,
        "ca": CA,
        "prefer_typed": True,
        "alert_types": ["service"],
        "fields": fields,
    }


class DeltaTests(unittest.TestCase):
    def test_speedf5_shape_keeps_every_other_field(self) -> None:
        # A fixture shaped like a 5-second speed field. Not a claim about the car.
        before = live_config(speed_interval=5)
        after = apply_delta(before)
        self.assertEqual(before["fields"]["VehicleSpeed"]["interval_seconds"], 5)
        self.assertEqual(after["fields"]["VehicleSpeed"]["interval_seconds"], 1)
        self.assertEqual(after["fields"]["VehicleSpeed"]["minimum_delta"], 1)
        self.assertEqual(after["fields"]["VehicleSpeed"]["resend_interval_seconds"], 30)
        for name in DROP_FIELDS:
            self.assertNotIn(name, after["fields"])
            self.assertIn(name, before["fields"])
        for name in ("Odometer", "Location", "BatteryLevel", "SentryMode"):
            self.assertEqual(after["fields"][name], before["fields"][name])
        self.assertEqual(after["fields"]["Gear"]["interval_seconds"], 1)
        for name, spec in CLIMATE_FIELDS.items():
            self.assertEqual(after["fields"][name], spec)
        self.assertEqual(after["ca"], CA)
        self.assertEqual(after["hostname"], HOST)
        self.assertEqual(after["alert_types"], ["service"])
        self.assertTrue(after["prefer_typed"])
        self.assertEqual(list(after["fields"]), [
            "Odometer", "Gear", "VehicleSpeed", "Location", "BatteryLevel", "SentryMode",
            *CLIMATE_FIELDS,
        ])

    def test_documented_two_second_shape_is_the_same_delta(self) -> None:
        # The old repo sentence said 2 seconds and 1 mph. Also not a live reading.
        after = apply_delta(live_config(speed_interval=2))
        self.assertEqual(after["fields"]["VehicleSpeed"]["interval_seconds"], 1)
        self.assertEqual(after["fields"]["VehicleSpeed"]["minimum_delta"], 1)

    def test_already_one_second_and_no_motion_fields_is_unchanged(self) -> None:
        before = live_config(speed_interval=1, with_motion=False)
        after = apply_delta(before)
        self.assertEqual(after["fields"]["VehicleSpeed"], before["fields"]["VehicleSpeed"])
        self.assertEqual(after["fields"]["Gear"], before["fields"]["Gear"])
        for name, spec in CLIMATE_FIELDS.items():
            self.assertEqual(after["fields"][name], spec)
            self.assertNotIn(name, before["fields"])

    def test_missing_speed_is_added_without_inventing_a_delta(self) -> None:
        before = live_config(speed_interval=2, with_motion=False)
        del before["fields"]["VehicleSpeed"]
        after = apply_delta(before)
        self.assertEqual(after["fields"]["VehicleSpeed"], {"interval_seconds": 1})
        self.assertEqual(after["fields"]["Location"], before["fields"]["Location"])

    def test_summary_hides_certificate_hostname_and_vin(self) -> None:
        before = live_config(speed_interval=5)
        before["vins"] = [VIN]
        text = public_report(extract_config({"response": {"config": before, "synced": False}}), apply_delta(before))
        self.assertNotIn("BEGIN CERTIFICATE", text)
        self.assertNotIn(HOST, text)
        self.assertNotIn(VIN, text)
        self.assertIn("vehicle_speed_interval=5->1", text)
        self.assertIn("vehicle_speed_minimum_delta=1->1", text)
        self.assertIn("removed=LongitudinalAcceleration,LateralAcceleration,PedalPosition,BrakePedal", text)
        self.assertIn("ca_unchanged=yes", text)
        self.assertIn("wake=no", text)
        self.assertIn("network=no", text)

    def test_list_fields_are_refused(self) -> None:
        with self.assertRaises(Exception):
            apply_delta({"fields": [{"field": "VehicleSpeed", "interval_seconds": 5}]})


class CliTests(unittest.TestCase):
    def test_out_and_verify_round_trip(self) -> None:
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        current = Path(folder.name) / "current.json"
        out = Path(folder.name) / "next.json"
        after = Path(folder.name) / "after.json"
        envelope = {"response": {"synced": False, "config": live_config(speed_interval=5), "limit_reached": False}}
        envelope["response"]["config"]["vins"] = [VIN]
        current.write_text(json.dumps(envelope), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--current", str(current), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertNotIn(CA, buf.getvalue())
        self.assertNotIn(VIN, buf.getvalue())
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["ca"], CA)
        self.assertNotIn("vins", written)
        self.assertNotIn("LongitudinalAcceleration", written["fields"])
        fetched = {"response": {"synced": False, "config": written}}
        after.write_text(json.dumps(fetched), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--current", str(current), "--verify", str(after)])
        self.assertEqual(code, 0)
        self.assertIn("verify=yes", buf.getvalue())
        self.assertIn("synced=no", buf.getvalue())
        self.assertIn("synced_means_received=no", buf.getvalue())

    def test_verify_rejects_another_field_changing(self) -> None:
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        current = Path(folder.name) / "current.json"
        after = Path(folder.name) / "after.json"
        cfg = live_config(speed_interval=5)
        current.write_text(json.dumps(cfg), encoding="utf-8")
        bad = apply_delta(cfg)
        bad["fields"]["Gear"]["interval_seconds"] = 9
        bad["ca"] = "different"
        after.write_text(json.dumps({"response": {"config": bad}}), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--current", str(current), "--verify", str(after)])
        self.assertEqual(code, 2)
        text = buf.getvalue()
        self.assertIn("differs=ca", text)
        self.assertIn("differs=fields.Gear.interval_seconds", text)
        self.assertNotIn("different", text)
        self.assertNotIn(CA, text)

    def test_module_source_has_no_network_or_wake(self) -> None:
        src = Path(__file__).resolve().parents[1].joinpath("process/telemetry_delta.py").read_text(encoding="utf-8")
        for banned in ("import urllib", "import socket", "http.client", "import requests", "/wake_up"):
            self.assertNotIn(banned, src)


class SenderReviewTests(unittest.TestCase):
    def test_speedf_builder_is_a_rebuild(self) -> None:
        text = 'const fields = { VehicleSpeed: speedf(5), Gear: f(1) };\npost("fleet_telemetry_config", {config});\n'
        got = review_sender(text)
        self.assertEqual(got["sender_verdict"], "rebuild")
        self.assertEqual(got["sender_wake"], "no")

    def test_template_file_is_a_rebuild(self) -> None:
        text = 'const body = JSON.parse(fs.readFileSync("telemetry-config.json"));\nfetch(base + "/api/1/vehicles/fleet_telemetry_config");\n'
        self.assertEqual(review_sender(text)["sender_verdict"], "rebuild")

    def test_prepared_body_without_wake_can_pass_through(self) -> None:
        text = 'const body = JSON.parse(fs.readFileSync(process.argv[2]));\nfetch(base + "/api/1/vehicles/fleet_telemetry_config", {method:"POST", body: JSON.stringify(body)});\n'
        got = review_sender(text)
        self.assertEqual(got, {
            "sender_verdict": "passthrough",
            "sender_wake": "no",
            "sender_builds_fields": "no",
        })

    def test_wake_is_refused(self) -> None:
        text = 'await fetch(`/api/1/vehicles/${vin}/wake_up`, {method:"POST"});\n'
        self.assertEqual(review_sender(text)["sender_verdict"], "wake")

    def test_review_cli_does_not_print_the_script(self) -> None:
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "send_telemetry_config.mts"
        secret = "SECRET-TOKEN-VALUE"
        path.write_text(
            f'const token = "{secret}";\nconst fields = {{ VehicleSpeed: speedf(5) }};\n',
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--review-sender", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("sender_verdict=rebuild", buf.getvalue())
        self.assertNotIn(secret, buf.getvalue())
        self.assertNotIn("speedf(5)", buf.getvalue())
