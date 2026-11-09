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
    """Current USD value of paper holdings, given {coin: price}."""
    rows = db.execute(
        "SELECT coin_id, amount FROM paper_holdings WHERE user_id = ?",
        (user_id,)).fetchall()
    return sum(r["amount"] * prices.get(r["coin_id"], 0.0) for r in rows)


def paper_bankroll(db: sqlite3.Connection, user_id: int,
                   prices: dict) -> float:
    """Cash + holdings value: the bankroll buy-size % is taken from."""
    return paper_cash(db, user_id) + holdings_value(db, user_id, prices)


def paper_buy(db: sqlite3.Connection, user_id: int, coin: str, usd: float,
              price: float, now: str) -> tuple[float, float]:
    """Buy `usd` worth of `coin` at `price`. Returns (amount, usd)."""
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
    db.execute("INSERT INTO paper_trades (user_id, coin_id, side, amount, "
               "price, total_usd, created_at) VALUES (?, ?, 'buy', ?, ?, ?, ?)",
               (user_id, coin, amount, price, usd, now))
    db.commit()
    return amount, usd


def paper_sell(db: sqlite3.Connection, user_id: int, coin: str,
               amount: float, price: float, now: str) -> float:
    """Sell `amount` of `coin` at `price`. Returns the USD received."""
    if amount <= 0:
        raise ValueError("Amount must be more than 0.")
    row = db.execute(
        "SELECT amount FROM paper_holdings WHERE user_id = ? AND coin_id = ?",
        (user_id, coin)).fetchone()
    if not row or row["amount"] < amount:
        raise InsufficientHolding("You don't hold that much.")
    usd = amount * price
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
               "price, total_usd, created_at) VALUES (?, ?, 'sell', ?, ?, ?, ?)",
               (user_id, coin, amount, price, usd, now))
    db.commit()
    return usd
