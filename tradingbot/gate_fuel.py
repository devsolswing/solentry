"""Holdings-funded $GATE fuel + the Buy $GATE quote (2026-11-22).

The bundle model is SUPERSEDED (James, 2026-11-22 — "we're
changing that"): bundles bought up front to the dev wallet are
never pushed (see ~/workspace/gate-fuel-staging/, held, do not
reuse or import). Holdings-funded fuel replaces it:

- The Buy $GATE button buys $GATE to HOLD in the user's wallet,
  like any other coin (presets 1M / 5M / 10M at the live curve
  price, or a custom SOL amount).
- The wallet page's "fuel trades with Gate" button enrolls held
  $GATE as fuel. Each confirmed executed REAL trade consumes
  exactly 10,000 $GATE: trades left = floor(held $GATE / 10,000).
- The trade gate reads the CURRENT held balance live before every
  real trade — below 10,000 it refuses/skips, at or above it
  proceeds and 10,000 is deducted after the trade confirms. Not a
  latch, no manual re-enable: topping up (buying more $GATE)
  resumes trading automatically. Paper trades stay free and never
  consume fuel.

Drawdown is accounting, not an on-chain transfer (Remy default,
awaiting James's call): one fuel-use row per consumed trade, keyed
by the confirmed trade signature (UNIQUE, so a retry can never
double-spend); the fuel balance is held minus consumed. The $GATE
itself stays in the wallet — the user can still sell it, and then
the held balance (the chain) drops and fuel drops with it. The
chain is the truth for the held amount (same as the wallet page's
reconciled holdings); the trade ledger is the truth for which
trades executed. A trade with no signature is never consumed
(the signature is the key).

Two reads, deliberately different:
- The TRADE GATE (fuel_gate_check) reads the live chain balance
  on EVERY trade — never cached, per James's instruction.
- The DISPLAY counter (cached_fuel_trades_left, the trades-left
  chip in the topbar) is cached 60 seconds per wallet, the same
  pattern as the header SOL balance — a display value, never a
  trade decision.
"""
from __future__ import annotations

import time

from .pumpfun import (TOKEN_BASE, bonding_curve_pda, buy_quote,
                      curve_progress_pct, global_pda, parse_bonding_curve,
                      parse_global, sol_for_tokens)
from .solana import lamports_to_sol, sol_to_lamports

# Exactly 10,000 $GATE per confirmed executed real trade.
FUEL_PER_TRADE_TOKENS = 10_000
# Buy $GATE presets: exact token amounts (whole $GATE).
GATE_PRESETS = (1_000_000, 5_000_000, 10_000_000)
# Devnet Gate mint (the app runs on devnet now) and the mainnet
# $GATE mint (only ever used on mainnet — see effective_gate_mint).
GATE_MINT_DEVNET = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_MINT_MAINNET = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"

# Display-counter cache: 60s per (wallet, mint). The trade gate
# never uses this cache.
_FUEL_COUNTER_CACHE: dict = {"at": {}, "value": {}}
_FUEL_COUNTER_TTL_SECONDS = 60.0


def clear_fuel_counter_cache() -> None:
    """Drop the display-counter cache (tests reset it)."""
    _FUEL_COUNTER_CACHE["at"].clear()
    _FUEL_COUNTER_CACHE["value"].clear()


def effective_gate_mint(cfg) -> str | None:
    """The mint the Buy $GATE button buys, or None when no mint is
    buyable. Devnet: GATE_MINT (env) or the devnet default — the
    mainnet mint is never used while NETWORK=devnet. Mainnet: the
    mainnet mint ONLY when the existing mainnet gate is open
    (JUPITER_REAL_SWAPS=1 AND NETWORK=mainnet); the devnet default
    is never buyable on mainnet. Anything else fails closed
    (None). This is the BUY path only — the fuel counter and the
    trade gate read HELD $GATE, which does not depend on whether
    more can be bought (see held_gate_mint)."""
    if cfg.network == "mainnet":
        if not cfg.jupiter_real_swaps:
            return None
        mint = (cfg.gate_mint or "").strip()
        if not mint or mint == GATE_MINT_DEVNET:
            return GATE_MINT_MAINNET
        return mint
    mint = (cfg.gate_mint or "").strip()
    if not mint or mint == GATE_MINT_MAINNET:
        return None
    return mint


def held_gate_mint(cfg) -> str | None:
    """The mint whose HELD balance counts as fuel — for the
    trades-left counter and the trade gate. Fuel is holdings-based:
    it must work on mainnet whether or not buying more $GATE is
    currently possible, so this deliberately does NOT require the
    JUPITER_REAL_SWAPS gate that effective_gate_mint (the Buy
    $GATE path) needs. Mainnet: GATE_MINT (env) unless it is the
    devnet mint, else the mainnet mint. Devnet: GATE_MINT unless
    it is the mainnet mint. None only when no mint is configured
    at all."""
    mint = (cfg.gate_mint or "").strip()
    if cfg.network == "mainnet":
        if mint and mint != GATE_MINT_DEVNET:
            return mint
        return GATE_MINT_MAINNET
    if mint and mint != GATE_MINT_MAINNET:
        return mint
    return GATE_MINT_DEVNET


def read_gate_curve(rpc, mint: str):
    """The live bonding curve + program fee_bps for `mint`, or
    (None, None) when either account cannot be read — the caller
    fails closed. Same read pattern as the executor."""
    try:
        global_info = rpc.get_account_info(global_pda()[0])
        if not global_info or not global_info.get("data"):
            return None, None
        fee_bps = int(parse_global(global_info["data"])
                      ["fee_basis_points"])
        curve_info = rpc.get_account_info(bonding_curve_pda(mint)[0])
        if not curve_info or not curve_info.get("data"):
            return None, None
        curve = parse_bonding_curve(curve_info["data"])
    except Exception:
        return None, None
    return curve, fee_bps


def gate_buy_quote_for_curve(curve, fee_bps: int, mint: str) -> dict:
    """Quote dict for a curve already read: the SOL cost of each
    exact preset token amount (curve integral, via sol_for_tokens)
    plus the curve progress. A preset too big for the curve
    reports sol=None individually, never a guessed price."""
    presets = []
    for tokens in GATE_PRESETS:
        lamports = sol_for_tokens(curve, fee_bps, tokens * TOKEN_BASE)
        presets.append({
            "tokens": tokens,
            "sol": (lamports_to_sol(lamports)
                    if lamports is not None else None),
        })
    return {"mint": mint, "presets": presets,
            "fee_bps": fee_bps,
            "curve_progress_pct": curve_progress_pct(curve)}


def gate_buy_quote(rpc, mint: str) -> dict | None:
    """Live quote for the Buy $GATE screen (curve read + quote).
    Returns None when the curve cannot be read or is complete
    (migrated) — the screen then shows "price unavailable" and
    disables buying."""
    curve, fee_bps = read_gate_curve(rpc, mint)
    if curve is None or curve.get("complete"):
        return None
    return gate_buy_quote_for_curve(curve, fee_bps, mint)


def tokens_out_for_sol(curve, fee_bps: int, sol: float) -> float | None:
    """Estimated whole $GATE out for `sol` SOL in, at the live
    curve (the custom-SOL estimate on the Buy $GATE screen).
    None for an invalid amount."""
    try:
        amount = float(sol)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return buy_quote(curve, fee_bps, sol_to_lamports(amount)) / TOKEN_BASE


def held_gate_tokens_from_chain(rpc, pubkey: str,
                                mint: str) -> float | None:
    """Whole $GATE the wallet actually holds on-chain, or None
    when the balance cannot be read. The chain is the truth for
    the held amount — never the app's DB mirror (which only
    records manual trades)."""
    if rpc is None or not pubkey or not mint:
        return None
    try:
        from .home_holdings import fetch_chain_token_balances
        balances = fetch_chain_token_balances(rpc, pubkey)
    except Exception:
        return None
    for row in balances or []:
        if row.get("mint") == mint:
            try:
                return float(row.get("ui_amount") or 0.0)
            except (TypeError, ValueError):
                return None
    return 0.0


def is_fuel_enrolled(db, user_id: int) -> bool:
    """Has this user tapped "fuel trades with Gate" (opt-in)?"""
    row = db.execute(
        "SELECT enrolled FROM gate_fuel_enrollment WHERE user_id = ?",
        (user_id,)).fetchone()
    return bool(row and row["enrolled"])


def set_fuel_enrolled(db, user_id: int, enrolled: bool) -> None:
    """Record the wallet-page fuel opt-in (or opt-out)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO gate_fuel_enrollment "
        "(user_id, enrolled, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET enrolled = excluded.enrolled, "
        "updated_at = excluded.updated_at",
        (user_id, 1 if enrolled else 0, now))
    db.commit()


def consumed_gate_fuel(db, user_id: int) -> float:
    """$GATE already consumed as fuel by this user (sum of the
    fuel-use records)."""
    row = db.execute(
        "SELECT COALESCE(SUM(tokens), 0) AS total FROM gate_fuel_uses "
        "WHERE user_id = ?", (user_id,)).fetchone()
    try:
        return float(row["total"]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def fuel_remaining_tokens(db, user_id: int, held: float) -> float:
    """Fuel left: held $GATE minus consumed, never below zero."""
    try:
        held = float(held)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, held - consumed_gate_fuel(db, user_id))


def fuel_trades_left(db, user_id: int, held: float) -> int:
    """Trades left = floor(held $GATE / 10,000), after consumed."""
    return int(fuel_remaining_tokens(db, user_id, held)
               // FUEL_PER_TRADE_TOKENS)


def consume_gate_fuel(db, user_id: int, signature) -> bool:
    """Record 10,000 $GATE consumed for one confirmed executed
    trade, keyed by its signature. Returns True when a new row was
    recorded, False when nothing was consumed (no signature, not
    enrolled, or the signature was already consumed — a retry can
    never double-spend). Never raises: a bookkeeping failure must
    never break a trade that already executed."""
    if not signature or not is_fuel_enrolled(db, user_id):
        return False
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            "INSERT OR IGNORE INTO gate_fuel_uses "
            "(user_id, signature, tokens, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, str(signature), FUEL_PER_TRADE_TOKENS, now))
        db.commit()
        return cur.rowcount == 1
    except Exception:
        return False


def fuel_gate_check(db, cfg, rpc, pubkey: str,
                    user_id: int) -> tuple[bool, str | None,
                                           float | None, int | None]:
    """The trade gate — LIVE balance, never cached. Returns
    (ok, reason, held, trades_left). Not enrolled: the gate is
    open (True, None, None, None) — today's behaviour, no fuel
    touched. Enrolled: the chain balance is read fresh; below
    10,000 (or unreadable, or no buyable mint) the trade is
    refused with a plain reason. At or above 10,000 the trade
    proceeds."""
    if not is_fuel_enrolled(db, user_id):
        return True, None, None, None
    mint = held_gate_mint(cfg)
    if mint is None:
        return (False, "Fuel is on but no $GATE mint is configured "
                "on this network — real trades are stopped.", None, None)
    if not pubkey:
        return (False, "No app wallet — real trades need a wallet.",
                None, None)
    held = held_gate_tokens_from_chain(rpc, pubkey, mint)
    if held is None:
        return (False, "Fuel balance unavailable — the trade is "
                "refused rather than guessed.", None, None)
    left = fuel_trades_left(db, user_id, held)
    if left < 1:
        return (False,
                f"No fuel — held $GATE is {held:,.0f}, below the "
                f"10,000 $GATE needed for one real trade. Buy more "
                f"$GATE to resume trading.", held, 0)
    return True, None, held, left


def cached_fuel_trades_left(db, cfg, user_id: int,
                            pubkey: str) -> int | None:
    """Trades left for the topbar chip — DISPLAY ONLY, cached 60s
    per wallet (same pattern as the header SOL balance). Never
    used for a trade decision. None when the balance cannot be
    read (the chip hides — a display failure never blocks a
    trade). The mint is the HELD mint (held_gate_mint): the counter
    works on mainnet whether or not buying more $GATE is possible,
    and the read uses the failover RPC client so one 429 on the
    primary does not hide the chip."""
    mint = held_gate_mint(cfg)
    if mint is None or not pubkey:
        return None
    key = (pubkey, mint)
    now = time.time()
    if (key in _FUEL_COUNTER_CACHE["value"]
            and now - _FUEL_COUNTER_CACHE["at"].get(key, 0)
            < _FUEL_COUNTER_TTL_SECONDS):
        return _FUEL_COUNTER_CACHE["value"][key]
    try:
        from .solana import rpc_client_for_config
        rpc = rpc_client_for_config(cfg)
        held = held_gate_tokens_from_chain(rpc, pubkey, mint)
    except Exception:
        held = None
    value = (fuel_trades_left(db, user_id, held)
             if held is not None else None)
    _FUEL_COUNTER_CACHE["at"][key] = now
    _FUEL_COUNTER_CACHE["value"][key] = value
    return value
