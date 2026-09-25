#!/usr/bin/env python3
"""Read the recorded ticks back and look for the market cycle.

Stdlib only. Safe to run while the recorder is writing (opens read-only).

    python3 analyze.py --since 7d --bucket 1h
    python3 analyze.py --pair SOLUSDT --since 30d --bucket 4h --swing 3
    python3 analyze.py --format json --since 24h --bucket 15m > report.json
    python3 analyze.py --basis --since 12h --held-bps 15 --edge-bps 40
    python3 analyze.py --follow --since 7d --cost-bps 30

What it reports per pair:
  * coverage    - how much of the window was actually recorded, and the gaps
  * price       - change, range, where the last price sits inside that range
  * trend       - log-price regression slope, expressed as % per day
  * swings      - zigzag pivots, so peak-to-peak / trough-to-trough cycle length
  * periodicity - a periodogram over detrended log price, dominant periods
  * phase       - accumulation / markup / distribution / markdown (heuristic)
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import common

MIN_BARS_FOR_STATS = 5
TARGET_BARS = 150  # what --auto aims for: enough to see a cycle, few enough to stay readable
BUCKET_LADDER = (60, 300, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
MIN_FOLLOW_TICKS = 100  # fewer simultaneous ticks than this are INSUFFICIENT for ranking

# Legacy rows written before ts_epoch existed still carry a usable ts_utc.
EPOCH_SQL = "COALESCE(NULLIF(ts_epoch, 0), CAST(strftime('%s', ts_utc) AS INTEGER))"
MIN_BARS_FOR_CYCLES = 20
PERIOD_CANDIDATES = 240  # log-spaced, keeps the periodogram O(candidates * bars)


# --------------------------------------------------------------------------- data


@dataclass
class Bar:
    start: int
    open: float
    high: float
    low: float
    close: float
    ticks: int
    errors: int
    spread_bps: float | None
    imbalance: float | None  # (bid_qty - ask_qty) / (bid_qty + ask_qty), -1..1


@dataclass
class Pivot:
    index: int
    epoch: int
    price: float
    kind: str  # "peak" | "trough"


@dataclass
class PairReport:
    pair: str
    bucket_sec: int
    window: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    price: dict = field(default_factory=dict)
    trend: dict = field(default_factory=dict)
    swings: dict = field(default_factory=dict)
    periodicity: dict = field(default_factory=dict)
    phase: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- loading


def list_pairs(conn) -> list[tuple[str, int, str, str]]:
    rows = conn.execute(
        """
        SELECT pair, COUNT(*) AS n, MIN(ts_utc) AS first_ts, MAX(ts_utc) AS last_ts
        FROM ticks GROUP BY pair ORDER BY pair
        """
    ).fetchall()
    return [(r["pair"], r["n"], r["first_ts"], r["last_ts"]) for r in rows]


def recorded_keys(conn, wanted: list[str] | None = None, venue: str | None = None) -> list[tuple[str, str]]:
    """Every (pair, venue) actually recorded, filtered by what was asked for."""
    rows = conn.execute("SELECT DISTINCT pair, source FROM ticks ORDER BY pair, source").fetchall()
    keys = [(row["pair"], row["source"]) for row in rows]
    if wanted:
        names = {name.upper() for name in wanted}
        keys = [key for key in keys if key[0].upper() in names]
    if venue:
        keys = [key for key in keys if key[1].lower() == venue.lower()]
    return keys


def venue_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(DISTINCT source) FROM ticks").fetchone()[0] or 0)


def label_for(pair: str, source: str, show_venue: bool) -> str:
    return f"{pair}@{source}" if show_venue else pair


def choose_bucket(span_sec: int) -> int:
    """A round bucket that turns `span_sec` of recording into ~TARGET_BARS bars."""
    target = max(60, span_sec / TARGET_BARS)
    return min(BUCKET_LADDER, key=lambda value: abs(math.log(value / target)))


def choose_swing(bars: list[Bar]) -> float:
    """A zigzag threshold scaled to how much this pair actually moves per bar.

    A fixed percentage is wrong in both directions: 5% never triggers on a
    two-day BTC window, and 0.5% turns SOL noise into dozens of fake pivots.
    """
    closes = [bar.close for bar in bars]
    if len(closes) < 3:
        return 0.5
    moves = [abs(math.log(closes[i] / closes[i - 1])) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not moves:
        return 0.5
    return round(min(10.0, max(0.1, 3.0 * statistics.fmean(moves) * 100.0)), 2)


def recorded_span(conn, pair: str, start: int, end: int, source: str | None = None) -> int:
    """Seconds between the first and last priced tick for this pair in the window."""
    row = conn.execute(
        f"""
        SELECT MIN({EPOCH_SQL}) AS first_epoch, MAX({EPOCH_SQL}) AS last_epoch
        FROM ticks
        WHERE pair = ? {"AND source = ?" if source else ""} AND note IS NULL AND mid IS NOT NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        """,
        (pair, source, start, end) if source else (pair, start, end),
    ).fetchone()
    if row is None or row["first_epoch"] is None:
        return 0
    return max(0, int(row["last_epoch"]) - int(row["first_epoch"]))


def _change_since(
    conn, pair: str, source: str, last_epoch: int, last_mid: float, window: int
) -> float | None:
    """Percent change against the last priced tick at or before `window` ago."""
    target = last_epoch - window
    row = conn.execute(
        f"""
        SELECT mid, {EPOCH_SQL} AS epoch
        FROM ticks
        WHERE pair = ? AND source = ? AND note IS NULL AND mid IS NOT NULL AND {EPOCH_SQL} <= ?
        ORDER BY epoch DESC LIMIT 1
        """,
        (pair, source, target),
    ).fetchone()
    if row is None or not row["mid"]:
        return None
    # A reference point far older than asked for would misreport the change.
    if target - int(row["epoch"]) > window * 0.5:
        return None
    return round(100.0 * (last_mid / float(row["mid"]) - 1.0), 3)


def latest_snapshot(conn, keys: list[tuple[str, str]], show_venue: bool = False) -> list[dict]:
    """Most recent priced tick per (pair, venue), with short-horizon changes."""
    now = common.to_epoch(common.now_utc())
    snapshot: list[dict] = []
    for pair, source in keys:
        row = conn.execute(
            f"""
            SELECT ts_utc, {EPOCH_SQL} AS epoch, bid, ask, mid, spread_bps
            FROM ticks
            WHERE pair = ? AND source = ? AND note IS NULL AND mid IS NOT NULL
            ORDER BY epoch DESC LIMIT 1
            """,
            (pair, source),
        ).fetchone()
        if row is None:
            snapshot.append({
                "pair": label_for(pair, source, show_venue),
                "venue": source,
                "status": "no priced ticks recorded yet",
            })
            continue

        last_epoch, last_mid = int(row["epoch"]), float(row["mid"])
        age = max(0, now - last_epoch)
        entry = {
            "pair": label_for(pair, source, show_venue),
            "venue": source,
            "at": row["ts_utc"],
            "age_sec": age,
            "age_human": common.format_duration(age),
            "bid": row["bid"],
            "ask": row["ask"],
            "mid": last_mid,
            "spread_bps": row["spread_bps"],
        }
        for label, window in (("1h", 3600), ("24h", 86400), ("7d", 604800)):
            entry[f"change_{label}_pct"] = _change_since(conn, pair, source, last_epoch, last_mid, window)
        snapshot.append(entry)
    return snapshot


def render_latest(snapshot: list[dict], stale_after: int) -> str:
    width = max([10] + [len(entry["pair"]) for entry in snapshot]) + 1
    lines = [f"{'pair':<{width}} {'price':>14} {'spread':>10} {'age':>8} {'1h':>9} {'24h':>9} {'7d':>9}"]
    for entry in snapshot:
        if "status" in entry:
            lines.append(f"{entry['pair']:<{width}} {entry['status']}")
            continue
        changes = []
        for label in ("1h", "24h", "7d"):
            value = entry[f"change_{label}_pct"]
            changes.append("-" if value is None else f"{value:+.2f}%")
        stale = "  (stale - is the recorder running?)" if entry["age_sec"] > stale_after else ""
        lines.append(
            f"{entry['pair']:<{width}} {price_fmt(entry['mid']):>14} "
            f"{entry['spread_bps']:>7.2f}bps {entry['age_human']:>8} "
            f"{changes[0]:>9} {changes[1]:>9} {changes[2]:>9}{stale}"
        )

    # "-" reads as "this pair is broken" without saying what it means. It is
    # only ever "this pair has not been recording that long yet".
    if any(
        entry.get(f"change_{label}_pct") is None
        for entry in snapshot
        for label in ("1h", "24h", "7d")
        if "status" not in entry
    ):
        lines.append("")
        lines.append("-  means not recording that long yet, not a problem. `age` is what to watch.")
    return "\n".join(lines)


PEG_PAIR = "USDCUSDT"


def cross_bases(conn, pairs: list[str] | None = None) -> list[str]:
    """Assets recorded against both USDT and USDC, given the peg is recorded too."""
    recorded = {row[0] for row in conn.execute("SELECT DISTINCT pair FROM ticks")}
    if PEG_PAIR not in recorded:
        return []
    wanted = {pair.upper() for pair in pairs} if pairs else None
    bases = []
    for pair in sorted(recorded):
        if not pair.endswith("USDT") or pair == PEG_PAIR:
            continue
        base = pair[: -len("USDT")]
        if base + "USDC" not in recorded:
            continue
        if wanted and not ({pair, base + "USDC", base} & wanted):
            continue
        bases.append(base)
    return bases


def cross_series(conn, base: str, start: int, end: int, source: str = "binance") -> list[dict]:
    """Per tick: what the two books imply about USDC, and what the peg actually says.

    The recorder stamps every pair in a tick with one timestamp, so the three
    legs line up exactly - no interpolation, no stale leg.
    """
    rows = conn.execute(
        f"""
        SELECT {EPOCH_SQL} AS epoch,
               MAX(CASE WHEN pair = ? THEN mid END) AS usdt,
               MAX(CASE WHEN pair = ? THEN mid END) AS usdc,
               MAX(CASE WHEN pair = ? THEN mid END) AS peg,
               MAX(CASE WHEN pair = ? THEN spread_bps END) AS usdt_spread,
               MAX(CASE WHEN pair = ? THEN spread_bps END) AS usdc_spread,
               MAX(CASE WHEN pair = ? THEN spread_bps END) AS peg_spread
        FROM ticks
        WHERE pair IN (?, ?, ?) AND source = ? AND note IS NULL AND mid IS NOT NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        GROUP BY epoch
        HAVING usdt IS NOT NULL AND usdc IS NOT NULL AND peg IS NOT NULL
        ORDER BY epoch
        """,
        (
            f"{base}USDT", f"{base}USDC", PEG_PAIR,
            f"{base}USDT", f"{base}USDC", PEG_PAIR,
            f"{base}USDT", f"{base}USDC", PEG_PAIR,
            source, start, end,
        ),
    ).fetchall()

    series = []
    for row in rows:
        usdt, usdc, peg = float(row["usdt"]), float(row["usdc"]), float(row["peg"])
        if usdc <= 0 or peg <= 0:
            continue
        implied = (usdt / usdc - 1.0) * 10000.0
        quoted = (peg - 1.0) * 10000.0
        # Half a spread crossed on each of the three legs. Excludes fees.
        cost = (
            float(row["usdt_spread"] or 0.0)
            + float(row["usdc_spread"] or 0.0)
            + float(row["peg_spread"] or 0.0)
        ) / 2.0
        series.append(
            {
                "epoch": int(row["epoch"]),
                "usdt": usdt,
                "usdc": usdc,
                "peg": peg,
                "implied_bps": implied,
                "quoted_bps": quoted,
                "residual_bps": implied - quoted,
                "cost_bps": cost,
            }
        )
    return series


def cross_report(conn, base: str, start: int, end: int, source: str = "binance") -> dict:
    series = cross_series(conn, base, start, end, source)
    if not series:
        return {"base": base, "status": "no tick has all three legs priced yet"}

    residuals = [point["residual_bps"] for point in series]
    last = series[-1]
    beyond = [point for point in series if abs(point["residual_bps"]) > point["cost_bps"]]
    widest = max(series, key=lambda point: abs(point["residual_bps"]))
    span = series[-1]["epoch"] - series[0]["epoch"]

    return {
        "base": base,
        "ticks": len(series),
        "span_human": common.format_duration(span),
        "last": {
            "at": common.to_ts_utc(common.from_epoch(last["epoch"])),
            "usdt": last["usdt"],
            "usdc": last["usdc"],
            "implied_bps": round(last["implied_bps"], 3),
            "quoted_bps": round(last["quoted_bps"], 3),
            "residual_bps": round(last["residual_bps"], 3),
            "cost_bps": round(last["cost_bps"], 3),
        },
        "residual_mean_bps": round(statistics.fmean(residuals), 3),
        "residual_stdev_bps": round(statistics.stdev(residuals), 3) if len(residuals) > 1 else 0.0,
        "residual_widest_bps": round(widest["residual_bps"], 3),
        "residual_widest_at": common.to_ts_utc(common.from_epoch(widest["epoch"])),
        "beyond_cost_ticks": len(beyond),
        "beyond_cost_pct": round(100.0 * len(beyond) / len(series), 2),
    }


def render_cross(reports: list[dict]) -> str:
    lines = []
    for report in reports:
        lines.append("")
        if "status" in report:
            lines.append(f"{report['base']}: {report['status']}")
            continue
        last = report["last"]
        lines.append(f"{report['base']}  {last['at']}")
        lines.append(
            f"  books    USDT {price_fmt(last['usdt'])} / USDC {price_fmt(last['usdc'])}"
        )
        lines.append(
            f"  implied  {last['implied_bps']:+.2f} bps   peg says {last['quoted_bps']:+.2f} bps"
        )
        lines.append(
            f"  residual {last['residual_bps']:+.2f} bps vs {last['cost_bps']:.2f} bps of spread"
            f"  -> {'outside' if abs(last['residual_bps']) > last['cost_bps'] else 'inside'} cost"
        )
        lines.append(
            f"  over {report['span_human']}: mean {report['residual_mean_bps']:+.2f}, "
            f"sd {report['residual_stdev_bps']:.2f}, "
            f"widest {report['residual_widest_bps']:+.2f} bps"
        )
        lines.append(
            f"  beyond spread cost in {report['beyond_cost_pct']}% of "
            f"{report['ticks']} aligned ticks"
        )
    if any("status" not in report for report in reports):
        lines.append("")
        lines.append(
            "Spread cost is half a spread on each of the three legs. Exchange fees are"
        )
        lines.append(
            "NOT included and are usually several bps - far wider than these residuals,"
        )
        lines.append(
            "so 'outside cost' here means measurable, not profitable."
        )
    return "\n".join(lines)


def venue_series(conn, pair: str, start: int, end: int) -> list[dict]:
    """Per tick, every venue's book for one pair - only ticks all of them priced.

    The recorder stamps every venue in a round with one timestamp, so these are
    simultaneous quotes rather than a stale book compared against a fresh one.
    """
    rows = conn.execute(
        f"""
        SELECT {EPOCH_SQL} AS epoch, source, bid, ask, mid
        FROM ticks
        WHERE pair = ? AND note IS NULL AND mid IS NOT NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, start, end),
    ).fetchall()

    by_epoch: dict[int, dict[str, dict]] = {}
    for row in rows:
        by_epoch.setdefault(int(row["epoch"]), {})[row["source"]] = {
            "bid": float(row["bid"]), "ask": float(row["ask"]), "mid": float(row["mid"])
        }

    sources = {source for books in by_epoch.values() for source in books}
    if len(sources) < 2:
        return []

    series = []
    for epoch in sorted(by_epoch):
        books = by_epoch[epoch]
        if len(books) < 2:
            continue  # a venue missed this round; comparing would be misleading
        best_bid_venue = max(books, key=lambda name: books[name]["bid"])
        best_ask_venue = min(books, key=lambda name: books[name]["ask"])
        mids = {name: book["mid"] for name, book in books.items()}
        cheapest, dearest = min(mids, key=mids.get), max(mids, key=mids.get)
        reference = statistics.fmean(mids.values())
        series.append({
            "epoch": epoch,
            "books": books,
            "best_bid_venue": best_bid_venue,
            "best_ask_venue": best_ask_venue,
            # Positive means somebody's bid sits above somebody else's ask.
            "cross_bps": (books[best_bid_venue]["bid"] - books[best_ask_venue]["ask"]) / reference * 10000.0,
            "spread_bps": (mids[dearest] - mids[cheapest]) / reference * 10000.0,
            "cheapest": cheapest,
            "dearest": dearest,
        })
    return series


def venue_report(conn, pair: str, start: int, end: int) -> dict:
    series = venue_series(conn, pair, start, end)
    if not series:
        return {"pair": pair, "status": "needs two venues priced in the same tick"}

    spreads = [point["spread_bps"] for point in series]
    crossed = [point for point in series if point["cross_bps"] > 0]
    widest = max(series, key=lambda point: point["spread_bps"])
    last = series[-1]

    return {
        "pair": pair,
        "ticks": len(series),
        "venues": sorted(last["books"]),
        "span_human": common.format_duration(series[-1]["epoch"] - series[0]["epoch"]),
        "last": {
            "at": common.to_ts_utc(common.from_epoch(last["epoch"])),
            "books": {name: dict(book) for name, book in last["books"].items()},
            "cheapest": last["cheapest"],
            "dearest": last["dearest"],
            "spread_bps": round(last["spread_bps"], 3),
            "cross_bps": round(last["cross_bps"], 3),
            "best_bid_venue": last["best_bid_venue"],
            "best_ask_venue": last["best_ask_venue"],
        },
        "spread_mean_bps": round(statistics.fmean(spreads), 3),
        "spread_max_bps": round(widest["spread_bps"], 3),
        "spread_max_at": common.to_ts_utc(common.from_epoch(widest["epoch"])),
        "crossed_ticks": len(crossed),
        "crossed_pct": round(100.0 * len(crossed) / len(series), 2),
        "crossed_max_bps": round(max((point["cross_bps"] for point in crossed), default=0.0), 3),
    }


def render_venues(reports: list[dict]) -> str:
    lines = []
    for report in reports:
        lines.append("")
        if "status" in report:
            lines.append(f"{report['pair']}: {report['status']}")
            continue
        last = report["last"]
        lines.append(f"{report['pair']}  {last['at']}  ({', '.join(report['venues'])})")
        for name in sorted(last["books"]):
            book = last["books"][name]
            lines.append(
                f"  {name:<9} bid {price_fmt(book['bid'])}  ask {price_fmt(book['ask'])}"
                f"  mid {price_fmt(book['mid'])}"
            )
        lines.append(
            f"  gap      {last['spread_bps']:+.2f} bps "
            f"({last['cheapest']} cheapest, {last['dearest']} dearest)"
        )
        lines.append(
            f"  crossed  {last['cross_bps']:+.2f} bps now "
            f"(best bid {last['best_bid_venue']}, best ask {last['best_ask_venue']})"
        )
        lines.append(
            f"  over {report['span_human']}: mean gap {report['spread_mean_bps']:.2f}, "
            f"widest {report['spread_max_bps']:.2f} bps at {report['spread_max_at']}"
        )
        lines.append(
            f"  books crossed in {report['crossed_pct']}% of {report['ticks']} "
            f"simultaneous ticks, at most {report['crossed_max_bps']:+.2f} bps"
        )
    if any("status" not in report for report in reports):
        lines.append("")
        lines.append("A crossed book across venues is not free money: taker fees, withdrawal")
        lines.append("cost and transfer time all sit between the two sides, and none are")
        lines.append("counted here. This measures how far the venues disagree, nothing more.")
    return "\n".join(lines)


FOLLOW_COST_CAVEAT = (
    "A crossed book across venues is not free money: taker fees, withdrawal cost and "
    "transfer time all sit between the two sides, and none are counted here. A crossed "
    "book at time T is not a fill at time T. This ranks where to look, not what to earn."
)


def venue_pair_series(
    conn, pair: str, venue_a: str, venue_b: str, start: int, end: int
) -> list[dict]:
    """Simultaneous ticks for one pair on two venues, with the mid-to-mid gap."""
    rows = conn.execute(
        f"""
        SELECT {EPOCH_SQL} AS epoch, source, mid
        FROM ticks
        WHERE pair = ? AND source IN (?, ?) AND note IS NULL AND mid IS NOT NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, venue_a, venue_b, start, end),
    ).fetchall()

    by_epoch: dict[int, dict[str, float]] = {}
    for row in rows:
        by_epoch.setdefault(int(row["epoch"]), {})[row["source"]] = float(row["mid"])

    series: list[dict] = []
    for epoch in sorted(by_epoch):
        mids = by_epoch[epoch]
        if venue_a not in mids or venue_b not in mids:
            continue
        mid_a, mid_b = mids[venue_a], mids[venue_b]
        reference = statistics.fmean((mid_a, mid_b))
        if reference <= 0:
            continue
        gap_bps = abs(mid_a - mid_b) / reference * 10000.0
        series.append({
            "epoch": epoch,
            "hour_utc": common.from_epoch(epoch).hour,
            "gap_bps": gap_bps,
        })
    return series


def pair_recording_rounds(conn, pair: str, start: int, end: int) -> int:
    """Distinct tick epochs for this pair in the window (any venue)."""
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT {EPOCH_SQL}) AS rounds
        FROM ticks
        WHERE pair = ? AND note IS NULL AND mid IS NOT NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        """,
        (pair, start, end),
    ).fetchone()
    return int(row["rounds"] or 0)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _run_lengths_above(gaps: list[float], floor_bps: float) -> list[int]:
    runs: list[int] = []
    current = 0
    for gap in gaps:
        if gap >= floor_bps:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _summarize_gap_series(gaps: list[float], cost_bps: float) -> dict:
    above = [gap for gap in gaps if gap >= cost_bps]
    runs = _run_lengths_above(gaps, cost_bps)
    fraction_above = len(above) / len(gaps)
    median_run = statistics.median(runs) if runs else 0.0
    return {
        "ticks": len(gaps),
        "median_bps": round(statistics.median(gaps), 2),
        "p90_bps": round(_percentile(gaps, 90), 2),
        "p99_bps": round(_percentile(gaps, 99), 2),
        "max_bps": round(max(gaps), 2),
        "above_floor_ticks": len(above),
        "above_floor_pct": round(100.0 * fraction_above, 2),
        "above_floor_fraction": fraction_above,
        "median_run_ticks": round(float(median_run), 2),
        "run_count": len(runs),
        "score": round(fraction_above * median_run, 4),
        "clears_floor": bool(above),
    }


def _hour_buckets(series: list[dict], cost_bps: float) -> list[dict]:
    by_hour: dict[int, list[float]] = {hour: [] for hour in range(24)}
    for point in series:
        by_hour[point["hour_utc"]].append(point["gap_bps"])

    buckets: list[dict] = []
    for hour in range(24):
        gaps = by_hour[hour]
        if not gaps:
            continue
        summary = _summarize_gap_series(gaps, cost_bps)
        buckets.append({"hour_utc": hour, **summary})
    return buckets


def follow_venue_pair_report(
    conn,
    pair: str,
    venue_a: str,
    venue_b: str,
    start: int,
    end: int,
    cost_bps: float,
) -> dict:
    series = venue_pair_series(conn, pair, venue_a, venue_b, start, end)
    expected_rounds = pair_recording_rounds(conn, pair, start, end)
    if not series:
        return {
            "pair": pair,
            "venue_a": venue_a,
            "venue_b": venue_b,
            "status": "no simultaneous ticks in window",
        }

    gaps = [point["gap_bps"] for point in series]
    summary = _summarize_gap_series(gaps, cost_bps)
    coverage_pct = round(100.0 * len(series) / max(1, expected_rounds), 1)
    span_sec = series[-1]["epoch"] - series[0]["epoch"]
    insufficient = len(series) < MIN_FOLLOW_TICKS

    return {
        "pair": pair,
        "venue_a": venue_a,
        "venue_b": venue_b,
        "cost_bps": cost_bps,
        "status": "INSUFFICIENT" if insufficient else "ok",
        "span_sec": span_sec,
        "span_human": common.format_duration(span_sec),
        "expected_rounds": expected_rounds,
        "coverage_pct": coverage_pct,
        "hours": _hour_buckets(series, cost_bps),
        **summary,
    }


def follow_report(
    conn,
    start: int,
    end: int,
    cost_bps: float,
    wanted_pairs: list[str] | None = None,
) -> dict:
    pairs = sorted({pair for pair, _ in recorded_keys(conn, wanted_pairs)})
    multi = [
        pair for pair in pairs
        if len({source for _, source in recorded_keys(conn, [pair])}) > 1
    ]
    reports: list[dict] = []
    for pair in multi:
        venues = sorted({source for _, source in recorded_keys(conn, [pair])})
        for index, venue_a in enumerate(venues):
            for venue_b in venues[index + 1 :]:
                reports.append(
                    follow_venue_pair_report(conn, pair, venue_a, venue_b, start, end, cost_bps)
                )

    eligible = [
        report for report in reports
        if report.get("status") == "ok" and report.get("clears_floor")
    ]
    ranked = sorted(eligible, key=lambda report: report["score"], reverse=True)
    for rank, report in enumerate(ranked, start=1):
        report["rank"] = rank

    return {
        "window": {
            "start_epoch": start,
            "end_epoch": end,
            "start": common.to_ts_utc(common.from_epoch(start)),
            "end": common.to_ts_utc(common.from_epoch(end)),
            "length": common.format_duration(end - start),
        },
        "cost_bps": cost_bps,
        "min_ticks": MIN_FOLLOW_TICKS,
        "venue_pairs": reports,
        "ranking": ranked,
        "any_clears_floor": bool(eligible),
    }


def render_follow(report: dict) -> str:
    lines: list[str] = []
    window = report["window"]
    cost_bps = report["cost_bps"]
    lines.append(
        f"window {window['start']} -> {window['end']}  "
        f"({window['length']})  cost floor {cost_bps:.2f} bps  "
        f"min ticks {report['min_ticks']}"
    )
    lines.append("")

    if not report["venue_pairs"]:
        lines.append("no pair is recorded on more than one venue")
        return "\n".join(lines)

    insufficient = [row for row in report["venue_pairs"] if row.get("status") == "INSUFFICIENT"]
    empty = [row for row in report["venue_pairs"] if row.get("status") not in ("ok", "INSUFFICIENT")]

    if not report["any_clears_floor"]:
        lines.append(
            f"nothing cleared the {cost_bps:.2f} bps floor in this window — ranking nothing."
        )
    else:
        lines.append(
            f"{'rank':>4}  {'pair':<10} {'venues':<22} {'ticks':>6} {'cov%':>6} "
            f"{'median':>7} {'p90':>7} {'p99':>7} {'max':>7} "
            f"{'above%':>7} {'med_run':>8} {'score':>8}"
        )
        for row in report["ranking"]:
            venues = f"{row['venue_a']} vs {row['venue_b']}"
            lines.append(
                f"{row['rank']:>4}  {row['pair']:<10} {venues:<22} {row['ticks']:>6} "
                f"{row['coverage_pct']:>5.1f}% {row['median_bps']:>7.2f} {row['p90_bps']:>7.2f} "
                f"{row['p99_bps']:>7.2f} {row['max_bps']:>7.2f} "
                f"{row['above_floor_pct']:>6.2f}% {row['median_run_ticks']:>8.2f} "
                f"{row['score']:>8.4f}"
            )
        lines.append("")
        for row in report["ranking"]:
            lines.append(
                f"--- rank {row['rank']}: {row['pair']}  {row['venue_a']} vs {row['venue_b']}  "
                f"score {row['score']:.4f} ---"
            )
            lines.append(
                f"  ticks {row['ticks']}  coverage {row['coverage_pct']:.1f}%  "
                f"span {row['span_human']}"
            )
            lines.append(
                f"  gap bps  median {row['median_bps']:.2f}  p90 {row['p90_bps']:.2f}  "
                f"p99 {row['p99_bps']:.2f}  max {row['max_bps']:.2f}"
            )
            lines.append(
                f"  above {cost_bps:.2f} bps floor: {row['above_floor_pct']:.2f}% of ticks  "
                f"median run {row['median_run_ticks']:.2f} ticks  "
                f"({row['run_count']} runs)"
            )
            if row["hours"]:
                lines.append("  by hour UTC:")
                for hour in row["hours"]:
                    lines.append(
                        f"    {hour['hour_utc']:02d}  ticks {hour['ticks']:>5}  "
                        f"median {hour['median_bps']:>6.2f}  above {hour['above_floor_pct']:>6.2f}%  "
                        f"med_run {hour['median_run_ticks']:>5.2f}"
                    )
            lines.append("")

    if insufficient:
        lines.append(f"INSUFFICIENT (< {report['min_ticks']} simultaneous ticks):")
        for row in insufficient:
            lines.append(
                f"  {row['pair']}  {row['venue_a']} vs {row['venue_b']}  "
                f"{row.get('ticks', 0)} ticks  coverage {row.get('coverage_pct', 0.0):.1f}%"
            )
        lines.append("")

    for row in empty:
        lines.append(f"{row['pair']}  {row['venue_a']} vs {row['venue_b']}: {row['status']}")

    lines.append(FOLLOW_COST_CAVEAT)
    return "\n".join(lines).rstrip()


def load_bars(conn, pair: str, start: int, end: int, bucket: int, source: str | None = None) -> list[Bar]:
    """Fold ticks into OHLC buckets. Error rows count but never move the price.

    `source` matters once the same pair is recorded on more than one venue:
    without it two venues' books would fold into one nonsense series.
    """
    cursor = conn.execute(
        f"""
        SELECT {EPOCH_SQL} AS epoch, mid, spread_bps, bid_qty, ask_qty, note
        FROM ticks
        WHERE pair = ? {"AND source = ?" if source else ""}
          AND {EPOCH_SQL} BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, source, start, end) if source else (pair, start, end),
    )

    bars: list[Bar] = []
    spreads: list[float] = []
    imbalances: list[float] = []

    for row in cursor:
        epoch = int(row["epoch"] or 0)
        if epoch <= 0:
            continue
        bucket_start = epoch - (epoch % bucket)

        if not bars or bars[-1].start != bucket_start:
            if bars:
                bars[-1].spread_bps = statistics.fmean(spreads) if spreads else None
                bars[-1].imbalance = statistics.fmean(imbalances) if imbalances else None
            spreads, imbalances = [], []
            bars.append(Bar(bucket_start, 0.0, 0.0, 0.0, 0.0, 0, 0, None, None))

        bar = bars[-1]
        bar.ticks += 1
        if row["note"] is not None or row["mid"] is None:
            bar.errors += 1
            continue

        mid = float(row["mid"])
        if bar.open == 0.0:
            bar.open = bar.high = bar.low = mid
        bar.high = max(bar.high, mid)
        bar.low = min(bar.low, mid)
        bar.close = mid

        if row["spread_bps"] is not None:
            spreads.append(float(row["spread_bps"]))
        bid_qty, ask_qty = row["bid_qty"], row["ask_qty"]
        if bid_qty is not None and ask_qty is not None and (bid_qty + ask_qty) > 0:
            imbalances.append((float(bid_qty) - float(ask_qty)) / (float(bid_qty) + float(ask_qty)))

    if bars:
        bars[-1].spread_bps = statistics.fmean(spreads) if spreads else None
        bars[-1].imbalance = statistics.fmean(imbalances) if imbalances else None

    # Buckets that held nothing but error rows carry no price at all.
    return [bar for bar in bars if bar.close > 0.0]


# --------------------------------------------------------------------------- maths


def linear_regression(values: list[float]) -> tuple[float, float, float]:
    """Least squares over index. Returns (slope per step, intercept, r_squared)."""
    n = len(values)
    if n < 2:
        return 0.0, values[0] if values else 0.0, 0.0
    mean_x = (n - 1) / 2.0
    mean_y = statistics.fmean(values)
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    sxy = sum((i - mean_x) * (values[i] - mean_y) for i in range(n))
    if sxx == 0:
        return 0.0, mean_y, 0.0
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((value - mean_y) ** 2 for value in values)
    r_squared = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return slope, intercept, r_squared


def find_pivots(bars: list[Bar], threshold_pct: float) -> list[Pivot]:
    """Zigzag: a pivot is confirmed once price reverses by `threshold_pct`."""
    if len(bars) < 3 or threshold_pct <= 0:
        return []
    threshold = threshold_pct / 100.0

    pivots: list[Pivot] = []
    direction = 0  # +1 in an upswing (hunting a peak), -1 in a downswing, 0 undecided
    high_index = low_index = 0
    high = low = bars[0].close

    for index in range(1, len(bars)):
        price = bars[index].close
        if price > high:
            high, high_index = price, index
        if price < low:
            low, low_index = price, index

        # While direction is still undecided, whichever reversal confirms first
        # sets it; after that only the reversal we are hunting can fire, so the
        # pivots always alternate peak / trough.
        if direction >= 0 and high > 0 and (high - price) / high >= threshold:
            pivots.append(Pivot(high_index, bars[high_index].start, high, "peak"))
            direction = -1
            high, high_index = price, index
            low, low_index = price, index
        elif direction <= 0 and low > 0 and (price - low) / low >= threshold:
            pivots.append(Pivot(low_index, bars[low_index].start, low, "trough"))
            direction = 1
            high, high_index = price, index
            low, low_index = price, index

    # A pivot on the very first bar is an artifact of the window starting
    # mid-swing, not a confirmed reversal, and it skews the cycle lengths.
    if pivots and pivots[0].index == 0:
        pivots.pop(0)

    return pivots


def periodogram(values: list[float], bucket_sec: int) -> list[dict]:
    """Dominant periods of a linearly detrended series, strongest first.

    Candidate periods are log-spaced so the cost stays bounded no matter how
    many bars the window holds.
    """
    n = len(values)
    if n < MIN_BARS_FOR_CYCLES:
        return []

    slope, intercept, _ = linear_regression(values)
    detrended = [values[i] - (intercept + slope * i) for i in range(n)]
    mean = statistics.fmean(detrended)
    detrended = [value - mean for value in detrended]
    energy = sum(value * value for value in detrended)
    if energy <= 0:
        return []

    min_period, max_period = 4.0, n / 2.0
    if max_period <= min_period:
        return []
    steps = min(PERIOD_CANDIDATES, max(8, n))
    ratio = math.log(max_period / min_period)
    candidates = sorted({round(min_period * math.exp(ratio * k / (steps - 1)), 4) for k in range(steps)})

    spectrum: list[tuple[float, float]] = []
    for period in candidates:
        omega = 2.0 * math.pi / period
        cosine = sum(value * math.cos(omega * i) for i, value in enumerate(detrended))
        sine = sum(value * math.sin(omega * i) for i, value in enumerate(detrended))
        spectrum.append((period, (cosine * cosine + sine * sine) / n))

    total_power = sum(power for _, power in spectrum) or 1.0
    peaks = [
        (period, power)
        for index, (period, power) in enumerate(spectrum)
        if (index == 0 or power > spectrum[index - 1][1]) and (index == len(spectrum) - 1 or power >= spectrum[index + 1][1])
    ]
    peaks.sort(key=lambda item: item[1], reverse=True)

    return [
        {
            "period_bars": round(period, 2),
            "period_sec": round(period * bucket_sec),
            "period_human": common.format_duration(period * bucket_sec),
            "power_share": round(power / total_power, 4),
            "cycles_in_window": round(n / period, 2),
        }
        for period, power in peaks[:3]
    ]


def classify_phase(
    bars: list[Bar], slope_pct_per_day: float, recent_slope_pct_per_day: float, daily_vol_pct: float
) -> dict:
    """Wyckoff-style four-phase read of where the cycle currently sits.

    Deliberately simple: position inside the window's range, plus whether the
    recent leg is trending faster than half a daily sigma.
    """
    closes = [bar.close for bar in bars]
    low, high, last = min(closes), max(closes), closes[-1]
    span = high - low
    position = (last - low) / span if span > 0 else 0.5

    threshold = max(0.5 * daily_vol_pct, 0.05)
    rising = recent_slope_pct_per_day > threshold
    falling = recent_slope_pct_per_day < -threshold

    if position >= 0.6:
        phase = "markup" if rising else ("markdown" if falling else "distribution")
    elif position <= 0.4:
        phase = "markdown" if falling else ("markup" if rising else "accumulation")
    else:
        phase = "markup" if rising else ("markdown" if falling else "ranging")

    return {
        "phase": phase,
        "range_position_pct": round(position * 100, 1),
        "trend_threshold_pct_per_day": round(threshold, 3),
        "window_slope_pct_per_day": round(slope_pct_per_day, 3),
        "recent_slope_pct_per_day": round(recent_slope_pct_per_day, 3),
        "basis": "range position + recent slope vs half a daily sigma (heuristic, not a signal)",
    }


# --------------------------------------------------------------------------- report


def _bucket_suggestion(bars: list[Bar], bucket: int) -> str:
    """Name a bucket that would fit the data actually present, if it differs."""
    if len(bars) < 2:
        return " - record for longer, or use --auto"
    span = bars[-1].start - bars[0].start + bucket
    better = choose_bucket(span)
    if better == bucket:
        return ""
    return (
        f" - {common.format_duration(span)} recorded, so try "
        f"--bucket {common.format_duration(better)} (or --auto)"
    )


def analyse_pair(bars: list[Bar], pair: str, bucket: int, swing_pct: float, window: dict) -> PairReport:
    report = PairReport(pair=pair, bucket_sec=bucket, window=window)
    closes = [bar.close for bar in bars]

    total_ticks = sum(bar.ticks for bar in bars)
    total_errors = sum(bar.errors for bar in bars)
    expected_bars = max(1, (window["end_epoch"] - window["start_epoch"]) // bucket)
    gaps = [
        {
            "after": common.to_ts_utc(common.from_epoch(bars[i].start)),
            "missing_bars": (bars[i + 1].start - bars[i].start) // bucket - 1,
        }
        for i in range(len(bars) - 1)
        if bars[i + 1].start - bars[i].start > bucket
    ]
    report.coverage = {
        "ticks": total_ticks,
        "error_ticks": total_errors,
        "error_rate_pct": round(100.0 * total_errors / total_ticks, 2) if total_ticks else 0.0,
        "bars": len(bars),
        "expected_bars": expected_bars,
        "coverage_pct": round(100.0 * len(bars) / expected_bars, 1),
        "recorded_span_pct": round(
            100.0 * len(bars) / max(1, (bars[-1].start - bars[0].start) // bucket + 1), 1
        ),
        "gap_count": len(gaps),
        "largest_gap_bars": max((gap["missing_bars"] for gap in gaps), default=0),
        "recorded_span_human": common.format_duration(bars[-1].start - bars[0].start + bucket),
        "first_bar": common.to_ts_utc(common.from_epoch(bars[0].start)),
        "last_bar": common.to_ts_utc(common.from_epoch(bars[-1].start)),
    }

    low, high, first, last = min(closes), max(closes), closes[0], closes[-1]
    peak_index = closes.index(high)
    trough_index = closes.index(low)
    report.price = {
        "first": first,
        "last": last,
        "min": low,
        "max": high,
        "change_pct": round(100.0 * (last / first - 1.0), 3) if first > 0 else 0.0,
        "range_pct": round(100.0 * (high / low - 1.0), 3) if low > 0 else 0.0,
        "drawdown_from_high_pct": round(100.0 * (last / high - 1.0), 3) if high > 0 else 0.0,
        "rally_from_low_pct": round(100.0 * (last / low - 1.0), 3) if low > 0 else 0.0,
        "high_at": common.to_ts_utc(common.from_epoch(bars[peak_index].start)),
        "low_at": common.to_ts_utc(common.from_epoch(bars[trough_index].start)),
    }
    spreads = [bar.spread_bps for bar in bars if bar.spread_bps is not None]
    if spreads:
        report.price["avg_spread_bps"] = round(statistics.fmean(spreads), 3)
        report.price["max_spread_bps"] = round(max(spreads), 3)
    imbalances = [bar.imbalance for bar in bars if bar.imbalance is not None]
    if imbalances:
        report.price["avg_book_imbalance"] = round(statistics.fmean(imbalances), 4)

    suggestion = _bucket_suggestion(bars, bucket)
    if len(bars) < MIN_BARS_FOR_STATS:
        report.notes.append(f"only {len(bars)} bars - too few for trend or cycle statistics{suggestion}")
        return report

    logs = [math.log(close) for close in closes]
    bars_per_day = 86400.0 / bucket
    returns = [logs[i] - logs[i - 1] for i in range(1, len(logs))]
    bar_vol = statistics.stdev(returns) if len(returns) > 1 else 0.0
    daily_vol_pct = 100.0 * bar_vol * math.sqrt(bars_per_day)

    slope, _, r_squared = linear_regression(logs)
    slope_pct_per_day = 100.0 * (math.exp(slope * bars_per_day) - 1.0)
    recent = logs[-max(MIN_BARS_FOR_STATS, len(logs) // 4):]
    recent_slope, _, _ = linear_regression(recent)
    recent_slope_pct_per_day = 100.0 * (math.exp(recent_slope * bars_per_day) - 1.0)

    report.trend = {
        "slope_pct_per_day": round(slope_pct_per_day, 3),
        "recent_slope_pct_per_day": round(recent_slope_pct_per_day, 3),
        "r_squared": round(r_squared, 3),
        "bar_volatility_pct": round(100.0 * bar_vol, 4),
        "daily_volatility_pct": round(daily_vol_pct, 3),
        "direction": "up" if slope > 0 else ("down" if slope < 0 else "flat"),
    }

    pivots = find_pivots(bars, swing_pct)
    peaks = [pivot for pivot in pivots if pivot.kind == "peak"]
    troughs = [pivot for pivot in pivots if pivot.kind == "trough"]
    peak_to_peak = [peaks[i].epoch - peaks[i - 1].epoch for i in range(1, len(peaks))]
    trough_to_trough = [troughs[i].epoch - troughs[i - 1].epoch for i in range(1, len(troughs))]
    full_cycles = peak_to_peak + trough_to_trough
    legs = [
        abs(pivots[i].price / pivots[i - 1].price - 1.0) * 100.0
        for i in range(1, len(pivots))
        if pivots[i - 1].price > 0
    ]

    report.swings = {
        "threshold_pct": swing_pct,
        "pivot_count": len(pivots),
        "peaks": len(peaks),
        "troughs": len(troughs),
        "mean_leg_move_pct": round(statistics.fmean(legs), 3) if legs else None,
        "mean_cycle_sec": round(statistics.fmean(full_cycles)) if full_cycles else None,
        "mean_cycle_human": common.format_duration(statistics.fmean(full_cycles)) if full_cycles else None,
        "median_cycle_human": common.format_duration(statistics.median(full_cycles)) if full_cycles else None,
        "completed_cycles": len(full_cycles),
        "recent_pivots": [
            {
                "kind": pivot.kind,
                "at": common.to_ts_utc(common.from_epoch(pivot.epoch)),
                "price": pivot.price,
            }
            for pivot in pivots[-6:]
        ],
    }
    if pivots:
        last_pivot = pivots[-1]
        report.swings["current_leg"] = {
            "direction": "up" if last_pivot.kind == "trough" else "down",
            "since": common.to_ts_utc(common.from_epoch(last_pivot.epoch)),
            "age_human": common.format_duration(bars[-1].start - last_pivot.epoch),
            "move_pct": round(100.0 * (last / last_pivot.price - 1.0), 3) if last_pivot.price > 0 else None,
        }
    else:
        suggested_swing = choose_swing(bars)
        report.notes.append(
            f"no {swing_pct}% swing found - this pair moves about {suggested_swing}% per swing here, "
            f"try --swing {suggested_swing} (or --auto)"
        )

    report.periodicity = {"dominant": periodogram(logs, bucket)}
    if len(bars) < MIN_BARS_FOR_CYCLES:
        report.notes.append(
            f"{len(bars)} bars is below the {MIN_BARS_FOR_CYCLES} needed for periodicity{suggestion}"
        )
    else:
        for entry in report.periodicity["dominant"]:
            if entry["cycles_in_window"] < 3:
                entry["confidence"] = "low"
            elif entry["power_share"] < 0.1:
                entry["confidence"] = "low"
            else:
                entry["confidence"] = "moderate"

    report.phase = classify_phase(bars, slope_pct_per_day, recent_slope_pct_per_day, daily_vol_pct)
    return report


# --------------------------------------------------------------------------- output


def price_fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 10:
        return f"{value:.4f}"
    if value >= 1:
        # A stablecoin sits just above 1.0 and its peg moves in the 5th and 6th
        # digits; four decimals would round every deviation away.
        return f"{value:.6f}"
    return f"{value:.8f}"


def render_text(report: PairReport) -> str:
    lines: list[str] = []
    bucket = common.format_duration(report.bucket_sec)
    lines.append("")
    lines.append(f"=== {report.pair} ===")
    lines.append(
        f"window {report.window['start']} -> {report.window['end']}  bucket {bucket}"
        f"  swing {report.swings.get('threshold_pct', '-')}%"
    )

    coverage = report.coverage
    lines.append(
        f"coverage   {coverage['bars']}/{coverage['expected_bars']} bars "
        f"({coverage['coverage_pct']}% of the window, {coverage['recorded_span_pct']}% of what was recorded), "
        f"{coverage['ticks']} ticks, {coverage['error_ticks']} errored ({coverage['error_rate_pct']}%), "
        f"{coverage['gap_count']} gaps (largest {coverage['largest_gap_bars']} bars)"
    )

    price = report.price
    lines.append(
        f"price      {price_fmt(price['first'])} -> {price_fmt(price['last'])} "
        f"({price['change_pct']:+.2f}%)  range {price_fmt(price['min'])}..{price_fmt(price['max'])} "
        f"({price['range_pct']:.2f}%)"
    )
    lines.append(
        f"           high {price['high_at']}, low {price['low_at']}, "
        f"now {price['drawdown_from_high_pct']:+.2f}% from high / {price['rally_from_low_pct']:+.2f}% from low"
    )
    if "avg_spread_bps" in price:
        book = f"spread avg {price['avg_spread_bps']:.2f} bps (max {price['max_spread_bps']:.2f})"
        if "avg_book_imbalance" in price:
            book += f", book imbalance {price['avg_book_imbalance']:+.3f}"
        lines.append(f"           {book}")

    if report.trend:
        trend = report.trend
        lines.append(
            f"trend      {trend['slope_pct_per_day']:+.2f}%/day over the window "
            f"(R2 {trend['r_squared']:.2f}), recent {trend['recent_slope_pct_per_day']:+.2f}%/day, "
            f"daily vol {trend['daily_volatility_pct']:.2f}%"
        )

    if report.swings:
        swings = report.swings
        lines.append(
            f"swings     {swings['pivot_count']} pivots at {swings['threshold_pct']}% "
            f"({swings['peaks']} peaks / {swings['troughs']} troughs), "
            f"{swings['completed_cycles']} completed cycles"
        )
        if swings.get("mean_cycle_human"):
            lines.append(
                f"           cycle length mean {swings['mean_cycle_human']} / "
                f"median {swings['median_cycle_human']}, mean leg {swings['mean_leg_move_pct']:.2f}%"
            )
        leg = swings.get("current_leg")
        if leg:
            lines.append(
                f"           current leg {leg['direction']} for {leg['age_human']} "
                f"({leg['move_pct']:+.2f}% since the last {('trough' if leg['direction'] == 'up' else 'peak')})"
            )
        for pivot in swings.get("recent_pivots", [])[-4:]:
            lines.append(f"             {pivot['kind']:<6} {pivot['at']}  {price_fmt(pivot['price'])}")

    dominant = report.periodicity.get("dominant") if report.periodicity else None
    if dominant:
        lines.append("periodicity")
        for entry in dominant:
            lines.append(
                f"             ~{entry['period_human']:<6} "
                f"({entry['period_bars']} bars, {entry['cycles_in_window']} cycles in window) "
                f"power {entry['power_share'] * 100:.1f}% [{entry.get('confidence', 'n/a')}]"
            )

    if report.phase:
        phase = report.phase
        lines.append(
            f"phase      {phase['phase'].upper()} - {phase['range_position_pct']:.0f}% up the window range, "
            f"recent {phase['recent_slope_pct_per_day']:+.2f}%/day vs "
            f"+/-{phase['trend_threshold_pct_per_day']:.2f}%/day trend threshold"
        )

    for note in report.notes:
        lines.append(f"note       {note}")
    return "\n".join(lines)


def render_brief(report: PairReport) -> str:
    """A few narrow lines per pair - readable on a phone, no wide tables."""
    price, coverage = report.price, report.coverage
    lines = [f"{report.pair}  {price_fmt(price['last'])}"]
    # The change spans the data that exists, which is rarely the whole window.
    lines.append(f"  move   {price['change_pct']:+.2f}% over {coverage['recorded_span_human']}")

    position = report.phase.get("range_position_pct")
    where = f", now {position:.0f}% up it" if position is not None else ""
    lines.append(f"  range  {price['range_pct']:.2f}%{where}")

    swings = report.swings
    if swings.get("mean_cycle_human"):
        lines.append(
            f"  cycle  ~{swings['mean_cycle_human']} "
            f"({swings['completed_cycles']} seen, {swings['threshold_pct']}% swings)"
        )
    dominant = (report.periodicity or {}).get("dominant") or []
    if dominant:
        best = dominant[0]
        lines.append(f"  repeat ~{best['period_human']} [{best.get('confidence', 'n/a')}]")
    if not swings.get("mean_cycle_human") and not dominant:
        lines.append("  cycle  not enough history yet")

    if report.phase:
        lines.append(f"  phase  {report.phase['phase'].upper()}")
    lines.append(
        f"  data   {coverage['recorded_span_human']}, "
        f"{coverage['gap_count']} gaps, {coverage['error_rate_pct']}% errors"
    )
    return "\n".join(lines)


def render_csv(pair: str, bars: list[Bar]) -> str:
    lines = ["pair,bucket_start_utc,open,high,low,close,ticks,errors,spread_bps,imbalance"]
    for bar in bars:
        lines.append(
            ",".join(
                [
                    pair,
                    common.to_ts_utc(common.from_epoch(bar.start)),
                    f"{bar.open:.10g}",
                    f"{bar.high:.10g}",
                    f"{bar.low:.10g}",
                    f"{bar.close:.10g}",
                    str(bar.ticks),
                    str(bar.errors),
                    "" if bar.spread_bps is None else f"{bar.spread_bps:.4f}",
                    "" if bar.imbalance is None else f"{bar.imbalance:.4f}",
                ]
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- basis (CEX vs on-chain)


def dual_venue_basis_pairs(conn, wanted: list[str] | None = None) -> list[str]:
    """Pairs recorded on both a CEX source and Jupiter with priced mids."""
    rows = conn.execute(
        """
        SELECT b.pair
        FROM (
            SELECT DISTINCT pair FROM ticks
            WHERE source = 'binance' AND mid IS NOT NULL AND note IS NULL
        ) b
        INNER JOIN (
            SELECT DISTINCT pair FROM ticks
            WHERE source = 'jupiter' AND mid IS NOT NULL AND note IS NULL
        ) j ON j.pair = b.pair
        ORDER BY b.pair
        """
    ).fetchall()
    pairs = [row["pair"] for row in rows]
    if wanted:
        names = {name.upper() for name in wanted}
        pairs = [pair for pair in pairs if pair.upper() in names]
    return pairs


def basis_keys(conn, wanted: list[str] | None = None, venue: str | None = None) -> list[tuple[str, str]]:
    """Pairs with dual-venue CEX/Jupiter ticks, on-chain ref, and/or per-pool samples."""
    keys: list[tuple[str, str]] = [(pair, "binance") for pair in dual_venue_basis_pairs(conn, wanted)]

    rows = conn.execute(
        """
        SELECT DISTINCT pair, source
        FROM ticks
        WHERE onchain_ref IS NOT NULL OR onchain_note IS NOT NULL
        UNION
        SELECT DISTINCT pair, 'binance' AS source
        FROM pool_samples
        ORDER BY pair, source
        """
    ).fetchall()
    for row in rows:
        key = (row["pair"], row["source"])
        if key not in keys:
            keys.append(key)
    if wanted:
        names = {name.upper() for name in wanted}
        keys = [key for key in keys if key[0].upper() in names]
    if venue:
        venue_lower = venue.lower()
        if venue_lower == "jupiter":
            keys = [key for key in keys if key in dual_venue_basis_pairs(conn, wanted)]
        else:
            keys = [key for key in keys if key[1].lower() == venue_lower]
    return keys


def dual_venue_basis_series(
    conn,
    pair: str,
    start: int,
    end: int,
    cex_source: str = "binance",
    dex_source: str = "jupiter",
) -> list[dict]:
    """Per-tick basis from CEX and Jupiter mids joined on ts_epoch."""
    rows = conn.execute(
        f"""
        SELECT b.ts_epoch AS epoch, b.mid AS cex_mid, j.mid AS dex_mid
        FROM ticks b
        INNER JOIN ticks j
          ON j.pair = b.pair AND j.ts_epoch = b.ts_epoch AND b.ts_epoch > 0
        WHERE b.pair = ? AND b.source = ? AND j.source = ?
          AND b.mid IS NOT NULL AND j.mid IS NOT NULL
          AND b.note IS NULL AND j.note IS NULL
          AND b.ts_epoch BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, cex_source, dex_source, start, end),
    ).fetchall()

    series: list[dict] = []
    for row in rows:
        cex_mid = float(row["cex_mid"])
        dex_mid = float(row["dex_mid"])
        if dex_mid <= 0:
            continue
        basis_bps = (cex_mid - dex_mid) / dex_mid * 10000.0
        series.append(
            {
                "epoch": int(row["epoch"]),
                "mid": cex_mid,
                "onchain_ref": dex_mid,
                "cex_mid": cex_mid,
                "dex_mid": dex_mid,
                "basis_bps": float(basis_bps),
            }
        )
    return series


def _source_span(conn, pair: str, source: str, start: int, end: int) -> dict:
    """How long one venue has priced ticks in the window."""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS ticks,
               MIN({EPOCH_SQL}) AS first_epoch,
               MAX({EPOCH_SQL}) AS last_epoch
        FROM ticks
        WHERE pair = ? AND source = ? AND mid IS NOT NULL AND note IS NULL
          AND {EPOCH_SQL} BETWEEN ? AND ?
        """,
        (pair, source, start, end),
    ).fetchone()
    ticks = int(row["ticks"] or 0)
    if ticks < 2:
        span = 0
    else:
        span = max(0, int(row["last_epoch"]) - int(row["first_epoch"]))
    return {
        "ticks": ticks,
        "span_sec": span,
        "span_human": common.format_duration(span),
        "meets_24h": span >= 86400,
    }


def basis_series(conn, pair: str, start: int, end: int, source: str = "binance") -> list[dict]:
    """Per-tick CEX mid vs Jupiter reference, both legs from the same timestamp."""
    rows = conn.execute(
        f"""
        SELECT {EPOCH_SQL} AS epoch, mid, onchain_ref, basis_bps, onchain_impact_bps, onchain_note
        FROM ticks
        WHERE pair = ? AND source = ? AND {EPOCH_SQL} BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, source, start, end),
    ).fetchall()

    series: list[dict] = []
    for row in rows:
        if row["mid"] is None:
            continue
        mid = float(row["mid"])
        onchain_ref = row["onchain_ref"]
        basis_bps = row["basis_bps"]
        if basis_bps is None and onchain_ref is not None and float(onchain_ref) > 0:
            basis_bps = (mid - float(onchain_ref)) / float(onchain_ref) * 10000.0
        if basis_bps is None:
            continue
        series.append(
            {
                "epoch": int(row["epoch"]),
                "mid": mid,
                "onchain_ref": float(onchain_ref) if onchain_ref is not None else None,
                "basis_bps": float(basis_bps),
                "impact_bps": float(row["onchain_impact_bps"]) if row["onchain_impact_bps"] is not None else None,
                "onchain_note": row["onchain_note"],
            }
        )
    return series


def pool_dexes(conn, pair: str, start: int, end: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT dex
        FROM pool_samples
        WHERE pair = ? AND ts_epoch BETWEEN ? AND ?
        ORDER BY dex
        """,
        (pair, start, end),
    ).fetchall()
    return [row["dex"] for row in rows]


def pool_basis_series(conn, pair: str, dex: str, start: int, end: int) -> list[dict]:
    """Per-tick basis for one DEX pool sample aligned to the CEX tick."""
    rows = conn.execute(
        """
        SELECT ps.ts_epoch AS epoch, ps.ref_price AS onchain_ref, ps.basis_bps,
               ps.impact_bps, ps.note AS onchain_note, t.mid
        FROM pool_samples ps
        LEFT JOIN ticks t
          ON t.pair = ps.pair AND t.ts_epoch = ps.ts_epoch AND t.source = 'binance'
        WHERE ps.pair = ? AND ps.dex = ? AND ps.ts_epoch BETWEEN ? AND ?
        ORDER BY ps.ts_epoch
        """,
        (pair, dex, start, end),
    ).fetchall()

    series: list[dict] = []
    for row in rows:
        if row["onchain_note"] is not None or row["onchain_ref"] is None:
            continue
        mid = float(row["mid"]) if row["mid"] is not None else None
        basis_bps = row["basis_bps"]
        onchain_ref = float(row["onchain_ref"])
        if basis_bps is None and mid is not None and onchain_ref > 0:
            basis_bps = (mid - onchain_ref) / onchain_ref * 10000.0
        if basis_bps is None:
            continue
        series.append(
            {
                "epoch": int(row["epoch"]),
                "mid": mid,
                "onchain_ref": onchain_ref,
                "basis_bps": float(basis_bps),
                "impact_bps": float(row["impact_bps"]) if row["impact_bps"] is not None else None,
                "onchain_note": None,
                "dex": dex,
            }
        )
    return series


def cross_pool_series(conn, pair: str, start: int, end: int) -> list[dict]:
    """Per-tick spread between cheapest and dearest pool (same quote mint)."""
    rows = conn.execute(
        """
        SELECT ts_epoch AS epoch, dex, ref_price
        FROM pool_samples
        WHERE pair = ? AND ts_epoch BETWEEN ? AND ? AND note IS NULL AND ref_price IS NOT NULL
        ORDER BY ts_epoch, dex
        """,
        (pair, start, end),
    ).fetchall()

    by_epoch: dict[int, list[tuple[str, float]]] = {}
    for row in rows:
        by_epoch.setdefault(int(row["epoch"]), []).append((row["dex"], float(row["ref_price"])))

    series: list[dict] = []
    for epoch in sorted(by_epoch):
        pools = by_epoch[epoch]
        if len(pools) < 2:
            continue
        best_buy = min(pools, key=lambda item: item[1])
        best_sell = max(pools, key=lambda item: item[1])
        if best_buy[1] <= 0:
            continue
        spread_bps = (best_sell[1] - best_buy[1]) / best_buy[1] * 10000.0
        series.append(
            {
                "epoch": epoch,
                "spread_bps": spread_bps,
                "best_buy_dex": best_buy[0],
                "best_buy_ref": best_buy[1],
                "best_sell_dex": best_sell[0],
                "best_sell_ref": best_sell[1],
                "pool_count": len(pools),
            }
        )
    return series


def _median_tick_interval(series: list[dict]) -> float | None:
    """Median seconds between consecutive joined ticks (1.0 when INTERVAL_SEC=1)."""
    if len(series) < 2:
        return None
    gaps = [series[index + 1]["epoch"] - series[index]["epoch"] for index in range(len(series) - 1)]
    return float(statistics.median(gaps))


def _summarize_basis_series(
    series: list[dict],
    *,
    held_bps: float,
    edge_bps: float,
    floor_bps: float,
    min_ticks: int,
) -> dict:
    values = [point["basis_bps"] for point in series]
    last = series[-1]
    span = series[-1]["epoch"] - series[0]["epoch"]
    held = _basis_runs(series, kind="held", threshold_bps=held_bps, min_ticks=min_ticks)
    edge = _basis_runs(series, kind="edge", threshold_bps=edge_bps, min_ticks=min_ticks)
    episodes = _basis_runs(series, kind="edge", threshold_bps=floor_bps, min_ticks=min_ticks)
    median_interval = _median_tick_interval(series)
    return {
        "ticks": len(series),
        "span_sec": span,
        "span_human": common.format_duration(span),
        "median_tick_sec": median_interval,
        "last": {
            "epoch": last["epoch"],
            "mid": last.get("mid"),
            "onchain_ref": last["onchain_ref"],
            "cex_mid": last.get("cex_mid", last.get("mid")),
            "dex_mid": last.get("dex_mid", last.get("onchain_ref")),
            "basis_bps": round(last["basis_bps"], 2),
            "impact_bps": last.get("impact_bps"),
            "dex": last.get("dex"),
        },
        "stats": {
            "mean_bps": round(statistics.fmean(values), 2),
            "stdev_bps": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
            "min_bps": round(min(values), 2),
            "max_bps": round(max(values), 2),
        },
        "held_periods": held,
        "edge_periods": edge,
        "episodes": episodes,
        "held_ticks_pct": round(100.0 * sum(run["ticks"] for run in held) / len(series), 1),
        "edge_ticks_pct": round(100.0 * sum(run["ticks"] for run in edge) / len(series), 1),
        "episode_ticks_pct": round(100.0 * sum(run["ticks"] for run in episodes) / len(series), 1),
    }


def _basis_runs(
    series: list[dict],
    *,
    kind: str,
    threshold_bps: float,
    min_ticks: int,
) -> list[dict]:
    """Find consecutive runs where |basis| is inside (held) or outside (edge) a band."""
    runs: list[dict] = []
    run_start: int | None = None
    run_values: list[float] = []

    def flush(end_index: int) -> None:
        nonlocal run_start, run_values
        if run_start is None or len(run_values) < min_ticks:
            run_start, run_values = None, []
            return
        start_epoch = series[run_start]["epoch"]
        end_epoch = series[end_index]["epoch"]
        runs.append(
            {
                "kind": kind,
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
                "ticks": len(run_values),
                "duration_sec": max(0, end_epoch - start_epoch),
                "mean_bps": round(statistics.fmean(run_values), 2),
                "min_bps": round(min(run_values), 2),
                "max_bps": round(max(run_values), 2),
            }
        )
        run_start, run_values = None, []

    for index, point in enumerate(series):
        value = point["basis_bps"]
        inside = abs(value) <= threshold_bps
        matches = inside if kind == "held" else not inside and abs(value) >= threshold_bps
        if matches:
            if run_start is None:
                run_start = index
            run_values.append(value)
        elif run_start is not None:
            flush(index - 1)
    if run_start is not None:
        flush(len(series) - 1)
    return runs


def basis_report(
    conn,
    pair: str,
    start: int,
    end: int,
    source: str = "binance",
    held_bps: float = 15.0,
    edge_bps: float = 40.0,
    floor_bps: float = 40.0,
    min_ticks: int = 3,
    dex_source: str = "jupiter",
) -> dict:
    thresholds = {
        "held_bps": held_bps,
        "edge_bps": edge_bps,
        "floor_bps": floor_bps,
        "min_ticks": min_ticks,
    }
    report: dict = {"pair": pair, "source": source, "thresholds": thresholds, "pools": {}}

    dual = dual_venue_basis_series(conn, pair, start, end, source, dex_source)
    if dual:
        report["mode"] = "dual_venue"
        report["dex_source"] = dex_source
        report.update(
            _summarize_basis_series(
                dual,
                held_bps=held_bps,
                edge_bps=edge_bps,
                floor_bps=floor_bps,
                min_ticks=min_ticks,
            )
        )
        report["aggregated"] = True
        report["coverage"] = {
            "cex": _source_span(conn, pair, source, start, end),
            "dex": _source_span(conn, pair, dex_source, start, end),
            "paired": {
                "ticks": len(dual),
                "span_sec": dual[-1]["epoch"] - dual[0]["epoch"] if len(dual) > 1 else 0,
                "span_human": common.format_duration(
                    dual[-1]["epoch"] - dual[0]["epoch"] if len(dual) > 1 else 0
                ),
                "meets_24h": len(dual) > 1 and dual[-1]["epoch"] - dual[0]["epoch"] >= 86400,
            },
        }
    else:
        report["mode"] = "onchain_ref"
        aggregated = basis_series(conn, pair, start, end, source)
        if aggregated:
            report.update(
                _summarize_basis_series(
                    aggregated,
                    held_bps=held_bps,
                    edge_bps=edge_bps,
                    floor_bps=floor_bps,
                    min_ticks=min_ticks,
                )
            )
            report["aggregated"] = True
        else:
            report["aggregated"] = False

    for dex in pool_dexes(conn, pair, start, end):
        pool_series = pool_basis_series(conn, pair, dex, start, end)
        if pool_series:
            report["pools"][dex] = _summarize_basis_series(
                pool_series,
                held_bps=held_bps,
                edge_bps=edge_bps,
                floor_bps=floor_bps,
                min_ticks=min_ticks,
            )

    cross = cross_pool_series(conn, pair, start, end)
    if cross:
        spreads = [point["spread_bps"] for point in cross]
        last = cross[-1]
        report["cross_pool"] = {
            "ticks": len(cross),
            "last": {
                "spread_bps": round(last["spread_bps"], 2),
                "best_buy_dex": last["best_buy_dex"],
                "best_buy_ref": last["best_buy_ref"],
                "best_sell_dex": last["best_sell_dex"],
                "best_sell_ref": last["best_sell_ref"],
                "pool_count": last["pool_count"],
            },
            "stats": {
                "mean_spread_bps": round(statistics.fmean(spreads), 2),
                "max_spread_bps": round(max(spreads), 2),
            },
        }

    if not report.get("aggregated") and not report["pools"]:
        report["status"] = "no paired CEX/on-chain ticks in window"
    return report


def _render_basis_leg(name: str, leg: dict, thresholds: dict) -> list[str]:
    lines: list[str] = []
    last = leg["last"]
    stats = leg["stats"]
    cex_mid = last.get("cex_mid", last.get("mid"))
    dex_mid = last.get("dex_mid", last.get("onchain_ref"))
    cex_text = price_fmt(cex_mid) if cex_mid is not None else "-"
    dex_text = price_fmt(dex_mid) if dex_mid is not None else "-"
    lines.append(
        f"  {name:<10} basis {last['basis_bps']:+.2f} bps  "
        f"(cex {cex_text} vs dex {dex_text})"
    )
    interval = leg.get("median_tick_sec")
    interval_text = f"  median {interval:.0f}s/tick" if interval is not None else ""
    lines.append(
        f"             {leg['ticks']} ticks  mean {stats['mean_bps']:+.2f} bps  "
        f"range {stats['min_bps']:+.2f}..{stats['max_bps']:+.2f}{interval_text}"
    )
    held = leg["held_periods"]
    edge = leg["edge_periods"]
    episodes = leg.get("episodes", edge)
    lines.append(
        f"             held {len(held)} ({leg['held_ticks_pct']}%)  "
        f"edge {len(edge)} ({leg['edge_ticks_pct']}%)  "
        f"(|basis|<={thresholds['held_bps']:.0f} / >={thresholds['edge_bps']:.0f} bps)"
    )
    lines.append(
        f"             episodes {len(episodes)} ({leg.get('episode_ticks_pct', 0)}%)  "
        f"(|basis|>={thresholds['floor_bps']:.0f} bps, min {thresholds['min_ticks']} ticks)"
    )
    for episode in episodes[:3]:
        lines.append(
            f"               {common.format_duration(episode['duration_sec'])}  "
            f"{episode['ticks']} ticks  mean {episode['mean_bps']:+.2f} bps"
        )
    return lines


def render_basis(reports: list[dict]) -> str:
    lines: list[str] = []
    for report in reports:
        if report.get("status") and not report.get("pools"):
            lines.append(f"{report['pair']}@{report['source']}: {report['status']}")
            continue
        if report.get("mode") == "dual_venue":
            label = f"{report['pair']}  {report['source']}+{report['dex_source']}"
        else:
            label = f"{report['pair']}@{report['source']}"
        thresholds = report["thresholds"]
        lines.append(f"=== {label} ===")

        coverage = report.get("coverage")
        if coverage:
            cex = coverage["cex"]
            dex = coverage["dex"]
            paired = coverage["paired"]
            lines.append(
                f"  coverage   cex {cex['span_human']} ({cex['ticks']} ticks"
                f"{', >=24h' if cex['meets_24h'] else ''})  "
                f"dex {dex['span_human']} ({dex['ticks']} ticks"
                f"{', >=24h' if dex['meets_24h'] else ''})  "
                f"paired {paired['span_human']} ({paired['ticks']} ticks"
                f"{', >=24h' if paired['meets_24h'] else ''})"
            )

        if report.get("aggregated") and "last" in report:
            leg_name = "dual_venue" if report.get("mode") == "dual_venue" else "aggregated"
            lines.extend(_render_basis_leg(leg_name, report, thresholds))

        cross = report.get("cross_pool")
        if cross:
            last = cross["last"]
            stats = cross["stats"]
            lines.append(
                f"  cross-pool {last['spread_bps']:+.2f} bps now  "
                f"buy {last['best_buy_dex']}@{price_fmt(last['best_buy_ref'])}  "
                f"sell {last['best_sell_dex']}@{price_fmt(last['best_sell_ref'])}  "
                f"({last['pool_count']} pools)"
            )
            lines.append(
                f"             mean spread {stats['mean_spread_bps']:+.2f} bps  "
                f"max {stats['max_spread_bps']:+.2f} bps over {cross['ticks']} ticks"
            )

        for dex in sorted(report.get("pools", {})):
            lines.extend(_render_basis_leg(dex, report["pools"][dex], thresholds))
        lines.append("")

    if any(not report.get("status") or report.get("pools") for report in reports):
        lines.append("Basis is (cex_mid - dex_mid) / dex_mid * 1e4 per joined tick (ts_epoch).")
        lines.append("Dual-venue mode joins binance + jupiter source rows; legacy mode uses onchain_ref.")
        lines.append("Episode duration follows actual tick spacing (1s when INTERVAL_SEC=1), not 30s buckets.")
        lines.append("No fees, latency or execution path are counted — measurement for eyes, not send.")
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyse recorded oakring ticks for market cycles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="database path (default: DB_PATH from .env)")
    parser.add_argument("--pair", action="append", dest="pairs", help="pair to analyse, repeatable (default: all recorded)")
    parser.add_argument("--since", default="7d", help="window length back from now, e.g. 90m, 24h, 7d (default: 7d)")
    parser.add_argument("--bucket", default="1h", help="resample bucket, e.g. 5m, 1h, 4h (default: 1h)")
    parser.add_argument("--swing", type=float, default=2.0, help="zigzag reversal threshold in percent (default: 2.0)")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="pick --bucket and --swing per pair from how much is recorded and how much it moves",
    )
    parser.add_argument(
        "--format",
        choices=("text", "brief", "json", "csv"),
        default="text",
        help="output format: text, brief (phone sized), json, csv (default: text)",
    )
    parser.add_argument("--list-pairs", action="store_true", help="list recorded pairs and exit")
    parser.add_argument("--venue", help="restrict to one venue (binance, coinbase, kraken, okx, bybit)")
    parser.add_argument(
        "--venues",
        action="store_true",
        help="compare each pair's book across the venues recording it, and exit",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="rank venue-pair dislocations that clear --cost-bps often and persistently, then exit",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=None,
        help="required with --follow: your all-in cost floor in bps (no default — you must state it)",
    )
    parser.add_argument(
        "--cross",
        action="store_true",
        help=f"compare each asset's USDT and USDC books against {PEG_PAIR}, and exit",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="show the current price per pair with 1h/24h/7d change, and exit",
    )
    parser.add_argument(
        "--stale-after",
        default="5m",
        help="flag a pair whose last tick is older than this in --latest (default: 5m)",
    )
    parser.add_argument(
        "--basis",
        action="store_true",
        help="CEX mid vs on-chain Jupiter reference over --since, then exit",
    )
    parser.add_argument(
        "--held-bps",
        type=float,
        default=15.0,
        help="|basis| at or below this for consecutive ticks counts as a held period (default: 15)",
    )
    parser.add_argument(
        "--edge-bps",
        type=float,
        default=40.0,
        help="|basis| at or above this for consecutive ticks counts as an edge period (default: 40)",
    )
    parser.add_argument(
        "--basis-min-ticks",
        type=int,
        default=3,
        help="minimum consecutive ticks to count a held/edge period (default: 3)",
    )
    parser.add_argument(
        "--floor-bps",
        type=float,
        default=None,
        help="|basis| at or above this for consecutive ticks counts as an episode (default: --edge-bps)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = common.load_config()
    common.setup_logging("WARNING")

    db_path = Path(args.db) if args.db else common.db_path_from(env)
    try:
        conn = common.connect(db_path, read_only=True)
    except FileNotFoundError:
        print(f"no database at {db_path} - run recorder.py first", file=sys.stderr)
        return 2

    try:
        if args.list_pairs:
            rows = list_pairs(conn)
            if not rows:
                print("no ticks recorded yet", file=sys.stderr)
                return 1
            print(f"{'pair':<12} {'ticks':>8}  first                 last")
            for pair, count, first_ts, last_ts in rows:
                print(f"{pair:<12} {count:>8}  {first_ts}  {last_ts}")
            return 0

        if args.venues:
            try:
                since_sec = common.parse_duration(args.since)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            end_epoch = common.to_epoch(common.now_utc())
            pairs = sorted({pair for pair, _ in recorded_keys(conn, args.pairs)})
            multi = [
                pair for pair in pairs
                if len({source for _, source in recorded_keys(conn, [pair])}) > 1
            ]
            if not multi:
                print(
                    "no pair is recorded on more than one venue - add e.g. "
                    "WATCHLIST_COINBASE=SOLUSDC to the .env file",
                    file=sys.stderr,
                )
                return 1
            reports = [venue_report(conn, pair, end_epoch - since_sec, end_epoch) for pair in multi]
            if args.format == "json":
                print(json.dumps({"venues": reports}, indent=2))
            else:
                print("oakring venues  |  the same pair, side by side")
                print(render_venues(reports))
                print("")
            return 0

        if args.follow:
            if args.cost_bps is None:
                print("--follow requires --cost-bps (no default — state what you think it costs)", file=sys.stderr)
                return 2
            if args.cost_bps < 0:
                print("--cost-bps must be >= 0", file=sys.stderr)
                return 2
            try:
                since_sec = common.parse_duration(args.since)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            end_epoch = common.to_epoch(common.now_utc())
            start_epoch = end_epoch - since_sec
            pairs = sorted({pair for pair, _ in recorded_keys(conn, args.pairs)})
            multi = [
                pair for pair in pairs
                if len({source for _, source in recorded_keys(conn, [pair])}) > 1
            ]
            if not multi:
                print(
                    "no pair is recorded on more than one venue - add e.g. "
                    "WATCHLIST_COINBASE=SOLUSDC to the .env file",
                    file=sys.stderr,
                )
                return 1
            report = follow_report(conn, start_epoch, end_epoch, args.cost_bps, args.pairs)
            if args.format == "json":
                print(json.dumps({"follow": report}, indent=2))
            else:
                print("oakring follow  |  venue-pair dislocation vs your cost floor")
                print(render_follow(report))
                print("")
            return 0

        if args.cost_bps is not None:
            print("--cost-bps is only used with --follow", file=sys.stderr)
            return 2

        if args.cross:
            try:
                since_sec = common.parse_duration(args.since)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            end_epoch = common.to_epoch(common.now_utc())
            bases = cross_bases(conn, args.pairs)
            if not bases:
                print(
                    f"--cross needs {PEG_PAIR} plus an asset recorded against both USDT and USDC "
                    f"(e.g. BTCUSDT and BTCUSDC)",
                    file=sys.stderr,
                )
                return 1
            source = args.venue or "binance"
            reports = [
                cross_report(conn, base, end_epoch - since_sec, end_epoch, source) for base in bases
            ]
            if args.format == "json":
                print(json.dumps({"cross": reports}, indent=2))
            else:
                print(f"oakring cross  |  {PEG_PAIR} vs the USDT/USDC books")
                print(render_cross(reports))
                print("")
            return 0

        if args.basis:
            try:
                since_sec = common.parse_duration(args.since)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.basis_min_ticks < 1:
                print("--basis-min-ticks must be >= 1", file=sys.stderr)
                return 2
            end_epoch = common.to_epoch(common.now_utc())
            keys = basis_keys(conn, args.pairs, args.venue)
            if not keys:
                print(
                    "no basis data recorded yet - add WATCHLIST_JUPITER=SOLUSDC:SOLUSDT alongside "
                    "WATCHLIST (binance) with INTERVAL_SEC=1, or enable JUPITER_ENABLED=1 in the "
                    "recorder .env and record for a while",
                    file=sys.stderr,
                )
                return 1
            floor_bps = args.edge_bps if args.floor_bps is None else args.floor_bps
            reports = [
                basis_report(
                    conn,
                    pair,
                    end_epoch - since_sec,
                    end_epoch,
                    source,
                    held_bps=args.held_bps,
                    edge_bps=args.edge_bps,
                    floor_bps=floor_bps,
                    min_ticks=args.basis_min_ticks,
                )
                for pair, source in keys
            ]
            if args.format == "json":
                print(json.dumps({"basis": reports}, indent=2))
            else:
                print("oakring basis  |  CEX mid vs Jupiter (dual-venue join on ts_epoch)")
                print(render_basis(reports))
                print("")
            return 0

        if args.latest:
            keys = recorded_keys(conn, args.pairs, args.venue)
            if not keys:
                print("no ticks recorded yet", file=sys.stderr)
                return 1
            try:
                stale_after = common.parse_duration(args.stale_after)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            snapshot = latest_snapshot(conn, keys, show_venue=venue_count(conn) > 1 and not args.venue)
            if args.format == "json":
                print(json.dumps({"latest": snapshot}, indent=2))
            else:
                print(render_latest(snapshot, stale_after))
            return 0

        try:
            since_sec = common.parse_duration(args.since)
            bucket = common.parse_duration(args.bucket)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.swing < 0:
            print("--swing must be >= 0", file=sys.stderr)
            return 2
        if bucket > since_sec and not args.auto:
            print(f"--bucket ({args.bucket}) is larger than --since ({args.since})", file=sys.stderr)
            return 2

        end_epoch = common.to_epoch(common.now_utc())
        start_epoch = end_epoch - since_sec
        window = {
            "start": common.to_ts_utc(common.from_epoch(start_epoch)),
            "end": common.to_ts_utc(common.from_epoch(end_epoch)),
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "length": common.format_duration(since_sec),
        }

        keys = recorded_keys(conn, args.pairs, args.venue)
        if not keys:
            print("no ticks recorded yet", file=sys.stderr)
            return 1
        show_venue = venue_count(conn) > 1 and not args.venue

        reports: list[PairReport] = []
        csv_chunks: list[str] = []
        empty: list[str] = []

        for pair, source in keys:
            label = label_for(pair, source, show_venue)
            pair_bucket, pair_swing = bucket, args.swing

            if args.auto:
                span = recorded_span(conn, pair, start_epoch, end_epoch, source)
                if span:
                    pair_bucket = choose_bucket(span)

            bars = load_bars(conn, pair, start_epoch, end_epoch, pair_bucket, source)
            if not bars:
                empty.append(label)
                continue
            if args.auto:
                pair_swing = choose_swing(bars)

            if args.format == "csv":
                csv_chunks.append(render_csv(label, bars))
            else:
                reports.append(analyse_pair(bars, label, pair_bucket, pair_swing, window))

        if args.format == "csv":
            if not csv_chunks:
                print(f"no priced ticks in the last {window['length']} for: {', '.join(empty)}", file=sys.stderr)
                return 1
            header, *_ = csv_chunks[0].split("\n", 1)
            print(header)
            for chunk in csv_chunks:
                print(chunk.split("\n", 1)[1])
            return 0

        if not reports:
            print(f"no priced ticks in the last {window['length']} for: {', '.join(empty)}", file=sys.stderr)
            return 1

        if args.format == "json":
            print(json.dumps({"window": window, "pairs": [asdict(report) for report in reports]}, indent=2))
        elif args.format == "brief":
            print(f"oakring  {window['length']} to {window['end']}")
            for report in reports:
                print("")
                print(render_brief(report))
            if empty:
                print(f"\nnothing recorded yet: {', '.join(empty)}")
        else:
            print(f"oakring market cycle report  |  db {db_path}")
            for report in reports:
                print(render_text(report))
            if empty:
                print(f"\nno priced ticks in this window for: {', '.join(empty)}")
            print("")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
