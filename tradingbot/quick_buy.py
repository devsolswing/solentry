"""Trenches quick buy (buy-flow batch 6, James 2026-11-27 ~07:32-07:41,
cap settled ~08:45): one-tap Buy 1 / Buy 2 buttons on each Trenches
badge, both amounts settable by the user in the buy popup, plus a
Sell All Now button that only shows when the coin is held.

The amounts live server-side (quick_buy_amounts, one row per user)
so they follow the user across devices — never in localStorage, never
trusted from the browser: a quick buy posts only its SLOT (1 or 2)
and the route resolves the SOL amount from this table. The badge
labels show the amounts for display only.

Bounds (Remy defaults, awaiting James's call): 0.001-10 SOL per
button. A setting outside that range is refused and the old values
stay — the buy itself is still gated by the wallet balance + the
tiered gas reserve, so a large setting can only ever be refused at
buy time, never overspend.
"""

from __future__ import annotations

# Defaults (James, 2026-11-27 ~07:37): Buy 1 = 0.02 SOL, Buy 2 = 0.05 SOL.
DEFAULT_BUY1_SOL = 0.02
DEFAULT_BUY2_SOL = 0.05

# Bounds on a saved amount (Remy defaults — see the module docstring).
MIN_AMOUNT_SOL = 0.001
MAX_AMOUNT_SOL = 10.0


def get_quick_buy_amounts(db, user_id: int) -> dict:
    """The user's Buy 1 / Buy 2 SOL amounts, or the defaults when
    the user never saved any. Always returns floats."""
    row = db.execute(
        "SELECT buy1_sol, buy2_sol FROM quick_buy_amounts "
        "WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {"buy1_sol": DEFAULT_BUY1_SOL,
                "buy2_sol": DEFAULT_BUY2_SOL}
    try:
        buy1 = float(row["buy1_sol"])
    except (TypeError, ValueError):
        buy1 = DEFAULT_BUY1_SOL
    try:
        buy2 = float(row["buy2_sol"])
    except (TypeError, ValueError):
        buy2 = DEFAULT_BUY2_SOL
    if not (MIN_AMOUNT_SOL <= buy1 <= MAX_AMOUNT_SOL):
        buy1 = DEFAULT_BUY1_SOL
    if not (MIN_AMOUNT_SOL <= buy2 <= MAX_AMOUNT_SOL):
        buy2 = DEFAULT_BUY2_SOL
    return {"buy1_sol": buy1, "buy2_sol": buy2}


def set_quick_buy_amounts(db, user_id: int, buy1, buy2):
    """Save the user's Buy 1 / Buy 2 SOL amounts. Returns
    (amounts_dict, error): on a bad value nothing is saved and the
    old amounts come back with the error. Both values must parse
    as numbers inside 0.001-10 SOL. The error names the box that
    is wrong, so the settings form can say so plainly."""
    try:
        buy1_sol = float(buy1)
    except (TypeError, ValueError):
        return get_quick_buy_amounts(db, user_id), (
            "Buy 1 must be a number.")
    try:
        buy2_sol = float(buy2)
    except (TypeError, ValueError):
        return get_quick_buy_amounts(db, user_id), (
            "Buy 2 must be a number.")
    if not (MIN_AMOUNT_SOL <= buy1_sol <= MAX_AMOUNT_SOL):
        return get_quick_buy_amounts(db, user_id), (
            "Buy 1 must be 0.001–10 SOL.")
    if not (MIN_AMOUNT_SOL <= buy2_sol <= MAX_AMOUNT_SOL):
        return get_quick_buy_amounts(db, user_id), (
            "Buy 2 must be 0.001–10 SOL.")
    db.execute(
        "INSERT INTO quick_buy_amounts "
        "(user_id, buy1_sol, buy2_sol, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "buy1_sol = excluded.buy1_sol, "
        "buy2_sol = excluded.buy2_sol, "
        "updated_at = datetime('now')",
        (user_id, buy1_sol, buy2_sol))
    db.commit()
    return {"buy1_sol": buy1_sol, "buy2_sol": buy2_sol}, None


def held_mints_for_user(db, user_id: int, mode: str) -> set:
    """The mints the user holds in `mode` right now — manual
    holdings plus bot-managed positions, the same total the Sell
    All Now path sells. The badge shows Sell All Now exactly for
    these mints (James, 2026-11-27 ~07:34: no holding, no button)."""
    table = ("paper_holdings" if mode == "paper" else "real_holdings")
    held = set()
    for row in db.execute(
            f"SELECT coin_id FROM {table} WHERE user_id = ? "
            "AND amount > 0", (user_id,)):
        held.add(row["coin_id"])
    for row in db.execute(
            "SELECT coin_id FROM bot_positions WHERE user_id = ? "
            "AND mode = ? AND amount > 0", (user_id, mode)):
        held.add(row["coin_id"])
    return held
