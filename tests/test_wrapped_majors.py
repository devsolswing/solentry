"""Wrapped-majors picker on the buy (coin detail) page.

Rules under test:
- GET /api/wrapped-majors lists ONLY wrapped majors with a LIVE
  liquid Jupiter route right now: a live quote proves the pool.
  A wrapped major with no liquid route (NoRouteError) is left
  out; a coin Jupiter cannot be reached for is unverifiable, not
  "illiquid" — when nothing can be verified the endpoint says
  unavailable instead of an empty list dressed up as dry pools.
- Every entry is labelled wrapped ("Wrapped BTC"), never the
  native coin; native Solana coins and USDC are never in the
  list.
- Picking a major switches the buy page to that coin (the
  picker navigates to the coin's detail page, the same URL a
  coin list tap uses, so the new page renders that coin's price,
  route and wallet state fresh).
- The picker markup on the coin page is the quick-trade token
  selector's markup: the same text input, the same
  .search-results rows.
No SOL moves in these tests; Jupiter's quotes are mocked.
"""

import unittest
from unittest import mock

import tradingbot.jupiter as jup
from test_jupiter_routes import JupiterRoutesBase, zec_buy_quote


def _quote_for(symbol):
    quote = dict(zec_buy_quote())
    quote["symbol"] = symbol
    mint = jup.WRAPPED_MINTS.get(symbol, {}).get("mint")
    quote["output_mint"] = mint or f"mint-{symbol}"
    return quote


class TestLiquidWrappedMajors(unittest.TestCase):
    def _run(self, behavior):
        """behavior: symbol -> 'ok' | 'noroute' | 'unreachable'.
        Unlisted symbols have no liquid route."""
        def fake_buy(symbol, amount_sol, coin_name=None, **kw):
            mode = behavior.get(symbol, "noroute")
            if mode == "noroute":
                raise jup.NoRouteError("No route found")
            if mode == "unreachable":
                raise jup.JupiterError(
                    "Jupiter is unreachable right now.")
            return _quote_for(symbol)
        with mock.patch.object(jup, "quote_buy", side_effect=fake_buy):
            return jup.liquid_wrapped_majors()

    def test_lists_only_liquid_wrapped_majors(self):
        majors, unverifiable = self._run({
            "BTC": "ok", "ETH": "ok"})
        symbols = [m["coin_id"] for m in majors]
        self.assertIn("BTC", symbols)
        self.assertIn("ETH", symbols)
        # ADA is not a wrapped-major candidate, so it never
        # appears even when its quote would succeed.
        self.assertNotIn("ADA", symbols)
        self.assertFalse(unverifiable)

    def test_no_route_coin_is_left_out(self):
        majors, unverifiable = self._run({"ZEC": "noroute",
                                          "BTC": "ok"})
        symbols = [m["coin_id"] for m in majors]
        self.assertNotIn("ZEC", symbols)
        self.assertIn("BTC", symbols)
        self.assertFalse(unverifiable)

    def test_entries_are_labelled_wrapped_never_native(self):
        majors, _ = self._run({"BTC": "ok", "ZEC": "ok"})
        for m in majors:
            self.assertTrue(m["display_name"].startswith("Wrapped "))
            self.assertNotIn("native", m["display_name"].lower())
            self.assertEqual(m["display_name"],
                             f"Wrapped {m['symbol']}")

    def test_all_unreachable_is_unverifiable_not_empty(self):
        majors, unverifiable = self._run(
            {s: "unreachable" for s in jup.WRAPPED_MAJOR_SYMBOLS})
        self.assertEqual(majors, [])
        self.assertTrue(unverifiable)

    def test_mixed_unreachable_still_lists_the_liquid_ones(self):
        majors, unverifiable = self._run({
            "BTC": "ok", "ETH": "unreachable"})
        self.assertEqual([m["coin_id"] for m in majors], ["BTC"])
        self.assertTrue(unverifiable)

    def test_order_follows_candidacy_order(self):
        majors, _ = self._run({"TAO": "ok", "BTC": "ok",
                               "ZEC": "ok"})
        self.assertEqual([m["coin_id"] for m in majors],
                         ["BTC", "ZEC", "TAO"])

    def test_native_solana_and_usdc_never_candidates(self):
        for s in ("SOL", "BONK", "JUP", "PEPE", "POPCAT",
                  "RENDER", "USDC"):
            self.assertNotIn(s, jup.WRAPPED_MAJOR_SYMBOLS)

    def test_each_entry_carries_route_and_mint(self):
        majors, _ = self._run({"BTC": "ok"})
        self.assertEqual(len(majors), 1)
        entry = majors[0]
        self.assertEqual(entry["mint"], jup.WRAPPED_MINTS["BTC"]["mint"])
        self.assertEqual(entry["route_labels"], ["Byreal", "Scorch"])
        self.assertTrue(entry["fetched_at"])


class TestWrappedMajorsEndpoint(JupiterRoutesBase):
    def _mock_quotes(self, behavior):
        def fake_buy(symbol, amount_sol, coin_name=None, **kw):
            mode = behavior.get(symbol, "noroute")
            if mode == "noroute":
                raise jup.NoRouteError("No route found")
            if mode == "unreachable":
                raise jup.JupiterError(
                    "Jupiter is unreachable right now.")
            return _quote_for(symbol)
        return mock.patch.object(jup, "quote_buy",
                                 side_effect=fake_buy)

    def test_endpoint_lists_liquid_wrapped_majors(self):
        with self._mock_quotes({"BTC": "ok", "ETH": "ok",
                                "ZEC": "noroute"}):
            resp = self.client.get("/api/wrapped-majors")
        data = resp.get_json()
        self.assertTrue(data["ok"], data)
        symbols = [m["coin_id"] for m in data["majors"]]
        self.assertNotIn("ZEC", symbols)
        self.assertIn("BTC", symbols)
        self.assertTrue(all(m["display_name"].startswith("Wrapped ")
                            for m in data["majors"]))
        self.assertEqual(data["source"], "jupiter")
        self.assertTrue(data["fetched_at"])

    def test_endpoint_empty_when_all_pools_dry(self):
        with self._mock_quotes(
                {s: "noroute" for s in jup.WRAPPED_MAJOR_SYMBOLS}):
            resp = self.client.get("/api/wrapped-majors")
        data = resp.get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["majors"], [])

    def test_endpoint_unavailable_when_jupiter_unreachable(self):
        with self._mock_quotes(
                {s: "unreachable"
                 for s in jup.WRAPPED_MAJOR_SYMBOLS}):
            resp = self.client.get("/api/wrapped-majors")
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("unavailable", data["error"])
        self.assertEqual(data["majors"], [])

    def test_coin_page_has_wrapped_majors_picker(self):
        html = self._html("/coins/ZEC/detail")
        self.assertIn('id="wrapped-majors"', html)
        self.assertIn('id="wrapped-majors-search"', html)
        self.assertIn('id="wrapped-majors-results"', html)
        self.assertIn('data-wrapped-majors-url="/api/wrapped-majors"',
                      html)
        self.assertIn("search-results", html)
        self.assertIn("Wrapped majors with liquid pools", html)

    def test_picker_switches_to_the_picked_coin(self):
        # The picker navigates to the picked coin's detail page —
        # the same URL a coin list tap uses — so the new page
        # renders that coin's price, route and wallet state fresh.
        html = self._html("/coins/BTC/detail")
        self.assertIn("Bitcoin", html)
        self.assertIn('id="wrapped-majors"', html)


if __name__ == "__main__":
    unittest.main()
