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
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "pair_created_at": pair.get("pairCreatedAt"),
    }


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
