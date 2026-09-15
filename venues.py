#!/usr/bin/env python3
"""Top-of-book from several exchanges, normalised to one shape.

Each venue knows three things: how to spell a pair, where to ask for it, and
how to read the answer. Everything else in oakring works in the canonical
Binance-style spelling (SOLUSDC) and the `source` column records which venue a
row came from.

All endpoints are public: no keys, no accounts, read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Longest first, so SOLUSDC splits at USDC and not at USD.
QUOTES = ("FDUSD", "USDT", "USDC", "TUSD", "DAI", "USD", "EUR", "BTC", "ETH", "BNB")


class VenueError(Exception):
    """The venue answered, but not with a book we can read."""


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


def split_pair(pair: str) -> tuple[str, str]:
    """SOLUSDC -> (SOL, USDC). Raises for anything we cannot split."""
    pair = pair.upper()
    for quote in QUOTES:
        if pair.endswith(quote) and len(pair) > len(quote):
            return pair[: -len(quote)], quote
    raise VenueError(f"cannot tell base from quote in {pair!r}")


def _number(value: object, field: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise VenueError(f"{field} is not a number: {value!r}") from None
    return number


# --------------------------------------------------------------------------- binance


def binance_symbol(pair: str) -> str:
    return pair.upper()


def binance_url(base_url: str, symbols: list[str]) -> str:
    import json as _json
    import urllib.parse

    if len(symbols) == 1:
        return f"{base_url}?{urllib.parse.urlencode({'symbol': symbols[0]})}"
    query = urllib.parse.urlencode({"symbols": _json.dumps(symbols, separators=(",", ":"))})
    return f"{base_url}?{query}"


def binance_parse(payload: object, _symbol: str) -> dict[str, Quote]:
    rows = payload if isinstance(payload, list) else [payload]
    quotes: dict[str, Quote] = {}
    for row in rows:
        if not isinstance(row, dict) or "symbol" not in row:
            continue
        quotes[str(row["symbol"]).upper()] = Quote(
            _number(row.get("bidPrice"), "bidPrice"),
            _number(row.get("askPrice"), "askPrice"),
            _number(row.get("bidQty", 0) or 0, "bidQty"),
            _number(row.get("askQty", 0) or 0, "askQty"),
        )
    if not quotes:
        raise VenueError("no symbol in the response")
    return quotes


# --------------------------------------------------------------------------- coinbase


def coinbase_symbol(pair: str) -> str:
    base, quote = split_pair(pair)
    return f"{base}-{quote}"


def coinbase_url(base_url: str, symbols: list[str]) -> str:
    # level=1 gives the best bid and ask with their sizes; the plain ticker
    # endpoint omits the sizes.
    return f"{base_url}/products/{symbols[0]}/book?level=1"


def coinbase_parse(payload: object, symbol: str) -> dict[str, Quote]:
    if not isinstance(payload, dict):
        raise VenueError("expected an object")
    bids, asks = payload.get("bids"), payload.get("asks")
    if not bids or not asks:
        raise VenueError(payload.get("message") or "empty book")
    return {
        symbol: Quote(
            _number(bids[0][0], "bid"),
            _number(asks[0][0], "ask"),
            _number(bids[0][1], "bid size"),
            _number(asks[0][1], "ask size"),
        )
    }


# --------------------------------------------------------------------------- kraken


def kraken_symbol(pair: str) -> str:
    return pair.upper()


def kraken_url(base_url: str, symbols: list[str]) -> str:
    return f"{base_url}/0/public/Ticker?pair={symbols[0]}"


def kraken_parse(payload: object, symbol: str) -> dict[str, Quote]:
    if not isinstance(payload, dict):
        raise VenueError("expected an object")
    if payload.get("error"):
        raise VenueError("; ".join(str(item) for item in payload["error"]))
    result = payload.get("result") or {}
    if not result:
        raise VenueError("empty result")
    # Kraken answers under its own name for the pair (XBT for BTC, and so on),
    # so read the single entry rather than looking our spelling back up.
    entry = next(iter(result.values()))
    ask, bid = entry.get("a"), entry.get("b")
    if not ask or not bid:
        raise VenueError("no top of book in the response")
    return {
        symbol: Quote(
            _number(bid[0], "bid"),
            _number(ask[0], "ask"),
            _number(bid[2] if len(bid) > 2 else 0, "bid volume"),
            _number(ask[2] if len(ask) > 2 else 0, "ask volume"),
        )
    }


# --------------------------------------------------------------------------- okx


def okx_symbol(pair: str) -> str:
    base, quote = split_pair(pair)
    return f"{base}-{quote}"


def okx_url(base_url: str, symbols: list[str]) -> str:
    return f"{base_url}/api/v5/market/ticker?instId={symbols[0]}"


def okx_parse(payload: object, symbol: str) -> dict[str, Quote]:
    if not isinstance(payload, dict):
        raise VenueError("expected an object")
    if str(payload.get("code", "0")) not in ("0", ""):
        raise VenueError(payload.get("msg") or f"code {payload.get('code')}")
    data = payload.get("data") or []
    if not data:
        raise VenueError("empty data")
    row = data[0]
    return {
        symbol: Quote(
            _number(row.get("bidPx"), "bidPx"),
            _number(row.get("askPx"), "askPx"),
            _number(row.get("bidSz", 0) or 0, "bidSz"),
            _number(row.get("askSz", 0) or 0, "askSz"),
        )
    }


# --------------------------------------------------------------------------- bybit


def bybit_symbol(pair: str) -> str:
    return pair.upper()


def bybit_url(base_url: str, symbols: list[str]) -> str:
    return f"{base_url}/v5/market/tickers?category=spot&symbol={symbols[0]}"


def bybit_parse(payload: object, symbol: str) -> dict[str, Quote]:
    if not isinstance(payload, dict):
        raise VenueError("expected an object")
    if payload.get("retCode") not in (0, "0", None):
        raise VenueError(payload.get("retMsg") or f"retCode {payload.get('retCode')}")
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        raise VenueError("empty list")
    row = rows[0]
    return {
        symbol: Quote(
            _number(row.get("bid1Price"), "bid1Price"),
            _number(row.get("ask1Price"), "ask1Price"),
            _number(row.get("bid1Size", 0) or 0, "bid1Size"),
            _number(row.get("ask1Size", 0) or 0, "ask1Size"),
        )
    }


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Venue:
    name: str
    base_url: str
    to_symbol: Callable[[str], str]
    build_url: Callable[[str, list[str]], str]
    parse: Callable[[object, str], dict[str, Quote]]
    batched: bool  # one request for the whole watchlist?


VENUES: dict[str, Venue] = {
    "binance": Venue(
        "binance", "https://api.binance.com/api/v3/ticker/bookTicker",
        binance_symbol, binance_url, binance_parse, True,
    ),
    "coinbase": Venue(
        "coinbase", "https://api.exchange.coinbase.com",
        coinbase_symbol, coinbase_url, coinbase_parse, False,
    ),
    "kraken": Venue(
        "kraken", "https://api.kraken.com",
        kraken_symbol, kraken_url, kraken_parse, False,
    ),
    "okx": Venue(
        "okx", "https://www.okx.com",
        okx_symbol, okx_url, okx_parse, False,
    ),
    "bybit": Venue(
        "bybit", "https://api.bybit.com",
        bybit_symbol, bybit_url, bybit_parse, False,
    ),
}


def get(name: str) -> Venue:
    venue = VENUES.get(name.strip().lower())
    if venue is None:
        raise VenueError(f"unknown venue {name!r} - known: {', '.join(sorted(VENUES))}")
    return venue
