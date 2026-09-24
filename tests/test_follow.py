#!/usr/bin/env python3
"""Tests for analyze.py --follow: gap persistence ranking."""

from __future__ import annotations

import io
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
import recorder  # noqa: E402


class FollowTests(unittest.TestCase):
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

    def seed_pair_gaps(
        self,
        venue_mids: dict[str, list[float]],
        *,
        pair: str = "SOLUSDC",
        step_sec: int = 60,
    ) -> int:
        """venue_mids: venue -> list of mid prices, one per simultaneous round."""
        tick_count = len(next(iter(venue_mids.values())))
        for mids in venue_mids.values():
            self.assertEqual(len(mids), tick_count)

        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        now -= now % step_sec
        rows = []
        for index in range(tick_count):
            epoch = now - (tick_count - index) * step_sec
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for venue, mids in venue_mids.items():
                mid = mids[index]
                spread = mid * 0.0002
                bid, ask = mid - spread / 2, mid + spread / 2
                rows.append(
                    (ts, epoch, pair, venue, bid, ask, mid, spread / mid * 10000, 5.0, 5.0, None)
                )
        recorder.insert_rows(conn, rows)
        conn.close()
        return now

    def test_sustained_narrow_gap_ranks_above_one_tick_spike(self) -> None:
        """Persistence must beat a wide gap that lasts only one tick."""
        count = 150
        baseline = 100.0
        wide_once = [baseline] * (count - 1) + [baseline * 1.005]  # ~50 bps on last tick
        sustained = [baseline * (1.0 + 0.0006)] * count  # ~6 bps every tick

        self.seed_pair_gaps({
            "binance": [baseline] * count,
            "kraken": wide_once,
            "okx": sustained,
        })

        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        report = analyze.follow_report(conn, now - 86400, now, cost_bps=5.0)
        conn.close()

        self.assertTrue(report["any_clears_floor"])
        self.assertGreaterEqual(len(report["ranking"]), 2)

        spike = next(
            row for row in report["venue_pairs"]
            if row["venue_a"] == "binance" and row["venue_b"] == "kraken"
        )
        sustained = next(
            row for row in report["venue_pairs"]
            if row["venue_a"] == "binance" and row["venue_b"] == "okx"
        )
        self.assertEqual(spike["median_run_ticks"], 1.0)
        self.assertEqual(sustained["median_run_ticks"], float(count))
        self.assertGreater(sustained["score"], spike["score"])
        self.assertEqual(report["ranking"][-1]["venue_a"], "binance")
        self.assertEqual(report["ranking"][-1]["venue_b"], "kraken")

    def test_insufficient_ticks_are_excluded_from_ranking(self) -> None:
        """Forty simultaneous ticks must not look like ten thousand."""
        self.seed_pair_gaps({
            "binance": [100.0] * 40,
            "kraken": [100.006] * 40,
        })

        conn = common.connect(self.db_path, read_only=True)
        now = common.to_epoch(common.now_utc())
        report = analyze.follow_report(conn, now - 86400, now, cost_bps=5.0)
        conn.close()

        row = report["venue_pairs"][0]
        self.assertEqual(row["status"], "INSUFFICIENT")
        self.assertEqual(row["ticks"], 40)
        self.assertEqual(report["ranking"], [])

    def test_cli_requires_cost_bps(self) -> None:
        self.seed_pair_gaps({"binance": [100.0], "kraken": [100.0]})
        stderr = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr
        try:
            code = analyze.main(["--db", str(self.db_path), "--follow", "--since", "1h"])
        finally:
            sys.stderr = old_stderr
        self.assertEqual(code, 2)
        self.assertIn("--cost-bps", stderr.getvalue())

    def test_nothing_clears_floor_ranks_nothing(self) -> None:
        count = 150
        self.seed_pair_gaps({
            "binance": [100.0] * count,
            "kraken": [100.0001] * count,  # ~0.01 bps apart
        })

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = analyze.main([
                "--db", str(self.db_path), "--follow", "--since", "1d", "--cost-bps", "30",
            ])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("ranking nothing", output)
        self.assertIn("not free money", output)
        self.assertNotIn("rank   1", output)

    def test_hour_buckets_differ_by_utc_hour(self) -> None:
        """A dislocation confined to one hour must show up in that hour bucket."""
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        # Start at 02:30 UTC so the first 60 one-minute rounds fall in hour 03.
        day_start = now - (now % 86400)
        base_epoch = day_start + 3 * 3600 - 1800
        rows = []
        for index in range(120):
            epoch = base_epoch + index * 60
            hour = common.from_epoch(epoch).hour
            ts = common.to_ts_utc(common.from_epoch(epoch))
            kraken_mid = 100.06 if hour == 3 else 100.0
            for venue, mid in (("binance", 100.0), ("kraken", kraken_mid)):
                spread = mid * 0.0002
                bid, ask = mid - spread / 2, mid + spread / 2
                rows.append(
                    (ts, epoch, "SOLUSDC", venue, bid, ask, mid, spread / mid * 10000, 5.0, 5.0, None)
                )
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        report = analyze.follow_report(conn, base_epoch - 60, base_epoch + 7200, cost_bps=5.0)
        conn.close()

        row = report["venue_pairs"][0]
        self.assertEqual(row["status"], "ok")
        hour_map = {bucket["hour_utc"]: bucket for bucket in row["hours"]}
        self.assertIn(3, hour_map)
        quiet_hour = 12 if 12 in hour_map else next(h for h in hour_map if h != 3)
        self.assertGreater(hour_map[3]["above_floor_pct"], hour_map[quiet_hour]["above_floor_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
