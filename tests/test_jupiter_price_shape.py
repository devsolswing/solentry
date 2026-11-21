"""Jupiter price v3 response shape — regression test.

Live 2026-11-16 the home feed read "Prices: unavailable" while
Jupiter answered fine (SOL ~$71.50). Both parsers — prices.py
(the price page) and livefeed.py (the live feed) — expected the
price wrapped in a "data" box that Jupiter price v3 does not send:
the real response is a flat {mint: {...}} map. The old tests
mocked the wrapped shape, so they passed while live failed.
These tests mock the REAL flat shape, plus the wrapped shape
for back-compat.
"""

import json
import unittest
from unittest import mock

import tradingbot.prices as prices


class FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._data).encode("utf-8")


class TestJupiterPriceShape(unittest.TestCase):
    def setUp(self):
        prices.clear_price_cache()
        self.addCleanup(prices.clear_price_cache)

    def _fetch(self, payload):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(payload)):
            return prices._fetch_sol_price()

    def test_flat_shape_is_the_live_shape(self):
        # Exactly what lite-api.jup.ag/price/v3 returned live
        # 2026-11-16 ~18:55 PST (SOL $71.745..., +0.61% 24h).
        payload = {prices.SOL_MINT: {
            "createdAt": "2024-06-05T08:55:25.527Z",
            "liquidity": 780447860.6299059,
            "usdPrice": 71.7452681748909,
            "blockId": 470356898,
            "decimals": 9,
            "priceChange24h": 0.6141603102061176}}
        price, change = self._fetch(payload)
        self.assertAlmostEqual(price, 71.7452681748909)
        self.assertAlmostEqual(change, 0.6141603102061176)

    def test_flat_shape_makes_prices_live(self):
        payload = {prices.SOL_MINT: {
            "usdPrice": 71.50, "priceChange24h": 0.5}}
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(payload)):
            data = prices.get_prices()
        self.assertEqual(data["source"], "live")
        self.assertEqual(data["prices"]["SOL"]["price"], 71.50)

    def test_data_wrapped_shape_still_parses(self):
        payload = {"data": {prices.SOL_MINT: {
            "usdPrice": 71.50, "priceChange24h": 0.5}}}
        price, change = self._fetch(payload)
        self.assertEqual(price, 71.50)
        self.assertEqual(change, 0.5)

    def test_empty_payload_is_unavailable(self):
        for payload in ({}, {prices.SOL_MINT: {}},
                        {prices.SOL_MINT: {"usdPrice": -1}},
                        {"data": {}}):
            price, change = self._fetch(payload)
            self.assertIsNone(price)
            self.assertIsNone(change)


if __name__ == "__main__":
    unittest.main()
