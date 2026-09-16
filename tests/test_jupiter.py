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

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import analyze  # noqa: E402
import common  # noqa: E402
import jupiter  # noqa: E402
import recorder  # noqa: E402
import venues  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jupiter_quote_sol_usdc.json"


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
        self.assertAlmostEqual(quote.impact_bps, 1.25)
        self.assertEqual(quote.in_amount, 1_000_000_000)
        self.assertEqual(quote.out_amount, 150_250_000)

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

        def fake_jupiter(url: str, timeout: int, headers: dict[str, str]) -> object:
            return fixture

        original_binance, recorder.http_get_json = recorder.http_get_json, fake_binance
        original_jupiter, jupiter.fetch_quote = jupiter.fetch_quote, (
            lambda **kwargs: jupiter.parse_quote(fixture, 9, 6)
        )
        try:
            conn = common.connect(self.db_path)
            venues.VENUES["binance"] = dataclasses.replace(
                venues.VENUES["binance"], base_url="https://example.test"
            )
            jupiter_cfg = {
                "enabled": True,
                "attach_pairs": {"SOLUSDT"},
                "quote_url": "https://example.test/quote",
                "input_mint": jupiter.DEFAULT_INPUT_MINT,
                "output_mint": jupiter.DEFAULT_OUTPUT_MINT,
                "amount": 1_000_000_000,
                "slippage_bps": 50,
                "input_decimals": 9,
                "output_decimals": 6,
                "api_key": None,
            }
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

    def test_migration_adds_onchain_columns(self) -> None:
        conn = common.connect(self.db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ticks)")}
        conn.close()
        self.assertTrue({"onchain_ref", "onchain_impact_bps", "basis_bps", "onchain_note"} <= columns)


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
        # held: basis near zero for 3 ticks
        for offset in (0, 60, 120):
            epoch = base + offset
            rows.append(
                (
                    common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT", "binance",
                    149.98, 150.02, 150.0, 2.7, 1.0, 1.0, None,
                    150.0, 0.5, 0.0, None,
                )
            )
        # edge: basis wide for 3 ticks
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
        self.assertAlmostEqual(report["held_periods"][0]["mean_bps"], 0.0, places=1)
        self.assertGreater(report["edge_periods"][0]["mean_bps"], 50.0)

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
        self.assertIn("on-chain", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
