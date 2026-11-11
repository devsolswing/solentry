"""Trading fees: 3.65% on every trade, paid to ONE recipient wallet.

The fee (James, 2026-11-10):
  * 3.65% of every trade, going forward only — past trades are never
    charged, and the fee total starts at zero for everyone at deploy.
  * The fee is taken in SOL for pump.fun trades. Paper trades charge
    the same 3.65% in the paper ledger (deducted from the trade
    value) and record their SOL equivalent at the trade-time SOL
    price; a paper fee recorded without a usable SOL price is
    converted at the live price when displayed, never at a guessed
    rate.
  * The fee goes to ONE wallet for the whole app — the recipient in
    the FEE_RECIPIENT_PUBKEY env var (James's own wallet, set in
    Railway). There is no app-generated Treasury wallet any more:
    the generated wallet is retired (its row is purged at startup
    by db._purge_generated_fee_wallets) and must never receive
    anything. The recipient address is never shown on any page
    and never logged.
  * Real (devnet) trades: the fee travels in the SAME transaction
    as the trade itself — a single System-program transfer of
    exactly the computed fee to the fee recipient, appended after
    the pump.fun buy/sell instruction. Either the whole transaction
    confirms (trade + fee) or neither happens, so a fee is never
    taken from a trade that did not execute, and never taken twice
    for one trade (fee rows are unique per trade signature).
  * The signing bot's destination restriction (executor.py) keeps
    exactly ONE exception for this: a System-program instruction is
    allowed only when it is the fee transfer — one instruction, to
    the fee recipient, for exactly the computed fee. Any other
    transfer, destination, or amount is still refused.
  * Fail-closed: when FEE_RECIPIENT_PUBKEY is unset, empty, or not
    a valid base58 32-byte pubkey, real trades refuse and the
    donate flow refuses — never a fallback wallet, never a guessed
    address.

Mainnet settlement rule (James, 2026-11-10): when the app launches
on mainnet, after selling a coin the proceeds are converted to SOL
and held as SOL — never cash, never USDC. On devnet this is already
how sells settle: pump.fun pays SOL to the seller's wallet, so
there is no cash leg to convert. MAINNET_SETTLEMENT is the rule's
single home in code; the README's "Trading fees" section states it
in plain words.
"""

from __future__ import annotations

from .solana import is_valid_address

# 3.65% — basis points, so fee math is exact integer math in
# lamports (amount * 365 // 10000, rounded down).
FEE_BPS = 365
FEE_PCT = 3.65

# Mainnet settlement: sells convert to SOL and stay in SOL.
MAINNET_SETTLEMENT = "SOL"


def fee_lamports(amount_lamports: int) -> int:
    """3.65% of a lamport amount, integer math, rounded down. Zero
    or negative amounts have no fee."""
    if amount_lamports is None or amount_lamports <= 0:
        return 0
    return int(amount_lamports) * FEE_BPS // 10000


def fee_usd(amount_usd: float) -> float:
    """3.65% of a USD/paper amount. Zero or negative amounts have
    no fee."""
    if amount_usd is None or amount_usd <= 0:
        return 0.0
    return float(amount_usd) * FEE_BPS / 10000.0


def fee_sol_from_usd(fee_usd_amount: float, sol_price_usd) -> float | None:
    """A paper fee's SOL equivalent at the SOL price of the trade, or
    None when no usable SOL price was available — the display layer
    converts at the live price instead, never a guessed rate."""
    if fee_usd_amount is None or fee_usd_amount <= 0:
        return None
    try:
        price = float(sol_price_usd)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return fee_usd_amount / price


def get_fee_recipient_pubkey(cfg) -> str:
    """Return the fee/donation recipient pubkey from the
    FEE_RECIPIENT_PUBKEY env var (Config.fee_recipient_pubkey).

    Raises ValueError when the var is unset, empty, or not a valid
    base58 32-byte Solana address — the caller must fail closed
    (real trades refuse, donations refuse), never fall back to a
    generated wallet or a guessed address. No wallet is ever
    created here; the recipient is configured, not generated."""
    raw = (cfg.fee_recipient_pubkey or "").strip()
    if not raw or not is_valid_address(raw):
        raise ValueError(
            "FEE_RECIPIENT_PUBKEY is not set or not a valid wallet "
            "address")
    return raw


def record_fee(db, user_id: int, bot_id: int | None, coin: str,
               side: str, mode: str, fee_sol: float | None,
               fee_usd: float | None = None,
               fee_lamports: int | None = None,
               signature: str | None = None,
               trade_id: str | None = None,
               now: str | None = None) -> bool:
    """Insert one fee row. Returns True when inserted, False when a
    row with the same signature already exists — a real trade
    recorded twice is one fee, never two. `signature` is None for
    paper trades (no on-chain transaction)."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.execute(
        "INSERT OR IGNORE INTO trade_fees (user_id, bot_id, coin, side, "
        "mode, fee_sol, fee_usd, fee_lamports, signature, trade_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, bot_id, coin, side, mode, fee_sol, fee_usd,
         fee_lamports, signature, trade_id, now))
    db.commit()
    return cur.rowcount == 1


def fee_row_sol(row, sol_price_usd) -> float | None:
    """SOL for one fee row: the recorded fee_sol, else the USD fee
    converted at the given live SOL price, else None (never a
    guessed rate)."""
    if row["fee_sol"] is not None:
        return float(row["fee_sol"])
    if row["fee_usd"] is None:
        return None
    return fee_sol_from_usd(float(row["fee_usd"]), sol_price_usd)


def user_fees(db, user_id: int, limit: int = 50) -> list:
    """The user's fee payments, newest first."""
    rows = db.execute(
        "SELECT id, user_id, bot_id, coin, side, mode, fee_sol, fee_usd, "
        "fee_lamports, signature, trade_id, created_at FROM trade_fees "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _total_sol(rows, sol_price_usd) -> float:
    """Sum fee rows in SOL: each row's recorded SOL, else its USD
    fee converted at the given live SOL price — without a price
    those rows are left out rather than guessed."""
    total = 0.0
    for row in rows:
        sol = fee_row_sol(row, sol_price_usd)
        if sol is not None:
            total += sol
    return total


def user_fee_total_sol(db, user_id: int, sol_price_usd=None) -> float:
    """The user's total fees paid, in SOL. Real fees use their
    recorded SOL; paper fees recorded without a SOL price are
    converted at the given live SOL price — without a price they are
    left out of the total rather than guessed."""
    rows = db.execute(
        "SELECT fee_sol, fee_usd FROM trade_fees WHERE user_id = ?",
        (user_id,)).fetchall()
    return _total_sol(rows, sol_price_usd)


def app_fee_total_sol(db, sol_price_usd=None) -> float:
    """The app-wide total of ALL 3.65% fees collected from every
    user, in SOL — the live balance shown next to the 🇨🇦 flag on
    the homepage. It starts at zero at deploy (no back-charging)
    and counts only fees actually recorded. Same conversion rule
    as the per-user total: a fee recorded without a SOL price is
    converted at the given live SOL price, and left out without
    one, never guessed."""
    rows = db.execute(
        "SELECT fee_sol, fee_usd FROM trade_fees").fetchall()
    return _total_sol(rows, sol_price_usd)
