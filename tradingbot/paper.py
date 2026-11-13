"""Paper-trading ledger operations, shared by the HTTP routes and the
silent-trading loop so both use exactly one implementation.

These functions only touch the paper portfolio / holdings / trades
tables. They never touch real money. Amounts are validated by the
caller (routes) or by the loop; the functions raise ValueError with a
user-facing message when the ledger itself refuses (not enough cash,
not enough holding).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .ledger import record_trade


class InsufficientCash(ValueError):
    pass


class InsufficientHolding(ValueError):
    pass


def paper_cash(db: sqlite3.Connection, user_id: int) -> float:
    row = db.execute(
        "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
        (user_id,)).fetchone()
    return row["cash_usd"] if row else 0.0


def holdings_value(db: sqlite3.Connection, user_id: int,
                   prices: dict) -> float:
    """Current USD value of paper holdings, given {coin: price}. A
    holding with no usable price is valued at its average cost,
    never $0 — a missing price must not shrink the bankroll (and
    with it every buy size taken from it)."""
    rows = db.execute(
        "SELECT coin_id, amount, avg_price FROM paper_holdings "
        "WHERE user_id = ?", (user_id,)).fetchall()
    total = 0.0
    for r in rows:
        price = prices.get(r["coin_id"])
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            total += r["amount"] * price
        else:
            total += r["amount"] * float(r["avg_price"])
    return total


def paper_bankroll(db: sqlite3.Connection, user_id: int,
                   prices: dict) -> float:
    """Cash + holdings value: the bankroll buy-size % is taken from."""
    return paper_cash(db, user_id) + holdings_value(db, user_id, prices)


def reset_paper_balance(db: sqlite3.Connection, user_id: int,
                        new_cash: float) -> None:
    """Reset the paper portfolio to a fresh start at `new_cash` CAD.

    Sets the paper cash to the new amount and clears the paper
    holdings and the paper trade history (positions + P&L history).
    The bot win/loss counts live in trade_ledger, which is NOT
    touched — those stay. Paper only: the paper tables never hold
    real money, and real balances/trades are nowhere near this
    function. Raises ValueError for a non-positive amount.
    """
    if new_cash is None or float(new_cash) <= 0:
        raise ValueError("Paper balance must be more than $0.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO paper_portfolio (user_id, cash_usd, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "cash_usd = excluded.cash_usd, "
        "updated_at = excluded.updated_at",
        (user_id, float(new_cash), now))
    db.execute("DELETE FROM paper_holdings WHERE user_id = ?",
               (user_id,))
    db.execute("DELETE FROM paper_trades WHERE user_id = ?",
               (user_id,))
    db.commit()


def paper_buy(db: sqlite3.Connection, user_id: int, coin: str, usd: float,
              price: float, now: str, bot_id: int | None = None,
              signals: dict | None = None) -> tuple[float, float]:
    """Buy `usd` worth of `coin` at `price`. Returns (amount, usd).

    `bot_id` is the bot that bought (None for a manual buy).
    `signals` is the buy-time signal snapshot from the feed entry
    (m5 gain %, 5-minute volume USD, 5-minute buys/sells, regime) —
    recorded so the losing-buy analysis can show how the coin got
    bought. Both are None for manual buys and for buys that predate
    the filter; the analysis section on the P&L page shows those as unavailable.

    No fee (removed 2026-11-12, James): the full `usd` buys coins
    — cash falls by the full usd and the coins bought are worth
    usd at the fill price.
    """
    if usd <= 0:
        raise ValueError("Amount must be more than 0.")
    cash = paper_cash(db, user_id)
    if usd > cash:
        raise InsufficientCash("Not enough paper cash.")
    amount = usd / price
    db.execute("UPDATE paper_portfolio SET cash_usd = cash_usd - ?, "
               "updated_at = ? WHERE user_id = ?",
               (usd, now, user_id))
    row = db.execute(
        "SELECT amount, avg_price FROM paper_holdings "
        "WHERE user_id = ? AND coin_id = ?",
        (user_id, coin)).fetchone()
    if row:
        total = row["amount"] + amount
        avg = ((row["amount"] * row["avg_price"]) + usd) / total
        db.execute("UPDATE paper_holdings SET amount = ?, avg_price = ? "
                   "WHERE user_id = ? AND coin_id = ?",
                   (total, avg, user_id, coin))
    else:
        db.execute("INSERT INTO paper_holdings (user_id, coin_id, amount, "
                   "avg_price) VALUES (?, ?, ?, ?)",
                   (user_id, coin, amount, price))
    signals = signals or {}
    db.execute("INSERT INTO paper_trades (user_id, coin_id, side, amount, "
               "price, total_usd, created_at, bot_id, buy_m5_pct, "
               "buy_volume_5m_usd, buy_buys_5m, buy_sells_5m, buy_regime) "
               "VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (user_id, coin, amount, price, usd, now, bot_id,
                signals.get("m5_pct"), signals.get("volume_5m_usd"),
                signals.get("buys_5m"), signals.get("sells_5m"),
                signals.get("regime")))
    # Trade ledger (builderBot): every fill gets its own 8-digit
    # trade ID, so a bot's win/loss record can be reviewed.
    record_trade(db, user_id, bot_id, coin, "buy", amount,
                 price, usd, mode="paper", now=now)
    db.commit()
    return amount, usd


def paper_sell(db: sqlite3.Connection, user_id: int, coin: str,
               amount: float, price: float, now: str,
               bot_id: int | None = None,
               exit_reason: str | None = None) -> float:
    """Sell `amount` of `coin` at `price`. Returns the USD received.

    `bot_id` is the bot that sold (None for a manual sell);
    `exit_reason` is why the position exited (sell ladder, trailing
    stop, stop loss, rug sell, top-10 dump, manual sell) — recorded
    so the losing-buy analysis can show it. Both are None for manual
    sells and for sells that predate the recording.

    No fee (removed 2026-11-12, James): the cash received is the
    gross proceeds, and the realised P&L is gross of any fee.
    """
    if amount <= 0:
        raise ValueError("Amount must be more than 0.")
    row = db.execute(
        "SELECT amount, avg_price FROM paper_holdings "
        "WHERE user_id = ? AND coin_id = ?",
        (user_id, coin)).fetchone()
    if not row or row["amount"] < amount:
        raise InsufficientHolding("You don't hold that much.")
    avg_price = float(row["avg_price"]) if row["avg_price"] else 0.0
    gross_usd = amount * price
    usd = gross_usd
    pnl_usd = usd - amount * avg_price
    remaining = row["amount"] - amount
    if remaining <= 0:
        db.execute("DELETE FROM paper_holdings WHERE user_id = ? "
                   "AND coin_id = ?", (user_id, coin))
    else:
        db.execute("UPDATE paper_holdings SET amount = ? "
                   "WHERE user_id = ? AND coin_id = ?",
                   (remaining, user_id, coin))
    db.execute("UPDATE paper_portfolio SET cash_usd = cash_usd + ?, "
               "updated_at = ? WHERE user_id = ?",
               (usd, now, user_id))
    db.execute("INSERT INTO paper_trades (user_id, coin_id, side, amount, "
               "price, total_usd, created_at, bot_id, exit_reason) "
               "VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?)",
               (user_id, coin, amount, price, gross_usd, now, bot_id,
                exit_reason))
    # Trade ledger (builderBot): the sell's P&L vs its average cost
    # is what decides win vs loss for the bot.
    record_trade(db, user_id, bot_id, coin, "sell", amount,
                 price, gross_usd, pnl_usd=pnl_usd,
                 exit_reason=exit_reason, mode="paper",
                 now=now)
    db.commit()
    return usd
