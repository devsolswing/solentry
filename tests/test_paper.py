"""Paper trading: buy/sell math, cash limits, holdings.

Buys and sells live in the coin popup and the coin detail page
(/coins/<coin>/buy and /coins/<coin>/sell) — the old /paper/buy and
/paper/sell endpoints (the dashboard's removed Quick buy card) are
gone. Both endpoints answer with the confirmation page (200), never
a redirect, and a refusal is a confirmation that says nothing was
bought or sold — never a fill.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup


class TestPaper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        # Deterministic $100 SOL for every test.
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "snapshot", "test"))
        self.price_patch.start()
        self.prices_patch = mock.patch.object(routes, "get_prices", return_value={
            "prices": {c: {"price": 100.0, "change_24h": 0.0}
                       for c in ["BTC", "ETH", "SOL", "USDC", "DOGE",
                                  "PEPE", "BONK", "JUP"]},
            "source": "snapshot", "as_of": "test"})
        self.prices_patch.start()
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")

    def tearDown(self):
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _buy(self, coin="SOL", usd="10"):
        return self.client.post(f"/coins/{coin}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{coin}"),
            "usd": usd, "bot_id": ""})

    def _sell(self, coin="SOL", amount="0.04"):
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
                             "WHERE coin_id = 'SOL'").fetchone()
        # $10 buys $9.635 of coins: the 3.65% fee is a cost, so the
        # holding is 0.09635 SOL, not 0.1. Cash still falls by the
        # full $10, and the cost basis stays $100 per coin.
        self.assertAlmostEqual(holding["amount"], 0.09635, places=9)
        self.assertEqual(holding["avg_price"], 100.0)
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
        # Sell 0.04 SOL at $100: gross $4.00, fee $0.146, net $3.854.
        self.assertEqual(db.execute("SELECT cash_usd FROM paper_portfolio"
                                    ).fetchone()["cash_usd"], 93.854)
        self.assertAlmostEqual(db.execute("SELECT amount FROM paper_holdings "
                                    "WHERE coin_id='SOL'").fetchone()["amount"],
                         0.05635, places=9)

    def test_sell_more_than_held_rejected(self):
        self._buy(usd="10")
        resp = self._sell(amount="99")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was sold", resp.get_data(as_text=True))
        db = self.app.config["DB"]
        self.assertAlmostEqual(db.execute(
            "SELECT amount FROM paper_holdings "
            "WHERE coin_id='SOL'").fetchone()["amount"],
            0.09635, places=9)

    def test_unknown_coin_rejected(self):
        resp = self.client.post("/coins/FAKE/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 404)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_trades"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
