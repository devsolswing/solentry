"""Paper trading: buy/sell math, cash limits, holdings.

Buys and sells live in the coin popup and the coin detail page
(/coins/<coin>/buy and /coins/<coin>/sell) — the old /paper/buy and
/paper/sell endpoints (the dashboard's removed Quick buy card) are
gone. Both endpoints answer with the confirmation page (200), never
a redirect, and a refusal is a confirmation that says nothing was
bought or sold — never a fill. Coins are pump.fun mints, priced
from the mocked trenches feed; symbols are not coins any more and
their buy/sell endpoints 404.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.jupiter as jupiter_mod
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup

# A pump.fun mint priced at $0.01 in the mocked trenches feed, so
# $10 buys exactly 1000 tokens.
MINT = "Bond1111111111111111111111111111111111111111"
MINT_PRICE = 0.01


class TestPaper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        # Deterministic $100 SOL for every test.
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "snapshot", "test"))
        self.price_patch.start()
        self.prices_patch = mock.patch.object(routes, "get_prices", return_value={
            "prices": {"SOL": {"price": 100.0, "change_24h": 0.0}},
            "source": "snapshot", "as_of": "test"})
        self.prices_patch.start()
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            return_value={MINT: MINT_PRICE})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.mint_1h_patch.start()
        self.mint_id_patch = mock.patch.object(
            routes, "fetch_mint_identities", return_value={})
        self.mint_id_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        # Paper sells ask Jupiter for a quote first; the test mint
        # has no route, so the quote fails and the sell falls back
        # to the live price — mock the failure, never hit Jupiter.
        self.jupiter_patch = mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route"))
        self.jupiter_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")

    def tearDown(self):
        self.rpc_patch.stop()
        self.jupiter_patch.stop()
        self.trenches_patch.stop()
        self.meta_patch.stop()
        self.mint_id_patch.stop()
        self.mint_1h_patch.stop()
        self.mint_prices_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.tmp.cleanup()

    def _buy(self, coin=MINT, usd="10"):
        return self.client.post(f"/coins/{coin}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "usd": usd, "bot_id": ""})

    def _sell(self, coin=MINT, amount="400"):
        return self.client.post(f"/coins/{coin}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "amount": amount})

    def test_buy_deducts_cash_and_adds_holding(self):
        resp = self._buy()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        db = self.app.config["DB"]
        cash = db.execute("SELECT cash_usd FROM paper_portfolio"
                          ).fetchone()["cash_usd"]
        self.assertEqual(cash, 90.0)
        holding = db.execute("SELECT amount, avg_price FROM paper_holdings "
                             "WHERE coin_id = ?", (MINT,)).fetchone()
        # $10 buys $10 of coins at $0.01: 1000 tokens. No fee
        # (removed 2026-11-12), so cash falls by the full $10 and
        # the cost basis stays $0.01 per token.
        self.assertAlmostEqual(holding["amount"], 1000.0, places=9)
        self.assertEqual(holding["avg_price"], MINT_PRICE)
        trade = db.execute("SELECT * FROM paper_trades").fetchone()
        self.assertEqual(trade["side"], "buy")
        self.assertEqual(trade["total_usd"], 10.0)

    def test_buy_more_than_cash_rejected(self):
        resp = self._buy(usd="999999")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was bought", resp.get_data(as_text=True))
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT cash_usd FROM paper_portfolio"
                                    ).fetchone()["cash_usd"], 100.0)
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_holdings"
                                    ).fetchone()["c"], 0)

    def test_sell_returns_cash(self):
        self._buy()
        resp = self._sell()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        db = self.app.config["DB"]
        # Sell 400 tokens at $0.01: gross $4.00, no fee (removed
        # 2026-11-12), so cash is 90 + 4 = 94.0 and 600 tokens remain.
        self.assertEqual(db.execute("SELECT cash_usd FROM paper_portfolio"
                                    ).fetchone()["cash_usd"], 94.0)
        self.assertAlmostEqual(db.execute("SELECT amount FROM paper_holdings "
                                    "WHERE coin_id=?", (MINT,)).fetchone()["amount"],
                         600.0, places=9)

    def test_sell_more_than_held_rejected(self):
        self._buy(usd="10")
        resp = self._sell(amount="99999")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was sold", resp.get_data(as_text=True))
        db = self.app.config["DB"]
        self.assertAlmostEqual(db.execute(
            "SELECT amount FROM paper_holdings "
            "WHERE coin_id=?", (MINT,)).fetchone()["amount"],
            1000.0, places=9)

    def test_unknown_coin_rejected(self):
        resp = self.client.post("/coins/FAKE/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 404)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_trades"
                                    ).fetchone()["c"], 0)

    def test_symbol_coin_rejected(self):
        # SOL is the unit of account, never a listed/buyable coin:
        # buying it 404s like any other symbol.
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 404)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_trades"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
