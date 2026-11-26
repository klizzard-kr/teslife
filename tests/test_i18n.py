"""Korean/English UI strings and bilingual place labels."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from process import connect as process_connect
from process.geocode import (
    GeocodeLookupError,
    admin_area,
    backfill_english,
    fill_trip_labels,
    label_from_nominatim,
)
from process.maintenance import alert_text
from web.l10n import error_text

ROOT = Path(__file__).resolve().parents[1]
I18N = (ROOT / "web" / "static" / "i18n.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


def _node(expr: str) -> str:
    src = I18N + "\nprocess.stdout.write(String((" + expr + ")));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return out.stdout


class I18nScriptTests(unittest.TestCase):
    def test_toggle_is_in_the_header(self) -> None:
        self.assertIn('id="lang-select"', HTML)
        self.assertIn("teslife-lang", HTML)
        self.assertIn('src="/static/i18n.js', HTML)
        self.assertLess(HTML.index('id="lang-select"'), HTML.index('id="theme-select"'))
        self.assertIn('const APP_VERSION = "v1.19.0"', HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_t_and_language_persistence(self) -> None:
        self.assertEqual(_node('TeslifeI18n.t("홈")'), "홈")
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("차량 상태")'), "Vehicle")
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("우리집 메모")'), "우리집 메모")
        self.assertEqual(_node('TeslifeI18n.setLanguage("en", false), TeslifeI18n.t("약 1,234원")'), "about 1,234원")
        store = (
            "const mem = {v:null, getItem(k){return this.v;}, setItem(k,val){this.v=val;}};"
            "TeslifeI18n.writeLang(mem, 'en');"
            "process.stdout.write(TeslifeI18n.readLang(mem) + TeslifeI18n.readLang({getItem(){return null;}}));"
        )
        out = subprocess.run(["node", "-e", I18N + "\n" + store], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, "enko")


class GeocodeLanguageTests(unittest.TestCase):
    def test_korean_admin_area_unchanged(self) -> None:
        self.assertEqual(admin_area("서울특별시 강남구 역삼1동 테헤란로 152"), "역삼1동")
        self.assertEqual(admin_area("집"), "집")

    def test_english_admin_area(self) -> None:
        self.assertEqual(admin_area("Yeoksam 1-dong, Gangnam-gu, Seoul"), "Yeoksam 1-dong")
        self.assertEqual(admin_area("Yeoksam 1(il)-dong"), "Yeoksam 1(il)-dong")
        self.assertEqual(admin_area("Gangnam-gu, Seoul"), "Gangnam-gu")
        self.assertEqual(admin_area("Seongnam-si"), "Seongnam-si")
        self.assertEqual(admin_area("SoMa"), "SoMa")

    def test_overseas_fallback_skips_road_and_postcode(self) -> None:
        london = {
            "display_name": "10, Downing Street, Westminster, London, SW1A 2AA, United Kingdom",
            "address": {
                "house_number": "10",
                "road": "Downing Street",
                "neighbourhood": "Westminster",
                "suburb": "City of Westminster",
                "city": "London",
                "state": "England",
                "postcode": "SW1A 2AA",
            },
        }
        self.assertEqual(label_from_nominatim(london), "Westminster")
        sf = {
            "display_name": "Market Street, San Francisco, California, 94103",
            "address": {"road": "Market Street", "city": "San Francisco", "state": "California", "postcode": "94103"},
        }
        self.assertEqual(label_from_nominatim(sf), "San Francisco")
        state_only = {
            "display_name": "Some Road, Bavaria, 80331",
            "address": {"road": "Some Road", "state": "Bavaria", "postcode": "80331"},
        }
        self.assertEqual(label_from_nominatim(state_only), "Bavaria")
        self.assertIsNone(label_from_nominatim({
            "display_name": "1 Main Street, 12345",
            "address": {"house_number": "1", "road": "Main Street", "postcode": "12345"},
        }))
        self.assertEqual(
            label_from_nominatim({"address": {"road": "테헤란로", "suburb": "역삼1동", "borough": "강남구"}}),
            "역삼1동",
        )
        self.assertIsNone(label_from_nominatim({}))

    def test_reverse_labels_are_two_paced_requests(self) -> None:
        import process.geocode as gc

        seen: list = []

        class Res:
            def __init__(self, lang: str) -> None:
                self.lang = lang

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                if "accept-language=en" in self.lang:
                    body = {"address": {"suburb": "Yeoksam 1-dong", "borough": "Gangnam-gu", "city": "Seoul"}}
                else:
                    body = {"address": {"suburb": "역삼1동", "borough": "강남구"}}
                return json.dumps(body).encode()

        def opener(req, timeout=0):
            seen.append(req)
            return Res(req.full_url)

        saved = gc.MIN_INTERVAL_S
        gc.MIN_INTERVAL_S = 0
        try:
            g = gc.NominatimGeocoder(opener)
            self.assertEqual(g.reverse_labels(37.5, 127.03), ("역삼1동", "Yeoksam 1-dong"))
        finally:
            gc.MIN_INTERVAL_S = saved
        self.assertEqual(len(seen), 2)
        self.assertIn("accept-language=ko", seen[0].full_url)
        self.assertIn("accept-language=en", seen[1].full_url)
        self.assertIn("teslife", seen[0].get_header("User-agent"))

    def test_fill_stores_both_labels(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "t.db"
        conn = process_connect(db)
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, start_lat, start_lon) "
            "VALUES ('V', '2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', 1.2, 37.5012, 127.0304)"
        )
        conn.commit()
        calls = []

        class Fake:
            name = "fake"

            def reverse_labels(self, lat, lon):
                calls.append((lat, lon))
                return "역삼1동", "Yeoksam 1-dong"

        try:
            self.assertEqual(fill_trip_labels(conn, "V", Fake(), limit=2), 2)
            self.assertEqual(fill_trip_labels(conn, "V", Fake(), limit=4), 0)
            row = conn.execute("SELECT label, label_en FROM geocode_cache").fetchone()
            self.assertEqual(tuple(row), ("역삼1동", "Yeoksam 1-dong"))
            self.assertEqual(len(calls), 1)
            kept = conn.execute("SELECT km, start_ts FROM trips").fetchone()
            self.assertEqual(tuple(kept), (1.2, "2026-11-01T00:00:00Z"))
        finally:
            conn.close()


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = process_connect(Path(self.tmp.name) / "b.db")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km) VALUES ('V', '2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', 3)"
        )
        self.conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, fetched_at) VALUES ('nominatim', '37.501,127.030', '역삼1동', '2026-11-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, label_en, fetched_at) VALUES ('nominatim', '37.500,127.000', '삼성동', 'Samseong-dong', '2026-11-01T00:00:00Z')"
        )
        self.conn.commit()

    def test_backfill_is_idempotent_and_leaves_trips(self) -> None:
        calls: list[str] = []

        class Fake:
            def reverse_lang(self, lat, lon, lang):
                calls.append(lang)
                return "Yeoksam 1-dong"

        first = backfill_english(self.conn, Fake())
        self.assertEqual(first["updated"], 1)
        self.assertEqual(calls, ["en"])
        second = backfill_english(self.conn, Fake())
        self.assertEqual(second["updated"], 0)
        self.assertEqual(calls, ["en"])
        row = self.conn.execute(
            "SELECT label_en FROM geocode_cache WHERE key = '37.501,127.030'"
        ).fetchone()
        self.assertEqual(row[0], "Yeoksam 1-dong")
        self.assertEqual(self.conn.execute("SELECT km FROM trips").fetchone()[0], 3)

    def test_lookup_error_can_be_resumed(self) -> None:
        class Down:
            def reverse_lang(self, lat, lon, lang):
                raise GeocodeLookupError("down")

        failed = backfill_english(self.conn, Down())
        self.assertEqual(failed["updated"], 0)
        self.assertEqual(failed["errors"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT label_en FROM geocode_cache WHERE key = '37.501,127.030'"
        ).fetchone()[0])

        class Ok:
            def reverse_lang(self, lat, lon, lang):
                return "Yeoksam 1-dong"

        self.assertEqual(backfill_english(self.conn, Ok())["updated"], 1)


class ServerTextTests(unittest.TestCase):
    def test_alert_text_english_is_opt_in(self) -> None:
        from process.maintenance import alert_dict

        name = next(k for k, v in alert_dict().items() if v and v[0] == "Windshield washer fluid low")
        self.assertEqual(alert_text(name, "en"), "Windshield washer fluid low")
        self.assertEqual(alert_text(name), "워셔액 부족")

    def test_error_text_default_korean(self) -> None:
        msg = "이미 장착된 세트예요"
        self.assertEqual(error_text(msg), msg)
        self.assertEqual(error_text(msg, "en"), "That set is already mounted.")
        self.assertEqual(error_text("name is required", "en"), "name is required")


class TripLabelApiTests(unittest.TestCase):
    def test_lang_picks_label_and_keeps_saved_names(self) -> None:
        from web import make_server, serve_in_thread

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "w.db"
        conn = process_connect(db)
        conn.execute(
            "INSERT INTO trips (vehicle_id, start_ts, end_ts, km, start_lat, start_lon, end_lat, end_lon) "
            "VALUES ('V', '2026-11-02T00:00:00Z', '2026-11-02T01:00:00Z', 2, 37.501, 127.03, 37.51, 127.04)"
        )
        conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, label_en) VALUES ('nominatim', '37.501,127.030', '역삼1동', 'Yeoksam 1-dong')"
        )
        conn.execute(
            "INSERT INTO geocode_cache (provider, key, label, label_en) VALUES ('nominatim', '37.510,127.040', '삼성동', NULL)"
        )
        conn.execute(
            "INSERT INTO places (vehicle_id, name, lat, lon, radius_m) VALUES ('V', '집', 37.501, 127.03, 150)"
        )
        conn.commit()
        conn.close()
        server = make_server(db, port=0, process_interval=0, seed=False)
        port = server.server_address[1]
        serve_in_thread(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        def labels(lang: str) -> tuple[str | None, str | None]:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trips?vehicle_id=V&lang={lang}", timeout=5) as res:
                body = json.loads(res.read().decode())
            trip = body["trips"][0]
            return trip["from_label"], trip["to_label"]

        self.assertEqual(labels("ko"), ("역삼1동", "삼성동"))
        self.assertEqual(labels("en"), ("Yeoksam 1-dong", "삼성동"))
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        self.assertEqual(labels("ko"), ("역삼1동", "삼성동"))
        self.assertEqual(labels("en"), ("Yeoksam 1-dong", "삼성동"))


if __name__ == "__main__":
    unittest.main()
