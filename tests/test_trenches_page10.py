"""Page 10 Trenches badges + feed accuracy (2026-11-25).

James: "Page 10 trenches there is no price or numbers for each coins
data. Add the chart and 1hr % in a clean layout, all sitting nicely
inside coins badge button. Nothing gets cutoff, all fitting
professionally." + "Double check we have an accurate feed so we can
trade properly." + the frozen list ("the Trenches are NOT changing"):
the rows never updated live.

What this file pins:
- the badge renders the 1h chart + 1h % + a real unit price, the
  name in full (never truncated), inside the button;
- the 1h % comes from the same points the chart draws (so the two
  can never disagree), with the feed's tick history as fallback;
- a stalled stream falls back to the batched chain read — a row is
  never blank when the chain can price it — and a row the chain
  cannot price is kept (not yanked) by the /api/trenches poll.
"""

import pathlib
import tempfile
import time
import unittest
from unittest import mock

import tradingbot.home_holdings as hh
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.solana import generate_keypair

ROOT = pathlib.Path(__file__).resolve().parent.parent

MINT1 = generate_keypair()[1]
MINT2 = generate_keypair()[1]
MINT3 = generate_keypair()[1]


def _scan(candidates):
    return {"candidates": candidates, "prices": {},
            "source": "test", "as_of": "test", "error": None}


def _row(mint, symbol, name):
    return {"mint": mint, "symbol": symbol, "name": name,
            "price_usd": 0.0009, "change_1h_pct": 5.0}


class Page10Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", 10.0, "jupiter")
        self.sol_patch = mock.patch.object(
            routes, "_feed_sol_price_usd", return_value=10.0)
        self.sol_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(10.0, "live", "test"))
        self.price_patch.start()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan", return_value=_scan([]))
        self.scan_patch.start()
        self.chain_prices = {}
        self.chain_patch = mock.patch.object(
            routes, "_chain_prices_for_mints",
            side_effect=lambda app, mints: {
                m: self.chain_prices[m] for m in mints
                if m in self.chain_prices})
        self.chain_patch.start()
        self.spark_points = {}
        self.spark_patch = mock.patch.object(
            routes, "sparkline_points_1h",
            side_effect=lambda coin_id, is_mint=False, feed=None,
            http_get=None: list(self.spark_points.get(coin_id, [])))
        self.spark_patch.start()
        self.idents_patch = mock.patch.object(
            routes, "fetch_mint_identities", return_value={})
        self.idents_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.meta_patch.stop()
        self.idents_patch.stop()
        self.spark_patch.stop()
        self.chain_patch.stop()
        self.scan_patch.stop()
        self.price_patch.stop()
        self.sol_patch.stop()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        self.tmp.cleanup()

    def _set_scan(self, candidates):
        self.scan_patch.stop()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value=_scan(candidates))
        self.scan_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)


class TestFmtPriceSig(unittest.TestCase):
    def test_tiny_price_never_renders_zero(self):
        # The bug: "%.4f" showed a $0.00002 coin as $0.0000 — no
        # price at all. Significant figures keep the number.
        self.assertEqual(hh.fmt_price_sig(0.00002), "$0.0000200")
        self.assertEqual(hh.fmt_price_sig(0.00123), "$0.00123")

    def test_normal_and_large_prices(self):
        self.assertEqual(hh.fmt_price_sig(1.234), "$1.23")
        self.assertEqual(hh.fmt_price_sig(123.456), "$123.46")

    def test_missing_price_is_no_data(self):
        self.assertEqual(hh.fmt_price_sig(0), "no data")
        self.assertEqual(hh.fmt_price_sig(None), "no data")
        self.assertEqual(hh.fmt_price_sig(-5), "no data")


class TestSparklineChange(unittest.TestCase):
    def test_change_from_points(self):
        self.assertAlmostEqual(
            hh.change_pct_from_points([100.0, 110.0]), 10.0)
        self.assertAlmostEqual(
            hh.change_pct_from_points([100.0, 90.0]), -10.0)

    def test_change_needs_two_points(self):
        self.assertIsNone(hh.change_pct_from_points([100.0]))
        self.assertIsNone(hh.change_pct_from_points([]))
        self.assertIsNone(hh.change_pct_from_points([0.0, 100.0]))

    def test_1h_change_uses_the_charts_points(self):
        with mock.patch.object(
                hh, "sparkline_points_1h",
                return_value=[50.0, 62.5]):
            self.assertAlmostEqual(
                hh.sparkline_1h_change_pct(MINT1, is_mint=True), 25.0)
        with mock.patch.object(
                hh, "sparkline_points_1h", return_value=[50.0]):
            self.assertIsNone(
                hh.sparkline_1h_change_pct(MINT1, is_mint=True))


class TestRepriceTrenches(Page10Base):
    def test_stale_stream_falls_back_to_chain_read(self):
        # The feed has a tick for MINT1 only (the stream is stale
        # for MINT2): the chain read prices MINT2 — the row is
        # never blank when the chain can price it.
        self.feed.ingest(MINT1, 0.00123, "helius-stream")
        self.chain_prices = {MINT2: 0.00002}
        self.spark_points = {MINT1: [100.0, 112.5],
                             MINT2: [200.0, 190.0]}
        rows = [_row(MINT1, "MOON", "Moon Coin"),
                _row(MINT2, "STAR", "Star Coin")]
        out = routes._reprice_trenches_rows(self.app, rows)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0]["price_usd"], 0.00123)
        self.assertEqual(out[0]["price_display"], "$0.00123")
        self.assertAlmostEqual(out[0]["change_1h_pct"], 12.5)
        self.assertIn("<svg", out[0]["sparkline_svg"])
        self.assertAlmostEqual(out[1]["price_usd"], 0.00002)
        self.assertEqual(out[1]["price_display"], "$0.0000200")
        self.assertAlmostEqual(out[1]["change_1h_pct"], -5.0)
        self.assertIn("<svg", out[1]["sparkline_svg"])
        # The cached scan rows are never mutated.
        self.assertEqual(rows[0]["price_usd"], 0.0009)

    def test_unpriced_row_dropped_for_page_kept_for_poll(self):
        self.spark_points = {MINT3: [100.0, 110.0]}
        rows = [_row(MINT3, "VOID", "Void Coin")]
        self.assertEqual(
            routes._reprice_trenches_rows(self.app, rows), [])
        kept = routes._reprice_trenches_rows(
            self.app, rows, drop_unpriced=False)
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0]["price_usd"])
        self.assertEqual(kept[0]["price_display"], "no data")
        self.assertAlmostEqual(kept[0]["change_1h_pct"], 10.0)
        self.assertIn("<svg", kept[0]["sparkline_svg"])


class TestTrenchesApi(Page10Base):
    def _setup_rows(self):
        self._set_scan([_row(MINT1, "MOON", "Moon Coin"),
                        _row(MINT2, "STAR", "Star Coin")])
        self.feed.ingest(MINT1, 0.00123, "helius-stream")
        self.chain_prices = {MINT2: 0.00002}
        self.spark_points = {MINT1: [100.0, 112.5],
                             MINT2: [200.0, 190.0]}

    def test_api_returns_live_rows_with_html(self):
        self._setup_rows()
        resp = self.client.get("/api/trenches")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["rows"]), 2)
        row = data["rows"][0]
        self.assertEqual(row["mint"], MINT1)
        self.assertAlmostEqual(row["price_usd"], 0.00123)
        self.assertEqual(row["price_display"], "$0.00123")
        self.assertAlmostEqual(row["change_1h"], 12.5)
        self.assertIn("<svg", row["sparkline_svg"])
        self.assertIn("Moon Coin", row["html"])
        self.assertIn("$0.00123", row["html"])
        self.assertIn("+12.5% 1h", row["html"])
        self.assertIn("data-trenches-mint", row["html"])

    def test_api_keeps_unpriced_row_as_no_data(self):
        self._set_scan([_row(MINT1, "MOON", "Moon Coin"),
                        _row(MINT3, "VOID", "Void Coin")])
        self.feed.ingest(MINT1, 0.00123, "helius-stream")
        self.spark_points = {MINT1: [100.0, 112.5],
                             MINT3: [100.0, 110.0]}
        data = self.client.get("/api/trenches").get_json()
        self.assertEqual(len(data["rows"]), 2)
        void = data["rows"][1]
        self.assertIsNone(void["price_usd"])
        self.assertEqual(void["price_display"], "no data")
        self.assertIn("no data", void["html"])

    def test_sparklines_endpoint_returns_1h_pct(self):
        self.spark_points = {MINT1: [100.0, 112.5]}
        data = self.client.get(
            "/api/sparklines?coins=" + MINT1).get_json()
        self.assertTrue(data["ok"])
        self.assertIn("<svg", data["sparklines"][MINT1])
        self.assertAlmostEqual(data["pct_1h"][MINT1], 12.5)


class TestTrenchesBadge(Page10Base):
    def test_badge_has_chart_pct_and_price(self):
        self._set_scan([_row(MINT1, "MOON", "Moon Coin")])
        self.feed.ingest(MINT1, 0.00123, "helius-stream")
        self.spark_points = {MINT1: [100.0, 112.5]}
        html = self._html("/trenches")
        self.assertIn("Moon Coin", html)
        self.assertIn("MOON", html)
        self.assertIn("$0.00123", html)
        self.assertIn("+12.5% 1h", html)
        self.assertIn("<svg", html)
        self.assertIn('data-sparkline-coin="%s"' % MINT1, html)
        self.assertIn('data-sparkline-pct-1h="%s"' % MINT1, html)
        self.assertIn('data-trenches-mint="%s"' % MINT1, html)
        # The list refresh hook: the page re-fetches its rows so
        # the list moves live instead of freezing at load.
        self.assertIn("data-trenches-list", html)

    def test_badge_name_is_never_truncated(self):
        css = (ROOT / "tradingbot" / "static" / "style.css").read_text()
        self.assertIn(".trenches-list .coin-name", css)
        self.assertIn("white-space: normal", css)

    def test_live_js_refreshes_the_list_and_the_pct(self):
        js = (ROOT / "tradingbot" / "static" / "live.js").read_text()
        self.assertIn("/api/trenches", js)
        self.assertIn("pct_1h", js)
        # The first snapshot must not blank the fresh
        # server-rendered prices before the stream has had a
        # chance to deliver its first tick.
        self.assertIn("pageLoadedAt", js)


if __name__ == "__main__":
    unittest.main()
