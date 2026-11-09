"""WebAuthn ceremony challenges: server-issued, single-use, short-lived.

A challenge is created when a ceremony begins (passkey registration
or passkey login), stored in the database, and taken exactly once at
verification time. Taking marks it used, so replaying it always
fails. Challenges expire after 5 minutes.

The challenge value itself is never logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers import generate_challenge

CHALLENGE_TTL_MINUTES = 5

_FMT = "%Y-%m-%dT%H:%M:%SZ"


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


def issue_challenge(db, user_id: int, purpose: str) -> str:
    """Create a fresh challenge for a ceremony. Returns base64url text."""
    now = _now_dt()
    challenge = bytes_to_base64url(generate_challenge())
    db.execute(
        "INSERT INTO webauthn_challenges "
        "(user_id, purpose, challenge, expires_at, used, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (user_id, purpose, challenge,
         _fmt(now + timedelta(minutes=CHALLENGE_TTL_MINUTES)), _fmt(now)))
    db.commit()
    return challenge


def take_challenge(db, user_id: int, purpose: str) -> bytes | None:
    """Take the newest live challenge for a ceremony (single-use).

    Returns the challenge bytes, or None when there is no live one.
    The row is marked used whether verification later succeeds or
    not — a challenge is never presented twice.
    """
    now = _now_dt()
    row = db.execute(
        "SELECT id, challenge, expires_at FROM webauthn_challenges "
        "WHERE user_id = ? AND purpose = ? AND used = 0 "
        "ORDER BY id DESC LIMIT 1",
        (user_id, purpose)).fetchone()
    if not row:
        return None
    expires_at = _parse(row["expires_at"])
    db.execute("UPDATE webauthn_challenges SET used = 1 WHERE id = ?",
               (row["id"],))
    db.commit()
    if expires_at is None or now > expires_at:
        return None
    return base64url_to_bytes(row["challenge"])
