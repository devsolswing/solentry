"""Silent trading loop: bots the user explicitly switched to silent
trade on their own, in the background, within the bot's set limits.

Guardrails (non-negotiable, from James's own rules):
  1. Silent is OFF by default. Only a bot the user explicitly enabled
     (through the confirm flow) is ever processed here.
  2. Every trade stays inside the bot's limits: buy size %, stop loss %,
     max open trades, and never more than the user's balance. A trade
     that would breach a limit is SKIPPED and the reason is logged.
  3. Every buy, sell, and skip-with-reason is written to the user's
     activity log. No-signal evaluations are NOT written to activity
     (they would flood the feed one row per bot per minute); they appear
     in the worker's stdout summary instead.
  4. The loop re-reads the silent flag from the database on every
     iteration, so turning silent off stops the bot on the next tick
     (worst case one interval).
  5. Real-money silent trading runs on the configured NETWORK
     (default devnet). Nothing mainnet unless NETWORK is mainnet.

Real swaps: the app has NO on-chain swap path in v1 (only plain SOL
transfers). The default real executor therefore SKIPS every real trade
with "real swap not available in v1" and logs it — it NEVER fakes a
fill. A real executor can be injected (tests do this); the wiring for
it is `real_executor(bot, user_id, coin, side, amount, price, db, cfg)`.

How it runs on Railway: the loop is a daemon thread inside the web
process, started only when SILENT_LOOP_ENABLED=1 (see
`start_background_loop`). A separate worker service is NOT used,
because the app's SQLite file lives on the web service's volume and a
second service cannot see it. `python -m tradingbot.silent_loop`
(standalone) exists for a future shared-database setup and for
`--once` smoke runs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .config import Config
from .db import init_db
from .paper import (InsufficientCash, InsufficientHolding, paper_bankroll,
                    paper_buy, paper_cash, paper_sell)
from .prices import get_prices
from .signals import signal_for_coin
from .solana import SolanaRpcClient, lamports_to_sol

MIN_PAPER_TRADE_USD = 1.0
REAL_SWAP_UNAVAILABLE = "real swap not available in v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(db: sqlite3.Connection, user_id: int, kind: str,
         detail: str) -> None:
    db.execute(
        "INSERT INTO activity (user_id, kind, detail, created_at) "
        "VALUES (?, ?, ?, ?)", (user_id, kind, detail, _now()))
    db.commit()


def default_real_executor(bot, user_id: int, coin: str, side: str,
                         amount: float, price: float, db, cfg) -> dict:
    """v1 real executor: there is no swap path, so every real trade is
    skipped honestly. Never fakes a fill."""
    return {"status": "skipped", "reason": REAL_SWAP_UNAVAILABLE}


def _positions(db: sqlite3.Connection, bot_id: int) -> dict:
    rows = db.execute(
        "SELECT * FROM bot_positions WHERE bot_id = ?", (bot_id,)).fetchall()
    return {r["coin_id"]: r for r in rows}


def _open_position(db: sqlite3.Connection, bot, coin: str, amount: float,
                   price: float) -> None:
    """Record (or merge into) a bot position after a fill."""
    row = db.execute(
        "SELECT amount, avg_price FROM bot_positions "
        "WHERE bot_id = ? AND coin_id = ?",
        (bot["id"], coin)).fetchone()
    if row:
        total = row["amount"] + amount
        avg = ((row["amount"] * row["avg_price"]) + amount * price) / total
        db.execute("UPDATE bot_positions SET amount = ?, avg_price = ? "
                   "WHERE bot_id = ? AND coin_id = ?",
                   (total, avg, bot["id"], coin))
    else:
        db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bot["user_id"], bot["id"], coin, amount, price,
             bot["mode"], _now()))
    db.commit()


def _close_position(db: sqlite3.Connection, bot, coin: str,
                    sold_amount: float) -> None:
    row = db.execute(
        "SELECT amount FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
        (bot["id"], coin)).fetchone()
    if not row:
        return
    remaining = row["amount"] - sold_amount
    if remaining <= 0:
        db.execute("DELETE FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
                   (bot["id"], coin))
    else:
        db.execute("UPDATE bot_positions SET amount = ? "
                   "WHERE bot_id = ? AND coin_id = ?",
                   (remaining, bot["id"], coin))
    db.commit()


def _sell_position(db: sqlite3.Connection, cfg: Config, bot, coin: str,
                   price: float, reason: str, rpc_factory,
                   real_executor, actions: list) -> None:
    """Sell a bot's whole position in `coin` (paper ledger, or the real
    executor). Logs the outcome either way; never fakes a fill."""
    pos = _positions(db, bot["id"]).get(coin)
    if not pos:
        return
    amount = pos["amount"]
    if bot["mode"] == "paper":
        try:
            held_row = db.execute(
                "SELECT amount FROM paper_holdings "
                "WHERE user_id = ? AND coin_id = ?",
                (bot["user_id"], coin)).fetchone()
            held = held_row["amount"] if held_row else 0.0
            if held <= 0:
                _close_position(db, bot, coin, amount)
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': {coin} position already "
                     f"sold — nothing to sell.")
                actions.append({"action": "skip", "coin": coin,
                                "reason": "position already sold"})
                return
            sell_amount = min(amount, held)
            usd = paper_sell(db, bot["user_id"], coin, sell_amount, price,
                             _now())
            _close_position(db, bot, coin, sell_amount)
            kind = ("silent_stop_loss" if reason == "stop_loss"
                    else "silent_sell")
            _log(db, bot["user_id"], kind,
                 f"Silent bot '{bot['name']}' sold {sell_amount:.4f} {coin} "
                 f"for ${usd:.2f} ({reason}).")
            actions.append({"action": "sell", "coin": coin,
                            "amount": sell_amount, "usd": usd,
                            "reason": reason})
        except InsufficientHolding:
            _close_position(db, bot, coin, amount)
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': {coin} holding smaller than "
                 f"the position — position cleared.")
            actions.append({"action": "skip", "coin": coin,
                            "reason": "holding smaller than position"})
        return
    # Real mode: the executor decides. v1 has no swap path.
    result = real_executor(bot, bot["user_id"], coin, "sell", amount, price,
                           db, cfg)
    if result.get("status") == "executed":
        _close_position(db, bot, coin, amount)
        _log(db, bot["user_id"], "silent_sell",
             f"Silent bot '{bot['name']}' sold {amount:.4f} {coin} "
             f"({reason}). Signature: "
             f"{str(result.get('signature', ''))[:12]}...")
        actions.append({"action": "sell", "coin": coin, "amount": amount,
                        "reason": reason})
    else:
        why = result.get("reason", "real execution failed")
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — {why}.")
        actions.append({"action": "skip", "coin": coin, "reason": why})


def _buy_coin(db: sqlite3.Connection, cfg: Config, bot, coin: str,
              price: float, prices_usd: dict, rpc_factory,
              real_executor, actions: list) -> None:
    """Buy `coin` for a bot within its limits, or skip with a reason."""
    if bot["mode"] == "paper":
        bankroll = paper_bankroll(db, bot["user_id"], prices_usd)
        buy_usd = bankroll * float(bot["buy_size_pct"]) / 100.0
        buy_usd = min(buy_usd, paper_cash(db, bot["user_id"]))
        if buy_usd < MIN_PAPER_TRADE_USD:
            why = ("insufficient paper cash"
                   if paper_cash(db, bot["user_id"]) < MIN_PAPER_TRADE_USD
                   else "trade too small (under $1)")
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': buy of {coin} skipped — "
                 f"{why}.")
            actions.append({"action": "skip", "coin": coin, "reason": why})
            return
        try:
            amount, usd = paper_buy(db, bot["user_id"], coin, buy_usd,
                                    price, _now())
        except InsufficientCash:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': buy of {coin} skipped — "
                 f"insufficient paper cash.")
            actions.append({"action": "skip", "coin": coin,
                            "reason": "insufficient paper cash"})
            return
        _open_position(db, bot, coin, amount, price)
        _log(db, bot["user_id"], "silent_buy",
             f"Silent bot '{bot['name']}' bought {amount:.4f} {coin} "
             f"for ${usd:.2f} (paper).")
        actions.append({"action": "buy", "coin": coin, "amount": amount,
                        "usd": usd})
        return
    # Real mode.
    wallet = db.execute(
        "SELECT pubkey FROM wallets WHERE user_id = ?",
        (bot["user_id"],)).fetchone()
    if not wallet:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"no app wallet.")
        actions.append({"action": "skip", "coin": coin,
                        "reason": "no app wallet"})
        return
    try:
        client = rpc_factory(cfg.rpc_url)
        balance = lamports_to_sol(
            client.get_balance_lamports(wallet["pubkey"]))
    except Exception:
        balance = None
    if balance is None:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"real balance unavailable.")
        actions.append({"action": "skip", "coin": coin,
                        "reason": "real balance unavailable"})
        return
    amount_sol = balance * float(bot["buy_size_pct"]) / 100.0
    if amount_sol <= 0:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"insufficient real balance ({balance:.4f} SOL).")
        actions.append({"action": "skip", "coin": coin,
                        "reason": "insufficient real balance"})
        return
    result = real_executor(bot, bot["user_id"], coin, "buy", amount_sol,
                           price, db, cfg)
    if result.get("status") == "executed":
        coin_amount = float(result.get("coin_amount") or 0)
        if coin_amount > 0:
            _open_position(db, bot, coin, coin_amount, price)
        _log(db, bot["user_id"], "silent_buy",
             f"Silent bot '{bot['name']}' bought {coin} for "
             f"{amount_sol:.4f} SOL. Signature: "
             f"{str(result.get('signature', ''))[:12]}...")
        actions.append({"action": "buy", "coin": coin,
                        "amount_sol": amount_sol})
    else:
        why = result.get("reason", "real execution failed")
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — {why}.")
        actions.append({"action": "skip", "coin": coin, "reason": why})


def _evaluate_bot(db: sqlite3.Connection, cfg: Config, bot,
                  prices_data: dict, rpc_factory,
                  real_executor) -> list:
    """One evaluation pass for one silent bot. Returns its actions."""
    actions: list = []
    prices = prices_data["prices"]
    prices_usd = {c: e.get("price", 0.0) for c, e in prices.items()}
    coins = json.loads(bot["coins"])
    positions = _positions(db, bot["id"])
    stop_loss_pct = float(bot["stop_loss_pct"])

    # 1. Stop loss + sell signals on open positions.
    for coin, pos in list(positions.items()):
        entry = prices.get(coin)
        if not entry or entry.get("price") is None:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': no price data for {coin} — "
                 f"position left alone.")
            actions.append({"action": "skip", "coin": coin,
                            "reason": "no price data"})
            continue
        price = entry["price"]
        stop_price = float(pos["avg_price"]) * (1 - stop_loss_pct / 100.0)
        if price <= stop_price:
            _sell_position(db, cfg, bot, coin, price, "stop_loss",
                           rpc_factory, real_executor, actions)
            continue
        side, _detail = signal_for_coin(
            coin, entry.get("change_24h"), bot["strategy"])
        if side == "sell":
            _sell_position(db, cfg, bot, coin, price, "sell signal",
                           rpc_factory, real_executor, actions)

    # 2. Buy signals on coins not already held.
    positions = _positions(db, bot["id"])
    for coin in coins:
        if coin in positions:
            continue
        entry = prices.get(coin)
        if not entry or entry.get("price") is None:
            continue
        side, _detail = signal_for_coin(
            coin, entry.get("change_24h"), bot["strategy"])
        if side != "buy":
            continue
        if len(_positions(db, bot["id"])) >= int(bot["max_open_trades"]):
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': buy of {coin} skipped — "
                 f"max open trades reached ({bot['max_open_trades']}).")
            actions.append({"action": "skip", "coin": coin,
                            "reason": "max open trades reached"})
            continue
        _buy_coin(db, cfg, bot, coin, entry["price"], prices_usd,
                  rpc_factory, real_executor, actions)
    return actions


def run_once(db: sqlite3.Connection, cfg: Config, price_fetcher=None,
             rpc_factory=None, real_executor=None) -> list:
    """One loop iteration over every silent-enabled bot.

    Returns a list of per-bot outcome dicts. A failure for one bot is
    recorded and never stops the others.
    """
    price_fetcher = price_fetcher or get_prices
    rpc_factory = rpc_factory or SolanaRpcClient
    real_executor = real_executor or default_real_executor
    prices_data = price_fetcher()
    bots = db.execute(
        "SELECT * FROM bots WHERE silent = 1 ORDER BY id").fetchall()
    outcomes = []
    for bot in bots:
        try:
            actions = _evaluate_bot(db, cfg, bot, prices_data, rpc_factory,
                                    real_executor)
            outcomes.append({"bot_id": bot["id"], "bot_name": bot["name"],
                             "user_id": bot["user_id"], "mode": bot["mode"],
                             "actions": actions, "error": None})
        except Exception as exc:  # one bad bot never stops the loop
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': evaluation failed — "
                 f"{str(exc)[:120]}. No trade was made.")
            outcomes.append({"bot_id": bot["id"], "bot_name": bot["name"],
                             "user_id": bot["user_id"], "mode": bot["mode"],
                             "actions": [], "error": str(exc)[:200]})
    return outcomes


def loop_forever(db_path: str, cfg: Config, interval: int | None = None,
                 price_fetcher=None, rpc_factory=None,
                 real_executor=None, once: bool = False) -> None:
    """Run iterations forever (or once with once=True). Opens a fresh
    DB connection per iteration so the web process's connection is
    never shared across threads."""
    interval = interval if interval is not None \
        else cfg.silent_loop_interval_seconds
    while True:
        db = init_db(db_path)
        try:
            outcomes = run_once(db, cfg, price_fetcher=price_fetcher,
                                rpc_factory=rpc_factory,
                                real_executor=real_executor)
        except Exception as exc:  # loop-level failure: log, keep going
            print(f"[silent-loop] iteration failed: {exc}", flush=True)
            outcomes = []
        finally:
            db.close()
        for outcome in outcomes:
            acts = ", ".join(
                f"{a['action']}:{a.get('coin', '')}" for a in outcome["actions"])
            print(f"[silent-loop] bot {outcome['bot_name']} "
                  f"({outcome['mode']}): {acts or 'no signal'}"
                  + (f" ERROR {outcome['error']}" if outcome["error"]
                     else ""), flush=True)
        if once:
            return
        time.sleep(max(10, interval))


def start_background_loop(app) -> threading.Thread | None:
    """Start the loop as a daemon thread inside the web process.

    Only called when SILENT_LOOP_ENABLED=1. Safe to call twice — the
    second call is a no-op.
    """
    if getattr(app, "_silent_loop_started", False):
        return None
    cfg = app.config["TRADINGBOT_CONFIG"]
    thread = threading.Thread(
        target=loop_forever,
        args=(cfg.database_path, cfg, cfg.silent_loop_interval_seconds),
        daemon=True, name="silent-loop")
    thread.start()
    app._silent_loop_started = True
    print("[silent-loop] background loop started "
          f"(every {cfg.silent_loop_interval_seconds}s)", flush=True)
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Silent trading loop (standalone worker).")
    parser.add_argument("--once", action="store_true",
                        help="Run one iteration and exit.")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between iterations.")
    args = parser.parse_args()
    cfg = Config()
    print(f"[silent-loop] network={cfg.network} "
          f"interval={args.interval or cfg.silent_loop_interval_seconds}s",
          flush=True)
    loop_forever(cfg.database_path, cfg, interval=args.interval,
                 once=args.once)


if __name__ == "__main__":
    main()
