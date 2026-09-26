#!/usr/bin/env python3
"""--venues compares only rounds where every venue priced."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import analyze  # noqa: E402
import common  # noqa: E402
import recorder  # noqa: E402


class VenueCompletenessTests(unittest.TestCase):
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

    def test_a_round_missing_one_of_three_venues_is_not_compared(self) -> None:
        """README: 'Only ticks where every venue priced are compared.'

        With three venues on the pair and kraken missing one round, that
        round still had two books and used to be counted as a
        "simultaneous" comparison - a fresh book against a missing one.
        """
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        now -= now % 60
        rows = []
        for index in range(5):
            epoch = now - (5 - index) * 60
            ts = common.to_ts_utc(common.from_epoch(epoch))
            for venue, (bid, ask) in {
                "binance": (100.0, 100.02),
                "coinbase": (100.05, 100.07),
                "kraken": (100.1, 100.14),
            }.items():
                if venue == "kraken" and index == 4:
                    continue  # kraken missed the final round
                mid = (bid + ask) / 2
                rows.append((ts, epoch, "SOLUSDC", venue, bid, ask, mid,
                             (ask - bid) / mid * 10000, 5.0, 5.0, None))
        recorder.insert_rows(conn, rows)
        conn.close()

        conn = common.connect(self.db_path, read_only=True)
        series = analyze.venue_series(conn, "SOLUSDC", now - 3600, now)
        conn.close()
        self.assertEqual(len(series), 4, "the 2-of-3-venue round must not be compared")


if __name__ == "__main__":
    unittest.main()
