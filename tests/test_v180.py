"""v1.8.0: station purge, charger rematch that keeps energy and price, detail wording."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledger import sync_auto_charges
from process import connect, format_ts
from process.charge_backfill import apply_station_matches, energy_price_snapshot, maybe_purge_stations
from process.evcharger import ensure_schema

VID = "car-a"
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)


def _db(test: unittest.TestCase):
    folder = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    path = Path(folder) / "signals.db"
    conn = connect(path)
    test.addCleanup(conn.close)
    ensure_schema(conn)
    return conn, path


class RematchTests(unittest.TestCase):
    def test_energy_and_price_stay_identical(self) -> None:
        conn, _path = _db(self)
        start = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        cid = format_ts(start)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added, charger_kwh, battery_kwh, "
            "ac_kwh, dc_kwh, loss_kwh, loss_pct, max_charger_kw, avg_charger_kw) "
            "VALUES (?, ?, ?, 'AC', 10, 10.5, 9, 10.5, 9, 1.5, 14.2, 7, 6.5)",
            (VID, cid, format_ts(start + timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, 37.5, 127.0, 'car')",
            (VID, cid),
        )
        conn.execute(
            "INSERT INTO charge_meta (vehicle_id, charge_id, operator, location_text, kwh_billed, discount_krw) "
            "VALUES (?, ?, 'old-op', '내가 쓴 장소', 10, 100)",
            (VID, cid),
        )
        conn.execute("INSERT INTO rates (vehicle_id, operator, unit_price_krw_per_kwh) VALUES (?, 'old-op', 300)", (VID,))
        conn.execute("INSERT INTO rates (vehicle_id, operator, unit_price_krw_per_kwh) VALUES (?, 'new-op', 90)", (VID,))
        conn.execute(
            "INSERT INTO ev_chargers (stat_id, chger_id, lat, lng, chger_type, output_kw, busi_nm, stat_nm, addr, zcode, updated_ts) "
            "VALUES ('ST', '1', 37.5, 127.0, '02', 7, 'new-op', '새충전소', '서울특별시 중구', '11', 't')",
        )
        conn.commit()
        sync_auto_charges(conn, VID)
        before = energy_price_snapshot(conn, VID)
        stats = apply_station_matches(conn, VID, NOW)
        after = energy_price_snapshot(conn, VID)
        self.assertEqual(before["charges"], after["charges"])
        self.assertEqual(before["prices"], after["prices"])
        self.assertEqual(before["ledger"], after["ledger"])
        sync_auto_charges(conn, VID)
        again = energy_price_snapshot(conn, VID)
        self.assertEqual(before["charges"], again["charges"])
        self.assertEqual(before["prices"], again["prices"])
        self.assertEqual(before["ledger"], again["ledger"])
        self.assertEqual(stats["matched"], 1)
        self.assertGreaterEqual(stats["operator_kept"], 1)
        saved = conn.execute(
            "SELECT stat_nm, busi_nm, output_kw, chger_id FROM charge_charger WHERE charge_id = ?",
            (cid,),
        ).fetchone()
        self.assertEqual(saved[0], "새충전소")
        self.assertEqual(saved[1], "new-op")
        self.assertEqual(saved[2], 7)
        self.assertIsNone(saved[3])
        self.assertEqual(conn.execute("SELECT operator, total_krw, unit_price_krw_per_kwh, kwh_billed, discount_krw FROM charge_meta").fetchone(),
                         ("old-op", None, None, 10, 100))
        self.assertEqual(conn.execute("SELECT amount_krw, estimated FROM ledger").fetchone(), (3000, 1))
        self.assertEqual(conn.execute("SELECT location_text FROM charge_meta").fetchone()[0], "내가 쓴 장소")
        self.assertEqual(conn.execute("SELECT station_name, station_addr FROM charge_meta").fetchone(), ("새충전소", "서울특별시 중구"))

    def test_purge_backs_up_then_deletes_saved_stations(self) -> None:
        conn, path = _db(self)
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, 'c0', 'c1', 'AC', 4)",
            (VID,),
        )
        conn.executemany(
            "INSERT INTO stations (vehicle_id, id, name, address_text) VALUES (?, ?, ?, ?)",
            [(VID, "a", "하나", "주소1"), (VID, "b", "둘", "주소2")],
        )
        conn.commit()
        first = maybe_purge_stations(conn, path)
        self.assertEqual(first["deleted"], 2)
        self.assertEqual(first["backed_up"], 2)
        backup = json.loads(Path(first["backup"]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(row["name"] for row in backup), ["둘", "하나"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT kwh_added FROM charges").fetchone()[0], 4)
        self.assertIsNone(maybe_purge_stations(conn, path))

    def test_existing_locations_stay_after_purge_and_repeated_fill(self) -> None:
        from process.chargeloc import fill_charge_locations

        conn, path = _db(self)
        sources = ["station"] * 4 + ["manual"] * 3 + ["car"] * 2 + ["estimated"] * 2
        for i, source in enumerate(sources):
            cid = f"2026-10-{i + 1:02d}T00:00:00Z"
            lat, lon = round(37.1 + i * 0.01, 6), round(126.7 + i * 0.01, 6)
            conn.execute(
                "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, ?, ?, 'AC', 5)",
                (VID, cid, cid),
            )
            conn.execute(
                "INSERT INTO charge_locations (vehicle_id, charge_id, lat, lon, source) VALUES (?, ?, ?, ?, ?)",
                (VID, cid, lat, lon, source),
            )
        conn.execute(
            "INSERT INTO stations (vehicle_id, id, name, lat, lon) VALUES (?, 's', '집', 37.1, 126.7)",
            (VID,),
        )
        conn.execute(
            "INSERT INTO charges (vehicle_id, start_ts, end_ts, kind, kwh_added) VALUES (?, '2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', 'AC', 8)",
            (VID,),
        )
        conn.commit()

        def points():
            return [tuple(row) for row in conn.execute(
                "SELECT charge_id, lat, lon, source FROM charge_locations WHERE vehicle_id = ? "
                "AND charge_id < '2026-11-01' ORDER BY charge_id",
                (VID,),
            ).fetchall()]

        before = points()
        self.assertEqual(len(before), 11)
        maybe_purge_stations(conn, path)
        for _ in range(3):
            fill_charge_locations(conn, VID)
            apply_station_matches(conn, VID, NOW)
            maybe_purge_stations(conn, path)
        self.assertEqual(points(), before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0], 0)
        fresh = conn.execute(
            "SELECT source FROM charge_locations WHERE charge_id = '2026-11-01T00:00:00Z'"
        ).fetchone()
        self.assertIsNone(fresh)


class ScreenTests(unittest.TestCase):
    def test_v180_wording(self) -> None:
        self.assertIn('const APP_VERSION = "v1.15.1"', HTML)
        self.assertIn('class="settings-heading">화면', HTML)
        self.assertIn('class="settings-heading">알림', HTML)
        self.assertIn('id="lang-select"', HTML)
        self.assertIn('id="theme-select"', HTML)
        self.assertIn('name="pay_method"', HTML)
        self.assertIn('name="station_id"', HTML)
        self.assertNotIn("id=\"tariff-open\"", HTML)
        self.assertNotIn("chargeFlowHtml", HTML)
        self.assertNotIn("충전기 번호", HTML)
        self.assertNotIn("이 장소를 위치와 함께 저장", HTML)
        self.assertNotIn("id='open-pay-settings'", HTML)
        self.assertIn("opacity: 0, interactive: false", HTML)
        self.assertIn('fill-rule="evenodd"', HTML)
        self.assertNotIn("M12 3v2", HTML)
        charge = HTML[HTML.index("function chargeSheetHtml"):HTML.index("function openChargeDetail")]
        labels = ["충전량", "배터리 저장량", "충전 중 사용량", "손실률", "충전효율"]
        energy = charge[charge.index('oneRow("충전량"'):]
        self.assertEqual([energy.index(label) for label in labels], sorted(energy.index(label) for label in labels))
        self.assertNotIn("손실량", energy)


if __name__ == "__main__":
    unittest.main()
