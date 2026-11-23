"""SOL price (unit of account ONLY) — pump.fun-only app.

The app trades pump.fun coins only. There is no major-coin price list
any more: no 36-coin COINS table, no Kraken feed, no CoinGecko
majors pricing, no baked snapshot. The one price this module still
provides is SOL in USD, because SOL is the base currency the app
values everything in (bonding-curve quotes, gas, balances) — never
a listed or buyable coin.

Source: Jupiter lite-api price v3 (lite-api.jup.ag/price/v3, no key),
one mint (wrapped SOL), cached 60 seconds. A fetch failure returns
None — the caller refuses plainly instead of converting at a guessed
rate. There is no fallback snapshot: the old baked CoinGecko
snapshot (2026-11-09) once served 4-day-old prices as if they were
live after a reload, and it is gone for good.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3?ids={}"
# Second SOL price source (CoinGecko free, no key): the cross-check
# for the Jupiter SOL price. A fetch failure returns None — the
# caller never guesses a price from it.
COINGECKO_SOL_PRICE_URL = ("https://api.coingecko.com/api/v3/"
                           "simple/price?ids=solana&vs_currencies=usd")

_CACHE_TTL_SECONDS = 60
_cache: dict = {"at": 0.0, "price": None, "change_24h": None}
# CoinGecko is rate-limited on the free tier; cache it separately
# for 60s too so a burst of checks does not hammer it.
_cg_cache: dict = {"at": 0.0, "price": None}


def _fetch_coingecko_sol_price(http_get=None) -> float | None:
    """SOL price in USD from CoinGecko free, or None when it does
    not answer. `http_get` is a test injection (url -> parsed JSON).
    """
    try:
        if http_get is not None:
            raw = http_get(COINGECKO_SOL_PRICE_URL)
        else:
            req = urllib.request.Request(
                COINGECKO_SOL_PRICE_URL,
                headers={"User-Agent": "trading-bot-app"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        price = float((raw or {}).get("solana", {}).get("usd"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def get_coingecko_sol_price_usd(http_get=None) -> float | None:
    """Current SOL price in USD from CoinGecko, or None when the
    feed does not answer. Cached 60 seconds. Never guessed."""
    now = time.time()
    if _cg_cache["price"] is not None \
            and now - _cg_cache["at"] < _CACHE_TTL_SECONDS:
        return _cg_cache["price"]
    price = _fetch_coingecko_sol_price(http_get=http_get)
    _cg_cache["at"] = now
    _cg_cache["price"] = price
    return price


def clear_coingecko_sol_price_cache() -> None:
    """Drop the cached CoinGecko SOL price (tests reset it)."""
    _cg_cache["at"] = 0.0
    _cg_cache["price"] = None


def sol_prices_agree(primary: float | None, secondary: float | None,
                     tolerance_pct: float = 2.0) -> bool | None:
    """Do two SOL prices agree within `tolerance_pct`? True when
    they agree, False when they disagree by more than the
    tolerance, None when either price is missing (no cross-check
    possible — the caller decides, never a guessed agreement)."""
    try:
        a = float(primary)
        b = float(secondary)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        return None
    return abs(a - b) / a * 100.0 <= float(tolerance_pct)


def _fetch_sol_price() -> tuple[float | None, float | None]:
    """(SOL price in USD, 24h change %) from Jupiter price v3, or
    (None, None) when the feed does not answer."""
    try:
        req = urllib.request.Request(
            JUPITER_PRICE_URL.format(SOL_MINT),
            headers={"User-Agent": "trading-bot-app"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None, None
    # Jupiter price v3 returns a flat {mint: {...}} map — no "data"
    # wrapper (verified live 2026-11-16: {"So11...": {"usdPrice": ...}}).
    # Accept the wrapped shape too, so either response parses.
    entry = raw.get(SOL_MINT) or (raw.get("data") or {}).get(SOL_MINT) or {}
    try:
        price = float(entry.get("usdPrice"))
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(price) or price <= 0:
        return None, None
    try:
        change = float(entry.get("priceChange24h"))
        if not math.isfinite(change):
            change = None
    except (TypeError, ValueError):
        change = None
    return price, change


def clear_sol_price_cache() -> None:
    """Drop the cached SOL price so the next call re-fetches live.
    Used by the dashboard Refresh button."""
    _cache["at"] = 0.0
    _cache["price"] = None
    _cache["change_24h"] = None


def get_sol_price_usd() -> float | None:
    """Current SOL price in USD from Jupiter, or None when the feed
    does not answer. Cached 60 seconds. Never guessed, never a
    snapshot."""
    now = time.time()
    if _cache["price"] is not None \
            and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["price"]
    price, change = _fetch_sol_price()
    _cache["at"] = now
    _cache["price"] = price
    _cache["change_24h"] = change
    return price


def clear_price_cache() -> None:
    """Drop the cached price list so the next get_prices() call
    re-fetches live. Used by the dashboard Refresh button — the
    user asked for live numbers on demand, not up-to-60-second-old
    ones."""
    clear_sol_price_cache()


def get_prices() -> dict:
    """Return {prices, source, as_of} — the app's price list.

    Pump.fun only: the list has exactly one entry, SOL — the unit
    of account everything is valued in (bonding-curve quotes, gas,
    balances). There is no major-coin list any more, so there is
    nothing else to price here; pump.fun mints are priced by the
    trenches feed (DexScreener / bonding curve), never by this
    function. `source` is 'live' when Jupiter answered just now,
    'unavailable' when it did not — and then the SOL price is
    None, never a snapshot: the old baked CoinGecko snapshot
    (2026-11-09) once served 4-day-old prices as if they were
    live after a reload, and it is gone for good.
    """
    price = get_sol_price_usd()
    now = time.time()
    return {"prices": {"SOL": {"price": price,
                               "change_24h": _cache["change_24h"],
                               "change_1h": None}},
            "source": "live" if price is not None else "unavailable",
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(now))}


def get_price(coin_id: str) -> tuple[float | None, str, str]:
    """(price, source, as_of) for one coin, or (None, source, as_of).
    Only SOL has a price here; anything else is (None, ...)."""
    data = get_prices()
    entry = data["prices"].get(coin_id)
    if entry is None:
        return None, data["source"], data["as_of"]
    return entry["price"], data["source"], data["as_of"]
