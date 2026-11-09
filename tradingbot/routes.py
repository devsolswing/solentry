"""HTTP routes for the Trading Bot app.

Security notes:
- Every route that touches user data requires login and scopes all
  queries by session user_id. Tests prove user A cannot read B's data.
- POST forms carry a per-session CSRF token.
- No route ever returns or logs a private key, password, session
  secret, encryption key, or token value. Bot API tokens are shown
  exactly once, on the creation response.
- Real-money movement only ever happens through the two-step send
  flow (create intent -> explicit confirm). The bot never sends
  autonomously in v1.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

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
from .config import Config
from .paper import (InsufficientCash, InsufficientHolding,
                    paper_buy as ledger_buy, paper_sell as ledger_sell)
from .prices import COINS, get_prices
from .signals import signal_for_coin
from .security import (decrypt_secret, encrypt_secret, hash_password,
                       hash_token, new_csrf_token, new_token,
                       verify_password)
from .solana import (SolanaRpcClient, build_signed_transfer,
                     generate_keypair, is_valid_address,
                     lamports_to_sol, sol_to_lamports)

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


def _log(app, user_id: int, kind: str, detail: str) -> None:
    _db(app).execute(
        "INSERT INTO activity (user_id, kind, detail, created_at) "
        "VALUES (?, ?, ?, ?)", (user_id, kind, detail, _now()))
    _db(app).commit()


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
        return {"current_user": user, "csrf_token": _new_csrf(),
                "network": _cfg(app).network, "coins": COINS}

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
        return render_template(
            "settings.html",
            has_recovery=bool(row["recovery_answer_hash"]),
            recovery_question=row["recovery_question"],
            recovery_hint=row["recovery_hint"],
            passkeys=passkeys)

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
    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = _current_user(app)
        wallet = _wallet(app, user["id"])
        real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
        prices = get_prices()
        holdings = _db(app).execute(
            "SELECT coin_id, amount, avg_price FROM paper_holdings "
            "WHERE user_id = ?", (user["id"],)).fetchall()
        holdings_value = sum(
            h["amount"] * prices["prices"].get(h["coin_id"], {}).get("price", 0)
            for h in holdings)
        bots = _db(app).execute(
            "SELECT * FROM bots WHERE user_id = ? ORDER BY id DESC",
            (user["id"],)).fetchall()
        activity = _db(app).execute(
            "SELECT kind, detail, created_at FROM activity "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user["id"],)).fetchall()
        return render_template(
            "dashboard.html", wallet=wallet, real_sol=real_sol,
            paper_cash=_paper(app, user["id"]), holdings=holdings,
            holdings_value=holdings_value, prices=prices, bots=bots,
            activity=activity)

    # ------------------------------------------------------------------
    # Wallet: view / send (two-step) / deposit
    # ------------------------------------------------------------------
    @app.route("/wallet")
    @login_required
    def wallet():
        user = _current_user(app)
        wallet = _wallet(app, user["id"])
        real_sol = _real_balance(app, wallet["pubkey"]) if wallet else None
        intents = _db(app).execute(
            "SELECT id, destination, amount_lamports, token, status, "
            "signature, created_at FROM send_intents WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
        return render_template("wallet.html", wallet=wallet,
                               real_sol=real_sol, intents=intents)

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
        if request.method == "POST":
            _csrf_check()
            if intent["status"] != "pending":
                flash("This send was already handled.")
                return redirect(url_for("wallet"))
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
                     f"signature {tx_sig[:12]}...")
                flash("Send submitted. Signature: " + tx_sig)
            except Exception as exc:
                db.execute(
                    "UPDATE send_intents SET status = 'failed', error = ? "
                    "WHERE id = ?", (str(exc)[:200], intent["id"]))
                db.commit()
                _log(app, user["id"], "send_failed",
                     "Send failed: " + str(exc)[:120])
                flash("Send failed: " + str(exc)[:200])
            return redirect(url_for("wallet"))
        return render_template("send_confirm.html", intent=intent)

    # ------------------------------------------------------------------
    # Paper trading
    # ------------------------------------------------------------------
    @app.route("/paper/buy", methods=["POST"])
    @login_required
    def paper_buy():
        _csrf_check()
        user = _current_user(app)
        coin = (request.form.get("coin") or "").upper()
        if coin not in COINS:
            flash("Unknown coin.")
            return redirect(url_for("dashboard"))
        try:
            usd = float(request.form.get("usd") or 0)
        except ValueError:
            flash("Amount must be a number.")
            return redirect(url_for("dashboard"))
        if usd <= 0:
            flash("Amount must be more than 0.")
            return redirect(url_for("dashboard"))
        price, source, as_of = _paper_price(coin)
        if price is None:
            flash("Price unavailable right now.")
            return redirect(url_for("dashboard"))
        try:
            amount, usd = ledger_buy(_db(app), user["id"], coin, usd, price,
                                     _now())
        except InsufficientCash:
            flash("Not enough paper cash.")
            return redirect(url_for("dashboard"))
        _log(app, user["id"], "paper_buy",
             f"Bought {amount:.4f} {coin} for ${usd:.2f} (paper, "
             f"price source: {source}).")
        flash(f"Bought {amount:.4f} {coin} (paper).")
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
        if amount <= 0:
            flash("Amount must be more than 0.")
            return redirect(url_for("dashboard"))
        price, source, as_of = _paper_price(coin)
        if price is None:
            flash("Price unavailable right now.")
            return redirect(url_for("dashboard"))
        try:
            usd = ledger_sell(_db(app), user["id"], coin, amount, price,
                              _now())
        except InsufficientHolding:
            flash("You don't hold that much.")
            return redirect(url_for("dashboard"))
        _log(app, user["id"], "paper_sell",
             f"Sold {amount:.4f} {coin} for ${usd:.2f} (paper, "
             f"price source: {source}).")
        flash(f"Sold {amount:.4f} {coin} (paper).")
        return redirect(url_for("dashboard"))

    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------
    @app.route("/bots")
    @login_required
    def bots():
        user = _current_user(app)
        bots = _db(app).execute(
            "SELECT * FROM bots WHERE user_id = ? ORDER BY id DESC",
            (user["id"],)).fetchall()
        return render_template("bots.html", bots=bots)

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
                "max_open_trades, api_token_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (user["id"], name, strategy, json.dumps(coins),
                 buy_size, stop_loss, mode, max_open, hash_token(token)))
            db.commit()
            _log(app, user["id"], "bot_create",
                 f"Created bot '{name}' ({strategy}, {mode}). "
                 f"Silent trading is off.")
            # Token shown exactly once, on this page only.
            return render_template("bot_created.html", bot_id=cur.lastrowid,
                                   name=name, token=token)
        return render_template("bot_form.html", bot=None)

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
        return render_template("bot_detail.html", bot=bot,
                               proposals=proposals, prices=prices)

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
                "max_open_trades = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (name, strategy, json.dumps(coins), min(max(buy_size, 1), 100),
                 min(max(stop_loss, 1), 50), mode,
                 min(max(max_open, 1), 5), _now(), bot_id, user["id"]))
            db.commit()
            _log(app, user["id"], "bot_edit", f"Edited bot '{name}'.")
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
                 f"{'ON' if want else 'OFF'} for bot '{bot['name']}' "
                 f"({bot['mode']}).")
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
        db.execute("DELETE FROM bots WHERE id = ? AND user_id = ?",
                   (bot_id, user["id"]))
        db.commit()
        _log(app, user["id"], "bot_delete",
             f"Deleted bot '{bot['name']}'.")
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
                 f"created — confirm before anything moves.")
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
             "in v2; nothing moved.")
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
        db.execute("UPDATE bot_proposals SET status = 'rejected' "
                   "WHERE id = ?", (proposal_id,))
        db.commit()
        _log(app, user["id"], "proposal_reject",
             f"Rejected bot proposal #{proposal_id}.")
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
                     f"(assistant).")
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
                    db.execute("DELETE FROM bots WHERE id = ? AND user_id = ?",
                               (pending, user["id"]))
                    db.commit()
                    _log(app, user["id"], "bot_delete",
                         f"Deleted bot '{bot['name']}' (assistant).")
                    return jsonify({"reply": f"Done — bot '{bot['name']}' "
                                             "is deleted."})
                return jsonify({"reply": "That bot is already gone."})
            return jsonify({"reply": "Nothing is waiting for confirmation."})

        if intent == "cancel":
            session.pop("pending_delete_bot", None)
            session.pop("pending_silent", None)
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
                 f"Created bot '{name}' ({strategy}) via assistant.")
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
                 f"Edited bot '{bot['name']}' via assistant.")
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
                     f"(assistant).")
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
