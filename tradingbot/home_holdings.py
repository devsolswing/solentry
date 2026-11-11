"""Home-page holdings helpers (Page 3): real token images, sparklines,
and on-chain holdings reconciliation.

NOT wired into the home route yet — routes.py is owned by the Android
PWA build right now. The wallet template already renders
`row.image_url` and `row.sparkline_svg` when present, so once the
route attaches them the images/sparklines appear with no template
change. Wiring needed (follow-up, after the PWA lands):
  1. In `_dashboard_data` (or `_coin_rows`): for each coin row, call
     `enrich_row(row, feed=app.config.get("LIVE_FEED"))` to attach
     `image_url` and `sparkline_svg`.
  2. In `_coin_rows` (real/devnet mode): merge on-chain balances via
     `reconcile_holdings(_real_holdings_view(db, user_id),
     fetch_chain_token_balances(rpc, wallet_pubkey))` so coins the
     wallet holds on-chain but the DB never recorded (e.g. Gate, whose
     six buys were recorded as failed) show on home.
  3. Widen the CSP `img-src` in `create_app` to allow the two image
     hosts (`https://dd.dexscreener.com` and
     `https://assets.coingecko.com`), OR proxy the images through a
     same-origin route — the CSP currently allows only 'self' + data:,
     so external image URLs would be blocked by the browser.
Everything here fails closed: any fetch failure returns None / [] /
"", never an exception and never invented data. Token images are
only returned after a HEAD check proves the URL serves an image,
so a row can never render a broken image.
"""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.request

from .charts import fetch_market_chart
from .prices import COINS
from .trenches import TOKENS_URL, _best_pair, _default_get

# Legacy SPL Token program + Token-2022 program (both can hold a
# user's tokens; Gate is legacy SPL).
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

COINGECKO_COIN_URL = (
    "https://api.coingecko.com/api/v3/coins/{}"
    "?localization=false&tickers=false&market_data=false"
    "&community_data=false&developer_data=false&sparkline=false")

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
                        coingecko_id: str | None = None,
                        http_get=None, http_head=None) -> str | None:
    """The real token image URL for a held coin, or None.

    Mints: DexScreener's token pairs for the mint — the best
    (highest-liquidity) pair's `info.imageUrl`. Majors: CoinGecko's
    coin page image (`large`, else `small`, else `thumb`) for the
    coin's CoinGecko id (from the app's price list when not passed).
    The URL is HEAD-verified before it is returned. Any failure —
    no pair, no image, a dead URL, a fetch error — returns None,
    and the page falls back to the ticker-initial coin icon. The
    result is cached for an hour per coin.
    """
    coin_id = (coin_id or "").strip()
    if not coin_id:
        return None
    cached = _image_cache.get(coin_id)
    now = time.time()
    if cached and now - cached[0] < _IMAGE_CACHE_TTL_SECONDS:
        return cached[1]
    get = http_get or _default_get
    url = None
    try:
        if is_mint:
            data = get(TOKENS_URL.format(coin_id))
            best = _best_pair(data.get("pairs") or [])
            if best is not None:
                url = (best.get("info") or {}).get("imageUrl") or None
        else:
            cid = coingecko_id or COINS.get(
                coin_id.upper(), {}).get("coingecko")
            if cid:
                data = get(COINGECKO_COIN_URL.format(cid))
                image = data.get("image") or {}
                url = (image.get("large") or image.get("small")
                       or image.get("thumb") or None)
    except Exception:
        url = None
    url = _verified_image_url(url, http_head=http_head)
    _image_cache[coin_id] = (now, url)
    return url


def clear_image_cache() -> None:
    """Drop the token-image cache (tests)."""
    _image_cache.clear()


def sparkline_svg(points, width: int = 64, height: int = 28) -> str:
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


def sparkline_points_for_coin(coin_id: str, is_mint: bool = False,
                              feed=None, http_get=None) -> list:
    """Real recent prices for a coin's sparkline, oldest first.

    Majors: CoinGecko market_chart (1 day) via the app's own chart
    fetcher. Mints: the live feed's recorded ticks
    (`feed.history`). A coin with no history returns [] and the
    page omits its sparkline — history is never invented. Any
    failure returns [].
    """
    coin_id = (coin_id or "").strip()
    if not coin_id:
        return []
    try:
        if is_mint:
            if feed is None:
                return []
            ticks = feed.history(coin_id) or []
            return [float(price) for _ts, price in ticks
                    if price is not None]
        result = fetch_market_chart(coin_id, 1, http_get=http_get)
        points = result.get("points") or []
        return [float(p["price"]) for p in points
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
               http_head=None) -> dict:
    """Attach a coin row's real image URL and sparkline SVG.

    Sets `row["image_url"]` (only when a verified real image URL
    resolves) and `row["sparkline_svg"]` (only when real price
    history exists). A row with no image or no history is left
    without those keys, and the template falls back to the
    ticker-initial icon / no sparkline. Never raises; the row is
    returned unchanged on any failure.
    """
    if not isinstance(row, dict):
        return row
    coin_id = row.get("coin_id") or ""
    is_mint = bool(row.get("is_mint"))
    try:
        coingecko_id = None
        if not is_mint:
            coingecko_id = COINS.get(
                str(coin_id).upper(), {}).get("coingecko")
        image = resolve_token_image(
            coin_id, is_mint=is_mint, coingecko_id=coingecko_id,
            http_get=http_get, http_head=http_head)
        if image:
            row["image_url"] = image
        points = sparkline_points_for_coin(
            coin_id, is_mint=is_mint, feed=feed, http_get=http_get)
        svg = sparkline_svg(points)
        if svg:
            row["sparkline_svg"] = svg
    except Exception:
        pass
    return row
