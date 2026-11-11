"""The 3.65% trading fee + the FEE_RECIPIENT_PUBKEY recipient
(fees.py) + the fee split (James, 2026-11-10/11): 1.7% of the
trade to the treasury (James's wallet), the rest (1.95%) burned
to the incinerator on devnet only, in the same transaction as
the trade.

Covers: fee math (3.65% of several amounts, rounding) and the
split math (treasury 1.7% / burn 1.95%, exact, devnet-gated),
recipient resolution from the env var + fail-closed behaviour
when it is unset or invalid, the purge of the retired generated
wallets, paper fee recording (recorded in the ledger but NEVER
counted in the displayed SOL totals — paper money is not SOL
paid), the displayed treasury share (1.7% of the trade, i.e.
recorded fee x 170/365), the pump.fun program fee per trade
(derived from the trade's amounts and the live global fee_bps),
the real-trade fee flow with mocked signing (no real SOL moves
— the RPC is fake, the wallet is a throwaway keypair, and the
signed transaction is decoded and checked), the home-page fee
dropdown + its balance switcher, the fee in buy/sell
confirmations, no fee on failed/refused trades, and no fee on
trades that predate the deploy.
"""

import tempfile
import unittest

from helpers import (TEST_ENCRYPTION_KEY, TEST_FEE_RECIPIENT,
                     TEST_SESSION_SECRET, csrf, login, make_client,
                     signup)
from test_executor import (ExecutorBase, FakePumpRpc, make_curve,
                           parse_signed_tx)
from tradingbot import fees as fees_mod
from tradingbot import pumpfun
from tradingbot.executor import (_check_instructions_only_allowed,
                                 execute_real_trade)
from tradingbot.paper import paper_buy, paper_sell
from tradingbot.solana import SYSTEM_PROGRAM, build_transfer_instruction


class TestFeeMath(unittest.TestCase):
    def test_lamports_fee(self):
        self.assertEqual(fees_mod.fee_lamports(1_000_000_000), 36_500_000)
        self.assertEqual(fees_mod.fee_lamports(100_000_000), 3_650_000)
        self.assertEqual(fees_mod.fee_lamports(10_000_000), 365_000)
        # Rounds down, never up: 100 lamports * 3.65% = 3.65 -> 3.
        self.assertEqual(fees_mod.fee_lamports(100), 3)
        # Too small to charge: 27 lamports * 3.65% < 1 -> no fee.
        self.assertEqual(fees_mod.fee_lamports(27), 0)
        self.assertEqual(fees_mod.fee_lamports(0), 0)

    def test_usd_fee(self):
        self.assertAlmostEqual(fees_mod.fee_usd(10.0), 0.365)
        self.assertAlmostEqual(fees_mod.fee_usd(100.0), 3.65)
        self.assertAlmostEqual(fees_mod.fee_usd(0.0), 0.0)
        self.assertAlmostEqual(fees_mod.fee_usd(-5.0), 0.0)

    def test_fee_sol_from_usd(self):
        self.assertAlmostEqual(
            fees_mod.fee_sol_from_usd(0.365, 100.0), 0.00365)
        self.assertIsNone(fees_mod.fee_sol_from_usd(0.365, None))
        self.assertIsNone(fees_mod.fee_sol_from_usd(0.365, 0))
        self.assertIsNone(fees_mod.fee_sol_from_usd(0.0, 100.0))

    def test_mainnet_settlement_rule(self):
        # Mainnet: after selling, proceeds convert to SOL and are
        # held as SOL — never cash, never USDC.
        self.assertEqual(fees_mod.MAINNET_SETTLEMENT, "SOL")


class TestFeeSplit(unittest.TestCase):
    def test_split_math_on_0_2_sol_buy(self):
        # James's numbers, 2026-11-10: 0.2 SOL buy, fee 7,300,000
        # lamports — treasury 3,400,000 (1.7%), burn 3,900,000
        # (1.95%). Treasury + burn is exactly the fee.
        split = fees_mod.fee_split_lamports(200_000_000, "devnet")
        self.assertEqual(split["fee_lamports"], 7_300_000)
        self.assertEqual(split["treasury_lamports"], 3_400_000)
        self.assertEqual(split["burn_lamports"], 3_900_000)
        self.assertEqual(split["treasury_lamports"]
                         + split["burn_lamports"],
                         split["fee_lamports"])

    def test_split_math_on_1_sol(self):
        split = fees_mod.fee_split_lamports(1_000_000_000, "devnet")
        self.assertEqual(split["fee_lamports"], 36_500_000)
        self.assertEqual(split["treasury_lamports"], 17_000_000)
        self.assertEqual(split["burn_lamports"], 19_500_000)

    def test_treasury_lamports(self):
        self.assertEqual(fees_mod.treasury_lamports(1_000_000_000),
                         17_000_000)
        self.assertEqual(fees_mod.treasury_lamports(0), 0)
        self.assertEqual(fees_mod.treasury_lamports(-5), 0)

    def test_burn_is_devnet_only(self):
        # James, 2026-11-11: the burn is devnet-only. On mainnet
        # the burn is 0 — this build can never burn on mainnet —
        # while the treasury share is unchanged. An unset network
        # behaves as devnet (the app's Config defaults NETWORK to
        # devnet), so the burn applies there too.
        for network in ("mainnet", "MAINNET", "Mainnet"):
            split = fees_mod.fee_split_lamports(200_000_000, network)
            self.assertEqual(split["burn_lamports"], 0, msg=network)
            self.assertEqual(split["treasury_lamports"], 3_400_000)
            self.assertEqual(split["fee_lamports"], 7_300_000)
        for network in ("devnet", "", None):
            split = fees_mod.fee_split_lamports(200_000_000, network)
            self.assertEqual(split["burn_lamports"], 3_900_000,
                             msg=network)
        self.assertEqual(
            fees_mod.burn_lamports(200_000_000, "devnet"), 3_900_000)
        self.assertEqual(
            fees_mod.burn_lamports(200_000_000, "mainnet"), 0)

    def test_incinerator_address_is_valid(self):
        # The burn destination is the well-known unspendable
        # incinerator — a valid base58 32-byte address, hardcoded,
        # never taken from user input or config.
        from tradingbot.solana import is_valid_address
        self.assertEqual(
            fees_mod.INCINERATOR_PUBKEY,
            "1nc1nerator11111111111111111111111111111111")
        self.assertTrue(is_valid_address(fees_mod.INCINERATOR_PUBKEY))

    def test_burn_share_constant(self):
        self.assertEqual(fees_mod.BURN_SHARE_BPS_OF_TRADE, 195)
        self.assertEqual(fees_mod.TREASURY_SHARE_BPS_OF_TRADE
                         + fees_mod.BURN_SHARE_BPS_OF_TRADE,
                         fees_mod.FEE_BPS)


class TestProgramFee(unittest.TestCase):
    def test_buy_program_fee(self):
        # 0.2 SOL buy: 192,700,000 lamports swapped at the live
        # global fee_bps 95 (0.95%) -> 1,830,650 program fee.
        self.assertEqual(
            pumpfun.buy_program_fee(192_700_000, 95), 1_830_650)
        self.assertEqual(pumpfun.buy_program_fee(0, 95), 0)

    def test_sell_program_fee_matches_quote(self):
        # The sell program fee is exactly what sell_quote
        # subtracts: gross - sell_quote == the program fee.
        curve = pumpfun.parse_bonding_curve(make_curve())
        vt = curve["virtual_token_reserves"]
        vs = curve["virtual_sol_reserves"]
        for tokens in (1_000_000_000_000, 500_000_000_000, 10_000_000):
            gross = vs - (vt * vs) // (vt + tokens)
            fee = pumpfun.sell_program_fee(curve, 95, tokens)
            self.assertEqual(fee, gross * 95 // 10000)
            self.assertEqual(
                pumpfun.sell_quote(curve, 95, tokens), gross - fee)
        # The big sell from the fee test: gross 279,330, program
        # fee 2,653, net 276,677.
        self.assertEqual(
            pumpfun.sell_program_fee(curve, 95, 1_000_000_000_000),
            2_653)
        self.assertEqual(
            pumpfun.sell_quote(curve, 95, 1_000_000_000_000), 276_677)

    def test_buy_program_fee_matches_quote(self):
        # buy_quote subtracts exactly the program fee before the
        # curve math: quote with fee_bps 0 vs 95 differs by the
        # fee's effect on the swap, and the fee itself is the
        # helper's value.
        self.assertEqual(
            pumpfun.buy_program_fee(963_500_000, 95), 9_153_250)


class TestFeeRecipient(ExecutorBase):
    def test_recipient_from_env_var(self):
        # The one recipient comes from FEE_RECIPIENT_PUBKEY — the
        # throwaway test address, never a real wallet.
        self.assertEqual(
            fees_mod.get_fee_recipient_pubkey(self.cfg),
            TEST_FEE_RECIPIENT)
        from tradingbot.solana import is_valid_address
        self.assertTrue(is_valid_address(TEST_FEE_RECIPIENT))

    def test_unset_var_fails_closed(self):
        from tradingbot.config import Config
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": self.tmp.name + "/other.db",
        })
        with self.assertRaises(ValueError):
            fees_mod.get_fee_recipient_pubkey(cfg)

    def test_empty_var_fails_closed(self):
        from tradingbot.config import Config
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": self.tmp.name + "/other.db",
            "FEE_RECIPIENT_PUBKEY": "   ",
        })
        with self.assertRaises(ValueError):
            fees_mod.get_fee_recipient_pubkey(cfg)

    def test_invalid_var_fails_closed(self):
        from tradingbot.config import Config
        for bad in ("not-an-address", "x" * 32,
                    TEST_FEE_RECIPIENT + "extra"):
            cfg = Config(env={
                "SESSION_SECRET": TEST_SESSION_SECRET,
                "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
                "NETWORK": "devnet",
                "DATABASE_PATH": self.tmp.name + "/other.db",
                "FEE_RECIPIENT_PUBKEY": bad,
            })
            with self.assertRaises(ValueError, msg=bad):
                fees_mod.get_fee_recipient_pubkey(cfg)

    def test_unset_recipient_refuses_real_trade(self):
        # Fail closed: with no recipient configured the trade is
        # refused before anything is signed or sent — never a
        # fallback wallet, never a guessed address.
        from tradingbot.config import Config
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": self.tmp.name + "/other.db",
        })
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fill_tokens = 1_000_000
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fee recipient", result["reason"])
        self.assertEqual(rpc.sent, [])
        self.assertEqual(fees_mod.user_fees(self.db, self.user_id), [])


class TestRetiredGeneratedWallets(unittest.TestCase):
    def test_purge_deletes_generated_wallet_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app, _client = make_client(tmp.name)
        db = app.config["DB"]
        # Simulate a database from before the switch: rows left by
        # the old generated Treasury / donation wallets.
        db.execute(
            "INSERT INTO treasury_wallet (id, pubkey, encrypted_secret) "
            "VALUES (1, 'TreasuryPubkey', 'x')")
        db.execute(
            "INSERT INTO donation_wallet (id, pubkey, encrypted_secret) "
            "VALUES (1, 'DonationPubkey', 'x')")
        db.commit()
        # The startup migration already ran once for this DB; clear
        # its marker so the purge can run again, as it would on a
        # live database that still holds the rows.
        db.execute(
            "DELETE FROM meta WHERE key = 'purge_generated_fee_wallets'")
        db.commit()
        from tradingbot.db import _purge_generated_fee_wallets
        _purge_generated_fee_wallets(db)
        self.assertEqual(
            db.execute("SELECT COUNT(*) n FROM treasury_wallet"
                       ).fetchone()["n"], 0)
        self.assertEqual(
            db.execute("SELECT COUNT(*) n FROM donation_wallet"
                       ).fetchone()["n"], 0)
        # The marker is set, so a second run is a no-op.
        self.assertEqual(
            db.execute("SELECT value FROM meta WHERE key = "
                       "'purge_generated_fee_wallets'").fetchone()["value"],
            "2026-11-10")

    def test_fresh_database_has_no_generated_wallet_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app, _client = make_client(tmp.name)
        db = app.config["DB"]
        self.assertEqual(
            db.execute("SELECT COUNT(*) n FROM treasury_wallet"
                       ).fetchone()["n"], 0)
        self.assertEqual(
            db.execute("SELECT COUNT(*) n FROM donation_wallet"
                       ).fetchone()["n"], 0)


class TestPaperFees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_paper_buy_charges_and_records_fee(self):
        amount, usd = paper_buy(self.db, self.user_id, "SOL", 10.0,
                                100.0, "2026-11-10T00:00:00Z",
                                sol_price_usd=100.0)
        # $10 spent, but only $9.635 of coins: the 3.65% fee is a
        # cost, not free money.
        self.assertAlmostEqual(amount, 0.09635)
        self.assertAlmostEqual(usd, 10.0)
        self.assertAlmostEqual(
            self.db.execute(
                "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
                (self.user_id,)).fetchone()["cash_usd"], 90.0)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["fee_usd"], 0.365)
        self.assertAlmostEqual(rows[0]["fee_sol"], 0.00365)
        self.assertEqual(rows[0]["mode"], "paper")
        self.assertIsNone(rows[0]["signature"])
        # The paper fee is recorded in the ledger — but it is play
        # money, so it NEVER counts in the displayed SOL total.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.0)
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db,
                                       sol_price_usd=100.0), 0.0)

    def test_paper_sell_charges_fee_on_proceeds(self):
        paper_buy(self.db, self.user_id, "SOL", 10.0, 100.0,
                  "2026-11-10T00:00:00Z", sol_price_usd=100.0)
        usd = paper_sell(self.db, self.user_id, "SOL", 0.05, 100.0,
                         "2026-11-10T00:01:00Z", sol_price_usd=100.0)
        # Gross $5.00, fee $0.1825, net $4.8175.
        self.assertAlmostEqual(usd, 4.8175)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["fee_usd"], 0.1825)
        # Paper fees never count in the SOL total.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.0)

    def test_paper_fee_never_counts_in_sol_total(self):
        # The regression test for the inflated balances: a user
        # with ONLY paper trades has paid 0 real SOL, so both the
        # per-user total and the app-wide total are 0 — with or
        # without a live SOL price. The rows are still recorded.
        paper_buy(self.db, self.user_id, "BTC", 10.0, 100.0,
                  "2026-11-10T00:00:00Z")
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["fee_sol"])
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id), 0.0)
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.0)
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db,
                                       sol_price_usd=100.0), 0.0)
        # The paper rows are still in the ledger, filterable by
        # mode — the fix is in the totals, not in the recording.
        self.assertEqual(
            len(fees_mod.user_fees(self.db, self.user_id,
                                   mode="paper")), 1)
        self.assertEqual(
            len(fees_mod.user_fees(self.db, self.user_id,
                                   mode="real")), 0)

    def test_failed_paper_buy_charges_no_fee(self):
        # $200 buy against a $100 paper balance: refused, no fee.
        with self.assertRaises(Exception):
            paper_buy(self.db, self.user_id, "SOL", 200.0, 100.0,
                      "2026-11-10T00:00:00Z")
        self.assertEqual(fees_mod.user_fees(self.db, self.user_id), [])

    def test_predeploy_trades_are_never_back_charged(self):
        # A trade recorded before the fee existed (inserted directly,
        # as the old code would have left it): no fee row, total 0.
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, amount, "
            "price, total_usd, created_at) VALUES (?, 'SOL', 'buy', "
            "0.1, 100.0, 10.0, '2026-11-09T00:00:00Z')",
            (self.user_id,))
        self.db.commit()
        self.assertEqual(fees_mod.user_fees(self.db, self.user_id), [])
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.0)

    def test_duplicate_signature_records_one_fee(self):
        self.assertTrue(fees_mod.record_fee(
            self.db, self.user_id, None, "SOL", "buy", "real", 0.0365,
            fee_lamports=36_500_000, signature="sig1"))
        self.assertFalse(fees_mod.record_fee(
            self.db, self.user_id, None, "SOL", "buy", "real", 0.0365,
            fee_lamports=36_500_000, signature="sig1"))
        self.assertEqual(len(fees_mod.user_fees(self.db, self.user_id)), 1)


class TestRealFeeFlow(ExecutorBase):
    def test_buy_fee_travels_in_the_same_transaction(self):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        curve = pumpfun.parse_bonding_curve(make_curve())
        # 1.0 SOL in: 0.0365 SOL fee, 0.9635 SOL swapped for tokens.
        trade_lamports = 1_000_000_000 - 36_500_000
        expected = pumpfun.buy_quote(curve, 95, trade_lamports)
        rpc.fill_tokens = expected
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["fee_lamports"], 36_500_000)
        self.assertAlmostEqual(result["fee_sol"], 0.0365)
        # The split (James, 2026-11-10/11): 1.7% of the trade to
        # the treasury, the rest (1.95%) burned, in the same tx.
        self.assertEqual(result["treasury_lamports"], 17_000_000)
        self.assertAlmostEqual(result["treasury_sol"], 0.017)
        self.assertEqual(result["burn_lamports"], 19_500_000)
        self.assertAlmostEqual(result["burn_sol"], 0.0195)
        # The pump.fun program fee on the SOL swapped, at the live
        # global fee_bps 95: 963,500,000 x 95 // 10000.
        self.assertEqual(result["pump_fee_lamports"], 9_153_250)
        self.assertAlmostEqual(result["pump_fee_sol"], 0.00915325)
        # The fee goes to the configured recipient — the throwaway
        # test address from FEE_RECIPIENT_PUBKEY — and the result
        # carries no wallet address at all.
        self.assertNotIn("treasury", result)
        parsed = parse_signed_tx(rpc.sent[0])
        programs = [i["program_id"] for i in parsed["instructions"]]
        self.assertIn(SYSTEM_PROGRAM, programs)
        # TWO System transfers, after the buy: treasury first,
        # then the burn — each exactly once.
        sys_ix = [i for i in parsed["instructions"]
                  if i["program_id"] == SYSTEM_PROGRAM]
        self.assertEqual(len(sys_ix), 2)
        self.assertEqual(parsed["instructions"][-1]["program_id"],
                         SYSTEM_PROGRAM)
        self.assertEqual(parsed["instructions"][-2]["program_id"],
                         SYSTEM_PROGRAM)
        treasury_ix, burn_ix = sys_ix
        self.assertEqual(len(treasury_ix["accounts"]), 2)
        self.assertEqual(treasury_ix["accounts"][0], self.pubkey)
        self.assertEqual(treasury_ix["accounts"][1], TEST_FEE_RECIPIENT)
        self.assertEqual(treasury_ix["data"][:4], bytes([2, 0, 0, 0]))
        self.assertEqual(
            int.from_bytes(treasury_ix["data"][4:12], "little"),
            17_000_000)
        self.assertEqual(len(burn_ix["accounts"]), 2)
        self.assertEqual(burn_ix["accounts"][0], self.pubkey)
        self.assertEqual(burn_ix["accounts"][1],
                         fees_mod.INCINERATOR_PUBKEY)
        self.assertEqual(burn_ix["data"][:4], bytes([2, 0, 0, 0]))
        self.assertEqual(
            int.from_bytes(burn_ix["data"][4:12], "little"), 19_500_000)
        # The buy itself swapped only the trade amount, not the fee.
        buy = [i for i in parsed["instructions"]
               if i["program_id"] == pumpfun.PUMP_PROGRAM][0]
        self.assertEqual(int.from_bytes(buy["data"][8:16], "little"),
                         expected)

    def test_sell_fee_is_on_proceeds(self):
        # Selling 1,000,000 tokens: ~276,677 lamports of proceeds,
        # so the 3.65% fee is 10,098 lamports — big enough to see.
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          ata_balance=1_000_000_000_000)
        curve = pumpfun.parse_bonding_curve(make_curve())
        expected_sol = pumpfun.sell_quote(curve, 95, 1_000_000_000_000)
        self.assertEqual(expected_sol, 276_677)
        fee = expected_sol * 365 // 10000
        self.assertEqual(fee, 10_098)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "sell", 1_000_000.0, 100.0, self.db,
                                    self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["fee_lamports"], fee)
        self.assertAlmostEqual(result["fee_sol"], fee / 1e9)
        # The split on the proceeds: treasury 276,677 x 170 //
        # 10000 = 4,703; burn = fee - treasury = 5,395. Pump fee
        # on the gross proceeds (279,330 x 95 // 10000) = 2,653.
        self.assertEqual(result["treasury_lamports"], 4_703)
        self.assertEqual(result["burn_lamports"], 5_395)
        self.assertEqual(result["pump_fee_lamports"], 2_653)
        parsed = parse_signed_tx(rpc.sent[0])
        sys_ix = [i for i in parsed["instructions"]
                  if i["program_id"] == SYSTEM_PROGRAM]
        self.assertEqual(len(sys_ix), 2)
        treasury_ix, burn_ix = sys_ix
        self.assertEqual(
            int.from_bytes(treasury_ix["data"][4:12], "little"), 4_703)
        # …and it goes to the configured recipient, not any
        # generated wallet.
        self.assertEqual(treasury_ix["accounts"][1], TEST_FEE_RECIPIENT)
        self.assertEqual(
            int.from_bytes(burn_ix["data"][4:12], "little"), 5_395)
        self.assertEqual(burn_ix["accounts"][1],
                         fees_mod.INCINERATOR_PUBKEY)

    def test_skipped_real_trade_charges_no_fee(self):
        # No bonding curve for this mint: refused, no fee, and no
        # wallet is ever generated.
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=None)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "buy", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("fee_lamports", result)
        self.assertEqual(fees_mod.user_fees(self.db, self.user_id), [])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) AS n FROM treasury_wallet"
            ).fetchone()["n"], 0)


class TestFeeTransferRestriction(unittest.TestCase):
    def test_arbitrary_transfer_still_refused(self):
        transfer = build_transfer_instruction(
            "A" * 44, "B" * 44, 1_000_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed([transfer])

    def test_fee_transfer_to_wrong_destination_refused(self):
        recipient = "T" * 44
        wrong = build_transfer_instruction(
            "A" * 44, "B" * 44, 36_500_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [wrong], {"payer": "A" * 44,
                          "transfers": [(recipient, 36_500_000)]})

    def test_fee_transfer_wrong_amount_refused(self):
        recipient = "T" * 44
        transfer = build_transfer_instruction(
            "A" * 44, recipient, 1_000_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [transfer], {"payer": "A" * 44,
                             "transfers": [(recipient, 36_500_000)]})

    def test_two_fee_transfers_refused(self):
        recipient = "T" * 44
        first = build_transfer_instruction(
            "A" * 44, recipient, 36_500_000)
        second = build_transfer_instruction(
            "A" * 44, recipient, 36_500_000)
        # Two identical transfers against ONE expected transfer:
        # the second has nothing left to match and is refused.
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [first, second],
                {"payer": "A" * 44,
                 "transfers": [(recipient, 36_500_000)]})

    def test_exact_fee_transfer_allowed(self):
        recipient = "T" * 44
        transfer = build_transfer_instruction(
            "A" * 44, recipient, 36_500_000)
        # No raise: the one exact fee transfer is the only allowed
        # System-program instruction.
        _check_instructions_only_allowed(
            [transfer], {"payer": "A" * 44,
                         "transfers": [(recipient, 36_500_000)]})

    def test_treasury_and_burn_transfers_allowed(self):
        # The split: treasury to the recipient + burn to the
        # incinerator, each exactly once, is allowed.
        recipient = "T" * 44
        treasury = build_transfer_instruction(
            "A" * 44, recipient, 17_000_000)
        burn = build_transfer_instruction(
            "A" * 44, fees_mod.INCINERATOR_PUBKEY, 19_500_000)
        _check_instructions_only_allowed(
            [treasury, burn],
            {"payer": "A" * 44,
             "transfers": [(recipient, 17_000_000),
                           (fees_mod.INCINERATOR_PUBKEY, 19_500_000)]})

    def test_burn_to_wrong_destination_refused(self):
        # The burn may go ONLY to the incinerator — the same
        # amount to any other address is refused.
        recipient = "T" * 44
        burn = build_transfer_instruction(
            "A" * 44, recipient, 19_500_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [burn],
                {"payer": "A" * 44,
                 "transfers": [(fees_mod.INCINERATOR_PUBKEY,
                                19_500_000)]})

    def test_burn_wrong_amount_refused(self):
        recipient = "T" * 44
        treasury = build_transfer_instruction(
            "A" * 44, recipient, 17_000_000)
        burn = build_transfer_instruction(
            "A" * 44, fees_mod.INCINERATOR_PUBKEY, 1_000_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [treasury, burn],
                {"payer": "A" * 44,
                 "transfers": [(recipient, 17_000_000),
                               (fees_mod.INCINERATOR_PUBKEY,
                                19_500_000)]})

    def test_crossed_amounts_refused(self):
        # Treasury amount to the incinerator and burn amount to
        # the recipient: both destinations right, amounts swapped
        # — refused, because each transfer must match exactly.
        recipient = "T" * 44
        crossed_treasury = build_transfer_instruction(
            "A" * 44, fees_mod.INCINERATOR_PUBKEY, 17_000_000)
        crossed_burn = build_transfer_instruction(
            "A" * 44, recipient, 19_500_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [crossed_treasury, crossed_burn],
                {"payer": "A" * 44,
                 "transfers": [(recipient, 17_000_000),
                               (fees_mod.INCINERATOR_PUBKEY,
                                19_500_000)]})

    def test_missing_burn_transfer_refused(self):
        # Only the treasury transfer present when two are
        # expected: the missing burn is refused, not silently
        # skipped.
        recipient = "T" * 44
        treasury = build_transfer_instruction(
            "A" * 44, recipient, 17_000_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [treasury],
                {"payer": "A" * 44,
                 "transfers": [(recipient, 17_000_000),
                               (fees_mod.INCINERATOR_PUBKEY,
                                19_500_000)]})


class TestFeeSplitDisplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES ('bob', 'x', 'y')")
        self.db.commit()
        self.bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_recorded_split_sums_to_three_totals(self):
        # A 1 SOL trade recorded with the split: treasury 0.017,
        # burn 0.0195, pump 0.00915325 — the totals sum the
        # recorded per-trade numbers, per user and app-wide.
        fees_mod.record_fee(
            self.db, self.user_id, None, "SOL", "buy", "real", 0.0365,
            fee_lamports=36_500_000, treasury_sol=0.017,
            burn_sol=0.0195, pump_fee_sol=0.00915325,
            treasury_lamports=17_000_000, burn_lamports=19_500_000,
            pump_fee_lamports=9_153_250, signature="sig-split-1")
        totals = fees_mod.user_fee_totals(
            self.db, self.user_id, sol_price_usd=100.0)
        self.assertAlmostEqual(totals["treasury_sol"], 0.017)
        self.assertAlmostEqual(totals["burn_sol"], 0.0195)
        self.assertAlmostEqual(totals["pump_fee_sol"], 0.00915325)
        app = fees_mod.app_fee_totals(self.db, sol_price_usd=100.0)
        self.assertAlmostEqual(app["treasury_sol"], 0.017)
        self.assertAlmostEqual(app["burn_sol"], 0.0195)
        self.assertAlmostEqual(app["pump_fee_sol"], 0.00915325)
        # The single-number totals are still the treasury share.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.017)
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db, sol_price_usd=100.0),
            0.017)
        # The row itself carries the split.
        row = fees_mod.user_fees(self.db, self.user_id)[0]
        self.assertAlmostEqual(row["treasury_sol"], 0.017)
        self.assertEqual(row["burn_lamports"], 19_500_000)
        self.assertEqual(row["pump_fee_lamports"], 9_153_250)

    def test_old_row_without_split_displays_derived(self):
        # A row recorded before the split: treasury derived from
        # the fee (x 170/365), burn 0 (it was collected in full to
        # the wallet — nothing was burned), pump unknown (None,
        # never guessed, left out of the pump total).
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "real", 0.0365, fee_lamports=36_500_000,
                            signature="sig-old-1")
        split = fees_mod.fee_row_split(
            fees_mod.user_fees(self.db, self.user_id)[0], 100.0)
        self.assertAlmostEqual(split["treasury_sol"], 0.017)
        self.assertAlmostEqual(split["burn_sol"], 0.0)
        self.assertIsNone(split["pump_fee_sol"])
        totals = fees_mod.user_fee_totals(
            self.db, self.user_id, sol_price_usd=100.0)
        self.assertAlmostEqual(totals["treasury_sol"], 0.017)
        self.assertAlmostEqual(totals["burn_sol"], 0.0)
        self.assertAlmostEqual(totals["pump_fee_sol"], 0.0)

    def test_paper_split_never_counts(self):
        # Paper fees with a split recorded still never count in
        # the SOL totals — paper money is not SOL paid.
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.0365, fee_usd=3.65,
                            treasury_sol=0.017, burn_sol=0.0195,
                            pump_fee_sol=0.00915325)
        totals = fees_mod.user_fee_totals(
            self.db, self.user_id, sol_price_usd=100.0)
        self.assertAlmostEqual(totals["treasury_sol"], 0.0)
        self.assertAlmostEqual(totals["burn_sol"], 0.0)
        self.assertAlmostEqual(totals["pump_fee_sol"], 0.0)


class TestFeeHomePage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_shows_fees_dropdown_at_zero(self):
        resp = self.client.get("/wallet")
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Fees paid to SolEntry", html)
        self.assertIn("0.0000 SOL", html)
        self.assertIn("No fees paid yet", html)
        self.assertIn('data-live-price="SOL"', html)
        # The fee recipient address is never shown on the home page
        # (James, 2026-11-10).
        self.assertNotIn(TEST_FEE_RECIPIENT, html)

    def test_home_lists_fee_payments(self):
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.01, fee_usd=1.0)
        sig = "sig" + "x" * 40
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature=sig)
        resp = self.client.get("/wallet")
        html = resp.get_data(as_text=True)
        # Only the real fee counts, shown at the treasury's share:
        # 0.02 x 170/365 = 0.009315... SOL. The paper fee is
        # recorded in the ledger but never shown as SOL paid.
        self.assertIn("0.0093 SOL", html)
        self.assertIn("0.009315 SOL", html)
        self.assertNotIn("0.010000 SOL", html)
        self.assertIn(sig[:12], html)

    def test_home_fee_switcher_dropdown(self):
        # James, 2026-11-11: the fees display has a DROPDOWN
        # switcher — the closed control shows the current balance
        # + a chevron, and pressing it drops down the 2 options.
        fees_mod.record_fee(
            self.db, self.user_id, None, "SOL", "buy", "real", 0.0365,
            fee_lamports=36_500_000, treasury_sol=0.017,
            burn_sol=0.0195, pump_fee_sol=0.00915325,
            treasury_lamports=17_000_000, burn_lamports=19_500_000,
            pump_fee_lamports=9_153_250, signature="sig-sw-1")
        html = self.client.get("/wallet").get_data(as_text=True)
        # The dropdown: closed control + chevron + both options.
        self.assertIn("data-fee-switcher-toggle", html)
        self.assertIn("▾", html)
        self.assertIn("SolEntry fee balance", html)
        self.assertIn("Pump.fun fee balance", html)
        self.assertIn('data-fee-switch="solentry"', html)
        self.assertIn('data-fee-switch="pumpfun"', html)
        # Both balances are in the page (SolEntry headline + pump
        # total), and the burned amount stays visible alongside.
        self.assertIn("0.0170 SOL", html)
        self.assertIn("0.0092 SOL", html)
        self.assertIn("Burned:", html)
        self.assertIn("0.0195 SOL", html)
        # The row shows the split too: treasury + burn + pump.
        self.assertIn("0.017000 SOL", html)
        self.assertIn("burned 0.019500 SOL", html)
        self.assertIn("0.009153 SOL", html)

    def test_paper_buy_confirmation_shows_fee(self):
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Fee (3.65%)", html)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["fee_usd"], 0.365)

    def test_paper_sell_confirmation_shows_fee(self):
        self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10"})
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount": "0.05"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Fee (3.65%)", html)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 2)


class TestAppWideTotal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES ('bob', 'x', 'y')")
        self.db.commit()
        self.bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_total_starts_at_zero(self):
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db, sol_price_usd=100.0), 0.0)

    def test_app_total_sums_every_users_fees(self):
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.01, fee_usd=1.0)
        fees_mod.record_fee(self.db, self.bob_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature="sig2")
        # Only bob's REAL fee counts, shown at the treasury's
        # share: 0.02 x 170/365. Alice's paper fee never counts.
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db, sol_price_usd=100.0),
            0.02 * 170 / 365)
        # Each user's own total is unchanged by the other's fees.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.0)
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.bob_id,
                                        sol_price_usd=100.0),
            0.02 * 170 / 365)

    def test_displayed_total_is_treasury_share_of_recorded(self):
        # James, 2026-11-10: the fee that is SHOWN is the
        # treasury's share — 1.7% of the trade, i.e. recorded fee
        # x 170/365. The full 3.65% is still collected; only the
        # display shows the share.
        self.assertEqual(fees_mod.TREASURY_SHARE_BPS_OF_TRADE, 170)
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "real", 0.0365, fee_lamports=36_500_000,
                            signature="sig-share-1")
        # A 1 SOL trade: recorded fee 0.0365 SOL, displayed 0.017.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.017)
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db, sol_price_usd=100.0),
            0.017)
        # The row itself still records the full fee...
        row = fees_mod.user_fees(self.db, self.user_id)[0]
        self.assertAlmostEqual(row["fee_sol"], 0.0365)
        # ...and the row's displayed value is the share.
        self.assertAlmostEqual(
            fees_mod.fee_row_sol(row, 100.0), 0.017)

    def test_api_fees_returns_both_totals(self):
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.01, fee_usd=1.0)
        fees_mod.record_fee(self.db, self.bob_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature="sig2")
        resp = self.client.get("/api/fees")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        # App total: only bob's real fee, at the treasury's share.
        self.assertAlmostEqual(data["app_total_sol"], 0.02 * 170 / 365)
        # Alice has only a paper fee: her displayed total is 0.
        self.assertAlmostEqual(data["user_total_sol"], 0.0)
        self.assertIsNotNone(data["sol_price_usd"])

    def test_api_fees_returns_three_numbers(self):
        fees_mod.record_fee(
            self.db, self.user_id, None, "SOL", "buy", "real", 0.0365,
            fee_lamports=36_500_000, treasury_sol=0.017,
            burn_sol=0.0195, pump_fee_sol=0.00915325,
            treasury_lamports=17_000_000, burn_lamports=19_500_000,
            pump_fee_lamports=9_153_250, signature="sig-api-1")
        fees_mod.record_fee(self.db, self.bob_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature="sig-api-2")
        resp = self.client.get("/api/fees")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        # Alice: recorded split. Bob: old row — treasury derived,
        # burn 0, pump unknown (left out of the pump total).
        self.assertAlmostEqual(data["user_total_sol"], 0.017)
        self.assertAlmostEqual(data["user_treasury_sol"], 0.017)
        self.assertAlmostEqual(data["user_pump_sol"], 0.00915325)
        self.assertAlmostEqual(data["user_burn_sol"], 0.0195)
        self.assertAlmostEqual(data["app_total_sol"],
                               0.017 + 0.02 * 170 / 365)
        self.assertAlmostEqual(data["app_treasury_sol"],
                               0.017 + 0.02 * 170 / 365)
        self.assertAlmostEqual(data["app_pump_sol"], 0.00915325)
        self.assertAlmostEqual(data["app_burn_sol"], 0.0195)

    def test_api_fees_requires_login(self):
        self.client.get("/logout")
        resp = self.client.get("/api/fees")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_home_shows_only_own_fees(self):
        # Correction (James, 2026-11-10): NO app-wide total on the
        # home page and NO Treasury wallet address there — the home
        # page shows each user only their OWN fees, in the dropdown.
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.01, fee_usd=1.0)
        fees_mod.record_fee(self.db, self.bob_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature="sig2")
        html = self.client.get("/wallet").get_data(as_text=True)
        # The flag stays; the app-wide total and its live span are
        # gone, and no other user's fees leak in.
        self.assertIn("🇨🇦", html)
        self.assertNotIn("App fees:", html)
        self.assertNotIn("data-fees-app-total", html)
        self.assertNotIn("0.0300 SOL", html)
        # The user's own total in the dropdown, as a live span —
        # alice has only a paper fee, so her displayed total is 0
        # and the dropdown lists no fees.
        self.assertIn("data-fees-user-total", html)
        self.assertIn("0.0000 SOL", html)
        self.assertNotIn("0.0100 SOL", html)
        self.assertIn("No fees paid yet", html)
        # The recipient address is never shown on the home page,
        # and no wallet is named as the fee destination.
        self.assertNotIn(TEST_FEE_RECIPIENT, html)
        self.assertNotIn("Treasury", html)
        self.assertIn("Every trade pays a 3.65% fee in SOL.", html)

    def test_live_js_polls_fees_every_five_seconds(self):
        js = open("tradingbot/static/live.js").read()
        # One lightweight GET /api/fees per tick, the user's own
        # total only — the home page shows no app-wide total.
        self.assertIn("/api/fees", js)
        self.assertIn("setInterval(fetchFees, FEES_POLL_MS)", js)
        self.assertIn("FEES_POLL_MS = 5000", js)
        self.assertNotIn("data-fees-app-total", js)
        self.assertNotIn("app_total_sol", js)
        # Stops when the page is hidden or closed.
        self.assertIn("visibilitychange", js)
        self.assertIn("pagehide", js)
        # Updates the user's figure; "—" when it is unavailable.
        self.assertIn("data-fees-user-total", js)
        self.assertIn("'—'", js)
        # All three figures poll: SolEntry total, pump.fun total,
        # burned total.
        self.assertIn("data-fees-user-pump-total", js)
        self.assertIn("data-fees-user-burn-total", js)
        self.assertIn("user_pump_sol", js)
        self.assertIn("user_burn_sol", js)
        # The fee-balance switcher dropdown (James, 2026-11-11):
        # closed control + 2 options, switching the fees view.
        self.assertIn("data-fee-switcher-toggle", js)
        self.assertIn("data-fee-switch", js)
        self.assertIn("data-fee-view", js)

    def test_fee_switcher_css(self):
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".fees-switcher-btn", css)
        self.assertIn(".fees-switcher-menu", css)
        self.assertIn(".fees-switcher-chevron", css)
        self.assertIn(".fees-switcher-option", css)
        # The card's data-fee-view decides which balance shows.
        self.assertIn('#fees[data-fee-view="solentry"]', css)
        self.assertIn('#fees[data-fee-view="pumpfun"]', css)

    def test_deleting_bot_keeps_its_fee_rows(self):
        # A bot-managed buy records a fee under the bot; deleting
        # the bot detaches the fee (bot_id NULL) but the user's
        # total — and the app-wide total — still count it. The
        # paper buy's fee is recorded but never counts in the SOL
        # total; a real fee under the same bot does, shown at the
        # treasury's share.
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Luna", "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5", "mode": "paper"})
        self.assertEqual(resp.status_code, 200)
        bot_id = self.db.execute(
            "SELECT id FROM bots WHERE name = 'Luna'").fetchone()["id"]
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(fees_mod.user_fees(self.db, self.user_id)),
                         1)
        fees_mod.record_fee(self.db, self.user_id, bot_id, "SOL", "buy",
                            "real", 0.0365, fee_lamports=36_500_000,
                            signature="sig-del-bot")
        total_before = fees_mod.user_fee_total_sol(
            self.db, self.user_id, sol_price_usd=100.0)
        self.assertAlmostEqual(total_before, 0.017)
        token = csrf(self.client, f"/bots/{bot_id}/delete/confirm")
        resp = self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["bot_id"] is None for r in rows))
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), total_before)


if __name__ == "__main__":
    unittest.main()
