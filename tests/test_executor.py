"""Real (devnet) executor: pump.fun buy/sell signing with throwaway
keys and a mocked RPC. NO real SOL moves in any test — the RPC is
fake, the wallet is a throwaway keypair, and every signed
transaction is decoded and checked, including a cryptographic
signature verification against the wallet key.

Covers: buy and sell end-to-end against mocks, guard refusals
(mainnet, paper bot, non-mint coin, over-limit amount, no bonding
curve, migrated curve, fee reserve, missing wallet), the
never-arbitrary-transfers destination restriction, and a
rule-governed real trade through the silent loop.
"""

import base64
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey)

from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, make_client,
                     signup)
from tradingbot import executor as executor_mod
from tradingbot import pumpfun
from tradingbot.config import Config
from tradingbot.executor import (_check_instructions_only_allowed,
                                 execute_real_trade)
from tradingbot.security import encrypt_secret
from tradingbot.signing_bot import get_signing_bot
from tradingbot.silent_loop import run_once
from tradingbot.solana import (SYSTEM_PROGRAM, TOKEN_PROGRAM, base58_decode,
                               base58_encode, generate_keypair)

BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")
FAKE_SIGNATURE = "5" * 43
BLOCKHASH = "11111111111111111111111111111111"


def make_global(fee_bps=95, fee_recipient=None):
    """113-byte pump.fun global account with the known devnet
    constants."""
    data = bytearray(113)
    data[8] = 1
    data[41:73] = base58_decode(
        fee_recipient or "11111111111111111111111111111111")
    data[73:81] = (1_073_000_000_000_000).to_bytes(8, "little")
    data[81:89] = (300_000_000).to_bytes(8, "little")
    data[89:97] = (793_100_000_000_000).to_bytes(8, "little")
    data[97:105] = (1_000_000_000_000_000).to_bytes(8, "little")
    data[105:113] = fee_bps.to_bytes(8, "little")
    return bytes(data)


def make_curve(complete=False):
    """49-byte bonding-curve account (not complete unless asked)."""
    data = bytearray(49)
    data[8:16] = (1_073_000_000_000_000).to_bytes(8, "little")
    data[16:24] = (300_000_000).to_bytes(8, "little")
    data[24:32] = (793_100_000_000_000).to_bytes(8, "little")
    data[32:40] = (0).to_bytes(8, "little")
    data[40:48] = (1_000_000_000_000_000).to_bytes(8, "little")
    data[48] = 1 if complete else 0
    return bytes(data)


class FakePumpRpc:
    """Mocked devnet RPC that models a pump.fun token: a global
    account, a bonding curve, a wallet SOL balance, and token
    balances. Sending a transaction credits the user's ATA with
    `fill_tokens` (the test sets this to the quoted amount)."""

    def __init__(self, wallet_pubkey, mint, balance_lamports=10_000_000_000,
                 curve=None, ata_balance=0):
        self.wallet = wallet_pubkey
        self.mint = mint
        self.balance = balance_lamports
        self.sent = []
        self.fill_tokens = 0
        self._accounts = {}
        self._ata_balances = {}
        global_addr, _ = pumpfun.global_pda()
        self._accounts[global_addr] = {
            "data": make_global(), "lamports": 0, "executable": False,
            "owner": pumpfun.PUMP_PROGRAM}
        if curve is not None:
            curve_addr, _ = pumpfun.bonding_curve_pda(mint)
            self._accounts[curve_addr] = {
                "data": curve, "lamports": 0, "executable": False,
                "owner": pumpfun.PUMP_PROGRAM}
        if ata_balance:
            ata = pumpfun.find_associated_token_address(wallet_pubkey,
                                                        mint)
            self._accounts[ata] = {
                "data": b"", "lamports": 2039280, "executable": False,
                "owner": TOKEN_PROGRAM}
            self._ata_balances[ata] = ata_balance

    def get_account_info(self, pubkey):
        return self._accounts.get(pubkey)

    def get_token_account_balance(self, pubkey):
        return self._ata_balances.get(pubkey, 0)

    def get_balance_lamports(self, pubkey):
        return self.balance

    def get_latest_blockhash(self):
        return BLOCKHASH

    def send_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        ata = pumpfun.find_associated_token_address(self.wallet, self.mint)
        self._ata_balances[ata] = (self._ata_balances.get(ata, 0)
                                   + self.fill_tokens)
        self._accounts[ata] = {
            "data": b"", "lamports": 2039280, "executable": False,
            "owner": TOKEN_PROGRAM}
        return FAKE_SIGNATURE

    def confirm_transaction(self, signature):
        return True


def parse_signed_tx(signed_b64):
    """Decode a signed legacy transaction into its message parts.
    Counts here are all < 128, so compact-u16 is one byte."""
    raw = base64.b64decode(signed_b64)
    n_sigs = raw[0]
    assert n_sigs == 1
    signature = raw[1:65]
    message = raw[65:]
    num_signers = message[0]
    off = 3
    count = message[off]
    off += 1
    keys = [base58_encode(message[off + i * 32:off + (i + 1) * 32])
            for i in range(count)]
    off += count * 32
    blockhash = base58_encode(message[off:off + 32])
    off += 32
    n_ins = message[off]
    off += 1
    instructions = []
    for _ in range(n_ins):
        prog_idx = message[off]
        off += 1
        n_acc = message[off]
        off += 1
        accs = list(message[off:off + n_acc])
        off += n_acc
        dlen = message[off]
        off += 1
        data = message[off:off + dlen]
        off += dlen
        instructions.append({
            "program_id": keys[prog_idx],
            "accounts": [keys[i] for i in accs],
            "data": data})
    return {"signature": signature, "message": message,
            "num_signers": num_signers, "keys": keys,
            "blockhash": blockhash, "instructions": instructions}


class ExecutorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        # Throwaway wallet (never funded, never on-chain) in place of
        # the signup wallet, so the executor signs with a known key.
        self.seed, self.pubkey, secret64 = generate_keypair()
        blob = encrypt_secret(secret64, self.cfg.encryption_key)
        self.db.execute(
            "UPDATE wallets SET pubkey = ?, encrypted_secret = ? "
            "WHERE user_id = ?",
            (self.pubkey, blob, self.user_id))
        self.db.commit()
        _, self.mint, _ = generate_keypair()
        self.support_id = get_signing_bot(self.db,
                                          self.user_id)["support_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _bot(self, mode="real", buy_size=20):
        return {"id": 1, "user_id": self.user_id, "name": "Trenches",
                "mode": mode, "buy_size_pct": buy_size,
                "stop_loss_pct": 7, "max_open_trades": 3}

    def _verify_signature(self, parsed):
        """The transaction really was signed by the wallet key."""
        pub = Ed25519PublicKey.from_public_bytes(
            base58_decode(self.pubkey))
        pub.verify(parsed["signature"], parsed["message"])  # raises if bad


class TestPumpfunMath(unittest.TestCase):
    def test_discriminators_match_known_values(self):
        self.assertEqual(pumpfun.discriminator("buy"), BUY_DISCRIMINATOR)
        self.assertEqual(pumpfun.discriminator("sell"), SELL_DISCRIMINATOR)

    def test_global_pda_matches_devnet(self):
        addr, _bump = pumpfun.global_pda()
        self.assertEqual(
            addr, "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")

    def test_parse_global_known_constants(self):
        glob = pumpfun.parse_global(make_global())
        self.assertEqual(glob["initial_virtual_token_reserves"],
                         1_073_000_000_000_000)
        self.assertEqual(glob["initial_virtual_sol_reserves"], 300_000_000)
        self.assertEqual(glob["fee_basis_points"], 95)

    def test_buy_sell_quotes_are_sane(self):
        curve = pumpfun.parse_bonding_curve(make_curve())
        tokens = pumpfun.buy_quote(curve, 95, 1_000_000_000)
        self.assertGreater(tokens, 0)
        back = pumpfun.sell_quote(curve, 95, tokens)
        self.assertGreater(back, 0)
        self.assertLess(back, 1_000_000_000)  # fees + curve spread


class TestExecutorBuy(ExecutorBase):
    def test_buy_executes_and_signs(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        curve = pumpfun.parse_bonding_curve(make_curve())
        # 1.0 SOL in: the 3.65% fee (36,500,000 lamports) goes to the
        # SolEntry Treasury wallet, and only 0.9635 SOL is swapped
        # for tokens.
        expected = pumpfun.buy_quote(curve, 95, 963_500_000)
        rpc.fill_tokens = expected
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        self.assertEqual(result["signing_bot"], self.support_id)
        self.assertEqual(result["fee_lamports"], 36_500_000)
        self.assertAlmostEqual(result["fee_sol"], 0.0365)
        self.assertAlmostEqual(result["coin_amount"], expected / 1e6)
        self.assertEqual(len(rpc.sent), 1)
        parsed = parse_signed_tx(rpc.sent[0])
        self.assertEqual(parsed["num_signers"], 1)
        self.assertEqual(parsed["keys"][0], self.pubkey)
        self.assertEqual(parsed["blockhash"], BLOCKHASH)
        self._verify_signature(parsed)
        programs = {i["program_id"] for i in parsed["instructions"]}
        self.assertEqual(programs, {pumpfun.PUMP_PROGRAM,
                                    pumpfun.ASSOCIATED_TOKEN_PROGRAM,
                                    SYSTEM_PROGRAM})
        buy = [i for i in parsed["instructions"]
               if i["program_id"] == pumpfun.PUMP_PROGRAM][0]
        self.assertEqual(buy["data"][:8], BUY_DISCRIMINATOR)
        self.assertEqual(int.from_bytes(buy["data"][8:16], "little"),
                         expected)
        self.assertEqual(int.from_bytes(buy["data"][16:24], "little"),
                         963_500_000 * 10100 // 10000 + 1)

    def test_sell_executes_and_signs(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          ata_balance=2_000_000)
        curve = pumpfun.parse_bonding_curve(make_curve())
        expected_sol = pumpfun.sell_quote(curve, 95, 1_000_000)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "sell", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        self.assertEqual(result["signing_bot"], self.support_id)
        self.assertEqual(len(rpc.sent), 1)
        parsed = parse_signed_tx(rpc.sent[0])
        self.assertEqual(parsed["num_signers"], 1)
        self.assertEqual(parsed["keys"][0], self.pubkey)
        self._verify_signature(parsed)
        self.assertEqual(len(parsed["instructions"]), 1)
        sell = parsed["instructions"][0]
        self.assertEqual(sell["program_id"], pumpfun.PUMP_PROGRAM)
        self.assertEqual(sell["data"][:8], SELL_DISCRIMINATOR)
        self.assertEqual(int.from_bytes(sell["data"][8:16], "little"),
                         1_000_000)
        self.assertEqual(int.from_bytes(sell["data"][16:24], "little"),
                         expected_sol * 9900 // 10000)


class TestExecutorGuards(ExecutorBase):
    def test_refuses_mainnet(self):
        env = {"SESSION_SECRET": TEST_SESSION_SECRET,
               "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
               "NETWORK": "mainnet",
               "DATABASE_PATH": os.path.join(self.tmp.name, "m.db"),
               "PAPER_START_CASH": "10000"}
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db,
                                    Config(env=env), rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("devnet", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_paper_bot(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = execute_real_trade(self._bot(mode="paper"), self.user_id,
                                    self.mint, "buy", 1.0, 100.0, self.db,
                                    self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("real mode", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_major_coin_no_swap_path(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = execute_real_trade(self._bot(), self.user_id, "SOL",
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "real swap not available in v1")
        self.assertEqual(rpc.sent, [])

    def test_refuses_over_limit_amount(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        # 10 SOL balance, 20% buy size = 2 SOL max; 3 SOL is over.
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 3.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("buy size", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_no_bonding_curve(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=None)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no bonding curve", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_migrated_curve(self):
        rpc = FakePumpRpc(self.pubkey, self.mint,
                          curve=make_curve(complete=True))
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("migrated", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_when_fee_reserve_would_break(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          balance_lamports=1_000_500_000)
        # 100% buy size passes the size check; 0.0005 SOL left is
        # below the 0.001 SOL fee reserve.
        result = execute_real_trade(self._bot(buy_size=100), self.user_id,
                                    self.mint, "buy", 1.0, 100.0, self.db,
                                    self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_missing_wallet(self):
        self.db.execute("DELETE FROM wallets WHERE user_id = ?",
                        (self.user_id,))
        self.db.commit()
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no app wallet", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_refuses_sell_over_held_balance(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          ata_balance=500_000)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "sell", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("insufficient token balance", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_failed_send_is_a_skip_not_a_fill(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.confirm_transaction = lambda sig: False
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not confirmed", result["reason"])


class TestDestinationRestriction(unittest.TestCase):
    def test_transfer_instruction_refused(self):
        """A plain SOL transfer is a System-program instruction — the
        executor's allowed set excludes it, so it can never be
        signed."""
        transfer = {
            "program_id": "11111111111111111111111111111111",
            "accounts": [
                {"pubkey": "11111111111111111111111111111111",
                 "is_signer": True, "is_writable": True}],
            "data": b"\x02\x00\x00\x00" + (1_000_000).to_bytes(8, "little")}
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed([transfer])

    def test_arbitrary_program_refused(self):
        other = {"program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "accounts": [], "data": b""}
        # Token program alone is allowed (it appears in pump trades
        # as an account program), but an unknown program is not.
        _check_instructions_only_allowed([other])
        bad = {"program_id": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
               "accounts": [], "data": b""}
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed([bad])


class TestLoopRealTrade(ExecutorBase):
    def test_rule_governed_real_buy_through_silent_loop(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        curve = pumpfun.parse_bonding_curve(make_curve())
        expected = pumpfun.buy_quote(curve, 95, 2_000_000_000)
        rpc.fill_tokens = expected
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Trenches', 'balanced', ?, 20, 7, "
            "'real', 1, 3, ?)",
            (self.user_id, json.dumps([self.mint]),
             hashlib.sha256(b"tok").hexdigest()))
        bot_id = cur.lastrowid
        self.db.commit()
        prices = {"prices": {self.mint: {"price": 100.0,
                                         "change_24h": 5.0}},
                  "source": "snapshot", "as_of": "test"}
        with mock.patch.object(executor_mod, "SolanaRpcClient",
                              return_value=rpc):
            outcomes = run_once(
                self.db, self.cfg,
                price_fetcher=lambda: prices,
                rpc_factory=lambda url: rpc)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(outcomes[0]["actions"]), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertEqual(len(rpc.sent), 1)
        parsed = parse_signed_tx(rpc.sent[0])
        self._verify_signature(parsed)
        rows = self.db.execute(
            "SELECT kind, detail FROM activity WHERE user_id = ? "
            "AND kind = 'silent_buy'", (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Signing bot:", rows[0]["detail"])
        self.assertIn(self.support_id, rows[0]["detail"])
        self.assertIn("Signature:", rows[0]["detail"])
        position = self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone()
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position["amount"], expected / 1e6)

    def test_loop_real_buy_skips_when_no_curve(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=None)
        self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Trenches', 'balanced', ?, 20, 7, "
            "'real', 1, 3, ?)",
            (self.user_id, json.dumps([self.mint]),
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        prices = {"prices": {self.mint: {"price": 100.0,
                                         "change_24h": 5.0}},
                  "source": "snapshot", "as_of": "test"}
        with mock.patch.object(executor_mod, "SolanaRpcClient",
                              return_value=rpc):
            outcomes = run_once(
                self.db, self.cfg,
                price_fetcher=lambda: prices,
                rpc_factory=lambda url: rpc)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertIn("no bonding curve",
                      outcomes[0]["actions"][0]["reason"])
        self.assertEqual(rpc.sent, [])


if __name__ == "__main__":
    unittest.main()
