"""Pump.fun program (devnet): account layouts, quote math, and
instruction builders for bonding-curve buys and sells.

Verified against devnet RPC (2026-11-09): the program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P is executable, and the
global account at PDA ["global"] parses to the known constants
(virtual token reserves 1,073,000,000,000,000; virtual SOL reserves
30 SOL; token supply 1,000,000,000,000,000; fee 95 bps).

Instruction discriminators are the first 8 bytes of
SHA-256("global:buy") / SHA-256("global:sell") — buy is
66063d1201daebea, sell is 33e685a4017f83ad, matching the program's
published IDL. Account order and flags follow the IDL exactly.

Nothing in this module signs or sends anything — it only builds
instruction data. Signing happens in executor.py, which also
enforces that ONLY these instructions are ever built (no transfers,
no withdrawals, no other destinations).
"""

from __future__ import annotations

import hashlib

from .solana import (ASSOCIATED_TOKEN_PROGRAM, RENT_SYSVAR, SYSTEM_PROGRAM,
                     TOKEN_PROGRAM, base58_decode, base58_encode,
                     find_associated_token_address, find_program_address)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
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
    points (u64)."""
    if len(data) < 113:
        raise ValueError("global account too short")
    return {
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
    }


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


def buy_quote(curve: dict, fee_bps: int, sol_lamports: int) -> int:
    """Token base units out for `sol_lamports` in, after the program
    fee. Integer math only. Returns 0 when the amount is too small
    to buy anything."""
    if sol_lamports <= 0:
        return 0
    fee = sol_lamports * fee_bps // 10000
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
    gross = vs - (vt * vs) // (vt + token_base_units)
    fee = gross * fee_bps // 10000
    return max(0, gross - fee)


def derive_trade_accounts(mint_b58: str, user_b58: str,
                          fee_recipient_b58: str) -> dict:
    """All accounts a pump.fun buy/sell needs, derived from the mint
    and the user. The fee recipient comes from the live global
    account — never hardcoded."""
    global_addr, _ = global_pda()
    event_authority, _ = event_authority_pda()
    curve_addr, _ = bonding_curve_pda(mint_b58)
    return {
        "global": global_addr,
        "fee_recipient": fee_recipient_b58,
        "mint": mint_b58,
        "bonding_curve": curve_addr,
        "associated_bonding_curve": find_associated_token_address(
            curve_addr, mint_b58),
        "associated_user": find_associated_token_address(
            user_b58, mint_b58),
        "user": user_b58,
        "event_authority": event_authority,
    }


def build_create_ata_instruction(payer_b58: str, ata_b58: str,
                                 owner_b58: str, mint_b58: str) -> dict:
    """Associated Token Program: create the ATA (empty instruction
    data). Only ever used for the user's own token account for the
    mint being bought."""
    return {
        "program_id": ASSOCIATED_TOKEN_PROGRAM,
        "accounts": [
            {"pubkey": payer_b58, "is_signer": True, "is_writable": True},
            {"pubkey": ata_b58, "is_signer": False, "is_writable": True},
            {"pubkey": owner_b58, "is_signer": False, "is_writable": False},
            {"pubkey": mint_b58, "is_signer": False, "is_writable": False},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": TOKEN_PROGRAM, "is_signer": False,
             "is_writable": False},
        ],
        "data": b"",
    }


def build_buy_instruction(accts: dict, token_amount: int,
                          max_sol_cost: int) -> dict:
    """Pump.fun buy: `token_amount` base units out, at most
    `max_sol_cost` lamports in. Account order and flags follow the
    program IDL exactly."""
    data = (discriminator("buy") + int(token_amount).to_bytes(8, "little")
            + int(max_sol_cost).to_bytes(8, "little"))
    return {
        "program_id": PUMP_PROGRAM,
        "accounts": [
            {"pubkey": accts["global"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["fee_recipient"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["bonding_curve"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["associated_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["user"], "is_signer": True,
             "is_writable": True},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": TOKEN_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": RENT_SYSVAR, "is_signer": False,
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
    """Pump.fun sell: `token_amount` base units in, at least
    `min_sol_output` lamports out. Account order and flags follow
    the program IDL exactly."""
    data = (discriminator("sell") + int(token_amount).to_bytes(8, "little")
            + int(min_sol_output).to_bytes(8, "little"))
    return {
        "program_id": PUMP_PROGRAM,
        "accounts": [
            {"pubkey": accts["global"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["fee_recipient"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["mint"], "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["bonding_curve"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["associated_bonding_curve"],
             "is_signer": False, "is_writable": True},
            {"pubkey": accts["associated_user"], "is_signer": False,
             "is_writable": True},
            {"pubkey": accts["user"], "is_signer": True,
             "is_writable": True},
            {"pubkey": SYSTEM_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": ASSOCIATED_TOKEN_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": TOKEN_PROGRAM, "is_signer": False,
             "is_writable": False},
            {"pubkey": accts["event_authority"], "is_signer": False,
             "is_writable": False},
            {"pubkey": PUMP_PROGRAM, "is_signer": False,
             "is_writable": False},
        ],
        "data": data,
    }
