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
    buy needs one). There is NO trading fee any more (James,
    2026-11-12 — the 3.65% fee, its 1.7% treasury share and its
    1.95% devnet burn were removed in full): the whole trade
    amount is swapped for tokens (buys) or paid out in SOL
    (sells), and NO System-program transfer is ever built or
    signed here. _check_instructions_only_allowed refuses any
    System-program instruction outright — any transfer,
    destination, or amount is refused. The total SOL spent is
    what the user approved; a trade that fails never pays
    anything.

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

Confirmation waits up to ~30s for the chain, and a transaction
still unconfirmed then gets a final on-chain re-check before it
is reported as a failure — a slow devnet RPC must never turn a
landed transaction into a reported error. A preflight rejection
surfaces the node's own error instead of a bare "not
confirmed"; a failed trade is never retried.
"""

from __future__ import annotations

import base64
import time

from . import pumpfun
from .config import Config
from .security import decrypt_secret_mutable, wipe_secret
from .wallet_keys import decrypt_wallet_secret
from .signing_bot import ensure_signing_bot
from .solana import (RpcError, SolanaRpcClient,
                     base58_encode, build_message,
                     ed25519_publickey_from_seed, is_valid_address,
                     lamports_to_sol, sign_message, sol_to_lamports)

# Lamports the wallet must keep after a buy so the transaction fee
# is always covered.
FEE_RESERVE_LAMPORTS = 1_000_000

# Every RPC write here goes through one of these program IDs only.
# The System program is deliberately NOT in this set: no instruction
# the executor builds ever uses it as a program (it only appears as
# an account inside pump.fun instructions). A plain SOL transfer to
# any destination can never be signed — there is no trading fee any
# more (removed 2026-11-12), so no transfer is ever needed.
ALLOWED_PROGRAM_IDS = frozenset([
    pumpfun.PUMP_PROGRAM,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
])


def _check_instructions_only_allowed(instructions: list):
    """Every instruction's program must be in ALLOWED_PROGRAM_IDS.
    Any System-program instruction raises — there is no trading
    fee any more (removed 2026-11-12), so no transfer is ever
    expected or allowed. This is the final gate before a
    transaction is signed."""
    for ins in instructions:
        pid = ins.get("program_id")
        if pid in ALLOWED_PROGRAM_IDS:
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
    in its own finally block. The wallet secret is decrypted through
    the password+email user-key layer (wallet_keys) — v1 wallets
    (server key only) and v2 wallets (user key, unwrapped with the
    server key) both work here, so the signing bot keeps signing
    while the user is offline."""
    row = db.execute(
        "SELECT pubkey FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()
    if not row:
        return None, None
    secret = None
    try:
        secret = decrypt_wallet_secret(db, user_id, cfg.encryption_key)
        if secret is None or len(secret) != 64:
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


def _signature_from_signed_tx(signed_b64: str) -> str | None:
    """The transaction signature (base58) from a signed legacy
    transaction, or None when it cannot be decoded. The signature
    is the first 64 bytes after the compact-u16 signature count."""
    try:
        raw = base64.b64decode(signed_b64)
        if not raw or raw[0] != 1 or len(raw) < 65:
            return None
        return base58_encode(raw[1:65])
    except Exception:
        return None


def _tx_status(rpc, signature: str) -> str | None:
    """One status check: "confirmed", "failed", or None (unknown).
    Prefers the client's single-shot transaction_status; older
    fakes only offer confirm_transaction (a bool), which is used
    as a one-shot check instead."""
    check = getattr(rpc, "transaction_status", None)
    if check is not None:
        try:
            return check(signature)
        except Exception:
            return None
    try:
        return "confirmed" if rpc.confirm_transaction(signature) else None
    except Exception:
        return None


def _tx_receipt_confirmed(rpc, signature: str) -> bool | None:
    """Definitive check via the full transaction receipt: True
    when the chain holds the transaction and it succeeded, False
    when it holds it and it failed, None when there is no receipt
    (not landed yet, or the client cannot ask). Only used after
    the status cache stops listing the signature — its window is
    only ~300 slots, so a slow confirmation can age out of it
    while the transaction has in fact landed."""
    get_tx = getattr(rpc, "get_transaction", None)
    if get_tx is None:
        return None
    try:
        receipt = get_tx(signature)
    except Exception:
        return None
    if not receipt:
        return None
    meta = receipt.get("meta") or {}
    return meta.get("err") is None


def _recheck_confirmed(rpc, signature: str) -> bool:
    """Final on-chain re-check after the main confirmation window
    closed: wait briefly, then check the chain a few more times.
    True only when the transaction is confirmed; a "failed"
    status ends the re-check at once (the chain processed it and
    it did not succeed). A slow devnet RPC can lag behind the
    chain by seconds — this is what stops a landed transaction
    from being reported as a failure. When the status cache has
    dropped the signature entirely, the receipt itself is the
    last word: a transaction the chain holds with no error has
    landed, whatever the cache says."""
    for delay in (3.0, 2.0, 2.0):
        time.sleep(delay)
        status = _tx_status(rpc, signature)
        if status == "confirmed":
            return True
        if status == "failed":
            return False
    return _tx_receipt_confirmed(rpc, signature) is True


def _is_node_rejection(exc: Exception) -> bool:
    """True when the node itself rejected the transaction (a
    preflight/simulation error in the RPC error response). A
    transport failure — the request never reached the node, or
    its response never came back — is not a rejection: the
    transaction may have been accepted anyway."""
    return isinstance(exc, RpcError) and str(exc).startswith(
        "rpc error response")


def _rejection_reason(exc: Exception) -> str:
    """The node's own rejection, trimmed for the caller to show.
    Contains only the program/node error — never key material."""
    msg = str(exc)
    prefix = "rpc error response: sendTransaction: "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return "transaction rejected: " + msg[:120]


def _send_and_confirm(rpc, signed_b64: str,
                      signature: str | None = None) -> tuple:
    """Send and wait for confirmation. Returns (signature, None)
    when the transaction landed on-chain, else (None, reason)
    where reason explains the failure for the caller to surface.

    The signature is known before sending (the executor signs
    locally), so a send or confirmation check that fails
    transiently is never taken as proof the trade failed: the
    chain is re-checked for the known signature before giving
    up. A transaction that landed is always reported as a
    success. Only a transaction the node rejected in preflight,
    one the chain reports as failed, or one the chain never
    confirms after the window plus the final re-check is
    reported as a failure — and a failed trade is never retried."""
    if signature is None:
        signature = _signature_from_signed_tx(signed_b64)
    try:
        sent = rpc.send_transaction(signed_b64)
    except Exception as exc:
        if _is_node_rejection(exc):
            return None, _rejection_reason(exc)
        # The send call itself failed (timeout, dropped response)
        # — the network may still have accepted the transaction.
        # The signature is known, so check the chain before
        # calling it a failure.
        if signature and _recheck_confirmed(rpc, signature):
            return signature, None
        return None, "transaction not confirmed"
    if not sent:
        if signature and _recheck_confirmed(rpc, signature):
            return signature, None
        return None, "transaction not sent"
    sig = sent or signature
    try:
        confirmed = rpc.confirm_transaction(sig)
    except Exception:
        confirmed = False
    if confirmed:
        return sig, None
    if _recheck_confirmed(rpc, sig):
        return sig, None
    return None, "transaction not confirmed"


def send_and_confirm(rpc, signed_b64: str,
                     signature: str | None = None) -> tuple:
    """Send a locally-signed transaction and wait for it to land —
    the shared helper for any send (trades and user-initiated
    sends alike). Returns (signature, None) when it landed,
    else (None, reason). A transaction the chain confirms is
    always reported as a success, even when the send call or the
    first confirmation checks failed transiently — see
    _send_and_confirm."""
    return _send_and_confirm(rpc, signed_b64, signature)


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
        # No trading fee (removed 2026-11-12): the whole trade
        # amount is swapped for tokens.
        trade_lamports = amount_lamports
        tokens = pumpfun.buy_quote(curve, fee_bps, trade_lamports)
        if tokens <= 0:
            return _skip("amount too small to buy")
        max_sol_cost = (trade_lamports
                        * (10000 + pumpfun.DEFAULT_SLIPPAGE_BPS) // 10000
                        + 1)

        # v3 trades need the mint's token program (from the mint
        # account's owner) and one of the global account's buyback
        # fee recipients. Fail closed when either is unavailable —
        # never a guessed token program or recipient.
        buyback_recipients = glob.get("buyback_fee_recipients")
        if not buyback_recipients:
            return _skip("buyback fee recipient unavailable")
        try:
            mint_info = rpc.get_account_info(mint)
        except Exception:
            mint_info = None
        if not mint_info:
            return _skip("mint account unavailable")
        token_program = pumpfun.token_program_for_owner(
            mint_info.get("owner") or "")
        accts = pumpfun.derive_trade_accounts(
            mint, wallet_pubkey, token_program, buyback_recipients[0])
        instructions = []
        ata_before = 0
        try:
            ata_info = rpc.get_account_info(accts["associated_base_user"])
        except Exception:
            ata_info = None
        if ata_info is None:
            instructions.append(pumpfun.build_create_ata_instruction(
                wallet_pubkey, accts["associated_base_user"], wallet_pubkey,
                mint, token_program))
        else:
            try:
                ata_before = rpc.get_token_account_balance(
                    accts["associated_base_user"])
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
        confirmed, confirm_error = _send_and_confirm(
            rpc, signed_b64, signature)
        if confirmed is None:
            return _skip(confirm_error or "transaction not confirmed")
        coin_amount = tokens / pumpfun.TOKEN_BASE
        try:
            ata_after = rpc.get_token_account_balance(
                accts["associated_base_user"])
            coin_amount = (ata_after - ata_before) / pumpfun.TOKEN_BASE
        except Exception:
            pass
        # Executed SOL spent, from the wallet's own balance delta:
        # the trade amount plus the network fee for this transaction.
        # The loop needs the real spend for cost basis — the amount
        # asked for is not what the chain took. A balance read that
        # fails leaves the key out; the caller falls back to the
        # mark price instead of guessing.
        result = {"status": "executed", "signature": confirmed,
                  "coin_amount": coin_amount,
                  "signing_bot": signing_bot["support_id"]}
        try:
            balance_after = rpc.get_balance_lamports(wallet_pubkey)
            result["sol_spent"] = lamports_to_sol(
                balance - balance_after)
        except Exception:
            pass
        return result
    finally:
        # Wipe the seed IN PLACE the moment the trade is done —
        # success, refusal, or failure. Overwriting the buffer is
        # what actually scrubs memory; reassigning the variable
        # would only drop the reference and leave the bytes sitting.
        if seed is not None:
            wipe_secret(seed)


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
        # v3 trades need the mint's token program (from the mint
        # account's owner) and one of the global account's buyback
        # fee recipients. Fail closed when either is unavailable.
        buyback_recipients = glob.get("buyback_fee_recipients")
        if not buyback_recipients:
            return _skip("buyback fee recipient unavailable")
        try:
            mint_info = rpc.get_account_info(mint)
        except Exception:
            mint_info = None
        if not mint_info:
            return _skip("mint account unavailable")
        token_program = pumpfun.token_program_for_owner(
            mint_info.get("owner") or "")
        accts = pumpfun.derive_trade_accounts(
            mint, wallet_pubkey, token_program, buyback_recipients[0])
        try:
            held = rpc.get_token_account_balance(
                accts["associated_base_user"])
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
        # No trading fee (removed 2026-11-12): the whole sale
        # proceeds go to the seller's wallet.
        try:
            sol_before = rpc.get_balance_lamports(wallet_pubkey)
        except Exception:
            sol_before = None
        instructions = [pumpfun.build_sell_instruction(
            accts, token_base, min_sol_output)]
        _check_instructions_only_allowed(instructions)
        try:
            blockhash = rpc.get_latest_blockhash()
        except Exception:
            return _skip("no blockhash")
        message = build_message(wallet_pubkey, instructions, blockhash)
        signed_b64, signature = sign_message(seed, message)
        confirmed, confirm_error = _send_and_confirm(
            rpc, signed_b64, signature)
        if confirmed is None:
            return _skip(confirm_error or "transaction not confirmed")
        # Executed SOL received, from the wallet's own balance delta
        # — the on-chain proceeds, not a quoted estimate. The loop
        # needs the real proceeds for P&L. A balance read that fails
        # leaves the key out; the caller records the trade without a
        # USD P&L instead of guessing one.
        result = {"status": "executed", "signature": confirmed,
                  "signing_bot": signing_bot["support_id"]}
        if sol_before is not None:
            try:
                sol_after = rpc.get_balance_lamports(wallet_pubkey)
                result["sol_received"] = lamports_to_sol(
                    sol_after - sol_before)
            except Exception:
                pass
        return result
    finally:
        # Wipe the seed IN PLACE the moment the trade is done —
        # success, refusal, or failure. Overwriting the buffer is
        # what actually scrubs memory; reassigning the variable
        # would only drop the reference and leave the bytes sitting.
        if seed is not None:
            wipe_secret(seed)


def is_curve_complete(rpc, mint: str) -> bool | None:
    """True when the mint's bonding curve exists and is complete —
    the coin graduated (migrated to Raydium) and trades through
    Jupiter, not this executor. False when the curve exists and is
    still bonding. None when the curve cannot be read (no curve
    account, an unreadable account, an RPC failure) — the caller
    refuses plainly rather than guessing the venue. A coin with no
    bonding curve at all (never a pump.fun coin) is None too."""
    try:
        info = rpc.get_account_info(pumpfun.bonding_curve_pda(mint)[0])
    except Exception:
        return None
    if not info:
        return None
    try:
        curve = pumpfun.parse_bonding_curve(info["data"])
    except (ValueError, KeyError, TypeError):
        return None
    return bool(curve["complete"])


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
    "executed", "signature", "coin_amount", "signing_bot", plus
    "sol_spent" on buys / "sol_received" on sells — the executed SOL
    from the wallet's own balance delta, so the loop can record P&L
    from on-chain amounts} or {"status": "skipped", "reason": ...}.
    There is NO trading fee (removed 2026-11-12): the whole trade
    amount is swapped / paid out, and a trade that is skipped or
    fails never pays anything. Never raises for a refused trade.
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
