#!/usr/bin/env python3
"""Jupiter public quote API (read-only, stdlib only).

Fetches a swap quote for a configured mint pair and turns it into an implied
mid price (output per one unit of input). No API key is required for the public
endpoint; set JUPITER_API_KEY in the environment when your deployment needs one.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "oakring/3.0"

# Wrapped SOL and USDC on mainnet — override via env when testing other mints.
DEFAULT_INPUT_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_OUTPUT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
DEFAULT_AMOUNT_LAMPORTS = 1_000_000_000  # 1 SOL
DEFAULT_INPUT_DECIMALS = 9
DEFAULT_OUTPUT_DECIMALS = 6
DEFAULT_SLIPPAGE_BPS = 50


class JupiterError(Exception):
    """The Jupiter endpoint answered, but not with a quote we can read."""


@dataclass(frozen=True)
class JupiterQuote:
    """Implied on-chain reference price from a Jupiter route quote."""

    ref_price: float  # output token per one input token (e.g. USDC per SOL)
    impact_bps: float | None
    in_amount: int
    out_amount: int


def _number(value: object, field: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise JupiterError(f"{field} is not a number: {value!r}") from None


def _int_amount(value: object, field: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise JupiterError(f"{field} is not an integer: {value!r}") from None


def ref_price_from_amounts(
    in_amount: int,
    out_amount: int,
    input_decimals: int,
    output_decimals: int,
) -> float:
    """Turn raw mint amounts into output-per-input (e.g. USDC per SOL)."""
    if in_amount <= 0 or out_amount <= 0:
        raise JupiterError("quote amounts must be positive")
    in_units = in_amount / (10**input_decimals)
    out_units = out_amount / (10**output_decimals)
    if in_units <= 0:
        raise JupiterError("input amount decodes to zero")
    return out_units / in_units


def build_quote_url(
    base_url: str,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
) -> str:
    query = urllib.parse.urlencode(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
    )
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{query}"


def parse_quote(
    payload: object,
    input_decimals: int,
    output_decimals: int,
) -> JupiterQuote:
    if not isinstance(payload, dict):
        raise JupiterError("response is not a JSON object")
    in_amount = _int_amount(payload.get("inAmount"), "inAmount")
    out_amount = _int_amount(payload.get("outAmount"), "outAmount")
    impact_raw = payload.get("priceImpactPct")
    impact_bps = None
    if impact_raw is not None:
        # Jupiter reports percent as a string like "0.0123" (= 0.0123%).
        impact_bps = _number(impact_raw, "priceImpactPct") * 100.0
    ref_price = ref_price_from_amounts(in_amount, out_amount, input_decimals, output_decimals)
    return JupiterQuote(ref_price, impact_bps, in_amount, out_amount)


def fetch_quote(
    *,
    base_url: str = DEFAULT_QUOTE_URL,
    input_mint: str = DEFAULT_INPUT_MINT,
    output_mint: str = DEFAULT_OUTPUT_MINT,
    amount: int = DEFAULT_AMOUNT_LAMPORTS,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    input_decimals: int = DEFAULT_INPUT_DECIMALS,
    output_decimals: int = DEFAULT_OUTPUT_DECIMALS,
    timeout: int = 15,
    api_key: str | None = None,
    http_get: object | None = None,
) -> JupiterQuote:
    """Fetch one Jupiter quote and return the implied reference price."""
    url = build_quote_url(base_url, input_mint, output_mint, amount, slippage_bps)
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key
    getter = http_get or _http_get_json
    payload = getter(url, timeout, headers)
    return parse_quote(payload, input_decimals, output_decimals)


def describe_error(exc: Exception | None) -> str:
    if exc is None:
        return "error:Unknown"
    if isinstance(exc, urllib.error.HTTPError):
        return f"error:HTTPError:{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "error:URLError"
    if isinstance(exc, JupiterError):
        return "error:JupiterError"
    return f"error:{type(exc).__name__}"


def _http_get_json(url: str, timeout: int, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def config_from(env: dict[str, str]) -> dict[str, object]:
    """Build Jupiter settings from oakring env/config."""
    enabled_raw = env.get("JUPITER_ENABLED", "").strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    pairs_raw = env.get("JUPITER_ATTACH_PAIRS", "SOLUSDT,SOLUSDC").strip()
    attach_pairs = {
        item.strip().upper()
        for item in pairs_raw.split(",")
        if item.strip()
    }
    api_key = env.get("JUPITER_API_KEY", "").strip() or None
    return {
        "enabled": enabled,
        "attach_pairs": attach_pairs,
        "quote_url": env.get("JUPITER_QUOTE_URL", DEFAULT_QUOTE_URL).strip() or DEFAULT_QUOTE_URL,
        "input_mint": env.get("JUPITER_INPUT_MINT", DEFAULT_INPUT_MINT).strip() or DEFAULT_INPUT_MINT,
        "output_mint": env.get("JUPITER_OUTPUT_MINT", DEFAULT_OUTPUT_MINT).strip() or DEFAULT_OUTPUT_MINT,
        "amount": _env_int(env, "JUPITER_AMOUNT_LAMPORTS", DEFAULT_AMOUNT_LAMPORTS, minimum=1),
        "slippage_bps": _env_int(env, "JUPITER_SLIPPAGE_BPS", DEFAULT_SLIPPAGE_BPS, minimum=0),
        "input_decimals": _env_int(env, "JUPITER_INPUT_DECIMALS", DEFAULT_INPUT_DECIMALS, minimum=0),
        "output_decimals": _env_int(env, "JUPITER_OUTPUT_DECIMALS", DEFAULT_OUTPUT_DECIMALS, minimum=0),
        "api_key": api_key,
    }


def _env_int(env: dict[str, str], key: str, default: int, minimum: int | None = None) -> int:
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
