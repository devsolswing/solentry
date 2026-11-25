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

from .solana import is_valid_address, rpc_client_for_config
from .streaming import SOL_MAX_AGE_SECONDS, chain_price_usd_for_mints
from .trenches import (DEXSCREENER_BASE, TOKENS_URL, TRENCH_DEX_IDS,
                       _best_pair_incl_graduated, _default_get,
                       fetch_mint_identities)

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


def _feed_sol_price_usd(feed) -> float | None:
    """The ONE SOL price in USD, or None: the live feed's SOL tick
    (Jupiter-fed) only. A SOL tick is usable up to
    SOL_MAX_AGE_SECONDS (90s, the stream's own SOL bound): the feed
    refreshes SOL every 60s, so the 30s fresh-tick cut would leave
    the SOL number empty about half the time. No other source."""
    if feed is None:
        return None
    try:
        age = feed.tick_age_seconds("SOL")
    except Exception:
        return None
    if age is None or age > SOL_MAX_AGE_SECONDS:
        return None
    try:
        tick = feed.snapshot(["SOL"]).get("SOL")
    except Exception:
        return None
    if tick is None:
        return None
    try:
        price = float(tick["price"])
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _chain_prices_for_mints(mints, feed=None, cfg=None) -> dict:
    """{mint: price_usd} from the single source: the feed's fresh
    ticks first; any mint without one is priced by one batched
    Helius RPC read of its bonding curve (graduated -> Raydium
    pool, the same lookup the stream uses). Mints no source can
    price are absent — never a DexScreener price, never guessed.
    No feed SOL tick (or no cfg to build the RPC client) means {}
    for the chain half — no USD conversion without a live SOL
    rate. Never raises."""
    wanted = [m for m in dict.fromkeys(mints or []) if m]
    if not wanted:
        return {}
    out: dict = {}
    missing = []
    if feed is not None:
        for mint in wanted:
            try:
                price = feed.live_price(mint)
            except Exception:
                price = None
            if price is not None and price > 0:
                out[mint] = price
            else:
                missing.append(mint)
    else:
        missing = list(wanted)
    if not missing or cfg is None:
        return out
    sol_price = _feed_sol_price_usd(feed)
    if sol_price is None:
        return out
    try:
        client = rpc_client_for_config(cfg)
    except Exception:
        return out
    try:
        out.update(chain_price_usd_for_mints(
            client, missing, sol_price) or {})
    except Exception:
        pass
    return out


def _search_by_address(mint: str, http_get=None, feed=None,
                       cfg=None) -> dict:
    """A pasted token address resolves directly, not as a name
    search: the discovery index gives the real name / ticker when
    it has them, and the on-chain Metaplex metadata names a
    bonding-curve mint the index has never seen (Gate, AuditTest).
    The PRICE comes from the single source only — the feed's fresh
    tick, else one Helius RPC read of the bonding curve — or it is
    None ("no data" on the page), never a DexScreener price. A
    mint nobody can name reads "Unknown coin" — the address is the
    `coin_id`, never shown as the name, and the old "Pump.fun token
    Eu4u…8uLE" fallback label is gone. No price when nobody has
    priced it yet — the real buy is priced from the bonding curve,
    not the feed; the executor refuses plainly at buy time when
    there is no devnet bonding curve. Nothing is ever fabricated:
    no name, no price, and no coin the address does not name. One
    lookup per query is cached 30 seconds, keyed by the exact
    address (base58 is case-sensitive, so the key is never
    lowercased)."""
    cached = _search_cache.get(mint)
    now = time.time()
    if cached and now - cached["at"] < SEARCH_CACHE_TTL_SECONDS:
        return {"results": [dict(r) for r in cached["results"]],
                "error": cached["error"], "source": cached["source"]}
    price = None
    name = None
    symbol = None
    try:
        price = _chain_prices_for_mints(
            [mint], feed=feed, cfg=cfg).get(mint)
    except Exception:
        price = None
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


def search_coins(query: str, http_get=None, feed=None,
                 cfg=None) -> dict:
    """Live coin search: {"results", "error", "source"}.

    Pump.fun coins only. `query` is a name, symbol, or mint fragment
    the user typed — or a pasted token address, which resolves
    directly to that mint instead of being matched as text (a devnet
    mint has no mainnet pair for a name search to find). Results
    come from DexScreener's free token search, filtered to Solana
    pairs on pump.fun / PumpSwap ONLY (the same dex filter the
    trenches feed uses) — best liquidity first, at most 8 — each
    with the coin's real name + ticker and its pump.fun mint
    address. That search is DISCOVERY only: the price on each
    result comes from the single source — the feed's fresh tick,
    else one batched Helius RPC read of the bonding curves — and a
    mint neither can price is dropped, never shown at a DexScreener
    price. DexScreener's search does NOT filter by DEX, so a
    query like "BTC" also matches wrapped majors (wBTC on Raydium
    / Orca, …): those pairs are dropped here, never shown. A
    memecoin that happens to share a major's ticker is still a
    pump.fun coin and may appear — the filter is the DEX, not the
    name. A feed that does not answer returns an error and no
    invented coins — the popup says so instead of guessing. A
    query shorter than 2 characters returns no results: single
    letters match nearly everything and are not useful. A majors
    query (BTC, ETH, SOL, ZEC, …) returns nothing — SOL is the
    unit of account, never a listed coin, and the old 36-coin
    list is gone.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return {"results": [], "error": None, "source": "live"}
    if is_valid_address(q):
        # A pasted token address is not a name search: resolve the
        # mint directly.
        return _search_by_address(q, http_get=http_get, feed=feed,
                                  cfg=cfg)
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
        # Solana AND pump.fun / PumpSwap only. DexScreener's search
        # matches the query text across every DEX, so without the
        # dex filter a "BTC" query returns wrapped majors (wBTC on
        # Raydium / Orca, …) — the pump.fun-only leak this filter
        # closes. A graduated pump.fun coin (now on Raydium) does
        # NOT appear in name search; it stays reachable by pasted
        # mint address and from holdings.
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"
                 and p.get("dexId") in TRENCH_DEX_IDS]
        by_mint: dict = {}
        for pair in pairs:
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if not mint:
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
                    "price_usd": None, "change_24h": None,
                    "is_mint": True})
        ranked = sorted(by_mint.values(), key=lambda item: -item[0])
        # Price from the single source only (feed tick, else one
        # batched Helius chain read) — never the DexScreener
        # priceUsd on the pair. A mint neither can price is
        # dropped, never guessed.
        chain_prices = _chain_prices_for_mints(
            [entry["coin_id"] for _liq, entry in ranked],
            feed=feed, cfg=cfg)
        for _liq, entry in ranked:
            price = chain_prices.get(entry["coin_id"])
            if price is None:
                continue
            entry["price_usd"] = price
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


# GeckoTerminal (free, no key) — the home page's per-coin market
# charts. Verified live 2026-11-24 for the real $GATE mint: the
# token-pools endpoint returns its GATE / SOL pool and the OHLCV
# endpoint returns real 5-minute candles [ts, open, high, low,
# close, volume], newest first. The pool lookup is cached an hour
# (a mint's pools don't move); closes are cached 120s (the home
# page polls every 60s, so a render rarely calls the network).
# Any failure returns None / [] — the caller falls back to the
# feed's recorded ticks, then to no chart at all. A chart is never
# invented, and a pool lookup that fails is cached as None so a
# down API can't stall every render.
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
_GT_POOL_TTL_SECONDS = 3600
_GT_OHLCV_TTL_SECONDS = 120
_gt_pool_cache: dict = {}
_gt_ohlcv_cache: dict = {}


def _gt_pick_pool(pools: list) -> str | None:
    """The pool address to chart a mint from: its pump.fun /
    PumpSwap pool when one is listed, else the deepest pool by
    reserve. None when no pool has an address."""
    best = None
    best_reserve = -1.0
    for p in pools or []:
        attrs = (p or {}).get("attributes") or {}
        address = attrs.get("address")
        if not address:
            continue
        dex_id = (((p.get("relationships") or {}).get("dex") or {})
                  .get("data", {}) or {}).get("id", "") or ""
        if "pump" in str(dex_id).lower():
            return address
        try:
            reserve = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            reserve = 0.0
        if reserve > best_reserve:
            best_reserve = reserve
            best = address
    return best


def geckoterminal_pool_for_mint(mint: str, http_get=None) -> str | None:
    """The chart pool for a mint address, or None. A symbol is not
    a mint and returns None."""
    mint = (mint or "").strip()
    if not is_valid_address(mint):
        return None
    now = time.time()
    cached = _gt_pool_cache.get(mint)
    if cached and now - cached["at"] < _GT_POOL_TTL_SECONDS:
        return cached["pool"]
    http_get = http_get or _default_get
    pool = None
    try:
        data = http_get(
            GECKOTERMINAL_BASE + "/networks/solana/tokens/"
            + mint + "/pools?page=1")
        pool = _gt_pick_pool((data or {}).get("data") or [])
    except Exception:
        pool = None
    _gt_pool_cache[mint] = {"at": now, "pool": pool}
    return pool


def geckoterminal_closes_1h(mint: str, http_get=None) -> list:
    """Close prices of the last 12 traded 5-minute candles for a
    mint's best pool, oldest first — the coin's recent market, true
    OHLCV data. For a quiet coin the last 12 traded candles can
    span more than an hour (a coin that barely trades has few
    candles); the points are still real traded prices, never a
    fixed-window fake. Fewer than 2 closes, or any failure,
    returns []."""
    mint = (mint or "").strip()
    if not is_valid_address(mint):
        return []
    now = time.time()
    cached = _gt_ohlcv_cache.get(mint)
    if cached and now - cached["at"] < _GT_OHLCV_TTL_SECONDS:
        return list(cached["closes"])
    http_get = http_get or _default_get
    closes = []
    try:
        pool = geckoterminal_pool_for_mint(mint, http_get=http_get)
        if pool:
            data = http_get(
                GECKOTERMINAL_BASE + "/networks/solana/pools/"
                + pool + "/ohlcv/minute?aggregate=5&limit=12")
            rows = (((data or {}).get("data") or {})
                    .get("attributes", {}).get("ohlcv_list") or [])
            for row in rows:
                try:
                    close = float(row[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if close > 0:
                    closes.append(close)
            closes.reverse()  # the API lists newest first
    except Exception:
        closes = []
    _gt_ohlcv_cache[mint] = {"at": now, "closes": list(closes)}
    return closes
