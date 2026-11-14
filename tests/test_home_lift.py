"""Home lift (2026-11-09).

Rules under test — this supersedes the redo's 1/4-screen fine-print
holdings:
- Everything lifted up: tighter top spacing, fox logo + wordmark in
  one horizontal row, action buttons in a single row right under
  the logo, near the top of the page and above the holdings list.
- Holdings follow the balance box immediately — no headings, no
  summary lines, no borders — and each held coin is a NORMAL-SIZE
  coin button: real name + ticker, amount held, value, and the
  1-hour % from the feed. A coin with no 1h figure shows "—" —
  never guessed. The page scrolls now (owner, 2026-11-12), so the
  holdings panel grows with the list and never scrolls internally
  (supersedes the compact build's internal scroll).
- 1h data: SOL's 1h change is always None (the SOL price feed
  carries no 1h figure); pump.fun mints get theirs from
  DexScreener best-pair priceChange.h1 (new fetch_mint_1h_changes,
  mirroring fetch_prices_for_mints). A price feed that cannot be
  reached reports "unavailable" with no price at all — there is no
  snapshot fallback any more.
- The portfolio chart moved off the home page to make room; the
  total and the 24h change stay. The candlestick build (2026-11-10)
  brings a COMPACT chart back into the balance card — candlestick
  view with duration options and the candle/line toggle, candles
  from recorded snapshots only, never faked — while the balance box
  is about a third smaller (compact build, 2026-11-11) and the
  chart keeps its 100px height. The old "Full chart ›" link to the
  P&L page is gone with that page (Logs build, 2026-11-10). The main
  screen still has NO back-button X.
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
# Gate (the devnet pump.fun coin): the mint the lift tests buy
# and hold, priced at $0.01 with a +2.50% 1h change from the
# mocked trenches feed. MINT above is the unnamed mint used for
# the no-data dash test.
GATE_MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.01
GATE_CHANGE_1H = 2.5


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
        # SOL only now — the app prices no major coins; pump.fun
        # mints are priced by the trenches feed (mocked below).
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "SOL": (10.0, 5.25, 2.5)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(10.0, "live", "test"))
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            return_value={GATE_MINT: GATE_PRICE})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes",
            return_value={GATE_MINT: GATE_CHANGE_1H})
        self.mint_1h_patch.start()
        self.mint_idents_patch = mock.patch.object(
            routes, "fetch_mint_identities",
            return_value={GATE_MINT: {"name": "Gate",
                                      "symbol": "GATE"}})
        self.mint_idents_patch.start()
        self.coin_meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.coin_meta_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.coin_meta_patch.stop()
        self.mint_idents_patch.stop()
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

    def _buy_mint(self, usd="10"):
        # A real paper buy of the Gate mint: $10 at the mocked
        # $0.01 feed price buys 1000 GATE. SOL is not a buyable
        # coin any more (no SOL detail page, no SOL buy route).
        return self.client.post(f"/coins/{GATE_MINT}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "usd": usd, "bot_id": ""})

    def _hold_mint(self, amount=100.0, avg_price=0.01, mint=MINT):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, amount, avg_price))
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
                        html.index("holdings-panel"))

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

    def test_holdings_panel_fills_remaining_space(self):
        # Scrollable home (owner, 2026-11-12, supersedes the
        # compact build's internal scroll): the page scrolls, so
        # the holdings panel grows with the list, borderless, and
        # never scrolls internally.
        css = open("tradingbot/static/style.css").read()
        self.assertIn(
            ".holdings-panel { max-height: none; border: none; "
            "background: transparent; padding: 0; }", css)
        # The old half-screen rule is gone.
        self.assertNotIn("flex: 0 1 50dvh", css)
        self.assertNotIn("max-height: 50dvh", css)
        self.assertNotIn("25dvh", css)

    def test_holdings_rows_are_normal_size_not_fine_print(self):
        css = open("tradingbot/static/style.css").read()
        self.assertNotIn(".holdings-row { font-size: 12px;", css)
        self.assertIn(".holdings-row { font-size: 16px;", css)
        self.assertIn(
            ".holdings-row .coin-ico { flex: 0 0 32px;", css)

    def test_holdings_show_name_ticker_value_and_1h(self):
        self._choose_mode("paper")
        self._buy_mint()
        html = self._html("/wallet")
        self.assertIn("Gate", html)
        self.assertIn("(GATE)", html)
        # $10 at the mocked $0.01 feed price buys 1000 GATE: no fee
        # (removed 2026-11-12), leaves 1,000.0000 GATE worth $10.00.
        self.assertIn("1,000.0000 GATE", html)
        self.assertIn("$10.00", html)
        self.assertIn("holdings-1h", html)
        self.assertIn("+2.50%", html)

    def test_mint_without_1h_data_shows_dash_not_a_guess(self):
        self._choose_mode("paper")
        self._hold_mint()
        html = self._html("/wallet")
        # Nothing names this mint (no feed identity, no coin_meta),
        # so it shows as "Unknown coin" — never a mint address and
        # never the old "Pump.fun token …" label.
        self.assertIn("Unknown coin", html)
        # No 1h data: the 1h cell still shows a dash, never a guess.
        # (The dash sits in the live-update value span, so the live
        # feed can fill it when data arrives. The screenshot shows
        # only the % — there is no "1h" label under it.)
        self.assertIn('class="holdings-1h muted"', html)
        self.assertIn('>—</span></span>', html)

    def test_home_has_no_back_button_x(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("data-go-back", html)
        self.assertNotIn('aria-label="Back">✕<', html)

    def test_home_chart_is_back_in_balance_area_with_durations(self):
        # The candlestick build (2026-11-10) brings a compact chart
        # back to the home balance area, by the user's order: a
        # candlestick chart with duration options and the same
        # candle/line toggle as the coin detail page. It lives in
        # the balance card as home-chart / home-chart-durations —
        # never the old portfolio-chart / chart-ranges classes.
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("portfolio-chart", html)
        self.assertNotIn("chart-ranges", html)
        self.assertIn('id="home-chart"', html)
        self.assertIn("home-chart-durations", html)
        for label in ("1h", "1d", "1w", "1m", "1y", "All"):
            self.assertIn(f">{label}<", html)
        # The chart lives on the home page itself — the old "Full
        # chart ›" link to the deleted P&L page is gone, never a
        # dead link.
        self.assertNotIn("Full chart ›", html)
        self.assertNotIn('href="/pnl"', html)
        # The total and the 24h change still show.
        self.assertIn("TOTAL PORTFOLIO VALUE", html)
        # No snapshots yet, so no candles — the page says so
        # instead of drawing a chart, and there is no toggle to
        # switch (the toggle appears once candles exist; see
        # test_candles.py).
        self.assertIn("Not enough history yet", html)
        self.assertNotIn('data-chart-toggle', html)


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

    def test_jupiter_live_fetch_prices_sol_only(self):
        # The only price list left is SOL (the unit of account),
        # from Jupiter's lite-api price v3 — no CoinGecko, no
        # 36-coin list, no 1h figure anywhere (SOL's change_1h is
        # always None; mints get theirs from the trenches feed).
        payload = {"data": {prices.SOL_MINT: {
            "usdPrice": 10.0, "priceChange24h": 5.25}}}
        seen = {}

        def fake_urlopen(req, timeout=10):
            seen["url"] = req.full_url
            return self._fake_urlopen(payload)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = prices.get_prices()
        self.assertIn("lite-api.jup.ag/price/v3", seen["url"])
        self.assertIn(prices.SOL_MINT, seen["url"])
        self.assertEqual(data["source"], "live")
        self.assertEqual(list(data["prices"]), ["SOL"])
        self.assertEqual(data["prices"]["SOL"]["price"], 10.0)
        self.assertEqual(data["prices"]["SOL"]["change_24h"], 5.25)
        # No 1h figure is ever carried for SOL ("—" on the page).
        self.assertIsNone(data["prices"]["SOL"]["change_1h"])

    def test_fetch_failure_is_unavailable_not_a_snapshot(self):
        # No snapshot fallback any more: a feed that does not
        # answer reports "unavailable" with no price at all — the
        # old baked CoinGecko snapshot once served 4-day-old
        # prices as if they were live, and it is gone for good.
        with mock.patch("urllib.request.urlopen",
                        side_effect=Exception("no network")):
            data = prices.get_prices()
        self.assertEqual(data["source"], "unavailable")
        self.assertIsNone(data["prices"]["SOL"]["price"])
        self.assertIsNone(data["prices"]["SOL"]["change_1h"])

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
    def test_mint_rows_carry_change_1h_and_legacy_sol_has_none(self):
        # Two mints, priced by the trenches feed: each row carries
        # its own 1h change and value. A legacy SOL holding (bought
        # when the app still listed major coins) has no feed any
        # more — it is shown at cost with no live price and no 1h
        # figure, never hidden and never guessed.
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 2.0, 5.0)",
            (self.user_id,))
        self._hold_mint()
        self._hold_mint(amount=50.0, avg_price=0.2, mint=GATE_MINT)
        self.db.commit()
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5, GATE_MINT: 0.25}), \
                mock.patch.object(
                routes, "fetch_mint_1h_changes",
                return_value={MINT: -3.25, GATE_MINT: 4.0}):
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25, 2.5)}))
        by_id = {r["coin_id"]: r for r in rows}
        self.assertEqual(by_id[MINT]["change_1h"], -3.25)
        self.assertEqual(by_id[MINT]["value"], 50.0)
        self.assertEqual(by_id[GATE_MINT]["change_1h"], 4.0)
        self.assertEqual(by_id[GATE_MINT]["value"], 12.5)
        self.assertIsNone(by_id["SOL"]["change_1h"])
        self.assertIsNone(by_id["SOL"]["price"])

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
