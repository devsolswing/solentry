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

Real swaps: the dedicated signing bot executes real trades on
devnet only, through pump.fun bonding-curve buys/sells (see
executor.py). A real trade is refused plainly — never faked —
whenever it is unverified: mainnet, a coin that is not a pump.fun
token mint, a token with no bonding curve on devnet, or a trade
outside the bot's limits. A real executor can still be injected
(tests do this); the wiring for it is `real_executor(bot, user_id,
coin, side, amount, price, db, cfg)`.

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
from .executor import execute_real_trade
from .jupiter import is_jupiter_coin
from .jupiter_executor import execute_jupiter_swap
from .ledger import record_trade
from .platforms import REAL_TRADEABLE_PLATFORMS
from .paper import (InsufficientCash, InsufficientHolding, paper_bankroll,
                    paper_buy, paper_cash, paper_sell)
from .pnl import portfolio_totals, record_snapshot
from .prices import get_prices
from .rules import load_rules
from .scanner import (check_top10_concentration, check_top10_rug,
                      clear_watches, detect_regime, gain_3m_pct,
                      gain_window_pct, next_ladder_rung,
                      passes_buy_filter, record_marks, top10_total)
from .signals import signal_for_coin
from .solana import SolanaRpcClient, lamports_to_sol
from .trenches import fetch_trenches_scan
from .devnet_watchlist import enrich_scan

MIN_PAPER_TRADE_USD = 1.0
REAL_SWAP_UNAVAILABLE = "real swap not available in v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(db: sqlite3.Connection, user_id: int, kind: str,
         detail: str, mode: str = "") -> None:
    """Activity row, tagged with the trading mode it belongs to
    ('paper' / 'real') so the pages can show each mode's activity
    separately. Account-level rows pass mode='' and show in both."""
    db.execute(
        "INSERT INTO activity (user_id, kind, detail, created_at, mode) "
        "VALUES (?, ?, ?, ?, ?)", (user_id, kind, detail, _now(), mode))
    db.commit()


def _live_sol_price_usd(feed, prices_data):
    """The live SOL price in USD, or None.

    The feed's own SOL tick first (fresh only — a stale tick is not
    live); then prices_data, but only when its source is live — a
    baked snapshot price is never a live price. None means the loop
    must not price anything in USD this scan: curve pricing fails
    closed and executed P&L is left unrecorded rather than computed
    from a stale number. SOL is the unit of account for pump.fun
    trades (curve prices, executed SOL in/out), not a traded major
    here.
    """
    if feed is not None:
        try:
            tick = feed.snapshot(["SOL"]).get("SOL")
        except Exception:
            tick = None
        if tick is not None and not tick.get("stale"):
            try:
                price = float(tick["price"])
            except (TypeError, ValueError):
                price = None
            else:
                if price and price > 0:
                    return price
    if isinstance(prices_data, dict) \
            and prices_data.get("source") == "live":
        entry = (prices_data.get("prices") or {}).get("SOL") or {}
        try:
            price = float(entry.get("price"))
        except (TypeError, ValueError):
            return None
        if price and price > 0:
            return price
    return None


def default_real_executor(bot, user_id: int, coin: str, side: str,
                         amount: float, price: float, db, cfg) -> dict:
    """Real executor: the account's dedicated signing bot executes
    pump.fun trades on devnet (see executor.py) — and Jupiter
    swaps on mainnet for listed coins (see jupiter_executor.py,
    gated behind James's go-ahead). Anything unverified is
    skipped honestly — never a fake fill."""
    if is_jupiter_coin(coin):
        return execute_jupiter_swap(bot, user_id, coin, side, amount,
                                    db, cfg)
    return execute_real_trade(bot, user_id, coin, side, amount, price,
                              db, cfg)


def _positions(db: sqlite3.Connection, bot_id: int) -> dict:
    rows = db.execute(
        "SELECT * FROM bot_positions WHERE bot_id = ?", (bot_id,)).fetchall()
    return {r["coin_id"]: r for r in rows}


def _open_position(db: sqlite3.Connection, bot, coin: str, amount: float,
                   price: float, avg_price: float | None = None) -> None:
    """Record (or merge into) a bot position after a fill.

    peak_price tracks the highest price since the position opened (for
    the trailing stop) and orig_amount the size at open (sell-ladder
    rungs are % of it). Merging into an existing position keeps the
    existing peak and grows the original amount. `price` is the live
    mark price (peak/last-price tracking always uses the mark);
    `avg_price` overrides the recorded cost basis — the executed cost
    when the caller knows it, else the mark price. Cost basis is what
    sell P&L is measured against, so an executed cost must never be
    overwritten by a mark.
    """
    cost = price if avg_price is None else float(avg_price)
    row = db.execute(
        "SELECT amount, avg_price, peak_price, orig_amount, ladder_stage "
        "FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
        (bot["id"], coin)).fetchone()
    if row:
        total = row["amount"] + amount
        avg = ((row["amount"] * row["avg_price"]) + amount * cost) / total
        peak = max(row["peak_price"] if row["peak_price"] is not None
                   else price, price)
        orig = (row["orig_amount"] if row["orig_amount"] is not None
                else row["amount"]) + amount
        db.execute("UPDATE bot_positions SET amount = ?, avg_price = ?, "
                   "peak_price = ?, orig_amount = ? "
                   "WHERE bot_id = ? AND coin_id = ?",
                   (total, avg, peak, orig, bot["id"], coin))
    else:
        db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at, peak_price, orig_amount, "
            "ladder_stage, last_price, last_price_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (bot["user_id"], bot["id"], coin, amount, cost,
             bot["mode"], _now(), price, amount, price, _now()))
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
        try:
            clear_watches(db, bot["id"], coin)
        except Exception:
            pass
    else:
        db.execute("UPDATE bot_positions SET amount = ? "
                   "WHERE bot_id = ? AND coin_id = ?",
                   (remaining, bot["id"], coin))
    db.commit()


def _sell_position(db: sqlite3.Connection, cfg: Config, bot, coin: str,
                   price: float, reason: str, rpc_factory,
                   real_executor, actions: list, amount: float | None = None,
                   ladder_stage: int | None = None,
                   feed=None, prices_data: dict | None = None) -> None:
    """Sell a bot's position in `coin` (paper ledger, or the real
    executor). `amount` None means the whole position; otherwise a
    partial sell (sell-ladder rung). `ladder_stage` advances the
    position's rung counter when given. `price` is the live mark
    price: it drives the stop/ladder decision and is the fallback
    display price — real-mode P&L is NEVER computed from it (see
    below). Logs the outcome either way; never fakes a fill."""
    pos = _positions(db, bot["id"]).get(coin)
    if not pos:
        return
    full_amount = pos["amount"]
    amount = full_amount if amount is None else min(amount, full_amount)
    if amount <= 0:
        return
    if bot["mode"] == "paper":
        try:
            held_row = db.execute(
                "SELECT amount FROM paper_holdings "
                "WHERE user_id = ? AND coin_id = ?",
                (bot["user_id"], coin)).fetchone()
            held = held_row["amount"] if held_row else 0.0
            if held <= 0:
                _close_position(db, bot, coin, full_amount)
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': {coin} position already "
                     f"sold — nothing to sell.", bot["mode"])
                actions.append({"action": "skip", "coin": coin,
                                "reason": "position already sold"})
                return
            sell_amount = min(amount, held)
            usd = paper_sell(db, bot["user_id"], coin, sell_amount, price,
                             _now(), bot_id=bot["id"], exit_reason=reason)
            _close_position(db, bot, coin, sell_amount)
            if ladder_stage is not None:
                db.execute("UPDATE bot_positions SET ladder_stage = ? "
                           "WHERE bot_id = ? AND coin_id = ?",
                           (ladder_stage, bot["id"], coin))
                db.commit()
            kind = ("silent_stop_loss" if reason == "stop_loss"
                    else "silent_sell")
            _log(db, bot["user_id"], kind,
                 f"Silent bot '{bot['name']}' sold {sell_amount:.4f} {coin} "
                 f"for ${usd:.2f} ({reason}).",
                 bot["mode"])
            actions.append({"action": "sell", "coin": coin,
                            "amount": sell_amount, "usd": usd,
                            "reason": reason})
        except InsufficientHolding:
            _close_position(db, bot, coin, full_amount)
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': {coin} holding smaller than "
                 f"the position — position cleared.", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "holding smaller than position"})
        return
    # Real mode: the signing bot executes on devnet (executor decides).
    result = real_executor(bot, bot["user_id"], coin, "sell", amount, price,
                           db, cfg)
    if result.get("status") == "executed":
        _close_position(db, bot, coin, amount)
        # Trade ledger (builderBot): P&L from the EXECUTED on-chain
        # amounts, never from the loop-seen mark price. The executor
        # reports the SOL actually received (wallet balance delta);
        # priced at the live SOL price it gives the real proceeds,
        # and the win/loss is proceeds minus the position's executed
        # average cost. When the executor did not report proceeds or
        # no live SOL price exists, the trade is recorded with no USD
        # P&L at all — an executed sell with unknown P&L is not a win
        # and not a loss, and must never be counted as one. (This is
        # the 2026-11-13 Gate/AuditTest lesson: mark-based P&L called
        # two losing round-trips "5 wins, +$5.78".)
        sol_received = result.get("sol_received")
        try:
            sol_received = (float(sol_received)
                            if sol_received is not None else None)
        except (TypeError, ValueError):
            sol_received = None
        exec_price = None
        pnl_usd = None
        if sol_received is not None and sol_received > 0 and amount > 0:
            sol_price = _live_sol_price_usd(feed, prices_data)
            if sol_price is not None:
                proceeds_usd = sol_received * sol_price
                exec_price = proceeds_usd / amount
                pnl_usd = (proceeds_usd
                           - amount * float(pos["avg_price"]))
        record_trade(db, bot["user_id"], bot["id"], coin, "sell", amount,
                     exec_price if exec_price is not None else price,
                     (exec_price * amount if exec_price is not None
                      else amount * price),
                     pnl_usd=pnl_usd,
                     exit_reason=reason, mode="real", now=_now())
        if ladder_stage is not None:
            db.execute("UPDATE bot_positions SET ladder_stage = ? "
                       "WHERE bot_id = ? AND coin_id = ?",
                       (ladder_stage, bot["id"], coin))
            db.commit()
        sold_line = (f"Silent bot '{bot['name']}' sold {amount:.4f} "
                     f"{coin} ({reason})")
        if sol_received is not None:
            sold_line += f" for {sol_received:.4f} SOL"
        _log(db, bot["user_id"], "silent_sell",
             sold_line + f". Signature: "
             f"{str(result.get('signature', ''))[:12]}... "
             f"Signing bot: {result.get('signing_bot', '')}.", bot["mode"])
        actions.append({"action": "sell", "coin": coin, "amount": amount,
                        "reason": reason})
    else:
        why = result.get("reason", "real execution failed")
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — {why}.", bot["mode"])
        actions.append({"action": "skip", "coin": coin, "reason": why})


def _buy_coin(db: sqlite3.Connection, cfg: Config, bot, coin: str,
              price: float, prices_usd: dict, rpc_factory,
              real_executor, actions: list,
              signals: dict | None = None,
              feed=None, prices_data: dict | None = None) -> None:
    """Buy `coin` for a bot within its limits, or skip with a reason.

    `signals` is the buy-time signal snapshot from the feed entry
    (m5 gain %, 5-minute volume, buys/sells, regime) — recorded on
    the paper trade so the losing-buy analysis can show how the coin
    got bought. None for v1 bots and manual buys. `price` is the live
    mark price; a real position's cost basis is the EXECUTED spend
    when the executor reports it (see below), never the mark.
    """
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
                 f"{why}.", bot["mode"])
            actions.append({"action": "skip", "coin": coin, "reason": why})
            return
        try:
            amount, usd = paper_buy(db, bot["user_id"], coin, buy_usd,
                                    price, _now(), bot_id=bot["id"],
                                    signals=signals)
        except InsufficientCash:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': buy of {coin} skipped — "
                 f"insufficient paper cash.", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "insufficient paper cash"})
            return
        _open_position(db, bot, coin, amount, price)
        _log(db, bot["user_id"], "silent_buy",
             f"Silent bot '{bot['name']}' bought {amount:.4f} {coin} "
             f"for ${usd:.2f} (paper).",
             bot["mode"])
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
             f"no app wallet.", bot["mode"])
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
             f"real balance unavailable.", bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "real balance unavailable"})
        return
    amount_sol = balance * float(bot["buy_size_pct"]) / 100.0
    if amount_sol <= 0:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"insufficient real balance ({balance:.4f} SOL).", bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "insufficient real balance"})
        return
    result = real_executor(bot, bot["user_id"], coin, "buy", amount_sol,
                           price, db, cfg)
    if result.get("status") == "executed":
        coin_amount = float(result.get("coin_amount") or 0)
        if coin_amount > 0:
            # Cost basis from the EXECUTED spend: the SOL the chain
            # actually took (wallet balance delta, including the
            # network fee) priced at the live SOL price, over the
            # tokens actually received. The loop-seen mark price is
            # only the fallback when the executor did not report the
            # spend or no live SOL price exists — and then the
            # position's sell P&L is still measured against executed
            # proceeds, so a mark cost can only understate a win, not
            # invent one.
            exec_avg = None
            sol_spent = result.get("sol_spent")
            try:
                sol_spent = (float(sol_spent)
                             if sol_spent is not None else None)
            except (TypeError, ValueError):
                sol_spent = None
            if sol_spent is not None and sol_spent > 0:
                sol_price = _live_sol_price_usd(feed, prices_data)
                if sol_price is not None:
                    exec_avg = sol_spent * sol_price / coin_amount
            _open_position(db, bot, coin, coin_amount, price,
                           avg_price=exec_avg)
            # Trade ledger (builderBot): every fill gets its own
            # 8-digit trade ID. The recorded price is the executed
            # cost when known, else the mark price.
            buy_price = exec_avg if exec_avg is not None else price
            record_trade(db, bot["user_id"], bot["id"], coin, "buy",
                         coin_amount, buy_price,
                         coin_amount * buy_price,
                         mode="real", now=_now())
        _log(db, bot["user_id"], "silent_buy",
             f"Silent bot '{bot['name']}' bought {coin} for "
             f"{amount_sol:.4f} SOL. Signature: "
             f"{str(result.get('signature', ''))[:12]}... "
             f"Signing bot: {result.get('signing_bot', '')}.", bot["mode"])
        actions.append({"action": "buy", "coin": coin,
                        "amount_sol": amount_sol})
    else:
        why = result.get("reason", "real execution failed")
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — {why}.", bot["mode"])
        actions.append({"action": "skip", "coin": coin, "reason": why})


_BUY_METRIC_TO_CHANGE_KEY = {
    "gain_5m_pct": "change_5m_pct",
    "gain_1h_pct": "change_1h_pct",
    "gain_6h_pct": "change_6h_pct",
    "gain_24h_pct": "change_24h_pct",
}


def _parse_scan_time(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value,
                                 "%Y-%m-%dT%H:%M:%SZ").replace(
                                     tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _rug_window_words(window_seconds: int) -> str:
    return {60: "about a minute", 300: "5 minutes"}.get(
        window_seconds, f"{window_seconds} seconds")


def _rug_reason(rug: dict, kind: str, coin: str, price: float,
                pos, price_map: dict | None, now_ts: float) -> str | None:
    """The rug sell check, run on every price update for an open
    position — before the stop loss, trailing stop and sell ladder,
    so a collapse exits on the same scan that sees it.

    Two triggers, either one sells everything:
      * feed 5-minute change (trenches/scanner only — CoinGecko has no
        5m change): the feed's change_5m_pct at or below -drop_5m_pct.
      * fast drop since the last recorded price mark: the price fell
        drop_pct or more and the mark is at most window_seconds old
        (plus 30 seconds of scheduling jitter — scans run about a
        minute apart, so a mark a few seconds stale is still a
        one-minute drop).
    Returns the plain-words reason, or None when nothing fired. This
    is PRICE-based: the feed cannot see the token creator's wallet,
    so a dev selling is only visible as the price falling — the rule
    triggers on that fall, never on a faked "dev sold" signal.
    """
    drop_5m = float(rug["drop_5m_pct"])
    if kind in ("trenches", "scanner") and price_map:
        entry = price_map.get(coin) or {}
        change_5m = entry.get("change_5m_pct")
        if change_5m is not None and float(change_5m) <= -drop_5m:
            return (f"rug sell: price fell {abs(float(change_5m)):.1f}% "
                    f"in 5 minutes")
    last_price = pos["last_price"] if "last_price" in pos.keys() else None
    last_at = pos["last_price_at"] if "last_price_at" in pos.keys() else None
    if last_price is not None and float(last_price) > 0:
        age = None
        if last_at:
            parsed = _parse_scan_time(last_at)
            age = now_ts - parsed if parsed is not None else None
        if age is not None and age <= int(rug["window_seconds"]) + 30:
            drop = (float(last_price) - price) / float(last_price) * 100.0
            if drop >= float(rug["drop_pct"]):
                return (f"rug sell: price fell {drop:.1f}% in "
                        f"{_rug_window_words(int(rug['window_seconds']))}")
    return None


def _gte(a: float, b: float) -> bool:
    """a >= b with a 1e-9 relative tolerance — prices are floats and a
    target computed as avg_price * 1.10 can sit one ulp above the
    literal price that should trigger it."""
    return a >= b or abs(a - b) <= 1e-9 * max(1.0, abs(b))


def _lte(a: float, b: float) -> bool:
    return a <= b or abs(a - b) <= 1e-9 * max(1.0, abs(b))


def _evaluate_rules_bot(db: sqlite3.Connection, cfg: Config, bot, rules,
                        prices_data: dict, trenches_data: dict | None,
                        rpc_factory, real_executor,
                        feed=None) -> list:
    """One evaluation pass for a bot with a custom rule set.

    Fail-closed: a feed error, missing price data, or an unusable rule
    means no trades this scan. The bot's own limits (buy size %,
    stop loss %, max open trades, balance) always apply on top of
    the rules — the rules never widen them. `feed` is the live feed:
    it prices executed P&L (see _sell_position/_buy_coin) and the
    curve SOL price, so the loop and the pages read the same source.
    """
    actions: list = []
    now_ts = datetime.now(timezone.utc).timestamp()
    last = _parse_scan_time(bot["last_scan_at"])
    interval = int(rules["scan_interval_seconds"])
    if last is not None and now_ts - last < interval:
        return [{"action": "skip", "coin": "",
                 "reason": "scan interval not reached"}]
    db.execute("UPDATE bots SET last_scan_at = ? WHERE id = ?",
               (_now(), bot["id"]))
    db.commit()

    kind = rules["kind"]
    if kind in ("trenches", "scanner"):
        if not trenches_data or trenches_data.get("error"):
            why = (trenches_data or {}).get("error") or "feed unavailable"
            feed_name = "scanner" if kind == "scanner" else "trenches"
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': {feed_name} feed "
                 f"unavailable — {why[:80]}. No trades this scan.",
                 bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": f"{feed_name} feed unavailable"})
            return actions
        price_map = trenches_data["prices"]
    else:
        price_map = None

    def _price_for(coin: str):
        if kind in ("trenches", "scanner"):
            entry = price_map.get(coin)
            return entry["price_usd"] if entry else None
        entry = prices_data["prices"].get(coin)
        return entry["price"] if entry else None

    def _change_for(coin: str):
        if kind == "trenches":
            entry = price_map.get(coin)
            if not entry:
                return None
            return entry.get(
                _BUY_METRIC_TO_CHANGE_KEY[rules["buy"]["metric"]])
        entry = prices_data["prices"].get(coin)
        if not entry:
            return None
        return entry.get("change_24h")

    def _bankroll_prices() -> dict:
        """Prices for the paper bankroll: the REST list plus the
        scan's mint prices, so a paper mint holding is valued at its
        live mark instead of $0. A price of 0 or less is dropped —
        missing, never zero (holdings_value then falls back to cost).
        """
        out = {}
        for coin, entry in (prices_data.get("prices") or {}).items():
            price = (entry or {}).get("price")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            if price is not None and price > 0:
                out[coin] = price
        if price_map:
            for mint, entry in price_map.items():
                price = (entry or {}).get("price_usd")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                if price is not None and price > 0:
                    out[mint] = price
        return out

    stop_loss_pct = float(bot["stop_loss_pct"])
    trailing_pct = float(rules["trailing_stop_pct"])
    ladder = rules["sell_ladder"]
    # Top-10 wallets watch state (scanner only): one RPC client per
    # evaluation, created lazily; a failed client or a failed lookup
    # is logged once per bot per scan and the watch is skipped.
    top10_client = None
    top10_failed = False
    top10_skip_logged = False

    # 1. Open positions: rug sell first, then hard stop loss,
    # trailing stop, sell ladder.
    for coin, pos in list(_positions(db, bot["id"]).items()):
        price = _price_for(coin)
        if price is None:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': no price data for {coin} — "
                 f"position left alone.", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "no price data"})
            continue
        # 0. Rug sell, checked FIRST on every price update: a fast
        # collapse sells everything on this scan, before the stop
        # loss, trailing stop or ladder get a say. The price mark is
        # refreshed here so the next scan compares against this one.
        rug = rules.get("rug_sell")
        if rug:
            reason = _rug_reason(rug, kind, coin, price, pos, price_map,
                                 now_ts)
            if reason:
                _sell_position(db, cfg, bot, coin, price, reason,
                               rpc_factory, real_executor, actions,
                               feed=feed, prices_data=prices_data)
                continue
            db.execute("UPDATE bot_positions SET last_price = ?, "
                       "last_price_at = ? WHERE bot_id = ? AND coin_id = ?",
                       (price, _now(), bot["id"], coin))
            db.commit()
        # 0b. Top-10 wallets watch (scanner and trenches — the
        # memecoin kinds): the top 10 holders dumping sells
        # everything. It runs alongside the fast-drop rug sell —
        # either one fires the exit. The baseline is the top-10 total
        # the first time the position is watched, tracked upward to
        # the peak since holding; a 10% drop from that peak is the
        # dump. If the RPC cannot serve the mint, the watch is
        # skipped for this scan — logged once per bot, never faked,
        # and the other exits still apply.
        top10 = rules.get("top10_rug")
        if kind in ("scanner", "trenches") and top10:
            if top10_client is None and not top10_failed:
                try:
                    top10_client = rpc_factory(cfg.rpc_url)
                except Exception:
                    top10_client = None
                    top10_failed = True
                    _log(db, bot["user_id"], "silent_skip",
                         f"Silent bot '{bot['name']}': top-10 wallet "
                         f"watch unavailable — no top-10 exit this "
                         f"scan.", bot["mode"])
            if top10_client is not None:
                total = top10_total(top10_client, coin)
                if total is None:
                    if not top10_skip_logged:
                        _log(db, bot["user_id"], "silent_skip",
                             f"Silent bot '{bot['name']}': top-10 wallet "
                             f"watch unavailable for {coin} — position "
                             f"left alone this scan.", bot["mode"])
                        top10_skip_logged = True
                else:
                    fired, reason = check_top10_rug(
                        db, bot["id"], coin, total,
                        float(top10["drop_pct"]), _now())
                    if fired:
                        sell_reason = reason
                        if bot["mode"] == "real":
                            sell_reason += " — proceeds in SOL"
                        _sell_position(db, cfg, bot, coin, price,
                                       sell_reason, rpc_factory,
                                       real_executor, actions,
                                       feed=feed,
                                       prices_data=prices_data)
                        continue
        avg_price = float(pos["avg_price"])
        peak = pos["peak_price"]
        peak = float(peak) if peak is not None else avg_price
        if price > peak:
            peak = price
            db.execute("UPDATE bot_positions SET peak_price = ? "
                       "WHERE bot_id = ? AND coin_id = ?",
                       (peak, bot["id"], coin))
            db.commit()
        stop_price = avg_price * (1 - stop_loss_pct / 100.0)
        if _lte(price, stop_price):
            _sell_position(db, cfg, bot, coin, price, "stop_loss",
                           rpc_factory, real_executor, actions,
                           feed=feed, prices_data=prices_data)
            continue
        trail_price = peak * (1 - trailing_pct / 100.0)
        if _lte(price, trail_price):
            _sell_position(db, cfg, bot, coin, price, "trailing stop",
                           rpc_factory, real_executor, actions,
                           feed=feed, prices_data=prices_data)
            continue
        stage = int(pos["ladder_stage"] or 0)
        if kind == "scanner" and rules.get("trending_ladder"):
            # Regime-aware profit ladders (scanner only): the regime
            # is picked fresh from this scan's feed entry and chooses
            # the slow ladder or the trending runner ladder. Rungs
            # fire on the cumulative fraction already sold (derived
            # from the position's amount vs its original amount), so
            # a regime flip mid-position never re-sells a slice and
            # the ladder always converges to a full exit.
            entry = (price_map or {}).get(coin) or {}
            regime = detect_regime(entry, rules.get("regime"))
            active = (rules["trending_ladder"]
                      if regime == "trending" else ladder)
            orig = pos["orig_amount"]
            orig = float(orig) if orig is not None \
                else float(pos["amount"])
            sold_frac = (1.0 - float(pos["amount"]) / orig
                         if orig > 0 else 0.0)
            profit = ((price - avg_price) / avg_price * 100.0
                      if avg_price > 0 else 0.0)
            nxt = next_ladder_rung(active, sold_frac, profit)
            if nxt is not None:
                sell_frac, target = nxt
                _sell_position(
                    db, cfg, bot, coin, price,
                    f"sell ladder ({regime}): {sell_frac * 100:.0f}% at "
                    f"+{target:.0f}% profit",
                    rpc_factory, real_executor, actions,
                    amount=orig * sell_frac, ladder_stage=stage + 1,
                    feed=feed, prices_data=prices_data)
        elif stage < len(ladder):
            rung = ladder[stage]
            target = avg_price * (1 + float(rung["profit_pct"]) / 100.0)
            if _gte(price, target):
                orig = pos["orig_amount"]
                orig = float(orig) if orig is not None \
                    else float(pos["amount"])
                amount = orig * float(rung["pct_of_position"]) / 100.0
                _sell_position(
                    db, cfg, bot, coin, price,
                    f"sell ladder: {rung['pct_of_position']:.0f}% at "
                    f"+{rung['profit_pct']:.0f}% profit",
                    rpc_factory, real_executor, actions, amount=amount,
                    ladder_stage=stage + 1,
                    feed=feed, prices_data=prices_data)

    # 2. Buys: strongest qualifying gainer, at most max_new per scan.
    bought = 0
    max_new = int(rules["max_new_positions_per_scan"])
    ranked = []
    # Candidates that passed the buy threshold but failed the
    # low-performer buy filter — kept so the scan logs one line about
    # the strongest rejected coin instead of one line per candidate.
    filtered = []
    if kind == "scanner":
        # The scanner ranks by the 3-minute gain from price marks the
        # loop records itself (scanner.py) — the feed only provides
        # 5-minute changes. A coin with no mark at least 3 minutes
        # old has no 3-minute gain yet and is not bought.
        threshold = float(rules["buy"]["threshold_pct"])
        held = set(_positions(db, bot["id"]))
        record_marks(db, bot["id"], list(price_map.values()), _now())
        for entry in trenches_data["candidates"]:
            if entry["mint"] in held:
                continue
            gain = gain_3m_pct(db, bot["id"], entry["mint"],
                               entry["price_usd"], now_ts)
            if gain is None or gain < threshold:
                continue
            ok, why = passes_buy_filter(
                entry, rules.get("buy_filter"), gain,
                devnet_watchlist_active=(cfg.network == "devnet"))
            if not ok:
                filtered.append((gain, entry, why))
                continue
            ranked.append((gain, entry))
        ranked.sort(key=lambda item: item[0], reverse=True)
    elif kind == "trenches":
        threshold = float(rules["buy"]["threshold_pct"])
        change_key = _BUY_METRIC_TO_CHANGE_KEY[rules["buy"]["metric"]]
        held = set(_positions(db, bot["id"]))
        # DEVNET-TEST-ONLY (remove before mainnet): watchlist mints
        # have no DexScreener 5-minute change — record price marks
        # for them so the 5-minute gain comes from the loop's own
        # marks, scanner-style. A mint with no mark at least 5
        # minutes old has no gain yet and is not bought.
        _watch = [e for e in trenches_data["candidates"]
                  if e.get("devnet_watchlist")]
        if _watch:
            record_marks(db, bot["id"], _watch, _now())
        for entry in trenches_data["candidates"]:
            if entry.get("devnet_watchlist"):
                change = gain_window_pct(
                    db, bot["id"], entry["mint"], entry["price_usd"],
                    now_ts, 300)
            else:
                change = entry.get(change_key)
            if change is None or float(change) < threshold:
                continue
            if entry["mint"] in held:
                continue
            ok, why = passes_buy_filter(
                entry, rules.get("buy_filter"), float(change),
                devnet_watchlist_active=(cfg.network == "devnet"))
            if not ok:
                filtered.append((float(change), entry, why))
                continue
            ranked.append((float(change), entry))
        ranked.sort(key=lambda item: item[0], reverse=True)
    if kind in ("trenches", "scanner"):
        if not ranked and filtered:
            # Every threshold-passing candidate failed the filter —
            # say so once, naming the strongest rejected coin.
            filtered.sort(key=lambda item: item[0], reverse=True)
            top = filtered[0][1]
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': strongest candidate "
                 f"{top.get('symbol') or top['mint']} failed the buy "
                 f"filter — {filtered[0][2]}. No buy this scan.",
                 bot["mode"])
            actions.append({"action": "skip", "coin": top["mint"],
                            "reason": f"buy filter: {filtered[0][2]}"})
        for _change, entry in ranked:
            if bought >= max_new:
                break
            if len(_positions(db, bot["id"])) >= int(bot["max_open_trades"]):
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': buy of "
                     f"{entry['mint']} skipped — max open trades reached "
                     f"({bot['max_open_trades']}).", bot["mode"])
                actions.append({"action": "skip", "coin": entry["mint"],
                                "reason": "max open trades reached"})
                break
            before = len(actions)
            signals = {
                "m5_pct": entry.get("change_5m_pct"),
                "volume_5m_usd": entry.get("volume_m5_usd"),
                "buys_5m": entry.get("txns_m5_buys"),
                "sells_5m": entry.get("txns_m5_sells"),
                "regime": detect_regime(entry, rules.get("regime")),
            }
            _buy_coin(db, cfg, bot, entry["mint"], entry["price_usd"],
                      _bankroll_prices(),
                      rpc_factory, real_executor, actions,
                      signals=signals, feed=feed,
                      prices_data=prices_data)
            if len(actions) > before and actions[-1]["action"] == "buy":
                bought += 1
    else:
        for coin in json.loads(bot["coins"]):
            if bought >= max_new:
                break
            if coin in _positions(db, bot["id"]):
                continue
            change = _change_for(coin)
            if change is None or float(change) < float(
                    rules["buy"]["threshold_pct"]):
                continue
            if len(_positions(db, bot["id"])) >= int(bot["max_open_trades"]):
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': buy of {coin} skipped "
                     f"— max open trades reached "
                     f"({bot['max_open_trades']}).", bot["mode"])
                actions.append({"action": "skip", "coin": coin,
                                "reason": "max open trades reached"})
                break
            before = len(actions)
            _buy_coin(db, cfg, bot, coin, _price_for(coin),
                      _bankroll_prices(),
                      rpc_factory, real_executor, actions,
                      feed=feed, prices_data=prices_data)
            if len(actions) > before and actions[-1]["action"] == "buy":
                bought += 1
    return actions


def _evaluate_bot(db: sqlite3.Connection, cfg: Config, bot,
                  prices_data: dict, rpc_factory,
                  real_executor, trenches_data=None,
                  feed=None) -> list:
    """One evaluation pass for one silent bot. Returns its actions.

    Bots with a stored rule set go through the rules engine; bots
    without one keep the standard v1 signals; a bot whose stored
    rules are broken makes NO trades at all. `feed` is the live feed,
    passed to the rules engine so executed P&L is priced live.
    """
    rules, rules_error = load_rules(bot)
    if rules_error is not None:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': stored rules are invalid "
             f"({rules_error}) — no trades until the rules are fixed.", bot["mode"])
        return [{"action": "skip", "coin": "", "reason": "invalid rules"}]
    if rules is not None:
        return _evaluate_rules_bot(db, cfg, bot, rules, prices_data,
                                   trenches_data, rpc_factory, real_executor,
                                   feed=feed)
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
                 f"position left alone.", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "no price data"})
            continue
        price = entry["price"]
        stop_price = float(pos["avg_price"]) * (1 - stop_loss_pct / 100.0)
        if _lte(price, stop_price):
            _sell_position(db, cfg, bot, coin, price, "stop_loss",
                           rpc_factory, real_executor, actions,
                           prices_data=prices_data)
            continue
        side, _detail = signal_for_coin(
            coin, entry.get("change_24h"), bot["strategy"])
        if side == "sell":
            _sell_position(db, cfg, bot, coin, price, "sell signal",
                           rpc_factory, real_executor, actions,
                           prices_data=prices_data)

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
                 f"max open trades reached ({bot['max_open_trades']}).", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "max open trades reached"})
            continue
        _buy_coin(db, cfg, bot, coin, entry["price"], prices_usd,
                  rpc_factory, real_executor, actions,
                  prices_data=prices_data)
    return actions


def _held_price(price_fetcher, coin: str, feed=None, cfg=None,
                rpc_factory=None):
    """Current USD price for one held mint, or None when no source
    can serve it. The live feed first (fresh ticks only — now
    including devnet curve prices); a devnet watchlist mint with no
    feed tick is priced straight from its bonding curve; the
    trenches feed last. The watcher only needs a price when a top-10
    trigger has already fired — a missing price means the sell waits
    for the next check, never a guess."""
    if feed is not None:
        try:
            tick = feed.snapshot([coin]).get(coin)
        except Exception:
            tick = None
        if tick is not None and not tick.get("stale"):
            try:
                price = float(tick["price"])
            except (TypeError, ValueError):
                price = None
            else:
                if price and price > 0:
                    return price
    # Devnet watchlist mints have no DexScreener price — price from
    # the bonding curve when the feed has no tick for them.
    if cfg is not None and rpc_factory is not None:
        try:
            from .devnet_watchlist import active_mints, curve_price_usd
            if coin in active_mints(cfg):
                sol_price = _live_sol_price_usd(feed, None)
                if sol_price is not None:
                    client = rpc_factory(cfg.rpc_url)
                    price, _curve = curve_price_usd(
                        client, coin, sol_price)
                    if price:
                        return float(price)
        except Exception:
            pass
    try:
        prices = price_fetcher([coin])
    except Exception:
        return None
    if not isinstance(prices, dict):
        return None
    entry = prices.get(coin) or {}
    price = entry.get("price_usd")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        return None
    return price if price and price > 0 else None


def _default_price_fetcher(mints):
    from .trenches import fetch_trenches_scan
    data = fetch_trenches_scan(mints=mints) or {}
    return data.get("prices", {}) or {}


def top10_watch_once(db: sqlite3.Connection, cfg: Config,
                     rpc_factory, price_fetcher=None,
                     feed=None) -> list:
    """One pass of the top-10 concentration watch, every 15 seconds
    per held coin (the per-coin cadence is gated on
    scanner_top10.last_checked_at, so a pass that runs early skips
    coins already checked).

    For every silent scanner or trenches bot with a top10_rug rule
    and an open position, one RPC round fetches the top-10 total AND
    the total supply, and BOTH top-10 exits run on that data:
      * the 10% dump from peak (existing rule), and
      * the concentration triggers (builderBot): share of total
        supply at or above concentration_pct (default 21%) sells
        everything immediately; a slow climb toward it (rising for
        3 consecutive checks) sells slow_climb_sell_pct (default
        50%) as an early rug warning.
    An RPC failure, an unserved mint, or a missing price when a
    trigger has fired skips that coin for this pass — logged once,
    never faked — and the next pass retries it. Returns the actions
    taken.
    """
    price_fetcher = price_fetcher or _default_price_fetcher
    actions: list = []
    now_ts = datetime.now(timezone.utc).timestamp()
    bots = db.execute("SELECT * FROM bots WHERE silent = 1").fetchall()
    for bot in bots:
        rules, error = load_rules(bot)
        if rules is None or error is not None:
            continue
        if rules["kind"] not in ("scanner", "trenches"):
            continue
        top10 = rules.get("top10_rug")
        if not top10:
            continue
        interval = float(top10.get("check_interval_seconds", 15.0))
        positions = _positions(db, bot["id"])
        if not positions:
            continue
        try:
            client = rpc_factory(cfg.rpc_url)
        except Exception:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': top-10 watch unavailable "
                 f"— no top-10 check this round.", bot["mode"])
            continue
        for coin, pos in list(positions.items()):
            row = db.execute(
                "SELECT last_checked_at FROM scanner_top10 "
                "WHERE bot_id = ? AND mint = ?",
                (bot["id"], coin)).fetchone()
            if row and row["last_checked_at"]:
                last_ts = _parse_scan_time(row["last_checked_at"])
                if last_ts is not None and now_ts - last_ts < interval:
                    continue
            total = top10_total(client, coin)
            if total is None:
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': top-10 watch "
                     f"unavailable for {coin} — position left alone "
                     f"this round.", bot["mode"])
                continue
            try:
                supply = int(client.get_token_supply(coin))
            except Exception:
                supply = None
            # 1. The dump-from-peak exit.
            fired, reason = check_top10_rug(
                db, bot["id"], coin, total, float(top10["drop_pct"]),
                _now())
            if fired:
                price = _held_price(price_fetcher, coin, feed=feed,
                                    cfg=cfg, rpc_factory=rpc_factory)
                if price is None:
                    _log(db, bot["user_id"], "silent_skip",
                         f"Silent bot '{bot['name']}': {reason} for "
                         f"{coin} but no price data — sell waits for "
                         f"the next check.", bot["mode"])
                    continue
                sell_reason = reason
                if bot["mode"] == "real":
                    sell_reason += " — proceeds in SOL"
                _sell_position(db, cfg, bot, coin, price, sell_reason,
                               rpc_factory, default_real_executor, actions,
                               feed=feed)
                continue
            # 2. The concentration triggers (21% / slow climb).
            action, reason = check_top10_concentration(
                db, bot["id"], coin, total, supply,
                float(top10.get("concentration_pct", 21.0)),
                float(top10.get("slow_climb_sell_pct", 50.0)), _now())
            if action is None:
                continue
            price = _held_price(price_fetcher, coin, feed=feed,
                                cfg=cfg, rpc_factory=rpc_factory)
            if price is None:
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': {reason} for {coin} "
                     f"but no price data — sell waits for the next "
                     f"check.", bot["mode"])
                continue
            sell_reason = reason
            if bot["mode"] == "real":
                sell_reason += " — proceeds in SOL"
            if action == "sell_all":
                _sell_position(db, cfg, bot, coin, price, sell_reason,
                               rpc_factory, default_real_executor, actions,
                               feed=feed)
            else:
                amount = (float(pos["amount"])
                          * float(top10.get("slow_climb_sell_pct", 50.0))
                          / 100.0)
                _sell_position(db, cfg, bot, coin, price, sell_reason,
                               rpc_factory, default_real_executor, actions,
                               amount=amount, feed=feed)
    return actions


_WATCH_STARTED = False
_WATCH_LOCK = threading.Lock()


def top10_watch_loop(db_path: str, cfg: Config,
                     rpc_factory=None, price_fetcher=None,
                     feed=None) -> None:
    """The 15-second watch, forever: one pass, then sleep 15 seconds.
    Opens a fresh DB connection per pass. A failed pass is logged and
    the next one still runs. `feed` is the live feed, so a fired
    trigger prices from fresh ticks (including devnet curve prices)
    instead of DexScreener alone."""
    rpc_factory = rpc_factory or SolanaRpcClient
    while True:
        db = init_db(db_path)
        try:
            top10_watch_once(db, cfg, rpc_factory,
                             price_fetcher=price_fetcher, feed=feed)
        except Exception as exc:
            print(f"[top10-watch] pass failed: {exc}", flush=True)
        finally:
            db.close()
        time.sleep(15)


def run_once(db: sqlite3.Connection, cfg: Config, price_fetcher=None,
             rpc_factory=None, real_executor=None,
             trenches_fetcher=None, feed=None) -> list:
    """One loop iteration over every silent-enabled bot.

    Returns a list of per-bot outcome dicts. A failure for one bot is
    recorded and never stops the others. The trenches feed (DexScreener)
    is fetched at most once per iteration, only when at least one
    silent bot has a valid trenches or scanner rule set. When a live
    feed is passed, both price sources are sanitized against it first:
    a price the feed rejects as an absurd single-tick jump never
    reaches an evaluation or a snapshot — the coin reads as unpriced
    for this iteration instead. With no feed, prices pass through
    exactly as before.
    """
    price_fetcher = price_fetcher or get_prices
    rpc_factory = rpc_factory or SolanaRpcClient
    real_executor = real_executor or default_real_executor
    trenches_fetcher = trenches_fetcher or fetch_trenches_scan
    prices_data = price_fetcher()
    bots = db.execute(
        "SELECT * FROM bots WHERE silent = 1 ORDER BY id").fetchall()
    trenches_data = None
    needs_trenches = False
    for bot in bots:
        rules, err = load_rules(bot)
        if rules is not None and rules["kind"] in ("trenches", "scanner"):
            needs_trenches = True
            break
    if needs_trenches:
        held_mints = [r["coin_id"] for r in db.execute(
            "SELECT DISTINCT coin_id FROM bot_positions WHERE bot_id IN "
            "(SELECT id FROM bots WHERE silent = 1)").fetchall()]
        trenches_data = trenches_fetcher(mints=held_mints)
        # DEVNET-TEST-ONLY (remove before mainnet): add the
        # devnet-mint watchlist as extra scan candidates, priced
        # from their bonding curves. Inert unless NETWORK is
        # devnet — see tradingbot/devnet_watchlist.py. Enriched
        # BEFORE the feed sanitizer so a curve price that jumps
        # absurdly in one scan is dropped like any other bad tick.
        # The SOL price comes from the live feed first — a snapshot
        # SOL price must never price a curve trade.
        sol_price_usd = _live_sol_price_usd(feed, prices_data)
        trenches_data = enrich_scan(trenches_data, cfg, rpc_factory,
                                    sol_price_usd)
    if feed is not None:
        # Bad-tick guard: an absurd price never becomes a buy/sell
        # signal or a snapshot value. The sanitizers return copies;
        # the fetched data is untouched.
        prices_data = feed.sanitize_prices_data(prices_data)
        if trenches_data is not None:
            trenches_data = feed.sanitize_trenches(trenches_data)
    outcomes = []
    for bot in bots:
        # Platform gate: real execution is built against the pump.fun
        # bonding curve only. A real bot on another platform (pumpSwap,
        # Phantom) is skipped with a plain reason — never faked.
        # Paper bots are simulated and platform-agnostic, so they are
        # never gated. A bot from before platforms existed has no
        # platform set and is treated as pump.fun.
        platform = (bot["platform"] if "platform" in bot.keys() else "") \
            or "pump.fun"
        if bot["mode"] == "real" \
                and platform not in REAL_TRADEABLE_PLATFORMS:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': real trading on "
                 f"{platform} is not live yet — real trades run on "
                 f"pump.fun (devnet) only. No trade was made.",
                 bot["mode"])
            outcomes.append({"bot_id": bot["id"], "bot_name": bot["name"],
                             "user_id": bot["user_id"],
                             "mode": bot["mode"],
                             "actions": [{"action": "skip", "coin": "",
                                          "reason": "real trading on "
                                          f"{platform} is not live yet"}],
                             "error": None})
            continue
        try:
            actions = _evaluate_bot(db, cfg, bot, prices_data, rpc_factory,
                                    real_executor,
                                    trenches_data=trenches_data, feed=feed)
            outcomes.append({"bot_id": bot["id"], "bot_name": bot["name"],
                             "user_id": bot["user_id"], "mode": bot["mode"],
                             "actions": actions, "error": None})
        except Exception as exc:  # one bad bot never stops the loop
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': evaluation failed — "
                 f"{str(exc)[:120]}. No trade was made.", bot["mode"])
            outcomes.append({"bot_id": bot["id"], "bot_name": bot["name"],
                             "user_id": bot["user_id"], "mode": bot["mode"],
                             "actions": [], "error": str(exc)[:200]})
    _record_snapshots(db, cfg, prices_data, trenches_data, rpc_factory,
                      {o["user_id"] for o in outcomes})
    # builderBot reviews each evaluated bot's trade ledger: a losing
    # bot's rules tighten one small step (never widen). A review
    # failure never breaks the loop.
    from .builderbot import review_bot
    for outcome in outcomes:
        try:
            bot = db.execute("SELECT * FROM bots WHERE id = ?",
                             (outcome["bot_id"],)).fetchone()
            if bot is not None:
                review_bot(db, bot, cfg.encryption_key)
        except Exception:
            pass
    return outcomes


def _record_snapshots(db: sqlite3.Connection, cfg: Config,
                      prices_data: dict, trenches_data: dict | None,
                      rpc_factory, user_ids: set) -> None:
    """One P&L snapshot per user evaluated this iteration.

    Prices merge the CoinGecko list with the trenches feed so held
    memecoin mints are valued at the price the loop just saw. A price
    of 0 or less is dropped from the map — missing, never zero — so a
    snapshot can never value a holding at $0 and poison the 24h
    baseline. The on-chain SOL balance is recorded when the RPC
    answers, else NULL (shown as "unavailable"). A snapshot failure
    is logged to stdout and never breaks the loop.
    """
    merged = {}
    for c, e in prices_data.get("prices", {}).items():
        price = (e or {}).get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            merged[c] = price
    if trenches_data:
        for mint, entry in (trenches_data.get("prices") or {}).items():
            price = (entry or {}).get("price_usd")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            if price is not None and price > 0:
                merged[mint] = price
    for user_id in sorted(user_ids):
        try:
            totals = portfolio_totals(db, user_id, merged)
            real_sol = None
            try:
                wallet = db.execute(
                    "SELECT pubkey FROM wallets WHERE user_id = ?",
                    (user_id,)).fetchone()
                if wallet:
                    client = rpc_factory(cfg.rpc_url)
                    real_sol = lamports_to_sol(
                        client.get_balance_lamports(wallet["pubkey"]))
            except Exception:
                real_sol = None
            record_snapshot(db, user_id, totals["cash_usd"],
                            totals["holdings_value_usd"], real_sol)
        except Exception as exc:  # snapshots never break trading
            print(f"[silent-loop] snapshot failed for user {user_id}: "
                  f"{exc}", flush=True)


def loop_forever(db_path: str, cfg: Config, interval: int | None = None,
                 price_fetcher=None, rpc_factory=None,
                 real_executor=None, once: bool = False,
                 trenches_fetcher=None, feed=None) -> None:
    """Run iterations forever (or once with once=True). Opens a fresh
    DB connection per iteration so the web process's connection is
    never shared across threads. The 15-second top-10 watch starts
    alongside (once per process, not in once mode) — it runs on its
    own thread so its cadence does not depend on the loop interval."""
    interval = interval if interval is not None \
        else cfg.silent_loop_interval_seconds
    if not once:
        global _WATCH_STARTED
        with _WATCH_LOCK:
            if not _WATCH_STARTED:
                _WATCH_STARTED = True
                # The watcher's prices come from the same trenches
                # feed the loop uses when one is injected (tests do
                # this); otherwise it fetches the live feed itself.
                def _injected_prices(mints, _tf=trenches_fetcher):
                    data = _tf(mints=mints) or {}
                    return data.get("prices", {}) or {}

                watch_price_fetcher = (_injected_prices
                                       if trenches_fetcher is not None
                                       else None)
                threading.Thread(
                    target=top10_watch_loop,
                    args=(db_path, cfg),
                    kwargs={"rpc_factory": rpc_factory,
                            "price_fetcher": watch_price_fetcher,
                            "feed": feed},
                    daemon=True, name="top10-watch").start()
                print("[top10-watch] background watch started "
                      "(every 15s)", flush=True)
    while True:
        db = init_db(db_path)
        try:
            outcomes = run_once(db, cfg, price_fetcher=price_fetcher,
                                rpc_factory=rpc_factory,
                                real_executor=real_executor,
                                trenches_fetcher=trenches_fetcher,
                                feed=feed)
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
        kwargs={"feed": app.config.get("LIVE_FEED")},
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
