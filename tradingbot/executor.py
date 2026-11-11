"""Real (devnet) trade executor: pump.fun buys/sells signed by the
user's dedicated signing bot.

The bot signs trades itself — no per-trade signing prompt — but only
within the rules that already govern it:

  * NETWORK must be devnet. Anything else (mainnet included) is
    refused loudly and nothing is signed.
  * The bot must be in real mode, and the trade must have been
    allowed by the rules engine (the silent loop enforces stop loss,
    max open trades, and the rules themselves before it ever calls
    here). This executor re-checks the amount-level limits it can
    verify: buy size % and balance, plus a small fee reserve.
  * The executor builds ONLY pump.fun buy/sell instructions (plus a
    create-ATA instruction for the user's own token account when the
    buy needs one) — and, since the 3.65% trading fee (fees.py), ONE
    extra instruction per trade: a System-program transfer of
    exactly the computed fee to the fee recipient
    (FEE_RECIPIENT_PUBKEY), in the SAME transaction as the trade.
    That is the ONLY transfer the executor can ever sign:
    _check_instructions_only_allowed refuses any System-program
    instruction that is not exactly one fee transfer — one
    instruction, to the fee recipient, for exactly the computed
    fee lamports, from the user's own wallet. Any other transfer,
    destination, or amount is still refused. The fee is taken
    from the trade amount (buys) or the sale proceeds (sells), so
    the total SOL spent is what the user approved; a trade that
    fails never pays a fee, and a fee is never charged twice for
    one trade.

The user's app wallet key is decrypted transiently with
ENCRYPTION_KEY into a mutable bytearray, used for one signature,
and wiped IN PLACE (overwritten with zeros) in a finally block the
moment the trade is done — success, refusal, or failure. The full
64-byte secret is scrubbed inside _load_wallet before it returns;
only the 32-byte seed leaves it, and the caller scrubs that too.
Key material is never logged, stored, or returned anywhere. Every
signed transaction is logged (signature, bot, coin, amount, rule)
by the caller.

Anything unverified refuses plainly with {"status": "skipped",
"reason": ...} and no fill: no bonding curve for the mint on
devnet, a migrated (complete) curve, a missing wallet, a failed
send, or an unconfirmed transaction all skip — never a fake fill.
"""

from __future__ import annotations

from . import pumpfun
from .config import Config
from .fees import fee_lamports as compute_fee_lamports
from .fees import get_fee_recipient_pubkey
from .security import decrypt_secret_mutable, wipe_secret
from .signing_bot import ensure_signing_bot
from .solana import (SYSTEM_PROGRAM, SolanaRpcClient, base58_encode,
                     build_message, build_transfer_instruction,
                     ed25519_publickey_from_seed, is_valid_address,
                     lamports_to_sol, sign_message, sol_to_lamports)

# Lamports the wallet must keep after a buy so the transaction fee
# (and any later fee) is always covered.
FEE_RESERVE_LAMPORTS = 1_000_000

# Every RPC write here goes through one of these program IDs only.
# The System program is deliberately NOT in this set: no instruction
# the executor builds ever uses it as a program (it only appears as
# an account inside pump.fun instructions) — EXCEPT the single
# 3.65% fee transfer to the fee recipient, which
# _check_instructions_only_allowed validates separately (see
# fees.py). A plain SOL transfer to any other destination can never
# be signed.
ALLOWED_PROGRAM_IDS = frozenset([
    pumpfun.PUMP_PROGRAM,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
])


def _is_fee_transfer(instruction: dict, payer_b58: str,
                     recipient_b58: str, fee_lamports: int) -> bool:
    """True only when this instruction is exactly the 3.65% fee
    transfer: a System-program transfer, from the user's own
    wallet, to the fee recipient, for exactly the computed fee
    lamports. Anything else — any other destination, amount, or
    account layout — is not a fee transfer."""
    if instruction.get("program_id") != SYSTEM_PROGRAM:
        return False
    accounts = instruction.get("accounts") or []
    if len(accounts) != 2:
        return False
    payer, destination = accounts
    if (payer.get("pubkey") != payer_b58
            or not payer.get("is_signer")
            or not payer.get("is_writable")):
        return False
    if (destination.get("pubkey") != recipient_b58
            or destination.get("is_signer")
            or not destination.get("is_writable")):
        return False
    expected = bytes([2, 0, 0, 0]) + fee_lamports.to_bytes(8, "little")
    return instruction.get("data") == expected


def _fee_transfer_context(payer_b58: str, recipient_b58: str,
                          fee_lamports: int) -> dict:
    """The validated fee-transfer expectation handed to the
    instruction allow-list check."""
    return {"payer": payer_b58, "recipient": recipient_b58,
            "lamports": fee_lamports}


def _check_instructions_only_allowed(instructions: list,
                                     fee_transfer: dict | None = None):
    """Every instruction's program must be in ALLOWED_PROGRAM_IDS —
    with exactly one narrow exception: when fee_transfer is given,
    exactly ONE System-program instruction is allowed, and only
    when it is the 3.65% fee transfer to the fee recipient for
    exactly the computed fee (see _is_fee_transfer). Any other
    System-program instruction, any second fee transfer, or any
    fee transfer to a different destination or amount raises.
    This is the final gate before a transaction is signed."""
    fee_seen = False
    for ins in instructions:
        pid = ins.get("program_id")
        if pid in ALLOWED_PROGRAM_IDS:
            continue
        if (fee_transfer is not None and not fee_seen
                and _is_fee_transfer(ins, fee_transfer["payer"],
                                     fee_transfer["recipient"],
                                     fee_transfer["lamports"])):
            fee_seen = True
            continue
        raise ValueError("refused: instruction for an unexpected program")


def _skip(reason: str) -> dict:
    return {"status": "skipped", "reason": reason}


def _load_wallet(db, user_id: int, cfg: Config):
    """Return (pubkey, seed_bytearray) for the user's app wallet, or
    (None, None) when there is no wallet. The seed is returned only
    so the caller can sign one transaction and then wipe it.

    Key handling: the full 64-byte decrypted secret is held in a
    mutable bytearray and wiped IN PLACE (overwritten with zeros) in
    a finally block before this function returns — it never leaves
    this function alive, on success or on failure. Only the 32-byte
    seed leaves, as its own bytearray, and the caller must wipe it
    in its own finally block."""
    row = db.execute(
        "SELECT pubkey, encrypted_secret FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()
    if not row:
        return None, None
    secret = None
    try:
        secret = decrypt_secret_mutable(row["encrypted_secret"],
                                        cfg.encryption_key)
        if len(secret) != 64:
            return None, None
        seed = bytearray(secret[:32])
        if base58_encode(ed25519_publickey_from_seed(seed)) != row["pubkey"]:
            wipe_secret(seed)
            return None, None
        return row["pubkey"], seed
    except ValueError:
        return None, None
    finally:
        # The full secret is scrubbed here, on every path — the
        # caller only ever holds the 32-byte seed.
        if secret is not None:
            wipe_secret(secret)


def _send_and_confirm(rpc, signed_b64: str) -> str | None:
    """Send and wait for confirmation. Returns the signature, or
    None when sending or confirmation failed."""
    try:
        signature = rpc.send_transaction(signed_b64)
        if not signature:
            return None
        if rpc.confirm_transaction(signature):
            return signature
        return None
    except Exception:
        return None


def _execute_buy(bot, user_id: int, mint: str, amount_sol: float,
                 db, cfg: Config, rpc, signing_bot: dict) -> dict:
    wallet_pubkey, seed = _load_wallet(db, user_id, cfg)
    if wallet_pubkey is None:
        return _skip("no app wallet")
    # The fee recipient pubkey is held in this mutable buffer while
    # the trade is built and sent, and wiped in place in the
    # finally block below — the moment the trade is done. It is
    # never logged.
    recipient_buf = None
    try:
        try:
            global_info = rpc.get_account_info(pumpfun.global_pda()[0])
        except Exception:
            return _skip("pump.fun global account unavailable")
        if not global_info:
            return _skip("pump.fun global account unavailable")
        try:
            glob = pumpfun.parse_global(global_info["data"])
        except ValueError:
            return _skip("pump.fun global account unreadable")
        fee_bps = int(glob["fee_basis_points"])
        try:
            curve_info = rpc.get_account_info(
                pumpfun.bonding_curve_pda(mint)[0])
        except Exception:
            return _skip("bonding curve unavailable")
        if not curve_info:
            return _skip("no bonding curve for this token on devnet")
        try:
            curve = pumpfun.parse_bonding_curve(curve_info["data"])
        except ValueError:
            return _skip("bonding curve unreadable")
        if curve["complete"]:
            return _skip("bonding curve complete (token migrated)")

        amount_lamports = sol_to_lamports(amount_sol)
        try:
            balance = rpc.get_balance_lamports(wallet_pubkey)
        except Exception:
            return _skip("real balance unavailable")
        allowed = balance * float(bot["buy_size_pct"]) / 100.0
        if amount_lamports > allowed * 1.001 + 1:
            return _skip("amount exceeds bot buy size")
        if balance - amount_lamports < FEE_RESERVE_LAMPORTS:
            return _skip("insufficient SOL left for fees")
        # The 3.65% trading fee comes out of the trade amount: the
        # user still spends amount_sol in total, but only
        # amount_sol minus the fee is swapped for tokens, and the
        # fee goes to the fee recipient in the same transaction
        # as the buy.
        fee_lamports = compute_fee_lamports(amount_lamports)
        trade_lamports = amount_lamports - fee_lamports
        tokens = pumpfun.buy_quote(curve, fee_bps, trade_lamports)
        if tokens <= 0:
            return _skip("amount too small to buy")
        max_sol_cost = (trade_lamports
                        * (10000 + pumpfun.DEFAULT_SLIPPAGE_BPS) // 10000
                        + 1)
        # The fee recipient comes from FEE_RECIPIENT_PUBKEY
        # (fees.py) — fail closed when it is not configured: the
        # trade is refused before anything is signed or sent,
        # never a fallback wallet.
        recipient_pubkey = None
        if fee_lamports > 0:
            try:
                recipient_pubkey = get_fee_recipient_pubkey(cfg)
            except ValueError:
                return _skip("fee recipient not configured "
                             "(FEE_RECIPIENT_PUBKEY)")
            recipient_buf = bytearray(recipient_pubkey.encode("ascii"))
            recipient_pubkey = bytes(recipient_buf).decode("ascii")

        accts = pumpfun.derive_trade_accounts(
            mint, wallet_pubkey, glob["fee_recipient"])
        instructions = []
        ata_before = 0
        try:
            ata_info = rpc.get_account_info(accts["associated_user"])
        except Exception:
            ata_info = None
        if ata_info is None:
            instructions.append(pumpfun.build_create_ata_instruction(
                wallet_pubkey, accts["associated_user"], wallet_pubkey,
                mint))
        else:
            try:
                ata_before = rpc.get_token_account_balance(
                    accts["associated_user"])
            except Exception:
                ata_before = 0
        instructions.append(pumpfun.build_buy_instruction(
            accts, tokens, max_sol_cost))
        fee_transfer = None
        if fee_lamports > 0:
            instructions.append(build_transfer_instruction(
                wallet_pubkey, recipient_pubkey, fee_lamports))
            fee_transfer = _fee_transfer_context(
                wallet_pubkey, recipient_pubkey, fee_lamports)
        _check_instructions_only_allowed(instructions, fee_transfer)
        try:
            blockhash = rpc.get_latest_blockhash()
        except Exception:
            return _skip("no blockhash")
        message = build_message(wallet_pubkey, instructions, blockhash)
        signed_b64, signature = sign_message(seed, message)
        confirmed = _send_and_confirm(rpc, signed_b64)
        if confirmed is None:
            return _skip("transaction not confirmed")
        coin_amount = tokens / pumpfun.TOKEN_BASE
        try:
            ata_after = rpc.get_token_account_balance(
                accts["associated_user"])
            coin_amount = (ata_after - ata_before) / pumpfun.TOKEN_BASE
        except Exception:
            pass
        return {"status": "executed", "signature": confirmed,
                "coin_amount": coin_amount,
                "fee_lamports": fee_lamports,
                "fee_sol": lamports_to_sol(fee_lamports),
                "signing_bot": signing_bot["support_id"]}
    finally:
        # Wipe the seed IN PLACE the moment the trade is done —
        # success, refusal, or failure. Overwriting the buffer is
        # what actually scrubs memory; reassigning the variable
        # would only drop the reference and leave the bytes sitting.
        # The fee recipient buffer is wiped the same way.
        if seed is not None:
            wipe_secret(seed)
        if recipient_buf is not None:
            wipe_secret(recipient_buf)


def _execute_sell(bot, user_id: int, mint: str, token_amount: float,
                  db, cfg: Config, rpc, signing_bot: dict) -> dict:
    wallet_pubkey, seed = _load_wallet(db, user_id, cfg)
    if wallet_pubkey is None:
        return _skip("no app wallet")
    # The fee recipient pubkey is held in this mutable buffer while
    # the trade is built and sent, and wiped in place in the
    # finally block below — the moment the trade is done. It is
    # never logged.
    recipient_buf = None
    try:
        try:
            global_info = rpc.get_account_info(pumpfun.global_pda()[0])
        except Exception:
            return _skip("pump.fun global account unavailable")
        if not global_info:
            return _skip("pump.fun global account unavailable")
        try:
            glob = pumpfun.parse_global(global_info["data"])
        except ValueError:
            return _skip("pump.fun global account unreadable")
        fee_bps = int(glob["fee_basis_points"])
        try:
            curve_info = rpc.get_account_info(
                pumpfun.bonding_curve_pda(mint)[0])
        except Exception:
            return _skip("bonding curve unavailable")
        if not curve_info:
            return _skip("no bonding curve for this token on devnet")
        try:
            curve = pumpfun.parse_bonding_curve(curve_info["data"])
        except ValueError:
            return _skip("bonding curve unreadable")
        if curve["complete"]:
            return _skip("bonding curve complete (token migrated)")

        token_base = int(round(token_amount * pumpfun.TOKEN_BASE))
        if token_base <= 0:
            return _skip("amount too small to sell")
        accts = pumpfun.derive_trade_accounts(
            mint, wallet_pubkey, glob["fee_recipient"])
        try:
            held = rpc.get_token_account_balance(accts["associated_user"])
        except Exception:
            return _skip("token balance unavailable")
        if token_base > held:
            return _skip("insufficient token balance")
        expected_sol = pumpfun.sell_quote(curve, fee_bps, token_base)
        if expected_sol <= 0:
            return _skip("amount too small to sell")
        min_sol_output = (expected_sol
                          * (10000 - pumpfun.DEFAULT_SLIPPAGE_BPS)
                          // 10000)
        # The 3.65% trading fee comes out of the sale proceeds: the
        # fee is 3.65% of the expected SOL out, transferred to the
        # fee recipient in the same transaction as the sell.
        # Proceeds are at least 99% of the quote even at worst
        # slippage, so the transfer is always covered.
        fee_lamports = compute_fee_lamports(expected_sol)
        # The fee recipient comes from FEE_RECIPIENT_PUBKEY
        # (fees.py) — fail closed when it is not configured: the
        # trade is refused before anything is signed or sent,
        # never a fallback wallet.
        recipient_pubkey = None
        instructions = [pumpfun.build_sell_instruction(
            accts, token_base, min_sol_output)]
        fee_transfer = None
        if fee_lamports > 0:
            try:
                recipient_pubkey = get_fee_recipient_pubkey(cfg)
            except ValueError:
                return _skip("fee recipient not configured "
                             "(FEE_RECIPIENT_PUBKEY)")
            recipient_buf = bytearray(recipient_pubkey.encode("ascii"))
            recipient_pubkey = bytes(recipient_buf).decode("ascii")
            instructions.append(build_transfer_instruction(
                wallet_pubkey, recipient_pubkey, fee_lamports))
            fee_transfer = _fee_transfer_context(
                wallet_pubkey, recipient_pubkey, fee_lamports)
        _check_instructions_only_allowed(instructions, fee_transfer)
        try:
            blockhash = rpc.get_latest_blockhash()
        except Exception:
            return _skip("no blockhash")
        message = build_message(wallet_pubkey, instructions, blockhash)
        signed_b64, signature = sign_message(seed, message)
        confirmed = _send_and_confirm(rpc, signed_b64)
        if confirmed is None:
            return _skip("transaction not confirmed")
        return {"status": "executed", "signature": confirmed,
                "fee_lamports": fee_lamports,
                "fee_sol": lamports_to_sol(fee_lamports),
                "signing_bot": signing_bot["support_id"]}
    finally:
        # Wipe the seed IN PLACE the moment the trade is done —
        # success, refusal, or failure. The fee recipient buffer
        # is wiped the same way.
        if seed is not None:
            wipe_secret(seed)
        if recipient_buf is not None:
            wipe_secret(recipient_buf)


def execute_real_trade(bot, user_id: int, coin: str, side: str,
                       amount: float, price: float, db, cfg: Config,
                       rpc_client=None) -> dict:
    """Execute one real trade for a silent bot, signed by the user's
    dedicated signing bot. Same (bot, user_id, coin, side, amount,
    price, db, cfg) protocol as the silent loop's real executor.

    `coin` must be a pump.fun token mint (a Solana address); major
    coins such as "SOL" have no swap path here and are refused with
    "real swap not available in v1". `amount` is SOL for buys and
    whole tokens for sells. `price` (USD) is accepted for the loop's
    protocol and not used for execution. Returns {"status":
    "executed", "signature", "fee_lamports", "fee_sol", ...} or
    {"status": "skipped", "reason": ...}.
    The fee is 3.65% of the trade, taken in the same transaction
    as the trade to the fee recipient (fees.py) — a trade that is
    skipped or fails never pays a fee. Never raises for a refused
    trade.
    """
    if cfg.network != "devnet":
        return _skip("real execution requires devnet")
    if bot["mode"] != "real":
        return _skip("bot is not in real mode")
    if side not in ("buy", "sell"):
        return _skip("unknown side")
    if amount is None or float(amount) <= 0:
        return _skip("amount must be positive")
    if not is_valid_address(coin):
        # Major coins and anything that is not a token mint have no
        # swap path in this app — refused plainly, no fill.
        return _skip("real swap not available in v1")
    try:
        signing_bot = ensure_signing_bot(db, user_id)
    except Exception:
        return _skip("signing bot unavailable")
    rpc = rpc_client if rpc_client is not None \
        else SolanaRpcClient(cfg.rpc_url)
    try:
        if side == "buy":
            return _execute_buy(bot, user_id, coin, float(amount),
                                db, cfg, rpc, signing_bot)
        return _execute_sell(bot, user_id, coin, float(amount),
                             db, cfg, rpc, signing_bot)
    except Exception as exc:
        # A failed signing attempt is a loud skip, never a retry
        # loop and never a fake fill.
        return _skip(f"real execution failed: {str(exc)[:80]}")
