"""Jupiter DEX aggregation: route discovery ("Finding route…") and
pricing for Solana coins.

The old Swap API v6 host (quote-api.jup.ag) is RETIRED — every call
to it fails. The live surface is lite-api.jup.ag/swap/v1 (keyless)
with api.jup.ag/swap/v1 as the keyed host. The unit tests below mock
Jupiter's HTTP responses using the real response shapes captured
live on 2026-11-11; the live tests at the bottom hit the real API
and skip (never fail) when the network is unavailable, so an
offline run stays green.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import tradingbot.jupiter as jup

# A real Jupiter quote response shape, captured live 2026-11-11
# (SOL -> USDC, 0.1 SOL): two hops, each with its DEX label. Only
# the fields the parser reads are kept.
REAL_QUOTE = {
    "inputMint": "So11111111111111111111111111111111111111112",
    "inAmount": "100000000",
    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "outAmount": "8455700",
    "otherAmountThreshold": "8371143",
    "swapMode": "ExactIn",
    "slippageBps": 100,
    "platformFee": None,
    "priceImpactPct": "0",
    "routePlan": [
        {"swapInfo": {
            "ammKey": "DyzGYEhdgSn5EEUt4XXviavZ7v7SV2YGrYV8HX3Aw5XT",
            "label": "Byreal",
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
            "inAmount": "100000000",
            "outAmount": "8468729"},
         "percent": 100},
        {"swapInfo": {
            "ammKey": "YxydH4j4NfnYHRy9VMsNscmrh6XtteEehdtEWcifaJU",
            "label": "Scorch",
            "inputMint": "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "inAmount": "8468729",
            "outAmount": "8455700"},
         "percent": 100},
    ],
    "contextSlot": 468281316,
}

# The four wrapped mints, exactly as Jupiter's token API returned
# them live on 2026-11-11 (all isVerified: true).
TOKEN_SEARCH_ZEC = [
    {"id": "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",
     "name": "Zcash", "symbol": "ZEC", "decimals": 8,
     "isVerified": True, "liquidity": 1234567.0,
     "usdPrice": 989.99},
    {"id": "HwUjcLCcnL2z766ReEKmynZTeY1516SPX47irXDpump",
     "name": "Zesty Estrogenic Chud", "symbol": "ZEC", "decimals": 6,
     "isVerified": False, "liquidity": 99999999.0,
     "usdPrice": 3.27e-06},
]


class JupiterQuoteTests(unittest.TestCase):
    def test_parse_real_quote_shape(self):
        quote = jup._parse_quote(REAL_QUOTE, side="buy", symbol="USDC",
                                 wrapped=False, display_name="USD Coin")
        self.assertEqual(quote["in_amount"], 100_000_000)
        self.assertEqual(quote["out_amount"], 8_455_700)
        self.assertEqual(quote["route_labels"], ["Byreal", "Scorch"])
        self.assertEqual(quote["price_impact_pct"], 0.0)
        self.assertEqual(quote["source"], "jupiter")
        self.assertTrue(quote["fetched_at"])

    def test_parse_rejects_error_field(self):
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote({"error": "No route found"}, side="buy",
                             symbol="ZEC", wrapped=True,
                             display_name="Zcash")

    def test_parse_rejects_empty_route(self):
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote({"inAmount": "1", "outAmount": "1",
                              "routePlan": []},
                             side="buy", symbol="ZEC", wrapped=True,
                             display_name="Zcash")

    def test_parse_rejects_zero_output(self):
        bad = dict(REAL_QUOTE, outAmount="0")
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(bad, side="buy", symbol="USDC",
                             wrapped=False, display_name="USD Coin")

    def test_parse_rejects_dead_hop_zero_out(self):
        # LIQUIDITY RULE (James, 2026-11-11): a hop that moves
        # zero is a dead pool — the whole quote is refused as no
        # liquid route, never shown as a tradeable route.
        dead = json.loads(json.dumps(REAL_QUOTE))
        dead["routePlan"][1]["swapInfo"]["outAmount"] = "0"
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(dead, side="buy", symbol="USDC",
                             wrapped=False, display_name="USD Coin")

    def test_parse_rejects_dead_hop_zero_in(self):
        dead = json.loads(json.dumps(REAL_QUOTE))
        dead["routePlan"][0]["swapInfo"]["inAmount"] = "0"
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(dead, side="buy", symbol="USDC",
                             wrapped=False, display_name="USD Coin")

    def test_quote_buy_uses_sol_input_and_wrapped_output(self):
        seen = {}

        def fake_get(url, headers=None):
            seen["url"] = url
            return dict(REAL_QUOTE,
                        outputMint=jup.WRAPPED_MINTS["ZEC"]["mint"],
                        outAmount="853837")

        with mock.patch.object(jup, "_http_get_json",
                               side_effect=fake_get):
            quote = jup.quote_buy("ZEC", 0.1)
        # 0.1 SOL = 100,000,000 lamports in the request.
        self.assertIn("amount=100000000", seen["url"])
        self.assertIn(jup.WRAPPED_MINTS["ZEC"]["mint"], seen["url"])
        self.assertTrue(quote["wrapped"])
        self.assertEqual(quote["display_name"], "Zcash")
        self.assertEqual(quote["in_amount_ui"], 0.1)
        # 853,837 units at 8 decimals = 0.00853837 wZEC.
        self.assertAlmostEqual(quote["out_amount_ui"], 0.00853837)

    def test_quote_sell_returns_sol_output(self):
        def fake_get(url, headers=None):
            return dict(REAL_QUOTE,
                        inputMint=jup.WRAPPED_MINTS["ZEC"]["mint"],
                        outputMint=jup.SOL_MINT,
                        inAmount="100000", outAmount="11713224")

        with mock.patch.object(jup, "_http_get_json",
                               side_effect=fake_get):
            quote = jup.quote_sell("ZEC", 0.001)
        self.assertEqual(quote["side"], "sell")
        self.assertAlmostEqual(quote["out_amount_ui"], 0.011713224)

    def test_quote_buy_refuses_zero_amount(self):
        with self.assertRaises(jup.JupiterError):
            jup.quote_buy("ZEC", 0)

    def test_quote_buy_unknown_symbol_is_no_route(self):
        with mock.patch.object(jup, "_search_token",
                               return_value=None):
            with self.assertRaises(jup.NoRouteError):
                jup.quote_buy("NOSUCHCOIN", 0.1)

    def test_quote_buy_sol_needs_no_route(self):
        with self.assertRaises(jup.JupiterError):
            jup.quote_buy("SOL", 0.1)

    def test_quote_http_error_becomes_jupiter_error(self):
        with mock.patch.object(
                jup, "_http_get_json",
                side_effect=jup.JupiterError("Jupiter answered HTTP 429")):
            with self.assertRaises(jup.JupiterError):
                jup.quote_buy("ZEC", 0.1)


class JupiterResolveTests(unittest.TestCase):
    def setUp(self):
        jup._clear_mint_cache()

    def tearDown(self):
        jup._clear_mint_cache()

    def test_wrapped_mints_are_the_verified_live_ones(self):
        # These four addresses were verified live against Jupiter's
        # token API on 2026-11-11 (all isVerified: true). They are
        # the ONLY mints ever hardcoded here.
        self.assertEqual(
            jup.resolve_mint("BTC")["mint"],
            "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh")
        self.assertEqual(
            jup.resolve_mint("ETH")["mint"],
            "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs")
        self.assertEqual(
            jup.resolve_mint("XRP")["mint"],
            "6UpQcMAb5xMzxc7ZfPaVMgx3KqsvKZdT5U718BzD5We2")
        self.assertEqual(
            jup.resolve_mint("ZEC")["mint"],
            "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS")
        for symbol in ("BTC", "ETH", "XRP", "ZEC"):
            self.assertTrue(jup.resolve_mint(symbol)["wrapped"])

    def test_sol_and_usdc_are_not_wrapped(self):
        sol = jup.resolve_mint("SOL")
        self.assertEqual(sol["mint"], jup.SOL_MINT)
        self.assertFalse(sol["wrapped"])
        usdc = jup.resolve_mint("USDC")
        self.assertEqual(
            usdc["mint"],
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        self.assertFalse(usdc["wrapped"])

    def test_dynamic_resolution_none_without_exact_symbol(self):
        # DOGE is not in the static map, so this goes through the
        # token search — mocked here to return only ZEC tokens, so
        # no exact DOGE match exists and the resolution is None
        # (never a wrong-symbol token).
        with mock.patch.object(jup, "_http_get_json",
                               return_value=TOKEN_SEARCH_ZEC):
            found = jup.resolve_mint("DOGE")
        self.assertIsNone(found)

    def test_dynamic_resolution_returns_verified_match(self):
        with mock.patch.object(jup, "_http_get_json",
                               return_value=TOKEN_SEARCH_ZEC):
            found = jup._search_token("ZEC")
        self.assertIsNotNone(found)
        self.assertEqual(
            found["mint"],
            "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS")
        self.assertEqual(found["decimals"], 8)

    def test_search_token_ignores_unverified_and_wrong_symbol(self):
        junk = [{"id": "11111111111111111111111111111111",
                 "name": "Fake", "symbol": "ZEC", "decimals": 6,
                 "isVerified": False, "liquidity": 1e9}]
        with mock.patch.object(jup, "_http_get_json",
                               return_value=junk):
            self.assertIsNone(jup._search_token("ZEC"))

    def test_search_token_rejects_same_symbol_memecoin(self):
        # A Solana token just called "ADA" is not Cardano: with the
        # coin's full name given, the token name must contain
        # "wrapped" or that full name.
        ada_meme = [{"id": "skDGuSDyKYGbYTWnMCNcPeDd4vHUxAsMNVVxxUfpump",
                     "name": "ADA", "symbol": "ADA", "decimals": 6,
                     "isVerified": True, "liquidity": 1e6}]
        with mock.patch.object(jup, "_http_get_json",
                               return_value=ada_meme):
            self.assertIsNone(jup._search_token("ADA",
                                                coin_name="Cardano"))
            # Without the coin name the same token resolves — the
            # name check only runs when the caller knows the coin.
            self.assertIsNotNone(jup._search_token("ADA"))

    def test_search_token_accepts_wrapped_name(self):
        wrapped = [{"id": "11111111111111111111111111111112",
                    "name": "Wrapped Litecoin", "symbol": "LTC",
                    "decimals": 8, "isVerified": True,
                    "liquidity": 1e6}]
        with mock.patch.object(jup, "_http_get_json",
                               return_value=wrapped):
            found = jup._search_token("LTC", coin_name="Litecoin")
        self.assertIsNotNone(found)
        self.assertEqual(found["mint"],
                         "11111111111111111111111111111112")

    def test_search_token_failure_is_none_not_raise(self):
        with mock.patch.object(
                jup, "_http_get_json",
                side_effect=jup.JupiterError("unreachable")):
            self.assertIsNone(jup._search_token("ZEC"))
            self.assertIsNone(jup.resolve_mint("DOGE"))

    def test_resolve_mint_cache_keeps_wrapped_flag(self):
        # Regression: the mint cache once stored the raw search
        # result (no "wrapped" key), so the SECOND resolution of
        # a dynamically-resolved coin crashed quote_buy with
        # KeyError: 'wrapped'. Both calls must return the same
        # finished dict.
        found = {"mint": "DoGEV7LASBkQbibMc5k5vKnTZoMg423GpJ5QtJEGfm7R",
                 "name": "Dogecoin", "decimals": 8}
        with mock.patch.object(jup, "_search_token",
                               return_value=found):
            first = jup.resolve_mint("DOGE", coin_name="Dogecoin")
            second = jup.resolve_mint("DOGE", coin_name="Dogecoin")
        self.assertEqual(first, second)
        self.assertIn("wrapped", first)
        self.assertFalse(first["wrapped"])
        self.assertEqual(first["mint"], found["mint"])


class JupiterLiquidityTests(unittest.TestCase):
    def test_no_liquid_route_message_maps_no_route_errors(self):
        self.assertEqual(
            jup.no_liquid_route_message(
                jup.NoRouteError("No route found")),
            jup.NO_LIQUID_ROUTE)
        self.assertEqual(
            jup.no_liquid_route_message(jup.JupiterError(
                "The token EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1"
                " is not tradable")),
            jup.NO_LIQUID_ROUTE)
        self.assertEqual(
            jup.no_liquid_route_message(
                jup.JupiterError("No route found")),
            jup.NO_LIQUID_ROUTE)

    def test_no_liquid_route_message_none_for_other_errors(self):
        # Unreachable, bad amounts and the like are reported as
        # themselves — never dressed up as a liquidity verdict.
        self.assertIsNone(jup.no_liquid_route_message(
            jup.JupiterError("Jupiter is unreachable right now.")))
        self.assertIsNone(jup.no_liquid_route_message(
            jup.JupiterError("Amount must be more than 0.")))
        self.assertEqual(jup.NO_LIQUID_ROUTE,
                         "No liquid route found for this coin.")


class JupiterSummaryTests(unittest.TestCase):
    def test_summarize_buy_route(self):
        quote = {
            "side": "buy", "symbol": "ZEC", "wrapped": True,
            "display_name": "Zcash", "in_amount_ui": 0.1,
            "out_amount_ui": 0.00853837,
            "route_labels": ["GoonFi V2", "ZeroFi"],
            "price_impact_pct": 0.0, "fetched_at": "2026-11-11T21:00:00Z",
        }
        lines = jup.summarize_route(quote)
        text = "\n".join(lines)
        self.assertIn("0.1 SOL", text)
        self.assertIn("GoonFi V2", text)
        self.assertIn("ZeroFi", text)
        self.assertIn("WRAPPED", text)
        self.assertIn("not the native coin", text)

    def test_summarize_sell_route(self):
        quote = {
            "side": "sell", "symbol": "ZEC", "wrapped": True,
            "display_name": "Zcash", "in_amount_ui": 0.001,
            "out_amount_ui": 0.011713224,
            "route_labels": ["Meteora DLMM"],
            "price_impact_pct": 0.0004,
            "fetched_at": "2026-11-11T21:00:00Z",
        }
        lines = jup.summarize_route(quote)
        self.assertIn("0.001 Zcash", "\n".join(lines))
        self.assertIn("SOL", "\n".join(lines))


class JupiterPriceTests(unittest.TestCase):
    def test_usd_price_from_quote(self):
        # 0.1 SOL at $84.55/SOL -> 0.00853837 wZEC implies
        # $8.455 / 0.00853837 = ~$990.24 per wZEC.
        def fake_buy(symbol, amount_sol, slippage_bps=100):
            return {"out_amount_ui": 0.00853837}

        with mock.patch.object(jup, "quote_buy",
                               side_effect=fake_buy):
            price = jup.jupiter_usd_price("ZEC", 84.55)
        self.assertAlmostEqual(price, 990.24, delta=1.0)

    def test_usd_price_none_when_quote_fails(self):
        with mock.patch.object(
                jup, "quote_buy",
                side_effect=jup.JupiterError("unreachable")):
            self.assertIsNone(jup.jupiter_usd_price("ZEC", 84.55))

    def test_usd_price_none_without_sol_price(self):
        self.assertIsNone(jup.jupiter_usd_price("ZEC", None))


class JupiterGateTests(unittest.TestCase):
    def test_real_swaps_refuse_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(jup.real_swaps_enabled())
            with self.assertRaises(jup.JupiterRealDisabled) as ctx:
                jup.request_real_swap()
            self.assertIn("not enabled yet", str(ctx.exception))

    def test_real_swaps_open_when_flag_set(self):
        # James's go-ahead (JUPITER_REAL_SWAPS=1) opens the gate:
        # the executor may then run the full real-swap path. The
        # gate alone executes nothing — it only permits it.
        with mock.patch.dict(os.environ, {"JUPITER_REAL_SWAPS": "1"}):
            self.assertTrue(jup.real_swaps_enabled())
            self.assertTrue(jup.request_real_swap())

    def test_fetch_swap_transaction_uses_mocked_response(self):
        def fake_post(url, payload, headers=None):
            self.assertIn("/swap/v1/swap", url)
            self.assertEqual(payload["userPublicKey"], "U" * 44)
            return {"swapTransaction": "BASE64TX",
                    "lastValidBlockHeight": 1}

        with mock.patch.object(jup, "_http_post_json",
                               side_effect=fake_post):
            tx = jup.fetch_swap_transaction(
                dict(REAL_QUOTE), "U" * 44)
        self.assertEqual(tx, "BASE64TX")

    def test_fetch_swap_transaction_refuses_empty(self):
        with mock.patch.object(jup, "_http_post_json",
                               return_value={}):
            with self.assertRaises(jup.JupiterError):
                jup.fetch_swap_transaction(dict(REAL_QUOTE), "U" * 44)


class JupiterLiveTests(unittest.TestCase):
    """Live checks against the real Jupiter API. They skip (never
    fail) when the network is unavailable, so offline runs stay
    green. They prove the exact code path above works against the
    live service — including that the four hardcoded wrapped mints
    are still the verified ones."""

    def _skip_on_network_error(self, fn):
        try:
            return fn()
        except (jup.JupiterError, OSError) as exc:
            self.skipTest(f"Jupiter unreachable: {exc}")

    def test_live_quote_sol_to_weth(self):
        def run():
            quote = jup.quote_buy("ETH", 0.1)
            self.assertGreater(quote["out_amount"], 0)
            self.assertTrue(quote["route_labels"])
            return quote
        self._skip_on_network_error(run)

    def test_live_quote_sol_to_wzec(self):
        def run():
            quote = jup.quote_buy("ZEC", 0.1)
            self.assertGreater(quote["out_amount"], 0)
            self.assertTrue(quote["route_labels"])
            # Sanity: 0.1 SOL (~$8.45) of wZEC at ~$990 is ~0.0085.
            self.assertAlmostEqual(quote["out_amount_ui"], 0.0085,
                                   delta=0.004)
            return quote
        self._skip_on_network_error(run)

    def test_live_wrapped_mints_still_verified(self):
        def run():
            # Re-verify the four hardcoded mints the way they were
            # first verified: search with the query that finds them
            # and check the mint is still there and still verified.
            # (The token search answers "WBTC" for a "BTC" query,
            # so this checks the mint directly, not the symbol
            # filter.)
            cases = (
                ("wBTC", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"),
                ("wETH", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"),
                ("XRP", "6UpQcMAb5xMzxc7ZfPaVMgx3KqsvKZdT5U718BzD5We2"),
                ("ZEC", "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"))
            for query, mint in cases:
                raw = jup._http_get_json(
                    jup.TOKEN_API_BASE + "/search?query=" + query)
                toks = raw if isinstance(raw, list) \
                    else raw.get("tokens", [])
                match = [t for t in toks if t.get("id") == mint]
                self.assertTrue(match, query)
                self.assertTrue(match[0].get("isVerified"), query)
        self._skip_on_network_error(run)


if __name__ == "__main__":
    unittest.main()
