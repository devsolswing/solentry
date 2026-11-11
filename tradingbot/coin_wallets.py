"""Per-coin wallets: the first-trade wallet step on the coin page.

James's rule (2026-11-11): before the FIRST trade of a particular
coin, the user taps a "Create wallet" button for that coin's wallet
— Buy stays disabled until then. After that first trade the button
is GONE, replaced by a small connected indicator: a green dot + the
wallet address as first 4 + last 4 digits. The indicator shows the
REAL address, never an invented one.

How it is recorded (the per-coin state):
One row per (user, coin) in the `coin_wallets` table
(user_id, coin_id, wallet_pubkey, network, created_at). The row is
written the first time the user taps "Create wallet" for that coin
and never written again for that coin — so the button can never
reappear for it. The row records WHICH wallet: for Solana coins
(the Jupiter flow, pump.fun mints, SOL itself) it is the user's
existing Solana app wallet (the `wallets` row created at signup) —
the tap records that wallet for the coin, it does not generate a
new key. No new key scheme is invented here: keys still live only
in the existing `wallets` table, encrypted as before.

Which coins get a wallet:
- A coin that trades on Solana — a Solana asset (networks.py) or a
  coin Jupiter can route (a wrapped/native Solana token) — gets the
  user's Solana app wallet.
- A coin whose network has no wallet support in the app yet gets NO
  button and NO fake wallet: the page says plainly "Wallet not
  available for this network yet", and a Buy for it is refused with
  the same words. (Today that is the coins with no Jupiter route
  and no Solana presence, e.g. ADA.)
"""

from __future__ import annotations

from . import jupiter as jupiter_mod
from .networks import coin_network

WALLET_NOT_AVAILABLE = "Wallet not available for this network yet."
WALLET_REQUIRED = ("Create the wallet for this coin first — tap "
                   "'Create wallet' on the coin page.")
NO_APP_WALLET = "No app wallet found for your account."


def truncate_pubkey(pubkey: str) -> str:
    """First 4 + last 4 of a real address, e.g. "Ab12…Xy98".
    Never invents an address — it only shortens the one given.
    A too-short value is returned as-is rather than fabricated."""
    if not pubkey:
        return ""
    if len(pubkey) <= 8:
        return pubkey
    return f"{pubkey[:4]}…{pubkey[-4:]}"


def wallet_support_for_coin(coin_id: str, coin_name: str | None = None) -> dict:
    """{supported, network, reason} for a coin. Supported when the
    coin trades on Solana: either it IS a Solana asset, or Jupiter
    can route it (a wrapped/native Solana token exists). Anything
    else has no wallet in the app yet — supported False, with the
    plain reason. Never guesses a network."""
    if not coin_id:
        return {"supported": False, "network": None,
                "reason": WALLET_NOT_AVAILABLE}
    net = coin_network(coin_id)
    if net.get("is_solana"):
        return {"supported": True, "network": "Solana", "reason": None}
    upper = coin_id.upper()
    if upper == "SOL":
        return {"supported": True, "network": "Solana", "reason": None}
    resolved = jupiter_mod.resolve_mint(upper, coin_name=coin_name)
    if resolved is not None:
        return {"supported": True, "network": "Solana", "reason": None}
    return {"supported": False, "network": net.get("network"),
            "reason": WALLET_NOT_AVAILABLE}


def get_coin_wallet(db, user_id: int, coin_id: str):
    """The recorded coin_wallets row for (user, coin), or None."""
    return db.execute(
        "SELECT user_id, coin_id, wallet_pubkey, network, created_at "
        "FROM coin_wallets WHERE user_id = ? AND coin_id = ?",
        (user_id, coin_id.upper())).fetchone()


def record_coin_wallet(db, user_id: int, coin_id: str,
                       wallet_pubkey: str, network: str):
    """Write the (user, coin) row. INSERT OR IGNORE: tapping
    "Create wallet" twice for the same coin keeps the first row —
    the button can never come back for that coin."""
    db.execute(
        "INSERT OR IGNORE INTO coin_wallets "
        "(user_id, coin_id, wallet_pubkey, network) "
        "VALUES (?, ?, ?, ?)",
        (user_id, coin_id.upper(), wallet_pubkey, network))
    db.commit()
    return get_coin_wallet(db, user_id, coin_id)


def coin_wallet_state(db, user_id: int, coin_id: str,
                      app_wallet_pubkey: str | None,
                      coin_name: str | None = None) -> dict:
    """Everything the coin page needs for the wallet step:
    {supported, recorded, wallet_pubkey, truncated, network,
    reason}. `recorded` is true only when a row exists for this
    coin — the button shows exactly while it is false. SOL is the
    exception: it is the native coin of the app wallet itself, and
    that wallet already exists from signup, so the "Create wallet"
    step is already satisfied for SOL — the indicator shows the
    app wallet immediately, no tap needed."""
    support = wallet_support_for_coin(coin_id, coin_name=coin_name)
    row = get_coin_wallet(db, user_id, coin_id)
    pubkey = row["wallet_pubkey"] if row else None
    if (coin_id.upper() == "SOL" and row is None
            and app_wallet_pubkey):
        pubkey = app_wallet_pubkey
        row = {"network": "Solana"}
    return {
        "supported": support["supported"],
        "recorded": row is not None,
        "wallet_pubkey": pubkey,
        "truncated": truncate_pubkey(pubkey) if pubkey else "",
        "network": row["network"] if row else support["network"],
        "reason": support["reason"],
    }


def create_coin_wallet(db, user_id: int, coin_id: str,
                       app_wallet_pubkey: str | None,
                       coin_name: str | None = None) -> dict:
    """The "Create wallet" tap, server-side. Returns
    {ok, coin_id, wallet_pubkey, truncated, network, created} on
    success, or {ok: False, error} — the wallet is the user's
    existing Solana app wallet, recorded for this coin. Idempotent:
    a second tap returns the same row. A coin with no supported
    wallet refuses plainly instead of faking one."""
    support = wallet_support_for_coin(coin_id, coin_name=coin_name)
    if not support["supported"]:
        return {"ok": False, "error": WALLET_NOT_AVAILABLE}
    if not app_wallet_pubkey:
        return {"ok": False, "error": NO_APP_WALLET}
    existing = get_coin_wallet(db, user_id, coin_id)
    if existing is not None:
        return {"ok": True, "coin_id": coin_id.upper(),
                "wallet_pubkey": existing["wallet_pubkey"],
                "truncated": truncate_pubkey(existing["wallet_pubkey"]),
                "network": existing["network"], "created": False}
    row = record_coin_wallet(db, user_id, coin_id, app_wallet_pubkey,
                             support["network"])
    return {"ok": True, "coin_id": coin_id.upper(),
            "wallet_pubkey": row["wallet_pubkey"],
            "truncated": truncate_pubkey(row["wallet_pubkey"]),
            "network": row["network"], "created": True}
