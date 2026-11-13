"""Page numbers: every full page carries its Page ID in the top-left
corner, and the numbering lives in one dict (page_numbers).

Also covers the Bots-page duplicate: one Trenches bot plus one
Scanner bot must render exactly one card each — the "Trenches"
title must not appear twice as a card. (Page 6 rebuild: the lower
scrolling section that held the Trenches link and the "+ Create
the Trenches bot" button is gone; the Trenches bot is created
through the assistant now.)
"""

import re
import tempfile
import unittest
from pathlib import Path

from helpers import csrf, login, make_client, signup
from tradingbot.page_numbers import (PAGE_ALIASES, PAGE_NUMBERS,
                                     page_number_for)

TEMPLATES = Path(__file__).resolve().parent.parent / "tradingbot" / "templates"


class TestPageNumberSource(unittest.TestCase):
    def test_numbers_unique_and_contiguous(self):
        values = sorted(PAGE_NUMBERS.values())
        self.assertEqual(values, list(range(1, len(values) + 1)))

    def test_every_numbered_endpoint_is_a_real_route(self):
        tmp = tempfile.TemporaryDirectory()
        app, _client = make_client(tmp.name)
        try:
            for endpoint in list(PAGE_NUMBERS) + list(PAGE_ALIASES):
                self.assertIn(endpoint, app.view_functions,
                              f"{endpoint} is numbered but not a route")
        finally:
            tmp.cleanup()

    def test_non_pages_have_no_number(self):
        for endpoint in (None, "", "health", "index", "logout",
                         "api_builderbot", "api_assistant",
                         "dashboard_refresh", "coin_buy", "coin_sell",
                         "bot_delete", "bot_delete_confirm",
                         "wallet_send_confirm", "bot_silent",
                         "choose_mode", "mode_switch", "donate_info",
                         "donate_record", "passkey_login_begin",
                         "passkey_login_finish"):
            self.assertIsNone(page_number_for(endpoint), endpoint)

    def test_coin_popup_alias_is_the_dashboard_number(self):
        self.assertEqual(page_number_for("coin_popup"),
                         page_number_for("dashboard"))
        self.assertEqual(page_number_for("coin_popup"), 4)

    def test_no_template_hardcodes_a_page_number(self):
        # The only place "Page N" may appear is _page_id.html, and
        # only as the variable — never a literal number.
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text()
            self.assertNotRegex(text, r"Page \d",
                                f"{path.name} hardcodes a page number")


class TestPageNumbersRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        login(self.client, "alice")
        # A second client on the same app, never logged in, for the
        # logged-out pages (login / signup / forgot-password).
        self.anon = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _html(self, client, path):
        resp = client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _assert_page_id(self, html, number):
        """The label renders, in the top-left: inside the topbar,
        before the SolEntry brand link (the login page has no brand,
        so there it must sit inside the topbar header)."""
        chip = f'class="page-id">Page {number}<'
        self.assertIn(chip, html)
        if 'class="brand"' in html:
            self.assertLess(html.index("page-id"),
                            html.index('class="brand"'),
                            "page ID must come before the brand")
        else:
            # The auth pages carry an extra fox-topbar class on the
            # header (fixed 40px topbar so Pages 1 and 2 fit on one
            # screen); match the header by its class prefix.
            start = html.index('<header class="topbar')
            end = html.index("</header>", start)
            self.assertIn(chip, html[start:end],
                          "page ID must sit in the topbar header")

    def _make_preset_bots(self):
        ids = {}
        for route, key in (("/bots/preset/trenches", "trenches"),
                           ("/bots/preset/scanner", "scanner")):
            resp = self.client.post(
                route, data={"csrf_token": csrf(self.client, "/bots")})
            self.assertEqual(resp.status_code, 302, route)
            ids[key] = int(resp.headers["Location"].rsplit("/", 1)[-1])
        return ids

    def test_logged_out_pages(self):
        for path, number in (("/login", 1), ("/signup", 2),
                             ("/forgot-password", 16),
                             ("/forgot-username", 19)):
            self._assert_page_id(self._html(self.anon, path), number)

    def test_logged_in_pages(self):
        ids = self._make_preset_bots()
        pages = [
            ("/wallet", 3), ("/dashboard", 4),
            ("/coins/SOL/detail", 5), ("/bots", 6),
            (f"/bots/{ids['trenches']}", 7),
            (f"/bots/{ids['trenches']}/edit", 8),
            ("/bots/create", 9), ("/trenches", 10), ("/logs", 11),
            ("/settings", 12), ("/wallet/details", 13),
            ("/wallet/send", 14), ("/admin/donations", 15),
        ]
        for path, number in pages:
            self._assert_page_id(self._html(self.client, path), number)

    def test_coin_popup_keeps_the_dashboard_number(self):
        self._assert_page_id(self._html(self.client, "/coins/SOL"), 4)

    def test_forgot_password_steps(self):
        # Set a recovery question, then walk the forgot flow on the
        # logged-out client: answer step, then reset step.
        self.client.post("/settings/recovery-question", data={
            "csrf_token": csrf(self.client, "/settings"),
            "question": "First pet?", "answer": "fluffy",
            "hint": "the dog"}, follow_redirects=False)
        resp = self.anon.post("/forgot-password", data={
            "csrf_token": csrf(self.anon, "/forgot-password"),
            "username": "alice"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self._assert_page_id(
            self._html(self.anon, "/forgot-password/answer"), 17)
        resp = self.anon.post("/forgot-password/answer", data={
            "csrf_token": csrf(self.anon, "/forgot-password/answer"),
            "answer": "fluffy"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self._assert_page_id(
            self._html(self.anon, "/forgot-password/reset"), 18)

    def test_confirm_pages_carry_no_number(self):
        ids = self._make_preset_bots()
        for path in (f"/bots/{ids['trenches']}/delete/confirm",
                     f"/bots/{ids['trenches']}/silent"):
            html = self._html(self.client, path)
            self.assertNotIn("page-id", html, path)

    def test_each_bot_appears_exactly_once_on_bots_page(self):
        ids = self._make_preset_bots()
        html = self._html(self.client, "/bots")
        cards = re.findall(
            r'<a class="card bot-card" href="\s*(/bots/\d+)\s*"', html)
        self.assertEqual(sorted(cards),
                         sorted(f"/bots/{i}" for i in ids.values()))
        # The lower scrolling section is gone (Page 6 rebuild): no
        # Trenches link, no memecoin card, no create button on this
        # page. (Owner, 2026-11-11: the assistant hint that used to
        # mention "create the trenches bot" is gone with the Page 6
        # assistant box — the Trenches preset itself is unchanged
        # and is still created through the assistant endpoint.)
        self.assertNotIn("trenches-card", html)
        self.assertNotIn("trenches-link", html)
        self.assertNotIn("memecoin-card", html)
        self.assertNotIn("+ Create the Trenches bot", html)
        self.assertNotIn("Your Trenches bot is in the carousel above.",
                         html)
        self.assertNotIn("create the trenches bot", html)

    def test_no_create_trenches_button_on_bots_page(self):
        # Page 6 rebuild: the "+ Create the Trenches bot" button is
        # gone from the Bots page. (Owner, 2026-11-11: the Page 6
        # assistant box is deleted, so its "create the trenches
        # bot" hint is gone too — the preset is still created
        # through the assistant endpoint.)
        html = self._html(self.client, "/bots")
        self.assertNotIn("+ Create the Trenches bot", html)
        self.assertNotIn("create the trenches bot", html)
