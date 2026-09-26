#!/usr/bin/env python3
"""Offline tests for recorder.py --probe."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import recorder  # noqa: E402
import venues  # noqa: E402


class ProbeTests(unittest.TestCase):
    def test_a_pair_a_venue_cannot_spell_still_gets_a_row(self) -> None:
        """SOLUSDC:SOLUSD has no Solana mint - jupiter must say FAILED, not crash.

        Before this fix the probe's own error-printing path called
        venue.to_symbol(symbol) a second time and the traceback aborted the
        whole run, so venues sorted after jupiter (kraken, okx) never printed.
        """
        original, recorder.fetch_venue = recorder.fetch_venue, (
            lambda venue, pairs, timeout: (_ for _ in ()).throw(venues.VenueError("offline"))
        )
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = recorder.probe({"SOLUSDC": "SOLUSD"}, 5)
        finally:
            recorder.fetch_venue = original

        output = buffer.getvalue()
        self.assertEqual(status, 1)
        for name in sorted(venues.VENUES):
            self.assertIn(f"{name:<10}", output, f"no row printed for {name}")
        self.assertIn("FAILED", output)

    def test_every_pair_on_every_venue_is_attempted(self) -> None:
        asked: list[tuple[str, str]] = []

        def fake_fetch(venue: venues.Venue, pairs: dict[str, str], timeout: int):
            for symbol in pairs.values():
                asked.append((venue.name, symbol))
            raise venues.VenueError("offline")

        original, recorder.fetch_venue = recorder.fetch_venue, fake_fetch
        try:
            with redirect_stdout(io.StringIO()):
                recorder.probe({"SOLUSDC": "SOLUSDC", "SOLUSDT": "SOLUSDT"}, 5)
        finally:
            recorder.fetch_venue = original

        self.assertEqual(
            len(asked), 2 * len(venues.VENUES), "every venue must be asked for every pair"
        )


if __name__ == "__main__":
    unittest.main()
