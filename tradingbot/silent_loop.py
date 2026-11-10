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
from .platforms import REAL_TRADEABLE_PLATFORMS
from .paper import (InsufficientCash, InsufficientHolding, paper_bankroll,
                    paper_buy, paper_cash, paper_sell)
from .pnl import portfolio_totals, record_snapshot
from .prices import get_prices
from .rules import load_rules
from .scanner import (check_top10_rug, clear_watches, detect_regime,
                      gain_3m_pct, next_ladder_rung,
                      passes_buy_filter, record_marks, top10_total)
from .signals import signal_for_coin
from .solana import SolanaRpcClient, lamports_to_sol
from .trenches import fetch_trenches_scan

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


def default_real_executor(bot, user_id: int, coin: str, side: str,
                         amount: float, price: float, db, cfg) -> dict:
    """Real executor: the account's dedicated signing bot executes
    pump.fun trades on devnet (see executor.py). Anything unverified
    is skipped honestly — never a fake fill."""
    return execute_real_trade(bot, user_id, coin, side, amount, price,
                              db, cfg)


def _positions(db: sqlite3.Connection, bot_id: int) -> dict:
    rows = db.execute(
        "SELECT * FROM bot_positions WHERE bot_id = ?", (bot_id,)).fetchall()
    return {r["coin_id"]: r for r in rows}


def _open_position(db: sqlite3.Connection, bot, coin: str, amount: float,
                   price: float) -> None:
    """Record (or merge into) a bot position after a fill.

    peak_price tracks the highest price since the position opened (for
    the trailing stop) and orig_amount the size at open (sell-ladder
    rungs are % of it). Merging into an existing position keeps the
    existing peak and grows the original amount.
    """
    row = db.execute(
        "SELECT amount, avg_price, peak_price, orig_amount, ladder_stage "
        "FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
        (bot["id"], coin)).fetchone()
    if row:
        total = row["amount"] + amount
        avg = ((row["amount"] * row["avg_price"]) + amount * price) / total
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
            (bot["user_id"], bot["id"], coin, amount, price,
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
                   ladder_stage: int | None = None) -> None:
    """Sell a bot's position in `coin` (paper ledger, or the real
    executor). `amount` None means the whole position; otherwise a
    partial sell (sell-ladder rung). `ladder_stage` advances the
    position's rung counter when given. Logs the outcome either way;
    never fakes a fill."""
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
                 f"for ${usd:.2f} ({reason}).", bot["mode"])
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
        if ladder_stage is not None:
            db.execute("UPDATE bot_positions SET ladder_stage = ? "
                       "WHERE bot_id = ? AND coin_id = ?",
                       (ladder_stage, bot["id"], coin))
            db.commit()
        _log(db, bot["user_id"], "silent_sell",
             f"Silent bot '{bot['name']}' sold {amount:.4f} {coin} "
             f"({reason}). Signature: "
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
              signals: dict | None = None) -> None:
    """Buy `coin` for a bot within its limits, or skip with a reason.

    `signals` is the buy-time signal snapshot from the feed entry
    (m5 gain %, 5-minute volume, buys/sells, regime) — recorded on
    the paper trade so the losing-buy analysis can show how the coin
    got bought. None for v1 bots and manual buys.
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
             f"for ${usd:.2f} (paper).", bot["mode"])
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
            _open_position(db, bot, coin, coin_amount, price)
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
                        rpc_factory, real_executor) -> list:
    """One evaluation pass for a bot with a custom rule set.

    Fail-closed: a feed error, missing price data, or an unusable rule
    means no trades this scan. The bot's own limits (buy size %,
    stop loss %, max open trades, balance) always apply on top of
    the rules — the rules never widen them.
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
                               rpc_factory, real_executor, actions)
                continue
            db.execute("UPDATE bot_positions SET last_price = ?, "
                       "last_price_at = ? WHERE bot_id = ? AND coin_id = ?",
                       (price, _now(), bot["id"], coin))
            db.commit()
        # 0b. Top-10 wallets watch (scanner only): the top 10 holders
        # dumping sells everything. It runs alongside the fast-drop
        # rug sell — either one fires the exit. The baseline is the
        # top-10 total the first time the position is watched,
        # tracked upward to the peak since holding; a 10% drop from
        # that peak is the dump. If the RPC cannot serve the mint,
        # the watch is skipped for this scan — logged once per bot,
        # never faked, and the other exits still apply.
        top10 = rules.get("top10_rug")
        if kind == "scanner" and top10:
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
                                       real_executor, actions)
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
                           rpc_factory, real_executor, actions)
            continue
        trail_price = peak * (1 - trailing_pct / 100.0)
        if _lte(price, trail_price):
            _sell_position(db, cfg, bot, coin, price, "trailing stop",
                           rpc_factory, real_executor, actions)
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
                    amount=orig * sell_frac, ladder_stage=stage + 1)
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
                    ladder_stage=stage + 1)

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
                entry, rules.get("buy_filter"), gain)
            if not ok:
                filtered.append((gain, entry, why))
                continue
            ranked.append((gain, entry))
        ranked.sort(key=lambda item: item[0], reverse=True)
    elif kind == "trenches":
        threshold = float(rules["buy"]["threshold_pct"])
        change_key = _BUY_METRIC_TO_CHANGE_KEY[rules["buy"]["metric"]]
        held = set(_positions(db, bot["id"]))
        for entry in trenches_data["candidates"]:
            change = entry.get(change_key)
            if change is None or float(change) < threshold:
                continue
            if entry["mint"] in held:
                continue
            ok, why = passes_buy_filter(
                entry, rules.get("buy_filter"), float(change))
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
                      {c: e.get("price", 0.0)
                       for c, e in prices_data["prices"].items()},
                      rpc_factory, real_executor, actions,
                      signals=signals)
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
                      {c: e.get("price", 0.0)
                       for c, e in prices_data["prices"].items()},
                      rpc_factory, real_executor, actions)
            if len(actions) > before and actions[-1]["action"] == "buy":
                bought += 1
    return actions


def _evaluate_bot(db: sqlite3.Connection, cfg: Config, bot,
                  prices_data: dict, rpc_factory,
                  real_executor, trenches_data=None) -> list:
    """One evaluation pass for one silent bot. Returns its actions.

    Bots with a stored rule set go through the rules engine; bots
    without one keep the standard v1 signals; a bot whose stored
    rules are broken makes NO trades at all.
    """
    rules, rules_error = load_rules(bot)
    if rules_error is not None:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': stored rules are invalid "
             f"({rules_error}) — no trades until the rules are fixed.", bot["mode"])
        return [{"action": "skip", "coin": "", "reason": "invalid rules"}]
    if rules is not None:
        return _evaluate_rules_bot(db, cfg, bot, rules, prices_data,
                                   trenches_data, rpc_factory, real_executor)
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
                 f"max open trades reached ({bot['max_open_trades']}).", bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "max open trades reached"})
            continue
        _buy_coin(db, cfg, bot, coin, entry["price"], prices_usd,
                  rpc_factory, real_executor, actions)
    return actions


def run_once(db: sqlite3.Connection, cfg: Config, price_fetcher=None,
             rpc_factory=None, real_executor=None,
             trenches_fetcher=None) -> list:
    """One loop iteration over every silent-enabled bot.

    Returns a list of per-bot outcome dicts. A failure for one bot is
    recorded and never stops the others. The trenches feed (DexScreener)
    is fetched at most once per iteration, only when at least one
    silent bot has a valid trenches or scanner rule set.
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
                                    trenches_data=trenches_data)
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
    return outcomes


def _record_snapshots(db: sqlite3.Connection, cfg: Config,
                      prices_data: dict, trenches_data: dict | None,
                      rpc_factory, user_ids: set) -> None:
    """One P&L snapshot per user evaluated this iteration.

    Prices merge the CoinGecko list with the trenches feed so held
    memecoin mints are valued at the price the loop just saw. The
    on-chain SOL balance is recorded when the RPC answers, else NULL
    (shown as "unavailable"). A snapshot failure is logged to stdout
    and never breaks the loop.
    """
    merged = {c: e.get("price", 0.0)
              for c, e in prices_data.get("prices", {}).items()}
    if trenches_data:
        for mint, entry in (trenches_data.get("prices") or {}).items():
            if entry.get("price_usd") is not None:
                merged[mint] = entry["price_usd"]
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
                 trenches_fetcher=None) -> None:
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
                                real_executor=real_executor,
                                trenches_fetcher=trenches_fetcher)
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
