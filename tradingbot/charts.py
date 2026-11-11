"""Coin detail data: market chart + buyers/sellers flow.

The coin detail page needs three things the price list does not have:
  * a market chart over a chosen range (24hr / 1day / 1w / 1m / 1y /
    all-time) — CoinGecko's free market_chart endpoint, no key;
  * the 24-hour high and low — computed from the 24-hour chart
    series itself, so the numbers always match the chart;
  * buyers vs sellers % — DexScreener buy/sell transaction counts
    for the pair over the last 24h (txns.h24.buys / txns.h24.sells).

Every failure returns an error / None, never fake data: a range
with no chart data says "No data yet", and a coin with no trade-flow
data shows "—". Pump.fun mints have no CoinGecko history, so their
chart ranges say "No data yet" — DexScreener's free API has no
historical candles to fill them with.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
import urllib.request

from .networks import search_result_network
from .prices import COINS, get_prices
from .solana import is_valid_address
from .trenches import (DEXSCREENER_BASE, TOKENS_URL, _best_pair,
                      fetch_mint_identities, fetch_prices_for_mints)

# Range key -> (label shown on the button, CoinGecko `days` value).
# 24hr and 1day are the same window (1 day of data), shown under both
# labels because that is what was asked for.
CHART_RANGES = {
    "24h": ("24hr", 1),
    "1d": ("1day", 1),
    "1w": ("1w", 7),
    "1m": ("1m", 30),
    "1y": ("1y", 365),
    "all": ("all-time", "max"),
}

COINGECKO_CHART_URL = ("https://api.coingecko.com/api/v3/coins/{}/"
                       "market_chart?vs_currency=usd&days={}")
COINGECKO_OHLC_URL = ("https://api.coingecko.com/api/v3/coins/{}/"
                      "ohlc?vs_currency=usd&days={}")
# CoinGecko's own OHLC granularity, used only when the returned
# candles cannot speak for themselves (empty or a single candle):
# 1 day -> 30-minute candles, 7-30 days -> 4-hour, 31+ days -> 4-day.
OHLC_INTERVAL_FALLBACK = {1: 1800, 7: 14400, 30: 14400, 365: 345600,
                          "max": 345600}
DEXSCREENER_SEARCH_URL = DEXSCREENER_BASE + "/latest/dex/search?q={}"


def _default_get(url: str):
    """Fetch JSON over HTTPS. Tolerates a truncated chunked body when
    the readable prefix still parses (sandbox connections sometimes
    drop the tail of large responses)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "SolEntry-coin-chart"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except http.client.IncompleteRead as exc:
        partial = exc.partial or b""
        if partial:
            return json.loads(partial.decode("utf-8"))
        raise


# Live coin search (quick Buy/Sell popup): one search per query is
# cached 30 seconds so typing does not hammer the feeds.
SEARCH_CACHE_TTL_SECONDS = 30
_search_cache: dict = {}


def _search_by_address(mint: str, http_get=None) -> dict:
    """A pasted token address resolves directly, not as a name
    search: the trenches feed (DexScreener token pairs) gives the
    price and the real name / ticker when it has them. A devnet mint
    the feed has never seen still resolves — with the honest
    "Pump.fun token …" label and no price — because the real buy is
    priced from the bonding curve, not the feed; the executor
    refuses plainly at buy time when there is no devnet bonding
    curve. Nothing is ever fabricated: no name, no price, and no
    coin the address does not name. One lookup per query is cached
    30 seconds, keyed by the exact address (base58 is
    case-sensitive, so the key is never lowercased)."""
    cached = _search_cache.get(mint)
    now = time.time()
    if cached and now - cached["at"] < SEARCH_CACHE_TTL_SECONDS:
        return {"results": [dict(r) for r in cached["results"]],
                "error": cached["error"], "source": cached["source"]}
    price = None
    name = None
    symbol = None
    try:
        prices = fetch_prices_for_mints([mint], http_get=http_get)
        price = prices.get(mint)
    except Exception:
        pass
    try:
        identities = fetch_mint_identities([mint], http_get=http_get)
        ident = identities.get(mint) or {}
        name = ident.get("name") or None
        symbol = ident.get("symbol") or None
    except Exception:
        pass
    if name is None and symbol is None:
        name = "Pump.fun token " + mint[:4] + "…" + mint[-4:]
        symbol = mint[:4] + "…"
    result = {"coin_id": mint, "name": name,
              "symbol": (symbol or mint[:8]).upper(),
              "price_usd": price, "change_24h": None, "is_mint": True,
              **search_result_network(mint, True, symbol)}
    _search_cache[mint] = {"at": now, "results": [dict(result)],
                           "error": None, "source": "live"}
    return {"results": [dict(result)], "error": None, "source": "live"}


def search_coins(query: str, http_get=None, prices_fn=None) -> dict:
    """Live coin search: {"results", "error", "source"}.

    `query` is a name, symbol, or mint fragment the user typed — or
    a pasted token address, which resolves directly to that mint
    instead of being matched as text (a devnet mint has no mainnet
    pair for a name search to find). Known coins (the app's price
    list) match by name or symbol and carry their live price from
    the price feed. Anything else comes from DexScreener's free
    token search (Solana pairs only, best liquidity first, at most
    8). A feed that does not answer returns an error and no invented
    coins — the popup says so instead of guessing. A query shorter
    than 2 characters returns no results: single letters match
    nearly everything and are not useful.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return {"results": [], "error": None, "source": "live"}
    if is_valid_address(q):
        # A pasted token address is not a name search: resolve the
        # mint directly.
        return _search_by_address(q, http_get=http_get)
    key = q.lower()
    now = time.time()
    cached = _search_cache.get(key)
    if cached and now - cached["at"] < SEARCH_CACHE_TTL_SECONDS:
        return {"results": [dict(r) for r in cached["results"]],
                "error": cached["error"], "source": cached["source"]}
    results = []
    error = None
    source = "live"
    get_live = prices_fn or get_prices
    try:
        data = get_live()
        source = data.get("source", "live")
        for symbol, info in COINS.items():
            if key in info["name"].lower() or key in symbol.lower():
                entry = (data.get("prices") or {}).get(symbol) or {}
                results.append({
                    "coin_id": symbol, "name": info["name"],
                    "symbol": symbol,
                    "price_usd": entry.get("price"),
                    "change_24h": entry.get("change_24h"),
                    "is_mint": False,
                    **search_result_network(symbol, False)})
    except Exception:
        error = "price feed unavailable right now"
    seen_symbols = {r["symbol"].upper() for r in results}
    http_get = http_get or _default_get
    try:
        data = http_get(DEXSCREENER_SEARCH_URL.format(
            urllib.parse.quote(q)))
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"]
        by_mint: dict = {}
        for pair in pairs:
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if not mint:
                continue
            price = pair.get("priceUsd")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            if price is None:
                continue
            liq = (pair.get("liquidity") or {}).get("usd") or 0.0
            try:
                liq = float(liq)
            except (TypeError, ValueError):
                liq = 0.0
            prev = by_mint.get(mint)
            if prev is None or liq > prev[0]:
                sym = (base.get("symbol") or mint[:8]).upper()
                by_mint[mint] = (liq, {
                    "coin_id": mint,
                    "name": base.get("name") or base.get("symbol") or mint,
                    "symbol": sym,
                    "price_usd": price, "change_24h": None,
                    "is_mint": True,
                    **search_result_network(mint, True, sym)})
        ranked = sorted(by_mint.values(), key=lambda item: -item[0])
        for _liq, entry in ranked:
            if entry["symbol"] in seen_symbols:
                continue
            results.append(entry)
            seen_symbols.add(entry["symbol"])
            if len(results) >= 8:
                break
    except Exception:
        error = "coin search feed unavailable right now"
    results = results[:8]
    _search_cache[key] = {"at": now, "results": [dict(r) for r in results],
                          "error": error, "source": source}
    return {"results": [dict(r) for r in results],
            "error": error, "source": source}


def fetch_ohlc(coin_id: str, days, http_get=None) -> dict:
    """Candlestick data for a coin: {"candles", "interval", "error"}.

    CoinGecko's free OHLC endpoint (no key) returns real
    [timestamp, open, high, low, close] candles — this is the actual
    candlestick data, not candles derived from a line series. Only
    the coins in the app's price list have CoinGecko history; a
    pump.fun mint returns no candles and an error string, and the
    page says the candles are still building instead of inventing
    them. `interval` is the candle width in seconds, read from the
    candles themselves (the gap between consecutive candles); the
    fallback table above is used only when the candles cannot speak
    for themselves.
    """
    http_get = http_get or _default_get
    info = COINS.get((coin_id or "").upper())
    if info is None:
        return {"candles": [], "interval": None,
                "error": "no chart history for this token"}
    try:
        data = http_get(COINGECKO_OHLC_URL.format(
            info["coingecko"], days))
        raw = data if isinstance(data, list) else []
        candles = []
        for entry in raw:
            try:
                ts = int(entry[0])
                o, h, l, c = (float(entry[1]), float(entry[2]),
                              float(entry[3]), float(entry[4]))
            except (TypeError, ValueError, IndexError):
                continue
            candles.append({"ts": ts, "open": o, "high": h, "low": l,
                            "close": c})
        if not candles:
            return {"candles": [], "interval": None,
                    "error": "chart came back empty"}
        interval = None
        if len(candles) >= 2:
            gaps = sorted(candles[i + 1]["ts"] - candles[i]["ts"]
                          for i in range(len(candles) - 1)
                          if candles[i + 1]["ts"] > candles[i]["ts"])
            if gaps:
                interval = gaps[len(gaps) // 2] // 1000
        if not interval:
            interval = OHLC_INTERVAL_FALLBACK.get(
                days, OHLC_INTERVAL_FALLBACK["max"])
        return {"candles": candles, "interval": int(interval),
                "error": None}
    except Exception as exc:
        return {"candles": [], "interval": None,
                "error": str(exc)[:160]}


def mint_candles_from_feed(coin_id: str, interval_seconds: int,
                           feed=None) -> list:
    """Candles for a pump.fun mint, built ONLY from the live feed's
    recorded ticks (see LiveFeed.history). The feed keeps roughly
    the last 2,000 ticks per coin — a few hours at the 5-second mint
    poll — so a fresh deploy or a coin nobody has watched yet has
    too little history to form candles and this returns []. That is
    honest: the page says the candles are still building instead of
    drawing a made-up chart."""
    from .candles import build_candles

    if feed is None:
        return []
    try:
        ticks = feed.history(coin_id)
    except Exception:
        return []
    points = [{"ts": int(ts * 1000), "price": price}
              for ts, price in ticks]
    return build_candles(points, interval_seconds)


def fetch_market_chart(coin_id: str, days, http_get=None) -> dict:
    """Market chart for a coin: {"points", "error"}.

    `points` is [{"ts": ms_since_epoch, "price": float}] oldest
    first, straight from CoinGecko's market_chart. Only the coins in
    the app's price list have CoinGecko history — a pump.fun mint
    returns no points and an error string, and the page says "No
    data yet" instead of inventing a chart.
    """
    http_get = http_get or _default_get
    info = COINS.get((coin_id or "").upper())
    if info is None:
        return {"points": [],
                "error": "no chart history for this token"}
    try:
        data = http_get(COINGECKO_CHART_URL.format(
            info["coingecko"], days))
        raw = data.get("prices") or []
        points = []
        for entry in raw:
            try:
                ts, price = entry[0], float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            points.append({"ts": int(ts), "price": price})
        if not points:
            return {"points": [], "error": "chart came back empty"}
        return {"points": points, "error": None}
    except Exception as exc:
        return {"points": [], "error": str(exc)[:160]}


def high_low_24h(points: list) -> tuple[float | None, float | None]:
    """(24h high, 24h low) from a 24-hour chart series, or (None,
    None) when the series is empty. Computed from the same data the
    chart draws, so the numbers always match what is shown."""
    if not points:
        return None, None
    values = [p["price"] for p in points]
    return max(values), min(values)


def to_pnl_points(points: list) -> list:
    """Convert chart points to the shape pnl.chart_svg expects
    (created_at ISO + total_value_usd), so the detail chart reuses
    the same SVG renderer as the P&L chart."""
    from datetime import datetime, timezone

    out = []
    for p in points:
        out.append({
            "created_at": datetime.fromtimestamp(
                p["ts"] / 1000.0, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "total_value_usd": float(p["price"]),
        })
    return out


def _flow_from_pair(pair: dict) -> dict | None:
    """Buyers/sellers % from one DexScreener pair's 24h txn counts,
    or None when the pair has no usable counts. Never guessed."""
    txns = (pair or {}).get("txns") or {}
    h24 = txns.get("h24") or {}
    try:
        buys = int(h24.get("buys") or 0)
        sells = int(h24.get("sells") or 0)
    except (TypeError, ValueError):
        return None
    total = buys + sells
    if total <= 0:
        return None
    return {"buys": buys, "sells": sells,
            "buys_pct": buys * 100.0 / total,
            "sells_pct": sells * 100.0 / total}


def fetch_trade_flow(coin_id: str, http_get=None) -> dict | None:
    """Buyers vs sellers over the last 24h for a coin, or None.

    Mints are looked up directly on DexScreener; known symbols are
    searched and the highest-liquidity Solana pair for that symbol
    is used. Any failure or missing txn counts returns None and the
    page shows "—".
    """
    http_get = http_get or _default_get
    try:
        if (coin_id or "") in COINS:
            query = urllib.parse.quote((coin_id or "").upper())
            data = http_get(DEXSCREENER_SEARCH_URL.format(query))
            pairs = [p for p in (data.get("pairs") or [])
                     if p.get("chainId") == "solana"
                     and ((p.get("baseToken") or {}).get("symbol") or "")
                     .upper() == (coin_id or "").upper()]
            best = None
            best_liq = -1.0
            for pair in pairs:
                liq = (pair.get("liquidity") or {}).get("usd") or 0.0
                try:
                    liq = float(liq)
                except (TypeError, ValueError):
                    liq = 0.0
                if liq >= best_liq:
                    best, best_liq = pair, liq
            return _flow_from_pair(best) if best else None
        data = http_get(TOKENS_URL.format(coin_id or ""))
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"]
        best = _best_pair(pairs)
        return _flow_from_pair(best) if best else None
    except Exception:
        return None
