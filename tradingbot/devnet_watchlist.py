"""DEVNET-TEST-ONLY watchlist for the silent loop (devnet proof).

The silent loop's trenches candidates come only from DexScreener,
which indexes mainnet tokens — and the executor can only buy
tokens with a devnet bonding curve. Those two sets are disjoint,
so as built the loop can never produce a real devnet trade: every
candidate ends in "no bonding curve for this token on devnet".

This module closes that gap for TESTING ONLY: on devnet, the
loop also watches a small list of devnet mints (Gate + AuditTest),
priced from their devnet bonding-curve quotes x the live SOL
price, with gains computed from the loop's own price marks
(scanner-style — DexScreener has no 5-minute change for them).

HARD DEVNET GATE — REMOVE BEFORE MAINNET: the watchlist is active
only when the app's NETWORK is "devnet". On any other network
`active_mints` returns [] and `enrich_scan` returns the scan
untouched: zero candidates, no curve pricing, no filter carve-out.
The mints themselves are devnet test tokens and must never be
candidates on mainnet. Every function here fails closed — a
missing/unreadable/complete curve, a missing SOL price, or any
RPC error means that mint is simply not a candidate this scan.

Nothing in this module signs, sends, or executes anything — it
only builds scan entries. Execution stays in executor.py with
all its existing checks (devnet-only, curve must exist and be
incomplete, real mode only, fee reserve, slippage).
"""

from __future__ import annotations

from .config import DEVNET_TRENCH_MINTS_DEFAULT
from .pumpfun import (TOKEN_BASE, bonding_curve_pda, parse_bonding_curve)

# Display labels for the built-in devnet test mints (their real
# on-chain name/symbol — DexScreener cannot supply them).
WATCHLIST_LABELS = {
    "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE": ("GATE", "Gate"),
    "3k6ZYa2Wh6P92VN2uGvKsmHiw2RQpcVgxpb447raf2kC": ("AUDT", "AuditTest"),
}

LAMPORTS_PER_SOL = 1_000_000_000


def active_mints(cfg) -> list:
    """Watchlist mints that may be candidates — [] on any network
    other than devnet. This is the hard gate: the watchlist is
    devnet-test-only scaffolding and is completely inert on
    mainnet."""
    if getattr(cfg, "network", "") != "devnet":
        return []
    mints = getattr(cfg, "devnet_trench_mints", None)
    if mints is None:
        # A Config built before the watchlist existed: the built-in
        # devnet test mints. An explicitly empty list means the
        # operator listed none — that stays empty.
        mints = list(DEVNET_TRENCH_MINTS_DEFAULT)
    return list(mints)


def curve_price_usd(client, mint: str, sol_price_usd: float):
    """Spot USD price for a devnet mint, from its bonding curve.

    Returns (price_usd, curve) or (None, None). The spot price is
    the curve's marginal price — virtual SOL reserves over virtual
    token reserves, converted to SOL per whole token and priced in
    USD — the same reserves the executor's buy_quote reads, so the
    loop's price and the on-chain quote can never drift apart by
    construction. A missing/unreadable/complete curve, a bad SOL
    price, or any RPC error means (None, None): not a candidate.
    """
    try:
        sol_price = float(sol_price_usd)
    except (TypeError, ValueError):
        return None, None
    if not sol_price or sol_price <= 0:
        return None, None
    try:
        curve_addr, _ = bonding_curve_pda(mint)
        info = client.get_account_info(curve_addr)
    except Exception:
        return None, None
    if not info or not info.get("data"):
        return None, None
    try:
        curve = parse_bonding_curve(info["data"])
    except (ValueError, TypeError):
        return None, None
    if curve.get("complete"):
        return None, None
    vt = curve.get("virtual_token_reserves") or 0
    vs = curve.get("virtual_sol_reserves") or 0
    if vt <= 0 or vs <= 0:
        return None, None
    # SOL per whole token: (lamports per base unit) x (base units
    # per token / lamports per SOL) = vs/vt x 1e6/1e9.
    price_sol = (vs / vt) * (TOKEN_BASE / LAMPORTS_PER_SOL)
    if price_sol <= 0:
        return None, None
    return price_sol * sol_price, curve


def watchlist_entry(client, mint: str, sol_price_usd: float):
    """One scan entry for a devnet watchlist mint, shaped like a
    trenches feed entry (see trenches._entry). None when the mint
    cannot be priced from its curve this scan.

    DexScreener-only fields stay None on purpose — the buy-filter
    carve-out (scanner.passes_buy_filter, devnet_watchlist_active)
    handles them. Curve-derived fields are filled in: the real SOL
    sitting in the curve (the volume check's replacement) and the
    market cap from the curve's total supply.
    """
    price_usd, curve = curve_price_usd(client, mint, sol_price_usd)
    if price_usd is None or curve is None:
        return None
    symbol, name = WATCHLIST_LABELS.get(mint, (mint[:8], ""))
    try:
        sol_price = float(sol_price_usd)
    except (TypeError, ValueError):
        return None
    real_sol = (curve.get("real_sol_reserves") or 0) / LAMPORTS_PER_SOL
    total_supply = curve.get("token_total_supply") or 0
    market_cap_usd = price_usd * (total_supply / TOKEN_BASE) \
        if total_supply > 0 else None
    return {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "price_usd": price_usd,
        # No DexScreener 5-minute change exists for a curve-priced
        # mint — the loop computes the gain from its own price
        # marks (scanner-style) at evaluation time.
        "change_5m_pct": None,
        "change_1h_pct": None,
        "change_6h_pct": None,
        "change_24h_pct": None,
        # Curve liquidity in USD (real SOL in the curve x SOL
        # price) — display only; the buy filter reads
        # curve_real_sol, not this.
        "liquidity_usd": real_sol * sol_price,
        "market_cap_usd": market_cap_usd,
        "dex_id": "pumpfun",
        "pair_address": None,
        "pair_created_at": None,
        "volume_m5_usd": None,
        "volume_h1_usd": None,
        "txns_m5_buys": None,
        "txns_m5_sells": None,
        # Marker for the buy-filter carve-out and the marks-based
        # gain. Only ever set by this module, only on devnet.
        "devnet_watchlist": True,
        # Real SOL in the bonding curve — the curve-derived
        # replacement for the 5-minute-volume check (a funded
        # curve is the devnet equivalent of "real money is in
        # this market").
        "curve_real_sol": real_sol,
    }


def enrich_scan(scan, cfg, rpc_factory, sol_price_usd):
    """Add devnet watchlist mints as scan candidates, priced from
    their bonding curves. Returns a NEW scan dict; the input is
    never mutated.

    Inert unless NETWORK == devnet (see active_mints). Fail-closed
    throughout: no SOL price, no active mints, an errored scan, an
    unreachable RPC, or a mint that cannot be priced all leave the
    scan exactly as it was. A mint DexScreener already reported
    keeps its DexScreener entry — the curve never overwrites it.
    """
    if not isinstance(scan, dict):
        return scan
    if scan.get("error"):
        return scan
    mints = active_mints(cfg)
    if not mints:
        return scan
    try:
        sol_price = float(sol_price_usd)
    except (TypeError, ValueError):
        return scan
    if not sol_price or sol_price <= 0:
        return scan
    try:
        client = rpc_factory(cfg.rpc_url)
    except Exception:
        return scan
    prices = dict(scan.get("prices") or {})
    candidates = list(scan.get("candidates") or [])
    have = {c.get("mint") for c in candidates}
    for mint in mints:
        if mint in prices:
            continue
        try:
            entry = watchlist_entry(client, mint, sol_price)
        except Exception:
            entry = None
        if entry is None:
            continue
        prices[mint] = entry
        if mint not in have:
            candidates.append(entry)
            have.add(mint)
    out = dict(scan)
    out["prices"] = prices
    out["candidates"] = candidates
    return out
