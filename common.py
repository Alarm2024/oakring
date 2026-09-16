#!/usr/bin/env python3
"""Shared configuration, database and time helpers for oakring.

Stdlib only. Imported by both the recorder (writer) and the analyzer (reader).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = REPO_DIR / "schema.sql"

DEFAULT_CONFIG_DIR = Path("/home/ubuntu/.config/oakring")
DEFAULT_BINANCE_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
SOURCE = "binance"

# Columns an older database may be missing, with the declaration used to add
# them in place. `id` is omitted: a primary key cannot be added by ALTER TABLE.
EXPECTED_COLUMNS: dict[str, str] = {
    "ts_utc": "TEXT",
    "ts_epoch": "INTEGER NOT NULL DEFAULT 0",
    "pair": "TEXT",
    "source": "TEXT",
    "bid": "REAL",
    "ask": "REAL",
    "mid": "REAL",
    "spread_bps": "REAL",
    "bid_qty": "REAL",
    "ask_qty": "REAL",
    "note": "TEXT",
    "onchain_ref": "REAL",
    "onchain_impact_bps": "REAL",
    "basis_bps": "REAL",
    "onchain_note": "TEXT",
}


def config_dir() -> Path:
    """Directory holding `.env` and (by default) the database.

    Overridable with OAKRING_CONFIG_DIR so the tools can run outside the
    deployment host without touching /home/ubuntu.
    """
    return Path(os.environ.get("OAKRING_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key.strip()] = value
    return env


def load_config() -> dict[str, str]:
    """`.env` values, with real process environment taking precedence."""
    env = load_env_file(config_dir() / ".env")
    for key in list(EXPECTED_ENV_KEYS):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in os.environ.items():
        if key.startswith("WATCHLIST_") or key.startswith("ALERT_"):
            env[key] = value
    return env


EXPECTED_ENV_KEYS = (
    "WATCHLIST",
    "INTERVAL_SEC",
    "DB_PATH",
    "BINANCE_URL",
    "HTTP_TIMEOUT_SEC",
    "MAX_RETRIES",
    "RETENTION_DAYS",
    "LOG_LEVEL",
    "JUPITER_ENABLED",
    "JUPITER_ATTACH_PAIRS",
    "JUPITER_QUOTE_URL",
    "JUPITER_INPUT_MINT",
    "JUPITER_OUTPUT_MINT",
    "JUPITER_AMOUNT_LAMPORTS",
    "JUPITER_SLIPPAGE_BPS",
    "JUPITER_INPUT_DECIMALS",
    "JUPITER_OUTPUT_DECIMALS",
    "JUPITER_USDT_MINT",
    "JUPITER_USDT_DECIMALS",
    "JUPITER_DEXES",
    "JUPITER_ONLY_DIRECT_ROUTES",
    "JUPITER_API_KEY",
)


def env_int(env: dict[str, str], key: str, default: int, minimum: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        logging.warning("%s=%r is not a number, using %d", key, raw, default)
        return default
    if minimum is not None and value < minimum:
        logging.warning("%s=%d is below the minimum %d, using %d", key, value, minimum, minimum)
        return minimum
    return value


def db_path_from(env: dict[str, str]) -> Path:
    raw = env.get("DB_PATH", "").strip()
    return Path(raw) if raw else config_dir() / "ring.db"


def watchlist_from(env: dict[str, str]) -> list[str]:
    raw = env.get("WATCHLIST", "SOLUSDT")
    pairs: list[str] = []
    for item in raw.split(","):
        pair = item.strip().upper()
        if pair and pair not in pairs:
            pairs.append(pair)
    return pairs or ["SOLUSDT"]


def parse_watchlist(raw: str) -> dict[str, str]:
    """Pairs to record, mapped to what this venue calls them.

    Plain `SOLUSDC` means both. `SOLUSDC:SOLUSD` records under SOLUSDC but asks
    the venue for SOLUSD - which is how the same market gets compared across
    exchanges that name it differently. Coinbase, for one, has no SOL-USDC at
    all: USD and USDC are interchangeable there, so SOL-USD is that book.
    """
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip().upper()
        if not entry:
            continue
        canonical, _, venue_pair = entry.partition(":")
        canonical = canonical.strip()
        if canonical:
            pairs[canonical] = venue_pair.strip() or canonical
    return pairs


def watchlists_from(env: dict[str, str]) -> dict[str, dict[str, str]]:
    """Which pairs to record on which venue, and what each venue calls them.

    WATCHLIST is binance, unchanged. Any other venue is turned on by giving it
    its own list, e.g. WATCHLIST_COINBASE=SOLUSDC:SOLUSD - a venue with no list
    is simply not polled.
    """
    watchlists: dict[str, dict[str, str]] = {
        "binance": {pair: pair for pair in watchlist_from(env)}
    }
    for key, value in env.items():
        if not key.startswith("WATCHLIST_"):
            continue
        name = key[len("WATCHLIST_") :].strip().lower()
        pairs = parse_watchlist(value)
        if name and pairs:
            watchlists[name] = pairs
    return watchlists


def ensure_permissions(db_path: Path) -> None:
    """Keep the config dir at 700 and the database at 600."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory.stat().st_mode & 0o777) != 0o700:
        directory.chmod(0o700)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and (db_path.stat().st_mode & 0o777) != 0o600:
        db_path.chmod(0o600)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add columns a pre-existing database is missing. Returns what was added."""
    columns = _table_columns(conn, "ticks")
    if not columns:
        return []
    added: list[str] = []
    for name, decl in EXPECTED_COLUMNS.items():
        if name in columns:
            continue
        conn.execute(f"ALTER TABLE ticks ADD COLUMN {name} {decl}")
        added.append(name)
    if "ts_epoch" in added:
        # Backfill from the text timestamp so old rows stay analysable.
        conn.execute(
            "UPDATE ticks SET ts_epoch = CAST(strftime('%s', ts_utc) AS INTEGER) "
            "WHERE ts_epoch = 0 AND ts_utc IS NOT NULL"
        )
    if added:
        conn.commit()
        logging.info("migrated ticks table, added columns: %s", ", ".join(added))
    return added


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database, applying schema, migrations and pragmas."""
    if read_only:
        if not db_path.exists():
            raise FileNotFoundError(f"database not found: {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    fresh = not db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # WAL lets analyze.py read while the recorder keeps writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if not fresh:
        # Must run before the schema script: its indexes reference columns that
        # a v1 database does not have yet.
        migrate(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    db_path.chmod(0o600)
    return conn


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ts_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def from_epoch(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def parse_duration(text: str) -> int:
    """Parse '90', '15m', '4h', '7d', '2w' into seconds."""
    raw = text.strip().lower()
    if not raw:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if raw[-1] in units:
        number, factor = raw[:-1], units[raw[-1]]
    else:
        number, factor = raw, 1
    try:
        value = float(number)
    except ValueError as exc:
        raise ValueError(f"bad duration: {text!r}") from exc
    seconds = int(value * factor)
    if seconds <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return seconds


def format_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def setup_logging(level_name: str = "INFO") -> None:
    level = getattr(logging, level_name.strip().upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
