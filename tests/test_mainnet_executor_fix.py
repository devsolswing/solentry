"""Mainnet executor fix: one test per fix, all against mocks — no
real SOL moves, no live RPC. Covers the gate lift, the v3 layout
(17 accounts, fee_config present, no fee_program), priority-fee
capping, 5% exit slippage (entries stay 1%), pending-trade
reconciliation, persistent sell claims, RPC failover, the Jupiter
fee budget, ATA rent in the reserve check, the single-source price
gate, the startup guardrail, and the trades-left chip / mainnet
copy fixes.

Reuses the shared fakes from test_executor (FakePumpRpc,
make_curve, make_global, CURVE_PRICE_USD, TEST_SOL_PRICE_USD,
parse_signed_tx) so the mock curve and the gate-consistent price
stay in one place.
"""

import os
import tempfile
import unittest
from unittest import mock

from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, make_client,
                     signup)
from test_executor import (BUYBACK_RECIPIENTS, CURVE_PRICE_USD,
                           FAKE_SIGNATURE, FakePumpRpc, make_curve,
                           parse_signed_tx, TEST_SOL_PRICE_USD)
from test_jupiter_executor import REAL_SWAP_TX_B64
from tradingbot import gate_fuel as gate_fuel_mod
from tradingbot import jupiter_executor as jex
from tradingbot import pumpfun
from tradingbot import silent_loop as silent_loop_mod
from tradingbot import solana as solana_mod
from tradingbot.config import Config
from tradingbot.executor import (_priority_fee_instruction,
                                 execute_real_trade, pending_signature_for,
                                 record_pending_trade,
                                 reconcile_pending_trades)
from tradingbot.price_gate import check_entry_price
from tradingbot.routes import _mode_label
from tradingbot.security import encrypt_secret
from tradingbot.signing_bot import get_signing_bot
from tradingbot.silent_loop import (_place_sell_claim,
                                    _reconcile_sell_claim,
                                    validate_loop_startup)
from tradingbot.solana import (COMPUTE_BUDGET_PROGRAM, RpcError,
                               SolanaRpcClient, base58_decode,
                               generate_keypair, priority_fee_from_samples)

FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"
MAINNET_GATE = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"


def _mainnet_env(tmpdir):
    return {"SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "mainnet",
            "GATE_MINT": MAINNET_GATE,
            "DATABASE_PATH": os.path.join(tmpdir, "m.db"),
            "PAPER_START_CASH": "10000"}


class MainnetFixBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.seed, self.pubkey, secret64 = generate_keypair()
        blob = encrypt_secret(secret64, self.cfg.encryption_key)
        self.db.execute(
            "UPDATE wallets SET pubkey = ?, encrypted_secret = ?, "
            "enc_version = 1 WHERE user_id = ?",
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

    def _mainnet_cfg(self):
        return Config(env=_mainnet_env(self.tmp.name))

    def _buy(self, rpc, cfg=None, amount=1.0, **kwargs):
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95,
                                            int(amount * 1_000_000_000))
        return execute_real_trade(
            self._bot(), self.user_id, self.mint, "buy", amount,
            CURVE_PRICE_USD, self.db, cfg or self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
 
            rpc_client=rpc, **kwargs)


class TestGateLiftAndLayout(MainnetFixBase):
    def test_mainnet_buy_executes_with_v3_layout(self):
        """The devnet-only gate is lifted AND the built buy matches
        the current mainnet v3 layout: 17 accounts on the buy
        instruction, the fee_config account present, and NO
        fee_program account (the current program reads the fee
        schedule from fee_config directly)."""
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fee_samples = [100_000]
        result = self._buy(rpc, cfg=self._mainnet_cfg())
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        self.assertEqual(len(rpc.sent), 1)
        parsed = parse_signed_tx(rpc.sent[0])
        # A priority-fee instruction is prepended when the RPC
        # reports fee samples (capped — see the fee tests).
        fee_ixs = [i for i in parsed["instructions"]
                   if i["program_id"] == COMPUTE_BUDGET_PROGRAM]
        self.assertEqual(len(fee_ixs), 1)
        self.assertEqual(fee_ixs[0]["data"][0], 3)  # setComputeUnitPrice
        self.assertEqual(int.from_bytes(fee_ixs[0]["data"][1:9], "little"),
                         50_000)  # median 100,000 capped at 50,000
        buy = [i for i in parsed["instructions"]
               if i["program_id"] == pumpfun.PUMP_PROGRAM][0]
        self.assertEqual(len(buy["accounts"]), 17)
        self.assertIn(FEE_CONFIG, buy["accounts"])
        self.assertNotIn(FEE_PROGRAM, buy["accounts"])
        self.assertIn(BUYBACK_RECIPIENTS[0], buy["accounts"])
        global_addr, _ = pumpfun.global_pda()
        self.assertIn(global_addr, buy["accounts"])

    def test_no_priority_fee_instruction_without_samples(self):
        """No fee samples (or all zero) means no priority-fee
        instruction — the trade sends with the default fee rather
        than a guessed price."""
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fee_samples = []
        result = self._buy(rpc)
        self.assertEqual(result["status"], "executed", result)
        parsed = parse_signed_tx(rpc.sent[0])
        programs = {i["program_id"] for i in parsed["instructions"]}
        self.assertNotIn(COMPUTE_BUDGET_PROGRAM, programs)
        rpc2 = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc2.fee_samples = [0, 0]
        self.assertIsNone(_priority_fee_instruction(
            rpc2, self.cfg, [self.mint]))


class TestPriorityFeeCapping(unittest.TestCase):
    def test_median_then_cap(self):
        # Median of [10k, 20k, 30k] is 20k — under the cap.
        self.assertEqual(priority_fee_from_samples(
            [10_000, 20_000, 30_000], 50_000), 20_000)
        # Median of [100k, 200k] is 150k — capped at 50k.
        self.assertEqual(priority_fee_from_samples(
            [100_000, 200_000], 50_000), 50_000)
        # One spiking slot does not set the price (median, not max).
        self.assertEqual(priority_fee_from_samples(
            [1_000, 1_000, 9_000_000], 50_000), 1_000)

    def test_no_usable_sample_is_zero(self):
        self.assertEqual(priority_fee_from_samples([], 50_000), 0)
        self.assertEqual(priority_fee_from_samples([0, 0], 50_000), 0)
        self.assertEqual(priority_fee_from_samples([5_000], 0), 0)


class TestExitSlippage(MainnetFixBase):
    def test_sell_uses_five_percent_exit_slippage(self):
        """A sell large enough for the quote to be non-zero must
        carry min_sol = expected * 9500 // 10000 (5% exit slippage,
        a Remy default). The old 1% bound (9900) would fail this
        assertion — the pre-existing sell test's quote rounds to
        zero, so it cannot tell the two apart."""
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          ata_balance=4_000_000_000_000)
        curve = pumpfun.parse_bonding_curve(make_curve())
        amount_tokens = 3_500_000.0
        expected_sol = pumpfun.sell_quote(
            curve, 95, int(amount_tokens * 1_000_000))
        self.assertGreater(expected_sol, 100_000)
        result = execute_real_trade(
            self._bot(), self.user_id, self.mint, "sell", amount_tokens,
            CURVE_PRICE_USD, self.db, self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "executed", result)
        parsed = parse_signed_tx(rpc.sent[0])
        sell = [i for i in parsed["instructions"]
                if i["program_id"] == pumpfun.PUMP_PROGRAM][0]
        self.assertEqual(int.from_bytes(sell["data"][16:24], "little"),
                         expected_sol * 9500 // 10000)

    def test_buy_keeps_one_percent_entry_slippage(self):
        """Entries stay at 1%: max SOL cost = amount * 10100 //
        10000 + 1."""
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = self._buy(rpc)
        self.assertEqual(result["status"], "executed", result)
        parsed = parse_signed_tx(rpc.sent[0])
        buy = [i for i in parsed["instructions"]
               if i["program_id"] == pumpfun.PUMP_PROGRAM][0]
        self.assertEqual(int.from_bytes(buy["data"][16:24], "little"),
                         1_000_000_000 * 10100 // 10000 + 1)


class TestPendingReconciliation(MainnetFixBase):
    def test_buy_records_pending_then_reconciles(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        result = self._buy(rpc)
        self.assertEqual(result["status"], "executed", result)
        rows = self.db.execute(
            "SELECT signature, status FROM pending_trades "
            "WHERE user_id = ? AND coin_id = ?",
            (self.user_id, self.mint)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "confirmed")

    def test_reconcile_marks_confirmed_failed_and_stale(self):
        # Three pending rows: one lands, one lands with an error,
        # one never lands and is past the horizon.
        now_old = "2020-01-01T00:00:00+00:00"
        for sig, side in (("sig-ok", "buy"), ("sig-bad", "buy"),
                          ("sig-old", "sell")):
            record_pending_trade(self.db, self.user_id, 1, self.mint,
                                 side, sig)
        self.db.execute(
            "UPDATE pending_trades SET created_at = ? WHERE signature = ?",
            (now_old, "sig-old"))
        self.db.commit()
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc._tx_receipts = {
            "sig-ok": {"meta": {"err": None}},
            "sig-bad": {"meta": {"err": "some failure"}},
        }
        counts = reconcile_pending_trades(self.db, rpc, horizon_seconds=600)
        self.assertEqual(counts, {"confirmed": 1, "failed": 2,
                                  "still_pending": 0})
        statuses = dict(self.db.execute(
            "SELECT signature, status FROM pending_trades").fetchall())
        self.assertEqual(statuses["sig-ok"], "confirmed")
        self.assertEqual(statuses["sig-bad"], "failed")
        self.assertEqual(statuses["sig-old"], "failed")

    def test_reconcile_keeps_fresh_unlanded_pending(self):
        record_pending_trade(self.db, self.user_id, 1, self.mint,
                             "buy", "sig-fresh")
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        counts = reconcile_pending_trades(self.db, rpc, horizon_seconds=600)
        self.assertEqual(counts["still_pending"], 1)
        self.assertEqual(pending_signature_for(
            self.db, self.user_id, self.mint, "buy"), "sig-fresh")


class TestPersistentSellClaims(MainnetFixBase):
    def _claim(self, signature=None):
        # sell_claims references bots(id): a claim needs a real
        # bot row, as the loop always has in production.
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, "
            "max_open_trades, api_token_hash) "
            "VALUES (?, 'Trenches', 'balanced', '[]', 20, 7, "
            "'real', 1, 3, 'x')", (self.user_id,))
        bot_id = cur.lastrowid
        self.db.commit()
        claim_id = _place_sell_claim(self.db, self.user_id, bot_id,
                                     self.mint, 1.0)
        self.assertIsNotNone(claim_id)
        if signature is not None:
            from tradingbot.silent_loop import _resolve_sell_claim
            _resolve_sell_claim(self.db, claim_id, "open", signature)
        return claim_id

    def test_no_claim_is_ok(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        bot = {"id": 1, "user_id": self.user_id}
        self.assertEqual(_reconcile_sell_claim(
            self.db, self.cfg, bot, self.mint, rpc), "ok")

    def test_claim_without_signature_releases(self):
        self._claim()
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        bot = {"id": 1, "user_id": self.user_id}
        self.assertEqual(_reconcile_sell_claim(
            self.db, self.cfg, bot, self.mint, rpc), "ok")
        row = self.db.execute(
            "SELECT status FROM sell_claims").fetchone()
        self.assertEqual(row["status"], "released")

    def test_landed_claim_is_sold(self):
        self._claim(signature="sig-landed")
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc._tx_receipts = {"sig-landed": {"meta": {"err": None}}}
        bot = {"id": 1, "user_id": self.user_id}
        self.assertEqual(_reconcile_sell_claim(
            self.db, self.cfg, bot, self.mint, rpc), "sold")

    def test_fresh_unlanded_claim_blocks(self):
        self._claim(signature="sig-flight")
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        bot = {"id": 1, "user_id": self.user_id}
        self.assertEqual(_reconcile_sell_claim(
            self.db, self.cfg, bot, self.mint, rpc), "blocked")

    def test_old_unlanded_claim_releases(self):
        claim_id = self._claim(signature="sig-old")
        self.db.execute(
            "UPDATE sell_claims SET claimed_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", claim_id))
        self.db.commit()
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        bot = {"id": 1, "user_id": self.user_id}
        self.assertEqual(_reconcile_sell_claim(
            self.db, self.cfg, bot, self.mint, rpc), "ok")


class TestRpcFailover(unittest.TestCase):
    def test_fallback_used_when_primary_transport_fails(self):
        calls = []

        def fake_post(url, payload):
            calls.append(url)
            if "primary" in url:
                raise ConnectionError("primary down")
            return {"result": {"value": 123}}

        client = SolanaRpcClient(
            "https://primary.example", http_post=fake_post,
            fallback_rpc_url="https://fallback.example")
        self.assertEqual(client.get_balance_lamports("1111111111111111"),
                         123)
        self.assertEqual(calls, ["https://primary.example",
                                 "https://fallback.example"])

    def test_node_error_is_not_retried_on_fallback(self):
        calls = []

        def fake_post(url, payload):
            calls.append(url)
            return {"error": {"code": -32601,
                              "message": "Method not found"}}

        client = SolanaRpcClient(
            "https://primary.example", http_post=fake_post,
            fallback_rpc_url="https://fallback.example")
        with self.assertRaises(RpcError):
            client.get_balance_lamports("1111111111111111")
        self.assertEqual(calls, ["https://primary.example"])

    def test_no_fallback_is_a_single_attempt(self):
        calls = []

        def fake_post(url, payload):
            calls.append(url)
            raise ConnectionError("down")

        client = SolanaRpcClient("https://primary.example",
                                 http_post=fake_post)
        with self.assertRaises(RpcError):
            client.get_balance_lamports("1111111111111111")
        self.assertEqual(calls, ["https://primary.example"])


class TestJupiterFeeBudget(unittest.TestCase):
    def test_real_swap_tx_fee_math(self):
        # The recorded real Jupiter swap: compute limit 1,400,000
        # x price 71,428 micro-lamports = 99,999 lamports, plus the
        # 5,000 base fee = 104,999 lamports (0.000105 SOL).
        parsed = jex.parse_versioned_transaction(REAL_SWAP_TX_B64)
        fee = jex.max_swap_fee_lamports(parsed, parsed["static_keys"])
        self.assertEqual(fee, 104_999)

    def test_budget_refuses_over_budget_and_passes_under(self):
        parsed = jex.parse_versioned_transaction(REAL_SWAP_TX_B64)
        # 104,999 lamports exceeds a 0.00001 SOL budget.
        with self.assertRaises(ValueError):
            jex.check_swap_fee_budget(parsed, parsed["static_keys"],
                                      0.00001)
        # …and fits the 0.01 SOL per-trade budget (a Remy default).
        jex.check_swap_fee_budget(parsed, parsed["static_keys"], 0.01)


class TestAtaRentReserve(MainnetFixBase):
    def test_buy_refuses_when_reserve_plus_rent_does_not_fit(self):
        """Balance covers amount + 0.001 SOL fee reserve but not
        the extra ~0.00204 SOL ATA rent (ATA does not exist yet):
        the buy refuses instead of landing short on rent."""
        balance = 1_000_000_000 + 1_000_000 + 2_039_280 - 1
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          balance_lamports=balance)
        result = execute_real_trade(
            self._bot(buy_size=100), self.user_id, self.mint, "buy",
            1.0, CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
 rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_buy_succeeds_when_reserve_plus_rent_fits(self):
        balance = 1_000_000_000 + 1_000_000 + 2_039_280
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          balance_lamports=balance)
        result = execute_real_trade(
            self._bot(buy_size=100), self.user_id, self.mint, "buy",
            1.0, CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
 rpc_client=rpc)
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(len(rpc.sent), 1)


class TestSingleSourceGate(MainnetFixBase):
    """The price gate after the single-source build (2026-11-25):
    one stream price, one SOL price — no second source to agree
    with, so no cross-check can refuse a trade. What remains is
    the same-source consistency check (the stream tick against a
    fresh on-chain curve read moments apart) and the staleness
    check."""

    def _gate(self, price, age=0.0, sol=TEST_SOL_PRICE_USD):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        return check_entry_price(rpc, self.cfg, self.mint, price, age,
                                 sol)

    def test_agreeing_prices_pass(self):
        ok, reason, details = self._gate(CURVE_PRICE_USD)
        self.assertTrue(ok, reason)
        self.assertLess(details["two_source_diff_pct"], 5.0)

    def test_stale_decision_price_refuses(self):
        ok, reason, _ = self._gate(CURVE_PRICE_USD, age=31.0)
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_disagreeing_onchain_price_refuses(self):
        ok, reason, _ = self._gate(CURVE_PRICE_USD * 2)
        self.assertFalse(ok)
        self.assertIn("moved away", reason)

    def test_single_sol_price_has_no_cross_check(self):
        """One SOL price only: the gate passes with the single
        feed SOL tick and there is no second source to disagree
        with — the old Jupiter-vs-CoinGecko refusal cannot fire."""
        ok, reason, details = self._gate(CURVE_PRICE_USD)
        self.assertTrue(ok, reason)
        self.assertNotIn("sol_cross_check", details)
        self.assertNotIn("sol_price_secondary", details)


class TestStartupGuardrail(unittest.TestCase):
    def test_mainnet_with_explicit_devnet_mints_refuses(self):
        env = _mainnet_env(tempfile.mkdtemp())
        env["DEVNET_TRENCH_MINTS"] = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
        cfg = Config(env=env)
        with self.assertRaises(ValueError):
            validate_loop_startup(cfg)

    def test_mainnet_with_defaults_only_passes(self):
        cfg = Config(env=_mainnet_env(tempfile.mkdtemp()))
        validate_loop_startup(cfg)  # built-in defaults are inert

    def test_devnet_with_explicit_mints_passes(self):
        env = _mainnet_env(tempfile.mkdtemp())
        env["NETWORK"] = "devnet"
        env["DEVNET_TRENCH_MINTS"] = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
        cfg = Config(env=env)
        validate_loop_startup(cfg)


class TestChipAndCopy(MainnetFixBase):
    def test_mode_label_is_network_aware(self):
        self.assertEqual(_mode_label("devnet", self._mainnet_cfg()),
                         "Real trading — mainnet")
        self.assertEqual(_mode_label("devnet", self.cfg),
                         "Real trading — devnet")
        self.assertEqual(_mode_label("paper", self._mainnet_cfg()),
                         "Demo trading (paper)")

    def test_gate_mint_resolves_on_mainnet_without_buy_path(self):
        """The package mint resolves on mainnet even when
        JUPITER_REAL_SWAPS is off (no Jupiter path) — packages buy
        through the pump.fun executor while $GATE is still bonding,
        so the mint is not buy-path-gated."""
        env = _mainnet_env(self.tmp.name)
        env["JUPITER_REAL_SWAPS"] = "0"
        cfg = Config(env=env)
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), MAINNET_GATE)

    def test_package_balance_starts_at_zero_on_mainnet(self):
        """25,000,000 $GATE held on mainnet is NOT fuel: the
        package balance starts at 0 and grows only from packages
        bought in the app. A package purchase then credits its
        trades."""
        cfg = self._mainnet_cfg()
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, MAINNET_GATE, 25_000_000.0, 0.000002))
        self.db.commit()
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)
        bundle = gate_fuel_mod.get_bundle("bundle_1m")
        gate_fuel_mod.credit_purchase(
            self.db, self.user_id, bundle, 0, "test-buy", "test-purchase")
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 100)

    def test_mainnet_pages_show_mainnet_copy(self):
        """Rendered pages on a mainnet-configured app say mainnet,
        never 'Real trading — devnet' or 'Mainnet is not
        connected'."""
        from tradingbot import create_app
        env = _mainnet_env(self.tmp.name)
        env["DATABASE_PATH"] = os.path.join(self.tmp.name, "m2.db")
        mainnet_app = create_app(Config(env=env))
        mainnet_app.testing = True
        client = mainnet_app.test_client()
        # Signed in with no mode chosen yet, the chooser popup
        # (in the base template, on the wallet homepage) offers
        # the real mode by its mainnet name — never the devnet
        # wording. (The index template itself is unrendered: "/"
        # redirects to the login screen, so its copy is checked
        # by the template grep in TestLandingPageGone's spirit,
        # not by HTTP.)
        signup(client, "bob")
        html = client.get("/", follow_redirects=True).get_data(
            as_text=True)
        self.assertIn("Real trading — mainnet", html)
        self.assertNotIn("Real trading — devnet", html)
        self.assertNotIn("Mainnet is not connected", html)
        # And the wallet page's own intro copy is mainnet too.
        self.assertNotIn("Real SOL on devnet", html)


if __name__ == "__main__":
    unittest.main()
