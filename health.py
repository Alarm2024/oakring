#!/usr/bin/env python3
"""Is the recording healthy? Shared by check.sh and alert.py.

Run directly for a human-readable report:

    python3 health.py [--db PATH] [--stale-after 5m] [--json]

Exit status is 0 when healthy and 1 when something needs attention.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import common

DEFAULT_STALE_SEC = 300
ERROR_RATE_LIMIT_PCT = 50.0  # sustained failures, not the occasional timeout
MIN_FREE_BYTES = 512 * 1024 * 1024


def expected_series(env: dict[str, str]) -> set[str]:
    """PAIR@venue for every series the recorder is configured to write.

    The recorder writes a row for every configured pair on every venue on every
    tick -- a failed fetch too, with `note` set. So a configured series with no
    recent row means the running recorder is not using this configuration, and
    a series in the database that is not configured was switched off.
    """
    return {
        f"{pair}@{venue}"
        for venue, pairs in common.watchlists_from(env).items()
        for pair in pairs
    }


def health(
    db_path: Path,
    stale_after: int = DEFAULT_STALE_SEC,
    expected: set[str] | None = None,
) -> dict:
    """Everything worth alerting on, in one pass over the recent ticks.

    `expected` is the set of PAIR@venue series the recorder is configured to
    write (see expected_series). Given, a series outside it is reported as
    retired instead of stalled: a venue someone switched off is not an outage,
    and alerting on it every six hours forever teaches the reader to ignore the
    channel. None keeps the old behaviour: every series ever recorded is
    expected to keep ticking.
    """
    report: dict = {
        "ok": True,
        "problems": [],
        "db": str(db_path),
        "checked_at": common.to_ts_utc(common.now_utc()),
    }

    if not db_path.exists():
        report["ok"] = False
        report["problems"].append(f"database missing at {db_path}")
        return report

    report["db_bytes"] = db_path.stat().st_size
    usage = shutil.disk_usage(db_path.parent)
    report["disk_free_bytes"] = usage.free
    report["disk_used_pct"] = round(100.0 * usage.used / usage.total, 1)
    if usage.free < MIN_FREE_BYTES:
        report["ok"] = False
        report["problems"].append(f"only {usage.free // (1024 * 1024)} MB of disk left")

    conn = common.connect(db_path, read_only=True)
    try:
        epoch_sql = "COALESCE(NULLIF(ts_epoch, 0), CAST(strftime('%s', ts_utc) AS INTEGER))"
        now = int(time.time())

        pairs, series, total, newest = conn.execute(
            "SELECT COUNT(DISTINCT pair), COUNT(DISTINCT pair || '@' || source), "
            f"COUNT(*), MAX({epoch_sql}) FROM ticks"
        ).fetchone()
        report["pairs"] = pairs or 0
        report["series"] = series or 0
        report["ticks"] = total or 0

        if not total:
            report["ok"] = False
            report["problems"].append("no ticks recorded yet")
            return report

        age = max(0, now - int(newest or 0))
        report["last_tick_age_sec"] = age
        report["last_tick_age"] = common.format_duration(age)
        if age > stale_after:
            report["ok"] = False
            report["problems"].append(
                f"no tick for {common.format_duration(age)} - is the recorder running?"
            )

        errors, recent = conn.execute(
            f"SELECT SUM(note IS NOT NULL), COUNT(*) FROM ticks WHERE {epoch_sql} > ?",
            (now - 3600,),
        ).fetchone()
        errors, recent = int(errors or 0), int(recent or 0)
        report["errors_1h"] = errors
        report["ticks_1h"] = recent
        report["error_rate_1h_pct"] = round(100.0 * errors / recent, 2) if recent else 0.0
        if recent and report["error_rate_1h_pct"] > ERROR_RATE_LIMIT_PCT:
            report["ok"] = False
            report["problems"].append(
                f"{report['error_rate_1h_pct']}% of the last hour's ticks failed"
            )

        # Group by pair AND source. Grouping by pair alone let a live venue
        # mask a dead one: with four venues ticking on SOLUSDC, MAX(ts) for the
        # group was seconds old while SOLUSDC@bybit had been silent for days,
        # so it never appeared here and the verdict stayed green. --latest
        # groups by pair and source, which is why only it could see the corpse.
        # One row per series: newest row of any kind, newest usable quote, and
        # the note on the newest row (the last error, when it failed).
        last: dict[str, tuple[int, int | None, str | None]] = {}
        for pair, source, newest_any, newest_good in conn.execute(
            f"SELECT pair, source, MAX({epoch_sql}), "
            f"MAX(CASE WHEN note IS NULL THEN {epoch_sql} END) "
            "FROM ticks GROUP BY pair, source"
        ):
            last[f"{pair}@{source}"] = (int(newest_any or 0), newest_good, None)
        for pair, source, note in conn.execute(
            "SELECT t.pair, t.source, t.note FROM ticks t JOIN ("
            f"  SELECT pair, source, MAX({epoch_sql}) AS e FROM ticks GROUP BY pair, source"
            f") m ON t.pair = m.pair AND t.source = m.source AND {epoch_sql.replace('ts_', 't.ts_')} = m.e"
        ):
            key = f"{pair}@{source}"
            if key in last and note:
                newest_any, newest_good, _ = last[key]
                last[key] = (newest_any, newest_good, note)

        watched = set(last) if expected is None else set(expected)
        retired = sorted(k for k in last if k not in watched)
        report["retired_series"] = [
            f"{k} (last tick {common.format_duration(max(0, now - last[k][0]))} ago)" for k in retired
        ]

        stalled = sorted(
            k for k in watched if k in last and now - last[k][0] > stale_after
        )
        never = sorted(k for k in watched if k not in last)
        report["stalled_pairs"] = stalled
        report["never_recorded"] = never

        # A venue that fails every tick still writes rows, so it never looked
        # stalled -- and one dead venue among five is only a 20% error rate,
        # under the 50% bar. Judge each watched series by its last usable quote.
        failing = []
        for k in sorted(watched):
            if k not in last or k in stalled:
                continue
            _, newest_good, note = last[k]
            good_age = None if newest_good is None else now - int(newest_good)
            if good_age is None or good_age > stale_after:
                since = "ever" if good_age is None else f"for {common.format_duration(good_age)}"
                failing.append(f"{k} (no usable quote {since}; last error: {note or 'unknown'})")
        report["failing_series"] = failing

        # Everything stalling at once is already covered by the last-tick check
        # above; naming series individually matters when only some went quiet.
        if age <= stale_after:
            if stalled:
                report["ok"] = False
                hint = "" if expected is None else " - they are in the watchlist, so the running recorder is not using the current .env; restart oakring"
                report["problems"].append(f"stopped reporting: {', '.join(stalled)}{hint}")
            if never:
                report["ok"] = False
                report["problems"].append(
                    f"configured but never recorded: {', '.join(never)} - restart oakring to pick up the .env"
                )
        if failing:
            report["ok"] = False
            report["problems"].append(f"every recent tick failed: {'; '.join(failing)}")

        return report
    finally:
        conn.close()


def summarise(report: dict) -> str:
    """One line, short enough for a phone notification."""
    if report["ok"]:
        return (
            f"oakring OK - {report.get('pairs', 0)} pairs, "
            f"last tick {report.get('last_tick_age', 'n/a')} ago, "
            f"{report.get('error_rate_1h_pct', 0)}% errors in the last hour"
        )
    return "oakring PROBLEM - " + "; ".join(report["problems"])


def render(report: dict) -> str:
    lines = []

    def say(label: str, value: str) -> None:
        lines.append(f"{label:<10} {value}")

    say("database", f"{report.get('db_bytes', 0) / 1e6:.1f} MB at {report['db']}")
    if "disk_free_bytes" in report:
        say("disk", f"{report['disk_free_bytes'] / 1e9:.1f} GB free ({report['disk_used_pct']}% used)")
    series = report.get("series")
    if series and series != report.get("pairs", 0):
        say("ticks", f"{report.get('ticks', 0)} across {report.get('pairs', 0)} pairs "
                     f"on {series} pair-venue series")
    else:
        say("ticks", f"{report.get('ticks', 0)} across {report.get('pairs', 0)} pairs")
    if "last_tick_age" in report:
        say("last tick", f"{report['last_tick_age']} ago")
    if "ticks_1h" in report:
        say("errors 1h", f"{report['errors_1h']} of {report['ticks_1h']}")
    if report.get("stalled_pairs"):
        say("stalled", ", ".join(report["stalled_pairs"]))
    if report.get("failing_series"):
        say("failing", "; ".join(report["failing_series"]))
    if report.get("retired_series"):
        say("retired", ", ".join(report["retired_series"]) + " - not in the watchlist, not alerting")

    lines.append("")
    lines.append("OK - recording." if report["ok"] else "NEEDS ATTENTION:")
    for problem in report["problems"]:
        lines.append(f"  - {problem}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check whether oakring is recording healthily.")
    parser.add_argument("--db", help="database path (default: DB_PATH from .env)")
    parser.add_argument("--stale-after", default="5m", help="tick age that counts as stale (default: 5m)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    env = common.load_config()
    common.setup_logging("WARNING")
    db_path = Path(args.db) if args.db else common.db_path_from(env)
    try:
        stale_after = common.parse_duration(args.stale_after)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = health(db_path, stale_after, expected_series(env))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
