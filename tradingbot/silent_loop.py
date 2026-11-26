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

Real swaps: the dedicated signing bot executes real trades on the
configured NETWORK (devnet or mainnet), through pump.fun
bonding-curve buys/sells (see executor.py) and Jupiter swaps for
graduated coins. A real trade is refused plainly — never faked —
whenever it is unverified: a coin that is not a pump.fun
token mint, a token with no bonding curve, or a trade
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
from .executor import (_skip, execute_real_trade, is_curve_complete,
                       pending_signature_for, reconcile_pending_trades)
from . import gate_fuel as gate_fuel_mod
from . import pit as pit_mod
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
                    MIDDY_BUY_DOMINANCE_RATIO, MIDDY_CATEGORY,
                    MIDDY_COOLDOWN_SECONDS, MIDDY_DAILY_LOSS_BREAKER_PCT,
                    MIDDY_MAX_BUYS_PER_HOUR, MIDDY_MID_HOLD_BUY_RATIO,
                    MIDDY_MIN_BUYS_PER_HOUR, MIDDY_MIN_GAIN_5M_PCT, MIDDY_SLOW_FLAT_PCT,
                    MIDDY_SLOW_SECONDS, MIDDY_STOP_LOSS_PCT,
                    MIDDY_TP_CEILING_PCT, MIDDY_TP_MID_PCT,
                    MIDDY_TP_SMALL_PCT,
                    MIDDY_TRAILING_STOP_PCT, PIT_BUY_SIZE_SOL, load_rules)
from .scanner import (BREAKEVEN_TRIGGER_PCT, MARK_KEEP_24H_SECONDS,
                      check_top10_concentration,
                      check_top10_rug, clear_watches, detect_regime,
                      gain_3m_pct, gain_window_pct, next_ladder_rung,
                      passes_buy_filter, passes_momentum_gate, record_marks,
                      signal_dead, top10_share, top10_total)
from .signals import signal_for_coin
from .solana import (SolanaRpcClient, is_valid_address,
                     lamports_to_sol, rpc_client_for_config)
from .streaming import SOL_MAX_AGE_SECONDS, chain_price_usd_for_mints
from .trenches import fetch_trenches_scan

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


def _live_sol_price_usd(feed, prices_data=None):
    """The live SOL price in USD, or None.

    The feed's own SOL tick — the one SOL number in the app (fed
    by the same Jupiter price every other SOL read uses). The tick
    is accepted up to SOL_MAX_AGE_SECONDS old (90s, the stream's
    own bound): SOL refreshes from Jupiter every 60s while the
    feed's display staleness is 30s, so a strict 30s cut would
    leave the loop with no SOL rate half the time; SOL moves
    slowly, so 90s bounds the conversion error to SOL's ~90s move.
    This is a staleness bound on the ONE source, not a second
    source: `prices_data` is accepted for call-site compatibility
    and deliberately ignored — there is no second SOL print to
    cross-check against, and a disagreement between two SOL prints
    was refusing real trades. None means the loop must not price
    anything in USD this scan: curve pricing fails closed and
    executed P&L is left unrecorded rather than computed from a
    stale number. SOL is the unit of account for pump.fun trades
    (curve prices, executed SOL in/out), not a traded major here.
    """
    if feed is not None:
        try:
            age = feed.tick_age_seconds("SOL")
        except Exception:
            age = None
        if age is not None and age <= SOL_MAX_AGE_SECONDS:
            try:
                tick = feed.snapshot(["SOL"]).get("SOL")
            except Exception:
                tick = None
            if tick is not None:
                try:
                    price = float(tick["price"])
                except (TypeError, ValueError):
                    price = None
                else:
                    if price and price > 0:
                        return price
    return None


def _chain_price_usd(mints, feed, cfg, rpc_factory):
    """{mint: price_usd} for mints priced from the single source:
    the feed's live tick first; any mint without a fresh tick is
    priced by one batched Helius RPC read of its bonding curve
    (graduated -> Raydium pool, same lookup the stream uses).
    A mint no source can price is absent — never a stale, guessed
    or second-source number. No live SOL tick means {} (no USD
    conversion without a live SOL rate)."""
    mints = [m for m in dict.fromkeys(mints or []) if m]
    if not mints:
        return {}
    out: dict = {}
    missing = []
    if feed is not None:
        try:
            ticks = feed.snapshot(mints)
        except Exception:
            ticks = {}
        for mint in mints:
            tick = (ticks or {}).get(mint)
            if tick is not None and not tick.get("stale"):
                try:
                    price = float(tick["price"])
                except (TypeError, ValueError):
                    price = None
                else:
                    if price and price > 0:
                        out[mint] = price
                        continue
            missing.append(mint)
    else:
        missing = list(mints)
    if not missing:
        return out
    sol_price = _live_sol_price_usd(feed)
    if sol_price is None:
        return out
    try:
        client = (rpc_factory(cfg.rpc_url) if rpc_factory is not None
                  else rpc_client_for_config(cfg))
    except Exception:
        return out
    try:
        chained = chain_price_usd_for_mints(client, missing, sol_price)
    except Exception:
        return out
    out.update(chained or {})
    return out


def _reprice_trenches_data(trenches_data, feed, cfg, rpc_factory):
    """Rewrite a trenches scan in place onto the single source, at
    the loop boundary — the one place a second source's numbers
    could enter an evaluation.

    * Every entry's `price_usd` becomes the chain price (feed live
      tick first, else one batched Helius RPC curve/pool read). An
      entry no source can price is dropped from `prices` and
      `candidates` — unpriced means no trade, never a guess.
    * The DexScreener change figures (`change_5m_pct`,
      `change_1h_pct`, `change_6h_pct`, `change_24h_pct`) are nulled
      on every kept entry. Momentum is recomputed downstream from
      the loop's own recorded price marks (see _evaluate_rules_bot);
      a change figure no mark can answer stays None and its gate
      fails closed.
    * Activity metadata is untouched: volume, buy/sell counts,
      market cap and socials stay DexScreener's — discovery +
      activity only, never a price. Returns the same dict."""
    if not trenches_data or trenches_data.get("error"):
        return trenches_data
    prices = trenches_data.get("prices") or {}
    if not prices:
        trenches_data["candidates"] = []
        return trenches_data
    chain = _chain_price_usd(list(prices), feed, cfg, rpc_factory)
    kept = {}
    for mint, entry in prices.items():
        price = chain.get(mint)
        if price is None:
            continue
        entry = dict(entry)
        entry["price_usd"] = price
        entry["change_5m_pct"] = None
        entry["change_1h_pct"] = None
        entry["change_6h_pct"] = None
        entry["change_24h_pct"] = None
        kept[mint] = entry
    trenches_data["prices"] = kept
    # The candidates list holds the same entry objects the scan
    # built — point them at the repriced copies so the buy sections
    # read chain prices, not the dropped DexScreener ones.
    trenches_data["candidates"] = [
        kept[(c or {}).get("mint")]
        for c in (trenches_data.get("candidates") or [])
        if (c or {}).get("mint") in kept]
    return trenches_data


# --- tuning build 2026-11-15: shared-engine guards ---------------------

def _default_rpc_factory(cfg):
    """rpc_factory for the loop's own RPC reads (top-10 watch, entry
    gates, buy-path balance check, sell-claim reconcile): clients
    built with the configured fallback RPC, so one rate-limited
    endpoint cannot freeze every balance read (2026-11-24: the
    bare SolanaRpcClient default left the Luna bot skipping every
    trade with "token balance unavailable" while prices kept
    moving). References the module-global SolanaRpcClient, so tests
    patching silent_loop.SolanaRpcClient still intercept it (see
    the test-mock trap: patch silent_loop.SolanaRpcClient as well
    as executor.SolanaRpcClient, or the loop silently hits live
    RPC). An injected rpc_factory (tests, or a future replacement)
    is used exactly as before."""
    fallback = (getattr(cfg, "rpc_fallback_url", "") or "").strip() or None
    if fallback:
        return lambda url: SolanaRpcClient(
            url, fallback_rpc_url=fallback)
    # No fallback configured: build bare, so a client class without
    # the fallback kwarg (test fakes) still constructs.
    return lambda url: SolanaRpcClient(url)


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


def validate_loop_startup(cfg: Config, candidates=None) -> None:
    """Startup guardrail (research report audit #14): refuse to
    start the silent loop on mainnet when a devnet-watchlist mint
    is present as a candidate. `candidates` is the devnet mint list
    to check (tests inject it); in production it is the configured
    list — but ONLY when DEVNET_TRENCH_MINTS was set explicitly.
    The built-in defaults (Gate/AuditTest) are inert on mainnet:
    active_mints() returns [] there, so they are never candidates
    and alone never block the loop. An operator who explicitly
    configures devnet mints while on mainnet has misconfigured the
    deployment, and the loop refuses to start rather than trade
    them. Raises ValueError."""
    if cfg.network != "mainnet":
        return
    if candidates is None:
        if not getattr(cfg, "devnet_trench_mints_explicit", False):
            return
        candidates = list(cfg.devnet_trench_mints or [])
    bad = [m for m in (candidates or []) if m]
    if bad:
        raise ValueError(
            "refusing to start the silent loop: NETWORK=mainnet but "
            "devnet watchlist mints are configured as candidates "
            f"({', '.join(bad)}). Clear DEVNET_TRENCH_MINTS (or set "
            "it empty) — devnet test tokens must never be candidates "
            "on mainnet.")


def _open_sell_claim(db, bot_id: int, coin: str):
    """The open persistent sell claim for (bot, coin), or None."""
    try:
        return db.execute(
            "SELECT * FROM sell_claims WHERE bot_id = ? AND coin_id = ? "
            "AND status = 'open' ORDER BY id DESC LIMIT 1",
            (bot_id, coin)).fetchone()
    except Exception:
        return None


def _place_sell_claim(db, user_id: int, bot_id: int, coin: str,
                      amount: float):
    """Open a persistent sell claim for (bot, coin). Returns the
    claim id, or None when the row could not be written (the
    in-memory claim still guards the same-process race)."""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            "INSERT INTO sell_claims (user_id, bot_id, coin_id, amount, "
            "status, claimed_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (user_id, bot_id, coin, float(amount), now, now))
        db.commit()
        return cur.lastrowid
    except Exception:
        return None


def _resolve_sell_claim(db, claim_id, status: str,
                        signature=None) -> None:
    """Mark a claim confirmed (with the landing signature) or
    released. Never raises."""
    if claim_id is None:
        return
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE sell_claims SET status = ?, signature = ?, "
            "updated_at = ? WHERE id = ?",
            (status,
             str(signature) if signature is not None else None,
             now, claim_id))
        db.commit()
    except Exception:
        return


def _reconcile_sell_claim(db, cfg: Config, bot, coin: str,
                          rpc) -> str:
    """Reconcile an open persistent sell claim against the chain
    before any re-sell. Returns "ok" (no claim, or a stale claim
    released), "sold" (the claimed sell already landed — the
    position is gone), or "blocked" (the claimed sell may still be
    in flight — do not re-sell yet). Never raises: on any doubt
    the safe answer is "blocked", never a second sell."""
    claim = _open_sell_claim(db, int(bot["id"]), coin)
    if claim is None:
        return "ok"
    signature = claim["signature"] or pending_signature_for(
        db, int(bot["user_id"]), coin, "sell")
    if not signature:
        # No signature was ever recorded: nothing was sent (or the
        # send predates pending-trade recording) — release it.
        _resolve_sell_claim(db, claim["id"], "released")
        return "ok"
    get_tx = getattr(rpc, "get_transaction", None) if rpc else None
    if get_tx is None:
        return "blocked"
    try:
        receipt = get_tx(signature)
    except Exception:
        return "blocked"
    if receipt is not None:
        meta = receipt.get("meta") or {}
        if meta.get("err") is None:
            _resolve_sell_claim(db, claim["id"], "confirmed",
                                signature)
            return "sold"
        _resolve_sell_claim(db, claim["id"], "released")
        return "ok"
    try:
        claimed_at = datetime.fromisoformat(
            str(claim["claimed_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
    except (TypeError, ValueError):
        age = 0.0
    if age > float(cfg.pending_trade_horizon_seconds):
        _resolve_sell_claim(db, claim["id"], "released")
        return "ok"
    return "blocked"


def _call_real_executor(real_executor, bot, user_id, coin, side, amount,
                        price, db, cfg, decision_price_as_of=None,
                        sol_price_usd=None):
    """Call the real executor, passing the price-gate context (how
    old the decision price is, the one SOL price) when it is the
    loop's default executor — which forwards it to the pump.fun /
    Jupiter executors. An injected executor (tests, or a future
    replacement) keeps the plain 7-argument protocol: it is called
    exactly as before, with no extra arguments. There is no second
    SOL price any more: the feed's SOL tick is the one SOL number,
    so there is nothing to cross-check against."""
    if real_executor is default_real_executor:
        age = None
        if decision_price_as_of is not None:
            try:
                age = time.time() - float(decision_price_as_of)
            except (TypeError, ValueError):
                age = None
        return real_executor(
            bot, user_id, coin, side, amount, price, db, cfg,
            decision_price_age_seconds=age,
            sol_price_usd=sol_price_usd)
    return real_executor(bot, user_id, coin, side, amount, price,
                         db, cfg)


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
                  now_ts: float, seconds: float | None = None) -> None:
    """Block re-buying (bot, mint) for `seconds` after a losing
    close (default REBUY_COOLDOWN_SECONDS, 30 minutes — Middy
    passes its own 5 minutes). Never raises — a failed write
    simply means no cooldown is recorded."""
    try:
        until = datetime.fromtimestamp(
            now_ts + (REBUY_COOLDOWN_SECONDS if seconds is None
                      else float(seconds)),
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
    the position exits in full. The 5-minute change is the loop's
    own recorded price marks (filled in on the entry by the
    evaluation); the spec's 10-second net-SOL-flow window does not
    exist in the app's data (no sub-5-minute flow, no transaction
    stream). Missing data never fires. Pure — no DB."""
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


# --- Middy preset (2026-11-24): James's spec, honest-mapped ------
# Mid-exposure, paper-first. Every threshold below is James's
# number unless the comment says REMY DEFAULT (the 1% gain floor,
# the $1,000 volume floor, the +/-0.5% slow band, the 21% top-10
# cap) or REMY PROPOSAL (the past-+55% moon test). The sub-second
# entry is NOT enforced as written — entries run on the 10-second
# loop with a second confirming scan; only the hard exits also
# run on the tick-driven fast-exit thread (see MIDDY_NOTES).

def _middy_hard_exit_reason(avg_price: float, peak_price: float,
                            price: float) -> str | None:
    """Middy's hard exits at `price`: the -5.5% hard stop from
    entry and the -8% trailing stop from the highest price since
    holding. No breakeven move — James ordered none. Pure — no
    DB, so the loop and the tick-driven fast-exit thread share
    exactly this logic. Returns the reason string or None."""
    if avg_price <= 0 or price <= 0:
        return None
    peak = max(float(peak_price), price)
    if _lte(price, avg_price * (1.0 - MIDDY_STOP_LOSS_PCT / 100.0)):
        return "stop_loss"
    if _lte(price, peak * (1.0 - MIDDY_TRAILING_STOP_PCT / 100.0)):
        return "trailing stop"
    return None


def _middy_mid_hold(entry: dict) -> bool:
    """The mid-level hold test: hold for the mid level ONLY while
    buys beat sells — any positive edge — in the 5-minute bucket.
    James, 2026-11-25 ~21:08 MST ("Both") and ~21:09 MST ("That
    one too"): the old 75/25 (3:1) ratio is gone AND the
    still-gaining condition is REMOVED entirely — the hold test is
    buys > sells only. A tie (buys == sells) does NOT beat sells.
    Missing or unusable counts fail closed — a hold that cannot
    be verified is a sell. Pure — no DB."""
    if not entry:
        return False
    buys = entry.get("txns_m5_buys")
    sells = entry.get("txns_m5_sells")
    try:
        buys = int(buys) if buys is not None else None
        sells = int(sells) if sells is not None else None
    except (TypeError, ValueError):
        return False
    if buys is None or sells is None or buys <= 0:
        return False
    if sells <= 0:
        return True
    return buys > MIDDY_MID_HOLD_BUY_RATIO * sells


def _middy_take_profit_reason(entry: dict, avg_price: float,
                              peak_price: float,
                              price: float) -> str | None:
    """Middy's two-tier take profit, in plain words — James's
    numbers: SMALL is +17% (retuned 2026-11-25 ~21:10 MST from
    +5% — "Sell all at 17% profit": take the profit on coins
    Middy is done with, exit, forget the coin); the HIGHER window
    is 25% to no more than 37% (hold for more profit ONLY while
    the mid-level hold test passes — buys beating sells, any
    positive edge, no still-gaining check); AT 37% the profit is
    secured IMMEDIATELY and Middy moves on. The 37% cap is the
    HARD CEILING: never hold past it, no exceptions — going
    higher exposes Middy to rugs and to others securing their
    profit, and Middy wants a clean exit before traders lock
    profit (James, 2026-11-24 ~12:35). Below +17% there is no
    take profit. peak_price is unused — the ceiling does not need
    the peak, it only needs the profit from entry. Pure — no DB."""
    if avg_price <= 0 or price <= 0:
        return None
    profit = (price - avg_price) / avg_price * 100.0
    if profit < MIDDY_TP_SMALL_PCT:
        return None
    if profit >= MIDDY_TP_CEILING_PCT:
        return (f"take profit: +{profit:.1f}% at/above the "
                f"{MIDDY_TP_CEILING_PCT:.0f}% ceiling — secure the "
                f"profit immediately and move on, never hold past it")
    if _middy_mid_hold(entry):
        return None
    if profit >= MIDDY_TP_MID_PCT:
        return (f"take profit: +{profit:.1f}% in the higher window "
                f"(25%-37%) and the hold test failed — buys no "
                f"longer beating sells")
    return (f"take profit: +{profit:.1f}% at the small take-profit "
            f"level (+{MIDDY_TP_SMALL_PCT:.0f}%) and the mid-level "
            f"hold test failed — buys no longer beating sells")


def _middy_slow_reason(opened_ts: float | None, entry: dict,
                       now_ts: float) -> str | None:
    """Middy slow coins: a flat coin sells all after 120 seconds.
    Flat is the 5-minute change within +/-0.5% (the same band as
    Pit), from the loop's own recorded price marks (filled in on
    the entry by the evaluation). A position with no recorded open
    time, or no 5-minute change, never fires — missing data exits
    nothing. Pure — no DB."""
    if opened_ts is None:
        return None
    if now_ts - opened_ts < MIDDY_SLOW_SECONDS:
        return None
    change_5m = (entry or {}).get("change_5m_pct")
    if change_5m is None:
        return None
    try:
        change_5m = float(change_5m)
    except (TypeError, ValueError):
        return None
    if abs(change_5m) <= MIDDY_SLOW_FLAT_PCT:
        return (f"slow coin: flat within +/-{MIDDY_SLOW_FLAT_PCT:.1f}% "
                f"for {MIDDY_SLOW_SECONDS}s — selling all")
    return None


def _middy_already_bought(db, bot_id: int, mint: str) -> bool:
    """True when this bot copy already bought `mint` once — paper
    or real, open or closed. The trade ledger is the record: every
    fill, paper and real, writes a buy row. A coin bought once is
    never bought again by the same bot, even if the last trade
    profited (James, 2026-11-24 ~12:22 MST: repeat buys would be
    deemed a scam and avoided). Another bot copy buying the same
    coin is unaffected — the check is per bot, not per coin."""
    try:
        row = db.execute(
            "SELECT 1 FROM trade_ledger WHERE bot_id = ? AND coin = ? "
            "AND side = 'buy' LIMIT 1",
            (bot_id, mint)).fetchone()
        return row is not None
    except Exception:
        # A ledger failure is a fail-CLOSED buy: without a
        # trustworthy record, the coin is treated as already
        # bought rather than risking a repeat buy.
        return True


def _middy_entry_gates(db, cfg, entry: dict, rpc_factory,
                       rpc_cache: dict) -> tuple[bool, str]:
    """Middy's own entry gate, after the gain + buy-filter +
    momentum checks: buys must just beat sells — any positive
    edge — in the 5-minute bucket (James, 2026-11-25 ~21:07 MST,
    "Buys just beat sells"). A tie (buys == sells) does NOT beat
    sells. The spec's window is the feed's 5-minute bucket —
    transaction counts, not unique wallets, and no sub-5-minute
    flow exists in the app's data. Fail-closed: a coin whose
    counts are missing is never bought. Devnet watchlist mints
    skip the gate, exactly like Gnat's and Pit's, so the devnet
    paper path still exercises the engine."""
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
        return False, "middy buy dominance: txn counts unreadable"
    if buys is None or sells is None:
        return False, "middy buy dominance: no 5-minute txn data"
    if buys <= 0:
        return False, "middy buy dominance: no 5-minute buys"
    if sells > 0 and buys <= MIDDY_BUY_DOMINANCE_RATIO * sells:
        return False, (
            f"middy buy dominance: buys {buys} vs sells {sells} in "
            f"the last 5 minutes — buys must beat sells")
    return True, ""


def _middy_buy_halt(db, bot, now_ts: float, feed,
                    prices_data: dict | None) -> tuple[bool, str]:
    """Middy's daily-loss breaker on NEW BUYS. Exits still run —
    a halted bot can still stop out, it just cannot open anything
    new. Returns (halted, reason).

    James's spec: down 20% of the total balance the user started
    with. The starting balance is the day-start capital (the
    user's total balance at the start of the UTC day, from the
    portfolio snapshots — paper portfolio value, or the app
    wallet's SOL priced live). Today's realized P&L comes from
    the trade ledger. When the day-start capital cannot be
    determined the breaker cannot be evaluated and does NOT trip
    — the loss is still logged by the caller, never hidden.
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
    if daily_realized >= 0:
        return False, ""
    start = _day_start_capital(db, bot, feed, prices_data)
    if start is None or start <= 0:
        return False, ""
    limit = MIDDY_DAILY_LOSS_BREAKER_PCT / 100.0 * float(start)
    if daily_realized <= -limit:
        return True, (
            f"middy daily-loss breaker: realized "
            f"${daily_realized:.2f} today is at or beyond -20% of the "
            f"${float(start):.2f} starting balance "
            f"(${limit:.2f}) — no new buys until tomorrow")
    return False, ""


def _middy_buys_last_hour(db, bot_id: int, now_ts: float) -> int:
    """Executed buys from the trade ledger in the rolling 60
    minutes (paper and real alike — every fill counts). A ledger
    failure returns 0 and fails open for the count only — the
    callers that gate on it treat a missing count as no halt."""
    try:
        hour_ago = datetime.fromtimestamp(
            now_ts - 3600.0, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        row = db.execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE bot_id = ? "
            "AND side = 'buy' AND created_at >= ?",
            (bot_id, hour_ago)).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _middy_pace_halt(db, bot, now_ts: float) -> tuple[bool, str]:
    """Middy's buy pace on NEW BUYS: at most
    MIDDY_MAX_BUYS_PER_HOUR buys in a rolling 60 minutes. Counts
    executed buys from the trade ledger (paper and real alike —
    every fill counts, including a position since closed). Exits
    still run — a halted bot can still stop out, it just cannot
    open anything new. Returns (halted, reason)."""
    count = _middy_buys_last_hour(db, bot["id"], now_ts)
    if count >= MIDDY_MAX_BUYS_PER_HOUR:
        return True, (
            f"middy buy pace: {count} buys in the last hour "
            f"(max {MIDDY_MAX_BUYS_PER_HOUR}) — waiting for the "
            f"window to slide before the next buy")
    return False, ""


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
                         amount: float, price: float, db, cfg,
                         decision_price_age_seconds=None,
                         sol_price_usd=None) -> dict:
    """Real executor: the account's dedicated signing bot executes
    pump.fun trades on mainnet and devnet (see executor.py) — and
    Jupiter swaps on mainnet for graduated coins (see
    jupiter_executor.py, gated behind James's go-ahead). The venue
    is the bonding curve: complete (graduated) -> Jupiter, still
    bonding -> pump.fun, unreadable -> refused plainly, never
    guessed. Anything unverified is skipped honestly — never a fake
    fill. The price-gate context (decision price age, the one SOL
    price) is forwarded to whichever executor runs, so a real buy is
    checked against a fresh on-chain read immediately before it
    executes."""
    if not is_valid_address(coin):
        return _skip("Not a valid token address")
    try:
        rpc = rpc_client_for_config(cfg)
        complete = is_curve_complete(rpc, coin)
    except Exception:
        complete = None
    if complete is None:
        return _skip("bonding curve unavailable")
    if complete:
        return execute_jupiter_swap(
            bot, user_id, coin, side, amount, db, cfg,
            decision_price_usd=price,
            decision_price_age_seconds=decision_price_age_seconds,
            sol_price_usd=sol_price_usd)
    return execute_real_trade(
        bot, user_id, coin, side, amount, price, db, cfg,
        decision_price_age_seconds=decision_price_age_seconds,
        sol_price_usd=sol_price_usd)


def _mint_price_map(coins, db, bot_id, now_ts, feed, cfg,
                    rpc_factory) -> dict:
    """{mint: {"price", "change_24h"}} for pump.fun mints, from the
    single source: the feed's live tick first, else one batched
    Helius RPC curve/pool read. Signals bots watch named coins —
    in this app those are mint addresses, and the price list (SOL
    only) has no entry for them. The 24-hour change comes from the
    loop's own recorded price marks for this bot's watched coins
    (kept 25 hours): a watched coin with less than 24 hours of
    marks has no 24-hour change yet, so no signal fires for it —
    never a second source's figure, never a guess. A mint with no
    price is left out — unpriced means no trade, never a guess."""
    mints = [c for c in (coins or []) if is_valid_address(c)]
    if not mints:
        return {}
    prices = _chain_price_usd(mints, feed, cfg, rpc_factory)
    out = {}
    if not prices:
        return out
    # Record this scan's marks for the watched coins (25-hour keep)
    # before reading the 24-hour gain off them.
    try:
        record_marks(db, bot_id,
                     [{"mint": m, "price_usd": p}
                      for m, p in prices.items()],
                     _now(), keep_seconds=MARK_KEEP_24H_SECONDS)
    except Exception:
        pass
    for mint, price in prices.items():
        try:
            change_24h = gain_window_pct(
                db, bot_id, mint, price, now_ts, 86400)
        except Exception:
            change_24h = None
        out[mint] = {"price": price, "change_24h": change_24h}
    return out


def _pit_buy_halt(db, bot, now_ts, feed, prices_data, daily_loss_sol):
    """Pit daily-loss breaker: down the breaker amount (0.025 SOL
    by default — about $5 CAD at the Nov-23 SOL price) in a day,
    and the bot buys nothing more until the next day. The breaker
    reads today's realized sells from the trade ledger; an empty
    ledger means no losses, so it never fires spuriously. Without
    a live SOL price the $5 figure has no exchange rate — the
    breaker then fails open, as documented in the build MANIFEST.
    Returns (halted, reason)."""
    if bot["mode"] == "paper":
        return False, ""
    # created_at is stored as an ISO string ("2026-11-23T...Z"),
    # not an epoch — match today's date by prefix, like the Gnat
    # breaker. DATE(created_at, 'unixepoch') would be NULL here
    # and the breaker would never fire.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT pnl_usd FROM trade_ledger WHERE bot_id = ? "
        "AND side = 'sell' AND pnl_usd IS NOT NULL AND "
        "substr(created_at, 1, 10) = ?",
        (bot["id"], today)).fetchall()
    daily_pnl_usd = sum(float(r["pnl_usd"]) for r in rows)
    if daily_pnl_usd >= 0.0:
        return False, ""
    sol_price = _live_sol_price_usd(feed, prices_data)
    if sol_price is None or sol_price <= 0:
        # No live SOL price: the $5 CAD breaker has no exchange
        # rate, so it cannot be computed honestly. Fail open and
        # say so — the worst case is one unprotected day, not a
        # bot stopped by a made-up number.
        return False, ""
    loss_sol = -daily_pnl_usd / sol_price
    if loss_sol >= float(daily_loss_sol):
        return True, (
            f"Pit daily-loss breaker: down {loss_sol:.3f} SOL "
            f"today (limit {float(daily_loss_sol):.3f} SOL) — no new "
            f"buys until tomorrow")
    return False, ""


def _pit_hard_exit_reason(avg, peak, price, stop_loss_pct,
                          breakeven_trigger_pct):
    """Pit hard exits, from the live price: the 4.9% stop loss
    fires in any time period, and a position that reached +6% has
    its stop moved to breakeven. This is a TRIGGER — it fires at
    -4.9% and the position exits at the next available price, so
    with 5% exit slippage the realized loss can be about -9.9%.
    The preset summary says so plainly; 'never lose more than the
    stop loss' is not promised anywhere. Returns a reason string
    or ''."""
    if price <= avg * (1.0 - float(stop_loss_pct) / 100.0):
        return (f"stop loss: -{stop_loss_pct}% in any time period — "
                f"exit now, worst case about "
                f"-{stop_loss_pct + 5.0:.1f}% realized at 5% exit "
                f"slippage")
    if (peak is not None and peak > 0
            and peak >= avg * (1.0 + float(breakeven_trigger_pct) / 100.0)
            and price <= avg):
        return ("breakeven stop: peak reached "
                f"+{breakeven_trigger_pct}% and the price fell "
                f"back to the entry — sell at cost")
    return ""


def _pit_pullback_fires(entry, pit_settings):
    """Pit pullback, from the loop's own 5-minute marks: the coin
    fell at or below -2% in the last 5 minutes — the top of the
    list is a dead coin now, so the position closes. Returns
    True/False."""
    change = entry.get("change_5m_pct") if entry else None
    if change is None:
        return False
    try:
        change = float(change)
    except (TypeError, ValueError):
        return False
    return change <= -float(pit_settings.get("pullback_pct", 2.0))


def _pit_flat_fires(entry, pit_settings):
    """Pit flat-dead, from the loop's own 5-minute marks: the coin
    moved less than +/-0.5% in the last 5 minutes — no movement,
    no signal, close it and consider it dead. Returns True/False."""
    change = entry.get("change_5m_pct") if entry else None
    if change is None:
        return False
    try:
        change = float(change)
    except (TypeError, ValueError):
        return False
    return abs(change) <= float(pit_settings.get("flat_pct", 0.5))


def _pit_debounced_exit(db, bot_id, coin, kind, condition):
    """The Pit 2-tick debounce: a pullback or flat exit fires only
    when the condition holds on two consecutive loop ticks (the
    votes live in bot_positions.pit_exit_votes, the kind in
    pit_exit_kind, so a pullback vote can never be spent on a
    flat exit). A condition that clears resets the votes. Returns
    True when the exit fires on this tick. Hard exits (stop,
    breakeven, take profit, rug) never go through this — they fire
    immediately."""
    pos = db.execute(
        "SELECT pit_exit_votes, pit_exit_kind FROM bot_positions "
        "WHERE bot_id = ? AND coin_id = ?",
        (bot_id, coin)).fetchone()
    if pos is None:
        return False
    votes = int(pos["pit_exit_votes"] or 0)
    stored_kind = pos["pit_exit_kind"] or ""
    if not condition:
        if stored_kind == kind:
            db.execute(
                "UPDATE bot_positions SET pit_exit_votes = 0, "
                "pit_exit_kind = '' WHERE bot_id = ? AND coin_id = ?",
                (bot_id, coin))
            db.commit()
        return False
    votes = votes + 1 if stored_kind == kind else 1
    db.execute(
        "UPDATE bot_positions SET pit_exit_votes = ?, "
        "pit_exit_kind = ? WHERE bot_id = ? AND coin_id = ?",
        (votes, kind, bot_id, coin))
    db.commit()
    return votes >= 2


def _pit_entry_gates(db, cfg, entry, rpc_factory, pit_settings):
    """Pit entry gates, applied after the shared buy filter: the
    buys must outnumber the sells at least 2 to 1 in the 5-minute
    bucket, and the coin must sit between 15% and 65% through its
    bonding curve (read on-chain). The devnet watchlist carve-out
    skips both gates for test mints, exactly like Gnat's, so the
    devnet paper path still exercises the engine. Returns
    (True, '') or (False, reason)."""
    mint = entry.get("mint")
    if not mint:
        return False, "no mint to check"
    if entry.get("devnet_watchlist"):
        # Devnet watchlist mints (the operator's own test coins)
        # skip both gates: they have no DexScreener txn data and
        # their curves sit below 15%, so the gates would block
        # every devnet test buy. The gain, volume, scam and
        # 2-scan gates still apply to them, so exits stay testable
        # on devnet.
        return True, ""
    buys = entry.get("txns_m5_buys")
    sells = entry.get("txns_m5_sells")
    try:
        buys = int(buys) if buys is not None else None
        sells = int(sells) if sells is not None else None
    except (TypeError, ValueError):
        buys = sells = None
    ratio = float(pit_settings.get("buy_ratio_min", 2.0))
    if buys is None or sells is None:
        return False, ("pit: no 5-minute buy/sell counts — buys "
                       "must outnumber sells 2 to 1")
    if sells <= 0:
        if buys <= 0:
            return False, ("pit: no 5-minute buys — buys must "
                           "outnumber sells 2 to 1")
    elif buys < ratio * sells:
        return False, (f"pit: buys {buys} vs sells {sells} in the "
                       f"last 5 minutes — buys must outnumber "
                       f"sells at least {ratio:g} to 1")
    if rpc_factory is None:
        return False, "pit: no RPC connection — curve band unreadable"
    try:
        rpc = rpc_factory(cfg.rpc_url)
        curve_pda, _bump = bonding_curve_pda(mint)
        account = rpc.get_account_info(curve_pda)
        if account is None:
            return False, "pit: no bonding curve found for this mint"
        curve = parse_bonding_curve(
            account.get("data") if account else None)
        progress = curve_progress_pct(curve)
    except Exception as exc:
        return False, f"pit: curve read failed — {exc}"
    lo = float(pit_settings.get("curve_min_pct", 15.0))
    hi = float(pit_settings.get("curve_max_pct", 65.0))
    if progress < lo or progress > hi:
        return False, (f"pit: curve {progress:.0f}% through — outside "
                       f"the {lo:g}-{hi:g}% band")
    return True, ""


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
    close sets the re-buy cooldown for (bot, coin); a full close
    fills in the signal-log outcome for the trade. Middy cools
    down for 5 minutes (its spec), every other kind for 30.
    Never raises."""
    try:
        if pnl_usd is not None and pnl_usd < 0:
            try:
                category = bot["category"]
            except Exception:
                category = None
            seconds = (MIDDY_COOLDOWN_SECONDS
                       if category == MIDDY_CATEGORY
                       else REBUY_COOLDOWN_SECONDS)
            _set_cooldown(db, bot["id"], coin, "losing close",
                          datetime.now(timezone.utc).timestamp(),
                          seconds=seconds)
        if sold_amount >= full_amount:
            _record_signal_outcome(db, bot["id"], coin)
    except Exception:
        pass


def _sell_position(db: sqlite3.Connection, cfg: Config, bot, coin: str,
                   price: float, reason: str, rpc_factory,
                   real_executor, actions: list, amount: float | None = None,
                   ladder_stage: int | None = None,
                   feed=None, prices_data: dict | None = None,
                   sweep_id: str | None = None) -> None:
    """Sell a bot's position in `coin` (paper ledger, or the real
    executor). `amount` None means the whole position; otherwise a
    partial sell (sell-ladder rung). `ladder_stage` advances the
    position's rung counter when given. `price` is the live mark
    price: it drives the stop/ladder decision and is the fallback
    display price — real-mode P&L is NEVER computed from it (see
    below). Logs the outcome either way; never fakes a fill.
    `sweep_id` marks a sell as part of the last-credit exit-all
    sweep: the sweep is the only spender of the last fuel credit,
    so a sweep sell skips the per-trade fuel gate and the per-trade
    consume — the sweep consumes exactly one credit for the whole
    pass, after the sells (see run_exit_all_sweep)."""
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
                               full_amount=full_amount,
                               sweep_id=sweep_id)
    finally:
        _release_position_for_sell(int(pos["id"]))


def _sell_position_claimed(db: sqlite3.Connection, cfg: Config, bot,
                           coin: str, price: float, reason: str,
                           rpc_factory, real_executor, actions: list,
                           amount: float, ladder_stage: int | None = None,
                           feed=None, prices_data: dict | None = None,
                           full_amount: float | None = None,
                           sweep_id: str | None = None) -> None:
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
    # Real mode: the signing bot executes (executor decides).
    # Persistent sell claim (restart-safe, 2026-11-22): reconcile
    # any open claim against the chain BEFORE re-selling, so a
    # ladder rung can never double-sell after a restart between an
    # on-chain fill and the position close.
    try:
        gate_client = rpc_factory(cfg.rpc_url)
    except Exception:
        gate_client = None
    claim_state = _reconcile_sell_claim(db, cfg, bot, coin, gate_client)
    if claim_state == "sold":
        _close_position(db, bot, coin, full_amount)
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {coin} position already "
             f"sold — the earlier sell landed on-chain.",
             bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "position already sold"})
        return
    if claim_state == "blocked":
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — an earlier sell may still be in flight.",
             bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "sell already in flight"})
        return
    # $GATE fuel gate (2026-11-23): a real sell costs 1 package
    # credit. No fuel means no trade — the bot is turned off by
    # the gate until the user refills. Paper sells never reach
    # this branch. A sweep sell (sweep_id set) skips this gate:
    # the sweep IS the authorized spender of the last credit, and
    # it consumes exactly one credit for the whole pass afterwards
    # (see run_exit_all_sweep) — gating it here would block the
    # very exit the rule exists for.
    if sweep_id is None and not gate_fuel_mod.enforce_fuel_gate(
            db, bot["user_id"]):
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — out of $GATE fuel (0 trades left). Buy a "
             f"$GATE package to resume.", bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "out of fuel"})
        return
    # Last-credit reserve (James, 2026-11-24): when the user's fuel
    # is down to its last credit and real positions are open, that
    # credit is reserved to exit every position in one sweep — a
    # single sell here would eat it. The sweep is the only spender
    # of the last credit, so an ordinary sell at fuel == 1 is
    # refused and the position waits for the sweep.
    if sweep_id is None and gate_fuel_mod.exit_all_pending(
            db, bot["user_id"]):
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — last $GATE trade is reserved to exit all "
             f"open positions. Buy a $GATE package to keep trading.",
             bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "last trade reserved"})
        return
    # The claim is placed only once the fuel gate has passed: a
    # refused sell never holds a claim. An unconfirmed send leaves
    # the claim open — the next pass reconciles it against the
    # chain via the pending-trades signature.
    claim_id = _place_sell_claim(db, bot["user_id"], int(bot["id"]),
                                 coin, amount)
    result = _call_real_executor(real_executor, bot, bot["user_id"],
                                 coin, "sell", amount, price, db, cfg)
    if result.get("status") == "executed":
        _resolve_sell_claim(db, claim_id, "confirmed",
                            result.get("signature"))
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
        # $GATE fuel: one credit for this confirmed executed trade.
        # The trade_id links the fuel-use row to the ledger row, so
        # the count is recomputable from the ledger at any time.
        # A sweep sell consumes NOTHING here: the sweep consumes
        # exactly one credit for the whole pass afterwards
        # (run_exit_all_sweep). The trade is marked as a sweep
        # trade instead, so reconcile_fuel's backfill never counts
        # it as its own credit.
        trade_id = record_trade(
            db, bot["user_id"], bot["id"], coin, "sell", amount,
            exec_price if exec_price is not None else price,
            (exec_price * amount if exec_price is not None
             else amount * price),
            pnl_usd=pnl_usd,
            exit_reason=reason, mode="real", now=_now(),
            signature=result.get("signature"),
            sol_amount=sol_received)
        if sweep_id is not None:
            gate_fuel_mod.record_sweep_trade(
                db, bot["user_id"], sweep_id, trade_id)
        else:
            gate_fuel_mod.consume_fuel(db, bot["user_id"], trade_id,
                                       result.get("signature"))
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
                        "reason": reason, "trade_id": trade_id,
                        "signature": result.get("signature")})
    else:
        if result.get("reason") != "transaction not confirmed":
            # The sell was refused before anything was sent — the
            # claim is released. An unconfirmed send keeps its
            # claim: the next pass reconciles it against the chain.
            _resolve_sell_claim(db, claim_id, "released")
        why = result.get("reason", "real execution failed")
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': {reason} sell of {coin} "
             f"skipped — {why}.", bot["mode"])
        actions.append({"action": "skip", "coin": coin, "reason": why})


def run_exit_all_sweep(db: sqlite3.Connection, cfg: Config,
                       user_id: int, rpc_factory, real_executor,
                       feed=None, prices_data: dict | None = None,
                       trenches_data: dict | None = None,
                       price_fetcher=None) -> dict:
    """The last-credit exit-all sweep (James, 2026-11-24, revised —
    NO free exits): when a user's fuel reaches its last trade left,
    that last credit exits EVERY open real position in one pass.
    One credit closes all positions; buys can never consume the
    last credit (see the reserve checks in _buy_coin,
    _sell_position_claimed and routes.py).

    Runs only when exit_all_pending is true (exactly 1 credit left
    AND open real bot positions). Returns {"ran": False, ...} at
    once otherwise — a pass with nothing to sweep costs one fuel
    read and no trades. Called once per loop pass per evaluated
    user (see run_once), never inside a web request: a sweep is
    several on-chain sells and must not hang a page render.

    Each position sells through the normal _sell_position path
    (sell claims, reconciliation, ledger, stop bookkeeping) with
    reason "exit_all" and this sweep's id, so the sells are
    indistinguishable from any other exit except for fuel: sweep
    sells consume nothing per trade. A refused position is logged
    and the sweep CONTINUES with the rest — one stuck position
    must not block the others. If ZERO sells execute, the credit
    is NOT consumed (fuel stays 1) and the next loop pass retries.
    If at least one sell executes, exactly ONE credit is consumed
    for the whole sweep, keyed by the first executed sell's
    trade_id/signature. After the sweep, fuel is 0 and the
    existing gate turns all real silent bots off (paper untouched).

    Only bot-managed real positions are swept. Manual (hand-bought)
    holdings are not positions in this app — they are not sold by
    the sweep (open call for James: see the MANIFEST). Prices come
    from the live feed first, then the trenches prices for this
    pass (already repriced onto the chain), then one batched Helius
    RPC curve/pool read — a position with no live price is left
    alone this pass and retried next time, never sold blind."""
    if not gate_fuel_mod.exit_all_pending(db, user_id):
        return {"ran": False, "reason": "no exit-all pending"}
    sweep_id = f"exit_all:{user_id}:{time.time_ns()}"
    rows = db.execute(
        "SELECT bp.coin_id AS coin_id, bp.amount AS amount, b.* "
        "FROM bot_positions bp JOIN bots b ON b.id = bp.bot_id "
        "WHERE bp.user_id = ? AND bp.mode = 'real' AND bp.amount > 0 "
        "ORDER BY bp.id",
        (user_id,)).fetchall()
    if not rows:
        return {"ran": False, "reason": "no open real positions"}
    if price_fetcher is None:
        trenches_prices = ((trenches_data or {}).get("prices") or {})

        def price_fetcher(mints):
            out = {}
            for mint in mints:
                entry = trenches_prices.get(mint) or {}
                price = entry.get("price_usd")
                if price is not None:
                    out[mint] = {"price_usd": price}
            if len(out) < len(mints):
                try:
                    rest = _default_price_fetcher(
                        [m for m in mints if m not in out],
                        feed=feed, cfg=cfg, rpc_factory=rpc_factory)
                except Exception:
                    rest = {}
                for mint, entry in (rest or {}).items():
                    price = (entry or {}).get("price_usd")
                    if price is not None:
                        out[mint] = {"price_usd": price}
            return out

    sold: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        coin = row["coin_id"]
        bot = dict(row)
        price = _held_price(price_fetcher, coin, feed, cfg, rpc_factory)
        if price is None:
            _log(db, user_id, "silent_skip",
                 f"Exit-all sweep: {coin} has no live price — position "
                 f"left alone this pass, will retry.", "real")
            skipped.append({"coin": coin, "reason": "no live price"})
            continue
        sub_actions: list = []
        _sell_position(db, cfg, bot, coin, price, "exit_all",
                       rpc_factory, real_executor, sub_actions,
                       feed=feed, prices_data=prices_data,
                       sweep_id=sweep_id)
        for action in sub_actions:
            if action.get("action") == "sell":
                sold.append(action)
            elif action.get("action") == "skip":
                skipped.append({"coin": coin,
                                "reason": action.get("reason", "skipped")})
    if not sold:
        _log(db, user_id, "gate_fuel_exit_all",
             f"Exit-all sweep {sweep_id}: no position sold "
             f"({len(skipped)} skipped) — last trade kept, will retry "
             f"next scan.", "real")
        return {"ran": True, "sweep_id": sweep_id, "sold": [],
                "skipped": skipped, "consumed": False,
                "trades_remaining": 1}
    # Exactly one credit for the whole sweep, keyed by the first
    # executed sell. Re-check fuel first: the sells above must not
    # have been counted already (a concurrent consume, or a
    # backfill that ran mid-sweep) — consuming twice would push
    # the ledger below zero.
    fuel_now = gate_fuel_mod.get_fuel(db, user_id)["trades_remaining"]
    consumed = False
    if fuel_now == 1:
        first = sold[0]
        consumed = gate_fuel_mod.consume_fuel(
            db, user_id, first.get("trade_id"), first.get("signature"))
    else:
        _log(db, user_id, "gate_fuel_exit_all",
             f"Exit-all sweep {sweep_id}: fuel moved during the sweep "
             f"(now {fuel_now}) — sweep credit not consumed again.",
             "real")
    remaining = gate_fuel_mod.get_fuel(db, user_id)["trades_remaining"]
    _log(db, user_id, "gate_fuel_exit_all",
         f"Exit-all sweep {sweep_id}: sold {len(sold)} position(s) "
         f"({', '.join(a['coin'] for a in sold)}) for 1 trade credit; "
         f"{len(skipped)} skipped; fuel now {remaining}.",
         "real")
    return {"ran": True, "sweep_id": sweep_id, "sold": sold,
            "skipped": skipped, "consumed": consumed,
            "trades_remaining": remaining}


def _buy_coin(db: sqlite3.Connection, cfg: Config, bot, coin: str,
              price: float, prices_usd: dict, rpc_factory,
              real_executor, actions: list,
              signals: dict | None = None,
              feed=None, prices_data: dict | None = None,
              kind: str | None = None,
              decision_price_as_of: float | None = None,
              pit_settings: dict | None = None) -> None:
    """Buy `coin` for a bot within its limits, or skip with a reason.

    `signals` is the buy-time signal snapshot from the feed entry
    (m5 gain %, 5-minute volume, buys/sells, regime) — recorded on
    the paper trade so the losing-buy analysis can show how the coin
    got bought. None for v1 bots and manual buys. `price` is the live
    mark price; a real position's cost basis is the EXECUTED spend
    when the executor reports it (see below), never the mark.
    `kind` is the bot's rule kind: only gnat and pit size in fixed
    SOL — every other kind buys a % of the bankroll. `pit_settings`
    is the bot's versioned 'pit' rules block (the source of the
    0.05 SOL fixed size); the module constant is only the fallback
    when the caller did not pass it.
    `decision_price_as_of` is when the decision price was fetched
    (epoch seconds, the scan time): the real executor refuses a
    buy on a price older than the freshness window. None when the
    caller cannot say — the freshness check is then skipped and
    recorded, never treated as fresh.
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
        elif kind == "pit":
            # Fixed 0.05 SOL per trade (25% of the 0.20 SOL bankroll
            # cap — James's spec), converted at the live SOL price.
            # The size comes from the bot's versioned rules; the
            # module constant is only the fallback when the caller
            # did not pass them. No live SOL price → fall back to
            # the bot row's buy-size % of the bankroll, and say so
            # on the buy line — the fallback is never silent.
            pit_size = float((pit_settings or {}).get(
                "buy_size_sol", PIT_BUY_SIZE_SOL))
            sol_price = _live_sol_price_usd(feed, prices_data)
            if sol_price is not None and sol_price > 0:
                buy_usd = pit_size * sol_price
                size_note = f" ({pit_size:g} SOL size)"
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
    if kind == "pit":
        # Fixed 0.05 SOL per trade from the bot's versioned rules
        # (25% of the 0.20 SOL bankroll cap) — real sizing uses the
        # cap, never the whole wallet balance. A wallet under the
        # fixed size buys nothing, instead of shrinking the trade
        # to whatever is left.
        amount_sol = float((pit_settings or {}).get(
            "buy_size_sol", PIT_BUY_SIZE_SOL))
        if amount_sol > balance:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': buy of {coin} skipped "
                 f"— insufficient real balance ({balance:.4f} SOL — "
                 f"a Pit trade needs {amount_sol:.4f} SOL).",
                 bot["mode"])
            actions.append({"action": "skip", "coin": coin,
                            "reason": "insufficient real balance"})
            return
    else:
        amount_sol = balance * float(bot["buy_size_pct"]) / 100.0
    if amount_sol <= 0:
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"insufficient real balance ({balance:.4f} SOL).", bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "insufficient real balance"})
        return
    # $GATE fuel gate (2026-11-23): a real buy costs 1 package
    # credit. No fuel means no trade — the bot is turned off by
    # the gate until the user refills. Paper buys never reach
    # this branch.
    # (The wallet + client above were read for the SOL balance
    # check; the gate needs neither.)
    if not gate_fuel_mod.enforce_fuel_gate(db, bot["user_id"]):
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"out of $GATE fuel (0 trades left). Buy a $GATE "
             f"package to resume.", bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "out of fuel"})
        return
    # Last-credit reserve (James, 2026-11-24): a buy can NEVER
    # consume the last fuel credit while real positions are open —
    # that credit is reserved to exit every position in one sweep.
    # The buy waits until the user refills; the sweep runs on the
    # next loop pass.
    if gate_fuel_mod.exit_all_pending(db, bot["user_id"]):
        _log(db, bot["user_id"], "silent_skip",
             f"Silent bot '{bot['name']}': buy of {coin} skipped — "
             f"last $GATE trade is reserved to exit all open "
             f"positions. Buy a $GATE package to keep trading.",
             bot["mode"])
        actions.append({"action": "skip", "coin": coin,
                        "reason": "last trade reserved"})
        return
    # Price-gate context for the real executor: the decision price
    # is the scan price fetched at decision_price_as_of (the loop
    # passes the scan time); the SOL price is the feed's live SOL
    # tick — the one SOL number. There is no second SOL source to
    # cross-check against any more.
    sol_price = _live_sol_price_usd(feed, prices_data)
    result = _call_real_executor(
        real_executor, bot, bot["user_id"], coin, "buy", amount_sol,
        price, db, cfg, decision_price_as_of=decision_price_as_of,
        sol_price_usd=sol_price)
    if result.get("status") == "executed":
        coin_amount = float(result.get("coin_amount") or 0)
        trade_id = None
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
            trade_id = record_trade(db, bot["user_id"], bot["id"],
                                    coin, "buy",
                                    coin_amount, buy_price,
                                    coin_amount * buy_price,
                                    mode="real", now=_now(),
                                    signature=result.get("signature"),
                                    sol_amount=sol_spent)
        # $GATE fuel: one credit for this confirmed executed trade.
        # The trade_id links the fuel-use row to the ledger row when
        # one was written; a fill with no ledger row (zero tokens
        # received) still counts by its signature — the transaction
        # executed on-chain, so the credit is owed either way.
        gate_fuel_mod.consume_fuel(db, bot["user_id"], trade_id,
                                   result.get("signature"))
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
      * 5-minute change (trenches/scanner only): the loop's own
        recorded price marks, 5 minutes back, at or below
        -drop_5m_pct.
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
    if kind in ("trenches", "scanner", "gnat", "pit", "middy") and price_map:
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
                        feed=None,
                        decision_price_as_of=None) -> list:
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
    if kind in ("trenches", "scanner", "gnat", "pit", "middy"):
        if not trenches_data or trenches_data.get("error"):
            why = (trenches_data or {}).get("error") or "feed unavailable"
            feed_name = ("scanner" if kind == "scanner"
                         else ("gnat" if kind == "gnat"
                               else ("pit" if kind == "pit"
                                     else ("middy" if kind == "middy"
                                           else "trenches"))))
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
        # from the single source (see _mint_price_map).
        price_map = _mint_price_map(json.loads(bot["coins"]), db,
                                    bot["id"], now_ts, feed, cfg,
                                    rpc_factory)

    def _price_for(coin: str):
        if kind in ("trenches", "scanner", "gnat", "pit", "middy"):
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

    # Momentum from the loop's own price marks (single-price source,
    # 2026-11-25): the scan's change figures were nulled at the loop
    # boundary, so every 5-minute / 1-hour change below is computed
    # here from marks this bot recorded itself. Record this scan's
    # marks first — candidates and held coins alike — then read the
    # changes off them. A coin with no mark at least as old as the
    # window has no change yet: its gates fail closed, never on a
    # second source's number. The documented consequence: a fresh
    # coin cannot show a 5-minute gain until the loop has watched
    # it for ~5 minutes, and a 6-hour / 24-hour buy metric has no
    # marks to read at all, so it never fires.
    if kind in ("trenches", "scanner", "gnat", "pit", "middy"):
        _candidates = list(
            (trenches_data or {}).get("candidates") or [])
        _held_coins = [coin for coin in _positions(db, bot["id"])]
        _mark_entries = [c for c in _candidates
                         if (c or {}).get("price_usd") is not None]
        for _coin in _held_coins:
            _p = _price_for(_coin)
            if _p is not None:
                _mark_entries.append({"mint": _coin, "price_usd": _p})
        if _mark_entries:
            try:
                record_marks(db, bot["id"], _mark_entries, _now())
            except Exception:
                pass
        for _entry in _candidates:
            _mint = (_entry or {}).get("mint")
            _p = (_entry or {}).get("price_usd")
            if not _mint or _p is None:
                continue
            try:
                _entry["change_5m_pct"] = gain_window_pct(
                    db, bot["id"], _mint, _p, now_ts, 300)
                _entry["change_1h_pct"] = gain_window_pct(
                    db, bot["id"], _mint, _p, now_ts, 3600)
            except Exception:
                _entry["change_5m_pct"] = None
                _entry["change_1h_pct"] = None
        for _coin in _held_coins:
            _entry = price_map.get(_coin)
            _p = _price_for(_coin)
            if _entry is None or _p is None:
                continue
            try:
                _entry["change_5m_pct"] = gain_window_pct(
                    db, bot["id"], _coin, _p, now_ts, 300)
            except Exception:
                _entry["change_5m_pct"] = None

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
        if kind in ("scanner", "trenches", "gnat", "pit", "middy") and top10:
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
        # or flat trailing. Pit runs its own too: the 4.9% stop in
        # any time period, and the breakeven stop armed at +6% —
        # both from the versioned 'pit' block, never the bot row's
        # stop loss.
        if kind == "gnat":
            hard = _gnat_hard_exit_reason(avg_price, peak, price)
        elif kind == "pit":
            pit_settings = rules["pit"]
            hard = _pit_hard_exit_reason(
                avg_price, peak, price,
                float(pit_settings["stop_trigger_pct"]),
                float(pit_settings["breakeven_trigger_pct"]))
        elif kind == "middy":
            hard = _middy_hard_exit_reason(avg_price, peak, price)
        else:
            hard = _hard_exit_reason(stop_loss_pct, trailing_pct, kind,
                                     avg_price, peak, price)
        if hard:
            _sell_position(db, cfg, bot, coin, price, hard,
                           rpc_factory, real_executor, actions,
                           feed=feed, prices_data=prices_data)
            continue
        # Pit pullback (pit only): a 2% or worse decline in the
        # 5-minute bucket sells everything — before the take-profit
        # rung, so a coin at +11% that dips 2% sells at +9% and
        # never reaches +12%. Debounced: two consecutive ticks.
        if kind == "pit":
            entry = (price_map or {}).get(coin) or {}
            if _pit_debounced_exit(db, bot["id"], coin, "pullback",
                                   _pit_pullback_fires(entry,
                                                       rules["pit"])):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "pullback: 5-minute change -2% or worse — the "
                    "top of the list is dead",
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
        # Middy take profit + slow coins (middy only): the
        # momentum-gated sell-all and the 120-second flat exit.
        # They run after the ladder so the +1000% backstop rung —
        # which only fires on a 10x — still pays out first on the
        # one path where it can fire. The position may have been
        # closed by the rung, so it is re-read before either fires.
        if kind == "middy":
            if _positions(db, bot["id"]).get(coin) is None:
                continue
            entry = (price_map or {}).get(coin) or {}
            tp_reason = _middy_take_profit_reason(
                entry, avg_price, peak, price)
            if tp_reason:
                _sell_position(db, cfg, bot, coin, price, tp_reason,
                               rpc_factory, real_executor, actions,
                               feed=feed, prices_data=prices_data)
                continue
            opened_at = (pos["opened_at"]
                         if "opened_at" in pos.keys() else None)
            opened_ts = (_parse_scan_time(opened_at)
                         if opened_at else None)
            slow_reason = _middy_slow_reason(opened_ts, entry, now_ts)
            if slow_reason:
                _sell_position(db, cfg, bot, coin, price, slow_reason,
                               rpc_factory, real_executor, actions,
                               feed=feed, prices_data=prices_data)
                continue
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
        # Pit flat-dead + time cap (pit only): a coin that moved
        # less than +/-0.5% in 5 minutes is dead (debounced, like
        # the pullback); a trade that has not hit +12% or the stop
        # within 10 minutes closes at market. They run after the
        # ladder so a rung that fired this pass still pays out
        # first. The position may have been closed by the rung
        # (Pit sells 100% at +12%), so it is re-read before either
        # fires.
        if kind == "pit":
            if _positions(db, bot["id"]).get(coin) is None:
                continue
            entry = (price_map or {}).get(coin) or {}
            if _pit_debounced_exit(db, bot["id"], coin, "flat",
                                   _pit_flat_fires(entry, rules["pit"])):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "flat: 5-minute change within +/-0.5% — no "
                    "movement, dead coin",
                    rpc_factory, real_executor, actions,
                    feed=feed, prices_data=prices_data)
                continue
            opened_at = (pos["opened_at"]
                         if "opened_at" in pos.keys() else None)
            opened_ts = (_parse_scan_time(opened_at)
                         if opened_at else None)
            if (opened_ts is not None
                    and now_ts - opened_ts
                    > float(rules["pit"]["max_hold_seconds"])):
                _sell_position(
                    db, cfg, bot, coin, price,
                    "time cap: 10 minutes without +12% or the stop",
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
    # Pit circuit breaker (pit only): the daily-loss breaker from
    # James's spec — down $5 CAD (~0.025 SOL) in a day stops new
    # buys until tomorrow. Real mode only: a paper loss is play
    # money, and the paper test measures the rule, not the breaker.
    if kind == "pit":
        halted, halt_reason = _pit_buy_halt(
            db, bot, now_ts, feed, prices_data,
            float(rules["pit"]["daily_loss_sol"]))
        if halted:
            if not _breaker_logged(db, bot["id"],
                                   "pit daily-loss breaker"):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': {halt_reason}.",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": halt_reason})
            return actions
    # Middy daily-loss breaker (middy only): down 20% of the
    # starting balance (day-start capital) in realized losses
    # stops new buys until tomorrow, paper and real alike — the
    # paper test measures the rule, and the $20 of real money
    # stays put until paper proves profit. Exits still run.
    if kind == "middy":
        halted, halt_reason = _middy_buy_halt(
            db, bot, now_ts, feed, prices_data)
        if halted:
            if not _breaker_logged(db, bot["id"],
                                   "middy daily-loss breaker"):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': {halt_reason}.",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": halt_reason})
            return actions
    # Middy buy pace (middy only): a few coins an hour, never
    # forced — when the rolling 60-minute window already holds
    # the maximum, no new buys until the window slides. The halt
    # is logged at most once an hour, not once per scan. Exits
    # still run.
    if kind == "middy":
        halted, halt_reason = _middy_pace_halt(db, bot, now_ts)
        if halted:
            hour_ago = datetime.fromtimestamp(
                now_ts - 3600.0, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            if not _breaker_logged(db, bot["id"], "middy buy pace",
                                   since=hour_ago):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': {halt_reason}.",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": halt_reason})
            return actions
        # 1-an-hour minimum (James, 2026-11-24 ~12:43): a TARGET,
        # never a quota. When the bot has open capacity and no buy
        # in the last hour, the miss is LOGGED (at most once an
        # hour), never forced — NO BLIND TRADES is still the hard
        # rule, so a buy is never made to meet the minimum.
        if _middy_buys_last_hour(db, bot["id"], now_ts) < \
                MIDDY_MIN_BUYS_PER_HOUR and len(
                    _positions(db, bot["id"])) < int(
                        bot["max_open_trades"]):
            hour_ago = datetime.fromtimestamp(
                now_ts - 3600.0, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            if not _breaker_logged(db, bot["id"],
                                   "middy minimum missed",
                                   since=hour_ago):
                _log(db, bot["user_id"], "silent_breaker",
                     f"Silent bot '{bot['name']}': middy minimum "
                     f"missed — no buy in the last hour (target "
                     f"{MIDDY_MIN_BUYS_PER_HOUR}/hour, max "
                     f"{MIDDY_MAX_BUYS_PER_HOUR}/hour); no coin "
                     f"passed the safety gates and entry "
                     f"confirmation, so nothing was forced.",
                     bot["mode"])
    # Pit fuel (pit real mode only, 2026-11-23): a Pit bot trades
    # real only while the owner holds package fuel credits — the
    # package gate is the fuel check now (the old enrollment
    # opt-in is gone). The position exits above already ran; this
    # gates only new buys. Logged once per day, not once per scan.
    if kind == "pit" and bot["mode"] == "real":
        if not gate_fuel_mod.has_fuel(db, bot["user_id"]):
            reason = ("Pit real trades need $GATE fuel — buy a $GATE "
                      "package to keep trading. No buy this scan.")
            if not _breaker_logged(db, bot["id"],
                                   "pit fuel enrollment"):
                _log(db, bot["user_id"], "silent_skip",
                     f"Silent bot '{bot['name']}': {reason}",
                     bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": reason})
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
    elif kind == "pit":
        # Pit ranks by the 5-minute gain from the loop's own price
        # marks (recorded + filled in above). A coin gaining 6.3%
        # or more in 5 minutes goes to the top of the watch list;
        # a coin above the mcap cap is logged as a miss for the
        # shared brain instead of being bought.
        pit_settings = rules["pit"]
        threshold = float(rules["buy"]["threshold_pct"])
        held = set(_positions(db, bot["id"]))
        movers = []
        for entry in trenches_data["candidates"]:
            change = entry.get("change_5m_pct")
            try:
                change = float(change) if change is not None else None
            except (TypeError, ValueError):
                change = None
            if change is None or change < threshold:
                continue
            movers.append((change, entry))
        movers.sort(key=lambda item: item[0], reverse=True)
        # Missed coins: above the mcap cap, so the bot could not
        # buy them — the shared brain reviews them after the
        # evaluations and raises the cap when 2 have been seen
        # since the active version was published.
        max_cap = float(rules["buy_filter"]["max_market_cap_usd"])
        for change, entry in movers:
            mcap = entry.get("market_cap_usd")
            try:
                mcap = float(mcap) if mcap is not None else None
            except (TypeError, ValueError):
                mcap = None
            if mcap is not None and max_cap > 0 and mcap > max_cap:
                pit_mod.record_pit_missed_coin(
                    db, entry["mint"], mcap, change)
        # Watch list memory: the top 5 movers, with scrap votes for
        # coins that show resistance or vanish from the feed.
        pit_mod.update_pit_watchlist(
            db, bot["id"], [e for _change, e in movers], price_map,
            pit_settings, _now())
        # Slow-time gate: only trade when the market is busy. The
        # top 5 movers must average 10 or more buys in 5 minutes —
        # a quiet scan is a skip, not a smaller trade. (Txn count
        # only: the feed has no chain-congestion or rug-time data,
        # so the slow-market and rug-time wording is dropped.) When
        # no top-5 entry carries txn counts at all, the gate cannot
        # be measured and is skipped — that is the devnet paper
        # path, whose test mints have no txn data.
        top5 = movers[:5]
        top5_buys = []
        for _change, entry in top5:
            buys = entry.get("txns_m5_buys")
            if buys is not None:
                try:
                    top5_buys.append(int(buys))
                except (TypeError, ValueError):
                    pass
        slow = (bool(top5_buys)
                and sum(top5_buys) / len(top5_buys)
                < float(pit_settings["min_top5_avg_buys_5m"]))
        if slow:
            _log(db, bot["user_id"], "silent_skip",
                 f"Silent bot '{bot['name']}': market too slow — top 5 "
                 f"movers average "
                 f"{sum(top5_buys) / len(top5_buys):.1f} buys in 5 "
                 f"minutes (needs "
                 f"{float(pit_settings['min_top5_avg_buys_5m']):.0f}). "
                 f"No buy this scan.", bot["mode"])
            actions.append({"action": "skip", "coin": "",
                            "reason": "market too slow — top 5 movers "
                                      "average under 10 buys in 5 minutes"})
        else:
            for change, entry in movers:
                if entry["mint"] in held:
                    continue
                ok, why = passes_buy_filter(
                    entry, rules.get("buy_filter"), change,
                    devnet_watchlist_active=(cfg.network == "devnet"))
                if not ok:
                    filtered.append((change, entry, why))
                    continue
                # Volume confirmation: the 5-minute volume must be
                # rising — the trending check from detect_regime,
                # volume surge 2x plus buys outnumbering sells,
                # with NO 1-hour-gain requirement (a fresh coin
                # has no 1-hour trend yet). Devnet watchlist test
                # mints have no volume data, so the check is
                # skipped for them — the same carve-out as the buy
                # filter and the entry gates.
                if not entry.get("devnet_watchlist") and \
                        detect_regime(entry, {"volume_surge_x": 2.0,
                                              "require_buy_dominance": True,
                                              "require_h1_gain": False}) != "trending":
                    filtered.append((change, entry,
                                     "volume confirmation: not trending — "
                                     "5-minute volume below 2x the hourly "
                                     "pace or buys do not outnumber sells"))
                    continue
                # Pit entry gates: buys at least 2x sells, curve
                # band 15-65% read on-chain.
                ok, why = _pit_entry_gates(
                    db, cfg, entry, rpc_factory, pit_settings)
                if not ok:
                    filtered.append((change, entry, why))
                    continue
                # 2-scan confirmation: the signal must still be
                # passing on a later scan. A one-scan spike never
                # buys.
                if not _signal_confirmed(db, bot["id"], entry["mint"],
                                         change, now_ts, interval):
                    filtered.append(
                        (change, entry,
                         "signal not yet confirmed — first seen this scan"))
                    continue
                ranked.append((change, entry))
            ranked.sort(key=lambda item: item[0], reverse=True)
    elif kind == "middy":
        # Middy ranks by the 5-minute gain from the loop's own
        # price marks (recorded + filled in above — the scan's
        # change figures were nulled at the loop boundary). A coin
        # gaining at least 1% in 5 minutes is a candidate; the buy
        # filter, the four-part momentum gate, the dominance gate
        # and the scam gate then decide, and the signal must still
        # be passing on a second scan — NO BLIND TRADES.
        threshold = float(rules["buy"]["threshold_pct"])
        held = set(_positions(db, bot["id"]))
        for entry in trenches_data["candidates"]:
            change = entry.get("change_5m_pct")
            try:
                change = float(change) if change is not None else None
            except (TypeError, ValueError):
                change = None
            if change is None or change < threshold:
                continue
            if entry["mint"] in held:
                continue
            ok, why = passes_buy_filter(
                entry, rules.get("buy_filter"), change,
                devnet_watchlist_active=(cfg.network == "devnet"))
            if not ok:
                filtered.append((change, entry, why))
                continue
            # Momentum-only entry: the four-part signal gate. A
            # coin gaining on one short window but flat/falling on
            # the others, or spiking without volume, is not a
            # signal — it is never bought.
            ok, why = passes_momentum_gate(entry, db, bot["id"],
                                           now_ts)
            if not ok:
                filtered.append((change, entry, f"momentum gate: {why}"))
                continue
            # Middy entry gate: buys just beat sells in the
            # 5-minute bucket.
            ok, why = _middy_entry_gates(
                db, cfg, entry, rpc_factory, rpc_cache)
            if not ok:
                filtered.append((change, entry, why))
                continue
            # Never rebuy: a coin this bot already bought once is
            # never bought again — even if the last trade
            # profited. Once traded, that coin is done; move on.
            if _middy_already_bought(db, bot["id"], entry["mint"]):
                filtered.append(
                    (change, entry,
                     "never rebuy: this bot already bought this "
                     "coin once — done, move on"))
                continue
            # 2-scan confirmation: the signal must still be passing
            # on a later scan. A one-scan spike never buys.
            if not _signal_confirmed(db, bot["id"], entry["mint"],
                                     change, now_ts, interval):
                filtered.append(
                    (change, entry,
                     "signal not yet confirmed — first seen this scan"))
                continue
            ranked.append((change, entry))
        ranked.sort(key=lambda item: item[0], reverse=True)
    elif kind == "trenches":
        threshold = float(rules["buy"]["threshold_pct"])
        change_key = _BUY_METRIC_TO_CHANGE_KEY[rules["buy"]["metric"]]
        held = set(_positions(db, bot["id"]))
        # The change figure is the loop's own marks (recorded +
        # filled in above) for every candidate, devnet watchlist
        # included — the old devnet-only marks path is subsumed
        # by the general fill. A mint with no mark at least as old
        # as the window has no gain yet and is not bought.
        for entry in trenches_data["candidates"]:
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
    if kind in ("trenches", "scanner", "gnat", "pit", "middy"):
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
            if kind in ("scanner", "gnat", "pit", "middy"):
                # Re-buy cooldown: a coin closed at a loss is not
                # bought again for 30 minutes (5 minutes for Middy).
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
            if kind in ("scanner", "gnat", "pit", "middy"):
                # The signal features for the signal-outcome log:
                # the triggering gain, the 5-minute volume ratio
                # vs the hourly pace, the buyer ratio, and the
                # 1-hour trend. Pit's and Middy's window is 5 minutes;
                # scanner and Gnat use 3 minutes.
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
                signals["gain_window"] = ("5m" if kind in ("pit", "middy")
                                          else "3m")
                signals["volume_ratio"] = volume_ratio
                signals["buyer_ratio"] = buyer_ratio
                signals["trend_1h_pct"] = entry.get("change_1h_pct")
            _buy_coin(db, cfg, bot, entry["mint"], entry["price_usd"],
                      _bankroll_prices(),
                      rpc_factory, real_executor, actions,
                      signals=signals, feed=feed, kind=kind,
                      prices_data=prices_data,
                      decision_price_as_of=decision_price_as_of,
                      pit_settings=(rules["pit"]
                                    if kind == "pit" else None))
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
                      feed=feed, prices_data=prices_data,
                      decision_price_as_of=decision_price_as_of)
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
                  feed=None, decision_price_as_of=None) -> list:
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
                                   feed=feed,
                                   decision_price_as_of=(
                                       decision_price_as_of))
    actions: list = []
    coins = json.loads(bot["coins"])
    # The price list is SOL only; a signals bot's coins are pump.fun
    # mints, priced from the single source (see _mint_price_map).
    now_ts = datetime.now(timezone.utc).timestamp()
    prices = dict(prices_data["prices"])
    prices.update(_mint_price_map(coins, db, bot["id"], now_ts, feed,
                                  cfg, rpc_factory))
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
                  prices_data=prices_data,
                  decision_price_as_of=decision_price_as_of)
    return actions


def _held_price(price_fetcher, coin: str, feed=None, cfg=None,
                rpc_factory=None):
    """Current USD price for one held mint, or None when no source
    can serve it. The live feed first (fresh ticks only — now
    including devnet curve prices); a devnet watchlist mint with no
    feed tick is priced straight from its bonding curve; then the
    injected price fetcher when one was passed (a test seam — the
    production callers pass the chain-based default below), else
    one batched Helius RPC curve/pool read. The watcher only needs
    a price when a top-10 trigger has already fired — a missing
    price means the sell waits for the next check, never a guess."""
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
    if price_fetcher is not None:
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
    if cfg is None:
        return None
    try:
        prices = _chain_price_usd([coin], feed, cfg, rpc_factory)
    except Exception:
        return None
    price = (prices or {}).get(coin)
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        return None
    return price if price and price > 0 else None


def _default_price_fetcher(mints, feed=None, cfg=None,
                           rpc_factory=None):
    """Default price fetcher for the top-10 watch and the exit-all
    sweep: {mint: {"price_usd": price}} from the single source
    (feed tick first, else one batched Helius RPC curve/pool read).
    A mint no source can price is absent — never a second-source
    number."""
    try:
        prices = _chain_price_usd(mints, feed, cfg, rpc_factory)
    except Exception:
        return {}
    return {mint: {"price_usd": price}
            for mint, price in (prices or {}).items()}


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
    price_fetcher = price_fetcher or (
        lambda mints: _default_price_fetcher(
            mints, feed=feed, cfg=cfg, rpc_factory=rpc_factory))
    actions: list = []
    now_ts = datetime.now(timezone.utc).timestamp()
    bots = db.execute("SELECT * FROM bots WHERE silent = 1").fetchall()
    for bot in bots:
        rules, error = load_rules(bot)
        if rules is None or error is not None:
            continue
        if rules["kind"] not in ("scanner", "trenches", "gnat", "pit",
                                 "middy"):
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
    first, then one batched Helius RPC read — never a second-source
    price."""
    rpc_factory = rpc_factory or _default_rpc_factory(cfg)
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
    (scanner, signals, gnat, pit and middy kinds — Trenches v2 owns trenches
    exits), check the hard exits (rug sell, stop / breakeven stop,
    trailing stop) at the tick price and sell at once when one
    fires. Pit also checks the pullback here: a tick 2% or more
    below the last loop mark closes the position at once — the
    loop's debounced 5-minute-bucket pullback is the careful
    backstop, this is the fast one. The loop is the backstop: if
    this thread misses a tick, the next loop pass (10s for Pit)
    still fires the same exit through the same shared helper.
    Returns the actions taken."""
    actions: list = []
    with _TICK_DIRTY_LOCK:
        mints = sorted(_TICK_DIRTY_MINTS)
        _TICK_DIRTY_MINTS.clear()
    if not mints:
        return actions
    now_ts = datetime.now(timezone.utc).timestamp()
    rpc_factory = rpc_factory or _default_rpc_factory(cfg)
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
            if rules["kind"] not in ("scanner", "signals", "gnat", "pit",
                                     "middy"):
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
            elif rules["kind"] == "pit":
                pit_settings = rules["pit"]
                hard = _pit_hard_exit_reason(
                    avg_price, peak, price,
                    float(pit_settings["stop_trigger_pct"]),
                    float(pit_settings["breakeven_trigger_pct"]))
            elif rules["kind"] == "middy":
                hard = _middy_hard_exit_reason(avg_price, peak, price)
            else:
                hard = _hard_exit_reason(float(bot["stop_loss_pct"]),
                                         float(rules["trailing_stop_pct"]),
                                         rules["kind"], avg_price, peak,
                                         price)
            if hard:
                _sell_position(db, cfg, bot, mint, price, hard,
                               rpc_factory, real_executor, actions,
                               feed=feed)
                continue
            # Pit tick-level pullback: a tick 2% or more below the
            # last loop mark (at most 5 minutes old) closes the
            # position at once. The loop's debounced 5-minute-bucket
            # pullback is the careful backstop — this is the fast
            # one, and a single noisy tick is caught by the bucket
            # check if this thread does not fire.
            if rules["kind"] == "pit":
                last = (pos["last_price"]
                        if "last_price" in pos.keys() else None)
                last_at = (pos["last_price_at"]
                           if "last_price_at" in pos.keys() else None)
                last_ts = (_parse_scan_time(last_at)
                           if last_at else None)
                if (last is not None and float(last) > 0
                        and last_ts is not None
                        and now_ts - last_ts <= 300
                        and price <= float(last) * (1.0 - float(
                            rules["pit"]["pullback_pct"]) / 100.0)):
                    _sell_position(
                        db, cfg, bot, mint, price,
                        "pullback: 2% or more below the last loop "
                        "mark — the top of the list is dead",
                        rpc_factory, real_executor, actions, feed=feed)
                    continue
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
             trenches_fetcher=None, feed=None,
             sweep_price_fetcher=None) -> list:
    """One loop iteration over every silent-enabled bot.

    Returns a list of per-bot outcome dicts. A failure for one bot is
    recorded and never stops the others. The trenches scan
    (DexScreener — discovery + activity metadata only) is fetched at
    most once per iteration, only when at least one silent bot has a
    valid trenches or scanner rule set, and is repriced onto the
    single source before any evaluation sees it: every price becomes
    the chain price (feed tick first, else a batched Helius RPC
    curve/pool read) and every DexScreener change figure is nulled,
    so momentum comes from the loop's own recorded price marks.
    When a live feed is passed, both price sources are sanitized
    against it first: a price the feed rejects as an absurd
    single-tick jump never reaches an evaluation or a snapshot —
    the coin reads as unpriced for this iteration instead. With no
    feed, prices pass through exactly as before.
    """
    price_fetcher = price_fetcher or get_prices
    rpc_factory = rpc_factory or _default_rpc_factory(cfg)
    real_executor = real_executor or default_real_executor
    trenches_fetcher = trenches_fetcher or fetch_trenches_scan
    prices_data = price_fetcher()
    # When the decision prices were fetched (epoch seconds): the
    # real executor refuses a buy on a price older than the
    # freshness window, so the scan time travels with the
    # evaluation. The price list is fetched first; the trenches
    # scan (when needed) refreshes it.
    decision_price_as_of = time.time()
    bots = db.execute(
        "SELECT * FROM bots WHERE silent = 1 ORDER BY id").fetchall()
    trenches_data = None
    needs_trenches = False
    for bot in bots:
        rules, err = load_rules(bot)
        if rules is not None and rules["kind"] in ("trenches", "scanner",
                                                  "gnat", "pit", "middy"):
            needs_trenches = True
            break
    if needs_trenches:
        held_mints = [r["coin_id"] for r in db.execute(
            "SELECT DISTINCT coin_id FROM bot_positions WHERE bot_id IN "
            "(SELECT id FROM bots WHERE silent = 1)").fetchall()]
        trenches_data = trenches_fetcher(mints=held_mints)
        decision_price_as_of = time.time()
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
        # Single-price source: reprice the scan onto the chain
        # before any evaluation sees it (see _reprice_trenches_data).
        # Enriched BEFORE the reprice so a devnet curve price is
        # re-verified against the chain like any other entry.
        trenches_data = _reprice_trenches_data(
            trenches_data, feed, cfg, rpc_factory)
    if feed is not None:
        # Bad-tick guard: an absurd price never becomes a buy/sell
        # signal or a snapshot value. The sanitizers return copies;
        # the fetched data is untouched.
        prices_data = feed.sanitize_prices_data(prices_data)
        if trenches_data is not None:
            trenches_data = feed.sanitize_trenches(trenches_data)
    # Pending-trade reconciliation (mainnet fix): re-check every
    # unconfirmed sent signature via getTransaction before this
    # pass settles anything. A failure here never breaks the loop.
    try:
        _reconcile_rpc = rpc_factory(cfg.rpc_url)
        reconcile_pending_trades(
            db, _reconcile_rpc, cfg.pending_trade_horizon_seconds)
    except Exception:
        pass
    outcomes = []
    for bot in bots:
        try:
            # Pit shared brain (2026-11-23): a Pit bot running an
            # older version adopts the active one before it
            # evaluates — rule changes reach every Pit user in real
            # time, on the next loop pass. Adoption never fails the
            # evaluation: a bot that cannot adopt trades on its
            # stored rules this pass.
            try:
                rules, _err = load_rules(bot)
                if rules is not None and rules.get("kind") == "pit":
                    bot = pit_mod.maybe_adopt_pit_version(db, bot) or bot
            except Exception:
                pass
            actions = _evaluate_bot(db, cfg, bot, prices_data, rpc_factory,
                                    real_executor,
                                    trenches_data=trenches_data, feed=feed,
                                    decision_price_as_of=(
                                        decision_price_as_of))
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
    # Last-credit exit-all sweep (James, 2026-11-24): when a user's
    # fuel reaches its last credit, that credit exits every open
    # real position in one pass. Checked once per loop pass for
    # each user evaluated above — the sweep runs in the loop, never
    # inside a web request, and never for paper. A user with
    # nothing to sweep costs one fuel read; a sweep that sold
    # nothing keeps the credit and retries next pass. Users with
    # no silent bot evaluated this pass are not checked (see the
    # MANIFEST open call) — their sweep waits until a bot runs.
    _swept_users: set = set()
    for _outcome in outcomes:
        _uid = _outcome["user_id"]
        if _uid in _swept_users:
            continue
        _swept_users.add(_uid)
        try:
            run_exit_all_sweep(db, cfg, _uid, rpc_factory,
                               real_executor, feed=feed,
                               prices_data=prices_data,
                               trenches_data=trenches_data,
                               price_fetcher=sweep_price_fetcher)
        except Exception as _exc:  # a failed sweep never stops the loop
            _log(db, _uid, "silent_skip",
                 f"Exit-all sweep failed — {str(_exc)[:120]}. No trade "
                 f"was made.", "real")
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
    # Pit shared brain, missed-coin review (2026-11-23): after the
    # evaluations, count the missed coins since the active version
    # was published — 2 or more raises the mcap cap and publishes
    # a new version, which every Pit bot adopts on its next pass.
    # A review failure never breaks the loop.
    try:
        note = pit_mod.pit_review_missed_coins(
            db, (trenches_data or {}).get("prices"))
        if note:
            print(f"[silent-loop] {note}", flush=True)
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
    # Startup guardrail (audit #14): on mainnet with devnet mints
    # explicitly configured as candidates, refuse to start at all.
    validate_loop_startup(cfg)
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
    try:
        validate_loop_startup(cfg)
    except ValueError as exc:
        # Refuse to start the loop (not the app): loud log, no
        # thread, no trades. Fix the config and restart.
        print(f"[silent-loop] NOT STARTED: {exc}", flush=True)
        return None
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
