"""Paper trading: buy/sell math, cash limits, holdings."""

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

    def test_buy_deducts_cash_and_adds_holding(self):
        resp = self.client.post("/paper/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "usd": "1000"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        cash = db.execute("SELECT cash_usd FROM paper_portfolio"
                          ).fetchone()["cash_usd"]
        self.assertEqual(cash, 9000.0)
        holding = db.execute("SELECT amount, avg_price FROM paper_holdings "
                             "WHERE coin_id = 'SOL'").fetchone()
        self.assertEqual(holding["amount"], 10.0)
        self.assertEqual(holding["avg_price"], 100.0)
        trade = db.execute("SELECT * FROM paper_trades").fetchone()
        self.assertEqual(trade["side"], "buy")
        self.assertEqual(trade["total_usd"], 1000.0)

    def test_buy_more_than_cash_rejected(self):
        resp = self.client.post("/paper/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "usd": "999999"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT cash_usd FROM paper_portfolio"
                                    ).fetchone()["cash_usd"], 10000.0)
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_holdings"
                                    ).fetchone()["c"], 0)

    def test_sell_returns_cash(self):
        self.client.post("/paper/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "usd": "1000"})
        resp = self.client.post("/paper/sell", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "amount": "4"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT cash_usd FROM paper_portfolio"
                                    ).fetchone()["cash_usd"], 9400.0)
        self.assertEqual(db.execute("SELECT amount FROM paper_holdings "
                                    "WHERE coin_id='SOL'").fetchone()["amount"],
                         6.0)

    def test_sell_more_than_held_rejected(self):
        self.client.post("/paper/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "usd": "100"})
        resp = self.client.post("/paper/sell", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "SOL", "amount": "99"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT amount FROM paper_holdings "
                                    "WHERE coin_id='SOL'").fetchone()["amount"],
                         1.0)

    def test_unknown_coin_rejected(self):
        resp = self.client.post("/paper/buy", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "coin": "FAKE", "usd": "10"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM paper_trades"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
