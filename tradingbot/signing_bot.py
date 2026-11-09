"""Per-account dedicated signing bot + immutable 8-digit support ID.

Every account has exactly ONE signing bot: it is the bot that signs
that account's real trades, and its support ID is the number the user
gives tech support. The ID is:

  * exactly 8 digits, first digit never 0 (10000000–99999999),
  * cryptographically random (secrets module),
  * unique across all accounts (UNIQUE constraint + retry),
  * IMMUTABLE: there is no route, form, or code path anywhere in
    this app that can change it. The only writes to this table are
    INSERTs at signup / backfill. Any attempt to set a support ID
    through a request is ignored because no handler reads one.

The signing bot holds no key itself — the app wallet key stays
encrypted in the wallets table, and the executor decrypts it
transiently only to sign a trade the rules engine allowed.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

_SUPPORT_ID_RE = re.compile(r"^[1-9][0-9]{7}$")
_MAX_ATTEMPTS = 12


def generate_support_id() -> str:
    """One random 8-digit ID, first digit 1-9."""
    return f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"


def is_valid_support_id(value) -> bool:
    return isinstance(value, str) and bool(_SUPPORT_ID_RE.match(value))


def get_signing_bot(db, user_id: int) -> dict | None:
    row = db.execute(
        "SELECT user_id, support_id, created_at FROM signing_bots "
        "WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def ensure_signing_bot(db, user_id: int) -> dict:
    """Return the account's signing bot, creating it (with a fresh
    support ID) when it does not exist yet. Idempotent: calling it
    twice returns the same row and the same ID. A collision on the
    UNIQUE support_id retries with a new random ID."""
    existing = get_signing_bot(db, user_id)
    if existing is not None:
        return existing
    for _ in range(_MAX_ATTEMPTS):
        support_id = generate_support_id()
        try:
            db.execute(
                "INSERT INTO signing_bots (user_id, support_id, "
                "created_at) VALUES (?, ?, datetime('now'))",
                (user_id, support_id))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            existing = get_signing_bot(db, user_id)
            if existing is not None:
                return existing
            continue
        created = get_signing_bot(db, user_id)
        if created is not None:
            return created
    raise RuntimeError("could not allocate a support ID")
