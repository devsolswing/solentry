"""Pump.fun-only rebuild: majors routes gone, real name + ticker
display, logo fetched on purchase for held coins only, and one
wallet for the whole app (send, buy, receive, sell).

No SOL moves in these tests; the send RPC is a recording mock and
Jupiter/DexScreener answers are mocked.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.routes import _coin_rows
from tradingbot.solana import TOKEN_PROGRAM

MINT = "Grad1111111111111111111111111111111111111111"
OTHER_MINT = "Othr1111111111111111111111111111111111111111"
DEST = "11111111111111111111111111111111"


class FakeRpc:
    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.sent = []

    def get_balance_lamports(self, pubkey):
        return 10_000_000_000

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def get_account_info(self, pubkey):
        if pubkey in (MINT, OTHER_MINT):
            return {"owner": TOKEN_PROGRAM, "data": bytes(82),
                    "lamports": 0, "executable": False}
        return None

    def get_token_account(self, pubkey):
        return {"amount": 10_000_000, "decimals": 6}

    def get_token_account_balance(self, pubkey):
        return 10_000_000

    def get_minimum_balance_for_rent_exemption(self, size):
        return 1_488_440

    def send_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices():
    return {"prices": {"SOL": {"price": 100.0, "change_24h": 0.0,
                               "change_1h": 0.0}},
            "source": "live", "as_of": "test"}


class PumpfunOnlyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeRpc)
        self.rpc_patch.start()
        self.prices_patch = mock.patch.object(
            routes, "get_prices", return_value=make_prices())
        self.prices_patch.start()
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            return_value={MINT: 0.002, OTHER_MINT: 0.001})
        self.mint_prices_patch.start()
        self.changes_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.changes_patch.start()
        self.identities_patch = mock.patch.object(
            routes, "fetch_mint_identities", return_value={})
        self.identities_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.enrich_patch = mock.patch.object(
            routes, "enrich_row",
            side_effect=lambda row, **kw: row)
        self.enrich_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.assertEqual(resp.status_code, 302)
        self.wallet_pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]

    def tearDown(self):
        self.enrich_patch.stop()
        self.meta_patch.stop()
        self.identities_patch.stop()
        self.changes_patch.stop()
        self.mint_prices_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _hold(self, mint, amount, avg_price):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, amount, avg_price))
        self.db.commit()

    def _rows(self):
        return _coin_rows(self.db, self.user_id, "paper",
                          make_prices())


class TestMajorsRoutesGone(PumpfunOnlyBase):
    def test_route_endpoint_gone(self):
        for coin_id in ("ZEC", "BTC", "SOL", MINT):
            resp = self.client.get(f"/api/coins/{coin_id}/route")
            self.assertEqual(resp.status_code, 404, coin_id)

    def test_wrapped_majors_endpoint_gone(self):
        resp = self.client.get("/api/wrapped-majors")
        self.assertEqual(resp.status_code, 404)

    def test_per_coin_wallet_endpoint_gone(self):
        resp = self.client.post(f"/api/coins/{MINT}/wallet", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}")})
        self.assertEqual(resp.status_code, 404)

    def test_platform_wallet_create_route_gone(self):
        resp = self.client.post("/platforms/pump.fun/create", data={
            "csrf_token": csrf(self.client, "/bots"),
            "return_to": "/bots"})
        self.assertEqual(resp.status_code, 404)

    def test_major_detail_pages_gone(self):
        for coin_id in ("BTC", "ETH", "SOL", "ZEC", "USDC", "ADA",
                        "DOGE", "PEPE"):
            resp = self.client.get(f"/coins/{coin_id}/detail")
            self.assertEqual(resp.status_code, 404, coin_id)

    def test_major_coin_popup_gone(self):
        for coin_id in ("BTC", "SOL", "ZEC"):
            resp = self.client.get(f"/coins/{coin_id}")
            self.assertEqual(resp.status_code, 404, coin_id)

    def test_major_buy_refused(self):
        resp = self.client.post("/coins/BTC/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 404)


class TestNameTickerDisplay(PumpfunOnlyBase):
    def test_feed_name_and_ticker_shown(self):
        self._hold(MINT, 1000.0, 0.001)
        with mock.patch.object(
                routes, "fetch_mint_identities",
                return_value={MINT: {"name": "Test Coin",
                                     "symbol": "TEST"}}):
            rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == MINT)
        self.assertEqual(row["name"], "Test Coin")
        self.assertEqual(row["ticker"], "TEST")

    def test_stored_meta_names_unlisted_mint(self):
        self._hold(MINT, 1000.0, 0.001)
        with mock.patch.object(
                routes, "ensure_coin_meta",
                return_value={"name": "Stored Coin", "symbol": "STOR",
                              "image_url": None}):
            rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == MINT)
        self.assertEqual(row["name"], "Stored Coin")
        self.assertEqual(row["ticker"], "STOR")

    def test_unlisted_mint_uses_feed_identity(self):
        # A graduated coin the feed knows by name must show that
        # name on its detail page even when nobody holds it yet —
        # the feed identity is the last resort before "Unknown coin".
        from tradingbot import routes as routes_mod
        prices = {"prices": {}}
        with mock.patch.object(
                routes_mod, "fetch_mint_identities",
                return_value={MINT: {"name": "Graduated Coin",
                                     "symbol": "GRAD"}}):
            row = routes_mod._unlisted_coin_row(
                MINT, 0.002, prices, db=None)
        self.assertEqual(row["name"], "Graduated Coin")
        self.assertEqual(row["ticker"], "GRAD")

    def test_unnamed_mint_is_unknown_coin_never_address(self):
        self._hold(MINT, 1000.0, 0.001)
        rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == MINT)
        self.assertEqual(row["name"], "Unknown coin")
        self.assertEqual(row["ticker"], "")
        self.assertNotIn(MINT, row["name"])
        self.assertNotIn("Pump.fun token", row["name"])

    def test_legacy_symbol_holding_shown_at_cost_never_hidden(self):
        self._hold("SOL", 2.0, 10.0)
        rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == "SOL")
        self.assertIsNone(row["price"])
        self.assertEqual(row["held"], 2.0)
        self.assertIsNone(row["pnl"])


class TestLogoOnPurchase(PumpfunOnlyBase):
    def setUp(self):
        super().setUp()
        # The real ensure_coin_meta runs in these tests, with its
        # two sources mocked: on-chain Metaplex names the coin and
        # carries the official logo URI.
        self.meta_patch.stop()
        self.enrich_patch.stop()
        self.metaplex_patch = mock.patch.object(
            coin_meta_mod, "_from_metaplex",
            return_value={"name": "Test Coin", "symbol": "TEST",
                          "image_url": "https://image.pump.fun/test.png",
                          "uri": "https://example.com/meta.json"})
        self.metaplex_patch.start()
        self.dex_patch = mock.patch.object(
            coin_meta_mod, "_from_dexscreener", return_value=None)
        self.dex_patch.start()
        import tradingbot.home_holdings as holdings_mod
        self.holdings_mod = holdings_mod
        # The image cache is module-level and order-dependent:
        # clear it so each logo test starts fresh.
        holdings_mod.clear_image_cache()
        self.verify_patch = mock.patch.object(
            holdings_mod, "_verified_image_url",
            side_effect=lambda url, http_head=None: url)
        self.verify_patch.start()

    def tearDown(self):
        self.verify_patch.stop()
        self.dex_patch.stop()
        self.metaplex_patch.stop()
        self.holdings_mod.clear_image_cache()
        super().tearDown()

    def _buy(self, mint):
        return self.client.post(f"/coins/{mint}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{mint}"),
            "usd": "10", "bot_id": ""})

    def test_paper_buy_stores_logo_for_held_coin(self):
        resp = self._buy(MINT)
        self.assertIn("Bought", resp.get_data(as_text=True))
        stored = coin_meta_mod.get_coin_meta(self.db, MINT)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["name"], "Test Coin")
        self.assertEqual(stored["symbol"], "TEST")
        self.assertEqual(stored["image_url"],
                         "https://image.pump.fun/test.png")
        # The held row renders with that logo.
        rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == MINT)
        self.assertEqual(row.get("image_url"),
                         "https://image.pump.fun/test.png")

    def test_unheld_coin_gets_no_logo(self):
        self._buy(MINT)
        self.assertIsNone(coin_meta_mod.get_coin_meta(self.db,
                                                       OTHER_MINT))

    def test_coin_with_no_logo_shows_no_image(self):
        with mock.patch.object(
                coin_meta_mod, "_from_metaplex", return_value=None):
            resp = self._buy(MINT)
        self.assertIn("Bought", resp.get_data(as_text=True))
        stored = coin_meta_mod.get_coin_meta(self.db, MINT)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored["image_url"])
        rows = self._rows()
        row = next(r for r in rows if r["coin_id"] == MINT)
        self.assertIsNone(row.get("image_url"))


class TestSingleWallet(PumpfunOnlyBase):
    def test_wallet_page_has_no_platform_wallets_card(self):
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "mode": "devnet"})
        resp = self.client.get("/wallet/details")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("Platform wallets", html)
        self.assertNotIn("platform_wallets", html)
        self.assertNotIn("Create pump.fun wallet", html)
        self.assertNotIn("Create Phantom wallet", html)
        # The one app wallet is the pump.fun wallet.
        self.assertIn(self.wallet_pubkey, html)

    def test_send_sol_uses_the_app_wallet(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL"})
        self.assertEqual(resp.status_code, 302)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(intent)
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["destination"], DEST)
        self.assertFalse(intent["mint"])  # a SOL send has no mint
        html = self.client.get(
            f"/wallet/send/{intent['id']}/confirm").get_data(
                as_text=True)
        self.assertIn("Solana (devnet)", html)
        self.assertIn("0.5000 SOL", html)

    def test_send_token_uses_the_app_wallet(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "1.5", "token": MINT})
        self.assertEqual(resp.status_code, 302)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(intent)
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["mint"], MINT)
        self.assertEqual(intent["amount_base_units"], 1_500_000)

    def test_receive_is_the_app_wallet_address(self):
        html = self.client.get(
            f"/coins/{MINT}/detail").get_data(as_text=True)
        self.assertIn(self.wallet_pubkey, html)
        self.assertIn("Only send on the Solana network", html)

    def test_paper_buy_and_sell_use_the_one_wallet(self):
        resp = self.client.post(f"/coins/{MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}"),
            "usd": "10", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, MINT)).fetchone()
        self.assertIsNotNone(holding)
        resp = self.client.post(f"/coins/{MINT}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}"),
            "amount": str(holding["amount"]), "bot_id": ""})
        self.assertIn("Sold", resp.get_data(as_text=True))
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, MINT)).fetchone()
        self.assertTrue(holding is None or holding["amount"] == 0)


if __name__ == "__main__":
    unittest.main()
