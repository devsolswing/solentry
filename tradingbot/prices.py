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
    # The full live universe (2026-11-11): every symbol here has a
    # Kraken USD pair in livefeed.KRAKEN_PAIRS, so the live feed
    # ticks it every few seconds — the Buy/Sell page price for any
    # of these comes from the socket, not a 60s CoinGecko cache.
    # Before this, only the 8 above existed: any other coin (ZEC
    # was the proof case) fell through to thin Solana wrapped
    # copies on DexScreener — five different "ZEC" mints priced
    # $268–$1,650 while the real ZEC traded ~$1,000 — and its
    # page price never updated at all. All CoinGecko ids below
    # were verified live against the API on 2026-11-11.
    "ZEC": {"name": "Zcash", "coingecko": "zcash", "symbol": "ZEC"},
    "XRP": {"name": "XRP", "coingecko": "ripple", "symbol": "XRP"},
    "ADA": {"name": "Cardano", "coingecko": "cardano", "symbol": "ADA"},
    "LTC": {"name": "Litecoin", "coingecko": "litecoin",
            "symbol": "LTC"},
    "DOT": {"name": "Polkadot", "coingecko": "polkadot",
            "symbol": "DOT"},
    "LINK": {"name": "Chainlink", "coingecko": "chainlink",
             "symbol": "LINK"},
    "AVAX": {"name": "Avalanche", "coingecko": "avalanche-2",
             "symbol": "AVAX"},
    "ATOM": {"name": "Cosmos", "coingecko": "cosmos", "symbol": "ATOM"},
    "XLM": {"name": "Stellar", "coingecko": "stellar", "symbol": "XLM"},
    "ALGO": {"name": "Algorand", "coingecko": "algorand",
             "symbol": "ALGO"},
    "NEAR": {"name": "NEAR Protocol", "coingecko": "near",
             "symbol": "NEAR"},
    "UNI": {"name": "Uniswap", "coingecko": "uniswap", "symbol": "UNI"},
    "AAVE": {"name": "Aave", "coingecko": "aave", "symbol": "AAVE"},
    "XTZ": {"name": "Tezos", "coingecko": "tezos", "symbol": "XTZ"},
    "ETC": {"name": "Ethereum Classic", "coingecko": "ethereum-classic",
            "symbol": "ETC"},
    "BCH": {"name": "Bitcoin Cash", "coingecko": "bitcoin-cash",
            "symbol": "BCH"},
    "FIL": {"name": "Filecoin", "coingecko": "filecoin",
            "symbol": "FIL"},
    "HBAR": {"name": "Hedera", "coingecko": "hedera-hashgraph",
             "symbol": "HBAR"},
    "SUI": {"name": "Sui", "coingecko": "sui", "symbol": "SUI"},
    "APT": {"name": "Aptos", "coingecko": "aptos", "symbol": "APT"},
    "SEI": {"name": "Sei", "coingecko": "sei-network", "symbol": "SEI"},
    "INJ": {"name": "Injective", "coingecko": "injective-protocol",
            "symbol": "INJ"},
    "TIA": {"name": "Celestia", "coingecko": "celestia",
            "symbol": "TIA"},
    "ONDO": {"name": "Ondo", "coingecko": "ondo-finance",
             "symbol": "ONDO"},
    "RENDER": {"name": "Render", "coingecko": "render-token",
               "symbol": "RENDER"},
    "TAO": {"name": "Bittensor", "coingecko": "bittensor",
            "symbol": "TAO"},
    "WIF": {"name": "dogwifhat", "coingecko": "dogwifcoin",
            "symbol": "WIF"},
    "POPCAT": {"name": "Popcat", "coingecko": "popcat",
               "symbol": "POPCAT"},
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
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true"
           "&include_1hr_change=true")
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
        # One coin the feed does not name is skipped, not fatal:
        # with 36 ids, one renamed id must never take the whole
        # price list down to the baked snapshot. A coin with no
        # entry gets no price — the page shows "—", never a
        # guessed number. Only a feed that answers for no coin at
        # all falls back to the snapshot.
        if not entry or "usd" not in entry:
            continue
        out[symbol] = {
            "price": float(entry["usd"]),
            "change_24h": entry.get("usd_24h_change"),
            # 1-hour change for the home holdings list. A coin the
            # feed has no 1h figure for gets None ("—" on the page)
            # — never guessed.
            "change_1h": entry.get("usd_1h_change"),
        }
    return out or None


def clear_price_cache() -> None:
    """Drop the cached price list so the next get_prices() call
    re-fetches live. Used by the dashboard Refresh button — the
    user asked for live numbers on demand, not up-to-60-second-old
    ones. A feed that does not answer still falls back to the
    labelled snapshot, as always."""
    _cache["at"] = 0.0
    _cache["data"] = None


def get_prices() -> dict:
    """Return {coin: {price, change_24h, change_1h}, source, as_of}.

    source is 'live' only when CoinGecko answered just now; otherwise
    the baked snapshot is returned and labeled 'snapshot'. A change
    the feed has no figure for is None — the page shows "—", never
    a guessed number.
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
            "prices": {s: {"price": p, "change_24h": None,
                           "change_1h": None}
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
