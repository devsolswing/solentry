"""Coin detail data: market chart + buyers/sellers flow, pump.fun only.

The coin detail page needs three things:
  * a market chart over a chosen range (24hr / 1day / 1w / 1m / 1y /
    all-time) — built ONLY from the live feed's recorded ticks for
    the mint (see LiveFeed.history). The feed keeps roughly the last
    2,000 ticks per coin — a few hours at the 5-second mint poll —
    so a fresh deploy or a coin nobody has watched yet has too
    little history and the page says the chart is still building
    instead of drawing a made-up chart. Longer ranges show the
    history the feed has; that is the honest limit of a live-only
    chart.
  * the 24-hour high and low — computed from the same chart series,
    so the numbers always match the chart;
  * buyers vs sellers % — DexScreener buy/sell transaction counts
    for the coin's own pump.fun / PumpSwap pair over the last 24h
    (txns.h24.buys / txns.h24.sells), or its Raydium pair when the
    coin graduated. Real counts for the coin itself — never the
    counts of some wrapped copy, never a 50/50 placeholder.

Every failure returns an error / None, never fake data: a range
with no chart data says "No data yet", and a coin with no trade-flow
data shows "—". There is no major-coin search, no CoinGecko
market chart, and no CoinGecko OHLC any more — the app lists no
major coins, so there is nothing for CoinGecko to price.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
import urllib.request

from .solana import is_valid_address
from .trenches import (DEXSCREENER_BASE, TOKENS_URL,
                       _best_pair_incl_graduated, _default_get,
                       fetch_mint_identities, fetch_prices_for_mints)

# Range key -> (label shown on the button, days of data). 24hr and 1day
# are the same window (1 day of data), shown under both labels because
# that is what was asked for. The labels are UI only — the chart
# itself is the live feed's recorded ticks (see fetch_market_chart).
CHART_RANGES = {
    "24h": ("24hr", 1),
    "1d": ("1day", 1),
    "1w": ("1w", 7),
    "1m": ("1m", 30),
    "1y": ("1y", 365),
    "all": ("all-time", "max"),
}

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
    price and the real name / ticker when it has them, and the
    on-chain Metaplex metadata names a bonding-curve mint the feed
    has never seen (Gate, AuditTest). A mint nobody can name reads
    "Unknown coin" — the address is the `coin_id`, never shown as
    the name, and the old "Pump.fun token Eu4u…8uLE" fallback label
    is gone. No price when nobody has priced it yet — the real buy
    is priced from the bonding curve, not the feed; the executor
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
        # A bonding-curve mint the feed has never seen still has a
        # real name on-chain: pump.fun wrote it into the Metaplex
        # metadata at creation.
        try:
            from .coin_meta import _from_metaplex
            from .config import Config
            chain = _from_metaplex(mint, Config().rpc_url)
            if chain:
                name = chain.get("name")
                symbol = chain.get("symbol")
        except Exception:
            pass
    result = {"coin_id": mint,
              "name": name or symbol or "Unknown coin",
              "symbol": (symbol or "").upper() or None,
              "price_usd": price, "change_24h": None, "is_mint": True}
    _search_cache[mint] = {"at": now, "results": [dict(result)],
                           "error": None, "source": "live"}
    return {"results": [dict(result)], "error": None, "source": "live"}


def search_coins(query: str, http_get=None) -> dict:
    """Live coin search: {"results", "error", "source"}.

    Pump.fun coins only. `query` is a name, symbol, or mint fragment
    the user typed — or a pasted token address, which resolves
    directly to that mint instead of being matched as text (a devnet
    mint has no mainnet pair for a name search to find). Results
    come from DexScreener's free token search (Solana pairs only,
    best liquidity first, at most 8), each with the coin's real name
    + ticker and its pump.fun mint address. A feed that does not
    answer returns an error and no invented coins — the popup says
    so instead of guessing. A query shorter than 2 characters
    returns no results: single letters match nearly everything and
    are not useful. A majors query (BTC, ETH, SOL, ZEC, …) returns
    nothing — SOL is the unit of account, never a listed coin, and
    the old 36-coin list is gone.
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
                sym = (base.get("symbol") or "").upper() or None
                by_mint[mint] = (liq, {
                    "coin_id": mint,
                    "name": base.get("name") or base.get("symbol")
                    or "Unknown coin",
                    "symbol": sym,
                    "price_usd": price, "change_24h": None,
                    "is_mint": True})
        ranked = sorted(by_mint.values(), key=lambda item: -item[0])
        for _liq, entry in ranked:
            results.append(entry)
            if len(results) >= 8:
                break
    except Exception:
        error = "coin search feed unavailable right now"
    results = results[:8]
    _search_cache[key] = {"at": now, "results": [dict(r) for r in results],
                          "error": error, "source": "live"}
    return {"results": [dict(r) for r in results],
            "error": error, "source": "live"}


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


def fetch_market_chart(coin_id: str, days, feed=None,
                       http_get=None) -> dict:
    """Market chart for a pump.fun mint: {"points", "error"}.

    `points` is [{"ts": ms_since_epoch, "price": float}] oldest
    first, built ONLY from the live feed's recorded ticks — the same
    ticks the candles are built from, so the line and the candles
    can never disagree. `days` bounds the window (points older than
    it are dropped); the feed only holds a few hours of history, so
    longer ranges show what the feed has. A mint with no recorded
    ticks returns no points and an error string, and the page says
    "No data yet" instead of inventing a chart. A symbol (not a
    mint address) is not a coin in this app any more and returns
    the same empty result.
    """
    if not is_valid_address(coin_id or ""):
        return {"points": [],
                "error": "no chart history for this token"}
    if feed is None:
        return {"points": [], "error": "live feed unavailable"}
    try:
        ticks = feed.history(coin_id) or []
    except Exception:
        return {"points": [], "error": "live feed unavailable"}
    points = []
    try:
        window = float(days) * 86400.0 if days != "max" else None
    except (TypeError, ValueError):
        window = None
    newest = max((ts for ts, _ in ticks), default=None)
    for ts, price in ticks:
        try:
            ts = float(ts)
            price = float(price)
        except (TypeError, ValueError):
            continue
        if window is not None and newest is not None \
                and ts < newest - window:
            continue
        points.append({"ts": int(ts * 1000), "price": price})
    if not points:
        return {"points": [],
                "error": "chart still building — no live ticks yet"}
    return {"points": points, "error": None}


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
    """Buyers vs sellers over the last 24h for a pump.fun mint, or
    None. The mint is looked up directly on DexScreener and its own
    best pump.fun / PumpSwap pair's txn counts are used — or its
    Raydium pair when the coin graduated. A symbol is not a coin in
    this app any more and returns None. Any failure or missing txn
    counts returns None and the page shows "—"."""
    if not is_valid_address(coin_id or ""):
        return None
    http_get = http_get or _default_get
    try:
        data = http_get(TOKENS_URL.format(coin_id))
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"]
        best = _best_pair_incl_graduated(pairs)
        return _flow_from_pair(best) if best else None
    except Exception:
        return None
