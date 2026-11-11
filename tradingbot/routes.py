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
  on devnet, only for trades the rules engine allows, and only
  within the bot's limits (see executor.py). The signing bot never
  sends to an arbitrary destination.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (Flask, abort, flash, has_request_context, jsonify,
                   redirect, render_template, request, session, url_for)

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
from . import analysis as analysis_mod
from . import candles as candles_mod
from . import donations as donations_mod
from . import fees as fees_mod
from . import networks as networks_mod
from . import passkey as passkey_mod
from . import platforms as platforms_mod
from . import pnl as pnl_mod
from .page_numbers import page_list, page_number_for
from . import rate_limit as rate_limit_mod
from . import signing_bot as signing_bot_mod
from . import token_send as token_send_mod
from .charts import (CHART_RANGES, fetch_market_chart, fetch_ohlc,
                     fetch_trade_flow, high_low_24h,
                     mint_candles_from_feed, search_coins)
from .config import Config
from .executor import execute_real_trade, send_and_confirm
from .paper import (InsufficientCash, InsufficientHolding,
                    paper_buy as ledger_buy, paper_sell as ledger_sell)
from .prices import COINS, clear_price_cache, get_prices
from .builderbot import list_adjustments
from .home_holdings import (enrich_row, fetch_chain_token_balances,
                            reconcile_holdings, sparkline_svg_1h)
from .ledger import bot_wl_summary, recent_trades
from .rules import (RUG_SELL_DEFAULT, SCANNER_CATEGORY, SCANNER_DISPLAY,
                    SCANNER_PRESET_NAME, TRENCHES_CATEGORY,
                    TRENCHES_PRESET_NAME, load_rules, scanner_ruleset,
                    summarize_rules, trenches_ruleset, validate_ruleset)
from .security import (decrypt_secret, encrypt_secret, hash_password,
                       hash_token, new_csrf_token, new_token,
                       temporary_secret, verify_password, verify_turnstile,
                       wipe_secret)
from .signals import signal_for_coin
from .silent_loop import _open_position
from .solana import (SolanaRpcClient, build_signed_transfer,
                     generate_keypair, is_valid_address,
                     lamports_to_sol, sol_to_lamports)
from .trenches import (fetch_mint_1h_changes, fetch_mint_identities,
                       fetch_prices_for_mints, fetch_trenches_scan)

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


def _find_bot_by_name(db, user_id: int, name: str):
    """First bot whose name contains `name` (assistant-style lookup)."""
    if not name:
        return None
    return db.execute(
        "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
        (user_id, "%" + name + "%")).fetchone()


def _wallet(app, user_id: int):
    return _db(app).execute(
        "SELECT pubkey, encrypted_secret FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()


def _real_balance(app, pubkey: str):
    """SOL balance or None when the RPC is unreachable (never faked)."""
    try:
        client = SolanaRpcClient(_cfg(app).rpc_url)
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


def _mode_label(mode: str) -> str:
    """The plain-words label for a mode, used on every page so the
    user always knows which side they are on."""
    return "Real trading — devnet" if mode == "devnet" \
        else "Demo trading (paper)"


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


def _reconciled_real_holdings(app, db, user_id: int) -> list:
    """Real holdings with on-chain balances merged in (devnet).

    The chain is the truth for the AMOUNT held: a coin the wallet
    holds on-chain but the DB never recorded (its buys were
    recorded as failed, e.g. Gate) appears with avg_price=None and
    on_chain_only=True. Any failure (no wallet, RPC down, partial
    read) falls back to the DB view alone — a chain problem must
    never hide a recorded holding. Nothing here writes to the DB;
    the reconciliation is per-render only."""
    db_rows = _real_holdings_view(db, user_id)
    try:
        wallet = _wallet(app, user_id)
        if not wallet:
            return db_rows
        client = SolanaRpcClient(_cfg(app).rpc_url)
        chain = fetch_chain_token_balances(client, wallet["pubkey"])
        return reconcile_holdings(db_rows, chain)
    except Exception:
        return db_rows


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


def _real_avg_price(app, mint: str, amount_sol: float,
                    coin_amount: float) -> float:
    """USD cost basis for a real buy: the mint's current price when
    the feed has it, else the price implied by the SOL spent (SOL
    price x SOL paid / tokens received). 0 when neither is known —
    the holding is then shown with P&L '—', never guessed."""
    try:
        from .trenches import fetch_prices_for_mints
        prices = fetch_prices_for_mints([mint])
        if mint in prices:
            return float(prices[mint])
    except Exception:
        pass
    try:
        sol_price, _s, _a = _paper_price("SOL")
    except Exception:
        sol_price = None
    if sol_price and coin_amount > 0:
        return amount_sol * float(sol_price) / coin_amount
    return 0.0


def _coin_rows(db, user_id: int, mode: str, prices: dict,
               feed=None, app=None) -> list:
    """The coins list: ONLY the coins the user holds in the ACTIVE
    mode, with name, ticker, current price, P&L and 1-hour % change.
    Coins the user does not hold are not listed here at all — the
    trenches list and the quick Buy/Sell popup (with live search)
    are how new coins are found. P&L is (current price − average
    cost) × amount; a holding with no current price has P&L None
    and is valued at cost — never guessed. A holding with no
    cost basis (on-chain only, e.g. Gate, whose buys were recorded
    as failed) has avg_price None and P&L None — never guessed.
    A holding with no 1h figure in the feed has change_1h None
    ("—" on the page). When a live feed is passed, any price it
    rejects against its last good tick is dropped here too, so a
    bad tick can never inflate a balance on render; with no feed
    (or no reference yet) every price passes unchanged. In devnet
    mode with `app`, holdings are reconciled with the wallet's
    on-chain token balances first (see _reconciled_real_holdings),
    so coins the chain holds but the DB never recorded appear.
    Each row is enriched with its real token image and sparkline
    (home_holdings.enrich_row) — a row with no image or no history
    keeps the ticker-initial icon / no sparkline, never a fake."""
    price_map = {c: e.get("price") for c, e in prices["prices"].items()}
    if mode == "devnet":
        if app is not None:
            held = {h["coin_id"]: h for h in
                    _reconciled_real_holdings(app, db, user_id)}
        else:
            held = {h["coin_id"]: h for h in _real_holdings_view(db, user_id)}
    else:
        held = {h["coin_id"]: h for h in db.execute(
            "SELECT coin_id, amount, avg_price FROM paper_holdings "
            "WHERE user_id = ?", (user_id,)).fetchall()}
    mint_ids = [c for c in held if c not in price_map]
    changes_1h: dict = {}
    identities: dict = {}
    if mint_ids:
        try:
            for mint, price in fetch_prices_for_mints(
                    mint_ids).items():
                price_map[mint] = price
        except Exception:
            pass
        try:
            changes_1h = fetch_mint_1h_changes(mint_ids)
        except Exception:
            changes_1h = {}
        try:
            # Real name + ticker for held mints, from the same feed.
            # A mint the feed does not name keeps its honest
            # "Pump.fun token …" label below — never guessed.
            identities = fetch_mint_identities(mint_ids)
        except Exception:
            identities = {}
    # A bad tick must never inflate a balance on render either: drop
    # any price the live feed rejects against its last good tick. No
    # reference yet (feed empty or off) means everything passes.
    if feed is not None:
        price_map = {c: p for c, p in price_map.items()
                     if p is None or feed.would_accept(c, p)}
    rows = []
    for symbol, info in COINS.items():
        h = held.get(symbol)
        if h is None:
            continue
        price = price_map.get(symbol)
        avg = h["avg_price"]
        pnl = None
        if price is not None and avg is not None:
            pnl = (float(price) - float(avg)) * float(h["amount"])
        rows.append({"coin_id": symbol, "name": info["name"],
                     "ticker": symbol, "price": price,
                     "change_24h": prices["prices"].get(symbol, {}).get(
                         "change_24h"),
                     "change_1h": prices["prices"].get(symbol, {}).get(
                         "change_1h"),
                     "held": float(h["amount"]),
                     "avg_price": (float(avg) if avg is not None else None),
                     "pnl": pnl, "is_mint": False,
                     "value": (float(h["amount"]) * float(price))
                     if price is not None else None})
    for mint in sorted(held):
        if mint in COINS:
            continue
        price = price_map.get(mint)
        h = held[mint]
        avg = h["avg_price"]
        pnl = None
        if price is not None and avg is not None:
            pnl = (float(price) - float(avg)) * float(h["amount"])
        ident = identities.get(mint) or {}
        rows.append({"coin_id": mint,
                     "name": ident.get("name") or (
                         "Pump.fun token " + mint[:4] + "…" + mint[-4:]),
                     "ticker": ident.get("symbol") or (mint[:4] + "…"),
                     "price": price, "change_24h": None,
                     "change_1h": changes_1h.get(mint),
                     "held": float(h["amount"]),
                     "avg_price": (float(avg) if avg is not None else None),
                     "pnl": pnl, "is_mint": True,
                     "value": (float(h["amount"]) * float(price))
                     if price is not None else None})
    # Attach each row's real token image + sparkline. Enrichment
    # never raises and never invents data — a row with no image or
    # no history keeps the ticker-initial icon / no sparkline.
    for row in rows:
        enrich_row(row, feed=feed)
    return rows


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


def _header_balance(app, user):
    """What the top corner shows: {'mode', 'label', 'text'}.

    Paper mode: paper portfolio value (cash + holdings at current
    prices). Devnet mode: the app wallet's on-chain SOL balance.
    Never faked — an unreachable price feed or RPC shows
    'unavailable' instead of a made-up number.
    """
    db = _db(app)
    mode = _active_mode(db, user["id"])
    if mode == "devnet":
        wallet = _wallet(app, user["id"])
        sol = (_cached_real_balance(app, wallet["pubkey"])
               if wallet else None)
        text = f"{sol:.4f} SOL" if sol is not None else "unavailable"
        return {"mode": "devnet", "label": "Devnet", "text": text}
    prices = get_prices()
    price_map = {c: e.get("price", 0.0)
                 for c, e in prices["prices"].items()}
    cash = _paper(app, user["id"])
    holdings = db.execute(
        "SELECT coin_id, amount FROM paper_holdings WHERE user_id = ?",
        (user["id"],)).fetchall()
    value = cash + sum(h["amount"] * price_map.get(h["coin_id"], 0.0)
                       for h in holdings)
    return {"mode": "paper", "label": "Paper", "text": f"${value:,.2f}"}


def _record_pnl_snapshot(app, user_id: int, price_map: dict) -> None:
    """One P&L snapshot after a trade. Never breaks the trade itself."""
    try:
        totals = pnl_mod.portfolio_totals(_db(app), user_id, price_map)
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
# rows. The cache is module-level so tests can reset it.
# ----------------------------------------------------------------------
_TRENCHES_CACHE: dict = {"at": 0.0, "rows": None, "error": None}


def _trenches_list() -> tuple[list, str | None]:
    """Top trenches rows: [{mint, symbol, name, price_usd,
    change_1h_pct}], best 1-hour gain first, at most 10. Returns
    (rows, error)."""
    now = time.time()
    if (_TRENCHES_CACHE["rows"] is not None
            and now - _TRENCHES_CACHE["at"] < 120):
        return _TRENCHES_CACHE["rows"], _TRENCHES_CACHE["error"]
    try:
        scan = fetch_trenches_scan()
    except Exception as exc:
        scan = {"candidates": [], "error": str(exc)[:160]}
    rows = []
    for entry in scan.get("candidates") or []:
        rows.append({
            "mint": entry["mint"],
            "symbol": entry["symbol"],
            "name": entry.get("name") or entry["symbol"],
            "price_usd": entry["price_usd"],
            "change_1h_pct": entry.get("change_1h_pct"),
        })
    rows.sort(key=lambda r: (
        r["change_1h_pct"] is None,
        -(r["change_1h_pct"] or 0.0)))
    rows = rows[:10]
    error = scan.get("error")
    _TRENCHES_CACHE["at"] = now
    _TRENCHES_CACHE["rows"] = rows
    _TRENCHES_CACHE["error"] = error
    return rows, error


def _trenches_entry(mint: str) -> dict | None:
    """The trenches-list entry for a mint, if the feed has it — so a
    trenches token opened from the list shows its real name instead
    of a truncated address."""
    try:
        rows, _err = _trenches_list()
    except Exception:
        return None
    return next((r for r in rows if r["mint"] == mint), None)


def _unlisted_coin_row(coin_id_resolved: str, price, prices: dict) -> dict:
    """The coin row for a known coin that is not in the user's list
    (no holding, no price-list entry): a trenches token shows its
    feed name/symbol, anything else a truncated address."""
    info = COINS.get(coin_id_resolved)
    trench = (_trenches_entry(coin_id_resolved)
              if coin_id_resolved not in COINS else None)
    if trench is not None:
        name, ticker = trench["name"], trench["symbol"]
    elif info:
        name, ticker = info["name"], coin_id_resolved
    else:
        name = ("Pump.fun token " + coin_id_resolved[:4] + "…" +
                coin_id_resolved[-4:])
        ticker = coin_id_resolved[:4] + "…"
    return {"coin_id": coin_id_resolved, "name": name, "ticker": ticker,
            "price": price, "held": 0.0, "avg_price": None,
            "change_24h": prices["prices"].get(
                coin_id_resolved, {}).get("change_24h"),
            "pnl": None, "is_mint": coin_id_resolved not in COINS}


def _token_ticker(mint: str) -> str:
    """The display ticker for a mint: the feed's real symbol when
    DexScreener names it, else a truncated address — never a
    fabricated name."""
    try:
        identities = fetch_mint_identities([mint])
    except Exception:
        identities = {}
    info = identities.get(mint) or {}
    symbol = info.get("symbol")
    if symbol:
        return str(symbol)
    return mint[:4] + "…"


def _token_send_fee_note(app, wallet_pubkey: str, intent) -> str:
    """The fee line for a token send's confirm screen: the network
    fee, plus the token-account rent when the recipient has no
    token account for the mint yet. Best effort — when the chain
    cannot be checked, the note says the rent may apply instead
    of guessing a number."""
    try:
        client = SolanaRpcClient(_cfg(app).rpc_url)
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
    client = SolanaRpcClient(_cfg(app).rpc_url)
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
        with temporary_secret(wallet["encrypted_secret"],
                              _cfg(app).encryption_key) as secret:
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


def register_routes(app: Flask) -> None:
    login_required = _login_required(app)

    # POST-burst guard: no single address may hammer the sensitive
    # endpoints (login, signup, sends, bot creation, trades). A
    # burst past the limit is a plain 429 — the action never runs.
    _BURST_ENDPOINTS = {
        "login", "signup", "wallet_send", "wallet_send_confirm",
        "bot_create", "bot_preset_trenches", "coin_buy", "coin_sell",
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
                "network": _cfg(app).network, "coins": COINS,
                "header_balance": header_balance,
                "header_mode": header_balance["mode"]
                if header_balance else "paper",
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
        return app.send_static_file("sw.js")

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
            # The sign-up page also collects the email and the
            # recovery question/answer/hint (same bounds as the
            # Settings recovery form). All three are optional here
            # so username+password-only signups keep working — the
            # page itself asks for them. A question without an
            # answer (or the reverse) is refused.
            email = (request.form.get("email") or "").strip()
            question = (request.form.get("question") or "").strip()
            answer = (request.form.get("answer") or "").strip()
            hint = (request.form.get("hint") or "").strip()
            if email and ("@" not in email
                          or "." not in email.rsplit("@", 1)[-1]
                          or len(email) > 254):
                flash("Enter a valid email address.")
                return redirect(url_for("signup"))
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
            salt, pw_hash = hash_password(password)
            ans_salt, ans_hash = ("", "")
            if question and answer:
                ans_salt, ans_hash = hash_password(answer)
            cur = db.execute(
                "INSERT INTO users (username, password_hash, salt, "
                "email, recovery_question, recovery_answer_hash, "
                "recovery_answer_salt, recovery_hint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username, pw_hash, salt, email, question, ans_hash,
                 ans_salt, hint))
            user_id = cur.lastrowid
            # Fresh app wallet for this user — generated here, encrypted
            # immediately, never shown in full and never logged. The
            # seed and secret are wiped IN PLACE the moment the
            # encrypted blob exists, even if the insert fails.
            seed, pubkey, secret64 = generate_keypair()
            try:
                blob = encrypt_secret(secret64, _cfg(app).encryption_key)
                db.execute(
                    "INSERT INTO wallets (user_id, pubkey, "
                    "encrypted_secret) VALUES (?, ?, ?)",
                    (user_id, pubkey, blob))
            finally:
                wipe_secret(seed)
                wipe_secret(secret64)
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
            db.commit()
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
                "SELECT id, password_hash, salt FROM users WHERE username = ?",
                (username,)).fetchone()
            if not row or not verify_password(password, row["salt"],
                                              row["password_hash"]):
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
        # takes the account username (per the fox design); the email
        # is collected at sign-up and used by the forgot-username
        # flow, never as a login name.
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
    @app.route("/settings")
    @login_required
    def settings():
        user = _current_user(app)
        row = _db(app).execute(
            "SELECT recovery_question, recovery_hint, "
            "recovery_answer_hash FROM users WHERE id = ?",
            (user["id"],)).fetchone()
        passkeys = _db(app).execute(
            "SELECT id, credential_id, transports, created_at "
            "FROM passkey_credentials WHERE user_id = ? ORDER BY id DESC",
            (user["id"],)).fetchall()
        signing_bot = signing_bot_mod.get_signing_bot(_db(app),
                                                      user["id"])
        return render_template(
            "settings.html",
            has_recovery=bool(row["recovery_answer_hash"]),
            recovery_question=row["recovery_question"],
            recovery_hint=row["recovery_hint"],
            passkeys=passkeys,
            support_id=signing_bot["support_id"] if signing_bot else "")

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
            salt, pw_hash = hash_password(password)
            _db(app).execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (pw_hash, salt, user_id))
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

    @app.route("/forgot-username", methods=["GET", "POST"])
    def forgot_username():
        """Recover a forgotten username: signup email + security answer.

        The app sends no mail, so the username is revealed on screen
        — only after the security answer proves ownership. One
        generic message covers every not-ready case (no such email,
        no question set) so the page never reveals whether an email
        is registered. Wrong answers count toward the same 5-try
        lockout as the forgot-password flow, keyed by username.
        """
        if request.method == "POST":
            _csrf_check()
            # Phase 2: the security answer, for the account the
            # email step already resolved.
            if "answer" in request.form:
                user_id = session.get("forgot_username_user_id")
                if not user_id:
                    return redirect(url_for("forgot_username"))
                row = _db(app).execute(
                    "SELECT username, recovery_question, recovery_hint, "
                    "recovery_answer_hash, recovery_answer_salt FROM users "
                    "WHERE id = ?", (user_id,)).fetchone()
                if not row or not row["recovery_question"]:
                    session.pop("forgot_username_user_id", None)
                    return redirect(url_for("forgot_username"))
                if _check_login_lock(app, row["username"]):
                    flash("Too many failed tries. Locked for 15 minutes.")
                    return redirect(url_for("login"))
                answer = request.form.get("answer") or ""
                if not verify_password(answer, row["recovery_answer_salt"],
                                       row["recovery_answer_hash"]):
                    _record_login_fail(app, row["username"])
                    flash("That answer is wrong.")
                    return redirect(url_for("forgot_username"))
                _clear_login_fails(app, row["username"])
                session.pop("forgot_username_user_id", None)
                _log(app, user_id, "username_recovered",
                     "Username revealed through email + security answer.")
                return render_template(
                    "forgot_username.html", phase="revealed",
                    username=row["username"])
            # Phase 1: the signup email.
            email = (request.form.get("email") or "").strip()
            not_ready = ("Account recovery is not set up for this "
                         "account.")
            row = _db(app).execute(
                "SELECT id, recovery_question FROM users "
                "WHERE LOWER(email) = LOWER(?) AND email != ''",
                (email,)).fetchone()
            if not row or not row["recovery_question"]:
                flash(not_ready)
                return redirect(url_for("forgot_username"))
            session["forgot_username_user_id"] = row["id"]
            return redirect(url_for("forgot_username"))
        # GET: the question form when the email step already resolved
        # an account, otherwise the email form.
        user_id = session.get("forgot_username_user_id")
        if user_id:
            row = _db(app).execute(
                "SELECT recovery_question, recovery_hint FROM users "
                "WHERE id = ?", (user_id,)).fetchone()
            if row and row["recovery_question"]:
                return render_template(
                    "forgot_username.html", phase="question",
                    question=row["recovery_question"],
                    hint=row["recovery_hint"])
            session.pop("forgot_username_user_id", None)
        return render_template("forgot_username.html", phase="email")

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
    # Dashboard
    # ------------------------------------------------------------------
    def _sol_price_usd() -> float | None:
        """Current SOL price in USD from the price feed, or None when
        the feed has none — SOL-denominated manual trades refuse
        plainly instead of converting at a guessed rate."""
        price = get_prices()["prices"].get("SOL", {}).get("price")
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    def _dashboard_data(app, user, popup_coin=None, include_trenches=True,
                        chart_range="all"):
        """Everything the coins page (dashboard) renders, for the
        ACTIVE mode only: paper shows the paper portfolio, paper
        bots and paper activity; real (devnet) shows the real wallet,
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
        prices = get_prices()
        coin_rows = _coin_rows(db, user["id"], mode, prices, feed=app.config.get("LIVE_FEED"), app=app)
        holdings_value = sum(r["held"] * (r["price"] or 0.0)
                             for r in coin_rows)
        # Portfolio header (Kraken-style): total balance + chart from
        # recorded snapshots only — never invented history. Paper
        # totals cash + holdings in USD; real totals the on-chain SOL
        # balance (with a USD approx. when SOL has a price).
        sol_price = prices["prices"].get("SOL", {}).get("price")
        if chart_range not in pnl_mod.RANGES:
            chart_range = "all"
        if mode == "devnet":
            portfolio_sol = real_sol
            portfolio_usd = (real_sol * sol_price
                             if real_sol is not None and sol_price
                             else None)
            _points, portfolio_has_data = pnl_mod.chart_points_real(
                db, user["id"], chart_range)
            portfolio_chart_svg = pnl_mod.chart_svg(
                _points, unit="SOL ")
            change_24h = pnl_mod.change_24h(
                db, user["id"], portfolio_sol, real=True)
        else:
            portfolio_sol = None
            portfolio_usd = _paper(app, user["id"]) + holdings_value
            _points, portfolio_has_data = pnl_mod.chart_points(
                db, user["id"], chart_range)
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
        # Trading fees (3.65% of every trade, fees.py): this user's
        # total REAL fees paid in SOL and their real fee payments
        # (newest first), plus the live SOL price for display. The
        # fee total starts at zero for everyone — trades that
        # predate the fee are never charged. Paper fees are never
        # shown here: they are play money, not SOL paid (James,
        # 2026-11-10). The home page shows ONLY the user's own
        # fees: no app-wide total, and the fee recipient address
        # is never shown here (James, 2026-11-10).
        fees_rows = fees_mod.user_fees(db, user["id"], limit=20,
                                       mode="real")
        for _row in fees_rows:
            _split = fees_mod.fee_row_split(_row, sol_price)
            _row["fee_sol_display"] = _split["treasury_sol"]
            _row["burn_sol_display"] = _split["burn_sol"]
            _row["pump_fee_sol_display"] = _split["pump_fee_sol"]
        fees_totals = fees_mod.user_fee_totals(
            db, user["id"], sol_price_usd=sol_price)
        fees_total_sol = fees_totals["treasury_sol"]
        trenches_rows, trenches_error = (
            _trenches_list() if include_trenches else ([], None))
        return {"wallet": wallet if mode == "devnet" else None,
                "receive_address": wallet["pubkey"] if wallet else "",
                "real_sol": real_sol,
                "paper_cash": _paper(app, user["id"]),
                "holdings_value": holdings_value, "prices": prices,
                "bots": bots, "activity": activity,
                "coin_rows": coin_rows, "mode": mode,
                "mode_label": _mode_label(mode),
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
                "fees_total_sol": fees_total_sol,
                "fees_pump_total_sol": fees_totals["pump_fee_sol"],
                "fees_burn_total_sol": fees_totals["burn_sol"],
                "fees_rows": fees_rows,
                "sol_price_usd": sol_price,
                "popup_coin": popup_coin,
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
        prices = get_prices()
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0) for c, e in prices["prices"].items()})
        _log(app, user["id"], "refresh",
             "Refreshed the coins page with live data.",
             _active_mode(db, user["id"]))
        flash("Refreshed with live data.")
        return redirect(url_for("dashboard"))

    @app.route("/api/coins/search")
    @login_required
    def coin_search():
        """Live coin search for the quick Buy/Sell popup: type a name
        and get matching coins with live prices. Known coins come
        from the price feed; anything else comes from DexScreener's
        token search. A feed that does not answer returns an error
        and no invented coins. Each result also carries how much of
        that coin the user holds in the active mode, so the popup
        can offer Sell all when there is something to sell."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        q = (request.args.get("q") or "").strip()
        found = search_coins(q, prices_fn=get_prices)
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

    @app.route("/choose-mode", methods=["POST"])
    @login_required
    def choose_mode():
        """The post-sign-in mode chooser: Demo trading (paper) or
        Real trading (devnet). Sets the user's active mode and
        remembers the choice for this session, so the popup does not
        come back on every page — only after the next sign-in (the
        session is cleared on logout). The user can switch any time
        from the HUD."""
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
             f"Trading mode chosen: {_mode_label(mode)}.")
        flash(f"You are now on {_mode_label(mode)}.")
        return redirect(url_for("wallet"))

    @app.route("/mode", methods=["POST"])
    @login_required
    def mode_switch():
        """Flip the header balance / buy default between Paper and
        Devnet. A plain POST form (no JavaScript) so the switch works
        in any browser, including in-app webviews. Sends the user back
        to the page they came from when it is a safe same-site path.
        """
        _csrf_check()
        user = _current_user(app)
        mode = (request.form.get("mode") or "").lower()
        if mode not in ("paper", "devnet"):
            flash("That mode is not valid.")
            return redirect(url_for("wallet"))
        db = _db(app)
        db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
                   (mode, user["id"]))
        db.commit()
        # A HUD switch is a mode choice too — the chooser must not
        # come back while this sign-in lasts.
        _mark_mode_chosen(db, user["id"])
        _log(app, user["id"], "mode_switch",
             f"Balance view switched to {mode}.")
        ref = request.referrer or ""
        path = urlparse(ref).path if ref else ""
        if not path.startswith("/"):
            path = url_for("wallet")
        return redirect(path)

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
        prices = get_prices()
        rows = _coin_rows(db, user["id"], mode, prices, feed=app.config.get("LIVE_FEED"), app=app)
        row = next((r for r in rows
                    if r["coin_id"] == coin_id_resolved), None)
        if row is None:
            # Known coin, not in the list (no holding, no price) —
            # build its row from what we know.
            row = _unlisted_coin_row(coin_id_resolved, price, prices)
        data = _dashboard_data(app, user, popup_coin=row)
        return render_template("dashboard.html", **data)

    # ------------------------------------------------------------------
    # Coin detail page: tap a coin on the wallet homepage (or the
    # coins page) and get its market chart, 24h high/low, buyers vs
    # sellers %, and Buy / Sell / Sell all — all in SOL. The chart
    # ranges are 24hr / 1day / 1w / 1m / 1y / all-time (24hr and 1day
    # are the same 1-day window, shown under both labels). Chart data
    # is real (CoinGecko market_chart) or the page says "No data yet"
    # — never invented. Buyers/sellers % comes from DexScreener 24h
    # buy/sell txn counts; "—" when the feed has none.
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
        prices = get_prices()
        rows = _coin_rows(db, user["id"], mode, prices, feed=app.config.get("LIVE_FEED"), app=app)
        row = next((r for r in rows
                    if r["coin_id"] == coin_id_resolved), None)
        if row is None:
            row = _unlisted_coin_row(coin_id_resolved, price, prices)
        range_key = (request.args.get("range") or "24h").strip()
        if range_key not in CHART_RANGES:
            range_key = "24h"
        _label, days = CHART_RANGES[range_key]
        chart = fetch_market_chart(coin_id_resolved, days)
        # The chart is a real candlestick chart now: CoinGecko OHLC
        # for known coins, live-feed tick history for mints. The
        # line ("regular") view is drawn from the same candles, so
        # the two views can never disagree. market_chart is still
        # fetched — the 24h high/low come from it, as before.
        if coin_id_resolved in COINS:
            ohlc = fetch_ohlc(coin_id_resolved, days)
            candles = ohlc["candles"]
            chart_interval = ohlc["interval"]
            chart_error = ohlc["error"]
        else:
            chart_interval = candles_mod.interval_for_mint(range_key)
            candles = mint_candles_from_feed(
                coin_id_resolved, chart_interval,
                app.config.get("LIVE_FEED"))
            chart_error = None
        candles_svg = candles_mod.candles_svg(candles, unit="$")
        line_svg = candles_mod.line_svg(candles, unit="$")
        # 24h high/low come from the 24-hour series itself, so they
        # always match the chart. When the detail range IS 24h/1day,
        # the range chart is the 24h series — no second fetch.
        if range_key in ("24h", "1d"):
            high_24h, low_24h = high_low_24h(chart["points"])
        else:
            chart_24h = fetch_market_chart(coin_id_resolved, 1)
            high_24h, low_24h = high_low_24h(chart_24h["points"])
        flow = fetch_trade_flow(coin_id_resolved)
        bots = db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC",
            (user["id"], "real" if mode == "devnet" else "paper")
            ).fetchall()
        wallet = _wallet(app, user["id"])
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
            mode_label=_mode_label(mode),
            receive_address=wallet["pubkey"] if wallet else "",
            prices_source=prices["source"],
            prices_as_of=prices["as_of"],
            # Page 5 Send/Receive must account for the network: the
            # coin's chain, the plain refusal when it is not a Solana
            # asset, and the network this app sends on.
            coin_network=networks_mod.coin_network(coin_id_resolved),
            send_refusal=networks_mod.send_refusal(
                coin_id_resolved, _cfg(app).network),
            network_label=networks_mod.network_label(_cfg(app).network))

    @app.route("/api/coins/<coin_id>/tick")
    @login_required
    def coin_tick(coin_id: str):
        """One live price tick for the coin detail page's live chart.

        The page polls this once a second while it is open (and
        pauses while the tab is hidden). Prices are never invented:
        a known coin comes from the CoinGecko feed — cached up to
        60 seconds server-side, so the returned as_of says when
        that price was actually fetched — and a mint comes fresh
        from the DexScreener trenches feed on each tick. When no
        price is available the response says so (ok false) and the
        page keeps showing the last real price and its time. This
        is a read of the same feeds the coins list already uses —
        no trade, no signing, nothing moves."""
        upper = (coin_id or "").upper()
        if upper in COINS:
            price, source, as_of = _paper_price(upper)
            if price is None:
                return jsonify({"ok": False, "coin_id": upper,
                                "price": None, "source": source,
                                "as_of": as_of,
                                "error": "Price unavailable right now."})
            return jsonify({"ok": True, "coin_id": upper, "price": price,
                            "source": source, "as_of": as_of})
        if is_valid_address(coin_id):
            try:
                prices = fetch_prices_for_mints([coin_id])
            except Exception:
                prices = {}
            if coin_id in prices:
                return jsonify({"ok": True, "coin_id": coin_id,
                                "price": float(prices[coin_id]),
                                "source": "trenches feed",
                                "as_of": _now()})
            return jsonify({"ok": False, "coin_id": coin_id, "price": None,
                            "source": "trenches feed", "as_of": _now(),
                            "error": "Price unavailable right now."})
        return jsonify({"ok": False, "coin_id": coin_id, "price": None,
                        "source": None, "as_of": _now(),
                        "error": "Unknown coin — no live price."})

    @app.route("/api/coins/<coin_id>/candles")
    @login_required
    def coin_candles(coin_id: str):
        """Fresh candle SVGs for the coin detail chart's live
        re-render. Same data as the page load: CoinGecko OHLC for
        known coins, live-feed tick history for mints. The chart
        re-renders from this at most once per candle interval — the
        page never redraws every second. Login required; a coin
        with no candles yet reports count 0 and the page says the
        candles are still building, never a made-up chart."""
        range_key = (request.args.get("range") or "24h").strip()
        if range_key not in CHART_RANGES:
            range_key = "24h"
        resolved, _price, _source = _resolve_coin_price(coin_id)
        if resolved is None:
            abort(404)
        _label, days = CHART_RANGES[range_key]
        if resolved in COINS:
            ohlc = fetch_ohlc(resolved, days)
            candles = ohlc["candles"]
            interval = ohlc["interval"]
        else:
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
        return jsonify({"ok": True,
                        "ticks": feed.snapshot(coins or None),
                        "as_of": _now()})

    @app.route("/api/sparklines")
    @login_required
    def sparklines_snapshot():
        """Fresh 1hr sparklines for the home page's holdings rows.
        The home page polls this every 60 seconds so each coin's
        1hr chart keeps moving with the live feed. Returns
        {ok: True, sparklines: {coin_id: svg-or-null}}. A coin with
        fewer than 2 real points in the last hour returns null —
        the page leaves its sparkline alone (chart building),
        never faked."""
        coins = [c.strip()
                 for c in (request.args.get("coins") or "").split(",")
                 if c.strip()]
        feed = app.config["LIVE_FEED"]
        out = {}
        for coin_id in coins:
            is_mint = coin_id.upper() not in COINS
            svg = sparkline_svg_1h(coin_id, is_mint=is_mint, feed=feed)
            out[coin_id] = svg or None
        return jsonify({"ok": True, "sparklines": out, "as_of": _now()})

    @app.route("/api/fees")
    @login_required
    def fees_snapshot():
        """Live fee totals for the homepage 5-second poll: the
        app-wide totals and this user's own totals, both in SOL,
        for each of the three displayed numbers — the SolEntry
        (treasury) fee, the pump.fun program fee, and the burned
        amount — plus the live SOL price. One lightweight request
        per tick covers all six figures. The totals are sums of
        recorded fee rows only, never guessed. The home page shows
        only the user's own figures; the app-wide figures are in
        this response for any surface that shows them."""
        db = _db(app)
        user = _current_user(app)
        sol_price = _sol_price_usd()
        user_totals = fees_mod.user_fee_totals(
            db, user["id"], sol_price_usd=sol_price)
        app_totals = fees_mod.app_fee_totals(db, sol_price_usd=sol_price)
        return jsonify({"ok": True,
                        "app_total_sol": app_totals["treasury_sol"],
                        "user_total_sol": user_totals["treasury_sol"],
                        "app_treasury_sol": app_totals["treasury_sol"],
                        "user_treasury_sol": user_totals["treasury_sol"],
                        "app_pump_sol": app_totals["pump_fee_sol"],
                        "user_pump_sol": user_totals["pump_fee_sol"],
                        "app_burn_sol": app_totals["burn_sol"],
                        "user_burn_sol": user_totals["burn_sol"],
                        "sol_price_usd": sol_price})

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
                            # Only real coins: known symbols or valid
                            # mint addresses, at most 100 per
                            # connection.
                            new_subs = []
                            for coin in want:
                                if not isinstance(coin, str):
                                    continue
                                if coin.upper() in COINS:
                                    new_subs.append(coin.upper())
                                elif is_valid_address(coin):
                                    new_subs.append(coin)
                                if len(new_subs) >= 100:
                                    break
                            for coin in subscribed:
                                if coin not in new_subs \
                                        and coin not in COINS:
                                    feed.unsubscribe_mint(coin)
                            for coin in new_subs:
                                if coin not in subscribed \
                                        and coin not in COINS:
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
                if coin not in COINS:
                    feed.unsubscribe_mint(coin)

    def _coin_result(heading: str, lines: list, ok: bool,
                     coin_id: str, mode_label: str):
        """The confirmation page after a popup action: what happened,
        the amounts, and the new balance/position — then back to the
        coins screen."""
        return render_template(
            "coin_result.html", heading=heading, lines=lines, ok=ok,
            coin_id=coin_id, mode_label=mode_label)

    def _real_bot_for_trade(app, user, bot_id_raw: str):
        """The real bot a manual real trade is governed by. Returns
        (bot, error_message). The bot's buy-size % is the per-trade
        cap the executor enforces, and its platform must be one real
        trading is built against (pump.fun)."""
        db = _db(app)
        if not (bot_id_raw or "").strip():
            return None, ("Pick one of your real bots to manage this "
                          "trade — its buy-size limit is the cap. "
                          "Create a real bot first if you don't have "
                          "one.")
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
            return None, (f"Bot '{bot['name']}' is a paper bot — pick a "
                          f"real bot for a real trade.")
        platform = (bot["platform"] or "pump.fun")
        if platform not in platforms_mod.REAL_TRADEABLE_PLATFORMS:
            return None, (f"Bot '{bot['name']}' is on {platform} — real "
                          f"trading on {platform} is not live yet. "
                          f"No trade was placed.")
        return bot, None

    @app.route("/coins/<coin_id>/buy", methods=["POST"])
    @login_required
    def coin_buy(coin_id: str):
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        label = _mode_label(mode)
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
                usd = tokens * price
            else:
                try:
                    usd = float(request.form.get("usd") or 0)
                except ValueError:
                    return _coin_result(
                        "Nothing was bought",
                        ["Amount must be a number."], False,
                        coin_id_resolved, label)
            result = _paper_buy_for(app, user, coin_id_resolved, usd,
                                    request.form.get("bot_id") or "")
            lines = [result["message"]]
            if result["ok"]:
                lines.append(
                    f"New paper cash: ${result['cash']:,.2f} CAD.")
                if result.get("bot"):
                    lines.append(
                        f"Bot '{result['bot']['name']}' is looking "
                        f"after this coin.")
            return _coin_result(
                "Bought" if result["ok"] else "Nothing was bought",
                lines, result["ok"], coin_id_resolved, label)
        # Real mode: the EXISTING pump.fun executor, with the same
        # guards as the silent loop. Anything it refuses is shown
        # plainly with no fill — never faked. Amounts arrive as SOL
        # (detail page / popup ticket), as a token amount, or as $
        # (quick Buy/Sell popup) — all convert at the server's own
        # live prices, never at a price the browser sent.
        amount_sol_raw = (request.form.get("amount_sol") or "").strip()
        amount_tokens_raw = (request.form.get("amount_tokens")
                             or "").strip()
        amount_usd_raw = (request.form.get("amount_usd") or "").strip()
        try:
            if amount_sol_raw:
                amount_sol = float(amount_sol_raw)
            elif amount_tokens_raw:
                tokens = float(amount_tokens_raw)
                sol_price = _sol_price_usd()
                if price is None or price <= 0 or sol_price is None:
                    return _coin_result(
                        "Nothing was bought",
                        ["Price unavailable right now — the buy needs "
                         "current prices to convert tokens."],
                        False, coin_id_resolved, label)
                amount_sol = tokens * price / sol_price
            elif amount_usd_raw:
                amount_usd = float(amount_usd_raw)
                sol_price = _sol_price_usd()
                if sol_price is None:
                    return _coin_result(
                        "Nothing was bought",
                        ["SOL price unavailable right now."],
                        False, coin_id_resolved, label)
                amount_sol = amount_usd / sol_price
            else:
                amount_sol = 0.0
        except ValueError:
            return _coin_result(
                "Nothing was bought", ["Amount must be a number."],
                False, coin_id_resolved, label)
        if amount_sol <= 0:
            return _coin_result(
                "Nothing was bought", ["Amount must be more than 0."],
                False, coin_id_resolved, label)
        bot, error = _real_bot_for_trade(
            app, user, request.form.get("bot_id") or "")
        if error:
            return _coin_result("Nothing was bought", [error], False,
                                coin_id_resolved, label)
        result = execute_real_trade(
            bot, user["id"], coin_id_resolved, "buy", amount_sol,
            price or 0.0, db, _cfg(app))
        if result.get("status") != "executed":
            return _coin_result(
                "Nothing was bought",
                [f"No buy was placed — "
                 f"{result.get('reason', 'real execution failed')}."],
                False, coin_id_resolved, label)
        coin_amount = float(result.get("coin_amount") or 0)
        avg_price = _real_avg_price(app, coin_id_resolved, amount_sol,
                                    coin_amount)
        if coin_amount > 0:
            _record_real_holding(db, user["id"], coin_id_resolved,
                                 coin_amount, avg_price)
        wallet = _wallet(app, user["id"])
        new_sol = (_real_balance(app, wallet["pubkey"])
                   if wallet else None)
        # The 3.65% fee travelled in the same transaction as the
        # buy (executor.py); record it once, keyed by the trade's
        # signature, so it is never charged twice.
        fee_sol = result.get("fee_sol")
        if fee_sol is not None:
            fees_mod.record_fee(db, user["id"], bot["id"],
                                coin_id_resolved, "buy", "real", fee_sol,
                                fee_lamports=result.get("fee_lamports"),
                                treasury_sol=result.get("treasury_sol"),
                                burn_sol=result.get("burn_sol"),
                                pump_fee_sol=result.get("pump_fee_sol"),
                                treasury_lamports=result.get(
                                    "treasury_lamports"),
                                burn_lamports=result.get("burn_lamports"),
                                pump_fee_lamports=result.get(
                                    "pump_fee_lamports"),
                                signature=result.get("signature"))
        fee_sentence = (
            f" Fee (3.65%): {fee_sol:.4f} SOL."
            if fee_sol is not None else "")
        _log(app, user["id"], "real_buy",
             f"Bought {coin_amount:.4f} {coin_id_resolved} for "
             f"{amount_sol:.4f} SOL on pump.fun (devnet). Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.{fee_sentence}", "real")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        lines = [f"Bought {coin_amount:.4f} {coin_id_resolved} for "
                 f"{amount_sol:.4f} SOL on pump.fun (devnet).",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
        if fee_sol is not None:
            lines.append(f"Fee (3.65%): {fee_sol:.4f} SOL.")
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
        label = _mode_label(mode)
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
                manual = db.execute(
                    "SELECT amount FROM real_holdings WHERE user_id = ? "
                    "AND coin_id = ?", (user["id"], coin_id_resolved)
                    ).fetchone()
                amount = float(manual["amount"]) if manual else 0.0
                if amount <= 0:
                    bot_managed = db.execute(
                        "SELECT COALESCE(SUM(amount), 0) AS total "
                        "FROM bot_positions WHERE user_id = ? "
                        "AND coin_id = ? AND mode = 'real'",
                        (user["id"], coin_id_resolved)).fetchone()["total"]
                    if bot_managed > 0:
                        return _coin_result(
                            "Nothing was sold",
                            [f"You hold {bot_managed:.4f} "
                             f"{coin_id_resolved} in real mode, but a bot "
                             f"is managing it — the bot sells it by its "
                             f"rules."],
                            False, coin_id_resolved, label)
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
            result = _paper_sell_for(app, user, coin_id_resolved, amount)
            lines = [result["message"]]
            if result["ok"]:
                lines.append(f"New paper cash: ${result['cash']:,.2f} CAD.")
            return _coin_result(
                "Sold" if result["ok"] else "Nothing was sold",
                lines, result["ok"], coin_id_resolved, label)
        # Real mode: sell only the manually-held part. Tokens a bot
        # is managing are sold by that bot's rules, not by hand, so
        # the two ledgers never get out of step.
        manual = db.execute(
            "SELECT amount FROM real_holdings WHERE user_id = ? "
            "AND coin_id = ?", (user["id"], coin_id_resolved)).fetchone()
        manual_amount = float(manual["amount"]) if manual else 0.0
        bot_managed = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM bot_positions "
            "WHERE user_id = ? AND coin_id = ? AND mode = 'real'",
            (user["id"], coin_id_resolved)).fetchone()["total"]
        if amount > manual_amount:
            if manual_amount <= 0 and bot_managed > 0:
                return _coin_result(
                    "Nothing was sold",
                    [f"You hold {bot_managed:.4f} {coin_id_resolved} in "
                     f"real mode, but a bot is managing it — the bot "
                     f"sells it by its rules."],
                    False, coin_id_resolved, label)
            return _coin_result(
                "Nothing was sold",
                [f"You hold {manual_amount:.4f} {coin_id_resolved} in "
                 f"real mode that you can sell by hand"
                 + (f" ({bot_managed:.4f} more is managed by a bot)."
                    if bot_managed > 0 else ".")],
                False, coin_id_resolved, label)
        bot, error = _real_bot_for_trade(
            app, user, request.form.get("bot_id") or "")
        if error:
            return _coin_result("Nothing was sold", [error], False,
                                coin_id_resolved, label)
        result = execute_real_trade(
            bot, user["id"], coin_id_resolved, "sell", amount,
            price or 0.0, db, _cfg(app))
        if result.get("status") != "executed":
            return _coin_result(
                "Nothing was sold",
                [f"No sell was placed — "
                 f"{result.get('reason', 'real execution failed')}."],
                False, coin_id_resolved, label)
        _remove_real_holding(db, user["id"], coin_id_resolved, amount)
        wallet = _wallet(app, user["id"])
        new_sol = (_real_balance(app, wallet["pubkey"])
                   if wallet else None)
        # The 3.65% fee travelled in the same transaction as the
        # sell (executor.py); record it once, keyed by the trade's
        # signature, so it is never charged twice.
        fee_sol = result.get("fee_sol")
        if fee_sol is not None:
            fees_mod.record_fee(db, user["id"], bot["id"],
                                coin_id_resolved, "sell", "real", fee_sol,
                                fee_lamports=result.get("fee_lamports"),
                                treasury_sol=result.get("treasury_sol"),
                                burn_sol=result.get("burn_sol"),
                                pump_fee_sol=result.get("pump_fee_sol"),
                                treasury_lamports=result.get(
                                    "treasury_lamports"),
                                burn_lamports=result.get("burn_lamports"),
                                pump_fee_lamports=result.get(
                                    "pump_fee_lamports"),
                                signature=result.get("signature"))
        fee_sentence = (
            f" Fee (3.65%): {fee_sol:.4f} SOL."
            if fee_sol is not None else "")
        _log(app, user["id"], "real_sell",
             f"Sold {amount:.4f} {coin_id_resolved} on pump.fun "
             f"(devnet). Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.{fee_sentence}", "real")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        lines = [f"Sold {amount:.4f} {coin_id_resolved} on pump.fun "
                 f"(devnet).",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
        if fee_sol is not None:
            lines.append(f"Fee (3.65%): {fee_sol:.4f} SOL.")
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
            mode=mode, mode_label=_mode_label(mode))

    # ------------------------------------------------------------------
    # Wallet: view / send (two-step) / deposit
    # ------------------------------------------------------------------
    @app.route("/wallet")
    @login_required
    def wallet():
        """The homepage: the user's portfolio total, 24h change and
        chart (recorded snapshots only), BUY/SELL, and the coins
        held — in the fox layout. One screen: the holdings list
        lives in a fixed-height panel that scrolls internally; the
        page itself never scrolls. The wallet itself (deposit /
        send / withdraw / platform wallets) lives on /wallet/details.
        `?range=` picks the chart range (1h/1d/1w/1mo/1y/all);
        anything else falls back to 24h."""
        user = _current_user(app)
        chart_range = (request.args.get("range") or "1d").strip()
        if chart_range not in pnl_mod.RANGES:
            chart_range = "1d"
        data = _dashboard_data(app, user, include_trenches=False,
                               chart_range=chart_range)
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
                pumpfun_wallet="", platform_wallets={})
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
            pumpfun_wallet=pumpfun_wallet,
            platform_wallets=platforms_mod.get_platform_wallets(
                db, user["id"]))

    @app.route("/trenches")
    @login_required
    def trenches():
        """The trenches page (black + gold theme): the top 10 fresh
        pump.fun / PumpSwap tokens by 1-hour gain, price + 1h gain
        per row — tapping a token opens the same Buy / Sell / Sell
        all popup as any other coin. A feed failure says the
        trenches are unavailable, never fake rows."""
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        trenches_rows, trenches_error = _trenches_list()
        return render_template(
            "trenches.html", trenches_rows=trenches_rows,
            trenches_error=trenches_error, mode=mode,
            mode_label=_mode_label(mode))

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
                    return redirect(url_for("coin_detail",
                                            coin_id=coin_id))
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
                client = SolanaRpcClient(_cfg(app).rpc_url)
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
                    flash("Amount is more than you hold of this token "
                          f"({token_send_mod.format_token_amount(inspection['balance'], inspection['decimals'])} "
                          f"{_token_ticker(token)}).")
                    return redirect(url_for("wallet_send"))
                cur = db.execute(
                    "INSERT INTO send_intents (user_id, destination, "
                    "amount_lamports, token, status, created_at, mint, "
                    "amount_base_units, decimals) "
                    "VALUES (?, ?, 0, ?, 'pending', ?, ?, ?, ?)",
                    (user["id"], destination, _token_ticker(token),
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
                with temporary_secret(
                        wallet["encrypted_secret"],
                        _cfg(app).encryption_key) as secret:
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
    # Platform wallets: one wallet per trading platform, per user.
    # The Create Wallet button on the bot form posts here. Creating
    # is idempotent — asking again returns the wallet that already
    # exists, it never makes a second one. The Phantom wallet's
    # private key is shown exactly once, at creation, on its own
    # page — never again, and never in a log.
    # ------------------------------------------------------------------
    @app.route("/platforms/<platform>/create", methods=["POST"])
    @login_required
    def platform_wallet_create(platform: str):
        _csrf_check()
        user = _current_user(app)
        if not platforms_mod.is_valid_platform(platform):
            flash("That platform is not valid.")
            return redirect(url_for("bots"))
        return_to = (request.form.get("return_to") or "").strip()
        if not return_to.startswith("/"):
            return_to = url_for("bots")
        db = _db(app)
        if platform == "phantom":
            pubkey, key_b58 = platforms_mod.create_phantom_wallet_with_key(
                db, user["id"], _cfg(app).encryption_key)
            if key_b58:
                _log(app, user["id"], "platform_wallet",
                     f"Created your Phantom wallet {pubkey[:8]}... "
                     f"(private key shown once at creation).")
                return render_template(
                    "platform_wallet_created.html",
                    platform=platform, pubkey=pubkey, key_b58=key_b58,
                    return_to=return_to)
            flash("You already have a Phantom wallet: " + pubkey)
            return redirect(return_to)
        pubkey, created = platforms_mod.create_platform_wallet(
            db, user["id"], platform, _cfg(app).encryption_key)
        if created:
            _log(app, user["id"], "platform_wallet",
                 f"Created your {platforms_mod.PLATFORM_LABELS[platform]} "
                 f"wallet {pubkey[:8]}....")
            flash("Created your "
                  f"{platforms_mod.PLATFORM_LABELS[platform]} wallet: "
                  + pubkey)
        else:
            flash("You already have a "
                  f"{platforms_mod.PLATFORM_LABELS[platform]} wallet: "
                  + pubkey)
        return redirect(return_to)
    # ------------------------------------------------------------------
    # Paper trading (shared by the coin popup and the coin detail
    # page). Helpers return result dicts; the callers decide how to
    # present them (the confirmation page). A coin is either a
    # price-list symbol (SOL, BTC, ...) or a pump.fun mint address
    # (a trenches token the user holds). Symbols are uppercased;
    # mints are NEVER uppercased — base58 addresses are case-sensitive.
    # The old /paper/buy and /paper/sell endpoints (the dashboard's
    # removed Quick buy card) are gone — buys and sells now live
    # only in the coin popup and the coin detail page.
    # ------------------------------------------------------------------
    def _resolve_coin_price(coin: str):
        """(coin_id, price, source) for a symbol or a mint, or
        (None, None, None) when the coin is unknown / unpriced."""
        if not coin:
            return None, None, None
        upper = coin.upper()
        if upper in COINS:
            price, source, _as_of = _paper_price(upper)
            return upper, price, source
        if is_valid_address(coin):
            try:
                prices = fetch_prices_for_mints([coin])
            except Exception:
                prices = {}
            if coin in prices:
                return coin, float(prices[coin]), "trenches feed"
            return coin, None, "trenches feed"
        return None, None, None

    def _fee_sentence(fee_info: list) -> str:
        """The fee sentence for a buy/sell confirmation or log line,
        e.g. " Fee (3.65%): $0.37 (≈ 0.0042 SOL)" — '' when no fee
        was charged. No trailing period: the caller supplies the
        sentence's end punctuation."""
        if not fee_info:
            return ""
        fee = fee_info[0]
        text = f" Fee (3.65%): ${fee['fee_usd']:.2f}"
        if fee.get("fee_sol") is not None:
            text += f" (≈ {fee['fee_sol']:.4f} SOL)"
        return text

    def _paper_buy_for(app, user, coin: str, usd: float,
                       bot_id_raw: str) -> dict:
        """One paper buy. Returns {"ok", "message", ...}. The caller
        presents the message. Never raises for a refused buy."""
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
                        f"Bot '{bot['name']}' is a real-mode bot — pick "
                        f"a paper bot to look after a paper buy. "
                        f"Nothing was bought."}
        if price is None:
            return {"ok": False,
                    "message": "Price unavailable right now."}
        try:
            fee_info: list = []
            amount, usd = ledger_buy(db, user["id"], coin_id, usd, price,
                                     _now(),
                                     bot_id=bot["id"] if bot else None,
                                     sol_price_usd=_sol_price_usd(),
                                     fee_out=fee_info)
        except InsufficientCash:
            return {"ok": False, "message": "Not enough paper cash."}
        if bot is not None:
            # Record the position under the managing bot so the silent
            # loop's rule evaluation (stop loss, sell ladder, trailing
            # stop) governs it per that bot's rules and limits.
            _open_position(db, bot, coin_id, amount, price)
        _log(app, user["id"], "paper_buy",
             f"Bought {amount:.4f} {coin_id} for ${usd:.2f} (paper, "
             f"price source: {source})"
             f"{_fee_sentence(fee_info)}"
             + (f" — looked after by bot '{bot['name']}'." if bot else "."),
             "paper")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Bought {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper)"
                           f"{_fee_sentence(fee_info)}.",
                "amount": amount, "usd": usd, "bot": bot, "cash": cash,
                "coin": coin_id}

    def _paper_sell_for(app, user, coin: str, amount: float) -> dict:
        """One paper sell. Returns {"ok", "message", ...}."""
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
            fee_info: list = []
            usd = ledger_sell(db, user["id"], coin_id, amount, price,
                              _now(), exit_reason="manual sell",
                              sol_price_usd=_sol_price_usd(),
                              fee_out=fee_info)
        except InsufficientHolding:
            return {"ok": False,
                    "message": "You don't hold that much."}
        _log(app, user["id"], "paper_sell",
             f"Sold {amount:.4f} {coin_id} for ${usd:.2f} (paper, "
             f"price source: {source})"
             f"{_fee_sentence(fee_info)}.", "paper")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Sold {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper)"
                           f"{_fee_sentence(fee_info)}.",
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
        # Platform wallets for the Bots-page rotating selector (Page 6
        # rebuild): the same per-platform wallets Page 8 shows, so
        # the selector renders real data and its Create buttons post
        # to the existing create route. Page 8 itself is untouched.
        return render_template("bots.html", **data,
                               memecoin_bots=memecoin_bots,
                               platforms=platforms_mod.PLATFORMS,
                               platform_labels=platforms_mod.PLATFORM_LABELS,
                               platform_notes=platforms_mod.PLATFORM_NOTES,
                               platform_wallets=platforms_mod.get_platform_wallets(
                                   _db(app), user["id"]))

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
            # Platform the bot trades on: pump.fun, Phantom or
            # pumpSwap. An unknown value is not stored — the form is
            # sent back with a plain message.
            platform = (request.form.get("platform") or "pump.fun").strip()
            if not platforms_mod.is_valid_platform(platform):
                flash("That platform is not valid — pick pump.fun, "
                      "Phantom, or pumpSwap.")
                return redirect(url_for("bot_create"))
            category = ""
            coins = [c.strip().upper() for c in
                     (request.form.get("coins") or "SOL").split(",")]
            coins = [c for c in coins if c in COINS] or ["SOL"]
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
        return jsonify(result)

    @app.route("/bots/preset/trenches", methods=["POST"])
    @login_required
    def bot_preset_trenches():
        """Create the Trenches memecoin bot (Memecoin build preset).

        Paper mode, silent off. One per user — if a Trenches bot
        already exists, go to it instead of making a duplicate.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND rules_json LIKE ?",
            (user["id"], TRENCHES_CATEGORY, '%"kind": "trenches"%')
            ).fetchone()
        if existing:
            flash("You already have a Trenches bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], TRENCHES_PRESET_NAME, hash_token(token),
             TRENCHES_CATEGORY, json.dumps(trenches_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{TRENCHES_PRESET_NAME}' (Memecoin build "
             f"preset, paper). Silent trading is off.", "paper")
        flash(f"Created the {TRENCHES_PRESET_NAME} bot (Memecoin build, "
              f"paper). Turn silent trading on when you are ready — "
              f"it only trades within its rules and limits.")
        return redirect(url_for("bot_detail", bot_id=cur.lastrowid))

    @app.route("/bots/preset/scanner", methods=["POST"])
    @login_required
    def bot_preset_scanner():
        """Create the Scanner sniper bot (∆scanner∆ preset).

        Paper mode, silent off. One per user — if a Scanner bot
        already exists, go to it instead of making a duplicate.
        """
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = ? "
            "AND rules_json LIKE ?",
            (user["id"], SCANNER_CATEGORY, '%"kind": "scanner"%')
            ).fetchone()
        if existing:
            flash("You already have a Scanner bot.")
            return redirect(url_for("bot_detail", bot_id=existing["id"]))
        token = new_token()
        cur = db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, platform) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?, ?, ?, "
            "'pump.fun')",
            (user["id"], SCANNER_PRESET_NAME, hash_token(token),
             SCANNER_CATEGORY, json.dumps(scanner_ruleset())))
        db.commit()
        _log(app, user["id"], "bot_create",
             f"Created bot '{SCANNER_PRESET_NAME}' ({SCANNER_DISPLAY} "
             f"preset, paper). Silent trading is off.", "paper")
        flash(f"Created the {SCANNER_PRESET_NAME} bot "
              f"({SCANNER_DISPLAY}, paper). Turn silent trading on when "
              f"you are ready — it only trades within its rules and "
              f"limits.")
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
        prices = get_prices()
        rules, rules_error = load_rules(bot)
        # builderBot's trade ledger: wins, losses, win rate and net
        # P&L for this bot, its recent fills, and every rule
        # adjustment it made (with the reason it gave).
        wl_summary = bot_wl_summary(_db(app), bot_id)
        trades = recent_trades(_db(app), bot_id)
        adjustments = list_adjustments(_db(app), bot_id)
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
                               adjustments=adjustments)

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
            name = (request.form.get("name") or "").strip() or bot["name"]
            # Strategy and category are no longer editable — the
            # pickers are gone (bots are built by builderBot, and
            # the Trenches/Scanner categories come from their
            # presets). The stored values stay exactly as they are.
            strategy = bot["strategy"]
            mode = (request.form.get("mode") or bot["mode"]).lower()
            if mode not in ("paper", "real"):
                mode = bot["mode"]
            platform = (request.form.get("platform")
                        or bot["platform"] or "pump.fun").strip()
            if not platforms_mod.is_valid_platform(platform):
                flash("That platform is not valid — pick pump.fun, "
                      "Phantom, or pumpSwap.")
                return redirect(url_for("bot_edit", bot_id=bot_id))
            category = bot["category"] or ""
            rules_json = bot["rules_json"]
            coins = [c.strip().upper() for c in
                     (request.form.get("coins") or "").split(",")]
            coins = [c for c in coins if c in COINS] or json.loads(bot["coins"])
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
        return render_template(
            "bot_form.html", bot=bot,
            platforms=platforms_mod.PLATFORMS,
            platform_labels=platforms_mod.PLATFORM_LABELS,
            platform_notes=platforms_mod.PLATFORM_NOTES,
            platform_wallets=platforms_mod.get_platform_wallets(
                db, user["id"]))

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
                 f"{'ON' if want else 'OFF'} for bot '{bot['name']}' "
                 f"({bot['mode']}).", bot["mode"])
            flash("Silent trading turned "
                  + ("ON" if want else "OFF")
                  + f" for '{bot['name']}'.")
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
             f"Cleared custom rules for bot '{bot['name']}' — back to "
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
        too."""
        builderbot_mod.purge_bot_data(db, bot_id, user_id)
        db.execute("DELETE FROM scanner_marks WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM scanner_top10 WHERE bot_id = ?",
                   (bot_id,))
        db.execute("DELETE FROM bot_proposals WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user_id))
        db.execute("DELETE FROM bot_positions WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user_id))
        # The bot's fee rows stay — the user still paid those fees
        # to the app — but they are detached from the deleted bot
        # (bot_id NULL), like the paper holdings that survive a bot
        # delete. Deleting the rows instead would understate the
        # user's fees-paid total and the app-wide total.
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
             f"Deleted bot '{bot['name']}'.", bot["mode"])
        flash(f"Bot '{bot['name']}' deleted permanently.")
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
        prices = get_prices()
        coins = json.loads(bot["coins"])
        made = 0
        for coin in coins:
            entry = prices["prices"].get(coin)
            if not entry:
                continue
            side, detail = signal_for_coin(
                coin, entry.get("change_24h"), bot["strategy"])
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
                     f"Set custom rules for bot '{bot['name']}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — bot '{bot['name']}' now follows these "
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
                     f"Cleared custom rules for bot '{bot['name']}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — bot '{bot['name']}' is back to the standard "
                    f"signals."})
            pending_scanner = session.pop("pending_scanner", None)
            if pending_scanner:
                db = _db(app)
                existing = db.execute(
                    "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                    "AND rules_json LIKE ?",
                    (user["id"], SCANNER_CATEGORY,
                     '%"kind": "scanner"%')).fetchone()
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
                    "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?, "
                    "?, ?, 'pump.fun')",
                    (user["id"], SCANNER_PRESET_NAME, hash_token(token),
                     SCANNER_CATEGORY, json.dumps(cleaned)))
                db.commit()
                _log(app, user["id"], "bot_create",
                     f"Created bot '{SCANNER_PRESET_NAME}' "
                     f"({SCANNER_DISPLAY} preset, paper) via assistant.",
                     "paper")
                return jsonify({"reply":
                    f"Done — created the {SCANNER_PRESET_NAME} bot "
                    f"({SCANNER_DISPLAY} sniper, paper):\n"
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
                        f"'{bot['name']}'. Nothing changed."})
                db.execute("UPDATE bots SET silent = ?, updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (want, _now(), bot_id, user["id"]))
                db.commit()
                _log(app, user["id"],
                     "silent_on" if want else "silent_off",
                     f"Silent trading turned "
                     f"{'ON' if want else 'OFF'} for bot '{bot['name']}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — silent trading is now "
                    f"{'ON' if want else 'OFF'} for bot '{bot['name']}'. "
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
                         f"Deleted bot '{bot['name']}' (assistant).", bot["mode"])
                    return jsonify({"reply": f"Done — bot '{bot['name']}' "
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
            prices = get_prices()
            holdings = _db(app).execute(
                "SELECT coin_id, amount FROM paper_holdings WHERE user_id = ?",
                (user["id"],)).fetchall()
            value = sum(h["amount"] * prices["prices"].get(
                h["coin_id"], {}).get("price", 0) for h in holdings)
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
            lines = [f"- {b['name']} ({b['strategy']}, "
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
            bot = db.execute(
                "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
                (user["id"], "%" + parsed["bot_name"] + "%")).fetchone()
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            fields = parsed.get("fields") or {}
            if not fields:
                return jsonify({"reply":
                    "What should I change? For example: \"edit bot "
                    f"{bot['name']} strategy to bold\" or \"stop loss 5%\"."})
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
                 f"Edited bot '{bot['name']}' via assistant.", bot["mode"])
            return jsonify({"reply":
                f"Done — bot '{bot['name']}' updated: " +
                ", ".join(f"{k} = {v}" for k, v in fields.items()) + "."})

        if intent == "bot_delete":
            db = _db(app)
            bot = db.execute(
                "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
                (user["id"], "%" + parsed["bot_name"] + "%")).fetchone()
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            session["pending_delete_bot"] = bot["id"]
            return jsonify({"reply":
                f"Are you sure you want to delete bot '{bot['name']}'? "
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
            bot = _find_bot_by_name(db, user["id"], bot_name)
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
                f"Here is what I understood for bot '{bot['name']}':\n"
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
            bot = _find_bot_by_name(db, user["id"], bot_name)
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, rules_err = load_rules(bot)
            if rules_err:
                return jsonify({"reply":
                    f"Bot '{bot['name']}' has stored rules I can't "
                    f"read ({rules_err}) — it makes no trades until "
                    f"they are fixed. Ask me to set new rules, or "
                    f"remove them."})
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{bot['name']}' has no custom rules yet — "
                    f"it uses the standard signals. Set its trading "
                    f"rules first (for example: \"bot {bot['name']} "
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
                f"Here is what I understood for bot '{bot['name']}':\n"
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
            bot = _find_bot_by_name(db, user["id"], bot_name)
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, rules_err = load_rules(bot)
            if rules_err:
                return jsonify({"reply":
                    f"Bot '{bot['name']}' has stored rules I can't "
                    f"read ({rules_err}) — it makes no trades until "
                    f"they are fixed. Ask me to set new rules, or "
                    f"remove them."})
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{bot['name']}' has no custom rules yet — "
                    f"it uses the standard signals. Set its trading "
                    f"rules first (for example: \"bot {bot['name']} "
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
                f"Here is what I understood for bot '{bot['name']}':\n"
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
                bot = _find_bot_by_name(db, user["id"], bot_name)
                if not bot:
                    return jsonify({"reply":
                        f"I couldn't find a bot called '{bot_name}'. "
                        f"Say \"show my bots\" to see them."})
                rules, err = load_rules(bot)
                if err:
                    return jsonify({"reply":
                        f"Bot '{bot['name']}' has stored rules I can't "
                        f"read ({err}) — it makes no trades until they "
                        f"are fixed. Ask me to set new rules, or remove "
                        f"them."})
                return jsonify({"reply":
                    f"Rules for '{bot['name']}':\n"
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
                    lines.append(f"- {b['name']}: stored rules are broken "
                                 f"({err}) — no trades.")
                elif rules:
                    lines.append(f"- {b['name']}:\n{summarize_rules(rules)}")
                else:
                    lines.append(
                        f"- {b['name']}: standard signals (buy +2% in "
                        f"24h, sell −5% in 24h).")
            return jsonify({"reply": "\n".join(lines)})

        if intent == "rules_clear":
            db = _db(app)
            bot_name = parsed.get("bot_name")
            if not bot_name:
                return jsonify({"reply":
                    "Which bot? Say \"remove rules for <bot name>\"."})
            bot = _find_bot_by_name(db, user["id"], bot_name)
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            rules, _err = load_rules(bot)
            if rules is None:
                return jsonify({"reply":
                    f"Bot '{bot['name']}' has no custom rules — it "
                    f"already uses the standard signals."})
            session["pending_rules_clear"] = bot["id"]
            return jsonify({"reply":
                f"Remove the custom rules from '{bot['name']}' and go "
                f"back to the standard signals? Reply \"yes\" to remove "
                f"them, or \"no\" to keep them."})

        if intent == "preset_create":
            preset = parsed.get("preset") or "trenches"
            if preset == "scanner":
                db = _db(app)
                existing = db.execute(
                    "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                    "AND rules_json LIKE ?",
                    (user["id"], SCANNER_CATEGORY,
                     '%"kind": "scanner"%')).fetchone()
                if existing:
                    return jsonify({"reply":
                        f"You already have a {SCANNER_PRESET_NAME} bot "
                        f"({SCANNER_DISPLAY}). Open it on the Bots page."})
                session["pending_scanner"] = {
                    "rules": scanner_ruleset()}
                return jsonify({"reply":
                    f"Here is the {SCANNER_PRESET_NAME} bot "
                    f"({SCANNER_DISPLAY} sniper, paper):\n"
                    f"{summarize_rules(scanner_ruleset())}\n\n"
                    f"Silent trading is off. Reply \"yes\" to create "
                    f"it, or \"no\" to leave things as they are."})
            db = _db(app)
            existing = db.execute(
                "SELECT id FROM bots WHERE user_id = ? AND category = ? "
                "AND rules_json LIKE ?",
                (user["id"], TRENCHES_CATEGORY,
                 '%"kind": "trenches"%')).fetchone()
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
                "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?, ?, ?, "
                "'pump.fun')",
                (user["id"], TRENCHES_PRESET_NAME, hash_token(token),
                 TRENCHES_CATEGORY, json.dumps(trenches_ruleset())))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{TRENCHES_PRESET_NAME}' (Memecoin build "
                 f"preset, paper) via assistant.", "paper")
            return jsonify({"reply":
                f"Created the {TRENCHES_PRESET_NAME} bot (Memecoin "
                f"build, paper):\n{summarize_rules(trenches_ruleset())}"
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
            lines = [f"- {b['name']} ({b['mode']}): silent "
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
            bot = db.execute(
                "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
                (user["id"], "%" + parsed["bot_name"] + "%")).fetchone()
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{parsed['bot_name']}'. "
                    f"Say \"show my bots\" to see them."})
            if int(bot["silent"]) == want:
                return jsonify({"reply":
                    f"Silent trading is already "
                    f"{'ON' if want else 'OFF'} for '{bot['name']}'."})
            if want == 0:
                # Turning off is the safe direction: apply at once.
                db.execute("UPDATE bots SET silent = 0, updated_at = ? "
                           "WHERE id = ? AND user_id = ?",
                           (_now(), bot["id"], user["id"]))
                db.commit()
                _log(app, user["id"], "silent_off",
                     f"Silent trading turned OFF for bot '{bot['name']}' "
                     f"(assistant).", bot["mode"])
                return jsonify({"reply":
                    f"Done — silent trading is OFF for '{bot['name']}'. "
                    f"The loop stops for it right away."})
            # Turning ON always needs a confirm first.
            session["pending_silent"] = [bot["id"], 1]
            return jsonify({"reply":
                f"Turn on silent trading for '{bot['name']}' "
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
