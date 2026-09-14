#!/usr/bin/env python3
"""Offline tests for oakring. No network, no writes outside a temp dir."""

from __future__ import annotations

import io
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import analyze  # noqa: E402
import common  # noqa: E402
import recorder  # noqa: E402


class TempConfigCase(unittest.TestCase):
    """Point OAKRING_CONFIG_DIR at a scratch dir so nothing touches the host."""

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


class DurationTests(unittest.TestCase):
    def test_parse_units(self) -> None:
        self.assertEqual(common.parse_duration("90"), 90)
        self.assertEqual(common.parse_duration("15m"), 900)
        self.assertEqual(common.parse_duration("4h"), 14400)
        self.assertEqual(common.parse_duration("7d"), 604800)
        self.assertEqual(common.parse_duration("2w"), 1209600)

    def test_rejects_bad_input(self) -> None:
        for bad in ("", "abc", "0h", "-3d"):
            with self.assertRaises(ValueError):
                common.parse_duration(bad)

    def test_format(self) -> None:
        self.assertEqual(common.format_duration(45), "45s")
        self.assertEqual(common.format_duration(900), "15.0m")
        self.assertEqual(common.format_duration(3600), "1.0h")
        self.assertEqual(common.format_duration(9000), "2.5h")
        self.assertEqual(common.format_duration(259200), "3.0d")


class ConfigTests(TempConfigCase):
    def test_env_file_parsing_and_precedence(self) -> None:
        config = Path(os.environ["OAKRING_CONFIG_DIR"])
        config.mkdir(parents=True)
        (config / ".env").write_text(
            "# comment\nWATCHLIST=solusdt, BTCUSDT ,solusdt\nINTERVAL_SEC=30\nBINANCE_URL='https://example.test'\n",
            encoding="utf-8",
        )
        env = common.load_config()
        self.assertEqual(common.watchlist_from(env), ["SOLUSDT", "BTCUSDT"])
        self.assertEqual(common.env_int(env, "INTERVAL_SEC", 60), 30)
        self.assertEqual(env["BINANCE_URL"], "https://example.test")

        os.environ["INTERVAL_SEC"] = "5"
        try:
            self.assertEqual(common.env_int(common.load_config(), "INTERVAL_SEC", 60), 5)
        finally:
            os.environ.pop("INTERVAL_SEC")

    def test_env_int_guards(self) -> None:
        self.assertEqual(common.env_int({"X": "not-a-number"}, "X", 7), 7)
        self.assertEqual(common.env_int({"X": "0"}, "X", 60, minimum=1), 1)
        self.assertEqual(common.env_int({}, "X", 60), 60)


class MigrationTests(TempConfigCase):
    def test_v1_database_is_migrated_and_backfilled(self) -> None:
        legacy = sqlite3.connect(str(self.db_path))
        legacy.executescript(
            """
            CREATE TABLE ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL, pair TEXT NOT NULL, source TEXT NOT NULL,
                bid REAL, ask REAL, mid REAL, spread_bps REAL, note TEXT
            );
            INSERT INTO ticks (ts_utc, pair, source, bid, ask, mid, spread_bps, note)
            VALUES ('2026-01-01T00:00:00Z', 'SOLUSDT', 'binance', 1.0, 1.1, 1.05, 952.4, NULL);
            """
        )
        legacy.commit()
        legacy.close()

        conn = common.connect(self.db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ticks)")}
        self.assertTrue({"ts_epoch", "bid_qty", "ask_qty"} <= columns)

        epoch = conn.execute("SELECT ts_epoch FROM ticks").fetchone()[0]
        self.assertEqual(epoch, 1767225600)  # 2026-01-01T00:00:00Z
        conn.close()

    def test_permissions(self) -> None:
        common.ensure_permissions(self.db_path)
        conn = common.connect(self.db_path)
        conn.close()
        self.assertEqual(Path(os.environ["OAKRING_CONFIG_DIR"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)


class RowBuildingTests(unittest.TestCase):
    def test_valid_payload(self) -> None:
        row = recorder.row_from_payload(
            "2026-01-01T00:00:00Z", 1767225600, "SOLUSDT",
            {"symbol": "SOLUSDT", "bidPrice": "100.0", "askPrice": "100.10", "bidQty": "5", "askQty": "3"},
            None,
        )
        self.assertAlmostEqual(row[6], 100.05)
        self.assertAlmostEqual(row[7], 9.995, places=2)
        self.assertEqual(row[8], 5.0)
        self.assertIsNone(row[10])

    def test_missing_and_bad_payloads(self) -> None:
        missing = recorder.row_from_payload("t", 1, "X", None, "error:URLError")
        self.assertEqual(missing[10], "error:URLError")
        self.assertIsNone(missing[6])

        bad = recorder.row_from_payload("t", 1, "X", {"bidPrice": "oops", "askPrice": "1"}, None)
        self.assertEqual(bad[10], "error:BadPayload")

        crossed = recorder.row_from_payload("t", 1, "X", {"bidPrice": "10", "askPrice": "9"}, None)
        self.assertEqual(crossed[10], "error:CrossedBook")
        self.assertIsNone(crossed[6])

    def test_tick_alignment_never_drifts(self) -> None:
        self.assertEqual(recorder.next_tick_at(1767225601.4, 60), 1767225660)
        self.assertEqual(recorder.next_tick_at(1767225660.0, 60), 1767225720)


class SignalHandlerTests(unittest.TestCase):
    def test_handler_sets_the_event_without_logging(self) -> None:
        """Logging from a signal handler raises a reentrant BufferedWriter call
        (or deadlocks on the logging lock) when the signal lands mid-write."""
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = Capture()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)  # otherwise a silent root makes this vacuous
        try:
            recorder._shutdown.clear()
            recorder._shutdown_signal = None
            recorder._handle_signal(signal.SIGTERM, None)

            self.assertTrue(recorder._shutdown.is_set())
            self.assertEqual(recorder._shutdown_signal, signal.SIGTERM)
            self.assertEqual([record.getMessage() for record in records], [])
        finally:
            root.removeHandler(capture)
            root.setLevel(previous_level)
            recorder._shutdown.clear()
            recorder._shutdown_signal = None


class RecorderLoopTests(TempConfigCase):
    def test_run_tick_writes_one_row_per_pair(self) -> None:
        calls: list[str] = []

        def fake_get(url: str, timeout: int) -> object:
            calls.append(url)
            return [
                {"symbol": "SOLUSDT", "bidPrice": "100", "askPrice": "100.1", "bidQty": "2", "askQty": "2"},
                {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60006", "bidQty": "1", "askQty": "4"},
            ]

        original, recorder.http_get_json = recorder.http_get_json, fake_get
        try:
            conn = common.connect(self.db_path)
            recorder.run_tick(conn, "https://example.test", ["SOLUSDT", "BTCUSDT"], 5, 0)
        finally:
            recorder.http_get_json = original

        self.assertEqual(len(calls), 1, "watchlist should be fetched in a single batched request")
        rows = conn.execute("SELECT pair, mid, note FROM ticks ORDER BY pair").fetchall()
        self.assertEqual([row["pair"] for row in rows], ["BTCUSDT", "SOLUSDT"])
        self.assertTrue(all(row["note"] is None for row in rows))
        conn.close()

    def test_failed_fetch_still_records_the_gap(self) -> None:
        def always_fails(url: str, timeout: int) -> object:
            raise OSError("no network")

        original, recorder.http_get_json = recorder.http_get_json, always_fails
        try:
            conn = common.connect(self.db_path)
            recorder.run_tick(conn, "https://example.test", ["SOLUSDT"], 1, 0)
        finally:
            recorder.http_get_json = original

        row = conn.execute("SELECT mid, note FROM ticks").fetchone()
        self.assertIsNone(row["mid"])
        self.assertEqual(row["note"], "error:OSError")
        conn.close()

    def test_prune_respects_retention(self) -> None:
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        conn.executemany(
            "INSERT INTO ticks (ts_utc, ts_epoch, pair, source, mid) VALUES (?, ?, 'X', 'binance', 1.0)",
            [(common.to_ts_utc(common.from_epoch(now - offset)), now - offset) for offset in (10, 86400 * 5)],
        )
        conn.commit()
        self.assertEqual(recorder.prune(conn, 2), 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0], 1)
        self.assertEqual(recorder.prune(conn, 0), 0)
        conn.close()


def seed_cycle_db(db_path: Path, pair: str = "SOLUSDT", days: int = 14, period_hours: int = 48) -> int:
    """Hourly ticks tracing a clean sine wave plus drift, so cycles are known."""
    conn = common.connect(db_path)
    end = common.to_epoch(common.now_utc())
    end -= end % 3600
    start = end - days * 86400
    rows = []
    for index, epoch in enumerate(range(start, end, 3600)):
        phase = 2.0 * math.pi * index / period_hours
        mid = 100.0 * (1.0 + 0.05 * math.sin(phase)) + 0.02 * index
        bid, ask = mid * 0.9999, mid * 1.0001
        rows.append(
            (
                common.to_ts_utc(common.from_epoch(epoch)), epoch, pair, "binance",
                bid, ask, mid, (ask - bid) / mid * 10000.0, 3.0, 1.0, None,
            )
        )
    recorder.insert_rows(conn, rows)
    conn.close()
    return len(rows)


class LocalServerTests(TempConfigCase):
    """Exercise real URL construction and HTTP parsing against a loopback server."""

    def setUp(self) -> None:
        super().setUp()
        quotes = {
            "SOLUSDT": {"symbol": "SOLUSDT", "bidPrice": "100.00", "askPrice": "100.10", "bidQty": "9", "askQty": "1"},
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "60000.0", "askPrice": "60012.0", "bidQty": "1", "askQty": "1"},
        }
        self.requests: list[str] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                requests.append(self.path)
                query = parse_qs(urlparse(self.path).query)
                if "symbols" in query:
                    symbols = json.loads(query["symbols"][0])
                    body = [quotes[symbol] for symbol in symbols if symbol in quotes]
                    missing = [symbol for symbol in symbols if symbol not in quotes]
                    if missing:  # Binance rejects the whole batch on an unknown symbol
                        self.send_error(400, "Invalid symbol")
                        return
                elif "symbol" in query:
                    symbol = query["symbol"][0]
                    if symbol not in quotes:
                        self.send_error(400, "Invalid symbol")
                        return
                    body = quotes[symbol]
                else:
                    self.send_error(400, "missing symbol")
                    return
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
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v3/ticker/bookTicker"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_single_pair_uses_symbol_and_batch_uses_symbols(self) -> None:
        one = recorder.fetch_batch(self.url, ["SOLUSDT"], 5)
        self.assertEqual(one["SOLUSDT"]["bidPrice"], "100.00")
        self.assertIn("symbol=SOLUSDT", self.requests[-1])

        many = recorder.fetch_batch(self.url, ["SOLUSDT", "BTCUSDT"], 5)
        self.assertEqual(sorted(many), ["BTCUSDT", "SOLUSDT"])
        self.assertIn("symbols=", self.requests[-1])

    def test_unknown_symbol_falls_back_so_good_pairs_still_record(self) -> None:
        conn = common.connect(self.db_path)
        recorder.run_tick(conn, self.url, ["SOLUSDT", "NOPEUSDT"], 5, 0)
        rows = {row["pair"]: row for row in conn.execute("SELECT pair, mid, note FROM ticks")}
        self.assertAlmostEqual(rows["SOLUSDT"]["mid"], 100.05)
        self.assertIsNone(rows["SOLUSDT"]["note"])
        self.assertEqual(rows["NOPEUSDT"]["note"], "error:Missing")
        conn.close()

    def test_end_to_end_tick_then_report(self) -> None:
        conn = common.connect(self.db_path)
        for _ in range(3):
            recorder.run_tick(conn, self.url, ["SOLUSDT"], 5, 0)
        conn.close()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            analyze.main(["--db", str(self.db_path), "--since", "1h", "--bucket", "60"])
        output = buffer.getvalue()
        self.assertIn("SOLUSDT", output)
        self.assertIn("9.99", output)  # ~10 bps spread on a 100.00/100.10 book


class AnalysisTests(TempConfigCase):
    def test_bars_bucket_and_skip_error_only_buckets(self) -> None:
        conn = common.connect(self.db_path)
        base = 1767225600
        recorder.insert_rows(
            conn,
            [
                ("t", base + 0, "X", "binance", 1.0, 1.02, 1.01, 198.0, 1.0, 1.0, None),
                ("t", base + 30, "X", "binance", 1.0, 1.06, 1.03, 582.0, 1.0, 3.0, None),
                ("t", base + 3600, "X", "binance", None, None, None, None, None, None, "error:URLError"),
                ("t", base + 7200, "X", "binance", 1.0, 1.04, 1.02, 392.0, 2.0, 1.0, None),
            ],
        )
        bars = analyze.load_bars(conn, "X", base, base + 10800, 3600)
        self.assertEqual(len(bars), 2, "the all-errors bucket carries no price and is dropped")
        self.assertAlmostEqual(bars[0].open, 1.01)
        self.assertAlmostEqual(bars[0].high, 1.03)
        self.assertAlmostEqual(bars[0].close, 1.03)
        self.assertEqual(bars[0].ticks, 2)
        self.assertLess(bars[0].imbalance, 0)  # more ask size than bid size
        conn.close()

    def test_pivots_follow_a_known_wave(self) -> None:
        bars = [
            analyze.Bar(i * 3600, 0, 0, 0, 100.0 * (1 + 0.05 * math.sin(2 * math.pi * i / 48)), 1, 0, None, None)
            for i in range(48 * 6)
        ]
        pivots = analyze.find_pivots(bars, 2.0)
        self.assertNotEqual(pivots[0].index, 0, "a pivot on the first bar is a window artifact")
        peaks = [p for p in pivots if p.kind == "peak"]
        troughs = [p for p in pivots if p.kind == "trough"]
        self.assertGreaterEqual(len(peaks), 4)
        self.assertGreaterEqual(len(troughs), 4)
        self.assertTrue(all(p.price > 104 for p in peaks))
        self.assertTrue(all(p.price < 96 for p in troughs))
        gaps = [peaks[i].epoch - peaks[i - 1].epoch for i in range(1, len(peaks))]
        for gap in gaps:
            self.assertAlmostEqual(gap / 3600.0, 48, delta=2)

    def test_pivots_alternate(self) -> None:
        bars = [
            analyze.Bar(i * 60, 0, 0, 0, 100.0 + (i % 17) - 8, 1, 0, None, None)
            for i in range(500)
        ]
        pivots = analyze.find_pivots(bars, 3.0)
        kinds = [pivot.kind for pivot in pivots]
        self.assertTrue(all(a != b for a, b in zip(kinds, kinds[1:])), "peaks and troughs must alternate")

    def test_periodogram_recovers_the_period(self) -> None:
        values = [math.log(100.0 * (1 + 0.05 * math.sin(2 * math.pi * i / 48))) for i in range(48 * 8)]
        dominant = analyze.periodogram(values, 3600)
        self.assertTrue(dominant)
        self.assertAlmostEqual(dominant[0]["period_bars"], 48, delta=3)
        self.assertEqual(dominant[0]["period_human"], "2.0d")

    def test_periodogram_needs_enough_bars(self) -> None:
        self.assertEqual(analyze.periodogram([1.0, 2.0, 3.0], 60), [])

    def test_report_on_seeded_cycles(self) -> None:
        seed_cycle_db(self.db_path)
        conn = common.connect(self.db_path, read_only=True)
        end = common.to_epoch(common.now_utc())
        start = end - 15 * 86400
        bars = analyze.load_bars(conn, "SOLUSDT", start, end, 3600)
        window = {
            "start": common.to_ts_utc(common.from_epoch(start)),
            "end": common.to_ts_utc(common.from_epoch(end)),
            "start_epoch": start,
            "end_epoch": end,
            "length": "15.0d",
        }
        report = analyze.analyse_pair(bars, "SOLUSDT", 3600, 2.0, window)
        conn.close()

        self.assertGreaterEqual(report.swings["completed_cycles"], 4)
        self.assertIsNotNone(report.swings["mean_cycle_human"])
        self.assertAlmostEqual(report.swings["mean_cycle_sec"] / 3600.0, 48, delta=4)
        self.assertAlmostEqual(report.periodicity["dominant"][0]["period_bars"], 48, delta=4)
        self.assertGreater(report.trend["slope_pct_per_day"], 0)
        self.assertIn(report.phase["phase"], {"accumulation", "markup", "distribution", "markdown", "ranging"})
        self.assertEqual(report.coverage["gap_count"], 0)
        self.assertEqual(report.coverage["recorded_span_pct"], 100.0)
        self.assertLess(report.coverage["coverage_pct"], 100.0)  # window is wider than the recording
        text = analyze.render_text(report)
        self.assertIn("periodicity", text)
        self.assertIn("SOLUSDT", text)


class LatestSnapshotTests(TempConfigCase):
    def seed(self) -> int:
        """SOLUSDT with a day of history, BTCUSDT only minutes old - the state a
        freshly added pair is actually in."""
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        last = now - 60  # the most recent tick; lookbacks are measured from it
        rows = []
        for epoch, mid in ((last - 86400, 100.0), (last - 3600, 101.0), (last, 102.0)):
            rows.append(
                (common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT",
                 "binance", mid - 0.01, mid + 0.01, mid, 1.94, 1.0, 1.0, None)
            )
        rows.append(
            (common.to_ts_utc(common.from_epoch(now - 30)), now - 30, "BTCUSDT",
             "binance", 78000.0, 78001.0, 78000.5, 0.13, 1.0, 1.0, None)
        )
        # An error row after the last good tick must not become "the price".
        rows.append(
            (common.to_ts_utc(common.from_epoch(now - 10)), now - 10, "SOLUSDT",
             "binance", None, None, None, None, None, None, "error:URLError")
        )
        recorder.insert_rows(conn, rows)
        conn.close()
        return now

    def test_snapshot_prices_changes_and_missing_history(self) -> None:
        self.seed()
        conn = common.connect(self.db_path, read_only=True)
        snapshot = {entry["pair"]: entry for entry in analyze.latest_snapshot(conn, ["SOLUSDT", "BTCUSDT"])}
        conn.close()

        sol = snapshot["SOLUSDT"]
        self.assertEqual(sol["mid"], 102.0, "an error row must not shadow the last priced tick")
        self.assertAlmostEqual(sol["change_1h_pct"], 0.99, places=1)   # 101 -> 102
        self.assertAlmostEqual(sol["change_24h_pct"], 2.0, places=1)   # 100 -> 102
        self.assertIsNone(sol["change_7d_pct"], "no week of history yet")

        btc = snapshot["BTCUSDT"]
        self.assertEqual(btc["mid"], 78000.5)
        self.assertIsNone(btc["change_1h_pct"], "a half-hour-old pair cannot report a 1h change")
        self.assertLess(btc["age_sec"], 120)

    def test_pair_with_no_priced_ticks(self) -> None:
        conn = common.connect(self.db_path)
        recorder.insert_rows(
            conn,
            [("t", common.to_epoch(common.now_utc()), "NEWUSDT", "binance",
              None, None, None, None, None, None, "error:URLError")],
        )
        conn.close()
        conn = common.connect(self.db_path, read_only=True)
        entry = analyze.latest_snapshot(conn, ["NEWUSDT"])[0]
        conn.close()
        self.assertIn("no priced ticks", entry["status"])

    def test_stale_marker_and_cli(self) -> None:
        self.seed()
        rendered = analyze.render_latest(
            [{"pair": "SOLUSDT", "mid": 102.0, "spread_bps": 1.94, "age_sec": 900,
              "age_human": "15.0m", "change_1h_pct": 1.0, "change_24h_pct": None,
              "change_7d_pct": None}],
            stale_after=300,
        )
        self.assertIn("stale", rendered)
        self.assertIn("+1.00%", rendered)
        self.assertIn("-", rendered)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--latest"]), 0)
        output = buffer.getvalue()
        self.assertIn("SOLUSDT", output)
        self.assertIn("BTCUSDT", output)
        self.assertNotIn("stale", output)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--latest", "--format", "json"]), 0)
        self.assertEqual(len(json.loads(buffer.getvalue())["latest"]), 2)


class PriceFormatTests(unittest.TestCase):
    def test_precision_follows_magnitude(self) -> None:
        self.assertEqual(analyze.price_fmt(78572.34), "78,572.34")
        self.assertEqual(analyze.price_fmt(102.875), "102.8750")
        self.assertEqual(analyze.price_fmt(0.99985), "0.99985000")

    def test_stablecoin_peg_deviation_stays_visible(self) -> None:
        # A 1.5 bps premium on USDC must not round to a flat 1.0000.
        self.assertEqual(analyze.price_fmt(1.00015), "1.000150")
        self.assertNotEqual(analyze.price_fmt(1.00015), analyze.price_fmt(1.00005))


class AutoSettingsTests(TempConfigCase):
    def test_bucket_scales_with_how_much_was_recorded(self) -> None:
        self.assertEqual(analyze.choose_bucket(47 * 3600), 900)      # two days -> 15m
        self.assertEqual(analyze.choose_bucket(7 * 86400), 3600)     # a week   -> 1h
        self.assertEqual(analyze.choose_bucket(30 * 86400), 14400)   # a month  -> 4h
        self.assertEqual(analyze.choose_bucket(600), 60)             # ten minutes clamps to the floor
        self.assertEqual(analyze.choose_bucket(5 * 365 * 86400), 86400)  # and to the ceiling

    def test_swing_scales_with_volatility(self) -> None:
        quiet = [analyze.Bar(i * 900, 0, 0, 0, 100.0 * (1 + 0.0005 * ((-1) ** i)), 1, 0, None, None) for i in range(60)]
        wild = [analyze.Bar(i * 900, 0, 0, 0, 100.0 * (1 + 0.02 * ((-1) ** i)), 1, 0, None, None) for i in range(60)]
        self.assertLess(analyze.choose_swing(quiet), analyze.choose_swing(wild))
        self.assertGreaterEqual(analyze.choose_swing(quiet), 0.1)
        self.assertLessEqual(analyze.choose_swing(wild), 10.0)
        self.assertEqual(analyze.choose_swing([]), 0.5, "no data falls back rather than dividing by zero")

    def test_suggestion_names_a_bucket_that_fits(self) -> None:
        # Two days of data asked for in 4h buckets: the note should point at 15m.
        bars = [analyze.Bar(i * 14400, 0, 0, 0, 100.0 + i, 1, 0, None, None) for i in range(12)]
        suggestion = analyze._bucket_suggestion(bars, 14400)
        self.assertIn("--bucket 15.0m", suggestion)
        self.assertIn("--auto", suggestion)
        self.assertEqual(analyze._bucket_suggestion(bars, 900), "", "no advice when the bucket already fits")

    def test_auto_rescues_a_window_far_wider_than_the_recording(self) -> None:
        """The exact situation on the host: 30d asked for, ~2 days recorded."""
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        now -= now % 60
        rows = []
        for index, epoch in enumerate(range(now - 47 * 3600, now + 60, 60)):
            mid = 100.0 * (1 + 0.03 * math.sin(2 * math.pi * index / (9 * 60)))
            rows.append(
                (common.to_ts_utc(common.from_epoch(epoch)), epoch, "SOLUSDT", "binance",
                 mid * 0.9999, mid * 1.0001, mid, 0.97, 5.0, 5.0, None)
            )
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        self.assertAlmostEqual(analyze.recorded_span(conn, "SOLUSDT", now - 30 * 86400, now) / 3600, 47, delta=1)
        conn.close()

        fixed = io.StringIO()
        with redirect_stdout(fixed):
            analyze.main(["--db", str(self.db_path), "--since", "30d", "--bucket", "4h", "--swing", "5"])
        self.assertIn("below the 20 needed", fixed.getvalue())
        self.assertIn("--auto", fixed.getvalue(), "a useless report must say what to use instead")

        auto = io.StringIO()
        with redirect_stdout(auto):
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--since", "30d", "--auto"]), 0)
        output = auto.getvalue()
        self.assertIn("bucket 15.0m", output)
        self.assertNotIn("below the 20 needed", output)
        self.assertIn("periodicity", output)
        # The planted 9h cycle should come back out of both detectors.
        self.assertRegex(output, r"cycle length mean 9\.\dh")
        self.assertRegex(output, r"~9\.\dh")


class BriefFormatTests(TempConfigCase):
    def test_brief_is_narrow_and_reports_the_recorded_span(self) -> None:
        seed_cycle_db(self.db_path)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(
                analyze.main(["--db", str(self.db_path), "--since", "30d", "--auto", "--format", "brief"]), 0
            )
        output = buffer.getvalue()
        self.assertIn("SOLUSDT", output)
        self.assertIn("phase", output)
        # The move must be labelled with the data's own span, not the asked-for window.
        self.assertNotIn("over 30.0d", output)
        self.assertIn("over 14", output)
        widest = max(len(line) for line in output.split("\n"))
        self.assertLessEqual(widest, 44, "brief output must not wrap on a phone terminal")


class CliTests(TempConfigCase):
    def test_text_json_and_csv_output(self) -> None:
        seed_cycle_db(self.db_path)
        argv = ["--db", str(self.db_path), "--since", "15d", "--bucket", "1h"]

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(argv), 0)
        self.assertIn("SOLUSDT", buffer.getvalue())
        self.assertIn("phase", buffer.getvalue())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(argv + ["--format", "json"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["pairs"][0]["pair"], "SOLUSDT")
        self.assertIn("dominant", payload["pairs"][0]["periodicity"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(argv + ["--format", "csv"]), 0)
        lines = buffer.getvalue().strip().split("\n")
        self.assertTrue(lines[0].startswith("pair,bucket_start_utc,open"))
        self.assertGreater(len(lines), 300)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--list-pairs"]), 0)
        self.assertIn("SOLUSDT", buffer.getvalue())

    def test_bad_arguments_and_empty_windows(self) -> None:
        seed_cycle_db(self.db_path)
        stderr = io.StringIO()
        sys.stderr, original = stderr, sys.stderr
        try:
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--since", "1h", "--bucket", "4h"]), 2)
            self.assertEqual(analyze.main(["--db", str(self.db_path), "--since", "nope"]), 2)
            self.assertEqual(analyze.main(["--db", str(self.tmp / 'missing.db')]), 2)
            self.assertEqual(
                analyze.main(["--db", str(self.db_path), "--pair", "NOPEUSDT", "--since", "7d"]), 1
            )
        finally:
            sys.stderr = original
        self.assertIn("larger than", stderr.getvalue())
        self.assertIn("run recorder.py first", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
