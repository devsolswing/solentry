"""Project donation wallet + donation records.

The donation wallet is ONE wallet for the whole project: generated once
at first use, encrypted with the same AES-256-GCM mechanism as user
wallets, and stored in the DB. Its public address is the destination for
donations. It belongs to the project — it is NOT any user's personal
wallet and NEVER James's personal Phantom wallet.

Donations are paid by the donor's own Phantom wallet (client-side,
Phantom signs). The server only records confirmed donations: donor
address, amount, signature, network. Each record is also the admin
notification for James (see /admin/donations). No email is sent —
no mail service is configured.
"""

from __future__ import annotations

from .security import decrypt_secret, encrypt_secret
from .solana import (base58_decode, generate_keypair, is_valid_address,
                     lamports_to_sol, sol_to_lamports)


def get_or_create_donation_wallet(app) -> str:
    """Return the donation wallet's public address, creating it once."""
    cfg = app.config["TRADINGBOT_CONFIG"]
    db = app.config["DB"]
    row = db.execute(
        "SELECT pubkey FROM donation_wallet WHERE id = 1").fetchone()
    if row:
        return row["pubkey"]
    _seed, pubkey, secret64 = generate_keypair()
    blob = encrypt_secret(secret64, cfg.encryption_key)
    secret64 = b"\x00" * len(secret64)  # drop the reference
    db.execute(
        "INSERT INTO donation_wallet (id, pubkey, encrypted_secret) "
        "VALUES (1, ?, ?)", (pubkey, blob))
    db.commit()
    return pubkey


def donation_wallet_decrypts(app) -> bool:
    """Internal check: the stored donation secret decrypts and matches
    the stored pubkey. Used by tests; never exposes the secret."""
    cfg = app.config["TRADINGBOT_CONFIG"]
    db = app.config["DB"]
    row = db.execute(
        "SELECT pubkey, encrypted_secret FROM donation_wallet WHERE id = 1"
    ).fetchone()
    if not row:
        return False
    secret = decrypt_secret(row["encrypted_secret"], cfg.encryption_key)
    from .solana import publickey_from_secret, base58_encode
    ok = base58_encode(publickey_from_secret(secret)) == row["pubkey"]
    secret = b"\x00" * len(secret)
    return ok


def validate_donation_record(body: dict, network: str) -> tuple[str, int, str]:
    """Validate a donation record POST body.

    Returns (donor_address, amount_lamports, signature).
    Raises ValueError with a plain message on any bad input.
    """
    donor = (body.get("donor_address") or "").strip()
    signature = (body.get("signature") or "").strip()
    if not is_valid_address(donor):
        raise ValueError("donor address is not valid")
    try:
        amount_sol = float(body.get("amount_sol"))
    except (TypeError, ValueError):
        raise ValueError("amount must be a number") from None
    if amount_sol <= 0:
        raise ValueError("amount must be more than 0")
    if amount_sol > 1000:
        raise ValueError("amount is too large")
    if body.get("network") != network:
        raise ValueError("network does not match this server")
    # A Solana transaction signature is 64 bytes, base58-encoded
    # (87-88 chars). Anything else cannot be a real signature.
    if not (87 <= len(signature) <= 88):
        raise ValueError("signature does not look like a transaction signature")
    try:
        if len(base58_decode(signature)) != 64:
            raise ValueError(
                "signature does not look like a transaction signature")
    except ValueError as exc:
        if "signature" in str(exc):
            raise
        raise ValueError(
            "signature does not look like a transaction signature") from exc
    return donor, sol_to_lamports(amount_sol), signature


def record_donation(app, donor: str, amount_lamports: int, signature: str,
                    network: str) -> int:
    """Insert a confirmed donation. Returns the row id.

    Raises sqlite3.IntegrityError when the signature was already
    recorded (donations are unique per signature).
    """
    from datetime import datetime, timezone

    db = app.config["DB"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.execute(
        "INSERT INTO donations (donor_address, amount_lamports, signature, "
        "network, created_at) VALUES (?, ?, ?, ?, ?)",
        (donor, amount_lamports, signature, network, now))
    db.commit()
    return cur.lastrowid


def list_donations(app, limit: int = 50):
    """Newest donations first, with amounts in SOL for display."""
    db = app.config["DB"]
    rows = db.execute(
        "SELECT id, donor_address, amount_lamports, signature, network, "
        "created_at FROM donations ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return [{
        "id": r["id"],
        "donor_address": r["donor_address"],
        "amount_sol": lamports_to_sol(r["amount_lamports"]),
        "signature": r["signature"],
        "network": r["network"],
        "created_at": r["created_at"],
    } for r in rows]
