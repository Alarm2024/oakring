#!/usr/bin/env python3
"""Parser tests against each venue's documented response shape.

These payloads are the contract: if a venue changes its response, the matching
test is what should fail first. Live verification is `recorder.py --probe`,
which is the only thing that can prove the real endpoints still match.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from contextlib import redirect_stdout  # noqa: E402

import analyze  # noqa: E402
import common  # noqa: E402
import recorder  # noqa: E402
import venues  # noqa: E402

SAMPLES = {
    "binance": {
        "symbol": "SOLUSDC",
        "payload": {"symbol": "SOLUSDC", "bidPrice": "100.48", "bidQty": "51.5",
                    "askPrice": "100.49", "askQty": "43.2"},
    },
    "coinbase": {
        "symbol": "SOL-USDC",
        "payload": {"bids": [["100.47", "12.5", 3]], "asks": [["100.50", "8.1", 2]],
                    "sequence": 123456789},
    },
    "kraken": {
        "symbol": "SOLUSDC",
        "payload": {"error": [], "result": {"SOLUSDC": {
            "a": ["100.510000", "1", "1.000"], "b": ["100.460000", "2", "2.000"],
            "c": ["100.48", "1.5"], "v": ["100", "200"]}}},
    },
    "okx": {
        "symbol": "SOL-USDC",
        "payload": {"code": "0", "msg": "", "data": [{"instId": "SOL-USDC",
                    "bidPx": "100.45", "bidSz": "30", "askPx": "100.52", "askSz": "25"}]},
    },
    "bybit": {
        "symbol": "SOLUSDC",
        "payload": {"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": [
            {"symbol": "SOLUSDC", "bid1Price": "100.44", "bid1Size": "60",
             "ask1Price": "100.53", "ask1Size": "55"}]}},
    },
}


class ParserTests(unittest.TestCase):
    def test_every_venue_parses_its_own_shape(self) -> None:
        for name, sample in SAMPLES.items():
            with self.subTest(venue=name):
                venue = venues.get(name)
                quotes = venue.parse(sample["payload"], sample["symbol"])
                quote = quotes[sample["symbol"]]
                self.assertGreater(quote.bid, 100.0, name)
                self.assertGreater(quote.ask, quote.bid, name)
                self.assertGreater(quote.bid_qty, 0.0, name)
                self.assertGreater(quote.ask_qty, 0.0, name)
                # Every venue must agree on SOL/USDC to within a basis point or so.
                self.assertAlmostEqual((quote.bid + quote.ask) / 2, 100.485, delta=0.06)

    def test_symbol_spelling_per_venue(self) -> None:
        self.assertEqual(venues.get("binance").to_symbol("SOLUSDC"), "SOLUSDC")
        self.assertEqual(venues.get("coinbase").to_symbol("SOLUSDC"), "SOL-USDC")
        self.assertEqual(venues.get("okx").to_symbol("SOLUSDC"), "SOL-USDC")
        self.assertEqual(venues.get("kraken").to_symbol("SOLUSDC"), "SOLUSDC")
        self.assertEqual(venues.get("bybit").to_symbol("SOLUSDC"), "SOLUSDC")

    def test_pair_splitting(self) -> None:
        self.assertEqual(venues.split_pair("SOLUSDC"), ("SOL", "USDC"))
        self.assertEqual(venues.split_pair("SOLUSDT"), ("SOL", "USDT"))
        self.assertEqual(venues.split_pair("BTCUSD"), ("BTC", "USD"))
        self.assertEqual(venues.split_pair("ETHBTC"), ("ETH", "BTC"))
        with self.assertRaises(venues.VenueError):
            venues.split_pair("NOTAPAIR")

    def test_kraken_answers_under_its_own_pair_name(self) -> None:
        """Kraken calls BTC 'XBT', so the response key is not our spelling."""
        payload = {"error": [], "result": {"XXBTZUSD": {
            "a": ["77000.1", "1", "1.0"], "b": ["77000.0", "1", "2.0"]}}}
        quotes = venues.get("kraken").parse(payload, "BTCUSD")
        self.assertAlmostEqual(quotes["BTCUSD"].bid, 77000.0)

    def test_error_responses_are_reported_not_parsed(self) -> None:
        cases = [
            ("kraken", {"error": ["EQuery:Unknown asset pair"], "result": {}}),
            ("okx", {"code": "51001", "msg": "Instrument ID does not exist", "data": []}),
            ("bybit", {"retCode": 10001, "retMsg": "params error", "result": {}}),
            ("coinbase", {"message": "NotFound"}),
            ("binance", {"code": -1121, "msg": "Invalid symbol."}),
        ]
        for name, payload in cases:
            with self.subTest(venue=name):
                with self.assertRaises(venues.VenueError):
                    venues.get(name).parse(payload, "SOLUSDC")

    def test_malformed_numbers_are_rejected(self) -> None:
        payload = {"symbol": "SOLUSDC", "bidPrice": "oops", "askPrice": "1"}
        with self.assertRaises(venues.VenueError):
            venues.get("binance").parse(payload, "SOLUSDC")

    def test_unknown_venue_names_the_known_ones(self) -> None:
        with self.assertRaises(venues.VenueError) as caught:
            venues.get("mtgox")
        self.assertIn("coinbase", str(caught.exception))

    def test_urls_carry_the_venue_symbol(self) -> None:
        for name, sample in SAMPLES.items():
            with self.subTest(venue=name):
                venue = venues.get(name)
                url = venue.build_url(venue.base_url, [sample["symbol"]])
                self.assertTrue(url.startswith("https://"), url)
                self.assertIn(sample["symbol"].replace("/", "%2F"), url)


class MultiVenueRecordingTests(unittest.TestCase):
    """One loopback server impersonating several venues at once."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._previous = os.environ.get("OAKRING_CONFIG_DIR")
        os.environ["OAKRING_CONFIG_DIR"] = str(self.tmp / "config")
        self.db_path = self.tmp / "ring.db"
        self.hits: list[str] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                hits.append(self.path)
                if "/products/" in self.path:
                    body = SAMPLES["coinbase"]["payload"]
                elif "/0/public/Ticker" in self.path:
                    body = SAMPLES["kraken"]["payload"]
                elif "/api/v5/" in self.path:
                    body = SAMPLES["okx"]["payload"]
                elif "/v5/market/tickers" in self.path:
                    body = SAMPLES["bybit"]["payload"]
                else:
                    body = SAMPLES["binance"]["payload"]
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self._original = dict(venues.VENUES)
        for name, venue in venues.VENUES.items():
            suffix = "/api/v3/ticker/bookTicker" if name == "binance" else ""
            venues.VENUES[name] = dataclasses.replace(venue, base_url=base + suffix)

    def tearDown(self) -> None:
        venues.VENUES.clear()
        venues.VENUES.update(self._original)
        self.server.shutdown()
        self.server.server_close()
        if self._previous is None:
            os.environ.pop("OAKRING_CONFIG_DIR", None)
        else:
            os.environ["OAKRING_CONFIG_DIR"] = self._previous
        self._tmp.cleanup()

    def test_one_tick_records_every_venue_under_one_timestamp(self) -> None:
        conn = common.connect(self.db_path)
        recorder.run_tick(
            conn,
            {"binance": ["SOLUSDC"], "coinbase": ["SOLUSDC"], "kraken": ["SOLUSDC"], "okx": ["SOLUSDC"]},
            5,
            0,
        )
        rows = conn.execute("SELECT source, pair, mid, ts_epoch, note FROM ticks ORDER BY source").fetchall()
        conn.close()

        self.assertEqual([row["source"] for row in rows], ["binance", "coinbase", "kraken", "okx"])
        self.assertTrue(all(row["note"] is None for row in rows))
        self.assertEqual(len({row["ts_epoch"] for row in rows}), 1,
                         "all venues in a tick must share one timestamp for comparison to be exact")
        for row in rows:
            self.assertAlmostEqual(row["mid"], 100.485, delta=0.06)

    def test_one_venue_failing_does_not_stop_the_others(self) -> None:
        venues.VENUES["kraken"] = dataclasses.replace(
            venues.VENUES["kraken"], base_url="http://127.0.0.1:1"
        )
        conn = common.connect(self.db_path)
        recorder.run_tick(conn, {"binance": ["SOLUSDC"], "kraken": ["SOLUSDC"]}, 2, 0)
        rows = {row["source"]: row for row in conn.execute("SELECT source, mid, note FROM ticks")}
        conn.close()

        self.assertIsNotNone(rows["binance"]["mid"])
        self.assertIsNone(rows["kraken"]["mid"])
        self.assertTrue(rows["kraken"]["note"].startswith("error:"))


class VenueComparisonTests(unittest.TestCase):
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

    def seed(self, books: dict[str, tuple[float, float]], ticks: int = 5, skip: str = "") -> int:
        """books: venue -> (bid, ask), repeated for `ticks` rounds."""
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        now -= now % 60
        rows = []
        for index in range(ticks):
            epoch = now - (ticks - index) * 60
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for venue, (bid, ask) in books.items():
                if venue == skip and index == ticks - 1:
                    continue  # this venue missed the final round
                mid = (bid + ask) / 2
                rows.append((ts, epoch, "SOLUSDC", venue, bid, ask, mid,
                             (ask - bid) / mid * 10000, 5.0, 5.0, None))
        recorder.insert_rows(conn, rows)
        conn.close()
        return now

    def test_gap_between_venues_is_measured(self) -> None:
        self.seed({"binance": (100.00, 100.02), "kraken": (100.10, 100.14)})
        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        report = analyze.venue_report(conn, "SOLUSDC", now - 3600, now)
        conn.close()

        self.assertEqual(report["venues"], ["binance", "kraken"])
        self.assertEqual(report["last"]["cheapest"], "binance")
        self.assertEqual(report["last"]["dearest"], "kraken")
        # mids 100.01 vs 100.12 -> ~11 bps apart
        self.assertAlmostEqual(report["last"]["spread_bps"], 11.0, delta=0.5)
        # kraken's bid (100.10) sits above binance's ask (100.02): a crossed book
        self.assertGreater(report["last"]["cross_bps"], 0)
        self.assertEqual(report["crossed_pct"], 100.0)

    def test_venues_that_agree_show_no_cross(self) -> None:
        self.seed({"binance": (100.00, 100.02), "okx": (100.00, 100.02)})
        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        report = analyze.venue_report(conn, "SOLUSDC", now - 3600, now)
        conn.close()
        self.assertAlmostEqual(report["last"]["spread_bps"], 0.0, places=6)
        self.assertEqual(report["crossed_ticks"], 0)

    def test_a_tick_only_one_venue_priced_is_skipped(self) -> None:
        """Comparing a fresh book against a missing one would invent a gap."""
        now = self.seed({"binance": (100.0, 100.02), "kraken": (100.1, 100.14)}, ticks=5, skip="kraken")
        conn = common.connect(self.db_path, read_only=True)
        series = analyze.venue_series(conn, "SOLUSDC", now - 3600, now)
        conn.close()
        self.assertEqual(len(series), 4, "the round kraken missed must not be compared")

    def test_one_venue_alone_is_not_a_comparison(self) -> None:
        self.seed({"binance": (100.0, 100.02)})
        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        report = analyze.venue_report(conn, "SOLUSDC", now - 3600, now)
        conn.close()
        self.assertIn("two venues", report["status"])

    def test_cli_states_the_cost_caveat(self) -> None:
        self.seed({"binance": (100.00, 100.02), "kraken": (100.10, 100.14)})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--venues", "--since", "1h"]), 0)
        output = buffer.getvalue()
        self.assertIn("binance", output)
        self.assertIn("kraken", output)
        self.assertIn("not free money", output)

        buffer = io.StringIO()
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            # Only one venue recorded for this pair: nothing to compare.
            conn = common.connect(self.db_path)
            conn.execute("DELETE FROM ticks WHERE source = 'kraken'")
            conn.commit()
            conn.close()
            with redirect_stdout(buffer):
                self.assertEqual(analyze.main(["--db", str(self.db_path), "--venues"]), 1)
            self.assertIn("WATCHLIST_COINBASE", sys.stderr.getvalue())
        finally:
            sys.stderr = stderr

    def test_analysis_keeps_venues_apart(self) -> None:
        """Two venues' books must never fold into one series."""
        self.seed({"binance": (100.0, 100.02), "kraken": (200.0, 200.04)}, ticks=30)
        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        binance = analyze.load_bars(conn, "SOLUSDC", now - 7200, now, 60, "binance")
        kraken = analyze.load_bars(conn, "SOLUSDC", now - 7200, now, 60, "kraken")
        mixed = analyze.load_bars(conn, "SOLUSDC", now - 7200, now, 60)
        conn.close()

        self.assertTrue(all(abs(bar.close - 100.01) < 0.01 for bar in binance))
        self.assertTrue(all(abs(bar.close - 200.02) < 0.01 for bar in kraken))
        # Without the filter the two venues interleave, which is exactly why
        # every analysis path passes a source.
        self.assertTrue(any(bar.high - bar.low > 50 for bar in mixed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
