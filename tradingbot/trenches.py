"""Trenches feed: new pump.fun / PumpSwap tokens via DexScreener.

The pump.fun frontend API is behind a Cloudflare challenge and cannot
be read server-side, so the feed reads DexScreener's free public API
(no key needed) instead:
  * token-profiles/latest/v1 — the newest token profiles, filtered to
    chainId "solana" (these are the fresh trenches listings);
  * latest/dex/tokens/{mint,...} — pairs for those mints, filtered to
    dexId "pumpfun" and "pumpswap", with priceUsd and priceChange
    (m5 / h1 / h6 / h24) per pair.

Every failure returns an error dict, never an exception and never
fake data: the rules engine treats a feed error as "no trades this
scan". A pair with no price or no change data is skipped, not guessed.

There is NO market-cap filter anywhere in this feed or in the rules
engine: a trending coin qualifies at any market cap, tiny or huge.
`market_cap_usd` is recorded on each entry for display only — the
buy decision reads the 5-minute gain alone.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.request

DEXSCREENER_BASE = "https://api.dexscreener.com"
PROFILES_URL = DEXSCREENER_BASE + "/token-profiles/latest/v1"
TOKENS_URL = DEXSCREENER_BASE + "/latest/dex/tokens/{}"

TRENCH_DEX_IDS = ("pumpfun", "pumpswap")
MAX_PROFILES = 30
_CHUNK = 30


def _default_get(url: str):
    """Fetch JSON over HTTPS. Tolerates a truncated chunked body when
    the readable prefix still parses (sandbox connections sometimes
    drop the tail of large responses)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "SolEntry-trenches-feed"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except http.client.IncompleteRead as exc:
        partial = exc.partial or b""
        if partial:
            return json.loads(partial.decode("utf-8"))
        raise


def _best_pair(pairs: list) -> dict | None:
    """The highest-liquidity pump.fun / PumpSwap pair for a mint."""
    best = None
    best_liq = -1.0
    for pair in pairs or []:
        if pair.get("chainId") != "solana":
            continue
        if pair.get("dexId") not in TRENCH_DEX_IDS:
            continue
        price = pair.get("priceUsd")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        liq = (pair.get("liquidity") or {}).get("usd") or 0.0
        try:
            liq = float(liq)
        except (TypeError, ValueError):
            liq = 0.0
        if liq >= best_liq:
            best, best_liq = pair, liq
    return best


def _entry(mint: str, pair: dict) -> dict:
    changes = pair.get("priceChange") or {}
    def _pct(key):
        value = changes.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    def _num(key):
        value = pair.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    def _vol(key):
        volume = pair.get("volume") or {}
        value = volume.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    def _txn(window, side):
        txns = pair.get("txns") or {}
        bucket = txns.get(window) or {}
        value = bucket.get(side)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return {
        "mint": mint,
        "symbol": (pair.get("baseToken") or {}).get("symbol") or mint[:8],
        "name": (pair.get("baseToken") or {}).get("name") or "",
        "price_usd": float(pair["priceUsd"]),
        "change_5m_pct": _pct("m5"),
        "change_1h_pct": _pct("h1"),
        "change_6h_pct": _pct("h6"),
        "change_24h_pct": _pct("h24"),
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
        # Recorded for display only. Nothing filters on it: a trending
        # coin qualifies at any market cap.
        "market_cap_usd": _num("marketCap"),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "pair_created_at": pair.get("pairCreatedAt"),
        # Regime inputs for the ∆scanner∆ profit ladders: 5-minute
        # and 1-hour USD volume, and 5-minute buy/sell counts.
        # Missing here means the regime reads SLOW (fail-closed) —
        # never guessed.
        "volume_m5_usd": _vol("m5"),
        "volume_h1_usd": _vol("h1"),
        "txns_m5_buys": _txn("m5", "buys"),
        "txns_m5_sells": _txn("m5", "sells"),
    }


def fetch_prices_for_mints(mints, http_get=None) -> dict:
    """Current USD prices for specific mints (held positions).

    Used by the P&L page to price coins the CoinGecko list does not
    cover. Returns {mint: price_usd} for pump.fun / PumpSwap pairs
    only; a mint with no usable pair is left out (the caller values
    it at cost and says so), and any fetch failure returns {}.
    """
    http_get = http_get or _default_get
    wanted = [m for m in dict.fromkeys(mints or []) if m]
    if not wanted:
        return {}
    try:
        out = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            data = http_get(TOKENS_URL.format(",".join(chunk)))
            by_mint: dict = {}
            for pair in data.get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = _best_pair(pairs)
                if best is not None:
                    out[mint] = float(best["priceUsd"])
        return out
    except Exception:
        return {}


def fetch_mint_identities(mints, http_get=None) -> dict:
    """Real name + ticker for specific mints (held positions), from
    the same DexScreener pairs `fetch_prices_for_mints` reads: the
    best (highest-liquidity) pump.fun / PumpSwap pair's base token.

    Returns {mint: {"name": str|None, "symbol": str|None}}. A mint
    with no usable pair, or a pair whose token carries no name or
    symbol, is left OUT — the caller keeps its honest "Pump.fun
    token …" label and short mint ticker instead of guessing — and
    any fetch failure returns {}.
    """
    http_get = http_get or _default_get
    wanted = [m for m in dict.fromkeys(mints or []) if m]
    if not wanted:
        return {}
    try:
        out = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            data = http_get(TOKENS_URL.format(",".join(chunk)))
            by_mint: dict = {}
            for pair in data.get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = _best_pair(pairs)
                if best is None:
                    continue
                base = best.get("baseToken") or {}
                name = base.get("name") or None
                symbol = base.get("symbol") or None
                if name is None and symbol is None:
                    continue
                out[mint] = {"name": name, "symbol": symbol}
        return out
    except Exception:
        return {}


def fetch_mint_1h_changes(mints, http_get=None) -> dict:
    """1-hour % change for specific mints (held positions), from the
    same DexScreener pairs `fetch_prices_for_mints` reads: the best
    (highest-liquidity) pump.fun / PumpSwap pair's priceChange.h1.

    Returns {mint: change_1h_pct}. A mint with no usable pair, or a
    pair with no 1h figure, is left OUT (the caller shows "—") — a
    change is never guessed — and any fetch failure returns {}.
    """
    http_get = http_get or _default_get
    wanted = [m for m in dict.fromkeys(mints or []) if m]
    if not wanted:
        return {}
    try:
        out = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            data = http_get(TOKENS_URL.format(",".join(chunk)))
            by_mint: dict = {}
            for pair in data.get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = _best_pair(pairs)
                if best is None:
                    continue
                change = (best.get("priceChange") or {}).get("h1")
                if change is None:
                    continue
                try:
                    out[mint] = float(change)
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        return {}


def fetch_trenches_scan(mints=None, http_get=None) -> dict:
    """Scan the trenches once.

    `mints` (optional) are extra token addresses to include — the loop
    passes the mints it already holds so their prices come back too.
    Returns {"candidates", "prices", "source", "as_of", "error"}.
    "candidates" are fresh trenches tokens with price data; "prices"
    maps every mint seen (candidates + held) to its entry.
    """
    http_get = http_get or _default_get
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        profiles = http_get(PROFILES_URL)
        if not isinstance(profiles, list):
            raise ValueError("unexpected profiles response")
        fresh = [p.get("tokenAddress") for p in profiles
                 if isinstance(p, dict)
                 and p.get("chainId") == "solana"
                 and p.get("tokenAddress")][:MAX_PROFILES]
        wanted = list(dict.fromkeys(list(mints or []) + fresh))
        prices: dict = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            data = http_get(TOKENS_URL.format(",".join(chunk)))
            by_mint: dict = {}
            for pair in data.get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = _best_pair(pairs)
                if best is not None:
                    prices[mint] = _entry(mint, best)
        candidates = [prices[m] for m in fresh if m in prices]
        return {"candidates": candidates, "prices": prices,
                "source": "dexscreener", "as_of": now, "error": None}
    except Exception as exc:  # feed failure: no data, never fake data
        return {"candidates": [], "prices": {}, "source": "dexscreener",
                "as_of": now, "error": str(exc)[:160]}
