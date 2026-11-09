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

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from webauthn import (generate_authentication_options,
                      generate_registration_options,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import (base64url_to_bytes, bytes_to_base64url,
                              options_to_json_dict)
from webauthn.helpers.structs import AuthenticatorTransport
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from . import assistant as assistant_mod
from . import donations as donations_mod
from . import passkey as passkey_mod
from . import platforms as platforms_mod
from . import pnl as pnl_mod
from . import signing_bot as signing_bot_mod
from .config import Config
from .executor import execute_real_trade
from .paper import (InsufficientCash, InsufficientHolding,
                    paper_buy as ledger_buy, paper_sell as ledger_sell)
from .prices import COINS, get_prices
from .rules import (TRENCHES_CATEGORY, TRENCHES_PRESET_NAME, load_rules,
                    summarize_rules, trenches_ruleset, validate_ruleset)
from .signals import signal_for_coin
from .silent_loop import _open_position
from .security import (decrypt_secret, encrypt_secret, hash_password,
                       hash_token, new_csrf_token, new_token,
                       verify_password)
from .solana import (SolanaRpcClient, build_signed_transfer,
                     generate_keypair, is_valid_address,
                     lamports_to_sol, sol_to_lamports)
from .trenches import fetch_prices_for_mints

MAX_LOGIN_FAILS = 5
LOCKOUT_MINUTES = 15


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


def _log(app, user_id: int, kind: str, detail: str,
         mode: str = "") -> None:
    """Activity row, tagged with the trading mode it belongs to
    ('paper' / 'real') so pages show each mode's activity separately.
    Account-level rows pass mode='' and show in both modes."""
    _db(app).execute(
        "INSERT INTO activity (user_id, kind, detail, created_at, mode) "
        "VALUES (?, ?, ?, ?, ?)", (user_id, kind, detail, _now(), mode))
    _db(app).commit()


def _find_bot_by_name(db, user_id: int, name: str):
    """First bot whose name contains `name` (assistant-style lookup)."""
    if not name:
        return None
    return db.execute(
        "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
        (user_id, "%" + name + "%")).fetchone()


def _valid_category(value: str) -> str:
    """Bot categories are a closed list — anything else is no category."""
    return value if value == TRENCHES_CATEGORY else ""


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


def _activity_for_mode(db, user_id: int, mode: str):
    """Recent activity for one mode only. Trading rows are tagged
    'paper' or 'real' and never cross over; account rows (mode='')
    belong to neither trading mode and show in both."""
    wanted = "real" if mode == "devnet" else "paper"
    return db.execute(
        "SELECT kind, detail, created_at FROM activity "
        "WHERE user_id = ? AND (mode = ? OR mode = '') "
        "ORDER BY id DESC LIMIT 20",
        (user_id, wanted)).fetchall()


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


def _coin_rows(db, user_id: int, mode: str, prices: dict) -> list:
    """The coins list: every coin in the price list with its name,
    ticker, current price and P&L in the ACTIVE mode, plus any extra
    coins the user holds that are not in the price list (trenches
    mints). P&L is (current price − average cost) × amount; a coin
    with no holding has P&L None, and a holding with no current
    price has P&L None and is valued at cost — never guessed."""
    price_map = {c: e.get("price") for c, e in prices["prices"].items()}
    if mode == "devnet":
        held = {h["coin_id"]: h for h in _real_holdings_view(db, user_id)}
    else:
        held = {h["coin_id"]: h for h in db.execute(
            "SELECT coin_id, amount, avg_price FROM paper_holdings "
            "WHERE user_id = ?", (user_id,)).fetchall()}
    mint_ids = [c for c in held if c not in price_map]
    if mint_ids:
        try:
            for mint, price in fetch_prices_for_mints(
                    mint_ids).items():
                price_map[mint] = price
        except Exception:
            pass
    rows = []
    for symbol, info in COINS.items():
        price = price_map.get(symbol)
        h = held.get(symbol)
        pnl = None
        if h is not None and price is not None:
            pnl = (float(price) - float(h["avg_price"])) * float(h["amount"])
        rows.append({"coin_id": symbol, "name": info["name"],
                     "ticker": symbol, "price": price,
                     "change_24h": prices["prices"].get(symbol, {}).get(
                         "change_24h"),
                     "held": float(h["amount"]) if h else 0.0,
                     "avg_price": float(h["avg_price"]) if h else None,
                     "pnl": pnl, "is_mint": False})
    for mint in sorted(held):
        if mint in COINS:
            continue
        price = price_map.get(mint)
        h = held[mint]
        pnl = None
        if price is not None:
            pnl = (float(price) - float(h["avg_price"])) * float(h["amount"])
        rows.append({"coin_id": mint,
                     "name": "Pump.fun token " + mint[:4] + "…" + mint[-4:],
                     "ticker": mint[:4] + "…",
                     "price": price, "change_24h": None,
                     "held": float(h["amount"]),
                     "avg_price": float(h["avg_price"]),
                     "pnl": pnl, "is_mint": True})
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


def register_routes(app: Flask) -> None:
    login_required = _login_required(app)

    @app.context_processor
    def inject_globals():
        user = _current_user(app)
        header_balance = _header_balance(app, user) if user else None
        return {"current_user": user, "csrf_token": _new_csrf(),
                "network": _cfg(app).network, "coins": COINS,
                "header_balance": header_balance,
                "header_mode": header_balance["mode"]
                if header_balance else "paper"}

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "network": _cfg(app).network})

    @app.route("/")
    def index():
        return render_template("index.html")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "POST":
            _csrf_check()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
                flash("Username must be 3-20 letters, numbers, or _.")
                return redirect(url_for("signup"))
            if len(password) < 8:
                flash("Password must be at least 8 characters.")
                return redirect(url_for("signup"))
            db = _db(app)
            if db.execute("SELECT id FROM users WHERE username = ?",
                          (username,)).fetchone():
                flash("That username is taken.")
                return redirect(url_for("signup"))
            salt, pw_hash = hash_password(password)
            cur = db.execute(
                "INSERT INTO users (username, password_hash, salt) "
                "VALUES (?, ?, ?)", (username, pw_hash, salt))
            user_id = cur.lastrowid
            # Fresh app wallet for this user — generated here, encrypted
            # immediately, never shown in full and never logged.
            _seed, pubkey, secret64 = generate_keypair()
            blob = encrypt_secret(secret64, _cfg(app).encryption_key)
            secret64 = b"\x00" * len(secret64)  # drop the reference
            db.execute(
                "INSERT INTO wallets (user_id, pubkey, encrypted_secret) "
                "VALUES (?, ?, ?)", (user_id, pubkey, blob))
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
            session["user_id"] = user_id
            flash("Welcome! Your app wallet was created for you.")
            return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="signup")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            _csrf_check()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if _check_login_lock(app, username):
                flash("Too many failed tries. Locked for 15 minutes.")
                return redirect(url_for("login"))
            row = _db(app).execute(
                "SELECT id, password_hash, salt FROM users WHERE username = ?",
                (username,)).fetchone()
            if not row or not verify_password(password, row["salt"],
                                              row["password_hash"]):
                _record_login_fail(app, username)
                flash("Wrong username or password.")
                return redirect(url_for("login"))
            _clear_login_fails(app, username)
            session["user_id"] = row["id"]
            return redirect(url_for("dashboard"))
        # SolEntry login page: hero + donate card. The donation wallet is
        # created once, on first use, and its public address is shown as
        # the donation destination. The field is labeled "Email Address"
        # per the design; accounts are username-based, so it accepts the
        # account username (no email addresses are stored).
        return render_template(
            "login.html",
            donation_address=donations_mod.get_or_create_donation_wallet(app),
            rpc_url=_cfg(app).rpc_url)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

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
        _log(app, user_id, "passkey_login", "Signed in with a passkey.")
        session["user_id"] = user_id
        return jsonify({"ok": True, "redirect": url_for("dashboard")})

    # ------------------------------------------------------------------
    # Donations (public: the donate card lives on the login page)
    # ------------------------------------------------------------------
    @app.route("/api/donate/info")
    def donate_info():
        return jsonify({
            "donation_address":
                donations_mod.get_or_create_donation_wallet(app),
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
        return render_template(
            "admin_donations.html",
            donations=donations_mod.list_donations(app),
            donation_address=donations_mod.get_or_create_donation_wallet(app))

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def _dashboard_data(app, user, popup_coin=None):
        """Everything the coins page (dashboard) renders, for the
        ACTIVE mode only: paper shows the paper portfolio, paper
        bots and paper activity; real (devnet) shows the real wallet,
        real bots and real activity. The two modes never mix on a
        page. `popup_coin` is the coin row for the open coin popup,
        or None."""
        db = _db(app)
        mode = _active_mode(db, user["id"])
        wallet = _wallet(app, user["id"])
        real_sol = (_real_balance(app, wallet["pubkey"])
                    if wallet and mode == "devnet" else None)
        prices = get_prices()
        coin_rows = _coin_rows(db, user["id"], mode, prices)
        holdings_value = sum(r["held"] * (r["price"] or 0.0)
                             for r in coin_rows)
        # Portfolio header (Kraken-style): total balance + chart from
        # recorded snapshots only — never invented history. Paper
        # totals cash + holdings in USD; real totals the on-chain SOL
        # balance (with a USD approx. when SOL has a price).
        sol_price = prices["prices"].get("SOL", {}).get("price")
        if mode == "devnet":
            portfolio_sol = real_sol
            portfolio_usd = (real_sol * sol_price
                             if real_sol is not None and sol_price
                             else None)
            _points, portfolio_has_data = pnl_mod.chart_points_real(
                db, user["id"], "all")
            portfolio_chart_svg = pnl_mod.chart_svg(
                _points, unit="SOL ")
        else:
            portfolio_sol = None
            portfolio_usd = _paper(app, user["id"]) + holdings_value
            _points, portfolio_has_data = pnl_mod.chart_points(
                db, user["id"], "all")
            portfolio_chart_svg = pnl_mod.chart_svg(_points)
        bots = db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC",
            (user["id"], "real" if mode == "devnet" else "paper")
            ).fetchall()
        activity = _activity_for_mode(db, user["id"], mode)
        signing_bot = signing_bot_mod.get_signing_bot(db, user["id"])
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
                "show_mode_chooser": not session.get("mode_chosen"),
                "support_id": signing_bot["support_id"]
                if signing_bot else "",
                "popup_coin": popup_coin}

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = _current_user(app)
        return render_template("dashboard.html",
                               **_dashboard_data(app, user))

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
            return redirect(url_for("dashboard"))
        db = _db(app)
        db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
                   (mode, user["id"]))
        db.commit()
        session["mode_chosen"] = True
        _log(app, user["id"], "mode_switch",
             f"Trading mode chosen: {_mode_label(mode)}.")
        flash(f"You are now on {_mode_label(mode)}.")
        return redirect(url_for("dashboard"))

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
            return redirect(url_for("dashboard"))
        db = _db(app)
        db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
                   (mode, user["id"]))
        db.commit()
        _log(app, user["id"], "mode_switch",
             f"Balance view switched to {mode}.")
        ref = request.referrer or ""
        path = urlparse(ref).path if ref else ""
        if not path.startswith("/"):
            path = url_for("dashboard")
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
        rows = _coin_rows(db, user["id"], mode, prices)
        row = next((r for r in rows
                    if r["coin_id"] == coin_id_resolved), None)
        if row is None:
            # Known coin, not in the list (no holding, no price) —
            # build its row from what we know.
            info = COINS.get(coin_id_resolved)
            row = {"coin_id": coin_id_resolved,
                   "name": info["name"] if info else
                   "Pump.fun token " + coin_id_resolved[:4] + "…" +
                   coin_id_resolved[-4:],
                   "ticker": coin_id_resolved if info else
                   coin_id_resolved[:4] + "…",
                   "price": price, "held": 0.0, "avg_price": None,
                   "change_24h": prices["prices"].get(
                       coin_id_resolved, {}).get("change_24h"),
                   "pnl": None, "is_mint": coin_id_resolved not in COINS}
        data = _dashboard_data(app, user, popup_coin=row)
        return render_template("dashboard.html", **data)

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
                    f"New paper cash: ${result['cash']:,.2f}.")
                if result.get("bot"):
                    lines.append(
                        f"Bot '{result['bot']['name']}' is looking "
                        f"after this coin.")
            return _coin_result(
                "Bought" if result["ok"] else "Nothing was bought",
                lines, result["ok"], coin_id_resolved, label)
        # Real mode: the EXISTING pump.fun executor, with the same
        # guards as the silent loop. Anything it refuses is shown
        # plainly with no fill — never faked.
        try:
            amount_sol = float(request.form.get("amount_sol") or 0)
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
        _log(app, user["id"], "real_buy",
             f"Bought {coin_amount:.4f} {coin_id_resolved} for "
             f"{amount_sol:.4f} SOL on pump.fun (devnet). Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.", "real")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        lines = [f"Bought {coin_amount:.4f} {coin_id_resolved} for "
                 f"{amount_sol:.4f} SOL on pump.fun (devnet).",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
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
                lines.append(f"New paper cash: ${result['cash']:,.2f}.")
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
        _log(app, user["id"], "real_sell",
             f"Sold {amount:.4f} {coin_id_resolved} on pump.fun "
             f"(devnet). Signature: "
             f"{str(result.get('signature', ''))[:12]}... Signing bot: "
             f"{result.get('signing_bot', '')}.", "real")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        lines = [f"Sold {amount:.4f} {coin_id_resolved} on pump.fun "
                 f"(devnet).",
                 f"Signature: {result.get('signature', '')}",
                 f"Signing bot: {result.get('signing_bot', '')}."]
        if new_sol is not None:
            lines.append(f"New wallet balance: {new_sol:.4f} SOL.")
        return _coin_result("Sold", lines, True, coin_id_resolved, label)
    # ------------------------------------------------------------------
    # P&L dashboard: balance, current coins with per-coin P&L, and a
    # P&L chart built ONLY from recorded snapshots (never invented
    # history). Ranges: All-time, 1 year, 1 month, 1 week, 1 day,
    # 1 hour. The on-chain SOL balance is shown alongside the paper
    # portfolio, labelled plainly, so any difference is visible.
    # ------------------------------------------------------------------
    @app.route("/pnl")
    @login_required
    def pnl():
        user = _current_user(app)
        range_key = (request.args.get("range") or "all").strip()
        if range_key not in pnl_mod.RANGES:
            range_key = "all"
        prices = get_prices()
        price_map = {c: e.get("price")
                     for c, e in prices["prices"].items()}
        db = _db(app)
        mode = _active_mode(db, user["id"])
        wallet = _wallet(app, user["id"])
        real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
        sol_price = prices["prices"].get("SOL", {}).get("price")
        real_sol_usd = (real_sol * sol_price
                        if real_sol is not None and sol_price else None)
        if mode == "devnet":
            # Real mode: real holdings only (manual real_holdings +
            # bot-managed real positions), the on-chain wallet, and a
            # chart of the recorded wallet SOL balance over time.
            # The paper portfolio is never shown here.
            real_rows = _real_holdings_view(db, user["id"])
            mint_ids = [h["coin_id"] for h in real_rows
                        if h["coin_id"] not in price_map]
            if mint_ids:
                try:
                    for mint, price in fetch_prices_for_mints(
                            mint_ids).items():
                        price_map[mint] = price
                except Exception:
                    pass
            holdings = []
            for h in real_rows:
                raw_price = price_map.get(h["coin_id"])
                price = float(raw_price) if raw_price is not None else None
                amount = float(h["amount"])
                avg = float(h["avg_price"])
                if price is not None:
                    value = amount * price
                    pnl_value = (price - avg) * amount
                else:
                    value = amount * avg
                    pnl_value = None
                holdings.append({"coin_id": h["coin_id"], "amount": amount,
                                 "avg_price": avg, "price": price,
                                 "value": value, "pnl": pnl_value})
            holdings_value = sum(h["value"] for h in holdings)
            totals = {"cash_usd": 0.0, "holdings": holdings,
                      "holdings_value_usd": holdings_value,
                      "total_value_usd": holdings_value}
            points, has_data = pnl_mod.chart_points_real(
                db, user["id"], range_key)
            return render_template(
                "pnl.html", range_key=range_key,
                ranges=pnl_mod.RANGES, totals=totals,
                real_sol=real_sol, real_sol_usd=real_sol_usd,
                sol_price=sol_price, start_cash=0.0,
                total_pnl=holdings_value,
                points=points, has_data=has_data,
                chart_svg=pnl_mod.chart_svg(points, unit="SOL "),
                prices_source=prices["source"],
                prices_as_of=prices["as_of"],
                mode=mode, mode_label=_mode_label(mode))
        # Paper mode: the paper portfolio only. The real wallet is
        # never shown here — switch to Real trading to see it.
        held = db.execute(
            "SELECT DISTINCT coin_id FROM paper_holdings WHERE user_id = ?",
            (user["id"],)).fetchall()
        trenches_mints = [r["coin_id"] for r in held
                          if r["coin_id"] not in price_map]
        if trenches_mints:
            try:
                for mint, price in fetch_prices_for_mints(
                        trenches_mints).items():
                    price_map[mint] = price
            except Exception:
                pass
        totals = pnl_mod.portfolio_totals(db, user["id"], price_map)
        start_cash = _cfg(app).paper_start_cash
        points, has_data = pnl_mod.chart_points(db, user["id"], range_key)
        return render_template(
            "pnl.html", range_key=range_key,
            ranges=pnl_mod.RANGES, totals=totals,
            real_sol=None, real_sol_usd=None,
            sol_price=sol_price, start_cash=start_cash,
            total_pnl=totals["total_value_usd"] - start_cash,
            points=points, has_data=has_data,
            chart_svg=pnl_mod.chart_svg(points),
            prices_source=prices["source"], prices_as_of=prices["as_of"],
            mode=mode, mode_label=_mode_label(mode))

    # ------------------------------------------------------------------
    # Wallet: view / send (two-step) / deposit
    # ------------------------------------------------------------------
    @app.route("/wallet")
    @login_required
    def wallet():
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        if mode == "paper":
            # Paper mode: the paper balance only. The real wallet,
            # send history and platform wallets live in Real trading
            # — switch mode in the HUD to see them.
            prices = get_prices()
            price_map = {c: e.get("price", 0.0)
                         for c, e in prices["prices"].items()}
            cash = _paper(app, user["id"])
            holdings_value = sum(
                h["amount"] * price_map.get(h["coin_id"], 0.0)
                for h in db.execute(
                    "SELECT coin_id, amount FROM paper_holdings "
                    "WHERE user_id = ?", (user["id"],)).fetchall())
            return render_template(
                "wallet.html", wallet=None, real_sol=None, intents=[],
                pumpfun_wallet="", platform_wallets={},
                mode=mode, mode_label=_mode_label(mode),
                paper_cash=cash, holdings_value=holdings_value)
        wallet = _wallet(app, user["id"])
        real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
        pumpfun_wallet = db.execute(
            "SELECT pumpfun_wallet FROM users WHERE id = ?",
            (user["id"],)).fetchone()["pumpfun_wallet"]
        intents = db.execute(
            "SELECT id, destination, amount_lamports, token, status, "
            "signature, created_at FROM send_intents WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
        return render_template(
            "wallet.html", wallet=wallet,
            real_sol=real_sol, intents=intents,
            pumpfun_wallet=pumpfun_wallet,
            platform_wallets=platforms_mod.get_platform_wallets(
                db, user["id"]),
            mode=mode, mode_label=_mode_label(mode),
            paper_cash=0.0, holdings_value=0.0)

    @app.route("/wallet/send", methods=["GET", "POST"])
    @login_required
    def wallet_send():
        user = _current_user(app)
        if request.method == "POST":
            _csrf_check()
            destination = (request.form.get("destination") or "").strip()
            token = (request.form.get("token") or "SOL").strip().upper()
            try:
                amount = float(request.form.get("amount") or 0)
            except ValueError:
                flash("Amount must be a number.")
                return redirect(url_for("wallet_send"))
            if token != "SOL":
                flash("Only SOL sends are supported in v1.")
                return redirect(url_for("wallet_send"))
            if not is_valid_address(destination):
                flash("That destination address is not valid.")
                return redirect(url_for("wallet_send"))
            if amount <= 0:
                flash("Amount must be more than 0.")
                return redirect(url_for("wallet_send"))
            lamports = sol_to_lamports(amount)
            db = _db(app)
            cur = db.execute(
                "INSERT INTO send_intents (user_id, destination, "
                "amount_lamports, token, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (user["id"], destination, lamports, token, _now()))
            db.commit()
            # Step 1 done: the user must confirm on the next screen.
            # return_to=coins (from the coin popup's Send) carries
            # through, so the completed send lands back on the coins
            # screen instead of the wallet page.
            return_to = (request.form.get("return_to") or "").strip()
            if return_to == "coins":
                return redirect(url_for("wallet_send_confirm",
                                        intent_id=cur.lastrowid,
                                        return_to="coins"))
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
        if return_to != "coins":
            return_to = ""
        if request.method == "POST":
            _csrf_check()
            if intent["status"] != "pending":
                flash("This send was already handled.")
                return redirect(url_for("dashboard" if return_to else
                                        "wallet"))
            wallet = _wallet(app, user["id"])
            try:
                secret = decrypt_secret(wallet["encrypted_secret"],
                                        _cfg(app).encryption_key)
                seed = secret[:32]
                pubkey = wallet["pubkey"]
                client = SolanaRpcClient(_cfg(app).rpc_url)
                blockhash = client.get_recent_blockhash()
                signed_b64, signature = build_signed_transfer(
                    seed, pubkey, intent["destination"],
                    intent["amount_lamports"], blockhash)
                seed = b"\x00" * 32
                secret = b"\x00" * len(secret)
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
            return redirect(url_for("dashboard" if return_to else
                                    "wallet"))
        return render_template("send_confirm.html", intent=intent,
                               return_to=return_to)

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
    # Paper trading (shared by the old buy/sell endpoints and the
    # coin popup). Helpers return result dicts; the callers decide
    # how to present them (flash + redirect, or the confirmation
    # page). A coin is either a price-list symbol (SOL, BTC, ...) or
    # a pump.fun mint address (a trenches token the user holds).
    # Symbols are uppercased; mints are NEVER uppercased — base58
    # addresses are case-sensitive.
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
            amount, usd = ledger_buy(db, user["id"], coin_id, usd, price,
                                     _now())
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
             + (f" — looked after by bot '{bot['name']}'." if bot else "."),
             "paper")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Bought {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper).",
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
            usd = ledger_sell(db, user["id"], coin_id, amount, price,
                              _now())
        except InsufficientHolding:
            return {"ok": False,
                    "message": "You don't hold that much."}
        _log(app, user["id"], "paper_sell",
             f"Sold {amount:.4f} {coin_id} for ${usd:.2f} (paper, "
             f"price source: {source}).", "paper")
        _record_pnl_snapshot(
            app, user["id"],
            {c: e.get("price", 0.0)
             for c, e in get_prices()["prices"].items()})
        cash = _paper(app, user["id"])
        return {"ok": True,
                "message": f"Sold {amount:.4f} {coin_id} for "
                           f"${usd:.2f} (paper).",
                "amount": amount, "usd": usd, "cash": cash,
                "coin": coin_id}

    @app.route("/paper/buy", methods=["POST"])
    @login_required
    def paper_buy():
        _csrf_check()
        user = _current_user(app)
        db = _db(app)
        # Mode comes from the buy form and defaults to the user's
        # header mode. Real (devnet) buys are placed from the coin
        # popup, which routes them through the pump.fun executor —
        # this endpoint is the paper one, so a real choice is sent
        # there instead of being refused into nothing.
        mode = (request.form.get("mode")
                or _active_mode(db, user["id"])).lower()
        if mode not in ("paper", "real"):
            mode = _active_mode(db, user["id"])
        coin = (request.form.get("coin") or "").upper()
        if mode == "real":
            if coin not in COINS:
                flash("Unknown coin.")
                return redirect(url_for("dashboard"))
            flash("Real buys are placed from the coin popup — tap the "
                  "coin and choose Buy (real). Nothing was bought here.")
            return redirect(url_for("coin_popup", coin_id=coin))
        if coin not in COINS:
            flash("Unknown coin.")
            return redirect(url_for("dashboard"))
        try:
            usd = float(request.form.get("usd") or 0)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("dashboard"))
        result = _paper_buy_for(app, user, coin, usd,
                                request.form.get("bot_id") or "")
        flash(result["message"]
              + (f" Bot '{result['bot']['name']}' is looking after it."
                 if result["ok"] and result.get("bot") else ""))
        return redirect(url_for("dashboard"))

    @app.route("/paper/sell", methods=["POST"])
    @login_required
    def paper_sell():
        _csrf_check()
        user = _current_user(app)
        coin = (request.form.get("coin") or "").upper()
        if coin not in COINS:
            flash("Unknown coin.")
            return redirect(url_for("dashboard"))
        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("dashboard"))
        result = _paper_sell_for(app, user, coin, amount)
        flash(result["message"])
        return redirect(url_for("dashboard"))
    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------
    @app.route("/bots")
    @login_required
    def bots():
        user = _current_user(app)
        db = _db(app)
        mode = _active_mode(db, user["id"])
        # Only this mode's bots are shown — paper and real bots never
        # mix on a page. The Trenches preset is a paper bot, so its
        # card only appears in paper mode.
        bots = db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC",
            (user["id"], "real" if mode == "devnet" else "paper")
            ).fetchall()
        memecoin_bots = [b for b in bots
                         if b["category"] == TRENCHES_CATEGORY]
        return render_template("bots.html", bots=bots,
                               memecoin_bots=memecoin_bots,
                               mode=mode, mode_label=_mode_label(mode))

    @app.route("/bots/create", methods=["GET", "POST"])
    @login_required
    def bot_create():
        user = _current_user(app)
        if request.method == "POST":
            _csrf_check()
            name = (request.form.get("name") or "").strip() or "My Bot"
            strategy = (request.form.get("strategy") or "balanced").lower()
            if strategy not in assistant_mod.STRATEGIES:
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
            category = _valid_category(
                (request.form.get("category") or "").strip())
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
            # Silent is NEVER on at creation — the user enables it
            # afterwards through the confirm flow. Any silent value in
            # the form is ignored on purpose.
            cur = db.execute(
                "INSERT INTO bots (user_id, name, strategy, coins, "
                "buy_size_pct, stop_loss_pct, mode, silent, "
                "max_open_trades, api_token_hash, category, platform) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (user["id"], name, strategy, json.dumps(coins),
                 buy_size, stop_loss, mode, max_open, hash_token(token),
                 category, platform))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{name}' ({strategy}, {mode}, "
                 f"{platform}). "
                 f"Silent trading is off.", mode)
            # Token shown exactly once, on this page only.
            return render_template("bot_created.html", bot_id=cur.lastrowid,
                                   name=name, token=token)
        return render_template(
            "bot_form.html", bot=None,
            platforms=platforms_mod.PLATFORMS,
            platform_labels=platforms_mod.PLATFORM_LABELS,
            platform_notes=platforms_mod.PLATFORM_NOTES,
            platform_wallets=platforms_mod.get_platform_wallets(
                _db(app), user["id"]))

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
        return render_template("bot_detail.html", bot=bot,
                               proposals=proposals, prices=prices,
                               rules_summary=summarize_rules(rules),
                               rules_error=rules_error)

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
            strategy = (request.form.get("strategy") or bot["strategy"]).lower()
            if strategy not in assistant_mod.STRATEGIES:
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
            category = _valid_category(
                (request.form.get("category")
                 if request.form.get("category") is not None
                 else bot["category"] or "").strip())
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
                "updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (name, strategy, json.dumps(coins), min(max(buy_size, 1), 100),
                 min(max(stop_loss, 1), 50), mode,
                 min(max(max_open, 1), 5), category, platform, _now(),
                 bot_id, user["id"]))
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
        db.execute("DELETE FROM bot_proposals WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user["id"]))
        # Positions the bot looks after are un-managed, not sold: the
        # coins stay in the user's paper holdings (sellable by hand),
        # only the bot's management record goes. This must happen
        # before deleting the bot row — bot_positions references
        # bots(id) and the delete would fail while rows exist.
        db.execute("DELETE FROM bot_positions WHERE bot_id = ? AND user_id = ?",
                   (bot_id, user["id"]))
        db.execute("DELETE FROM bots WHERE id = ? AND user_id = ?",
                   (bot_id, user["id"]))
        db.commit()
        _log(app, user["id"], "bot_delete",
             f"Deleted bot '{bot['name']}'.", bot["mode"])
        flash("Bot deleted.")
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
                    db.execute("DELETE FROM bot_proposals "
                               "WHERE bot_id = ? AND user_id = ?",
                               (pending, user["id"]))
                    # Same as the page delete: un-manage positions
                    # first (coins stay in paper holdings), or the
                    # bot row delete fails on the foreign key.
                    db.execute("DELETE FROM bot_positions "
                               "WHERE bot_id = ? AND user_id = ?",
                               (pending, user["id"]))
                    db.execute("DELETE FROM bots WHERE id = ? AND user_id = ?",
                               (pending, user["id"]))
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
                f"Paper: ${_paper(app, user['id']):,.2f} cash + "
                f"${value:,.2f} in holdings. Real wallet: {real_txt} "
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
            if not bot_name:
                return jsonify({"reply":
                    "Which bot should follow these rules? Say \"bot "
                    "<name> scan ...\" — or \"show my bots\" to see them."})
            bot = _find_bot_by_name(db, user["id"], bot_name)
            if not bot:
                return jsonify({"reply":
                    f"I couldn't find a bot called '{bot_name}'. "
                    f"Say \"show my bots\" to see them."})
            partial = dict(parsed.get("rules") or {})
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
