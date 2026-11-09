"""Platform picker + paper/real split + UI rebuild (HUD, coins list,
coin popup, confirmation-then-back-to-coins).

Rules under test:
- After sign-in the mode chooser shows Demo / Real (devnet); the
  choice persists as the user's active mode and the chooser only
  reappears after a fresh sign-in.
- Paper and real data are fully separated: dashboard, P&L, bots and
  wallet show only the active mode's data.
- One wallet per platform per user; the Phantom key is shown exactly
  once; keys are encrypted at rest.
- The coin popup's Buy / Sell / Send / Receive all end on a
  confirmation that states the results, then the user goes back to
  the coins screen.
- Real popup buys/sells go through the existing pump.fun executor
  with the same guards as the silent loop — a refusal shows plainly
  and nothing is filled.
- The silent loop never executes a real bot whose platform is not
  pump.fun.
No SOL moves in these tests; real execution is a recording mock.
"""

import re
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.silent_loop import run_once
from tradingbot.security import decrypt_secret
from tradingbot.solana import (
    base58_encode,
    ed25519_publickey_from_seed,
    generate_keypair,
    is_valid_address,
    sol_to_lamports,
)

DEST = "11111111111111111111111111111111"


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_recent_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes):
    """changes: {coin: (price, change_24h)}"""
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


class SplitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        return self.client.get(path).get_data(as_text=True)

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _create_bot(self, name="Luna", mode="paper", platform="pump.fun",
                    silent=0, coins="SOL"):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": "balanced", "platform": platform,
            "coins": coins, "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": mode})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = ? AND user_id = ?",
            (name, self.user_id)).fetchone()
        self.assertIsNotNone(bot, f"bot {name} was not created")
        if silent:
            self.db.execute("UPDATE bots SET silent = 1 WHERE id = ?",
                            (bot["id"],))
            self.db.commit()
        return bot["id"]

    def _paper_buy(self, coin="SOL", usd="10"):
        return self.client.post(f"/coins/{coin}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "usd": usd, "bot_id": ""})

    def _platform_create(self, platform, return_to="/bots/create"):
        return self.client.post(f"/platforms/{platform}/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "return_to": return_to})


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
        self._paper_buy("SOL", "10")
        self.paper_bot = self._create_bot("Luna", mode="paper")
        self.real_bot = self._create_bot("Nova", mode="real")

    def test_paper_data_hidden_in_real_mode(self):
        self._choose_mode("devnet")
        dash = self._html("/dashboard")
        # The paper bot and the paper holding must not leak into real.
        self.assertNotIn("Luna", dash)
        self.assertNotIn("100.0000", dash)
        self.assertIn("Nova", self._html("/bots"))
        self.assertNotIn("Luna", self._html("/bots"))
        pnl = self._html("/pnl")
        self.assertNotIn("Balance — paper portfolio", pnl)
        self.assertIn("Real app wallet", pnl)

    def test_real_data_hidden_in_paper_mode(self):
        self._choose_mode("paper")
        self.assertNotIn("Nova", self._html("/bots"))
        self.assertNotIn("Nova", self._html("/dashboard"))
        self.assertIn("Luna", self._html("/bots"))
        pnl = self._html("/pnl")
        self.assertIn("Balance — paper portfolio", pnl)
        self.assertNotIn("Real app wallet", pnl)

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
        self.assertIn("Paper balance", self._html("/wallet"))
        self.assertNotIn("Send history", self._html("/wallet"))
        self._choose_mode("devnet")
        wallet = self._html("/wallet")
        self.assertIn("Deposit", wallet)
        self.assertIn("Platform wallets", wallet)
        self.assertIn("Send history", wallet)

    def test_mode_switch_in_hud_flips_dashboard(self):
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": "devnet"},
            follow_redirects=True)
        self.assertIn("Real trading — devnet", self._html("/dashboard"))
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": "paper"},
            follow_redirects=True)
        self.assertIn("Demo trading (paper)", self._html("/dashboard"))


class TestPlatformWallets(SplitBase):
    def test_create_one_per_platform(self):
        for p in ("pump.fun", "phantom", "pumpswap"):
            resp = self._platform_create(p)
            self.assertIn(resp.status_code, (200, 302))
        rows = self.db.execute(
            "SELECT platform, pubkey FROM platform_wallets "
            "WHERE user_id = ?", (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["pubkey"] for r in rows}), 3)
        for r in rows:
            self.assertTrue(is_valid_address(r["pubkey"]))

    def test_second_create_keeps_the_same_wallet(self):
        self._platform_create("pump.fun")
        first = self.db.execute(
            "SELECT pubkey FROM platform_wallets WHERE user_id = ? "
            "AND platform = 'pump.fun'", (self.user_id,)).fetchone()["pubkey"]
        resp = self._platform_create("pump.fun")
        self.assertEqual(resp.status_code, 302)
        rows = self.db.execute(
            "SELECT * FROM platform_wallets WHERE user_id = ? "
            "AND platform = 'pump.fun'", (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pubkey"], first)

    def test_phantom_key_shown_once_then_never_again(self):
        resp = self._platform_create("phantom")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("shown once", html)
        self.assertIn("Import it into Phantom", html)
        # The page shows two addresses: the wallet address, then the
        # private key. Capture the key so we can prove it never
        # appears anywhere else.
        addrs = re.findall(r'<p class="addr">([^<]+)</p>', html)
        self.assertEqual(len(addrs), 2)
        private_key = addrs[1]
        self.assertGreater(len(private_key), 80)
        # A second create just redirects — the key is never re-shown.
        resp2 = self._platform_create("phantom")
        self.assertEqual(resp2.status_code, 302)
        self.assertNotIn(private_key, resp2.get_data(as_text=True))
        bot_form = self._html("/bots/create")
        self.assertIn("Wallet ready.", bot_form)
        self.assertNotIn(private_key, bot_form)
        self.assertNotIn(private_key, self._html("/wallet"))

    def test_keys_encrypted_at_rest(self):
        self._platform_create("pump.fun")
        row = self.db.execute(
            "SELECT * FROM platform_wallets WHERE user_id = ? "
            "AND platform = 'pump.fun'", (self.user_id,)).fetchone()
        self.assertNotIn(row["pubkey"], row["encrypted_secret"])
        seed = decrypt_secret(row["encrypted_secret"],
                              self.cfg.encryption_key)
        self.assertEqual(len(seed), 64)
        self.assertEqual(base58_encode(ed25519_publickey_from_seed(seed[:32])),
                         row["pubkey"])

    def test_other_user_cannot_see_my_platform_wallet(self):
        self._platform_create("pump.fun")
        pubkey = self.db.execute(
            "SELECT pubkey FROM platform_wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]
        self.client.post("/logout")
        signup(self.client, "bob")
        self.assertNotIn(pubkey, self._html("/bots/create"))
        self.assertNotIn(pubkey, self._html("/wallet"))

    def test_invalid_platform_rejected(self):
        resp = self._platform_create("nope")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM platform_wallets").fetchone()["c"], 0)

    def test_bot_form_has_platform_selector_not_coins_to_watch(self):
        html = self._html("/bots/create")
        self.assertIn('name="platform"', html)
        self.assertIn("pump.fun", html)
        self.assertIn("Phantom", html)
        self.assertIn("pumpSwap", html)
        self.assertIn("Create", html)
        self.assertNotIn("Coins to watch", html)

    def test_create_bot_stores_platform(self):
        bot_id = self._create_bot("Swapper", mode="paper", platform="pumpswap")
        self.assertEqual(self.db.execute(
            "SELECT platform FROM bots WHERE id = ?",
            (bot_id,)).fetchone()["platform"], "pumpswap")

    def test_create_bot_rejects_invalid_platform(self):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Bogus", "strategy": "balanced", "platform": "bogus",
            "coins": "SOL", "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": "paper"})
        # Invalid platform: back to the form, nothing created.
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE name = 'Bogus'"
        ).fetchone()["c"], 0)

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
        html = self._html("/coins/SOL")
        self.assertIn("Buy", html)
        self.assertIn("Sell", html)
        self.assertIn("Send", html)
        self.assertIn("Receive", html)
        self.assertIn("Back to coins", html)
        self.assertIn("Solana", html)
        self.assertIn("(SOL)", html)

    def test_popup_unknown_coin_404(self):
        self.assertEqual(self.client.get("/coins/NOPE").status_code, 404)

    def test_paper_buy_shows_confirmation_then_back(self):
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("New paper cash", html)
        self.assertIn("Back to coins", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE coin_id = 'SOL'"
        ).fetchone()["c"], 1)

    def test_paper_buy_refusal_is_a_confirmation_not_a_fill(self):
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "999999", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Not enough paper cash", html)
        self.assertIn("Back to coins", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings").fetchone()["c"], 0)

    def test_paper_sell_shows_confirmation(self):
        self._paper_buy("SOL", "10")
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"), "amount": "0.05"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("New paper cash", html)
        self.assertIn("Back to coins", html)

    def test_real_buy_without_bot_refused_plainly(self):
        self._choose_mode("devnet")
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_sol": "0.1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Pick one of your real bots", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)

    def test_real_buy_of_non_pumpfun_coin_refused_no_fill(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_sol": "0.1", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Back to coins", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)

    def test_real_buy_with_mocked_executor_fills_and_confirms(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        calls = []

        def fake_execute(*args, **kwargs):
            calls.append(args)
            return {"status": "executed", "signature": "fakesigXYZ",
                    "coin_amount": 12.5}

        # The app wallet needs enough SOL for the buy-size limit check.
        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            with mock.patch.object(routes, "execute_real_trade", fake_execute):
                resp = self.client.post(f"/coins/{mint}/buy", data={
                    "csrf_token": csrf(self.client, f"/coins/{mint}"),
                    "amount_sol": "0.1", "bot_id": str(bot_id)})
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 1)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("fakesigXYZ", html)
        self.assertIn("Back to coins", html)
        row = self.db.execute(
            "SELECT * FROM real_holdings WHERE coin_id = ?",
            (mint,)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["amount"], 12.5)

    def test_real_sell_with_mocked_executor_fills_and_confirms(self):
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, 10.0, 0.01))
        self.db.commit()

        def fake_execute(*args, **kwargs):
            return {"status": "executed", "signature": "fakesigSELL",
                    "coin_amount": 4.0}

        with mock.patch.object(routes, "execute_real_trade", fake_execute):
            resp = self.client.post(f"/coins/{mint}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{mint}"),
                "amount": "4", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("fakesigSELL", html)
        self.assertIn("Back to coins", html)
        row = self.db.execute(
            "SELECT amount FROM real_holdings WHERE coin_id = ?",
            (mint,)).fetchone()
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
        for path in ("/dashboard", "/bots", "/wallet", "/pnl", "/settings"):
            html = self._html(path)
            self.assertIn("hud-item", html, path)
            for label in ("Coins", "Bots", "Wallet", "P&amp;L", "Settings"):
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


class TestLoopPlatformGate(SplitBase):
    def _run(self, prices, executor):
        return run_once(
            self.db, self.cfg,
            price_fetcher=lambda: prices,
            rpc_factory=lambda url: FakeRpc(url, 0),
            real_executor=executor)

    def test_non_pumpfun_real_bot_never_executes(self):
        self._create_bot("Swapper", mode="real",
                         platform="pumpswap", silent=1)
        calls = []

        def executor(*args, **kwargs):
            calls.append(args)
            return {"status": "skipped", "reason": "test refused"}

        self._run(make_prices({"SOL": (100.0, 5.0)}), executor)
        self.assertEqual(calls, [])
        skip = self.db.execute(
            "SELECT * FROM activity WHERE user_id = ? AND kind = "
            "'silent_skip' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(skip)
        self.assertIn("not live yet", skip["detail"])
        self.assertIn("pumpswap", skip["detail"])

    def test_pumpfun_real_bot_reaches_executor(self):
        self._create_bot("Nova", mode="real", platform="pump.fun", silent=1)
        calls = []

        def executor(*args, **kwargs):
            calls.append(args)
            return {"status": "skipped", "reason": "test refused"}

        # The loop checks the wallet balance before buying, so the
        # wallet needs funds for the buy to reach the executor.
        FakeRpc.balance_lamports = sol_to_lamports(10.0)
        try:
            self._run(make_prices({"SOL": (100.0, 5.0)}), executor)
        finally:
            FakeRpc.balance_lamports = 0
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "SOL")
        self.assertEqual(calls[0][3], "buy")


if __name__ == "__main__":
    unittest.main()
