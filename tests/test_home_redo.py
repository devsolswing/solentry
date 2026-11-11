"""Home-page redo (2026-11-09) + home lift (2026-11-09).

Rules under test:
- Home lift (supersedes the redo's 1/4-screen fine-print panel):
  the holdings section takes HALF the screen and scrolls internally;
  every held coin is a NORMAL-SIZE coin button — real name + ticker,
  amount held, value, and the 1-hour % from the feed ("—" when the
  feed has no 1h figure, never guessed). The action buttons sit in
  one row right under the fox logo, above the holdings list, and the
  page still fits one phone screen (only the holdings list scrolls).
  Home has Send and Receive buttons; Receive opens a window with
  the app wallet address + copy. The portfolio chart moved off the
  home page to make room for the half-screen list; the total, the
  24h change and a "Full chart ›" link to the P&L page stay.
- The X (back) button is GONE from the home screen: on the homepage
  it looped (back -> login page -> instant redirect to /wallet, so
  the homepage kept re-applying itself). It stays on Bots and
  Trenches, where going back makes sense.
- Coin detail page: tapping a held coin opens its detail page with
  a live price that ticks every second while the page is open
  (GET /api/coins/<id>/tick) — real prices only, never invented;
  a failed tick keeps the last real price and its time. The chart
  itself is a real candlestick chart (CoinGecko OHLC for known
  coins, live-feed tick history for mints) with a corner button
  switching it to the regular line chart; it re-renders at most
  once per candle interval, never every second. Send,
  Receive, Buy and Sell are four separate windows over the coin
  page, reusing the existing flows (two-step send with a way back
  to the coin page; paper ledger / pump.fun executor guards for
  Buy/Sell). Every action ends on a confirmation with a way back
  to the coin.
- The Demo / Real chooser is remembered in the database as well as
  the session: answering it (or switching mode from the HUD) sets
  users.mode_chosen_at, so a dropped session flag alone can never
  make the chooser reappear mid-session. Signing in clears it, so
  the chooser still shows once after each sign-in.
No SOL moves in these tests; sends are never confirmed.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup

DEST = "11111111111111111111111111111111"
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


def fake_chart(points):
    return {"points": [{"ts": 1790000000000 + i * 60000, "price": p}
                       for i, p in enumerate(points)], "error": None}


def fake_ohlc(*args, **kwargs):
    """Two 30-minute candles — enough for a candlestick chart."""
    return {"candles": [
        {"ts": 1790000000000, "open": 80.0, "high": 82.0,
         "low": 79.0, "close": 81.0},
        {"ts": 1790001800000, "open": 81.0, "high": 83.0,
         "low": 80.5, "close": 82.0}],
        "interval": 1800, "error": None}


class HomeRedoBase(unittest.TestCase):
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
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]

    def tearDown(self):
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

    def _drop_session_flag(self):
        """Simulate the session flag being lost (e.g. Phantom
        dropping the session cookie's mode flag) while the user
        stays signed in."""
        with self.client.session_transaction() as sess:
            sess.pop("mode_chosen", None)


class TestHomeHasNoX(HomeRedoBase):
    def test_homepage_has_no_back_button(self):
        html = self._html("/wallet")
        self.assertNotIn("data-go-back", html)
        self.assertNotIn('aria-label="Back">✕<', html)

    def test_bots_and_trenches_keep_back_button(self):
        # Going back makes sense there — only the home screen
        # loses the X, because there is nothing to go back from.
        self.assertIn("data-go-back", self._html("/bots"))
        self.assertIn("data-go-back", self._html("/trenches"))


class TestHoldingsHalfPanel(HomeRedoBase):
    def test_holdings_panel_is_half_screen_and_scrolls(self):
        # Home lift (supersedes the 1/4-screen fine-print panel):
        # the holdings list takes HALF the screen and scrolls
        # internally; on short screens it may shrink so the page
        # still fits one screen, never beyond half.
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".holdings-panel { flex: 0 1 50dvh;", css)
        self.assertIn("max-height: 50dvh;", css)
        self.assertIn("overflow-y: auto;", css)

    def test_holdings_rows_are_normal_size_with_value_and_1h(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("holdings-row", html)
        self.assertIn("holdings-summary", html)
        self.assertIn("Solana", html)
        self.assertIn("(SOL)", html)
        # 1 SOL bought at the mocked $10 price.
        self.assertIn("1.0000 SOL", html)
        self.assertIn("$10.00", html)
        self.assertIn("1 coin", html)
        # Normal-size rows now carry the 1-hour % (mocked +2.5%)
        # next to the amount + value.
        self.assertIn("holdings-1h", html)
        self.assertIn("+2.50%", html)
        self.assertIn("1h", html)
        # The old P&L / 24h columns still live on the coins page
        # and the coin detail page, not on the home rows.
        self.assertNotIn("coin-pnl", html)
        self.assertNotIn("coin-change", html)

    def test_home_still_shows_paper_balance(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("Paper balance", html)
        self.assertIn("$100.00 CAD cash", html)

    def test_home_has_send_and_receive(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)
        self.assertIn('id="home-receive-overlay"', html)
        self.assertIn(self.pubkey, html)
        self.assertIn(f'data-copy="{self.pubkey}"', html)

    def test_home_ui_enlarged_css(self):
        css = open("tradingbot/static/style.css").read()
        # Bigger portfolio total and logo than the pre-redo sizes
        # (34px total, 118px logo), sized in dvh so a short phone
        # still fits one screen.
        self.assertIn("clamp(32px, 5dvh, 44px)", css)
        self.assertIn("clamp(56px, 10dvh, 96px)", css)


class TestCoinDetailActions(HomeRedoBase):
    def setUp(self):
        super().setUp()
        self._choose_mode("paper")
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart",
            side_effect=lambda coin, days: fake_chart(
                [80.0, 84.0, 86.0, 90.0, 88.0]))
        self.chart_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow",
            return_value={"buys": 120, "sells": 80,
                          "buys_pct": 60.0, "sells_pct": 40.0})
        self.flow_patch.start()
        self.ohlc_patch = mock.patch.object(
            routes, "fetch_ohlc", side_effect=fake_ohlc)
        self.ohlc_patch.start()
        self.addCleanup(self.chart_patch.stop)
        self.addCleanup(self.flow_patch.stop)
        self.addCleanup(self.ohlc_patch.stop)
        self._buy_sol()

    def test_four_action_buttons_each_open_their_own_window(self):
        html = self._html("/coins/SOL/detail")
        for overlay in ("coin-send-overlay", "coin-receive-overlay",
                        "coin-buy-overlay", "coin-sell-overlay"):
            self.assertIn(f'data-overlay-open="{overlay}"', html)
            self.assertIn(f'id="{overlay}"', html)
        # Buy / Sell reuse the existing coin endpoints.
        self.assertIn('action="/coins/SOL/buy"', html)
        self.assertIn('action="/coins/SOL/sell"', html)
        self.assertIn('name="sell_all"', html)
        # Send reuses the wallet send flow, with a way back to
        # this coin; Receive shows the app wallet address + copy.
        self.assertIn('action="/wallet/send"', html)
        self.assertIn('value="coin:SOL"', html)
        self.assertIn(self.pubkey, html)
        self.assertIn(f'data-copy="{self.pubkey}"', html)

    def test_live_chart_block_present(self):
        # The detail chart is a real candlestick chart (not a
        # every-second redraw): the live price readout still ticks
        # every second, but the chart re-renders at most once per
        # candle interval, and a corner button switches it to the
        # regular line view.
        html = self._html("/coins/SOL/detail")
        self.assertIn('id="live-chart"', html)
        self.assertIn('data-tick-url="/api/coins/SOL/tick"', html)
        self.assertIn('id="live-price"', html)
        self.assertIn('id="live-updated"', html)
        self.assertNotIn('id="live-chart-svg"', html)
        self.assertNotIn("updating every second", html)
        self.assertIn('aria-label="Candlestick chart"', html)
        self.assertIn('id="chart-view-toggle"', html)

    def test_coin_result_links_back_to_coin(self):
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_sol": "1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Back to coin", html)
        self.assertIn('href="/coins/SOL/detail"', html)

    def test_tick_endpoint_known_coin(self):
        resp = self.client.get("/api/coins/SOL/tick")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        self.assertEqual(data["coin_id"], "SOL")
        self.assertEqual(data["price"], 10.0)
        self.assertEqual(data["source"], "snapshot")
        self.assertEqual(data["as_of"], "test")

    def test_tick_endpoint_unknown_coin_says_so(self):
        resp = self.client.get("/api/coins/NOPE/tick")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])

    def test_tick_endpoint_mint_uses_trenches_feed(self):
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5}):
            resp = self.client.get(f"/api/coins/{MINT}/tick")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 0.5)
        self.assertEqual(data["source"], "trenches feed")

    def test_tick_endpoint_needs_login(self):
        self.client.get("/logout")
        resp = self.client.get("/api/coins/SOL/tick")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_tick_js_is_served(self):
        resp = self.client.get("/static/coin_tick.js")
        self.assertEqual(resp.status_code, 200)
        js = resp.get_data(as_text=True)
        self.assertIn("setInterval(tick, 1000)", js)
        self.assertIn("visibilitychange", js)


class TestChooserRememberedInDb(HomeRedoBase):
    def test_chooser_shows_once_after_signup(self):
        html = self._html("/wallet")
        self.assertIn("How do you want to trade?", html)

    def test_chooser_stays_away_when_session_flag_is_dropped(self):
        self._choose_mode("paper")
        self.assertNotIn("How do you want to trade?",
                         self._html("/wallet"))
        # The database remembers the answer now.
        row = self.db.execute(
            "SELECT mode_chosen_at FROM users WHERE id = ?",
            (self.user_id,)).fetchone()
        self.assertTrue(row["mode_chosen_at"])
        # Drop the session flag: the chooser must NOT come back.
        self._drop_session_flag()
        self.assertNotIn("How do you want to trade?",
                         self._html("/wallet"))

    def test_chooser_shows_again_after_next_signin(self):
        self._choose_mode("paper")
        self._drop_session_flag()
        self.assertNotIn("How do you want to trade?",
                         self._html("/wallet"))
        self.client.get("/logout")
        login(self.client, "alice")
        self.assertIn("How do you want to trade?",
                      self._html("/wallet"))

    def test_hud_mode_switch_also_marks_chosen(self):
        resp = self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "mode": "devnet"})
        self.assertEqual(resp.status_code, 302)
        self._drop_session_flag()
        self.assertNotIn("How do you want to trade?",
                         self._html("/wallet"))

    def test_never_answered_chooser_still_shows(self):
        # A user who never answered still gets the chooser, even
        # with no session flag at all.
        self._drop_session_flag()
        self.assertIn("How do you want to trade?",
                      self._html("/wallet"))


class TestSendReturnToCoin(HomeRedoBase):
    def _create_intent(self, return_to):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.01", "token": "SOL",
            "return_to": return_to})
        self.assertEqual(resp.status_code, 302)
        return resp.headers["Location"]

    def test_send_carries_coin_return_to_confirm_page(self):
        location = self._create_intent("coin:SOL")
        self.assertIn("/wallet/send/", location)
        self.assertIn("return_to=coin:SOL", location)
        html = self._html(location)
        self.assertIn("Back to coin", html)
        self.assertIn('href="/coins/SOL/detail"', html)

    def test_send_return_to_coins_still_works(self):
        location = self._create_intent("coins")
        self.assertIn("return_to=coins", location)
        html = self._html(location)
        self.assertIn("Back to coins", html)
        self.assertIn('href="/dashboard"', html)

    def test_send_return_to_garbage_falls_back_to_wallet(self):
        location = self._create_intent("coin:NOPE")
        self.assertNotIn("return_to=", location)
        html = self._html(location)
        self.assertNotIn("Back to coin", html)
        self.assertIn("Cancel", html)
