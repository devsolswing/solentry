"""The Security bot — server-side only.

The Security bot is NOT part of any user's account and users have
NO access to it, ever: it has no UI, no chat, no page, and no API
route any user can call. It is a server-side helper that the app
calls from the signup and account-recovery routes only.

Its role is exactly two things:

  * Send a 4-digit confirmation code to a new user's email when
    they press Create on Page 2 (Create Account), and
  * Send a 4-digit recovery code to a user's email when they use
    Recover Account on Page 1 (for users who forgot their secret
    question's answer and lost access).

Those are the ONLY two ways account information ever reaches a user
by email. The Security bot sends nothing else, to nobody else, for
no other reason. The forgot-username flow (Page 19) reveals a
username ON SCREEN after the security answer proves ownership —
that is not email, not the Security bot, and it is unchanged.

Codes:
  * 4 digits, cryptographically random (secrets module).
  * Single-use, 10-minute expiry.
  * At most 5 wrong tries — the 5th wrong try voids the code and
    a new one must be requested.
  * Only the SHA-256 hash of (per-code salt + code) is stored; the
    code itself is never stored and never logged. The mailer sends
    the code in the email body and then drops it.
  * When SMTP is not configured, issuing a code fails closed
    ("email is not set up yet") — the caller says so plainly
    instead of pretending a code was sent.
  * Dev/test fallback: when the config flag
    SECURITY_CODE_DEV_ECHO is on (tests only, never production),
    issue_code returns the code in its result so the flow can be
    tested without SMTP. The flag defaults off and is never set on
    Railway.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from . import mailer as mailer_mod

CODE_TTL_SECONDS = 10 * 60
CODE_MAX_ATTEMPTS = 5
PURPOSE_SIGNUP = "signup"
PURPOSE_RECOVER = "recover"
PURPOSES = (PURPOSE_SIGNUP, PURPOSE_RECOVER)

_CODE_SUBJECTS = {
    PURPOSE_SIGNUP: "SolEntry — your confirmation code",
    PURPOSE_RECOVER: "SolEntry — your account recovery code",
}


def _now() -> int:
    return int(time.time())


def _new_code() -> str:
    """A random 4-digit code, zero-padded (0000–9999)."""
    return f"{secrets.randbelow(10000):04d}"


def _hash_code(code: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


def issue_code(db, cfg, *, purpose: str, email: str,
               user_id: int | None = None) -> dict:
    """Generate a 4-digit code for (purpose, email), store only its
    hash, and send it via the Security bot's mailer.

    Any outstanding code for the same purpose+email is voided first,
    so there is only ever one live code per purpose per email.

    Returns {"ok": True} on success, or {"ok": False, "error": ...}
    when the code could not be sent (SMTP unconfigured or the send
    failed). When the dev-echo flag is on, the result also carries
    "dev_code" — test-only, never in production.
    """
    if purpose not in PURPOSES:
        return {"ok": False, "error": "bad code purpose"}
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "error": "bad email address"}
    # One live code per purpose per email: void the old ones.
    db.execute(
        "UPDATE email_codes SET used = 1 WHERE purpose = ? AND email = ? "
        "AND used = 0", (purpose, email))
    code = _new_code()
    salt_hex = os.urandom(16).hex()
    code_hash = _hash_code(code, salt_hex)
    expires_at = _now() + CODE_TTL_SECONDS
    db.execute(
        "INSERT INTO email_codes (purpose, user_id, email, code_hash, "
        "code_salt, expires_at, attempts, used, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
        (purpose, user_id, email, code_hash, salt_hex, expires_at,
         _now()))
    db.commit()
    body = (
        "Here is your code:\n\n"
        f"{code}\n\n"
        "Enter it in SolEntry to finish. It expires in 10 minutes "
        "and can only be used once. If you did not ask for this "
        "code, ignore this email.\n"
    )
    if getattr(cfg, "security_code_dev_echo", False):
        # Test-only fallback: no email is sent at all; the code is
        # returned so the flow can be tested without SMTP. Never
        # enabled in production.
        return {"ok": True, "dev_code": code}
    ok, error = mailer_mod.send_email(
        cfg, email, _CODE_SUBJECTS[purpose], body)
    if not ok:
        # The send failed — void the code so a code nobody received
        # can never be entered.
        db.execute(
            "UPDATE email_codes SET used = 1 WHERE purpose = ? "
            "AND email = ? AND code_hash = ?",
            (purpose, email, code_hash))
        db.commit()
        return {"ok": False, "error": error}
    return {"ok": True}


def verify_code(db, *, purpose: str, email: str,
                code: str) -> tuple[bool, str]:
    """Check a 4-digit code for (purpose, email).

    Returns (True, "ok") when the code matches the latest live code
    for that purpose+email, and marks it used (single-use). Otherwise
    returns (False, reason) — "wrong code", "code expired", "too many
    tries", or "no code was sent". Wrong tries are counted; the 5th
    wrong try voids the code. The code itself is never logged.
    """
    if purpose not in PURPOSES:
        return False, "bad code purpose"
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if len(code) != 4 or not code.isdigit():
        return False, "Enter the 4-digit code."
    row = db.execute(
        "SELECT id, code_hash, code_salt, expires_at, attempts, used "
        "FROM email_codes WHERE purpose = ? AND email = ? "
        "ORDER BY id DESC LIMIT 1",
        (purpose, email)).fetchone()
    if not row:
        # Generic on purpose: the caller never learns whether a
        # code was ever sent to that email (no enumeration).
        return False, "That code is wrong."
    if row["used"]:
        return False, "That code has already been used."
    if int(row["attempts"]) >= CODE_MAX_ATTEMPTS:
        db.execute("UPDATE email_codes SET used = 1 WHERE id = ?",
                   (row["id"],))
        db.commit()
        return False, ("Too many wrong tries — that code is void. "
                       "Ask for a new one.")
    if _now() > int(row["expires_at"]):
        db.execute("UPDATE email_codes SET used = 1 WHERE id = ?",
                   (row["id"],))
        db.commit()
        return False, "That code has expired. Ask for a new one."
    expected = _hash_code(code, row["code_salt"])
    if not hmac.compare_digest(expected, row["code_hash"]):
        attempts = int(row["attempts"]) + 1
        db.execute(
            "UPDATE email_codes SET attempts = ?, used = ? WHERE id = ?",
            (attempts, 1 if attempts >= CODE_MAX_ATTEMPTS else 0,
             row["id"]))
        db.commit()
        if attempts >= CODE_MAX_ATTEMPTS:
            return False, ("Too many wrong tries — that code is void. "
                           "Ask for a new one.")
        return False, "That code is wrong."
    db.execute("UPDATE email_codes SET used = 1 WHERE id = ?",
               (row["id"],))
    db.commit()
    return True, "ok"


def void_codes(db, *, purpose: str | None = None,
               user_id: int | None = None,
               email: str | None = None) -> int:
    """Void outstanding (unused) codes — the admin override path
    for a stuck code. Returns how many codes were voided. At least
    one of purpose / user_id / email must be given."""
    if purpose is None and user_id is None and email is None:
        return 0
    conditions = ["used = 0"]
    params: list = []
    if purpose is not None:
        conditions.append("purpose = ?")
        params.append(purpose)
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if email is not None:
        conditions.append("email = ?")
        params.append(email.strip().lower())
    cur = db.execute(
        "UPDATE email_codes SET used = 1 WHERE " + " AND ".join(conditions),
        params)
    db.commit()
    return cur.rowcount
