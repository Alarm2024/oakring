#!/usr/bin/env python3
"""Per-pool basis uses the Binance mid even when other venues record the pair."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import common  # noqa: E402
import jupiter  # noqa: E402
import recorder  # noqa: E402
import venues  # noqa: E402

FIXTURE_RAY = Path(__file__).resolve().parent / "fixtures" / "jupiter_quote_raydium.json"


class PoolBasisVenueTests(unittest.TestCase):
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

    def test_pool_basis_uses_the_binance_mid_not_the_last_venue(self) -> None:
        """README: CEX mids for per-pool basis are 'Binance-only today'.

        Binance quotes SOLUSDC at mid 150.50 and kraken at 151.50; the
        Raydium sample ref is 150.20. The stored basis must be against the
        Binance mid ((150.50-150.20)/150.20 = 19.97 bps), not kraken's
        (86.42 bps) - before this fix the last venue in the round won.
        """
        def fake_http(url: str, timeout: int) -> object:
            if "kraken" in url:
                return {"error": [], "result": {"SOLUSDC": {
                    "a": ["151.55", "1", "1"], "b": ["151.45", "1", "1"]}}}
            return [
                {"symbol": "SOLUSDC", "bidPrice": "150.40", "askPrice": "150.60",
                 "bidQty": "1", "askQty": "1"},
            ]

        def fake_jupiter(url: str, timeout: int, headers: dict[str, str]) -> object:
            assert parse_qs(urlparse(url).query).get("dexes") == ["Raydium"]
            return json.loads(FIXTURE_RAY.read_text(encoding="utf-8"))

        original_http, recorder.http_get_json = recorder.http_get_json, fake_http
        original_jup, recorder._jupiter_http_get = recorder._jupiter_http_get, fake_jupiter
        original_fetch, jupiter.fetch_quote = jupiter.fetch_quote, (
            lambda **kwargs: jupiter.parse_quote(
                json.loads(FIXTURE_RAY.read_text(encoding="utf-8")), 9, 6)
        )
        try:
            conn = common.connect(self.db_path)
            jupiter_cfg = jupiter.config_from({
                "JUPITER_ENABLED": "1",
                "JUPITER_ATTACH_PAIRS": "SOLUSDC",
                "JUPITER_DEXES": "Raydium",
            })
            recorder.run_tick(
                conn,
                {"binance": {"SOLUSDC": "SOLUSDC"}, "kraken": {"SOLUSDC": "SOLUSDC"}},
                5,
                0,
                jupiter_cfg,
            )
        finally:
            recorder.http_get_json = original_http
            recorder._jupiter_http_get = original_jup
            jupiter.fetch_quote = original_fetch

        row = conn.execute(
            "SELECT ref_price, basis_bps FROM pool_samples WHERE dex = 'Raydium'"
        ).fetchone()
        conn.close()
        self.assertAlmostEqual(row["ref_price"], 150.20)
        self.assertAlmostEqual(row["basis_bps"], 19.97, places=1)


if __name__ == "__main__":
    unittest.main()
