"""Delete-button fix, header balance + Paper/Devnet switch, and the
buy flow's managing-bot selectors.

The dashboard's Quick buy card (and its /paper/buy + /paper/sell
endpoints) are gone — buys and sells live only in the coin popup and
the coin detail page, so the buy tests post to /coins/<coin>/buy and
get the confirmation page (200), not a redirect."""

import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
from helpers import csrf, make_client, signup

# Gate — the devnet pump.fun mint used as the test coin throughout
# this file. SOL is the unit of account, never a listed/buyable
# coin, so every buy test uses this mint instead.
GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.0000005


def start_route_mocks(tc):
    """Keep every live feed local: the trenches price/identity for
    the Gate mint, a SOL-only price list, and no logo/RPC/Metaplex
    or Jupiter calls in these tests."""
    tc.mint_prices = {GATE: GATE_PRICE}
    tc.mint_identities = {GATE: {"name": "Gate", "symbol": "GATE"}}
    patches = [
        mock.patch.object(
            routes, "fetch_prices_for_mints",
            side_effect=lambda mints, http_get=None: {
                m: tc.mint_prices[m] for m in mints
                if m in tc.mint_prices}),
        mock.patch.object(routes, "fetch_mint_1h_changes",
                          return_value={}),
        mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints, http_get=None: {
                m: tc.mint_identities[m] for m in mints
                if m in tc.mint_identities}),
        mock.patch.object(routes, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "_default_get",
                          return_value={"pairs": []}),
        mock.patch.object(coin_meta_mod, "_from_metaplex",
                          return_value=None),
        mock.patch.object(
            jupiter_mod, "quote_buy_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None}),
        mock.patch.object(
            routes, "get_prices",
            return_value={"prices": {"SOL": {
                "price": 10.0, "change_24h": 5.25,
                "change_1h": None}},
                "source": "live", "as_of": "test"}),
    ]
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)
    routes._TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
    home_holdings_mod.clear_image_cache()


class TestDeleteButton(unittest.TestCase):
    """Regression: the edit-page Delete control used to depend on the
    browser's native confirm() dialog, which in-app browsers often
    never show — the submit was cancelled and the button looked dead.
    Deletion now goes through a server-rendered confirm page (no
    JavaScript), and deleting a bot with open positions works too
    (positions are un-managed, coins stay in paper holdings)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        start_route_mocks(self)
        signup(self.client, "alice")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, name="Luna", mode="paper"):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5", "mode": mode})
        self.assertEqual(resp.status_code, 200)
        db = self.app.config["DB"]
        return db.execute("SELECT id FROM bots WHERE name = ?",
                          (name,)).fetchone()["id"]

    def test_edit_page_delete_goes_to_confirm_page_not_js(self):
        bot_id = self._create()
        html = self.client.get(f"/bots/{bot_id}/edit").get_data(as_text=True)
        # No JavaScript confirm() anywhere on the edit page.
        self.assertNotIn("confirm(", html)
        # The delete control is a plain link to the confirm page.
        self.assertIn(f"/bots/{bot_id}/delete/confirm", html)

    def test_delete_via_confirm_page_removes_bot(self):
        bot_id = self._create()
        resp = self.client.get(f"/bots/{bot_id}/delete/confirm")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Luna", html)
        self.assertIn(f"/bots/{bot_id}/delete", html)
        token = csrf(self.client, f"/bots/{bot_id}/delete/confirm")
        resp = self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertIsNone(db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())

    def test_delete_from_list_page_link(self):
        bot_id = self._create()
        html = self.client.get("/bots").get_data(as_text=True)
        self.assertIn(f"/bots/{bot_id}/delete/confirm", html)
        token = csrf(self.client, "/bots")
        resp = self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertIsNone(db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())

    def test_delete_bot_with_open_position_keeps_holdings(self):
        bot_id = self._create()
        # Manual buy with this bot as managing bot opens a position.
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "usd": "10", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        db = self.app.config["DB"]
        self.assertIsNotNone(db.execute(
            "SELECT id FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())
        token = csrf(self.client, f"/bots/{bot_id}/delete/confirm")
        resp = self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())
        self.assertIsNone(db.execute(
            "SELECT id FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())
        # The coins stay in the paper holdings — only the bot's
        # management record is gone.
        holding = db.execute(
            "SELECT amount FROM paper_holdings WHERE coin_id = ?",
            (GATE,)).fetchone()
        self.assertIsNotNone(holding)
        self.assertGreater(holding["amount"], 0)


class TestModeSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")

    def tearDown(self):
        self.tmp.cleanup()

    def _header(self):
        return self.client.get("/dashboard").get_data(as_text=True)

    def test_default_is_paper_and_header_shows_paper_value(self):
        db = self.app.config["DB"]
        mode = db.execute(
            "SELECT active_mode FROM users WHERE username = 'alice'"
            ).fetchone()["active_mode"]
        self.assertEqual(mode, "paper")
        html = self._header()
        self.assertIn("Paper", html)
        # Fresh account: $100 CAD paper cash, no holdings.
        self.assertIn("$100.00 CAD", html)

    def test_switch_persists_and_header_follows(self):
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "devnet"})
        db = self.app.config["DB"]
        mode = db.execute(
            "SELECT active_mode FROM users WHERE username = 'alice'"
            ).fetchone()["active_mode"]
        self.assertEqual(mode, "devnet")
        # Preference survives a fresh page load.
        html = self._header()
        self.assertIn("Devnet", html)
        # Devnet header shows an on-chain SOL balance or plainly says
        # it is unavailable (RPC may not answer in the sandbox) —
        # never a paper dollar figure as the devnet balance.
        self.assertTrue("SOL" in html or "unavailable" in html)
        # Switch back.
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "paper"})
        mode = db.execute(
            "SELECT active_mode FROM users WHERE username = 'alice'"
            ).fetchone()["active_mode"]
        self.assertEqual(mode, "paper")
        self.assertIn("$100.00 CAD", self._header())

    def test_invalid_mode_changes_nothing(self):
        resp = self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "mainnet"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        mode = db.execute(
            "SELECT active_mode FROM users WHERE username = 'alice'"
            ).fetchone()["active_mode"]
        self.assertEqual(mode, "paper")


class TestBuySelectors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        start_route_mocks(self)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, name="Luna", mode="paper"):
        self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5", "mode": mode})
        return self.db.execute("SELECT id FROM bots WHERE name = ?",
                               (name,)).fetchone()["id"]

    def _buy(self, coin=GATE, usd="10", bot_id=""):
        return self.client.post(f"/coins/{coin}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "usd": usd, "bot_id": bot_id})

    def test_paper_buy_with_managing_bot_records_position(self):
        bot_id = self._create()
        resp = self._buy(bot_id=str(bot_id))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        holding = self.db.execute(
            "SELECT amount, avg_price FROM paper_holdings "
            "WHERE coin_id = ?", (GATE,)).fetchone()
        self.assertIsNotNone(holding)
        self.assertGreater(holding["amount"], 0)
        pos = self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone()
        self.assertIsNotNone(pos, "managing bot should own a position")
        self.assertEqual(pos["coin_id"], GATE)
        self.assertAlmostEqual(pos["amount"], holding["amount"])
        self.assertEqual(pos["mode"], "paper")
        self.assertIsNotNone(pos["peak_price"])
        self.assertEqual(pos["ladder_stage"], 0)
        log = self.db.execute(
            "SELECT detail FROM activity WHERE kind = 'paper_buy' "
            "ORDER BY id DESC LIMIT 1").fetchone()["detail"]
        self.assertIn("Luna", log)

    def test_paper_buy_without_bot_records_no_position(self):
        self._create()
        resp = self._buy()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions").fetchone()["c"], 0)

    def test_buy_with_someone_elses_bot_is_refused(self):
        bot_id = self._create()
        # Bob, a second user on the same app, tries to use Alice's bot.
        bob = self.app.test_client()
        signup(bob, "bob")
        resp = bob.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(bob, f"/coins/{GATE}"),
            "usd": "10", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was bought", resp.get_data(as_text=True))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings").fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions").fetchone()["c"], 0)

    def test_buy_with_real_mode_bot_is_refused(self):
        bot_id = self._create("RealBot", mode="real")
        resp = self._buy(bot_id=str(bot_id))
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("real-mode bot", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings").fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions").fetchone()["c"], 0)

    def test_quick_buy_card_and_paper_endpoints_are_gone(self):
        # The dashboard trade form that repeated the HUD buttons is
        # removed: no Quick buy card, no /paper/buy or /paper/sell.
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn("Quick buy", html)
        self.assertNotIn('action="/paper/buy"', html)
        self.assertNotIn('action="/paper/sell"', html)
        self.assertEqual(self.client.post(
            "/paper/buy", data={"csrf_token": csrf(self.client, "/dashboard"),
                                "coin": "SOL", "usd": "10"}).status_code,
            404)
        self.assertEqual(self.client.post(
            "/paper/sell", data={"csrf_token": csrf(self.client, "/dashboard"),
                                 "coin": "SOL", "amount": "1"}).status_code,
            404)

    def test_real_buy_without_bot_refused_plainly_with_no_fill(self):
        self._create()
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "devnet"})
        cash_before = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount_sol": "0.1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Pick one of your real bots", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions").fetchone()["c"], 0)
        cash_after = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        self.assertEqual(cash_after, cash_before)

    def test_popup_buy_form_follows_active_mode(self):
        self._create()
        html = self.client.get(f"/coins/{GATE}").get_data(as_text=True)
        self.assertIn("Buy (paper)", html)
        self.assertIn('name="amount_sol"', html)
        self.assertIn('name="bot_id"', html)
        self.assertIn("Luna (paper)", html)
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "devnet"})
        html = self.client.get(f"/coins/{GATE}").get_data(as_text=True)
        self.assertIn("Buy (real, devnet)", html)
        self.assertIn("Choose a real bot", html)


if __name__ == "__main__":
    unittest.main()
