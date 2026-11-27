"""Stop loss / take profit on hand-bought (User) holdings.

A User buy has no bot watching it, so nothing enforces an exit —
these rows are the enforcement. One row per (user, coin, mode):
the % the user typed at buy time plus the trigger prices in USD,
computed from the EXECUTED average cost (never the mark price at
buy time). The silent loop checks these rows every pass, even
when no bot is running, and sells the whole holding when the
live price reaches a trigger. A row is deleted when its holding
is fully sold (manual sell or loop exit).

Take profit is ALWAYS a positive profit % (James, 2026-11-26):
a zero or negative take-profit % is refused here as well as in
the route — a loss can never be stored as a take profit. A stop
loss % is the drop from entry (also positive: "5" means sell at
5% below what was paid).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def trigger_prices(entry_price, stop_loss_pct=None,
                   take_profit_pct=None):
    """(stop_price, take_profit_price) in USD from the entry price
    and the % the user typed. A pct that is None means that exit
    is not set, and its trigger is None. Never raises for bad
    numbers — a bad pct yields no trigger for that side."""
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None, None
    if entry <= 0:
        return None, None
    stop_price = None
    take_profit_price = None
    try:
        if stop_loss_pct is not None and float(stop_loss_pct) > 0:
            stop_price = entry * (1.0 - float(stop_loss_pct) / 100.0)
    except (TypeError, ValueError):
        stop_price = None
    try:
        if take_profit_pct is not None and float(take_profit_pct) > 0:
            take_profit_price = entry * (
                1.0 + float(take_profit_pct) / 100.0)
    except (TypeError, ValueError):
        take_profit_price = None
    return stop_price, take_profit_price


def get_user_exit(db, user_id: int, coin_id: str, mode: str):
    """The exit row for (user, coin, mode), or None."""
    return db.execute(
        "SELECT * FROM user_exits WHERE user_id = ? AND coin_id = ? "
        "AND mode = ?",
        (user_id, coin_id, mode)).fetchone()


def set_user_exit(db, user_id: int, coin_id: str, mode: str,
                  entry_price: float, stop_loss_pct=None,
                  take_profit_pct=None):
    """Store (or replace) the exit rule for a holding. `entry_price`
    is the holding's current average cost in USD — the triggers
    are computed from it, so a later buy that moves the average
    re-prices the triggers. Passing neither pct deletes the row:
    no exits set means no row. Returns the stored row, or None
    when the row was deleted. Raises ValueError for a take-profit
    % that is not positive (a loss is never a take profit) or a
    stop-loss % that is not positive."""
    if stop_loss_pct is not None:
        try:
            sl = float(stop_loss_pct)
        except (TypeError, ValueError):
            raise ValueError("Stop loss must be a number.")
        if sl <= 0:
            raise ValueError(
                "Stop loss must be a positive % below the price "
                "paid.")
    if take_profit_pct is not None:
        try:
            tp = float(take_profit_pct)
        except (TypeError, ValueError):
            raise ValueError("Take profit must be a number.")
        if tp <= 0:
            raise ValueError(
                "Take profit must be a positive % — a loss can "
                "never be a take profit.")
    if stop_loss_pct is None and take_profit_pct is None:
        clear_user_exit(db, user_id, coin_id, mode)
        return None
    stop_price, take_profit_price = trigger_prices(
        entry_price, stop_loss_pct, take_profit_pct)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO user_exits (user_id, coin_id, mode, entry_price, "
        "stop_loss_pct, take_profit_pct, stop_price, take_profit_price, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, coin_id, mode) DO UPDATE SET "
        "entry_price = excluded.entry_price, "
        "stop_loss_pct = excluded.stop_loss_pct, "
        "take_profit_pct = excluded.take_profit_pct, "
        "stop_price = excluded.stop_price, "
        "take_profit_price = excluded.take_profit_price, "
        "updated_at = excluded.updated_at",
        (user_id, coin_id, mode, float(entry_price),
         None if stop_loss_pct is None else float(stop_loss_pct),
         None if take_profit_pct is None else float(take_profit_pct),
         stop_price, take_profit_price, now, now))
    db.commit()
    return get_user_exit(db, user_id, coin_id, mode)


def clear_user_exit(db, user_id: int, coin_id: str, mode: str) -> None:
    """Delete the exit row for (user, coin, mode). Never raises."""
    try:
        db.execute(
            "DELETE FROM user_exits WHERE user_id = ? AND coin_id = ? "
            "AND mode = ?",
            (user_id, coin_id, mode))
        db.commit()
    except sqlite3.Error:
        pass


def user_exit_reason(row, price: float):
    """Which exit a live `price` triggers for a stored row:
    "stop_loss" / "take_profit" / None. A price that is missing
    or not a number triggers nothing — an unpriced holding is
    never sold blind. The stop is checked first: a price cannot
    be below the stop and above the take profit at once, so the
    order only matters for a degenerate row."""
    if row is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    try:
        stop_price = (float(row["stop_price"])
                      if row["stop_price"] is not None else None)
    except (TypeError, ValueError):
        stop_price = None
    try:
        take_profit_price = (float(row["take_profit_price"])
                             if row["take_profit_price"] is not None
                             else None)
    except (TypeError, ValueError):
        take_profit_price = None
    if stop_price is not None and price <= stop_price:
        return "stop_loss"
    if take_profit_price is not None and price >= take_profit_price:
        return "take_profit"
    return None
