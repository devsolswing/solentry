"""Home compact (2026-11-11, owner order from his Page 3 screenshot;
scrolls, 2026-11-12 — the one-screen fit is gone, the layout is
unchanged).

Page 3 scrolls now:
- Everything lifted up (tighter top spacing).
- The balance box is about a third smaller, but the chart keeps
  its 100px height.
- Nothing under the chart: no HOLDINGS heading, no "All coins"
  link, no holdings-summary line — the coin list follows the
  balance box immediately.
- No border lines: the holdings panel and rows are borderless.
- Rows look like the Page 6 bot cards: circular icon, name over
  the ticker in small type, 1hr sparkline fitting smoothly,
  price + amount, 1h % far right.
- Real data only — the screenshot's coins and numbers are
  examples, never hardcoded.
No SOL moves in these tests; sends are never confirmed.
"""

import re
import unittest
from unittest import mock

import tradingbot.routes as routes
from test_home_mockup import HomeMockupBase


def _css():
    with open("tradingbot/static/style.css", encoding="utf-8") as f:
        return f.read()


def _rule(css, selector):
    # The LAST matching rule wins in the cascade, so return it
    # (several selectors are defined more than once, the later
    # one overriding).
    matches = re.findall(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert matches, "%s rule missing from style.css" % selector
    return matches[-1]


class TestHomeCompactLayout(HomeMockupBase):
    def test_holdings_heading_follows_the_chart(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("HOLDINGS", html)
        self.assertNotIn("All coins", html)
        self.assertNotIn("holdings-summary", html)
        # Rebuild from scratch (2026-11-11): the photo HAS a gold
        # HOLDINGS heading, so it is back — the ONLY thing between
        # the chart and the holdings panel is that heading.
        between = html.split('id="home-chart"')[1].split(
            "holdings-panel")[0]
        self.assertIn("section-title", between)
        self.assertIn("HOLDINGS", between)
        self.assertNotIn("holdings-summary", between)
        self.assertLess(html.index('id="home-chart"'),
                        html.index("holdings-panel"))

    def test_balance_box_is_smaller_but_chart_keeps_100px(self):
        css = _css()
        self.assertIn("height: 100px",
                      _rule(css, ".home-chart-wrap svg"))
        # The box is compressed: tighter padding, smaller total.
        self.assertIn("padding: 6px 12px", _rule(css, ".home-total"))
        total = _rule(css, ".home-total .portfolio-total")
        self.assertIn("clamp(24px, 4dvh, 32px)", total)
        self.assertNotIn("clamp(32px, 5dvh, 44px)", total)

    def test_holdings_rows_are_borderless(self):
        css = _css()
        panel = _rule(css, ".holdings-panel")
        self.assertIn("border: none", panel)
        self.assertIn("background: transparent", panel)
        row = _rule(css, ".holdings-row")
        self.assertIn("border: none", row)
        self.assertIn("background: transparent", row)

    def test_name_over_ticker_in_small_type(self):
        row = {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
               "price": 10.0, "change_1h": 2.5, "held": 0.9635,
               "value": 9.63, "is_mint": False}
        with mock.patch.object(routes, "_coin_rows",
                               return_value=[row]):
            self._choose_mode("paper")
            html = self._html("/wallet")
        # The ticker sits in its own small element under the name
        # (display:block), not "Name (TICKER)" on one line.
        part = html.split("holdings-row")[1]
        self.assertIn("<strong>Solana</strong>", part)
        self.assertIn("(SOL)", part)
        self.assertLess(part.index("<strong>Solana</strong>"),
                        part.index("(SOL)"))
        css = _css()
        ticker = _rule(css, ".holdings-row .coin-ticker")
        self.assertIn("display: block", ticker)
        self.assertIn("font-size: 12px", ticker)
        self.assertIn("font-size: 16px", _rule(css, ".holdings-name"))

    def test_rows_keep_icon_sparkline_price_amount_and_1h(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        # Circular icon (image when there is one, ticker-initial
        # fallback otherwise) + sparkline slot + price + amount +
        # 1h % far right. 1 SOL at the mocked $10 price, 3.65%
        # fee leaves 0.9635 SOL worth $9.63; 1h +2.50%.
        self.assertTrue("coin-ico" in html or "coin-img" in html)
        self.assertIn("holdings-spark", html)
        self.assertIn('data-sparkline-coin="SOL"', html)
        self.assertIn("$10.00", html)
        self.assertIn("0.9635 SOL", html)
        self.assertIn("holdings-1h", html)
        self.assertIn("+2.50%", html)
        # The name column takes the free space so the sparkline
        # sits in its fixed slot, vertically centred.
        css = _css()
        self.assertIn("flex: 1 1 auto",
                      _rule(css, ".holdings-row .coin-name"))
        self.assertIn("flex: 0 0 64px", _rule(css, ".holdings-spark"))
        self.assertIn("font-size: 14px", _rule(css, ".holdings-1h"))


if __name__ == "__main__":
    unittest.main()
