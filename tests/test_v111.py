"""v1.11.0: Naver map fallback, reverse geocode, place search, telemetry dry-run, release notes."""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from process.evcharger import region_from_point
from process.geocode import NaverGeocoder, from_env
from process.naver_maps import area_name, clear_area_cache, place_search, strip_title
from process.regions import query_prefix, zscode_from_name
from process.send_telemetry import main as send_main
from process.telemetry_delta import review_sender

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
SENDER = (ROOT / "process" / "send_telemetry.py").read_text(encoding="utf-8")

DANGJIN = {
    "status": {"code": 0, "name": "ok"},
    "results": [{
        "name": "admcode",
        "region": {
            "area1": {"name": "충청남도"},
            "area2": {"name": "당진시"},
            "area3": {"name": "예시읍"},
        },
    }],
}


class _Body:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _headers(req) -> dict[str, str]:
    return {key.lower(): value for key, value in req.header_items()}


class RegionNameTests(unittest.TestCase):
    def test_sigungu_name_maps_to_one_code(self) -> None:
        self.assertEqual(zscode_from_name("당진시"), "44270")
        self.assertEqual(zscode_from_name("당진"), "44270")
        self.assertEqual(zscode_from_name("부천시"), "41190")
        self.assertEqual(zscode_from_name("서울특별시 중구"), "11140")
        self.assertIsNone(zscode_from_name("중구"))
        self.assertEqual(query_prefix("44270"), "당진")
        self.assertEqual(region_from_point(0, 0, admin_name="당진시"), ("44", "44270"))
        self.assertNotIn("동", query_prefix("44270"))

    def test_polygon_still_wins_without_a_name(self) -> None:
        self.assertEqual(region_from_point(36.80, 126.65), ("44", "44270"))
        self.assertEqual(region_from_point(37.503, 126.766), ("41", "41190"))


class ReverseTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_area_cache()
        os.environ["NAVER_MAPS_CLIENT_ID"] = "map-id"
        os.environ["NAVER_MAPS_CLIENT_SECRET"] = "map-secret"
        self.calls: list[str] = []

    def tearDown(self) -> None:
        clear_area_cache()
        os.environ.pop("NAVER_MAPS_CLIENT_ID", None)
        os.environ.pop("NAVER_MAPS_CLIENT_SECRET", None)
        os.environ.pop("TESLIFE_GEOCODER", None)

    def _opener(self, fail_times: int = 0, nominatim_city: str | None = None):
        state = {"left": fail_times}

        def opener(req, timeout=8):
            url = req.full_url
            kind = "nominatim" if "nominatim" in url else "naver"
            self.calls.append(kind)
            headers = _headers(req)
            if kind == "naver":
                if "x-ncp-apigw-api-key-id" not in headers or "x-ncp-apigw-api-key" not in headers:
                    raise AssertionError("map header")
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                lon_s, lat_s = query["coords"][0].split(",")
                if not lon_s.startswith("126.") or not lat_s.startswith("36."):
                    raise AssertionError("coords order")
                if "orders" not in query or query["orders"] != ["admcode,legalcode"]:
                    raise AssertionError("orders")
                if state["left"] > 0:
                    state["left"] -= 1
                    raise urllib.error.URLError("down")
                return _Body(DANGJIN)
            return _Body({"address": {"city": nominatim_city or "부천시", "suburb": "예시읍"}})

        return opener

    def test_retry_then_cache(self) -> None:
        opener = self._opener(fail_times=1)
        self.assertEqual(area_name(36.80, 126.65, opener), "당진시")
        self.assertEqual(self.calls, ["naver", "naver"])
        self.assertEqual(region_from_point(36.80, 126.65, opener=opener), ("44", "44270"))
        self.assertEqual(self.calls, ["naver", "naver"])

    def test_nominatim_after_naver_fails(self) -> None:
        opener = self._opener(fail_times=2, nominatim_city="부천시")
        self.assertEqual(region_from_point(36.80, 126.65, opener=opener), ("41", "41190"))
        self.assertEqual(self.calls, ["naver", "naver", "nominatim"])

    def test_label_is_the_dong(self) -> None:
        geo = NaverGeocoder(opener=self._opener())
        self.assertEqual(geo.reverse(36.80, 126.65), "예시읍")

    def test_from_env_uses_naver_only_when_keys_exist(self) -> None:
        self.assertIsInstance(from_env(), NaverGeocoder)
        os.environ.pop("NAVER_MAPS_CLIENT_ID")
        os.environ.pop("NAVER_MAPS_CLIENT_SECRET")
        os.environ["TESLIFE_GEOCODER"] = "nominatim"
        self.assertEqual(type(from_env()).__name__, "NominatimGeocoder")


class PlaceSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["NAVER_SEARCH_CLIENT_ID"] = "search-id"
        os.environ["NAVER_SEARCH_CLIENT_SECRET"] = "search-secret"
        self.queries: list[str] = []

    def tearDown(self) -> None:
        os.environ.pop("NAVER_SEARCH_CLIENT_ID", None)
        os.environ.pop("NAVER_SEARCH_CLIENT_SECRET", None)

    def test_nearest_five_titles_without_coordinates(self) -> None:
        def opener(req, timeout=8):
            headers = _headers(req)
            if headers.get("x-ncp-apigw-api-key-id") != "search-id":
                raise AssertionError("search header")
            if headers.get("x-ncp-apigw-api-key") != "search-secret":
                raise AssertionError("search header")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            if query.get("display") != ["5"] or query.get("format") != ["json"]:
                raise AssertionError("search query")
            self.queries.append(query["query"][0])
            return _Body({"items": [
                {"title": "<b>멀리</b> 있는 곳", "mapx": "1270000000", "mapy": "375000000"},
                {"title": "<b>한빛</b>캐슬", "mapx": "1266500000", "mapy": "368000000", "category": "부동산>오피스텔"},
                {"title": "세 번째", "mapx": "1267000000", "mapy": "369000000"},
                {"title": "네 번째", "mapx": "1268000000", "mapy": "370000000"},
                {"title": "다섯째", "mapx": "1269000000", "mapy": "371000000"},
                {"title": "<b>여섯째</b>", "mapx": "1265000000", "mapy": "368000000"},
            ]})

        out = place_search("한빛캐슬", 36.80, 126.65, opener)
        self.assertEqual(self.queries, ["당진 한빛캐슬"])
        self.assertEqual(out["items"][0]["title"], "한빛캐슬")
        self.assertEqual(out["items"][0]["category"], "오피스텔")
        self.assertLessEqual(len(out["items"]), 5)
        self.assertNotIn("<b>", json.dumps(out))
        self.assertNotIn("mapx", json.dumps(out))
        self.assertNotIn("search-secret", json.dumps(out))
        self.assertEqual(strip_title("<b>한빛</b>캐슬"), "한빛캐슬")

    def test_missing_keys_do_not_call_out(self) -> None:
        os.environ.pop("NAVER_SEARCH_CLIENT_ID")
        os.environ.pop("NAVER_SEARCH_CLIENT_SECRET")
        called = []

        def opener(req, timeout=8):
            called.append(1)
            return _Body({})

        out = place_search("한빛캐슬", 36.80, 126.65, opener)
        self.assertEqual(out["available"], False)
        self.assertEqual(called, [])
        self.assertEqual(out["items"], [])


class PageTests(unittest.TestCase):
    def test_version_notes_and_place_field(self) -> None:
        self.assertIn('const APP_VERSION = "v1.21.0"', HTML)
        self.assertIn("ncpKeyId=", HTML)
        self.assertIn("loadLeaflet()", HTML)
        self.assertIn('id="place-clear"', HTML)
        self.assertIn('id="notes-overlay"', HTML)
        self.assertIn("RELEASE_NOTES", HTML)
        self.assertLess(HTML.index('version: "1.11.0"'), HTML.index('version: "초기 버전"'))
        self.assertIn("새 기능", HTML)
        self.assertIn("고친 점", HTML)
        listener = HTML.split("station_id.addEventListener", 1)[1].split("syncPlaceClear();", 1)[0]
        self.assertNotIn("location_text.value", listener)
        self.assertIn("/api/place-suggest", HTML)
        self.assertIn("place-tag", HTML)
        self.assertNotIn("NAVER_SEARCH_CLIENT_SECRET", HTML)
        self.assertNotIn("NAVER_MAPS_CLIENT_SECRET", HTML)

    def test_map_config_sends_only_the_public_id(self) -> None:
        from web import map_config

        os.environ["NAVER_MAPS_CLIENT_ID"] = "public-map-id"
        os.environ["NAVER_MAPS_CLIENT_SECRET"] = "hidden-map-secret"
        os.environ["NAVER_SEARCH_CLIENT_SECRET"] = "hidden-search-secret"
        try:
            data = map_config()
        finally:
            os.environ.pop("NAVER_MAPS_CLIENT_ID", None)
            os.environ.pop("NAVER_MAPS_CLIENT_SECRET", None)
            os.environ.pop("NAVER_SEARCH_CLIENT_SECRET", None)
        self.assertEqual(data["ncpKeyId"], "public-map-id")
        self.assertEqual(data["default"], "naver")
        blob = json.dumps(data)
        self.assertNotIn("hidden-map-secret", blob)
        self.assertNotIn("hidden-search-secret", blob)


class TelemetryDryRunTests(unittest.TestCase):
    def test_sender_is_a_passthrough_and_dry_run_is_the_default(self) -> None:
        verdict = review_sender(SENDER)
        self.assertEqual(verdict["sender_verdict"], "passthrough")
        self.assertEqual(verdict["sender_wake"], "no")
        self.assertEqual(verdict["sender_builds_fields"], "no")
        posted: list = []

        def post(url, body):
            posted.append((url, body))

        text = _capture(lambda: send_main([], {"post": post}))
        self.assertIn("sent=no", text)
        self.assertEqual(posted, [])

    def test_sample_dry_run_changes_only_the_five_fields(self) -> None:
        text = _capture(lambda: send_main([]))
        self.assertIn("dry_run=yes", text)
        self.assertIn("sent=no", text)
        self.assertIn("network=no", text)
        self.assertIn("wake=no", text)
        self.assertIn("vehicle_speed_interval=10->1", text)
        for name in ("LongitudinalAcceleration", "LateralAcceleration", "PedalPosition", "BrakePedal"):
            self.assertIn(name, text.split("removed=", 1)[1].split("\n", 1)[0])
        self.assertIn("hostname_unchanged=yes", text)
        self.assertIn("ca_unchanged=yes", text)
        self.assertNotIn("BEGIN ", text)

    def test_send_posts_the_delta_and_a_wake_url_is_refused(self) -> None:
        posted: list = []

        def post(url, body):
            posted.append(body)

        code = _capture(lambda: send_main(
            ["--send", "--url", "https://example.test/fleet_telemetry_config"], {"post": post}))
        self.assertIn("sent=yes", code)
        self.assertEqual(len(posted), 1)
        fields = posted[0]["fields"]
        self.assertEqual(fields["VehicleSpeed"]["interval_seconds"], 1)
        self.assertEqual(fields["VehicleSpeed"]["minimum_delta"], 0.1)
        self.assertEqual(fields["BatteryLevel"]["interval_seconds"], 60)
        for name in ("LongitudinalAcceleration", "LateralAcceleration", "PedalPosition", "BrakePedal"):
            self.assertNotIn(name, fields)
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            refused = send_main(["--send", "--url", "https://example.test/rejected-wake"], {"post": post})
        self.assertEqual(refused, 1)
        self.assertIn("error=", err.getvalue())
        self.assertEqual(len(posted), 1)


def _capture(fn) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn()
    if code not in (0, None):
        raise AssertionError("command failed")
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
