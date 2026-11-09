"""Phone + email linking and two-code account recovery.

Design:
- Verification codes are 6 random digits, valid for 10 minutes.
- Only a salted SHA-256 hash is stored — the plaintext code is never
  written to the database, never logged, and never returned in any
  response. It exists only in memory long enough to send it.
- At most 5 wrong guesses per code; the 5th voids the code.
- At most one code per (user, purpose, contact) at a time; a resend
  is allowed only 60 seconds after the previous send.
- Purposes: 'link' (confirming a new phone/email on the account) and
  'recover' (forgot-password flow, one row per contact).
- Phone numbers are normalized to E.164 before sending. Email format
  is validated server-side.

Nothing in this module sends anything or knows about providers — see
notify.py. The routes own the send step and patch it in tests.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
RESEND_MIN_SECONDS = 60

_FMT = "%Y-%m-%dT%H:%M:%SZ"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(_FMT)


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, _FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def generate_code() -> str:
    """A random 6-digit code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str, salt: str) -> str:
    """Salted SHA-256 of a code. The salt is stored with the row."""
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def normalize_phone(raw: str) -> str | None:
    """Normalize a phone number to E.164, or None if it is not usable.

    Accepts an optional leading +, spaces, dashes, parentheses, and
    dots. A 10-digit North American number gets +1; an 11-digit number
    starting with 1 gets +. Anything else must already be E.164
    (+ followed by 8-15 digits).
    """
    cleaned = re.sub(r"[\s\-().]", "", (raw or "").strip())
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if digits.isdigit() and 8 <= len(digits) <= 15:
            return "+" + digits
        return None
    if not cleaned.isdigit():
        return None
    if len(cleaned) == 10:
        return "+1" + cleaned
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return "+" + cleaned
    return None


def is_valid_email(addr: str) -> bool:
    addr = (addr or "").strip()
    return len(addr) <= 254 and bool(_EMAIL_RE.fullmatch(addr))


def mask_email(addr: str) -> str:
    """Show only enough of an email to recognise it (logged-out pages)."""
    local, _, domain = (addr or "").partition("@")
    if not domain:
        return "***"
    shown = local[:2] if len(local) >= 2 else (local[:1] or "*")
    return f"{shown}***@{domain}"


def mask_phone(phone: str) -> str:
    """Show only the last 4 digits of a phone number."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "***"
    return f"+*** *** {digits[-4:]}"


def sent_recently(db, user_id: int, purpose: str, contact: str) -> bool:
    """True when a code was sent for this contact within the resend
    window. Callers check this BEFORE requesting fresh codes, so a
    two-code flow never replaces one side's code while the other side
    is still rate-limited (the user would hold a void code)."""
    row = db.execute(
        "SELECT created_at FROM verification_codes "
        "WHERE user_id = ? AND purpose = ? AND contact = ?",
        (user_id, purpose, contact)).fetchone()
    if not row:
        return False
    sent_at = _parse(row["created_at"])
    return bool(sent_at and (_now_dt() - sent_at).total_seconds()
                 < RESEND_MIN_SECONDS)


def request_code(db, user_id: int, purpose: str, contact: str):
    """Create (or replace) a verification code for a contact.

    Returns (code, None) on success, or (None, "rate_limited") when a
    code was sent for this contact less than RESEND_MIN_SECONDS ago.
    The returned code is the only copy of the plaintext — the caller
    sends it and drops the reference.
    """
    now = _now_dt()
    row = db.execute(
        "SELECT created_at FROM verification_codes "
        "WHERE user_id = ? AND purpose = ? AND contact = ?",
        (user_id, purpose, contact)).fetchone()
    if row:
        sent_at = _parse(row["created_at"])
        if sent_at and (now - sent_at).total_seconds() < RESEND_MIN_SECONDS:
            return None, "rate_limited"
    code = generate_code()
    salt = secrets.token_hex(8)
    db.execute(
        "INSERT INTO verification_codes "
        "(user_id, purpose, contact, code_hash, salt, created_at, "
        " expires_at, attempts, used) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0) "
        "ON CONFLICT(user_id, purpose, contact) DO UPDATE SET "
        "code_hash = excluded.code_hash, salt = excluded.salt, "
        "created_at = excluded.created_at, "
        "expires_at = excluded.expires_at, attempts = 0, used = 0",
        (user_id, purpose, contact, hash_code(code, salt), salt,
         _fmt(now), _fmt(now + timedelta(minutes=CODE_TTL_MINUTES))))
    db.commit()
    return code, None


def verify_code(db, user_id: int, purpose: str, contact: str,
                code: str) -> bool:
    """Check a code. True only on an exact match for a live code.

    A wrong guess counts toward the 5-attempt limit; an expired code
    and a code that hits the limit are voided. The caller must not say
    which of these happened — every failure looks the same.
    """
    now = _now_dt()
    row = db.execute(
        "SELECT id, code_hash, salt, expires_at, attempts, used "
        "FROM verification_codes "
        "WHERE user_id = ? AND purpose = ? AND contact = ?",
        (user_id, purpose, contact)).fetchone()
    if not row or row["used"]:
        return False
    expires_at = _parse(row["expires_at"])
    if expires_at is None or now > expires_at:
        _void(db, row["id"])
        return False
    if row["attempts"] >= CODE_MAX_ATTEMPTS:
        _void(db, row["id"])
        return False
    if hmac.compare_digest(
            hash_code((code or "").strip(), row["salt"]), row["code_hash"]):
        db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?",
                   (row["id"],))
        db.commit()
        return True
    attempts = row["attempts"] + 1
    db.execute(
        "UPDATE verification_codes SET attempts = ?, used = ? WHERE id = ?",
        (attempts, 1 if attempts >= CODE_MAX_ATTEMPTS else 0, row["id"]))
    db.commit()
    return False


def void_code(db, user_id: int, purpose: str, contact: str) -> None:
    """Void a code (e.g. when its send failed and it must never verify)."""
    db.execute(
        "UPDATE verification_codes SET used = 1 "
        "WHERE user_id = ? AND purpose = ? AND contact = ?",
        (user_id, purpose, contact))
    db.commit()


def _void(db, row_id: int) -> None:
    db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?",
               (row_id,))
    db.commit()
