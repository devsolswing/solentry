"""Pump.fun program (devnet): account layouts, quote math, and
instruction builders for bonding-curve buys and sells.

Verified against devnet RPC (2026-11-09): the program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P is executable, and the
global account at PDA ["global"] parses to the known constants
(virtual token reserves 1,073,000,000,000,000; virtual SOL reserves
30 SOL; token supply 1,000,000,000,000,000; fee 95 bps).

Trades use the v3 instructions (`buy_v3` / `sell_v3`), per the
program's own docs (pump-fun/pump-public-docs,
docs/instructions/TRADE_V3.md) and live devnet transactions — they
are the only buy/sell path that works on the deployed program.
The legacy `buy` / `sell` instructions are dead there: the legacy
buy fails in simulation with AccountNotEnoughKeys for
global_volume_accumulator, and after adding that, the fee
config, the creator vault and the 8 buyback recipients it then
fails with InvalidBondingCurveV2 (the legacy path needs a
bonding_curve_v2 remaining account that no legacy-created token
has). The v3 buy for Gate simulates cleanly (err null). v3 keeps
the protocol and creator fees on the curve instead of paying them
out per trade, so there is no fee_recipient, creator_vault,
global_volume_accumulator or fee_program account — and the
buyback fee goes to ONE of the global account's 8 buyback
recipients, passed as a single account. Prices, slippage checks
and fee amounts are the same as v2/legacy, so the quote math
below is unchanged.

Instruction discriminators are the first 8 bytes of
SHA-256("global:buy_v3") / SHA-256("global:sell_v3") — buy_v3 is
07051dc4f5176550, sell_v3 is 1c92de7726c469d5, matching the
program's published IDL. Account order and flags follow
TRADE_V3.md exactly.

Nothing in this module signs or sends anything — it only builds
instruction data. Signing happens in executor.py, which also
enforces that ONLY these instructions are ever built (no transfers,
no withdrawals, no other destinations).
"""

from __future__ import annotations

import hashlib

from .solana import (ASSOCIATED_TOKEN_PROGRAM, SYSTEM_PROGRAM,
                     TOKEN_PROGRAM, base58_decode, base58_encode,
                     find_associated_token_address, find_program_address)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Pump Fees program (owns the fee_config account).
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
# Wrapped SOL — the quote mint for every SOL-paired coin in v3.
WSOL_MINT = "So11111111111111111111111111111111111111112"
# Token-2022 program — the token program for coins created with
# create_v2. Legacy coins (like Gate) use the SPL Token program.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_DECIMALS = 6
TOKEN_BASE = 10 ** TOKEN_DECIMALS

# Default slippage guard on the trade itself (on top of the fee):
# the program must fill within 1% of the quoted price or the
# transaction fails on-chain. The executor never widens this.
DEFAULT_SLIPPAGE_BPS = 100


def discriminator(name: str) -> bytes:
    """First 8 bytes of SHA-256("global:<name>")."""
    return hashlib.sha256(("global:" + name).encode("utf-8")).digest()[:8]


def global_pda() -> tuple[str, int]:
    return find_program_address([b"global"], PUMP_PROGRAM)


def event_authority_pda() -> tuple[str, int]:
    return find_program_address([b"__event_authority"], PUMP_PROGRAM)


def bonding_curve_pda(mint_b58: str) -> tuple[str, int]:
    return find_program_address(
        [b"bonding-curve", base58_decode(mint_b58)], PUMP_PROGRAM)


def parse_global(data: bytes) -> dict:
    """Global account layout: discriminator (8), initialized (1),
    authority (32), fee_recipient (32), initial virtual token
    reserves (u64), initial virtual SOL reserves (u64), initial real
    token reserves (u64), token total supply (u64), fee basis
    points (u64). The live account is 1088 bytes and also carries
    the buyback config: 8 buyback fee recipient pubkeys at offset
    741 and buyback_basis_points at 997. Those two fields are
    only present when the account is long enough; a short (113
    byte) account parses without them (buyback fields None)."""
    if len(data) < 113:
        raise ValueError("global account too short")
    out = {
        "authority": base58_encode(data[9:41]),
        "fee_recipient": base58_encode(data[41:73]),
        "initial_virtual_token_reserves": int.from_bytes(data[73:81],
                                                         "little"),
        "initial_virtual_sol_reserves": int.from_bytes(data[81:89],
                                                       "little"),
        "initial_real_token_reserves": int.from_bytes(data[89:97],
                                                      "little"),
        "token_total_supply": int.from_bytes(data[97:105], "little"),
        "fee_basis_points": int.from_bytes(data[105:113], "little"),
        "buyback_fee_recipients": None,
        "buyback_basis_points": None,
    }
    if len(data) >= 1005:
        out["buyback_fee_recipients"] = [
            base58_encode(data[741 + i * 32:741 + (i + 1) * 32])
            for i in range(8)]
        out["buyback_basis_points"] = int.from_bytes(data[997:1005],
                                                     "little")
    return out


def parse_bonding_curve(data: bytes) -> dict:
    """Bonding-curve layout: discriminator (8), virtual token
    reserves (u64), virtual SOL reserves (u64), real token reserves
    (u64), real SOL reserves (u64), token total supply (u64),
    complete (bool)."""
    if len(data) < 49:
        raise ValueError("bonding curve account too short")
    return {
        "virtual_token_reserves": int.from_bytes(data[8:16], "little"),
        "virtual_sol_reserves": int.from_bytes(data[16:24], "little"),
        "real_token_reserves": int.from_bytes(data[24:32], "little"),
        "real_sol_reserves": int.from_bytes(data[32:40], "little"),
        "token_total_supply": int.from_bytes(data[40:48], "little"),
        "complete": bool(data[48]),
    }


def buy_program_fee(sol_lamports: int, fee_bps: int) -> int:
    """The pump.fun program fee on a buy of `sol_lamports` SOL in,
    at the live global fee_bps: sol * fee_bps // 10000. This is
    the fee buy_quote subtracts before the curve math — the same
    formula the program applies on-chain, so the fee recorded
    per trade is the actual program fee, never an estimate.
    Honest limit: the quote is computed just before execution,
    and the program's fee is computed on the SOL actually spent,
    which can move by up to the 1% slippage — so the recorded
    pump fee is accurate to within slippage, not to the lamport
    after execution."""
    if sol_lamports <= 0:
        return 0
    return sol_lamports * fee_bps // 10000


def _sell_gross_sol(curve: dict, token_base_units: int) -> int:
    """Gross SOL out for a sell, before the program fee."""
    vt = curve["virtual_token_reserves"]
    vs = curve["virtual_sol_reserves"]
    if vt <= 0 or vs <= 0:
        return 0
    return vs - (vt * vs) // (vt + token_base_units)


def sell_program_fee(curve: dict, fee_bps: int,
                     token_base_units: int) -> int:
    """The pump.fun program fee on a sell, in SOL lamports: the
    fee on the gross SOL out, gross * fee_bps // 10000 — the same
    formula sell_quote subtracts. Same slippage caveat as
    buy_program_fee: accurate to within slippage."""
    if token_base_units <= 0:
        return 0
    return _sell_gross_sol(curve, token_base_units) * fee_bps // 10000


def buy_quote(curve: dict, fee_bps: int, sol_lamports: int) -> int:
    """Token base units out for `sol_lamports` in, after the program
    fee. Integer math only. Returns 0 when the amount is too small
    to buy anything."""
    if sol_lamports <= 0:
        return 0
    fee = buy_program_fee(sol_lamports, fee_bps)
    net_sol = sol_lamports - fee
    vt = curve["virtual_token_reserves"]
    vs = curve["virtual_sol_reserves"]
    if net_sol <= 0 or vt <= 0 or vs <= 0:
        return 0
    return max(0, vt - (vt * vs) // (vs + net_sol))


def sell_quote(curve: dict, fee_bps: int, token_base_units: int) -> int:
    """SOL lamports out for `token_base_units` in, after the program
    fee. Integer math only."""
    if token_base_units <= 0:
        return 0
    vt = curve["virtual_token_reserves"]
    vs = curve["virtual_sol_reserves"]
    if vt <= 0 or vs <= 0:
        return 0
    gross = _sell_gross_sol(curve, token_base_units)
    fee = sell_program_fee(curve, fee_bps, token_base_units)
    return max(0, gross - fee)


def fee_config_pda() -> tuple[str, int]:
    """Pump Fees PDA: seeds ["fee_config", pump_program_id],
    derived under the fee program (not the pump program)."""
    return find_program_address(
        [b"fee_config", base58_decode(PUMP_PROGRAM)], FEE_PROGRAM)


def user_volume_accumulator_pda(user_b58: str) -> tuple[str, int]:
    return find_program_address(
        [b"user_volume_accumulator", base58_decode(user_b58)],
        PUMP_PROGRAM)


def token_program_for_owner(owner_b58: str) -> str:
    """The token program for a mint, from the mint account's owner:
    Token-2022 for create_v2 coins, the SPL Token program for
    legacy coins (and anything else — the safe default)."""
    if owner_b58 == TOKEN_2022_PROGRAM:
        return TOKEN_2022_PROGRAM
    return TOKEN_PROGRAM


def derive_trade_accounts(mint_b58: str, user_b58: str,
                          base_token_program_b58: str,
                          buyback_fee_recipient_b58: str) -> dict:
    """All accounts a pump.fun v3 buy/sell needs (TRADE_V3.md order),
    derived from the mint and the user. The base token program
    comes from the mint account's owner (Token-2022 for create_v2
    coins, SPL Token for legacy ones); the buyback recipient is
    one of the global account's 8 buyback fee recipients."""
    global_addr, _ = global_pda()
    event_authority, _ = event_authority_pda()
    curve_addr, _ = bonding_curve_pda(mint_b58)
    fee_config_addr, _ = fee_config_pda()
    user_volume_acc, _ = user_volume_accumulator_pda(user_b58)
    return {
        "global": global_addr,
        "base_mint": mint_b58,
        "quote_mint": WSOL_MINT,
        "base_token_program": base_token_program_b58,
        "quote_token_program": TOKEN_PROGRAM,
        "bonding_curve": curve_addr,
        "associated_base_bonding_curve": find_associated_token_address(
            curve_addr, mint_b58, base_token_program_b58),
        "associated_quote_bonding_curve":
            find_associated_token_address(curve_addr, WSOL_MINT),
        "user": user_b58,
        "associated_base_user": find_associated_token_address(
            user_b58, mint_b58, base_token_program_b58),
        "associated_quote_user": find_associated_token_address(
            user_b58, WSOL_MINT),
        "user_volume_accumulator": user_volume_acc,
        "fee_config": fee_config_addr,
        "buyback_fee_recipient": buyback_fee_recipient_b58,
        "event_authority": event_authority,
    }


def build_create_ata_instruction(payer_b58: str, ata_b58: str,
                                 owner_b58: str, mint_b58: str,
                                 token_program_b58: str = TOKEN_PROGRAM
                                 ) -> dict:
    """Associated Token Program: create the ATA (empty instruction
    data). Only ever used for the user's own token account for the
    mint being bought. The token program is the mint's own program
    (SPL Token for legacy coins, Token-2022 for create_v2 coins)."""
    return {
        "program_id": ASSOCIATED_TOKEN_PROGRAM,
        "accounts": [
            {"pubkey": payer_b58, "is_signer": True, "is_writable": True},
            {"pubkey": ata_b58, "is_signer": False, "is_writable": True},
            {"pubkey": owner_b58, "is_signer": False, "is_writable": False},
            {"pubkey": mint_b58, "is_signer": False, "is_writable": False},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": token_program_b58, "is_signer": False,
             "is_writable": False},
        ],
        "data": b"",
    }


def build_buy_instruction(accts: dict, token_amount: int,
                          max_sol_cost: int) -> dict:
    """Pump.fun buy_v3: `token_amount` base units out, at most
    `max_sol_cost` lamports in. The 17 accounts follow TRADE_V3.md
    exactly. `partial_fill` is omitted — the docs allow leaving it
    off, and the devnet simulation passes without it. For
    SOL-paired coins the quote mint is wrapped SOL; the
    associated quote accounts are derived addresses that the
    program does not use and that do not need to exist. The
    user_volume_accumulator is created by the program when
    missing, and the buyback fee goes to one of the global
    account's 8 buyback recipients."""
    data = (discriminator("buy_v3")
            + int(token_amount).to_bytes(8, "little")
            + int(max_sol_cost).to_bytes(8, "little"))
    return {
        "program_id": PUMP_PROGRAM,
        "accounts": [
            {"pubkey": accts["global"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["base_mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["quote_mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["base_token_program"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["quote_token_program"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["bonding_curve"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_base_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["associated_quote_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["user"], "is_signer": True,
             "is_writable": True},
            {"pubkey": accts["associated_base_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_quote_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["user_volume_accumulator"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["fee_config"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["buyback_fee_recipient"], "is_signer": False,
             "is_writable": True},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["event_authority"], "is_signer": False,
             "is_writable": False},
            {"pubkey": PUMP_PROGRAM, "is_signer": False,
             "is_writable": False},
        ],
        "data": data,
    }


def build_sell_instruction(accts: dict, token_amount: int,
                           min_sol_output: int) -> dict:
    """Pump.fun sell_v3: `token_amount` base units in, at least
    `min_sol_output` lamports out. The same 17 accounts as buy_v3,
    in the same order (TRADE_V3.md)."""
    data = (discriminator("sell_v3")
            + int(token_amount).to_bytes(8, "little")
            + int(min_sol_output).to_bytes(8, "little"))
    return {
        "program_id": PUMP_PROGRAM,
        "accounts": [
            {"pubkey": accts["global"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["base_mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["quote_mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["base_token_program"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["quote_token_program"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["bonding_curve"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_base_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["associated_quote_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["user"], "is_signer": True,
             "is_writable": True},
            {"pubkey": accts["associated_base_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_quote_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["user_volume_accumulator"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["fee_config"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["buyback_fee_recipient"], "is_signer": False,
             "is_writable": True},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["event_authority"], "is_signer": False,
             "is_writable": False},
            {"pubkey": PUMP_PROGRAM, "is_signer": False,
             "is_writable": False},
        ],
        "data": data,
    }
