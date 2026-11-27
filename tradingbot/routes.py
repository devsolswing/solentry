"""HTTP routes for the Trading Bot app.

Security notes:
- Every route that touches user data requires login and scopes all
  queries by session user_id. Tests prove user A cannot read B's data.
- POST forms carry a per-session CSRF token.
- No route ever returns or logs a private key, password, session
  secret, encryption key, or token value. Bot API tokens are shown
  exactly once, on the creation response.
- Real-money movement only ever happens through the two-step send
  flow (create intent -> explicit confirm). Real TRADES are signed
  autonomously, but only by the account's dedicated signing bot, only
  on the configured network (devnet or mainnet), only for trades the rules engine allows, and only
  within the bot's limits (see executor.py). The signing bot never
  sends to an arbitrary destination.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (Flask, abort, flash, has_request_context, jsonify,
                   make_response, redirect, render_template, request,
                   session, url_for)

from webauthn import (generate_authentication_options,
                      generate_registration_options,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import (base64url_to_bytes, bytes_to_base64url,
                              options_to_json_dict)
from webauthn.helpers.structs import AuthenticatorTransport
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from . import assistant as assistant_mod
from . import builderbot as builderbot_mod
from .bot_names import (display_bot_name, preset_base_name,
                        suffixed_name)
from . import analysis as analysis_mod
from . import candles as candles_mod
from . import donations as donations_mod
from . import gate_fuel as gate_fuel_mod
from . import networks as networks_mod
from . import passkey as passkey_mod
from . import pnl as pnl_mod
from . import wallet_keys as wallet_keys_mod
from .page_numbers import page_list, page_number_for
from . import rate_limit as rate_limit_mod
from . import rent_reclaim as rent_reclaim_mod
from . import signing_bot as signing_bot_mod
from . import token_send as token_send_mod
from .charts import (CHART_RANGES, fetch_market_chart,
                     fetch_trade_flow, high_low_24h,
                     mint_candles_from_feed, search_coins)
from .coin_meta import ensure_coin_meta, stored_display
from .config import Config
from .executor import (execute_real_trade, fix_for_skip_reason,
                       is_curve_complete, send_and_confirm)
from .jupiter_executor import execute_jupiter_swap
from .user_exits import set_user_exit
from .paper import (PAPER_BUY_HAIRCUT_PCT, InsufficientCash,
                    InsufficientHolding, paper_buy as ledger_buy,
                    paper_sell as ledger_sell)
from .prices import clear_price_cache
from .builderbot import list_adjustments
from .home_holdings import (change_pct_from_points, enrich_row,
                            fetch_chain_token_balances, fmt_price_sig,
                            reconcile_holdings, sparkline_1h_change_pct,
                            sparkline_points_1h, sparkline_svg)
# NOTE: sparkline_svg_1h is NOT imported here any more — after E7
# the /api/sparklines endpoint computes points once and derives
# both the SVG and the % from them, so routes.py no longer calls
# the wrapper (it still lives in home_holdings.py, used there).
from .ledger import (bot_wl_summary, kind_wl_summary, recent_trades,
                     version_wl_summary)
from .pit import (current_pit_version, ensure_pit_version_1_0,
                  pit_changelog, pit_version_rules)
from .rules import (CLYDE_CATEGORY, CLYDE_PRESET_NAME,
                    CLYDE_STOP_LOSS_PCT, GNAT_BUY_SIZE_PCT, GNAT_CATEGORY,
                    GNAT_PRESET_NAME, GNAT_STOP_LOSS_PCT,
                    MIDDY_BUY_SIZE_PCT, MIDDY_CATEGORY, MIDDY_PRESET_NAME,
                    MIDDY_STOP_LOSS_PCT, PIT_BANKROLL_SOL, PIT_BUY_SIZE_PCT,
                    PIT_CATEGORY,
                    PIT_PRESET_NAME, PIT_PRESET_VERSION_INITIAL,
                    PIT_STOP_LOSS_PCT, RUG_SELL_DEFAULT, SCANNER_CATEGORY,
                    SCANNER_DISPLAY, SCANNER_PRESET_NAME,
                    TRENCHES_CATEGORY, TRENCHES_PRESET_NAME, clyde_ruleset,
                    gnat_ruleset, load_rules, middy_ruleset, pit_ruleset,
                    preset_bots,
                    scanner_ruleset, summarize_rules, trenches_ruleset,
                    validate_ruleset)
from .security import (decrypt_secret, encrypt_secret, hash_account_password,
                       hash_password, hash_token, new_csrf_token, new_token,
                       verify_account_password, verify_password,
                       verify_turnstile, wipe_secret)
from .wallet_keys import temporary_wallet_secret
from .signals import signal_for_coin
from .silent_loop import _open_position
from .solana import (SolanaRpcClient, base58_encode,
                     build_signed_transfer, generate_keypair,
                     is_valid_address, lamports_to_sol,
                     rpc_client_for_config, rpc_client_for_url,
                     sol_to_lamports)
from .streaming import (SOL_MAX_AGE_SECONDS,
                        chain_price_usd_for_mints,
                        sol_rate_for_conversion)
from .trenches import (fetch_mint_identities, fetch_trenches_scan)

log = logging.getLogger("tradingbot.routes")

MAX_LOGIN_FAILS = 5
LOCKOUT_MINUTES = 15
# Anti-bot / rate limits (see rate_limit.py — counters live in the
# DB, so a restart does not clear them). Generous enough that a real
# user never trips them; tight enough to stop a script hammering.
MAX_LOGIN_FAILS_PER_IP = 20          # failed logins, 15 min, per IP
LOGIN_FAIL_IP_WINDOW_SECONDS = 15 * 60
MAX_SIGNUPS_PER_IP = 10              # signups, 1 hour, per IP
SIGNUP_IP_WINDOW_SECONDS = 60 * 60
MAX_SENDS_PER_USER = 10              # send intents, 1 hour, per user
SEND_USER_WINDOW_SECONDS = 60 * 60
MAX_BOT_CREATES_PER_USER = 20        # bot creates, 1 hour, per user
BOT_CREATE_USER_WINDOW_SECONDS = 60 * 60
MAX_POST_BURST_PER_IP = 120          # POSTs to guarded endpoints, 1 min
POST_BURST_WINDOW_SECONDS = 60
# Anomaly detection: this many failed logins in 15 minutes from IPs
# the account has never successfully logged in from locks the
# account for 15 minutes and writes a suspicious-activity audit row.
ANOMALY_FAIL_THRESHOLD = 3
ANOMALY_WINDOW_SECONDS = 15 * 60
# Honeypot field name on login/signup forms. Real users never see
# it (hidden with CSS); bots that fill every field give themselves
# away. The name looks like an ordinary field on purpose.
HONEYPOT_FIELD = "website"


def _cfg(app: Flask) -> Config:
    return app.config["TRADINGBOT_CONFIG"]


def _db(app: Flask):
    return app.config["DB"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_user(app):
    uid = session.get("user_id")
    if not uid:
        return None
    row = _db(app).execute(
        "SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    return row


def _login_required(app):
    from functools import wraps

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _current_user(app):
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper
    return deco


def _csrf_check():
    token = request.form.get("csrf_token") or (
        request.json.get("csrf_token") if request.is_json else None)
    if not token or token != session.get("csrf_token"):
        abort(400, "bad csrf token")


def _new_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = new_csrf_token()
    return session["csrf_token"]


def _client_ip() -> str:
    """Client IP, honouring the X-Forwarded-For header Railway's
    proxy sets (first entry = the real client). 'unknown' when there
    is no request (silent loop) or no address at all."""
    if not has_request_context():
        return ""
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def _client_ua() -> str:
    """User-Agent string, truncated — the 'device' half of the audit
    trail. Empty outside a request."""
    if not has_request_context():
        return ""
    return (request.user_agent.string or "")[:200]


def _log(app, user_id: int, kind: str, detail: str,
         mode: str = "") -> None:
    """Activity row, tagged with the trading mode it belongs to
    ('paper' / 'real') so pages show each mode's activity separately.
    Account-level rows pass mode='' and show in both modes.

    Every row also carries the client IP and device string behind
    the action, where a request exists — this is the audit trail:
    sends, trades, key operations, and mode changes are all
    attributable. Rows the silent loop writes (no request) record
    empty ip/device."""
    _db(app).execute(
        "INSERT INTO activity (user_id, kind, detail, created_at, mode, "
        "ip, device) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, detail, _now(), mode, _client_ip(),
         _client_ua()))
    _db(app).commit()


def _honeypot_filled() -> bool:
    """True when the hidden honeypot field on a login/signup form
    was filled in — only a bot filling every field does that."""
    return bool((request.form.get(HONEYPOT_FIELD) or "").strip())


def _turnstile_ok(app) -> bool:
    """Cloudflare Turnstile check for login/signup.

    Enforced ONLY when both Turnstile keys are configured. When
    they are absent the app continues without the challenge (and
    logs a warning at startup) — never lock James out for missing
    keys. A failed verification is a plain refusal, same as a
    wrong password, so a bot learns nothing.
    """
    cfg = _cfg(app)
    if not cfg.turnstile_site_key or not cfg.turnstile_secret_key:
        return True
    token = request.form.get("cf-turnstile-response") or ""
    return verify_turnstile(token, cfg.turnstile_secret_key,
                            _client_ip() or None)


def _rotate_session(user_id: int) -> None:
    """Session rotation on login/signup: drop the old session id
    entirely (so a stolen pre-login session is useless) and start a
    fresh one, marked permanent so the 12-hour lifetime applies."""
    session.clear()
    session.permanent = True
    session["user_id"] = user_id


def _mark_mode_chosen(db, user_id: int) -> None:
    """Record in the database that the user answered the Demo / Real
    chooser since sign-in (users.mode_chosen_at). Called on
    choose_mode and on a HUD mode switch — either one is a mode
    choice, so the chooser must not come back while this sign-in
    lasts, even if the session flag is lost."""
    db.execute("UPDATE users SET mode_chosen_at = ? WHERE id = ?",
               (_now(), user_id))
    db.commit()


def _clear_mode_chosen(db, user_id: int) -> None:
    """Forget the chooser answer: called on every sign-in, so the
    chooser shows exactly once after sign-in, then never again
    until the next sign-in."""
    db.execute("UPDATE users SET mode_chosen_at = '' WHERE id = ?",
               (user_id,))
    db.commit()


def _mode_chosen_in_db(db, user_id: int) -> bool:
    """True when the user answered the chooser since sign-in
    (mode_chosen_at set in the database)."""
    row = db.execute(
        "SELECT mode_chosen_at FROM users WHERE id = ?",
        (user_id,)).fetchone()
    return bool(row and row["mode_chosen_at"])


def _record_login_event(app, username: str, success: bool) -> None:
    """One row per login attempt, for anomaly detection."""
    _db(app).execute(
        "INSERT INTO login_events (username, ip, user_agent, success, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (username, _client_ip(), _client_ua(), 1 if success else 0,
         _now()))
    _db(app).commit()


def _anomaly_lock(app, username: str) -> bool:
    """Anomaly check, run after a failed login: when this username
    has ANOMALY_FAIL_THRESHOLD or more failed logins in the last 15
    minutes from IPs it has NEVER successfully logged in from, the
    account is locked for 15 minutes and a suspicious-activity audit
    row is written. Returns True when the lock was applied."""
    db = _db(app)
    window_start = (datetime.now(timezone.utc)
                    - timedelta(seconds=ANOMALY_WINDOW_SECONDS)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    known_ips = {r["ip"] for r in db.execute(
        "SELECT DISTINCT ip FROM login_events WHERE username = ? "
        "AND success = 1", (username,)).fetchall()}
    fails = db.execute(
        "SELECT ip FROM login_events WHERE username = ? AND success = 0 "
        "AND created_at >= ?", (username, window_start)).fetchall()
    new_ip_fails = sum(1 for r in fails if r["ip"] not in known_ips)
    if new_ip_fails < ANOMALY_FAIL_THRESHOLD:
        return False
    locked_until = (datetime.now(timezone.utc)
                    + timedelta(minutes=LOCKOUT_MINUTES)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO login_attempts (username, fail_count, locked_until) "
        "VALUES (?, 0, ?) ON CONFLICT(username) DO UPDATE SET "
        "locked_until = excluded.locked_until",
        (username, locked_until))
    db.commit()
    row = db.execute("SELECT id FROM users WHERE username = ?",
                     (username,)).fetchone()
    if row:
        _log(app, row["id"], "suspicious_activity",
             f"Account locked for {LOCKOUT_MINUTES} minutes: "
             f"{new_ip_fails} failed logins in 15 minutes from "
             f"unrecognised addresses.")
    return True


def _sent_sol_today(db, user_id: int, exclude_intent_id: int = None) -> float:
    """SOL sent (or queued to send) today, UTC, across all of the
    user's send intents that have not failed. The daily send cap is
    checked against this. `exclude_intent_id` leaves one intent out
    (the confirm step re-checks the intent it is about to sign)."""
    if exclude_intent_id is None:
        row = db.execute(
            "SELECT COALESCE(SUM(amount_lamports), 0) AS total "
            "FROM send_intents WHERE user_id = ? AND status IN "
            "('pending', 'sent', 'sent_unconfirmed') AND "
            "date(created_at) = date('now')",
            (user_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT COALESCE(SUM(amount_lamports), 0) AS total "
            "FROM send_intents WHERE user_id = ? AND id != ? AND "
            "status IN ('pending', 'sent', 'sent_unconfirmed') AND "
            "date(created_at) = date('now')",
            (user_id, exclude_intent_id)).fetchone()
    return lamports_to_sol(int(row["total"]))


def _bot_mode(db, user_id: int) -> str:
    """The bot mode for the user's session: 'real' when the session
    is in real (devnet) mode, else 'paper' (same mapping as the bots
    list in _dashboard_data)."""
    return ("real" if _active_mode(db, user_id) == "devnet" else "paper")


def _find_bot_by_name(db, user_id: int, name: str, mode: str | None = None):
    """First bot whose name contains `name` (assistant-style lookup).

    Scoped to `mode` when given: with the same preset running in
    paper and real, an unscoped lookup always hits the older
    (paper) copy, so the assistant must ask for the copy in the
    user's current mode.
    """
    if not name:
        return None
    if mode is not None:
        return db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name LIKE ? "
            "AND mode = ?",
            (user_id, "%" + name + "%", mode)).fetchone()
    return db.execute(
        "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
        (user_id, "%" + name + "%")).fetchone()


def _wallet(app, user_id: int):
    return _db(app).execute(
        "SELECT pubkey, encrypted_secret FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()


def _real_balance(app, pubkey: str):
    """SOL balance or None when the RPC is unreachable (never faked).
    Uses the fallback RPC (see rpc_client_for_url): the bare client
    used to freeze the header balance whenever the primary endpoint
    rate-limited, while prices kept moving."""
    try:
        client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
        return lamports_to_sol(client.get_balance_lamports(pubkey))
    except Exception:
        return None


def _paper(app, user_id: int):
    row = _db(app).execute(
        "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
        (user_id,)).fetchone()
    return row["cash_usd"] if row else 0.0


def _active_mode(db, user_id: int) -> str:
    """The user's header/buy mode: 'paper' or 'devnet'. Anything else
    (or a missing row) is paper — fail closed to the safe side."""
    row = db.execute(
        "SELECT active_mode FROM users WHERE id = ?", (user_id,)).fetchone()
    mode = (row["active_mode"] if row else None) or "paper"
    return mode if mode in ("paper", "devnet") else "paper"


def _mode_label(mode: str, cfg=None) -> str:
    """The plain-words label for a mode, used on every page so the
    user always knows which side they are on. The real-mode label
    names the network the app actually runs on — "Real trading —
    mainnet" now that the app is on mainnet (real mainnet SOL),
    "Real trading — devnet" on devnet. The STORED mode value stays
    'devnet' for real mode (internal); only the words change."""
    if mode == "devnet":
        network = (cfg.network if cfg is not None else "devnet")
        return f"Real trading — {network}"
    return "Demo trading (paper)"


def _activity_for_mode(db, user_id: int, mode: str, limit: int = 20):
    """Recent activity for one mode only. Trading rows are tagged
    'paper' or 'real' and never cross over; account rows (mode='')
    belong to neither trading mode and show in both. `limit` caps
    the rows (20 for the dashboard snippet, 100 for the Logs page)."""
    wanted = "real" if mode == "devnet" else "paper"
    return db.execute(
        "SELECT kind, detail, created_at FROM activity "
        "WHERE user_id = ? AND (mode = ? OR mode = '') "
        "ORDER BY id DESC LIMIT ?",
        (user_id, wanted, limit)).fetchall()


def _real_holdings_view(db, user_id: int) -> list:
    """The user's real positions, merged by coin: manual real
    holdings (real_holdings) plus bot-managed real positions
    (bot_positions, mode='real'). The two tables never hold the same
    tokens, so merging them never double-counts. Returns
    [{coin_id, amount, avg_price}] sorted by coin."""
    merged: dict = {}
    for table in ("real_holdings",):
        for r in db.execute(
                "SELECT coin_id, amount, avg_price FROM " + table +
                " WHERE user_id = ?", (user_id,)).fetchall():
            merged[r["coin_id"]] = {
                "coin_id": r["coin_id"], "amount": float(r["amount"]),
                "avg_price": float(r["avg_price"])}
    for r in db.execute(
            "SELECT coin_id, amount, avg_price FROM bot_positions "
            "WHERE user_id = ? AND mode = 'real'", (user_id,)).fetchall():
        entry = merged.get(r["coin_id"])
        if entry is None:
            merged[r["coin_id"]] = {
                "coin_id": r["coin_id"], "amount": float(r["amount"]),
                "avg_price": float(r["avg_price"])}
        else:
            total = entry["amount"] + float(r["amount"])
            entry["avg_price"] = (
                (entry["amount"] * entry["avg_price"]
                 + float(r["amount"]) * float(r["avg_price"])) / total
                if total > 0 else entry["avg_price"])
            entry["amount"] = total
    return [merged[k] for k in sorted(merged)]


def _reconciled_real_holdings(app, db, user_id: int) -> tuple[list, bool, bool]:
    """Real holdings with on-chain balances merged in (devnet).

    The chain is the truth for the AMOUNT held: a coin the wallet
    holds on-chain but the DB never recorded (its buys were
    recorded as failed, e.g. Gate) appears with avg_price=None and
    on_chain_only=True. Returns (holdings, chain_ok,
    chain_complete): chain_ok is False only when the wallet exists
    and the on-chain read failed — then the rows are the DB's last
    saved amounts and the page MUST say so (balances_stale, shown
    on Home), never present them as live. chain_complete is True
    only when BOTH token programs answered: only then does the
    reconciliation drop DB rows the chain does not list, so a coin
    leaves the holdings list the moment its balance hits 0 — while
    a partial read keeps every DB row (a flaky Token-2022 read
    must never hide Gate). Any failure (no wallet, RPC down,
    partial read) falls back to the DB view alone — a chain
    problem must never hide a recorded holding. Nothing here
    writes to the DB; the reconciliation is per-render only."""
    db_rows = _real_holdings_view(db, user_id)
    try:
        wallet = _wallet(app, user_id)
        if not wallet:
            return db_rows, True, False
        client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
        chain, chain_ok, chain_complete = fetch_chain_token_balances(
            client, wallet["pubkey"])
        return reconcile_holdings(
            db_rows, chain, chain_complete=chain_complete), chain_ok, \
            chain_complete
    except Exception:
        return db_rows, False, False


def _record_real_holding(db, user_id: int, mint: str, coin_amount: float,
                         avg_price: float) -> None:
    """Merge a real buy into real_holdings (manual real trades)."""
    row = db.execute(
        "SELECT amount, avg_price FROM real_holdings "
        "WHERE user_id = ? AND coin_id = ?",
        (user_id, mint)).fetchone()
    if row:
        total = float(row["amount"]) + coin_amount
        avg = ((float(row["amount"]) * float(row["avg_price"]))
               + coin_amount * avg_price) / total
        db.execute("UPDATE real_holdings SET amount = ?, avg_price = ? "
                   "WHERE user_id = ? AND coin_id = ?",
                   (total, avg, user_id, mint))
    else:
        db.execute("INSERT INTO real_holdings (user_id, coin_id, amount, "
                   "avg_price) VALUES (?, ?, ?, ?)",
                   (user_id, mint, coin_amount, avg_price))
    db.commit()


def _remove_real_holding(db, user_id: int, mint: str,
                         amount: float) -> None:
    """Take `amount` out of real_holdings after a real sell."""
    row = db.execute(
        "SELECT amount FROM real_holdings WHERE user_id = ? AND coin_id = ?",
        (user_id, mint)).fetchone()
    if not row:
        return
    remaining = float(row["amount"]) - amount
    if remaining <= 0:
        db.execute("DELETE FROM real_holdings WHERE user_id = ? "
                   "AND coin_id = ?", (user_id, mint))
    else:
        db.execute("UPDATE real_holdings SET amount = ? "
                   "WHERE user_id = ? AND coin_id = ?",
                   (remaining, user_id, mint))
    db.commit()


def _shrink_bot_positions(db, user_id: int, coin: str, amount: float,
                          mode: str) -> None:
    """Take `amount` out of the user's bot_positions rows for
    (coin, mode), oldest-first (opened_at, then id). A row fully
    consumed is deleted and its scanner watches cleared (like
    _close_position in the silent loop); a partially consumed row
    is updated. The caller attributes the sold amount first to
    the manual holding, then to bot rows — so `amount` here is
    only the part of the sell that came from bot-managed rows.
    Never raises — a bookkeeping failure must never break a
    completed sell."""
    if amount is None or amount <= 0:
        return
    try:
        from .scanner import clear_watches
    except Exception:
        clear_watches = None
    try:
        rows = db.execute(
            "SELECT id, bot_id, amount FROM bot_positions "
            "WHERE user_id = ? AND coin_id = ? AND mode = ? "
            "ORDER BY opened_at ASC, id ASC",
            (user_id, coin, mode)).fetchall()
        remaining = float(amount)
        for row in rows:
            if remaining <= 0:
                break
            row_amount = float(row["amount"])
            if row_amount <= remaining:
                db.execute("DELETE FROM bot_positions WHERE id = ?",
                           (row["id"],))
                if clear_watches is not None:
                    try:
                        clear_watches(db, row["bot_id"], coin)
                    except Exception:
                        pass
                remaining -= row_amount
            else:
                db.execute("UPDATE bot_positions SET amount = ? "
                           "WHERE id = ?",
                           (row_amount - remaining, row["id"]))
                remaining = 0
        db.commit()
    except Exception:
        return


def _real_avg_price(app, mint: str, amount_sol: float,
                    coin_amount: float) -> float:
    """USD cost basis for a real buy, from the EXECUTED amounts: the
    SOL actually spent (what the chain took) priced at the live SOL
    price, over the tokens actually received. The mint's CURRENT
    price is never the cost basis — that is what the coin is worth
    now, not what was paid, and using it erases the trade's P&L from
    the first render. 0.0 when the executed cost cannot be computed
    — the holding is then shown with P&L '—', never guessed. The
    live SOL price comes from the feed's SOL tick only — the one
    SOL number (Jupiter-fed, via the feed) — else 0.0. `mint` is
    kept for the caller's signature; it is not priced here."""
    if not amount_sol or amount_sol <= 0 or not coin_amount \
            or coin_amount <= 0:
        return 0.0
    sol_price = _feed_sol_price_usd(app)
    if sol_price and sol_price > 0:
        return amount_sol * sol_price / coin_amount
    return 0.0


def _feed_sol_price_usd(app) -> float | None:
    """The ONE SOL price in USD, or None: the live feed's SOL tick
    (Jupiter-fed) only — never a direct Jupiter/CoinGecko call, never
    a snapshot. A SOL tick is usable up to SOL_MAX_AGE_SECONDS (90s,
    the stream's own SOL bound in streaming.py): the feed refreshes
    SOL every 60s, so the 30s fresh-tick cut would leave the SOL
    number empty about half the time, and SOL moves slowly enough
    that 90s bounds the conversion error. Older than that, or no
    tick at all, is None — "no data", never a stale number."""
    if app is None:
        return None
    feed = app.config.get("LIVE_FEED")
    if feed is None:
        return None
    try:
        age = feed.tick_age_seconds("SOL")
    except Exception:
        age = None
    if age is None or age > SOL_MAX_AGE_SECONDS:
        return None
    try:
        tick = feed.snapshot(["SOL"]).get("SOL")
    except Exception:
        return None
    if tick is None:
        return None
    try:
        price = float(tick["price"])
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _feed_price_list(app) -> dict:
    """The get_prices-shaped price list, from the feed only: exactly
    one entry, SOL — the unit of account — at the feed's SOL tick,
    with source "live" when the tick is there and "unavailable"
    when it is not. This is what the dashboard / coin-detail
    templates read for their "Prices: live" label and what
    _coin_rows takes as its price seed (SOL only; mints are priced
    from the feed ticks / chain read inside _coin_rows). No
    Jupiter/CoinGecko read happens here."""
    sol_price = _feed_sol_price_usd(app)
    return {"prices": {"SOL": {"price": sol_price,
                               "change_24h": None,
                               "change_1h": None}},
            "source": "live" if sol_price is not None else "unavailable",
            "as_of": _now()}


def _log_last_good_sol_rate(app, sol_price: float,
                            age: float) -> None:
    """Throttled warning (one per minute per app) when a chain read
    converts at the last-known-good SOL rate: the feed's SOL tick
    is stale and the Jupiter fetch failed, so the conversion
    carries SOL's whole move since that tick. The label is the
    point — a stale rate must be visible in the logs, never
    silent. State lives on the app config, so tests (fresh app
    per test) never inherit another test's throttle."""
    try:
        state = app.config.setdefault("_fallback_sol_log_at", {})
        last = state.get("last-good", 0.0)
        now = time.time()
        if now - last < 60.0:
            return
        state["last-good"] = now
    except Exception:
        pass
    log.warning("[price-fallback] chain read converting at "
                "last-known-good SOL rate $%.2f, %.0fs old "
                "(feed SOL tick stale, Jupiter fetch failed)",
                sol_price, age)


def _chain_prices_for_mints(app, mints) -> dict:
    """{mint: price_usd} from the single source's pull twin: one
    batched Helius RPC read of the bonding curves (graduated ->
    Raydium pool, the same lookup the stream uses). Mints it cannot
    price are ABSENT — never guessed, never a second source. The
    SOL rate for the USD conversion is decoupled from the feed's
    SOL tick (see sol_rate_for_conversion in streaming.py): a
    fresh feed tick wins, else a direct Jupiter fetch, else the
    last-known-good tick labeled stale in the logs. No rate at
    all means {} — logged, never a silent skip. Never raises."""
    wanted = [m for m in dict.fromkeys(mints or []) if m]
    if not wanted or app is None:
        return {}
    feed = app.config.get("LIVE_FEED") if app is not None else None
    rate = sol_rate_for_conversion(feed)
    if rate is None:
        log.warning("[price-fallback] _chain_prices_for_mints: no "
                    "SOL rate for %d mint(s) — feed SOL tick "
                    "missing/stale, Jupiter fetch failed, no "
                    "last-known-good; returning {}",
                    len(wanted))
        return {}
    sol_price, source, age = rate
    if source == "last-good":
        _log_last_good_sol_rate(app, sol_price, age)
    try:
        client = rpc_client_for_config(_cfg(app))
    except Exception as exc:
        log.warning("[price-fallback] _chain_prices_for_mints: "
                    "RPC client unavailable: %s", exc)
        return {}
    try:
        return chain_price_usd_for_mints(client, wanted, sol_price) or {}
    except Exception as exc:
        log.warning("[price-fallback] _chain_prices_for_mints: "
                    "chain read failed: %s", exc)
        return {}


def _snapshot_price_map(app, user_id: int) -> dict:
    """{mint: price_usd} for a P&L snapshot, from the single source:
    the feed's fresh ticks for the user's paper holdings, else one
    batched Helius chain read for the uncovered mints; SOL from the
    feed tick. A mint the source cannot price is LEFT OUT — the
    snapshot then values it at cost (pnl.current_holdings), never
    $0 and never a second source's number. This replaces the old
    get_prices() map, which carried SOL only and silently valued
    every coin holding at cost after every trade and refresh."""
    feed = app.config.get("LIVE_FEED") if app is not None else None
    try:
        mints = [r["coin_id"] for r in _db(app).execute(
            "SELECT coin_id FROM paper_holdings WHERE user_id = ?",
            (user_id,)).fetchall()]
    except Exception:
        mints = []
    out = {}
    if feed is not None:
        for mint in mints:
            try:
                price = feed.live_price(mint)
            except Exception:
                price = None
            if price is not None and price > 0:
                out[mint] = price
    sol = _feed_sol_price_usd(app)
    if sol is not None:
        out["SOL"] = sol
    uncovered = [m for m in mints if m not in out]
    if uncovered:
        out.update(_chain_prices_for_mints(app, uncovered))
    return out


def _coin_rows(db, user_id: int, mode: str, prices: dict,
               feed=None, app=None) -> list:
    """The coins list: ONLY the coins the user holds in the ACTIVE
    mode, with name, ticker, current price, P&L and 1-hour % change.
    Returns (rows, balances_stale): balances_stale is True only in
    real mode when the on-chain balance read failed, so the amounts
    shown are the DB's last saved ones — the caller must surface
    that on the page instead of presenting them as live. Coins the
    user does not hold are not listed here at all — the trenches
    list and the quick Buy/Sell popup (with live search) are how
    new coins are found. P&L is (current price − average
    cost) × amount; a holding with no current price has P&L None
    and is valued at cost — never guessed. A holding with no
    cost basis (on-chain only, e.g. Gate, whose buys were recorded
    as failed) has avg_price None and P&L None — never guessed.
    A holding with no 1h figure in the feed has change_1h None
    ("—" on the page). When a live feed is passed, held mints are
    priced from its fresh ticks first — that is the only live price
    for a mint with no DEX pool (bonding-curve price) — and only
    mints the feed has no fresh tick for fall back to ONE batched
    Helius RPC read of their bonding curves (graduated -> Raydium
    pool, the same lookup the stream uses); a mint neither can
    price stays unpriced — valued at cost, P&L None, never
    guessed. The 1h change comes from the feed's own recorded tick
    history (feed.change_over, 1h) — never a DexScreener figure.
    A price the feed rejects against its last good tick is dropped
    too, so a bad tick can never inflate a balance on render; with
    no feed (or no reference yet) every price passes unchanged. In devnet
    mode with `app`, holdings are reconciled with the wallet's
    on-chain token balances first (see _reconciled_real_holdings),
    so coins the chain holds but the DB never recorded appear —
    and, when BOTH token programs answered, coins the chain no
    longer lists are dropped, so the list shows only coins
    actually owned and a coin leaves it the moment its balance
    hits 0. A partial or failed chain read keeps every DB row.
    Every row shows the coin's real official name + ticker only:
    the trenches feed names it, else the stored `coin_meta` row
    (fetched on purchase, or lazily here), else the on-chain
    Metaplex metadata — and when nothing names it, "Unknown coin"
    with an empty ticker. A mint address is never shown as a name
    and the old "Pump.fun token …" label is gone. Each row is
    enriched with its real token image and sparkline
    (home_holdings.enrich_row) — a row with no image or no history
    keeps the ticker-initial icon / no sparkline, never a fake.
    A legacy holding whose coin_id is not a mint address (a symbol
    bought when the app still listed major coins) is shown at cost
    with no live price — it has no feed any more, so it can never
    be priced, but it is never hidden either."""
    price_map = {c: e.get("price") for c, e in prices["prices"].items()}
    balances_stale = False
    if mode == "devnet":
        if app is not None:
            held_rows, chain_ok, _chain_complete = \
                _reconciled_real_holdings(
                    app, db, user_id)
            held = {h["coin_id"]: h for h in held_rows}
            # The chain read failed: the amounts below are the DB's
            # last saved ones, not live — the page must say so (the
            # caller puts balances_stale in the template data).
            balances_stale = not chain_ok
        else:
            held = {h["coin_id"]: h for h in _real_holdings_view(db, user_id)}
    else:
        held = {h["coin_id"]: h for h in db.execute(
            "SELECT coin_id, amount, avg_price FROM paper_holdings "
            "WHERE user_id = ?", (user_id,)).fetchall()}
    # Only current coins: a zero-balance row drops off the list on
    # the next refresh (a coin whose balance hit 0 is gone, never
    # shown at 0). This is the filter the holdings list serves —
    # reconcile_holdings keeps DB rows the chain does not list (a
    # partial chain read must never hide a holding), so the
    # balance > 0 cut happens here, on what the page renders.
    held = {c: h for c, h in held.items()
            if float(h["amount"] or 0) > 0}
    mint_ids = [c for c in held if c not in price_map
                and is_valid_address(c)]
    changes_1h: dict = {}
    identities: dict = {}
    # Mints priced by the chain fallback this call (see below):
    # the jump guard never vetoes these (fix 3).
    chain_priced: set = set()
    if mint_ids:
        # The live feed first: a fresh validated tick (the Helius
        # stream, or the bonding-curve price for a mint with no DEX
        # pool) is the price — that is how Gate/AuditTest get a live
        # price here at all. Only mints the feed has no fresh tick
        # for fall through to ONE batched Helius chain read; a stale
        # feed tick is never presented as live, and a mint neither
        # can price stays unpriced (valued at cost, P&L None).
        for mint in mint_ids:
            try:
                price = feed.live_price(mint) if feed is not None \
                    else None
            except Exception:
                price = None
            if price is not None and price > 0:
                price_map[mint] = price
        uncovered = [m for m in mint_ids if m not in price_map]
        if uncovered and app is not None:
            chain = _chain_prices_for_mints(app, uncovered)
            price_map.update(chain)
            chain_priced = {m for m, p in chain.items()
                            if p is not None and p > 0}
        if feed is not None:
            now = time.time()
            for mint in mint_ids:
                try:
                    changes_1h[mint] = feed.change_over(
                        mint, 3600, now)
                except Exception:
                    changes_1h[mint] = None
            changes_1h = {m: v for m, v in changes_1h.items()
                          if v is not None}
        try:
            # Real name + ticker for held mints, from the discovery
            # index (identity, not price). A mint the index does
            # not name is resolved from the stored coin_meta /
            # on-chain metadata below — never guessed, never an
            # address.
            identities = fetch_mint_identities(mint_ids)
        except Exception:
            identities = {}
    # A bad tick must never inflate a balance on render either: drop
    # any price the live feed rejects against its last good tick. No
    # reference yet (feed empty or off) means everything passes.
    # The jump guard must never veto the FALLBACK, though: a chain
    # price is a fresh on-chain read, so it is shown even when it
    # moved >50% from the last good tick — a genuine step change
    # after a stall, which the guard would otherwise drop forever
    # (a chain read cannot re-base the reference: re-basing needs
    # 3 agreeing stream ticks). This matches
    # _reprice_trenches_rows, which never applies the guard to
    # chain prices. Chain prices the guard would have dropped are
    # logged once per call (count + mints) and marked on their
    # rows (price_moved). The guard still applies to
    # feed-sourced prices.
    would_drop_chain: list = []
    if feed is not None:
        would_drop_chain = [c for c, p in price_map.items()
                            if p is not None and c in chain_priced
                            and not feed.would_accept(c, p)]
        if would_drop_chain:
            log.warning("[price-fallback] _coin_rows showing %d "
                        "chain price(s) the jump guard would have "
                        "dropped: %s", len(would_drop_chain),
                        ", ".join(sorted(would_drop_chain)))
        price_map = {c: p for c, p in price_map.items()
                     if p is None or c in chain_priced
                     or feed.would_accept(c, p)}
    # A price of 0 or less is not a price: treat it as missing, so a
    # dead quote can never value a holding at $0 or invent a loss.
    clean_map = {}
    for c, p in price_map.items():
        if p is None:
            clean_map[c] = None
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            clean_map[c] = None
            continue
        clean_map[c] = p if p > 0 else None
    price_map = clean_map
    rpc_url = _cfg(app).rpc_url if app is not None else None
    rows = []
    for coin_id in sorted(held):
        h = held[coin_id]
        price = price_map.get(coin_id)
        avg = h["avg_price"]
        pnl = None
        if price is not None and avg is not None:
            pnl = (float(price) - float(avg)) * float(h["amount"])
        value = (float(h["amount"]) * float(price)
                 if price is not None else None)
        if not is_valid_address(coin_id):
            # Legacy symbol holding (bought when the app still
            # listed major coins): no feed, no live price — shown
            # at cost, never hidden, never guessed.
            rows.append({"coin_id": coin_id, "name": coin_id,
                         "ticker": coin_id, "price": None,
                         "change_24h": None, "change_1h": None,
                         "held": float(h["amount"]),
                         "avg_price": (float(avg)
                                       if avg is not None else None),
                         "pnl": None, "is_mint": False,
                         "value": value})
            continue
        ident = identities.get(coin_id) or {}
        name = ident.get("name")
        ticker = ident.get("symbol")
        if name is None and ticker is None:
            # The feed did not name it: stored coin_meta (fetched
            # on purchase, or lazily now), then the on-chain
            # Metaplex metadata. Nothing names it -> "Unknown coin".
            try:
                meta = ensure_coin_meta(db, coin_id, rpc_url=rpc_url)
            except Exception:
                meta = None
            if meta:
                name = meta.get("name")
                ticker = meta.get("symbol")
        rows.append({"coin_id": coin_id,
                     "name": name or ticker or "Unknown coin",
                     "ticker": (ticker or "").upper(),
                     "price": price, "change_24h": None,
                     "change_1h": changes_1h.get(coin_id),
                     "held": float(h["amount"]),
                     "avg_price": (float(avg) if avg is not None else None),
                     "pnl": pnl, "is_mint": True,
                     "value": value,
                     "price_moved": coin_id in would_drop_chain})
    # Attach each row's real token image + sparkline. Enrichment
    # never raises and never invents data — a row with no image or
    # no history keeps the ticker-initial icon / no sparkline.
    for row in rows:
        enrich_row(row, feed=feed, db=db, rpc_url=rpc_url)
    return rows, balances_stale


# On-chain SOL balances are cached 60 seconds per wallet so the
# header balance (shown on every page) does not hit the RPC on every
# render. A stale balance is only ever shown for a minute, and a
# failed lookup is cached as None ("unavailable"), never guessed.
_REAL_BALANCE_CACHE: dict = {"at": {}, "value": {}}


def _cached_real_balance(app, pubkey: str):
    now = time.time()
    if (pubkey in _REAL_BALANCE_CACHE["value"]
            and now - _REAL_BALANCE_CACHE["at"].get(pubkey, 0) < 60):
        return _REAL_BALANCE_CACHE["value"][pubkey]
    value = _real_balance(app, pubkey)
    _REAL_BALANCE_CACHE["at"][pubkey] = now
    _REAL_BALANCE_CACHE["value"][pubkey] = value
    return value


def _gate_fuel_trades_left(app, user):
    """Trades left from the package ledger, for the topbar chip
    (base.html) — the ledger truth, read fresh on every render
    (a cheap DB read, no cache). The chip shows in real mode on
    every page. None in paper mode (paper trades are free — the
    template shows "free ~ ∞" there instead) and when the user
    has no wallet. Never raises."""
    try:
        db = _db(app)
        if _active_mode(db, user["id"]) != "devnet":
            return None
        wallet = _wallet(app, user["id"])
        if not wallet:
            return None
        return gate_fuel_mod.get_fuel(db, user["id"])[
            "trades_remaining"]
    except Exception:
        return None


def _decision_price_age(app, coin: str) -> float | None:
    """How old the decision price for a manual trade is, in
    seconds — or None when no fresh decision price exists at all.
    The live feed's fresh tick carries its own time; a price
    resolved just now in-request (one batched Helius chain read)
    is 0. The caller refuses the buy outright on None: a buy with
    no fresh decision price must not execute. The real executor
    refuses a buy on a price older than the freshness window, so
    this age is the honest bound the route can state — never a
    guessed zero for a tick it did not see."""
    feed = app.config.get("LIVE_FEED")
    if feed is not None:
        try:
            tick = feed.snapshot([coin]).get(coin)
        except Exception:
            tick = None
        if tick is not None and not tick.get("stale"):
            try:
                as_of = datetime.strptime(
                    tick["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc)
                                 - as_of).total_seconds())
            except (TypeError, ValueError):
                return 0.0
    # No fresh feed tick: a chain read just now is still a fresh
    # decision price (age 0). Only when that fails too is there
    # no decision price — None — and the caller refuses the buy.
    if _chain_prices_for_mints(app, [coin]).get(coin):
        return 0.0
    return None


def _parse_buy_amount(app, form, price, sol_price_usd):
    """The buy amount from the form, as (amount_sol, amount_usd,
    error). Buy amounts are USD only (James, 2026-11-26 ~21:48 —
    the multi-currency picker + live FX source are deferred to a
    later date; USD is the default currency because the live SOL
    and coin prices are USD-denominated). The new buy forms post
    `amount` in USD: the typed amount converts at the LIVE SOL
    price, then the live coin price — exact, never more or less
    than typed, with no FX step. The legacy fields (amount_sol /
    amount_tokens / amount_usd / usd) are still accepted — old
    cached pages and tests post them. `amount_usd` is the buy's
    size in USD: it picks the gas-reserve tier, nothing else. A
    SOL-denominated buy with no live SOL price still parses — its
    USD size is None and the tier fails safe to the large one."""
    amount_raw = (form.get("amount") or "").strip()
    if amount_raw:
        try:
            usd = float(amount_raw)
        except ValueError:
            return None, None, "Amount must be a number."
        if usd <= 0:
            return None, None, "Amount must be more than 0."
        if sol_price_usd is None:
            return None, None, "SOL price unavailable right now."
        return usd / sol_price_usd, usd, None
    amount_sol_raw = (form.get("amount_sol") or "").strip()
    if amount_sol_raw:
        try:
            amount_sol = float(amount_sol_raw)
        except ValueError:
            return None, None, "Amount must be a number."
        if amount_sol <= 0:
            return None, None, "Amount must be more than 0."
        # A SOL-denominated buy needs no SOL price to size the
        # spend — only to size it in USD for the reserve tier. With
        # no live SOL price the USD size is unknown (None) and the
        # tier fails safe to the large one, so the buy still reaches
        # the fuel gate instead of refusing here on a price it
        # never needed.
        if sol_price_usd is None:
            return amount_sol, None, None
        return amount_sol, amount_sol * sol_price_usd, None
    amount_tokens_raw = (form.get("amount_tokens") or "").strip()
    if amount_tokens_raw:
        try:
            tokens = float(amount_tokens_raw)
        except ValueError:
            return None, None, "Amount must be a number."
        if tokens <= 0:
            return None, None, "Amount must be more than 0."
        if price is None or price <= 0 or sol_price_usd is None:
            return None, None, (
                "Price unavailable right now — the buy needs current "
                "prices to convert tokens.")
        return tokens * price / sol_price_usd, tokens * price, None
    usd_raw = (form.get("amount_usd") or form.get("usd") or "").strip()
    if usd_raw:
        try:
            usd = float(usd_raw)
        except ValueError:
            return None, None, "Amount must be a number."
        if usd <= 0:
            return None, None, "Amount must be more than 0."
        if sol_price_usd is None:
            return None, None, "SOL price unavailable right now."
        return usd / sol_price_usd, usd, None
    return 0.0, None, None


def _parse_exit_fields(form):
    """(stop_loss_pct, take_profit_pct, error) from the buy form's
    stop loss / take profit checkboxes. The % is the move off the
    price paid: stop loss is the drop %, take profit the gain %.
    Take profit is ALWAYS a positive profit % (James, 2026-11-26):
    a zero or negative number is refused here — a loss can never
    be stored as a take profit. A checkbox left unticked means
    that exit is not set."""
    sl_pct = None
    tp_pct = None
    if form.get("stop_loss"):
        raw = (form.get("stop_loss_pct") or "").strip()
        try:
            sl_pct = float(raw)
        except ValueError:
            return None, None, "Stop loss must be a number."
        if sl_pct <= 0:
            return None, None, (
                "Stop loss must be a positive % below the price paid.")
    if form.get("take_profit"):
        raw = (form.get("take_profit_pct") or "").strip()
        try:
            tp_pct = float(raw)
        except ValueError:
            return None, None, "Take profit must be a number."
        if tp_pct <= 0:
            return None, None, (
                "Take profit must be a positive % — a loss can never "
                "be a take profit.")
    return sl_pct, tp_pct, None


def _store_user_exit_for_buy(db, user_id: int, coin_id: str, mode: str,
                             entry_price, sl_pct, tp_pct):
    """Store the stop loss / take profit set on a buy, and return
    the note lines for the result page. The triggers are priced
    off the EXECUTED average cost (the holding's merged average
    after this buy), never the mark price. Nothing is stored when
    neither exit was ticked, or when no entry price exists to base
    the triggers on."""
    if sl_pct is None and tp_pct is None:
        return []
    if entry_price is None or entry_price <= 0:
        return ["Stop loss / take profit were not set — no entry "
                "price to base them on."]
    try:
        row = set_user_exit(db, user_id, coin_id, mode, entry_price,
                            sl_pct, tp_pct)
    except ValueError as exc:
        return [f"Stop loss / take profit were not set — {exc}"]
    if row is None:
        return []
    lines = []
    if sl_pct is not None:
        lines.append(
            f"Stop loss set: sells at {fmt_price_sig(row['stop_price'])} "
            f"({sl_pct:g}% below the {fmt_price_sig(entry_price)} paid).")
    if tp_pct is not None:
        lines.append(
            f"Take profit set: sells at "
            f"{fmt_price_sig(row['take_profit_price'])} "
            f"({tp_pct:g}% above the {fmt_price_sig(entry_price)} paid).")
    return lines


def _trade_failure_lines(result, side: str):
    """Result-page lines for a failed real trade: what went wrong,
    how to fix it, and the transaction ID — or a plain statement
    that there is none when the trade never reached the chain
    (James, 2026-11-26: the log must never invent a transaction
    ID). The same lines go into the activity log."""
    reason = result.get("reason", "real execution failed")
    sig = result.get("signature")
    lines = [f"No {side} was placed — {reason}.",
             f"Fix: {fix_for_skip_reason(reason)}"]
    if sig:
        lines.append(f"Transaction: {sig}")
    else:
        lines.append("No transaction reached the chain — there is no "
                     "transaction ID.")
    return lines


def _trade_sol_prices(app):
    """(primary, secondary) SOL prices in USD for a manual trade's
    price-gate context: the feed's SOL tick only (the one SOL
    number, Jupiter-fed via the feed), and None — there is no
    second SOL source any more (single-price source). Either may
    be None — the gate skips only the check it cannot run, never a
    guessed price."""
    return _feed_sol_price_usd(app), None


def _header_balance(app, user):
    """What the top corner shows: {'mode', 'label', 'text'}.

    Paper mode: paper portfolio value (cash + holdings at current
    prices). Real mode: the app wallet's on-chain SOL balance.
    Never faked — an unreachable price feed or RPC shows
    'unavailable' instead of a made-up number. The label names
    the network the app actually runs on (Mainnet now). The SOL
    in the paper total is valued at the feed's SOL tick only — no
    REST/Jupiter read here.
    """
    db = _db(app)
    mode = _active_mode(db, user["id"])
    if mode == "devnet":
        wallet = _wallet(app, user["id"])
        sol = (_cached_real_balance(app, wallet["pubkey"])
               if wallet else None)
        text = f"{sol:.4f} SOL" if sol is not None else "unavailable"
        label = ("Mainnet" if _cfg(app).network == "mainnet"
                 else "Devnet")
        return {"mode": "devnet", "label": label, "text": text}
    sol_price = _feed_sol_price_usd(app)
    price_map = ({"SOL": sol_price}
                 if sol_price is not None and sol_price > 0 else {})
    # Same totals as the P&L page: a holding with no usable price is
    # valued at its average cost, never $0.
    totals = pnl_mod.portfolio_totals(db, user["id"], price_map)
    return {"mode": "paper", "label": "Paper",
            "text": f"${totals['total_value_usd']:,.2f}"}


def _record_pnl_snapshot(app, user_id: int, price_map: dict) -> None:
    """One P&L snapshot after a trade. Never breaks the trade itself.
    A price of 0 or less is dropped from the map — missing, never
    zero — so a snapshot can never value a holding at $0."""
    try:
        clean = {}
        for c, p in (price_map or {}).items():
            try:
                p = float(p) if p is not None else None
            except (TypeError, ValueError):
                p = None
            if p is not None and p > 0:
                clean[c] = p
        totals = pnl_mod.portfolio_totals(_db(app), user_id, clean)
        wallet = _wallet(app, user_id)
        real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
        pnl_mod.record_snapshot(_db(app), user_id, totals["cash_usd"],
                                totals["holdings_value_usd"], real_sol)
    except Exception:
        pass


def _check_login_lock(app, username: str):
    row = _db(app).execute(
        "SELECT fail_count, locked_until FROM login_attempts WHERE username = ?",
        (username,)).fetchone()
    if row and row["locked_until"]:
        locked_until = datetime.strptime(
            row["locked_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < locked_until:
            return True
    return False


def _record_login_fail(app, username: str):
    db = _db(app)
    row = db.execute(
        "SELECT fail_count FROM login_attempts WHERE username = ?",
        (username,)).fetchone()
    count = (row["fail_count"] if row else 0) + 1
    locked_until = None
    if count >= MAX_LOGIN_FAILS:
        locked_until = (datetime.now(timezone.utc)
                        + timedelta(minutes=LOCKOUT_MINUTES)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
        count = 0
    db.execute(
        "INSERT INTO login_attempts (username, fail_count, locked_until) "
        "VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
        "fail_count = excluded.fail_count, "
        "locked_until = excluded.locked_until",
        (username, count, locked_until))
    db.commit()


def _clear_login_fails(app, username: str):
    _db(app).execute("DELETE FROM login_attempts WHERE username = ?",
                     (username,))
    _db(app).commit()


# ----------------------------------------------------------------------
# Trenches list (coins page): fresh pump.fun / PumpSwap tokens from
# the DexScreener feed, cached two minutes so the coins page does not
# hit the feed on every render. A feed failure returns an error and
# the page says the trenches are unavailable right now — never fake
# rows. The cache is module-level so tests can reset it. The scan
# is DISCOVERY only: its DexScreener prices never reach the page —
# _reprice_trenches_rows rewrites every row onto the single source
# (feed tick, else one batched Helius chain read) before it is
# returned, and gives each row its own 1h % and 1h sparkline from
# the same points. A row neither price source can price is dropped
# on the page render (drop_unpriced=True) and kept reading
# "no data" on a live list refresh (drop_unpriced=False).
# ----------------------------------------------------------------------
_TRENCHES_CACHE: dict = {"at": 0.0, "rows": None, "error": None}


def _reprice_trenches_rows(app, rows: list,
                           drop_unpriced: bool = True) -> list:
    """Rewrite trenches rows onto the single price source, per
    mint (never a shared value): each row's price_usd becomes the
    feed's fresh tick, else one batched Helius chain read for the
    rows the feed cannot price (the no-break-rule-4 backup path,
    so a Helius stall cannot blank the page). Each row also
    carries its own 1h % AND its own 1h sparkline, both from the
    SAME points (GeckoTerminal OHLCV first, then the feed's
    last-hour ticks) — so the % and the chart can never disagree,
    and a fresh coin with no history shows "—" / no chart rather
    than a made-up number (the old change_over path could never
    move for a fresh coin: it needs an hour of stream history).
    A row neither source can price is dropped on the PAGE render
    (unpriced means no trade, never a guess), while a LIVE LIST
    refresh keeps it reading "no data" so a stall cannot yank
    rows off the page. Returns new dicts; the cached scan rows
    are never mutated."""
    if not rows:
        return []
    feed = app.config.get("LIVE_FEED") if app is not None else None
    priced: dict = {}
    uncovered = []
    for row in rows:
        price = None
        if feed is not None:
            try:
                price = feed.live_price(row["mint"])
            except Exception:
                price = None
        if price is not None and price > 0:
            priced[row["mint"]] = price
        else:
            uncovered.append(row["mint"])
    if uncovered and app is not None:
        priced.update(_chain_prices_for_mints(app, uncovered))
    out = []
    for row in rows:
        price = priced.get(row["mint"])
        if price is None or price <= 0:
            if drop_unpriced:
                continue
            price = None
        # The 1h % and the 1h chart come from the SAME points.
        try:
            points = sparkline_points_1h(row["mint"], is_mint=True,
                                         feed=feed)
        except Exception:
            points = []
        out.append({**row,
                    "price_usd": (float(price) if price is not None
                                  else None),
                    "price_display": fmt_price_sig(price),
                    "change_1h_pct": change_pct_from_points(points),
                    "sparkline_svg": sparkline_svg(points)})
    return out


def _set_server_watched(app, rows) -> None:
    """Tell the live feed to watch the current trenches rows, so
    the Helius stream subscribes them and ticks them live even
    when no browser is watching (see
    LiveFeed.set_server_watched). Best-effort: a missing feed or
    a failure never breaks the page render — the poll-path chain
    read still prices the rows."""
    try:
        feed = app.config.get("LIVE_FEED") if app is not None else None
        setter = getattr(feed, "set_server_watched", None)
        if callable(setter):
            setter([r.get("mint") for r in (rows or [])
                    if r.get("mint")])
    except Exception:
        pass


def _trenches_list(app=None, drop_unpriced: bool = True) -> tuple[list, str | None]:
    """Top trenches rows: [{mint, symbol, name, price_usd,
    change_1h_pct, change_5m_pct, price_display, sparkline_svg}],
    best 5-minute gain first, at most 10. Returns (rows, error).
    The scan's order is its own 5-minute-gain ranking (discovery
    order); the prices, 1h changes and sparklines on the rows are
    the single source (see _reprice_trenches_rows). Without `app`
    the raw scan rows are returned (name/symbol lookup only —
    _trenches_entry never shows a price). drop_unpriced=False
    keeps a row neither source can price (reading "no data") —
    the live list refresh uses that, so a stall cannot yank rows
    off the page; the page render drops unpriced rows."""
    now = time.time()
    if (_TRENCHES_CACHE["rows"] is not None
            and now - _TRENCHES_CACHE["at"] < 120):
        cached = _TRENCHES_CACHE["rows"]
        if app is None:
            return cached, _TRENCHES_CACHE["error"]
        _set_server_watched(app, cached)
        return (_reprice_trenches_rows(app, cached,
                                       drop_unpriced=drop_unpriced),
                _TRENCHES_CACHE["error"])
    try:
        scan = fetch_trenches_scan()
    except Exception as exc:
        scan = {"candidates": [], "error": str(exc)[:160]}
    if scan.get("error"):
        # The scan is the ONLY discovery path for the Trenches
        # list: a failure here is never silent — it is the reason
        # the page shows "unavailable" instead of coins.
        log.warning("[trenches-feed] scan failed: %s", scan["error"])
    rows = []
    for entry in scan.get("candidates") or []:
        rows.append({
            "mint": entry["mint"],
            "symbol": entry["symbol"],
            "name": entry.get("name") or entry["symbol"],
            "price_usd": entry["price_usd"],
            "change_1h_pct": entry.get("change_1h_pct"),
            "change_5m_pct": entry.get("change_5m_pct"),
        })
    # Rank by the 5-minute gain — what is moving NOW — not the
    # 1-hour gain: a coin that pumped hours ago kept a big 1h
    # figure and held its slot for an hour (2026-11-26 live data:
    # a coin at +488% 1h with a +0.6% 5m sat #2), so fresh coins
    # that had not pumped yet never appeared. The bot buys on the
    # same 5-minute gain (TRENCHES_RULES: gain_5m_pct, 3.4%), so
    # the page and the bot now agree on what is hot. A coin with
    # no 5-minute figure (no trades in 5 minutes) sorts last.
    rows.sort(key=lambda r: (
        r["change_5m_pct"] is None,
        -(r["change_5m_pct"] or 0.0)))
    rows = rows[:10]
    error = scan.get("error")
    if not rows and not error:
        log.warning("[trenches-feed] scan returned zero candidates "
                    "with no error — the list is empty, not broken")
    _TRENCHES_CACHE["at"] = now
    _TRENCHES_CACHE["rows"] = rows
    _TRENCHES_CACHE["error"] = error
    if app is None:
        return rows, error
    _set_server_watched(app, rows)
    return _reprice_trenches_rows(app, rows,
                                 drop_unpriced=drop_unpriced), error


def _trenches_entry(mint: str) -> dict | None:
    """The trenches-list entry for a mint, if the feed has it — so a
    trenches token opened from the list shows its real name instead
    of a truncated address."""
    try:
        rows, _err = _trenches_list()
    except Exception:
        return None
    return next((r for r in rows if r["mint"] == mint), None)


def _unlisted_coin_row(coin_id_resolved: str, price, prices: dict,
                       db=None, rpc_url: str | None = None) -> dict:
    """The coin row for a coin that is not in the user's list (no
    holding): a trenches token shows its feed name/symbol, anything
    else its stored coin_meta / on-chain metadata name — and when
    nothing names it, "Unknown coin" with an empty ticker. A mint
    address is never shown as a name and the old "Pump.fun token
    …" label is gone."""
    trench = _trenches_entry(coin_id_resolved)
    if trench is not None:
        name, ticker = trench["name"], trench["symbol"]
    else:
        name = ticker = None
        if db is not None and is_valid_address(coin_id_resolved):
            try:
                meta = ensure_coin_meta(db, coin_id_resolved,
                                        rpc_url=rpc_url)
            except Exception:
                meta = None
            if meta:
                name = meta.get("name")
                ticker = meta.get("symbol")
        if name is None and ticker is None:
            # Last resort: the live feed's identity for this mint.
            # This matters for graduated coins — ensure_coin_meta's
            # DexScreener fallback only reads pump.fun pairs, while
            # fetch_mint_identities also reads the Raydium pair, so
            # without this an unheld graduated coin showed
            # "Unknown coin" even though the feed knows its name.
            try:
                identities = fetch_mint_identities(
                    [coin_id_resolved])
            except Exception:
                identities = {}
            info = identities.get(coin_id_resolved) or {}
            name = info.get("name")
            ticker = info.get("symbol")
        if name is None and ticker is None:
            name, ticker = "Unknown coin", ""
    return {"coin_id": coin_id_resolved, "name": name,
            "ticker": (ticker or "").upper(),
            "price": price, "held": 0.0, "avg_price": None,
            "change_24h": prices["prices"].get(
                coin_id_resolved, {}).get("change_24h"),
            "pnl": None, "is_mint": is_valid_address(coin_id_resolved)}


def _token_ticker(mint: str, db=None,
                    rpc_url: str | None = None) -> str | None:
    """The display ticker for a mint: the feed's real symbol when
    DexScreener names it, else the stored coin_meta / on-chain
    metadata symbol — else None, never a truncated address and
    never a fabricated name. Callers show the coin's name or
    "this token" when this is None. SOL is the native currency:
    its ticker is SOL by definition, never a guess."""
    if mint == "SOL":
        return "SOL"
    try:
        identities = fetch_mint_identities([mint])
    except Exception:
        identities = {}
    info = identities.get(mint) or {}
    symbol = info.get("symbol")
    if symbol:
        return str(symbol)
    if db is not None:
        try:
            _name, stored_symbol, _image = stored_display(db, mint)
        except Exception:
            stored_symbol = None
        if stored_symbol:
            return str(stored_symbol)
        try:
            meta = ensure_coin_meta(db, mint, rpc_url=rpc_url)
        except Exception:
            meta = None
        if meta and meta.get("symbol"):
            return str(meta["symbol"])
    return None


def _token_send_fee_note(app, wallet_pubkey: str, intent) -> str:
    """The fee line for a token send's confirm screen: the network
    fee, plus the token-account rent when the recipient has no
    token account for the mint yet. Best effort — when the chain
    cannot be checked, the note says the rent may apply instead
    of guessing a number."""
    try:
        client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
        inspection = token_send_mod.inspect_token(
            client, wallet_pubkey, intent["mint"], intent["destination"])
    except Exception:
        return ("Network fee + token account rent if needed, paid "
                "from your SOL.")
    if inspection["create_ata"]:
        total = (token_send_mod.TX_FEE_LAMPORTS
                 + inspection["rent_lamports"])
        return (f"≈ {lamports_to_sol(total):.5f} SOL (network fee + "
                "token account rent, estimated)")
    return "≈ 0.000005 SOL (network fee, estimated)"


def _confirm_token_send(app, user, intent, return_to, return_coin_id):
    """The confirm step for a token send intent: re-check the
    on-chain balance, build + sign + send the token transaction,
    and record the outcome. The daily SOL cap does not apply —
    no SOL value is sent, only the network fee and the token
    account rent, both paid from the app wallet's SOL. Returns
    the redirect response, as the SOL confirm does."""
    db = _db(app)
    mint = intent["mint"]
    ticker = intent["token"]
    amount_base_units = int(intent["amount_base_units"])
    ui_amount = token_send_mod.format_token_amount(
        amount_base_units, int(intent["decimals"]))

    def _fail(reason: str):
        db.execute(
            "UPDATE send_intents SET status = 'failed', error = ? "
            "WHERE id = ?", (reason[:200], intent["id"]))
        db.commit()
        _log(app, user["id"], "send_failed",
             f"Token send failed: {reason}"[:120], "real")
        flash("Send failed: " + reason[:200])

    def _done():
        if return_coin_id:
            return redirect(url_for("coin_detail", coin_id=return_coin_id))
        return redirect(url_for("dashboard" if return_to else "wallet"))

    wallet = _wallet(app, user["id"])
    if not wallet:
        _fail("No app wallet.")
        return _done()
    client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
    try:
        inspection = token_send_mod.inspect_token(
            client, wallet["pubkey"], mint, intent["destination"])
    except token_send_mod.TokenSendRefusal as exc:
        _fail(str(exc))
        return _done()
    if amount_base_units > inspection["balance"]:
        _fail("Amount is more than you hold of this token now.")
        return _done()
    required_sol = (token_send_mod.TX_FEE_LAMPORTS
                    + inspection["rent_lamports"])
    try:
        sol_balance = client.get_balance_lamports(wallet["pubkey"])
    except Exception:
        _fail("Could not check your SOL balance right now — try again.")
        return _done()
    if sol_balance < required_sol:
        _fail("Not enough SOL for the network fee.")
        return _done()
    try:
        instructions = token_send_mod.build_token_send_instructions(
            inspection, amount_base_units)
    except token_send_mod.TokenSendRefusal as exc:
        _fail(str(exc))
        return _done()
    try:
        # The key is decrypted into a mutable buffer, used for one
        # signature, and wiped IN PLACE the moment signing is done —
        # before the transaction is even sent, as in the SOL send.
        # Decrypted through the password+email user-key layer, so
        # v1 and v2 wallets both work here.
        with temporary_wallet_secret(_db(app), user["id"],
                                     _cfg(app).encryption_key) as secret:
            if secret is None:
                raise ValueError("No app wallet.")
            seed = bytearray(secret[:32])
            try:
                blockhash = client.get_latest_blockhash()
                signed_b64, signature = token_send_mod.sign_token_send(
                    seed, wallet["pubkey"], instructions, blockhash)
            finally:
                wipe_secret(seed)
    except Exception as exc:
        _fail(str(exc)[:200])
        return _done()
    tx_sig, reason = send_and_confirm(client, signed_b64, signature)
    if tx_sig is None:
        _fail(reason or "transaction not confirmed")
        return _done()
    db.execute(
        "UPDATE send_intents SET status = 'sent', signature = ? "
        "WHERE id = ?", (tx_sig, intent["id"]))
    db.commit()
    _remove_real_holding(db, user["id"], mint, float(ui_amount))
    _log(app, user["id"], "send",
         f"Sent {ui_amount} {ticker} to {intent['destination'][:8]}... "
         f"signature {tx_sig[:12]}...", "real")
    flash("Send submitted. Signature: " + tx_sig)
    return _done()


def _create_account_from_pending(db, app, pending) -> int | None:
    """Create a user account from a pending signup row. The row is
    staged by signup() and consumed here in the same request —
    there is no email-code step any more. Returns the new user id,
    or None when the username was taken in the meantime (the
    pending row is deleted either way).

    Creates the user (v2 password hash bound to the email), the
    app wallet encrypted with the password+email user key, the
    signing bot, and the paper portfolio. The caller logs the
    signup / override and rotates the session."""
    username = pending["username"]
    email = pending["email"]
    if db.execute("SELECT id FROM users WHERE username = ?",
                  (username,)).fetchone() or db.execute(
            "SELECT id FROM users WHERE LOWER(email) = LOWER(?) "
            "AND email != ''", (email,)).fetchone():
        db.execute("DELETE FROM pending_signups WHERE id = ?",
                   (pending["id"],))
        db.commit()
        return None
    cur = db.execute(
        "INSERT INTO users (username, password_hash, salt, "
        "pw_version, email, recovery_question, "
        "recovery_answer_hash, recovery_answer_salt, "
        "recovery_hint, user_key_salt, wrapped_user_key) "
        "VALUES (?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?)",
        (username, pending["password_hash"], pending["salt"],
         email, pending["recovery_question"],
         pending["recovery_answer_hash"],
         pending["recovery_answer_salt"], pending["recovery_hint"],
         pending["user_key_salt"], pending["wrapped_user_key"]))
    user_id = cur.lastrowid
    # Fresh app wallet — generated here, encrypted with the
    # password+email user key (unwrapped with the server key),
    # never shown in full and never logged. The seed and secret
    # are wiped IN PLACE the moment the encrypted blob exists,
    # even if the insert fails.
    user_key = None
    seed, pubkey, secret64 = generate_keypair()
    try:
        user_key = wallet_keys_mod.unwrap_user_key(
            pending["wrapped_user_key"], _cfg(app).encryption_key)
        blob = encrypt_secret(secret64, bytes(user_key))
        db.execute(
            "INSERT INTO wallets (user_id, pubkey, "
            "encrypted_secret, enc_version) VALUES (?, ?, ?, 2)",
            (user_id, pubkey, blob))
    finally:
        wipe_secret(seed)
        wipe_secret(secret64)
        if user_key is not None:
            wipe_secret(user_key)
    # The app wallet IS the user's pump.fun trading wallet:
    # pump.fun has no separate accounts, so trading on pump.fun
    # uses this linked Solana wallet.
    db.execute(
        "UPDATE users SET pumpfun_wallet = ? WHERE id = ?",
        (pubkey, user_id))
    # Every account gets ONE dedicated signing bot (with its
    # immutable 8-digit support ID) at signup.
    signing_bot_mod.ensure_signing_bot(db, user_id)
    db.execute(
        "INSERT INTO paper_portfolio (user_id, cash_usd) "
        "VALUES (?, ?)", (user_id, _cfg(app).paper_start_cash))
    db.execute("DELETE FROM pending_signups WHERE id = ?",
               (pending["id"],))
    db.commit()
    return user_id


def _set_account_password(db, cfg, user_id: int, new_password: str) -> bool:
    """Set a user's password to `new_password`, bound to the
    account's email (v2), and re-encrypt the wallet key with the
    new password+email-derived user key.

    When the account has an email, the password hash is the v2
    password+email hash and the wallet is re-encrypted with the
    new user key (enc_version 2). When the account has no email
    (an account from before email was collected), the password
    stays on the legacy v1 hash and the wallet stays as it is —
    the v2 layer needs an email to derive from. The user key and
    any decrypted wallet secret are wiped in place.

    Returns True when the password was set. Used by the
    forgot-password reset, the Recover Account reset, and the
    admin override — every path that changes a password goes
    through here, so the wallet key always follows the current
    password+email."""
    row = db.execute(
        "SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return False
    email = (row["email"] or "").strip()
    if not email:
        salt, pw_hash = hash_password(new_password)
        db.execute(
            "UPDATE users SET password_hash = ?, salt = ?, "
            "pw_version = 1 WHERE id = ?",
            (pw_hash, salt, user_id))
        db.commit()
        return True
    salt, pw_hash = hash_account_password(new_password, email)
    user_key_salt = wallet_keys_mod.new_user_key_salt()
    user_key = None
    try:
        user_key = wallet_keys_mod.derive_user_key(
            new_password, email, user_key_salt)
        db.execute(
            "UPDATE users SET password_hash = ?, salt = ?, "
            "pw_version = 2, user_key_salt = ? WHERE id = ?",
            (pw_hash, salt, user_key_salt, user_id))
        db.commit()
        wallet_keys_mod.rewrap_wallet_to_user_key(
            db, user_id, cfg.encryption_key, bytes(user_key))
    finally:
        if user_key is not None:
            wipe_secret(user_key)
    return True


def _upgrade_account_to_v2(db, cfg, user_row, password: str) -> bool:
    """Transparent upgrade for a legacy (v1) account at login: the
    password is available, so re-hash it bound to the email (v2)
    and re-encrypt the wallet with the password+email-derived
    user key. No-op when the account is already v2 or has no
    email. Returns True when an upgrade happened."""
    if int(user_row["pw_version"] or 1) == 2:
        return False
    email = (user_row["email"] or "").strip()
    if not email:
        return False
    return _set_account_password(db, cfg, int(user_row["id"]), password)


def _sell_prefill_for_row(row, sol_price_usd):
    """SOL value of a held coin row + the sell-form prefill: the
    holding's value floored to 6 decimals, capped at 0.1 SOL.
    Returns (held_sol_value, prefill) or (None, None) when the row
    holds nothing or has no live price — the form then keeps its
    0.1 placeholder. The floor matters: rounding UP could prefill
    more than the holding is worth, and the sell would refuse with
    "insufficient token balance"."""
    try:
        held = float(row["held"] or 0)
        price = float(row["price"] or 0)
        sol = float(sol_price_usd or 0)
    except (KeyError, TypeError, ValueError):
        return None, None
    if held <= 0 or price <= 0 or sol <= 0:
        return None, None
    held_sol = int(held * price / sol * 1_000_000) / 1_000_000
    if held_sol <= 0:
        return None, None
    return held_sol, min(held_sol, 0.1)


def register_routes(app: Flask) -> None:
    login_required = _login_required(app)

    # POST-burst guard: no single address may hammer the sensitive
    # endpoints (login, signup, sends, bot creation, trades). A
    # burst past the limit is a plain 429 — the action never runs.
    _BURST_ENDPOINTS = {
        "login", "signup", "wallet_send",
        "wallet_send_confirm", "bot_create", "bot_preset_trenches",
        "coin_buy", "coin_sell",
    }

    @app.before_request
    def _post_burst_guard():
        if request.method != "POST" or not request.endpoint:
            return None
        if request.endpoint not in _BURST_ENDPOINTS:
            return None
        if not rate_limit_mod.record_hit(
                _db(app), f"burst:ip:{_client_ip()}",
                MAX_POST_BURST_PER_IP, POST_BURST_WINDOW_SECONDS):
            abort(429)
        return None

    @app.context_processor
    def inject_globals():
        user = _current_user(app)
        header_balance = _header_balance(app, user) if user else None
        return {"current_user": user, "csrf_token": _new_csrf(),
                "network": _cfg(app).network,
                # The real network's display name ("Mainnet" now
                # that the app is on mainnet) and the real-mode
                # label ("Real trading — mainnet"). The stored mode
                # value stays 'devnet' for real mode (internal);
                # only the words the user sees are network-aware.
                "real_network_name": (
                    "Mainnet" if _cfg(app).network == "mainnet"
                    else "Devnet"),
                "real_mode_label": _mode_label("devnet", _cfg(app)),
                "header_balance": header_balance,
                "header_mode": header_balance["mode"]
                if header_balance else "paper",
                # $GATE fuel (2026-11-23): trades left from the
                # package ledger, in the topbar on every page. The
                # number is the ledger truth (see
                # _gate_fuel_trades_left); the Buy $GATE popup and
                # the GATEkeeper card read the same gate_fuel
                # status, bundles, logo and pump.fun link below.
                # None in paper mode — the template shows
                # "free ~ ∞" there instead.
                "gate_fuel_trades_left": (
                    _gate_fuel_trades_left(app, user) if user else None),
                "gate_fuel": (
                    gate_fuel_mod.gatekeeper_status(_db(app), user["id"])
                    if user else None),
                "gate_bundles": gate_fuel_mod.bundles(),
                "gate_quote_url": url_for("api_gate_quote"),
                "gate_logo_url": gate_fuel_mod.get_gate_logo_url(
                    gate_fuel_mod.gate_mint(_cfg(app))),
                "gate_pumpfun_url": gate_fuel_mod.pumpfun_url(
                    gate_fuel_mod.gate_mint(_cfg(app))),
                # Page ID: the page's number in the top-left corner
                # (see page_numbers.py — the single source of truth).
                # None on popups, confirm screens and API/action
                # endpoints, where no label renders.
                "page_number": page_number_for(request.endpoint),
                # The page picker: every numbered page, in order,
                # from the same single source of truth.
                "page_list": page_list(),
                # The post-sign-in Demo / Real chooser: shown on any
                # logged-in page until the user picks (the wallet is
                # the landing page, so that is where it appears
                # first). The choice sticks for the sign-in: it is
                # remembered in the session AND in the database, so
                # a dropped session flag alone can never make the
                # chooser reappear mid-session. Signing in clears
                # the database flag, so the chooser shows once
                # after the next sign-in.
                "show_mode_chooser": user is not None
                and not session.get("mode_chosen")
                and not _mode_chosen_in_db(_db(app), user["id"]),
                # Cloudflare Turnstile site key, or '' when the
                # challenge is not configured (then no widget renders
                # and login/signup run without it).
                "turnstile_site_key": _cfg(app).turnstile_site_key}

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "network": _cfg(app).network})

    # ------------------------------------------------------------------
    # PWA (Android installable app)
    # ------------------------------------------------------------------
    # The manifest and service worker must be served from the app
    # root (not /static/) so the worker controls the whole app scope
    # and Chrome offers "Install app". Both are plain static files;
    # the routes just expose them at the root.
    @app.route("/manifest.json")
    def manifest():
        return app.send_static_file("manifest.json")

    @app.route("/sw.js")
    def service_worker():
        # The service worker IS the update mechanism for the whole
        # app shell: it must never be served stale. Flask's static
        # default (12h max-age) let a phone run an old worker for
        # up to ~24h after a deploy, and the old worker kept
        # serving its cached shell. No-store, always fresh.
        resp = make_response(app.send_static_file("sw.js"))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/")
    def index():
        # No landing page: the login screen is the front door.
        # Logged-in users go straight to the wallet homepage.
        if _current_user(app) is not None:
            return redirect(url_for("wallet"))
        return redirect(url_for("login"))

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "POST":
            _csrf_check()
            # Anti-bot: a filled honeypot field or a failed
            # Turnstile challenge is a plain refusal, same as any
            # other invalid signup — a bot learns nothing.
            if _honeypot_filled() or not _turnstile_ok(app):
                flash("Something looks wrong with this signup. "
                      "Please try again.")
                return redirect(url_for("signup"))
            if not rate_limit_mod.record_hit(
                    _db(app), f"signup:ip:{_client_ip()}",
                    MAX_SIGNUPS_PER_IP, SIGNUP_IP_WINDOW_SECONDS):
                flash("Too many signups from this address. "
                      "Please try again later.")
                return redirect(url_for("signup"))
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
                flash("Username must be 3-20 letters, numbers, or _.")
                return redirect(url_for("signup"))
            if len(password) < 8:
                flash("Password must be at least 8 characters.")
                return redirect(url_for("signup"))
            # No email is collected at signup any more (owner,
            # 2026-11-20): the emailed confirmation codes are gone,
            # and so is the Email field. New accounts store
            # email = '' — the v2 password hash and wallet key are
            # still derived through the same password+email code,
            # with the email factor empty. Existing accounts keep
            # their stored email (it is the crypto input for their
            # login + wallet) — never blank it. The recovery
            # question/answer/hint (same bounds as the Settings
            # recovery form) are the only account recovery now: a
            # question without an answer (or the reverse) is refused.
            email = ""
            question = (request.form.get("question") or "").strip()
            answer = (request.form.get("answer") or "").strip()
            hint = (request.form.get("hint") or "").strip()
            if question or answer:
                if not 3 <= len(question) <= 200:
                    flash("The security question must be 3-200 "
                          "characters.")
                    return redirect(url_for("signup"))
                if not answer or len(answer) > 200:
                    flash("The security answer must not be empty "
                          "(max 200 characters).")
                    return redirect(url_for("signup"))
            if len(hint) > 200:
                flash("The hint must be at most 200 characters.")
                return redirect(url_for("signup"))
            db = _db(app)
            if db.execute("SELECT id FROM users WHERE username = ?",
                          (username,)).fetchone():
                flash("That username is taken.")
                return redirect(url_for("signup"))
            # The account is created directly — no email code step.
            # The password hash is bound to the (empty) email (v2)
            # and the user key (password+email) is derived now —
            # while the plaintext password is available — and
            # stored wrapped with the server key, so the wallet can
            # be created without ever storing the password. The
            # signup is staged as a pending row only so the shared
            # account-creation helper can consume it in this same
            # request (exactly the path the old admin override
            # used); the row is deleted by that helper.
            salt, pw_hash = hash_account_password(password, email)
            ans_salt, ans_hash = ("", "")
            if question and answer:
                ans_salt, ans_hash = hash_password(answer)
            user_key_salt = wallet_keys_mod.new_user_key_salt()
            user_key = wallet_keys_mod.derive_user_key(
                password, email, user_key_salt)
            try:
                wrapped_user_key = wallet_keys_mod.wrap_user_key(
                    user_key, _cfg(app).encryption_key)
            finally:
                wipe_secret(user_key)
            # A re-submit starts fresh.
            db.execute("DELETE FROM pending_signups WHERE username = ?",
                       (username,))
            cur = db.execute(
                "INSERT INTO pending_signups (username, password_hash, "
                "salt, pw_version, email, recovery_question, "
                "recovery_answer_hash, recovery_answer_salt, "
                "recovery_hint, user_key_salt, wrapped_user_key, "
                "created_at) VALUES (?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, pw_hash, salt, email, question, ans_hash,
                 ans_salt, hint, user_key_salt, wrapped_user_key,
                 int(time.time())))
            pending_id = cur.lastrowid
            db.commit()
            pending = db.execute(
                "SELECT * FROM pending_signups WHERE id = ?",
                (pending_id,)).fetchone()
            user_id = _create_account_from_pending(db, app, pending)
            if user_id is None:
                flash("That username was just taken. "
                      "Please start again.")
                return redirect(url_for("signup"))
            _log(app, user_id, "signup",
                 "Account created; app wallet generated.")
            _rotate_session(user_id)
            # Fresh sign-in: the mode chooser shows once, then the
            # answer is remembered (see _clear_mode_chosen).
            _clear_mode_chosen(_db(app), user_id)
            flash("Welcome! Your app wallet was created for you.")
            return redirect(url_for("wallet"))
        return render_template("auth.html", mode="signup")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            _csrf_check()
            # Anti-bot: honeypot / Turnstile first, same plain refusal
            # as a wrong password.
            if _honeypot_filled() or not _turnstile_ok(app):
                flash("Something looks wrong with this login. "
                      "Please try again.")
                return redirect(url_for("login"))
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            ip = _client_ip()
            if _check_login_lock(app, username):
                flash("Too many failed tries. Locked for 15 minutes.")
                return redirect(url_for("login"))
            # Per-IP failed-login limit (failures only — successful
            # logins never count against it).
            if rate_limit_mod.hits_in_window(
                    _db(app), f"login-fail:ip:{ip}",
                    LOGIN_FAIL_IP_WINDOW_SECONDS) >= \
                    MAX_LOGIN_FAILS_PER_IP:
                flash("Too many failed tries from this address. "
                      "Please try again later.")
                return redirect(url_for("login"))
            row = _db(app).execute(
                "SELECT id, password_hash, salt, pw_version, email, "
                "user_key_salt, wrapped_user_key FROM users "
                "WHERE username = ?",
                (username,)).fetchone()
            # Password check is version-aware: v2 hashes are bound
            # to the account email (password+email), v1 hashes are
            # the legacy password-only hash. A correct v1 login
            # upgrades the account transparently to v2 (the
            # password is available, so the user key can be
            # derived and the wallet re-encrypted).
            password_ok = False
            if row:
                if int(row["pw_version"] or 1) == 2:
                    password_ok = verify_account_password(
                        password, row["email"] or "", row["salt"],
                        row["password_hash"])
                else:
                    password_ok = verify_password(
                        password, row["salt"], row["password_hash"])
            if not row or not password_ok:
                _record_login_fail(app, username)
                rate_limit_mod.record_hit(
                    _db(app), f"login-fail:ip:{ip}",
                    MAX_LOGIN_FAILS_PER_IP, LOGIN_FAIL_IP_WINDOW_SECONDS)
                _record_login_event(app, username, False)
                # Anomaly: repeated failures from addresses this
                # account has never logged in from locks the account
                # and writes a suspicious-activity audit row.
                _anomaly_lock(app, username)
                flash("Wrong username or password.")
                return redirect(url_for("login"))
            _clear_login_fails(app, username)
            rate_limit_mod.reset_key(
                _db(app), f"login-fail:ip:{ip}")
            _record_login_event(app, username, True)
            if int(row["pw_version"] or 1) != 2:
                _upgrade_account_to_v2(_db(app), _cfg(app), row, password)
            _rotate_session(row["id"])
            # Fresh sign-in: the mode chooser shows once, then the
            # answer is remembered (see _clear_mode_chosen).
            _clear_mode_chosen(_db(app), row["id"])
            return redirect(url_for("wallet"))
        # SolEntry login page: fox header + donate card. The donate
        # card shows only the MASKED recipient (first 5 characters +
        # '*****'); the full address never reaches a template —
        # the donor's browser fetches it from /api/donate/info
        # when it builds the transfer. When the recipient is not
        # configured the card says so plainly. The username field
        # takes the account username (per the fox design); no email
        # is collected at sign-up any more, and the username is
        # never an email address.
        try:
            donation_masked = donations_mod.mask_address(
                donations_mod.get_donation_recipient(_cfg(app)))
        except ValueError:
            donation_masked = ""
        return render_template(
            "login.html",
            donation_address_masked=donation_masked,
            rpc_url=_cfg(app).rpc_url)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------
    # Account settings: recovery question + passkeys
    # Account recovery: forgot password, recovery-question answer
    # ------------------------------------------------------------------
    def _settings_page(app, user, wallet_key=None):
        """Render Settings. wallet_key is only ever passed by the
        export route after a password check — the GET never has it.
        A response carrying the key is marked no-store so the key
        never sits in a browser or proxy cache."""
        row = _db(app).execute(
            "SELECT username, recovery_question, recovery_hint, "
            "recovery_answer_hash FROM users WHERE id = ?",
            (user["id"],)).fetchone()
        passkeys = _db(app).execute(
            "SELECT id, credential_id, transports, created_at "
            "FROM passkey_credentials WHERE user_id = ? ORDER BY id DESC",
            (user["id"],)).fetchall()
        signing_bot = signing_bot_mod.get_signing_bot(_db(app),
                                                      user["id"])
        resp = make_response(render_template(
            "settings.html",
            username=row["username"],
            has_recovery=bool(row["recovery_answer_hash"]),
            recovery_question=row["recovery_question"],
            recovery_hint=row["recovery_hint"],
            passkeys=passkeys,
            support_id=signing_bot["support_id"] if signing_bot else "",
            wallet_key=wallet_key or ""))
        if wallet_key:
            resp.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate")
            resp.headers["Pragma"] = "no-cache"
        return resp

    @app.route("/settings")
    @login_required
    def settings():
        return _settings_page(app, _current_user(app))

    @app.route("/settings/wallet-export", methods=["POST"])
    @login_required
    def settings_wallet_export():
        """Show the owner's own wallet private key, once, after they
        re-enter their password. The key leaves the server ONLY
        here: the logged-in owner, for their OWN wallet only. No
        user id is ever accepted, so nobody can ask for another
        account's key — and admin oversight never reaches this
        route (it shows no keys, ever). The key is rendered once,
        never logged, never stored, and the response is marked
        no-store. A wrong password counts toward the same login
        lockout as a wrong login, so the gate cannot be
        brute-forced. App wallets have no seed phrase — the
        private key (base58, the form Phantom imports) IS the
        backup."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        row = db.execute(
            "SELECT username, password_hash, salt, pw_version, email "
            "FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row is None:
            return redirect(url_for("settings"))
        if _check_login_lock(app, row["username"]):
            flash("Too many failed tries. Locked for 15 minutes.")
            return redirect(url_for("settings"))
        password = request.form.get("password") or ""
        if int(row["pw_version"] or 1) == 2:
            password_ok = verify_account_password(
                password, row["email"] or "", row["salt"],
                row["password_hash"])
        else:
            password_ok = verify_password(
                password, row["salt"], row["password_hash"])
        if not password_ok:
            _record_login_fail(app, row["username"])
            flash("Wrong password.")
            return redirect(url_for("settings"))
        _clear_login_fails(app, row["username"])
        wallet_key = None
        with temporary_wallet_secret(
                db, user["id"], _cfg(app).encryption_key) as secret:
            if secret is None:
                flash("No app wallet on this account.")
                return redirect(url_for("settings"))
            wallet_key = base58_encode(bytes(secret))
        _log(app, user["id"], "wallet_export",
             "Wallet key shown in Settings (password confirmed).")
        return _settings_page(app, user, wallet_key=wallet_key)

    @app.route("/settings/promo-code", methods=["POST"])
    @login_required
    def settings_promo_code():
        """Redeem a one-time promo code from the coupon box in
        Settings (James, 2026-11-24). A code works at most
        max_uses times IN TOTAL across all users — BetaGate1994
        is one use, first redeemer only. Unknown and already-used
        codes get the SAME generic error, so the response never
        hints at which codes exist. On success the flash shows
        the new trades-left balance (the header chip reads the
        same ledger number on the redirected page)."""
        _csrf_check()
        user = _current_user(app)
        code = request.form.get("code") or ""
        result = gate_fuel_mod.redeem_promo_code(_db(app), user["id"],
                                                 code)
        if result is None:
            flash("Invalid or already used code.")
        else:
            flash(
                f"Promo code accepted — {result['trades']} trades "
                f"added. {result['trades_remaining']} trades left.")
        return redirect(url_for("settings"))

    @app.route("/settings/recovery-question", methods=["POST"])
    @login_required
    def settings_recovery_question():
        """Set (or replace) the recovery question + answer + hint.

        The answer is hashed with scrypt, like a password — never
        stored in plaintext, never logged. The question and hint are
        shown on the forgot-password page, so they must not give the
        answer away."""
        _csrf_check()
        user = _current_user(app)
        question = (request.form.get("question") or "").strip()
        answer = (request.form.get("answer") or "").strip()
        hint = (request.form.get("hint") or "").strip()
        if not 3 <= len(question) <= 200:
            flash("The question must be 3-200 characters.")
            return redirect(url_for("settings"))
        if not answer or len(answer) > 200:
            flash("The answer must not be empty (max 200 characters).")
            return redirect(url_for("settings"))
        if len(hint) > 200:
            flash("The hint must be at most 200 characters.")
            return redirect(url_for("settings"))
        salt, answer_hash = hash_password(answer)
        _db(app).execute(
            "UPDATE users SET recovery_question = ?, "
            "recovery_answer_hash = ?, recovery_answer_salt = ?, "
            "recovery_hint = ? WHERE id = ?",
            (question, answer_hash, salt, hint, user["id"]))
        _db(app).commit()
        _log(app, user["id"], "recovery_question",
             "Recovery question set.")
        flash("Recovery question saved. You can use it on the "
              "forgot-password page if you ever get locked out.")
        return redirect(url_for("settings"))

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            _csrf_check()
            username = (request.form.get("username") or "").strip()
            # One generic message covers every not-ready case (no such
            # user, no question set) so the page never reveals whether
            # the account exists or has recovery set up.
            not_ready = ("Account recovery is not set up for this "
                         "account.")
            if _check_login_lock(app, username):
                flash("Too many failed tries. Locked for 15 minutes.")
                return redirect(url_for("login"))
            row = _db(app).execute(
                "SELECT id, recovery_question FROM users WHERE username = ?",
                (username,)).fetchone()
            if not row or not row["recovery_question"]:
                flash(not_ready)
                return redirect(url_for("forgot_password"))
            session["recovery_user_id"] = row["id"]
            return redirect(url_for("forgot_answer"))
        return render_template("forgot_password.html")

    @app.route("/forgot-password/answer", methods=["GET", "POST"])
    def forgot_answer():
        user_id = session.get("recovery_user_id")
        if not user_id:
            return redirect(url_for("forgot_password"))
        row = _db(app).execute(
            "SELECT username, recovery_question, recovery_hint, "
            "recovery_answer_hash, recovery_answer_salt FROM users "
            "WHERE id = ?", (user_id,)).fetchone()
        if not row or not row["recovery_question"]:
            session.pop("recovery_user_id", None)
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            _csrf_check()
            if _check_login_lock(app, row["username"]):
                flash("Too many failed tries. Locked for 15 minutes.")
                return redirect(url_for("login"))
            answer = request.form.get("answer") or ""
            if not verify_password(answer, row["recovery_answer_salt"],
                                   row["recovery_answer_hash"]):
                _record_login_fail(app, row["username"])
                flash("That answer is wrong.")
                return redirect(url_for("forgot_answer"))
            _clear_login_fails(app, row["username"])
            session.pop("recovery_user_id", None)
            session["recovery_verified_user_id"] = user_id
            return redirect(url_for("forgot_reset"))
        return render_template(
            "forgot_verify.html", question=row["recovery_question"],
            hint=row["recovery_hint"])

    @app.route("/forgot-password/reset", methods=["GET", "POST"])
    def forgot_reset():
        user_id = session.get("recovery_verified_user_id")
        if not user_id:
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            _csrf_check()
            password = request.form.get("password") or ""
            if len(password) < 8:
                flash("Password must be at least 8 characters.")
                return redirect(url_for("forgot_reset"))
            row = _db(app).execute(
                "SELECT username FROM users WHERE id = ?",
                (user_id,)).fetchone()
            if not row:
                session.pop("recovery_verified_user_id", None)
                return redirect(url_for("forgot_password"))
            # The new password is bound to the account email (v2)
            # and the wallet key is re-encrypted with the new
            # password+email user key — one helper for every
            # password change, so the wallet always follows the
            # current password+email.
            _set_account_password(_db(app), _cfg(app), user_id, password)
            _db(app).commit()
            # The recovery answer proved ownership, so any login
            # lockout is cleared.
            _clear_login_fails(app, row["username"])
            _log(app, user_id, "password_reset",
                 "Password changed through recovery-question recovery.")
            session.pop("recovery_verified_user_id", None)
            flash("Password changed. Log in with your new password.")
            return redirect(url_for("login"))
        return render_template("forgot_reset.html")

    # ------------------------------------------------------------------
    # Passkeys (WebAuthn): register in Settings, sign in from Login.
    # Password login stays — a passkey is an option, not a replacement.
    # ------------------------------------------------------------------
    def _passkey_descriptors(app, user_id: int):
        rows = _db(app).execute(
            "SELECT credential_id, transports FROM passkey_credentials "
            "WHERE user_id = ?", (user_id,)).fetchall()
        descriptors = []
        for row in rows:
            try:
                transports = [
                    AuthenticatorTransport(t) for t in
                    json.loads(row["transports"] or "[]")
                    if t in {e.value for e in AuthenticatorTransport}]
            except (ValueError, TypeError):
                transports = []
            descriptors.append(PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(row["credential_id"]),
                transports=transports or None))
        return descriptors

    @app.route("/settings/passkey/register/begin", methods=["POST"])
    @login_required
    def passkey_register_begin():
        """Start a passkey registration ceremony. Returns the publicKey
        creation options as JSON for navigator.credentials.create()."""
        _csrf_check()
        user = _current_user(app)
        cfg = _cfg(app)
        challenge = passkey_mod.issue_challenge(
            _db(app), user["id"], "register")
        options = generate_registration_options(
            rp_id=cfg.webauthn_rp_id, rp_name=cfg.webauthn_rp_name,
            user_name=user["username"],
            user_id=str(user["id"]).encode("utf-8"),
            challenge=base64url_to_bytes(challenge),
            exclude_credentials=_passkey_descriptors(app, user["id"]))
        return jsonify(options_to_json_dict(options))

    @app.route("/settings/passkey/register/finish", methods=["POST"])
    @login_required
    def passkey_register_finish():
        """Verify a completed registration ceremony and store the
        credential. The private key never leaves the user's device —
        only the public key is stored."""
        if not request.is_json:
            return jsonify({"ok": False, "error": "json required"}), 400
        _csrf_check()
        user = _current_user(app)
        cfg = _cfg(app)
        body = request.get_json()
        challenge = passkey_mod.take_challenge(
            _db(app), user["id"], "register")
        if challenge is None:
            return jsonify({"ok": False, "error":
                            "Challenge expired or already used. "
                            "Start again."}), 400
        try:
            verified = verify_registration_response(
                credential=body.get("credential") or {},
                expected_challenge=challenge,
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin)
        except Exception:
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        credential_id = bytes_to_base64url(verified.credential_id)
        public_key = bytes_to_base64url(verified.credential_public_key)
        transports = [t for t in (body.get("transports") or [])
                      if isinstance(t, str)][:10]
        try:
            _db(app).execute(
                "INSERT INTO passkey_credentials "
                "(user_id, credential_id, public_key, sign_count, "
                "transports) VALUES (?, ?, ?, ?, ?)",
                (user["id"], credential_id, public_key,
                 verified.sign_count, json.dumps(transports)))
            _db(app).commit()
        except Exception:
            return jsonify({"ok": False, "error":
                            "This passkey is already registered."}), 400
        _log(app, user["id"], "passkey_register",
             "Passkey registered.")
        return jsonify({"ok": True, "credential_id": credential_id})

    @app.route("/settings/passkey/<int:credential_row_id>/delete",
               methods=["POST"])
    @login_required
    def passkey_delete(credential_row_id: int):
        _csrf_check()
        user = _current_user(app)
        _db(app).execute(
            "DELETE FROM passkey_credentials WHERE id = ? AND user_id = ?",
            (credential_row_id, user["id"]))
        _db(app).commit()
        _log(app, user["id"], "passkey_delete", "Passkey removed.")
        flash("Passkey removed.")
        return redirect(url_for("settings"))

    @app.route("/login/passkey/begin", methods=["POST"])
    def passkey_login_begin():
        """Start a passkey sign-in ceremony for a username. Returns
        the publicKey request options as JSON for
        navigator.credentials.get()."""
        if not request.is_json:
            return jsonify({"ok": False, "error": "json required"}), 400
        _csrf_check()
        username = (request.get_json().get("username") or "").strip()
        row = _db(app).execute(
            "SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error":
                            "No passkey is registered for this "
                            "username."}), 400
        descriptors = _passkey_descriptors(app, row["id"])
        if not descriptors:
            return jsonify({"ok": False, "error":
                            "No passkey is registered for this "
                            "username."}), 400
        cfg = _cfg(app)
        challenge = passkey_mod.issue_challenge(
            _db(app), row["id"], "login")
        options = generate_authentication_options(
            rp_id=cfg.webauthn_rp_id,
            challenge=base64url_to_bytes(challenge),
            allow_credentials=descriptors)
        return jsonify(options_to_json_dict(options))

    @app.route("/login/passkey/finish", methods=["POST"])
    def passkey_login_finish():
        """Verify a completed passkey sign-in ceremony and log the user
        in. The userHandle in the credential identifies the user."""
        if not request.is_json:
            return jsonify({"ok": False, "error": "json required"}), 400
        _csrf_check()
        body = request.get_json()
        credential = body.get("credential") or {}
        try:
            user_handle = base64url_to_bytes(
                credential.get("response", {}).get("userHandle") or "")
            user_id = int(user_handle.decode("utf-8"))
        except Exception:
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        row = _db(app).execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        if _check_login_lock(app, row["username"]):
            return jsonify({"ok": False, "error":
                            "Too many failed tries. "
                            "Locked for 15 minutes."}), 403
        try:
            credential_id = credential.get("id") or ""
        except AttributeError:
            credential_id = ""
        stored = _db(app).execute(
            "SELECT public_key, sign_count FROM passkey_credentials "
            "WHERE user_id = ? AND credential_id = ?",
            (user_id, credential_id)).fetchone()
        if not stored:
            _record_login_fail(app, row["username"])
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        challenge = passkey_mod.take_challenge(
            _db(app), user_id, "login")
        if challenge is None:
            return jsonify({"ok": False, "error":
                            "Challenge expired or already used. "
                            "Start again."}), 400
        cfg = _cfg(app)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin,
                credential_public_key=base64url_to_bytes(
                    stored["public_key"]),
                credential_current_sign_count=stored["sign_count"])
        except Exception:
            _record_login_fail(app, row["username"])
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        _db(app).execute(
            "UPDATE passkey_credentials SET sign_count = ? "
            "WHERE user_id = ? AND credential_id = ?",
            (verified.new_sign_count, user_id, credential_id))
        _db(app).commit()
        _clear_login_fails(app, row["username"])
        _record_login_event(app, row["username"], True)
        _log(app, user_id, "passkey_login", "Signed in with a passkey.")
        _rotate_session(user_id)
        # Fresh sign-in: the mode chooser shows once, then the
        # answer is remembered (see _clear_mode_chosen).
        _clear_mode_chosen(_db(app), user_id)
        return jsonify({"ok": True, "redirect": url_for("wallet")})

    @app.route("/login/passkey/discoverable/begin", methods=["POST"])
    def passkey_discoverable_begin():
        """Start a one-tap passkey sign-in: no username needed.
        Returns publicKey request options with an EMPTY
        allowCredentials list, so the authenticator offers any
        discoverable credential registered for this site and the
        user picks their account on the device. The challenge is
        stored with no user (purpose "login-discoverable") — the
        user is resolved only at finish, from the credential."""
        if not request.is_json:
            return jsonify({"ok": False, "error": "json required"}), 400
        _csrf_check()
        cfg = _cfg(app)
        challenge = passkey_mod.issue_challenge(
            _db(app), None, "login-discoverable")
        options = generate_authentication_options(
            rp_id=cfg.webauthn_rp_id,
            challenge=base64url_to_bytes(challenge),
            allow_credentials=[])
        return jsonify(options_to_json_dict(options))

    @app.route("/login/passkey/discoverable/finish", methods=["POST"])
    def passkey_discoverable_finish():
        """Verify a completed one-tap passkey sign-in and log the
        user in. The user is resolved ONLY from the credential ID
        stored in passkey_credentials — the client-supplied
        userHandle is never trusted, and no account is ever
        created from a passkey login. Unknown credentials fail
        plainly, exactly like the username-first flow."""
        if not request.is_json:
            return jsonify({"ok": False, "error": "json required"}), 400
        _csrf_check()
        body = request.get_json()
        credential = body.get("credential") or {}
        try:
            credential_id = credential.get("id") or ""
        except AttributeError:
            credential_id = ""
        stored = _db(app).execute(
            "SELECT user_id, public_key, sign_count "
            "FROM passkey_credentials WHERE credential_id = ?",
            (credential_id,)).fetchone()
        if not stored:
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        user_id = stored["user_id"]
        row = _db(app).execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        if _check_login_lock(app, row["username"]):
            return jsonify({"ok": False, "error":
                            "Too many failed tries. "
                            "Locked for 15 minutes."}), 403
        challenge = passkey_mod.take_challenge(
            _db(app), None, "login-discoverable")
        if challenge is None:
            return jsonify({"ok": False, "error":
                            "Challenge expired or already used. "
                            "Start again."}), 400
        cfg = _cfg(app)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin,
                credential_public_key=base64url_to_bytes(
                    stored["public_key"]),
                credential_current_sign_count=stored["sign_count"])
        except Exception:
            _record_login_fail(app, row["username"])
            return jsonify({"ok": False, "error":
                            "Passkey verification failed."}), 400
        _db(app).execute(
            "UPDATE passkey_credentials SET sign_count = ? "
            "WHERE user_id = ? AND credential_id = ?",
            (verified.new_sign_count, user_id, credential_id))
        _db(app).commit()
        _clear_login_fails(app, row["username"])
        _record_login_event(app, row["username"], True)
        _log(app, user_id, "passkey_login", "Signed in with a passkey.")
        _rotate_session(user_id)
        # Fresh sign-in: the mode chooser shows once, then the
        # answer is remembered (see _clear_mode_chosen).
        _clear_mode_chosen(_db(app), user_id)
        return jsonify({"ok": True, "redirect": url_for("wallet")})

    # ------------------------------------------------------------------
    # Donations (public: the donate card lives on the login page)
    # ------------------------------------------------------------------
    @app.route("/api/donate/info")
    def donate_info():
        # The full recipient address is returned ONLY here, for
        # the donor's browser to build the transfer — no template
        # ever renders it. The page shows only the masked value.
        # When the recipient is not configured, donations refuse
        # plainly (503) instead of falling back to any wallet.
        try:
            recipient = donations_mod.get_donation_recipient(_cfg(app))
        except ValueError:
            return jsonify({"error": "donations are not configured"}), 503
        return jsonify({
            "donation_address": recipient,
            "donation_address_masked":
                donations_mod.mask_address(recipient),
            "network": _cfg(app).network,
            "rpc_url": _cfg(app).rpc_url,
        })

    @app.route("/api/donate/record", methods=["POST"])
    def donate_record():
        """Record a donation the donor already paid from their own
        Phantom wallet. The server never touches the donor's money —
        it only stores the confirmed record (donor, amount, signature).
        CSRF-protected: the login page embeds the session token."""
        if not request.is_json:
            return jsonify({"error": "json required"}), 400
        _csrf_check()
        body = request.get_json()
        try:
            donor, lamports, signature = (
                donations_mod.validate_donation_record(
                    body, _cfg(app).network))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            row_id = donations_mod.record_donation(
                app, donor, lamports, signature, _cfg(app).network)
        except Exception:
            # Signature already recorded (UNIQUE) — the donation is
            # already on file, so this is a repeat, not a new donation.
            return jsonify({"error": "donation already recorded"}), 409
        return jsonify({"ok": True, "donation_id": row_id}), 201

    def _is_admin(user) -> bool:
        # James (the owner) — his Dev_SolEntry account is always an
        # admin, whatever ADMIN_USERNAME is set to. Live, the env
        # var / first-registered-user fallback pointed elsewhere and
        # Dev_SolEntry got 403 on both admin pages; this makes the
        # owner's account pass the gate outright. Everyone else
        # still goes through the env var / first-user rule below,
        # and admin actions stay logged exactly as before.
        if user["username"] == "Dev_SolEntry":
            return True
        admin = _cfg(app).admin_username
        if admin:
            return user["username"] == admin
        first = _db(app).execute(
            "SELECT username FROM users ORDER BY id ASC LIMIT 1").fetchone()
        return bool(first) and user["username"] == first["username"]

    @app.route("/admin/donations")
    @login_required
    def admin_donations():
        """Admin-only list of confirmed donations. This page IS the
        notification for James: every confirmed donation appears here.
        Email notification is NOT configured (no mail service), so this
        page is where donations are seen."""
        user = _current_user(app)
        if not _is_admin(user):
            abort(403)
        # The admin page shows only the MASKED recipient, like the
        # login page — never the full address.
        try:
            donation_masked = donations_mod.mask_address(
                donations_mod.get_donation_recipient(_cfg(app)))
        except ValueError:
            donation_masked = ""
        return render_template(
            "admin_donations.html",
            donations=donations_mod.list_donations(app),
            donation_address_masked=donation_masked)

    # ------------------------------------------------------------------
    # Admin override (James only): when something goes wrong with
    # an account, the owner can fix it from his end. NEVER
    # user-facing — the page is admin-only (403 otherwise) and is
    # not linked from any user page. Every override is LOGGED
    # (kind "admin_override", the admin's user id, the target
    # username, the action, and the time) so there is an audit
    # trail of who overrode what, when. The target user also gets
    # an activity row for password resets, so their own log shows
    # it. The app sends no emails at all — the admin tells the
    # user the new password himself, out of band.
    #   * reset password — set a new password for a user (bound
    #     to their stored email, wallet re-encrypted, as everywhere).
    #   * clear lockout — clear a user's 15-minute login lockout.
    # ------------------------------------------------------------------
    @app.route("/admin/security")
    @login_required
    def admin_security():
        user = _current_user(app)
        if not _is_admin(user):
            abort(403)
        db = _db(app)
        users = db.execute(
            "SELECT id, username, email, pw_version, "
            "recovery_question, created_at FROM users "
            "ORDER BY id ASC").fetchall()
        locked = {r["username"] for r in db.execute(
            "SELECT username FROM login_attempts "
            "WHERE locked_until IS NOT NULL").fetchall()}
        return render_template(
            "admin_security.html", users=users, locked=locked)

    def _admin_override_log(app, admin_user, action: str,
                            target_username: str) -> None:
        _log(app, admin_user["id"], "admin_override",
             f"Admin override by {admin_user['username']}: {action} "
             f"for {target_username}.")

    @app.route("/admin/security/reset-password", methods=["POST"])
    @login_required
    def admin_security_reset_password():
        admin = _current_user(app)
        if not _is_admin(admin):
            abort(403)
        _csrf_check()
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        db = _db(app)
        row = db.execute(
            "SELECT id, email FROM users WHERE username = ?",
            (username,)).fetchone()
        if not row:
            flash("No such user.")
            return redirect(url_for("admin_security"))
        if len(new_password) < 8:
            flash("New password must be at least 8 characters.")
            return redirect(url_for("admin_security"))
        _set_account_password(db, _cfg(app), row["id"], new_password)
        _clear_login_fails(app, username)
        _log(app, row["id"], "password_reset",
             "Password reset by the admin.")
        _admin_override_log(app, admin, "reset password", username)
        flash(f"Password reset for {username}. Give them the new "
              "password yourself — the app does not email it.")
        return redirect(url_for("admin_security"))

    @app.route("/admin/security/clear-lockout", methods=["POST"])
    @login_required
    def admin_security_clear_lockout():
        admin = _current_user(app)
        if not _is_admin(admin):
            abort(403)
        _csrf_check()
        username = (request.form.get("username") or "").strip()
        _clear_login_fails(app, username)
        _admin_override_log(app, admin, "cleared login lockout", username)
        flash(f"Lockout cleared for {username}.")
        return redirect(url_for("admin_security"))

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def _sol_price_usd() -> float | None:
        """Current SOL price in USD from the feed's SOL tick only —
        the one SOL number — or None when the feed has none.
        SOL-denominated manual trades refuse plainly instead of
        converting at a guessed rate."""
        return _feed_sol_price_usd(app)

    def _dashboard_data(app, user, popup_coin=None, include_trenches=True,
                        chart_range="all"):
        """Everything the coins page (dashboard) renders, for the
        ACTIVE mode only: paper shows the paper portfolio, paper
        bots and paper activity; real shows the real wallet,
        real bots and real activity. The two modes never mix on a
        page. `popup_coin` is the coin row for the open coin popup,
        or None. The wallet homepage reuses this data (with
        include_trenches=False) for its coins list and portfolio
        header. `chart_range` picks which recorded-snapshot range
        the portfolio chart shows (pnl.RANGES keys; anything else
        falls back to "all") — history is never invented."""
        db = _db(app)
        mode = _active_mode(db, user["id"])
        wallet = _wallet(app, user["id"])
        real_sol = (_real_balance(app, wallet["pubkey"])
                    if wallet and mode == "devnet" else None)
        prices = _feed_price_list(app)
        coin_rows, balances_stale = _coin_rows(
            db, user["id"], mode, prices,
            feed=app.config.get("LIVE_FEED"), app=app)
        # A holding with no live price is valued at cost, like the
        # P&L page — never $0. (A mint the feed cannot price yet
        # still has its cost basis.)
        holdings_value = 0.0
        for r in coin_rows:
            if r["price"] is not None:
                holdings_value += r["held"] * r["price"]
            elif r["avg_price"] is not None:
                holdings_value += r["held"] * r["avg_price"]
        # Portfolio header (Kraken-style): total balance + chart from
        # recorded snapshots only — never invented history. Paper
        # totals cash + holdings in USD; real totals the on-chain SOL
        # balance (with a USD approx. when SOL has a price).
        # The USD total is only as live as its SOL price: the feed's
        # SOL tick only — no REST fallback — so the page shows SOL
        # only until a live price exists. SOL is the unit of account
        # here, not a listed coin.
        sol_price = _feed_sol_price_usd(app)
        if chart_range not in pnl_mod.RANGES:
            chart_range = "all"
        if mode == "devnet":
            # The header total is the whole portfolio: on-chain SOL
            # + holdings at the SAME amounts and prices the rows
            # above show (holdings_value is priced from those rows;
            # a row with no live price is valued at cost, as the
            # rows are). Before this fix the total was the SOL
            # balance alone, so it read 0.0030 SOL while the rows
            # below it were worth ~$3.94. Without a live SOL price
            # the holdings cannot be expressed in SOL, so the total
            # falls back to the SOL balance alone.
            holdings_sol = (holdings_value / sol_price
                            if sol_price else None)
            if real_sol is not None and holdings_sol is not None:
                portfolio_sol = real_sol + holdings_sol
            else:
                portfolio_sol = real_sol
            portfolio_usd = (portfolio_sol * sol_price
                             if portfolio_sol is not None and sol_price
                             else None)
            _points, portfolio_has_data = pnl_mod.chart_points_real(
                db, user["id"], chart_range)
            # Glitch guard: a single wild snapshot must not flatten
            # the chart or poison the 24h baseline (see pnl).
            _points = pnl_mod.sane_points(_points)
            portfolio_has_data = len(_points) > 0
            portfolio_chart_svg = pnl_mod.chart_svg(
                _points, unit="SOL ")
            # The 24h % stays on the SOL balance alone: snapshots
            # record real_sol only, so a holdings-inclusive current
            # total against a SOL-only baseline would inflate the
            # %. The % tracks the SOL balance until snapshots
            # record the full portfolio total (follow-up).
            change_24h = pnl_mod.change_24h(
                db, user["id"], real_sol, real=True)
        else:
            portfolio_sol = None
            portfolio_usd = _paper(app, user["id"]) + holdings_value
            _points, portfolio_has_data = pnl_mod.chart_points(
                db, user["id"], chart_range)
            # Glitch guard: a single wild snapshot must not flatten
            # the chart or poison the 24h baseline (see pnl).
            _points = pnl_mod.sane_points(_points)
            portfolio_has_data = len(_points) > 0
            portfolio_chart_svg = pnl_mod.chart_svg(_points)
            change_24h = pnl_mod.change_24h(
                db, user["id"], portfolio_usd)
        bots = db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC",
            (user["id"], "real" if mode == "devnet" else "paper")
            ).fetchall()
        # Homepage balance-area chart: candles from the SAME recorded
        # snapshots as the portfolio line chart — never invented
        # history. Fewer than two candles means the page says so
        # instead of drawing a chart.
        _tick_points = candles_mod.normalize_points(_points)
        _span = ((_tick_points[-1]["ts"] - _tick_points[0]["ts"])
                 / 1000.0 if len(_tick_points) >= 2 else None)
        _interval = candles_mod.interval_for_home(chart_range, _span)
        _candles = candles_mod.build_candles(_tick_points, _interval)
        portfolio_candles_svg = candles_mod.candles_svg(_candles)
        portfolio_line_svg = candles_mod.line_svg(_candles)
        activity = _activity_for_mode(db, user["id"], mode)
        signing_bot = signing_bot_mod.get_signing_bot(db, user["id"])
        trenches_rows, trenches_error = (
            _trenches_list(app) if include_trenches else ([], None))
        # Coin popup sell form: the popup holding's SOL value +
        # prefill (same rule as the coin detail page).
        popup_held_sol, popup_sell_prefill = (
            _sell_prefill_for_row(popup_coin, sol_price)
            if popup_coin is not None else (None, None))
        # D2: the popup's 24h % from the coin's 24h chart points —
        # the same derivation as the coin detail page, so the % and
        # the chart can never disagree. The row's change_24h is
        # hardcoded None in _coin_rows (the feed never carries a
        # 24h % and _feed_price_list never has one), so the popup
        # showed "—" always. One feed-history read, only when a
        # popup is actually open — never on a plain dashboard
        # render. None while the series has fewer than 2 points.
        if popup_coin is not None:
            _popup_chart = fetch_market_chart(
                popup_coin["coin_id"], 1,
                feed=app.config.get("LIVE_FEED"))
            popup_coin["change_24h"] = change_pct_from_points(
                [p["price"] for p in _popup_chart.get("points") or []
                 if p.get("price") is not None])
        # Each row's position value in SOL (the home page shows
        # the SOL value to 5 decimals, never the unit price): the
        # row's USD value — live price, or cost when there is no
        # live price, the same rule as holdings_value above —
        # divided by the live SOL price. No SOL price, or no value
        # at all, leaves the row with value_sol None ("—" on the
        # page, never $0).
        for r in coin_rows:
            eff = r["value"]
            if eff is None and r.get("avg_price") is not None:
                eff = r["held"] * r["avg_price"]
            r["value_sol"] = (eff / sol_price
                              if eff is not None and sol_price
                              else None)
        return {"wallet": wallet if mode == "devnet" else None,
                "receive_address": wallet["pubkey"] if wallet else "",
                "real_sol": real_sol,
                "paper_cash": _paper(app, user["id"]),
                "holdings_value": holdings_value, "prices": prices,
                "bots": bots, "activity": activity,
                "coin_rows": coin_rows, "mode": mode,
                "balances_stale": balances_stale,
                "mode_label": _mode_label(mode, _cfg(app)),
                "portfolio_sol": portfolio_sol,
                "portfolio_usd": portfolio_usd,
                "portfolio_chart_svg": portfolio_chart_svg,
                "portfolio_has_data": portfolio_has_data,
                "portfolio_candles_svg": portfolio_candles_svg,
                "portfolio_line_svg": portfolio_line_svg,
                "portfolio_candles_count": len(_candles),
                "portfolio_chart_interval": _interval,
                "home_range_labels": pnl_mod.HOME_RANGE_LABELS,
                "change_24h": change_24h,
                "chart_range": chart_range,
                "support_id": signing_bot["support_id"]
                if signing_bot else "",
                "sol_price_usd": sol_price,
                "popup_coin": popup_coin,
                "held_sol_value": popup_held_sol,
                "sell_prefill_sol": popup_sell_prefill,
                "trenches_rows": trenches_rows,
                "trenches_error": trenches_error}

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = _current_user(app)
        return render_template("dashboard.html",
                               **_dashboard_data(app, user))

    @app.route("/dashboard/refresh", methods=["POST"])
    @login_required
    def dashboard_refresh():
        """Refresh every chart with live data: drop the cached price
        list, the cached on-chain balance and the cached trenches
        list, record a fresh portfolio snapshot (so the portfolio
        chart gains a live point), and send the user back to the
        coins page. The page then re-renders from live feeds. A feed
        that does not answer still falls back to the labelled
        snapshot — refreshed, never invented."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        clear_price_cache()
        _REAL_BALANCE_CACHE["at"].clear()
        _REAL_BALANCE_CACHE["value"].clear()
        _TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
        _record_pnl_snapshot(
            app, user["id"], _snapshot_price_map(app, user["id"]))
        _log(app, user["id"], "refresh",
             "Refreshed the coins page with live data.",
             _active_mode(db, user["id"]))
        flash("Refreshed with live data.")
        return redirect(url_for("dashboard"))

    @app.route("/api/coins/search")
    @login_required
    def coin_search():
        """Live coin search for the quick Buy/Sell popup: type a name
        and get matching coins with live prices. DexScreener's token
        search is discovery only (which coins match); every price on
        a result comes from the single source (feed tick, else one
        Helius chain read) or is None ("no data"). A feed that does
        not answer returns an error and no invented coins. Each
        result also carries how much of that coin the user holds in
        the active mode, so the popup can offer Sell all when there
        is something to sell."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        q = (request.args.get("q") or "").strip()
        found = search_coins(q, feed=app.config.get("LIVE_FEED"),
                             cfg=_cfg(app))
        if mode == "devnet":
            held_rows = db.execute(
                "SELECT coin_id, amount FROM real_holdings "
                "WHERE user_id = ?", (user["id"],)).fetchall()
        else:
            held_rows = db.execute(
                "SELECT coin_id, amount FROM paper_holdings "
                "WHERE user_id = ?", (user["id"],)).fetchall()
        held = {h["coin_id"]: float(h["amount"]) for h in held_rows}
        results = []
        for result in found["results"]:
            entry = dict(result)
            entry["held"] = held.get(entry["coin_id"], 0.0)
            results.append(entry)
        return jsonify({"results": results, "error": found["error"],
                        "source": found["source"]})

    @app.route("/api/gate-buy-quote")
    @login_required
    def gate_buy_quote():
        """Live quote for the Buy $GATE screen: the SOL cost of each
        exact preset (1M / 5M / 10M $GATE) read from the live
        bonding curve when the screen opens, plus the estimated
        $GATE out for a SOL amount (?sol=). Fail-closed: an
        unreadable or complete curve returns available=false and
        the screen disables buying — never a stale, cached, or
        invented price. Mainnet quotes now too: the package buy
        runs through the live executor on mainnet (the pump.fun
        executor while $GATE is still bonding, Jupiter once it
        graduates), so quoting a price for a buy that can execute
        is honest."""
        cfg = _cfg(app)
        mint = gate_fuel_mod.gate_mint(cfg)
        if mint is None:
            return jsonify({"available": False,
                            "reason": "No $GATE mint is buyable on "
                            "this network right now."})
        try:
            rpc = SolanaRpcClient(cfg.rpc_url)
            curve, fee_bps = gate_fuel_mod.read_gate_curve(rpc, mint)
        except Exception:
            curve, fee_bps = None, None
        if curve is None or curve.get("complete"):
            return jsonify({"available": False,
                            "reason": "Bonding curve unavailable — "
                            "the $GATE price cannot be read right "
                            "now. Buying is disabled."})
        quote = gate_fuel_mod.gate_buy_quote_for_curve(
            curve, fee_bps, mint)
        quote["available"] = True
        sol_param = (request.args.get("sol") or "").strip()
        if sol_param:
            try:
                sol_in = float(sol_param)
            except ValueError:
                return jsonify({"available": False,
                                "reason": "Invalid SOL amount."}), 400
            if sol_in <= 0:
                return jsonify({"available": False,
                                "reason": "SOL amount must be more "
                                "than 0."}), 400
            quote["tokens_out"] = gate_fuel_mod.tokens_out_for_sol(
                curve, fee_bps, sol_in)
        return jsonify(quote)

    def _gate_price_usd(app):
        """Live $GATE price in USD, or None. Single source only —
        the same resolution every other coin price uses: the live
        feed's fresh tick first, else one batched Helius chain read
        of the bonding curve (graduated -> Raydium pool). No
        DexScreener, no second read. None means no live price
        anywhere: the buy refuses rather than pricing from a
        guessed number."""
        mint = gate_fuel_mod.gate_mint(_cfg(app))
        if mint is None:
            return None
        _coin, price, _source = _resolve_coin_price(mint)
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    def _gate_no_fuel_context(app, user):
        """The $GATE context a no-fuel result page needs: the
        ledger-truth countdown (0), the bundles, the logo, the direct
        pump.fun link, and the support ID to quote to tech support."""
        db = _db(app)
        return {
            "gate_no_fuel": True,
            "gate_fuel": gate_fuel_mod.gatekeeper_status(
                db, user["id"]),
            "gate_bundles": gate_fuel_mod.bundles(),
            "gate_logo_url": gate_fuel_mod.get_gate_logo_url(
                gate_fuel_mod.gate_mint(_cfg(app))),
            "gate_pumpfun_url": gate_fuel_mod.pumpfun_url(
                gate_fuel_mod.gate_mint(_cfg(app))),
            "support_id": gate_fuel_mod.get_support_id(
                db, user["id"]),
        }

    @app.route("/gate/buy", methods=["POST"])
    @login_required
    def gate_buy():
        """Buy one $GATE package: GATEkeeper swaps SOL for $GATE
        on-chain straight through the pump.fun program while $GATE
        is still bonding (Jupiter once it graduates), sends the
        purchased $GATE straight to the dev wallet, and credits the
        package's trades automatically once that transfer confirms
        on-chain. The purchase itself never costs fuel.
        Success or plain failure is flashed; the user lands back on
        the homepage where the countdown shows the new balance."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        cfg = _cfg(app)
        bundle_id = (request.form.get("bundle_id") or "").strip()
        price_usd = _gate_price_usd(app)
        sol_price_usd = _sol_price_usd()
        result = gate_fuel_mod.buy_gate_bundle(
            db, cfg, user["id"], bundle_id, price_usd, sol_price_usd)
        if result.get("ok"):
            flash(f"Package bought — {result['trades_credited']} trades "
                  f"credited. {result['trades_remaining']} trades left. "
                  f"Send signature: {result['send_signature']}")
        else:
            flash(result.get("error") or "Package purchase failed.")
        return redirect(url_for("wallet"))

    @app.route("/api/gate-fuel")
    @login_required
    def api_gate_fuel():
        """The fuel countdown as JSON, from the ledger truth — the
        number the pages poll to count down live as trades execute.
        No key material, ever."""
        user = _current_user(app)
        return jsonify(gate_fuel_mod.gatekeeper_status(
            _db(app), user["id"]))

    @app.route("/api/gate-quote")
    @login_required
    def api_gate_quote():
        """Live SOL estimates for the packages, from the ONE pricing
        rule (gate_fuel.price_bundle_sol) — the same number the buy
        itself charges. Two shapes, one endpoint:
        - No ?bundle=: all three packages at once, the shape the
          Buy popup's script reads — {"ok", "prices": [{bundle_id,
          label, trades, sol_amount, best_value}], sol_price_usd}.
          (Before this fix the popup fetched this URL with no
          bundle and got a 400, so its prices stayed "—" and Buy
          stayed disabled forever.)
        - ?bundle=bundle_1m: the single-package shape (sol_estimate
          + price_available). price_available false means no live
          price: the popup disables Buy instead of showing a
          guessed price. The CAD price is the package's EXPECTED
          launch price, reference only; the SOL figure is the live
          quote and is what the user pays."""
        user = _current_user(app)
        db = _db(app)
        bundle_id = (request.args.get("bundle") or "").strip()
        price_usd = _gate_price_usd(app)
        sol_price_usd = _sol_price_usd()
        if not bundle_id:
            prices = []
            for b in gate_fuel_mod.bundles():
                prices.append({
                    "bundle_id": b["id"],
                    "label": b["label"],
                    "trades": b["trades"],
                    "sol_amount": gate_fuel_mod.price_bundle_sol(
                        b, price_usd, sol_price_usd),
                    "best_value": False,
                })
            available = [p for p in prices
                         if p["sol_amount"] is not None]
            if available:
                best = min(available,
                           key=lambda p: p["sol_amount"] / p["trades"])
                best["best_value"] = True
            return jsonify({
                "ok": True,
                "prices": prices,
                "sol_price_usd": sol_price_usd,
            })
        bundle = gate_fuel_mod.get_bundle(bundle_id)
        if bundle is None:
            return jsonify({"ok": False,
                            "error": "Unknown package."}), 400
        sol_estimate = gate_fuel_mod.price_bundle_sol(
            bundle, price_usd, sol_price_usd)
        return jsonify({
            "ok": True,
            "bundle_id": bundle["id"],
            "coins": bundle["coins"],
            "trades": bundle["trades"],
            # Bonus trades over the 1-trade-per-10,000-$GATE base
            # (1M: 0, 5M: 50, 10M: 450).
            "bonus": bundle["trades"] - bundle["coins"] // 10_000,
            "cad_price": bundle["cad_price"],
            "cad_price_note": "expected launch price",
            "label": bundle["label"],
            "badge": bundle["badge"],
            "sol_estimate": sol_estimate,
            "price_available": sol_estimate is not None,
            "trades_remaining": gate_fuel_mod.get_fuel(
                db, user["id"])["trades_remaining"],
        })

    @app.route("/admin/gate-support/<support_id>")
    @login_required
    def admin_gate_support(support_id: str):
        """Admin-only support lookup by the 8-digit support ID:
        the account, its fuel balance (ledger truth) and its package
        purchases. Read-only, and it returns NO key material — the
        ID identifies the account, it never grants access. Support
        actions still go through the normal admin checks."""
        user = _current_user(app)
        if not _is_admin(user):
            abort(403)
        found = gate_fuel_mod.lookup_user_by_support_id(
            _db(app), support_id)
        if found is None:
            return jsonify({"ok": False,
                            "error": "No account holds that support "
                                     "ID."}), 404
        purchases = [dict(row) for row in _db(app).execute(
            "SELECT bundle_id, coins, trades, sol_spent, buy_signature, "
            "send_signature, status, source, promo_code, created_at "
            "FROM gate_fuel_purchases "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (found["user_id"],)).fetchall()]
        return jsonify({"ok": True,
                        "user_id": found["user_id"],
                        "username": found["username"],
                        "support_id": found["support_id"],
                        "fuel": found["fuel"],
                        "purchases": purchases})

    @app.route("/choose-mode", methods=["POST"])
    @login_required
    def choose_mode():
        """The post-sign-in mode chooser: Demo trading (paper) or
        Real trading (mainnet on mainnet, devnet on devnet). Sets the user's active mode and
        remembers the choice for this session, so the popup does not
        come back on every page — only after the next sign-in (the
        session is cleared on logout). Mode is locked for the
        session: the user changes it only by signing out in
        Settings and choosing again at the next sign-in."""
        _csrf_check()
        user = _current_user(app)
        mode = (request.form.get("mode") or "").lower()
        if mode not in ("paper", "devnet"):
            flash("Pick Demo trading or Real trading.")
            return redirect(url_for("wallet"))
        db = _db(app)
        db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
                   (mode, user["id"]))
        db.commit()
        session["mode_chosen"] = True
        # Remembered in the database as well as the session, so a
        # dropped session flag alone can never make the chooser
        # reappear mid-session.
        _mark_mode_chosen(db, user["id"])
        _log(app, user["id"], "mode_switch",
             f"Trading mode chosen: {_mode_label(mode, _cfg(app))}.")
        flash(f"You are now on {_mode_label(mode, _cfg(app))}.")
        return redirect(url_for("wallet"))

    # ------------------------------------------------------------------
    # Coin popup: tap a coin on the coins page and get Buy / Sell /
    # Send / Receive for it. After any action, a confirmation page
    # states the result, then the user goes back to the coins screen.
    # ------------------------------------------------------------------
    @app.route("/coins/<coin_id>")
    @login_required
    def coin_popup(coin_id: str):
        """The coins page with the popup open for one coin."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        coin_id_resolved, price, _source = _resolve_coin_price(coin_id)
        if coin_id_resolved is None:
            abort(404)
        prices = _feed_price_list(app)
        rows, _balances_stale = _coin_rows(db, user["id"], mode, prices, feed=app.config.get("LIVE_FEED"), app=app)
        row = next((r for r in rows
                    if r["coin_id"] == coin_id_resolved), None)
        if row is None:
            # Known coin, not in the list (no holding, no price) —
            # build its row from what we know.
            row = _unlisted_coin_row(coin_id_resolved, price, prices,
                                     db=db,
                                     rpc_url=_cfg(app).rpc_url)
        data = _dashboard_data(app, user, popup_coin=row)
        return render_template("dashboard.html", **data)

    # ------------------------------------------------------------------
    # Coin detail page: tap a coin on the wallet homepage (or the
    # coins page) and get its market chart, 24h high/low, buyers vs
    # sellers %, and Buy / Sell / Sell all — all in SOL. The chart
    # ranges are 24hr / 1day / 1w / 1m / 1y / all-time (24hr and 1day
    # are the same 1-day window, shown under both labels). Chart data
    # is real (the live feed's recorded ticks for the mint) or the
    # page says "No data yet" — never invented. Buyers/sellers %
    # comes from DexScreener 24h buy/sell txn counts; "—" when the
    # feed has none.
    # ------------------------------------------------------------------
    @app.route("/coins/<coin_id>/detail")
    @login_required
    def coin_detail(coin_id: str):
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        coin_id_resolved, price, _source = _resolve_coin_price(coin_id)
        if coin_id_resolved is None:
            abort(404)
        prices = _feed_price_list(app)
        rows, _balances_stale = _coin_rows(db, user["id"], mode, prices, feed=app.config.get("LIVE_FEED"), app=app)
        row = next((r for r in rows
                    if r["coin_id"] == coin_id_resolved), None)
        if row is None:
            row = _unlisted_coin_row(coin_id_resolved, price, prices,
                                     db=db,
                                     rpc_url=_cfg(app).rpc_url)
        range_key = (request.args.get("range") or "24h").strip()
        if range_key not in CHART_RANGES:
            range_key = "24h"
        _label, days = CHART_RANGES[range_key]
        feed = app.config.get("LIVE_FEED")
        chart = fetch_market_chart(coin_id_resolved, days, feed=feed)
        # The chart is a real candlestick chart: live-feed tick
        # history for the mint. The line ("regular") view is drawn
        # from the same candles, so the two views can never
        # disagree. The market chart is still fetched — the 24h
        # high/low come from it, as before.
        chart_interval = candles_mod.interval_for_mint(range_key)
        candles = mint_candles_from_feed(
            coin_id_resolved, chart_interval, feed)
        chart_error = chart.get("error") if not candles else None
        candles_svg = candles_mod.candles_svg(candles, unit="$")
        line_svg = candles_mod.line_svg(candles, unit="$")
        # 24h high/low come from the 24-hour series itself, so they
        # always match the chart. When the detail range IS 24h/1day,
        # the range chart is the 24h series — no second fetch.
        if range_key in ("24h", "1d"):
            high_24h, low_24h = high_low_24h(chart["points"])
            points_24h = chart["points"]
        else:
            chart_24h = fetch_market_chart(coin_id_resolved, 1,
                                           feed=feed)
            high_24h, low_24h = high_low_24h(chart_24h["points"])
            points_24h = chart_24h["points"]
        # D2: the 24h % from the SAME 24h series the high/low come
        # from, so the % and the chart can never disagree. The row's
        # change_24h was hardcoded None in _coin_rows (the feed
        # never carries a 24h % and _feed_price_list never has one),
        # so the detail page showed "—" always. None while the
        # series has fewer than 2 points — the template keeps "—".
        # The % covers the window the feed actually holds (a few
        # hours when the feed restarted), the same window the 24h
        # high/low already use.
        change_24h = change_pct_from_points(
            [p["price"] for p in points_24h
             if p.get("price") is not None])
        flow = fetch_trade_flow(coin_id_resolved)
        bots = db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC",
            (user["id"], "real" if mode == "devnet" else "paper")
            ).fetchall()
        wallet = _wallet(app, user["id"])
        # Sell form: the holding's SOL value + prefill, so the form
        # never suggests 0.1 SOL for a holding worth less.
        sol_price = _sol_price_usd()
        held_sol_value, sell_prefill_sol = _sell_prefill_for_row(
            row, sol_price)
        return render_template(
            "coin_detail.html", coin=row, range_key=range_key,
            ranges=CHART_RANGES, candles_svg=candles_svg,
            line_svg=line_svg, chart_error=chart_error,
            chart_interval=chart_interval,
            chart_interval_label=(
                candles_mod.interval_label(chart_interval)
                if chart_interval else ""),
            chart_candles_count=len(candles),
            high_24h=high_24h,
            low_24h=low_24h, flow=flow, bots=bots, mode=mode,
            change_24h=change_24h,
            mode_label=_mode_label(mode, _cfg(app)),
            receive_address=wallet["pubkey"] if wallet else "",
            prices_source=prices["source"],
            prices_as_of=prices["as_of"],
            # Page 5 Send/Receive must account for the network: the
            # coin's chain, the plain refusal when it is not a Solana
            # asset, and the network this app sends on.
            coin_network=networks_mod.coin_network(coin_id_resolved),
            send_refusal=networks_mod.send_refusal(
                coin_id_resolved, _cfg(app).network),
            network_label=networks_mod.network_label(_cfg(app).network),
            sol_price_usd=sol_price,
            held_sol_value=held_sol_value,
            sell_prefill_sol=sell_prefill_sol)

    @app.route("/api/coins/<coin_id>/tick")
    @login_required
    def coin_tick(coin_id: str):
        """One live price tick for the coin detail page's live chart.

        The page polls this once a second while it is open (and
        pauses while the tab is hidden). Prices are never invented.
        The live feed goes FIRST and ONLY: a fresh validated tick
        from the Helius stream is the most current price the app
        has. A stale feed tick (older than 30s) is never served as
        fresh, and there is no second-source fetch any more — when
        no fresh tick exists the response is ok:false with message
        "no data" and the page shows "no data" instead of the last
        real price. This is a read of the same feed the coins list
        already uses — no trade, no signing, nothing moves."""
        feed = app.config.get("LIVE_FEED")
        if feed is not None:
            key = (coin_id or "").upper() if (coin_id or "").upper() == "SOL" \
                else coin_id
            tick = feed.snapshot([key]).get(key)
            if tick is not None and not tick["stale"]:
                return jsonify({"ok": True, "coin_id": key,
                                "price": tick["price"],
                                "source": tick["source"],
                                "as_of": tick["as_of"]})
        if is_valid_address(coin_id):
            return jsonify({"ok": False, "coin_id": coin_id, "price": None,
                            "source": None, "as_of": _now(),
                            "error": "no data"})
        return jsonify({"ok": False, "coin_id": coin_id, "price": None,
                        "source": None, "as_of": _now(),
                        "error": "no data"})

    @app.route("/api/coins/<coin_id>/candles")
    @login_required
    def coin_candles(coin_id: str):
        """Fresh candle SVGs for the coin detail chart's live
        re-render. Same data as the page load: live-feed tick
        history for the mint. The chart re-renders from this at
        most once per candle interval — the page never redraws
        every second. Login required; a coin with no candles yet
        reports count 0 and the page says the candles are still
        building, never a made-up chart."""
        range_key = (request.args.get("range") or "24h").strip()
        if range_key not in CHART_RANGES:
            range_key = "24h"
        resolved, _price, _source = _resolve_coin_price(coin_id)
        if resolved is None:
            abort(404)
        _label, days = CHART_RANGES[range_key]
        interval = candles_mod.interval_for_mint(range_key)
        candles = mint_candles_from_feed(
            resolved, interval, app.config.get("LIVE_FEED"))
        return jsonify({"ok": True, "coin_id": resolved,
                        "candles_svg": candles_mod.candles_svg(
                            candles, unit="$"),
                        "line_svg": candles_mod.line_svg(
                            candles, unit="$"),
                        "count": len(candles), "interval": interval,
                        "updated": _now()})

    # ------------------------------------------------------------------
    # Live prices over WebSocket (/ws/live) + REST snapshot (/api/live).
    # One feed for the whole app (see livefeed.py): browsers subscribe
    # to the coins on their page and get validated ticks pushed as
    # they arrive. A tick the feed rejected never reaches this socket.
    # The REST tick endpoint above stays as the fallback: when the
    # socket drops, live.js polls /api/live instead and says so.
    # ------------------------------------------------------------------
    @app.route("/api/live")
    @login_required
    def live_snapshot():
        coins = [c.strip()
                 for c in (request.args.get("coins") or "").split(",")
                 if c.strip()]
        feed = app.config["LIVE_FEED"]
        ticks = feed.snapshot(coins or None)
        if coins:
            # Coins the snapshot cannot price (no tick, or a stale
            # one) fall back to one batched chain read — the
            # coin-detail page's backup path (no-break rule 4), so
            # a stall shows a fresh chain price instead of
            # "no data". Only valid mint addresses are read (a
            # symbol like BTC has no curve to read); a coin the
            # chain cannot price stays missing. Chain ticks carry
            # source "chain" and a fresh timestamp, so the client
            # applies them exactly like a live tick (see live.js
            # applyTick) and the page title stamps the source.
            # The 1-second tick endpoint (/api/coins/<id>/tick)
            # stays feed-only by design — its tests pin "no data"
            # with no tick — this 4s snapshot is the page's
            # fallback path.
            missing = [c for c in coins
                       if (c not in ticks
                           or ticks[c].get("stale"))
                       and is_valid_address(c)]
            if missing:
                chain = _chain_prices_for_mints(app, missing)
                as_of = _now()
                for mint, price in chain.items():
                    if price is None or price <= 0:
                        continue
                    ticks[mint] = {"coin_id": mint, "price": price,
                                   "source": "chain", "as_of": as_of,
                                   "stale": False}
        return jsonify({"ok": True,
                        "ticks": ticks,
                        "as_of": _now()})

    @app.route("/api/sparklines")
    @login_required
    def sparklines_snapshot():
        """Fresh sparklines for the home page's holdings rows and
        the trenches badges. The page polls this every 5 seconds
        (live.js) so each coin's chart keeps moving with the live
        market instead of sitting static from page load. Each
        chart is real market data (GeckoTerminal OHLCV first,
        then the feed's recorded ticks). Returns {ok: True,
        sparklines: {coin_id: svg-or-null}, pct_1h: {coin_id:
        number-or-null}}. The % comes from the SAME points the
        chart draws, so the two can never disagree. A coin with
        fewer than 2 real recent points returns null — the page
        leaves its sparkline alone (chart building), never
        faked."""
        coins = [c.strip()
                 for c in (request.args.get("coins") or "").split(",")
                 if c.strip()]
        feed = app.config["LIVE_FEED"]
        out = {}
        pct = {}
        for coin_id in coins:
            is_mint = is_valid_address(coin_id)
            points = sparkline_points_1h(coin_id, is_mint=is_mint,
                                        feed=feed)
            out[coin_id] = sparkline_svg(points) or None
            pct[coin_id] = change_pct_from_points(points)
        return jsonify({"ok": True, "sparklines": out, "pct_1h": pct,
                        "as_of": _now()})

    @app.route("/api/trenches")
    @login_required
    def trenches_snapshot():
        """Fresh trenches rows for Page 10's 5-second list poll
        (live.js): the same rows the page renders, with each row's
        HTML plus its live bits (price display, 1h %, sparkline
        SVG). The page updates rows in place when the coin set is
        unchanged, and inserts / removes rows when it changes —
        so a coin leaves the list the moment it drops out of the
        top 10, without a manual reload. A row neither price
        source can price is KEPT here (reading "no data") — the
        page render drops it, but the live refresh must not yank
        rows off the page during a stall. Returns {ok: True,
        rows: [{mint, html, price_usd, price_display, change_1h,
        sparkline_svg}], error}."""
        rows, error = _trenches_list(app, drop_unpriced=False)
        out = []
        for row in rows:
            html = render_template("_trenches_row.html", t=row)
            out.append({
                "mint": row["mint"],
                "html": html,
                "price_usd": row.get("price_usd"),
                "price_display": row.get("price_display"),
                "change_1h": row.get("change_1h_pct"),
                "sparkline_svg": row.get("sparkline_svg"),
            })
        return jsonify({"ok": True, "rows": out, "error": error,
                        "as_of": _now()})

    @app.route("/api/home-holdings")
    @login_required
    def home_holdings_snapshot():
        """Fresh home holdings rows for the home page's 5-second
        auto-refresh: the same rows _dashboard_data renders, with
        each row's HTML plus its live bits (SOL value text, 1h %,
        sparkline SVG). The page updates rows in place when the
        coin set is unchanged, and inserts / removes rows when it
        changes — so a coin leaves the list the moment its balance
        hits 0, without a manual reload. A coin with no SOL value
        reports value_sol null ("—" on the page, never $0). The
        order matches the home page ($GATE first). Returns
        {ok: True, balances_stale, rows: [{coin_id, html,
        value_sol, change_1h, sparkline_svg}]}."""
        user = _current_user(app)
        data = _dashboard_data(app, user, include_trenches=False,
                               chart_range="1d")
        coin_rows = data["coin_rows"]
        gate_mint = gate_fuel_mod.gate_mint(_cfg(app))
        if gate_mint:
            coin_rows = sorted(
                coin_rows,
                key=lambda r: 0 if r.get("coin_id") == gate_mint else 1)
        rows = []
        for row in coin_rows:
            value_sol = row.get("value_sol")
            rows.append({
                "coin_id": row["coin_id"],
                "html": render_template("_home_holdings_row.html",
                                        row=row),
                "value_sol": (f"{value_sol:.5f} SOL"
                              if value_sol is not None else None),
                "change_1h": row.get("change_1h"),
                "sparkline_svg": row.get("sparkline_svg") or None,
            })
        return jsonify({"ok": True,
                        "balances_stale": data["balances_stale"],
                        "rows": rows, "as_of": _now()})

    @app.route("/api/coins-rows")
    @login_required
    def coins_rows():
        """Fresh coins-page holdings rows for the 5-second list poll
        (live.js): the same rows the coins page renders, with each
        row's HTML plus its live bits (price display, 1h %, P&L).
        The page updates rows in place when the coin set is
        unchanged, and inserts / removes rows when it changes — so
        a coin leaves the list the moment its balance hits 0, and
        the 1h % moves, without a manual reload. Mirrors
        /api/home-holdings, which covers the home page's rows; the
        coins page renders the same rows in its own markup (unit
        price + 1h % + P&L, no sparkline, no SOL value), so it gets
        its own endpoint. Returns {ok: True, balances_stale,
        rows: [{coin_id, html, price_display, change_1h,
        pnl_display, pnl}], mode_label, as_of} — mode_label lets
        the poll build the empty-list message ("You hold no coins
        in …") with the server's own wording when the list
        empties; the page itself renders that message only when
        the list starts empty, never beside real rows."""
        user = _current_user(app)
        data = _dashboard_data(app, user, include_trenches=False,
                               chart_range="1d")
        rows = []
        for row in data["coin_rows"]:
            pnl = row.get("pnl")
            # P&L text matches the row template exactly
            # ("+$12.34" / "$-1.23"): the poll must not flip the
            # format the server rendered.
            if pnl is None:
                pnl_display = None
            elif pnl >= 0:
                pnl_display = "+$" + f"{pnl:.2f}"
            else:
                pnl_display = "$" + f"{pnl:.2f}"
            rows.append({
                "coin_id": row["coin_id"],
                "html": render_template("_coins_row.html", row=row),
                "price_display": fmt_price_sig(row.get("price")),
                "change_1h": row.get("change_1h"),
                "pnl_display": pnl_display,
                "pnl": pnl,
            })
        return jsonify({"ok": True,
                        "balances_stale": data["balances_stale"],
                        "rows": rows,
                        "mode_label": data["mode_label"],
                        "as_of": _now()})

    sock = app.config["SOCK"]

    @sock.route("/ws/live")
    def live_ws(ws):
        user = _current_user(app)
        if user is None:
            try:
                ws.send(json.dumps(
                    {"type": "error",
                     "error": "Sign in to use live prices."}))
            except Exception:
                pass
            return
        feed = app.config["LIVE_FEED"]
        subscribed: list = []
        last_sent: dict = {}

        def _push_new():
            # Dedup on the whole tick, not just its timestamp: as_of
            # is second-precision, so two ticks in the same second
            # with different prices must both be pushed.
            snap = feed.snapshot(subscribed)
            for cid, tick in snap.items():
                if last_sent.get(cid) == tick:
                    continue
                ws.send(json.dumps({"type": "tick", **tick}))
                last_sent[cid] = tick

        try:
            while True:
                try:
                    msg = ws.receive(timeout=0.5)
                except Exception:
                    break  # connection closed
                try:
                    if msg is not None:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            data = {}
                        want = data.get("subscribe")
                        if isinstance(want, list):
                            # Only real coins: pump.fun mint
                            # addresses, plus SOL (the unit of
                            # account) — at most 100 per connection.
                            new_subs = []
                            for coin in want:
                                if not isinstance(coin, str):
                                    continue
                                if coin.upper() == "SOL":
                                    new_subs.append("SOL")
                                elif is_valid_address(coin):
                                    new_subs.append(coin)
                                if len(new_subs) >= 100:
                                    break
                            for coin in subscribed:
                                if coin not in new_subs and coin != "SOL":
                                    feed.unsubscribe_mint(coin)
                            for coin in new_subs:
                                if coin not in subscribed and coin != "SOL":
                                    feed.subscribe_mint(coin)
                            subscribed = new_subs
                            snap = feed.snapshot(subscribed)
                            ws.send(json.dumps(
                                {"type": "snapshot", "ticks": snap}))
                            last_sent = dict(snap)
                    if subscribed:
                        _push_new()
                except Exception:
                    break  # send failed — the connection is gone
        finally:
            for coin in subscribed:
                if coin != "SOL":
                    feed.unsubscribe_mint(coin)

    def _coin_result(heading: str, lines: list, ok: bool,
                     coin_id: str, mode_label: str, **extra):
        """The confirmation page after a popup action: what happened,
        the amounts, and the new balance/position — then back to the
        coins screen. Extra template context (for example the
        no-fuel popup context) passes straight through."""
        return render_template(
            "coin_result.html", heading=heading, lines=lines, ok=ok,
            coin_id=coin_id, mode_label=mode_label, **extra)

    def _real_bot_for_trade(app, user, bot_id_raw: str):
        """The real bot a manual real trade is governed by. Returns
        (bot, error_message) — or (None, None) for a User buy: no
        bot picked means no bot watching (buy-flow rebuild,
        2026-11-26), and the % -of-wallet buttons + the gas reserve
        replace the bot's buy-size cap. A bot id that is invalid,
        not the user's, or a paper bot is still an error. The bot's
        buy-size % is the per-trade cap the executor enforces, and
        its platform must be one real trading is built against
        (pump.fun)."""
        db = _db(app)
        if not (bot_id_raw or "").strip():
            return None, None
        try:
            bot_id = int(bot_id_raw)
        except ValueError:
            return None, "That bot choice is not valid."
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            return None, "That bot isn't yours."
        if bot["mode"] != "real":
            return None, (f"Bot '{display_bot_name(bot['name'])}' is a paper bot — pick a "
                          f"real bot for a real trade.")
        return bot, None

    @app.route("/coins/<coin_id>/buy", methods=["POST"])
    @login_required
    def coin_buy(coin_id: str):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        label = _mode_label(mode, _cfg(app))
        coin_id_resolved, price, _source = _resolve_coin_price(coin_id)
        if coin_id_resolved is None:
            abort(404)
        if mode == "paper":
            # Buys are in SOL on the detail page and the coin popup:
            # convert at the current SOL price. The old $ form is
            # still accepted (it is what the tests post). A SOL price
            # the feed does not have refuses plainly — never guessed.
            amount_sol_raw = (request.form.get("amount_sol") or "").strip()
            if amount_sol_raw:
                try:
                    amount_sol = float(amount_sol_raw)
                except ValueError:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be a number."], False,
                        coin_id_resolved, label)
                if amount_sol <= 0:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be more than 0."], False,
                        coin_id_resolved, label)
                sol_price = _sol_price_usd()
                if sol_price is None:
                    return _coin_result(
                        "Nothing was bought",
                        ["SOL price unavailable right now."],
                        False, coin_id_resolved, label)
                usd = amount_sol * sol_price
            elif (request.form.get("amount_tokens") or "").strip():
                # Quick Buy/Sell popup: buy a token amount. Convert
                # at the server's own live price — the price the
                # popup showed is never trusted for the trade.
                try:
                    tokens = float(request.form.get("amount_tokens"))
                except ValueError:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be a number."], False,
                        coin_id_resolved, label)
                if tokens <= 0:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be more than 0."], False,
                        coin_id_resolved, label)
                if price is None or price <= 0:
                    return _coin_result(
                        "Nothing was bought",
                        ["Price unavailable right now — the buy needs "
                         "a current price to convert tokens."],
                        False, coin_id_resolved, label)
                # A token-amount buy must deliver exactly the tokens
                # asked for: the USD is grossed up by the 1% buy
                # haircut, so the fill (mark x 1.01) returns `tokens`.
                usd = tokens * price * (
                    1.0 + PAPER_BUY_HAIRCUT_PCT / 100.0)
            else:
                try:
                    usd = float(request.form.get("usd") or 0)
                except ValueError:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be a number."], False,
                        coin_id_resolved, label)
            # Paper buys price from the resolved feed-tick price
            # (helius stream) — the fill rate comes from the single
            # source, never a Jupiter quote.
            sl_pct, tp_pct, exit_error = _parse_exit_fields(
                request.form)
            if exit_error:
                return _coin_result(
                    "Nothing was bought", [exit_error], False,
                    coin_id_resolved, label)
            result = _paper_buy_for(app, user, coin_id_resolved, usd,
                                    request.form.get("bot_id") or "")
            if not result["ok"]:
                _log(app, user["id"], "paper_buy_failed",
                     f"Paper buy of {coin_id_resolved} failed — "
                     f"{result['message']} No transaction — paper "
                     f"trades never reach the chain.",
                     "paper")
                return _coin_result(
                    "Nothing was bought", [result["message"]], False,
                    coin_id_resolved, label)
            lines = [result["message"]]
            if result["ok"]:
                lines.append(
                    f"New paper cash: ${result['cash']:,.2f} CAD.")
                if result.get("bot"):
                    lines.append(
                        f"Bot '{result['bot']['name']}' is looking "
                        f"after this coin.")
                else:
                    lines.append("User buy — no bot watching this coin.")
            # Stop loss / take profit off the executed average cost.
            held = db.execute(
                "SELECT avg_price FROM paper_holdings WHERE user_id = ? "
                "AND coin_id = ?",
                (user["id"], coin_id_resolved)).fetchone()
            entry_price = (float(held["avg_price"])
                           if held else None)
            lines.extend(_store_user_exit_for_buy(
                db, user["id"], coin_id_resolved, "paper", entry_price,
                sl_pct, tp_pct))
            return _coin_result(
                "Bought" if result["ok"] else "Nothing was bought",
                lines, result["ok"], coin_id_resolved, label)
        # Real mode: the EXISTING pump.fun executor, with the same
        # guards as the silent loop. Anything it refuses is shown
        # plainly with no fill — never faked. Amounts arrive as SOL
        # (detail page / popup ticket), as a token amount, or as $
        # (quick Buy/Sell popup) — all convert at the server's own
        # live prices, never at a price the browser sent.
        # Amounts arrive as a typed USD amount (new buy forms),
        # as SOL, as a token amount, or as $ (legacy forms) — all
        # convert at the server's own live prices, never at a price
        # the browser sent. `amount_usd` is the buy's size in USD:
        # it picks the gas-reserve tier, nothing else.
        sol_price = _sol_price_usd()
        (amount_sol, amount_usd,
         amount_error) = _parse_buy_amount(
             app, request.form, price, sol_price)
        if amount_error:
            return _coin_result(
                "Nothing was bought", [amount_error], False,
                coin_id_resolved, label)
        if amount_sol is None or amount_sol <= 0:
            return _coin_result(
                "Nothing was bought", ["Amount must be more than 0."],
                False, coin_id_resolved, label)
        sl_pct, tp_pct, exit_error = _parse_exit_fields(request.form)
        if exit_error:
            return _coin_result(
                "Nothing was bought", [exit_error], False,
                coin_id_resolved, label)
        # A Jupiter-routed coin trades through the Jupiter executor
        # (mainnet, gated behind James's go-ahead); anything else
        # is a pump.fun mint and trades through the pump.fun
        # executor (mainnet now — the devnet-only gate is lifted).
        # With the Jupiter gate off the executor refuses plainly —
        # nothing is built, signed or sent — instead of letting
        # the pump.fun executor fail on a coin it was never built
        # for.
        bot, error = _real_bot_for_trade(
            app, user, request.form.get("bot_id") or "")
        if error:
            return _coin_result("Nothing was bought", [error], False,
                                coin_id_resolved, label)
        # $GATE fuel gate (2026-11-23): a real buy costs 1 trade
        # credit. It sits here — after the bot lookup, before the
        # executor — so a trade that can never be attempted (no bot)
        # is refused with its own reason, and a trade that can be
        # attempted is never placed without fuel. No fuel means no
        # trade: the page shows the no-fuel popup (logo, countdown
        # at 0, Buy $GATE button) instead of placing the buy. The
        # gate also turns the user's real bots off until they
        # refill. Paper buys never reach this branch.
        if not gate_fuel_mod.enforce_fuel_gate(db, user["id"]):
            return _coin_result(
                "Out of fuel",
                ["You're out of $GATE fuel — this buy needs 1 trade "
                 "credit and you have 0 left.",
                 "Buy a $GATE package to keep trading. Your bots are "
                 "off until you refill."],
                False, coin_id_resolved, label,
                **_gate_no_fuel_context(app, user))
        # Last-credit reserve (James, 2026-11-24): when fuel is down
        # to its last credit and real positions are open, that
        # credit is reserved to exit every position in one sweep —
        # a buy here would eat it. Buys can never consume the last
        # credit; the sweep is its only spender.
        if gate_fuel_mod.exit_all_pending(db, user["id"]):
            return _coin_result(
                "Last trade reserved",
                ["Your last $GATE trade is reserved to exit all your "
                 "open positions at once.",
                 "This buy would use it up — buys can't use the last "
                 "trade. Buy a $GATE package to keep trading."],
                False, coin_id_resolved, label)
        # The venue is the bonding curve: a graduated coin trades
        # through Jupiter (mainnet, gated behind James's go-ahead);
        # anything still bonding trades through the pump.fun
        # executor. An unreadable curve refuses plainly — the
        # venue is never guessed. The price-gate context (how old
        # the decision price is, the one SOL price) travels with
        # the buy so the executor can check it against a fresh
        # on-chain read before signing. No fresh decision price
        # (age None) means no buy at all — the trade must not
        # execute on a price the feed cannot vouch for.
        rpc = rpc_client_for_config(_cfg(app))
        decision_age = _decision_price_age(app, coin_id_resolved)
        if decision_age is None:
            return _coin_result(
                "Nothing was bought",
                ["No live price right now — the buy needs a fresh "
                 "price to decide on. Try again in a moment."],
                False, coin_id_resolved, label)
        sol_price_usd, _sol_price_secondary = _trade_sol_prices(app)
        try:
            complete = is_curve_complete(rpc, coin_id_resolved)
        except Exception:
            complete = None
        if complete is None:
            return _coin_result(
                "Nothing was bought",
                ["Bonding curve unavailable — the trade venue could "
                 "not be determined."],
                False, coin_id_resolved, label)
        # Gas reserve (buy-flow rebuild, James 2026-11-26 ~21:45,
        # threshold in USD since 2026-11-26 ~21:48): the buy's size
        # in USD picks the tier — under $5 USD holds back 0.0012
        # SOL, $5 USD or more holds back 0.012 SOL — so the wallet
        # can always sell back to SOL. A buy whose USD size is
        # unknown fails safe to the large tier. The executor
        # enforces it for User buys (bot None); a bot buy keeps
        # its own fee reserve and ignores this value.
        reserve_sol = _cfg(app).user_buy_gas_reserve_sol(amount_usd)
        if complete:
            result = execute_jupiter_swap(
                bot, user["id"], coin_id_resolved, "buy", amount_sol,
                db, _cfg(app), decision_price_usd=price or 0.0,
                decision_price_age_seconds=decision_age,
                sol_price_usd=sol_price_usd,
                user_buy_reserve_sol=reserve_sol)
            venue_label = "via Jupiter (mainnet)"
        else:
            result = execute_real_trade(
                bot, user["id"], coin_id_resolved, "buy", amount_sol,
                price or 0.0, db, _cfg(app),
                decision_price_age_seconds=decision_age,
                sol_price_usd=sol_price_usd,
                user_buy_reserve_sol=reserve_sol)
            venue_label = f"on pump.fun ({_cfg(app).network})"
        if result.get("status") != "executed":
            failure_lines = _trade_failure_lines(result, "buy")
            _log(app, user["id"], "real_buy_failed",
                 f"Real buy of {coin_id_resolved} failed — "
                 + " ".join(failure_lines), "real")
            return _coin_result(
                "Nothing was bought", failure_lines, False,
                coin_id_resolved, label)
        # Fuel drawdown: the trade executed — 1 package credit,
        # keyed by the confirmed signature. A bookkeeping failure
        # never breaks the trade result. Manual trades write no
        # trade_ledger row, so the use row is keyed by signature
        # only (trade_id None).
        gate_fuel_mod.consume_fuel(
            db, user["id"], None, result.get("signature"))
        coin_amount = float(result.get("coin_amount") or 0)
        _exit_lines = []
        # Cost basis from the SOL the chain actually took, when the
        # executor reported it — the amount asked for is not what
        # landed (slippage + network fee).
        sol_spent = result.get("sol_spent")
        try:
            sol_spent = (float(sol_spent)
                         if sol_spent is not None else None)
        except (TypeError, ValueError):
            sol_spent = None
        avg_price = _real_avg_price(
            app, coin_id_resolved,
            sol_spent if sol_spent is not None else amount_sol,
            coin_amount)
        if coin_amount > 0:
            _record_real_holding(db, user["id"], coin_id_resolved,
                                 coin_amount, avg_price)
            # Stop loss / take profit off the executed average cost
            # (the holding's merged average after this buy).
            _exit_lines = _store_user_exit_for_buy(
                db, user["id"], coin_id_resolved, "real", avg_price,
                sl_pct, tp_pct)
            # First time this coin is held: fetch + store its
            # official logo (see the paper-buy hook above). Best
            # effort — never blocks the trade.
            try:
                ensure_coin_meta(db, coin_id_resolved,
                                 rpc_url=_cfg(app).rpc_url)
            except Exception:
                pass
        wallet = _wallet(app, user["id"])
        new_sol = (_real_balance(app, wallet["pubkey"])
                   if wallet else None)
        # No trading fee (removed 2026-11-12): the whole trade
        # amount is swapped / paid out, and nothing is recorded
        # as a fee.
        _log(app, user["id"], "real_buy",
             f"Bought {coin_amount:.4f} {coin_id_resolved} for "
             f"{amount_sol:.4f} SOL {venue_label}. Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.", "real")
        _record_pnl_snapshot(
            app, user["id"],
            _snapshot_price_map(app, user["id"]))
        lines = [f"Bought {coin_amount:.4f} {coin_id_resolved} for "
                 f"{amount_sol:.4f} SOL {venue_label}.",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
        if bot is None:
            lines.append("User buy — no bot watching this coin.")
        lines.extend(_exit_lines)
        if new_sol is not None:
            lines.append(f"New wallet balance: {new_sol:.4f} SOL.")
        return _coin_result("Bought", lines, True, coin_id_resolved, label)

    @app.route("/coins/<coin_id>/sell", methods=["POST"])
    @login_required
    def coin_sell(coin_id: str):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        label = _mode_label(mode, _cfg(app))
        coin_id_resolved, price, _source = _resolve_coin_price(coin_id)
        if coin_id_resolved is None:
            abort(404)
        # Sells are in SOL on the detail page and the coin popup, and
        # Sell all sells the whole hand-held position. The old
        # coin-amount form is still accepted.
        if request.form.get("sell_all") == "1":
            if mode == "paper":
                held = db.execute(
                    "SELECT amount FROM paper_holdings WHERE user_id = ? "
                    "AND coin_id = ?", (user["id"], coin_id_resolved)
                    ).fetchone()
                amount = float(held["amount"]) if held else 0.0
                if amount <= 0:
                    return _coin_result(
                        "Nothing was sold",
                        ["You hold none of this coin in " + label + "."],
                        False, coin_id_resolved, label)
            else:
                # Manual sell-all covers the whole position: what
                # the user holds by hand PLUS what any bot is
                # managing. The user's Sell button overrides the
                # bot — the bot's rules never block a manual sell.
                manual = db.execute(
                    "SELECT amount FROM real_holdings WHERE user_id = ? "
                    "AND coin_id = ?", (user["id"], coin_id_resolved)
                    ).fetchone()
                manual_amount = float(manual["amount"]) if manual else 0.0
                bot_managed = db.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total "
                    "FROM bot_positions WHERE user_id = ? "
                    "AND coin_id = ? AND mode = 'real'",
                    (user["id"], coin_id_resolved)).fetchone()["total"]
                amount = manual_amount + float(bot_managed)
                if amount <= 0:
                    return _coin_result(
                        "Nothing was sold",
                        ["You hold none of this coin in " + label + "."],
                        False, coin_id_resolved, label)
        elif (request.form.get("amount_sol") or "").strip():
            try:
                amount_sol = float(request.form.get("amount_sol"))
            except ValueError:
                return _coin_result(
                    "Nothing was sold", ["Amount must be a number."],
                    False, coin_id_resolved, label)
            if amount_sol <= 0:
                return _coin_result(
                    "Nothing was sold", ["Amount must be more than 0."],
                    False, coin_id_resolved, label)
            sol_price = _sol_price_usd()
            if sol_price is None or price is None or price <= 0:
                return _coin_result(
                    "Nothing was sold",
                    ["Price unavailable right now — the sell needs a "
                     "current price to convert SOL."],
                    False, coin_id_resolved, label)
            amount = amount_sol * sol_price / price
        elif (request.form.get("amount_usd") or "").strip():
            # Quick Buy/Sell popup: sell a $ amount. Convert at the
            # server's own live price — never at a browser-sent price.
            try:
                amount_usd = float(request.form.get("amount_usd"))
            except ValueError:
                return _coin_result(
                    "Nothing was sold", ["Amount must be a number."],
                    False, coin_id_resolved, label)
            if amount_usd <= 0:
                return _coin_result(
                    "Nothing was sold", ["Amount must be more than 0."],
                    False, coin_id_resolved, label)
            if price is None or price <= 0:
                return _coin_result(
                    "Nothing was sold",
                    ["Price unavailable right now — the sell needs a "
                     "current price to convert $."],
                    False, coin_id_resolved, label)
            amount = amount_usd / price
        else:
            try:
                amount = float(request.form.get("amount") or 0)
            except ValueError:
                return _coin_result(
                    "Nothing was sold", ["Amount must be a number."],
                    False, coin_id_resolved, label)
            if amount <= 0:
                return _coin_result(
                    "Nothing was sold", ["Amount must be more than 0."],
                    False, coin_id_resolved, label)
        if mode == "paper":
            # The part of this sell that came from bot-managed
            # rows (manual holdings first, then bots) — shrunk
            # from bot_positions after a successful sell so no
            # ghost row is left behind.
            paper_row = db.execute(
                "SELECT amount FROM paper_holdings WHERE user_id = ? "
                "AND coin_id = ?", (user["id"], coin_id_resolved)
                ).fetchone()
            paper_before = float(paper_row["amount"]) if paper_row else 0.0
            bot_managed_paper = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total "
                "FROM bot_positions WHERE user_id = ? AND coin_id = ? "
                "AND mode = 'paper'",
                (user["id"], coin_id_resolved)).fetchone()["total"]
            sold_from_bots = max(
                0.0, amount - max(
                    0.0, paper_before - float(bot_managed_paper)))
            # Paper sells price from the resolved feed-tick price
            # (helius stream) — never a Jupiter quote.
            result = _paper_sell_for(app, user, coin_id_resolved, amount)
            lines = [result["message"]]
            if result["ok"]:
                lines.append(f"New paper cash: ${result['cash']:,.2f} CAD.")
                _shrink_bot_positions(db, user["id"], coin_id_resolved,
                                      sold_from_bots, "paper")
            return _coin_result(
                "Sold" if result["ok"] else "Nothing was sold",
                lines, result["ok"], coin_id_resolved, label)
        # Real mode: a manual sell covers the whole position —
        # what the user holds by hand PLUS what any bot is
        # managing. The user's Sell button overrides the bot's
        # rules and its selling point: the sell executes for the
        # amount entered, at the live price. No bot exit rule
        # (TP tiers, trailing stop, stop loss, time caps) is
        # consulted on this path. The ONLY rule the override does
        # not touch is the last-credit reserve (fuel==1) below —
        # it stays exactly as confirmed, so the user is never
        # stuck in positions with no fuel left to act.
        manual = db.execute(
            "SELECT amount FROM real_holdings WHERE user_id = ? "
            "AND coin_id = ?", (user["id"], coin_id_resolved)).fetchone()
        manual_amount = float(manual["amount"]) if manual else 0.0
        bot_managed = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM bot_positions "
            "WHERE user_id = ? AND coin_id = ? AND mode = 'real'",
            (user["id"], coin_id_resolved)).fetchone()["total"]
        total_held = manual_amount + float(bot_managed)
        if amount > total_held:
            return _coin_result(
                "Nothing was sold",
                [f"You hold {total_held:.4f} {coin_id_resolved} total "
                 f"in real mode."],
                False, coin_id_resolved, label)
        # Fuel checks come first: they are DB-only and binding,
        # so a refused sell never spends an RPC read — and the
        # last-credit message is never shadowed by a chain read.
        # $GATE fuel gate (2026-11-23): same as the buy path — a
        # real sell costs 1 trade credit. No fuel means no trade:
        # the page shows the no-fuel popup instead of placing the
        # sell. The gate also turns the user's real bots off until
        # they refill. Paper sells never reach this branch.
        if not gate_fuel_mod.enforce_fuel_gate(db, user["id"]):
            return _coin_result(
                "Out of fuel",
                ["You're out of $GATE fuel — this sell needs 1 trade "
                 "credit and you have 0 left.",
                 "Buy a $GATE package to keep trading. Your bots are "
                 "off until you refill."],
                False, coin_id_resolved, label,
                **_gate_no_fuel_context(app, user))
        # Last-credit reserve (James, 2026-11-24): same as the buy
        # path — when fuel is down to its last credit and real
        # positions are open, that credit is reserved to exit every
        # position in one sweep. A single sell here would eat it,
        # so it is refused: the sweep closes every position for
        # that one credit instead.
        if gate_fuel_mod.exit_all_pending(db, user["id"]):
            return _coin_result(
                "Last trade reserved",
                ["Your last $GATE trade is reserved to exit all your "
                 "open positions at once.",
                 "This sell would use it up — single sells can't use "
                 "the last trade. The exit-all sweep closes every "
                 "position for that one credit instead."],
                False, coin_id_resolved, label)
        # Live-balance pre-check: the sell is validated against
        # the wallet's actual on-chain balance for the mint — the
        # chain is the truth for how much can be sold. If the
        # chain read fails, the DB total stands and the executor
        # enforces live. Never sell a guessed amount.
        chain_amount = None
        try:
            wallet = _wallet(app, user["id"])
            if wallet:
                chain, chain_ok = fetch_chain_token_balances(
                    rpc_client_for_config(_cfg(app)), wallet["pubkey"])
                if chain_ok:
                    chain_amount = 0.0
                    for entry in chain:
                        if entry.get("mint") == coin_id_resolved:
                            chain_amount = float(
                                entry.get("ui_amount") or 0)
                            break
        except Exception:
            chain_amount = None
        if chain_amount is not None and amount > chain_amount:
            return _coin_result(
                "Nothing was sold",
                [f"You hold {chain_amount:.4f} {coin_id_resolved} "
                 f"on-chain right now — the sell can't exceed your "
                 f"live balance."],
                False, coin_id_resolved, label)
        # A Jupiter-routed coin trades through the Jupiter executor
        # (mainnet, gated behind James's go-ahead); anything else
        # is a pump.fun mint and trades through the pump.fun
        # executor. With the Jupiter gate off the
        # executor refuses plainly — nothing is built, signed or
        # sent. No bot is consulted on a manual sell: the user's
        # Sell button overrides the bot managing the coin, and
        # `bot_id` from the form is ignored on sells.
        # The venue is the bonding curve (see the buy path
        # above): graduated -> Jupiter, still bonding -> pump.fun,
        # unreadable -> refused plainly.
        rpc = rpc_client_for_config(_cfg(app))
        try:
            complete = is_curve_complete(rpc, coin_id_resolved)
        except Exception:
            complete = None
        if complete is None:
            return _coin_result(
                "Nothing was sold",
                ["Bonding curve unavailable — the trade venue could "
                 "not be determined."],
                False, coin_id_resolved, label)
        if complete:
            result = execute_jupiter_swap(
                None, user["id"], coin_id_resolved, "sell", amount,
                db, _cfg(app))
            venue_label = "via Jupiter (mainnet)"
        else:
            result = execute_real_trade(
                None, user["id"], coin_id_resolved, "sell", amount,
                price or 0.0, db, _cfg(app))
            venue_label = f"on pump.fun ({_cfg(app).network})"
        if result.get("status") != "executed":
            failure_lines = _trade_failure_lines(result, "sell")
            _log(app, user["id"], "real_sell_failed",
                 f"Real sell of {coin_id_resolved} failed — "
                 + " ".join(failure_lines), "real")
            return _coin_result(
                "Nothing was sold", failure_lines, False,
                coin_id_resolved, label)
        # Fuel drawdown: the trade executed — 1 package credit,
        # keyed by the confirmed signature.
        gate_fuel_mod.consume_fuel(
            db, user["id"], None, result.get("signature"))
        _remove_real_holding(db, user["id"], coin_id_resolved, amount)
        # The sold tokens came from the manual holding first, then
        # from bot-managed rows — shrink the bot rows by the part
        # that came from them, so no ghost row is left behind for
        # the bot to keep trying to sell.
        _shrink_bot_positions(
            db, user["id"], coin_id_resolved,
            max(0.0, amount - manual_amount), "real")
        wallet = _wallet(app, user["id"])
        new_sol = (_real_balance(app, wallet["pubkey"])
                   if wallet else None)
        # No trading fee (removed 2026-11-12): the whole sale
        # proceeds go to the seller's wallet, and nothing is
        # recorded as a fee.
        _log(app, user["id"], "real_sell",
             f"Sold {amount:.4f} {coin_id_resolved} {venue_label}. "
             f"Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.", "real")
        _record_pnl_snapshot(
            app, user["id"],
            _snapshot_price_map(app, user["id"]))
        lines = [f"Sold {amount:.4f} {coin_id_resolved} {venue_label}.",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
        if new_sol is not None:
            lines.append(f"New wallet balance: {new_sol:.4f} SOL.")
        return _coin_result("Sold", lines, True, coin_id_resolved, label)
    # ------------------------------------------------------------------
    # Logs: the user's activity/trade log (latest 100, newest first,
    # active mode only) plus the losing-buy analysis (paper mode
    # only, moved here from the deleted P&L page). Page 11.
    # ------------------------------------------------------------------
    @app.route("/logs")
    @login_required
    def logs():
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        activity = _activity_for_mode(db, user["id"], mode, limit=100)
        # Losing-buy analysis (paper trades only): every paper trade
        # paired FIFO per coin — shown as a section on this page.
        # Only recorded data; anything never recorded shows as
        # unavailable, never guessed. Real mode has no paper trades,
        # so the analysis is paper-only by construction.
        analysis = (analysis_mod.analyze_paper_trades(db, user["id"])
                    if mode == "paper" else None)
        return render_template(
            "logs.html", activity=activity, analysis=analysis,
            mode=mode, mode_label=_mode_label(mode, _cfg(app)))

    # ------------------------------------------------------------------
    # Wallet: view / send (two-step) / deposit
    # ------------------------------------------------------------------
    @app.route("/wallet")
    @login_required
    def wallet():
        """The homepage: the user's portfolio total, 24h change and
        chart (recorded snapshots only), BUY/SELL, and the coins
        held — in the fox layout. The page scrolls (owner,
        2026-11-12): the holdings list grows with the page; the
        photo layout is unchanged. The wallet itself (deposit /
        send / withdraw / platform wallets) lives on /wallet/details.
        `?range=` picks the chart range (1h/1d/1w/1mo/1y/all);
        anything else falls back to 24h."""
        user = _current_user(app)
        chart_range = (request.args.get("range") or "1d").strip()
        if chart_range not in pnl_mod.RANGES:
            chart_range = "1d"
        data = _dashboard_data(app, user, include_trenches=False,
                               chart_range=chart_range)
        # $GATE at the top of the holdings list (2026-11-22): the
        # fuel coin comes first; everything else keeps its order
        # (sorted is stable). The trades-left chip comes from
        # inject_globals, so no second balance read here. The sort
        # uses the package mint.
        gate_mint = gate_fuel_mod.gate_mint(_cfg(app))
        if gate_mint:
            data["coin_rows"] = sorted(
                data["coin_rows"],
                key=lambda r: 0 if r.get("coin_id") == gate_mint else 1)
        return render_template("wallet.html", **data)

    @app.route("/wallet/details")
    @login_required
    def wallet_details():
        """The wallet itself: deposit / send / withdraw / platform
        wallets in real mode, the paper balance in paper mode. The
        portfolio home lives on /wallet. One screen: the cards live
        in a fixed-height panel that scrolls internally; the page
        itself never scrolls."""
        user = _current_user(app)
        db = _db(app)
        data = _dashboard_data(app, user, include_trenches=False)
        mode = data["mode"]
        if mode == "paper":
            # Paper mode: the paper balance only. The real wallet,
            # send history and platform wallets live in Real trading
            # — switch mode in the HUD to see them.
            return render_template(
                "wallet_detail.html", **data, intents=[],
                pumpfun_wallet="")
        pumpfun_wallet = db.execute(
            "SELECT pumpfun_wallet FROM users WHERE id = ?",
            (user["id"],)).fetchone()["pumpfun_wallet"]
        intents = db.execute(
            "SELECT id, destination, amount_lamports, token, status, "
            "signature, created_at, mint, amount_base_units, decimals "
            "FROM send_intents WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
        # The history table shows each send in the units it was
        # sent in: SOL for SOL sends, token units for token sends.
        intents = [dict(row) for row in intents]
        for item in intents:
            if item["mint"]:
                item["display_amount"] = \
                    token_send_mod.format_token_amount(
                        item["amount_base_units"], item["decimals"])
            else:
                item["display_amount"] = "%.4f" % lamports_to_sol(
                    item["amount_lamports"])
        return render_template(
            "wallet_detail.html", **data, intents=intents,
            pumpfun_wallet=pumpfun_wallet)

    @app.route("/wallet/reclaim")
    @login_required
    def wallet_reclaim():
        """Preview what reclaiming SOL rent would do: every empty
        token account the wallet owns (closed, rent back to the
        wallet) and every holding worth $0.00 (burned to the
        incinerator, then closed). Nothing here moves anything —
        the page only lists what WOULD happen, with the total SOL
        reclaimed, for the user to confirm on the next step. A
        holding the price feed cannot price is never listed for
        burning. In paper mode the same page is a read-only
        preview: reclaim runs on the real wallet only. NOT a
        trade: no fuel is charged and the fuel gate never applies
        to reclaim (see wallet_reclaim_confirm)."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        wallet = _wallet(app, user["id"])
        if not wallet:
            flash("No app wallet.")
            return redirect(url_for("wallet_details"))
        client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
        try:
            plan = rent_reclaim_mod.plan_reclaim(
                client, wallet["pubkey"],
                lambda mint: _resolve_coin_price(mint)[1])
        except rent_reclaim_mod.ReclaimRefusal as exc:
            flash(str(exc))
            return redirect(url_for("wallet_details"))
        return render_template(
            "rent_reclaim.html", plan=plan, mode=mode,
            mode_label=_mode_label(mode, _cfg(app)),
            wallet_pubkey=wallet["pubkey"])

    @app.route("/wallet/reclaim/confirm", methods=["POST"])
    @login_required
    def wallet_reclaim_confirm():
        """Execute a confirmed reclaim. The confirm is a gate,
        not a formality: the plan is re-read fresh from the chain
        and only items the user confirmed AND that still qualify
        (same account, same balance — still empty, or still worth
        $0.00 with the same amount) are executed. Anything that
        changed since the preview is skipped, never executed.
        Transactions are signed first (seed wiped the moment
        signing is done, before anything is sent), then sent one
        at a time, each confirmed before the next — a failure
        stops the run and what already landed stays landed and is
        reported. NOT a trade: no fuel is consumed and
        enforce_fuel_gate is never called here — a user at 0 fuel
        can still reclaim rent, and reclaiming never turns bots
        off. Burned holdings leave real_holdings so the coins
        list no longer shows them."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        if mode != "devnet":
            # Paper mode is a preview only: reclaim runs on the
            # real wallet, never on paper.
            flash("Reclaim runs on your real wallet — this was a "
                  "preview only. Switch to Real trading to run it.")
            return redirect(url_for("wallet_reclaim"))
        wallet = _wallet(app, user["id"])
        if not wallet:
            flash("No app wallet.")
            return redirect(url_for("wallet_details"))
        confirmed = {}
        for account, amount in zip(
                request.form.getlist("account"),
                request.form.getlist("amount")):
            account = (account or "").strip()
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                continue
            if account and amount >= 0:
                confirmed[account] = amount
        if not confirmed:
            flash("Nothing was confirmed to reclaim.")
            return redirect(url_for("wallet_reclaim"))
        client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
        try:
            plan = rent_reclaim_mod.plan_reclaim(
                client, wallet["pubkey"],
                lambda mint: _resolve_coin_price(mint)[1])
        except rent_reclaim_mod.ReclaimRefusal as exc:
            flash(str(exc))
            return redirect(url_for("wallet_reclaim"))
        verified = [item for item in plan["items"]
                    if item["account"] in confirmed
                    and confirmed[item["account"]] == item["amount"]]
        if not verified:
            flash("Nothing left to reclaim — the wallet changed "
                  "since the preview. Review the list again.")
            return redirect(url_for("wallet_reclaim"))
        chunks = rent_reclaim_mod.build_reclaim_transactions(
            verified, wallet["pubkey"])
        try:
            sol_balance = client.get_balance_lamports(wallet["pubkey"])
        except Exception:
            flash("Could not check your SOL balance right now — "
                  "try again.")
            return redirect(url_for("wallet_reclaim"))
        if sol_balance < len(chunks) * rent_reclaim_mod.TX_FEE_LAMPORTS:
            flash("Not enough SOL for the network fees.")
            return redirect(url_for("wallet_reclaim"))
        signed = []
        try:
            # The key is decrypted into a mutable buffer, used for
            # signing only, and wiped IN PLACE the moment signing
            # is done — before any transaction is sent, as in the
            # SOL / token send flows.
            with temporary_wallet_secret(
                    _db(app), user["id"],
                    _cfg(app).encryption_key) as secret:
                if secret is None:
                    raise ValueError("No app wallet.")
                seed = bytearray(secret[:32])
                try:
                    for instructions in chunks:
                        blockhash = client.get_latest_blockhash()
                        signed.append(rent_reclaim_mod.sign_reclaim(
                            seed, wallet["pubkey"], instructions,
                            blockhash))
                finally:
                    wipe_secret(seed)
        except Exception as exc:
            flash("Reclaim failed: " + str(exc)[:200])
            return redirect(url_for("wallet_reclaim"))
        landed_sigs = []
        failed_reason = None
        for signed_b64, signature in signed:
            tx_sig, reason = send_and_confirm(
                client, signed_b64, signature)
            if tx_sig is None:
                failed_reason = reason or "transaction not confirmed"
                break
            landed_sigs.append(tx_sig)
        landed = verified[:len(landed_sigs)
                          * rent_reclaim_mod.MAX_ACCOUNTS_PER_TX]
        reclaimed = sum(i["rent_lamports"] for i in landed)
        burned = [i for i in landed if i["kind"] == "burn"]
        for item in burned:
            _remove_real_holding(db, user["id"], item["mint"],
                                 item["ui_amount"])
        if landed:
            _log(app, user["id"], "rent_reclaim",
                 f"Reclaimed {lamports_to_sol(reclaimed):.5f} SOL: "
                 f"closed {len(landed)} token account(s)"
                 + (f", burned {len(burned)} $0.00 holding(s)"
                    if burned else "")
                 + f". Signatures: "
                 + ", ".join(s[:12] for s in landed_sigs) + ".",
                 "real")
            flash(f"Reclaimed {lamports_to_sol(reclaimed):.5f} SOL "
                  f"into your wallet — closed {len(landed)} "
                  f"account(s)"
                  + (f", burned {len(burned)} $0.00 holding(s)"
                     if burned else "") + ".")
        if failed_reason is not None:
            flash("Reclaim stopped part-way: " + failed_reason[:200]
                  + (f" — {len(landed)} account(s) already closed."
                     if landed else " Nothing was closed."))
        return redirect(url_for("wallet_details"))

    @app.route("/trenches")
    @login_required
    def trenches():
        """The trenches page (black + gold theme): the top 10 fresh
        pump.fun / PumpSwap tokens by 1-hour gain, price + 1h gain
        + 1h chart per row — tapping a token opens the same Buy /
        Sell / Sell all popup as any other coin. The list live-
        refreshes every 5 seconds (GET /api/trenches) so it never
        sits frozen. A feed failure says the trenches are
        unavailable, never fake rows."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        trenches_rows, trenches_error = _trenches_list(app)
        return render_template(
            "trenches.html", trenches_rows=trenches_rows,
            trenches_error=trenches_error, mode=mode,
            mode_label=_mode_label(mode, _cfg(app)))

    def _coin_return_to(value: str):
        """The coin id behind a return_to of the form "coin:<id>",
        or None. The id must be well-formed AND resolvable to a
        real price (a known coin or a live mint) — anything else is
        not a coin page, so the send falls back to the wallet. The
        resolved id is returned (known coins come back upper-cased),
        so the redirect always lands on a real coin detail page."""
        if not value.startswith("coin:"):
            return None
        coin_id = value[5:]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}", coin_id):
            return None
        resolved, _price, _source = _resolve_coin_price(coin_id)
        return resolved

    @app.route("/wallet/send", methods=["GET", "POST"])
    @login_required
    def wallet_send():
        user = _current_user(app)
        if request.method == "POST":
            _csrf_check()
            destination = (request.form.get("destination") or "").strip()
            # "SOL" case-insensitively; a mint address is kept
            # exactly as pasted — base58 is case-sensitive, and
            # upper-casing a mint would corrupt it.
            token_raw = (request.form.get("token") or "SOL").strip()
            token = "SOL" if token_raw.upper() == "SOL" else token_raw
            amount_text = (request.form.get("amount") or "").strip()
            # SOL, or a token mint address — anything else (a ticker
            # like "USDC", a name) is not a sendable asset here.
            is_token = token != "SOL" and is_valid_address(token)
            if token != "SOL" and not is_token:
                flash("Only SOL or a token mint address can be sent.")
                return redirect(url_for("wallet_send"))
            if not is_valid_address(destination):
                flash("That destination address is not valid.")
                return redirect(url_for("wallet_send"))
            if token == "SOL":
                try:
                    amount = float(amount_text or 0)
                except ValueError:
                    flash("Amount must be a number.")
                    return redirect(url_for("wallet_send"))
                if amount <= 0:
                    flash("Amount must be more than 0.")
                    return redirect(url_for("wallet_send"))
            # The coin detail page's Send posts here with
            # return_to=coin:<id>. A coin that is not a Solana asset
            # is refused here too — the template hides the form, but
            # a crafted POST must not be able to send on the wrong
            # network. The refusal names the coin's network, and no
            # intent is created. (A return_to that names no real coin
            # falls through to the plain wallet send, as before.)
            return_to = (request.form.get("return_to") or "").strip()
            coin_id = _coin_return_to(return_to)
            if coin_id:
                refusal = networks_mod.send_refusal(
                    coin_id, _cfg(app).network)
                if refusal:
                    flash(refusal)
                    # A pump.fun mint has a detail page to return
                    # to; anything else (a major symbol, a crafted
                    # coin id) has no page any more, so the flash
                    # is shown on the wallet send page instead of
                    # being lost to a 404.
                    if is_valid_address(coin_id):
                        return redirect(url_for("coin_detail",
                                                coin_id=coin_id))
                    return redirect(url_for("wallet_send"))
            db = _db(app)
            # Anti-bot / wallet protection: at most 10 sends an hour
            # — a compromised session cannot drain a wallet in one
            # day. Shared by SOL and token sends alike.
            if not rate_limit_mod.record_hit(
                    db, f"send:user:{user['id']}",
                    MAX_SENDS_PER_USER, SEND_USER_WINDOW_SECONDS):
                flash("Too many sends this hour. Please try again "
                      "later.")
                return redirect(url_for("wallet_send"))
            if is_token:
                # Token send: the amount is validated against the
                # wallet's REAL on-chain balance for this mint —
                # never only against the app's holdings table, so a
                # holding the app never recorded is still sendable.
                # No daily SOL cap: no SOL value is sent, only the
                # network fee and the token-account rent.
                wallet = _wallet(app, user["id"])
                if not wallet:
                    flash("No app wallet yet.")
                    return redirect(url_for("wallet_send"))
                client = rpc_client_for_url(_cfg(app), _cfg(app).rpc_url)
                try:
                    inspection = token_send_mod.inspect_token(
                        client, wallet["pubkey"], token, destination)
                except token_send_mod.TokenSendRefusal as exc:
                    flash(str(exc))
                    return redirect(url_for("wallet_send"))
                try:
                    amount_base_units = \
                        token_send_mod.amount_to_base_units(
                            amount_text, inspection["decimals"])
                except token_send_mod.TokenSendRefusal as exc:
                    flash(str(exc))
                    return redirect(url_for("wallet_send"))
                if amount_base_units > inspection["balance"]:
                    ticker = (_token_ticker(token, db=db,
                                           rpc_url=_cfg(app).rpc_url)
                            or "this token")
                    flash("Amount is more than you hold of this token "
                          f"({token_send_mod.format_token_amount(inspection['balance'], inspection['decimals'])} "
                          f"{ticker}).")
                    return redirect(url_for("wallet_send"))
                cur = db.execute(
                    "INSERT INTO send_intents (user_id, destination, "
                    "amount_lamports, token, status, created_at, mint, "
                    "amount_base_units, decimals) "
                    "VALUES (?, ?, 0, ?, 'pending', ?, ?, ?, ?)",
                    (user["id"], destination,
                     _token_ticker(token, db=db,
                                   rpc_url=_cfg(app).rpc_url)
                     or "Unknown coin",
                     _now(), token, amount_base_units,
                     inspection["decimals"]))
                db.commit()
            else:
                lamports = sol_to_lamports(amount)
                # The daily cap (default 5 SOL) applies to SOL value
                # sent — token sends carry no SOL value, so they do
                # not count toward it.
                if _sent_sol_today(db, user["id"]) + amount > \
                        _cfg(app).daily_send_limit_sol:
                    flash(f"Daily send limit is "
                          f"{_cfg(app).daily_send_limit_sol:g} SOL — this "
                          f"send would go over it.")
                    return redirect(url_for("wallet_send"))
                cur = db.execute(
                    "INSERT INTO send_intents (user_id, destination, "
                    "amount_lamports, token, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (user["id"], destination, lamports, token, _now()))
                db.commit()
            # Step 1 done: the user must confirm on the next screen.
            # return_to=coins (from the coin popup's Send) carries
            # through, so the completed send lands back on the coins
            # screen instead of the wallet page. return_to=coin:<id>
            # (from a coin detail page's Send window) carries
            # through to that coin's page instead. return_to and
            # coin_id were resolved above, before the intent was
            # created (the network check lives there).
            if return_to == "coins":
                return redirect(url_for("wallet_send_confirm",
                                        intent_id=cur.lastrowid,
                                        return_to="coins"))
            if coin_id:
                return redirect(url_for("wallet_send_confirm",
                                        intent_id=cur.lastrowid,
                                        return_to=f"coin:{coin_id}"))
            return redirect(url_for("wallet_send_confirm",
                                    intent_id=cur.lastrowid))
        return render_template("send.html")

    @app.route("/wallet/send/<int:intent_id>/confirm", methods=["GET", "POST"])
    @login_required
    def wallet_send_confirm(intent_id: int):
        user = _current_user(app)
        db = _db(app)
        intent = db.execute(
            "SELECT * FROM send_intents WHERE id = ? AND user_id = ?",
            (intent_id, user["id"])).fetchone()
        if not intent:
            abort(404)
        return_to = (request.args.get("return_to")
                     or request.form.get("return_to") or "").strip()
        # "coins" goes back to the coins screen; "coin:<id>" goes
        # back to that coin's detail page; anything else is not a
        # real destination and falls back to the wallet page.
        return_coin_id = _coin_return_to(return_to)
        if return_to != "coins" and return_coin_id is None:
            return_to = ""
        elif return_coin_id is not None:
            return_to = f"coin:{return_coin_id}"
        if request.method == "POST":
            _csrf_check()
            if intent["status"] != "pending":
                flash("This send was already handled.")
                if return_coin_id:
                    return redirect(url_for(
                        "coin_detail", coin_id=return_coin_id))
                return redirect(url_for("dashboard" if return_to else
                                        "wallet"))
            # A token send goes through its own confirm step
            # (on-chain balance re-check, token transaction,
            # long-poll + re-check confirmation). The daily SOL
            # cap does not apply to it — no SOL value is sent.
            if intent["mint"]:
                return _confirm_token_send(app, user, intent,
                                           return_to, return_coin_id)
            # Daily cap, re-checked at confirm time: the intent may
            # have been created yesterday or before other sends.
            if _sent_sol_today(db, user["id"],
                               exclude_intent_id=intent["id"]) + \
                    lamports_to_sol(intent["amount_lamports"]) > \
                    _cfg(app).daily_send_limit_sol:
                db.execute(
                    "UPDATE send_intents SET status = 'failed', "
                    "error = ? WHERE id = ?",
                    ("daily send limit", intent["id"]))
                db.commit()
                _log(app, user["id"], "send_failed",
                     "Send refused: daily send limit.", "real")
                flash(f"Daily send limit is "
                      f"{_cfg(app).daily_send_limit_sol:g} SOL — this "
                      f"send would go over it.")
                return redirect(url_for("wallet"))
            wallet = _wallet(app, user["id"])
            try:
                # The key is decrypted into a mutable buffer, used
                # for one signature, and wiped IN PLACE the moment
                # signing is done — before the transaction is even
                # sent. The finally blocks guarantee the wipe on
                # every path, including a signing failure.
                # Decrypted through the password+email user-key
                # layer, so v1 and v2 wallets both work here.
                with temporary_wallet_secret(
                        _db(app), user["id"],
                        _cfg(app).encryption_key) as secret:
                    if secret is None:
                        raise ValueError("No app wallet.")
                    seed = bytearray(secret[:32])
                    try:
                        pubkey = wallet["pubkey"]
                        client = SolanaRpcClient(_cfg(app).rpc_url)
                        blockhash = client.get_latest_blockhash()
                        signed_b64, signature = build_signed_transfer(
                            seed, pubkey, intent["destination"],
                            intent["amount_lamports"], blockhash)
                    finally:
                        wipe_secret(seed)
                tx_sig = client.send_transaction(signed_b64)
                confirmed = client.confirm_transaction(tx_sig, attempts=3,
                                                       delay_seconds=1.0)
                status = "sent" if confirmed else "sent_unconfirmed"
                db.execute(
                    "UPDATE send_intents SET status = ?, signature = ? "
                    "WHERE id = ?", (status, tx_sig, intent["id"]))
                db.commit()
                _log(app, user["id"], "send",
                     f"Sent {lamports_to_sol(intent['amount_lamports'])} "
                     f"SOL to {intent['destination'][:8]}... "
                     f"signature {tx_sig[:12]}...", "real")
                flash("Send submitted. Signature: " + tx_sig)
            except Exception as exc:
                db.execute(
                    "UPDATE send_intents SET status = 'failed', error = ? "
                    "WHERE id = ?", (str(exc)[:200], intent["id"]))
                db.commit()
                _log(app, user["id"], "send_failed",
                     "Send failed: " + str(exc)[:120], "real")
                flash("Send failed: " + str(exc)[:200])
            if return_coin_id:
                return redirect(url_for("coin_detail",
                                        coin_id=return_coin_id))
            return redirect(url_for("dashboard" if return_to else
                                    "wallet"))
        # The confirm step restates destination, amount and token
        # in plain words. SOL renders as before (lamports → SOL, 4
        # decimals); a token renders in token units from its base
        # units and decimals, with its ticker — never as SOL.
        if intent["mint"]:
            display_amount = token_send_mod.format_token_amount(
                intent["amount_base_units"], intent["decimals"])
            display_token = intent["token"]
            wallet = _wallet(app, user["id"])
            fee_note = (_token_send_fee_note(
                app, wallet["pubkey"], intent) if wallet else
                "Network fee + token account rent if needed, paid "
                "from your SOL.")
        else:
            display_amount = "%.4f" % lamports_to_sol(
                intent["amount_lamports"])
            display_token = intent["token"]
            fee_note = "≈ 0.000005 SOL (network fee, estimated)"
        return render_template("send_confirm.html", intent=intent,
                               display_amount=display_amount,
                               display_token=display_token,
                               fee_note=fee_note,
                               return_to=return_to,
                               return_coin_id=return_coin_id,
                               # The Network row in the template was
                               # rendering the bare config value
                               # ("devnet") from the context processor
                               # — not the network in plain words.
                               # The confirm step must state the
                               # network alongside destination, amount
                               # and token — it is always Solana on
                               # this app's configured network.
                               network=networks_mod.network_label(
                                   _cfg(app).network))

    # ------------------------------------------------------------------
    # Paper trading (shared by the coin popup and the coin detail
    # page). Helpers return result dicts; the callers decide how to
    # present them (the confirmation page). A coin is a pump.fun
    # mint address — base58 addresses are case-sensitive and are
    # NEVER uppercased.
    # The old /paper/buy and /paper/sell endpoints (the dashboard's
    # removed Quick buy card) are gone — buys and sells now live
    # only in the coin popup and the coin detail page.
    # ------------------------------------------------------------------
    def _resolve_coin_price(coin: str):
        """(coin_id, price, source) for a pump.fun mint, or
        (None, None, None) when the coin is unknown. A mint the
        feed has not priced still resolves — with price None and
        source "no data" — so its detail page loads and says "no
        data" instead of 404ing. Single price source only: the
        live feed's fresh tick first; if no fresh tick, ONE batched
        Helius RPC read of the bonding curve (graduated -> Raydium
        pool, the same lookup the stream uses). Source is
        "helius stream" when priced, "no data" when not."""
        if not coin:
            return None, None, None
        if not is_valid_address(coin):
            return None, None, None
        # The live feed first: a fresh validated tick — the
        # bonding-curve price for a mint with no DEX pool
        # (Gate/AuditTest have no trenches-feed price at all).
        feed = app.config.get("LIVE_FEED")
        if feed is not None:
            try:
                price = feed.live_price(coin)
            except Exception:
                price = None
            if price is not None and price > 0:
                return coin, price, "helius stream"
        chained = _chain_prices_for_mints(app, [coin])
        price = chained.get(coin)
        if price is not None and price > 0:
            return coin, float(price), "helius stream"
        return coin, None, "no data"

    def _paper_buy_for(app, user, coin: str, usd: float,
                       bot_id_raw: str) -> dict:
        """One paper buy. Returns {"ok", "message", ...}. The caller
        presents the message. Never raises for a refused buy. The
        fill price is the resolved feed-tick price from
        _resolve_coin_price (source "helius stream") — no Jupiter
        quote price anywhere."""
        db = _db(app)
        coin_id, price, source = _resolve_coin_price(coin)
        if coin_id is None:
            return {"ok": False,
                    "message": "Unknown coin — nothing was bought."}
        if usd <= 0:
            return {"ok": False,
                    "message": "Amount must be more than 0."}
        # Optional managing bot: the bot looks after this coin — the
        # silent loop governs the position by that bot's rules once
        # its silent trading is on. The bot must be the user's own
        # and in paper mode, or the buy does not happen at all.
        bot = None
        if (bot_id_raw or "").strip():
            try:
                bot_id = int(bot_id_raw)
            except ValueError:
                return {"ok": False, "message":
                        "That bot choice is not valid — nothing was "
                        "bought."}
            bot = db.execute(
                "SELECT * FROM bots WHERE id = ? AND user_id = ?",
                (bot_id, user["id"])).fetchone()
            if not bot:
                return {"ok": False, "message":
                        "That bot isn't yours — nothing was bought."}
            if bot["mode"] != "paper":
                return {"ok": False, "message":
                        f"Bot '{display_bot_name(bot['name'])}' is a real-mode bot — pick "
                        f"a paper bot to look after a paper buy. "
                        f"Nothing was bought."}
        if price is None:
            return {"ok": False,
                    "message": "Price unavailable right now."}
        try:
            amount, usd = ledger_buy(db, user["id"], coin_id, usd, price,
                                     _now(),
                                     bot_id=bot["id"] if bot else None)
        except InsufficientCash:
            return {"ok": False, "message": "Not enough paper cash."}
        # First time this coin is held: fetch its official logo
        # (on-chain Metaplex metadata, then DexScreener) and store
        # it so the held coin renders with its image. Best-effort —
        # a missing logo never blocks the trade, and only held coins
        # are ever fetched.
        try:
            ensure_coin_meta(db, coin_id, rpc_url=_cfg(app).rpc_url)
        except Exception:
            pass
        if bot is not None:
            # Record the position under the managing bot so the silent
            # loop's rule evaluation (stop loss, sell ladder, trailing
            # stop) governs it per that bot's rules and limits.
            _open_position(db, bot, coin_id, amount, price)
        _log(app, user["id"], "paper_buy",
             f"Bought {amount:.4f} {coin_id} for ${usd:.2f} (paper, "
             f"price source: {source})"
             + (f" — looked after by bot '{display_bot_name(bot['name'])}'." if bot else "."),
             "paper")
        _record_pnl_snapshot(
            app, user["id"],
            _snapshot_price_map(app, user["id"]))
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Bought {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper).",
                "amount": amount, "usd": usd, "bot": bot, "cash": cash,
                "coin": coin_id}

    def _paper_sell_for(app, user, coin: str, amount: float) -> dict:
        """One paper sell. Returns {"ok", "message", ...}. The fill
        price is the resolved feed-tick price from
        _resolve_coin_price (source "helius stream") — no Jupiter
        quote price anywhere."""
        db = _db(app)
        coin_id, price, source = _resolve_coin_price(coin)
        if coin_id is None:
            return {"ok": False,
                    "message": "Unknown coin — nothing was sold."}
        if amount <= 0:
            return {"ok": False,
                    "message": "Amount must be more than 0."}
        if price is None:
            return {"ok": False,
                    "message": "Price unavailable right now."}
        try:
            usd = ledger_sell(db, user["id"], coin_id, amount, price,
                              _now(), exit_reason="manual sell")
        except InsufficientHolding:
            return {"ok": False,
                    "message": "You don't hold that much."}
        _log(app, user["id"], "paper_sell",
             f"Sold {amount:.4f} {coin_id} for ${usd:.2f} (paper, "
             f"price source: {source}).", "paper")
        _record_pnl_snapshot(
            app, user["id"],
            _snapshot_price_map(app, user["id"]))
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Sold {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper).",
                "amount": amount, "usd": usd, "cash": cash,
                "coin": coin_id}

    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------
    @app.route("/bots")
    @login_required
    def bots():
        user = _current_user(app)
        # Only this mode's bots are shown — paper and real bots never
        # mix on a page. The Trenches preset is a paper bot, so its
        # card only appears in paper mode. The portfolio totals come
        # along for the balance header (paper total, or real SOL +
        # USD approx) — the same data as the homepage, same mode.
        data = _dashboard_data(app, user, include_trenches=False)
        bots = data["bots"]
        memecoin_bots = [b for b in bots
                         if b["category"] == TRENCHES_CATEGORY]
        # The Signer Bot card at the bottom shows what the signing
        # bot is doing: its current tasks, each with the time it was
        # tasked, and how many real transactions it has signed.
        # Pending bot_proposals are the task queue (created_at is
        # when the task was tasked); trade_ledger mode='real' rows
        # are the trades the signing bot executed on the configured
        # network — paper
        # rows are simulated, never signed, and never counted. A
        # figure that cannot be read is None, and the template
        # shows "—" instead of a guess. The Support ID rides along
        # in _dashboard_data as support_id.
        db = _db(app)
        signing_tasks = [dict(row) for row in db.execute(
            "SELECT p.id, p.bot_id, p.kind, p.coin_id, p.side, "
            "p.detail, p.created_at, b.name AS bot_name "
            "FROM bot_proposals p LEFT JOIN bots b ON b.id = p.bot_id "
            "WHERE p.user_id = ? AND p.status = 'pending' "
            "ORDER BY p.created_at DESC, p.id DESC LIMIT 10",
            (user["id"],)).fetchall()]
        try:
            signing_tx_count = db.execute(
                "SELECT COUNT(*) FROM trade_ledger WHERE user_id = ? "
                "AND mode = 'real'", (user["id"],)).fetchone()[0]
        except Exception:
            signing_tx_count = None
        # Preset bots for the Preset button on Page 6 (Clyde first,
        # then Trenches, then Scanner): each entry carries its
        # plain-words rules (from summarize_rules) plus its notes —
        # the rules the engine cannot enforce as written, stated
        # plainly — and the URL that creates it. The list is data
        # (rules.preset_bots), so a new preset is one more entry.
        presets = [{
            "key": p["key"],
            "name": p["name"],
            "tagline": p["tagline"],
            "rules_lines": summarize_rules(p["rules"]).split("\n"),
            "notes": p["notes"],
            "create_url": url_for(p["endpoint"]),
        } for p in preset_bots()]
        # The Page 6 preset window is a single Clyde button (owner,
        # 2026-11-11): only Clyde's stats are rendered there, from
        # the same stored values the create route writes
        # (strategy 'bold', buy size 20%, stop loss
        # CLYDE_STOP_LOSS_PCT, max 3 open). Gnat joined the window
        # 2026-11-21 as a second button with the same shape: its
        # stats are the values its create route writes (strategy
        # 'bold', buy size GNAT_BUY_SIZE_PCT, stop loss
        # GNAT_STOP_LOSS_PCT, max 3 open).
        clyde_preset = next(p for p in presets if p["key"] == "clyde")
        clyde_preset.update({
            "strategy": "bold",
            "buy_size_pct": 15,
            "stop_loss_pct": int(CLYDE_STOP_LOSS_PCT),
            "max_open_trades": 3,
        })
        # Trenches returned to the window 2026-11-24 (James):
        # second button, same shape — its stats are the values its
        # create route writes (strategy 'bold', buy size 20%,
        # stop loss 7%, max 3 open).
        trenches_preset = next(p for p in presets if p["key"] == "trenches")
        trenches_preset.update({
            "strategy": "bold",
            "buy_size_pct": 20,
            "stop_loss_pct": 7,
            "max_open_trades": 3,
        })
        gnat_preset = next(p for p in presets if p["key"] == "gnat")
        gnat_preset.update({
            "strategy": "bold",
            "buy_size_pct": GNAT_BUY_SIZE_PCT,
            "stop_loss_pct": int(GNAT_STOP_LOSS_PCT),
            "max_open_trades": 3,
        })
        # Pit Sweeper joined the window 2026-11-23 as a third
        # button with the same shape: its stats are the values its
        # create route writes (strategy 'bold', buy size
        # PIT_BUY_SIZE_PCT, stop loss PIT_STOP_LOSS_PCT, max 1 open
        # — one trade at a time, 0.05 SOL from the 0.20 SOL
        # bankroll cap). The button's name tag carries the active
        # shared-brain version (1.0 first, then 1.1, 1.2...), read
        # only here — the version row is seeded by the create
        # route, never by a page view.
        pit_preset = next(p for p in presets if p["key"] == "pit")
        pit_preset.update({
            "strategy": "bold",
            "buy_size_pct": PIT_BUY_SIZE_PCT,
            "stop_loss_pct": PIT_STOP_LOSS_PCT,
            "max_open_trades": 1,
        })
        _pit_active = current_pit_version(_db(app))
        _pit_version = (_pit_active["version"] if _pit_active
                        else PIT_PRESET_VERSION_INITIAL)
        pit_preset["name"] = f"{PIT_PRESET_NAME} v{_pit_version}"
        # Middy joined the window 2026-11-24 as a fourth button
        # with the same shape: its stats are the values its create
        # route writes (strategy 'bold', buy size 20%, stop loss
        # 5.5%, max 2 open). The card also shows Middy's VERIFIED
        # PAPER results from the trade ledger (site-wide, paper
        # mode only) — proof on card, paper results never presented
        # as real profit. No paper trades yet means no numbers,
        # never a guess.
        middy_preset = next(p for p in presets if p["key"] == "middy")
        middy_preset.update({
            "strategy": "bold",
            "buy_size_pct": MIDDY_BUY_SIZE_PCT,
            "stop_loss_pct": MIDDY_STOP_LOSS_PCT,
            "max_open_trades": 2,
            "paper_wl": kind_wl_summary(
                _db(app), MIDDY_CATEGORY, mode="paper"),
        })
        return render_template("bots.html", **data,
                               memecoin_bots=memecoin_bots,
                               signing_tasks=signing_tasks,
                               signing_tx_count=signing_tx_count,
                               presets=presets,
                               clyde_preset=clyde_preset,
                               trenches_preset=trenches_preset,
                               gnat_preset=gnat_preset,
                               pit_preset=pit_preset,
                               middy_preset=middy_preset)

    @app.route("/bots/create", methods=["GET", "POST"])
    @login_required
    def bot_create():
        user = _current_user(app)
        if request.method == "POST":
            _csrf_check()
            if not rate_limit_mod.record_hit(
                    _db(app), f"bot-create:user:{user['id']}",
                    MAX_BOT_CREATES_PER_USER,
                    BOT_CREATE_USER_WINDOW_SECONDS):
                flash("Too many bots created this hour. Please try "
                      "again later.")
                return redirect(url_for("bots"))
            name = (request.form.get("name") or "").strip() or "My Bot"
            # The category and strategy pickers are gone — bots are
            # built by builderBot's chatbox (GET, below), which
            # writes validated rules. This POST stays as a plain
            # creation endpoint: strategy is always "balanced" and
            # the bot carries no category; the Trenches/Scanner
            # presets live on their own routes.
            strategy = "balanced"
            mode = (request.form.get("mode") or "paper").lower()
            if mode not in ("paper", "real"):
                mode = "paper"
            # Every bot trades on pump.fun — there is no other
            # platform to pick. The bot's coins are pump.fun mint
            # addresses (a signals bot starts with none — the user
            # adds mints on the bot's page).
            platform = "pump.fun"
            category = ""
            coins = [c.strip() for c in
                     (request.form.get("coins") or "").split(",")]
            coins = [c for c in coins if is_valid_address(c)]
            try:
                buy_size = float(request.form.get("buy_size_pct") or 20)
                stop_loss = float(request.form.get("stop_loss_pct") or 7)
                max_open = int(request.form.get("max_open_trades") or 3)
            except ValueError:
                flash("Buy size, stop loss, and max open trades must "
                      "be numbers.")
                return redirect(url_for("bot_create"))
            buy_size = min(max(buy_size, 1), 100)
            stop_loss = min(max(stop_loss, 1), 50)
            max_open = min(max(max_open, 1), 5)
            token = new_token()
            db = _db(app)
            rules_json = ""
            # Silent is NEVER on at creation — the user enables it
            # afterwards through the confirm flow. Any silent value in
            # the form is ignored on purpose.
            cur = db.execute(
                "INSERT INTO bots (user_id, name, strategy, coins, "
                "buy_size_pct, stop_loss_pct, mode, silent, "
                "max_open_trades, api_token_hash, category, platform, "
                "rules_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (user["id"], name, strategy, json.dumps(coins),
                 buy_size, stop_loss, mode, max_open, hash_token(token),
                 category, platform, rules_json))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{name}' ({mode}, "
                 f"{platform}). "
                 f"Silent trading is off.", mode)
            # Token shown exactly once, on this page only.
            return render_template("bot_created.html", bot_id=cur.lastrowid,
                                   name=name, token=token)
        # GET: the builderBot chatbox. It asks simple questions one
        # at a time and builds the bot from the answers — the
        # category/strategy pickers are gone.
        identity = builderbot_mod.ensure_builderbot(
            _db(app), user["id"])
        state = builderbot_mod.start_conversation(
            _db(app), user["id"], _cfg(app).encryption_key)
        return render_template(
            "builderbot.html", builder_id=identity["builder_id"],
            messages=state["messages"],
            suggestions=state["suggestions"])

    @app.route("/api/builderbot", methods=["POST"])
    @login_required
    def api_builderbot():
        """One builderBot chatbox turn: {"message": "..."} (JSON, with
        the session CSRF token, same as /api/assistant). The answer
        is scrubbed before storage, parsed deterministically, and
        shown back for a yes before any bot is created."""
        if not request.is_json:
            return jsonify({"error": "json required"}), 400
        body = request.get_json()
        if body.get("csrf_token") != session.get("csrf_token"):
            return jsonify({"error": "bad csrf token"}), 400
        user = _current_user(app)
        db = _db(app)
        enc_key = _cfg(app).encryption_key
        if body.get("action") == "start":
            state = builderbot_mod.start_conversation(db, user["id"],
                                                      enc_key)
            return jsonify(state)
        result = builderbot_mod.answer(
            db, user["id"], enc_key, body.get("message", ""))
        if result.get("created"):
            _log(app, user["id"], "bot_create",
                 "Created a bot with builderBot.", "")
        if result.get("paper_reset") is not None:
            _log(app, user["id"], "paper_reset",
                 f"Paper balance reset to "
                 f"${result['paper_reset']:,.2f} CAD via builderBot "
                 f"(paper positions and history cleared; bot "
                 f"win/loss counts kept).", "paper")
        return jsonify(result)

    @app.route("/bots/preset/trenches", methods=["POST"])
    @login_required
    def bot_preset_trenches():
        """Create the Trenches memecoin bot (Memecoin build preset).

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Trenches bot
        already exists in this mode, go to it instead of making a
        duplicate. The stored name carries the mode suffix
        (Trenchesdemobot / Trenchesrealsol), never shown — the Bots
        page still reads Trenches.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        # The mode the bot is created in: the session's current
        # trading mode. The stored session value is 'devnet' for
        # real trading; bots store 'real' (same mapping as the bots
        # list in _dashboard_data).
        bot_mode = _bot_mode(db, user["id"])
        # Match by exact preset name, not by rules kind: builderBot
        # bots can carry Trenches-kind rules under another name (live
        # 2026-11-24: Luna, bot 3), and a kind match wrongly blocked
        # creating a real Trenches bot. Same shape as Clyde/Gnat/Middy.
        # Scoped to this mode: a paper Trenches must not block a real
        # one (live 2026-11-24: paper bot 9 blocked Real-mode creation).
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND name = ? AND mode = ?",
            (user["id"], TRENCHES_CATEGORY,
             suffixed_name(TRENCHES_PRESET_NAME, bot_mode),
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Trenches bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, ?, 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], suffixed_name(TRENCHES_PRESET_NAME, bot_mode),
             bot_mode,
             hash_token(token),
             TRENCHES_CATEGORY, json.dumps(trenches_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{TRENCHES_PRESET_NAME}' (Memecoin build "
             f"preset, {bot_mode}). Silent trading is off.", bot_mode)
        flash(f"Created the {TRENCHES_PRESET_NAME} bot (Memecoin build, "
              f"{bot_mode}). Turn silent trading on when you are ready — "
              f"it only trades within its rules and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/scanner", methods=["POST"])
    @login_required
    def bot_preset_scanner():
        """Create the Scanner sniper bot (∆scanner∆ preset).

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Scanner bot
        already exists in this mode, go to it instead of making a
        duplicate. The stored name carries the mode suffix
        (Scannerdemobot / Scannerrealsol), never shown.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot_mode = _bot_mode(db, user["id"])
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND rules_json LIKE ? AND mode = ?",
            (user["id"], SCANNER_CATEGORY, '%"kind": "scanner"%',
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Scanner bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', 15, 7, ?, 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], suffixed_name(SCANNER_PRESET_NAME, bot_mode),
             bot_mode, hash_token(token),
             SCANNER_CATEGORY, json.dumps(scanner_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{SCANNER_PRESET_NAME}' ({SCANNER_DISPLAY} "
             f"preset, {bot_mode}). Silent trading is off.", bot_mode)
        flash(f"Created the {SCANNER_PRESET_NAME} bot "
              f"({SCANNER_DISPLAY}, {bot_mode}). Turn silent trading on "
              f"when you are ready — it only trades within its rules "
              f"and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/clyde", methods=["POST"])
    @login_required
    def bot_preset_clyde():
        """Create the Clyde preset bot.

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Clyde bot already
        exists in this mode, go to it instead of making a duplicate.
        Stop loss 4% (Clyde's hard number, on the bot row), buy
        size 15% and max 3 open trades (the preset defaults).
        The stored name carries the mode suffix (Clydedemobot /
        Clyderealsol), never shown.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot_mode = _bot_mode(db, user["id"])
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND name = ? AND mode = ?",
            (user["id"], CLYDE_CATEGORY,
             suffixed_name(CLYDE_PRESET_NAME, bot_mode),
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Clyde bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', 15, ?, ?, 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], suffixed_name(CLYDE_PRESET_NAME, bot_mode),
             CLYDE_STOP_LOSS_PCT, bot_mode,
             hash_token(token), CLYDE_CATEGORY,
             json.dumps(clyde_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{CLYDE_PRESET_NAME}' (Clyde preset, "
             f"{bot_mode}). Silent trading is off.", bot_mode)
        flash(f"Created the {CLYDE_PRESET_NAME} bot (Clyde preset, "
              f"{bot_mode}). Turn silent trading on when you are ready — "
              f"it only trades within its rules and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/gnat", methods=["POST"])
    @login_required
    def bot_preset_gnat():
        """Create the Gnat preset bot.

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Gnat bot already
        exists in this mode, go to it instead of making a duplicate.
        Stop loss 18% (Gnat's hard number, on the bot row), buy
        size 18% (the fallback cap — the loop buys a fixed 0.25
        SOL when a live SOL price exists) and max 3 open trades
        (the preset defaults). The stored name carries the mode
        suffix (Gnatdemobot / Gnatrealsol), never shown.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot_mode = _bot_mode(db, user["id"])
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND name = ? AND mode = ?",
            (user["id"], GNAT_CATEGORY,
             suffixed_name(GNAT_PRESET_NAME, bot_mode),
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Gnat bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', ?, ?, ?, 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], suffixed_name(GNAT_PRESET_NAME, bot_mode),
             GNAT_BUY_SIZE_PCT,
             GNAT_STOP_LOSS_PCT, bot_mode, hash_token(token), GNAT_CATEGORY,
             json.dumps(gnat_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{GNAT_PRESET_NAME}' (Gnat preset, "
             f"{bot_mode}). Silent trading is off.", bot_mode)
        flash(f"Created the {GNAT_PRESET_NAME} bot (Gnat preset, "
              f"{bot_mode}). Turn silent trading on when you are ready — "
              f"it only trades within its rules and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/pit", methods=["POST"])
    @login_required
    def bot_preset_pit():
        """Create the Pit Sweeper preset bot.

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Pit bot already
        exists in this mode, go to it instead of making a duplicate.
        The bot is created on the active shared-brain version
        (1.0 first, then 1.1, 1.2...) and its name tag carries
        that version; its stored rules are that version's snapshot.
        Buy size 25% is the fallback cap — the loop buys a fixed
        0.05 SOL per trade from the 0.20 SOL bankroll cap when a
        live SOL price exists. Stop loss 4.9% is the trigger from
        the versioned rules; the realized loss can reach about
        -9.9% at 5% exit slippage — that is stated on the preset,
        never promised away. The stored name carries the mode suffix
        after the version tag ("Pit Sweeper v1.0demobot"), never shown.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot_mode = _bot_mode(db, user["id"])
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND name LIKE ? AND mode = ?",
            (user["id"], PIT_CATEGORY, f"{PIT_PRESET_NAME}%",
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Pit Sweeper bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        # Seed the v1.0 version row (the complete first build) if
        # the shared brain has no version yet, then create the bot
        # on the ACTIVE version's rules — never a stale snapshot.
        version = ensure_pit_version_1_0(db)
        active_rules = pit_version_rules(db) or pit_ruleset()
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform, "
            "preset_version, bankroll_sol) "
            "VALUES (?, ?, 'bold', '[]', ?, ?, ?, 0, 1, ?, ?, ?, "
            "'pump.fun', ?, ?)",
            (user["id"],
             suffixed_name(f"{PIT_PRESET_NAME} v{version}", bot_mode),
             PIT_BUY_SIZE_PCT, PIT_STOP_LOSS_PCT, bot_mode,
             hash_token(token),
             PIT_CATEGORY, json.dumps(active_rules),
             version, PIT_BANKROLL_SOL))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{PIT_PRESET_NAME} v{version}' (Pit Sweeper "
             f"preset, {bot_mode}, 0.20 SOL bankroll cap). Silent trading "
             f"is off.", bot_mode)
        flash(f"Created the {PIT_PRESET_NAME} v{version} bot (Pit "
              f"Sweeper preset, {bot_mode}). Turn silent trading on when "
              f"you are ready — it only trades within its rules and "
              f"limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/middy", methods=["POST"])
    @login_required
    def bot_preset_middy():
        """Create the Middy preset bot.

        Creates in the session's current mode (paper or real),
        silent off. One per user per mode — if a Middy bot already
        exists in this mode, go to it instead of making a duplicate.
        Stop loss 5.5% (Middy's hard number, on the bot row), buy
        size 20% of balance per trade and max 2 open trades (the
        preset defaults). Paper first: the $20 of real money
        James set aside stays put until paper results verify
        actual profit — nothing here funds or enables real
        trading. The stored name carries the mode suffix
        (Middydemobot / Middyrealsol), never shown.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot_mode = _bot_mode(db, user["id"])
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND name = ? AND mode = ?",
            (user["id"], MIDDY_CATEGORY,
             suffixed_name(MIDDY_PRESET_NAME, bot_mode),
             bot_mode)).fetchone()
        if existing:
            flash("You already have a Middy bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', ?, ?, ?, 0, 2, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], suffixed_name(MIDDY_PRESET_NAME, bot_mode),
             MIDDY_BUY_SIZE_PCT,
             MIDDY_STOP_LOSS_PCT, bot_mode, hash_token(token), MIDDY_CATEGORY,
             json.dumps(middy_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{MIDDY_PRESET_NAME}' (Middy preset, "
             f"{bot_mode}). Silent trading is off.", bot_mode)
        flash(f"Created the {MIDDY_PRESET_NAME} bot (Middy preset, "
              f"{bot_mode}). Turn silent trading on when you are ready — "
              f"it only trades within its rules and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/<int:bot_id>")
    @login_required
    def bot_detail(bot_id: int):
        user = _current_user(app)
        bot = _db(app).execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        proposals = _db(app).execute(
            "SELECT * FROM bot_proposals WHERE bot_id = ? AND user_id = ? "
            "ORDER BY id DESC", (bot_id, user["id"])).fetchall()
        prices = _feed_price_list(app)
        rules, rules_error = load_rules(bot)
        # builderBot's trade ledger: wins, losses, win rate and net
        # P&L for this bot, its recent fills, and every rule
        # adjustment it made (with the reason it gave).
        wl_summary = bot_wl_summary(_db(app), bot_id)
        trades = recent_trades(_db(app), bot_id)
        adjustments = list_adjustments(_db(app), bot_id)
        # Pit Sweeper: the per-version win/loss record (every trade
        # is tagged with the preset version it ran under) and the
        # shared brain's numbered changelog, so any change can be
        # pinpointed and any update scrapped to a previous version.
        version_wl = None
        pit_change_log = None
        if rules is not None and rules_error is None \
                and rules.get("kind") == "pit":
            version_wl = version_wl_summary(_db(app), bot_id)
            pit_change_log = pit_changelog(_db(app))
        # Middy: the site-wide win/loss aggregate across every
        # Middy copy, all users summed — shown beside the bot's own
        # record. Counts only, from the trade ledger (real sells
        # carry the signer bot's executed-transaction records);
        # no other user's individual results are ever shown.
        kind_wl = None
        if bot["category"] == MIDDY_CATEGORY:
            kind_wl = kind_wl_summary(_db(app), MIDDY_CATEGORY)
        # The adjustment reason is stored encrypted; only this page
        # (the display layer) decrypts it for the owner. builderBot
        # itself never decrypts anything but its own chat state.
        enc_key = _cfg(app).encryption_key
        for adjustment in adjustments:
            try:
                adjustment["reason"] = decrypt_secret(
                    adjustment["reason"], enc_key).decode("utf-8")
            except Exception:
                adjustment["reason"] = "(could not read this note)"
        return render_template("bot_detail.html", bot=bot,
                               proposals=proposals, prices=prices,
                               rules_summary=summarize_rules(rules),
                               rules_error=rules_error,
                               wl_summary=wl_summary, trades=trades,
                               adjustments=adjustments,
                               version_wl=version_wl,
                               kind_wl=kind_wl,
                               pit_change_log=pit_change_log)

    @app.route("/bots/<int:bot_id>/edit", methods=["GET", "POST"])
    @login_required
    def bot_edit(bot_id: int):
        user = _current_user(app)
        db = _db(app)
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        if request.method == "POST":
            _csrf_check()
            # Strategy and category are no longer editable — the
            # pickers are gone (bots are built by builderBot, and
            # the Trenches/Scanner categories come from their
            # presets). The stored values stay exactly as they are.
            strategy = bot["strategy"]
            mode = (request.form.get("mode") or bot["mode"]).lower()
            if mode not in ("paper", "real"):
                mode = bot["mode"]
            # The name the form shows is the plain display name (the
            # stored mode suffix is stripped at render). Put the
            # suffix back for a preset bot, in the (possibly new)
            # mode, so an edit — or a mode switch — never silently
            # drops it; a custom name is stored exactly as typed.
            typed = (request.form.get("name") or "").strip()
            base = preset_base_name(typed or bot["name"])
            if base is not None:
                name = suffixed_name(base, mode)
            else:
                name = typed or bot["name"]
            platform = bot["platform"] or "pump.fun"
            category = bot["category"] or ""
            rules_json = bot["rules_json"]
            coins = [c.strip() for c in
                     (request.form.get("coins") or "").split(",")]
            coins = ([c for c in coins if is_valid_address(c)]
                     or json.loads(bot["coins"]))
            try:
                buy_size = float(request.form.get("buy_size_pct")
                                 or bot["buy_size_pct"])
                stop_loss = float(request.form.get("stop_loss_pct")
                                  or bot["stop_loss_pct"])
                max_open = int(request.form.get("max_open_trades")
                               or bot["max_open_trades"])
            except ValueError:
                flash("Buy size, stop loss, and max open trades must "
                      "be numbers.")
                return redirect(url_for("bot_edit", bot_id=bot_id))
            # Mode can only change while the bot holds no positions —
            # otherwise its open trades would belong to the wrong ledger.
            open_positions = db.execute(
                "SELECT COUNT(*) c FROM bot_positions WHERE bot_id = ?",
                (bot_id,)).fetchone()["c"]
            if mode != bot["mode"] and open_positions:
                flash("Close this bot's open positions before switching "
                      "it between paper and real. Mode left unchanged.")
                mode = bot["mode"]
            db.execute(
                "UPDATE bots SET name = ?, strategy = ?, coins = ?, "
                "buy_size_pct = ?, stop_loss_pct = ?, mode = ?, "
                "max_open_trades = ?, category = ?, platform = ?, "
                "rules_json = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (name, strategy, json.dumps(coins), min(max(buy_size, 1), 100),
                 min(max(stop_loss, 1), 50), mode,
                 min(max(max_open, 1), 5), category, platform, rules_json,
                 _now(), bot_id, user["id"]))
            db.commit()
            _log(app, user["id"], "bot_edit", f"Edited bot '{name}'.", mode)
            # Silent is changed only through the confirm flow, never by
            # a bare form flip. If the checkbox differs from what is
            # stored, save the other edits and send the user to confirm.
            want_silent = 1 if request.form.get("silent") == "on" else 0
            if want_silent != int(bot["silent"]):
                return redirect(url_for("bot_silent", bot_id=bot_id,
                                        to=want_silent))
            flash("Bot updated.")
            return redirect(url_for("bot_detail", bot_id=bot_id))
        return render_template("bot_form.html", bot=bot)

    @app.route("/bots/<int:bot_id>/silent", methods=["GET", "POST"])
    @login_required
    def bot_silent(bot_id: int):
        """Silent mode confirm flow. GET shows what silent trading does
        and its limits; POST applies the change only with an explicit
        confirm. Silent is off by default and never flips on a bare
        form submit."""
        user = _current_user(app)
        db = _db(app)
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        if request.method == "POST":
            _csrf_check()
            if request.form.get("confirmed") != "yes":
                flash("Silent setting left unchanged.")
                return redirect(url_for("bot_detail", bot_id=bot_id))
            want = request.form.get("silent")
            if want not in ("0", "1"):
                flash("That silent value is not valid.")
                return redirect(url_for("bot_detail", bot_id=bot_id))
            want = int(want)
            if want == int(bot["silent"]):
                flash("Silent setting was already that way.")
                return redirect(url_for("bot_detail", bot_id=bot_id))
            db.execute("UPDATE bots SET silent = ?, updated_at = ? "
                       "WHERE id = ? AND user_id = ?",
                       (want, _now(), bot_id, user["id"]))
            db.commit()
            _log(app, user["id"],
                 "silent_on" if want else "silent_off",
                 f"Silent trading turned "
                 f"{'ON' if want else 'OFF'} for bot '{display_bot_name(bot['name'])}' "
                 f"({bot['mode']}).", bot["mode"])
            flash("Silent trading turned "
                  + ("ON" if want else "OFF")
                  + f" for '{display_bot_name(bot['name'])}'.")
            return redirect(url_for("bot_detail", bot_id=bot_id))
        try:
            want = int(request.args.get("to", "1"))
        except ValueError:
            want = 1
        want = 1 if want else 0
        return render_template("silent_confirm.html", bot=bot,
                               want_silent=want,
                               network=_cfg(app).network)

    @app.route("/bots/<int:bot_id>/rules/clear", methods=["POST"])
    @login_required
    def bot_rules_clear(bot_id: int):
        """Drop a bot's custom rules — it goes back to the standard
        v1 signals. The confirm is the button's own prompt."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        rules, _err = load_rules(bot)
        if rules is None:
            flash("That bot has no custom rules to clear.")
            return redirect(url_for("bot_detail", bot_id=bot_id))
        db.execute("UPDATE bots SET rules_json = '', updated_at = ? "
                   "WHERE id = ? AND user_id = ?",
                   (_now(), bot_id, user["id"]))
        db.commit()
        _log(app, user["id"], "rules_clear",
             f"Cleared custom rules for bot '{display_bot_name(bot['name'])}' — back to "
             f"standard signals.", bot["mode"])
        flash("Custom rules cleared — the bot is back to standard signals.")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    def _delete_bot(db, user_id: int, bot_id: int) -> None:
        """Hard-delete a bot and every row it owns, in dependency
        order. bot_positions, bot_proposals, scanner_marks and
        scanner_top10 all reference bots(id) with foreign keys
        enforced, so the child rows must go first — deleting only
        positions/proposals left scanner rows behind and the bot row
        delete then failed with a foreign-key error, which made the
        Delete button look dead (any scanner bot that had ever
        scanned could not be deleted at all). The bot's API token
        hash and rules live on the bots row itself, so they go with
        it. Paper holdings the bot managed are NOT sold — the coins
        stay in the user's paper holdings, only the bot's management
        record goes. builderBot's stored data for the bot (its
        adjustment log and trade-ledger rows) is purged at the same
        moment — deleting a bot spoofs it from builderBot's memory
        too. Every table carrying FOREIGN KEY (bot_id) REFERENCES
        bots (id) is covered: purge_bot_data takes
        builderbot_adjustments + trade_ledger, trade_fees is
        detached (SET NULL, history kept), and the rest —
        scanner_marks, scanner_top10, scanner_signal_confirmations,
        bot_buy_cooldowns, bot_signal_log, sell_claims,
        pit_watchlist, bot_proposals, bot_positions — is deleted
        here, before the bot row.
        """
        builderbot_mod.purge_bot_data(db, bot_id, user_id)
        db.execute("DELETE FROM scanner_marks WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM scanner_top10 WHERE bot_id = ?",
                   (bot_id,))
        # 2026-11-26: the same bug came back for five more tables —
        # scanner_signal_confirmations, bot_buy_cooldowns,
        # bot_signal_log, sell_claims and pit_watchlist all carry
        # FOREIGN KEY (bot_id) REFERENCES bots (id) and were never
        # cleaned, so deleting any bot that had scanned 500'd the
        # POST and the phone showed a white error page. Bot 7
        # (Middy) hit it via 2-scan confirmations + signal-log rows.
        db.execute("DELETE FROM scanner_signal_confirmations WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM bot_buy_cooldowns WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM bot_signal_log WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM sell_claims WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user_id))
        db.execute("DELETE FROM pit_watchlist WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM bot_proposals WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user_id))
        db.execute("DELETE FROM bot_positions WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user_id))
        # The bot's historical fee rows stay — they are history
        # (trades made while the fee existed, before its removal
        # 2026-11-12) — but they are detached from the deleted bot
        # (bot_id NULL), like the paper holdings that survive a bot
        # delete. Deleting the rows instead would erase history.
        db.execute("UPDATE trade_fees SET bot_id = NULL WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM bots WHERE id = ? AND user_id = ?",
                   (bot_id, user_id))

    @app.route("/bots/<int:bot_id>/delete/confirm", methods=["GET"])
    @login_required
    def bot_delete_confirm(bot_id: int):
        """Server-rendered delete confirmation page.

        The old edit-page delete relied on the browser's native
        confirm() dialog. In-app browsers that do not show JavaScript
        dialogs cancel the submit silently, so the button looked dead
        and nothing happened. This page asks the same question with a
        plain form instead — no JavaScript needed. The delete itself
        still only happens on POST to bot_delete, with a CSRF token.
        """
        user = _current_user(app)
        bot = _db(app).execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        return render_template("bot_delete_confirm.html", bot=bot)

    @app.route("/bots/<int:bot_id>/delete", methods=["POST"])
    @login_required
    def bot_delete(bot_id: int):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        _delete_bot(db, user["id"], bot_id)
        db.commit()
        _log(app, user["id"], "bot_delete",
             f"Deleted bot '{display_bot_name(bot['name'])}'.", bot["mode"])
        flash(f"Bot '{display_bot_name(bot['name'])}' deleted permanently.")
        return redirect(url_for("bots"))

    @app.route("/bots/<int:bot_id>/suggest", methods=["POST"])
    @login_required
    def bot_suggest(bot_id: int):
        """Bot proposes a trade from price signals. Suggestions only —
        nothing moves until the user approves, and in v1 only SOL
        transfers execute on-chain (swaps are labeled not-yet-available)."""
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        bot = db.execute(
            "SELECT * FROM bots WHERE id = ? AND user_id = ?",
            (bot_id, user["id"])).fetchone()
        if not bot:
            abort(404)
        coins = json.loads(bot["coins"])
        # Signals bots watch pump.fun mints: price + 24h change
        # from the single source only — the feed's fresh tick first,
        # else one batched Helius chain read for prices, and the
        # 24h change from the feed's own recorded tick history
        # (None when the history does not reach back 24h). A mint
        # with no price is skipped — no signal is ever built on a
        # missing price.
        mints = [c for c in coins if is_valid_address(c)]
        mint_prices: dict = {}
        mint_changes: dict = {}
        if mints:
            feed = app.config.get("LIVE_FEED")
            if feed is not None:
                for mint in mints:
                    try:
                        price = feed.live_price(mint)
                    except Exception:
                        price = None
                    if price is not None and price > 0:
                        mint_prices[mint] = price
            uncovered = [m for m in mints if m not in mint_prices]
            if uncovered:
                mint_prices.update(
                    _chain_prices_for_mints(app, uncovered))
            if feed is not None:
                now = time.time()
                for mint in mints:
                    try:
                        change = feed.change_over(mint, 86400, now)
                    except Exception:
                        change = None
                    if change is not None:
                        mint_changes[mint] = change
        made = 0
        for coin in coins:
            if coin not in mint_prices:
                continue
            side, detail = signal_for_coin(
                coin, mint_changes.get(coin), bot["strategy"])
            if side is None:
                continue
            db.execute(
                "INSERT INTO bot_proposals (user_id, bot_id, kind, coin_id, "
                "side, detail, status, created_at) "
                "VALUES (?, ?, 'signal', ?, ?, ?, 'pending', ?)",
                (user["id"], bot_id, coin, side, detail, _now()))
            made += 1
        db.commit()
        flash(f"Bot suggested {made} trade(s) for your approval."
              if made else "No strong signals right now.")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    @app.route("/bots/proposals/<int:proposal_id>/approve", methods=["POST"])
    @login_required
    def proposal_approve(proposal_id: int):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        prop = db.execute(
            "SELECT * FROM bot_proposals WHERE id = ? AND user_id = ?",
            (proposal_id, user["id"])).fetchone()
        if not prop:
            abort(404)
        if prop["status"] != "pending":
            flash("That proposal was already handled.")
            return redirect(url_for("bot_detail", bot_id=prop["bot_id"]))
        bot_row = db.execute(
            "SELECT mode FROM bots WHERE id = ? AND user_id = ?",
            (prop["bot_id"], user["id"])).fetchone()
        bot_mode = bot_row["mode"] if bot_row else ""
        if prop["kind"] == "transfer" and prop["destination"]:
            # Approved transfer -> the normal two-step send flow.
            cur = db.execute(
                "INSERT INTO send_intents (user_id, destination, "
                "amount_lamports, token, status, created_at) "
                "VALUES (?, ?, ?, 'SOL', 'pending', ?)",
                (user["id"], prop["destination"],
                 int(prop["amount_sol"] * 1_000_000_000), _now()))
            db.execute("UPDATE bot_proposals SET status = 'approved' "
                       "WHERE id = ?", (proposal_id,))
            db.commit()
            _log(app, user["id"], "proposal_approve",
                 f"Approved bot proposal #{proposal_id}; send intent "
                 f"created — confirm before anything moves.",
                 bot_mode)
            return redirect(url_for("wallet_send_confirm",
                                    intent_id=cur.lastrowid))
        # Signal (buy/sell) proposals: approval records the decision.
        # On-chain swaps are NOT executed in v1 — labeled plainly.
        db.execute("UPDATE bot_proposals SET status = 'approved' "
                   "WHERE id = ?", (proposal_id,))
        db.commit()
        _log(app, user["id"], "proposal_approve",
             f"Approved bot proposal #{proposal_id} ({prop['side']} "
             f"{prop['coin_id']}). No on-chain swap in v1 — swaps arrive "
             "in v2; nothing moved.",
             bot_mode)
        flash("Approved. Note: on-chain swaps are not executed in v1 — "
              "this is recorded as your decision, and nothing moved.")
        return redirect(url_for("bot_detail", bot_id=prop["bot_id"]))

    @app.route("/bots/proposals/<int:proposal_id>/reject", methods=["POST"])
    @login_required
    def proposal_reject(proposal_id: int):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        prop = db.execute(
            "SELECT * FROM bot_proposals WHERE id = ? AND user_id = ?",
            (proposal_id, user["id"])).fetchone()
        if not prop:
            abort(404)
        bot_row = db.execute(
            "SELECT mode FROM bots WHERE id = ? AND user_id = ?",
            (prop["bot_id"], user["id"])).fetchone()
        bot_mode = bot_row["mode"] if bot_row else ""
        db.execute("UPDATE bot_proposals SET status = 'rejected' "
                   "WHERE id = ?", (proposal_id,))
        db.commit()
        _log(app, user["id"], "proposal_reject",
             f"Rejected bot proposal #{proposal_id}.",
             bot_mode)
        return redirect(url_for("bot_detail", bot_id=prop["bot_id"]))

    # ------------------------------------------------------------------
    # In-app assistant
    # ------------------------------------------------------------------
    @app.route("/api/assistant", methods=["POST"])
    @login_required
    def api_assistant():
        if not request.is_json:
            return jsonify({"error": "json required"}), 400
        body = request.get_json()
        if body.get("csrf_token") != session.get("csrf_token"):
            return jsonify({"error": "bad csrf token"}), 400
        user = _current_user(app)
        parsed = assistant_mod.parse_message(body.get("message", ""))
        intent = parsed["intent"]

        if intent == "confirm_yes":
            pending_rules = session.pop("pending_rules", None)
            if pending_rules:
                db = _db(app)
                bot = db.execute(
                    "SELECT * FROM bots WHERE id = ? AND user_id = ?",
                    (pending_rules["bot_id"], user["id"])).fetchone()
                if not bot:
                    return jsonify({"reply": "That bot is already gone."})
                cleaned, err = validate_ruleset(pending_rules["rules"],
                                                bot=bot)
                if err:
                    return jsonify({"reply":
                        f"Those rules no longer check out ({err}) — "
                        f"nothing was changed."})
                db.execute("UPDATE bots SET rules_json = ?, updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (json.dumps(cleaned), _now(), bot["id"],
                            user["id"]))
                db.commit()
                _log(app, user["id"], "rules_set",
                     f"Set custom rules for bot '{display_bot_name(bot['name'])}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — bot '{display_bot_name(bot['name'])}' now follows these "
                    f"rules:\n{summarize_rules(cleaned)}\n\n"
                    f"It will never trade outside them or its limits."})
            pending_clear = session.pop("pending_rules_clear", None)
            if pending_clear:
                db = _db(app)
                bot = db.execute(
                    "SELECT * FROM bots WHERE id = ? AND user_id = ?",
                    (pending_clear, user["id"])).fetchone()
                if not bot:
                    return jsonify({"reply": "That bot is already gone."})
                db.execute("UPDATE bots SET rules_json = '', updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (_now(), bot["id"], user["id"]))
                db.commit()
                _log(app, user["id"], "rules_clear",
                     f"Cleared custom rules for bot '{display_bot_name(bot['name'])}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — bot '{display_bot_name(bot['name'])}' is back to the standard "
                    f"signals."})
            pending_scanner = session.pop("pending_scanner", None)
            if pending_scanner:
                db = _db(app)
                bot_mode = _bot_mode(db, user["id"])
                existing = db.execute(
                    "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                    "AND rules_json LIKE ? AND mode = ?",
                    (user["id"], SCANNER_CATEGORY,
                     '%"kind": "scanner"%', bot_mode)).fetchone()
                if existing:
                    return jsonify({"reply":
                        f"You already have a {SCANNER_PRESET_NAME} bot "
                        f"({SCANNER_DISPLAY}). Open it on the Bots page."})
                rules = pending_scanner.get("rules") or scanner_ruleset()
                cleaned, err = validate_ruleset(rules)
                if err:
                    return jsonify({"reply":
                        f"Those scanner rules no longer check out ({err}) "
                        f"— nothing was changed."})
                token = new_token()
                cur = db.execute(
                    "INSERT INTO bots (user_id, name, strategy, coins, "
                    "buy_size_pct, stop_loss_pct, mode, silent, "
                    "max_open_trades, api_token_hash, category, "
                    "rules_json, platform) "
                    "VALUES (?, ?, 'bold', '[]', 15, 7, ?, 0, 3, ?, "
                    "?, ?, 'pump.fun')",
                    (user["id"],
                     suffixed_name(SCANNER_PRESET_NAME, bot_mode), bot_mode,
                     hash_token(token),
                     SCANNER_CATEGORY, json.dumps(cleaned)))
                db.commit()
                _log(app, user["id"], "bot_create",
                     f"Created bot '{SCANNER_PRESET_NAME}' "
                     f"({SCANNER_DISPLAY} preset, {bot_mode}) via assistant.",
                     bot_mode)
                return jsonify({"reply":
                    f"Done — created the {SCANNER_PRESET_NAME} bot "
                    f"({SCANNER_DISPLAY} sniper, {bot_mode}):\n"
                    f"{summarize_rules(cleaned)}\n\n"
                    f"Silent trading is off. Say \"turn on silent "
                    f"trading for {SCANNER_PRESET_NAME}\" when you want "
                    f"it to trade on its own within these rules.",
                    "bot_id": cur.lastrowid})
            pending_silent = session.pop("pending_silent", None)
            if pending_silent:
                db = _db(app)
                bot_id, want = pending_silent
                bot = db.execute(
                    "SELECT * FROM bots WHERE id = ? AND user_id = ?",
                    (bot_id, user["id"])).fetchone()
                if not bot:
                    return jsonify({"reply": "That bot is already gone."})
                if int(bot["silent"]) == want:
                    return jsonify({"reply":
                        f"Silent trading is already "
                        f"{'ON' if want else 'OFF'} for "
                        f"'{display_bot_name(bot['name'])}'. Nothing changed."})
                db.execute("UPDATE bots SET silent = ?, updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (want, _now(), bot_id, user["id"]))
                db.commit()
                _log(app, user["id"],
                     "silent_on" if want else "silent_off",
                     f"Silent trading turned "
                     f"{'ON' if want else 'OFF'} for bot '{display_bot_name(bot['name'])}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — silent trading is now "
                    f"{'ON' if want else 'OFF'} for bot '{display_bot_name(bot['name'])}'. "
                    f"It trades on its own within your limits, and every "
                    f"trade is logged on your dashboard."})
            pending = session.pop("pending_delete_bot", None)
            if pending:
                db = _db(app)
                bot = db.execute(
                    "SELECT * FROM bots WHERE id = ? AND user_id = ?",
                    (pending, user["id"])).fetchone()
                if bot:
                    _delete_bot(db, user["id"], pending)
                    db.commit()
                    _log(app, user["id"], "bot_delete",
                         f"Deleted bot '{display_bot_name(bot['name'])}' (assistant).", bot["mode"])
                    return jsonify({"reply": f"Done — bot '{display_bot_name(bot['name'])}' "
                                             "is deleted."})
                return jsonify({"reply": "That bot is already gone."})
            return jsonify({"reply": "Nothing is waiting for confirmation."})

        if intent == "cancel":
            session.pop("pending_delete_bot", None)
            session.pop("pending_silent", None)
            session.pop("pending_rules", None)
            session.pop("pending_rules_clear", None)
            session.pop("pending_scanner", None)
            return jsonify({"reply": "Cancelled — nothing was changed."})

        if intent == "balance":
            wallet = _wallet(app, user["id"])
            real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
            # Single source: holdings valued from the feed's ticks /
            # one chain read (unpriced holdings at cost, never $0),
            # not the old SOL-only get_prices() map.
            value = sum(
                h["value"] for h in pnl_mod.current_holdings(
                    _db(app), user["id"],
                    _snapshot_price_map(app, user["id"])))
            real_txt = (f"{real_sol:.4f} SOL" if real_sol is not None
                        else "unavailable right now")
            return jsonify({"reply":
                f"Paper: ${_paper(app, user['id']):,.2f} CAD cash + "
                f"${value:,.2f} CAD in holdings. Real wallet: {real_txt} "
                f"(network: {_cfg(app).network})."})

        if intent == "wallet_show":
            wallet = _wallet(app, user["id"])
            real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
            real_txt = (f"{real_sol:.4f} SOL" if real_sol is not None
                        else "unavailable right now")
            return jsonify({"reply":
                f"Your app wallet is {wallet['pubkey']} with {real_txt}. "
                f"Deposit to that address. Open the wallet (top right) to "
                f"send or withdraw — I always ask you to confirm first.",
                "wallet": wallet["pubkey"]})

        if intent == "bots_list":
            bots = _db(app).execute(
                "SELECT id, name, strategy, coins FROM bots WHERE user_id = ?",
                (user["id"],)).fetchall()
            if not bots:
                return jsonify({"reply":
                    "You have no bots yet. Say \"create bot\" and I will "
                    "make one for you."})
            lines = [f"- {display_bot_name(b['name'])} ({b['strategy']}, "
                     f"{', '.join(json.loads(b['coins']))})" for b in bots]
            return jsonify({"reply": "Your bots:\n" + "\n".join(lines)})

        if intent == "bot_create":
            name = parsed.get("bot_name") or "My Bot"
            strategy = parsed.get("strategy") or "balanced"
            token = new_token()
            db = _db(app)
            cur = db.execute(
                "INSERT INTO bots (user_id, name, strategy, coins, "
                "buy_size_pct, stop_loss_pct, api_token_hash) "
                "VALUES (?, ?, ?, '[\"SOL\"]', 20, 7, ?)",
                (user["id"], name, strategy, hash_token(token)))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{name}' ({strategy}) via assistant.", "paper")
            return jsonify({"reply":
                f"Created bot '{name}' ({strategy}, watching SOL, "
                f"buy size 20%, stop loss 7%). Its API token is shown once "
                f"on the Bots page — I can't show it here because it is "
                f"secret. You can edit it any time with \"edit bot {name}\".",
                "bot_id": cur.lastrowid})

        if intent == "bot_edit":
            db = _db(app)
            bot = _find_bot_by_name(
                db, user["id"], parsed["bot_name"],
                mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            fields = parsed.get("fields") or {}
            if not fields:
                return jsonify({"reply":
                    "What should I change? For example: \"edit bot "
                    f"{display_bot_name(bot['name'])} strategy to bold\" or \"stop loss 5%\"."})
            sets, vals = [], []
            if "strategy" in fields:
                sets.append("strategy = ?")
                vals.append(fields["strategy"])
            if "buy_size_pct" in fields:
                sets.append("buy_size_pct = ?")
                vals.append(min(max(fields["buy_size_pct"], 1), 100))
            if "stop_loss_pct" in fields:
                sets.append("stop_loss_pct = ?")
                vals.append(min(max(fields["stop_loss_pct"], 1), 50))
            sets.append("updated_at = ?")
            vals.append(_now())
            vals.extend([bot["id"], user["id"]])
            db.execute("UPDATE bots SET " + ", ".join(sets) +
                       " WHERE id = ? AND user_id = ?", vals)
            db.commit()
            _log(app, user["id"], "bot_edit",
                 f"Edited bot '{display_bot_name(bot['name'])}' via assistant.", bot["mode"])
            return jsonify({"reply":
                f"Done — bot '{display_bot_name(bot['name'])}' updated: " +
                ", ".join(f"{k} = {v}" for k, v in fields.items()) + "."})

        if intent == "bot_delete":
            db = _db(app)
            bot = _find_bot_by_name(
                db, user["id"], parsed["bot_name"],
                mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            session["pending_delete_bot"] = bot["id"]
            return jsonify({"reply":
                f"Are you sure you want to delete bot '{display_bot_name(bot['name'])}'? "
                f"Reply \"yes\" to delete it, or \"no\" to keep it."})

        if intent == "rules_propose":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            partial = dict(parsed.get("rules") or {})
            if not bot_name and partial.get("kind") == "scanner":
                # "snipe coins gaining 5% in 3 minutes" names no bot:
                # the scanner IS the bot. Show the rules back and
                # create it on "yes".
                cleaned, err = validate_ruleset(partial)
                if err:
                    return jsonify({"reply":
                        f"I can't use those rules: {err} Nothing was "
                        f"changed."})
                session["pending_scanner"] = {"rules": cleaned}
                return jsonify({"reply":
                    f"Here is the Scanner bot ({SCANNER_DISPLAY} "
                    f"sniper) with those rules:\n"
                    f"{summarize_rules(cleaned)}\n\n"
                    f"Reply \"yes\" to create it, or \"no\" to leave "
                    f"things as they are."})
            if not bot_name:
                return jsonify({"reply":
                    "Which bot should follow these rules? Say \"bot "
                    "<name> scan ...\" — or \"show my bots\" to see them."})
            bot = _find_bot_by_name(db, user["id"], bot_name,
                               mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            if partial.get("trailing_stop_pct") is None:
                partial["trailing_stop_pct"] = float(bot["stop_loss_pct"])
            cleaned, err = validate_ruleset(partial, bot=bot)
            if err:
                return jsonify({"reply":
                    f"I can't use those rules: {err} Nothing was changed."})
            summary = summarize_rules(cleaned)
            session["pending_rules"] = {"bot_id": bot["id"],
                                        "rules": cleaned}
            return jsonify({"reply":
                f"Here is what I understood for bot '{display_bot_name(bot['name'])}':\n"
                f"{summary}\n\n"
                f"The bot will only trade inside these rules, and never "
                f"outside its limits (buy size {bot['buy_size_pct']}%, "
                f"stop loss {bot['stop_loss_pct']}%, max "
                f"{bot['max_open_trades']} open trades, never more than "
                f"your balance). Reply \"yes\" to apply them, or \"no\" "
                f"to leave the bot as it is."})

        if intent == "rules_rug":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            if not bot_name:
                return jsonify({"reply":
                    "Which bot should get the rug sell rule? Say "
                    "\"sell everything if the dev starts selling for "
                    "<bot name>\" — or \"show my bots\" to see them."})
            bot = _find_bot_by_name(db, user["id"], bot_name,
                               mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, rules_err = load_rules(bot)
            if rules_err:
                return jsonify({"reply":
                    f"Bot '{display_bot_name(bot['name'])}' has stored rules I can't "
                    f"read ({rules_err}) — it makes no trades until "
                    f"they are fixed. Ask me to set new rules, or "
                    f"remove them."})
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{display_bot_name(bot['name'])}' has no custom rules yet — "
                    f"it uses the standard signals. Set its trading "
                    f"rules first (for example: \"bot {display_bot_name(bot['name'])} "
                    f"scan pump.fun, buy when a coin gains 5% in 5 "
                    f"minutes\"), then add the rug sell rule. Nothing "
                    f"was changed."})
            merged = json.loads(json.dumps(rules))
            rug = dict(RUG_SELL_DEFAULT)
            rug.update(parsed.get("rug") or {})
            merged["rug_sell"] = rug
            cleaned, err = validate_ruleset(merged, bot=bot)
            if err:
                return jsonify({"reply":
                    f"I can't use that rug rule: {err} Nothing was "
                    f"changed."})
            summary = summarize_rules(cleaned)
            session["pending_rules"] = {"bot_id": bot["id"],
                                        "rules": cleaned}
            return jsonify({"reply":
                f"Here is what I understood for bot '{display_bot_name(bot['name'])}':\n"
                f"{summary}\n\n"
                f"The bot will only trade inside these rules, and never "
                f"outside its limits (buy size {bot['buy_size_pct']}%, "
                f"stop loss {bot['stop_loss_pct']}%, max "
                f"{bot['max_open_trades']} open trades, never more than "
                f"your balance). Reply \"yes\" to apply them, or \"no\" "
                f"to leave the bot as it is."})

        if intent == "rules_filter":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            if not bot_name:
                return jsonify({"reply":
                    "Which bot should get the buy filter? Say "
                    "\"set the buy filter for <bot name>: min volume "
                    "$1000, min signal 5%\" — or \"show my bots\" to "
                    "see them."})
            bot = _find_bot_by_name(db, user["id"], bot_name,
                               mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, rules_err = load_rules(bot)
            if rules_err:
                return jsonify({"reply":
                    f"Bot '{display_bot_name(bot['name'])}' has stored rules I can't "
                    f"read ({rules_err}) — it makes no trades until "
                    f"they are fixed. Ask me to set new rules, or "
                    f"remove them."})
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{display_bot_name(bot['name'])}' has no custom rules yet — "
                    f"it uses the standard signals. Set its trading "
                    f"rules first (for example: \"bot {display_bot_name(bot['name'])} "
                    f"scan pump.fun, buy when a coin gains 5% in 5 "
                    f"minutes\"), then set the buy filter. Nothing "
                    f"was changed."})
            merged = json.loads(json.dumps(rules))
            merged.setdefault("buy_filter", {})
            merged["buy_filter"].update(parsed.get("filter") or {})
            cleaned, err = validate_ruleset(merged, bot=bot)
            if err:
                return jsonify({"reply":
                    f"I can't use that buy filter: {err} Nothing was "
                    f"changed."})
            summary = summarize_rules(cleaned)
            session["pending_rules"] = {"bot_id": bot["id"],
                                        "rules": cleaned}
            return jsonify({"reply":
                f"Here is what I understood for bot '{display_bot_name(bot['name'])}':\n"
                f"{summary}\n\n"
                f"The bot will only trade inside these rules, and never "
                f"outside its limits (buy size {bot['buy_size_pct']}%, "
                f"stop loss {bot['stop_loss_pct']}%, max "
                f"{bot['max_open_trades']} open trades, never more than "
                f"your balance). Reply \"yes\" to apply them, or \"no\" "
                f"to leave the bot as it is."})

        if intent == "rules_show":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            if bot_name:
                bot = _find_bot_by_name(db, user["id"], bot_name,
                               mode=_bot_mode(db, user["id"]))
                if not bot:
                    return jsonify({"reply":
                        f"I couldn't find a bot called '{bot_name}'. "
                        f"Say \"show my bots\" to see them."})
                rules, err = load_rules(bot)
                if err:
                    return jsonify({"reply":
                        f"Bot '{display_bot_name(bot['name'])}' has stored rules I can't "
                        f"read ({err}) — it makes no trades until they "
                        f"are fixed. Ask me to set new rules, or remove "
                        f"them."})
                return jsonify({"reply":
                    f"Rules for '{display_bot_name(bot['name'])}':\n"
                    f"{summarize_rules(rules)}"})
            bots = db.execute(
                "SELECT * FROM bots WHERE user_id = ?",
                (user["id"],)).fetchall()
            if not bots:
                return jsonify({"reply":
                    "You have no bots yet. Say \"create bot\" and I will "
                    "make one for you."})
            lines = []
            for b in bots:
                rules, err = load_rules(b)
                if err:
                    lines.append(f"- {display_bot_name(b['name'])}: stored rules are broken "
                                 f"({err}) — no trades.")
                elif rules:
                    lines.append(f"- {display_bot_name(b['name'])}:\n{summarize_rules(rules)}")
                else:
                    lines.append(
                        f"- {display_bot_name(b['name'])}: standard signals (buy +2% in "
                        f"24h, sell −5% in 24h).")
            return jsonify({"reply": "\n".join(lines)})

        if intent == "rules_clear":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            if not bot_name:
                return jsonify({"reply":
                    "Which bot? Say \"remove rules for <bot name>\"."})
            bot = _find_bot_by_name(db, user["id"], bot_name,
                               mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, _err = load_rules(bot)
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{display_bot_name(bot['name'])}' has no custom rules — it "
                    f"already uses the standard signals."})
            session["pending_rules_clear"] = bot["id"]
            return jsonify({"reply":
                f"Remove the custom rules from '{display_bot_name(bot['name'])}' and go "
                f"back to the standard signals? Reply \"yes\" to remove "
                f"them, or \"no\" to keep them."})

        if intent == "preset_create":
            preset = parsed.get("preset") or "trenches"
            db = _db(app)
            bot_mode = _bot_mode(db, user["id"])
            if preset == "scanner":
                existing = db.execute(
                    "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                    "AND rules_json LIKE ? AND mode = ?",
                    (user["id"], SCANNER_CATEGORY,
                     '%"kind": "scanner"%', bot_mode)).fetchone()
                if existing:
                    return jsonify({"reply":
                        f"You already have a {SCANNER_PRESET_NAME} bot "
                        f"({SCANNER_DISPLAY}). Open it on the Bots page."})
                session["pending_scanner"] = {
                    "rules": scanner_ruleset()}
                return jsonify({"reply":
                    f"Here is the {SCANNER_PRESET_NAME} bot "
                    f"({SCANNER_DISPLAY} sniper, {bot_mode}):\n"
                    f"{summarize_rules(scanner_ruleset())}\n\n"
                    f"Silent trading is off. Reply \"yes\" to create "
                    f"it, or \"no\" to leave things as they are."})
            existing = db.execute(
                "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                "AND rules_json LIKE ? AND mode = ?",
                (user["id"], TRENCHES_CATEGORY,
                 '%"kind": "trenches"%', bot_mode)).fetchone()
            if existing:
                return jsonify({"reply":
                    "You already have a Trenches bot (Memecoin build). "
                    "Open it on the Bots page."})
            token = new_token()
            cur = db.execute(
                "INSERT INTO bots (user_id, name, strategy, coins, "
                "buy_size_pct, stop_loss_pct, mode, silent, "
                "max_open_trades, api_token_hash, category, rules_json, "
                "platform) "
                "VALUES (?, ?, 'bold', '[]', 20, 7, ?, 0, 3, ?, ?, ?, "
                "'pump.fun')",
                (user["id"],
                 suffixed_name(TRENCHES_PRESET_NAME, bot_mode), bot_mode,
                 hash_token(token),
                 TRENCHES_CATEGORY, json.dumps(trenches_ruleset())))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{TRENCHES_PRESET_NAME}' (Memecoin build "
                 f"preset, {bot_mode}) via assistant.", bot_mode)
            return jsonify({"reply":
                f"Created the {TRENCHES_PRESET_NAME} bot (Memecoin "
                f"build, {bot_mode}):\n{summarize_rules(trenches_ruleset())}"
                f"\n\nSilent trading is off. Say \"turn on silent "
                f"trading for {TRENCHES_PRESET_NAME}\" when you want it "
                f"to trade on its own within these rules.",
                "bot_id": cur.lastrowid})

        if intent == "silent_status":
            bots = _db(app).execute(
                "SELECT name, mode, silent FROM bots WHERE user_id = ?",
                (user["id"],)).fetchall()
            if not bots:
                return jsonify({"reply":
                    "You have no bots yet. Say \"create bot\" and I will "
                    "make one for you."})
            lines = [f"- {display_bot_name(b['name'])} ({b['mode']}): silent "
                     f"{'ON' if b['silent'] else 'OFF'}" for b in bots]
            return jsonify({"reply": "Silent trading:\n" + "\n".join(lines)
                            + "\n\nSilent is off by default. Say \"turn on "
                            "silent trading for <bot>\" to enable it — I "
                            "always ask you to confirm first."})

        if intent in ("silent_on", "silent_off"):
            want = 1 if intent == "silent_on" else 0
            if not parsed.get("bot_name"):
                return jsonify({"reply":
                    "Which bot? Say \"turn "
                    f"{'on' if want else 'off'} silent trading for "
                    "<bot name>\" — or \"show my bots\" to see them."})
            db = _db(app)
            bot = _find_bot_by_name(
                db, user["id"], parsed["bot_name"],
                mode=_bot_mode(db, user["id"]))
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            if int(bot["silent"]) == want:
                return jsonify({"reply":
                    f"Silent trading is already "
                    f"{'ON' if want else 'OFF'} for '{display_bot_name(bot['name'])}'."})
            if want == 0:
                # Turning off is the safe direction: apply at once.
                db.execute("UPDATE bots SET silent = 0, updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (_now(), bot["id"], user["id"]))
                db.commit()
                _log(app, user["id"], "silent_off",
                     f"Silent trading turned OFF for bot '{display_bot_name(bot['name'])}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — silent trading is OFF for '{display_bot_name(bot['name'])}'. "
                    f"The loop stops for it right away."})
            # Turning ON always needs a confirm first.
            session["pending_silent"] = [bot["id"], 1]
            return jsonify({"reply":
                f"Turn on silent trading for '{display_bot_name(bot['name'])}' "
                f"({bot['mode']})? It will trade on its own within your "
                f"limits — buy size {bot['buy_size_pct']}%, stop loss "
                f"{bot['stop_loss_pct']}%, max "
                f"{bot['max_open_trades']} open trades, never more than "
                f"your balance — and every trade is logged on your "
                f"dashboard. Reply \"yes\" to turn it on, or \"no\" to "
                f"leave it off."})

        if intent == "send":
            try:
                amount = float(parsed["amount"])
            except ValueError:
                return jsonify({"reply": "That amount doesn't look right."})
            if parsed["token"] != "SOL":
                return jsonify({"reply":
                    "Only SOL sends are supported in v1."})
            if not is_valid_address(parsed["destination"]):
                return jsonify({"reply":
                    "That destination address is not valid."})
            if amount <= 0:
                return jsonify({"reply": "Amount must be more than 0."})
            db = _db(app)
            cur = db.execute(
                "INSERT INTO send_intents (user_id, destination, "
                "amount_lamports, token, status, created_at) "
                "VALUES (?, ?, ?, 'SOL', 'pending', ?)",
                (user["id"], parsed["destination"], sol_to_lamports(amount),
                 _now()))
            db.commit()
            return jsonify({"reply":
                f"Send ready to review: {amount} SOL to "
                f"{parsed['destination']}. Nothing has moved — open the "
                f"confirm screen to check it and send, or ignore it to "
                f"leave it pending.",
                "intent_id": cur.lastrowid,
                "confirm_url": url_for("wallet_send_confirm",
                                       intent_id=cur.lastrowid)})

        # help / paper_help / unknown: the parser already wrote the reply.
        return jsonify({"reply": parsed["reply"]})


def _paper_price(coin: str):
    from .prices import get_price

    return get_price(coin)
