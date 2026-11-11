"""Search shows the network of every listed coin.

As the user types, each search result shows its network: known
coins show their native chain (all 36 — ZEC is Zcash, BTC is
Bitcoin, ...), a multi-chain coin (USDC) shows EVERY verified
network as a small tag, a Solana mint shows Solana, and a wrapped
token shows Solana + Wrapped — never the native coin. The network
is the coin's real chain, never guessed: an unknown coin lists
nothing, and the row says "Network unknown" instead.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts_mod
import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.networks import (
    MULTI_CHAIN_NETWORKS, coin_network, coin_networks,
    search_result_network)
from tradingbot.prices import COINS

USDC_NETWORKS = ["Solana", "Algorand", "Aptos", "Arbitrum", "Avalanche",
                 "Base", "Celo", "Cronos", "Ethereum", "Hedera",
                 "Injective", "NEAR Protocol", "Optimism", "Polkadot",
                 "Polygon", "Sei", "Starknet", "Stellar", "Sui",
                 "XRP Ledger", "zkSync"]

EXPECTED_NETWORKS = {
    "BTC": ["Bitcoin"], "ETH": ["Ethereum"], "SOL": ["Solana"],
    "USDC": USDC_NETWORKS, "DOGE": ["Dogecoin"],
    "PEPE": ["Ethereum"], "BONK": ["Solana"], "JUP": ["Solana"],
    "ZEC": ["Zcash"], "XRP": ["XRP Ledger"], "ADA": ["Cardano"],
    "LTC": ["Litecoin"], "DOT": ["Polkadot"], "LINK": ["Ethereum"],
    "AVAX": ["Avalanche"], "ATOM": ["Cosmos"], "XLM": ["Stellar"],
    "ALGO": ["Algorand"], "NEAR": ["NEAR Protocol"],
    "UNI": ["Ethereum"], "AAVE": ["Ethereum"], "XTZ": ["Tezos"],
    "ETC": ["Ethereum Classic"], "BCH": ["Bitcoin Cash"],
    "FIL": ["Filecoin"], "HBAR": ["Hedera"], "SUI": ["Sui"],
    "APT": ["Aptos"], "SEI": ["Sei"], "INJ": ["Injective"],
    "TIA": ["Celestia"], "ONDO": ["Ethereum"],
    "RENDER": ["Ethereum"], "TAO": ["Bittensor"], "WIF": ["Solana"],
    "POPCAT": ["Solana"],
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


class TestNetworkData(unittest.TestCase):
    def test_every_known_coin_has_a_verified_network(self):
        self.assertEqual(set(EXPECTED_NETWORKS), set(COINS))
        for symbol, networks in EXPECTED_NETWORKS.items():
            self.assertEqual(coin_networks(symbol), networks, symbol)
            self.assertTrue(networks, symbol)

    def test_usdc_lists_every_verified_network(self):
        self.assertEqual(MULTI_CHAIN_NETWORKS["USDC"], USDC_NETWORKS)
        self.assertEqual(len(USDC_NETWORKS), 21)
        self.assertEqual(USDC_NETWORKS[0], "Solana")
        info = coin_network("USDC")
        self.assertTrue(info["multi_chain"])
        self.assertIsNone(info["network"])

    def test_unknown_coin_lists_nothing_never_guessed(self):
        self.assertEqual(coin_networks("NOPE"), [])
        self.assertFalse(coin_network("NOPE")["known"])
        self.assertEqual(
            search_result_network("NOPE", False),
            {"networks": [], "wrapped": False})


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
            {"networks": ["Zcash"], "wrapped": False})


class TestSearchCoinsNetwork(unittest.TestCase):
    def setUp(self):
        charts_mod._search_cache.clear()

    def tearDown(self):
        charts_mod._search_cache.clear()

    def _prices(self, changes):
        return lambda: make_prices(changes)

    def test_search_zec_shows_zcash(self):
        data = charts_mod.search_coins(
            "zec", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({"ZEC": (1000.0, -4.6)}))
        zec = next(r for r in data["results"]
                   if r["coin_id"] == "ZEC")
        self.assertEqual(zec["networks"], ["Zcash"])
        self.assertFalse(zec["wrapped"])

    def test_search_usdc_shows_every_network(self):
        data = charts_mod.search_coins(
            "usdc", http_get=lambda url: {"pairs": []},
            prices_fn=self._prices({"USDC": (1.0, 0.0)}))
        usdc = next(r for r in data["results"]
                    if r["coin_id"] == "USDC")
        self.assertEqual(usdc["networks"], USDC_NETWORKS)
        self.assertFalse(usdc["wrapped"])

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
                "ZEC": (1000.0, -4.6)}))
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

    def test_endpoint_zec_has_network(self):
        data = self._search("zec")
        zec = next(r for r in data["results"]
                   if r["coin_id"] == "ZEC")
        self.assertEqual(zec["networks"], ["Zcash"])
        self.assertFalse(zec["wrapped"])

    def test_endpoint_usdc_has_every_network(self):
        data = self._search("usdc")
        usdc = next(r for r in data["results"]
                    if r["coin_id"] == "USDC")
        self.assertEqual(usdc["networks"], USDC_NETWORKS)
        self.assertFalse(usdc["wrapped"])


if __name__ == "__main__":
    unittest.main()
