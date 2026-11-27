"""v1.14.6: /api/place-suggest is a real route, and the place field can suggest again."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from process import PROCESS_VERSION, connect
from process.evcharger import ensure_schema as ensure_charger_schema
from web import MENU_PATHS, WRITE_PATHS, make_server, serve_in_thread

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
WEB = (ROOT / "web" / "__init__.py").read_text(encoding="utf-8")
VID = "car-suggest"

_TEMPLATES = (
    re.compile(r"^/api/trips/[^/]+/power$"),
    re.compile(r"^/api/trips/[^/]+/speed$"),
    re.compile(r"^/api/trips/[^/]+/climate$"),
    re.compile(r"^/api/trips/[^/]+/route$"),
    re.compile(r"^/api/charges/[^/]+/curve$"),
)


def _frontend_api_paths(html: str) -> set[str]:
    quoted = set(re.findall(r'["\'](/api/[A-Za-z0-9_./-]+)(?:\?[^"\']*)?["\']', html))
    built: set[str] = set()
    prefixes: set[str] = set()
    for prefix, suffix in re.findall(
        r'["\'](/api/[A-Za-z0-9_./-]*/)["\']\s*\+\s*[^+\n;]{1,160}?\s*\+\s*["\'](/[A-Za-z0-9_./-]+)["\']',
        html,
    ):
        prefixes.add(prefix)
        built.add(prefix + "id" + suffix)
    return (quoted - prefixes) | built


def _registered_paths() -> set[str]:
    exact = set(re.findall(r'path == "(/api/[^"]+)"', WEB))
    exact |= set(MENU_PATHS)
    for paths in WRITE_PATHS.values():
        exact |= set(paths)
    return exact


def _is_registered(path: str, exact: set[str]) -> bool:
    if path in exact:
        return True
    sample = path.replace("id", "sample", 1)
    return any(pattern.fullmatch(sample) for pattern in _TEMPLATES)


class RouteRegistrationTests(unittest.TestCase):
    def test_menu_handlers_are_listed(self) -> None:
        body = WEB.split("def _menu_payload", 1)[1].split("def _api_payload", 1)[0]
        handled = set(re.findall(r'path == "(/api/[^"]+)"', body))
        self.assertIn("/api/place-suggest", handled)
        self.assertEqual(handled - set(MENU_PATHS), set())

    def test_frontend_api_paths_are_registered(self) -> None:
        exact = _registered_paths()
        missing = sorted(path for path in _frontend_api_paths(HTML) if not _is_registered(path, exact))
        self.assertEqual(missing, [])

    def test_version_and_note(self) -> None:
        self.assertEqual(PROCESS_VERSION, "25")
        self.assertIn('const APP_VERSION = "v1.21.0"', HTML)
        note = HTML.split('version: "1.14.6"', 1)[1].split("fixes:", 1)[0]
        self.assertIn("충전 장소: 장소 칸에서 추천이 다시 떠요", note)
        clear = HTML.split('getElementById("place-clear").addEventListener("click"', 1)[1]
        clear = clear.split("function renderPlaceHits", 1)[0]
        self.assertIn('loadPlaceHits("")', clear)
        self.assertIn("activeElement", clear)


class PlaceSuggestHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signals.db"
        conn = connect(self.db)
        ensure_charger_schema(conn)
        conn.execute(
            "INSERT INTO signals (vehicle_id, ts, name, value) VALUES (?, ?, 'Gear', ?)",
            (VID, "2026-11-01T00:00:00Z", json.dumps("P")),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, 'old', '우리집')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, location_text) VALUES (?, 'now', '지금 칸')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) "
            "VALUES (?, 'old', 37.5, 127.0, 'car')",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charge_charger (vehicle_id, charge_id, stat_id, source, updated_ts) "
            "VALUES (?, 'old', 'ST1', 'manual', '2026-11-01T00:00:00Z')",
            (VID,),
        )
        conn.commit()
        conn.close()
        self.server = make_server(str(self.db), host="127.0.0.1", port=0, process_interval=0, seed=False)
        self.thread = serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.lookup = patch("process.evcharger._reverse_place", return_value="강남파이낸스센터")
        self.search = patch(
            "process.naver_maps.place_search",
            return_value={"items": [{"title": "집앞 편의점", "category": "편의점"}]},
        )
        self.lookup.start()
        self.search.start()
        self.addCleanup(self.lookup.stop)
        self.addCleanup(self.search.stop)
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, **query: str) -> tuple[int, str, dict]:
        url = f"http://127.0.0.1:{self.port}/api/place-suggest?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, resp.headers.get("Content-Type", ""), json.loads(raw)

    def test_empty_query_returns_saved_place_then_building(self) -> None:
        status, ctype, body = self._get(q="", lat="37.5", lon="127.0", charge_id="now")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(
            [(row["title"], row["tag"]) for row in body["items"]],
            [("우리집", "예전 장소"), ("강남파이낸스센터", "건물")],
        )
        self.assertNotIn("지금 칸", [row["title"] for row in body["items"]])

    def test_typed_query_puts_a_saved_place_first(self) -> None:
        status, ctype, body = self._get(q="집", lat="37.5", lon="127.0", charge_id="now", stat_id="ST1")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(body["items"][0], {"title": "우리집", "tag": "예전 장소"})
        self.assertEqual(body["items"][1]["title"], "집앞 편의점")
        self.assertEqual(body["items"][1]["tag"], "편의점")

    def test_charge_id_is_accepted(self) -> None:
        status, _ctype, body = self._get(charge_id="now")
        self.assertEqual(status, 200)
        self.assertIn("items", body)
        self.assertIsInstance(body["items"], list)
