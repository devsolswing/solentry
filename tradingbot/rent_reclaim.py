"""SOL rent reclaim: close empty token accounts and burn zero-value
holdings, returning the locked rent to the user's wallet.

Rules (James, 2026-11-24):
- Empty token accounts (zero balance) are closed; the rent-exempt
  lamports go straight to the user's wallet (the close destination)
  — instantly, no delay, no manual claim step.
- A holding worth $0.00 is burned first — its tokens are sent to
  the incinerator (1nc1nerator...), then its account is closed.
- Nothing executes silently: the user sees exactly what will be
  burned + closed and the total SOL reclaimed, and confirms first.
- Only zero-value holdings and only zero-balance accounts. A
  holding the price feed cannot price is NEVER burned
  (fail-closed). The token program also refuses to close an
  account holding tokens — the code checks first and the program
  checks again.
- NOT a trade: reclaim never costs fuel and the fuel gate never
  applies to it. Paper mode shows the list as a read-only preview;
  the action runs on the real wallet only.

Nothing here logs or stores keys. The seed is passed in transiently
by the caller and wiped by the caller, as in the SOL / token send
flows.
"""

from __future__ import annotations

from .solana import (
    TOKEN_2022_PROGRAM,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_PROGRAM,
    base58_encode,
    build_close_token_account_instruction,
    build_create_associated_token_account_instruction,
    build_message,
    build_token_transfer_instruction,
    ed25519_publickey_from_seed,
    find_associated_token_address,
    lamports_to_sol,
    sign_message,
)
from .token_send import format_token_amount

# The burn address: nobody holds its key, so tokens sent to its
# token account are gone for good. The app burned the old trading
# fee to this same address (2026-11-13).
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# A holding "worth $0.00" is one whose USD value displays as $0.00
# — under half a cent. The preview shows the exact value, so the
# user confirms with the real number in front of them.
ZERO_VALUE_USD = 0.005

# A legacy transaction holds at most ~8 accounts' worth of
# burn+close instructions comfortably (each account adds its ATA,
# mint and (for burns) the incinerator ATA to the account keys).
# More than that is split into a second transaction — each one is
# confirmed before the next is sent, so a failure stops the run
# instead of leaving half the list done and unreported.
MAX_ACCOUNTS_PER_TX = 8

# A plain transaction costs 5,000 lamports (no priority fee) — the
# same base fee the token send uses. The preview subtracts it per
# transaction so the reclaimed total is honest.
TX_FEE_LAMPORTS = 5_000


class ReclaimRefusal(Exception):
    """A plain, user-facing reason reclaim cannot go ahead.
    Fail-closed: a refusal means nothing was built or sent."""


def list_token_accounts(client, wallet_pubkey: str) -> list:
    """Every token account the wallet owns, on-chain — both token
    programs, parsed. Each entry is {"account", "mint", "amount"
    (base units), "decimals", "lamports", "token_program"}.
    Zero-balance accounts are INCLUDED (they are the reclaim):
    unlike the holdings read, nothing is skipped here. An account
    is listed once, under the program that owns it. Raises
    ReclaimRefusal when the chain cannot be read at all — a
    partial read must never present an incomplete list as the
    whole wallet."""
    if client is None or not wallet_pubkey:
        raise ReclaimRefusal(
            "Could not check your wallet on the network right now — "
            "try again.")
    out = []
    failed = 0
    for program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            result = client._call(
                "getTokenAccountsByOwner",
                [wallet_pubkey, {"programId": program_id},
                 {"encoding": "jsonParsed"}])
        except Exception:
            failed += 1
            continue
        for acct in (result or {}).get("value") or []:
            try:
                account = acct.get("pubkey")
                data = acct.get("account", {}) or {}
                info = (data.get("data", {}).get("parsed", {})
                        .get("info", {}))
                mint = info.get("mint")
                token_amount = info.get("tokenAmount") or {}
                amount = int(token_amount.get("amount") or 0)
                decimals = int(token_amount.get("decimals") or 0)
                lamports = int(data.get("lamports") or 0)
                if not account or not mint:
                    continue
                out.append({"account": account, "mint": mint,
                            "amount": amount, "decimals": decimals,
                            "lamports": lamports,
                            "token_program": program_id})
            except (TypeError, ValueError, AttributeError):
                continue
    if failed == 2:
        raise ReclaimRefusal(
            "Could not check your wallet on the network right now — "
            "try again.")
    return out


def plan_reclaim(client, wallet_pubkey: str, price_for_mint) -> dict:
    """What reclaiming would do, from a fresh chain read — the
    preview the user confirms. `price_for_mint(mint)` returns the
    live USD price or None when the mint cannot be priced (the
    same price the holdings page shows, so the preview matches
    what the user sees).

    Returns {"empty": [...], "burn": [...], "skipped_unpriced":
    [...], "items": [...], "total_rent_lamports",
    "total_rent_sol", "ata_creation_lamports",
    "estimated_fee_lamports", "net_lamports", "net_sol",
    "tx_count"}. An item is {"kind": "close"|"burn", "account",
    "mint", "amount", "decimals", "ui_amount", "display_amount",
    "value_usd", "price_usd", "rent_lamports", "token_program",
    "incinerator_ata", "create_incinerator_ata",
    "incinerator_ata_rent"}.

    - amount == 0 → close: the rent comes back, nothing is burned.
    - amount > 0 and value < $0.005 → burn: the full balance goes
      to the incinerator, then the account closes. The incinerator's
      token account is created first when it does not exist yet —
      its rent is subtracted from the total, so the net is honest:
      the first burn of a mint nets ~0 (rent out ≈ rent back) and
      later burns of the same mint net the full rent.
    - amount > 0 and worth anything → not listed at all.
    - amount > 0 and unpriced → skipped_unpriced: never burned
      without a price, and shown separately so the user knows the
      list is incomplete rather than clean.
    """
    accounts = list_token_accounts(client, wallet_pubkey)
    empty: list = []
    burn: list = []
    skipped_unpriced: list = []
    for acct in accounts:
        amount = acct["amount"]
        if amount == 0:
            empty.append({
                "kind": "close", "account": acct["account"],
                "mint": acct["mint"], "amount": 0,
                "decimals": acct["decimals"], "ui_amount": 0.0,
                "display_amount": "0", "value_usd": 0.0,
                "price_usd": None,
                "rent_lamports": acct["lamports"],
                "token_program": acct["token_program"],
                "incinerator_ata": None,
                "create_incinerator_ata": False,
                "incinerator_ata_rent": 0})
            continue
        try:
            price = price_for_mint(acct["mint"])
        except Exception:
            price = None
        if price is None:
            skipped_unpriced.append({
                "account": acct["account"], "mint": acct["mint"],
                "amount": amount, "decimals": acct["decimals"]})
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            skipped_unpriced.append({
                "account": acct["account"], "mint": acct["mint"],
                "amount": amount, "decimals": acct["decimals"]})
            continue
        if price <= 0:
            # A price of 0 or less is not a price — never burn on it.
            skipped_unpriced.append({
                "account": acct["account"], "mint": acct["mint"],
                "amount": amount, "decimals": acct["decimals"]})
            continue
        ui_amount = amount / (10 ** acct["decimals"]) \
            if acct["decimals"] else float(amount)
        value_usd = ui_amount * price
        if value_usd >= ZERO_VALUE_USD:
            continue
        incinerator_ata = find_associated_token_address(
            INCINERATOR, acct["mint"], acct["token_program"])
        try:
            dest_info = client.get_account_info(incinerator_ata)
        except Exception:
            raise ReclaimRefusal(
                "Could not check the network right now — try again.")
        create_ata = dest_info is None
        ata_rent = 0
        if create_ata:
            try:
                ata_rent = client.get_minimum_balance_for_rent_exemption(
                    TOKEN_ACCOUNT_SIZE)
            except Exception:
                raise ReclaimRefusal(
                    "Could not check the network fee right now — "
                    "try again.")
        burn.append({
            "kind": "burn", "account": acct["account"],
            "mint": acct["mint"], "amount": amount,
            "decimals": acct["decimals"], "ui_amount": ui_amount,
            "display_amount": format_token_amount(
                amount, acct["decimals"]),
            "value_usd": value_usd, "price_usd": price,
            "rent_lamports": acct["lamports"],
            "token_program": acct["token_program"],
            "incinerator_ata": incinerator_ata,
            "create_incinerator_ata": create_ata,
            "incinerator_ata_rent": ata_rent})
    items = empty + burn
    total_rent = sum(i["rent_lamports"] for i in items)
    ata_creation = sum(i["incinerator_ata_rent"] for i in burn)
    tx_count = ((len(items) + MAX_ACCOUNTS_PER_TX - 1)
                // MAX_ACCOUNTS_PER_TX) if items else 0
    fees = tx_count * TX_FEE_LAMPORTS
    net = total_rent - ata_creation - fees
    return {"empty": empty, "burn": burn,
            "skipped_unpriced": skipped_unpriced, "items": items,
            "total_rent_lamports": total_rent,
            "total_rent_sol": lamports_to_sol(total_rent),
            "ata_creation_lamports": ata_creation,
            "estimated_fee_lamports": fees,
            "net_lamports": net, "net_sol": lamports_to_sol(net),
            "tx_count": tx_count}


def build_reclaim_transactions(items: list, wallet_pubkey: str) -> list:
    """The transactions for the confirmed items, chunked so each
    stays small: a list of instruction lists, one per transaction.
    Per account, in order: create the incinerator's token account
    (burn items only, only when it does not exist yet), transfer
    the full balance to it (burn items only), then close the
    account with the destination set to the user's wallet — the
    reclaimed rent lands in the wallet the moment the close lands.
    A close-only item is just the close. Nothing else — no fee
    instruction, no other transfer."""
    chunks = []
    for start in range(0, len(items), MAX_ACCOUNTS_PER_TX):
        instructions = []
        for item in items[start:start + MAX_ACCOUNTS_PER_TX]:
            if item["kind"] == "burn":
                if item["create_incinerator_ata"]:
                    instructions.append(
                        build_create_associated_token_account_instruction(
                            wallet_pubkey, item["incinerator_ata"],
                            INCINERATOR, item["mint"],
                            item["token_program"]))
                instructions.append(build_token_transfer_instruction(
                    item["account"], item["incinerator_ata"],
                    wallet_pubkey, item["amount"],
                    item["token_program"]))
            instructions.append(build_close_token_account_instruction(
                item["account"], wallet_pubkey, wallet_pubkey,
                item["token_program"]))
        chunks.append(instructions)
    return chunks


def sign_reclaim(seed: bytes, fee_payer_b58: str, instructions: list,
                 blockhash_b58: str) -> tuple[str, str]:
    """Build + sign one reclaim transaction. Returns
    (signed_tx_base64, signature_base58). Verifies the derived
    public key matches the fee payer before signing — a mismatch
    is a bug, never a signed transaction."""
    derived = base58_encode(ed25519_publickey_from_seed(seed))
    if derived != fee_payer_b58:
        raise ValueError("fee payer mismatch")
    message = build_message(fee_payer_b58, instructions, blockhash_b58)
    return sign_message(seed, message)
