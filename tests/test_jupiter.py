#!/usr/bin/env python3
"""Offline tests for Jupiter on-chain reference integration."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import analyze  # noqa: E402
import common  # noqa: E402
import jupiter  # noqa: E402
import recorder  # noqa: E402
import venues  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jupiter_quote_sol_usdc.json"
FIXTURE_RAY = Path(__file__).resolve().parent / "fixtures" / "jupiter_quote_raydium.json"
FIXTURE_ORCA = Path(__file__).resolve().parent / "fixtures" / "jupiter_quote_orca.json"


class TempConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._previous = os.environ.get("OAKRING_CONFIG_DIR")
        os.environ["OAKRING_CONFIG_DIR"] = str(self.tmp / "config")
        self.db_path = self.tmp / "ring.db"

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("OAKRING_CONFIG_DIR", None)
        else:
            os.environ["OAKRING_CONFIG_DIR"] = self._previous
        self._tmp.cleanup()


class JupiterParseTests(unittest.TestCase):
    def test_ref_price_from_fixture(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        quote = jupiter.parse_quote(payload, input_decimals=9, output_decimals=6)
        self.assertAlmostEqual(quote.ref_price, 150.25)
        self.assertAlmostEqual(quote.impact_bps, 125.0)
        self.assertEqual(quote.in_amount, 1_000_000_000)
        self.assertEqual(quote.out_amount, 150_250_000)

    def test_extract_amm_key(self) -> None:
        payload = json.loads(FIXTURE_RAY.read_text(encoding="utf-8"))
        self.assertEqual(jupiter.extract_amm_key(payload), "ray-pool-abc")

    def test_build_quote_url_with_dex_filter(self) -> None:
        url = jupiter.build_quote_url(
            "https://example.test/quote",
            jupiter.DEFAULT_INPUT_MINT,
            jupiter.DEFAULT_OUTPUT_MINT,
            1_000_000_000,
            50,
            dex="Raydium",
            only_direct_routes=True,
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["dexes"], ["Raydium"])
        self.assertEqual(query["onlyDirectRoutes"], ["true"])

    def test_output_for_pair_usdc_and_usdt(self) -> None:
        cfg = jupiter.config_from({})
        usdc = jupiter.output_for_pair("SOLUSDC", cfg)
        usdt = jupiter.output_for_pair("SOLUSDT", cfg)
        self.assertEqual(usdc[0], jupiter.DEFAULT_OUTPUT_MINT)
        self.assertEqual(usdt[0], jupiter.DEFAULT_USDT_MINT)

    def test_basis_bps(self) -> None:
        self.assertAlmostEqual(recorder.compute_basis_bps(150.50, 150.25), 16.64, places=1)


class JupiterRecorderTests(TempConfigCase):
    def test_run_tick_attaches_onchain_to_configured_pairs(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        def fake_binance(url: str, timeout: int) -> object:
            return [
                {"symbol": "SOLUSDT", "bidPrice": "150.40", "askPrice": "150.60", "bidQty": "1", "askQty": "1"},
                {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60010", "bidQty": "1", "askQty": "1"},
            ]

        original_binance, recorder.http_get_json = recorder.http_get_json, fake_binance
        original_jupiter, jupiter.fetch_quote = jupiter.fetch_quote, (
            lambda **kwargs: jupiter.parse_quote(fixture, 9, 6)
        )
        try:
            conn = common.connect(self.db_path)
            venues.VENUES["binance"] = dataclasses.replace(
                venues.VENUES["binance"], base_url="https://example.test"
            )
            jupiter_cfg = jupiter.config_from({"JUPITER_ENABLED": "1", "JUPITER_ATTACH_PAIRS": "SOLUSDT"})
            recorder.run_tick(
                conn,
                {"binance": {"SOLUSDT": "SOLUSDT", "BTCUSDT": "BTCUSDT"}},
                5,
                0,
                jupiter_cfg,
            )
        finally:
            recorder.http_get_json = original_binance
            jupiter.fetch_quote = original_jupiter

        rows = {
            row["pair"]: row
            for row in conn.execute(
                "SELECT pair, mid, onchain_ref, basis_bps, onchain_note FROM ticks"
            ).fetchall()
        }
        conn.close()

        sol = rows["SOLUSDT"]
        self.assertAlmostEqual(sol["mid"], 150.50)
        self.assertAlmostEqual(sol["onchain_ref"], 150.25)
        self.assertAlmostEqual(sol["basis_bps"], 16.64, places=1)
        self.assertIsNone(sol["onchain_note"])

        btc = rows["BTCUSDT"]
        self.assertIsNone(btc["onchain_ref"])
        self.assertIsNone(btc["basis_bps"])

    def test_run_tick_records_per_pool_samples(self) -> None:
        fixtures = {
            "Raydium": json.loads(FIXTURE_RAY.read_text(encoding="utf-8")),
            "Orca": json.loads(FIXTURE_ORCA.read_text(encoding="utf-8")),
        }

        def fake_binance(url: str, timeout: int) -> object:
            return [
                {"symbol": "SOLUSDC", "bidPrice": "150.40", "askPrice": "150.60", "bidQty": "1", "askQty": "1"},
            ]

        def fake_jupiter(url: str, timeout: int, headers: dict[str, str]) -> object:
            dex = parse_qs(urlparse(url).query).get("dexes", [""])[0]
            return fixtures[dex]

        original_binance, recorder.http_get_json = recorder.http_get_json, fake_binance
        original_get, recorder._jupiter_http_get = recorder._jupiter_http_get, fake_jupiter
        original_fetch, jupiter.fetch_quote = jupiter.fetch_quote, (
            lambda **kwargs: jupiter.parse_quote(json.loads(FIXTURE.read_text(encoding="utf-8")), 9, 6)
        )
        try:
            conn = common.connect(self.db_path)
            venues.VENUES["binance"] = dataclasses.replace(
                venues.VENUES["binance"], base_url="https://example.test"
            )
            jupiter_cfg = jupiter.config_from({
                "JUPITER_ENABLED": "1",
                "JUPITER_ATTACH_PAIRS": "SOLUSDC",
                "JUPITER_DEXES": "Raydium,Orca",
            })
            recorder.run_tick(conn, {"binance": {"SOLUSDC": "SOLUSDC"}}, 5, 0, jupiter_cfg)
        finally:
            recorder.http_get_json = original_binance
            recorder._jupiter_http_get = original_get
            jupiter.fetch_quote = original_fetch

        pools = {
            row["dex"]: row
            for row in conn.execute(
                "SELECT dex, ref_price, basis_bps, amm_key, note FROM pool_samples"
            ).fetchall()
        }
        conn.close()

        self.assertEqual(set(pools), {"Raydium", "Orca"})
        ray = pools["Raydium"]
        self.assertAlmostEqual(ray["ref_price"], 150.20)
        self.assertAlmostEqual(ray["basis_bps"], 19.97, places=1)
        self.assertEqual(ray["amm_key"], "ray-pool-abc")
        self.assertIsNone(ray["note"])
        self.assertAlmostEqual(pools["Orca"]["ref_price"], 150.32)

    def test_migration_adds_onchain_columns(self) -> None:
        conn = common.connect(self.db_path)
        tick_columns = {row[1] for row in conn.execute("PRAGMA table_info(ticks)")}
        pool_columns = {row[1] for row in conn.execute("PRAGMA table_info(pool_samples)")}
        conn.close()
        self.assertTrue({"onchain_ref", "onchain_impact_bps", "basis_bps", "onchain_note"} <= tick_columns)
        self.assertTrue({"dex", "ref_price", "basis_bps", "amm_key"} <= pool_columns)


class DualVenueBasisTests(TempConfigCase):
    JUPITER_PAYLOAD = {
        "inputMint": "So11111111111111111111111111111111111111112",
        "inAmount": "1000000000",
        "outputMint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        "outAmount": "150250000",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "routePlan": [],
    }

    def test_watchlist_jupiter_alias(self) -> None:
        env = {"WATCHLIST": "SOLUSDC", "WATCHLIST_JUPITER": "SOLUSDC:SOLUSDT"}
        watchlists = common.watchlists_from(env)
        self.assertEqual(watchlists["binance"], {"SOLUSDC": "SOLUSDC"})
        self.assertEqual(watchlists["jupiter"], {"SOLUSDC": "SOLUSDT"})

    def test_recorder_records_jupiter_as_separate_source(self) -> None:
        def fake_http(url: str, timeout: int) -> object:
            if "inputMint" in url:
                return self.JUPITER_PAYLOAD
            return [
                {"symbol": "SOLUSDC", "bidPrice": "150.40", "askPrice": "150.60", "bidQty": "1", "askQty": "1"},
            ]

        original_http, recorder.http_get_json = recorder.http_get_json, fake_http
        jupiter_venue = venues.VENUES["jupiter"]
        binance_venue = venues.VENUES["binance"]
        venues.VENUES["jupiter"] = dataclasses.replace(jupiter_venue, base_url="https://example.test")
        venues.VENUES["binance"] = dataclasses.replace(binance_venue, base_url="https://example.test/bookTicker")
        try:
            conn = common.connect(self.db_path)
            recorder.run_tick(
                conn,
                {"binance": {"SOLUSDC": "SOLUSDC"}, "jupiter": {"SOLUSDC": "SOLUSDT"}},
                5,
                0,
            )
        finally:
            recorder.http_get_json = original_http
            venues.VENUES["jupiter"] = jupiter_venue
            venues.VENUES["binance"] = binance_venue

        rows = {
            (row["source"], row["pair"]): row
            for row in conn.execute(
                "SELECT source, pair, mid FROM ticks WHERE note IS NULL"
            ).fetchall()
        }
        conn.close()

        self.assertAlmostEqual(rows[("binance", "SOLUSDC")]["mid"], 150.50)
        self.assertAlmostEqual(rows[("jupiter", "SOLUSDC")]["mid"], 150.25)

    def seed_dual_venue_ticks(self, *, interval_sec: int = 1, count: int = 10) -> tuple[int, int]:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - count * interval_sec
        rows = []
        for index in range(count):
            epoch = base + index * interval_sec
            ts = common.to_ts_utc(common.from_epoch(epoch))
            cex_mid = 150.0 + index * 0.01
            dex_mid = 150.0
            rows.append(
                (
                    ts, epoch, "SOLUSDC", "binance",
                    cex_mid - 0.05, cex_mid + 0.05, cex_mid, 6.6, 1.0, 1.0, None,
                    None, None, None, None,
                )
            )
            rows.append(
                (
                    ts, epoch, "SOLUSDC", "jupiter",
                    dex_mid, dex_mid, dex_mid, 0.0, 0.0, 0.0, None,
                    None, None, None, None,
                )
            )
        recorder.insert_rows(conn, rows)
        conn.close()
        return base, base + (count - 1) * interval_sec

    def test_dual_venue_basis_series_joins_on_ts_epoch(self) -> None:
        start, end = self.seed_dual_venue_ticks(interval_sec=1, count=5)
        conn = common.connect(self.db_path, read_only=True)
        series = analyze.dual_venue_basis_series(conn, "SOLUSDC", start, end)
        conn.close()

        self.assertEqual(len(series), 5)
        self.assertAlmostEqual(series[0]["basis_bps"], 0.0, places=1)
        self.assertAlmostEqual(series[-1]["basis_bps"], 2.67, places=1)
        self.assertEqual(series[0]["cex_mid"], 150.0)
        self.assertEqual(series[0]["dex_mid"], 150.0)

    def test_episode_duration_at_1s_resolution(self) -> None:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - 10
        rows = []
        for index in range(5):
            epoch = base + index
            ts = common.to_ts_utc(common.from_epoch(epoch))
            cex_mid = 151.0 if index >= 2 else 150.0
            dex_mid = 150.0
            for source, mid, spread in (
                ("binance", cex_mid, 6.6),
                ("jupiter", dex_mid, 0.0),
            ):
                bid = mid - 0.05 if source == "binance" else mid
                ask = mid + 0.05 if source == "binance" else mid
                rows.append(
                    (
                        ts, epoch, "SOLUSDC", source,
                        bid, ask, mid, spread, 1.0, 1.0, None,
                        None, None, None, None,
                    )
                )
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        report = analyze.basis_report(
            conn, "SOLUSDC", base, base + 4, floor_bps=50.0, edge_bps=50.0, min_ticks=3
        )
        conn.close()

        self.assertEqual(report["mode"], "dual_venue")
        self.assertAlmostEqual(report["median_tick_sec"], 1.0)
        self.assertEqual(len(report["episodes"]), 1)
        self.assertEqual(report["episodes"][0]["duration_sec"], 2)
        self.assertEqual(report["episodes"][0]["ticks"], 3)

    def test_24h_coverage_flags(self) -> None:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - 86401
        rows = []
        for offset in (0, 43200, 86400):
            epoch = base + offset
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for source, mid in (("binance", 150.0), ("jupiter", 150.0)):
                bid = mid - 0.05 if source == "binance" else mid
                ask = mid + 0.05 if source == "binance" else mid
                rows.append(
                    (
                        ts, epoch, "SOLUSDC", source,
                        bid, ask, mid, 6.6 if source == "binance" else 0.0, 1.0, 1.0, None,
                        None, None, None, None,
                    )
                )
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        end = base + 86401
        report = analyze.basis_report(conn, "SOLUSDC", base, end, min_ticks=1)
        conn.close()

        self.assertTrue(report["coverage"]["cex"]["meets_24h"])
        self.assertTrue(report["coverage"]["dex"]["meets_24h"])
        self.assertTrue(report["coverage"]["paired"]["meets_24h"])

    def test_dual_venue_basis_cli(self) -> None:
        self.seed_dual_venue_ticks(interval_sec=1, count=8)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(
                analyze.main(["--db", str(self.db_path), "--basis", "--since", "1m", "--floor-bps", "5"]),
                0,
            )
        output = buffer.getvalue()
        self.assertIn("binance+jupiter", output)
        self.assertIn("episodes", output)
        self.assertIn("coverage", output)


class BasisAnalysisTests(TempConfigCase):
    def seed_basis_ticks(self) -> None:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - 600
        rows = []
        for index, offset in enumerate(range(0, 300, 60)):
            epoch = base + offset
            mid = 150.0 + index * 0.05
            onchain = 150.0
            basis = recorder.compute_basis_bps(mid, onchain)
            rows.append(
                (
                    common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT", "binance",
                    mid - 0.05, mid + 0.05, mid, 6.6, 1.0, 1.0, None,
                    onchain, 1.0, basis, None,
                )
            )
        recorder.insert_rows(conn, rows)
        conn.close()

    def test_basis_report_finds_held_and_edge(self) -> None:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - 600
        rows = []
        for offset in (0, 60, 120):
            epoch = base + offset
            rows.append(
                (
                    common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT", "binance",
                    149.98, 150.02, 150.0, 2.7, 1.0, 1.0, None,
                    150.0, 0.5, 0.0, None,
                )
            )
        for offset in (180, 240, 300):
            epoch = base + offset
            rows.append(
                (
                    common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT", "binance",
                    150.98, 151.02, 151.0, 2.7, 1.0, 1.0, None,
                    150.0, 0.5, 66.67, None,
                )
            )
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        end = base + 360
        report = analyze.basis_report(conn, "SOLUSDT", base, end, held_bps=10.0, edge_bps=50.0, min_ticks=3)
        conn.close()

        self.assertEqual(report["ticks"], 6)
        self.assertEqual(len(report["held_periods"]), 1)
        self.assertEqual(len(report["edge_periods"]), 1)

    def test_basis_report_per_pool_and_cross_pool(self) -> None:
        conn = common.connect(self.db_path)
        base = common.to_epoch(common.now_utc()) - 300
        ts = common.to_ts_utc(common.from_epoch(base))
        recorder.insert_rows(
            conn,
            [(ts, base, "SOLUSDC", "binance", 150.40, 150.60, 150.50, 13.0, 1.0, 1.0, None, None, None, None, None)],
        )
        recorder.insert_pool_samples(
            conn,
            [
                (ts, base, "SOLUSDC", "Raydium", 150.20, 19.97, 1.0, 1_000_000_000, 150_200_000, "ray", None),
                (ts, base, "SOLUSDC", "Orca", 150.32, 11.98, 0.8, 1_000_000_000, 150_320_000, "orca", None),
            ],
        )
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        report = analyze.basis_report(conn, "SOLUSDC", base - 60, base + 60, held_bps=15.0, edge_bps=40.0, min_ticks=1)
        conn.close()

        self.assertIn("Raydium", report["pools"])
        self.assertIn("Orca", report["pools"])
        self.assertAlmostEqual(report["cross_pool"]["last"]["spread_bps"], 7.99, places=1)
        self.assertEqual(report["cross_pool"]["last"]["best_buy_dex"], "Raydium")
        self.assertEqual(report["cross_pool"]["last"]["best_sell_dex"], "Orca")

    def test_basis_cli(self) -> None:
        self.seed_basis_ticks()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(
                analyze.main(["--db", str(self.db_path), "--basis", "--since", "15m"]),
                0,
            )
        output = buffer.getvalue()
        self.assertIn("SOLUSDT", output)
        self.assertIn("basis", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
