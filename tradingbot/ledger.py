"""Trade ledger: every trade (buy and sell, paper and real) is
recorded with a unique 8-digit trade ID and a timestamp, so a bot's
win/loss record can be reviewed and builderBot can adjust its rules
when it is losing.

One row per fill:
  trade_id    unique 8-digit ID (same pattern as the signing bot's
              support ID — random 10000000-99999999, UNIQUE, retries
              on collision)
  user_id     owner
  bot_id      the bot that traded (NULL for a manual buy/sell)
  coin        coin id or token mint
  side        'buy' or 'sell'
  amount      tokens bought/sold
  price       USD price at the time of the trade
  total_usd   amount * price (the USD value of the fill)
  pnl_usd     USD profit/loss on a sell (vs the position's average
              cost), NULL on a buy — a buy has no P&L yet
  win         1 when pnl_usd > 0, 0 when pnl_usd < 0, NULL otherwise
              (a buy, or a break-even sell, is neither a win nor a
              loss)
  exit_reason why the position exited (stop loss, trailing stop, sell
              ladder, rug sell, top-10 dump, manual sell, ...)
  mode        'paper' or 'real'
  created_at  UTC timestamp of the fill

For real trades the USD figures use the USD price the loop saw at
trade time; the on-chain SOL proceeds are in the transaction the
signing bot logged (signature in the activity log), not here.
"""

from __future__ import annotations

import sqlite3

from .signing_bot import generate_support_id


def record_trade(db: sqlite3.Connection, user_id: int,
                 bot_id: int | None, coin: str, side: str,
                 amount: float, price: float, total_usd: float,
                 pnl_usd: float | None = None, exit_reason: str | None = None,
                 mode: str = "paper", now: str | None = None) -> str:
    """Insert one ledger row and return its 8-digit trade ID. Retries
    with a fresh ID on a (rare) collision. Never raises for a
    collision — a ledger write that fails would hide a trade, so the
    caller keeps its own error handling for anything else."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The column is NOT NULL: a buy has no exit reason, so store ''.
    exit_reason = exit_reason or ""
    win = None
    if pnl_usd is not None:
        if pnl_usd > 0:
            win = 1
        elif pnl_usd < 0:
            win = 0
    for _attempt in range(5):
        trade_id = generate_support_id()
        try:
            db.execute(
                "INSERT INTO trade_ledger (trade_id, user_id, bot_id, "
                "coin, side, amount, price, total_usd, pnl_usd, win, "
                "exit_reason, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, user_id, bot_id, coin, side, amount, price,
                 total_usd, pnl_usd, win, exit_reason, mode, now))
            db.commit()
            return trade_id
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a unique trade ID")


def bot_wl_summary(db: sqlite3.Connection, bot_id: int) -> dict:
    """Wins, losses, win rate and net P&L for one bot, from its
    closed sells. A break-even sell counts as neither. Win rate is
    wins / (wins + losses); a bot with no closed sells has no rate."""
    rows = db.execute(
        "SELECT pnl_usd, win FROM trade_ledger "
        "WHERE bot_id = ? AND side = 'sell' AND pnl_usd IS NOT NULL",
        (bot_id,)).fetchall()
    wins = sum(1 for r in rows if r["win"] == 1)
    losses = sum(1 for r in rows if r["win"] == 0)
    net = sum(float(r["pnl_usd"]) for r in rows)
    decided = wins + losses
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "net_pnl_usd": net,
    }


def recent_trades(db: sqlite3.Connection, bot_id: int,
                  limit: int = 20) -> list:
    """Most recent ledger rows for one bot, newest first."""
    return db.execute(
        "SELECT * FROM trade_ledger WHERE bot_id = ? "
        "ORDER BY id DESC LIMIT ?", (bot_id, limit)).fetchall()


def user_recent_trades(db: sqlite3.Connection, user_id: int,
                       limit: int = 20) -> list:
    """Most recent ledger rows for one user, newest first."""
    return db.execute(
        "SELECT * FROM trade_ledger WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
