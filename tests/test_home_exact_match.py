"""Home exact match (owner 2026-11-11 ~05:21 + 05:23 PST) + live 1hr
sparklines.

The homescreen matches the Home screenshot precisely — same size,
same font, same color, same logo, same portfolio window — and every
button/word links to what the app has. Each holdings coin has a 1hr
chart that refreshes every 60 seconds from the live feed.

REAL DATA ONLY: the screenshot's numbers (2.45 SOL, $185.20 CAD,
+5.82%, Pepe / Bitcoin / Solana / Dogecoin / Ethereum) are the
mockup's examples, never the user's data — no test here (and no
template) may hardcode them. Holdings come from the route's real
coin_rows; a missing price is "—", never invented.
No SOL moves in these tests; sends are never confirmed.
"""

import re
import tempfile
import time
import unittest
from unittest import mock

import tradingbot.home_holdings as hh
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from test_home_mockup import HomeMockupBase


def _css():
    with open("tradingbot/static/style.css", encoding="utf-8") as f:
        return f.read()


def _rule(css, selector):
    # The LAST matching rule wins in the cascade, so return it
    # (several selectors — .home-wordmark, .sell-btn, the home
    # total — are defined more than once, the later one overriding).
    # Anchored to the start of a line so a compound selector that
    # merely ENDS in the name (".buy-btn, .sell-btn {") or a longer
    # one (".bots-screen .home-wordmark {") never matches.
    matches = re.findall(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert matches, "%s rule missing from style.css" % selector
    return matches[-1]


class TestHomeButtonsExact(HomeMockupBase):
    """The four action buttons match the photo: solid colours, one
    size, clean SVG icons, same destinations."""

    def test_button_colours_are_solid_and_exact(self):
        css = _css()
        buy = _rule(css, ".buy-btn")
        self.assertIn("background: #ffd700", buy)
        self.assertIn("color: #1a0505", buy)
        self.assertNotIn("linear-gradient", buy)
        sell = _rule(css, ".sell-btn")
        self.assertIn("background: #e62e1b", sell)
        self.assertIn("color: #fff", sell)
        self.assertNotIn("linear-gradient", sell)
        send = _rule(css, ".home-actions-row .send-receive-btn")
        self.assertIn("background: #000", send)
        self.assertIn("border: 2px solid #ffb300", send)
        self.assertIn("color: #ffd700", send)

    def test_buttons_are_one_size(self):
        css = _css()
        row = _rule(
            css,
            ".home-actions-row .buy-btn, .home-actions-row .sell-btn, "
            ".home-actions-row .send-receive-btn")
        self.assertIn("flex: 1 1 0", row)
        self.assertIn("font-size: 16px", row)
        # The old split (BUY/SELL 17px, Send/Receive 15px) is gone.
        self.assertNotIn(
            ".home-actions-row .buy-btn, .home-actions-row .sell-btn "
            "{ flex: 1 1 0; padding: 12px 2px; font-size: 17px; }", css)
        self.assertNotIn(
            ".home-actions-row .send-receive-btn { flex: 1 1 0; "
            "padding: 12px 2px; font-size: 15px; text-align: center; }",
            css)

    def test_button_icons_are_svg_sized_like_the_photo(self):
        css = _css()
        self.assertIn("width: 26px", _rule(css, ".home-action-ico svg"))
        self.assertIn("height: 26px", _rule(css, ".home-action-ico svg"))
        # The icons inherit each button's colour (black BUY, white
        # SELL, gold Send/Receive) — stroke=currentColor.
        self._choose_mode("paper")
        html = self._html("/wallet")
        actions = html.split('class="home-actions-row"')[1].split("</div>")[0]
        self.assertEqual(actions.count('stroke="currentColor"'), 4)

    def test_button_destinations_unchanged(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn('data-quick-trade-open', html)
        self.assertIn('data-trade="buy"', html)
        self.assertIn('data-trade="sell"', html)
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)


class TestHomeHeaderAndBalanceExact(HomeMockupBase):
    """Header wordmark all gold, balance total white, % proportioned
    — as in the photo."""

    def test_wordmark_is_all_gold_not_red(self):
        css = _css()
        # The base rule (the max-height:740px media query shrinks it
        # to 30px on very short screens — that override is fine).
        # A gold gradient only — the .se-title red (#ff1e14) must
        # not colour the home wordmark.
        self.assertIn(
            ".home-wordmark { font-size: clamp(36px, 6dvh, 50px); "
            "background: linear-gradient(100deg, #ffb300 0%, #ffd700 "
            "50%, #ffcf3d 100%); }", css)
        self.assertNotIn(
            ".home-wordmark { font-size: clamp(36px, 6dvh, 50px); "
            "background: linear-gradient(100deg, #ff1e14", css)

    def test_balance_total_is_white(self):
        css = _css()
        total = _rule(css, ".home-total .portfolio-total")
        self.assertIn("color: #fff", total)

    def test_balance_pct_is_proportioned_to_the_total(self):
        css = _css()
        pct = _rule(css, ".home-total-pct")
        self.assertIn("clamp(16px, 2.4dvh, 20px)", pct)


class TestHoldingsRowExact(HomeMockupBase):
    """The holdings row matches the photo: name + ticker left,
    sparkline middle, price + amount right, 1h % far right."""

    def test_amount_sits_under_the_price(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        # 1 SOL bought at the mocked $10 price: the 3.65% fee leaves
        # 0.9635 SOL worth $9.63; the price is $10.00; 1h +2.50%.
        self.assertIn("0.9635 SOL", html)
        self.assertIn("$10.00", html)
        self.assertIn("+2.50%", html)
        # The amount comes after the price and before the % — it is
        # under the price on the right, not under the name.
        self.assertLess(html.index("$10.00"), html.index("0.9635 SOL"))
        self.assertLess(html.index("0.9635 SOL"), html.index("+2.50%"))
        # The $ value is still in the page (hidden, for the live
        # paper total) but not a visible row line.
        self.assertIn("$9.63", html)
        self.assertIn("holdings-value-live", html)
        self.assertIn("display: none", _rule(_css(), ".holdings-value-live"))

    def test_no_1h_label_under_the_percent(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertNotIn("holdings-1h-label", html)
        # The holdings list follows the balance box directly (no
        # HOLDINGS heading anymore) — the 1h % still has no label.
        self.assertNotIn(">1h</span>", html.split("holdings-panel")[1])

    def test_sparkline_span_is_always_present_with_the_coin_id(self):
        # A row with no sparkline yet still renders the span (empty
        # while the chart builds) so the 60s refresh can fill it.
        row = {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
               "price": 10.0, "change_1h": 2.5, "held": 0.9635,
               "value": 9.63, "is_mint": False}
        with mock.patch.object(routes, "_coin_rows",
                               return_value=[row]):
            self._choose_mode("paper")
            html = self._html("/wallet")
        self.assertIn('data-sparkline-coin="SOL"', html)
        # An empty sparkline takes no space.
        self.assertIn("display: none",
                      _rule(_css(), ".holdings-spark:empty"))


class TestHoldingsWindowSizesExact(HomeMockupBase):
    """The HOLDINGS window matches the photo's sizes exactly.

    Measured from the Home screenshot (832x1248, scale ~2.0):
    compact rows (~38px), 32px coin icons, name/price 16px,
    1h % 14px, ticker/amount 12px, wide thin sparkline (64x16),
    6px row gap, 14px HOLDINGS heading. Real data only — the
    screenshot's coins and numbers are examples, never hardcoded."""

    def test_row_is_compact_not_tall(self):
        css = _css()
        row = _rule(css, ".holdings-row")
        self.assertIn("font-size: 16px", row)
        self.assertIn("padding: 6px 8px", row)

    def test_coin_icons_are_32px(self):
        css = _css()
        ico = _rule(css, ".holdings-row .coin-ico")
        self.assertIn("flex: 0 0 32px", ico)
        self.assertIn("width: 32px", ico)
        self.assertIn("height: 32px", ico)
        img = _rule(css, ".holdings-row .coin-img")
        self.assertIn("flex: 0 0 32px", img)
        self.assertIn("width: 32px", img)
        self.assertIn("height: 32px", img)

    def test_name_and_price_are_16px(self):
        css = _css()
        self.assertIn("font-size: 16px", _rule(css, ".holdings-name"))
        self.assertIn("font-size: 16px", _rule(css, ".holdings-price"))

    def test_ticker_and_amount_are_12px(self):
        css = _css()
        self.assertIn("font-size: 12px",
                      _rule(css, ".holdings-row .coin-ticker"))
        self.assertIn("font-size: 12px", _rule(css, ".holdings-amount"))

    def test_1h_percent_is_14px(self):
        css = _css()
        self.assertIn("font-size: 14px", _rule(css, ".holdings-1h"))

    def test_sparkline_is_wide_and_thin(self):
        css = _css()
        spark = _rule(css, ".holdings-spark")
        self.assertIn("flex: 0 0 64px", spark)
        svg = _rule(css, ".holdings-spark svg")
        self.assertIn("width: 64px", svg)
        self.assertIn("height: 16px", svg)
        # The generated SVG matches (64x16 viewBox) so it is not
        # letterboxed or squished.
        made = hh.sparkline_svg([1.0, 2.0, 1.5])
        self.assertIn('width="64"', made)
        self.assertIn('height="16"', made)
        self.assertIn('viewBox="0 0 64 16"', made)

    def test_row_gap_matches_the_photo_and_no_holdings_heading(self):
        # Compact build (2026-11-11): the HOLDINGS heading is gone
        # — the coin list follows the balance box immediately — but
        # the 6px row gap stays.
        css = _css()
        self.assertIn("gap: 6px",
                      _rule(css, ".holdings-panel .coin-list"))
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("HOLDINGS", html)
        self.assertNotIn("All coins", html)


class TestSparkline1h(unittest.TestCase):
    """The 1hr sparkline helper: last hour of real prices, never
    faked, never invented."""

    class Feed:
        def __init__(self, history):
            self._history = history

        def history(self, coin_id):
            return self._history.get(coin_id, [])

    def test_last_hour_from_feed_history(self):
        # Three ticks an hour apart: only the last hour (ending at
        # the newest tick) comes back.
        feed = self.Feed({"MINT": [(1700000000, 1.0),
                                   (1700003600, 2.0),
                                   (1700007200, 3.0)]})
        self.assertEqual(
            hh.sparkline_points_1h("MINT", is_mint=True, feed=feed),
            [2.0, 3.0])

    def test_fewer_than_two_points_in_the_hour_is_empty(self):
        feed = self.Feed({"MINT": [(1700000000, 1.0)]})
        self.assertEqual(
            hh.sparkline_points_1h("MINT", is_mint=True, feed=feed), [])
        self.assertEqual(
            hh.sparkline_svg_1h("MINT", is_mint=True, feed=feed), "")

    def test_major_falls_back_to_coingecko(self):
        # No feed history: CoinGecko's 1-day chart, last hour only.
        with mock.patch.object(
                hh, "fetch_market_chart",
                return_value={"points": [
                    {"ts": 1700000000000, "price": 9.0},
                    {"ts": 1700003600000, "price": 10.0},
                    {"ts": 1700007200000, "price": 11.0}],
                    "error": None}):
            self.assertEqual(
                hh.sparkline_points_1h("SOL", feed=self.Feed({})),
                [10.0, 11.0])

    def test_svg_1h_is_a_gold_line(self):
        feed = self.Feed({"MINT": [(1700000000, 1.0),
                                   (1700003600, 2.0),
                                   (1700007200, 3.0)]})
        svg = hh.sparkline_svg_1h("MINT", is_mint=True, feed=feed)
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        self.assertIn('stroke="#ffd700"', svg)


class TestSparklinesEndpoint(HomeMockupBase):
    """GET /api/sparklines: fresh 1hr SVGs for the home refresh."""

    def _ingest(self, coin_id, prices):
        feed = self.app.config["LIVE_FEED"]
        now = time.time()
        for i, price in enumerate(prices):
            feed.ingest(coin_id, price, "test",
                        now=now - (len(prices) - 1 - i) * 60)

    def test_returns_svg_for_a_coin_with_recent_history(self):
        self._ingest("SOL", [10.0, 10.1, 10.2])
        resp = self.client.get("/api/sparklines?coins=SOL")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        svg = data["sparklines"]["SOL"]
        self.assertIn("<svg", svg)
        self.assertIn("holdings-spark-svg", svg)

    def test_returns_null_without_recent_history(self):
        with mock.patch.object(
                hh, "fetch_market_chart",
                return_value={"points": [], "error": "test"}):
            resp = self.client.get("/api/sparklines?coins=BTC")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data["sparklines"]["BTC"])

    def test_requires_login(self):
        tmp = tempfile.TemporaryDirectory()
        _app, anon = make_client(tmp.name)
        resp = anon.get("/api/sparklines?coins=SOL")
        self.assertIn(resp.status_code, (302, 401))
        tmp.cleanup()


class TestSparklinePoll(unittest.TestCase):
    """live.js refreshes the holdings sparklines every 60 seconds."""

    def test_live_js_polls_sparklines_every_minute(self):
        with open("tradingbot/static/live.js", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("SPARK_POLL_MS = 60000", js)
        self.assertIn("fetch('/api/sparklines?coins='", js)
        self.assertIn("setInterval(fetchSparklines, SPARK_POLL_MS)", js)
        self.assertIn("data-sparkline-coin", js)
        # The poll stops when the page hides, like the fees poll.
        self.assertIn("stopSparkPoll", js)
        self.assertIn("visibilitychange", js)


if __name__ == "__main__":
    unittest.main()
