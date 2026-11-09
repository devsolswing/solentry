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
    buy needs one). It never builds a transfer, a withdrawal, or any
    other instruction, and it never sends to any destination other
    than the trade's own program/token accounts. That is enforced by
    construction: the only instruction builders used are in
    pumpfun.py, and the only program IDs that can appear are the
    pump.fun program and the Token / Associated Token programs.

The user's app wallet key is decrypted transiently with
ENCRYPTION_KEY, used for one signature, and the reference is
dropped immediately. Key material is never logged, stored, or
returned anywhere. Every signed transaction is logged (signature,
bot, coin, amount, rule) by the caller.

Anything unverified refuses plainly with {"status": "skipped",
"reason": ...} and no fill: no bonding curve for the mint on
devnet, a migrated (complete) curve, a missing wallet, a failed
send, or an unconfirmed transaction all skip — never a fake fill.
"""

from __future__ import annotations

import sqlite3

from . import pumpfun
from .config import Config
from .security import decrypt_secret
from .signing_bot import ensure_signing_bot
from .solana import (SolanaRpcClient, base58_encode,
                     ed25519_publickey_from_seed, is_valid_address,
                     build_message, sign_message, sol_to_lamports)

# Lamports the wallet must keep after a buy so the transaction fee
# (and any later fee) is always covered.
FEE_RESERVE_LAMPORTS = 1_000_000

# Every RPC write here goes through one of these program IDs only.
# The System program is deliberately NOT in this set: no instruction
# the executor builds ever uses it as a program (it only appears as
# an account inside pump.fun instructions), so a plain SOL transfer
# — which IS a System-program instruction — can never be signed.
ALLOWED_PROGRAM_IDS = frozenset([
    pumpfun.PUMP_PROGRAM,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
])


def _skip(reason: str) -> dict:
    return {"status": "skipped", "reason": reason}


def _load_wallet(db, user_id: int, cfg: Config):
    """Return (pubkey, seed_bytes) for the user's app wallet, or
    (None, None) when there is no wallet. The seed is returned only
    so the caller can sign one transaction and then drop it."""
    row = db.execute(
        "SELECT pubkey, encrypted_secret FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()
    if not row:
        return None, None
    try:
        secret = decrypt_secret(row["encrypted_secret"],
                                cfg.encryption_key)
    except ValueError:
        return None, None
    if len(secret) != 64:
        return None, None
    seed = secret[:32]
    if base58_encode(ed25519_publickey_from_seed(seed)) != row["pubkey"]:
        return None, None
    return row["pubkey"], seed


def _check_instructions_only_allowed(instructions: list) -> None:
    """Destination restriction, enforced before anything is signed:
    the only programs that may appear are the pump.fun program and
    the Token / Associated Token programs. A plain SOL transfer (a
    System-program instruction) or any other program is a refusal,
    not a signature."""
    for ins in instructions:
        if ins["program_id"] not in ALLOWED_PROGRAM_IDS:
            raise ValueError(
                "refused: instruction for an unexpected program")


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
        tokens = pumpfun.buy_quote(curve, fee_bps, amount_lamports)
        if tokens <= 0:
            return _skip("amount too small to buy")
        max_sol_cost = (amount_lamports
                        * (10000 + pumpfun.DEFAULT_SLIPPAGE_BPS) // 10000
                        + 1)

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
        _check_instructions_only_allowed(instructions)
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
                "signing_bot": signing_bot["support_id"]}
    finally:
        # Drop the key reference the moment signing is done.
        seed = b"\x00" * 32


def _execute_sell(bot, user_id: int, mint: str, token_amount: float,
                  db, cfg: Config, rpc, signing_bot: dict) -> dict:
    wallet_pubkey, seed = _load_wallet(db, user_id, cfg)
    if wallet_pubkey is None:
        return _skip("no app wallet")
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
        instructions = [pumpfun.build_sell_instruction(
            accts, token_base, min_sol_output)]
        _check_instructions_only_allowed(instructions)
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
                "signing_bot": signing_bot["support_id"]}
    finally:
        seed = b"\x00" * 32


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
    "executed", "signature", ...} or {"status": "skipped",
    "reason": ...}. Never raises for a refused trade.
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
