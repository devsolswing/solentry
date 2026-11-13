"""Jupiter route endpoint + coin page buy flow (HTTP level).

Rules under test:
- GET /api/coins/<coin>/route shows the "Finding route…" scan's
  result: the real quoted route (pools/DEXes, expected output,
  price impact, fetch time) or an honest "No liquid route found"
  when Jupiter has no liquid route — never a dead route, never a
  made-up one. TOKEN_NOT_TRADABLE maps to the same plain message;
  Jupiter being unreachable is reported as itself.
- There is NO per-coin wallet step: the coin page shows no
  "Create wallet" button and no wallet indicator, the Buy button
  is enabled, and a Buy goes through with no wallet tap first.
  The old /api/coins/<coin>/wallet endpoint is gone (404).
- Wrapped coins are labelled wrapped on the coin page, and paper
  buys price from the live Jupiter quote.
No SOL moves in these tests; Jupiter's HTTP answers are mocked
(except the live tests in test_jupiter.py, which hit the real API).
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.jupiter as jup
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from test_jupiter import REAL_QUOTE
from tradingbot.solana import generate_keypair

MINT = "11111111111111111111111111111111"


class FakeRpc:
    def __init__(self, url, http_post=None, timeout=20):
        self.url = url

    def get_balance_lamports(self, pubkey):
        return 0

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def fake_ohlc(*args, **kwargs):
    return {"candles": [
        {"ts": 1790000000000, "open": 80.0, "high": 82.0,
         "low": 79.0, "close": 81.0},
        {"ts": 1790001800000, "open": 81.0, "high": 83.0,
         "low": 80.0, "close": 82.0}],
        "interval": 1800, "error": None}


def fake_chart(coin, days):
    return {"points": [
        {"ts": 1790000000000, "price": 80.0},
        {"ts": 1790000060000, "price": 84.0},
        {"ts": 1790000120000, "price": 82.0}],
        "error": None}


def make_prices():
    out = {}
    for coin, price in (("SOL", 100.0), ("ZEC", 990.0),
                        ("BTC", 74000.0), ("ETH", 2100.0),
                        ("ADA", 0.5), ("USDC", 1.0)):
        out[coin] = {"price": price, "change_24h": 0.0,
                     "change_1h": 0.0}
    return {"prices": out, "source": "live", "as_of": "test"}


def zec_buy_quote():
    """A normalised SOL -> wZEC buy quote, from the real captured
    quote shape: 0.1 SOL in, 0.00853837 wZEC out."""
    quote = jup._parse_quote(
        dict(REAL_QUOTE,
             outputMint=jup.WRAPPED_MINTS["ZEC"]["mint"],
             outAmount="853837"),
        side="buy", symbol="ZEC", wrapped=True, display_name="Zcash")
    quote["in_amount_ui"] = 0.1
    quote["out_amount_ui"] = 0.00853837
    return quote


def zec_sell_quote():
    """A normalised wZEC -> SOL sell quote: 0.001 wZEC in,
    ~0.0117 SOL out."""
    quote = jup._parse_quote(
        dict(REAL_QUOTE,
             inputMint=jup.WRAPPED_MINTS["ZEC"]["mint"],
             outputMint=jup.SOL_MINT,
             inAmount="100000", outAmount="11713224"),
        side="sell", symbol="ZEC", wrapped=True, display_name="Zcash")
    quote["in_amount_ui"] = 0.001
    quote["out_amount_ui"] = 0.011713224
    return quote


class JupiterRoutesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeRpc)
        self.rpc_patch.start()
        self.prices_patch = mock.patch.object(
            routes, "get_prices", return_value=make_prices())
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "live", "test"))
        self.price_patch.start()
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart", side_effect=fake_chart)
        self.chart_patch.start()
        self.ohlc_patch = mock.patch.object(
            routes, "fetch_ohlc", side_effect=fake_ohlc)
        self.ohlc_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow",
            return_value={"buys": 120, "sells": 80,
                          "buys_pct": 60.0, "sells_pct": 40.0})
        self.flow_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.assertEqual(resp.status_code, 302)

    def tearDown(self):
        self.trenches_patch.stop()
        self.flow_patch.stop()
        self.ohlc_patch.stop()
        self.chart_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)


class TestJupiterRouteEndpoint(JupiterRoutesBase):
    def test_route_buy_returns_real_route(self):
        with mock.patch.object(jup, "quote_buy",
                               return_value=zec_buy_quote()):
            resp = self.client.get("/api/coins/ZEC/route"
                                   "?amount_sol=0.1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["coin_id"], "ZEC")
        self.assertEqual(data["side"], "buy")
        self.assertTrue(data["wrapped"])
        self.assertEqual(data["display_name"], "Zcash")
        self.assertEqual(data["route_labels"], ["Byreal", "Scorch"])
        self.assertAlmostEqual(data["out_amount_ui"], 0.00853837)
        self.assertTrue(data["fetched_at"])
        self.assertEqual(data["source"], "jupiter")
        text = "\n".join(data["lines"])
        self.assertIn("WRAPPED", text)
        self.assertIn("not the native coin", text)

    def test_route_sell_returns_sol_output(self):
        with mock.patch.object(jup, "quote_sell",
                               return_value=zec_sell_quote()):
            resp = self.client.get("/api/coins/ZEC/route"
                                   "?side=sell&amount_sol=0.1")
        data = resp.get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["side"], "sell")
        self.assertAlmostEqual(data["out_amount_ui"], 0.011713224)

    def test_route_no_route_says_so_plainly(self):
        with mock.patch.object(
                jup, "quote_buy",
                side_effect=jup.NoRouteError("No route found")):
            resp = self.client.get("/api/coins/ZEC/route"
                                   "?amount_sol=0.1")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], jup.NO_LIQUID_ROUTE)
        self.assertEqual(data["error"],
                         "No liquid route found for this coin.")

    def test_route_not_tradable_says_so_plainly(self):
        with mock.patch.object(
                jup, "quote_buy",
                side_effect=jup.JupiterError(
                    "The token EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1"
                    " is not tradable")):
            resp = self.client.get("/api/coins/ZEC/route"
                                   "?amount_sol=0.1")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], jup.NO_LIQUID_ROUTE)

    def test_route_unreachable_reports_itself(self):
        with mock.patch.object(
                jup, "quote_buy",
                side_effect=jup.JupiterError(
                    "Jupiter is unreachable right now.")):
            resp = self.client.get("/api/coins/ZEC/route"
                                   "?amount_sol=0.1")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("unreachable", data["error"])
        self.assertNotIn("No liquid route", data["error"])

    def test_route_pumpfun_mint_has_no_jupiter_route(self):
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}):
            resp = self.client.get(f"/api/coins/{MINT}/route")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("pump.fun", data["error"])

    def test_route_sol_needs_no_route(self):
        resp = self.client.get("/api/coins/SOL/route")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("no route needed", data["error"])

    def test_route_amount_must_be_positive(self):
        resp = self.client.get("/api/coins/ZEC/route?amount_sol=0")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("more than 0", data["error"])


class TestBuyNeedsNoWalletStep(JupiterRoutesBase):
    def test_buy_works_with_no_wallet_step(self):
        # No tap first: the Buy goes straight through.
        resp = self.client.post("/coins/ZEC/buy", data={
            "csrf_token": csrf(self.client, "/coins/ZEC"),
            "usd": "10", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        count = self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = 'ZEC'", (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_detail_has_no_wallet_step_and_buy_enabled(self):
        html = self._html("/coins/ZEC/detail")
        self.assertNotIn("Create wallet", html)
        self.assertNotIn("wallet-create-btn", html)
        self.assertNotIn("wallet-connected", html)
        self.assertNotIn("coin-wallet", html)
        # The Buy button carries no disabled attribute.
        self.assertNotIn("data-buy-btn", html)
        # And a Buy from the page goes through with no tap first.
        resp = self.client.post("/coins/ZEC/buy", data={
            "csrf_token": csrf(self.client, "/coins/ZEC"),
            "usd": "10", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        html = self._html("/coins/ZEC/detail")
        self.assertNotIn("Create wallet", html)
        self.assertNotIn("wallet-connected", html)

    def test_detail_shows_finding_route_state(self):
        html = self._html("/coins/ZEC/detail")
        self.assertIn("jupiter-route-status", html)
        self.assertIn("Finding route…", html)

    def test_detail_labels_wrapped_coin(self):
        html = self._html("/coins/ZEC/detail")
        self.assertIn("Trades as Zcash on Solana", html)
        self.assertIn("the wrapped token, not native", html)

    def test_wallet_endpoint_is_gone_and_ada_buys_in_paper(self):
        # The per-coin wallet endpoint no longer exists.
        resp = self.client.post("/api/coins/ADA/wallet", data={
            "csrf_token": csrf(self.client, "/coins/ADA")})
        self.assertEqual(resp.status_code, 404)
        # And with no wallet gate, an ADA paper buy goes through.
        resp = self.client.post("/coins/ADA/buy", data={
            "csrf_token": csrf(self.client, "/coins/ADA"),
            "usd": "10", "bot_id": ""})
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)

    def test_paper_buy_prices_from_jupiter_quote(self):
        # 0.1 SOL at a $100 SOL price is $10; the Jupiter quote
        # says that buys 0.00853837 wZEC, so the fill price is
        # ~$1,171/wZEC — not the $100 feed price (which would
        # fill 0.1 wZEC). No fee (removed 2026-11-12).
        with mock.patch.object(jup, "quote_buy",
                               return_value=zec_buy_quote()):
            resp = self.client.post("/coins/ZEC/buy", data={
                "csrf_token": csrf(self.client, "/coins/ZEC"),
                "amount_sol": "0.1", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        amount = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = 'ZEC'", (self.user_id,)).fetchone()["amount"]
        expected = 10.0 / (0.1 * 100.0 / 0.00853837)
        self.assertAlmostEqual(amount, expected, places=5)
        self.assertNotAlmostEqual(amount, 0.1, places=3)


if __name__ == "__main__":
    unittest.main()
