#!/usr/bin/env python3
"""Jupiter public quote API (read-only, stdlib only).

Fetches swap quotes for a configured mint pair and turns them into implied mid
prices (output per one unit of input). Supports one aggregated quote plus
parallel per-DEX quotes when JUPITER_DEXES is set. No API key is required for
the public endpoint; set JUPITER_API_KEY in the environment when needed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import venues

USER_AGENT = "oakring/3.0"

# Wrapped SOL and USDC on mainnet — override via env when testing other mints.
DEFAULT_INPUT_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_OUTPUT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
DEFAULT_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
DEFAULT_AMOUNT_LAMPORTS = 1_000_000_000  # 1 SOL
DEFAULT_INPUT_DECIMALS = 9
DEFAULT_OUTPUT_DECIMALS = 6
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_DEXES = ("Raydium", "Orca", "Meteora DLMM")

QUOTE_SUFFIXES = ("USDC", "USDT", "FDUSD")


class JupiterError(Exception):
    """The Jupiter endpoint answered, but not with a quote we can read."""


@dataclass(frozen=True)
class JupiterQuote:
    """Implied on-chain reference price from a Jupiter route quote."""

    ref_price: float  # output token per one input token (e.g. USDC per SOL)
    impact_bps: float | None
    in_amount: int
    out_amount: int
    amm_key: str | None = None
    dex: str | None = None


@dataclass(frozen=True)
class PoolSample:
    """One per-DEX on-chain sample aligned to a CEX pair tick."""

    dex: str
    ref_price: float | None
    impact_bps: float | None
    in_amount: int | None
    out_amount: int | None
    amm_key: str | None
    note: str | None


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


def output_for_pair(pair: str, jupiter_cfg: dict[str, object]) -> tuple[str, int] | None:
    """Map a CEX pair to the Jupiter output mint and its decimals."""
    pair = pair.upper()
    for suffix in QUOTE_SUFFIXES:
        if pair.endswith(suffix):
            if suffix == "USDC":
                mint = str(jupiter_cfg.get("output_mint") or DEFAULT_OUTPUT_MINT)
                decimals = int(jupiter_cfg.get("output_decimals") or DEFAULT_OUTPUT_DECIMALS)
                return mint, decimals
            if suffix == "USDT":
                mint = str(jupiter_cfg.get("usdt_mint") or DEFAULT_USDT_MINT)
                decimals = int(jupiter_cfg.get("usdt_decimals") or DEFAULT_OUTPUT_DECIMALS)
                return mint, decimals
            if suffix == "FDUSD":
                mint = str(jupiter_cfg.get("fdusd_mint") or "")
                if not mint:
                    return None
                decimals = int(jupiter_cfg.get("fdusd_decimals") or DEFAULT_OUTPUT_DECIMALS)
                return mint, decimals
    return None


def parse_dex_list(raw: str) -> list[str]:
    """Comma-separated Jupiter dex labels, preserving spaces (e.g. Meteora DLMM)."""
    dexes: list[str] = []
    for item in raw.split(","):
        dex = item.strip()
        if dex and dex not in dexes:
            dexes.append(dex)
    return dexes


def extract_amm_key(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    route = payload.get("routePlan")
    if not isinstance(route, list) or not route:
        return None
    first = route[0]
    if not isinstance(first, dict):
        return None
    swap = first.get("swapInfo")
    if not isinstance(swap, dict):
        return None
    amm_key = swap.get("ammKey")
    return str(amm_key) if amm_key else None


def build_quote_url(
    base_url: str,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
    *,
    dex: str | None = None,
    only_direct_routes: bool = False,
) -> str:
    params: dict[str, str] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    if dex:
        params["dexes"] = dex
    if only_direct_routes:
        params["onlyDirectRoutes"] = "true"
    query = urllib.parse.urlencode(params)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{query}"


def parse_quote(
    payload: object,
    input_decimals: int,
    output_decimals: int,
    *,
    dex: str | None = None,
) -> JupiterQuote:
    if not isinstance(payload, dict):
        raise JupiterError("response is not a JSON object")
    in_amount = _int_amount(payload.get("inAmount"), "inAmount")
    out_amount = _int_amount(payload.get("outAmount"), "outAmount")
    impact_raw = payload.get("priceImpactPct")
    impact_bps = None
    if impact_raw is not None:
        # Jupiter priceImpactPct is a fraction (e.g. 0.0123 = 1.23%), not a percent.
        impact_bps = _number(impact_raw, "priceImpactPct") * 10000.0
    ref_price = ref_price_from_amounts(in_amount, out_amount, input_decimals, output_decimals)
    return JupiterQuote(
        ref_price,
        impact_bps,
        in_amount,
        out_amount,
        amm_key=extract_amm_key(payload),
        dex=dex,
    )


def fetch_quote_payload(
    *,
    base_url: str = DEFAULT_QUOTE_URL,
    input_mint: str = DEFAULT_INPUT_MINT,
    output_mint: str = DEFAULT_OUTPUT_MINT,
    amount: int = DEFAULT_AMOUNT_LAMPORTS,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    timeout: int = 15,
    api_key: str | None = None,
    dex: str | None = None,
    only_direct_routes: bool = False,
    http_get: object | None = None,
) -> object:
    url = build_quote_url(
        base_url,
        input_mint,
        output_mint,
        amount,
        slippage_bps,
        dex=dex,
        only_direct_routes=only_direct_routes,
    )
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key
    getter = http_get or _http_get_json
    return getter(url, timeout, headers)


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
    dex: str | None = None,
    only_direct_routes: bool = False,
    http_get: object | None = None,
) -> JupiterQuote:
    """Fetch one Jupiter quote and return the implied reference price."""
    payload = fetch_quote_payload(
        base_url=base_url,
        input_mint=input_mint,
        output_mint=output_mint,
        amount=amount,
        slippage_bps=slippage_bps,
        timeout=timeout,
        api_key=api_key,
        dex=dex,
        only_direct_routes=only_direct_routes,
        http_get=http_get,
    )
    return parse_quote(payload, input_decimals, output_decimals, dex=dex)


def fetch_pool_samples_for_pair(
    pair: str,
    jupiter_cfg: dict[str, object],
    timeout: int,
    *,
    http_get: object | None = None,
) -> list[PoolSample]:
    """Parallel per-DEX quotes for one attached CEX pair."""
    dexes = jupiter_cfg.get("dexes") or []
    if not dexes:
        return []

    output = output_for_pair(pair, jupiter_cfg)
    if output is None:
        return []
    output_mint, output_decimals = output

    samples: list[PoolSample] = []
    only_direct = bool(jupiter_cfg.get("only_direct_routes", True))
    for dex in dexes:
        try:
            payload = fetch_quote_payload(
                base_url=str(jupiter_cfg["quote_url"]),
                input_mint=str(jupiter_cfg["input_mint"]),
                output_mint=output_mint,
                amount=int(jupiter_cfg["amount"]),
                slippage_bps=int(jupiter_cfg["slippage_bps"]),
                timeout=timeout,
                api_key=jupiter_cfg.get("api_key"),  # type: ignore[arg-type]
                dex=str(dex),
                only_direct_routes=only_direct,
                http_get=http_get,
            )
            quote = parse_quote(
                payload,
                int(jupiter_cfg["input_decimals"]),
                output_decimals,
                dex=str(dex),
            )
            samples.append(
                PoolSample(
                    dex=str(dex),
                    ref_price=quote.ref_price,
                    impact_bps=quote.impact_bps,
                    in_amount=quote.in_amount,
                    out_amount=quote.out_amount,
                    amm_key=quote.amm_key,
                    note=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - record the miss, keep other pools
            samples.append(
                PoolSample(
                    dex=str(dex),
                    ref_price=None,
                    impact_bps=None,
                    in_amount=None,
                    out_amount=None,
                    amm_key=None,
                    note=describe_error(exc),
                )
            )
    return samples


def pools_from_route_plan(
    payload: object,
    input_decimals: int,
    output_decimals: int,
) -> list[PoolSample]:
    """Extract per-hop pool prices from an aggregated quote's routePlan."""
    if not isinstance(payload, dict):
        return []
    route = payload.get("routePlan")
    if not isinstance(route, list):
        return []

    samples: list[PoolSample] = []
    seen: set[str] = set()
    for hop in route:
        if not isinstance(hop, dict):
            continue
        swap = hop.get("swapInfo")
        if not isinstance(swap, dict):
            continue
        amm_key = swap.get("ammKey")
        label = swap.get("label") or swap.get("feeMint") or "unknown"
        dex = str(label)
        if dex in seen:
            continue
        seen.add(dex)
        try:
            in_amount = _int_amount(swap.get("inAmount"), "inAmount")
            out_amount = _int_amount(swap.get("outAmount"), "outAmount")
            ref_price = ref_price_from_amounts(in_amount, out_amount, input_decimals, output_decimals)
        except JupiterError:
            continue
        samples.append(
            PoolSample(
                dex=dex,
                ref_price=ref_price,
                impact_bps=None,
                in_amount=in_amount,
                out_amount=out_amount,
                amm_key=str(amm_key) if amm_key else None,
                note=None,
            )
        )
    return samples


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
    dexes_raw = env.get("JUPITER_DEXES", "").strip()
    dexes = parse_dex_list(dexes_raw) if dexes_raw else []
    only_direct_raw = env.get("JUPITER_ONLY_DIRECT_ROUTES", "1").strip().lower()
    only_direct_routes = only_direct_raw not in {"0", "false", "no", "off"}
    api_key = env.get("JUPITER_API_KEY", "").strip() or None
    return {
        "enabled": enabled,
        "attach_pairs": attach_pairs,
        "dexes": dexes,
        "only_direct_routes": only_direct_routes,
        "quote_url": env.get("JUPITER_QUOTE_URL", DEFAULT_QUOTE_URL).strip() or DEFAULT_QUOTE_URL,
        "input_mint": env.get("JUPITER_INPUT_MINT", DEFAULT_INPUT_MINT).strip() or DEFAULT_INPUT_MINT,
        "output_mint": env.get("JUPITER_OUTPUT_MINT", DEFAULT_OUTPUT_MINT).strip() or DEFAULT_OUTPUT_MINT,
        "usdt_mint": env.get("JUPITER_USDT_MINT", DEFAULT_USDT_MINT).strip() or DEFAULT_USDT_MINT,
        "amount": _env_int(env, "JUPITER_AMOUNT_LAMPORTS", DEFAULT_AMOUNT_LAMPORTS, minimum=1),
        "slippage_bps": _env_int(env, "JUPITER_SLIPPAGE_BPS", DEFAULT_SLIPPAGE_BPS, minimum=0),
        "input_decimals": _env_int(env, "JUPITER_INPUT_DECIMALS", DEFAULT_INPUT_DECIMALS, minimum=0),
        "output_decimals": _env_int(env, "JUPITER_OUTPUT_DECIMALS", DEFAULT_OUTPUT_DECIMALS, minimum=0),
        "usdt_decimals": _env_int(env, "JUPITER_USDT_DECIMALS", DEFAULT_OUTPUT_DECIMALS, minimum=0),
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


def pair_quote_suffix(pair: str) -> str | None:
    """Return the quote currency suffix for a pair, if recognised."""
    pair = pair.upper()
    for suffix in QUOTE_SUFFIXES:
        if pair.endswith(suffix):
            return suffix
    try:
        return venues.split_pair(pair)[1]
    except venues.VenueError:
        return None
