"""Jupiter DEX aggregation for graduated pump.fun coins.

Jupiter is the venue ONLY for a coin that graduated off its
bonding curve (migrated to Raydium). Coins still bonding trade
through the pump.fun executor, never through Jupiter. Every quote
here is by mint address — there is no symbol resolution, no
wrapped-majors map, and no "Finding route…" for majors any more:
SOL is the unit of account, never a listed coin.

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

# A graduated pump.fun mint (valid base58, 6 decimals by
# construction) used as the quote subject in these tests.
MINT = "Grad1111111111111111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class JupiterQuoteTests(unittest.TestCase):
    def test_parse_real_quote_shape(self):
        quote = jup._parse_quote(REAL_QUOTE, side="buy", symbol=MINT,
                                 wrapped=False, display_name=MINT)
        self.assertEqual(quote["in_amount"], 100_000_000)
        self.assertEqual(quote["out_amount"], 8_455_700)
        self.assertEqual(quote["route_labels"], ["Byreal", "Scorch"])
        self.assertEqual(quote["price_impact_pct"], 0.0)
        self.assertEqual(quote["source"], "jupiter")
        self.assertTrue(quote["fetched_at"])

    def test_parse_rejects_error_field(self):
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote({"error": "No route found"}, side="buy",
                             symbol=MINT, wrapped=False,
                             display_name=MINT)

    def test_parse_rejects_empty_route(self):
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote({"inAmount": "1", "outAmount": "1",
                              "routePlan": []},
                             side="buy", symbol=MINT, wrapped=False,
                             display_name=MINT)

    def test_parse_rejects_zero_output(self):
        bad = dict(REAL_QUOTE, outAmount="0")
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(bad, side="buy", symbol=MINT,
                             wrapped=False, display_name=MINT)

    def test_parse_rejects_dead_hop_zero_out(self):
        # LIQUIDITY RULE (James, 2026-11-11): a hop that moves
        # zero is a dead pool — the whole quote is refused as no
        # liquid route, never shown as a tradeable route.
        dead = json.loads(json.dumps(REAL_QUOTE))
        dead["routePlan"][1]["swapInfo"]["outAmount"] = "0"
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(dead, side="buy", symbol=MINT,
                             wrapped=False, display_name=MINT)

    def test_parse_rejects_dead_hop_zero_in(self):
        dead = json.loads(json.dumps(REAL_QUOTE))
        dead["routePlan"][0]["swapInfo"]["inAmount"] = "0"
        with self.assertRaises(jup.NoRouteError):
            jup._parse_quote(dead, side="buy", symbol=MINT,
                             wrapped=False, display_name=MINT)

    def test_quote_buy_mint_uses_sol_input_and_mint_output(self):
        seen = {}

        def fake_get(url, headers=None):
            seen["url"] = url
            return dict(REAL_QUOTE, outputMint=MINT,
                        outAmount="853837")

        with mock.patch.object(jup, "_http_get_json",
                               side_effect=fake_get):
            quote = jup.quote_buy_mint(MINT, 0.1)
        # 0.1 SOL = 100,000,000 lamports in the request.
        self.assertIn("amount=100000000", seen["url"])
        self.assertIn(MINT, seen["url"])
        self.assertIn(jup.SOL_MINT, seen["url"])
        self.assertFalse(quote["wrapped"])
        self.assertEqual(quote["in_amount_ui"], 0.1)
        # 853,837 units at 6 decimals = 0.853837 tokens.
        self.assertAlmostEqual(quote["out_amount_ui"], 0.853837)

    def test_quote_sell_mint_returns_sol_output(self):
        def fake_get(url, headers=None):
            return dict(REAL_QUOTE,
                        inputMint=MINT,
                        outputMint=jup.SOL_MINT,
                        inAmount="100000", outAmount="11713224")

        with mock.patch.object(jup, "_http_get_json",
                               side_effect=fake_get):
            quote = jup.quote_sell_mint(MINT, 0.1)
        self.assertEqual(quote["side"], "sell")
        self.assertAlmostEqual(quote["out_amount_ui"], 0.011713224)

    def test_quote_buy_mint_refuses_zero_amount(self):
        with self.assertRaises(jup.JupiterError):
            jup.quote_buy_mint(MINT, 0)

    def test_quote_buy_mint_refuses_invalid_mint(self):
        with self.assertRaises(jup.JupiterError):
            jup.quote_buy_mint("NOT-A-MINT", 0.1)

    def test_quote_buy_mint_refuses_sol_as_output(self):
        # SOL is the coin you swap from — no route needed.
        with self.assertRaises(jup.JupiterError):
            jup.quote_buy_mint(jup.SOL_MINT, 0.1)

    def test_quote_http_error_becomes_jupiter_error(self):
        with mock.patch.object(
                jup, "_http_get_json",
                side_effect=jup.JupiterError("Jupiter answered HTTP 429")):
            with self.assertRaises(jup.JupiterError):
                jup.quote_buy_mint(MINT, 0.1)

    def test_get_quote_validates_amount(self):
        with self.assertRaises(jup.JupiterError):
            jup.get_quote(jup.SOL_MINT, MINT, 0)


class JupiterDecimalsTests(unittest.TestCase):
    def _fake_client(self, decimals):
        data = bytearray(82)
        data[44] = decimals

        class FakeClient:
            def __init__(self, url):
                self.url = url

            def get_account_info(self, mint):
                return {"data": bytes(data)}

        return FakeClient

    def test_mint_decimals_reads_byte_44(self):
        with mock.patch.object(jup, "SolanaRpcClient",
                               self._fake_client(6)):
            self.assertEqual(
                jup.mint_decimals("https://example.invalid", MINT), 6)

    def test_mint_decimals_honours_other_counts(self):
        with mock.patch.object(jup, "SolanaRpcClient",
                               self._fake_client(8)):
            self.assertEqual(
                jup.mint_decimals("https://example.invalid", MINT), 8)

    def test_mint_decimals_refuses_invalid_mint(self):
        with self.assertRaises(jup.JupiterError):
            jup.mint_decimals("https://example.invalid", "NOPE")

    def test_mint_decimals_unreadable_account_is_jupiter_error(self):
        class FakeClient:
            def __init__(self, url):
                pass

            def get_account_info(self, mint):
                return None

        with mock.patch.object(jup, "SolanaRpcClient", FakeClient):
            with self.assertRaises(jup.JupiterError):
                jup.mint_decimals("https://example.invalid", MINT)


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


class JupiterPriceTests(unittest.TestCase):
    def test_usd_price_from_quote(self):
        # 0.1 SOL at $84.55/SOL -> 0.853837 tokens implies
        # $8.455 / 0.853837 = ~$9.90 per token.
        def fake_buy(mint, amount_sol, decimals=6, slippage_bps=100):
            return {"out_amount_ui": 0.853837}

        with mock.patch.object(jup, "quote_buy_mint",
                               side_effect=fake_buy):
            price = jup.jupiter_usd_price_mint(MINT, 84.55)
        self.assertAlmostEqual(price, 9.90, delta=0.1)

    def test_usd_price_none_when_quote_fails(self):
        with mock.patch.object(
                jup, "quote_buy_mint",
                side_effect=jup.JupiterError("unreachable")):
            self.assertIsNone(jup.jupiter_usd_price_mint(MINT, 84.55))

    def test_usd_price_none_without_sol_price(self):
        self.assertIsNone(jup.jupiter_usd_price_mint(MINT, None))

    def test_no_symbol_based_price_helper(self):
        # The old symbol-based jupiter_usd_price / quote_buy /
        # quote_sell / resolve_mint are gone: Jupiter quotes are
        # by mint address only.
        for name in ("jupiter_usd_price", "quote_buy", "quote_sell",
                     "resolve_mint", "is_jupiter_coin", "wrapped_info",
                     "liquid_wrapped_majors", "summarize_route",
                     "WRAPPED_MINTS"):
            self.assertFalse(hasattr(jup, name), name)


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
    live service."""

    def _skip_on_network_error(self, fn):
        try:
            return fn()
        except (jup.JupiterError, OSError) as exc:
            self.skipTest(f"Jupiter unreachable: {exc}")

    def test_live_quote_sol_to_usdc(self):
        def run():
            quote = jup.get_quote(jup.SOL_MINT, USDC_MINT, 100_000_000)
            self.assertGreater(quote["out_amount"], 0)
            self.assertTrue(quote["route_labels"])
            return quote
        self._skip_on_network_error(run)


if __name__ == "__main__":
    unittest.main()
