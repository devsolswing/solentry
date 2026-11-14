"""Theme unification + Kraken Pro layout pass.

Rules under test:
- One theme everywhere: the red login-page theme. The stylesheet has
  no second-theme (blue/grey/green) chrome markers left, and every
  main page renders with the red theme shell (topbar + HUD).
- The coins list shows a 1h change column (from the trenches feed)
  alongside P&L; a held mint has no 24h change figure, so its row
  shows "—", never a guess. The coin popup says "24h change
  unavailable" for a mint, never a guessed number.
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

# Valid pump.fun mint addresses (base58, 32 bytes) used as test coins.
MINT = "Bond1111111111111111111111111111111111111111"
GRAD_MINT = "Grad1111111111111111111111111111111111111111"
AUDIT_MINT = "3k6ZYa2Wh6P92VN2uGvKsmHiw2RQpcVgxpb447raf2kC"
# A pump.fun mint priced at $0.01 in the mocked trenches feed.
MINT_PRICE = 0.01


class ThemeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        # The trenches feed prices the test mints; nothing here
        # reaches the live network.
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            return_value={MINT: MINT_PRICE})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.mint_1h_patch.start()
        self.mint_id_patch = mock.patch.object(
            routes, "fetch_mint_identities", return_value={})
        self.mint_id_patch.start()
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.trenches_patch.stop()
        self.meta_patch.stop()
        self.mint_id_patch.stop()
        self.mint_1h_patch.stop()
        self.mint_prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _set_prices(self):
        """Mock live prices: SOL only, at $100."""
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(100.0, "live", "test"))
        self.price_patch.start()
        self.prices_patch = mock.patch.object(routes, "get_prices",
                                              return_value={
            "prices": {"SOL": {"price": 100.0, "change_24h": 5.25,
                               "change_1h": 2.5}},
            "source": "live", "as_of": "test"})
        self.prices_patch.start()
        self.addCleanup(self.price_patch.stop)
        self.addCleanup(self.prices_patch.stop)

    def _buy(self, mint=MINT, usd="10"):
        return self.client.post(f"/coins/{mint}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{mint}"),
            "usd": usd, "bot_id": ""})

    def _trenches(self, candidates):
        """Run the block with the trenches list showing `candidates`."""
        patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": candidates, "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        self.addCleanup(patch.stop)
        return patch


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
    def test_coins_list_shows_1h_change_alongside_pnl(self):
        self._set_prices()
        # Hold the mint first so its row (and P&L) renders; then
        # move its price so the P&L is real and non-zero.
        resp = self._buy()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self._trenches([
            {"mint": GRAD_MINT, "symbol": "GRAD", "name": "Grad",
             "price_usd": 0.05, "change_1h_pct": 5.25},
            {"mint": AUDIT_MINT, "symbol": "AUDT", "name": "AuditTest",
             "price_usd": 0.02, "change_1h_pct": -2.5},
        ])
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.012}):
            html = self._html("/dashboard")
        self.assertIn("coin-change", html)
        self.assertIn("+5.2% 1h", html)
        self.assertIn("-2.5% 1h", html)
        self.assertIn("coin-pnl", html)
        # $10 at $0.01 bought 1000 tokens; at $0.012 the P&L is +$2.
        self.assertIn("+$2.00", html)
        self.assertIn('class="up"', html)
        self.assertIn('class="down"', html)

    def test_no_change_data_shows_dash_not_a_guess(self):
        self._set_prices()
        # A held mint has no 24h change figure: its row shows "—",
        # never a guessed percentage.
        self._buy()
        html = self._html("/dashboard")
        self.assertIn(
            'class="coin-change"><span class="muted">—</span>', html)
        self.assertNotIn("%</span>", html.split("coin-change")[1]
                         .split("coin-pnl")[0])

    def test_popup_shows_no_24h_change_for_mint(self):
        self._set_prices()
        html = self._html(f"/coins/{MINT}")
        self.assertIn("24h", html)
        self.assertIn("24h change unavailable", html)
        # No guessed percentage anywhere in the popup.
        self.assertNotIn("+5.2%", html)


class TestPortfolioHeader(ThemeBase):
    def test_header_shows_total_and_no_data_yet(self):
        self._set_prices()
        html = self._html("/dashboard")
        self.assertIn("portfolio-head", html)
        self.assertIn("$100.00 CAD", html)
        self.assertIn("No chart data yet", html)

    def test_header_chart_draws_from_recorded_snapshots(self):
        self._set_prices()
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
        self._set_prices()
        html = self._html(f"/coins/{MINT}")
        self.assertIn("trade-ticket", html)
        self.assertIn('id="ticket-buy"', html)
        self.assertIn('id="ticket-sell"', html)
        self.assertIn(f'action="/coins/{MINT}/buy"', html)
        self.assertIn(f'action="/coins/{MINT}/sell"', html)
        for word in ("Buy", "Sell", "Send", "Receive", "Back to coins"):
            self.assertIn(word, html)

    def test_paper_buy_still_works_from_ticket(self):
        self._set_prices()
        resp = self.client.post(f"/coins/{MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{MINT}"),
            "usd": "10", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("Back to coins", html)


if __name__ == "__main__":
    unittest.main()
