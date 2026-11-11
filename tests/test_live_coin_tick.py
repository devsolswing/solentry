"""Live Buy/Sell price for every listed coin (ZEC is the proof case).

Before this build the coin detail page's 1-second tick only served
the 8 original COINS (via a 60s-cached CoinGecko price) and Solana
mint addresses (via DexScreener) — any other coin got ok:false
"Unknown coin — no live price" and its price never updated after
page load, sitting at the load-time price (or the baked 2026-11-09
snapshot when CoinGecko failed). Worse, searching "ZEC" only found
thin Solana wrapped copies ($268–$1,650) while the real ZEC traded
~$1,000. Now every coin in prices.COINS has a Kraken pair, the tick
endpoint serves the live feed's fresh tick first, and search returns
the real coin before any same-symbol mint.
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

MINT = "11111111111111111111111111111111"
# A real Solana wrapped-ZEC pair, as DexScreener returned it live
# on 2026-11-11: same name/symbol as the real coin, a different
# price ($1,650.65 vs the real ~$1,000), and it barely moves.
WRAPPED_ZEC_MINT = "F6KkTuEQN8XX9ZwdmZBkB96WvDg8YGrNNzLRXqpLJTd6"


def make_prices(changes):
    out = {}
    for c, tup in changes.items():
        out[c] = {"price": tup[0], "change_24h": tup[1],
                  "change_1h": tup[2] if len(tup) > 2 else None}
    return {"prices": out, "source": "snapshot", "as_of": "test"}


def fake_get_price(coin):
    table = {"SOL": (10.0, "snapshot", "test"),
             "ZEC": (1000.0, "snapshot", "test")}
    return table.get(coin, (None, "snapshot", "test"))


class LiveCoinTickBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25, 2.5), "USDC": (1.0, 0.0),
                "ZEC": (1000.0, -4.6, -1.2)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", side_effect=fake_get_price)
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.feed = self.app.config["LIVE_FEED"]

    def tearDown(self):
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.tmp.cleanup()

    def _tick(self, coin_id):
        resp = self.client.get(f"/api/coins/{coin_id}/tick")
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.get_data(as_text=True))


class TestZecTick(LiveCoinTickBase):
    def test_tick_serves_fresh_feed_price_for_zec(self):
        self.feed.ingest("ZEC", 1001.5, "kraken")
        data = self._tick("ZEC")
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], "ZEC")
        self.assertEqual(data["price"], 1001.5)
        self.assertEqual(data["source"], "kraken")

    def test_tick_lowercase_zec(self):
        self.feed.ingest("ZEC", 1001.5, "kraken")
        data = self._tick("zec")
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], "ZEC")
        self.assertEqual(data["price"], 1001.5)

    def test_tick_falls_back_to_coingecko_when_feed_empty(self):
        data = self._tick("ZEC")
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], "ZEC")
        self.assertEqual(data["price"], 1000.0)
        self.assertEqual(data["source"], "snapshot")
        self.assertEqual(data["as_of"], "test")

    def test_tick_stale_feed_tick_falls_back(self):
        # A feed tick older than 30s is never served as fresh: the
        # endpoint falls back to the CoinGecko price instead of
        # showing the stale number with a fresh face.
        self.feed.ingest("ZEC", 999.0, "kraken",
                         now=time.time() - 60)
        data = self._tick("ZEC")
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 1000.0)
        self.assertEqual(data["source"], "snapshot")

    def test_tick_unknown_coin_says_so(self):
        data = self._tick("NOPE")
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])

    def test_tick_known_coin_prefers_feed_over_cached_price(self):
        # SOL has a mocked CoinGecko price of $10.00; a fresh feed
        # tick of $10.50 wins — the page shows the live number,
        # not the up-to-60-second-old cached one.
        self.feed.ingest("SOL", 10.5, "kraken")
        data = self._tick("SOL")
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 10.5)
        self.assertEqual(data["source"], "kraken")


class TestZecDetail(LiveCoinTickBase):
    def test_coin_detail_zec_loads(self):
        with mock.patch.object(
                routes, "fetch_market_chart",
                return_value={"points": [
                    {"ts": 1790000000000, "price": 1000.0},
                    {"ts": 1790000060000, "price": 1001.0}],
                    "error": None}), \
                mock.patch.object(routes, "fetch_ohlc", return_value={
                    "candles": [
                        {"ts": 1790000000000, "open": 999.0,
                         "high": 1002.0, "low": 998.0, "close": 1001.0}],
                    "interval": 1800, "error": None}), \
                mock.patch.object(routes, "fetch_trade_flow",
                                  return_value=None):
            resp = self.client.get("/coins/ZEC/detail")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Zcash", html)
        self.assertIn('data-tick-url="/api/coins/ZEC/tick"', html)
        self.assertIn('id="live-price"', html)
        self.assertIn('data-live-price="ZEC"', html)

    def test_coin_detail_zec_labels_snapshot_as_snapshot(self):
        # The mocked price feed is the baked snapshot: the page
        # must say so plainly, never present it as live.
        with mock.patch.object(
                routes, "fetch_market_chart",
                return_value={"points": [], "error": "no data"}), \
                mock.patch.object(routes, "fetch_ohlc", return_value={
                    "candles": [], "interval": None,
                    "error": "no data"}), \
                mock.patch.object(routes, "fetch_trade_flow",
                                  return_value=None):
            resp = self.client.get("/coins/ZEC/detail")
        html = resp.get_data(as_text=True)
        self.assertIn("snapshot from", html)
        self.assertIn("not live", html)


class TestZecSearch(LiveCoinTickBase):
    def test_search_returns_real_zec_not_wrapped_mint(self):
        # DexScreener offers a thin Solana wrapped ZEC at $1,650;
        # the real ZEC (from the price list, $1,000) must win, and
        # the same-symbol mint must be skipped, not listed beside
        # it as a second "ZEC".
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
        ids = [r["coin_id"] for r in data["results"]]
        self.assertIn("ZEC", ids)
        self.assertNotIn(WRAPPED_ZEC_MINT, ids)
        zec = next(r for r in data["results"]
                   if r["coin_id"] == "ZEC")
        self.assertEqual(zec["name"], "Zcash")
        self.assertEqual(zec["price_usd"], 1000.0)
        self.assertFalse(zec["is_mint"])


if __name__ == "__main__":
    unittest.main()
