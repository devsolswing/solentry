"""Price-validation gate for real buys (research report §3, rules
1–2 and 5). A price earns the right to execute only when it passes
these checks:

1. Freshness — a decision price older than
   `decision_price_max_age_seconds` (default 30s) is stale: new buys
   refuse on it. Exits may decide on the last good price, but the
   executor re-quotes before selling, so this gate only guards buys.
   When the caller cannot say how old the price is (age None), the
   freshness check is skipped — the caller is then responsible for
   the bound (the manual-trade route resolves its price fresh
   in-request; the silent loop passes the scan age). The skip is
   recorded in the returned details, never silently treated as fresh.
2. Same-source consistency — immediately before a real buy, the
   decision price (the Helius stream's latest tick for this mint)
   must agree with a fresh on-chain read of the same bonding curve
   within `two_source_tolerance_pct` (default 5%, a Remy default
   awaiting James's call). The two reads are the same source
   moments apart — the stream tick and this fresh read — so a
   disagreement means the price moved since the tick, not that two
   sources disagree: the buy refuses rather than executing on a
   moved price. The fill price is never the decision price — the
   executor re-reads the curve inside the trade call.

There is no third check any more. The old check 3 (SOL-price
cross-check: Jupiter vs CoinGecko disagreeing by more than
`sol_price_disagree_pct`) is gone with the single-price source
(2026-11-25): SOL has one source (Jupiter) and one number (the
live feed's SOL tick), so there is nothing to cross-check against
— and the cross-check was refusing real trades whenever the two
SOL prints drifted apart. What guards a bad SOL quote now is the
feed's tick validation on the SOL tick itself (the 50% single-tick
jump guard and the 3-agreeing-tick re-base in livefeed.ingest):
a bad SOL print is rejected and the last good SOL price stands;
a SOL tick older than 90s stops the stream ingesting rather than
converting at a stale rate.

Every refusal is a plain (ok=False, reason) — the caller logs it as
a skip, never a fill.
"""

from __future__ import annotations

from .pumpfun import (TOKEN_BASE, bonding_curve_pda,
                      parse_bonding_curve)
from .solana import LAMPORTS_PER_SOL


def curve_price_sol_per_token(curve: dict) -> float | None:
    """The bonding curve's marginal price: SOL per whole token, from
    the virtual reserves (virtual SOL / virtual tokens). None for a
    degenerate curve. This is the price a tiny buy would pay — the
    right comparison for a feed (last-trade) price."""
    try:
        vt = float(curve["virtual_token_reserves"])
        vs = float(curve["virtual_sol_reserves"])
    except (KeyError, TypeError, ValueError):
        return None
    if vt <= 0 or vs <= 0:
        return None
    # vs is lamports, vt is base units (6 decimals): SOL per token
    # = (vs / LAMPORTS_PER_SOL) / (vt / TOKEN_BASE).
    return (vs / LAMPORTS_PER_SOL) / (vt / TOKEN_BASE)


def check_entry_price(rpc, cfg, mint: str, decision_price_usd,
                      decision_price_age_seconds=None,
                      sol_price_primary=None,
                      curve=None) -> tuple:
    """(ok, reason, details) for a real buy of `mint` at
    `decision_price_usd` (the Helius stream's latest tick for the
    mint — the one price). `curve` is an already-read parsed
    bonding curve (the executor passes the one it just read);
    otherwise the curve is read fresh here. `sol_price_primary`
    is the SOL price in USD (the live feed's SOL tick — the one
    SOL number); when None it is fetched here. A refusal always
    names the failed check; `details` carries the numbers for
    the log."""
    details: dict = {"decision_price_usd": decision_price_usd,
                     "age_checked": decision_price_age_seconds is not None}
    try:
        decision = float(decision_price_usd)
    except (TypeError, ValueError):
        return False, "no decision price for this buy", details
    if decision <= 0:
        return False, "no decision price for this buy", details
    if decision_price_age_seconds is not None:
        try:
            age = float(decision_price_age_seconds)
        except (TypeError, ValueError):
            age = None
        if age is not None and age > float(
                cfg.decision_price_max_age_seconds):
            return False, (f"decision price is stale ({age:.0f}s old, "
                           f"older than "
                           f"{cfg.decision_price_max_age_seconds:.0f}s)"), \
                details
    if curve is None:
        try:
            info = rpc.get_account_info(bonding_curve_pda(mint)[0])
        except Exception:
            return False, ("bonding curve unavailable for the "
                           "price check"), details
        if not info:
            return False, ("bonding curve unavailable for the "
                           "price check"), details
        try:
            curve = parse_bonding_curve(info["data"])
        except ValueError:
            return False, ("bonding curve unreadable for the "
                           "price check"), details
    if curve.get("complete"):
        return False, ("bonding curve complete — this coin trades "
                       "through Jupiter, not the pump.fun executor"), \
            details
    if sol_price_primary is None:
        try:
            from .prices import get_sol_price_usd
            sol_price_primary = get_sol_price_usd()
        except Exception:
            sol_price_primary = None
    if sol_price_primary is None or float(sol_price_primary) <= 0:
        return False, "SOL price unavailable for the price check", details
    onchain_sol = curve_price_sol_per_token(curve)
    if onchain_sol is None:
        return False, ("bonding curve price unreadable for the "
                       "price check"), details
    onchain_usd = onchain_sol * float(sol_price_primary)
    details["onchain_price_usd"] = onchain_usd
    details["sol_price_usd"] = float(sol_price_primary)
    if onchain_usd <= 0:
        return False, ("bonding curve price unreadable for the "
                       "price check"), details
    diff_pct = abs(decision - onchain_usd) / onchain_usd * 100.0
    details["two_source_diff_pct"] = diff_pct
    if diff_pct > float(cfg.two_source_tolerance_pct):
        return False, (f"stream price ${decision:.6g} moved away from "
                       f"the on-chain price ${onchain_usd:.6g} "
                       f"({diff_pct:.1f}% apart, tolerance "
                       f"{cfg.two_source_tolerance_pct:.0f}%) — the "
                       f"price moved since the tick"), details
    return True, None, details


def check_quote_vs_decision(quote_out_amount: float, input_sol: float,
                            decision_price_usd, sol_price_usd,
                            tolerance_pct: float) -> tuple:
    """Two-source check for a graduated coin (Jupiter path): the
    fresh quote's implied price (SOL in / tokens out) against the
    decision price, in SOL terms so a wrong SOL price cancels out.
    (ok, reason). Skipped (ok=True) when any input is missing — the
    caller records that, never a guessed agreement."""
    try:
        out = float(quote_out_amount)
        spent = float(input_sol)
        decision = float(decision_price_usd)
        sol = float(sol_price_usd)
    except (TypeError, ValueError):
        return True, None
    if out <= 0 or spent <= 0 or decision <= 0 or sol <= 0:
        return True, None
    # Implied fill price, SOL per token: spent SOL / tokens out.
    # Decision price in the same units: decision USD / SOL USD.
    implied_sol = spent / out
    decision_sol = decision / sol
    if decision_sol <= 0:
        return True, None
    diff_pct = abs(decision_sol - implied_sol) / implied_sol * 100.0
    if diff_pct > float(tolerance_pct):
        return False, (f"decision price disagrees with the fresh "
                       f"Jupiter quote ({diff_pct:.1f}% apart, "
                       f"tolerance {float(tolerance_pct):.0f}%)")
    return True, None
