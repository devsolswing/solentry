"""Price fix, whole app (2026-11-25).

James: "Price fix first" / "Across whole app" — the Page 10
Trenches fix (defects D1–D4) extended to every price surface:
home holdings rows, wallet page, coins page, coin detail page,
dashboard popup, bots page, buy/sell screens, P&L / Logs pages.

What this file pins:
- D2 home/coins rows: the 1h % comes from the same points the
  chart draws (enrich_row), so a fresh coin stops showing "—"
  forever; the feed-derived value stands when the points yield
  nothing.
- D2 coins rows: the old 24h % (hardcoded None in _coin_rows)
  is gone — rows show the 1h % instead.
- D2 coin detail + dashboard popup: a real 24h % derived from
  the same 24h chart points the high/low come from.
- D3 coins page: the holdings list has its own 5s poll
  (/api/coins-rows) and the trenches section reuses the shared
  badge partial + the existing trenches poll hook.
- D4 unit prices: significant figures everywhere a unit price
  renders (coins rows, bot trade ledger, Logs analysis pairs) —
  a $0.00002 coin never reads $0.0000.
- D1: the holdings poll clears a stale 1h % to "—" when the
  server stops having one, instead of leaving it standing.
"""

import hashlib
import pathlib
import tempfile
import unittest
from unittest import mock

import tradingbot.home_holdings as hh
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.solana import generate_keypair

ROOT = pathlib.Path(__file__).resolve().parent.parent

MINT1 = generate_keypair()[1]
MINT2 = generate_keypair()[1]
MINT3 = generate_keypair()[1]
MINT4 = generate_keypair()[1]


def _scan(candidates):
    return {"candidates": candidates, "prices": {},
            "source": "test", "as_of": "test", "error": None}


def _trow(mint, symbol, name):
    return {"mint": mint, "symbol": symbol, "name": name,
            "price_usd": 0.0009, "change_1h_pct": 5.0}


class AppwideBase(unittest.TestCase):
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
        # enrich_row calls the same helper from the home_holdings
        # namespace — patch it there too, or the row enrichment
        # would hit the real GeckoTerminal fetch.
        self.hh_spark_patch = mock.patch.object(
            hh, "sparkline_points_1h",
            side_effect=lambda coin_id, is_mint=False, feed=None,
            http_get=None: list(self.spark_points.get(coin_id, [])))
        self.hh_spark_patch.start()
        # No token-image fetch from the row enrichment either.
        self.image_patch = mock.patch.object(
            hh, "resolve_token_image", return_value=None)
        self.image_patch.start()
        self.idents_patch = mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints: {
                m: {"name": "Test Coin", "symbol": "TEST"}
                for m in (mints or [])
                if m in (MINT1, MINT2, MINT3, MINT4)})
        self.idents_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        # The detail page's buyers/sellers read is DexScreener —
        # keep the render local.
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow", return_value=None)
        self.flow_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.flow_patch.stop()
        self.meta_patch.stop()
        self.idents_patch.stop()
        self.image_patch.stop()
        self.hh_spark_patch.stop()
        self.spark_patch.stop()
        self.chain_patch.stop()
        self.scan_patch.stop()
        self.price_patch.stop()
        self.sol_patch.stop()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _hold(self, mint, amount, avg_price):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, amount, avg_price))
        self.db.commit()

    def _set_scan(self, candidates):
        self.scan_patch.stop()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value=_scan(candidates))
        self.scan_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})

    def _insert_bot(self, name="Luna"):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, "
            "max_open_trades, api_token_hash) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?)",
            (self.user_id, name,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid

    def _paper_trade(self, coin, side, amount, price, when,
                     exit_reason=None):
        # Same columns as test_analysis.AnalysisBase._trade.
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, "
            "amount, price, total_usd, created_at, bot_id, "
            "buy_m5_pct, buy_volume_5m_usd, buy_buys_5m, "
            "buy_sells_5m, buy_regime, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, "
            "NULL, NULL, ?)",
            (self.user_id, coin, side, amount, price,
             amount * price, when, exit_reason))
        self.db.commit()


class TestSigPriceFilter(unittest.TestCase):
    def test_filter_is_registered_and_uses_sig_figs(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            app, _client = make_client(tmp.name)
            sig = app.jinja_env.filters["sig_price"]
            self.assertEqual(sig(0.00002), "$0.0000200")
            self.assertEqual(sig(0.00123), "$0.00123")
            self.assertEqual(sig(None), "no data")
        finally:
            tmp.cleanup()


class TestEnrichRowChange1h(unittest.TestCase):
    def test_change_1h_comes_from_the_chart_points(self):
        # The feed has no 1h figure for this coin (change_1h None
        # in the row) — the points the sparkline draws fill it.
        with mock.patch.object(
                hh, "sparkline_points_1h",
                return_value=[100.0, 110.0]), \
                mock.patch.object(
                    hh, "resolve_token_image", return_value=None):
            row = {"coin_id": MINT1, "is_mint": True,
                   "change_1h": None}
            hh.enrich_row(row)
            self.assertAlmostEqual(row["change_1h"], 10.0)

    def test_feed_value_stands_when_points_yield_nothing(self):
        # One point cannot make a % — the feed-derived value the
        # row already carries is kept, never overwritten with None.
        with mock.patch.object(
                hh, "sparkline_points_1h", return_value=[100.0]), \
                mock.patch.object(
                    hh, "resolve_token_image", return_value=None):
            row = {"coin_id": MINT1, "is_mint": True,
                   "change_1h": 3.5}
            hh.enrich_row(row)
            self.assertAlmostEqual(row["change_1h"], 3.5)


class TestCoinsPageRows(AppwideBase):
    def test_row_price_is_sig_figs_and_1h_moves(self):
        # The D4 bug: "%.4f" showed a $0.00002 coin as $0.0000.
        # The D2 bug: the 24h % was hardcoded None — "—" forever.
        self._hold(MINT1, 1000, 0.00001)
        self.feed.ingest(MINT1, 0.00002, "helius-stream")
        self.spark_points = {MINT1: [100.0, 105.0]}
        html = self._html("/dashboard")
        self.assertIn("$0.0000200", html)
        self.assertNotIn("$0.0000<", html)
        self.assertIn("+5.0% 1h", html)
        self.assertIn('data-live-change-1h="%s"' % MINT1, html)
        self.assertNotIn("data-live-change-24h", html)
        self.assertIn("data-coins-list", html)
        self.assertIn("Test Coin", html)

    def test_row_pnl_from_live_price(self):
        self._hold(MINT1, 1000, 0.00001)
        self.feed.ingest(MINT1, 0.00002, "helius-stream")
        self.spark_points = {MINT1: [100.0, 105.0]}
        html = self._html("/dashboard")
        # (0.00002 - 0.00001) x 1000 = +$0.01.
        self.assertIn("+$0.01", html)

    def test_coins_rows_api(self):
        self._hold(MINT1, 1000, 0.00001)
        self.feed.ingest(MINT1, 0.00002, "helius-stream")
        self.spark_points = {MINT1: [100.0, 105.0]}
        resp = self.client.get("/api/coins-rows")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["rows"]), 1)
        row = data["rows"][0]
        self.assertEqual(row["coin_id"], MINT1)
        self.assertEqual(row["price_display"], "$0.0000200")
        self.assertAlmostEqual(row["change_1h"], 5.0)
        self.assertEqual(row["pnl_display"], "+$0.01")
        self.assertIn("$0.0000200", row["html"])
        self.assertIn("data-live-change-1h", row["html"])
        # The poll builds the empty-list message from this label
        # when the list empties (the page never renders the message
        # beside real rows, even hidden).
        self.assertIn("mode_label", data)
        self.assertTrue(data["mode_label"])

    def test_coins_rows_api_empty_when_no_holdings(self):
        resp = self.client.get("/api/coins-rows")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["rows"], [])

    def test_trenches_section_uses_shared_badge(self):
        # D3: the coins-page trenches section was a frozen server
        # render. It now renders the same badge partial as Page 10
        # and carries the poll hook, so the existing trenches poll
        # covers it.
        self._set_scan([_trow(MINT2, "MOON", "Moon Coin")])
        self.feed.ingest(MINT2, 0.00123, "helius-stream")
        self.spark_points = {MINT2: [100.0, 112.5]}
        html = self._html("/dashboard")
        self.assertIn("data-trenches-list", html)
        self.assertIn('data-trenches-mint="%s"' % MINT2, html)
        self.assertIn("$0.00123", html)
        self.assertIn("+12.5% 1h", html)
        self.assertIn('data-sparkline-pct-1h="%s"' % MINT2, html)


class TestCoinDetail24h(AppwideBase):
    def test_detail_24h_from_chart_points(self):
        # Two feed ticks: 1.00 -> 1.01. The 24h % comes from those
        # same points the high/low come from: +1.0%.
        self.feed.ingest(MINT3, 1.00, "helius-stream")
        self.feed.ingest(MINT3, 1.01, "helius-stream")
        html = self._html("/coins/%s/detail" % MINT3)
        self.assertIn('class="up">+1.0%</span>', html)
        self.assertIn('data-live-change-24h="%s"' % MINT3, html)

    def test_detail_24h_is_dash_without_history(self):
        # One tick cannot make a % — the page says so instead of
        # inventing one.
        self.feed.ingest(MINT4, 1.00, "helius-stream")
        html = self._html("/coins/%s/detail" % MINT4)
        self.assertIn(
            'data-live-change-24h="%s"><span class="muted">—'
            "</span>" % MINT4, html)


class TestCoinPopup24h(AppwideBase):
    def test_popup_24h_from_chart_points(self):
        self.feed.ingest(MINT3, 1.00, "helius-stream")
        self.feed.ingest(MINT3, 1.01, "helius-stream")
        html = self._html("/coins/%s" % MINT3)
        self.assertIn('24h <span class="up">+1.0%</span>', html)
        self.assertNotIn("24h change unavailable", html)


class TestBotDetailPrice(AppwideBase):
    def test_trade_price_is_sig_figs(self):
        bot_id = self._insert_bot()
        self.db.execute(
            "INSERT INTO trade_ledger (trade_id, user_id, bot_id, "
            "coin, side, amount, price, total_usd, pnl_usd, win, "
            "created_at) VALUES (?, ?, ?, ?, 'buy', 1000, 0.00002, "
            "0.02, NULL, NULL, '2026-11-25T10:00:00Z')",
            ("T1", self.user_id, bot_id, "TEST"))
        self.db.commit()
        html = self._html("/bots/%d" % bot_id)
        self.assertIn("$0.0000200", html)
        self.assertNotIn("$0.0000<", html)


class TestLogsAnalysisPrice(AppwideBase):
    def test_analysis_prices_are_sig_figs(self):
        # A completed pair: bought at $0.00002, sold at $0.00003.
        self._paper_trade(MINT1, "buy", 10, 0.00002,
                          "2026-11-25T10:00:00Z")
        self._paper_trade(MINT1, "sell", 10, 0.00003,
                          "2026-11-25T10:05:00Z",
                          exit_reason="stop_loss")
        html = self._html("/logs")
        self.assertIn("$0.0000200", html)
        self.assertIn("$0.0000300", html)
        self.assertNotIn("$0.0000<", html)


class TestLiveJsCoinsPoll(unittest.TestCase):
    def test_coins_list_poll_exists(self):
        js = (ROOT / "tradingbot" / "static" / "live.js").read_text()
        self.assertIn("/api/coins-rows", js)
        self.assertIn("fetchCoinsRows", js)
        self.assertIn("data-coins-list", js)
        # The holdings poll clears a stale 1h % to "—" when the
        # server stops having one, instead of leaving it standing.
        self.assertIn("r.change_1h === null", js)
        # The coins poll creates the empty-list message when the
        # list empties and removes it when a row arrives — the
        # server renders it only when the list starts empty.
        self.assertIn("data-coins-empty", js)
        self.assertIn("emptyEl.remove()", js)


if __name__ == "__main__":
    unittest.main()
