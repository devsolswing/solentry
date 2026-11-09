"""Login page: SolEntry design from James's screenshot."""

import tempfile
import unittest

from helpers import login, make_client, signup


class TestLoginPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _page(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_login_page_has_solentry_branding(self):
        html = self._page()
        self.assertIn("SolEntry", html)
        self.assertIn("AUTOMATED TRADING TOOL", html)
        self.assertIn("hero-shiba.jpg", html)
        self.assertIn("Version:", html)
        self.assertIn("1.00", html)
        self.assertIn("J. Gordon", html)

    def test_login_page_has_working_form(self):
        html = self._page()
        self.assertIn('name="username"', html)
        self.assertIn('placeholder="Email Address"', html)
        self.assertIn('name="password"', html)
        self.assertIn('id="se-eye"', html)
        self.assertIn(">Login</button>", html)
        self.assertIn('href="/signup"', html)
        self.assertIn("Create Account", html)

    def test_login_page_has_contact_card(self):
        html = self._page()
        self.assertIn("Contact Us", html)
        self.assertIn("mintendont94@gmail.com", html)
        self.assertIn("mailto:mintendont94@gmail.com", html)

    def test_donate_card_has_connect_wallet_not_dropdowns(self):
        html = self._page()
        self.assertIn("Donate (Optional)", html)
        self.assertIn("Connect Wallet", html)
        self.assertIn('id="donate-amount"', html)
        self.assertIn("Send Donation", html)
        self.assertIn('id="donate-send"', html)
        # The old dropdowns must be gone.
        self.assertNotIn("Select Token", html)
        self.assertNotIn("Select Network", html)
        self.assertNotIn("donate-token", html)
        self.assertNotIn("donate-network", html)

    def test_login_still_works_from_new_page(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        resp = login(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_signup_page_still_renders(self):
        resp = self.client.get("/signup")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Create your account", html)
        self.assertIn('href="/login"', html)

    def test_donate_js_is_linked(self):
        html = self._page()
        self.assertIn("donate.js", html)
        resp = self.client.get("/static/donate.js")
        self.assertEqual(resp.status_code, 200)
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "tradingbot", "static",
                               "donate.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("signAndSendTransaction", js)
        self.assertIn("isPhantom", js)
        self.assertIn("/api/donate/record", js)


if __name__ == "__main__":
    unittest.main()
