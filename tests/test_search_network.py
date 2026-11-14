"""Search is pump.fun-only.

The app lists no major coins any more, so search returns pump.fun
coins only: a name/symbol query matches DexScreener Solana pairs
(real name + ticker + mint address), a pasted mint address resolves
directly (trenches feed, then on-chain Metaplex metadata), and a
majors query (BTC, ETH, SOL, ZEC, USDC, ADA, …) returns nothing —
SOL is the unit of account, never a listed coin. A mint nobody can
name reads "Unknown coin" — the address is the `coin_id`, never
shown as the name, and the old "Pump.fun token …" label is gone.
No `networks` / `wrapped` fields exist any more: every result is a
Solana pump.fun coin by construction.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts_mod
import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.networks import coin_network, send_refusal

MINT = "11111111111111111111111111111111"
BFUN_MINT = "BfunMint11111111111111111111111111111111"
GATE_MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"


def dex_pairs(*entries):
    return {"pairs": [{
        "chainId": "solana", "dexId": dex_id,
        "baseToken": {"address": mint, "name": name, "symbol": sym},
        "priceUsd": str(price), "liquidity": {"usd": liq}}
        for mint, name, sym, price, liq, dex_id in entries]}


def pumpfun_pair(mint, name, sym, price=0.002, liq=50000):
    return (mint, name, sym, price, liq, "pumpfun")


class TestCoinNetworkUnit(unittest.TestCase):
    def test_mint_is_solana(self):
        info = coin_network(MINT)
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertFalse(info["multi_chain"])
        self.assertTrue(info["known"])
        self.assertIsNone(send_refusal(MINT))

    def test_sol_is_native_solana_not_a_listed_coin(self):
        # SOL is the network's native currency — a SOL send IS a
        # Solana send, so there is no refusal — but SOL is never a
        # listed or tradable coin (see the search tests below).
        info = coin_network("SOL")
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertIsNone(send_refusal("SOL"))

    def test_symbol_is_not_a_coin(self):
        # A major symbol is not a mint address: it is not a coin in
        # this app at all, never guessed onto a chain.
        for symbol in ("BTC", "ETH", "ZEC", "USDC", "ADA"):
            info = coin_network(symbol)
            self.assertFalse(info["known"], symbol)
            self.assertFalse(info["is_solana"], symbol)
            refusal = send_refusal(symbol)
            self.assertIn("not a Solana token", refusal)
            self.assertIn("Nothing was sent", refusal)

    def test_unknown_is_refused_never_guessed(self):
        info = coin_network("NOPE")
        self.assertFalse(info["known"])
        self.assertFalse(info["is_solana"])
        refusal = send_refusal("NOPE")
        self.assertIn("not a Solana token", refusal)
        self.assertIn("Nothing was sent", refusal)


class TestSearchCoinsPumpfunOnly(unittest.TestCase):
    def setUp(self):
        charts_mod._search_cache.clear()

    def tearDown(self):
        charts_mod._search_cache.clear()

    def test_name_search_returns_pumpfun_coin(self):
        payload = dex_pairs(pumpfun_pair(BFUN_MINT, "BonkFun", "BFUN"))
        data = charts_mod.search_coins(
            "bfun", http_get=lambda url: payload)
        self.assertEqual(len(data["results"]), 1)
        entry = data["results"][0]
        self.assertTrue(entry["is_mint"])
        self.assertEqual(entry["coin_id"], BFUN_MINT)
        self.assertEqual(entry["name"], "BonkFun")
        self.assertEqual(entry["symbol"], "BFUN")
        self.assertEqual(entry["price_usd"], 0.002)
        self.assertNotIn("networks", entry)
        self.assertNotIn("wrapped", entry)

    def test_majors_queries_return_nothing(self):
        # DexScreener filters by query server-side: a majors query
        # matches no pump.fun pair, so the feed returns no pairs.
        payload = dex_pairs(pumpfun_pair(BFUN_MINT, "BonkFun", "BFUN"))
        for q in ("zec", "btc", "eth", "sol", "usdc", "ada"):
            charts_mod._search_cache.clear()
            data = charts_mod.search_coins(
                q, http_get=lambda url: {"pairs": []})
            self.assertEqual(data["results"], [], q)

    def test_non_solana_chain_pairs_excluded(self):
        payload = {"pairs": [{
            "chainId": "ethereum", "dexId": "uniswap",
            "baseToken": {"address": BFUN_MINT, "name": "BonkFun",
                          "symbol": "BFUN"},
            "priceUsd": "0.002", "liquidity": {"usd": 50000}}]}
        data = charts_mod.search_coins(
            "bfun", http_get=lambda url: payload)
        self.assertEqual(data["results"], [])

    def test_best_liquidity_pair_wins_per_mint(self):
        payload = dex_pairs(
            pumpfun_pair(BFUN_MINT, "BonkFun", "BFUN", 0.002, 1000),
            (BFUN_MINT, "BonkFun", "BFUN", 0.003, 90000, "pumpswap"))
        data = charts_mod.search_coins(
            "bfun", http_get=lambda url: payload)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["price_usd"], 0.003)

    def test_search_by_address_uses_feed_identity(self):
        with mock.patch.object(
                charts_mod, "fetch_prices_for_mints",
                return_value={MINT: 0.002}), \
                mock.patch.object(
                    charts_mod, "fetch_mint_identities",
                    return_value={MINT: {"name": "BonkFun",
                                         "symbol": "BFUN"}}):
            data = charts_mod.search_coins(MINT)
        self.assertEqual(len(data["results"]), 1)
        entry = data["results"][0]
        self.assertTrue(entry["is_mint"])
        self.assertEqual(entry["coin_id"], MINT)
        self.assertEqual(entry["name"], "BonkFun")
        self.assertEqual(entry["symbol"], "BFUN")
        self.assertEqual(entry["price_usd"], 0.002)

    def test_search_by_address_unknown_mint_is_unknown_coin(self):
        # Nobody can name it: no feed identity, no on-chain
        # metadata — the name is "Unknown coin", never the address
        # and never the old "Pump.fun token …" label.
        with mock.patch.object(
                charts_mod, "fetch_prices_for_mints",
                return_value={}), \
                mock.patch.object(
                    charts_mod, "fetch_mint_identities",
                    return_value={}), \
                mock.patch("tradingbot.coin_meta._from_metaplex",
                           return_value=None):
            data = charts_mod.search_coins(MINT)
        entry = data["results"][0]
        self.assertEqual(entry["name"], "Unknown coin")
        self.assertIsNone(entry["symbol"])
        self.assertIsNone(entry["price_usd"])
        self.assertNotIn(MINT, entry["name"])

    def test_search_by_address_uses_chain_metadata(self):
        # A bonding-curve mint the feed has never seen still has
        # its real name on-chain (Gate).
        with mock.patch.object(
                charts_mod, "fetch_prices_for_mints",
                return_value={}), \
                mock.patch.object(
                    charts_mod, "fetch_mint_identities",
                    return_value={}), \
                mock.patch("tradingbot.coin_meta._from_metaplex",
                           return_value={"name": "Gate",
                                         "symbol": "GATE",
                                         "uri": None}):
            data = charts_mod.search_coins(GATE_MINT)
        entry = data["results"][0]
        self.assertEqual(entry["name"], "Gate")
        self.assertEqual(entry["symbol"], "GATE")

    def test_search_nonsense_returns_nothing_invented(self):
        data = charts_mod.search_coins(
            "zzzzqq", http_get=lambda url: {"pairs": []})
        self.assertEqual(data["results"], [])
        self.assertIsNone(data["error"])

    def test_search_feed_down_returns_error_not_coins(self):
        data = charts_mod.search_coins(
            "bfun", http_get=lambda url: (_ for _ in ()).throw(
                Exception("down")))
        self.assertEqual(data["results"], [])
        self.assertIsNotNone(data["error"])

    def test_short_query_returns_nothing(self):
        data = charts_mod.search_coins(
            "b", http_get=lambda url: {"pairs": []})
        self.assertEqual(data["results"], [])


class TestSearchEndpointPumpfunOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

        def fake_get(url):
            # DexScreener filters by query server-side: only a
            # query matching the coin returns its pair.
            if "bfun" in url.lower():
                return dex_pairs(
                    pumpfun_pair(BFUN_MINT, "BonkFun", "BFUN"))
            return {"pairs": []}

        self.search_patch = mock.patch.object(
            charts_mod, "_default_get", side_effect=fake_get)
        self.search_patch.start()
        charts_mod._search_cache.clear()
        signup(self.client, "alice")

    def tearDown(self):
        self.search_patch.stop()
        charts_mod._search_cache.clear()
        self.tmp.cleanup()

    def _search(self, q):
        resp = self.client.get(f"/api/coins/search?q={q}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_endpoint_name_search_finds_pumpfun_coin(self):
        data = self._search("bfun")
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["coin_id"], BFUN_MINT)
        self.assertEqual(data["results"][0]["name"], "BonkFun")
        self.assertEqual(data["results"][0]["symbol"], "BFUN")

    def test_endpoint_majors_queries_find_nothing(self):
        # A majors query matches no pump.fun pair, so the feed
        # returns no pairs and the endpoint returns no results.
        for q in ("zec", "btc", "sol", "usdc"):
            charts_mod._search_cache.clear()
            data = self._search(q)
            self.assertEqual(data["results"], [], q)


if __name__ == "__main__":
    unittest.main()
