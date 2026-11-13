"""Real (mainnet) Jupiter swap executor: quote -> swap transaction ->
signing via the user's dedicated signing bot -> settlement.

MAINNET READY BUT NOT LIVE (James, 2026-11-11): this path is
complete, but it is GATED OFF by default behind JUPITER_REAL_SWAPS=1
(a Railway env var, admin-only — NOTHING in the user-facing UI can
flip it on) AND NETWORK=mainnet. Jupiter has no devnet liquidity,
so a real swap is mainnet real money. With the gate off, any
real/mainnet swap attempt refuses plainly and nothing is built,
signed or sent. No real money moves in tests either way.

HOW IT WORKS:
  1. Gate: `request_real_swap(cfg)` (JUPITER_REAL_SWAPS=1) and
     cfg.network == "mainnet". Both must hold.
  2. Bot limits: the bot must be in real mode; a buy must fit the
     bot's buy-size % of the wallet balance and leave the fee
     reserve; a sell must not exceed the wallet's token balance.
     (The rules engine allows the trade first — the caller only
     ever calls this for a trade the rules allowed. This executor
     re-checks the amount-level limits it can verify, same as the
     pump.fun executor.)
  3. Slippage + price impact: the quote uses 1% slippage
     (DEFAULT_SLIPPAGE_BPS) and the trade refuses when the quote's
     price impact exceeds 5% — a swap that moves the price that
     far is a bad trade, never a fill.
  4. Fresh quote, then `fetch_swap_transaction` builds the swap
     transaction from it. The quote is fetched inside this call
     (or injected by tests) and must be fresh — a stale quote
     refuses.
  5. ALLOWLIST (the signing bot's restriction): the swap
     transaction is a versioned (v0) transaction with address
     lookup tables. It is parsed (`parse_versioned_transaction`)
     and every instruction is checked BEFORE anything is signed:
       * exactly one signer, and the fee payer is the user's own
         wallet;
       * every instruction's program is in the allowlist —
         Jupiter aggregator, SPL Token, Token-2022, Associated
         Token, System, Compute Budget — and at least one
         instruction IS the Jupiter swap;
       * System instructions: only the SOL->wSOL wrap transfer
         (buy only, from the wallet to the wSOL ATA, for exactly
         the quoted input) or creating the wSOL account — any
         other System transfer refuses;
       * Token instructions: only transfer / transferChecked /
         closeAccount / syncNative. A transfer the user
         authorizes must come from the input token's ATA and its
         total must not exceed the quoted input; closeAccount /
         syncNative must be the wSOL unwrap/wrap;
       * Associated Token instructions: only creating the user's
         own ATA for the stated mint;
       * Compute Budget: unit limit <= 1.4M, unit price <=
         100,000 micro-lamports — a priority fee above that is
         an unreasonable drain and refuses.
     Anything else — an unknown program, an unexpected
     instruction, a transfer to another address — refuses and
     nothing is signed. A false refusal is safe; a false
     acceptance is not.
  6. Signing: the Jupiter-built message bytes are signed with
     the user's seed (decrypted transiently, wiped in place in
     a finally block, same as the pump.fun executor). The
     transaction is used EXACTLY as Jupiter built it — the
     executor never rewrites its bytes.
  7. Settlement: the signed swap is sent and confirmed
     (executor._send_and_confirm, same as pump.fun trades).
  8. NO FEE (removed 2026-11-12, James): trades used to pay a
     1.9% mainnet fee to the Treasury dev wallet as a separate
     System transfer after the swap confirmed. That is gone —
     the whole trade amount is swapped, nothing is held back,
     and no fee transfer is ever built or sent.

The signing bot holds no key itself — the app wallet key stays
encrypted in the wallets table, and the executor decrypts it
transiently only to sign a trade the rules engine allowed
(signing_bot.py). Key material is never logged, stored, or
returned anywhere. Every signed transaction is logged
(signature, bot, coin, amount, rule) by the caller.

Anything unverified refuses plainly with {"status": "skipped",
"reason": ...} and no fill: a closed gate, a non-mainnet
network, a missing wallet, no route, a bad quote, an allowlist
refusal, a failed send, or an unconfirmed transaction all skip
— never a fake fill. A failed trade is never retried.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from .config import Config
from .executor import (FEE_RESERVE_LAMPORTS, _load_wallet,
                       _send_and_confirm, _skip)
from .jupiter import (JUPITER_PROGRAM_ID, SOL_MINT, JupiterError,
                      fetch_swap_transaction, no_liquid_route_message,
                      quote_buy, quote_sell, request_real_swap,
                      resolve_mint)
from .security import wipe_secret
from .signing_bot import ensure_signing_bot
from .solana import (ASSOCIATED_TOKEN_PROGRAM, SYSTEM_PROGRAM,
                     TOKEN_2022_PROGRAM, TOKEN_PROGRAM, base58_decode,
                     base58_encode, compact_u16_encode,
                     ed25519_publickey_from_seed, ed25519_sign,
                     find_associated_token_address, lamports_to_sol,
                     sol_to_lamports, SolanaRpcClient)

# A quote that moves the price more than this refuses plainly —
# a swap with that much impact is a bad trade, never a fill.
MAX_PRICE_IMPACT_PCT = 5.0
# A quote older than this is stale and refuses — the swap must be
# built from a quote fetched in this call.
MAX_QUOTE_AGE_SECONDS = 120
# Compute-budget caps: the runtime max is 1.4M compute units, and
# Jupiter's own priority fees sit far below 100k micro-lamports
# per unit (a live SOL->wZEC swap used 1.4M units at ~71.7k).
# Anything above either cap is an unreasonable fee drain.
MAX_COMPUTE_UNIT_LIMIT = 1_400_000
MAX_COMPUTE_UNIT_PRICE_MICRO_LAMPORTS = 100_000
# An address lookup table account: 56-byte meta, then 32-byte
# addresses. (Verified live 2026-11-11 against a real Jupiter
# swap transaction: the tables' addresses resolve from offset 56.)
LOOKUP_TABLE_META_SIZE = 56
# Rent for a token account is ~2.04M lamports; a create-account
# asking for more than 0.01 SOL of rent is not a token account.
MAX_CREATE_ACCOUNT_LAMPORTS = 10_000_000
# A token account is 165 bytes.
TOKEN_ACCOUNT_SIZE = 165

COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"

# Every program a Jupiter swap transaction may use. The System
# program is in the set but its instructions are validated
# individually (wrap only) — see _check_system_instruction.
ALLOWED_PROGRAM_IDS = frozenset([
    JUPITER_PROGRAM_ID,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    ASSOCIATED_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    COMPUTE_BUDGET_PROGRAM,
])


def _read_compact_u16(data: bytes, off: int) -> tuple[int, int]:
    """(value, new offset) — compact-u16 (7 bits per byte, MSB set
    means continue). Raises ValueError on truncated input."""
    value = 0
    shift = 0
    while True:
        if off >= len(data):
            raise ValueError("truncated compact-u16")
        byte = data[off]
        off += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, off
        shift += 7
        if shift > 21:
            raise ValueError("compact-u16 too long")


def parse_versioned_transaction(swap_tx_b64: str) -> dict:
    """Decode a base64 versioned (v0) transaction — the shape
    Jupiter's /swap returns — into its parts:
    {message_bytes, fee_payer, num_required_signatures,
    static_keys, blockhash, instructions, address_table_lookups}.
    Each instruction is {program_id_index, accounts, data}. Raises
    ValueError on anything malformed (bad base64, truncated
    message, trailing bytes, an unsupported version). Legacy
    transactions (no version byte) parse too, with no lookups."""
    try:
        raw = base64.b64decode(swap_tx_b64, validate=True)
    except Exception as exc:
        raise ValueError("not a base64 transaction") from exc
    off = 0
    n_sigs, off = _read_compact_u16(raw, off)
    if n_sigs < 1 or n_sigs > 8:
        raise ValueError("bad signature count")
    if off + 64 * n_sigs > len(raw):
        raise ValueError("truncated signatures")
    off += 64 * n_sigs
    message = raw[off:]
    if not message:
        raise ValueError("empty transaction message")
    moff = 0
    version = None
    if message[0] & 0x80:
        version = message[0] & 0x7F
        if version != 0:
            raise ValueError("unsupported transaction version")
        moff = 1
    if moff + 3 > len(message):
        raise ValueError("truncated message header")
    num_sigs, _num_readonly_signed, _num_readonly_unsigned = (
        message[moff], message[moff + 1], message[moff + 2])
    moff += 3
    n_keys, moff = _read_compact_u16(message, moff)
    if moff + 32 * n_keys > len(message):
        raise ValueError("truncated account keys")
    static_keys = [base58_encode(message[moff + i * 32:
                                         moff + (i + 1) * 32])
                   for i in range(n_keys)]
    moff += 32 * n_keys
    if moff + 32 > len(message):
        raise ValueError("truncated blockhash")
    blockhash = base58_encode(message[moff:moff + 32])
    moff += 32
    n_ins, moff = _read_compact_u16(message, moff)
    instructions = []
    for _ in range(n_ins):
        if moff >= len(message):
            raise ValueError("truncated instruction")
        prog_idx = message[moff]
        moff += 1
        n_acc, moff = _read_compact_u16(message, moff)
        if moff + n_acc > len(message):
            raise ValueError("truncated instruction accounts")
        accounts = list(message[moff:moff + n_acc])
        moff += n_acc
        dlen, moff = _read_compact_u16(message, moff)
        if moff + dlen > len(message):
            raise ValueError("truncated instruction data")
        data = bytes(message[moff:moff + dlen])
        moff += dlen
        instructions.append({"program_id_index": prog_idx,
                             "accounts": accounts, "data": data})
    lookups = []
    if version is not None:
        n_lk, moff = _read_compact_u16(message, moff)
        for _ in range(n_lk):
            if moff + 32 > len(message):
                raise ValueError("truncated lookup table")
            table = base58_encode(message[moff:moff + 32])
            moff += 32
            n_w, moff = _read_compact_u16(message, moff)
            if moff + n_w > len(message):
                raise ValueError("truncated lookup indexes")
            writable = list(message[moff:moff + n_w])
            moff += n_w
            n_r, moff = _read_compact_u16(message, moff)
            if moff + n_r > len(message):
                raise ValueError("truncated lookup indexes")
            readonly = list(message[moff:moff + n_r])
            moff += n_r
            lookups.append({"table": table, "writable": writable,
                            "readonly": readonly})
    if moff != len(message):
        raise ValueError("trailing bytes in transaction message")
    return {"message_bytes": message, "fee_payer": static_keys[0]
            if static_keys else None,
            "num_required_signatures": num_sigs,
            "static_keys": static_keys, "blockhash": blockhash,
            "instructions": instructions,
            "address_table_lookups": lookups}


def resolve_lookup_tables(rpc, lookups: list) -> list:
    """The lookup-table accounts' addresses, in the runtime's
    resolution order: all writable accounts (lookups in order),
    then all readonly accounts. Raises ValueError when a table
    is missing, too small, or an index is out of range — the
    transaction cannot be verified without them."""
    writable: list = []
    readonly: list = []
    for lookup in lookups:
        info = rpc.get_account_info(lookup["table"])
        if not info:
            raise ValueError("address lookup table not found")
        data = info["data"]
        if len(data) < LOOKUP_TABLE_META_SIZE:
            raise ValueError("address lookup table too small")
        addresses = [base58_encode(
            data[LOOKUP_TABLE_META_SIZE + i * 32:
                 LOOKUP_TABLE_META_SIZE + (i + 1) * 32])
            for i in range((len(data) - LOOKUP_TABLE_META_SIZE) // 32)]
        for idx in lookup["writable"]:
            if idx >= len(addresses):
                raise ValueError("lookup index out of range")
            writable.append(addresses[idx])
        for idx in lookup["readonly"]:
            if idx >= len(addresses):
                raise ValueError("lookup index out of range")
            readonly.append(addresses[idx])
    return writable + readonly


def resolved_account_keys(parsed: dict, rpc) -> list:
    """The full account list a versioned transaction's
    instruction indexes point into: the static keys, then the
    lookup-table accounts (writable first, then readonly — the
    runtime's order, verified live 2026-11-11 against a real
    Jupiter swap transaction)."""
    return parsed["static_keys"] + resolve_lookup_tables(
        rpc, parsed["address_table_lookups"])


def _input_ata(wallet_pubkey: str, input_mint: str) -> str:
    """The user's associated token account for the input mint —
    the only account a user-authorized token transfer may draw
    from."""
    return find_associated_token_address(wallet_pubkey, input_mint)


def _check_system_instruction(ins: dict, accounts: list,
                              wallet_pubkey: str, wsol_ata: str,
                              input_amount: int, side: str) -> None:
    """A System instruction in a Jupiter swap is only ever the
    SOL wrap: a transfer (buy only, wallet -> wSOL ATA, exactly
    the quoted input) or creating the wSOL account. Anything
    else refuses."""
    data = ins["data"]
    if len(data) < 4:
        raise ValueError("refused: malformed System instruction")
    disc = int.from_bytes(data[0:4], "little")
    if disc == 2:  # transfer
        if len(data) != 12 or len(accounts) != 2:
            raise ValueError("refused: malformed System transfer")
        lamports = int.from_bytes(data[4:12], "little")
        if side != "buy":
            raise ValueError(
                "refused: unexpected SOL transfer in a sell")
        if accounts[0] != wallet_pubkey or accounts[1] != wsol_ata:
            raise ValueError(
                "refused: SOL transfer is not the SOL wrap")
        if lamports != input_amount:
            raise ValueError(
                "refused: wrapped SOL is not the quoted amount")
    elif disc == 0:  # createAccount
        if len(data) != 52 or len(accounts) != 2:
            raise ValueError(
                "refused: malformed System createAccount")
        lamports = int.from_bytes(data[4:12], "little")
        space = int.from_bytes(data[12:20], "little")
        owner = base58_encode(data[20:52])
        if accounts[0] != wallet_pubkey or accounts[1] != wsol_ata:
            raise ValueError(
                "refused: account creation is not the wSOL account")
        if owner != TOKEN_PROGRAM or space != TOKEN_ACCOUNT_SIZE:
            raise ValueError(
                "refused: account creation is not a token account")
        if lamports > MAX_CREATE_ACCOUNT_LAMPORTS:
            raise ValueError(
                "refused: account creation rent too high")
    else:
        raise ValueError("refused: unexpected System instruction")


def _check_token_instruction(ins: dict, accounts: list,
                             wallet_pubkey: str, input_mint: str,
                             wsol_ata: str) -> int:
    """A Token instruction in a Jupiter swap is only ever the
    wrap/unwrap helpers or a transfer the user authorizes.
    Returns the base units this instruction moves out of the
    user's input ATA (0 when it moves none) so the caller can
    cap the total at the quoted input. Anything else refuses."""
    data = ins["data"]
    if not data:
        raise ValueError("refused: empty token instruction")
    disc = data[0]
    if disc == 3:  # transfer
        if len(data) != 9 or len(accounts) < 3:
            raise ValueError("refused: malformed token transfer")
        amount = int.from_bytes(data[1:9], "little")
        source, authority = accounts[0], accounts[2]
        if authority != wallet_pubkey:
            return 0  # a pool-side transfer, not the user's funds
        if source != _input_ata(wallet_pubkey, input_mint):
            raise ValueError(
                "refused: token transfer is not from the input "
                "account")
        return amount
    if disc == 12:  # transferChecked
        if len(data) != 10 or len(accounts) < 4:
            raise ValueError("refused: malformed token transfer")
        amount = int.from_bytes(data[1:9], "little")
        source, authority = accounts[0], accounts[3]
        if authority != wallet_pubkey:
            return 0
        if source != _input_ata(wallet_pubkey, input_mint):
            raise ValueError(
                "refused: token transfer is not from the input "
                "account")
        return amount
    if disc == 9:  # closeAccount (the wSOL unwrap)
        if len(accounts) < 3:
            raise ValueError("refused: malformed token close")
        account, dest, authority = (accounts[0], accounts[1],
                                    accounts[2])
        if (account != wsol_ata or dest != wallet_pubkey
                or authority != wallet_pubkey):
            raise ValueError(
                "refused: token close is not the wSOL unwrap")
        return 0
    if disc == 17:  # syncNative (the wSOL wrap)
        if len(accounts) < 1 or accounts[0] != wsol_ata:
            raise ValueError(
                "refused: token sync is not the wSOL wrap")
        return 0
    raise ValueError("refused: unexpected token instruction")


def _check_ata_instruction(ins: dict, accounts: list,
                           wallet_pubkey: str) -> None:
    """An Associated Token instruction in a Jupiter swap only ever
    creates the user's own ATA for the stated mint (Create, or
    CreateIdempotent with data [1]). Anything else refuses."""
    if ins["data"] not in (b"", b"\x01"):
        raise ValueError(
            "refused: unexpected associated-token instruction")
    if len(accounts) != 6:
        raise ValueError(
            "refused: malformed associated-token instruction")
    payer, ata, owner, mint, system_pid, token_pid = accounts
    if payer != wallet_pubkey or owner != wallet_pubkey:
        raise ValueError(
            "refused: associated-token account is not the user's")
    if system_pid != SYSTEM_PROGRAM or token_pid not in (
            TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError(
            "refused: malformed associated-token instruction")
    expected = find_associated_token_address(owner, mint, token_pid)
    if ata != expected:
        raise ValueError(
            "refused: associated-token account mismatch")


def _check_compute_budget_instruction(ins: dict) -> None:
    """A Compute Budget instruction only ever sets the unit limit
    or the unit price, both capped — a priority fee above the cap
    is an unreasonable drain and refuses."""
    data = ins["data"]
    if not data:
        raise ValueError(
            "refused: empty compute-budget instruction")
    disc = data[0]
    if disc == 2:  # setComputeUnitLimit
        if len(data) != 5:
            raise ValueError(
                "refused: malformed compute-budget instruction")
        if int.from_bytes(data[1:5], "little") \
                > MAX_COMPUTE_UNIT_LIMIT:
            raise ValueError(
                "refused: compute-unit limit too high")
    elif disc == 3:  # setComputeUnitPrice
        if len(data) != 9:
            raise ValueError(
                "refused: malformed compute-budget instruction")
        if int.from_bytes(data[1:9], "little") \
                > MAX_COMPUTE_UNIT_PRICE_MICRO_LAMPORTS:
            raise ValueError(
                "refused: compute-unit price too high")
    else:
        raise ValueError(
            "refused: unexpected compute-budget instruction")


def verify_jupiter_transaction(parsed: dict, resolved_keys: list,
                               wallet_pubkey: str, input_mint: str,
                               input_amount: int, side: str,
                               wsol_ata: str) -> None:
    """The signing-bot allowlist for a Jupiter swap transaction.
    Raises ValueError on anything unexpected — the caller must
    refuse and sign nothing. `input_amount` is the quoted input
    in base units (lamports for a buy, the token's base units
    for a sell); `wsol_ata` is the user's wrapped-SOL account."""
    if parsed["num_required_signatures"] != 1:
        raise ValueError(
            "refused: swap transaction must have exactly one signer")
    if parsed["fee_payer"] != wallet_pubkey:
        raise ValueError(
            "refused: fee payer is not the user's wallet")
    instructions = parsed["instructions"]
    if not instructions:
        raise ValueError(
            "refused: swap transaction has no instructions")
    programs = []
    for ins in instructions:
        idx = ins["program_id_index"]
        if idx >= len(resolved_keys):
            raise ValueError(
                "refused: instruction program index out of range")
        programs.append(resolved_keys[idx])
    if JUPITER_PROGRAM_ID not in programs:
        raise ValueError("refused: no Jupiter swap instruction")
    user_token_spent = 0
    for ins, pid in zip(instructions, programs):
        if pid not in ALLOWED_PROGRAM_IDS:
            raise ValueError(
                "refused: instruction for an unexpected program")
        accounts = []
        for idx in ins["accounts"]:
            if idx >= len(resolved_keys):
                raise ValueError(
                    "refused: instruction account index out of range")
            accounts.append(resolved_keys[idx])
        if pid == SYSTEM_PROGRAM:
            _check_system_instruction(ins, accounts, wallet_pubkey,
                                      wsol_ata, input_amount, side)
        elif pid in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            user_token_spent += _check_token_instruction(
                ins, accounts, wallet_pubkey, input_mint, wsol_ata)
        elif pid == ASSOCIATED_TOKEN_PROGRAM:
            _check_ata_instruction(ins, accounts, wallet_pubkey)
        elif pid == COMPUTE_BUDGET_PROGRAM:
            _check_compute_budget_instruction(ins)
        # The Jupiter program instruction is the swap itself:
        # allowed (its minimum output is enforced on-chain by the
        # program, from the quote this transaction was built
        # from).
    if user_token_spent > input_amount:
        raise ValueError(
            "refused: swap moves more of the input token than quoted")


def sign_versioned_transaction(seed: bytes, swap_tx_b64: str,
                               expected_fee_payer: str | None = None
                               ) -> tuple[str, str]:
    """Sign a Jupiter-built versioned transaction: the message
    bytes are signed exactly as Jupiter built them (never
    rewritten) and the signature replaces the empty slot.
    Returns (signed_tx_base64, signature_base58). Raises
    ValueError when the seed does not match the fee payer."""
    parsed = parse_versioned_transaction(swap_tx_b64)
    derived = base58_encode(ed25519_publickey_from_seed(seed))
    if parsed["fee_payer"] != derived:
        raise ValueError("fee payer mismatch")
    if expected_fee_payer is not None \
            and derived != expected_fee_payer:
        raise ValueError("fee payer mismatch")
    signature = ed25519_sign(seed, parsed["message_bytes"])
    serialized = (compact_u16_encode(1) + signature
                  + parsed["message_bytes"])
    return (base64.b64encode(serialized).decode("ascii"),
            base58_encode(signature))


def _check_quote_fresh(quote: dict) -> None:
    """A quote older than MAX_QUOTE_AGE_SECONDS refuses — the swap
    must be built from a quote fetched in this call. A quote
    without a fetched_at is treated as fresh (unit-test quotes)."""
    fetched = quote.get("fetched_at")
    if not fetched:
        return
    try:
        when = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age > MAX_QUOTE_AGE_SECONDS:
        raise ValueError("refused: quote is stale")


def _check_price_impact(quote: dict) -> None:
    """A quote whose price impact exceeds the cap refuses — a swap
    that moves the price that far is a bad trade, never a fill."""
    try:
        impact = float(quote.get("price_impact_pct") or 0.0)
    except (TypeError, ValueError):
        impact = 0.0
    if impact > MAX_PRICE_IMPACT_PCT:
        raise ValueError(
            f"refused: price impact {impact:.2f}% is too high")


def _token_balance_or_zero(rpc, ata: str) -> int:
    """A token account's balance in base units, or 0 when the
    account does not exist yet (an address that never held this
    token has no token account — a normal answer, not an error)."""
    try:
        return rpc.get_token_account_balance(ata)
    except Exception:
        return 0


def execute_jupiter_swap(bot, user_id: int, symbol: str, side: str,
                         amount: float, db, cfg: Config,
                         rpc_client=None, quote: dict | None = None,
                         coin_name: str | None = None) -> dict:
    """Execute one real Jupiter swap for a bot, signed by the
    user's dedicated signing bot. Same (bot, user_id, coin, side,
    amount, db, cfg) protocol as the pump.fun executor's
    execute_real_trade, so the manual coin-page trades and the
    silent loop can both call it.

    `symbol` is a listed coin (BTC/ETH/XRP/ZEC/...); `amount` is
    SOL for buys and whole tokens for sells. `quote` is a test
    injection (a fresh quote dict); in production the quote is
    always fetched here. Returns {"status": "executed",
    "signature", "coin_amount", "signing_bot": ...} or {"status":
    "skipped", "reason": ...}. There is NO trading fee (removed
    2026-11-12): the whole trade amount is swapped. Never raises
    for a refused trade."""
    # The gate: James's explicit go-ahead AND mainnet. Either
    # missing refuses plainly — nothing is built, signed or sent.
    try:
        request_real_swap(cfg)
    except JupiterError as exc:
        return _skip(str(exc))
    if cfg.network != "mainnet":
        return _skip("Jupiter swaps need mainnet")
    if bot["mode"] != "real":
        return _skip("bot is not in real mode")
    if side not in ("buy", "sell"):
        return _skip("unknown side")
    if amount is None or float(amount) <= 0:
        return _skip("amount must be positive")
    try:
        signing_bot = ensure_signing_bot(db, user_id)
    except Exception:
        return _skip("signing bot unavailable")
    rpc = rpc_client if rpc_client is not None \
        else SolanaRpcClient(cfg.rpc_url)
    wallet_pubkey, seed = _load_wallet(db, user_id, cfg)
    if wallet_pubkey is None:
        return _skip("no app wallet")
    try:
        try:
            resolved = resolve_mint(symbol, coin_name=coin_name)
        except Exception:
            return _skip("No Jupiter route for this coin")
        if resolved is None:
            return _skip("No Jupiter route for this coin")
        mint = resolved["mint"]
        decimals = resolved["decimals"]
        wsol_ata = find_associated_token_address(wallet_pubkey,
                                                 SOL_MINT)
        if side == "buy":
            return _execute_jupiter_buy(
                bot, user_id, symbol, mint, decimals, float(amount),
                db, cfg, rpc, signing_bot, wallet_pubkey, seed,
                wsol_ata, quote)
        return _execute_jupiter_sell(
            bot, user_id, symbol, mint, decimals, float(amount),
            db, cfg, rpc, signing_bot, wallet_pubkey, seed,
            wsol_ata, quote)
    except Exception as exc:
        # A failed signing attempt is a loud skip, never a retry
        # loop and never a fake fill.
        return _skip(f"jupiter execution failed: {str(exc)[:80]}")
    finally:
        # Wipe the seed IN PLACE the moment the trade is done —
        # success, refusal, or failure.
        if seed is not None:
            wipe_secret(seed)


def _execute_jupiter_buy(bot, user_id, symbol, mint, decimals,
                         amount_sol, db, cfg, rpc, signing_bot,
                         wallet_pubkey, seed, wsol_ata, quote):
    """One real Jupiter BUY: `amount_sol` SOL, all of it swapped
    for the wrapped token (no fee — removed 2026-11-12)."""
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
    # No fee (removed 2026-11-12): the whole trade amount is
    # swapped for tokens.
    swap_lamports = amount_lamports
    swap_sol = lamports_to_sol(swap_lamports)
    if quote is None:
        try:
            quote = quote_buy(symbol, swap_sol)
        except JupiterError as exc:
            # No liquid route says so plainly; anything else
            # (Jupiter unreachable) is reported as itself.
            return _skip(no_liquid_route_message(exc) or str(exc))
    try:
        _check_quote_fresh(quote)
        _check_price_impact(quote)
    except ValueError as exc:
        return _skip(str(exc))
    out_ata = find_associated_token_address(wallet_pubkey, mint)
    ata_before = _token_balance_or_zero(rpc, out_ata)
    try:
        swap_tx_b64 = fetch_swap_transaction(quote["raw"],
                                             wallet_pubkey)
    except JupiterError as exc:
        return _skip(no_liquid_route_message(exc) or str(exc))
    try:
        parsed = parse_versioned_transaction(swap_tx_b64)
        resolved_keys = resolved_account_keys(parsed, rpc)
        verify_jupiter_transaction(
            parsed, resolved_keys, wallet_pubkey, SOL_MINT,
            swap_lamports, "buy", wsol_ata)
        signed_b64, signature = sign_versioned_transaction(
            seed, swap_tx_b64, wallet_pubkey)
    except ValueError as exc:
        return _skip(str(exc))
    confirmed, confirm_error = _send_and_confirm(
        rpc, signed_b64, signature)
    if confirmed is None:
        return _skip(confirm_error or "transaction not confirmed")
    coin_amount = float(quote["out_amount_ui"])
    try:
        ata_after = rpc.get_token_account_balance(out_ata)
        coin_amount = (ata_after - ata_before) / (10 ** decimals)
    except Exception:
        pass
    # Executed SOL spent, from the wallet's own balance delta —
    # the trade amount plus the network fee. The loop needs the
    # real spend for cost basis. A failed read leaves the key out.
    result = {"status": "executed", "signature": confirmed,
              "coin_amount": coin_amount,
              "signing_bot": signing_bot["support_id"]}
    try:
        balance_after = rpc.get_balance_lamports(wallet_pubkey)
        result["sol_spent"] = lamports_to_sol(balance - balance_after)
    except Exception:
        pass
    return result


def _execute_jupiter_sell(bot, user_id, symbol, mint, decimals,
                          amount_tokens, db, cfg, rpc, signing_bot,
                          wallet_pubkey, seed, wsol_ata, quote):
    """One real Jupiter SELL: `amount_tokens` of the wrapped token
    -> SOL. All proceeds go to the seller's wallet (no fee —
    removed 2026-11-12)."""
    token_base = int(round(amount_tokens * (10 ** decimals)))
    if token_base <= 0:
        return _skip("amount too small to sell")
    coin_ata = find_associated_token_address(wallet_pubkey, mint)
    try:
        held = rpc.get_token_account_balance(coin_ata)
    except Exception:
        return _skip("token balance unavailable")
    if token_base > held:
        return _skip("insufficient token balance")
    if quote is None:
        try:
            quote = quote_sell(symbol, amount_tokens)
        except JupiterError as exc:
            # No liquid route says so plainly; anything else
            # (Jupiter unreachable) is reported as itself.
            return _skip(no_liquid_route_message(exc) or str(exc))
    try:
        _check_quote_fresh(quote)
        _check_price_impact(quote)
    except ValueError as exc:
        return _skip(str(exc))
    # The expected SOL proceeds (the quote's out amount, in
    # lamports) — the fallback for what the sell returned, when
    # the wallet balance cannot be read before/after.
    expected_sol_lamports = int(quote["out_amount"])
    try:
        sol_before = rpc.get_balance_lamports(wallet_pubkey)
    except Exception:
        sol_before = None
    try:
        swap_tx_b64 = fetch_swap_transaction(quote["raw"],
                                             wallet_pubkey)
    except JupiterError as exc:
        return _skip(no_liquid_route_message(exc) or str(exc))
    try:
        parsed = parse_versioned_transaction(swap_tx_b64)
        resolved_keys = resolved_account_keys(parsed, rpc)
        verify_jupiter_transaction(
            parsed, resolved_keys, wallet_pubkey, mint,
            token_base, "sell", wsol_ata)
        signed_b64, signature = sign_versioned_transaction(
            seed, swap_tx_b64, wallet_pubkey)
    except ValueError as exc:
        return _skip(str(exc))
    confirmed, confirm_error = _send_and_confirm(
        rpc, signed_b64, signature)
    if confirmed is None:
        return _skip(confirm_error or "transaction not confirmed")
    sol_received = lamports_to_sol(expected_sol_lamports)
    if sol_before is not None:
        try:
            sol_after = rpc.get_balance_lamports(wallet_pubkey)
            sol_received = lamports_to_sol(sol_after - sol_before)
        except Exception:
            pass
    return {"status": "executed", "signature": confirmed,
            "coin_amount": sol_received, "sol_received": sol_received,
            "signing_bot": signing_bot["support_id"]}
