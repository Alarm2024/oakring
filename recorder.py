#!/usr/bin/env python3
"""Binance bookTicker SQLite recorder (stdlib only)."""

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path("/home/ubuntu/.config/oakring")
ENV_PATH = CONFIG_DIR / ".env"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = CONFIG_DIR / "ring.db"
DEFAULT_BINANCE_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
SOURCE = "binance"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def ensure_permissions(db_path: Path) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if (CONFIG_DIR.stat().st_mode & 0o777) != 0o700:
        CONFIG_DIR.chmod(0o700)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and (db_path.stat().st_mode & 0o777) != 0o600:
        db_path.chmod(0o600)


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    db_path.chmod(0o600)
    return conn


def ts_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_book_ticker(base_url: str, symbol: str) -> dict:
    url = f"{base_url}?symbol={symbol}"
    request = urllib.request.Request(url, headers={"User-Agent": "oakring/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def compute_mid_spread(bid: float, ask: float) -> tuple[float, float]:
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0 if mid > 0 else 0.0
    return mid, spread_bps


def insert_tick(
    conn: sqlite3.Connection,
    ts: str,
    pair: str,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    spread_bps: float | None,
    note: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO ticks (ts_utc, pair, source, bid, ask, mid, spread_bps, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, pair, SOURCE, bid, ask, mid, spread_bps, note),
    )
    conn.commit()


def process_pair(conn: sqlite3.Connection, base_url: str, pair: str) -> None:
    ts = ts_utc_now()
    try:
        data = fetch_book_ticker(base_url, pair)
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
        mid, spread_bps = compute_mid_spread(bid, ask)
        insert_tick(conn, ts, pair, bid, ask, mid, spread_bps, None)
        print(f"{ts} {pair} bid={bid} ask={ask} mid={mid:.8f} spread_bps={spread_bps:.2f}")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        note = f"error:{type(exc).__name__}"
        insert_tick(conn, ts, pair, None, None, None, None, note)
        print(f"{ts} {pair} {note}")
    except Exception as exc:
        note = f"error:{type(exc).__name__}"
        insert_tick(conn, ts, pair, None, None, None, None, note)
        print(f"{ts} {pair} {note}")


def main() -> None:
    env = load_env(ENV_PATH)
    watchlist = [p.strip() for p in env.get("WATCHLIST", "SOLUSDT").split(",") if p.strip()]
    interval_sec = int(env.get("INTERVAL_SEC", "60"))
    db_path = Path(env.get("DB_PATH", str(DEFAULT_DB_PATH)))
    binance_url = env.get("BINANCE_URL", DEFAULT_BINANCE_URL)

    ensure_permissions(db_path)
    conn = init_db(db_path)

    while True:
        for pair in watchlist:
            process_pair(conn, binance_url, pair)
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
