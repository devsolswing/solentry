"""Search is Solana-only.

SolEntry is powered by Solana alone, so search results show no
other chain: a coin with a genuine Solana presence (a native
Solana token, or a wrapped version with a liquid Jupiter route)
gets exactly one "Solana" tag; a coin with no Solana presence
gets no tag at all. A Solana mint shows Solana, and a wrapped
token shows Solana + Wrapped — never the native coin. Nothing
is ever guessed: no other chain is ever shown.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts_mod
import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.networks import (
    SOLANA_PRESENT, coin_network, search_result_network)
from tradingbot.prices import COINS

# Native Solana tokens (SOL, BONK, JUP, WIF, POPCAT) plus the
# wrapped versions with a live liquid Jupiter route (verified
# 2026-11-11, 0.1 SOL buys).
EXPECTED_SOLANA = {
    "AAVE", "BONK", "BTC", "DOGE", "ETH", "INJ", "JUP", "LINK",
    "ONDO", "PEPE", "POPCAT", "RENDER", "SOL", "SUI", "TAO",
    "UNI", "USDC", "WIF", "XRP", "ZEC",
}

MINT = "11111111111111111111111111111111"
WRAPPED_ZEC_MINT = "F6KkTuEQN8XX9ZwdmZBkB96WvDg8YGrNNzLRXqpLJTd6"
BFUN_MINT = "BfunMint11111111111111111111111111111111"


def make_prices(changes):
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


def dex_pairs(*entries):
    return {"pairs": [{
        "chainId": "solana", "dexId": "raydium",
        "baseToken": {"address": mint, "name": name, "symbol": sym},
        "priceUsd": str(price), "liquidity": {"usd": liq}}
        for mint, name, sym, price, liq in entries]}


class TestSolanaOnlyData(unittest.TestCase):
    def test_solana_set_matches_expected(self):
        self.assertEqual(SOLANA_PRESENT, EXPECTED_SOLANA)
        self.assertEqual(len(EXPECTED_SOLANA), 20)

    def test_every_known_coin_is_solana_or_nothing(self):
        # No coin ever shows another chain: Solana tag or no tag.
        for symbol in COINS:
            info = search_result_network(symbol, False)
            if symbol in EXPECTED_SOLANA:
                self.assertEqual(info, {"networks": ["Solana"],
                                        "wrapped": False}, symbol)
            else:
                self.assertEqual(info, {"networks": [],
                                        "wrapped": False}, symbol)

    def test_usdc_has_exactly_one_solana_tag(self):
        self.assertEqual(
            search_result_network("USDC", False),
            {"networks": ["Solana"], "wrapped": False})

    def test_liquid_wrapped_coins_show_solana(self):
        for symbol in ("ZEC", "BTC", "ETH", "XRP"):
            self.assertEqual(
                search_result_network(symbol, False),
                {"networks": ["Solana"], "wrapped": False}, symbol)

    def test_solana_natives_show_solana(self):
        for symbol in ("WIF", "POPCAT", "BONK", "SOL", "JUP"):
            self.assertEqual(
                search_result_network(symbol, False),
                {"networks": ["Solana"], "wrapped": False}, symbol)

    def test_no_solana_presence_means_no_tag(self):
        for symbol in ("ADA", "ALGO", "APT", "ATOM", "AVAX", "BCH",
                       "DOT", "ETC", "FIL", "HBAR", "LTC", "NEAR",
                       "SEI", "TIA", "XLM", "XTZ"):
            self.assertEqual(
                search_result_network(symbol, False),
                {"networks": [], "wrapped": False}, symbol)

    def test_unknown_coin_lists_nothing_never_guessed(self):
        self.assertEqual(
            search_result_network("NOPE", False),
            {"networks": [], "wrapped": False})
        self.assertFalse(coin_network("NOPE")["known"])

    def test_detail_page_network_still_knows_usdc_is_multi_chain(self):
        # coin_network (coin detail page + send refusal) is
        # untouched by the search change: USDC is still multi-chain
        # there, it just shows one Solana tag in search.
        info = coin_network("USDC")
        self.assertTrue(info["multi_chain"])
        self.assertIsNone(info["network"])


class TestWrappedDetection(unittest.TestCase):
    def test_wrapped_mint_symbols(self):
        for sym in ("ZEC", "WZEC", "wZEC", "WBTC", "WETH", "WXRP",
                    "soBTC", "soETH"):
            info = search_result_network(WRAPPED_ZEC_MINT, True, sym)
            self.assertEqual(info["networks"], ["Solana"], sym)
            self.assertTrue(info["wrapped"], sym)

    def test_plain_solana_tokens_are_not_wrapped(self):
        for sym in ("BFUN", "BONK", "$WIF", "POPCAT", "USDC", None):
            info = search_result_network(BFUN_MINT, True, sym)
            self.assertEqual(info["networks"], ["Solana"], sym)
            self.assertFalse(info["wrapped"], sym)

    def test_known_coin_is_never_wrapped(self):
        self.assertEqual(
            search_result_network("ZEC", False),
            {"networks": ["Solana"], "wrapped": False})


class TestSearchCoinsNetwork(unittest.TestCase):
    def setUp(self):
        charts_mod._search_cache.clear()

    def tearDown(self):
        charts_mod._search_cache.clear()

    def _prices(self, changes):
        return lambda: make_prices(changes)

    def test_search_zec_shows_solana(self):
        data = charts_mod.search_coins(
            "zec", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({"ZEC": (1000.0, -4.6)}))
        zec = next(r for r in data["results"]
                   if r["coin_id"] == "ZEC")
        self.assertEqual(zec["networks"], ["Solana"])
        self.assertFalse(zec["wrapped"])

    def test_search_usdc_shows_one_solana_tag(self):
        data = charts_mod.search_coins(
            "usdc", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({"USDC": (1.0, 0.0)}))
        usdc = next(r for r in data["results"]
                    if r["coin_id"] == "USDC")
        self.assertEqual(usdc["networks"], ["Solana"])
        self.assertFalse(usdc["wrapped"])

    def test_search_ada_shows_no_tag(self):
        data = charts_mod.search_coins(
            "ada", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({"ADA": (0.5, 1.2)}))
        ada = next(r for r in data["results"]
                   if r["coin_id"] == "ADA")
        self.assertEqual(ada["networks"], [])
        self.assertFalse(ada["wrapped"])

    def test_search_mint_shows_solana(self):
        payload = dex_pairs(
            (BFUN_MINT, "BonkFun", "BFUN", 0.002, 50000))
        data = charts_mod.search_coins(
            "bfun", http_get=lambda url: payload,
            prices_fn=self._prices({}))
        entry = next(r for r in data["results"]
                     if r["symbol"] == "BFUN")
        self.assertTrue(entry["is_mint"])
        self.assertEqual(entry["networks"], ["Solana"])
        self.assertFalse(entry["wrapped"])

    def test_search_wrapped_mint_shows_solana_plus_wrapped(self):
        # "wzec" matches no known coin, so the thin Solana wrapped
        # ZEC mint is listed — as Solana + Wrapped, never as the
        # native ZEC.
        payload = dex_pairs(
            (WRAPPED_ZEC_MINT, "Zcash", "ZEC", 1650.65, 1650344970.64))
        data = charts_mod.search_coins(
            "wzec", http_get=lambda url: payload,
            prices_fn=self._prices({"ZEC": (1000.0, -4.6)}))
        entry = next(r for r in data["results"]
                     if r["coin_id"] == WRAPPED_ZEC_MINT)
        self.assertTrue(entry["is_mint"])
        self.assertEqual(entry["networks"], ["Solana"])
        self.assertTrue(entry["wrapped"])

    def test_search_by_address_shows_solana(self):
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
        self.assertEqual(entry["networks"], ["Solana"])
        self.assertFalse(entry["wrapped"])

    def test_search_nonsense_returns_nothing_invented(self):
        data = charts_mod.search_coins(
            "zzzzqq", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({}))
        self.assertEqual(data["results"], [])


class TestSearchEndpointNetwork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "SOL": (10.0, 5.25), "USDC": (1.0, 0.0),
                "ZEC": (1000.0, -4.6), "ADA": (0.5, 1.2)}))
        self.prices_patch.start()
        self.search_patch = mock.patch.object(
            charts_mod, "_default_get", return_value={"pairs": []})
        self.search_patch.start()
        charts_mod._search_cache.clear()
        signup(self.client, "alice")

    def tearDown(self):
        self.search_patch.stop()
        self.prices_patch.stop()
        charts_mod._search_cache.clear()
        self.tmp.cleanup()

    def _search(self, q):
        resp = self.client.get(f"/api/coins/search?q={q}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_endpoint_zec_has_solana_tag(self):
        data = self._search("zec")
        zec = next(r for r in data["results"]
                   if r["coin_id"] == "ZEC")
        self.assertEqual(zec["networks"], ["Solana"])
        self.assertFalse(zec["wrapped"])

    def test_endpoint_usdc_has_one_solana_tag(self):
        data = self._search("usdc")
        usdc = next(r for r in data["results"]
                    if r["coin_id"] == "USDC")
        self.assertEqual(usdc["networks"], ["Solana"])
        self.assertFalse(usdc["wrapped"])

    def test_endpoint_ada_has_no_tag(self):
        data = self._search("ada")
        ada = next(r for r in data["results"]
                   if r["coin_id"] == "ADA")
        self.assertEqual(ada["networks"], [])
        self.assertFalse(ada["wrapped"])


if __name__ == "__main__":
    unittest.main()
