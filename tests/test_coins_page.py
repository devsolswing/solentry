"""Coins page refinement + scanner placement.

Rules under test:
- The dashboard Coins page lists ONLY the coins the user holds in
  the active mode (same on the wallet homepage). A fresh user sees
  an empty state, not the whole price list.
- A small "Buy or Sell Coin" button below the list opens a
  full-screen pop-up: type a coin name and live results appear
  (CoinGecko list + DexScreener search, never invented), then an
  amount in $ or tokens at the live price, bought/sold through the
  existing coin endpoints (which now also accept a token amount on
  buy and a $ amount on sell, converted at the server's own live
  price).
- A Refresh button at the top re-fetches live data (price cache,
  balance cache, trenches cache cleared + a fresh portfolio
  snapshot) instead of serving up-to-60-second-old prices.
- The scanner lives in Edit Bots under the category list next to
  the Memecoin build — picking it there gives the bot the scanner
  sniper rules (one per user). Its standalone Bots-page card is
  gone; preset route and agent flow are unchanged.
No SOL moves in these tests; real execution is a recording mock.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts_mod
import tradingbot.prices as prices_mod
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.rules import load_rules, scanner_ruleset

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


DEX_PAIRS = [
    {"chainId": "solana", "dexId": "pumpfun", "pairAddress": "PAIR1",
     "baseToken": {"address": "BfunMint11111111111111111111111111111111",
                   "name": "BonkFun", "symbol": "BFUN"},
     "priceUsd": "0.002", "liquidity": {"usd": 50000}},
    {"chainId": "ethereum", "dexId": "uniswap", "pairAddress": "PAIR2",
     "baseToken": {"address": "0x0000000000000000000000000000000000000001",
                   "name": "EtherFun", "symbol": "EFUN"},
     "priceUsd": "3.00", "liquidity": {"usd": 90000}},
]


class CoinsPageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25), "USDC": (1.0, 0.0),
                "DOGE": (0.07, 3.0), "PEPE": (0.000005, -1.0),
                "BONK": (0.000003, 8.0), "JUP": (0.23, -0.5)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices_mod, "get_price", return_value=(10.0, "snapshot", "test"))
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        # The detail page fetches a live market chart and trade flow;
        # keep both local and deterministic in tests.
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart",
            return_value={"points": [], "error": "no chart history"})
        self.chart_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow", return_value=None)
        self.flow_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        charts_mod._search_cache.clear()
        self.search_patch = mock.patch.object(
            charts_mod, "_default_get",
            return_value={"pairs": list(DEX_PAIRS)})
        self.search_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.flow_patch.stop()
        self.chart_patch.stop()
        self.search_patch.stop()
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _buy_sol(self, usd="10"):
        return self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": usd, "bot_id": ""})

    def _holding(self, coin="SOL"):
        row = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, coin)).fetchone()
        return float(row["amount"]) if row else 0.0


class TestHeldCoinsOnly(CoinsPageBase):
    def test_dashboard_lists_only_held_coins(self):
        self._choose_mode("paper")
        html = self._html("/dashboard")
        self.assertIn("You hold no coins", html)
        self.assertNotIn("/coins/SOL/detail", html)
        self.assertNotIn("/coins/BTC/detail", html)
        self._buy_sol()
        html = self._html("/dashboard")
        self.assertIn("/coins/SOL/detail", html)
        self.assertIn("Solana", html)
        self.assertNotIn("/coins/BTC/detail", html)
        self.assertNotIn("Bitcoin", html)
        self.assertNotIn("You hold no coins", html)

    def test_wallet_lists_only_held_coins(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("You hold no coins", html)
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("/coins/SOL/detail", html)
        self.assertIn("Solana", html)
        self.assertNotIn("/coins/BTC/detail", html)
        self.assertNotIn("Bitcoin", html)

    def test_quick_trade_button_and_popup_present(self):
        self._choose_mode("paper")
        html = self._html("/dashboard")
        self.assertIn("Buy or Sell Coin", html)
        self.assertIn('id="quick-trade-overlay"', html)
        self.assertIn('id="quick-trade-search"', html)
        self.assertIn('id="quick-trade-buy-form"', html)
        self.assertIn('id="quick-trade-sell-form"', html)
        self.assertIn("quick-amount-kind", html)
        # The popup is full screen and hidden until opened.
        self.assertIn("overlay-fullscreen", html)
        self.assertIn('class="overlay overlay-fullscreen hidden"', html)

    def test_refresh_button_present_on_coins_and_detail(self):
        self._choose_mode("paper")
        html = self._html("/dashboard")
        self.assertIn("↻ Refresh live data", html)
        self.assertIn('action="/dashboard/refresh"', html)
        self._buy_sol()
        detail = self._html("/coins/SOL/detail")
        self.assertIn("↻ Refresh", detail)


class TestCoinSearch(CoinsPageBase):
    def _search(self, q):
        resp = self.client.get(f"/api/coins/search?q={q}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_search_returns_live_known_coin(self):
        self._choose_mode("paper")
        data = self._search("sol")
        ids = [r["coin_id"] for r in data["results"]]
        self.assertIn("SOL", ids)
        sol = next(r for r in data["results"] if r["coin_id"] == "SOL")
        self.assertEqual(sol["name"], "Solana")
        self.assertEqual(sol["price_usd"], 10.0)
        self.assertEqual(sol["held"], 0.0)
        # Every result is a real coin with a live price — nothing
        # invented, no Ethereum pair from the DexScreener payload.
        self.assertNotIn("EFUN", [r["symbol"] for r in data["results"]])

    def test_search_returns_dexscreener_mint(self):
        data = self._search("bfun")
        ids = [r["coin_id"] for r in data["results"]]
        self.assertIn("BfunMint11111111111111111111111111111111", ids)
        entry = next(r for r in data["results"]
                     if r["symbol"] == "BFUN")
        self.assertEqual(entry["name"], "BonkFun")
        self.assertEqual(entry["price_usd"], 0.002)
        self.assertTrue(entry["is_mint"])

    def test_search_includes_held_amount(self):
        self._choose_mode("paper")
        self._buy_sol()
        data = self._search("sol")
        sol = next(r for r in data["results"] if r["coin_id"] == "SOL")
        self.assertEqual(sol["held"], 1.0)

    def test_search_nonsense_returns_nothing_invented(self):
        with mock.patch.object(charts_mod, "_default_get",
                               return_value={"pairs": []}):
            charts_mod._search_cache.clear()
            data = self._search("zzzzqq")
        self.assertEqual(data["results"], [])
        self.assertIsNone(data["error"])

    def test_search_feed_failure_reports_error(self):
        with mock.patch.object(charts_mod, "_default_get",
                               side_effect=Exception("boom")):
            charts_mod._search_cache.clear()
            data = self._search("zzzzqq")
            self.assertEqual(data["results"], [])
            self.assertIsNotNone(data["error"])
            # Known coins still come from the price feed.
            data = self._search("sol")
            self.assertIn("SOL", [r["coin_id"] for r in data["results"]])
            self.assertIsNotNone(data["error"])

    def test_search_short_query_returns_nothing(self):
        data = self._search("s")
        self.assertEqual(data["results"], [])

    def test_search_requires_login(self):
        self.client.get("/logout")
        resp = self.client.get("/api/coins/search?q=sol")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestQuickTradeAmounts(CoinsPageBase):
    def test_buy_by_token_amount(self):
        self._choose_mode("paper")
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_tokens": "2", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertEqual(self._holding("SOL"), 2.0)

    def test_sell_by_usd_amount(self):
        self._choose_mode("paper")
        self._buy_sol()
        self.assertEqual(self._holding("SOL"), 1.0)
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_usd": "5"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        self.assertEqual(self._holding("SOL"), 0.5)

    def test_sell_by_token_amount_still_works(self):
        self._choose_mode("paper")
        self._buy_sol()
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount": "0.5"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        self.assertEqual(self._holding("SOL"), 0.5)

    def test_buy_tokens_without_price_refuses_plainly(self):
        self._choose_mode("paper")
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}):
            resp = self.client.post(f"/coins/{DEST}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{DEST}"),
                "amount_tokens": "1", "bot_id": ""})
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Price unavailable", html)

    def test_sell_usd_without_price_refuses_plainly(self):
        self._choose_mode("paper")
        self._buy_sol()
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}):
            # SOL has a price, so use a mint holding instead: put a
            # mint holding in directly and sell it by $.
            self.db.execute(
                "INSERT INTO paper_holdings (user_id, coin_id, amount, "
                "avg_price) VALUES (?, ?, 5, 0.01)",
                (self.user_id, DEST))
            self.db.commit()
            resp = self.client.post(f"/coins/{DEST}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{DEST}"),
                "amount_usd": "10"})
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was sold", html)
        self.assertIn("Price unavailable", html)


class TestRefreshLive(unittest.TestCase):
    """Refresh with the REAL price feed path (no routes.get_prices
    patch) so the 60-second cache is actually exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        prices_mod.clear_price_cache()
        self.sol_price = 10.0

        def _live():
            base = {"BTC": 74000.0, "ETH": 2100.0, "SOL": self.sol_price,
                    "USDC": 1.0, "DOGE": 0.07, "PEPE": 0.000005,
                    "BONK": 0.000003, "JUP": 0.23}
            return {s: {"price": p, "change_24h": 0.0}
                    for s, p in base.items()}

        self.live_patch = mock.patch.object(
            prices_mod, "_fetch_live", side_effect=_live)
        self.live_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": ""})

    def tearDown(self):
        self.live_patch.stop()
        self.trenches_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _snapshots(self):
        return self.db.execute(
            "SELECT COUNT(*) c FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]

    def test_refresh_clears_price_cache_and_records_snapshot(self):
        self.assertIn("$10.0000", self._html("/dashboard"))
        self.sol_price = 99.0
        # Still the cached price until a refresh.
        self.assertIn("$10.0000", self._html("/dashboard"))
        before = self._snapshots()
        resp = self.client.post("/dashboard/refresh", data={
            "csrf_token": csrf(self.client, "/dashboard")})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        html = self._html("/dashboard")
        self.assertIn("$99.0000", html)
        self.assertIn("Refreshed with live data.", html)
        self.assertGreater(self._snapshots(), before)

    def test_refresh_requires_login(self):
        self.client.get("/logout")
        resp = self.client.post("/dashboard/refresh", data={
            "csrf_token": "x"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestScannerInEditBots(CoinsPageBase):
    """The scanner is built from its preset route (or by builderBot /
    the assistant) — the category/strategy pickers on the bot forms
    are gone, so the forms can no longer set a category at all."""

    def _create_bot(self, name="Luna", category=""):
        token = csrf(self.client, "/bots/create")
        resp = self.client.post("/bots/create", data={
            "csrf_token": token, "name": name, "strategy": "balanced",
            "platform": "pump.fun", "category": category,
            "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper", "coins": "SOL"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 200, resp.status_code)
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = ?",
            (self.user_id, name)).fetchone()

    def _create_scanner(self):
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/scanner",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302, resp.status_code)
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND category = ?",
            (self.user_id, "scanner")).fetchone()

    def test_preset_scanner_applies_scanner_rules(self):
        bot = self._create_scanner()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["category"], "scanner")
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules, scanner_ruleset())

    def test_edit_keeps_existing_scanner_rules(self):
        bot = self._create_scanner()
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules["kind"], "scanner")
        # Customise the rules through the stored JSON, then edit the
        # bot again — the custom rules must survive, not reset.
        custom = dict(scanner_ruleset())
        custom["top10_rug"] = {"drop_pct": 12.0}
        self.db.execute("UPDATE bots SET rules_json = ? WHERE id = ?",
                        (json.dumps(custom), bot["id"]))
        self.db.commit()
        token = csrf(self.client, f"/bots/{bot['id']}/edit")
        resp = self.client.post(f"/bots/{bot['id']}/edit", data={
            "csrf_token": token, "name": "Luna", "strategy": "bold",
            "platform": "pump.fun", "category": "scanner",
            "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper", "coins": "SOL"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        edited = self.db.execute("SELECT * FROM bots WHERE id = ?",
                                 (bot["id"],)).fetchone()
        self.assertEqual(edited["category"], "scanner")
        rules, err = load_rules(edited)
        self.assertIsNone(err)
        # The custom 12% drop survives; validation fills the
        # concentration defaults around it.
        self.assertEqual(rules["top10_rug"]["drop_pct"], 12.0)
        self.assertEqual(rules["top10_rug"]["concentration_pct"], 21.0)

    def test_create_with_scanner_category_ignored(self):
        # A category posted to /bots/create is ignored now — the
        # scanner only comes from its preset route.
        bot = self._create_bot(name="Snipe", category="scanner")
        self.assertEqual(bot["category"], "")
        rules, err = load_rules(bot)
        # No rules at all — a plain bot has no kind until one is set.
        self.assertIsNone(rules)

    def test_preset_scanner_twice_goes_to_existing(self):
        first = self._create_scanner()
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/scanner",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/bots/{first['id']}",
                      resp.headers["Location"])
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ? "
            "AND category = 'scanner'", (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_edit_cannot_turn_bot_into_scanner(self):
        first = self._create_scanner()
        second = self._create_bot(name="Other")
        token = csrf(self.client, f"/bots/{second['id']}/edit")
        resp = self.client.post(f"/bots/{second['id']}/edit", data={
            "csrf_token": token, "name": "Other", "strategy": "bold",
            "platform": "pump.fun", "category": "scanner",
            "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper", "coins": "SOL"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        untouched = self.db.execute("SELECT * FROM bots WHERE id = ?",
                                    (second["id"],)).fetchone()
        self.assertEqual(untouched["category"], "")
        self.assertEqual(first["id"], self.db.execute(
            "SELECT id FROM bots WHERE user_id = ? "
            "AND category = 'scanner'",
            (self.user_id,)).fetchone()["id"])


if __name__ == "__main__":
    unittest.main()
