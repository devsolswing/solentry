"""Graduated-coin Jupiter dispatch + coin page buy flow (HTTP level).

Rules under test:
- The "Finding route…" route endpoint is GONE: GET
  /api/coins/<coin>/route 404s for every coin, and so does
  /api/wrapped-majors. The coin detail page shows no route box,
  no "Finding route…" state, and no wrapped-majors picker.
- A real buy dispatches by bonding-curve completeness: a
  graduated mint goes through the Jupiter executor, a mint still
  bonding through the pump.fun executor, and an unreadable curve
  refuses plainly — the venue is never guessed.
- Paper buys price from the live Jupiter quote when the coin has
  one (a graduated coin); a failed quote falls back to the live
  price and never blocks the trade.
- There is NO per-coin wallet step: the coin page shows no
  "Create wallet" button and no wallet indicator, the Buy button
  is enabled, and a Buy goes through with no wallet tap first.
  The old /api/coins/<coin>/wallet endpoint is gone (404).
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

# A graduated pump.fun mint (6 decimals by construction).
MINT = "Grad1111111111111111111111111111111111111111"
# A mint still on its bonding curve.
BONDING_MINT = "Bond1111111111111111111111111111111111111111"


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


def fake_chart(coin, days, feed=None, http_get=None):
    return {"points": [
        {"ts": 1790000000000, "price": 80.0},
        {"ts": 1790000060000, "price": 84.0},
        {"ts": 1790000120000, "price": 82.0}],
        "error": None}


def make_prices():
    return {"prices": {"SOL": {"price": 100.0, "change_24h": 0.0,
                               "change_1h": 0.0}},
            "source": "live", "as_of": "test"}


def grad_buy_quote():
    """A normalised SOL -> graduated-mint buy quote, from the real
    captured quote shape: 0.1 SOL in, 0.853837 tokens out."""
    quote = jup._parse_quote(
        dict(REAL_QUOTE, outputMint=MINT, outAmount="853837"),
        side="buy", symbol=MINT, wrapped=False, display_name=MINT)
    quote["in_amount_ui"] = 0.1
    quote["out_amount_ui"] = 0.853837
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
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            return_value={MINT: 0.002, BONDING_MINT: 0.001})
        self.mint_prices_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.assertEqual(resp.status_code, 302)

    def tearDown(self):
        self.meta_patch.stop()
        self.mint_prices_patch.stop()
        self.trenches_patch.stop()
        self.flow_patch.stop()
        self.chart_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _make_real_bot(self):
        import hashlib
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, "Trenches", "balanced",
             json.dumps([MINT]), 20, 7, "real", 0, 3,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid

    def _switch_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)


class TestRouteEndpointGone(JupiterRoutesBase):
    def test_route_endpoint_404s_for_mint(self):
        resp = self.client.get(f"/api/coins/{MINT}/route")
        self.assertEqual(resp.status_code, 404)

    def test_route_endpoint_404s_for_symbol(self):
        resp = self.client.get("/api/coins/ZEC/route?amount_sol=0.1")
        self.assertEqual(resp.status_code, 404)

    def test_wrapped_majors_endpoint_404s(self):
        resp = self.client.get("/api/wrapped-majors")
        self.assertEqual(resp.status_code, 404)

    def test_detail_has_no_route_box_or_wrapped_picker(self):
        html = self._html(f"/coins/{MINT}/detail")
        self.assertNotIn("jupiter-route-status", html)
        self.assertNotIn("Finding route", html)
        self.assertNotIn("wrapped-majors", html)
        self.assertNotIn("Wrapped majors", html)
        self.assertNotIn("Trades as", html)


class TestBuyNeedsNoWalletStep(JupiterRoutesBase):
    def test_buy_works_with_no_wallet_step(self):
        # No tap first: the Buy goes straight through.
        resp = self.client.post(f"/coins/{MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}"),
            "usd": "10", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        count = self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, MINT)).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_detail_has_no_wallet_step_and_buy_enabled(self):
        html = self._html(f"/coins/{MINT}/detail")
        self.assertNotIn("Create wallet", html)
        self.assertNotIn("wallet-create-btn", html)
        self.assertNotIn("wallet-connected", html)
        self.assertNotIn("coin-wallet", html)
        # The Buy button carries no disabled attribute.
        self.assertNotIn("data-buy-btn", html)

    def test_wallet_endpoint_is_gone(self):
        resp = self.client.post(f"/api/coins/{MINT}/wallet", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}")})
        self.assertEqual(resp.status_code, 404)

    def test_paper_buy_prices_from_jupiter_quote(self):
        # 0.1 SOL at a $100 SOL price is $10; the Jupiter quote
        # says that buys 0.853837 tokens, so the fill price is
        # ~$11.71/token — not the $0.002 feed price (which would
        # fill 5,000 tokens). No fee (removed 2026-11-12).
        with mock.patch.object(
                jup, "jupiter_usd_price_mint", return_value=11.71):
            resp = self.client.post(f"/coins/{MINT}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{MINT}"),
                "amount_sol": "0.1", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        amount = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, MINT)
            ).fetchone()["amount"]
        self.assertAlmostEqual(amount, 10.0 / 11.71, places=5)
        self.assertNotAlmostEqual(amount, 5000.0, places=3)

    def test_paper_buy_falls_back_to_feed_when_quote_fails(self):
        # Jupiter cannot quote (bonding coin, no route): the buy
        # still goes through at the live feed price.
        with mock.patch.object(
                jup, "jupiter_usd_price_mint", return_value=None):
            resp = self.client.post(f"/coins/{MINT}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{MINT}"),
                "amount_sol": "0.1", "bot_id": ""})
        self.assertIn("Bought", resp.get_data(as_text=True))
        amount = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, MINT)
            ).fetchone()["amount"]
        self.assertAlmostEqual(amount, 10.0 / 0.002, places=3)


class TestRealBuyDispatchByCurve(JupiterRoutesBase):
    def _buy(self, mint, bot_id):
        return self.client.post(f"/coins/{mint}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{mint}"),
            "amount_sol": "0.1", "bot_id": str(bot_id)})

    def test_graduated_mint_goes_through_jupiter(self):
        bot_id = self._make_real_bot()
        self._switch_mode("devnet")
        with mock.patch.object(routes, "is_curve_complete",
                               return_value=True), \
                mock.patch.object(
                    routes, "execute_jupiter_swap",
                    return_value={"status": "executed",
                                  "coin_amount": 0.5,
                                  "signature": "sig1",
                                  "signing_bot": "SB1"}) as jup_exec, \
                mock.patch.object(
                    routes, "execute_real_trade") as pump_exec:
            resp = self._buy(MINT, bot_id)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertEqual(jup_exec.call_count, 1)
        self.assertEqual(pump_exec.call_count, 0)
        args = jup_exec.call_args[0]
        self.assertEqual(args[2], MINT)
        self.assertEqual(args[3], "buy")

    def test_bonding_mint_goes_through_pumpfun(self):
        bot_id = self._make_real_bot()
        self._switch_mode("devnet")
        with mock.patch.object(routes, "is_curve_complete",
                               return_value=False), \
                mock.patch.object(
                    routes, "execute_jupiter_swap") as jup_exec, \
                mock.patch.object(
                    routes, "execute_real_trade",
                    return_value={"status": "executed",
                                  "coin_amount": 0.5,
                                  "signature": "sig2",
                                  "signing_bot": "SB1"}) as pump_exec:
            resp = self._buy(BONDING_MINT, bot_id)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertEqual(pump_exec.call_count, 1)
        self.assertEqual(jup_exec.call_count, 0)

    def test_unreadable_curve_refuses_plainly(self):
        bot_id = self._make_real_bot()
        self._switch_mode("devnet")
        with mock.patch.object(routes, "is_curve_complete",
                               return_value=None), \
                mock.patch.object(
                    routes, "execute_jupiter_swap") as jup_exec, \
                mock.patch.object(
                    routes, "execute_real_trade") as pump_exec:
            resp = self._buy(MINT, bot_id)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Bonding curve unavailable", html)
        self.assertEqual(jup_exec.call_count, 0)
        self.assertEqual(pump_exec.call_count, 0)


if __name__ == "__main__":
    unittest.main()
