"""Coin prices: CoinGecko public API server-side, with a clearly-labeled
baked fallback snapshot. Stale prices are NEVER presented as live — the
caller always gets `source` ('live' or 'snapshot') and `as_of`.
"""

from __future__ import annotations

import json
import time
import urllib.request

COINS = {
    "BTC": {"name": "Bitcoin", "coingecko": "bitcoin", "symbol": "BTC"},
    "ETH": {"name": "Ethereum", "coingecko": "ethereum", "symbol": "ETH"},
    "SOL": {"name": "Solana", "coingecko": "solana", "symbol": "SOL"},
    "USDC": {"name": "USD Coin", "coingecko": "usd-coin", "symbol": "USDC"},
    "DOGE": {"name": "Dogecoin", "coingecko": "dogecoin", "symbol": "DOGE"},
    "PEPE": {"name": "Pepe", "coingecko": "pepe", "symbol": "PEPE"},
    "BONK": {"name": "Bonk", "coingecko": "bonk", "symbol": "BONK"},
    "JUP": {"name": "Jupiter", "coingecko": "jupiter-exchange-solana",
            "symbol": "JUP"},
}

# Baked snapshot, fetched live from CoinGecko at build time.
# NEVER presented as live: the API always labels this "snapshot".
SNAPSHOT_AS_OF = "2026-11-09T10:10:11Z"
SNAPSHOT_PRICES = {
    "BTC": 74110.0,
    "ETH": 2102.18,
    "SOL": 86.14,
    "USDC": 0.999387,
    "DOGE": 0.068835,
    "PEPE": 0.0000051,
    "BONK": 0.00000273,
    "JUP": 0.233506,
}

_CACHE_TTL_SECONDS = 60
_cache: dict = {"at": 0.0, "data": None}


def _fetch_live() -> dict | None:
    ids = ",".join(c["coingecko"] for c in COINS.values())
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "trading-bot-app"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    out = {}
    for symbol, info in COINS.items():
        entry = raw.get(info["coingecko"])
        if not entry or "usd" not in entry:
            return None
        out[symbol] = {
            "price": float(entry["usd"]),
            "change_24h": entry.get("usd_24h_change"),
        }
    return out


def get_prices() -> dict:
    """Return {coin: {price, change_24h}, source, as_of}.

    source is 'live' only when CoinGecko answered just now; otherwise
    the baked snapshot is returned and labeled 'snapshot'.
    """
    now = time.time()
    if _cache["data"] and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["data"]
    live = _fetch_live()
    if live is not None:
        data = {"prices": live, "source": "live",
                "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    else:
        data = {
            "prices": {s: {"price": p, "change_24h": None}
                       for s, p in SNAPSHOT_PRICES.items()},
            "source": "snapshot",
            "as_of": SNAPSHOT_AS_OF,
        }
    _cache["at"] = now
    _cache["data"] = data
    return data


def get_price(coin_id: str) -> tuple[float | None, str, str]:
    """(price, source, as_of) for one coin, or (None, source, as_of)."""
    data = get_prices()
    entry = data["prices"].get(coin_id)
    if entry is None:
        return None, data["source"], data["as_of"]
    return entry["price"], data["source"], data["as_of"]
