"""The 3.65% trading fee + SolEntry Treasury wallet (fees.py).

Covers: fee math (3.65% of several amounts, rounding), treasury
wallet creation + encryption (the key is never in plaintext
output), paper fee recording, the real-trade fee flow with mocked
signing (no real SOL moves — the RPC is fake, the wallet is a
throwaway keypair, and the signed transaction is decoded and
checked), the home-page fee dropdown, the fee in buy/sell
confirmations, no fee on failed/refused trades, and no fee on
trades that predate the deploy.
"""

import tempfile
import unittest

from helpers import csrf, login, make_client, signup
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


class TestTreasuryWallet(ExecutorBase):
    def test_created_once_and_encrypted(self):
        pubkey = fees_mod.get_or_create_treasury_wallet(self.db, self.cfg)
        self.assertTrue(pubkey)
        # Second call returns the same wallet — no second row.
        self.assertEqual(
            fees_mod.get_or_create_treasury_wallet(self.db, self.cfg),
            pubkey)
        rows = self.db.execute(
            "SELECT * FROM treasury_wallet").fetchall()
        self.assertEqual(len(rows), 1)
        # The stored secret is encrypted, not plaintext: it does not
        # equal the pubkey, and it decrypts back to a key that
        # matches the stored pubkey.
        self.assertNotEqual(rows[0]["encrypted_secret"], pubkey)
        self.assertTrue(
            fees_mod.treasury_wallet_decrypts(self.db, self.cfg))
        # The public address is a valid Solana address shape.
        from tradingbot.solana import is_valid_address
        self.assertTrue(is_valid_address(pubkey))


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
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.00365)

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
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0),
            0.00365 + 0.001825)

    def test_paper_fee_without_sol_price_converts_at_live_price(self):
        paper_buy(self.db, self.user_id, "BTC", 10.0, 100.0,
                  "2026-11-10T00:00:00Z")
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertIsNone(rows[0]["fee_sol"])
        # Without a SOL price the fee is left out of the total,
        # never guessed; with one it converts at that price.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id), 0.0)
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.00365)

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
        treasury = result["treasury"]
        self.assertTrue(treasury)
        # The treasury wallet was created by the trade.
        self.assertEqual(
            fees_mod.get_or_create_treasury_wallet(self.db, self.cfg),
            treasury)
        parsed = parse_signed_tx(rpc.sent[0])
        programs = [i["program_id"] for i in parsed["instructions"]]
        self.assertIn(SYSTEM_PROGRAM, programs)
        # The fee transfer is the last instruction, after the buy.
        self.assertEqual(parsed["instructions"][-1]["program_id"],
                         SYSTEM_PROGRAM)
        fee_ix = [i for i in parsed["instructions"]
                  if i["program_id"] == SYSTEM_PROGRAM][0]
        self.assertEqual(len(fee_ix["accounts"]), 2)
        self.assertEqual(fee_ix["accounts"][0], self.pubkey)
        self.assertEqual(fee_ix["accounts"][1], treasury)
        self.assertEqual(fee_ix["data"][:4], bytes([2, 0, 0, 0]))
        self.assertEqual(
            int.from_bytes(fee_ix["data"][4:12], "little"), 36_500_000)
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
        parsed = parse_signed_tx(rpc.sent[0])
        fee_ix = [i for i in parsed["instructions"]
                  if i["program_id"] == SYSTEM_PROGRAM][0]
        self.assertEqual(int.from_bytes(fee_ix["data"][4:12], "little"),
                         fee)

    def test_skipped_real_trade_charges_no_fee(self):
        # No bonding curve for this mint: refused, no fee, and the
        # treasury wallet is not even created.
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
        treasury = "T" * 44
        wrong = build_transfer_instruction(
            "A" * 44, "B" * 44, 36_500_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [wrong], {"payer": "A" * 44, "treasury": treasury,
                          "lamports": 36_500_000})

    def test_fee_transfer_wrong_amount_refused(self):
        treasury = "T" * 44
        transfer = build_transfer_instruction(
            "A" * 44, treasury, 1_000_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [transfer], {"payer": "A" * 44, "treasury": treasury,
                             "lamports": 36_500_000})

    def test_two_fee_transfers_refused(self):
        treasury = "T" * 44
        first = build_transfer_instruction(
            "A" * 44, treasury, 36_500_000)
        second = build_transfer_instruction(
            "A" * 44, treasury, 36_500_000)
        with self.assertRaises(ValueError):
            _check_instructions_only_allowed(
                [first, second],
                {"payer": "A" * 44, "treasury": treasury,
                 "lamports": 36_500_000})

    def test_exact_fee_transfer_allowed(self):
        treasury = "T" * 44
        transfer = build_transfer_instruction(
            "A" * 44, treasury, 36_500_000)
        # No raise: the one exact fee transfer is the only allowed
        # System-program instruction.
        _check_instructions_only_allowed(
            [transfer], {"payer": "A" * 44, "treasury": treasury,
                         "lamports": 36_500_000})


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
        # The Treasury wallet address is never shown on the home
        # page (James, 2026-11-10) — the dropdown names the wallet,
        # not its address.
        treasury = fees_mod.get_or_create_treasury_wallet(
            self.db, self.app.config["TRADINGBOT_CONFIG"])
        self.assertNotIn(treasury, html)

    def test_home_lists_fee_payments(self):
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "buy",
                            "paper", 0.01, fee_usd=1.0)
        sig = "sig" + "x" * 40
        fees_mod.record_fee(self.db, self.user_id, None, "SOL", "sell",
                            "real", 0.02, fee_lamports=20_000_000,
                            signature=sig)
        resp = self.client.get("/wallet")
        html = resp.get_data(as_text=True)
        self.assertIn("0.0300 SOL", html)
        self.assertIn("0.010000 SOL", html)
        self.assertIn("0.020000 SOL", html)
        self.assertIn(sig[:12], html)
        self.assertIn("paper", html)

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
        self.assertAlmostEqual(
            fees_mod.app_fee_total_sol(self.db, sol_price_usd=100.0),
            0.03)
        # Each user's own total is unchanged by the other's fees.
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), 0.01)
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.bob_id,
                                        sol_price_usd=100.0), 0.02)

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
        self.assertAlmostEqual(data["app_total_sol"], 0.03)
        self.assertAlmostEqual(data["user_total_sol"], 0.01)
        self.assertIsNotNone(data["sol_price_usd"])

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
        # The user's own total in the dropdown, as a live span.
        self.assertIn("data-fees-user-total", html)
        self.assertIn("0.0100 SOL", html)
        # The Treasury wallet address is never shown on the home
        # page — the dropdown names the wallet, not its address.
        self.assertIn("SolEntry Treasury wallet.", html)
        self.assertNotIn("SolEntry Treasury wallet (", html)

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

    def test_deleting_bot_keeps_its_fee_rows(self):
        # A bot-managed buy records a fee under the bot; deleting
        # the bot detaches the fee (bot_id NULL) but the user's
        # total — and the app-wide total — still count it.
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
        total_before = fees_mod.user_fee_total_sol(
            self.db, self.user_id, sol_price_usd=100.0)
        token = csrf(self.client, f"/bots/{bot_id}/delete/confirm")
        resp = self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        rows = fees_mod.user_fees(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["bot_id"])
        self.assertAlmostEqual(
            fees_mod.user_fee_total_sol(self.db, self.user_id,
                                        sol_price_usd=100.0), total_before)


if __name__ == "__main__":
    unittest.main()
