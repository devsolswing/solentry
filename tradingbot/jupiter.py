"""Jupiter DEX aggregation for GRADUATED pump.fun coins: quotes and
the gated real-swap path — by mint address, never by symbol.

The app trades pump.fun coins only. A coin still on its bonding
curve trades through the pump.fun executor (tradingbot/executor.py).
A coin that GRADUATED (bonding curve complete, migrated to Raydium)
trades through Jupiter: this module quotes SOL <-> the coin's mint
and (behind the mainnet gate) builds the swap transaction. There is
no majors quoting any more: no wrapped-majors picker, no
"Finding route…" box, no symbol resolution, no 36-coin list.

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
Do NOT "fix" these URLs back to quote-api.jup.ag — that host is
retired and every call to it fails.

WHAT THIS DOES:
- `quote_buy_mint` / `quote_sell_mint`: fetch a live Jupiter quote
  for a mint address. The quote IS the scan of the pools SOL is
  paired with: Jupiter routes across every DEX/pool it integrates
  and returns the route plan (which pools, in which order, with
  what output). A quote is never cached and never presented as
  fresh after the fact: every quote carries `fetched_at` (the time
  it was fetched). Pump.fun mints are 6 decimals by construction
  (see tradingbot/pumpfun.py TOKEN_DECIMALS), so quotes default to
  6 — `mint_decimals` reads the real decimals from the chain when
  the caller needs them verified.
- Paper trades price from the quote (see `jupiter_usd_price_mint`):
  the fill rate comes from Jupiter, only the USD denomination comes
  from the app's own SOL price. When Jupiter is unreachable the
  caller falls back to the existing live price — a paper trade is
  never blocked by a quote failure.
- Real swaps are GATED (see `request_real_swap`): Jupiter has no
  devnet liquidity, so a real Jupiter swap is mainnet real money.
  The gate stays closed until James gives an explicit mainnet
  go-ahead. Until then any real-swap request refuses plainly and
  nothing is built, signed or sent. There is no trading fee on any
  swap (fees were removed entirely 2026-11-13).

FUTURE SIGNING — BUILT (2026-11-11, jupiter_executor.py):
The real swap path is complete but GATED OFF by default
(JUPITER_REAL_SWAPS=1 + NETWORK=mainnet). When the gate opens,
the signing bot signs the Jupiter-built swap transaction after
an allowlist check (Jupiter aggregator program + the swap
instruction only, plus the Token/ATA/System/Compute-Budget
instructions a swap needs — each validated, never arbitrary
instructions), within the bot's limits and only when the rules
engine allows it. Jupiter builds the swap transaction as a
versioned (v0) transaction with address lookup tables, and the
lite API rejects `asLegacyTransaction` (verified live 2026-11-11:
HTTP 400 / timeout), so the swap bytes are used exactly as
Jupiter built them.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .solana import SolanaRpcClient, is_valid_address

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


def mint_decimals(rpc_url: str, mint: str) -> int:
    """A mint's decimals, read from the chain (byte 44 of the mint
    account: mint_authority option+key (36) + supply u64 (8) +
    decimals u8). Raises JupiterError when the mint account cannot
    be read — the caller refuses plainly rather than quoting at a
    guessed decimal count. Pump.fun mints are 6 by construction
    (see tradingbot/pumpfun.py TOKEN_DECIMALS), so callers that
    already know the coin is a pump.fun mint pass decimals=6
    instead of paying for this read on every quote."""
    if not is_valid_address(mint):
        raise JupiterError("Not a valid token address.")
    try:
        client = SolanaRpcClient(rpc_url)
        info = client.get_account_info(mint)
    except Exception as exc:
        raise JupiterError(
            "Could not read the token's decimals from the chain."
            ) from exc
    if not info or not info.get("data") or len(info["data"]) < 45:
        raise JupiterError(
            "Could not read the token's decimals from the chain.")
    return int(info["data"][44])


def quote_buy_mint(mint: str, amount_sol: float,
                   decimals: int = 6,
                   slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """Live quote: `amount_sol` SOL -> the coin at `mint` (a
    graduated pump.fun coin). Returns the normalised quote (with
    in/out amounts in both units and human form, the route labels,
    and `fetched_at`). `decimals` is the mint's decimal count —
    6 for every pump.fun mint by construction; pass
    `mint_decimals(rpc_url, mint)` when it must be verified from
    the chain."""
    if amount_sol is None or float(amount_sol) <= 0:
        raise JupiterError("Amount must be more than 0.")
    if not is_valid_address(mint):
        raise JupiterError("Not a valid token address.")
    if mint == SOL_MINT:
        raise JupiterError(
            "SOL is the coin you swap from — no route needed.")
    units = _to_units(amount_sol, SOL_DECIMALS)
    base, headers = _swap_base()
    raw = _http_get_json(
        f"{base}/quote?inputMint={SOL_MINT}"
        f"&outputMint={mint}&amount={units}"
        f"&slippageBps={int(slippage_bps)}", headers)
    quote = _parse_quote(raw, side="buy", symbol=mint,
                         wrapped=False, display_name=mint)
    quote["in_amount_ui"] = float(amount_sol)
    quote["out_amount_ui"] = _from_units(quote["out_amount"], decimals)
    return quote


def quote_sell_mint(mint: str, amount_tokens: float,
                    decimals: int = 6,
                    slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """Live quote: `amount_tokens` of the coin at `mint` (a graduated
    pump.fun coin) -> SOL. Returns the normalised quote (out amount
    in SOL, human form)."""
    if amount_tokens is None or float(amount_tokens) <= 0:
        raise JupiterError("Amount must be more than 0.")
    if not is_valid_address(mint):
        raise JupiterError("Not a valid token address.")
    if mint == SOL_MINT:
        raise JupiterError(
            "SOL is the coin you swap from — no route needed.")
    units = _to_units(amount_tokens, decimals)
    base, headers = _swap_base()
    raw = _http_get_json(
        f"{base}/quote?inputMint={mint}"
        f"&outputMint={SOL_MINT}&amount={units}"
        f"&slippageBps={int(slippage_bps)}", headers)
    quote = _parse_quote(raw, side="sell", symbol=mint,
                         wrapped=False, display_name=mint)
    quote["in_amount_ui"] = float(amount_tokens)
    quote["out_amount_ui"] = _from_units(quote["out_amount"],
                                         SOL_DECIMALS)
    return quote


def jupiter_usd_price_mint(mint: str, sol_price_usd: float | None,
                           amount_sol: float = DEFAULT_BUY_SOL,
                           decimals: int = 6) -> float | None:
    """The coin at `mint`'s implied USD price from a live Jupiter
    buy quote (0.1 SOL by default): USD per token. None when
    Jupiter cannot quote or the SOL price is unknown — the caller
    falls back to the existing live price and never blocks a trade
    on a quote. The rate comes from Jupiter; only the USD
    denomination comes from the app's own SOL price."""
    if sol_price_usd is None or float(sol_price_usd) <= 0:
        return None
    try:
        quote = quote_buy_mint(mint, amount_sol, decimals=decimals)
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
