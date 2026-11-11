"""Theme unification + Kraken Pro layout pass.

Rules under test:
- One theme everywhere: the red login-page theme. The stylesheet has
  no second-theme (blue/grey/green) chrome markers left, and every
  main page renders with the red theme shell (topbar + HUD).
- Coins list shows a 24h change column alongside P&L; the coin popup
  shows it too. No change data (snapshot) shows "—", never a guess.
- Portfolio header on the coins page: total balance + chart from
  recorded snapshots only ("no data yet" until two snapshots exist).
- The coin popup's Buy/Sell is a trade ticket with a Buy/Sell switch
  over the same forms and endpoints as before.
No SOL moves in these tests.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup

# The old in-app theme's chrome colours. None of these may remain in
# the stylesheet — green/red survive only as data colours (.up/.down).
SECOND_THEME_MARKERS = (
    "#0b0e14", "#11151f", "#141926", "#16a34a", "#1a2030", "#232a38",
    "#2b3446", "#9aa7b8",
)


def create_coin_wallet(client, coin):
    """Tap "Create wallet" for a coin (the first-trade wallet step):
    records the user's Solana app wallet for the coin so Buy works.
    The gate refuses any Buy before this tap, so tests that buy a
    non-SOL coin must create its wallet first."""
    resp = client.post(f"/api/coins/{coin}/wallet", data={
        "csrf_token": csrf(client, f"/coins/{coin}")})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["ok"], data
    return data


class ThemeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _set_prices(self, changes):
        """Mock live prices with the given {coin: change_24h}."""
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "live", "test"))
        self.price_patch.start()
        self.prices_patch = mock.patch.object(routes, "get_prices",
                                              return_value={
            "prices": {c: {"price": 100.0,
                           "change_24h": changes.get(c, 0.0)}
                       for c in ["BTC", "ETH", "SOL", "USDC", "DOGE",
                                 "PEPE", "BONK", "JUP"]},
            "source": "live", "as_of": "test"})
        self.prices_patch.start()
        self.addCleanup(self.price_patch.stop)
        self.addCleanup(self.prices_patch.stop)


class TestOneTheme(ThemeBase):
    def test_no_second_theme_chrome_in_stylesheet(self):
        with open("tradingbot/static/style.css") as f:
            css = f.read()
        for marker in SECOND_THEME_MARKERS:
            self.assertNotIn(marker, css,
                             f"second-theme chrome {marker} still in CSS")
        for token in (".app-shell", ".hud", ".coin-list", ".coin-row",
                      ".overlay", ".chooser-card", ".coin-popup",
                      ".mode-banner", ".platform-row",
                      ".portfolio-head", ".coin-change", ".trade-ticket",
                      ".ticket-switch", ".up", ".down"):
            self.assertIn(token, css, f"missing {token} in CSS")
        # The red theme itself is present: page gradient, panel, gold.
        for token in ("#2a0705", "#160b08", "#ffb300", "#ffd700"):
            self.assertIn(token, css, f"red theme {token} missing")

    def test_chart_svg_uses_theme_colours(self):
        with open("tradingbot/pnl.py") as f:
            src = f.read()
        self.assertNotIn("#22c55e", src)
        self.assertIn('stroke="#ffd700"', src)

    def test_main_pages_render_with_theme_shell(self):
        for path in ("/dashboard", "/bots", "/wallet", "/logs",
                     "/settings"):
            html = self._html(path)
            self.assertIn('class="topbar"', html, path)
            self.assertIn("hud-item", html, path)


class TestChange24h(ThemeBase):
    def test_coins_list_shows_24h_change_alongside_pnl(self):
        self._set_prices({"SOL": 5.25, "BTC": -2.5})
        # The coins list shows held coins only — hold both coins
        # first so their rows (and 24h changes) render.
        for coin in ("SOL", "BTC"):
            if coin != "SOL":
                # The first-trade wallet step: a non-SOL Buy is
                # refused until the coin's wallet is created.
                create_coin_wallet(self.client, coin)
            self.client.post(f"/coins/{coin}/buy", data={
                "csrf_token": csrf(self.client, f"/coins/{coin}"),
                "usd": "10", "bot_id": ""})
        html = self._html("/dashboard")
        self.assertIn("coin-change", html)
        self.assertIn("+5.2%", html)
        self.assertIn("-2.5%", html)
        self.assertIn("coin-pnl", html)
        self.assertIn('class="up"', html)
        self.assertIn('class="down"', html)

    def test_no_change_data_shows_dash_not_a_guess(self):
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "snapshot", "test"))
        self.price_patch.start()
        self.prices_patch = mock.patch.object(routes, "get_prices",
                                              return_value={
            "prices": {c: {"price": 100.0, "change_24h": None}
                       for c in ["BTC", "ETH", "SOL", "USDC", "DOGE",
                                 "PEPE", "BONK", "JUP"]},
            "source": "snapshot", "as_of": "test"})
        self.prices_patch.start()
        self.addCleanup(self.price_patch.stop)
        self.addCleanup(self.prices_patch.stop)
        # The coins list shows held coins only — hold one first so
        # its row (with no change data) renders at all.
        self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": ""})
        html = self._html("/dashboard")
        self.assertIn(
            'class="coin-change"><span class="muted">—</span>', html)
        self.assertNotIn("%</span>", html.split("coin-change")[1]
                         .split("coin-pnl")[0])

    def test_popup_shows_24h_change(self):
        self._set_prices({"SOL": 5.25})
        html = self._html("/coins/SOL")
        self.assertIn("24h", html)
        self.assertIn("+5.2%", html)


class TestPortfolioHeader(ThemeBase):
    def test_header_shows_total_and_no_data_yet(self):
        self._set_prices({})
        html = self._html("/dashboard")
        self.assertIn("portfolio-head", html)
        self.assertIn("$100.00 CAD", html)
        self.assertIn("No chart data yet", html)

    def test_header_chart_draws_from_recorded_snapshots(self):
        self._set_prices({})
        for total, when in ((10000.0, "2026-11-09T10:00:00Z"),
                            (10500.0, "2026-11-09T11:00:00Z")):
            self.db.execute(
                "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
                "holdings_value_usd, total_value_usd, real_sol, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.user_id, 10000.0, total - 10000.0, total, None,
                 when))
        self.db.commit()
        html = self._html("/dashboard")
        self.assertIn("portfolio-chart", html)
        self.assertIn("<svg", html)
        self.assertIn("$10,500.00", html)
        self.assertNotIn("No chart data yet", html)


class TestTradeTicket(ThemeBase):
    def test_popup_has_ticket_switch_over_same_forms(self):
        self._set_prices({})
        html = self._html("/coins/SOL")
        self.assertIn("trade-ticket", html)
        self.assertIn('id="ticket-buy"', html)
        self.assertIn('id="ticket-sell"', html)
        self.assertIn('action="/coins/SOL/buy"', html)
        self.assertIn('action="/coins/SOL/sell"', html)
        for word in ("Buy", "Sell", "Send", "Receive", "Back to coins"):
            self.assertIn(word, html)

    def test_paper_buy_still_works_from_ticket(self):
        self._set_prices({})
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("Back to coins", html)


if __name__ == "__main__":
    unittest.main()
