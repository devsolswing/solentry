"""General SPL token sends: any token the app wallet holds, to any
Solana address. No per-token code — the mint, the token program
(legacy SPL Token or Token-2022), the decimals and the balances all
come from the chain at send time. Gate is only the first use.

The send transaction contains ONLY the recipient's associated-token-
account creation (when the recipient has no token account for the
mint yet) plus the token transfer. No trading fee is charged on a
send — no fee applies to trades either (the trading fee was
removed, James, 2026-11-12); the only cost is the network fee
plus the ATA rent, both paid from the app wallet's SOL. The balance is always read on-chain (getTokenAccountBalance on
the sender's ATA) — never only from the app's real_holdings table —
so a holding the app never recorded is still sendable.

Nothing here logs or stores keys. The seed is passed in transiently
by the caller and wiped by the caller, as in the SOL send flow.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .solana import (
    TOKEN_ACCOUNT_SIZE,
    TOKEN_PROGRAM,
    base58_encode,
    build_create_associated_token_account_instruction,
    build_message,
    build_token_transfer_instruction,
    ed25519_publickey_from_seed,
    find_associated_token_address,
    is_valid_address,
    sign_message,
)

# A plain SOL transfer costs 5,000 lamports on devnet; the send
# also needs the rent-exempt minimum for a new token account when
# the recipient has none. Both come from the app wallet's SOL.
TX_FEE_LAMPORTS = 5_000


class TokenSendRefusal(Exception):
    """A plain, user-facing reason a token send cannot go ahead.
    Fail-closed: a refusal means nothing was built or sent."""


def amount_to_base_units(amount_text: str, decimals: int) -> int:
    """A UI amount (e.g. "55,034,069.346715") in base units for a
    mint with `decimals` decimal places. Exact decimal math — a
    float would silently round a 6-decimal amount. Raises
    TokenSendRefusal when the amount is not a number, is not
    positive, or carries more decimal places than the mint allows
    (truncating it would send less than the user asked for)."""
    try:
        amount = Decimal(str(amount_text).strip())
    except (InvalidOperation, ValueError):
        raise TokenSendRefusal("Amount must be a number.")
    if amount <= 0:
        raise TokenSendRefusal("Amount must be more than 0.")
    scaled = amount * (10 ** decimals)
    if scaled != scaled.to_integral_value():
        raise TokenSendRefusal(
            "Amount has more decimal places than this token allows.")
    return int(scaled)


def format_token_amount(amount_base_units: int, decimals: int) -> str:
    """Base units back to a plain UI string, without trailing
    zeros — "55034069.346715", "100", "0.5"."""
    scaled = Decimal(amount_base_units) / (10 ** decimals)
    text = format(scaled.normalize(), "f")
    return text


def inspect_token(client, wallet_pubkey: str, mint: str,
                  destination: str) -> dict:
    """Read everything a token send needs from the chain, before
    anything is built or sent.

    Returns {"token_program", "payer", "mint", "destination",
    "sender_ata", "dest_ata", "create_ata" (bool), "balance" (base
    units), "decimals", "rent_lamports" (0 when no ATA is needed)}.

    Raises TokenSendRefusal with a plain reason when the send
    cannot go ahead: bad addresses, an unreadable mint, or no
    holding. A down RPC raises the same refusal ("could not check
    the chain right now") — never a crash, and never a guess.
    """
    if not is_valid_address(mint):
        raise TokenSendRefusal("That token address is not valid.")
    if not is_valid_address(destination):
        raise TokenSendRefusal("That destination address is not valid.")
    # The mint's owner IS its token program (legacy SPL Token or
    # Token-2022) — read it, never assumed.
    try:
        mint_info = client.get_account_info(mint)
    except Exception:
        raise TokenSendRefusal(
            "Could not check this token on the network right now — "
            "try again.")
    if mint_info is None:
        raise TokenSendRefusal("That token could not be found on "
                               "the network.")
    token_program = mint_info.get("owner") or TOKEN_PROGRAM
    sender_ata = find_associated_token_address(
        wallet_pubkey, mint, token_program)
    try:
        sender = client.get_token_account(sender_ata)
    except Exception:
        raise TokenSendRefusal(
            "Could not check your balance on the network right now — "
            "try again.")
    if sender is None or sender["amount"] <= 0:
        raise TokenSendRefusal("You hold none of this token.")
    dest_ata = find_associated_token_address(
        destination, mint, token_program)
    try:
        dest_info = client.get_account_info(dest_ata)
    except Exception:
        raise TokenSendRefusal(
            "Could not check the destination on the network right "
            "now — try again.")
    create_ata = dest_info is None
    rent_lamports = 0
    if create_ata:
        try:
            rent_lamports = client.get_minimum_balance_for_rent_exemption(
                TOKEN_ACCOUNT_SIZE)
        except Exception:
            raise TokenSendRefusal(
                "Could not check the network fee right now — try again.")
    return {"token_program": token_program,
            "payer": wallet_pubkey,
            "mint": mint,
            "destination": destination,
            "sender_ata": sender_ata,
            "dest_ata": dest_ata,
            "create_ata": create_ata,
            "balance": sender["amount"],
            "decimals": sender["decimals"],
            "rent_lamports": rent_lamports}


def build_token_send_instructions(inspection: dict,
                                  amount_base_units: int) -> list:
    """Exactly the instructions for the send: the recipient's ATA
    creation first (only when they have no token account for the
    mint), then the token transfer. Nothing else — no other
    transfer, no fee instruction, no extra account."""
    if amount_base_units <= 0:
        raise TokenSendRefusal("Amount must be more than 0.")
    if amount_base_units > inspection["balance"]:
        raise TokenSendRefusal(
            "Amount is more than you hold of this token.")
    instructions = []
    if inspection["create_ata"]:
        instructions.append(
            build_create_associated_token_account_instruction(
                inspection["payer"], inspection["dest_ata"],
                inspection["destination"], inspection["mint"],
                inspection["token_program"]))
    instructions.append(build_token_transfer_instruction(
        inspection["sender_ata"], inspection["dest_ata"],
        inspection["payer"], amount_base_units,
        inspection["token_program"]))
    return instructions


def sign_token_send(seed: bytes, fee_payer_b58: str, instructions: list,
                    blockhash_b58: str) -> tuple[str, str]:
    """Build + sign the token-send transaction. Returns
    (signed_tx_base64, signature_base58). Verifies the derived
    public key matches the fee payer before signing — a mismatch
    is a bug, never a signed transaction."""
    derived = base58_encode(ed25519_publickey_from_seed(seed))
    if derived != fee_payer_b58:
        raise ValueError("fee payer mismatch")
    message = build_message(fee_payer_b58, instructions, blockhash_b58)
    return sign_message(seed, message)
