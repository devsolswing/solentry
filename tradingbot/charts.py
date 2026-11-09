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
import urllib.parse
import urllib.request

from .prices import COINS
from .trenches import DEXSCREENER_BASE, TOKENS_URL, _best_pair

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
