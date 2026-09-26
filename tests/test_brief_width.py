#!/usr/bin/env python3
"""brief format keeps every line under 40 characters, as the README promises."""

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


class BriefWidthTests(unittest.TestCase):
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

    def test_unrecorded_series_are_listed_one_per_line(self) -> None:
        """README: 'brief keeps every line under 40 characters'.

        A priced pair plus a long-named pair with no priced ticks in the
        window used to print "nothing recorded yet: 1000PEPEUSDT@coinbase"
        - 43 characters, wrapping on a phone.
        """
        conn = common.connect(self.db_path)
        now = common.to_epoch(common.now_utc())
        now -= now % 60
        rows = []
        for index in range(60):
            epoch = now - (60 - index) * 60
            ts = common.to_ts_utc(common.from_epoch(epoch))
            rows.append((ts, epoch, "SOLUSDT", "binance", 99.9, 100.1, 100.0,
                         2.0, 1.0, 1.0, None))
            rows.append((ts, epoch, "1000PEPEUSDT", "coinbase", None, None, None,
                         None, None, None, "error:HTTPError:404"))
        recorder.insert_rows(conn, rows)
        conn.close()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(
                analyze.main(["--db", str(self.db_path), "--since", "2h", "--format", "brief"]), 0
            )
        output = buffer.getvalue()
        self.assertIn("1000PEPEUSDT@coinbase", output)
        widest = max(len(line) for line in output.split("\n"))
        self.assertLess(widest, 40, f"brief line wraps on a phone: {output!r}")


if __name__ == "__main__":
    unittest.main()
