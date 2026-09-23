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


def health(db_path: Path, stale_after: int = DEFAULT_STALE_SEC) -> dict:
    """Everything worth alerting on, in one pass over the recent ticks."""
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
        stalled = [
            f"{row[0]}@{row[1]}"
            for row in conn.execute(
                f"SELECT pair, source, MAX({epoch_sql}) FROM ticks "
                f"GROUP BY pair, source HAVING ? - MAX({epoch_sql}) > ?",
                (now, stale_after),
            )
        ]
        report["stalled_pairs"] = stalled
        # Everything stalling at once is already covered by the last-tick check
        # above; naming series individually matters when only some went quiet.
        if stalled and age <= stale_after:
            report["ok"] = False
            report["problems"].append(f"stopped reporting: {', '.join(stalled)}")

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

    report = health(db_path, stale_after)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
