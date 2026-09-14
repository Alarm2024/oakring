#!/usr/bin/env python3
"""Binance bookTicker SQLite recorder (stdlib only).

Polls the configured watchlist on a wall-clock aligned interval and stores one
row per pair per tick: bid, ask, mid, spread and top-of-book sizes. Rows that
failed to fetch are still written, with `note` set, so gaps in the record are
visible to the analyzer instead of silently disappearing.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

import common

USER_AGENT = "oakring/2.0"
_shutdown = threading.Event()


def _handle_signal(signum: int, _frame: object) -> None:
    logging.info("received %s, shutting down after this tick", signal.Signals(signum).name)
    _shutdown.set()


def http_get_json(url: str, timeout: int) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def fetch_batch(base_url: str, pairs: list[str], timeout: int) -> dict[str, dict]:
    """One request for the whole watchlist.

    Binance rejects the entire batch if a single symbol is unknown, so callers
    fall back to per-pair requests when this raises.
    """
    if len(pairs) == 1:
        query = urllib.parse.urlencode({"symbol": pairs[0]})
    else:
        query = urllib.parse.urlencode({"symbols": json.dumps(pairs, separators=(",", ":"))})
    payload = http_get_json(f"{base_url}?{query}", timeout)
    rows = payload if isinstance(payload, list) else [payload]
    return {str(row["symbol"]).upper(): row for row in rows if isinstance(row, dict) and "symbol" in row}


def fetch_with_retries(
    base_url: str, pairs: list[str], timeout: int, max_retries: int
) -> tuple[dict[str, dict], str | None]:
    """Batch fetch, retried with exponential backoff, then per-pair fallback."""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_batch(base_url, pairs, timeout), None
        except Exception as exc:  # noqa: BLE001 - any failure is worth retrying
            last_error = exc
            if attempt < max_retries and not _shutdown.is_set():
                delay = min(2.0**attempt, 30.0)
                logging.warning("batch fetch failed (%s), retrying in %.0fs", _describe(exc), delay)
                _shutdown.wait(delay)

    if len(pairs) == 1 or _shutdown.is_set():
        return {}, _describe(last_error)

    logging.warning("batch fetch failed (%s), falling back to per-pair", _describe(last_error))
    results: dict[str, dict] = {}
    for pair in pairs:
        if _shutdown.is_set():
            break
        try:
            results.update(fetch_batch(base_url, [pair], timeout))
        except Exception as exc:  # noqa: BLE001
            logging.warning("%s fetch failed: %s", pair, _describe(exc))
    return results, None if results else _describe(last_error)


def _describe(exc: Exception | None) -> str:
    if exc is None:
        return "error:Unknown"
    if isinstance(exc, urllib.error.HTTPError):
        return f"error:HTTPError:{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "error:URLError"
    return f"error:{type(exc).__name__}"


def compute_mid_spread(bid: float, ask: float) -> tuple[float, float]:
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0 if mid > 0 else 0.0
    return mid, spread_bps


def row_from_payload(ts: str, epoch: int, pair: str, payload: dict | None, fallback_note: str | None) -> tuple:
    """Build one `ticks` row, valid or errored."""
    if payload is None:
        return (ts, epoch, pair, common.SOURCE, None, None, None, None, None, None, fallback_note or "error:Missing")
    try:
        bid = float(payload["bidPrice"])
        ask = float(payload["askPrice"])
        bid_qty = float(payload.get("bidQty", 0.0) or 0.0)
        ask_qty = float(payload.get("askQty", 0.0) or 0.0)
    except (KeyError, TypeError, ValueError):
        return (ts, epoch, pair, common.SOURCE, None, None, None, None, None, None, "error:BadPayload")
    if bid <= 0 or ask <= 0 or ask < bid:
        return (ts, epoch, pair, common.SOURCE, bid, ask, None, None, bid_qty, ask_qty, "error:CrossedBook")
    mid, spread_bps = compute_mid_spread(bid, ask)
    return (ts, epoch, pair, common.SOURCE, bid, ask, mid, spread_bps, bid_qty, ask_qty, None)


def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO ticks
            (ts_utc, ts_epoch, pair, source, bid, ask, mid, spread_bps, bid_qty, ask_qty, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def run_tick(conn: sqlite3.Connection, base_url: str, pairs: list[str], timeout: int, max_retries: int) -> None:
    moment = common.now_utc()
    ts, epoch = common.to_ts_utc(moment), common.to_epoch(moment)
    payloads, error = fetch_with_retries(base_url, pairs, timeout, max_retries)

    rows = [row_from_payload(ts, epoch, pair, payloads.get(pair), error) for pair in pairs]
    insert_rows(conn, rows)

    for row in rows:
        pair, bid, ask, mid, spread_bps, note = row[2], row[4], row[5], row[6], row[7], row[10]
        if note:
            logging.warning("%s %s", pair, note)
        else:
            logging.info("%s bid=%s ask=%s mid=%.8f spread_bps=%.2f", pair, bid, ask, mid, spread_bps)


def prune(conn: sqlite3.Connection, retention_days: int) -> int:
    """Drop ticks older than the retention window. 0 keeps everything."""
    if retention_days <= 0:
        return 0
    cutoff = common.to_epoch(common.now_utc() - timedelta(days=retention_days))
    cursor = conn.execute("DELETE FROM ticks WHERE ts_epoch > 0 AND ts_epoch < ?", (cutoff,))
    conn.commit()
    if cursor.rowcount > 0:
        logging.info("pruned %d ticks older than %d days", cursor.rowcount, retention_days)
    return cursor.rowcount


def next_tick_at(now: float, interval_sec: int) -> float:
    """Wall-clock aligned so buckets stay tidy and the loop never drifts."""
    return (int(now // interval_sec) + 1) * interval_sec


def main() -> None:
    env = common.load_config()
    common.setup_logging(env.get("LOG_LEVEL", "INFO"))

    pairs = common.watchlist_from(env)
    interval_sec = common.env_int(env, "INTERVAL_SEC", 60, minimum=1)
    timeout = common.env_int(env, "HTTP_TIMEOUT_SEC", 15, minimum=1)
    max_retries = common.env_int(env, "MAX_RETRIES", 2, minimum=0)
    retention_days = common.env_int(env, "RETENTION_DAYS", 0, minimum=0)
    db_path = common.db_path_from(env)
    base_url = env.get("BINANCE_URL", common.DEFAULT_BINANCE_URL)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    common.ensure_permissions(db_path)
    conn = common.connect(db_path)

    logging.info(
        "recording %s every %ds into %s (retention=%s)",
        ",".join(pairs),
        interval_sec,
        db_path,
        f"{retention_days}d" if retention_days else "forever",
    )

    last_prune = 0.0
    try:
        while not _shutdown.is_set():
            run_tick(conn, base_url, pairs, timeout, max_retries)

            now = time.time()
            if retention_days and now - last_prune > 3600:
                prune(conn, retention_days)
                last_prune = now

            delay = max(0.0, next_tick_at(time.time(), interval_sec) - time.time())
            _shutdown.wait(delay)
    finally:
        conn.close()
        logging.info("recorder stopped")


if __name__ == "__main__":
    main()
