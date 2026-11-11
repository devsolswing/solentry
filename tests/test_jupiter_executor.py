"""Real (mainnet) Jupiter swap executor: v0 transaction parsing,
the signing-bot allowlist, the mainnet gate, and the full
quote -> swap -> sign -> settle path in simulation.

No real SOL moves in any test: the RPC is fake, the wallet is a
throwaway keypair, Jupiter's HTTP answers are mocked, and every
signed transaction is decoded and checked — including a
cryptographic signature verification against the wallet key and a
decode of the fee transfer proving it goes to the Treasury dev
wallet for exactly the 1.9% fee.

Covers: parsing a REAL captured Jupiter swap transaction (base64
captured live 2026-11-11, SOL -> wZEC, throwaway fee payer);
parsing + allowlist accept/refuse on a synthetic v0 transaction
built to the same shape; signing; the gate OFF (refuses, nothing
sent) and ON (executes in simulation, mainnet only); the 1.9%
mainnet fee (all treasury, zero burn); stale-quote / price-impact /
bot-limit / insufficient-balance refusals; and settle_jupiter_fee.
"""

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey)

from helpers import (TEST_ENCRYPTION_KEY, TEST_FEE_RECIPIENT,
                     TEST_SESSION_SECRET, make_client, signup)
from test_executor import parse_signed_tx
from test_jupiter import REAL_QUOTE
from tradingbot import jupiter as jup
from tradingbot import jupiter_executor as jex
from tradingbot.config import Config
from tradingbot.fees import mainnet_fee_split_lamports
from tradingbot.security import encrypt_secret
from tradingbot.signing_bot import get_signing_bot
from tradingbot.solana import (
    ASSOCIATED_TOKEN_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM,
    base58_decode, base58_encode, compact_u16_encode,
    find_associated_token_address, generate_keypair)

FAKE_SIGNATURE = "5" * 43
BLOCKHASH = "11111111111111111111111111111111"
TABLE_PROGRAM = "AddressLookupTab1e1111111111111111111111111"

# A REAL Jupiter swap transaction, captured live 2026-11-11
# (SOL -> wZEC, 0.1 SOL in, 840,987 wZEC units out). The fee payer
# is a throwaway address with no real wallet behind it — it exists
# here only so the parser is tested against real Jupiter bytes.
# (Quote in /tmp/jup_swap_quote.json at capture time.)
REAL_SWAP_TX_B64 = (
    "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAQAIDjRkMzjQ759Xd3WTN40VKBvHTVpKwWat9aGbvRejKK1HCYwjqvXP2KHbtD/GOsljoPiPUqcBoxh7eVUjb4IAbPOcta2qhyzR9dzrsPiyY5wzMdYJMu7g0Zu2JS3BOnCft6IdmklnVTL3GrXKI61rcFwE4jsO/6OYROzRbst6fL4CtIufdf71g6uEG1xSzlOHi0jmVdnxTmiisCKZd7ty9qHfsLhf7Ff9NofcPgIyAbMytm5ggTyKwmR+JqgXUXARVQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwZGb+UhFzL/7K26csOb57yM5bvF9xJrLEObOkAAAAAEedVb8jHAbu50xW7OaBUH/bGy3qP0jlECsc2iVrwTjwbd9uHXZaGT2cvhRs7reawctIXtX1s3kTqM9YV+/wCpNZtjiLgkUYJsu4O7lCF6+WWGCGKfgWNPCiRJ3mtQnHZPS2wOQQj9KmokeOrgrMWdshu07fURwWLPYC0eDAkB+4yXJY9OJInxuz0QKRSODYMLWhOZ2v8QhASOe9jb6fhZtD/6J/XX9kp0wJsfKVh53ksJqzbfyd1RSzIap7OM5ehDt+0+g09lCZmJtn7hQQsi9KkUuXLo06I2xgIZSQzR+wgHAAUCwFwVAAcACQMEFwEAAAAAAAwGAAEAFgYJAQEGAgABDAIAAAAA4fUFAAAAAAkBAQERDAYAAgAZBgkBAQgrCQsAAQMEAhYZCAgNCBwLFBUTAwUJCRYaFx0KGA8bDhASEQUECwkZCRoXCinBIJszQdacgQcCAAAAsQFkAAHDZAECAOH1BQAAAAAb1QwAAAAAAGQAAAkDAQAAAQkCTMjLI7CznGfLD1MGVilPoWKVOl7jEP4QflZMJ4vhZHUFDgoTEQsGAgwQDRQPaqnD6zW9JDo+H+KAaN7anlH5OQXEZKvnbsMiY28t+NkDgIWEAoKH"
)
REAL_FEE_PAYER = "4XWpRuKMH5uVGiXkvV5PSKSBi36dBQVSy9QJ59Kvda7Q"

COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"


def _ins_bytes(prog_idx, accounts, data):
    return (bytes([prog_idx])
            + compact_u16_encode(len(accounts))
            + bytes(accounts)
            + compact_u16_encode(len(data)) + data)


def build_swap_tx(wallet, wsol_ata, out_ata, input_mint, output_mint,
                  side, swap_input_units, table_pubkey,
                  wrap_dest_idx=1, jupiter_prog_idx=3,
                  compute_price=50000, compute_limit=1400000,
                  token_decimals=8, extra_static=(),
                  extra_instructions=()):
    """A synthetic v0 Jupiter swap transaction in the exact shape
    the real one has (ComputeBudget, ATA creates, SOL wrap,
    syncNative, Jupiter swap, wSOL close), with the throwaway
    wallet as fee payer. `wrap_dest_idx` / `jupiter_prog_idx` /
    `compute_price` are the tamper knobs for the refuse tests.

    Static keys: [wallet, input ATA, output ATA, Jupiter, Token,
    ATA, System, ComputeBudget] (+ extras). One lookup table
    whose writable accounts are always [SOL mint, the other
    mint], in that order — and the instruction indexes are
    computed from the static-key count, so extra static keys
    never shift the mint indexes out from under the
    instructions."""
    static_keys = [wallet, wsol_ata, out_ata, jup.JUPITER_PROGRAM_ID,
                   TOKEN_PROGRAM, ASSOCIATED_TOKEN_PROGRAM,
                   SYSTEM_PROGRAM, COMPUTE_BUDGET_PROGRAM]
    static_keys.extend(extra_static)
    sol_idx = len(static_keys)
    other_idx = len(static_keys) + 1
    if side == "buy":
        instructions = [
            (7, [], b"\x02" + compute_limit.to_bytes(4, "little")),
            (7, [], b"\x03" + compute_price.to_bytes(8, "little")),
            (5, [0, 1, 0, sol_idx, 6, 4], b"\x01"),  # ATA create wSOL
            (6, [0, wrap_dest_idx],
             (2).to_bytes(4, "little")
             + swap_input_units.to_bytes(8, "little")),  # wrap
            (4, [1], b"\x11"),  # syncNative
            (jupiter_prog_idx, [1, 2, sol_idx, other_idx],
             b"\x01\x02\x03"),
            (5, [0, 2, 0, other_idx, 6, 4], b"\x01"),  # ATA create out
            (4, [1, 0, 0], b"\x09"),  # closeAccount wSOL
        ]
    else:
        instructions = [
            (7, [], b"\x02" + compute_limit.to_bytes(4, "little")),
            (7, [], b"\x03" + compute_price.to_bytes(8, "little")),
            (5, [0, 2, 0, sol_idx, 6, 4], b"\x01"),  # ATA create wSOL
            (4, [1, other_idx, 2, 0],  # transferChecked (input -> pool)
             b"\x0c" + swap_input_units.to_bytes(8, "little")
             + bytes([token_decimals])),
            (jupiter_prog_idx, [1, 2, sol_idx, other_idx],
             b"\x01\x02\x03"),
            (4, [2, 0, 0], b"\x09"),  # closeAccount wSOL
        ]
    instructions.extend(extra_instructions)
    message = (b"\x80" + bytes([1, 0, 0])
               + compact_u16_encode(len(static_keys))
               + b"".join(base58_decode(k) for k in static_keys)
               + base58_decode(BLOCKHASH)
               + compact_u16_encode(len(instructions))
               + b"".join(_ins_bytes(*ins) for ins in instructions)
               + compact_u16_encode(1)
               + base58_decode(table_pubkey)
               + compact_u16_encode(2) + bytes([0, 1])
               + compact_u16_encode(0))
    raw = compact_u16_encode(1) + b"\x00" * 64 + message
    return base64.b64encode(raw).decode("ascii")


class FakeJupiterRpc:
    """Mocked mainnet RPC: a wallet SOL balance, token balances,
    one address lookup table, and sends that "land". The swap send
    credits the output ATA with `fill_tokens` (buy) or the wallet
    with `sell_credit_lamports` (sell) so the executor can measure
    what the swap actually delivered."""

    def __init__(self, wallet_pubkey, out_ata, table_pubkey,
                 table_addresses, balance_lamports=10_000_000_000):
        self.wallet = wallet_pubkey
        self.out_ata = out_ata
        self.table = table_pubkey
        self.table_data = (b"\x00" * 56 + b"".join(
            base58_decode(a) for a in table_addresses))
        self.balance = balance_lamports
        self.sent = []
        self.fill_tokens = 0
        self.sell_credit_lamports = 0
        self._ata_balances = {}
        self._sends = 0

    def get_account_info(self, pubkey):
        if pubkey == self.table:
            return {"data": self.table_data, "lamports": 0,
                    "executable": False, "owner": TABLE_PROGRAM}
        return None

    def get_token_account_balance(self, pubkey):
        return self._ata_balances.get(pubkey, 0)

    def get_balance_lamports(self, pubkey):
        return self.balance

    def get_latest_blockhash(self):
        return BLOCKHASH

    def send_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        self._sends += 1
        if self._sends == 1:
            # The first send is the swap itself.
            if self.fill_tokens:
                self._ata_balances[self.out_ata] = (
                    self._ata_balances.get(self.out_ata, 0)
                    + self.fill_tokens)
            if self.sell_credit_lamports:
                self.balance += self.sell_credit_lamports
        return FAKE_SIGNATURE

    def confirm_transaction(self, signature, attempts=5,
                            delay_seconds=1.0):
        return True


class JupiterExecutorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        # Throwaway wallet (never funded, never on-chain) in place
        # of the signup wallet, so the executor signs with a known
        # key — the same pattern as test_executor.py.
        self.seed, self.pubkey, secret64 = generate_keypair()
        blob = encrypt_secret(secret64, self.cfg.encryption_key)
        self.db.execute(
            "UPDATE wallets SET pubkey = ?, encrypted_secret = ?, "
            "enc_version = 1 WHERE user_id = ?",
            (self.pubkey, blob, self.user_id))
        self.db.commit()
        self.support_id = get_signing_bot(
            self.db, self.user_id)["support_id"]
        _, self.table_pubkey, _ = generate_keypair()
        self.wsol_ata = find_associated_token_address(
            self.pubkey, jup.SOL_MINT)
        self.wzec_mint = jup.WRAPPED_MINTS["ZEC"]["mint"]
        self.wzec_ata = find_associated_token_address(
            self.pubkey, self.wzec_mint)

    def tearDown(self):
        self.tmp.cleanup()

    def _bot(self, mode="real", buy_size=20):
        return {"id": 1, "user_id": self.user_id, "name": "Trenches",
                "mode": mode, "buy_size_pct": buy_size,
                "stop_loss_pct": 7, "max_open_trades": 3}

    def _mainnet_cfg(self):
        """A mainnet Config with James's go-ahead flag ON."""
        return Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "mainnet",
            "DATABASE_PATH": self.cfg.database_path,
            "PAPER_START_CASH": "100",
            "FEE_RECIPIENT_PUBKEY": TEST_FEE_RECIPIENT,
            "SECURITY_CODE_DEV_ECHO": "1",
            "JUPITER_REAL_SWAPS": "1",
        })

    def _devnet_gate_cfg(self):
        """The flag ON but still devnet: the gate alone is not
        enough — mainnet is required too."""
        cfg = self._mainnet_cfg()
        cfg.network = "devnet"
        return cfg

    def _rpc(self, **kwargs):
        return FakeJupiterRpc(
            self.pubkey, self.wzec_ata, self.table_pubkey,
            [jup.SOL_MINT, self.wzec_mint], **kwargs)

    def _buy_tx(self, swap_lamports=98_100_000, **kwargs):
        return build_swap_tx(
            self.pubkey, self.wsol_ata, self.wzec_ata,
            jup.SOL_MINT, self.wzec_mint, "buy", swap_lamports,
            self.table_pubkey, **kwargs)

    def _sell_tx(self, token_base=100_000, **kwargs):
        return build_swap_tx(
            self.pubkey, self.wzec_ata, self.wsol_ata,
            self.wzec_mint, jup.SOL_MINT, "sell", token_base,
            self.table_pubkey, **kwargs)

    def _quote(self, side="buy", out_amount=838_000,
               out_amount_ui=0.00838, price_impact_pct=0.0,
               fetched_at=None):
        fetched = fetched_at or datetime.now(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"side": side, "symbol": "ZEC", "wrapped": True,
                "display_name": "Zcash",
                "in_amount": 98_100_000 if side == "buy" else 100_000,
                "out_amount": out_amount,
                "in_amount_ui": 0.1 if side == "buy" else 0.001,
                "out_amount_ui": out_amount_ui,
                "route_labels": ["BisonFi"],
                "price_impact_pct": price_impact_pct,
                "fetched_at": fetched, "source": "jupiter",
                "raw": dict(REAL_QUOTE)}

    def _verify_signature(self, signed_b64, message):
        raw = base64.b64decode(signed_b64)
        self.assertEqual(raw[0], 1)
        signature = raw[1:65]
        self.assertEqual(raw[65:], message)
        pub = Ed25519PublicKey.from_public_bytes(
            base58_decode(self.pubkey))
        pub.verify(signature, message)  # raises if bad


class TestParseVersionedTransaction(unittest.TestCase):
    def test_parse_real_jupiter_swap_tx(self):
        parsed = jex.parse_versioned_transaction(REAL_SWAP_TX_B64)
        self.assertEqual(parsed["fee_payer"], REAL_FEE_PAYER)
        self.assertEqual(parsed["num_required_signatures"], 1)
        self.assertEqual(len(parsed["static_keys"]), 14)
        self.assertEqual(len(parsed["instructions"]), 8)
        self.assertEqual(len(parsed["address_table_lookups"]), 2)
        self.assertEqual(parsed["message_bytes"][0], 0x80)
        self.assertEqual(list(parsed["message_bytes"][1:4]), [1, 0, 8])
        # One instruction IS the Jupiter swap (program in the
        # static keys), and the SOL wrap moves exactly 0.1 SOL.
        programs = [parsed["static_keys"][ins["program_id_index"]]
                    for ins in parsed["instructions"]]
        self.assertIn(jup.JUPITER_PROGRAM_ID, programs)
        wrap = next(ins for ins in parsed["instructions"]
                    if parsed["static_keys"][ins["program_id_index"]]
                    == SYSTEM_PROGRAM)
        self.assertEqual(
            int.from_bytes(wrap["data"][4:12], "little"), 100_000_000)

    def test_parse_rejects_bad_base64(self):
        with self.assertRaises(ValueError):
            jex.parse_versioned_transaction("not base64!!!")

    def test_parse_rejects_trailing_bytes(self):
        raw = base64.b64decode(REAL_SWAP_TX_B64) + b"\x00"
        with self.assertRaises(ValueError):
            jex.parse_versioned_transaction(
                base64.b64encode(raw).decode("ascii"))

    def test_parse_rejects_unsupported_version(self):
        raw = bytearray(base64.b64decode(REAL_SWAP_TX_B64))
        # The version byte is the first byte of the message, right
        # after the 1-byte sig count + 64-byte signature slot.
        raw[65] = 0x81
        with self.assertRaises(ValueError):
            jex.parse_versioned_transaction(
                base64.b64encode(bytes(raw)).decode("ascii"))


class TestVerifyJupiterTransaction(JupiterExecutorBase):
    def test_verify_accepts_synthetic_buy_tx(self):
        tx = self._buy_tx()
        parsed = jex.parse_versioned_transaction(tx)
        self.assertEqual(parsed["fee_payer"], self.pubkey)
        self.assertEqual(len(parsed["instructions"]), 8)
        self.assertEqual(len(parsed["address_table_lookups"]), 1)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        self.assertEqual(keys[8], jup.SOL_MINT)
        self.assertEqual(keys[9], self.wzec_mint)
        # No raise: the allowlist accepts the real shape.
        jex.verify_jupiter_transaction(
            parsed, keys, self.pubkey, jup.SOL_MINT, 98_100_000,
            "buy", self.wsol_ata)

    def test_verify_accepts_synthetic_sell_tx(self):
        tx = self._sell_tx()
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        jex.verify_jupiter_transaction(
            parsed, keys, self.pubkey, self.wzec_mint, 100_000,
            "sell", self.wsol_ata)

    def test_verify_refuses_swapped_jupiter_instruction(self):
        # The Jupiter instruction replaced by a Token instruction:
        # no Jupiter swap instruction at all.
        tx = self._buy_tx(jupiter_prog_idx=4)
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        with self.assertRaisesRegex(ValueError, "no Jupiter swap"):
            jex.verify_jupiter_transaction(
                parsed, keys, self.pubkey, jup.SOL_MINT, 98_100_000,
                "buy", self.wsol_ata)

    def test_verify_refuses_wrap_to_wrong_destination(self):
        # The wrap transfer sent to the output ATA instead of the
        # wSOL ATA.
        tx = self._buy_tx(wrap_dest_idx=2)
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        with self.assertRaisesRegex(ValueError,
                                    "not the SOL wrap"):
            jex.verify_jupiter_transaction(
                parsed, keys, self.pubkey, jup.SOL_MINT, 98_100_000,
                "buy", self.wsol_ata)

    def test_verify_refuses_wrong_fee_payer(self):
        tx = self._buy_tx()
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        _, other, _ = generate_keypair()
        with self.assertRaisesRegex(ValueError, "fee payer"):
            jex.verify_jupiter_transaction(
                parsed, keys, other, jup.SOL_MINT, 98_100_000,
                "buy", self.wsol_ata)

    def test_verify_refuses_compute_price_too_high(self):
        tx = self._buy_tx(compute_price=200_000)
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        with self.assertRaisesRegex(ValueError,
                                    "compute-unit price too high"):
            jex.verify_jupiter_transaction(
                parsed, keys, self.pubkey, jup.SOL_MINT, 98_100_000,
                "buy", self.wsol_ata)

    def test_verify_refuses_unknown_program(self):
        _, stranger, _ = generate_keypair()
        tx = self._buy_tx(
            extra_static=[stranger],
            extra_instructions=[(8, [], b"\x00")])
        parsed = jex.parse_versioned_transaction(tx)
        keys = jex.resolved_account_keys(parsed, self._rpc())
        with self.assertRaisesRegex(ValueError, "unexpected program"):
            jex.verify_jupiter_transaction(
                parsed, keys, self.pubkey, jup.SOL_MINT, 98_100_000,
                "buy", self.wsol_ata)


class TestSignVersionedTransaction(JupiterExecutorBase):
    def test_sign_verifies_cryptographically(self):
        tx = self._buy_tx()
        parsed = jex.parse_versioned_transaction(tx)
        signed_b64, signature = jex.sign_versioned_transaction(
            self.seed, tx, self.pubkey)
        self.assertEqual(signature,
                         base58_encode(base64.b64decode(signed_b64)[1:65]))
        self._verify_signature(signed_b64, parsed["message_bytes"])

    def test_sign_refuses_fee_payer_mismatch(self):
        tx = self._buy_tx()
        other_seed, _, _ = generate_keypair()
        with self.assertRaises(ValueError):
            jex.sign_versioned_transaction(other_seed, tx, self.pubkey)


class TestExecuteJupiterSwap(JupiterExecutorBase):
    def test_gate_off_refuses_plainly_and_sends_nothing(self):
        rpc = self._rpc()
        result = jex.execute_jupiter_swap(
            self._bot(), self.user_id, "ZEC", "buy", 0.1,
            self.db, self.cfg, rpc_client=rpc,
            quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not enabled yet", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_gate_on_but_devnet_refuses(self):
        rpc = self._rpc()
        result = jex.execute_jupiter_swap(
            self._bot(), self.user_id, "ZEC", "buy", 0.1,
            self.db, self._devnet_gate_cfg(), rpc_client=rpc,
            quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("mainnet", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_gate_on_mainnet_buy_executes(self):
        rpc = self._rpc()
        rpc.fill_tokens = 838_000
        with mock.patch.object(jex, "fetch_swap_transaction",
                               return_value=self._buy_tx()):
            result = jex.execute_jupiter_swap(
                self._bot(), self.user_id, "ZEC", "buy", 0.1,
                self.db, self._mainnet_cfg(), rpc_client=rpc,
                quote=self._quote())
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        self.assertAlmostEqual(result["coin_amount"], 0.00838)
        # The mainnet fee: 1.9% of 0.1 SOL, ALL to the treasury,
        # NO burn.
        self.assertEqual(result["fee_lamports"], 1_900_000)
        self.assertEqual(result["treasury_lamports"], 1_900_000)
        self.assertEqual(result["burn_lamports"], 0)
        self.assertEqual(result["pump_fee_lamports"], 0)
        self.assertAlmostEqual(result["fee_sol"], 0.0019)
        self.assertEqual(result["fee_status"], "paid")
        self.assertEqual(result["fee_signature"], FAKE_SIGNATURE)
        self.assertEqual(result["signing_bot"], self.support_id)
        # Two sends: the swap, then the fee transfer — and the fee
        # transfer really is a System transfer to the Treasury dev
        # wallet for exactly the fee.
        self.assertEqual(len(rpc.sent), 2)
        fee_tx = parse_signed_tx(rpc.sent[1])
        self.assertEqual(len(fee_tx["instructions"]), 1)
        fee_ins = fee_tx["instructions"][0]
        self.assertEqual(fee_ins["program_id"], SYSTEM_PROGRAM)
        self.assertEqual(fee_ins["accounts"],
                         [self.pubkey, TEST_FEE_RECIPIENT])
        self.assertEqual(int.from_bytes(fee_ins["data"][4:12], "little"),
                         1_900_000)
        # The swap itself was signed by the wallet key (a versioned
        # transaction — parsed with the v0 parser, not the legacy
        # one).
        swap_parsed = jex.parse_versioned_transaction(rpc.sent[0])
        self._verify_signature(rpc.sent[0], swap_parsed["message_bytes"])

    def test_gate_on_mainnet_sell_executes(self):
        rpc = self._rpc()
        rpc._ata_balances[self.wzec_ata] = 1_000_000
        rpc.sell_credit_lamports = 11_713_224
        quote = self._quote(side="sell", out_amount=11_713_224,
                            out_amount_ui=0.011713224)
        with mock.patch.object(jex, "fetch_swap_transaction",
                               return_value=self._sell_tx()):
            result = jex.execute_jupiter_swap(
                self._bot(), self.user_id, "ZEC", "sell", 0.001,
                self.db, self._mainnet_cfg(), rpc_client=rpc,
                quote=quote)
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        self.assertAlmostEqual(result["coin_amount"], 0.011713224)
        # 1.9% of the 11,713,224-lamport proceeds, all treasury.
        self.assertEqual(result["fee_lamports"], 222_551)
        self.assertEqual(result["treasury_lamports"], 222_551)
        self.assertEqual(result["burn_lamports"], 0)
        self.assertEqual(result["fee_status"], "paid")
        self.assertEqual(len(rpc.sent), 2)

    def test_paper_bot_refuses(self):
        rpc = self._rpc()
        result = jex.execute_jupiter_swap(
            self._bot(mode="paper"), self.user_id, "ZEC", "buy", 0.1,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not in real mode", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_buy_over_limit_refuses(self):
        rpc = self._rpc()
        result = jex.execute_jupiter_swap(
            self._bot(buy_size=20), self.user_id, "ZEC", "buy", 5.0,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("exceeds bot buy size", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_buy_insufficient_fee_reserve_refuses(self):
        rpc = self._rpc(balance_lamports=100_500_000)
        result = jex.execute_jupiter_swap(
            self._bot(buy_size=100), self.user_id, "ZEC", "buy", 0.1,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("insufficient SOL left for fees", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_stale_quote_refuses(self):
        rpc = self._rpc()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = jex.execute_jupiter_swap(
            self._bot(), self.user_id, "ZEC", "buy", 0.1,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote(fetched_at=old))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("stale", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_price_impact_refuses(self):
        rpc = self._rpc()
        result = jex.execute_jupiter_swap(
            self._bot(), self.user_id, "ZEC", "buy", 0.1,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote(price_impact_pct=6.0))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("too high", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_tampered_swap_refuses_and_signs_nothing(self):
        rpc = self._rpc()
        rpc.fill_tokens = 838_000
        with mock.patch.object(jex, "fetch_swap_transaction",
                               return_value=self._buy_tx(
                                   wrap_dest_idx=2)):
            result = jex.execute_jupiter_swap(
                self._bot(), self.user_id, "ZEC", "buy", 0.1,
                self.db, self._mainnet_cfg(), rpc_client=rpc,
                quote=self._quote())
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not the SOL wrap", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_sell_over_held_balance_refuses(self):
        rpc = self._rpc()
        rpc._ata_balances[self.wzec_ata] = 50_000  # < 100,000 needed
        result = jex.execute_jupiter_swap(
            self._bot(), self.user_id, "ZEC", "sell", 0.001,
            self.db, self._mainnet_cfg(), rpc_client=rpc,
            quote=self._quote(side="sell", out_amount=11_713_224,
                              out_amount_ui=0.011713224))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("insufficient token balance", result["reason"])
        self.assertEqual(rpc.sent, [])


class TestSettleJupiterFee(JupiterExecutorBase):
    def test_settle_gate_off_skips(self):
        rpc = self._rpc()
        result = jex.settle_jupiter_fee(
            self.db, self.user_id, 1_900_000, self.cfg,
            rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not enabled yet", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_settle_pays_when_gate_open(self):
        rpc = self._rpc()
        result = jex.settle_jupiter_fee(
            self.db, self.user_id, 1_900_000, self._mainnet_cfg(),
            rpc_client=rpc)
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["fee_signature"], FAKE_SIGNATURE)
        self.assertEqual(len(rpc.sent), 1)
        fee_tx = parse_signed_tx(rpc.sent[0])
        fee_ins = fee_tx["instructions"][0]
        self.assertEqual(fee_ins["program_id"], SYSTEM_PROGRAM)
        self.assertEqual(fee_ins["accounts"],
                         [self.pubkey, TEST_FEE_RECIPIENT])
        self.assertEqual(int.from_bytes(fee_ins["data"][4:12], "little"),
                         1_900_000)


class TestMainnetFeeMath(unittest.TestCase):
    def test_mainnet_fee_is_1_9_percent_all_treasury_no_burn(self):
        split = mainnet_fee_split_lamports(100_000_000)
        self.assertEqual(split["fee_lamports"], 1_900_000)
        self.assertEqual(split["treasury_lamports"], 1_900_000)
        self.assertEqual(split["burn_lamports"], 0)
        split = mainnet_fee_split_lamports(200_000_000)
        self.assertEqual(split["fee_lamports"], 3_800_000)
        self.assertEqual(split["treasury_lamports"], 3_800_000)
        self.assertEqual(split["burn_lamports"], 0)


if __name__ == "__main__":
    unittest.main()
