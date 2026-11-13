"""Home rebuild from scratch (owner, 2026-11-11 ~19:39 PST).

Page 3 (tradingbot/templates/wallet.html) was rewritten from
scratch to match the owner's original home mockup photo exactly —
header, four action buttons, BALANCE box, HOLDINGS heading + rows,
bottom nav — and every button goes to the page it belongs to:
each holdings row taps through to that coin's info page, BUY/SELL
open the quick-trade flow, Send goes to the Send page, Receive
opens the address window, and the four bottom-nav buttons go to
Home / Bots / Wallet / Settings.

The original build is saved as a backup at
workspace/solentry-home-backup/2026-11-11-b3d1afc0/ (wallet.html,
style.css, live.js, routes.py from main HEAD b3d1afc0 + home test
files + README) — the rebuild never touches it.

REAL DATA ONLY: the photo's numbers (2.45 SOL, $185.20 CAD,
+5.82%, Pepe / Bitcoin / Solana / Dogecoin / Ethereum) are the
mockup's examples, never the user's data — no test here (and no
template) may hardcode them. Holdings come from the route's real
coin_rows; a missing price is "—", never invented.
No SOL moves in these tests; sends are never confirmed.

2026-11-12 (owner): the fees card moved off Page 3 to the wallet
page (/wallet/details), and Page 3 scrolls (the one-screen fit is
gone) — the photo layout is unchanged. The signing-bot info STAYS
on Page 3 (James said no to removing it, 2026-11-12); it is also
on Page 6, in the Signer Bot card.

2026-11-12 (owner): the trading fee was REMOVED entirely — the
fees card is gone from the wallet page too, and the Signer Bot
card keeps Signer ID + signed-transaction count but no fee line.
"""

import re
import tempfile
import unittest
from unittest import mock

from flask import render_template

import tradingbot.home_holdings as hh
import tradingbot.routes as routes
from helpers import make_client, signup
from test_home_mockup import HomeMockupBase

# The mockup photo's figures — examples only, never the user's data.
MOCKUP_NUMBERS = ("2.45 SOL", "$185.20", "+5.82%", "0.0731 SOL",
                  "$84,652.15", "0.9987 SOL", "$3,450.00",
                  "$0.00001234")
MOCKUP_COINS = ("Pepe", "Dogecoin", "Ethereum")


def _direct_ctx(coin_rows):
    # The template context routes.py passes for the wallet page,
    # with arbitrary test values (deliberately NOT the photo's).
    return {
        "mode": "devnet", "mode_label": "Real trading — devnet",
        "paper_cash": 100.0, "portfolio_usd": 106.30,
        "portfolio_sol": 1.2345,
        "change_24h": {"delta": 0.04, "pct": 3.25},
        "chart_range": "1d",
        "home_range_labels": {"1h": "1h", "1d": "1d", "1w": "1w",
                              "1mo": "1m", "1y": "1y", "all": "All"},
        "portfolio_candles_svg": "", "portfolio_line_svg": "",
        "coin_rows": coin_rows, "holdings_value": 0.0,
        "sol_price_usd": 86.14, "support_id": "12345678",
        "receive_address": "11111111111111111111111111111111",
        "bots": [],
    }


class TestHomeRebuildPhotoOrder(HomeMockupBase):
    """The rebuilt page follows the photo top to bottom: header,
    four action buttons, BALANCE box, HOLDINGS heading + rows,
    bottom nav — with nothing above the header."""

    def test_sections_in_photo_order(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        order = ("home-logo", "home-actions-row", "home-total",
                 "HOLDINGS", "holdings-panel", "home-logs-link",
                 "trenches-card")
        indices = [html.index(part) for part in order]
        self.assertEqual(indices, sorted(indices), order)

    def test_nothing_above_the_header(self):
        # The photo has nothing above the fox + SolEntry header —
        # no top bar, no back button, no Logs link up top.
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn('class="home-top"', html)
        self.assertLess(html.index("home-logo"),
                        html.index("home-actions-row"))

    def test_holdings_heading_is_back(self):
        # Rebuild from scratch (2026-11-11): the photo HAS a gold
        # HOLDINGS heading, so it is back — but the "All coins"
        # link and the holdings-summary line stay gone.
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("HOLDINGS", html)
        self.assertIn('class="section-title">HOLDINGS', html)
        self.assertNotIn("All coins", html)
        self.assertNotIn("holdings-summary", html)

    def test_logs_link_is_below_the_holdings(self):
        # Logs moved out of the top (the photo has nothing above
        # the header) to below the holdings. (base.html also has
        # a page-picker link to /logs — that is not this button.)
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn('class="btn ghost small home-logs-link"', html)
        self.assertGreater(html.index("home-logs-link"),
                           html.index("holdings-panel"))


class TestHomeRebuildNavigation(HomeMockupBase):
    """Every button on the rebuilt page goes to the page it
    belongs to — scanned from the photo."""

    def test_every_holdings_row_links_to_its_coin_page(self):
        # A real paper buy: the SOL row taps through to the coin
        # detail page, not a popup.
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("/coins/SOL/detail", html)
        # The row itself is a link to the coin page — not a popup
        # trigger.
        row = html.split('class="coin-row holdings-row"')[1].split("</a>")[0]
        self.assertIn("/coins/SOL/detail", row)
        self.assertNotIn("data-popup", row)

    def test_fabricated_rows_each_link_to_their_coin_page(self):
        # Two fabricated rows (a mint + SOL): EVERY row is a
        # button to its own coin's info page.
        rows = [
            {"coin_id": "PEPE", "name": "Pepe", "ticker": "PEPE",
             "price": 0.00000731, "change_1h": -1.23,
             "held": 123456.789, "value": 0.90, "is_mint": True,
             "image_url": "https://dd.dexscreener.com/pepe.png",
             "sparkline_svg": hh.sparkline_svg([1.0, 0.9, 1.1])},
            {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
             "price": 10.0, "change_1h": 2.5, "held": 0.9635,
             "value": 9.63, "is_mint": False,
             "image_url": None, "sparkline_svg": ""},
        ]
        with mock.patch.object(routes, "_coin_rows",
                               return_value=rows):
            self._choose_mode("paper")
            html = self._html("/wallet")
        self.assertIn("/coins/PEPE/detail", html)
        self.assertIn("/coins/SOL/detail", html)
        # The Pepe row shows its real (fabricated) data, never
        # the photo's mockup price.
        self.assertIn("Pepe", html)
        self.assertIn("(PEPE)", html)
        self.assertIn("$0.00000731", html)
        self.assertIn("-1.23%", html)
        self.assertNotIn("$0.00001234", html)

    def test_row_layout_order_is_icon_name_chart_total_1h(self):
        # James's order: coin icon, name with ticker underneath,
        # 1hr line chart next to the name, total (price + amount)
        # next to that, 1h % P&L far right.
        rows = [
            {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
             "price": 10.0, "change_1h": 2.5, "held": 0.9635,
             "value": 9.63, "is_mint": False,
             "image_url": "https://assets.coingecko.com/sol.png",
             "sparkline_svg": hh.sparkline_svg([9.0, 10.0, 11.0])},
        ]
        with mock.patch.object(routes, "_coin_rows",
                               return_value=rows):
            self._choose_mode("paper")
            html = self._html("/wallet")
        row = html.split("holdings-row")[1]
        order = ("coin-img", "holdings-name", "holdings-spark",
                 "holdings-price-block", "holdings-1h")
        indices = [row.index(part) for part in order]
        self.assertEqual(indices, sorted(indices), order)
        # The ticker sits underneath the name in small type.
        self.assertIn("(SOL)", row)

    def test_bottom_nav_goes_to_each_page(self):
        # The four bottom buttons (the <nav class="hud"> in
        # base.html): Home -> /wallet (Page 3), Bots -> /bots
        # (Page 6), Wallet -> /wallet/details, Settings ->
        # /settings.
        self._choose_mode("paper")
        html = self._html("/wallet")
        nav = html.split('class="hud"')[1].split("</nav>")[0]
        self.assertIn('href="/wallet"', nav)
        self.assertIn('href="/bots"', nav)
        self.assertIn('href="/wallet/details"', nav)
        self.assertIn('href="/settings"', nav)
        for label in ("Home", "Bots", "Wallet", "Settings"):
            self.assertIn(label, nav)
        # Home is the active tab on the home page.
        self.assertIn('hud-item active', nav)

    def test_action_buttons_still_wired(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn('data-trade="buy"', html)
        self.assertIn('data-trade="sell"', html)
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)


class TestHomeRebuildRealData(unittest.TestCase):
    """The rebuilt template renders whatever the route passes and
    hardcodes nothing — arbitrary test values, deliberately NOT
    the photo's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_direct_render_never_shows_mockup_numbers(self):
        rows = [
            {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
             "price": 10.0, "change_1h": 2.5, "held": 0.9635,
             "value": 9.63, "is_mint": False,
             "image_url": None, "sparkline_svg": ""},
        ]
        with self.app.test_request_context("/wallet"):
            html = render_template("wallet.html",
                                   **_direct_ctx(rows))
        self.assertIn("1.2345 SOL", html)
        self.assertIn("≈ $106.30 CAD", html)
        self.assertIn("+3.25%", html)
        self.assertIn("HOLDINGS", html)
        self.assertIn("/coins/SOL/detail", html)
        for fake in MOCKUP_NUMBERS:
            self.assertNotIn(fake, html)
        for fake_coin in MOCKUP_COINS:
            self.assertNotIn(fake_coin, html)

    def test_missing_price_is_a_dash_never_a_guess(self):
        rows = [
            {"coin_id": "PEPE", "name": "Pepe", "ticker": "PEPE",
             "price": None, "change_1h": None,
             "held": 1000.0, "value": None, "is_mint": True,
             "image_url": None, "sparkline_svg": ""},
        ]
        with self.app.test_request_context("/wallet"):
            html = render_template("wallet.html",
                                   **_direct_ctx(rows))
        self.assertIn("/coins/PEPE/detail", html)
        self.assertIn("—", html)
        self.assertNotIn("$0.00", html)


class TestHomeFeesMoveScrollable(HomeMockupBase):
    """2026-11-12 (owner): the fees card moved off Page 3 to the
    wallet page (/wallet/details), and Page 3 scrolls — the photo
    layout is unchanged. The signing-bot info stays on Page 3."""

    def test_fees_card_is_gone_from_home(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("fees-card", html)
        self.assertNotIn("Fees paid to SolEntry", html)
        self.assertNotIn("Pump.fun fees", html)
        self.assertNotIn("data-fees-user-total", html)
        self.assertNotIn("data-fees-user-pump-total", html)
        self.assertNotIn("data-fees-user-burn-total", html)
        self.assertNotIn("fees-switcher", html)
        self.assertNotIn("Burned:", html)

    def test_support_id_stays_on_home(self):
        # The signing-bot info (the support ID IS the Signer ID)
        # stays on Page 3 — James said no to removing it
        # (2026-11-12). It is also on Page 6, in the Signer Bot
        # card, but Page 3 keeps its line.
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("Support ID", html)
        self.assertIn("Your signing bot", html)

    def test_home_is_scrollable(self):
        # Page 3 scrolls (owner, 2026-11-12): no fit-screen shell,
        # the wrap scrolls, and the holdings panel grows with the
        # list instead of scrolling internally.
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertNotIn("fit-screen", html)
        css = open("tradingbot/static/style.css").read()
        self.assertIn(
            "body.app-shell .wrap { flex: 1 1 auto; "
            "overflow-y: auto;", css)
        panel = _last_rule(css, ".holdings-panel")
        self.assertNotIn("overflow-y", panel)
        self.assertNotIn("flex:", panel)
        # The photo layout is unchanged by the scroll.
        self.assertIn("home-logo", html)
        self.assertIn("home-total", html)
        self.assertIn("HOLDINGS", html)
        self.assertIn("holdings-panel", html)

    def test_fees_card_is_gone_from_wallet_details(self):
        # The trading fee was removed (James, 2026-11-12): the
        # whole fees card is gone from the wallet page, in paper
        # mode and in real mode — no card, no dropdown switcher,
        # no burned line, no fee figures.
        for mode in ("paper", "devnet"):
            self._choose_mode(mode)
            html = self._html("/wallet/details")
            self.assertNotIn('id="fees"', html)
            self.assertNotIn("fees-card", html)
            self.assertNotIn("Fees paid to SolEntry", html)
            self.assertNotIn("Pump.fun fees", html)
            self.assertNotIn("SolEntry fee balance", html)
            self.assertNotIn("Pump.fun fee balance", html)
            self.assertNotIn("Burned:", html)
            self.assertNotIn("data-fees-user-total", html)
            self.assertNotIn("data-fees-user-pump-total", html)
            self.assertNotIn("data-fees-user-burn-total", html)
            self.assertNotIn("fees-switcher", html)

    def test_signer_info_is_on_bots_page(self):
        # Page 6 already had it (Signer Bot card, Page 6 rework):
        # Signer ID and signed-transaction count. No fee line: the
        # trading fee was removed (James, 2026-11-12).
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("Signer Bot", html)
        self.assertIn("Signer ID:", html)
        self.assertIn("Transactions signed:", html)
        self.assertNotIn("Gas fees paid:", html)


def _last_rule(css, selector):
    # The LAST matching rule wins in the cascade, so return it.
    matches = re.findall(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert matches, "%s rule missing from style.css" % selector
    return matches[-1]


class TestHomeRebuildCss(unittest.TestCase):
    """The consolidated home CSS block landed: the rebuild's
    single source of truth sits at the end of style.css and the
    superseded "Home compact" block is gone."""

    def test_rebuild_css_block_is_present(self):
        css = open("tradingbot/static/style.css").read()
        self.assertIn("Home rebuild from scratch", css)
        self.assertIn(".home-logo { display: flex;", css)
        self.assertIn(
            ".home-screen .section-title { margin: 6px 0 4px; "
            "font-size: 14px; border-bottom: none;",
            css)

    def test_old_home_compact_block_is_gone(self):
        css = open("tradingbot/static/style.css").read()
        self.assertNotIn("Home compact", css)

