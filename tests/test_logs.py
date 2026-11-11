"""Logs page (Page 11, 2026-11-10) + page-number picker.

The old P&L page (/pnl) was deleted: its activity log + losing-buy
analysis moved to the dedicated Logs page, and the "Page N" chip
became a clickable picker listing every page by number + name.
Tests prove /pnl is really gone, /logs shows the log + analysis,
the homepage Log button opens it, and the picker lists every page
with the right number (11 = Logs) and loads the page it names.
No SOL moves in these tests."""

import re
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.page_numbers import PAGE_NAMES, PAGE_NUMBERS, page_list


class LogsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
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

    def _activity(self, kind, detail, when, mode="paper"):
        self.db.execute(
            "INSERT INTO activity (user_id, kind, detail, created_at, "
            "mode) VALUES (?, ?, ?, ?, ?)",
            (self.user_id, kind, detail, when, mode))
        self.db.commit()

    def _trade(self, coin, side, amount, price, when,
               exit_reason=None):
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, amount, "
            "price, total_usd, created_at, bot_id, buy_m5_pct, "
            "buy_volume_5m_usd, buy_buys_5m, buy_sells_5m, buy_regime, "
            "exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (self.user_id, coin, side, amount, price, amount * price,
             when, None, None, None, None, None, None, exit_reason))
        self.db.commit()


class TestLogsPage(LogsBase):
    def test_requires_login(self):
        self.client.get("/logout")
        resp = self.client.get("/logs")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_pnl_is_gone(self):
        # The route is deleted, not redirected: 404 logged in and
        # out, in both modes.
        self.assertEqual(self.client.get("/pnl").status_code, 404)
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "devnet"})
        self.assertEqual(self.client.get("/pnl").status_code, 404)
        self.client.get("/logout")
        self.assertEqual(self.client.get("/pnl").status_code, 404)

    def test_no_nav_or_home_link_points_to_pnl(self):
        for path in ("/wallet", "/dashboard", "/bots", "/logs",
                     "/settings", "/trenches"):
            self.assertNotIn("/pnl", self._html(path), path)

    def test_shows_activity_newest_first(self):
        self._activity("paper_buy", "Bought 1 SOL",
                       "2026-11-10T10:00:00Z")
        self._activity("paper_sell", "Sold 1 SOL",
                       "2026-11-10T10:05:00Z")
        self._activity("silent_skip", "No signal this scan",
                       "2026-11-10T10:06:00Z")
        html = self._html("/logs")
        self.assertIn("paper_buy", html)
        self.assertIn("paper_sell", html)
        self.assertIn("silent_skip", html)
        # Newest first: the 10:06 skip, then the 10:05 sell, then
        # the 10:00 buy.
        self.assertLess(html.index("silent_skip"),
                        html.index("paper_sell"))
        self.assertLess(html.index("paper_sell"),
                        html.index("paper_buy"))

    def test_shows_more_than_the_dashboard_snippet(self):
        # The dashboard shows the latest 20; the Logs page shows
        # the latest 100. 25 rows prove the fuller log.
        for i in range(25):
            self._activity("paper_buy", f"Buy number {i}",
                           f"2026-11-10T10:{i:02d}:00Z")
        html = self._html("/logs")
        self.assertIn("Buy number 0", html)
        self.assertIn("Buy number 24", html)

    def test_other_users_activity_never_shows(self):
        # A second real user (bob) with his own activity row.
        bob_client = self.app.test_client()
        signup(bob_client, "bob")
        bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO activity (user_id, kind, detail, created_at, "
            "mode) VALUES (?, 'paper_buy', 'Someone elses buy', "
            "'2026-11-10T10:00:00Z', 'paper')", (bob_id,))
        self.db.commit()
        self.assertNotIn("Someone elses buy", self._html("/logs"))

    def test_fresh_account_log_shows_the_signup_row(self):
        # Signup itself writes an activity row ("Account created;
        # app wallet generated"), so a fresh account's log is never
        # empty — the empty state ("No activity yet") is only for a
        # log with no rows at all.
        html = self._html("/logs")
        self.assertIn("signup", html)
        self.assertIn("Account created", html)

    def test_logs_page_shows_losing_buy_analysis(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 0.50, "2026-11-09T10:05:00Z",
                    exit_reason="stop_loss")
        html = self._html("/logs")
        self.assertIn("Losing-buy analysis", html)
        self.assertIn("MINTA", html)
        self.assertIn("stop loss", html)
        self.assertIn("Losing buys (1)", html)

    def test_real_mode_logs_show_no_paper_analysis(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 0.50, "2026-11-09T10:05:00Z",
                    exit_reason="stop_loss")
        self._activity("paper_buy", "Paper buy",
                       "2026-11-10T10:00:00Z", "paper")
        self._activity("real_buy", "Real buy",
                       "2026-11-10T10:01:00Z", "real")
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "devnet"})
        html = self._html("/logs")
        self.assertIn("Real buy", html)
        self.assertNotIn("Paper buy", html)
        # The losing-buy analysis is paper trades only.
        self.assertNotIn("Losing-buy analysis", html)

    def test_home_has_log_button_opening_logs(self):
        html = self._html("/wallet")
        self.assertIn('href="/logs"', html)
        self.assertIn(">Logs<", html)


class TestPagePicker(LogsBase):
    def _picker_items(self, html):
        """(href, number, name) for every entry in the rendered
        picker, in the order shown."""
        return re.findall(
            r'href="([^"]+)"[^>]*>Page (\d+) — ([^<]+)</a>', html)

    def test_picker_lists_every_page_with_number_and_name(self):
        html = self._html("/wallet")
        items = self._picker_items(html)
        self.assertEqual(len(items), len(PAGE_NUMBERS))
        by_number = {int(n): (name, href)
                     for href, n, name in items}
        for endpoint, number in PAGE_NUMBERS.items():
            self.assertIn(number, by_number)
            self.assertEqual(by_number[number][0], PAGE_NAMES[endpoint])
        # Page 11 is the Logs page — never the deleted P&L page.
        self.assertEqual(by_number[11][0], "Logs")
        self.assertTrue(all(name != "P&L" for name, _ in
                            by_number.values()))
        self.assertTrue(all(href != "/pnl" for _, href in
                            by_number.values()))

    def test_picker_entries_load_their_pages(self):
        html = self._html("/wallet")
        items = {int(n): href
                 for href, n, _ in self._picker_items(html)}
        for entry in page_list():
            href = items[entry["number"]]
            resp = self.client.get(href)
            if entry["endpoint"] in ("forgot_answer", "forgot_reset"):
                # Those two need an in-progress reset flow: without
                # one they redirect to the forgot-password page.
                # That is the designed behaviour, not a dead link.
                self.assertEqual(resp.status_code, 302, href)
                self.assertIn("/forgot-password",
                              resp.headers["Location"], href)
                continue
            self.assertEqual(resp.status_code, 200, href)
            page_html = resp.get_data(as_text=True)
            # Detail pages load their list page instead, so the
            # loaded page carries the TARGET's number.
            target_number = PAGE_NUMBERS[entry["target"]]
            self.assertIn(f"Page {target_number}", page_html, href)

    def test_picker_works_on_the_login_page_too(self):
        self.client.get("/logout")
        html = self._html("/login")
        items = self._picker_items(html)
        self.assertEqual(len(items), len(PAGE_NUMBERS))
        self.assertEqual({int(n) for _, n, _ in items},
                         set(PAGE_NUMBERS.values()))
        # A page that needs login redirects to login, as today.
        resp = self.client.get("/logs")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
