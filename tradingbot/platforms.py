"""Per-platform wallets: one wallet per trading platform, per user.

James picks a platform for a bot (pump.fun, Phantom, pumpSwap) and the
app creates that platform's wallet automatically. Honest mapping:

  * pump.fun and pumpSwap have NO accounts of their own — trading on
    them uses a Solana wallet, so their wallets live in the app,
    encrypted like the app wallet. The app signs for them.
  * Phantom IS a wallet app, not a venue — a wallet cannot be created
    inside Phantom itself. The app generates a fresh wallet for the
    user to import into Phantom; the private key is shown exactly
    once, at creation, with a clear warning, and never again.

One wallet per platform per user (UNIQUE(user_id, platform)) — asking
again returns the wallet that already exists, it never makes a second
one. Keys are AES-256-GCM encrypted with ENCRYPTION_KEY, exactly like
the app wallet; key material is never logged.
"""

from __future__ import annotations

import sqlite3

from .security import decrypt_secret, encrypt_secret
from .solana import base58_encode, generate_keypair

PLATFORMS = ("pump.fun", "phantom", "pumpswap")

PLATFORM_LABELS = {
    "pump.fun": "pump.fun",
    "phantom": "Phantom",
    "pumpswap": "pumpSwap",
}

# What each platform's wallet is, in plain words for the user.
PLATFORM_NOTES = {
    "pump.fun": "pump.fun has no accounts of its own — trading on "
                "pump.fun uses this Solana wallet, which lives in the "
                "app. The app signs for it.",
    "phantom": "Phantom is a wallet app, not a trading venue — this "
               "wallet is made for you to import into Phantom. Its "
               "private key is shown once, at creation, and never "
               "again. Anyone with that key controls this wallet.",
    "pumpswap": "pumpSwap has no accounts of its own — trading on "
                "pumpSwap uses this Solana wallet, which lives in the "
                "app. The app signs for it.",
}

# Platforms the silent loop can actually trade on for real. Real
# execution is built against the pump.fun bonding curve only — a real
# bot on any other platform is skipped with a plain reason, never
# faked. Paper bots are simulated and platform-agnostic.
REAL_TRADEABLE_PLATFORMS = ("pump.fun",)


def is_valid_platform(value) -> bool:
    return isinstance(value, str) and value in PLATFORMS


def get_platform_wallets(db, user_id: int) -> dict:
    """{platform: pubkey} for the platforms this user already has."""
    rows = db.execute(
        "SELECT platform, pubkey FROM platform_wallets WHERE user_id = ?",
        (user_id,)).fetchall()
    return {r["platform"]: r["pubkey"] for r in rows}


def create_platform_wallet(db, user_id: int, platform: str,
                           encryption_key: bytes) -> tuple[str, bool]:
    """Create the user's wallet for `platform`, or return the one
    that already exists. Returns (pubkey, created_now). The secret
    is encrypted before it is stored and never returned here.

    Raises ValueError for an unknown platform.
    """
    if not is_valid_platform(platform):
        raise ValueError("unknown platform")
    existing = db.execute(
        "SELECT pubkey FROM platform_wallets WHERE user_id = ? "
        "AND platform = ?", (user_id, platform)).fetchone()
    if existing:
        return existing["pubkey"], False
    _seed, pubkey, secret64 = generate_keypair()
    blob = encrypt_secret(secret64, encryption_key)
    secret64 = b"\x00" * len(secret64)
    try:
        db.execute(
            "INSERT INTO platform_wallets (user_id, platform, pubkey, "
            "encrypted_secret) VALUES (?, ?, ?, ?)",
            (user_id, platform, pubkey, blob))
        db.commit()
    except sqlite3.IntegrityError:
        # Another request created it first — return that one.
        db.rollback()
        existing = db.execute(
            "SELECT pubkey FROM platform_wallets WHERE user_id = ? "
            "AND platform = ?", (user_id, platform)).fetchone()
        if existing:
            return existing["pubkey"], False
        raise
    return pubkey, True


def create_phantom_wallet_with_key(db, user_id: int,
                                   encryption_key: bytes) -> tuple[str, str]:
    """Create the Phantom platform wallet and return (pubkey, key).

    The key is the base58 private key the user needs to import the
    wallet into Phantom. It is returned ONLY here, at creation, so
    the caller can show it once — it is never stored in plaintext
    and never returned again. If the wallet already exists, returns
    (pubkey, "") — the key is gone, by design.
    """
    existing = db.execute(
        "SELECT pubkey FROM platform_wallets WHERE user_id = ? "
        "AND platform = 'phantom'", (user_id,)).fetchone()
    if existing:
        return existing["pubkey"], ""
    _seed, pubkey, secret64 = generate_keypair()
    blob = encrypt_secret(secret64, encryption_key)
    key_b58 = base58_encode(secret64)
    secret64 = b"\x00" * len(secret64)
    try:
        db.execute(
            "INSERT INTO platform_wallets (user_id, platform, pubkey, "
            "encrypted_secret) VALUES (?, 'phantom', ?, ?)",
            (user_id, pubkey, blob))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        existing = db.execute(
            "SELECT pubkey FROM platform_wallets WHERE user_id = ? "
            "AND platform = 'phantom'", (user_id,)).fetchone()
        if existing:
            return existing["pubkey"], ""
        raise
    return pubkey, key_b58


def decrypt_platform_secret(db, user_id: int, platform: str,
                            encryption_key: bytes) -> bytes | None:
    """Decrypt a platform wallet's secret. Used ONLY to show the
    Phantom key once at creation (via create_phantom_wallet_with_key
    instead) — kept for completeness; no route calls this to display
    a key after creation."""
    row = db.execute(
        "SELECT encrypted_secret FROM platform_wallets WHERE user_id = ? "
        "AND platform = ?", (user_id, platform)).fetchone()
    if not row:
        return None
    try:
        return decrypt_secret(row["encrypted_secret"], encryption_key)
    except ValueError:
        return None
