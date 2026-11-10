"""Fox-layout UI rebuild: login first, home, bots, trenches pages.

Rules under test:
- / is not a landing page: logged-out users are sent to /login,
  logged-in users to /wallet.
- The home page (wallet) is the fox layout: fox logo, TOTAL
  PORTFOLIO VALUE, 24h change, chart range selector, BUY / SELL
  buttons that open the buy/sell flow, HOLDINGS list (coins held
  only, in a fixed-height panel that scrolls internally), a Trenches
  card, a Logs button, and a four-button bottom nav (Home / Bots /
  Wallet / Settings) with the mode switch in the topbar.
- The bots page is the fox layout: SOL balance + USD with an eye
  (hide/show), fox logo, + create, - delete (server-rendered
  confirm, never a native confirm()), Edit Bot, an Active Bots
  carousel with < > arrows, and bot cards showing real fields only
  (name, silent status, id, created date, strategy, buy size, stop
  loss) — no per-bot 24H PNL, which the app cannot compute.
- The trenches page is a NEW page with its own black/gold theme and
  the fox-gold logo; it lists the top 10 by 1h gain, each row opening
  the same Buy / Sell / Sell all popup.
- Every page fits one screen: the page itself never scrolls; lists
  scroll only inside fixed-height panels.
No SOL moves in these tests; real execution is a recording mock.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.solana import generate_keypair


def make_prices(changes):
    """changes: {coin: (price, change_24h)}"""
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_recent_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def fake_trenches(mint):
    return {"candidates": [{
        "mint": mint, "symbol": "MOON", "name": "Moon Coin",
        "price_usd": 0.00123, "change_1h_pct": 12.5,
        "change_5m_pct": 4.0, "change_24h_pct": 30.0,
        "liquidity_usd": 50000, "market_cap_usd": 120000,
        "dex_id": "pumpfun", "pair_address": "pair1",
        "pair_created_at": 1790000000000}],
        "prices": {}, "source": "dexscreener", "as_of": "test",
        "error": None}


class UiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25), "USDC": (1.0, 0.0),
                "DOGE": (0.07, 3.0), "PEPE": (0.000005, -1.0),
                "BONK": (0.000003, 8.0), "JUP": (0.23, -0.5)}))
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

    def tearDown(self):
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _reset_trenches_cache(self):
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _buy_sol(self, usd="10"):
        return self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": usd, "bot_id": ""})

    def _create_bot(self, name="Luna", strategy="bold"):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": strategy, "coins": "SOL, BTC",
            "buy_size_pct": "25", "stop_loss_pct": "5"})
        self.assertEqual(resp.status_code, 200)
        return self.db.execute(
            "SELECT * FROM bots WHERE name = ?", (name,)).fetchone()


class TestLandingPageGone(UiBase):
    """The old landing page is deleted entirely, not bypassed.

    James's 2026-11-09 screenshot showed it live: "Trade with a bot.
    Your call." with Paper Trading / Real SOL Trading cards, an
    "Open your wallet" button, and the old five-tab nav. It is ugly
    and unnecessary — the URL goes straight to the login screen.
    No part of that page survives: no template, no route rendering
    it, none of its copy anywhere in the app.
    """
    LANDING_COPY = ("Trade with a bot", "Your call.",
                    "Paper Trading", "Real SOL Trading",
                    "Open your wallet")

    def test_root_goes_straight_to_login_logged_out(self):
        self.client.get("/logout")
        resp = self.client.get("/", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request.path, "/login")
        html = resp.get_data(as_text=True)
        for phrase in self.LANDING_COPY:
            self.assertNotIn(phrase, html)
        # The login screen is the first screen — no bottom nav on it.
        self.assertNotIn("hud-item", html)

    def test_root_goes_straight_to_wallet_logged_in(self):
        resp = self.client.get("/", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request.path, "/wallet")
        html = resp.get_data(as_text=True)
        for phrase in self.LANDING_COPY:
            self.assertNotIn(phrase, html)

    def test_no_landing_template_left(self):
        import os
        self.assertFalse(
            os.path.exists("tradingbot/templates/index.html"),
            "index.html must be deleted, not left on disk")

    def test_no_landing_copy_anywhere_in_templates(self):
        import glob
        for path in glob.glob("tradingbot/templates/*.html"):
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            for phrase in self.LANDING_COPY:
                self.assertNotIn(phrase, html, f"{path} still has it")


class TestIndexRedirect(UiBase):
    def test_logged_out_root_goes_to_login(self):
        self.client.get("/logout")
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_logged_in_root_goes_to_wallet(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_logout_goes_to_login(self):
        resp = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_login_page_keeps_passkey_forgot_and_donate(self):
        self.client.get("/logout")
        html = self._html("/login")
        self.assertIn("passkey-login-btn", html)
        self.assertIn("Forgot password?", html)
        self.assertIn("donate-connect", html)
        # The dropdowns from the screenshot stay removed.
        self.assertNotIn("Select Token", html)
        self.assertNotIn("Select Network", html)


class TestHomePage(UiBase):
    def test_home_has_fox_layout(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("TOTAL PORTFOLIO VALUE", html)
        self.assertIn("fox-logo", html)
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertIn("SolEntry fox logo", html)
        self.assertIn("HOLDINGS", html)
        self.assertIn("holdings-panel", html)
        self.assertIn("Support ID", html)
        self.assertIn("Paper balance", html)
        self.assertIn("Trenches", html)
        self.assertIn('href="/trenches"', html)
        self.assertIn("Logs", html)
        self.assertIn('href="/dashboard#activity"', html)

    def test_home_buy_sell_buttons_open_quick_trade(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("BUY", html)
        self.assertIn("SELL", html)
        self.assertIn("data-quick-trade-open", html)
        self.assertIn('data-trade="buy"', html)
        self.assertIn('data-trade="sell"', html)
        self.assertIn('id="quick-trade-overlay"', html)

    def test_home_nav_is_four_buttons(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertEqual(html.count("hud-item"), 4)
        for href, label in (("/wallet", "Home"), ("/bots", "Bots"),
                            ("/wallet/details", "Wallet"),
                            ("/settings", "Settings")):
            self.assertIn(f'href="{href}"', html, href)
            self.assertIn(f">{label}</span>", html, label)
        # The old five-tab labels are gone from the nav.
        self.assertNotIn("Coins</a>", html)
        self.assertNotIn("P&amp;L</a>", html)
        # The mode switch lives in the topbar, not a fifth tab.
        self.assertIn('action="/mode"', html)

    def test_home_nav_links_all_resolve(self):
        self._choose_mode("paper")
        for path in ("/wallet", "/bots", "/wallet/details", "/settings",
                     "/pnl", "/dashboard", "/trenches"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)

    def test_home_is_one_screen(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("fit-screen", html)
        css = open("tradingbot/static/style.css").read()
        self.assertIn("body.app-shell.fit-screen .wrap", css)
        self.assertIn("overflow: hidden", css)
        self.assertIn(".holdings-panel", css)

    def test_home_chart_moved_to_pnl_no_range_switches(self):
        # Home lift: the portfolio chart (and its range switches)
        # moved off the home page to make room for the half-screen
        # holdings list. The full chart lives on the P&L page.
        self._choose_mode("paper")
        html = self._html("/wallet?range=1h")
        self.assertNotIn('href="/wallet?range=1h"', html)
        self.assertNotIn("chart-ranges", html)
        self.assertNotIn("portfolio-chart", html)
        # The "Full chart ›" link opens the P&L page.
        self.assertIn("Full chart ›", html)
        self.assertIn('href="/pnl"', html)

    def test_home_24h_change_dash_without_snapshots(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("change-24h", html)
        # No recorded snapshots yet: no invented change. The chart
        # itself lives on the P&L page now, not on home.
        self.assertIn("—", html)
        self.assertNotIn("No chart data yet", html)

    def test_home_24h_change_from_snapshot(self):
        self._choose_mode("paper")
        stamp = (datetime.now(timezone.utc)
                 - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
            "holdings_value_usd, total_value_usd, real_sol, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.user_id, 40.0, 0.0, 40.0, None, stamp))
        self.db.commit()
        html = self._html("/wallet")
        # Paper total is $100 CAD; the snapshot 23h ago was $40.
        self.assertIn("+$60.00", html)
        self.assertIn("(+150.00%)", html)

    def test_home_holdings_rows_link_to_detail(self):
        # Home lift: holdings rows are normal-size coin buttons
        # (name + ticker, amount held, value, 1h %) — the 24h % and
        # P&L columns live on the coins page and the coin detail
        # page now.
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("HOLDINGS", html)
        self.assertIn("Solana", html)
        self.assertIn("SOL", html)
        self.assertIn("holdings-row", html)
        self.assertIn("1.0000 SOL", html)
        self.assertIn("$10.00", html)
        self.assertNotIn("coin-pnl", html)
        self.assertNotIn("coin-change", html)
        self.assertIn("/coins/SOL/detail", html)


class TestBotsPage(UiBase):
    def test_bots_page_fox_layout(self):
        self._choose_mode("paper")
        bot = self._create_bot()
        html = self._html("/bots")
        self.assertIn("data-balance", html)
        self.assertIn("data-eye-toggle", html)
        self.assertIn("fox-logo", html)
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertIn('href="/bots/create"', html)
        self.assertIn(f'href="/bots/{bot["id"]}/delete/confirm"', html)
        self.assertIn(f'href="/bots/{bot["id"]}/edit"', html)
        self.assertIn("Edit Bot", html)
        self.assertIn("bot-carousel", html)
        self.assertIn("data-carousel-prev", html)
        self.assertIn("data-carousel-next", html)
        self.assertIn("Active Bots • 1", html)
        # The card shows real fields only.
        self.assertIn("Luna", html)
        self.assertIn(f"ID: {bot['id']}", html)
        self.assertIn("Created:", html)
        # Strategy is no longer picked on the form (builderBot builds
        # bots now): every bot created through the form is "balanced".
        self.assertIn("balanced", html)
        self.assertIn("Buy 25.0%", html)
        self.assertIn("Stop 5.0%", html)
        # No per-bot 24H PNL: the app cannot compute it.
        self.assertNotIn("24H PNL", html)
        self.assertNotIn("24H P&amp;L", html)
        # Card taps through to the bot detail page.
        self.assertIn(f'href="/bots/{bot["id"]}"', html)
        self.assertIn("Trenches", html)
        self.assertIn('href="/trenches"', html)
        self.assertIn("Logs", html)

    def test_bots_page_no_bots_state(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("No bots yet", html)
        self.assertIn("Active Bots • 0", html)
        # + still creates; - and Edit are disabled, never dead links.
        self.assertIn('href="/bots/create"', html)
        self.assertIn("disabled", html)

    def test_bots_delete_goes_to_server_confirm_page(self):
        self._choose_mode("paper")
        bot = self._create_bot()
        html = self._html(f"/bots/{bot['id']}/delete/confirm")
        self.assertIn("Delete", html)
        self.assertIn("Luna", html)

    def test_bots_page_is_one_screen(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("fit-screen", html)
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".bot-carousel", css)
        self.assertIn(".page-scroll-panel", css)


class TestTrenchesPage(UiBase):
    def test_trenches_page_black_gold_theme(self):
        self._choose_mode("paper")
        html = self._html("/trenches")
        self.assertIn("trenches-theme", html)
        self.assertIn("fox-logo", html)
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertIn("Trenches", html)
        self.assertIn("Back home", html)
        self.assertIn("No trenches tokens right now.", html)
        css = open("tradingbot/static/style.css").read()
        self.assertIn("body.trenches-theme", css)

    def test_trenches_page_lists_top10_with_price_and_1h_gain(self):
        mint = generate_keypair()[1]
        self.trenches_patch.stop()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value=fake_trenches(mint))
        self.trenches_patch.start()
        self._reset_trenches_cache()
        html = self._html("/trenches")
        self.assertIn("Moon Coin", html)
        self.assertIn("MOON", html)
        self.assertIn("$0.0012", html)
        self.assertIn("+12.5% 1h", html)
        self.assertIn(f"/coins/{mint}", html)

    def test_trenches_feed_failure_says_unavailable(self):
        self.trenches_patch.stop()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": "feed down"})
        self.trenches_patch.start()
        self._reset_trenches_cache()
        html = self._html("/trenches")
        self.assertIn("Trenches unavailable right now", html)

    def test_trenches_reachable_from_home_and_bots(self):
        self._choose_mode("paper")
        self.assertIn('href="/trenches"', self._html("/wallet"))
        self.assertIn('href="/trenches"', self._html("/bots"))


class TestWalletDetails(UiBase):
    def test_details_paper_shows_paper_balance_and_door_art(self):
        self._choose_mode("paper")
        html = self._html("/wallet/details")
        self.assertIn("Paper balance", html)
        self.assertIn("Dogewifhat walking through a door", html)
        self.assertIn("🇨🇦", html)
        self.assertNotIn("Send history", html)

    def test_details_real_shows_deposit_and_platforms(self):
        self._choose_mode("devnet")
        html = self._html("/wallet/details")
        self.assertIn("Deposit", html)
        self.assertIn("Platform wallets", html)
        self.assertIn("Send history", html)
        self.assertIn("Pump.fun trading wallet", html)

    def test_home_keeps_flag_but_not_wallet_sections(self):
        self._choose_mode("devnet")
        html = self._html("/wallet")
        self.assertIn("🇨🇦", html)
        self.assertNotIn("Send history", html)
        self.assertNotIn("Platform wallets", html)


class TestQuickTradeShared(UiBase):
    def test_quick_trade_overlay_on_home_and_dashboard(self):
        self._choose_mode("paper")
        for path in ("/wallet", "/dashboard"):
            html = self._html(path)
            self.assertIn('id="quick-trade-overlay"', html, path)
            self.assertIn("Buy or Sell Coin", html, path)

    def test_quick_trade_js_served(self):
        for path in ("/static/quick_trade.js", "/static/ui.js"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
        qt = self.client.get("/static/quick_trade.js").get_data(as_text=True)
        self.assertIn("quick-trade-overlay", qt)
        ui = self.client.get("/static/ui.js").get_data(as_text=True)
        self.assertIn("data-eye-toggle", ui)
        self.assertIn("data-carousel-next", ui)
        self.assertIn("data-go-back", ui)


if __name__ == "__main__":
    unittest.main()
