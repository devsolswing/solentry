"""Home-page holdings helpers (Page 3): real token images, sparklines,
and on-chain holdings reconciliation — pump.fun coins only.

The wallet template renders `row.image_url` and `row.sparkline_svg`
when present. `_coin_rows` in routes.py attaches them via
`enrich_row(row, feed=..., db=..., rpc_url=...)`. Token images come
from the stored `coin_meta` row for the mint (fetched on purchase, or
lazily on render of a held row) — the coin's official logo from its
metadata image URI or its DexScreener pair — and never from a
bulk load: only coins the user holds are ever fetched. A held coin
with no logo shows no image. Sparklines come from the live feed's
recorded ticks only; there is no CoinGecko fallback any more.
Everything here fails closed: any fetch failure returns None / [] /
"", never an exception and never invented data. Token images are
only returned after a HEAD check proves the URL serves an image,
so a row can never render a broken image. The CSP `img-src` in
`create_app` allows the logo hosts (dd.dexscreener.com,
image.pump.fun, ipfs.io, arweave.net).
"""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.request

from .charts import fetch_market_chart
from .coin_meta import ensure_coin_meta
from .solana import is_valid_address
from .trenches import TOKENS_URL, _best_pair_incl_graduated, _default_get

# Legacy SPL Token program + Token-2022 program (both can hold a
# user's tokens; Gate is legacy SPL).
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Token images change rarely; cache resolutions for an hour so the
# home page does not re-fetch them on every render.
_IMAGE_CACHE_TTL_SECONDS = 3600
_image_cache: dict = {}


def _default_head(url: str):
    """HEAD a URL: returns (status, content_type) or (None, None)."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "SolEntry-home"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type") or ""
    except Exception:
        return None, None


def _verified_image_url(url, http_head=None):
    """Return `url` only if it serves an image (HEAD 200 + image/*
    content type); otherwise None. Never raises."""
    if not url or not isinstance(url, str):
        return None
    if not url.startswith("https://"):
        return None
    head = http_head or _default_head
    try:
        status, ctype = head(url)
    except Exception:
        return None
    if status != 200:
        return None
    if not (ctype or "").lower().startswith("image/"):
        return None
    return url


def resolve_token_image(coin_id: str, is_mint: bool = False,
                        http_get=None, http_head=None, db=None,
                        rpc_url: str | None = None) -> str | None:
    """The real token image URL for a held coin, or None.

    First the stored `coin_meta` row for the mint (fetched on
    purchase, or lazily here when the row is missing — only for a
    coin the user holds, never bulk-loaded): the coin's official
    logo from its metadata image URI or its DexScreener pair. Then,
    when no stored logo exists, DexScreener's token pairs for the
    mint directly — the best (highest-liquidity) pump.fun /
    PumpSwap pair's `info.imageUrl`, or its Raydium pair when the
    coin graduated. The URL is HEAD-verified before it is returned.
    Any failure — no meta, no pair, no image, a dead URL, a fetch
    error — returns None, and the page falls back to the
    ticker-initial coin icon. The result is cached for an hour per
    coin.
    """
    coin_id = (coin_id or "").strip()
    if not coin_id:
        return None
    cached = _image_cache.get(coin_id)
    now = time.time()
    if cached and now - cached[0] < _IMAGE_CACHE_TTL_SECONDS:
        return cached[1]
    url = None
    try:
        if db is not None and is_valid_address(coin_id):
            stored = ensure_coin_meta(
                db, coin_id, rpc_url=rpc_url, http_get=http_get,
                http_head=http_head)
            if stored:
                url = stored.get("image_url") or None
        if url is None and is_mint:
            get = http_get or _default_get
            data = get(TOKENS_URL.format(coin_id))
            best = _best_pair_incl_graduated(data.get("pairs") or [])
            if best is not None:
                url = (best.get("info") or {}).get("imageUrl") or None
    except Exception:
        url = None
    url = _verified_image_url(url, http_head=http_head)
    _image_cache[coin_id] = (now, url)
    return url


def clear_image_cache() -> None:
    """Drop the token-image cache (tests)."""
    _image_cache.clear()


def sparkline_svg(points, width: int = 64, height: int = 16) -> str:
    """A small gold line chart (SVG) from real price points, or "".

    `points` is a list of prices (oldest first). Fewer than two
    points means there is no line to draw, so "" is returned and
    the page omits the sparkline instead of faking one. A flat
    series draws a horizontal midline. At most 48 points are kept
    (evenly spaced, always including first + last) so the SVG stays
    small. All values are numbers — nothing user-supplied ever
    reaches the markup.
    """
    try:
        values = [float(p) for p in (points or []) if p is not None
                  and math.isfinite(float(p))]
    except (TypeError, ValueError):
        return ""
    if len(values) < 2:
        return ""
    max_points = 48
    if len(values) > max_points:
        step = (len(values) - 1) / (max_points - 1)
        values = [values[round(i * step)]
                  for i in range(max_points)]
    lo = min(values)
    hi = max(values)
    span = hi - lo
    n = len(values)
    coords = []
    for i, value in enumerate(values):
        x = round(i * width / (n - 1), 1) if n > 1 else 0
        if span == 0:
            y = round(height / 2, 1)
        else:
            y = round(2 + (1 - (value - lo) / span) * (height - 4), 1)
        coords.append(f"{x},{y}")
    pts = " ".join(coords)
    return (f'<svg class="holdings-spark-svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            f'aria-hidden="true"><polyline points="{pts}" fill="none" '
            f'stroke="#ffd700" stroke-width="1.5" stroke-linejoin="round" '
            f'stroke-linecap="round"/></svg>')


def _last_hour(pairs) -> list:
    """Prices from the last hour of (epoch_seconds, price) pairs,
    oldest first. The hour ends at the newest point, so a feed
    that is briefly behind still yields its last hour. Fewer than
    2 points in the hour returns [] — one point is no line."""
    if len(pairs) < 2:
        return []
    newest = max(ts for ts, _ in pairs)
    cutoff = newest - 3600.0
    return [price for ts, price in pairs if ts >= cutoff]


def sparkline_points_1h(coin_id: str, is_mint: bool = False,
                        feed=None, http_get=None) -> list:
    """Real prices from the last hour for a coin's 1hr sparkline,
    oldest first, from the live feed's recorded ticks (via the
    feed-based market chart — the same ticks the candles are built
    from). There is no CoinGecko fallback any more: the app lists
    no major coins. Fewer than 2 points in the hour returns [] —
    the page omits the sparkline (chart building) instead of
    faking one. Any failure returns []."""
    coin_id = (coin_id or "").strip()
    if not coin_id:
        return []
    try:
        result = fetch_market_chart(coin_id, 1, feed=feed,
                                    http_get=http_get)
        pairs = []
        for p in (result.get("points") or []):
            try:
                pairs.append((float(p.get("ts") or 0) / 1000.0,
                              float(p["price"])))
            except (TypeError, ValueError, AttributeError):
                continue
        return _last_hour(pairs)
    except Exception:
        return []


def sparkline_svg_1h(coin_id: str, is_mint: bool = False,
                     feed=None, http_get=None) -> str:
    """The 1hr sparkline SVG for a coin, or "" when fewer than 2
    real points exist in the last hour."""
    return sparkline_svg(sparkline_points_1h(
        coin_id, is_mint=is_mint, feed=feed, http_get=http_get))


def sparkline_points_for_coin(coin_id: str, is_mint: bool = False,
                              feed=None, http_get=None) -> list:
    """Real recent prices for a coin's sparkline, oldest first,
    from the live feed's recorded ticks (via the feed-based market
    chart). A coin with no history returns [] and the page omits
    its sparkline — history is never invented. Any failure
    returns []."""
    coin_id = (coin_id or "").strip()
    if not coin_id:
        return []
    try:
        result = fetch_market_chart(coin_id, 1, feed=feed,
                                    http_get=http_get)
        return [float(p["price"])
                for p in (result.get("points") or [])
                if p.get("price") is not None]
    except Exception:
        return []


def fetch_chain_token_balances(rpc, pubkey: str) -> list:
    """Token balances the wallet actually holds on-chain.

    Reads `getTokenAccountsByOwner` (parsed) for the legacy SPL
    Token program and Token-2022, and returns
    [{mint, amount (base units), decimals, ui_amount}] with zero
    balances skipped. `rpc` is the app's SolanaRpcClient (anything
    with a `_call(method, params)`). A program that errors is
    skipped; total failure returns []. Amounts come from the chain
    only — never from the app's DB.
    """
    if rpc is None or not pubkey:
        return []
    out: dict = {}
    for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        try:
            result = rpc._call(
                "getTokenAccountsByOwner",
                [pubkey, {"programId": program_id},
                 {"encoding": "jsonParsed"}])
        except Exception:
            continue
        for acct in (result or {}).get("value") or []:
            try:
                info = (acct.get("account", {}).get("data", {})
                        .get("parsed", {}).get("info", {}))
                mint = info.get("mint")
                token_amount = info.get("tokenAmount") or {}
                amount = int(token_amount.get("amount") or 0)
                if not mint or amount == 0:
                    continue
                decimals = int(token_amount.get("decimals") or 0)
                ui = token_amount.get("uiAmount")
                if ui is None:
                    ui = amount / (10 ** decimals) if decimals else float(amount)
                entry = out.get(mint)
                if entry is None:
                    out[mint] = {"mint": mint, "amount": amount,
                                 "decimals": decimals,
                                 "ui_amount": float(ui)}
                else:
                    entry["ui_amount"] += float(ui)
                    entry["amount"] += amount
            except (TypeError, ValueError, AttributeError):
                continue
    return [out[k] for k in sorted(out)]


def reconcile_holdings(db_rows, chain_balances) -> list:
    """Merge the DB's recorded holdings with the chain's truth.

    `db_rows` is [{coin_id, amount, avg_price}] (the app's record,
    which carries the cost basis). `chain_balances` is the output of
    `fetch_chain_token_balances`. The chain is the truth for the
    AMOUNT held: a coin on-chain but missing from the DB (its buys
    were recorded as failed, e.g. Gate) is added with
    `avg_price=None` and `on_chain_only=True`; a coin in both keeps
    its DB cost basis but takes the chain amount. DB rows the chain
    does not list are kept as-is — a partial chain read must never
    hide a holding. Returns [{coin_id, amount, avg_price,
    on_chain_only}] sorted by coin.
    """
    merged: dict = {}
    for row in db_rows or []:
        coin_id = row.get("coin_id")
        if not coin_id:
            continue
        merged[coin_id] = {
            "coin_id": coin_id,
            "amount": float(row.get("amount") or 0.0),
            "avg_price": (float(row["avg_price"])
                          if row.get("avg_price") is not None else None),
            "on_chain_only": False}
    for balance in chain_balances or []:
        mint = balance.get("mint")
        if not mint:
            continue
        try:
            ui_amount = float(balance.get("ui_amount"))
        except (TypeError, ValueError):
            continue
        entry = merged.get(mint)
        if entry is None:
            merged[mint] = {"coin_id": mint, "amount": ui_amount,
                            "avg_price": None, "on_chain_only": True}
        else:
            entry["amount"] = ui_amount
    return [merged[k] for k in sorted(merged)]


def enrich_row(row: dict, feed=None, http_get=None,
               http_head=None, db=None,
               rpc_url: str | None = None) -> dict:
    """Attach a coin row's real image URL and 1hr sparkline SVG.

    Sets `row["image_url"]` (only when a verified real image URL
    resolves — from the stored `coin_meta` row for the mint, fetched
    on purchase or lazily here when `db` is given) and
    `row["sparkline_svg"]` (only when at least 2 real prices exist
    in the last hour). A row with no image or no recent history is
    left without those keys, and the template falls back to the
    ticker-initial icon / no sparkline. Never raises; the row is
    returned unchanged on any failure.
    """
    if not isinstance(row, dict):
        return row
    coin_id = row.get("coin_id") or ""
    is_mint = bool(row.get("is_mint"))
    try:
        image = resolve_token_image(
            coin_id, is_mint=is_mint, http_get=http_get,
            http_head=http_head, db=db, rpc_url=rpc_url)
        if image:
            row["image_url"] = image
        points = sparkline_points_1h(
            coin_id, is_mint=is_mint, feed=feed, http_get=http_get)
        svg = sparkline_svg(points)
        if svg:
            row["sparkline_svg"] = svg
    except Exception:
        pass
    return row
