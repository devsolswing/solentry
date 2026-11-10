"""Home lift (2026-11-09).

Rules under test — this supersedes the redo's 1/4-screen fine-print
holdings:
- Everything lifted up: tighter top spacing, fox logo + wordmark in
  one horizontal row, action buttons in a single row right under
  the logo, near the top of the page and above the holdings list.
- Holdings take HALF the screen (50dvh, scrolling internally) and
  each held coin is a NORMAL-SIZE coin button: real name + ticker,
  amount held, value, and the 1-hour % from the feed. A coin with
  no 1h figure shows "—" — never guessed.
- 1h data: CoinGecko simple/price with include_1hr_change for the
  known coins; DexScreener best-pair priceChange.h1 for pump.fun
  mints (new fetch_mint_1h_changes, mirroring
  fetch_prices_for_mints). Snapshot fallback carries change_1h
  None.
- The portfolio chart moved off the home page to make room; the
  total, the 24h change and a "Full chart ›" link to the P&L page
  stay. The main screen still has NO back-button X.
No SOL moves in these tests; sends are never confirmed.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
import tradingbot.trenches as trenches
from helpers import csrf, make_client, signup

MINT = "11111111111111111111111111111111"


def make_prices(changes):
    """changes: {coin: (price, change_24h)} or
    (price, change_24h, change_1h)"""
    out = {}
    for c, tup in changes.items():
        price, ch24 = tup[0], tup[1]
        ch1h = tup[2] if len(tup) > 2 else None
        out[c] = {"price": price, "change_24h": ch24,
                  "change_1h": ch1h}
    return {"prices": out, "source": "snapshot", "as_of": "test"}


class HomeLiftBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25, 2.5), "USDC": (1.0, 0.0)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(10.0, "snapshot", "test"))
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints", return_value={})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.mint_1h_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.mint_1h_patch.stop()
        self.mint_prices_patch.stop()
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _buy_sol(self, usd="10"):
        return self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": usd, "bot_id": ""})

    def _hold_mint(self, amount=100.0, avg_price=0.01):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, MINT, amount, avg_price))
        self.db.commit()


class TestHomeLiftLayout(HomeLiftBase):
    def test_action_buttons_are_one_row_under_the_fox_above_holdings(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("home-actions-row", html)
        self.assertIn("BUY", html)
        self.assertIn("SELL", html)
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)
        # The single actions row sits under the fox logo, above the
        # portfolio total and above the holdings list.
        self.assertLess(html.index("home-actions-row"),
                        html.index("home-total"))
        self.assertLess(html.index("home-actions-row"),
                        html.index("HOLDINGS"))

    def test_fox_logo_on_home_and_everything_lifted_up(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("fox-logo", html)
        self.assertIn("SolEntry fox logo", html)
        css = open("tradingbot/static/style.css").read()
        # Logo + wordmark share one horizontal row; the page starts
        # higher (4px top padding instead of 8px).
        self.assertIn(".home-logo { display: flex;", css)
        self.assertIn(
            "body.app-shell.fit-screen .wrap { padding: 4px 12px 84px; }",
            css)

    def test_holdings_panel_is_half_screen(self):
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".holdings-panel { flex: 0 1 50dvh;", css)
        self.assertIn("max-height: 50dvh;", css)
        self.assertIn("overflow-y: auto;", css)
        # The old quarter-screen rule is gone.
        self.assertNotIn("25dvh", css)

    def test_holdings_rows_are_normal_size_not_fine_print(self):
        css = open("tradingbot/static/style.css").read()
        self.assertNotIn(".holdings-row { font-size: 12px;", css)
        self.assertIn(".holdings-row { font-size: 16px;", css)
        self.assertIn(
            ".holdings-row .coin-ico { flex: 0 0 34px;", css)

    def test_holdings_show_name_ticker_value_and_1h(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("Solana", html)
        self.assertIn("(SOL)", html)
        self.assertIn("1.0000 SOL", html)
        self.assertIn("$10.00", html)
        self.assertIn("holdings-1h", html)
        self.assertIn("+2.50%", html)

    def test_mint_without_1h_data_shows_dash_not_a_guess(self):
        self._choose_mode("paper")
        self._hold_mint()
        html = self._html("/wallet")
        self.assertIn("Pump.fun token 1111…1111", html)
        self.assertIn('class="holdings-1h muted">—', html)

    def test_home_has_no_back_button_x(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("data-go-back", html)
        self.assertNotIn('aria-label="Back">✕<', html)

    def test_home_chart_moved_off_but_full_chart_link_stays(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("portfolio-chart", html)
        self.assertNotIn("chart-ranges", html)
        self.assertIn("Full chart ›", html)
        self.assertIn('href="/pnl"', html)
        # The total and the 24h change still show.
        self.assertIn("TOTAL PORTFOLIO VALUE", html)


class TestChange1hFeed(unittest.TestCase):
    def setUp(self):
        prices.clear_price_cache()
        self.addCleanup(prices.clear_price_cache)

    def _fake_urlopen(self, payload):
        class FakeResp:
            def __init__(self, data):
                self._data = data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self._data).encode("utf-8")
        return FakeResp(payload)

    def test_coingecko_live_fetch_carries_1h_change(self):
        payload = {
            "bitcoin": {"usd": 74000.0, "usd_24h_change": -2.5,
                        "usd_1h_change": 0.4},
            "ethereum": {"usd": 2100.0, "usd_24h_change": 1.0,
                         "usd_1h_change": -0.2},
            "solana": {"usd": 10.0, "usd_24h_change": 5.25,
                       "usd_1h_change": 2.5},
            "usd-coin": {"usd": 1.0, "usd_24h_change": 0.0,
                         "usd_1h_change": 0.0},
            "dogecoin": {"usd": 0.07, "usd_24h_change": 3.0},
            "pepe": {"usd": 0.000005, "usd_24h_change": -1.0,
                     "usd_1h_change": 1.1},
            "bonk": {"usd": 0.000003, "usd_24h_change": 8.0,
                     "usd_1h_change": -4.0},
            "jupiter-exchange-solana": {"usd": 0.23,
                                        "usd_24h_change": -0.5,
                                        "usd_1h_change": 0.9},
        }
        seen = {}

        def fake_urlopen(req, timeout=10):
            seen["url"] = req.full_url
            return self._fake_urlopen(payload)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = prices.get_prices()
        self.assertIn("include_1hr_change=true", seen["url"])
        self.assertEqual(data["source"], "live")
        self.assertEqual(data["prices"]["SOL"]["change_1h"], 2.5)
        self.assertEqual(data["prices"]["BTC"]["change_1h"], 0.4)
        # A coin the feed gives no 1h figure for reads None ("—").
        self.assertIsNone(data["prices"]["DOGE"]["change_1h"])

    def test_snapshot_fallback_has_no_1h_change(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=Exception("no network")):
            data = prices.get_prices()
        self.assertEqual(data["source"], "snapshot")
        for entry in data["prices"].values():
            self.assertIsNone(entry["change_1h"])

    def _dex_pair(self, mint, h1, liq=1000.0):
        return {"chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.5",
                "priceChange": {"m5": 1.0, "h1": h1, "h6": 2.0,
                                "h24": 3.0},
                "liquidity": {"usd": liq},
                "baseToken": {"address": mint, "symbol": "TEST",
                              "name": "Test"}}

    def test_fetch_mint_1h_changes_from_best_pair(self):
        payload = {"pairs": [
            self._dex_pair(MINT, 7.5, liq=1000.0),
            self._dex_pair(MINT, -9.9, liq=5000.0),
        ]}

        def fake_get(url):
            return payload

        out = trenches.fetch_mint_1h_changes([MINT], http_get=fake_get)
        # Highest-liquidity pair wins, not the first one.
        self.assertEqual(out, {MINT: -9.9})

    def test_fetch_mint_1h_changes_skips_missing_h1(self):
        pair = self._dex_pair(MINT, None)
        del pair["priceChange"]["h1"]
        out = trenches.fetch_mint_1h_changes(
            [MINT], http_get=lambda url: {"pairs": [pair]})
        self.assertEqual(out, {})

    def test_fetch_mint_1h_changes_failure_is_empty(self):
        out = trenches.fetch_mint_1h_changes(
            [MINT], http_get=lambda url: (_ for _ in ()).throw(
                Exception("boom")))
        self.assertEqual(out, {})


class TestCoinRowsChange1h(HomeLiftBase):
    def test_known_coin_and_mint_rows_carry_change_1h(self):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 2.0, 5.0)",
            (self.user_id,))
        self._hold_mint()
        self.db.commit()
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5}), mock.patch.object(
                routes, "fetch_mint_1h_changes",
                return_value={MINT: -3.25}):
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25, 2.5)}))
        by_id = {r["coin_id"]: r for r in rows}
        self.assertEqual(by_id["SOL"]["change_1h"], 2.5)
        self.assertEqual(by_id[MINT]["change_1h"], -3.25)
        self.assertEqual(by_id[MINT]["value"], 50.0)

    def test_missing_1h_figures_are_none_not_zero(self):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 2.0, 5.0)",
            (self.user_id,))
        self._hold_mint()
        self.db.commit()
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5}), mock.patch.object(
                routes, "fetch_mint_1h_changes", return_value={}):
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25)}))
        by_id = {r["coin_id"]: r for r in rows}
        self.assertIsNone(by_id["SOL"]["change_1h"])
        self.assertIsNone(by_id[MINT]["change_1h"])
