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
from .coin_meta import (ensure_coin_meta, get_coin_meta, metadata_pda,
                        parse_metadata_account)
from .devnet_watchlist import active_mints, enrich_scan
from .executor import _skip, execute_real_trade, is_curve_complete
from .jupiter_executor import execute_jupiter_swap
from .ledger import record_trade
from .paper import (InsufficientCash, InsufficientHolding, paper_bankroll,
                    paper_buy, paper_cash, paper_sell)
from .pnl import portfolio_totals, record_snapshot
from .prices import get_prices
from .pumpfun import (bonding_curve_pda, curve_progress_pct,
                      parse_bonding_curve)
from .rules import (GNAT_BREAKEVEN_PLUS_PCT, GNAT_BUY_SIZE_PCT,
                    GNAT_BUY_SIZE_SOL, GNAT_CURVE_MAX_PCT,
                    GNAT_CURVE_MIN_PCT, GNAT_EXHAUST_DROP_5M_PCT,
                    GNAT_EXHAUST_MIN_PEAK_PCT, GNAT_MAX_CONSEC_LOSSES,
                    GNAT_MAX_DAILY_LOSS_SOL, GNAT_MIN_BUY_SELL_RATIO,
                    GNAT_MIN_BUYERS_5M, GNAT_REQUIRE_SOCIALS,
                    GNAT_STOP_LOSS_PCT, GNAT_TIER1_PROFIT_PCT,
                    GNAT_TIER2_PROFIT_PCT, GNAT_TIME_DECAY_MIN_GAIN_PCT,
                    GNAT_TIME_DECAY_SECONDS, GNAT_TRAIL_HIGH_PCT,
                    GNAT_TRAIL_LOW_PCT, GNAT_TRAIL_TIGHTEN_AT_PCT,
                    load_rules)
from .scanner import (BREAKEVEN_TRIGGER_PCT, check_top10_concentration,
                      check_top10_rug, clear_watches, detect_regime,
                      gain_3m_pct, gain_window_pct, next_ladder_rung,
                      passes_buy_filter, passes_momentum_gate, record_marks,
                      signal_dead, top10_share, top10_total)
from .signals import signal_for_coin
from .solana import SolanaRpcClient, is_valid_address, lamports_to_sol
from .trenches import (fetch_mint_24h_changes, fetch_prices_for_mints,
                       fetch_trenches_scan)

MIN_PAPER_TRADE_USD = 1.0
REAL_SWAP_UNAVAILABLE = "real swap not available in v1"

# --- tuning build 2026-11-15: shared-engine guards ---------------------
# REMY DEFAULTS — pending James's call, not his rules. The loss
# prevention thresholds (drawdown, loss streak, breakeven) are
# defaults he can adjust before any push; the 30-minute cooldown
# and max-hold come from the tuning report.
DRAWDOWN_BREAKER_PCT = 10.0  # halt new buys when today's realized
# P&L falls to -10% of the starting capital; resumes next day.
LOSS_STREAK_COUNT = 3  # consecutive losing closed trades before
# new buys pause.
LOSS_STREAK_PAUSE_SECONDS = 1800  # 30-minute pause after a streak.
REBUY_COOLDOWN_SECONDS = 1800  # 30 minutes before re-buying a coin
# closed at a loss.
MAX_HOLD_SECONDS = 1800  # 30 minutes: exit at market if no ladder
# rung hit.
# Sell-in-flight guard (tick-driven exits + loop backstop
# idempotency): a position id claimed here is being sold right now —
# a second sell for it (tick + loop racing) is refused, never
# executed twice. Same process (loop + feed threads), so an
# in-memory set is enough.
_SELL_IN_FLIGHT: set = set()
_SELL_IN_FLIGHT_LOCK = threading.Lock()
# Top-10 watch skip-log throttle: once per hour per (bot, mint), so
# an unserved mint does not flood the activity log one row per scan.
_TOP10_SKIP_LOGGED_AT: dict = {}
TOP10_SKIP_LOG_SECONDS = 3600
# Tick-driven fast exits: mints dirtied by live-feed ticks, and the
# event the fast-exit thread waits on. The feed's poller thread only
# marks the mint dirty — it never sells inline.
_TICK_DIRTY_MINTS: set = set()
_TICK_DIRTY_LOCK = threading.Lock()
_TICK_EVENT = threading.Event()
_FAST_EXIT_STARTED = False
_FAST_EXIT_LOCK = threading.Lock()


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


# --- tuning build 2026-11-15: shared-engine guards ---------------------

def _top10_client_for_mint(cfg, mint: str, rpc_factory, cache: dict):
    """The RPC client that can serve a mint's top-10 data: the devnet
    RPC for devnet watchlist mints, the MAINNET RPC for every other
    mint. The old code read every mint through cfg.rpc_url (the
    devnet RPC on devnet), which cannot serve mainnet mints at all
    — the watch was dead for every mainnet coin. Clients are cached
    per network in `cache` (one client per network per evaluation).
    Raises when the client cannot be built — the caller treats that
    as "watch unavailable", never as a pass."""
    try:
        watchlist = active_mints(cfg)
    except Exception:
        watchlist = []
    if mint in watchlist:
        key, url = "devnet", cfg.rpc_url
    else:
        key, url = "mainnet", cfg.mainnet_rpc_url
    if key not in cache:
        cache[key] = rpc_factory(url)
    return cache[key]


def _log_top10_skip_throttled(db, user_id: int, bot, coin: str,
                              detail: str, mode: str, now_ts: float
                              ) -> None:
    """Log a top-10 "watch unavailable" line at most once per hour
    per (bot, mint) — an unserved mint used to flood the activity
    log one row per scan. Never raises."""
    key = (int(bot["id"]), coin)
    last = _TOP10_SKIP_LOGGED_AT.get(key)
    if last is not None and now_ts - last < TOP10_SKIP_LOG_SECONDS:
        return
    _TOP10_SKIP_LOGGED_AT[key] = now_ts
    try:
        _log(db, user_id, "silent_skip", detail, mode)
    except Exception:
        pass


def _claim_position_for_sell(position_id: int) -> bool:
    """Claim a position for a sell in flight. False when another
    thread is already selling it (tick-driven exit vs loop backstop)
    — the caller must NOT sell then. Always paired with
    _release_position_for_sell in a finally."""
    with _SELL_IN_FLIGHT_LOCK:
        if position_id in _SELL_IN_FLIGHT:
            return False
        _SELL_IN_FLIGHT.add(position_id)
        return True


def _release_position_for_sell(position_id: int) -> None:
    with _SELL_IN_FLIGHT_LOCK:
        _SELL_IN_FLIGHT.discard(position_id)


def _signal_confirmed(db, bot_id: int, mint: str, gain: float,
                      now_ts: float, scan_interval: int) -> bool:
    """2-scan entry confirmation. A signal that passes the buy
    threshold + filter + momentum gate is recorded on the first
    scan and buys only when it is STILL passing on a later scan —
    a one-scan spike never buys. Returns True when the signal was
    already recorded (confirmed); the confirmation row is consumed
    (deleted) on a confirm, so a buy blocked after confirming must
    re-confirm on a later scan. Stale rows (older than twice the
    bot's scan interval, at least 10 minutes) are pruned."""
    try:
        expiry = max(600.0, 2.0 * float(scan_interval))
        cutoff = datetime.fromtimestamp(now_ts - expiry,
                                        tz=timezone.utc).strftime(
                                            "%Y-%m-%dT%H:%M:%SZ")
        db.execute("DELETE FROM scanner_signal_confirmations "
                   "WHERE bot_id = ? AND first_seen_at < ?",
                   (bot_id, cutoff))
        db.commit()
        row = db.execute(
            "SELECT 1 FROM scanner_signal_confirmations "
            "WHERE bot_id = ? AND mint = ?",
            (bot_id, mint)).fetchone()
        if row:
            db.execute("DELETE FROM scanner_signal_confirmations "
                       "WHERE bot_id = ? AND mint = ?",
                       (bot_id, mint))
            db.commit()
            return True
        db.execute(
            "INSERT INTO scanner_signal_confirmations "
            "(bot_id, mint, first_seen_at, first_gain_pct) "
            "VALUES (?, ?, ?, ?)",
            (bot_id, mint,
             datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%SZ"),
             float(gain)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        # A confirmation-table failure is a fail-CLOSED buy: without
        # a trustworthy confirmation record, the signal does not buy.
        return False
    return False


def _cooldown_until(db, bot_id: int, mint: str,
                    now_ts: float) -> float | None:
    """The epoch second until which (bot, mint) may not be re-bought
    (30 minutes after a losing close), or None when no cooldown
    applies. Expired rows are pruned."""
    try:
        now_s = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        db.execute("DELETE FROM bot_buy_cooldowns "
                   "WHERE bot_id = ? AND blocked_until <= ?",
                   (bot_id, now_s))
        db.commit()
        row = db.execute(
            "SELECT blocked_until FROM bot_buy_cooldowns "
            "WHERE bot_id = ? AND mint = ?", (bot_id, mint)).fetchone()
        if not row or not row["blocked_until"]:
            return None
        parsed = _parse_scan_time(row["blocked_until"])
        if parsed is None or parsed <= now_ts:
            return None
        return parsed
    except Exception:
        return None


def _set_cooldown(db, bot_id: int, mint: str, reason: str,
                  now_ts: float) -> None:
    """Block re-buying (bot, mint) for REBUY_COOLDOWN_SECONDS after
    a losing close. Never raises — a failed write simply means no
    cooldown is recorded."""
    try:
        until = datetime.fromtimestamp(
            now_ts + REBUY_COOLDOWN_SECONDS,
            tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "INSERT INTO bot_buy_cooldowns "
            "(bot_id, mint, blocked_until, reason) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bot_id, mint) DO UPDATE SET "
            "blocked_until = excluded.blocked_until, "
            "reason = excluded.reason",
            (bot_id, mint, until, reason or ""))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _day_start_capital(db, bot, feed, prices_data):
    """The bot's starting capital for TODAY (UTC), in USD, or None
    when it cannot be determined. Paper bots: the user's paper
    portfolio value at the latest snapshot before midnight (the
    paper bankroll is shared across the user's paper bots, so the
    portfolio is the capital). Real bots: the app wallet's SOL at
    the latest snapshot before midnight, priced at the live SOL
    price. Fallbacks: the earliest snapshot today, then the current
    bankroll / wallet balance. A capital that cannot be determined
    means the drawdown breaker cannot be evaluated — it does not
    trip, and the reason is recorded by the caller."""
    user_id = bot["user_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = db.execute(
            "SELECT total_value_usd, real_sol FROM portfolio_snapshots "
            "WHERE user_id = ? AND substr(created_at, 1, 10) < ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, today)).fetchone()
        if row is None:
            row = db.execute(
                "SELECT total_value_usd, real_sol FROM "
                "portfolio_snapshots WHERE user_id = ? "
                "AND substr(created_at, 1, 10) = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (user_id, today)).fetchone()
        if bot["mode"] == "paper":
            if row is not None and row["total_value_usd"] is not None:
                return float(row["total_value_usd"])
            return None
        # Real mode: SOL at day start x live SOL price.
        sol_price = _live_sol_price_usd(feed, prices_data)
        if sol_price is None:
            return None
        real_sol = None
        if row is not None and row["real_sol"] is not None:
            real_sol = float(row["real_sol"])
        if real_sol is None:
            return None
        return real_sol * sol_price
    except Exception:
        return None


def _buy_halt(db, bot, now_ts: float, feed,
              prices_data: dict | None) -> tuple[bool, str]:
    """Loss-prevention halts on NEW BUYS for a scanner/signals bot.
    Exits still run — a halted bot can still stop out, it just
    cannot open anything new. Returns (halted, reason).

    1. Daily drawdown circuit breaker (REMY DEFAULT -10%): today's
       realized P&L at or below -10% of the starting capital halts
       buys for the rest of the day; the day rolling over resumes
       them (today's realized P&L resets to zero).
    2. Loss-streak halt (REMY DEFAULT 3 consecutive losing closed
       trades): new buys pause for 30 minutes from the latest loss.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM trade_ledger "
            "WHERE bot_id = ? AND side = 'sell' AND pnl_usd IS NOT NULL "
            "AND substr(created_at, 1, 10) = ?",
            (bot["id"], today)).fetchone()
        daily_realized = float(row[0]) if row else 0.0
    except Exception:
        daily_realized = 0.0
    capital = _day_start_capital(db, bot, feed, prices_data)
    if capital is not None and capital > 0 and daily_realized <= (
            -DRAWDOWN_BREAKER_PCT / 100.0 * capital):
        return True, (
            f"daily drawdown circuit breaker: realized "
            f"${daily_realized:.2f} today is at or below "
            f"-{DRAWDOWN_BREAKER_PCT:.0f}% of the ${capital:.2f} "
            f"starting capital — no new buys until tomorrow")
    try:
        rows = db.execute(
            "SELECT win, created_at FROM trade_ledger "
            "WHERE bot_id = ? AND side = 'sell' "
            "ORDER BY id DESC LIMIT ?",
            (bot["id"], LOSS_STREAK_COUNT)).fetchall()
    except Exception:
        rows = []
    streak = 0
    anchor = None
    for r in rows:
        if r["win"] == 0:
            streak += 1
            if anchor is None:
                anchor = r["created_at"]
        else:
            break
    if streak >= LOSS_STREAK_COUNT and anchor:
        anchor_ts = _parse_scan_time(anchor)
        if anchor_ts is not None and now_ts < (
                anchor_ts + LOSS_STREAK_PAUSE_SECONDS):
            until = datetime.fromtimestamp(
                anchor_ts + LOSS_STREAK_PAUSE_SECONDS,
                tz=timezone.utc).strftime("%H:%M UTC")
            return True, (
                f"loss-streak halt: {streak} consecutive losing "
                f"closed trades — no new buys until {until}")
    return False, ""


def _breaker_logged(db, bot_id: int, marker: str,
                    since: str | None = None) -> bool:
    """True when a circuit-breaker activity row for this bot with
    `marker` in its detail already exists (today, or since `since`
    when given) — so the halt is logged once, not once per scan."""
    try:
        if since is None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = db.execute(
                "SELECT 1 FROM activity WHERE user_id = (SELECT user_id "
                "FROM bots WHERE id = ?) AND kind = 'silent_breaker' "
                "AND detail LIKE ? AND substr(created_at, 1, 10) = ? "
                "LIMIT 1",
                (bot_id, f"%{marker}%", today)).fetchone()
        else:
            row = db.execute(
                "SELECT 1 FROM activity WHERE user_id = (SELECT user_id "
                "FROM bots WHERE id = ?) AND kind = 'silent_breaker' "
                "AND detail LIKE ? AND created_at >= ? LIMIT 1",
                (bot_id, f"%{marker}%", since)).fetchone()
        return row is not None
    except Exception:
        return False


def _prebuy_scam_gate(db, cfg, bot, entry: dict, rules: dict,
                      rpc_factory, rpc_cache: dict) -> tuple[bool, str]:
    """Pre-buy scam gates for a coin about to be bought (scanner and
    signals kinds). Returns (True, "") when the coin passes, else
    (False, reason). Fail-closed at every step: a check that cannot
    be verified rejects the buy — an unverified coin is never
    bought on a guess. Only enforced what the app can actually
    check with data it has:
      1. Top-10 holder concentration at entry (the bot's own
         concentration threshold — Clyde 12%, Scanner 21%): reject
         at or over the threshold, via the fixed mainnet RPC client.
      2. Mint / freeze authority via RPC getAccountInfo on the
         mint: reject when the mint authority is still enabled
         (supply can be inflated) or the freeze authority is still
         enabled (holders can be frozen).
      3. Metadata / social presence via the stored coin_meta row
         (fetched on demand): reject when the coin has no usable
         metadata — no name AND no symbol.
    Devnet watchlist mints (known test coins, not discovered coins)
    skip checks 2 and 3 — they are the operator's own test mints.
    NOT enforced here (recorded in the MANIFEST, never faked):
    bundler / block-zero detection, dev-history blacklists,
    unique-wallet counts, creator-sell monitoring, and
    placeholder-image detection — the app has no data source for
    any of them."""
    mint = entry.get("mint")
    if not mint:
        return False, "no mint to check"
    try:
        concentration = float(
            (rules.get("top10_rug") or {}).get("concentration_pct",
                                               21.0))
    except (TypeError, ValueError):
        concentration = 21.0
    try:
        client = _top10_client_for_mint(cfg, mint, rpc_factory,
                                        rpc_cache)
    except Exception:
        return False, "top-10 data unavailable (no RPC client)"
    try:
        _top10_total, _supply, share = top10_share(client, mint)
    except Exception:
        share = None
    if share is None:
        return False, ("top-10 concentration unverifiable — the RPC "
                       "cannot serve this mint")
    if share >= concentration:
        return False, (f"top-10 holders own {share:.1f}% of supply "
                       f"(at or over the {concentration:.0f}% bar)")
    if entry.get("devnet_watchlist"):
        return True, ""
    try:
        authorities = client.get_mint_authorities(mint)
    except Exception:
        authorities = None
    if authorities is None:
        return False, "mint authority unverifiable"
    mint_on, freeze_on = authorities
    if mint_on:
        return False, ("mint authority still enabled — supply can "
                       "be inflated")
    if freeze_on:
        return False, ("freeze authority still enabled — holders "
                       "can be frozen")
    try:
        meta = get_coin_meta(db, mint)
        if meta is None or (meta.get("source") in (None, "none")):
            fetched = ensure_coin_meta(
                db, mint,
                rpc_url=(cfg.rpc_url if mint in active_mints(cfg)
                         else cfg.mainnet_rpc_url))
            meta = fetched or meta
        if meta is None or (not meta.get("name")
                            and not meta.get("symbol")):
            return False, "no usable metadata (no name or symbol)"
    except Exception:
        return False, "metadata unavailable"
    return True, ""


def _record_signal_entry(db, bot_id: int, mint: str, signals: dict,
                         now: str) -> None:
    """Log an entry signal's features with the trade it approved
    (bot_signal_log). LOGGING ONLY — nothing here ever changes a
    rule. Never raises."""
    try:
        # One open row per (bot, mint): an older open row means its
        # position was cleared without a sell (already-sold holding)
        # — close it as unknown rather than letting a later close
        # fill it in with another trade's outcome.
        db.execute(
            "UPDATE bot_signal_log SET outcome_at = ? "
            "WHERE bot_id = ? AND mint = ? AND outcome_at IS NULL",
            (now, bot_id, mint))
        db.execute(
            "INSERT INTO bot_signal_log (bot_id, mint, entered_at, "
            "gain_pct, gain_window, volume_ratio, buyer_ratio, "
            "trend_1h_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, mint, now, signals.get("gain_pct"),
             signals.get("gain_window") or "",
             signals.get("volume_ratio"), signals.get("buyer_ratio"),
             signals.get("trend_1h_pct")))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _record_signal_outcome(db, bot_id: int, mint: str) -> None:
    """Fill in the outcome of the open signal row for (bot, mint)
    when its position fully closes: the total realized P&L of that
    position's sells since entry, from the trade ledger (the truth).
    A position whose sells carry no USD P&L closes with an unknown
    outcome (NULL) — never counted a win or a loss on a guess.
    Never raises."""
    try:
        row = db.execute(
            "SELECT id, entered_at FROM bot_signal_log "
            "WHERE bot_id = ? AND mint = ? AND outcome_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (bot_id, mint)).fetchone()
        if not row:
            return
        total = db.execute(
            "SELECT COALESCE(SUM(pnl_usd), NULL) FROM trade_ledger "
            "WHERE bot_id = ? AND coin = ? AND side = 'sell' "
            "AND pnl_usd IS NOT NULL AND created_at >= ?",
            (bot_id, mint, row["entered_at"])).fetchone()
        pnl = float(total[0]) if total and total[0] is not None else None
        win = None
        if pnl is not None:
            win = 1 if pnl > 0 else (0 if pnl < 0 else None)
        db.execute(
            "UPDATE bot_signal_log SET outcome_pnl_usd = ?, "
            "outcome_win = ?, outcome_at = ? WHERE id = ?",
            (pnl, win,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             row["id"]))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _hard_exit_reason(stop_loss_pct: float, trailing_pct: float,
                      kind: str, avg_price: float, peak_price: float,
                      price: float) -> str | None:
    """The hard exits for an open position at `price`: stop loss,
    breakeven stop, trailing stop. Pure — no DB, so the loop and the
    tick-driven fast-exit thread share exactly this logic.

    Breakeven stop (scanner + signals kinds, tuning build
    2026-11-15): once the peak reaches +BREAKEVEN_TRIGGER_PCT%
    over entry, the stop moves to the entry price — the remainder
    cannot lose money. The trailing stop above it still ratchets
    the floor up while the coin climbs. Returns the reason string
    ("stop_loss", "breakeven stop", "trailing stop") or None."""
    if avg_price <= 0 or price <= 0:
        return None
    peak = max(float(peak_price), price)
    # _lte, not a bare >=: 0.01 * 1.10 is 0.011000000000000001 in
    # binary float, so a peak stored as exactly 0.011 would miss a
    # strict comparison and the breakeven stop would never arm.
    breakeven = (kind in ("scanner", "signals")
                 and _lte(avg_price * (1.0 + BREAKEVEN_TRIGGER_PCT
                                       / 100.0), peak))
    stop_price = avg_price if breakeven else avg_price * (
        1.0 - float(stop_loss_pct) / 100.0)
    if _lte(price, stop_price):
        return "breakeven stop" if breakeven else "stop_loss"
    trail_price = peak * (1.0 - float(trailing_pct) / 100.0)
    if _lte(price, trail_price):
        return "trailing stop"
    return None


# --- Gnat preset (2026-11-21): James's spec, honest-mapped ----------
# Every threshold below is a REMY DEFAULT from James's suggested
# values, awaiting his call — labelled so in the MANIFEST and in
# rules.GNAT_NOTES. The engine's fastest data is the 5-second
# DexScreener poll and the 30-second loop tick; anything the spec
# asks for in a 5/10/15/30-second window, a unique-wallet count, or
# a creator/dev-history signal is mapped to its closest version or
# recorded as not enforced (see the MANIFEST), never faked.

def _gnat_hard_exit_reason(avg_price: float, peak_price: float,
                           price: float) -> str | None:
    """Gnat's hard exits at `price`: the -18% hard stop, the
    breakeven +2% stop armed at the Tier 1 peak (+40%), and the
    tiered trailing stop on the moonbag (18% while the peak is
    +100% to +250%, 12% above +250%). Pure — no DB, so the loop
    and the tick-driven fast-exit thread share exactly this logic.

    The floors stack upward only: the hard stop is the lowest
    floor and the only exit before Tier 1; the breakeven floor
    replaces it once armed; the trailing floor replaces the
    breakeven floor once armed (it always sits above it). The
    trailing tier is set by the PEAK profit — once the coin
    peaked above +250% the stop stays at 12%, it never loosens
    back to 18%. Returns the reason string or None."""
    if avg_price <= 0 or price <= 0:
        return None
    peak = max(float(peak_price), price)
    if _lte(price, avg_price * (1.0 - GNAT_STOP_LOSS_PCT / 100.0)):
        return "stop_loss"
    floor = None
    reason = None
    if _gte(peak, avg_price * (1.0 + GNAT_TIER1_PROFIT_PCT / 100.0)):
        floor = avg_price * (1.0 + GNAT_BREAKEVEN_PLUS_PCT / 100.0)
        reason = "breakeven stop"
    if _gte(peak, avg_price * (1.0 + GNAT_TIER2_PROFIT_PCT / 100.0)):
        peak_profit = (peak - avg_price) / avg_price * 100.0
        trail_pct = (GNAT_TRAIL_HIGH_PCT
                     if peak_profit > GNAT_TRAIL_TIGHTEN_AT_PCT
                     else GNAT_TRAIL_LOW_PCT)
        trail_floor = peak * (1.0 - trail_pct / 100.0)
        if floor is None or trail_floor > floor:
            floor = trail_floor
            reason = "trailing stop"
    if floor is not None and _lte(price, floor):
        return reason
    return None


def _gnat_time_decay_reason(opened_ts: float | None, peak_price: float,
                            avg_price: float,
                            now_ts: float) -> str | None:
    """Gnat time decay: not at least +15% within 90 seconds of
    entry → exit 100%. The PEAK price is the test — a coin that
    touched +15% and fell back is handled by the stops, not by
    this exit. A position with no recorded open time never fires
    (missing data exits nothing). Pure — no DB."""
    if opened_ts is None or avg_price <= 0:
        return None
    if now_ts - opened_ts <= GNAT_TIME_DECAY_SECONDS:
        return None
    peak_gain = (float(peak_price) - avg_price) / avg_price * 100.0
    if peak_gain < GNAT_TIME_DECAY_MIN_GAIN_PCT:
        return (f"time decay: not +{GNAT_TIME_DECAY_MIN_GAIN_PCT:.0f}% "
                f"within {GNAT_TIME_DECAY_SECONDS}s of entry")
    return None


def _gnat_momentum_exhausted(entry: dict, peak_price: float,
                             avg_price: float) -> bool:
    """Gnat momentum exhaustion, closest version: the peak reached
    at least +25% and the 5-minute change is now at or below -5%
    — the flow that carried the coin has flipped, so the rest of
    the position exits in full. The spec's 10-second net-SOL-flow
    window does not exist in the app's data (no sub-5-minute flow,
    no transaction stream); the 5-minute change is the fastest
    flow proxy the feed provides. Missing data never fires. Pure
    — no DB."""
    if avg_price <= 0:
        return False
    peak_gain = (float(peak_price) - avg_price) / avg_price * 100.0
    if peak_gain < GNAT_EXHAUST_MIN_PEAK_PCT:
        return False
    change_5m = (entry or {}).get("change_5m_pct")
    if change_5m is None:
        return False
    try:
        return float(change_5m) <= -GNAT_EXHAUST_DROP_5M_PCT
    except (TypeError, ValueError):
        return False


def _gnat_buy_halt(db, bot, now_ts: float, feed,
                   prices_data: dict | None) -> tuple[bool, str]:
    """Gnat circuit breakers on NEW BUYS for a gnat bot. Exits
    still run — a halted bot can still stop out, it just cannot
    open anything new. Returns (halted, reason).

    1. Daily-loss breaker (James's spec: 2.0 SOL): today's realized
       P&L at or beyond -2.0 SOL, converted at the live SOL price,
       halts buys for the rest of the day. With no live SOL price
       the SOL threshold cannot be evaluated and this breaker does
       NOT trip — the loss-streak breaker below still applies.
    2. Loss-streak breaker (James's spec: 4 consecutive losing
       closed trades): halts buys for the rest of the day.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM trade_ledger "
            "WHERE bot_id = ? AND side = 'sell' AND pnl_usd IS NOT NULL "
            "AND substr(created_at, 1, 10) = ?",
            (bot["id"], today)).fetchone()
        daily_realized = float(row[0]) if row else 0.0
    except Exception:
        daily_realized = 0.0
    sol_price = _live_sol_price_usd(feed, prices_data)
    if sol_price is not None and sol_price > 0:
        limit_usd = GNAT_MAX_DAILY_LOSS_SOL * sol_price
        if daily_realized <= -limit_usd:
            return True, (
                f"gnat daily-loss circuit breaker: realized "
                f"${daily_realized:.2f} today is at or beyond "
                f"-{GNAT_MAX_DAILY_LOSS_SOL:.1f} SOL "
                f"(${limit_usd:.2f}) — no new buys until tomorrow")
    try:
        rows = db.execute(
            "SELECT win FROM trade_ledger WHERE bot_id = ? "
            "AND side = 'sell' ORDER BY id DESC LIMIT ?",
            (bot["id"], GNAT_MAX_CONSEC_LOSSES)).fetchall()
    except Exception:
        rows = []
    if len(rows) >= GNAT_MAX_CONSEC_LOSSES and all(
            r["win"] == 0 for r in rows):
        return True, (
            f"gnat loss-streak circuit breaker: "
            f"{GNAT_MAX_CONSEC_LOSSES} consecutive losing closed "
            f"trades — no new buys until tomorrow")
    return False, ""


def _gnat_entry_gates(db, cfg, entry: dict, rpc_factory,
                      rpc_cache: dict) -> tuple[bool, str]:
    """Gnat entry gates, after the gain + buy-filter + momentum
    checks. Returns (True, "") when the coin passes, else
    (False, reason). Fail-closed at every step.

    1. Buy pressure (closest version): at least 15 buy
       transactions in the 5-minute bucket and a buy-to-sell
       transaction ratio of at least 2.5:1. The spec's 30-second
       window and unique-buyer count do not exist in the app's
       data — the 5-minute DexScreener txn bucket is the closest
       window, and txn counts stand in for unique wallets. Zero
       sells with buys present is an infinite ratio, so it
       passes; missing counts fail.
    2. Socials + metadata URI: when REQUIRE_SOCIALS (default
       TRUE), at least one published social/website link in the
       pair info; always, a non-empty metadata URI on the
       on-chain Metaplex metadata account. Presence only — the
       app cannot verify a link is live.
    3. Curve band: bonding-curve progress 12-65%, read from the
       on-chain curve account. A curve the RPC cannot read is
       never bought.
    Devnet watchlist mints (the operator's own test coins) skip
    all three gates: they have no DexScreener pair data and their
    curves sit below 12%, so the gates would block every devnet
    test buy. The gain, momentum and scam gates still apply to
    them, so exits stay testable on devnet.
    """
    mint = entry.get("mint")
    if not mint:
        return False, "no mint to check"
    if entry.get("devnet_watchlist"):
        return True, ""
    buys = entry.get("txns_m5_buys")
    sells = entry.get("txns_m5_sells")
    try:
        buys = int(buys) if buys is not None else None
        sells = int(sells) if sells is not None else None
    except (TypeError, ValueError):
        return False, "gnat buy pressure: txn counts unreadable"
    if buys is None or sells is None:
        return False, "gnat buy pressure: no 5-minute txn data"
    if buys < GNAT_MIN_BUYERS_5M:
        return False, (f"gnat buy pressure: {buys} buys in 5 minutes, "
                       f"need at least {GNAT_MIN_BUYERS_5M}")
    if sells > 0 and buys / sells < GNAT_MIN_BUY_SELL_RATIO:
        return False, (f"gnat buy pressure: buy/sell ratio "
                       f"{buys / sells:.1f}:1, need at least "
                       f"{GNAT_MIN_BUY_SELL_RATIO}:1")
    if GNAT_REQUIRE_SOCIALS:
        try:
            social_links = int(entry.get("social_links") or 0)
        except (TypeError, ValueError):
            social_links = 0
        if social_links < 1:
            return False, ("gnat socials: no published social or "
                           "website link")
    try:
        client = _top10_client_for_mint(cfg, mint, rpc_factory, rpc_cache)
    except Exception:
        return False, "gnat metadata: RPC unavailable"
    try:
        account = client.get_account_info(metadata_pda(mint))
    except Exception:
        return False, "gnat metadata: could not read metadata account"
    meta = parse_metadata_account(
        account.get("data") if account else None)
    if not meta or not meta.get("uri"):
        return False, "gnat metadata: no valid metadata URI"
    try:
        curve_addr, _ = bonding_curve_pda(mint)
        curve_account = client.get_account_info(curve_addr)
    except Exception:
        return False, "gnat curve: could not read bonding curve"
    try:
        curve = parse_bonding_curve(
            curve_account.get("data") if curve_account else None)
    except Exception:
        return False, "gnat curve: bonding curve unreadable"
    progress = curve_progress_pct(curve)
    if progress is None:
        return False, "gnat curve: bonding curve unreadable"
    if progress < GNAT_CURVE_MIN_PCT or progress > GNAT_CURVE_MAX_PCT:
        return False, (f"gnat curve: {progress:.1f}% filled, outside "
                       f"the {GNAT_CURVE_MIN_PCT:.0f}-"
                       f"{GNAT_CURVE_MAX_PCT:.0f}% band")
    return True, ""


def default_real_executor(bot, user_id: int, coin: str, side: str,
                         amount: float, price: float, db, cfg) -> dict:
    """Real executor: the account's dedicated signing bot executes
    pump.fun trades on devnet (see executor.py) — and Jupiter
    swaps on mainnet for graduated coins (see jupiter_executor.py,
    gated behind James's go-ahead). The venue is the bonding
    curve: complete (graduated) -> Jupiter, still bonding ->
    pump.fun, unreadable -> refused plainly, never guessed.
    Anything unverified is skipped honestly — never a fake fill."""
    if not is_valid_address(coin):
        return _skip("Not a valid token address")
    try:
        rpc = SolanaRpcClient(cfg.rpc_url)
        complete = is_curve_complete(rpc, coin)
    except Exception:
        complete = None
    if complete is None:
        return _skip("bonding curve unavailable")
    if complete:
        return execute_jupiter_swap(bot, user_id, coin, side, amount,
                                    db, cfg)
    return execute_real_trade(bot, user_id, coin, side, amount, price,
                              db, cfg)


def _mint_price_map(coins) -> dict:
    """{mint: {"price", "change_24h"}} for pump.fun mints, from the
    trenches feed (DexScreener, Raydium fallback for graduated
    coins). Signals bots watch named coins — in this app those are
    mint addresses, and the price list (SOL only) has no entry for
    them. A mint with no price is left out — unpriced means no
    trade, never a guess. A fetch failure returns {} (the caller
    then skips, never trades blind)."""
    mints = [c for c in (coins or []) if is_valid_address(c)]
    if not mints:
        return {}
    try:
        prices = fetch_prices_for_mints(mints)
    except Exception:
        prices = {}
    try:
        changes = fetch_mint_24h_changes(mints)
    except Exception:
        changes = {}
    out = {}
    for mint in mints:
        if mint in prices or mint in changes:
            out[mint] = {"price": prices.get(mint),
                         "change_24h": changes.get(mint)}
    return out


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


def _after_sell(db, bot, coin: str, sold_amount: float,
                full_amount: float, pnl_usd: float | None) -> None:
    """Post-sell bookkeeping (tuning build 2026-11-15): a losing
    close sets the 30-minute re-buy cooldown for (bot, coin); a
    full close fills in the signal-log outcome for the trade.
    Never raises."""
    try:
        if pnl_usd is not None and pnl_usd < 0:
            _set_cooldown(db, bot["id"], coin, "losing close",
                          datetime.now(timezone.utc).timestamp())
        if sold_amount >= full_amount:
            _record_signal_outcome(db, bot["id"], coin)
    except Exception:
        pass


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
    # Idempotency (tuning build 2026-11-15): the tick-driven
    # fast-exit thread and the loop backstop can both decide to sell
    # this position at the same moment. The first claim wins; the
    # second sell is refused, never executed twice.
    if not _claim_position_for_sell(int(pos["id"])):
        actions.append({"action": "skip", "coin": coin,
                        "reason": "sell already in flight"})
        return
    try:
        _sell_position_claimed(db, cfg, bot, coin, price, reason,
                               rpc_factory, real_executor, actions,
                               amount=amount, ladder_stage=ladder_stage,
                               feed=feed, prices_data=prices_data,
                               full_amount=full_amount)
    finally:
        _release_position_for_sell(int(pos["id"]))


def _sell_position_claimed(db: sqlite3.Connection, cfg: Config, bot,
                           coin: str, price: float, reason: str,
                           rpc_factory, real_executor, actions: list,
                           amount: float, ladder_stage: int | None = None,
                           feed=None, prices_data: dict | None = None,
                           full_amount: float | None = None) -> None:
    """The sell itself, run only while the position is claimed (see
    _sell_position). After a sell, two tuning-build bookkeeping
    steps run: a losing close sets the 30-minute re-buy cooldown
    for (bot, coin), and a FULL close fills in the outcome of the
    open signal-log row for the trade."""
    pos = _positions(db, bot["id"]).get(coin)
    if not pos:
        return
    if full_amount is None:
        full_amount = pos["amount"]
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
            _after_sell(db, bot, coin, sell_amount, full_amount,
                        usd - sell_amount * float(pos["avg_price"]))
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
                     exit_reason=reason, mode="real", now=_now(),
                     signature=result.get("signature"),
                     sol_amount=sol_received)
        if ladder_stage is not None:
            db.execute("UPDATE bot_positions SET ladder_stage = ? "
                       "WHERE bot_id = ? AND coin_id = ?",
                       (ladder_stage, bot["id"], coin))
            db.commit()
        _after_sell(db, bot, coin, amount, full_amount, pnl_usd)
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
              feed=None, prices_data: dict | None = None,
              kind: str | None = None) -> None:
    """Buy `coin` for a bot within its limits, or skip with a reason.

    `signals` is the buy-time signal snapshot from the feed entry
    (m5 gain %, 5-minute volume, buys/sells, regime) — recorded on
    the paper trade so the losing-buy analysis can show how the coin
    got bought. None for v1 bots and manual buys. `price` is the live
    mark price; a real position's cost basis is the EXECUTED spend
    when the executor reports it (see below), never the mark.
    `kind` is the bot's rule kind: only gnat sizes in fixed SOL —
    every other kind buys a % of the bankroll.
    """
    if bot["mode"] == "paper":
        bankroll = paper_bankroll(db, bot["user_id"], prices_usd)
        size_note = ""
        if kind == "gnat":
            # Fixed 0.25 SOL per trade (James's spec), converted
            # at the live SOL price. No live SOL price → fall back
            # to the bot row's buy-size % of the bankroll, and say
            # so on the buy line — the fallback is never silent.
            sol_price = _live_sol_price_usd(feed, prices_data)
            if sol_price is not None and sol_price > 0:
                buy_usd = GNAT_BUY_SIZE_SOL * sol_price
                size_note = " (0.25 SOL size)"
            else:
                buy_usd = bankroll * float(bot["buy_size_pct"]) / 100.0
                size_note = (f" (fallback "
                             f"{float(bot['buy_size_pct']):.0f}% size — "
                             f"no live SOL price)")
        else:
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
        # Cost basis is the EXECUTED fill price (mark + the 1% paper
        # haircut), never the mark — usd / amount is exactly that
        # fill price. Sell P&L is measured against what was paid.
        _open_position(db, bot, coin, amount, price,
                       avg_price=(usd / amount if amount > 0 else price))
        if signals and signals.get("gain_pct") is not None:
            _record_signal_entry(db, bot["id"], coin, signals, _now())
        # First time this coin is held: fetch + store its official
        # logo (on-chain Metaplex metadata, then DexScreener).
        # Best-effort — never blocks the trade, and only held coins
        # are ever fetched.
        try:
            ensure_coin_meta(db, coin, rpc_url=cfg.rpc_url)
        except Exception:
            pass
        _log(db, bot["user_id"], "silent_buy",
             f"Silent bot '{bot['name']}' bought {amount:.4f} {coin} "
             f"for ${usd:.2f} (paper){size_note}.",
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
            # First time this coin is held: fetch + store its
            # official logo (see the paper-buy hook above).
            try:
                ensure_coin_meta(db, coin, rpc_url=cfg.rpc_url)
            except Exception:
                pass
            # Trade ledger (builderBot): every fill gets its own
            # 8-digit trade ID. The recorded price is the executed
            # cost when known, else the mark price.
            buy_price = exec_avg if exec_avg is not None else price
            record_trade(db, bot["user_id"], bot["id"], coin, "buy",
                         coin_amount, buy_price,
                         coin_amount * buy_price,
                         mode="real", now=_now(),
                         signature=result.get("signature"),
                         sol_amount=sol_spent)
        if signals and signals.get("gain_pct") is not None:
            _record_signal_entry(db, bot["id"], coin, signals, _now())
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
      * feed 5-minute change (trenches/scanner only): the feed's
        change_5m_pct at or below -drop_5m_pct.
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
    if kind in ("trenches", "scanner", "gnat") and price_map:
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
    if kind in ("trenches", "scanner", "gnat"):
        if not trenches_data or trenches_data.get("error"):
            why = (trenches_data or {}).get("error") or "feed unavailable"
            feed_name = ("scanner" if kind == "scanner"
                         else ("gnat" if kind == "gnat" else "trenches"))
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': {feed_name} feed "
                 f"unavailable — {why[:80]}. No trades this scan.",
                 bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": f"{feed_name} feed unavailable"})
            return actions
        price_map = trenches_data["prices"]
    else:
        # Signals bots watch named coins — pump.fun mints, priced
        # from the trenches feed (see _mint_price_map).
        price_map = _mint_price_map(json.loads(bot["coins"]))

    def _price_for(coin: str):
        if kind in ("trenches", "scanner", "gnat"):
            entry = price_map.get(coin)
            return entry["price_usd"] if entry else None
        entry = (price_map or {}).get(coin)
        return entry["price"] if entry else None

    def _change_for(coin: str):
        if kind == "trenches":
            entry = price_map.get(coin)
            if not entry:
                return None
            return entry.get(
                _BUY_METRIC_TO_CHANGE_KEY[rules["buy"]["metric"]])
        entry = (price_map or {}).get(coin)
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
    # Top-10 wallets watch state: one RPC client per NETWORK per
    # evaluation, created lazily (mainnet mints read through the
    # mainnet RPC, devnet watchlist mints through the devnet RPC —
    # tuning build 2026-11-15). A failed client or a failed lookup
    # is logged at most once per hour per (bot, mint) and the watch
    # is skipped for that scan — never faked, and the other exits
    # still apply.
    rpc_cache: dict = {}

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
        if kind in ("scanner", "trenches", "gnat") and top10:
            try:
                top10_client = _top10_client_for_mint(
                    cfg, coin, rpc_factory, rpc_cache)
            except Exception:
                top10_client = None
                _log_top10_skip_throttled(
                    db, bot["user_id"], bot, coin,
                    f"Silent bot '{bot['name']}': top-10 wallet watch "
                    f"unavailable — no top-10 exit this scan.",
                    bot["mode"], now_ts)
            if top10_client is not None:
                total = top10_total(top10_client, coin)
                if total is None:
                    _log_top10_skip_throttled(
                        db, bot["user_id"], bot, coin,
                        f"Silent bot '{bot['name']}': top-10 wallet "
                        f"watch unavailable for {coin} — position left "
                        f"alone this scan.", bot["mode"], now_ts)
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
        # Hard exits (stop / breakeven stop / trailing stop) — one
        # shared helper, so the loop and the tick-driven fast-exit
        # thread fire on exactly the same conditions. The stop fires
        # on the first pass that sees the price at or below it; with
        # the 30-second loop tick (and the 5-second tick-driven fast
        # exits) there is no 60-second lag any more — the report's
        # "4% stop exiting at -12.6%" came from the old 60s tick.
        # Gnat runs its own hard exits: the -18% stop, the breakeven
        # +2% armed at +40%, and the tiered 18%/12% trailing stop
        # on the moonbag — never the shared helper's 10% breakeven
        # or flat trailing.
        if kind == "gnat":
            hard = _gnat_hard_exit_reason(avg_price, peak, price)
        else:
            hard = _hard_exit_reason(stop_loss_pct, trailing_pct, kind,
                                     avg_price, peak, price)
        if hard:
            _sell_position(db, cfg, bot, coin, price, hard,
                           rpc_factory, real_executor, actions,
                           feed=feed, prices_data=prices_data)
            continue
        stage = int(pos["ladder_stage"] or 0)
        ladder_fired = False
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
                ladder_fired = True
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
                ladder_fired = True
                _sell_position(
                    db, cfg, bot, coin, price,
                    f"sell ladder: {rung['pct_of_position']:.0f}% at "
                    f"+{rung['profit_pct']:.0f}% profit",
                    rpc_factory, real_executor, actions, amount=amount,
                    ladder_stage=stage + 1,
                    feed=feed, prices_data=prices_data)
        # Gnat time decay + momentum exhaustion (gnat only): the
        # time decay fires on age + peak, the exhaustion on peak +
        # the 5-minute change — both exit the rest of the position
        # in full. They run after the ladder so a rung that fired
        # this pass still pays out first.
        if kind == "gnat":
            opened_at = (pos["opened_at"]
                         if "opened_at" in pos.keys() else None)
            opened_ts = (_parse_scan_time(opened_at)
                         if opened_at else None)
            decay = _gnat_time_decay_reason(opened_ts, peak,
                                            avg_price, now_ts)
            if decay:
                _sell_position(db, cfg, bot, coin, price, decay,
                               rpc_factory, real_executor, actions,
                               feed=feed, prices_data=prices_data)
                continue
            entry = (price_map or {}).get(coin) or {}
            if _gnat_momentum_exhausted(entry, peak, avg_price):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "momentum exhaustion: up 25%+ then 5-minute "
                    "change -5% or worse",
                    rpc_factory, real_executor, actions,
                    feed=feed, prices_data=prices_data)
                continue
        # Signal-death exit (scanner only): the position is held
        # only while the signal keeps coming. When the 5-minute
        # gain, the volume edge and the buyer edge are ALL gone,
        # the thesis is dead — exit at market rather than waiting
        # for the stop. A signal that cannot be evaluated (missing
        # data) never triggers this exit.
        if kind == "scanner":
            entry = (price_map or {}).get(coin) or {}
            if signal_dead(entry, db, bot["id"], coin, price, now_ts):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "signal died: gain, volume and buyer edge all gone",
                    rpc_factory, real_executor, actions,
                    feed=feed, prices_data=prices_data)
                continue
        # Max hold (scanner + signals): no ladder rung within 30
        # minutes of opening means the coin is not doing what it
        # was bought for — exit at market instead of holding a flat
        # coin and hoping. A rung fired this pass resets the clock
        # (the position is working).
        if kind in ("scanner", "signals") and not ladder_fired:
            opened_at = (pos["opened_at"]
                         if "opened_at" in pos.keys() else None)
            opened_ts = _parse_scan_time(opened_at) if opened_at else None
            if (opened_ts is not None
                    and now_ts - opened_ts > MAX_HOLD_SECONDS
                    and stage == 0):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "max hold time: no ladder rung in 30 minutes",
                    rpc_factory, real_executor, actions,
                    feed=feed, prices_data=prices_data)
                continue

    # Loss-prevention halts (scanner + signals — Trenches v2 owns
    # trenches exits): a tripped drawdown breaker or loss-streak
    # halt stops NEW BUYS for this pass. The position exits above
    # already ran — a halted bot can still stop out, it just cannot
    # open anything new. The halt is logged once per day, not once
    # per scan.
    if kind in ("scanner", "signals"):
        halted, halt_reason = _buy_halt(db, bot, now_ts, feed,
                                        prices_data)
        if halted:
            marker = ("drawdown circuit breaker"
                      if "drawdown" in halt_reason else "loss-streak halt")
            if not _breaker_logged(db, bot["id"], marker):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': {halt_reason}.",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": halt_reason})
            return actions
    # Gnat circuit breakers (gnat only): the daily-loss and
    # loss-streak breakers from James's spec — same shape as the
    # halts above, Gnat's own thresholds, logged once per day.
    if kind == "gnat":
        halted, halt_reason = _gnat_buy_halt(db, bot, now_ts, feed,
                                             prices_data)
        if halted:
            if not _breaker_logged(db, bot["id"],
                                   "gnat circuit breaker"):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': {halt_reason}.",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": halt_reason})
            return actions

    # 2. Buys: strongest qualifying gainer, at most max_new per scan.
    bought = 0
    # Candidates blocked by the pre-buy scam gate — logged once per
    # scan (strongest blocked coin) when nothing was bought, like
    # the buy-filter log above.
    blocked_gates: list = []
    max_open_hit = False
    max_new = int(rules["max_new_positions_per_scan"])
    ranked = []
    # Candidates that passed the buy threshold but failed the
    # low-performer buy filter — kept so the scan logs one line about
    # the strongest rejected coin instead of one line per candidate.
    filtered = []
    if kind in ("scanner", "gnat"):
        # The scanner ranks by the 3-minute gain from price marks the
        # loop records itself (scanner.py) — the feed only provides
        # 5-minute changes. A coin with no mark at least 3 minutes
        # old has no 3-minute gain yet and is not bought. Gnat
        # shares this branch: it buys on the same 3-minute gain,
        # then adds its own entry gates below.
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
            # Momentum-only entry: the four-part signal gate. A
            # coin gaining on one short window but flat/falling on
            # the others, or spiking without volume, is not a
            # signal — it is never bought.
            ok, why = passes_momentum_gate(entry, db, bot["id"],
                                           now_ts)
            if not ok:
                filtered.append((gain, entry, f"momentum gate: {why}"))
                continue
            # Gnat entry gates (gnat only): buy pressure, socials +
            # metadata URI, curve band. Fail-closed — a coin that
            # cannot be verified is never bought.
            if kind == "gnat":
                ok, why = _gnat_entry_gates(
                    db, cfg, entry, rpc_factory, rpc_cache)
                if not ok:
                    filtered.append((gain, entry, why))
                    continue
            # 2-scan confirmation: the signal must still be passing
            # on a later scan. A one-scan spike never buys.
            if not _signal_confirmed(db, bot["id"], entry["mint"],
                                     gain, now_ts, interval):
                filtered.append(
                    (gain, entry,
                     "signal not yet confirmed — first seen this scan"))
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
    if kind in ("trenches", "scanner", "gnat"):
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
                max_open_hit = True
                break
            if kind in ("scanner", "gnat"):
                # Re-buy cooldown: a coin closed at a loss is not
                # bought again for 30 minutes.
                blocked_until = _cooldown_until(
                    db, bot["id"], entry["mint"], now_ts)
                if blocked_until is not None:
                    until = datetime.fromtimestamp(
                        blocked_until, tz=timezone.utc).strftime(
                            "%H:%M UTC")
                    actions.append(
                        {"action": "skip", "coin": entry["mint"],
                         "reason": f"re-buy cooldown until {until}"})
                    continue
                # Pre-buy scam gates: top-10 concentration, mint /
                # freeze authority, metadata. A blocked coin is
                # skipped for the next ranked candidate — the gate
                # never buys a coin it cannot verify.
                ok, why = _prebuy_scam_gate(
                    db, cfg, bot, entry, rules, rpc_factory, rpc_cache)
                if not ok:
                    blocked_gates.append((_change, entry, why))
                    continue
            before = len(actions)
            signals = {
                "m5_pct": entry.get("change_5m_pct"),
                "volume_5m_usd": entry.get("volume_m5_usd"),
                "buys_5m": entry.get("txns_m5_buys"),
                "sells_5m": entry.get("txns_m5_sells"),
                "regime": detect_regime(entry, rules.get("regime")),
            }
            if kind in ("scanner", "gnat"):
                # The signal features for the signal-outcome log:
                # the triggering 3-minute gain, the 5-minute volume
                # ratio vs the hourly pace, the buyer ratio, and
                # the 1-hour trend.
                vol_m5 = entry.get("volume_m5_usd")
                vol_h1 = entry.get("volume_h1_usd")
                volume_ratio = None
                try:
                    if (vol_m5 is not None and vol_h1 is not None
                            and float(vol_h1) > 0):
                        volume_ratio = (float(vol_m5)
                                        / (float(vol_h1) / 12.0))
                except (TypeError, ValueError):
                    volume_ratio = None
                buyer_ratio = None
                try:
                    sells = entry.get("txns_m5_sells")
                    if sells is not None and int(sells) > 0:
                        buyer_ratio = (int(entry.get("txns_m5_buys") or 0)
                                       / int(sells))
                except (TypeError, ValueError):
                    buyer_ratio = None
                signals["gain_pct"] = float(_change)
                signals["gain_window"] = "3m"
                signals["volume_ratio"] = volume_ratio
                signals["buyer_ratio"] = buyer_ratio
                signals["trend_1h_pct"] = entry.get("change_1h_pct")
            _buy_coin(db, cfg, bot, entry["mint"], entry["price_usd"],
                      _bankroll_prices(),
                      rpc_factory, real_executor, actions,
                      signals=signals, feed=feed, kind=kind,
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
                max_open_hit = True
                break
            # 2-scan confirmation (signals kind): the 24h signal
            # must still be passing on a later scan.
            if not _signal_confirmed(db, bot["id"], coin, float(change),
                                     now_ts, interval):
                actions.append({"action": "skip", "coin": coin,
                                "reason": "signal not yet confirmed — "
                                          "first seen this scan"})
                continue
            blocked_until = _cooldown_until(db, bot["id"], coin, now_ts)
            if blocked_until is not None:
                until = datetime.fromtimestamp(
                    blocked_until, tz=timezone.utc).strftime("%H:%M UTC")
                actions.append({"action": "skip", "coin": coin,
                                "reason": f"re-buy cooldown until {until}"})
                continue
            entry = {"mint": coin, "price_usd": _price_for(coin),
                     "devnet_watchlist": coin in active_mints(cfg)}
            ok, why = _prebuy_scam_gate(
                db, cfg, bot, entry, rules, rpc_factory, rpc_cache)
            if not ok:
                blocked_gates.append((float(change), entry, why))
                continue
            before = len(actions)
            _buy_coin(db, cfg, bot, coin, _price_for(coin),
                      _bankroll_prices(),
                      rpc_factory, real_executor, actions,
                      signals={"gain_pct": float(change),
                               "gain_window": "24h"},
                      feed=feed, prices_data=prices_data)
            if len(actions) > before and actions[-1]["action"] == "buy":
                bought += 1
    if bought == 0 and blocked_gates and not max_open_hit:
        # Nothing bought and at least one candidate was blocked by
        # the scam gate — say so once, naming the strongest blocked
        # coin, like the buy-filter log above.
        blocked_gates.sort(key=lambda item: item[0], reverse=True)
        top = blocked_gates[0][1]
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': strongest candidate "
             f"{top.get('symbol') or top['mint']} blocked by the scam "
             f"gate — {blocked_gates[0][2]}. No buy this scan.",
             bot["mode"])
        actions.append({"action": "skip", "coin": top["mint"],
                        "reason": f"scam gate: {blocked_gates[0][2]}"})
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
    coins = json.loads(bot["coins"])
    # The price list is SOL only; a signals bot's coins are pump.fun
    # mints, priced from the trenches feed (see _mint_price_map).
    prices = dict(prices_data["prices"])
    prices.update(_mint_price_map(coins))
    prices_usd = {c: e.get("price", 0.0) for c, e in prices.items()}
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

    For every silent scanner, trenches or gnat bot with a top10_rug
    rule and an open position, one RPC round fetches the top-10
    total AND the total supply, and BOTH top-10 exits run on that
    data:
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
        if rules["kind"] not in ("scanner", "trenches", "gnat"):
            continue
        top10 = rules.get("top10_rug")
        if not top10:
            continue
        interval = float(top10.get("check_interval_seconds", 15.0))
        positions = _positions(db, bot["id"])
        if not positions:
            continue
        # One RPC client per network for this pass (tuning build
        # 2026-11-15): mainnet mints read through the mainnet RPC,
        # devnet watchlist mints through the devnet RPC. The old
        # code read every mint through cfg.rpc_url (devnet), which
        # cannot serve mainnet mints at all.
        rpc_cache: dict = {}
        for coin, pos in list(positions.items()):
            row = db.execute(
                "SELECT last_checked_at FROM scanner_top10 "
                "WHERE bot_id = ? AND mint = ?",
                (bot["id"], coin)).fetchone()
            if row and row["last_checked_at"]:
                last_ts = _parse_scan_time(row["last_checked_at"])
                if last_ts is not None and now_ts - last_ts < interval:
                    continue
            try:
                client = _top10_client_for_mint(
                    cfg, coin, rpc_factory, rpc_cache)
            except Exception:
                _log_top10_skip_throttled(
                    db, bot["user_id"], bot, coin,
                    f"Silent bot '{bot['name']}': top-10 watch "
                    f"unavailable — no top-10 check this round.",
                    bot["mode"], now_ts)
                continue
            total = top10_total(client, coin)
            if total is None:
                _log_top10_skip_throttled(
                    db, bot["user_id"], bot, coin,
                    f"Silent bot '{bot['name']}': top-10 watch "
                    f"unavailable for {coin} — position left alone "
                    f"this round.", bot["mode"], now_ts)
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


def _on_live_tick(coin_id: str, price: float, now: float) -> None:
    """Live-feed tick listener (tuning build 2026-11-15): mark the
    mint dirty and wake the fast-exit thread. Fast and never
    raising — it runs on the feed's poller thread, so it never
    sells inline and never blocks a poll."""
    with _TICK_DIRTY_LOCK:
        _TICK_DIRTY_MINTS.add(coin_id)
    _TICK_EVENT.set()


def _fast_exit_once(db, cfg, feed, rpc_factory=None,
                    real_executor=None) -> list:
    """One fast-exit pass over the mints dirtied by live-feed ticks
    since the last pass: for every open position in a dirtied mint
    (scanner, signals and gnat kinds — Trenches v2 owns trenches
    exits), check the hard exits (rug sell, stop / breakeven stop,
    trailing stop) at the tick price and sell at once when one
    fires. The loop is the backstop: if this thread misses a tick,
    the next loop pass (30s) still fires the same exit through the
    same shared helper. Returns the actions taken."""
    actions: list = []
    with _TICK_DIRTY_LOCK:
        mints = sorted(_TICK_DIRTY_MINTS)
        _TICK_DIRTY_MINTS.clear()
    if not mints:
        return actions
    now_ts = datetime.now(timezone.utc).timestamp()
    rpc_factory = rpc_factory or SolanaRpcClient
    real_executor = real_executor or default_real_executor
    for mint in mints:
        try:
            tick = feed.snapshot([mint]).get(mint) if feed else None
        except Exception:
            tick = None
        if not tick or tick.get("stale"):
            continue
        try:
            price = float(tick["price"])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        bots = db.execute(
            "SELECT * FROM bots WHERE silent = 1").fetchall()
        for bot in bots:
            rules, error = load_rules(bot)
            if rules is None or error is not None:
                continue
            if rules["kind"] not in ("scanner", "signals", "gnat"):
                continue
            pos = _positions(db, bot["id"]).get(mint)
            if not pos:
                continue
            rug = rules.get("rug_sell")
            if rug:
                reason = _rug_reason(rug, rules["kind"], mint, price,
                                     pos, None, now_ts)
                if reason:
                    _sell_position(db, cfg, bot, mint, price, reason,
                                   rpc_factory, real_executor, actions,
                                   feed=feed)
                    continue
            avg_price = float(pos["avg_price"])
            peak = pos["peak_price"]
            peak = float(peak) if peak is not None else avg_price
            if rules["kind"] == "gnat":
                hard = _gnat_hard_exit_reason(avg_price, peak, price)
            else:
                hard = _hard_exit_reason(float(bot["stop_loss_pct"]),
                                         float(rules["trailing_stop_pct"]),
                                         rules["kind"], avg_price, peak,
                                         price)
            if hard:
                _sell_position(db, cfg, bot, mint, price, hard,
                               rpc_factory, real_executor, actions,
                               feed=feed)
    return actions


def _fast_exit_loop(db_path: str, cfg, feed, rpc_factory=None,
                    real_executor=None) -> None:
    """The tick-driven fast-exit thread, forever: wake on a live-feed
    tick (or every 5 seconds as a fallback), then check the dirtied
    mints' open positions for hard exits. Opens a fresh DB connection
    per wake. A failed wake is logged and the next one still runs —
    the loop backstop covers any miss."""
    while True:
        _TICK_EVENT.wait(timeout=5.0)
        _TICK_EVENT.clear()
        db = init_db(db_path)
        try:
            actions = _fast_exit_once(db, cfg, feed, rpc_factory,
                                      real_executor)
        except Exception as exc:
            print(f"[fast-exit] pass failed: {exc}", flush=True)
            actions = []
        finally:
            db.close()
        for action in actions:
            print(f"[fast-exit] {action['action']}:"
                  f"{action.get('coin', '')} "
                  f"({action.get('reason', '')})", flush=True)


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
        if rules is not None and rules["kind"] in ("trenches", "scanner",
                                                  "gnat"):
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

    Prices merge the SOL price with the trenches feed so held
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
        # Tick-driven fast exits (tuning build 2026-11-15): the live
        # feed's ticks wake a fast-exit thread that sells a position
        # the moment a tick crosses its stop / trailing stop / rug
        # line, instead of waiting for the next 30-second loop pass.
        # The loop stays as the backstop. Started once per process,
        # only when a live feed is attached.
        global _FAST_EXIT_STARTED
        with _FAST_EXIT_LOCK:
            if not _FAST_EXIT_STARTED and feed is not None:
                listener_ok = True
                try:
                    feed.set_tick_listener(_on_live_tick)
                except Exception:
                    listener_ok = False
                if listener_ok:
                    _FAST_EXIT_STARTED = True
                    threading.Thread(
                        target=_fast_exit_loop,
                        args=(db_path, cfg, feed),
                        kwargs={"rpc_factory": rpc_factory,
                                "real_executor": real_executor},
                        daemon=True, name="fast-exit").start()
                    print("[fast-exit] tick-driven exits started (5s)",
                          flush=True)
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
