import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import connect, store_signals
from process import process_db
from simulator import generate, signal
from web import (
    cold_tire_alert,
    make_server,
    serve_in_thread,
)


def _tmp_db(test: unittest.TestCase) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
    return tmp.name


class ColdTireAlertTests(unittest.TestCase):
    def test_cold_below_threshold(self) -> None:
        reading = {
            "ts": "2026-10-26T07:50:00Z",
            "fl": 2.4,
            "fr": 2.9,
            "rl": 2.88,
            "rr": 2.87,
            "phase": "cold",
        }
        result = cold_tire_alert(reading, 2.6)
        self.assertTrue(result["alert"])
        self.assertEqual(result["wheels"], ["fl"])

    def test_warm_never_triggers(self) -> None:
        warm = {
            "ts": "2026-10-26T08:40:00Z",
            "fl": 2.1,
            "fr": 2.1,
            "rl": 2.1,
            "rr": 2.1,
            "phase": "warm",
        }
        # Helper is only called with cold readings; even a low dict tagged
        # warm would still be numerically low, so callers must pass cold-only.
        self.assertFalse(cold_tire_alert(None, 2.6)["alert"])
        self.assertFalse(
            cold_tire_alert(
                {"ts": "t", "fl": 2.9, "fr": 2.9, "rl": 2.9, "rr": 2.9, "phase": "cold"},
                2.6,
            )["alert"]
        )
        self.assertEqual(warm["phase"], "warm")


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _tmp_db(self)
        conn = connect(self.db)
        try:
            store_signals(conn, generate("day"))
        finally:
            conn.close()
        process_db(self.db)
        self.server = make_server(
            self.db,
            host="127.0.0.1",
            port=0,
            tire_alert_bar=2.6,
            process_interval=0,
        )
        self.thread = serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body

    def test_live_and_lists(self) -> None:
        status, live = self._get("/api/live")
        self.assertEqual(status, 200)
        self.assertIn("BatteryLevel", live["signals"])
        self.assertIn("ts", live["signals"]["BatteryLevel"])
        self.assertFalse(live["trip_in_progress"])
        self.assertFalse(live["charge_in_progress"])
        self.assertFalse(live["tire_alert"]["alert"])

        _, trips = self._get("/api/trips")
        self.assertEqual(len(trips["trips"]), 2)
        self.assertGreaterEqual(trips["trips"][0]["start_ts"], trips["trips"][1]["start_ts"])
        self.assertAlmostEqual(trips["trips"][1]["km"], 12.0, places=3)
        self.assertAlmostEqual(trips["trips"][0]["km"], 30.0, places=3)

        _, charges = self._get("/api/charges")
        self.assertEqual(len(charges["charges"]), 1)
        self.assertEqual(charges["charges"][0]["kind"], "AC")
        self.assertAlmostEqual(charges["charges"][0]["kwh_added"], 20.0, places=3)

        # : no tire history API; the working window stays in the DB for alerts
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/api/tires")
        self.assertEqual(cm.exception.code, 404)
        self.assertTrue(live["tire_alert"] is not None)

        _, summary = self._get("/api/summary?month=2026-10")
        self.assertEqual(summary["trip_count"], 2)
        self.assertAlmostEqual(summary["km"], 42.0, places=3)
        self.assertEqual(summary["charge_count"], 1)
        self.assertAlmostEqual(summary["kwh_ac"], 20.0, places=3)
        self.assertEqual(summary["kwh_dc"], 0.0)

    def test_api_cannot_modify_db(self) -> None:
        conn = sqlite3.connect(self.db)
        before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        conn.close()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/live",
            data=b'{"vehicle_id":"x"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)
        self._get("/api/trips")
        conn = sqlite3.connect(self.db)
        after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        trips = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)
        self.assertEqual(trips, 2)

    def test_closed_stream_drops_its_thread_and_live_still_works(self) -> None:
        """A refresh must not leave the SSE handler running with no new signals."""
        before = {t.ident for t in threading.enumerate() if t.is_alive()}
        # Hold the response objects. Closing only the HTTPConnection drops its
        # socket ref while the response still owns the TCP connection.
        held = []
        for _ in range(6):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.putrequest("GET", "/api/stream")
            conn.putheader("Accept", "text/event-stream")
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIsNotNone(_read_sse_event(resp))
            held.append(resp)
        self.assertGreaterEqual(self._handler_threads(before), 6)
        for resp in held:
            resp.close()
        deadline = time.time() + 3
        left = 6
        while time.time() < deadline:
            left = self._handler_threads(before)
            if left == 0:
                break
            time.sleep(0.05)
        self.assertEqual(left, 0, "closed streams still hold handler threads")
        t0 = time.perf_counter()
        status, _live = self._get("/api/live")
        self.assertEqual(status, 200)
        self.assertLess(time.perf_counter() - t0, 2.0)
        again = HTTPConnection("127.0.0.1", self.port, timeout=5)
        again.putrequest("GET", "/api/stream")
        again.putheader("Accept", "text/event-stream")
        again.endheaders()
        resp = again.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIsNotNone(_read_sse_event(resp))
        resp.close()

    def _handler_threads(self, before: set[int | None]) -> int:
        return sum(
            1 for t in threading.enumerate()
            if t.is_alive() and t.ident not in before and t is not threading.current_thread()
        )

    def test_stream_emits_after_ingest(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=8)
        conn.putrequest("GET", "/api/stream")
        conn.putheader("Accept", "text/event-stream")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        first = _read_sse_event(resp)
        self.assertIsNotNone(first)
        store = connect(self.db)
        try:
            store_signals(
                store,
                [
                    signal(
                        "VINTEST0000000001",
                        datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
                        "BatteryLevel",
                        41.0,
                    )
                ],
            )
        finally:
            store.close()
        second = _read_sse_event(resp)
        conn.close()
        self.assertIsNotNone(second)
        self.assertIn("signals", second.get("live", second))

    def test_index_korean(self) -> None:
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertNotIn("자동 갱신", html)
        self.assertIn("공기압", html)

    def test_index_tire_pressure_in_psi(self) -> None:
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        # Stored/API values stay in bar; the page converts them to psi.
        self.assertIn("PSI_PER_BAR = 14.5038", html)
        self.assertIn('" psi"', html)
        # : the 통계 tab no longer has the 공기압 추이 chart (: GET /api/tires removed too)
        self.assertNotIn("공기압 추이", html)
        self.assertNotIn('id="tire-chart"', html)
        self.assertNotIn("function drawTires", html)
        self.assertNotIn('fetchJson("/api/tires")', html)
        self.assertIn('id="v-fl"', html)   # home tire values (status) stay
        self.assertNotIn('" bar"', html)
        self.assertNotIn(' bar</text>', html)

    def test_index_mobile_tabs_and_favicon(self) -> None:
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("viewport-fit=cover", html)
        head = html.split("</head>", 1)[0]
        self.assertIn('rel="icon"', head)
        self.assertIn('href="/favicon.ico?v=2"', head)
        self.assertIn('href="/static/favicon-32.png?v=2"', head)
        self.assertIn('href="/static/favicon-16.png?v=2"', head)
        self.assertIn('rel="apple-touch-icon"', head)
        self.assertIn('href="/apple-touch-icon.png?v=2"', head)
        self.assertIn('href="/static/icon-192.png?v=2"', head)
        self.assertIn('href="/static/icon-512.png?v=2"', head)
        self.assertNotIn("data:image/svg+xml", html)
        brand = html[html.index('id="brand-home"'):html.index('id="brand-home"') + 500]
        self.assertIn('class="brand-mark"', brand)
        self.assertIn('class="brand-name"', brand)
        self.assertIn("/static/logo-128.png", brand)
        self.assertIn(">Teslife<", brand)
        # Wordmark cap-height sits 2px above the 32px mark; nudge glyphs only.
        self.assertIn(".brand-name { display: block; line-height: 1; transform: translateY(2px); }", html)
        self.assertIn("width: 32px; height: 32px", html)
        for path, magic, ctype in (
            ("/favicon.ico", b"\x00\x00\x01\x00", "image/x-icon"),
            ("/static/favicon-32.png", b"\x89PNG", "image/png"),
            ("/static/favicon-16.png", b"\x89PNG", "image/png"),
            ("/apple-touch-icon.png", b"\x89PNG", "image/png"),
            ("/static/logo-64.png", b"\x89PNG", "image/png"),
            ("/static/logo-128.png", b"\x89PNG", "image/png"),
            ("/static/icon-192.png", b"\x89PNG", "image/png"),
            ("/static/icon-512.png", b"\x89PNG", "image/png"),
            ("/static/teslife-logo-1024.png", b"\x89PNG", "image/png"),
        ):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                self.assertEqual(resp.status, 200, path)
                self.assertEqual(resp.headers.get("Content-Type"), ctype, path)
                self.assertTrue(resp.read(8).startswith(magic), path)
        self.assertIn('id="tab-bar"', html)
        for tab in ("home", "trips", "charges", "ledger", "consumables"):
            self.assertIn(f'data-tab="{tab}"', html)
            self.assertIn(f'id="tab-{tab}"', html)
        for gone in ('data-tab="stats"', 'data-tab="map"', "통계", "자동 갱신"):
            self.assertNotIn(gone, html)
        self.assertIn('id="tab-btn-home"', html)
        for s in ("주행", "충전", "차계부", "정비", "더보기", "Asia/Seoul", 'toLocaleString("ko-KR")', "sheet-handle",
                  "충전 정보 입력", "더 입력", "방금", "충전 완료", "주기 미설정", "필요 시",
                  "fmtMemoHtml", 'lang="ko"', 'id="period-overlay"', 'id="period-apply"'):
            self.assertIn(s, html, s)


class MultiVehicleWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _tmp_db(self)
        conn = connect(self.db)
        try:
            store_signals(conn, generate("day", vehicle_id="VINAAA"))
            store_signals(conn, generate("dc_day", vehicle_id="VINBBB"))
        finally:
            conn.close()
        process_db(self.db)
        self.server = make_server(
            self.db, host="127.0.0.1", port=0, process_interval=0
        )
        serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def _get(self, path: str) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_vehicle_ids_do_not_mix(self) -> None:
        a = self._get("/api/charges?vehicle_id=VINAAA")
        b = self._get("/api/charges?vehicle_id=VINBBB")
        self.assertEqual(a["vehicle_id"], "VINAAA")
        self.assertEqual(b["vehicle_id"], "VINBBB")
        self.assertEqual(a["charges"][0]["kind"], "AC")
        self.assertEqual(b["charges"][0]["kind"], "DC")
        a_trips = self._get("/api/trips?vehicle_id=VINAAA")["trips"]
        b_trips = self._get("/api/trips?vehicle_id=VINBBB")["trips"]
        self.assertTrue(all(t["vehicle_id"] == "VINAAA" for t in a_trips))
        self.assertTrue(all(t["vehicle_id"] == "VINBBB" for t in b_trips))
        live_a = self._get("/api/live?vehicle_id=VINAAA")
        self.assertEqual(live_a["vehicle_id"], "VINAAA")


class TireAlertApiTests(unittest.TestCase):
    def test_home_alert_matches_the_pressure_on_screen(self) -> None:
        """A colder stored reading must not warn when the tiles show a higher psi."""
        db = _tmp_db(self)
        conn = connect(db)
        try:
            store_signals(conn, generate("day"))
        finally:
            conn.close()
        process_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            """
            INSERT OR REPLACE INTO tire_readings
            (vehicle_id, ts, fl, fr, rl, rr, outside_temp, phase)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("VINTEST0000000001", "2026-10-26T23:00:00Z", 2.4, 2.4, 2.4, 2.4, 8.0, "cold"),
        )
        conn.commit()
        conn.close()
        server = make_server(db, host="127.0.0.1", port=0, process_interval=0)
        serve_in_thread(server)
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/live", timeout=5) as resp:
            live = json.loads(resp.read().decode("utf-8"))
        # The day ends on warm pressures around 45 psi. The cold 2.4 bar row is not what the tiles show.
        self.assertFalse(live["tire_alert"]["alert"])
        self.assertEqual(live["tire_alert"]["basis"], "displayed")


CHROME_BIN = Path("/usr/bin/google-chrome")
TAB_SCREENSHOTS = {
    "home": "tab-home.png",
    "trips": "tab-trips.png",
    "ledger": "tab-ledger.png",
    "consumables": "tab-consumables.png",
    "stats": "tab-stats.png",
}


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _ChromeCdp:
    def __init__(self, port: int) -> None:
        self.port = port
        self.sock: socket.socket | None = None
        self.buf = b""
        self._id = 0
        self.session_id: str | None = None

    def connect_browser(self) -> None:
        version = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=8
            ).read()
        )
        self._ws_connect(version["webSocketDebuggerUrl"])

    def open_page(self, page_url: str) -> None:
        self.session_id = None
        created = self.call(
            "Target.createTarget",
            {"url": page_url, "newWindow": True, "width": 390, "height": 844},
        )
        attached = self.call(
            "Target.attachToTarget",
            {"targetId": created["targetId"], "flatten": True},
        )
        self.session_id = attached["sessionId"]
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                state = self.evaluate("document.readyState")
            except Exception:
                state = None
            if state == "complete":
                break
            time.sleep(0.15)
        time.sleep(0.5)

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        if self.session_id:
            payload["sessionId"] = self.session_id
        self._send(json.dumps(payload))
        while True:
            msg = self._recv()
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result") or {}

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        return result.get("result", {}).get("value")

    def screenshot_png(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _ws_connect(self, ws_url: str) -> None:
        assert ws_url.startswith("ws://")
        hostport, path = ws_url[5:].split("/", 1)
        path = "/" + path
        host, port_s = hostport.split(":")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port_s}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        sock = socket.create_connection((host, int(port_s)), timeout=15)
        sock.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("chrome websocket handshake failed")
            buf += chunk
        self.sock = sock
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        header = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
        if "101" not in header.split("\r\n", 1)[0] or expected not in header:
            raise RuntimeError("chrome websocket upgrade rejected")

    def _send(self, text: str) -> None:
        assert self.sock is not None
        data = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + masked)

    def _recv(self) -> dict:
        assert self.sock is not None
        while True:
            if len(self.buf) >= 2:
                b1, b2 = self.buf[0], self.buf[1]
                masked = b2 & 0x80
                ln = b2 & 0x7F
                idx = 2
                if ln == 126:
                    if len(self.buf) < 4:
                        self.buf += self.sock.recv(4096)
                        continue
                    ln = struct.unpack("!H", self.buf[2:4])[0]
                    idx = 4
                elif ln == 127:
                    if len(self.buf) < 10:
                        self.buf += self.sock.recv(4096)
                        continue
                    ln = struct.unpack("!Q", self.buf[2:10])[0]
                    idx = 10
                if masked:
                    if len(self.buf) < idx + 4:
                        self.buf += self.sock.recv(4096)
                        continue
                    mask = self.buf[idx : idx + 4]
                    idx += 4
                else:
                    mask = b""
                if len(self.buf) < idx + ln:
                    self.buf += self.sock.recv(max(4096, idx + ln - len(self.buf)))
                    continue
                payload = self.buf[idx : idx + ln]
                self.buf = self.buf[idx + ln :]
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                opcode = b1 & 0x0F
                if opcode == 0x1:
                    return json.loads(payload.decode("utf-8"))
                if opcode == 0x8:
                    return {}
                continue
            more = self.sock.recv(4096)
            if not more:
                return {}
            self.buf += more


class MobileLayoutChromeTests(unittest.TestCase):
    def setUp(self) -> None:
        if not CHROME_BIN.exists():
            self.skipTest("headless Chrome not available")
        self.db = _tmp_db(self)
        conn = connect(self.db)
        try:
            store_signals(conn, generate("day"))
        finally:
            conn.close()
        process_db(self.db)
        self.server = make_server(
            self.db, host="127.0.0.1", port=0, process_interval=0
        )
        serve_in_thread(self.server)
        self.port = self.server.server_address[1]
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def test_tabs_fit_390_and_screenshots(self) -> None:
        dbg = _free_port()
        proc = subprocess.Popen(
            [
                str(CHROME_BIN),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--remote-debugging-port={dbg}",
                "--window-size=390,844",
                "--ozone-override-screen-size=390x844",
                "--force-device-scale-factor=1",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        def _stop_chrome() -> None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stderr:
                proc.stderr.close()

        self.addCleanup(_stop_chrome)
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{dbg}/json/version", timeout=1
                ).read()
                break
            except OSError:
                time.sleep(0.15)
        else:
            self.skipTest("Chrome DevTools port did not open")
        cdp = _ChromeCdp(dbg)
        self.addCleanup(cdp.close)
        cdp.connect_browser()
        docs = ROOT / "docs"
        docs.mkdir(exist_ok=True)
        for tab, filename in TAB_SCREENSHOTS.items():
            url = f"http://127.0.0.1:{self.port}/#{tab}"
            cdp.open_page(url)
            inner = cdp.evaluate("window.innerWidth")
            cdp.evaluate(
                "history.replaceState(null,'','#" + tab + "');"
                "window.dispatchEvent(new HashChangeEvent('hashchange'));"
            )
            ready_deadline = time.time() + 6
            while time.time() < ready_deadline:
                bat = cdp.evaluate(
                    "document.getElementById('v-battery') && "
                    "document.getElementById('v-battery').textContent"
                )
                if bat and bat != "—":
                    break
                time.sleep(0.2)
            time.sleep(0.4)
            def _assert_fit(note: str) -> None:
                width = cdp.evaluate("document.documentElement.scrollWidth")
                self.assertIsNotNone(width)
                self.assertLessEqual(
                    int(width),
                    390,
                    msg=f"{tab} {note} overflow scrollWidth={width} inner={inner}",
                )

            _assert_fit("default")
            if tab == "trips":
                cdp.evaluate(
                    "document.querySelector('#activity-seg [data-activity-mode=\"year\"]').click()"
                )
                time.sleep(0.6)
                _assert_fit("year")
                cdp.evaluate(
                    "document.querySelector('#activity-year-panel .info-btn').click()"
                )
                time.sleep(0.2)
                _assert_fit("year-info")
                cdp.evaluate(
                    "document.querySelector('#activity-year-panel .info-btn').click()"
                )
                cdp.evaluate(
                    "document.querySelector('#activity-seg [data-activity-mode=\"month\"]').click()"
                )
                time.sleep(0.4)
                _assert_fit("month")
            if tab == "ledger":
                cdp.evaluate(
                    "document.querySelector('#ledger-seg [data-ledger-mode=\"year\"]').click()"
                )
                time.sleep(0.6)
                _assert_fit("year")
                cdp.evaluate(
                    "document.querySelector('#ledger-year-panel .info-btn').click()"
                )
                time.sleep(0.2)
                _assert_fit("year-info")
                cdp.evaluate("document.body.click()")
                cdp.evaluate(
                    "document.querySelector('#ledger-seg [data-ledger-mode=\"month\"]').click()"
                )
                time.sleep(0.4)
                _assert_fit("month")
            cdp.screenshot_png(docs / filename)


def _read_sse_event(resp, timeout_lines: int = 80) -> dict | None:
    data_lines: list[str] = []
    for _ in range(timeout_lines):
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
        elif text == "" and data_lines:
            return json.loads("\n".join(data_lines))
    return None


if __name__ == "__main__":
    unittest.main()
