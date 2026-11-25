"""Live Buy/Sell price for every listed coin (a pump.fun mint is the
proof case).

The coin detail page's 1-second tick serves pump.fun mints and SOL
only, from the single Helius source: a fresh live-feed tick wins,
a stale one is no price at all ("no data"), and a mint nobody has
priced says so plainly ("no data") instead of showing a made-up
number. There is no second-source fallback any more. Symbols (ZEC, BTC, …) are not coins any more —
the tick endpoint answers ok:false "Unknown coin" for them and
their detail pages 404. Search returns pump.fun coins only: a name
search returns the Solana mint(s) DexScreener finds (real name +
ticker + mint address), and a pasted mint address resolves
directly to that mint.
"""

import json
import tempfile
import time
import unittest
from unittest import mock

import tradingbot.charts as charts_mod
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import make_client, signup

# Valid pump.fun mint addresses (base58, 32 bytes) used as test coins.
BOND_MINT = "Bond1111111111111111111111111111111111111111"
GRAD_MINT = "Grad1111111111111111111111111111111111111111"
# A real Solana wrapped-ZEC pair, as DexScreener returned it live
# on 2026-11-11: same name/symbol as the real coin, a different
# price ($1,650.65 vs the real ~$1,000).
WRAPPED_ZEC_MINT = "F6KkTuEQN8XX9ZwdmZBkB96WvDg8YGrNNzLRXqpLJTd6"


def make_prices():
    """SOL only: the price list has no majors any more."""
    return {"prices": {"SOL": {"price": 100.0, "change_24h": 5.25,
                               "change_1h": 2.5}},
            "source": "live", "as_of": "test"}


def fake_get_price(coin):
    if coin == "SOL":
        return (100.0, "live", "test")
    return (None, "live", "test")


class LiveCoinTickBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.feed = self.app.config["LIVE_FEED"]
        # Single price source: the feed's SOL tick is the SOL price
        # (the old get_prices / fetch_prices_for_mints mocks are
        # gone with the second source). Mint prices come from feed
        # ticks seeded per test.
        self.feed.ingest("SOL", 100.0, "jupiter")
        self.price_patch = mock.patch.object(
            prices, "get_price", side_effect=fake_get_price)
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.meta_patch.stop()
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.tmp.cleanup()

    def _tick(self, coin_id):
        resp = self.client.get(f"/api/coins/{coin_id}/tick")
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.get_data(as_text=True))

    def _mint_price(self, mint, price):
        """Seed the feed with a fresh stream tick pricing `mint`
        (the single source) and return a no-op context, so the old
        call sites keep reading the same way."""
        self.feed.ingest(mint, price, "helius-stream")
        return mock.patch.object(routes, "_TRENCHES_CACHE",
                                 routes._TRENCHES_CACHE)


class TestMintTick(LiveCoinTickBase):
    def test_tick_serves_fresh_feed_price_for_mint(self):
        self.feed.ingest(BOND_MINT, 0.0015, "helius-stream")
        data = self._tick(BOND_MINT)
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], BOND_MINT)
        self.assertEqual(data["price"], 0.0015)
        self.assertEqual(data["source"], "helius-stream")

    def test_tick_lowercase_sol(self):
        # SOL is the one case-insensitive coin id: "sol" resolves
        # to the SOL tick. Mint addresses are case-sensitive and
        # are never lowercased.
        self.feed.ingest("SOL", 101.5, "jupiter")
        data = self._tick("sol")
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], "SOL")
        self.assertEqual(data["price"], 101.5)

    def test_tick_no_feed_tick_says_no_data(self):
        # No stream tick for the mint: no second source to fall
        # back to — the endpoint says "no data", never a number
        # from anywhere else.
        data = self._tick(BOND_MINT)
        self.assertFalse(data["ok"])
        self.assertEqual(data["coin_id"], BOND_MINT)
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")

    def test_tick_stale_feed_tick_says_no_data(self):
        # A feed tick older than 30s is never served as fresh, and
        # there is no fallback any more: the endpoint says "no
        # data" instead of showing the stale number with a fresh
        # face.
        self.feed.ingest(BOND_MINT, 0.0009, "helius-stream",
                         now=time.time() - 60)
        data = self._tick(BOND_MINT)
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")

    def test_tick_unknown_coin_says_so(self):
        data = self._tick("NOPE")
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])

    def test_tick_symbol_is_not_a_coin(self):
        # ZEC is not a pump.fun mint and not SOL: no live price,
        # never a guessed one.
        data = self._tick("ZEC")
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")

    def test_tick_mint_with_no_price_says_so(self):
        data = self._tick(BOND_MINT)
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")

    def test_tick_known_coin_prefers_feed_over_cached_price(self):
        # The feed's SOL tick is $100.00; a fresher tick of $101.50
        # wins — the page shows the live number, never a stale one.
        self.feed.ingest("SOL", 101.5, "jupiter")
        data = self._tick("SOL")
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 101.5)
        self.assertEqual(data["source"], "jupiter")


class TestMintDetail(LiveCoinTickBase):
    def test_coin_detail_mint_loads(self):
        with self._mint_price(BOND_MINT, 0.001), \
                mock.patch.object(
                    routes, "fetch_trenches_scan",
                    return_value={"candidates": [{
                        "mint": BOND_MINT, "symbol": "BOND",
                        "name": "BondCoin", "price_usd": 0.001,
                        "change_1h_pct": None}],
                        "prices": {}, "source": "dexscreener",
                        "as_of": "test", "error": None}), \
                mock.patch.object(
                    routes, "fetch_market_chart",
                    return_value={"points": [
                        {"ts": 1790000000000, "price": 0.001},
                        {"ts": 1790000060000, "price": 0.0011}],
                        "error": None}), \
                mock.patch.object(routes, "fetch_trade_flow",
                                  return_value=None):
            routes._TRENCHES_CACHE.update(
                {"at": 0.0, "rows": None, "error": None})
            resp = self.client.get(f"/coins/{BOND_MINT}/detail")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("BondCoin", html)
        self.assertIn(
            f'data-tick-url="/api/coins/{BOND_MINT}/tick"', html)
        self.assertIn('id="live-price"', html)
        self.assertIn(f'data-live-price="{BOND_MINT}"', html)

    def test_coin_detail_symbol_404s(self):
        # A symbol is not a coin any more: its detail page 404s
        # instead of rendering a page for a coin that does not exist.
        resp = self.client.get("/coins/ZEC/detail")
        self.assertEqual(resp.status_code, 404)

    def test_coin_detail_unpriced_mint_says_price_unavailable(self):
        # A mint the feed has not priced still loads — and says
        # plainly that the price is unavailable, never inventing
        # one. The address itself is never shown as the coin's name.
        with mock.patch.object(
                routes, "fetch_market_chart",
                return_value={"points": [],
                              "error": "chart still building — "
                                       "no live ticks yet"}), \
                mock.patch.object(routes, "fetch_trade_flow",
                                  return_value=None):
            resp = self.client.get(f"/coins/{BOND_MINT}/detail")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("no data", html)
        self.assertIn("<h1>Unknown coin", html)
        self.assertIn("Candles are still building", html)


class TestMintSearch(LiveCoinTickBase):
    def test_search_drops_wrapped_major_not_symbol(self):
        # DexScreener offers a thin Solana wrapped ZEC at $1,650
        # on Raydium — search drops it: a wrapped major is not a
        # pump.fun coin, so a "zec" name search returns nothing
        # (no symbol entry either). The mint stays reachable by
        # pasted address (see the test below), never by name
        # search. (This test used to assert the wrapped mint came
        # back — that was the pump.fun-only search leak.)
        payload = {"pairs": [{
            "chainId": "solana",
            "dexId": "raydium",
            "baseToken": {"address": WRAPPED_ZEC_MINT,
                          "name": "Zcash", "symbol": "ZEC"},
            "priceUsd": "1650.65",
            "liquidity": {"usd": 1650344970.64}}]}
        with mock.patch.object(charts_mod, "_default_get",
                               return_value=payload):
            charts_mod._search_cache.clear()
            resp = self.client.get("/api/coins/search?q=zec")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [])
        self.assertNotIn(WRAPPED_ZEC_MINT,
                         [r["coin_id"] for r in data["results"]])

    def test_search_by_pasted_mint_resolves_directly(self):
        # A pasted mint address resolves to that mint, with the
        # feed's real name/ticker/price — never a name search.
        self.feed.ingest(BOND_MINT, 0.001, "helius-stream")
        with mock.patch.object(
                charts_mod, "fetch_mint_identities",
                return_value={BOND_MINT: {"name": "BondCoin",
                                          "symbol": "BOND"}}):
            charts_mod._search_cache.clear()
            resp = self.client.get(
                f"/api/coins/search?q={BOND_MINT}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertEqual(result["coin_id"], BOND_MINT)
        self.assertEqual(result["name"], "BondCoin")
        self.assertEqual(result["symbol"], "BOND")
        self.assertEqual(result["price_usd"], 0.001)
        self.assertTrue(result["is_mint"])


if __name__ == "__main__":
    unittest.main()
