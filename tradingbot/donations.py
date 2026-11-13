"""Donations to the one donation recipient (FEE_RECIPIENT_PUBKEY).

All donations from the login screen go ONLY to the recipient in the
FEE_RECIPIENT_PUBKEY env var (James's own wallet, set in Railway).
There is no app-generated donation wallet any more: the generated
wallet is retired (its row is purged at startup by
db._purge_generated_fee_wallets) and must never receive anything.

The trading fee was REMOVED (James, 2026-11-12): no trade charges
a fee any more, paper or real. FEE_RECIPIENT_PUBKEY stays set for
donations, and is reserved for the future bot-rental system (the
wallet will receive rental payments when that is built — another
day, not this build). The recipient address is never shown in full
on any page and never logged.

Donations are paid by the donor's own Phantom wallet (client-side,
Phantom signs). The server only records confirmed donations: donor
address, amount, signature, network. Each record is also the admin
notification for James (see /admin/donations). No email is sent —
no mail service is configured.

Masking: the login page and the admin page show only the first 5
characters of the recipient address + '*****' — the full address
is never rendered in a template. The donor's browser still needs
the full address to build the transfer, so /api/donate/info
returns it for tx-building only, and Phantom shows the full
destination when the donor approves — the masking is the page
display, not the signed transaction (see the README).
"""

from __future__ import annotations

from .solana import (base58_decode, is_valid_address, lamports_to_sol,
                     sol_to_lamports)


def get_fee_recipient_pubkey(cfg) -> str:
    """Return the donation / future bot-rental recipient pubkey from
    the FEE_RECIPIENT_PUBKEY env var (Config.fee_recipient_pubkey).
    Moved here from the deleted fees.py when the trading fee was
    removed (James, 2026-11-12): no trade reads this var any more,
    but donations still do, and the same wallet is reserved for
    bot-rental payments later.

    Raises ValueError when the var is unset, empty, or not a valid
    base58 32-byte Solana address — the caller must fail closed
    (donations refuse), never fall back to a generated wallet or a
    guessed address. No wallet is ever created here; the recipient
    is configured, not generated."""
    raw = (cfg.fee_recipient_pubkey or "").strip()
    if not raw or not is_valid_address(raw):
        raise ValueError(
            "FEE_RECIPIENT_PUBKEY is not set or not a valid wallet "
            "address")
    return raw


def get_donation_recipient(cfg) -> str:
    """Return the donation recipient pubkey (FEE_RECIPIENT_PUBKEY).

    Raises ValueError when it is unset, empty, or invalid — the
    donate flow must refuse plainly, never fall back to a
    generated wallet or a guessed address."""
    return get_fee_recipient_pubkey(cfg)


def mask_address(address: str) -> str:
    """Masked display for a wallet address: first 5 characters +
    '*****'. The full address is never rendered on a page."""
    if not address:
        return ""
    return address[:5] + "*****"


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
