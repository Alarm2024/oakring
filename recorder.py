#!/usr/bin/env python3
"""Binance bookTicker SQLite recorder (stdlib only).

Polls the configured watchlist on a wall-clock aligned interval and stores one
row per pair per tick: bid, ask, mid, spread and top-of-book sizes. Rows that
failed to fetch are still written, with `note` set, so gaps in the record are
visible to the analyzer instead of silently disappearing.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import timedelta

import common
import jupiter
import venues

USER_AGENT = "oakring/3.0"
_shutdown = threading.Event()
_shutdown_signal: int | None = None


def _handle_signal(signum: int, _frame: object) -> None:
    """Must stay async-signal-safe.

    A signal can land while the main thread is inside a stdout write or holds
    the logging lock, so logging from here raises "reentrant call inside
    BufferedWriter" at best and deadlocks at worst. Record it and let the main
    loop do the talking.
    """
    global _shutdown_signal
    _shutdown_signal = signum
    _shutdown.set()


def http_get_json(url: str, timeout: int) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def fetch_venue(venue: venues.Venue, pairs: dict[str, str], timeout: int) -> dict[str, venues.Quote]:
    """Top of book for these pairs on one venue, keyed by canonical pair.

    `pairs` maps the name we store under to the name this venue uses, so a
    market called SOL-USD on one exchange still lands beside SOLUSDC from
    another. Batched venues take the whole list in one request; the rest are
    asked one at a time, so a single bad symbol costs only that symbol.
    """
    quotes: dict[str, venues.Quote] = {}
    canonical_names = list(pairs)
    groups = [canonical_names] if venue.batched else [[name] for name in canonical_names]

    for group in groups:
        if _shutdown.is_set():
            break
        symbols = [venue.to_symbol(pairs[name]) for name in group]
        payload = http_get_json(venue.build_url(venue.base_url, symbols), timeout)
        parsed = venue.parse(payload, symbols[0])
        for name, symbol in zip(group, symbols):
            quote = parsed.get(symbol) or parsed.get(pairs[name].upper())
            if quote is not None:
                quotes[name] = quote
    return quotes


def fetch_with_retries(
    venue: venues.Venue, pairs: dict[str, str], timeout: int, max_retries: int
) -> tuple[dict[str, venues.Quote], str | None]:
    """Fetch, retried with exponential backoff, then one pair at a time."""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_venue(venue, pairs, timeout), None
        except Exception as exc:  # noqa: BLE001 - any failure is worth retrying
            last_error = exc
            if attempt < max_retries and not _shutdown.is_set():
                delay = min(2.0**attempt, 30.0)
                logging.warning(
                    "%s fetch failed (%s), retrying in %.0fs", venue.name, _describe(exc), delay
                )
                _shutdown.wait(delay)

    if len(pairs) == 1 or not venue.batched or _shutdown.is_set():
        return {}, _describe(last_error)

    logging.warning(
        "%s batch failed (%s), falling back to per-pair", venue.name, _describe(last_error)
    )
    results: dict[str, venues.Quote] = {}
    for name, symbol in pairs.items():
        if _shutdown.is_set():
            break
        try:
            results.update(fetch_venue(venue, {name: symbol}, timeout))
        except Exception as exc:  # noqa: BLE001
            logging.warning("%s %s failed: %s", venue.name, name, _describe(exc))
    return results, None if results else _describe(last_error)


def _describe(exc: Exception | None) -> str:
    if exc is None:
        return "error:Unknown"
    if isinstance(exc, urllib.error.HTTPError):
        return f"error:HTTPError:{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "error:URLError"
    if isinstance(exc, venues.VenueError):
        return "error:VenueError"
    return f"error:{type(exc).__name__}"


def compute_mid_spread(bid: float, ask: float) -> tuple[float, float]:
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0 if mid > 0 else 0.0
    return mid, spread_bps


def compute_basis_bps(mid: float | None, onchain_ref: float | None) -> float | None:
    """CEX mid vs on-chain reference, in basis points."""
    if mid is None or onchain_ref is None or onchain_ref <= 0:
        return None
    return (mid - onchain_ref) / onchain_ref * 10000.0


def row_from_quote(
    ts: str,
    epoch: int,
    pair: str,
    source: str,
    quote: venues.Quote | None,
    fallback_note: str | None,
    onchain: jupiter.JupiterQuote | None = None,
    onchain_note: str | None = None,
) -> tuple:
    """Build one `ticks` row, valid or errored."""
    onchain_ref = onchain.ref_price if onchain is not None else None
    onchain_impact = onchain.impact_bps if onchain is not None else None
    if quote is None:
        return (
            ts, epoch, pair, source, None, None, None, None, None, None,
            fallback_note or "error:Missing", onchain_ref, onchain_impact, None, onchain_note,
        )
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return (
            ts, epoch, pair, source, quote.bid, quote.ask, None, None,
            quote.bid_qty, quote.ask_qty, "error:CrossedBook",
            onchain_ref, onchain_impact, None, onchain_note,
        )
    mid, spread_bps = compute_mid_spread(quote.bid, quote.ask)
    basis_bps = compute_basis_bps(mid, onchain_ref)
    return (
        ts, epoch, pair, source, quote.bid, quote.ask, mid, spread_bps,
        quote.bid_qty, quote.ask_qty, None, onchain_ref, onchain_impact, basis_bps, onchain_note,
    )


def _pad_row(row: tuple) -> tuple:
    """Older callers wrote 11 columns; on-chain fields default to NULL."""
    if len(row) == 11:
        return row + (None, None, None, None)
    return row


def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    rows = [_pad_row(row) for row in rows]
    conn.executemany(
        """
        INSERT INTO ticks
            (ts_utc, ts_epoch, pair, source, bid, ask, mid, spread_bps, bid_qty, ask_qty, note,
             onchain_ref, onchain_impact_bps, basis_bps, onchain_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def pool_sample_rows(
    ts: str,
    epoch: int,
    pair: str,
    cex_mid: float | None,
    samples: list[jupiter.PoolSample],
) -> list[tuple]:
    rows: list[tuple] = []
    for sample in samples:
        basis_bps = compute_basis_bps(cex_mid, sample.ref_price)
        rows.append(
            (
                ts,
                epoch,
                pair,
                sample.dex,
                sample.ref_price,
                basis_bps,
                sample.impact_bps,
                sample.in_amount,
                sample.out_amount,
                sample.amm_key,
                sample.note,
            )
        )
    return rows


def insert_pool_samples(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO pool_samples
            (ts_utc, ts_epoch, pair, dex, ref_price, basis_bps, impact_bps,
             in_amount, out_amount, amm_key, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def record_pool_samples(
    conn: sqlite3.Connection,
    ts: str,
    epoch: int,
    cex_mids: dict[str, float],
    jupiter_cfg: dict[str, object],
    timeout: int,
) -> list[tuple]:
    """Fetch per-DEX quotes for attached pairs and write pool_samples rows."""
    dexes = jupiter_cfg.get("dexes") or []
    attach_pairs = jupiter_cfg.get("attach_pairs") or set()
    if not dexes or not attach_pairs:
        return []

    pool_rows: list[tuple] = []
    for pair in sorted(attach_pairs):
        cex_mid = cex_mids.get(pair)
        samples = jupiter.fetch_pool_samples_for_pair(
            pair, jupiter_cfg, timeout, http_get=_jupiter_http_get
        )
        pool_rows.extend(pool_sample_rows(ts, epoch, pair, cex_mid, samples))
    insert_pool_samples(conn, pool_rows)
    return pool_rows


def fetch_jupiter_quote(
    jupiter_cfg: dict[str, object],
    timeout: int,
) -> tuple[jupiter.JupiterQuote | None, str | None]:
    """One Jupiter quote per tick, retried like venue fetches."""
    if not jupiter_cfg.get("enabled"):
        return None, None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            quote = jupiter.fetch_quote(
                base_url=str(jupiter_cfg["quote_url"]),
                input_mint=str(jupiter_cfg["input_mint"]),
                output_mint=str(jupiter_cfg["output_mint"]),
                amount=int(jupiter_cfg["amount"]),
                slippage_bps=int(jupiter_cfg["slippage_bps"]),
                input_decimals=int(jupiter_cfg["input_decimals"]),
                output_decimals=int(jupiter_cfg["output_decimals"]),
                timeout=timeout,
                api_key=jupiter_cfg.get("api_key"),  # type: ignore[arg-type]
                http_get=_jupiter_http_get,
            )
            return quote, None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2 and not _shutdown.is_set():
                delay = min(2.0**attempt, 10.0)
                logging.warning(
                    "jupiter fetch failed (%s), retrying in %.0fs",
                    jupiter.describe_error(exc),
                    delay,
                )
                _shutdown.wait(delay)
    return None, jupiter.describe_error(last_error)


def _jupiter_http_get(url: str, timeout: int, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def run_tick(
    conn: sqlite3.Connection,
    watchlists: dict[str, dict[str, str]],
    timeout: int,
    max_retries: int,
    jupiter_cfg: dict[str, object] | None = None,
) -> None:
    """One round across every venue, all stamped with the same timestamp.

    The shared timestamp is what makes cross-venue and cross-pair comparison
    exact later: every leg of a comparison comes from the same instant.
    """
    moment = common.now_utc()
    ts, epoch = common.to_ts_utc(moment), common.to_epoch(moment)
    rows: list[tuple] = []
    jupiter_cfg = jupiter_cfg or {"enabled": False}
    onchain_quote, onchain_error = fetch_jupiter_quote(jupiter_cfg, timeout)
    attach_pairs = jupiter_cfg.get("attach_pairs") or set()

    for name, pairs in watchlists.items():
        try:
            venue = venues.get(name)
        except venues.VenueError as exc:
            logging.error("%s", exc)
            continue

        quotes, error = fetch_with_retries(venue, pairs, timeout, max_retries)
        for pair in pairs:
            onchain = onchain_quote if pair in attach_pairs else None
            onchain_note = onchain_error if pair in attach_pairs else None
            rows.append(
                row_from_quote(ts, epoch, pair, name, quotes.get(pair), error, onchain, onchain_note)
            )

    insert_rows(conn, rows)

    cex_mids = {
        row[2]: row[6]
        for row in rows
        if row[2] in attach_pairs and row[10] is None and row[6] is not None
    }
    pool_rows = record_pool_samples(conn, ts, epoch, cex_mids, jupiter_cfg, timeout)

    multi = len(watchlists) > 1
    for row in rows:
        pair, source, bid, ask, mid, spread_bps, note = row[2], row[3], row[4], row[5], row[6], row[7], row[10]
        onchain_ref, basis_bps, onchain_note = row[11], row[13], row[14]
        label = f"{source} {pair}" if multi else pair
        if note:
            logging.warning("%s %s", label, note)
        elif onchain_ref is not None and basis_bps is not None:
            logging.info(
                "%s bid=%s ask=%s mid=%.8f spread_bps=%.2f onchain_ref=%.8f basis_bps=%+.2f",
                label, bid, ask, mid, spread_bps, onchain_ref, basis_bps,
            )
        else:
            logging.info("%s bid=%s ask=%s mid=%.8f spread_bps=%.2f", label, bid, ask, mid, spread_bps)
        if onchain_note and pair in attach_pairs:
            logging.warning("%s onchain %s", label, onchain_note)

    pools_by_pair: dict[str, list[tuple]] = {}
    for row in pool_rows:
        pools_by_pair.setdefault(row[2], []).append(row)
    for pair, samples in pools_by_pair.items():
        label = pair if not multi else f"binance {pair}"
        priced = [sample for sample in samples if sample[4] is not None and sample[10] is None]
        if not priced:
            continue
        parts = []
        for sample in priced:
            dex, ref, basis = sample[3], sample[4], sample[5]
            parts.append(f"{dex}={ref:.4f}({basis:+.1f}bps)" if basis is not None else f"{dex}={ref:.4f}")
        logging.info("%s pools: %s", label, "  ".join(parts))


def prune(conn: sqlite3.Connection, retention_days: int) -> int:
    """Drop ticks older than the retention window. 0 keeps everything."""
    if retention_days <= 0:
        return 0
    cutoff = common.to_epoch(common.now_utc() - timedelta(days=retention_days))
    tick_cursor = conn.execute("DELETE FROM ticks WHERE ts_epoch > 0 AND ts_epoch < ?", (cutoff,))
    pool_cursor = conn.execute("DELETE FROM pool_samples WHERE ts_epoch > 0 AND ts_epoch < ?", (cutoff,))
    conn.commit()
    removed = tick_cursor.rowcount + pool_cursor.rowcount
    if removed > 0:
        logging.info(
            "pruned %d ticks and %d pool samples older than %d days",
            tick_cursor.rowcount,
            pool_cursor.rowcount,
            retention_days,
        )
    return removed


def next_tick_at(now: float, interval_sec: int) -> float:
    """Wall-clock aligned so buckets stay tidy and the loop never drifts."""
    return (int(now // interval_sec) + 1) * interval_sec


def probe(pairs: dict[str, str], timeout: int) -> int:
    """Ask every known venue for these pairs once, and report what came back.

    This is the check that the parsers still match the live APIs - nothing
    offline can prove that. Writes nothing to the database.
    """
    print(f"{'venue':<10} {'pair':<10} {'result'}")
    failures = 0
    for name in sorted(venues.VENUES):
        venue = venues.get(name)
        for pair, symbol in pairs.items():
            try:
                quote = fetch_venue(venue, {pair: symbol}, timeout).get(pair)
                if quote is None:
                    raise venues.VenueError("no quote in the response")
                mid, spread = compute_mid_spread(quote.bid, quote.ask)
                print(
                    f"{name:<10} {venue.to_symbol(symbol):<10} ok   bid={quote.bid:<12g} "
                    f"ask={quote.ask:<12g} mid={mid:<12g} spread={spread:.2f}bps"
                )
            except Exception as exc:  # noqa: BLE001 - report, never raise
                failures += 1
                detail = getattr(exc, "reason", None) or exc
                print(
                    f"{name:<10} {venue.to_symbol(symbol):<10} FAILED  "
                    f"{_describe(exc)}: {str(detail)[:80]}"
                )
    if failures:
        print(f"\n{failures} venue/pair combinations failed - only add the ones that say ok.")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Record top of book into SQLite.")
    parser.add_argument(
        "--probe",
        nargs="*",
        metavar="PAIR",
        help="ask every venue for these pairs once, print the result, and exit. "
             "PAIR:VENUE_PAIR tries a different name, e.g. SOLUSDC:SOLUSD "
             "(default: SOLUSDC)",
    )
    args = parser.parse_args()

    env = common.load_config()
    common.setup_logging(env.get("LOG_LEVEL", "INFO"))

    if args.probe is not None:
        common.setup_logging(env.get("LOG_LEVEL", "WARNING"))
        timeout = common.env_int(env, "HTTP_TIMEOUT_SEC", 15, minimum=1)
        raise SystemExit(probe(common.parse_watchlist(",".join(args.probe) or "SOLUSDC"), timeout))

    watchlists = common.watchlists_from(env)
    jupiter_cfg = jupiter.config_from(env)
    interval_sec = common.env_int(env, "INTERVAL_SEC", 60, minimum=1)
    timeout = common.env_int(env, "HTTP_TIMEOUT_SEC", 15, minimum=1)
    max_retries = common.env_int(env, "MAX_RETRIES", 2, minimum=0)
    retention_days = common.env_int(env, "RETENTION_DAYS", 0, minimum=0)
    db_path = common.db_path_from(env)
    if "BINANCE_URL" in env and env["BINANCE_URL"].strip():
        venues.VENUES["binance"] = replace(venues.VENUES["binance"], base_url=env["BINANCE_URL"].strip())

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    common.ensure_permissions(db_path)
    conn = common.connect(db_path)

    for name, pairs in watchlists.items():
        listed = ", ".join(
            pair if pair == symbol else f"{pair} (as {symbol})" for pair, symbol in pairs.items()
        )
        logging.info("recording %s on %s", listed, name)
    logging.info(
        "every %ds into %s (retention=%s)",
        interval_sec,
        db_path,
        f"{retention_days}d" if retention_days else "forever",
    )
    if jupiter_cfg["enabled"]:
        listed = ", ".join(sorted(jupiter_cfg["attach_pairs"]))  # type: ignore[arg-type]
        logging.info(
            "jupiter on-chain ref for %s via %s -> %s",
            listed or "(none)",
            jupiter_cfg["input_mint"],
            jupiter_cfg["output_mint"],
        )
        dexes = jupiter_cfg.get("dexes") or []
        if dexes:
            logging.info("jupiter per-pool samples: %s", ", ".join(dexes))  # type: ignore[arg-type]

    last_prune = 0.0
    try:
        while not _shutdown.is_set():
            run_tick(conn, watchlists, timeout, max_retries, jupiter_cfg)

            now = time.time()
            if retention_days and now - last_prune > 3600:
                prune(conn, retention_days)
                last_prune = now

            delay = max(0.0, next_tick_at(time.time(), interval_sec) - time.time())
            _shutdown.wait(delay)
    finally:
        conn.close()
        if _shutdown_signal is not None:
            logging.info("received %s", signal.Signals(_shutdown_signal).name)
        logging.info("recorder stopped")


if __name__ == "__main__":
    main()
