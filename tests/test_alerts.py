#!/usr/bin/env python3
"""Tests for the health check and the alerting state machine. No real network."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import alert  # noqa: E402
import common  # noqa: E402
import health  # noqa: E402
import recorder  # noqa: E402


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

    def seed(self, *, age_sec: int = 30, pairs=("SOLUSDT", "BTCUSDT"), errors: int = 0, stale_pair: str = "") -> None:
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        rows = []
        for index, offset in enumerate(range(age_sec, age_sec + 600, 60)):
            epoch = now - offset
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for pair in pairs:
                if pair == stale_pair and offset < 3600:
                    continue  # this pair went quiet an hour ago
                note = "error:URLError" if index < errors else None
                mid = None if note else 100.0 + index
                rows.append((ts, epoch, pair, "binance", mid, mid, mid, 1.0, 1.0, 1.0, note))
        if stale_pair:
            epoch = now - 7200
            rows.append(
                (common.to_ts_utc(common.from_epoch(epoch)), epoch, stale_pair, "binance",
                 100.0, 100.0, 100.0, 1.0, 1.0, 1.0, None)
            )
        recorder.insert_rows(conn, rows)
        conn.close()


class HealthTests(TempConfigCase):
    def test_healthy_recording(self) -> None:
        self.seed()
        report = health.health(self.db_path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["pairs"], 2)
        self.assertIn("oakring OK", health.summarise(report))

    def test_missing_database(self) -> None:
        report = health.health(self.tmp / "nothing.db")
        self.assertFalse(report["ok"])
        self.assertIn("missing", report["problems"][0])

    def test_no_ticks(self) -> None:
        common.connect(self.db_path).close()
        report = health.health(self.db_path)
        self.assertFalse(report["ok"])
        self.assertIn("no ticks", report["problems"][0])

    def test_stale_recording(self) -> None:
        self.seed(age_sec=1800)
        report = health.health(self.db_path, stale_after=300)
        self.assertFalse(report["ok"])
        self.assertIn("is the recorder running", report["problems"][0])
        self.assertGreater(report["last_tick_age_sec"], 300)

    def test_one_pair_goes_quiet_while_the_rest_keep_going(self) -> None:
        self.seed(stale_pair="ETHUSDT", pairs=("SOLUSDT", "ETHUSDT"))
        report = health.health(self.db_path, stale_after=300)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stalled_pairs"], ["ETHUSDT@binance"])
        self.assertIn("ETHUSDT", health.summarise(report))

    def test_dead_venue_is_not_masked_by_a_live_one_on_the_same_pair(self) -> None:
        """A live venue must not vouch for a dead one on the same pair.

        Grouping staleness by pair alone made MAX(ts) for the pair seconds old
        while one of its venues had been silent for days, so the verdict stayed
        green. This is that case, and it must come back red.
        """
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        rows = []
        for offset in range(30, 630, 60):  # both venues ticking, current
            epoch = now - offset
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for source in ("binance", "coinbase"):
                rows.append((ts, epoch, "SOLUSDC", source, 118.1, 118.2, 118.15,
                             1.0, 1.0, 1.0, None))
        dead = now - 7 * 86400  # third venue, same pair, silent for a week
        rows.append((common.to_ts_utc(common.from_epoch(dead)), dead, "SOLUSDC",
                     "bybit", 99.1, 99.2, 99.15, 3.0, 1.0, 1.0, None))
        recorder.insert_rows(conn, rows)
        conn.close()

        report = health.health(self.db_path, stale_after=300)
        self.assertFalse(report["ok"], "a week-dead venue must not report OK")
        self.assertEqual(report["stalled_pairs"], ["SOLUSDC@bybit"])
        self.assertEqual(report["pairs"], 1)
        self.assertEqual(report["series"], 3)

    def test_every_venue_live_stays_green(self) -> None:
        """The negative: several venues on one pair, all current, is healthy."""
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        rows = []
        for offset in range(30, 630, 60):
            epoch = now - offset
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for source in ("binance", "coinbase", "kraken"):
                rows.append((ts, epoch, "SOLUSDC", source, 118.1, 118.2, 118.15,
                             1.0, 1.0, 1.0, None))
        recorder.insert_rows(conn, rows)
        conn.close()

        report = health.health(self.db_path, stale_after=300)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["stalled_pairs"], [])
        self.assertEqual(report["series"], 3)

    def _two_live_one_old_bybit(self, bybit_note=None, bybit_recent=False) -> None:
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        rows = []
        for offset in range(30, 630, 60):
            epoch = now - offset
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for source in ("binance", "coinbase"):
                rows.append((ts, epoch, "SOLUSDC", source, 118.1, 118.2, 118.15, 1.0, 1.0, 1.0, None))
            if bybit_recent:
                rows.append((ts, epoch, "SOLUSDC", "bybit", None, None, None, None, None, None, bybit_note))
        dead = now - 8 * 86400
        rows.append((common.to_ts_utc(common.from_epoch(dead)), dead, "SOLUSDC",
                     "bybit", 99.1, 99.2, 99.15, 3.0, 1.0, 1.0, None))
        recorder.insert_rows(conn, rows)
        conn.close()

    def test_a_venue_switched_off_is_retired_not_an_outage(self) -> None:
        """SOLUSDC@bybit, 24 Sep: gone from the watchlist, alerting every 6h.

        The recorder writes a row for every configured series on every tick,
        failed or not, so a series with no recent row that is also not in the
        watchlist was switched off. That is not an outage.
        """
        self._two_live_one_old_bybit()
        expected = {"SOLUSDC@binance", "SOLUSDC@coinbase"}
        report = health.health(self.db_path, stale_after=300, expected=expected)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["stalled_pairs"], [])
        self.assertEqual(len(report["retired_series"]), 1)
        self.assertIn("SOLUSDC@bybit", report["retired_series"][0])
        self.assertIn("retired", health.render(report))

    def test_a_configured_venue_gone_quiet_says_restart(self) -> None:
        self._two_live_one_old_bybit()
        expected = {"SOLUSDC@binance", "SOLUSDC@coinbase", "SOLUSDC@bybit"}
        report = health.health(self.db_path, stale_after=300, expected=expected)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stalled_pairs"], ["SOLUSDC@bybit"])
        self.assertIn("restart oakring", report["problems"][0])

    def test_configured_but_never_recorded(self) -> None:
        self._two_live_one_old_bybit()
        expected = {"SOLUSDC@binance", "SOLUSDC@coinbase", "SOLUSDC@kraken"}
        report = health.health(self.db_path, stale_after=300, expected=expected)
        self.assertFalse(report["ok"])
        self.assertEqual(report["never_recorded"], ["SOLUSDC@kraken"])

    def test_a_venue_failing_every_tick_is_named(self) -> None:
        """Error rows count as ticks, so a venue that fails every time never
        looked stalled, and one of three is a 33% error rate -- under the 50%
        bar. It must still be named, with the error it keeps getting."""
        self._two_live_one_old_bybit(bybit_note="error:HTTPError:403: Forbidden", bybit_recent=True)
        expected = {"SOLUSDC@binance", "SOLUSDC@coinbase", "SOLUSDC@bybit"}
        report = health.health(self.db_path, stale_after=300, expected=expected)
        self.assertFalse(report["ok"])
        self.assertLess(report["error_rate_1h_pct"], 50.0)
        self.assertEqual(len(report["failing_series"]), 1)
        self.assertIn("SOLUSDC@bybit", report["failing_series"][0])
        self.assertIn("403", report["failing_series"][0])

    def test_expected_series_reads_the_watchlists(self) -> None:
        env = {"WATCHLIST": "SOLUSDC", "WATCHLIST_COINBASE": "SOLUSDC:SOLUSD", "WATCHLIST_OKX": "SOLUSDC"}
        self.assertEqual(
            health.expected_series(env),
            {"SOLUSDC@binance", "SOLUSDC@coinbase", "SOLUSDC@okx"},
        )

    def test_sustained_errors(self) -> None:
        self.seed(errors=9)  # 9 of 10 rounds failed
        report = health.health(self.db_path)
        self.assertFalse(report["ok"])
        self.assertGreater(report["error_rate_1h_pct"], 50.0)

    def test_cli_exit_codes(self) -> None:
        self.seed()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(health.main(["--db", str(self.db_path)]), 0)
        self.assertIn("OK - recording.", buffer.getvalue())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(health.main(["--db", str(self.tmp / 'gone.db'), "--json"]), 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


class NotifyDecisionTests(unittest.TestCase):
    OK = {"ok": True, "problems": []}
    BROKEN = {"ok": False, "problems": ["no tick for 20.0m - is the recorder running?"]}
    WORSE = {"ok": False, "problems": ["no tick for 20.0m - is the recorder running?", "only 50 MB of disk left"]}

    def decide(self, report, state, now=1000.0, repeat=6 * 3600):
        return alert.should_notify(report, state, repeat, now)

    def test_first_run_is_quiet_when_healthy(self) -> None:
        self.assertEqual(self.decide(self.OK, {}), (False, "first run"))

    def test_first_run_speaks_up_when_already_broken(self) -> None:
        notify, _ = self.decide(self.BROKEN, {})
        self.assertTrue(notify)

    def test_healthy_runs_stay_silent(self) -> None:
        notify, reason = self.decide(self.OK, {"ok": True})
        self.assertFalse(notify)
        self.assertEqual(reason, "no change")

    def test_breaking_and_recovering_both_notify(self) -> None:
        self.assertEqual(self.decide(self.BROKEN, {"ok": True}), (True, "broke"))
        self.assertEqual(self.decide(self.OK, {"ok": False}), (True, "recovered"))

    def test_still_broken_is_quiet_until_the_repeat_window(self) -> None:
        state = {"ok": False, "problems": self.BROKEN["problems"], "last_sent": 1000.0}
        notify, _ = self.decide(self.BROKEN, state, now=1000.0 + 3600)
        self.assertFalse(notify, "must not repeat every few minutes")
        notify, reason = self.decide(self.BROKEN, state, now=1000.0 + 7 * 3600)
        self.assertTrue(notify)
        self.assertEqual(reason, "still broken")

    def test_a_new_problem_notifies_immediately(self) -> None:
        state = {"ok": False, "problems": self.BROKEN["problems"], "last_sent": 1000.0}
        notify, reason = self.decide(self.WORSE, state, now=1001.0)
        self.assertTrue(notify)
        self.assertEqual(reason, "new problem")


class TransportTests(TempConfigCase):
    def setUp(self) -> None:
        super().setUp()
        self.received: list[tuple[str, dict]] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            status = 200
            body = b"ok"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append((self.path, json.loads(body.decode())))
                self.send_response(self.status)
                self.send_header("Content-Length", str(len(self.body)))
                self.end_headers()
                self.wfile.write(self.body)

            def log_message(self, *args: object) -> None:
                pass

        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_each_transport_shapes_its_own_payload(self) -> None:
        env = {
            "ALERT_TELEGRAM_TOKEN": "123:SECRET",
            "ALERT_TELEGRAM_CHAT_ID": "999",
            "ALERT_DISCORD_WEBHOOK": f"{self.base}/discord",
            "ALERT_WEBHOOK": f"{self.base}/generic",
        }
        configured = {name: (url, template) for name, url, template in alert.transports(env)}
        self.assertEqual(set(configured), {"telegram", "discord", "webhook"})
        self.assertEqual(configured["telegram"][1]["chat_id"], "999")

        for name in ("discord", "webhook"):
            url, template = configured[name]
            self.assertTrue(alert.send(name, url, template, "hello"))
        paths = {path for path, _ in self.received}
        self.assertEqual(paths, {"/discord", "/generic"})
        bodies = {path: body for path, body in self.received}
        self.assertEqual(bodies["/discord"]["content"], "hello")
        self.assertEqual(bodies["/generic"]["text"], "hello")

    def test_chat_id_shapes(self) -> None:
        for good in ("8538514876", "-1001234567890", "@mychannel"):
            self.assertTrue(alert.looks_like_chat_id(good), good)
        for bad in ("THE_NUMBER", "", "@", "PASTE_CHAT_ID", "123abc"):
            self.assertFalse(alert.looks_like_chat_id(bad), bad)

    def test_an_unedited_placeholder_is_caught_before_sending(self) -> None:
        env = {"ALERT_TELEGRAM_TOKEN": "123:SECRET", "ALERT_TELEGRAM_CHAT_ID": "THE_NUMBER"}
        with self.assertLogs(level="WARNING") as captured:
            self.assertEqual(alert.transports(env), [])
        logged = "".join(captured.output)
        self.assertIn("THE_NUMBER", logged)
        self.assertIn("getUpdates", logged)
        self.assertNotIn("SECRET", logged)

    def test_half_configured_telegram_is_reported_not_used(self) -> None:
        with self.assertLogs(level="WARNING") as captured:
            self.assertEqual(alert.transports({"ALERT_TELEGRAM_TOKEN": "123:SECRET"}), [])
        self.assertIn("ALERT_TELEGRAM_CHAT_ID", captured.output[0])
        self.assertNotIn("SECRET", "".join(captured.output))

    def test_the_api_reason_reaches_the_log(self) -> None:
        """A bare status code is not actionable; Telegram explains 400 in the body."""
        self.handler.status = 400
        self.handler.body = b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
        with self.assertLogs(level="ERROR") as captured:
            alert.send("telegram", f"{self.base}/send", {"text": None}, "hello")
        self.assertIn("chat not found", "".join(captured.output))

    def test_an_echoed_secret_in_the_body_is_redacted(self) -> None:
        self.handler.status = 400
        self.handler.body = b'{"description":"bad token 123:SUPERSECRETTOKEN"}'
        with self.assertLogs(level="ERROR") as captured:
            alert.send(
                "telegram", f"{self.base}/send", {"text": None}, "hello",
                secrets=("123:SUPERSECRETTOKEN",),
            )
        logged = "".join(captured.output)
        self.assertNotIn("SUPERSECRET", logged)
        self.assertIn("***", logged)

    def test_a_failing_transport_never_logs_the_url(self) -> None:
        """A telegram URL contains the bot token, so it must not reach a log."""
        self.handler.status = 401
        secret_url = f"{self.base}/bot123:SUPERSECRET/sendMessage"
        with self.assertLogs(level="ERROR") as captured:
            self.assertFalse(alert.send("telegram", secret_url, {"text": None}, "hello"))
        logged = "".join(captured.output)
        self.assertNotIn("SUPERSECRET", logged)
        self.assertNotIn("127.0.0.1", logged)
        self.assertIn("401", logged)

    def test_unreachable_transport_is_caught(self) -> None:
        with self.assertLogs(level="ERROR") as captured:
            self.assertFalse(alert.send("webhook", "http://127.0.0.1:1/nope", {"text": None}, "hi", timeout=2))
        self.assertIn("webhook", "".join(captured.output))


class AlertCliTests(TempConfigCase):
    def setUp(self) -> None:
        super().setUp()
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append(json.loads(body.decode()))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        config = Path(os.environ["OAKRING_CONFIG_DIR"])
        config.mkdir(parents=True, exist_ok=True)
        (config / ".env").write_text(
            f"ALERT_WEBHOOK=http://127.0.0.1:{self.server.server_port}/hook\nLOG_LEVEL=CRITICAL\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_full_cycle_break_repeat_recover(self) -> None:
        # Healthy first run: records state, says nothing.
        self.seed()
        self.assertEqual(alert.main(["--db", str(self.db_path)]), 0)
        self.assertEqual(self.received, [])
        self.assertTrue(json.loads(alert.state_path().read_text())["ok"])
        self.assertEqual(alert.state_path().stat().st_mode & 0o777, 0o600)

        # It breaks: one alert.
        self.assertEqual(alert.main(["--db", str(self.db_path), "--stale-after", "1s"]), 1)
        self.assertEqual(len(self.received), 1)
        self.assertIn("PROBLEM", self.received[0]["text"])

        # Still broken moments later: silent.
        self.assertEqual(alert.main(["--db", str(self.db_path), "--stale-after", "1s"]), 1)
        self.assertEqual(len(self.received), 1, "must not re-alert on every run")

        # Recovered: one more.
        self.assertEqual(alert.main(["--db", str(self.db_path)]), 0)
        self.assertEqual(len(self.received), 2)
        self.assertIn("OK", self.received[1]["text"])

    def test_dry_run_sends_nothing(self) -> None:
        self.seed()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            alert.main(["--db", str(self.db_path), "--stale-after", "1s", "--dry-run"])
        self.assertIn("would send", buffer.getvalue())
        self.assertEqual(self.received, [])
        self.assertFalse(alert.state_path().exists(), "a dry run must not move the state forward")

    def test_test_message(self) -> None:
        self.assertEqual(alert.main(["--test"]), 0)
        self.assertEqual(len(self.received), 1)
        self.assertIn("test message", self.received[0]["text"])

    def test_missing_configuration_is_explicit(self) -> None:
        (Path(os.environ["OAKRING_CONFIG_DIR"]) / ".env").write_text("", encoding="utf-8")
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            self.assertEqual(alert.main(["--db", str(self.db_path)]), 2)
            self.assertIn("ALERT_TELEGRAM_TOKEN", sys.stderr.getvalue())
        finally:
            sys.stderr = stderr

    def test_corrupt_state_file_does_not_crash(self) -> None:
        self.seed()
        alert.state_path().parent.mkdir(parents=True, exist_ok=True)
        alert.state_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(alert.main(["--db", str(self.db_path)]), 0)
        self.assertTrue(json.loads(alert.state_path().read_text())["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
