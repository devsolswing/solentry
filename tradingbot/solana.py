"""Solana primitives: pure Python, no web3 dependency.

Ed25519 + base58 + legacy-transaction building follow the pattern proven
in the live engine's tx_builder.py / engine.py (RFC 8032 signing,
compact-u16 message layout). No trading logic is copied from there — this
module only knows how to make a keypair, validate an address, and build /
sign a plain SOL transfer.

Secret keys live here only as transient bytes passed in by the caller.
Nothing in this module logs or stores them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.request

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
RENT_SYSVAR = "SysvarRent111111111111111111111111111111111"
LAMPORTS_PER_SOL = 1_000_000_000
# A token account is 165 bytes — the rent-exempt minimum for one is
# queried live (getMinimumBalanceForRentExemption), never hardcoded.
TOKEN_ACCOUNT_SIZE = 165

# ---------------------------------------------------------------------------
# base58 (Bitcoin alphabet, as Phantom exports)
# ---------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big") if data else 0
    if n == 0:
        return "1" * len(data)
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def base58_decode(text: str) -> bytes:
    if not text:
        raise ValueError("empty base58 string")
    n = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise ValueError("invalid base58 character")
        n = n * 58 + _B58_INDEX[ch]
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in text:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full


def is_valid_address(text: str) -> bool:
    """A valid Solana address is 32 bytes when base58-decoded."""
    if not isinstance(text, str) or not (32 <= len(text) <= 44):
        return False
    try:
        return len(base58_decode(text)) == 32
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Ed25519 (RFC 8032)
# ---------------------------------------------------------------------------
_ED_Q = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_Q - 2, _ED_Q) % _ED_Q
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = x * _ED_I % _ED_Q
    if x % 2 != 0:
        x = _ED_Q - x
    return x


_ED_BY = 4 * pow(5, _ED_Q - 2, _ED_Q) % _ED_Q
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = [_ED_BX % _ED_Q, _ED_BY % _ED_Q]


def _ed_add(point_p, point_q):
    x1, y1 = point_p
    x2, y2 = point_q
    denom = pow(1 + _ED_D * x1 * x2 * y1 * y2 % _ED_Q, _ED_Q - 2, _ED_Q)
    x3 = (x1 * y2 + x2 * y1) * denom % _ED_Q
    denom2 = pow(1 - _ED_D * x1 * x2 * y1 * y2 % _ED_Q, _ED_Q - 2, _ED_Q)
    y3 = (y1 * y2 + x1 * x2) * denom2 % _ED_Q
    return [x3, y3]


def _ed_scalarmult(point, scalar: int):
    if scalar == 0:
        return [0, 1]
    half = _ed_scalarmult(point, scalar // 2)
    half = _ed_add(half, half)
    if scalar & 1:
        half = _ed_add(half, point)
    return half


def _encode_point(point) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_publickey_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    hashed = bytearray(hashlib.sha512(seed).digest())
    hashed[0] &= 248
    hashed[31] &= 127
    hashed[31] |= 64
    scalar = int.from_bytes(hashed[:32], "little")
    point = _ed_scalarmult(_ED_BASE, scalar)
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    hashed = hashlib.sha512(seed).digest()
    a_bytes = bytearray(hashed[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    a = int.from_bytes(a_bytes, "little")
    prefix = hashed[32:]
    public = _encode_point(_ed_scalarmult(_ED_BASE, a))
    r = int.from_bytes(
        hashlib.sha512(prefix + message).digest(), "little") % _ED_L
    r_point = _encode_point(_ed_scalarmult(_ED_BASE, r))
    h = int.from_bytes(
        hashlib.sha512(r_point + public + message).digest(),
        "little") % _ED_L
    s = (r + h * a) % _ED_L
    return r_point + s.to_bytes(32, "little")


def generate_keypair() -> tuple[bytearray, str, bytearray]:
    """Fresh random Solana keypair.

    Returns (seed_32, pubkey_base58, secret_64) where secret_64 is
    seed + pubkey, the form Phantom exports. Seed and secret are
    MUTABLE bytearrays so the caller can wipe them in place with
    wipe_secret the moment they are encrypted — never hold them as
    plain bytes past the encrypt call.
    """
    seed = os.urandom(32)
    pubkey = ed25519_publickey_from_seed(seed)
    return bytearray(seed), base58_encode(pubkey), bytearray(seed + pubkey)


def publickey_from_secret(secret: bytes) -> bytes:
    """Public key for a 64-byte secret (seed+pubkey) or 32-byte seed."""
    if len(secret) == 64:
        derived = ed25519_publickey_from_seed(secret[:32])
        if derived != secret[32:]:
            raise ValueError("secret key public-key half does not match seed")
        return secret[32:]
    if len(secret) == 32:
        return ed25519_publickey_from_seed(secret)
    raise ValueError("secret must be 32 or 64 bytes")


# ---------------------------------------------------------------------------
# Legacy transaction building (SystemProgram transfer only)
# ---------------------------------------------------------------------------
def compact_u16_encode(value: int) -> bytes:
    out = bytearray()
    rem = value
    while True:
        byte = rem & 0x7F
        rem >>= 7
        if rem == 0:
            out.append(byte)
            break
        out.append(byte | 0x80)
    return bytes(out)


def build_transfer_message(fee_payer_b58: str, to_b58: str,
                           lamports: int, blockhash_b58: str) -> bytes:
    """Legacy message for a single SystemProgram transfer.

    Instruction data: [2, 0, 0, 0] (transfer discriminator) +
    lamports as u64 little-endian. Accounts: fee payer (signer,
    writable) first, then destination (writable), then the program id.
    """
    if lamports <= 0:
        raise ValueError("lamports must be positive")
    instruction = {
        "program_id": SYSTEM_PROGRAM,
        "accounts": [fee_payer_b58, to_b58],
        "data": bytes([2, 0, 0, 0]) + lamports.to_bytes(8, "little"),
    }
    program_ids = [SYSTEM_PROGRAM]
    account_keys = [fee_payer_b58]
    for acct in instruction["accounts"]:
        if acct not in account_keys:
            account_keys.append(acct)
    for pid in program_ids:
        if pid not in account_keys:
            account_keys.append(pid)
    header = bytes([1, 0, len(program_ids)])
    out = bytearray(header)
    out += compact_u16_encode(len(account_keys))
    for key in account_keys:
        out += base58_decode(key)
    out += base58_decode(blockhash_b58)
    out += compact_u16_encode(1)
    prog_idx = account_keys.index(instruction["program_id"])
    out.append(prog_idx)
    out += compact_u16_encode(len(instruction["accounts"]))
    for acct in instruction["accounts"]:
        out.append(account_keys.index(acct))
    out += compact_u16_encode(len(instruction["data"]))
    out += instruction["data"]
    return bytes(out)


def build_signed_transfer(seed: bytes, fee_payer_b58: str, to_b58: str,
                          lamports: int, blockhash_b58: str) -> tuple[str, str]:
    """Build + sign a transfer. Returns (signed_tx_base64, signature_base58).

    Verifies the derived public key matches the fee payer before signing.
    """
    derived = base58_encode(ed25519_publickey_from_seed(seed))
    if derived != fee_payer_b58:
        raise ValueError("fee payer mismatch")
    message = build_transfer_message(fee_payer_b58, to_b58, lamports,
                                     blockhash_b58)
    signature = ed25519_sign(seed, message)
    serialized = compact_u16_encode(1) + signature + message
    return base64.b64encode(serialized).decode("ascii"), base58_encode(signature)


# ---------------------------------------------------------------------------
# Program-derived addresses (PDAs) and generic legacy messages
# ---------------------------------------------------------------------------
def _is_on_curve(candidate: bytes) -> bool:
    """True when `candidate` (32 bytes) decompresses to a point on the
    Ed25519 curve — i.e. it is NOT a valid program-derived address."""
    y = int.from_bytes(candidate, "little") & ((1 << 255) - 1)
    sign = (candidate[31] >> 7) & 1
    x = _ed_xrecover(y)
    if sign and x != 0:
        x = _ED_Q - x
    return (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_Q == 0


def find_program_address(seeds: list, program_id: str) -> tuple[str, int]:
    """Solana createProgramAddress: SHA-256 over seeds + bump +
    program id + "ProgramDerivedAddress", first bump (255 down) whose
    hash is off the curve. Returns (address_base58, bump).

    Verified against the pump.fun global account: seeds [b"global"]
    with the pump.fun program give 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf.
    """
    program_bytes = base58_decode(program_id)
    for bump in range(255, -1, -1):
        data = b"".join(seeds) + bytes([bump]) + program_bytes \
            + b"ProgramDerivedAddress"
        digest = hashlib.sha256(data).digest()
        if not _is_on_curve(digest):
            return base58_encode(digest), bump
    raise ValueError("no program-derived address found")


def find_associated_token_address(owner_b58: str, mint_b58: str,
                                  token_program_b58: str = TOKEN_PROGRAM
                                  ) -> str:
    """The associated token account for (owner, mint) under the
    given token program (SPL Token by default; Token-2022 for
    create_v2 mints)."""
    address, _bump = find_program_address(
        [base58_decode(owner_b58), base58_decode(token_program_b58),
         base58_decode(mint_b58)],
        ASSOCIATED_TOKEN_PROGRAM)
    return address


def build_token_transfer_instruction(source_ata_b58: str,
                                     dest_ata_b58: str, authority_b58: str,
                                     amount_base_units: int,
                                     token_program_b58: str = TOKEN_PROGRAM
                                     ) -> dict:
    """One SPL Token `transfer` instruction (build_message format).

    Data: [3] (transfer discriminator) + amount as u64 little-endian.
    Accounts: the source token account (writable), the destination
    token account (writable), then the owner/authority (signer).
    Works for the legacy Token program and Token-2022 alike — the
    caller passes the mint's own token program.
    """
    if amount_base_units <= 0:
        raise ValueError("token amount must be positive")
    return {
        "program_id": token_program_b58,
        "accounts": [
            {"pubkey": source_ata_b58, "is_signer": False,
             "is_writable": True},
            {"pubkey": dest_ata_b58, "is_signer": False,
             "is_writable": True},
            {"pubkey": authority_b58, "is_signer": True,
             "is_writable": False},
        ],
        "data": bytes([3]) + amount_base_units.to_bytes(8, "little"),
    }


def build_create_associated_token_account_instruction(
        payer_b58: str, ata_b58: str, owner_b58: str, mint_b58: str,
        token_program_b58: str = TOKEN_PROGRAM) -> dict:
    """One Associated Token Program `create` instruction
    (build_message format).

    Data: empty (the create instruction takes no data). Accounts,
    in the program's expected order: the funding account (signer,
    writable), the associated token account to create (writable),
    the wallet that will own it (readonly), the mint (readonly),
    the System program, the token program, and the Rent sysvar
    (all readonly).
    """
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
            {"pubkey": RENT_SYSVAR, "is_signer": False,
             "is_writable": False},
        ],
        "data": b"",
    }


def build_transfer_instruction(fee_payer_b58: str, to_b58: str,
                               lamports: int) -> dict:
    """One SystemProgram transfer instruction (build_message format).

    Data: [2, 0, 0, 0] (transfer discriminator) + lamports u64 LE.
    Accounts: the fee payer (signer, writable), then the
    destination (writable). The executor is the only caller, and it
    only ever builds this for the 3.65% trading fee to the fee
    recipient — see executor._check_instructions_only_allowed.
    """
    if lamports <= 0:
        raise ValueError("lamports must be positive")
    return {
        "program_id": SYSTEM_PROGRAM,
        "accounts": [
            {"pubkey": fee_payer_b58, "is_signer": True,
             "is_writable": True},
            {"pubkey": to_b58, "is_signer": False, "is_writable": True},
        ],
        "data": bytes([2, 0, 0, 0]) + lamports.to_bytes(8, "little"),
    }


def build_message(fee_payer_b58: str, instructions: list,
                  blockhash_b58: str) -> bytes:
    """Generic legacy message for any list of instructions.

    Each instruction is {"program_id": str, "accounts": [{"pubkey",
    "is_signer", "is_writable"}], "data": bytes}. Account keys are
    ordered canonically: fee payer first, then other signers, then
    writable non-signers, then readonly non-signers (program ids last).
    For a single SystemProgram transfer this reproduces
    build_transfer_message exactly.
    """
    metas: dict = {}
    order: list = []

    def _add(pubkey: str, is_signer: bool, is_writable: bool) -> None:
        if pubkey not in metas:
            metas[pubkey] = {"is_signer": bool(is_signer),
                             "is_writable": bool(is_writable)}
            order.append(pubkey)
        else:
            metas[pubkey]["is_signer"] |= bool(is_signer)
            metas[pubkey]["is_writable"] |= bool(is_writable)

    _add(fee_payer_b58, True, True)
    for ins in instructions:
        for acct in ins["accounts"]:
            _add(acct["pubkey"], acct.get("is_signer", False),
                 acct.get("is_writable", False))
    program_ids: list = []
    for ins in instructions:
        if ins["program_id"] not in program_ids:
            program_ids.append(ins["program_id"])
    for pid in program_ids:
        _add(pid, False, False)
    signers = [k for k in order if metas[k]["is_signer"]]
    writable = [k for k in order
                if not metas[k]["is_signer"] and metas[k]["is_writable"]]
    readonly = [k for k in order
                if not metas[k]["is_signer"] and not metas[k]["is_writable"]]
    account_keys = signers + writable + readonly
    header = bytes([
        len(signers),
        sum(1 for k in signers if not metas[k]["is_writable"]),
        len(readonly),
    ])
    out = bytearray(header)
    out += compact_u16_encode(len(account_keys))
    for key in account_keys:
        out += base58_decode(key)
    out += base58_decode(blockhash_b58)
    out += compact_u16_encode(len(instructions))
    for ins in instructions:
        out.append(account_keys.index(ins["program_id"]))
        out += compact_u16_encode(len(ins["accounts"]))
        for acct in ins["accounts"]:
            out.append(account_keys.index(acct["pubkey"]))
        out += compact_u16_encode(len(ins["data"]))
        out += ins["data"]
    return bytes(out)


def sign_message(seed: bytes, message: bytes) -> tuple[str, str]:
    """Sign a message. Returns (signed_tx_base64, signature_base58)."""
    signature = ed25519_sign(seed, message)
    serialized = compact_u16_encode(1) + signature + message
    return base64.b64encode(serialized).decode("ascii"), base58_encode(signature)


# ---------------------------------------------------------------------------
# Minimal Solana JSON-RPC client (stdlib; injectable for tests)
# ---------------------------------------------------------------------------
class RpcError(Exception):
    pass


class SolanaRpcClient:
    def __init__(self, rpc_url: str, http_post=None, timeout: int = 20):
        self.rpc_url = rpc_url
        self.http_post = http_post or self._default_post
        self.timeout = timeout

    def _default_post(self, url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "trading-bot-app"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call(self, method, params):
        try:
            resp = self.http_post(
                self.rpc_url,
                {"jsonrpc": "2.0", "id": 1, "method": method,
                 "params": params})
        except Exception as exc:
            raise RpcError("rpc request failed") from exc
        if not isinstance(resp, dict):
            raise RpcError(
                f"rpc error response: {method}: not a JSON object")
        err = resp.get("error")
        if err is not None:
            # Surface the node's real error (method + code +
            # message) instead of a bare "rpc error response" — a
            # removed method used to fail with no usable detail.
            if isinstance(err, dict):
                detail = f"{err.get('code')} {err.get('message')}"
            else:
                detail = str(err)
            raise RpcError(
                f"rpc error response: {method}: {detail}"[:200])
        return resp.get("result")

    def get_balance_lamports(self, pubkey: str) -> int:
        result = self._call("getBalance", [pubkey])
        if not result or "value" not in result:
            raise RpcError("bad getBalance result")
        return int(result["value"])

    def get_account_info(self, pubkey: str) -> dict | None:
        """Account data as {"data": bytes, "lamports": int,
        "executable": bool, "owner": str}, or None when the account
        does not exist."""
        result = self._call("getAccountInfo",
                            [pubkey, {"encoding": "base64"}])
        if not result or not result.get("value"):
            return None
        value = result["value"]
        raw = value.get("data") or []
        if not raw:
            return None
        return {"data": base64.b64decode(raw[0]),
                "lamports": int(value.get("lamports") or 0),
                "executable": bool(value.get("executable")),
                "owner": value.get("owner") or ""}

    def get_token_account_balance(self, pubkey: str) -> int:
        """Token balance in base units (smallest denomination)."""
        result = self._call("getTokenAccountBalance", [pubkey])
        if not result or "value" not in result:
            raise RpcError("bad getTokenAccountBalance result")
        return int(result["value"]["amount"])

    def get_token_account(self, pubkey: str) -> dict | None:
        """A token account's balance and decimals, as {"amount":
        int (base units), "decimals": int} — or None when the
        account does not exist on-chain. A missing account is a
        normal answer (an address that never held this token has
        no token account); any other RPC failure is raised, so a
        down RPC is never mistaken for an empty balance."""
        try:
            result = self._call("getTokenAccountBalance", [pubkey])
        except RpcError as exc:
            if "could not find account" in str(exc):
                return None
            raise
        if not result or "value" not in result:
            raise RpcError("bad getTokenAccountBalance result")
        value = result["value"]
        return {"amount": int(value["amount"]),
                "decimals": int(value["decimals"])}

    def get_minimum_balance_for_rent_exemption(self, data_size: int) -> int:
        """The rent-exempt minimum for an account of `data_size`
        bytes, in lamports — queried live, never hardcoded."""
        result = self._call("getMinimumBalanceForRentExemption",
                            [data_size])
        if result is None:
            raise RpcError(
                "bad getMinimumBalanceForRentExemption result")
        return int(result)

    def get_token_largest_accounts(self, mint: str) -> list:
        """Largest token accounts for a mint (public RPC, no key).

        Returns the raw `value` list of {"address", "amount",
        "decimals", "uiAmount", ...} — the caller sums the top 10.
        Raises RpcError on any bad response.
        """
        result = self._call("getTokenLargestAccounts", [mint])
        if not result or "value" not in result:
            raise RpcError("bad getTokenLargestAccounts result")
        return result["value"]

    def get_token_supply(self, mint: str) -> int:
        """Total token supply for a mint, in base units (public RPC,
        no key). Raises RpcError on any bad response."""
        result = self._call("getTokenSupply", [mint])
        if not result or "value" not in result:
            raise RpcError("bad getTokenSupply result")
        return int(result["value"]["amount"])

    def get_latest_blockhash(self) -> str:
        result = self._call("getLatestBlockhash", [])
        blockhash = (result or {}).get("value", {}).get("blockhash")
        if not blockhash:
            raise RpcError("no blockhash")
        return blockhash

    # NOTE: there is deliberately no get_recent_blockhash here —
    # the devnet RPC removed getRecentBlockhash (returns -32601
    # "Method not found"); getLatestBlockhash is the replacement.

    def send_transaction(self, signed_b64: str) -> str:
        result = self._call(
            "sendTransaction",
            [signed_b64, {"encoding": "base64",
                          "preflightCommitment": "confirmed"}])
        if not result:
            raise RpcError("sendTransaction returned nothing")
        return result

    def transaction_status(self, signature: str) -> str | None:
        """One status check for a transaction: "confirmed" when the
        chain reports it confirmed or finalized, "failed" when the
        chain reports a processing error, None when the chain has
        not seen it yet — or when the status check itself failed
        transiently. A wobbly RPC is never mistaken for a failed
        transaction: the caller keeps polling on None."""
        try:
            result = self._call("getSignatureStatuses", [[signature]])
        except Exception:
            return None
        if result and result[0]:
            status = result[0]
            if status.get("err") is not None:
                return "failed"
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return "confirmed"
        return None

    def confirm_transaction(self, signature: str, attempts: int = 15,
                            delay_seconds: float = 2.0) -> bool:
        """Wait until the transaction is confirmed, up to ~30s. A
        status of "failed" ends the wait at once (the chain
        processed it and it did not succeed — it can never become
        confirmed). A missing status or a transient RPC error just
        keeps the wait going: a slow or lagging devnet RPC must
        never be reported as a failed trade."""
        for _ in range(attempts):
            status = self.transaction_status(signature)
            if status == "confirmed":
                return True
            if status == "failed":
                return False
            time.sleep(delay_seconds)
        return False


def sol_to_lamports(sol: float) -> int:
    return int(round(sol * LAMPORTS_PER_SOL))


def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL
