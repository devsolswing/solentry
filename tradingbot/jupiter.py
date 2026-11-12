"""Jupiter DEX aggregation for Solana coins: route discovery ("Finding
route…") and pricing, plus the gated real-swap path.

API SURFACE — READ FIRST (the old v6 host is dead):
Jupiter retired the old Swap API v6 host (`quote-api.jup.ag`) and the
Token v1 host (`tokens.jup.ag`); both now fail (empty replies / no
resolve). The live surface, verified against the real API on
2026-11-11, is:
- Swap API, keyless:  https://lite-api.jup.ag/swap/v1
  (GET /quote, POST /swap). No key; lowest rate limit.
- Swap API, keyed:    https://api.jup.ag/swap/v1
  (x-api-key header, key format `jup_...`). Optional: set
  JUPITER_API_KEY to use it for higher rate limits. The key is
  never logged.
- Token API:          https://api.jup.ag/tokens/v2
  (GET /search — resolves a symbol or mint to its metadata,
  verification status and decimals; answers without a key for
  testing).
Do NOT "fix" these URLs back to quote-api.jup.ag — that host is
retired and every call to it fails.

WHAT THIS DOES:
- `quote_buy` / `quote_sell`: fetch a live Jupiter quote. The quote
  IS the scan of the pools SOL is paired with: Jupiter routes across
  every DEX/pool it integrates and returns the route plan (which
  pools, in which order, with what output). The coin page shows
  "Finding route…" while this runs, then the route found — or an
  honest "No route found" when there is none. A quote is never
  cached and never presented as fresh after the fact: every quote
  carries `fetched_at` (the time it was fetched).
- Paper trades price from the quote (see `jupiter_usd_price`):
  the fill rate comes from Jupiter, only the USD denomination comes
  from the app's own SOL price. When Jupiter is unreachable the
  caller falls back to the existing live price — a paper trade is
  never blocked by a quote failure.
- Real swaps are GATED (see `request_real_swap`): Jupiter has no
  devnet liquidity, so a real Jupiter swap is mainnet real money.
  The gate stays closed until James gives an explicit mainnet
  go-ahead. Until then any real-swap request refuses plainly and
  nothing is built, signed or sent.

WRAPPED vs NATIVE — the honesty rule:
On Solana, BTC / ETH / XRP / ZEC exist as WRAPPED tokens. Jupiter
swaps SOL for the wrapped mint — NOT the native coin on its own
chain. The UI must label wrapped coins as wrapped (e.g. "Wrapped
ZEC (Solana)") and NEVER present a wrapped token as the native
coin. Native ZEC / BTC / ETH is the THORChain cross-chain route —
separate, NOT part of this build. Wrapped mints are resolved via
Jupiter's token list API; the four below were verified live against
it on 2026-11-11 (all `isVerified: true`) and are the only mints
ever hardcoded here. Any other symbol is resolved live through
`resolve_mint` (verified + exact-symbol match, highest liquidity
wins) — never from a guessed address.

FUTURE SIGNING — BUILT (2026-11-11, jupiter_executor.py):
The real swap path is complete but GATED OFF by default
(JUPITER_REAL_SWAPS=1 + NETWORK=mainnet). When the gate opens,
the signing bot signs the Jupiter-built swap transaction after
an allowlist check (Jupiter aggregator program + the swap
instruction only, plus the Token/ATA/System/Compute-Budget
instructions a swap needs — each validated, never arbitrary
instructions), within the bot's limits and only when the rules
engine allows it. The 1.9% mainnet fee (fees.py — ALL to the
Treasury dev wallet, NO burn on mainnet) travels as its own
System transfer right after the swap confirms, from the
unspent fee portion, recorded once by swap signature: Jupiter
builds the swap transaction as a versioned (v0) transaction
with address lookup tables, and the lite API rejects
`asLegacyTransaction` (verified live 2026-11-11: HTTP 400 /
timeout), so appending the fee into the same transaction would
mean rewriting a v0 message — the highest-risk byte surgery in
the app, unprovable until mainnet. The separate fee transaction
keeps the swap bytes exactly as Jupiter built them.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .prices import COINS
from .solana import is_valid_address

log = __import__("logging").getLogger("tradingbot.jupiter")

SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9

# Live Jupiter surface (see module docstring — v6 host is retired).
SWAP_API_BASE = "https://lite-api.jup.ag/swap/v1"
SWAP_API_KEYED_BASE = "https://api.jup.ag/swap/v1"
TOKEN_API_BASE = "https://api.jup.ag/tokens/v2"

# Jupiter aggregator program (for the FUTURE signing allowlist only —
# see the module docstring. Not used anywhere in this build).
JUPITER_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

DEFAULT_SLIPPAGE_BPS = 100  # 1%
DEFAULT_BUY_SOL = 0.1
_HTTP_TIMEOUT_SECONDS = 10
_MINT_CACHE_TTL_SECONDS = 3600
_MINT_NEGATIVE_TTL_SECONDS = 600

# Wrapped mints, VERIFIED LIVE 2026-11-11 via
# GET api.jup.ag/tokens/v2/search?query=<symbol> (all isVerified:
# true). Quotes for all four were fetched live the same day
# (SOL -> wBTC / wXRP / wZEC and wZEC -> SOL all returned routes).
# Do NOT add a mint here without the same live verification.
WRAPPED_MINTS = {
    "BTC": {"mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
            "name": "Wrapped BTC (Portal)", "decimals": 8},
    "ETH": {"mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
            "name": "Ether (Portal)", "decimals": 8},
    "XRP": {"mint": "6UpQcMAb5xMzxc7ZfPaVMgx3KqsvKZdT5U718BzD5We2",
            "name": "Wrapped XRP", "decimals": 6},
    "ZEC": {"mint": "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",
            "name": "Zcash", "decimals": 8},
}

# Coins that live natively on Solana (not wrapped): their mint is the
# coin itself. USDC verified live 2026-11-11 (quoted SOL -> USDC).
NATIVE_SOLANA_MINTS = {
    "USDC": {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "name": "USD Coin", "decimals": 6},
}

# In-memory cache for dynamic (token-API) mint resolutions:
# symbol -> (resolved dict | None, expires_at).
_mint_cache: dict = {}


class JupiterError(Exception):
    """Anything that stops a Jupiter lookup: no route, bad mint,
    API error, network failure. The message is user-facing plain
    language — never a stack trace, never a guessed price."""


class NoRouteError(JupiterError):
    """Jupiter answered but has no route for this pair."""


class JupiterRealDisabled(JupiterError):
    """Real Jupiter swaps are gated off (mainnet real money)."""


def _api_key() -> str | None:
    key = (os.environ.get("JUPITER_API_KEY") or "").strip()
    return key or None


def _swap_base() -> tuple[str, dict]:
    """(base URL, extra headers) — keyed host when a key is set,
    otherwise the keyless lite host. The key is never logged."""
    key = _api_key()
    if key:
        return SWAP_API_KEYED_BASE, {"x-api-key": key}
    return SWAP_API_BASE, {}


def _http_get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "solentry-jupiter/1.0",
                      **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        message = (body.get("error") if isinstance(body, dict) else None) \
            or f"Jupiter answered HTTP {exc.code}"
        raise JupiterError(str(message)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JupiterError("Jupiter is unreachable right now.") from exc


def _http_post_json(url: str, payload: dict,
                   headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "solentry-jupiter/1.0",
                 "Content-Type": "application/json",
                 **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        message = (body.get("error") if isinstance(body, dict) else None) \
            or f"Jupiter answered HTTP {exc.code}"
        raise JupiterError(str(message)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JupiterError("Jupiter is unreachable right now.") from exc


def _clear_mint_cache() -> None:
    """Tests only: drop the dynamic mint-resolution cache."""
    _mint_cache.clear()


def wrapped_info(symbol: str) -> dict | None:
    """Static-map wrapped info for a symbol — NO network call:
    {mint, name, decimals} when the coin is one of the verified
    wrapped coins, else None. Used for the coin page's wrapped
    label (the full `resolve_mint` may hit the token API, which a
    page render must not do)."""
    entry = WRAPPED_MINTS.get((symbol or "").upper())
    if entry is None:
        return None
    return {"mint": entry["mint"], "name": entry["name"],
            "decimals": entry["decimals"]}


def _search_token(symbol: str, coin_name: str | None = None) -> dict | None:
    """One verified Solana token for `symbol` from Jupiter's token
    API, or None. Only `isVerified: true` tokens with an exact
    symbol match count — the search also returns junk tokens that
    share a symbol (e.g. several fake "ZEC"s), and those must never
    be picked. When the coin's full name is given, the token's name
    must also contain "wrapped" or that full name (case-insensitive):
    a same-symbol memecoin (e.g. a Solana token just called "ADA")
    is not the native coin and must never be routed to. Highest
    liquidity wins among the survivors."""
    url = (TOKEN_API_BASE + "/search?query="
           + urllib.parse.quote(symbol))
    try:
        raw = _http_get_json(url)
    except JupiterError:
        return None
    tokens = raw if isinstance(raw, list) else raw.get("tokens", [])
    candidates = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if not token.get("isVerified"):
            continue
        if str(token.get("symbol", "")).upper() != symbol.upper():
            continue
        mint = token.get("id")
        decimals = token.get("decimals")
        if not mint or decimals is None:
            continue
        name = str(token.get("name") or "")
        if coin_name:
            name_lower = name.lower()
            if ("wrapped" not in name_lower
                    and coin_name.lower() not in name_lower):
                continue
        try:
            decimals = int(decimals)
        except (TypeError, ValueError):
            continue
        candidates.append({
            "mint": mint,
            "name": name or symbol,
            "decimals": decimals,
            "liquidity": float(token.get("liquidity") or 0.0),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["liquidity"], reverse=True)
    best = candidates[0]
    return {"mint": best["mint"], "name": best["name"],
            "decimals": best["decimals"]}


def resolve_mint(symbol: str, coin_name: str | None = None) -> dict | None:
    """{mint, name, decimals, wrapped} for a coin symbol, or None
    when Jupiter has no Solana token for it. SOL is the input asset
    itself (never wrapped). The four wrapped coins come from the
    verified-live static map; anything else is resolved live through
    Jupiter's token API (cached 1h; a miss caches 10 minutes so a
    bad symbol does not hammer the API on every page load).
    `coin_name` is the coin's full name (e.g. "Cardano") — it keeps
    same-symbol memecoins out of the resolution (see
    `_search_token`)."""
    if not symbol:
        return None
    upper = symbol.upper()
    if upper == "SOL":
        return {"mint": SOL_MINT, "name": "Solana",
                "decimals": SOL_DECIMALS, "wrapped": False}
    if upper in WRAPPED_MINTS:
        entry = WRAPPED_MINTS[upper]
        return {"mint": entry["mint"], "name": entry["name"],
                "decimals": entry["decimals"], "wrapped": True}
    if upper in NATIVE_SOLANA_MINTS:
        entry = NATIVE_SOLANA_MINTS[upper]
        return {"mint": entry["mint"], "name": entry["name"],
                "decimals": entry["decimals"], "wrapped": False}
    now = time.time()
    cached = _mint_cache.get(upper)
    if cached and cached[1] > now:
        return cached[0]
    found = _search_token(upper, coin_name=coin_name)
    # The cache holds the finished dict (with "wrapped"), not the
    # raw search result: a cache hit must return exactly what a
    # cache miss returns, or quote_buy/quote_sell crash on the
    # missing key the second time a coin is resolved.
    result = None
    if found is not None:
        result = {"mint": found["mint"], "name": found["name"],
                  "decimals": found["decimals"], "wrapped": False}
    ttl = (_MINT_CACHE_TTL_SECONDS if result is not None
           else _MINT_NEGATIVE_TTL_SECONDS)
    _mint_cache[upper] = (result, now + ttl)
    return result


def _to_units(amount: float, decimals: int) -> int:
    """A human amount (e.g. 0.1 SOL) in the token's smallest units.
    Rounds to the nearest unit; a non-positive amount is refused by
    the caller, never silently turned into zero."""
    return int(round(float(amount) * (10 ** decimals)))


def _from_units(units: int, decimals: int) -> float:
    return float(units) / (10 ** decimals)


def _parse_quote(raw: dict, side: str, symbol: str,
                 wrapped: bool, display_name: str) -> dict:
    """Validate a raw Jupiter quote response and normalise it into
    the shape the app shows and prices from. Anything malformed —
    an error field, a missing/empty route, a zero output — raises
    instead of producing a route that was never quoted. LIQUIDITY
    RULE (James, 2026-11-11): a route only counts when it has
    tradable liquidity — every hop must move a positive amount.
    A hop with zero in/out is a dead pool and the whole quote is
    refused as no liquid route, never shown as a tradeable route."""
    if not isinstance(raw, dict):
        raise JupiterError("Jupiter answered with a bad quote.")
    if raw.get("error"):
        raise NoRouteError(f"No route found: {raw['error']}")
    route_plan = raw.get("routePlan") or []
    if not route_plan:
        raise NoRouteError("No route found for this trade.")
    try:
        in_amount = int(raw["inAmount"])
        out_amount = int(raw["outAmount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JupiterError("Jupiter answered with a bad quote.") from exc
    if in_amount <= 0 or out_amount <= 0:
        raise NoRouteError("No route found for this trade.")
    route = []
    labels = []
    for hop in route_plan:
        info = hop.get("swapInfo") or {}
        label = info.get("label") or "DEX"
        labels.append(label)
        try:
            hop_in = int(info.get("inAmount") or 0)
            hop_out = int(info.get("outAmount") or 0)
        except (TypeError, ValueError):
            raise NoRouteError(
                "No liquid route found for this trade.")
        if hop_in <= 0 or hop_out <= 0:
            raise NoRouteError(
                "No liquid route found for this trade.")
        route.append({
            "label": label,
            "amm_key": info.get("ammKey"),
            "input_mint": info.get("inputMint"),
            "output_mint": info.get("outputMint"),
            "in_amount": info.get("inAmount"),
            "out_amount": info.get("outAmount"),
        })
    try:
        impact = float(raw.get("priceImpactPct") or 0.0)
    except (TypeError, ValueError):
        impact = 0.0
    return {
        "side": side,
        "symbol": symbol.upper(),
        "wrapped": wrapped,
        "display_name": display_name,
        "input_mint": raw.get("inputMint"),
        "output_mint": raw.get("outputMint"),
        "in_amount": in_amount,
        "out_amount": out_amount,
        "route": route,
        "route_labels": labels,
        "price_impact_pct": impact,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime()),
        "source": "jupiter",
        "raw": raw,
    }


def get_quote(input_mint: str, output_mint: str, amount_units: int,
              slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """One live Jupiter quote for an exact-in swap, normalised.
    Raises JupiterError / NoRouteError — never returns a made-up
    route."""
    if amount_units <= 0:
        raise JupiterError("Amount must be more than 0.")
    base, headers = _swap_base()
    url = (f"{base}/quote?inputMint={input_mint}"
           f"&outputMint={output_mint}&amount={amount_units}"
           f"&slippageBps={int(slippage_bps)}")
    raw = _http_get_json(url, headers)
    return _parse_quote(raw, side="quote", symbol="",
                        wrapped=False, display_name="")


def quote_buy(symbol: str, amount_sol: float,
              slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
              coin_name: str | None = None) -> dict:
    """Live quote: `amount_sol` SOL -> the coin's Solana token.
    Returns the normalised quote (with in/out amounts in both units
    and human form, the route labels, and `fetched_at`)."""
    if amount_sol is None or float(amount_sol) <= 0:
        raise JupiterError("Amount must be more than 0.")
    resolved = resolve_mint(symbol, coin_name=coin_name)
    if resolved is None:
        raise NoRouteError(
            f"No Jupiter route for {symbol} — no Solana token found.")
    if resolved["mint"] == SOL_MINT:
        raise JupiterError(
            "SOL is the coin you swap from — no route needed.")
    units = _to_units(amount_sol, SOL_DECIMALS)
    base, headers = _swap_base()
    raw = _http_get_json(
        f"{base}/quote?inputMint={SOL_MINT}"
        f"&outputMint={resolved['mint']}&amount={units}"
        f"&slippageBps={int(slippage_bps)}", headers)
    quote = _parse_quote(raw, side="buy", symbol=symbol,
                         wrapped=resolved["wrapped"],
                         display_name=resolved["name"])
    quote["in_amount_ui"] = float(amount_sol)
    quote["out_amount_ui"] = _from_units(quote["out_amount"],
                                         resolved["decimals"])
    return quote


def quote_sell(symbol: str, amount_tokens: float,
               slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
               coin_name: str | None = None) -> dict:
    """Live quote: `amount_tokens` of the coin's Solana token -> SOL.
    Returns the normalised quote (out amount in SOL, human form)."""
    if amount_tokens is None or float(amount_tokens) <= 0:
        raise JupiterError("Amount must be more than 0.")
    resolved = resolve_mint(symbol, coin_name=coin_name)
    if resolved is None:
        raise NoRouteError(
            f"No Jupiter route for {symbol} — no Solana token found.")
    if resolved["mint"] == SOL_MINT:
        raise JupiterError(
            "SOL is the coin you swap from — no route needed.")
    units = _to_units(amount_tokens, resolved["decimals"])
    base, headers = _swap_base()
    raw = _http_get_json(
        f"{base}/quote?inputMint={resolved['mint']}"
        f"&outputMint={SOL_MINT}&amount={units}"
        f"&slippageBps={int(slippage_bps)}", headers)
    quote = _parse_quote(raw, side="sell", symbol=symbol,
                         wrapped=resolved["wrapped"],
                         display_name=resolved["name"])
    quote["in_amount_ui"] = float(amount_tokens)
    quote["out_amount_ui"] = _from_units(quote["out_amount"],
                                         SOL_DECIMALS)
    return quote


# Wrapped majors for the buy page's picker: the major coins whose
# native chain is NOT Solana, so their Solana token is a wrapped /
# bridged version. The four in WRAPPED_MINTS are the static wrapped
# map; the other eight resolve through the token API and all quoted
# live on 2026-11-11 (DOGE via BisonFi; LINK, AAVE, UNI, INJ,
# ONDO, SUI and TAO all returned real routes). NOT in this list,
# on purpose: native Solana coins (SOL, BONK, JUP, PEPE, POPCAT —
# and RENDER, whose token is native Solana since the RNDR ->
# RENDER migration) and USDC (a native Solana stablecoin, not a
# wrapped major). Membership here is candidacy only: a coin
# appears in the picker only when a live quote proves its pool is
# liquid right now — never from this list alone.
WRAPPED_MAJOR_SYMBOLS = [
    "BTC", "ETH", "XRP", "ZEC", "DOGE", "LINK", "AAVE", "UNI",
    "INJ", "ONDO", "SUI", "TAO",
]


def liquid_wrapped_majors(
        amount_sol: float = DEFAULT_BUY_SOL) -> tuple:
    """The wrapped majors with a LIVE liquid Jupiter route right
    now, in WRAPPED_MAJOR_SYMBOLS order. Returns (majors,
    any_unverifiable): each major is {coin_id, symbol,
    display_name ("Wrapped BTC"), mint, route_labels,
    price_impact_pct, fetched_at}. A coin whose quote fails with
    NoRouteError (no liquid route / not tradable / dead pool) is
    simply left out; a coin whose quote fails any other way
    (Jupiter unreachable, a bad answer) is unverifiable, not
    "illiquid" — the caller must never present an unverifiable
    list as "no liquid majors". Quotes run in parallel (one
    thread per candidate) so the picker answers in about one
    quote's time instead of twelve. Read-only market data: no
    money moves, no signing."""
    import concurrent.futures

    def _check(symbol: str) -> tuple:
        coin_name = COINS.get(symbol, {}).get("name")
        try:
            quote = quote_buy(symbol, amount_sol,
                              coin_name=coin_name)
        except NoRouteError:
            return (None, False)
        except JupiterError:
            return (None, True)
        return ({
            "coin_id": symbol,
            "symbol": symbol,
            "display_name": f"Wrapped {symbol}",
            "mint": quote["output_mint"],
            "route_labels": quote["route_labels"],
            "price_impact_pct": quote["price_impact_pct"],
            "fetched_at": quote["fetched_at"],
        }, False)

    majors = []
    any_unverifiable = False
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(WRAPPED_MAJOR_SYMBOLS)) as pool:
        futures = {pool.submit(_check, s): s
                   for s in WRAPPED_MAJOR_SYMBOLS}
        for future in concurrent.futures.as_completed(futures):
            try:
                entry, unverifiable = future.result()
            except Exception:
                entry, unverifiable = None, True
            if unverifiable:
                any_unverifiable = True
            if entry is not None:
                majors.append(entry)
    # as_completed scrambles the order: restore candidacy order.
    order = {s: i for i, s in enumerate(WRAPPED_MAJOR_SYMBOLS)}
    majors.sort(key=lambda m: order[m["coin_id"]])
    return majors, any_unverifiable


def summarize_route(quote: dict) -> list:
    """Plain-language lines for the coin page's route box, from a
    normalised quote. Never invents a step the quote did not take."""
    if quote["side"] == "buy":
        first = f"{quote['in_amount_ui']:g} SOL"
        second = (f"~{quote['out_amount_ui']:g} "
                  f"{quote['display_name']}")
    else:
        first = (f"{quote['in_amount_ui']:g} "
                 f"{quote['display_name']}")
        second = f"~{quote['out_amount_ui']:g} SOL"
    path = " -> ".join(quote["route_labels"]) or "Jupiter"
    lines = [
        f"Route: {first} → {second} via {path}",
        f"Price impact: {quote['price_impact_pct']:.4f}%",
        f"Quoted {quote['fetched_at']} (Jupiter, live)",
    ]
    if quote.get("wrapped"):
        lines.append(
            f"{quote['display_name']} is the WRAPPED token on "
            f"Solana — not the native coin on its own chain.")
    return lines


def jupiter_usd_price(symbol: str, sol_price_usd: float | None,
                      amount_sol: float = DEFAULT_BUY_SOL) -> float | None:
    """The coin's implied USD price from a live Jupiter buy quote
    (0.1 SOL by default): USD per token. None when Jupiter cannot
    quote or the SOL price is unknown — the caller falls back to the
    existing live price and never blocks a trade on a quote. The
    rate comes from Jupiter; only the USD denomination comes from
    the app's own SOL price."""
    if sol_price_usd is None or float(sol_price_usd) <= 0:
        return None
    try:
        quote = quote_buy(symbol, amount_sol)
    except JupiterError:
        return None
    if quote["out_amount_ui"] <= 0:
        return None
    return float(amount_sol) * float(sol_price_usd) / quote["out_amount_ui"]


def real_swaps_enabled(cfg=None) -> bool:
    """The mainnet gate: real Jupiter swaps stay OFF unless James
    explicitly enables them (Railway env var JUPITER_REAL_SWAPS=1
    is his go-ahead). Jupiter has no devnet liquidity — a real swap
    is mainnet real money, so the default is closed and there is no
    way to open it from inside the app (no UI toggle). `cfg` is the
    app Config when the caller has it (its jupiter_real_swaps flag
    reads the same env var); without it, the env var is read
    directly."""
    if cfg is not None:
        return bool(getattr(cfg, "jupiter_real_swaps", False))
    return (os.environ.get("JUPITER_REAL_SWAPS") or "").strip() == "1"


def is_jupiter_coin(coin_id: str) -> bool:
    """True when `coin_id` is a listed coin (a symbol in the
    app's coin list) that is a CANDIDATE for Jupiter routing —
    i.e. a real trade of it is dispatched to the Jupiter
    executor, not the pump.fun executor. Pump.fun mints are
    addresses, never symbols, so they never match. SOL is the
    coin you swap from, not a Jupiter destination. Network-free:
    membership in the coin list only, so the trade dispatch
    never waits on an API. This is a routing candidate only —
    it does NOT mean the coin is tradable: tradability is proven
    live by the quote (a real quote with actual liquidity),
    never by this list. A candidate with no liquid route refuses
    with "No liquid route found" inside the executor."""
    if not coin_id or is_valid_address(coin_id):
        return False
    upper = coin_id.upper()
    if upper == "SOL":
        return False
    return upper in COINS


NO_LIQUID_ROUTE = "No liquid route found for this coin."


def no_liquid_route_message(exc: Exception) -> str | None:
    """The plain user-facing message when Jupiter has no liquid
    route for a coin — a NoRouteError, a TOKEN_NOT_TRADABLE (or
    any "not tradable" / "no route") answer — or None when the
    error is something else (Jupiter unreachable, a bad amount),
    which must be reported as itself, never dressed up as a
    liquidity verdict. LIQUIDITY RULE (James, 2026-11-11): only
    a real quote with actual liquidity counts as tradable; a
    wrapped mint merely existing does not."""
    if isinstance(exc, NoRouteError):
        return NO_LIQUID_ROUTE
    msg = str(exc).lower()
    if "not tradable" in msg or "no route" in msg:
        return NO_LIQUID_ROUTE
    return None


def request_real_swap(cfg=None):
    """The real-swap gate check. Raises JupiterRealDisabled while
    the gate is closed — nothing is built, signed or sent. Returns
    True when the gate is open: the caller (jupiter_executor.py)
    then runs the rules-engine check, the bot-limit check, the
    quote, the allowlist and the signing. The gate alone does not
    execute anything — it only permits the executor to run."""
    if not real_swaps_enabled(cfg):
        raise JupiterRealDisabled(
            "Real Jupiter swaps are not enabled yet — Jupiter trades "
            "are mainnet real money, and James has not given the "
            "go-ahead. Paper trades and quotes still work.")
    return True


def fetch_swap_transaction(quote_raw: dict, user_pubkey: str,
                           wrap_and_unwrap_sol: bool = True) -> str:
    """Build (not sign, not send) a Jupiter swap transaction from a
    fresh quote response. Returns the base64-encoded transaction.
    Only ever called behind `request_real_swap`'s gate — it cannot
    run while the gate is closed."""
    base, headers = _swap_base()
    payload = {"quoteResponse": quote_raw,
               "userPublicKey": user_pubkey,
               "wrapAndUnwrapSol": wrap_and_unwrap_sol}
    raw = _http_post_json(f"{base}/swap", payload, headers)
    tx = raw.get("swapTransaction") if isinstance(raw, dict) else None
    if not tx:
        raise JupiterError(
            "Jupiter did not build a swap transaction.")
    return tx
