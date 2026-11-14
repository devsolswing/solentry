"""Coins page refinement + scanner placement.

Rules under test:
- The dashboard Coins page lists ONLY the coins the user holds in
  the active mode (same on the wallet homepage). A fresh user sees
  an empty state, not the whole price list.
- A small "Buy or Sell Coin" button below the list opens a
  full-screen pop-up: type a coin name and live results appear
  (DexScreener search of Solana pairs, never invented), then an
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
import tradingbot.coin_meta as coin_meta_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.prices as prices_mod
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.rules import load_rules, scanner_ruleset

DEST = "11111111111111111111111111111111"
# Gate — the devnet pump.fun mint used as the test coin throughout
# this file. SOL is the unit of account, never a listed/buyable
# coin, so every buy/sell/detail test uses this mint instead.
GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.0000005


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

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes):
    """changes: {coin: (price, change_24h)} — pump.fun only, so the
    list holds just SOL, the unit of account."""
    return {
        "prices": {c: {"price": p, "change_24h": ch, "change_1h": None}
                   for c, (p, ch) in changes.items()},
        "source": "live", "as_of": "test",
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
        # The price list is SOL only now (unit of account); mints
        # are priced by the trenches feed, mocked below.
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({"SOL": (10.0, 5.25)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices_mod, "get_price", return_value=(10.0, "live", "test"))
        self.price_patch.start()
        # Mint prices / 1h changes / identities, driven by dicts
        # tests can tweak (self.mint_prices[...] = ...).
        self.mint_prices = {GATE: GATE_PRICE}
        self.mint_1h = {}
        self.mint_identities = {
            GATE: {"name": "Gate", "symbol": "GATE"}}
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            side_effect=lambda mints, http_get=None: {
                m: self.mint_prices[m] for m in mints
                if m in self.mint_prices})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes",
            side_effect=lambda mints, http_get=None: {
                m: self.mint_1h[m] for m in mints
                if m in self.mint_1h})
        self.mint_1h_patch.start()
        self.mint_id_patch = mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints, http_get=None: {
                m: self.mint_identities[m] for m in mints
                if m in self.mint_identities})
        self.mint_id_patch.start()
        # Logo fetch on purchase / render must stay local: no live
        # RPC, Metaplex or DexScreener calls in these tests.
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.hh_meta_patch = mock.patch.object(
            home_holdings_mod, "ensure_coin_meta", return_value=None)
        self.hh_meta_patch.start()
        self.hh_get_patch = mock.patch.object(
            home_holdings_mod, "_default_get",
            return_value={"pairs": []})
        self.hh_get_patch.start()
        home_holdings_mod.clear_image_cache()
        self.metaplex_patch = mock.patch.object(
            coin_meta_mod, "_from_metaplex", return_value=None)
        self.metaplex_patch.start()
        # Paper sells/buys price from a Jupiter quote when one
        # exists; there is no route in tests, so refuse the quote
        # instead of hitting the live API.
        self.jup_buy_patch = mock.patch.object(
            jupiter_mod, "quote_buy_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests"))
        self.jup_buy_patch.start()
        self.jup_sell_patch = mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests"))
        self.jup_sell_patch.start()
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
        self.jup_sell_patch.stop()
        self.jup_buy_patch.stop()
        self.metaplex_patch.stop()
        self.hh_get_patch.stop()
        self.hh_meta_patch.stop()
        self.meta_patch.stop()
        self.mint_id_patch.stop()
        self.mint_1h_patch.stop()
        self.mint_prices_patch.stop()
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

    def _buy_gate(self, usd="10"):
        # $10 at the mocked Gate price buys 20,000,000 GATE.
        return self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "usd": usd, "bot_id": ""})

    def _holding(self, coin=GATE):
        row = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, coin)).fetchone()
        return float(row["amount"]) if row else 0.0


class TestHeldCoinsOnly(CoinsPageBase):
    def test_dashboard_lists_only_held_coins(self):
        self._choose_mode("paper")
        html = self._html("/dashboard")
        self.assertIn("You hold no coins", html)
        self.assertNotIn(f"/coins/{GATE}/detail", html)
        self.assertNotIn("/coins/BTC/detail", html)
        self._buy_gate()
        html = self._html("/dashboard")
        self.assertIn(f"/coins/{GATE}/detail", html)
        self.assertIn("Gate", html)
        self.assertNotIn("/coins/BTC/detail", html)
        self.assertNotIn("Bitcoin", html)
        self.assertNotIn("You hold no coins", html)

    def test_wallet_lists_only_held_coins(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("You hold no coins", html)
        self._buy_gate()
        html = self._html("/wallet")
        self.assertIn(f"/coins/{GATE}/detail", html)
        self.assertIn("Gate", html)
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
        self._buy_gate()
        detail = self._html(f"/coins/{GATE}/detail")
        self.assertIn("↻ Refresh", detail)


class TestCoinSearch(CoinsPageBase):
    def _search(self, q):
        resp = self.client.get(f"/api/coins/search?q={q}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_search_never_lists_sol_or_majors(self):
        # SOL is the unit of account, never a listed coin — a name
        # search for it returns only the DexScreener Solana pairs,
        # never a SOL entry; a majors query with an empty feed
        # returns nothing invented.
        data = self._search("sol")
        ids = [r["coin_id"] for r in data["results"]]
        self.assertNotIn("SOL", ids)
        self.assertNotIn("Solana", [r["name"] for r in data["results"]])
        self.assertIn("BfunMint11111111111111111111111111111111", ids)
        with mock.patch.object(charts_mod, "_default_get",
                               return_value={"pairs": []}):
            charts_mod._search_cache.clear()
            data = self._search("btc")
        self.assertEqual(data["results"], [])
        self.assertIsNone(data["error"])

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
        self._buy_gate()
        with mock.patch.object(charts_mod, "fetch_prices_for_mints",
                               return_value={GATE: GATE_PRICE}), \
                mock.patch.object(charts_mod, "fetch_mint_identities",
                                  return_value={GATE: {"name": "Gate",
                                                      "symbol": "GATE"}}):
            charts_mod._search_cache.clear()
            data = self._search(GATE)
        entry = data["results"][0]
        # $10 at the mocked $0.0000005 price: 20,000,000 GATE held.
        self.assertAlmostEqual(entry["held"], 20_000_000.0, places=3)

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
            # No majors list to fall back on: a majors query is
            # empty too, with the same error.
            data = self._search("sol")
            self.assertEqual(data["results"], [])
            self.assertIsNotNone(data["error"])
        # A pasted mint address still resolves while the
        # name-search feed is down.
        with mock.patch.object(charts_mod, "fetch_prices_for_mints",
                               return_value={GATE: GATE_PRICE}), \
                mock.patch.object(charts_mod, "fetch_mint_identities",
                                  return_value={GATE: {"name": "Gate",
                                                      "symbol": "GATE"}}):
            charts_mod._search_cache.clear()
            data = self._search(GATE)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["coin_id"], GATE)
        self.assertIsNone(data["error"])

    def test_search_short_query_returns_nothing(self):
        data = self._search("s")
        self.assertEqual(data["results"], [])

    def test_search_pasted_address_resolves_with_price(self):
        # A pasted devnet mint address resolves directly to that
        # coin — name, ticker and live price from the trenches feed.
        gate = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
        with mock.patch.object(charts_mod, "fetch_prices_for_mints",
                               return_value={gate: 0.0000005}), \
                mock.patch.object(charts_mod, "fetch_mint_identities",
                                  return_value={gate: {"name": "Gate",
                                                      "symbol": "GATE"}}):
            charts_mod._search_cache.clear()
            data = self._search(gate)
        self.assertEqual(len(data["results"]), 1)
        entry = data["results"][0]
        self.assertEqual(entry["coin_id"], gate)
        self.assertEqual(entry["name"], "Gate")
        self.assertEqual(entry["symbol"], "GATE")
        self.assertEqual(entry["price_usd"], 0.0000005)
        self.assertTrue(entry["is_mint"])
        self.assertIsNone(data["error"])

    def test_search_pasted_address_without_feed_still_resolves(self):
        # A devnet mint the feed has never seen still resolves: the
        # honest label, no price — never a fabricated name or price.
        # The real buy is priced from the bonding curve, not the
        # feed, so the coin is still buyable from the popup.
        gate = GATE
        with mock.patch.object(charts_mod, "fetch_prices_for_mints",
                               return_value={}), \
                mock.patch.object(charts_mod, "fetch_mint_identities",
                                  return_value={}):
            charts_mod._search_cache.clear()
            data = self._search(gate)
        self.assertEqual(len(data["results"]), 1)
        entry = data["results"][0]
        self.assertEqual(entry["coin_id"], gate)
        self.assertEqual(entry["name"], "Unknown coin")
        self.assertIsNone(entry["symbol"])
        self.assertIsNone(entry["price_usd"])
        self.assertTrue(entry["is_mint"])

    def test_search_pasted_address_is_not_a_name_search(self):
        # The DexScreener name search would match BFUN for "bfun";
        # a pasted address must return only the mint it names.
        gate = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
        with mock.patch.object(charts_mod, "fetch_prices_for_mints",
                               return_value={}), \
                mock.patch.object(charts_mod, "fetch_mint_identities",
                                  return_value={}):
            charts_mod._search_cache.clear()
            data = self._search(gate)
        self.assertEqual([r["coin_id"] for r in data["results"]], [gate])
        self.assertNotIn("BFUN", [r["symbol"] for r in data["results"]])

    def test_quick_trade_popup_invites_address(self):
        self._choose_mode("paper")
        html = self._html("/dashboard")
        self.assertIn("paste a token address", html)
        self.assertIn('data-mode="paper"', html)

    def test_pasted_address_opens_coin_detail(self):
        # The pasted address resolves to the same detail/buy view
        # as a name search — the buy form is on that page. A mint
        # nothing names shows the honest "Unknown coin" label, so
        # the feed must name nothing here as well as price nothing.
        gate = GATE
        self._choose_mode("paper")
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}), \
                mock.patch.object(routes, "fetch_mint_identities",
                                  return_value={}):
            html = self._html(f"/coins/{gate}/detail")
        self.assertIn("Unknown coin", html)
        self.assertIn("Buy", html)

    def test_pasted_address_with_feed_name_opens_named_detail(self):
        # The same mint with the feed naming it shows its real
        # name + ticker on the detail page, not the fallback.
        gate = GATE
        self._choose_mode("paper")
        html = self._html(f"/coins/{gate}/detail")
        self.assertIn("Gate", html)
        self.assertIn("(GATE)", html)
        self.assertIn("Buy", html)

    def test_search_requires_login(self):
        self.client.get("/logout")
        resp = self.client.get("/api/coins/search?q=sol")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestQuickTradeAmounts(CoinsPageBase):
    def test_buy_by_token_amount(self):
        self._choose_mode("paper")
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount_tokens": "2", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        # 2 tokens at $0.0000005: $0.000001 spent, no fee (removed
        # 2026-11-12), net 2.0 GATE.
        self.assertAlmostEqual(self._holding(GATE), 2.0, places=9)

    def test_sell_by_usd_amount(self):
        self._choose_mode("paper")
        self._buy_gate()
        self.assertAlmostEqual(self._holding(GATE), 20_000_000.0,
                               places=3)
        resp = self.client.post(f"/coins/{GATE}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount_usd": "5"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        self.assertAlmostEqual(self._holding(GATE), 10_000_000.0,
                               places=3)

    def test_sell_by_token_amount_still_works(self):
        self._choose_mode("paper")
        self._buy_gate()
        resp = self.client.post(f"/coins/{GATE}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount": "0.5"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        self.assertAlmostEqual(self._holding(GATE), 19_999_999.5,
                               places=3)

    def test_buy_tokens_without_price_refuses_plainly(self):
        self._choose_mode("paper")
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}):
            # No wallet step stands between the Buy and pricing:
            # a Buy with no price is refused with the price
            # message.
            resp = self.client.post(f"/coins/{DEST}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{DEST}"),
                "amount_tokens": "1", "bot_id": ""})
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Price unavailable", html)

    def test_sell_usd_without_price_refuses_plainly(self):
        self._choose_mode("paper")
        self._buy_gate()
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={}):
            # Gate has a price, so use a mint holding instead: put a
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
    patch) so the 60-second SOL price cache is actually exercised;
    the trenches list's 120-second cache is what the page shows
    going stale and then fresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        # Gate priced at $10 for this class, so the holding row
        # reads "$10.0000" like the old SOL row did.
        self.mint_prices = {GATE: 10.0}
        self.mint_identities = {GATE: {"name": "Gate", "symbol": "GATE"}}
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            side_effect=lambda mints, http_get=None: {
                m: self.mint_prices[m] for m in mints
                if m in self.mint_prices})
        self.mint_prices_patch.start()
        self.mint_id_patch = mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints, http_get=None: {
                m: self.mint_identities[m] for m in mints
                if m in self.mint_identities})
        self.mint_id_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.mint_1h_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.hh_meta_patch = mock.patch.object(
            home_holdings_mod, "ensure_coin_meta", return_value=None)
        self.hh_meta_patch.start()
        self.hh_get_patch = mock.patch.object(
            home_holdings_mod, "_default_get",
            return_value={"pairs": []})
        self.hh_get_patch.start()
        home_holdings_mod.clear_image_cache()
        self.jup_buy_patch = mock.patch.object(
            jupiter_mod, "quote_buy_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests"))
        self.jup_buy_patch.start()
        self.jup_sell_patch = mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests"))
        self.jup_sell_patch.start()
        prices_mod.clear_price_cache()
        self.sol_price = 10.0

        def _live():
            return self.sol_price, 0.0

        self.live_patch = mock.patch.object(
            prices_mod, "_fetch_sol_price", side_effect=_live)
        self.live_patch.start()
        # The trenches row carries the price that proves the cache:
        # it moves only after a refresh clears the 120-second list.
        self.trench_price = 10.0

        def _scan():
            return {"candidates": [
                {"mint": "Bond1111111111111111111111111111111111111111",
                 "symbol": "BOND", "name": "Bondy",
                 "price_usd": self.trench_price,
                 "change_1h_pct": 5.0}],
                "prices": {}, "source": "dexscreener",
                "as_of": "test", "error": None}

        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan", side_effect=_scan)
        self.trenches_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "usd": "10", "bot_id": ""})

    def tearDown(self):
        self.live_patch.stop()
        self.trenches_patch.stop()
        self.jup_sell_patch.stop()
        self.jup_buy_patch.stop()
        self.hh_get_patch.stop()
        self.hh_meta_patch.stop()
        self.meta_patch.stop()
        self.mint_1h_patch.stop()
        self.mint_id_patch.stop()
        self.mint_prices_patch.stop()
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
        self.trench_price = 99.0
        self.sol_price = 99.0
        # Still the cached trenches rows (and the cached SOL price)
        # until a refresh.
        self.assertIn("$10.0000", self._html("/dashboard"))
        self.assertNotIn("$99.0000", self._html("/dashboard"))
        self.assertEqual(prices_mod.get_sol_price_usd(), 10.0)
        before = self._snapshots()
        resp = self.client.post("/dashboard/refresh", data={
            "csrf_token": csrf(self.client, "/dashboard")})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        html = self._html("/dashboard")
        self.assertIn("$99.0000", html)
        self.assertIn("Refreshed with live data.", html)
        self.assertEqual(prices_mod.get_sol_price_usd(), 99.0)
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
