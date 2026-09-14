#!/usr/bin/env python3
"""Read the recorded ticks back and look for the market cycle.

Stdlib only. Safe to run while the recorder is writing (opens read-only).

    python3 analyze.py --since 7d --bucket 1h
    python3 analyze.py --pair SOLUSDT --since 30d --bucket 4h --swing 3
    python3 analyze.py --format json --since 24h --bucket 15m > report.json

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


def load_bars(conn, pair: str, start: int, end: int, bucket: int) -> list[Bar]:
    """Fold ticks into OHLC buckets. Error rows count but never move the price."""
    cursor = conn.execute(
        """
        SELECT COALESCE(NULLIF(ts_epoch, 0), CAST(strftime('%s', ts_utc) AS INTEGER)) AS epoch,
               mid, spread_bps, bid_qty, ask_qty, note
        FROM ticks
        WHERE pair = ?
          AND COALESCE(NULLIF(ts_epoch, 0), CAST(strftime('%s', ts_utc) AS INTEGER)) BETWEEN ? AND ?
        ORDER BY epoch
        """,
        (pair, start, end),
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

    if len(bars) < MIN_BARS_FOR_STATS:
        report.notes.append(f"only {len(bars)} bars - too few for trend or cycle statistics")
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
        report.notes.append(
            f"no {swing_pct}% swing found - lower --swing or record a longer window to see cycles"
        )

    report.periodicity = {"dominant": periodogram(logs, bucket)}
    if len(bars) < MIN_BARS_FOR_CYCLES:
        report.notes.append(
            f"{len(bars)} bars is below the {MIN_BARS_FOR_CYCLES} needed for periodicity - widen --since or shrink --bucket"
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
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}"


def render_text(report: PairReport) -> str:
    lines: list[str] = []
    bucket = common.format_duration(report.bucket_sec)
    lines.append("")
    lines.append(f"=== {report.pair} ===")
    lines.append(
        f"window {report.window['start']} -> {report.window['end']}  bucket {bucket}"
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
    parser.add_argument("--format", choices=("text", "json", "csv"), default="text", help="output format (default: text)")
    parser.add_argument("--list-pairs", action="store_true", help="list recorded pairs and exit")
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

        try:
            since_sec = common.parse_duration(args.since)
            bucket = common.parse_duration(args.bucket)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.swing < 0:
            print("--swing must be >= 0", file=sys.stderr)
            return 2
        if bucket > since_sec:
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

        pairs = args.pairs or [row[0] for row in list_pairs(conn)]
        if not pairs:
            print("no ticks recorded yet", file=sys.stderr)
            return 1

        reports: list[PairReport] = []
        csv_chunks: list[str] = []
        empty: list[str] = []

        for pair in pairs:
            pair = pair.upper()
            bars = load_bars(conn, pair, start_epoch, end_epoch, bucket)
            if not bars:
                empty.append(pair)
                continue
            if args.format == "csv":
                csv_chunks.append(render_csv(pair, bars))
            else:
                reports.append(analyse_pair(bars, pair, bucket, args.swing, window))

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
