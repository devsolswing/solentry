"""Mode chooser + paper/real split + UI rebuild (HUD, coins list,
coin popup, confirmation-then-back-to-coins) — pump.fun-only.

Rules under test:
- After sign-in the mode chooser shows Demo / Real (devnet); the
  choice persists as the user's active mode and the chooser only
  reappears after a fresh sign-in.
- Paper and real data are fully separated: dashboard, P&L, bots and
  wallet show only the active mode's data.
- ONE wallet for the whole app: the per-user app wallet is the
  pump.fun wallet for send, buy, receive and sell. There are no
  platform wallets any more (no platform_wallets table, no
  /platforms/<platform>/create route, no "Platform wallets" card);
  the app wallet's key is shown never in full and is encrypted
  at rest.
- Every bot trades on pump.fun — the bot-create form has no
  platform to pick, a bogus platform value is ignored, and a
  bot's coins are pump.fun mint addresses only (a symbol such as
  "SOL" is dropped, never stored as a coin to watch).
- The coin popup's Buy / Sell / Send / Receive all end on a
  confirmation that states the results, then the user goes back to
  the coins screen. Coins are pump.fun mints, priced from the
  trenches feed.
- Real popup buys/sells dispatch by bonding-curve completeness
  (see executor.is_curve_complete): graduated -> Jupiter executor,
  still bonding -> pump.fun executor, unreadable -> refused
  plainly, never guessed. A refusal shows plainly and nothing
  is filled.
- The silent loop never executes a non-pump.fun coin: its bots
  watch mint addresses only, and the default real executor
  refuses anything that is not a valid mint address.
No SOL moves in these tests; real execution is a recording mock
and the trenches / Jupiter feeds are mocked.
"""

import json
import re
import tempfile
import unittest
from unittest import mock

import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
import tradingbot.silent_loop as silent_loop
from helpers import csrf, login, make_client, signup
from tradingbot.silent_loop import default_real_executor, run_once
from tradingbot.security import wipe_secret
from tradingbot.wallet_keys import decrypt_wallet_secret
from tradingbot.solana import (
    base58_encode,
    ed25519_publickey_from_seed,
    generate_keypair,
    is_valid_address,
    sol_to_lamports,
)

DEST = "11111111111111111111111111111111"
# Valid pump.fun mint addresses (base58, 32 bytes).
GRAD_MINT = "Grad1111111111111111111111111111111111111111"
BONDING_MINT = "Bond1111111111111111111111111111111111111111"
MINT_PRICES = {GRAD_MINT: 0.002, BONDING_MINT: 0.001}


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def get_account_info(self, pubkey):
        return None

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes=None):
    """changes: {coin: (price, change_24h)} — SOL only, live source."""
    prices = {"SOL": {"price": 100.0, "change_24h": 0.0,
                      "change_1h": 0.0}}
    if changes:
        prices.update({c: {"price": p, "change_24h": ch}
                       for c, (p, ch) in changes.items()})
    return {"prices": prices, "source": "live", "as_of": "test"}


class SplitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self._patches = [
            mock.patch.object(routes, "SolanaRpcClient", FakeRpc),
            mock.patch.object(routes, "get_prices",
                              return_value=make_prices()),
            mock.patch.object(routes, "fetch_prices_for_mints",
                              return_value=dict(MINT_PRICES)),
            mock.patch.object(routes, "fetch_mint_1h_changes",
                              return_value={}),
            mock.patch.object(routes, "fetch_mint_identities",
                              return_value={}),
            mock.patch.object(routes, "ensure_coin_meta",
                              return_value=None),
            mock.patch.object(routes, "enrich_row",
                              side_effect=lambda row, **kw: row),
            mock.patch.object(routes, "fetch_trenches_scan",
                              return_value={"candidates": [],
                                            "prices": {},
                                            "source": "dexscreener",
                                            "as_of": "test",
                                            "error": None}),
            # Paper sells price from a Jupiter quote when one
            # exists; a bonding coin has no route, so the quote
            # fails and the sell falls back to the feed price.
            mock.patch.object(jupiter_mod, "quote_sell_mint",
                              side_effect=jupiter_mod.JupiterError(
                                  "no route")),
        ]
        for p in self._patches:
            p.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.wallet_pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def _html(self, path):
        return self.client.get(path).get_data(as_text=True)

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _create_bot(self, name="Luna", mode="paper",
                    silent=0, coins=GRAD_MINT):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": "balanced",
            "platform": "pump.fun",
            "coins": coins, "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": mode})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = ? AND user_id = ?",
            (name, self.user_id)).fetchone()
        self.assertIsNotNone(bot, f"bot {name} was not created")
        # Every bot trades on pump.fun — there is no other
        # platform to pick.
        self.assertEqual(bot["platform"], "pump.fun")
        if silent:
            self.db.execute("UPDATE bots SET silent = 1 WHERE id = ?",
                            (bot["id"],))
            self.db.commit()
        return bot["id"]

    def _paper_buy(self, coin=GRAD_MINT, usd="10"):
        return self.client.post(f"/coins/{coin}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "usd": usd, "bot_id": ""})


class TestModeChooser(SplitBase):
    def test_chooser_shows_after_signup(self):
        html = self._html("/dashboard")
        self.assertIn("How do you want to trade?", html)
        self.assertIn("Demo trading", html)
        self.assertIn("Real trading — devnet", html)
        self.assertIn("SolEntry", html)

    def test_choose_paper_persists_and_chooser_goes_away(self):
        self._choose_mode("paper")
        self.assertEqual(self.db.execute(
            "SELECT active_mode FROM users WHERE id = ?",
            (self.user_id,)).fetchone()["active_mode"], "paper")
        self.assertNotIn("How do you want to trade?", self._html("/dashboard"))

    def test_choose_devnet_persists_and_labels_real(self):
        self._choose_mode("devnet")
        self.assertEqual(self.db.execute(
            "SELECT active_mode FROM users WHERE id = ?",
            (self.user_id,)).fetchone()["active_mode"], "devnet")
        html = self._html("/dashboard")
        self.assertIn("Real trading — devnet", html)
        self.assertNotIn("How do you want to trade?", html)

    def test_invalid_choice_changes_nothing(self):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": "mainnet"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.execute(
            "SELECT active_mode FROM users WHERE id = ?",
            (self.user_id,)).fetchone()["active_mode"], "paper")
        # Chooser still shows — the user still has not chosen.
        self.assertIn("How do you want to trade?", self._html("/dashboard"))

    def test_chooser_shows_again_after_relogin(self):
        self._choose_mode("paper")
        self.assertNotIn("How do you want to trade?", self._html("/dashboard"))
        self.client.get("/logout")
        login(self.client, "alice")
        self.assertIn("How do you want to trade?", self._html("/dashboard"))


class TestModeSeparation(SplitBase):
    def setUp(self):
        super().setUp()
        # $10 of the graduated mint at $0.002 = 5,000 tokens;
        # $90.00 paper cash remains, portfolio total $100.00.
        self._paper_buy(GRAD_MINT, "10")
        self.paper_bot = self._create_bot("Luna", mode="paper")
        self.real_bot = self._create_bot("Nova", mode="real")

    def test_paper_data_hidden_in_real_mode(self):
        self._choose_mode("devnet")
        dash = self._html("/dashboard")
        # The paper bot and the paper holding must not leak into real.
        self.assertNotIn("Luna", dash)
        self.assertNotIn("5000.0000", dash)
        self.assertNotIn(GRAD_MINT, dash)
        self.assertIn("Nova", self._html("/bots"))
        self.assertNotIn("Luna", self._html("/bots"))
        # The home page must not leak the paper portfolio into real
        # mode (the old /pnl page, where this was checked, is gone).
        home = self._html("/wallet")
        self.assertIn("Real trading — devnet", home)
        self.assertNotIn("$100.00 CAD", home)

    def test_real_data_hidden_in_paper_mode(self):
        self._choose_mode("paper")
        self.assertNotIn("Nova", self._html("/bots"))
        self.assertNotIn("Nova", self._html("/dashboard"))
        self.assertIn("Luna", self._html("/bots"))
        # The home page shows the paper portfolio in paper mode
        # (the old /pnl page, where this was checked, is gone).
        home = self._html("/wallet")
        self.assertIn("Demo trading (paper)", home)
        self.assertIn("$100.00 CAD", home)

    def test_activity_rows_are_mode_tagged_and_filtered(self):
        rows = self.db.execute(
            "SELECT * FROM activity WHERE user_id = ? AND kind = 'paper_buy'",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "paper")
        self._choose_mode("devnet")
        self.assertNotIn("paper_buy", self._html("/dashboard"))
        self._choose_mode("paper")
        self.assertIn("paper_buy", self._html("/dashboard"))

    def test_wallet_page_branches_by_mode(self):
        self._choose_mode("paper")
        paper = self._html("/wallet")
        # The old "Paper balance: $X CAD cash" summary line is
        # gone (2026-11-11 compact): the paper cash is the
        # portfolio total in the balance box. This class buys
        # $10 of paper mint in setUp, so $90.00 cash remains.
        self.assertIn('data-live-cash="90.0"', paper)
        self.assertIn("Demo trading (paper)", paper)
        self.assertNotIn("Send history", paper)
        self._choose_mode("devnet")
        wallet = self._html("/wallet")
        self.assertNotIn("Deposit", wallet)
        self.assertNotIn("Send history", wallet)
        details = self._html("/wallet/details")
        self.assertIn("Deposit", details)
        self.assertIn("Send history", details)
        # One wallet for the whole app: the pump.fun trading
        # wallet is the app wallet — no "Platform wallets" card.
        self.assertIn("Pump.fun trading wallet", details)
        self.assertIn(self.wallet_pubkey, details)
        self.assertNotIn("Platform wallets", details)

    def test_mode_switch_in_hud_flips_dashboard(self):
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": "devnet"},
            follow_redirects=True)
        self.assertIn("Real trading — devnet", self._html("/dashboard"))
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": "paper"},
            follow_redirects=True)
        self.assertIn("Demo trading (paper)", self._html("/dashboard"))


class TestSingleWallet(SplitBase):
    """The platform-wallet feature is deleted: one app wallet,
    the pump.fun wallet, for send / buy / receive / sell."""

    def test_platform_wallet_create_route_gone(self):
        for platform in ("pump.fun", "phantom", "pumpswap", "nope"):
            resp = self.client.post(
                f"/platforms/{platform}/create", data={
                    "csrf_token": csrf(self.client, "/bots/create"),
                    "return_to": "/bots/create"})
            self.assertEqual(resp.status_code, 404, platform)
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'platform_wallets'").fetchone()
        self.assertIsNone(row)

    def test_one_wallet_for_the_whole_app(self):
        self._choose_mode("devnet")
        html = self._html("/wallet/details")
        self.assertIn("Pump.fun trading wallet", html)
        self.assertIn(self.wallet_pubkey, html)
        self.assertNotIn("Platform wallets", html)
        self.assertNotIn("platform_wallets", html)
        self.assertNotIn("Create pump.fun wallet", html)
        self.assertNotIn("Create Phantom wallet", html)
        # The users row links the same address as the pump.fun
        # wallet (backfilled from the app wallet).
        linked = self.db.execute(
            "SELECT pumpfun_wallet FROM users WHERE id = ?",
            (self.user_id,)).fetchone()["pumpfun_wallet"]
        self.assertEqual(linked, self.wallet_pubkey)

    def test_app_wallet_key_encrypted_at_rest(self):
        row = self.db.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row)
        # The stored blob is ciphertext, never the key itself.
        self.assertNotIn(row["pubkey"], row["encrypted_secret"])
        # v2 wallets are encrypted with the per-user key
        # (password+email), unwrapped with the server key — the
        # server key alone does not decrypt the blob.
        secret = decrypt_wallet_secret(self.db, self.user_id,
                                       self.cfg.encryption_key)
        self.assertIsNotNone(secret)
        try:
            self.assertEqual(len(secret), 64)
            self.assertEqual(
                base58_encode(ed25519_publickey_from_seed(secret[:32])),
                row["pubkey"])
        finally:
            wipe_secret(secret)

    def test_other_user_cannot_see_my_app_wallet(self):
        self.client.post("/logout")
        signup(self.client, "bob")
        self.assertNotIn(self.wallet_pubkey, self._html("/bots/create"))
        self.assertNotIn(self.wallet_pubkey, self._html("/wallet"))

    def test_bot_create_is_builderbot_chatbox(self):
        # /bots/create is now the builderBot chatbox: it asks the
        # platform as a question, so there is no platform selector
        # (and no "Coins to watch") on the page itself.
        html = self._html("/bots/create")
        self.assertIn("builderBot", html)
        self.assertIn("Create a bot", html)
        self.assertNotIn('name="platform"', html)
        self.assertNotIn("Coins to watch", html)

    def test_create_bot_is_always_pumpfun_even_with_bogus_platform(self):
        # The platform form field is ignored entirely now: a
        # bogus value does not reject the bot, it just never
        # lands — the bot is pump.fun.
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Bogus", "strategy": "balanced",
            "platform": "bogus",
            "coins": GRAD_MINT, "buy_size_pct": "20",
            "stop_loss_pct": "7", "mode": "paper"})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Bogus'").fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["platform"], "pump.fun")
        self.assertEqual(json.loads(bot["coins"]), [GRAD_MINT])

    def test_create_bot_drops_symbol_coins_keeps_mints_only(self):
        bot_id = self._create_bot("Swapper", mode="paper",
                                  coins="SOL,BTC," + GRAD_MINT)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        self.assertEqual(bot["platform"], "pump.fun")
        self.assertEqual(json.loads(bot["coins"]), [GRAD_MINT])

    def test_trenches_preset_is_pumpfun(self):
        resp = self.client.post("/bots/preset/trenches", data={
            "csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE category = 'Memecoin build'"
        ).fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["platform"], "pump.fun")


class TestCoinPopup(SplitBase):
    def test_popup_renders_actions(self):
        html = self._html(f"/coins/{GRAD_MINT}")
        self.assertIn("Buy", html)
        self.assertIn("Sell", html)
        self.assertIn("Send", html)
        self.assertIn("Receive", html)
        self.assertIn("Back to coins", html)
        # An unnamed mint shows "Unknown coin" — never its
        # address as a name.
        self.assertIn("Unknown coin", html)

    def test_popup_shows_real_name_and_ticker(self):
        with mock.patch.object(
                routes, "ensure_coin_meta",
                return_value={"name": "Test Coin", "symbol": "TEST",
                              "image_url": None}):
            html = self._html(f"/coins/{GRAD_MINT}")
        self.assertIn("Test Coin", html)
        self.assertIn("(TEST)", html)

    def test_popup_unknown_coin_404(self):
        self.assertEqual(self.client.get("/coins/NOPE").status_code, 404)

    def test_popup_symbol_coins_404(self):
        # SOL is the unit of account, never a listed coin; majors
        # are gone entirely.
        for coin in ("SOL", "BTC", "ETH", "ZEC", "USDC"):
            self.assertEqual(self.client.get(f"/coins/{coin}").status_code,
                             404, coin)

    def test_paper_buy_shows_confirmation_then_back(self):
        resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("New paper cash", html)
        self.assertIn("Back to coins", html)
        row = self.db.execute(
            "SELECT * FROM paper_holdings WHERE coin_id = ?",
            (GRAD_MINT,)).fetchone()
        self.assertIsNotNone(row)
        # $10 at the $0.002 feed price = 5,000 tokens.
        self.assertAlmostEqual(row["amount"], 5000.0)

    def test_paper_buy_refusal_is_a_confirmation_not_a_fill(self):
        resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "usd": "999999", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Not enough paper cash", html)
        self.assertIn("Back to coins", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings").fetchone()["c"], 0)

    def test_paper_sell_shows_confirmation(self):
        self._paper_buy(GRAD_MINT, "10")
        resp = self.client.post(f"/coins/{GRAD_MINT}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "amount": "0.05"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("New paper cash", html)
        self.assertIn("Back to coins", html)

    def test_real_buy_without_bot_refused_plainly(self):
        self._choose_mode("devnet")
        resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "amount_sol": "0.1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Pick one of your real bots", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)

    def test_real_buy_of_sol_refused_no_fill(self):
        # SOL is not a coin: its popup, detail and buy are all 404.
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_sol": "0.1", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)

    def test_real_buy_unreadable_curve_refused_plainly(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        with mock.patch.object(routes, "is_curve_complete",
                               return_value=None), \
                mock.patch.object(routes, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(routes, "execute_real_trade") \
                as pump_exec:
            resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
                "amount_sol": "0.1", "bot_id": str(bot_id)})
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Bonding curve unavailable", html)
        self.assertIn("Back to coins", html)
        self.assertEqual(jup_exec.call_count, 0)
        self.assertEqual(pump_exec.call_count, 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)

    def test_real_buy_bonding_mint_goes_through_pumpfun_executor(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real",
                                  coins=BONDING_MINT)
        calls = []

        def fake_execute(*args, **kwargs):
            calls.append(args)
            return {"status": "executed", "signature": "fakesigXYZ",
                    "coin_amount": 12.5}

        # The app wallet needs enough SOL for the buy-size limit check.
        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            with mock.patch.object(routes, "is_curve_complete",
                                   return_value=False), \
                    mock.patch.object(routes, "execute_jupiter_swap") \
                    as jup_exec, \
                    mock.patch.object(routes, "execute_real_trade",
                                      fake_execute):
                resp = self.client.post(f"/coins/{BONDING_MINT}/buy",
                                        data={
                    "csrf_token": csrf(
                        self.client, f"/coins/{BONDING_MINT}"),
                    "amount_sol": "0.1", "bot_id": str(bot_id)})
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(jup_exec.call_count, 0)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("fakesigXYZ", html)
        self.assertIn("pump.fun", html)
        self.assertIn("Back to coins", html)
        row = self.db.execute(
            "SELECT * FROM real_holdings WHERE coin_id = ?",
            (BONDING_MINT,)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["amount"], 12.5)

    def test_real_buy_graduated_mint_goes_through_jupiter(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            with mock.patch.object(routes, "is_curve_complete",
                                   return_value=True), \
                    mock.patch.object(
                        routes, "execute_jupiter_swap",
                        return_value={"status": "executed",
                                      "coin_amount": 0.5,
                                      "signature": "sigJUP",
                                      "signing_bot": "SB1"}) as jup_exec, \
                    mock.patch.object(routes, "execute_real_trade") \
                    as pump_exec:
                resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
                    "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
                    "amount_sol": "0.1", "bot_id": str(bot_id)})
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(jup_exec.call_count, 1)
        self.assertEqual(pump_exec.call_count, 0)
        self.assertEqual(jup_exec.call_args[0][2], GRAD_MINT)
        self.assertEqual(jup_exec.call_args[0][3], "buy")
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("sigJUP", html)
        self.assertIn("Jupiter", html)
        self.assertIn("Back to coins", html)

    def test_real_sell_with_mocked_executor_fills_and_confirms(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real",
                                  coins=BONDING_MINT)
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, BONDING_MINT, 10.0, 0.01))
        self.db.commit()

        def fake_execute(*args, **kwargs):
            return {"status": "executed", "signature": "fakesigSELL",
                    "coin_amount": 4.0}

        with mock.patch.object(routes, "is_curve_complete",
                               return_value=False), \
                mock.patch.object(routes, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(routes, "execute_real_trade",
                                  fake_execute):
            resp = self.client.post(f"/coins/{BONDING_MINT}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{BONDING_MINT}"),
                "amount": "4", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(jup_exec.call_count, 0)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("fakesigSELL", html)
        self.assertIn("Back to coins", html)
        row = self.db.execute(
            "SELECT amount FROM real_holdings WHERE coin_id = ?",
            (BONDING_MINT,)).fetchone()
        self.assertAlmostEqual(row["amount"], 6.0)

    def test_real_sell_graduated_mint_goes_through_jupiter(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GRAD_MINT, 10.0, 0.01))
        self.db.commit()
        with mock.patch.object(routes, "is_curve_complete",
                               return_value=True), \
                mock.patch.object(
                    routes, "execute_jupiter_swap",
                    return_value={"status": "executed",
                                  "coin_amount": 4.0,
                                  "signature": "sigJUPSELL",
                                  "signing_bot": "SB1"}) as jup_exec, \
                mock.patch.object(routes, "execute_real_trade") \
                as pump_exec:
            resp = self.client.post(f"/coins/{GRAD_MINT}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
                "amount": "4", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(jup_exec.call_count, 1)
        self.assertEqual(pump_exec.call_count, 0)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("sigJUPSELL", html)
        self.assertIn("Jupiter", html)
        row = self.db.execute(
            "SELECT amount FROM real_holdings WHERE coin_id = ?",
            (GRAD_MINT,)).fetchone()
        self.assertAlmostEqual(row["amount"], 6.0)

    def test_real_sell_of_bot_managed_coin_refused(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, ?, ?, ?, 'real')",
            (self.user_id, bot_id, mint, 5.0, 0.01))
        self.db.commit()
        resp = self.client.post(f"/coins/{mint}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{mint}"),
            "amount": "1", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was sold", html)
        self.assertIn("bot is managing it", html)
        self.assertIn("Back to coins", html)

    def test_send_from_popup_returns_to_coins_after_confirm(self):
        intent_resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL",
            "return_to": "coins"})
        self.assertEqual(intent_resp.status_code, 302)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1").fetchone()
        confirm_html = self.client.get(
            f"/wallet/send/{intent['id']}/confirm?return_to=coins"
        ).get_data(as_text=True)
        self.assertIn("Back to coins", confirm_html)
        resp = self.client.post(
            f"/wallet/send/{intent['id']}/confirm?return_to=coins",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])


class TestHud(SplitBase):
    def test_hud_on_every_main_page(self):
        for path in ("/dashboard", "/bots", "/wallet", "/logs", "/settings"):
            html = self._html(path)
            self.assertIn("hud-item", html, path)
            # Four-button nav: Home / Bots / Wallet / Settings.
            for label in ("Home", "Bots", "Wallet", "Settings"):
                self.assertIn(label, html, f"{path} missing {label}")
            self.assertIn('action="/mode"', html, path)

    def test_hud_not_on_login_page(self):
        self.client.get("/logout")
        html = self._html("/login")
        self.assertNotIn("hud-item", html)

    def test_app_shell_styles_exist(self):
        css = open("tradingbot/static/style.css").read()
        for token in (".app-shell", ".hud", ".coin-list", ".coin-row",
                      ".overlay", ".chooser-card", ".coin-popup",
                      ".mode-banner", ".platform-row"):
            self.assertIn(token, css)


class TestLoopCurveDispatch(SplitBase):
    """The platform gate is gone — every bot is pump.fun now.
    The silent loop dispatches real execution by bonding-curve
    completeness, and it can only ever watch pump.fun mints."""

    def _run(self, prices, executor, change_24h=5.0):
        with mock.patch.object(
                silent_loop, "fetch_prices_for_mints",
                return_value={GRAD_MINT: 0.002,
                              BONDING_MINT: 0.001}), \
                mock.patch.object(
                    silent_loop, "fetch_mint_24h_changes",
                    return_value={GRAD_MINT: change_24h,
                                  BONDING_MINT: change_24h}), \
                mock.patch.object(silent_loop, "ensure_coin_meta",
                                  return_value=None):
            return run_once(
                self.db, self.cfg,
                price_fetcher=lambda: prices,
                rpc_factory=lambda url: FakeRpc(url, 0),
                real_executor=executor)

    def _bot_row(self, bot_id):
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()

    def test_real_bot_with_bonding_mint_reaches_executor(self):
        self._create_bot("Nova", mode="real", silent=1,
                         coins=BONDING_MINT)
        calls = []

        def executor(*args, **kwargs):
            calls.append(args)
            return {"status": "executed", "signature": "fakesig",
                    "coin_amount": 100.0}

        # The loop checks the wallet balance before buying, so the
        # wallet needs funds for the buy to reach the executor.
        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            self._run(make_prices(), executor)
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], BONDING_MINT)
        self.assertEqual(calls[0][3], "buy")

    def test_real_bot_with_no_buy_signal_never_executes(self):
        self._create_bot("Nova", mode="real", silent=1,
                         coins=BONDING_MINT)
        calls = []

        def executor(*args, **kwargs):
            calls.append(args)
            return {"status": "executed", "signature": "fakesig",
                    "coin_amount": 100.0}

        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            self._run(make_prices(), executor, change_24h=0.0)
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(calls, [])

    def test_non_mint_coin_never_executes(self):
        # A bot whose coins list holds "SOL" (a symbol, not a
        # mint) can never trade it: the loop prices mints only,
        # so the symbol has no price and no signal. (Bot-create
        # drops symbols before they are even stored.)
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, "
            "max_open_trades, api_token_hash, platform) "
            "VALUES (?, 'Legacy', 'balanced', ?, 20, 7, 'real', 1, "
            "3, 'tok', 'pump.fun')",
            (self.user_id, json.dumps(["SOL"])))
        self.db.commit()
        self.assertIsNotNone(cur.lastrowid)
        calls = []

        def executor(*args, **kwargs):
            calls.append(args)
            return {"status": "executed", "signature": "fakesig",
                    "coin_amount": 100.0}

        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            self._run(make_prices(), executor)
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(calls, [])

    def test_default_executor_graduated_mint_goes_to_jupiter(self):
        bot_id = self._create_bot("Nova", mode="real")
        bot = self._bot_row(bot_id)
        with mock.patch.object(silent_loop, "SolanaRpcClient",
                               FakeRpc), \
                mock.patch.object(silent_loop, "is_curve_complete",
                                  return_value=True), \
                mock.patch.object(
                    silent_loop, "execute_jupiter_swap",
                    return_value={"status": "executed",
                                  "coin_amount": 0.5,
                                  "signature": "sigJUP"}) as jup_exec, \
                mock.patch.object(silent_loop, "execute_real_trade") \
                as pump_exec:
            result = default_real_executor(
                bot, self.user_id, GRAD_MINT, "buy", 0.1, 0.002,
                self.db, self.cfg)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(jup_exec.call_count, 1)
        self.assertEqual(pump_exec.call_count, 0)
        self.assertEqual(jup_exec.call_args[0][2], GRAD_MINT)

    def test_default_executor_bonding_mint_goes_to_pumpfun(self):
        bot_id = self._create_bot("Nova", mode="real")
        bot = self._bot_row(bot_id)
        with mock.patch.object(silent_loop, "SolanaRpcClient",
                               FakeRpc), \
                mock.patch.object(silent_loop, "is_curve_complete",
                                  return_value=False), \
                mock.patch.object(silent_loop, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(
                    silent_loop, "execute_real_trade",
                    return_value={"status": "executed",
                                  "coin_amount": 12.5,
                                  "signature": "sigPF"}) as pump_exec:
            result = default_real_executor(
                bot, self.user_id, BONDING_MINT, "buy", 0.1, 0.001,
                self.db, self.cfg)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(pump_exec.call_count, 1)
        self.assertEqual(jup_exec.call_count, 0)
        self.assertEqual(pump_exec.call_args[0][2], BONDING_MINT)

    def test_default_executor_unreadable_curve_refuses(self):
        bot_id = self._create_bot("Nova", mode="real")
        bot = self._bot_row(bot_id)
        with mock.patch.object(silent_loop, "SolanaRpcClient",
                               FakeRpc), \
                mock.patch.object(silent_loop, "is_curve_complete",
                                  return_value=None), \
                mock.patch.object(silent_loop, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(silent_loop, "execute_real_trade") \
                as pump_exec:
            result = default_real_executor(
                bot, self.user_id, GRAD_MINT, "buy", 0.1, 0.002,
                self.db, self.cfg)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("bonding curve unavailable", result["reason"])
        self.assertEqual(jup_exec.call_count, 0)
        self.assertEqual(pump_exec.call_count, 0)

    def test_default_executor_refuses_non_mint_coin(self):
        bot_id = self._create_bot("Nova", mode="real")
        bot = self._bot_row(bot_id)
        with mock.patch.object(silent_loop, "is_curve_complete") \
                as curve, \
                mock.patch.object(silent_loop, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(silent_loop, "execute_real_trade") \
                as pump_exec:
            result = default_real_executor(
                bot, self.user_id, "SOL", "buy", 0.1, 100.0,
                self.db, self.cfg)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("valid token address", result["reason"])
        self.assertEqual(curve.call_count, 0)
        self.assertEqual(jup_exec.call_count, 0)
        self.assertEqual(pump_exec.call_count, 0)


if __name__ == "__main__":
    unittest.main()
