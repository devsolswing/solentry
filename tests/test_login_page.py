"""Login page: fox design from James's screenshots (Pages 1 and 2).

Page 1 (Login): black background, gold fox logo + "SolEntry"
wordmark, gold-bordered fields, red "Forgot username?"/"Forgot
password?" links, gold Login button, red Back button, Donate button,
gold fox hero with "Still on the $GATE?" — every word functional.
Page 2 (Sign up): same header/hero, username + email + password
(with eye) + security question + answer + hint, Create Account and
Back to Login.
"""

import tempfile
import unittest

from helpers import login, make_client, signup


class TestLoginPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _page(self, path="/login"):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def test_login_page_has_fox_branding(self):
        html = self._page()
        self.assertIn("SolEntry", html)
        self.assertIn("fox-red-480.jpg", html)
        self.assertIn("fox-gold-480.jpg", html)
        self.assertIn("Still on", html)
        self.assertIn("$GATE?", html)
        # The filled-in username look: gold, bold, same font as the
        # screenshot's "Dev_SolEntry" — the field is empty, the
        # style carries the look. No value is hardcoded.
        self.assertIn("fox-field", html)
        self.assertNotIn('value="Dev_SolEntry"', html)
        # The old Shiba design is gone.
        self.assertNotIn("AUTOMATED TRADING TOOL", html)
        self.assertNotIn("J. Gordon", html)
        self.assertNotIn("Version:", html)
        self.assertNotIn("data:image/jpeg;base64", html)

    def test_login_page_has_working_form(self):
        html = self._page()
        self.assertIn('name="username"', html)
        self.assertIn('placeholder="Username"', html)
        self.assertIn('name="password"', html)
        self.assertIn('id="fox-eye"', html)
        self.assertIn(">Login</button>", html)
        self.assertIn('href="/forgot-username"', html)
        self.assertIn("Forgot username?", html)
        self.assertIn('href="/forgot-password"', html)
        self.assertIn("Forgot password?", html)
        # Back is gone: a Create Account button goes to Sign up.
        self.assertNotIn('id="fox-back"', html)
        self.assertNotIn(">Back</button>", html)
        self.assertIn('href="/signup"', html)
        self.assertIn(">Create Account</a>", html)
        self.assertIn('id="fox-donate"', html)
        self.assertIn(">Donate</button>", html)
        self.assertIn('id="passkey-login-btn"', html)

    def test_donate_card_is_behind_the_donate_button(self):
        html = self._page()
        self.assertIn('id="donate-card"', html)
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

    def test_signup_page_has_fox_design_and_fields(self):
        html = self._page("/signup")
        self.assertIn("fox-red-480.jpg", html)
        self.assertIn("fox-gold-480.jpg", html)
        self.assertIn("Still on", html)
        self.assertIn("$GATE?", html)
        for name in ("username", "email", "password", "question",
                     "answer", "hint"):
            self.assertIn(f'name="{name}"', html, name)
        self.assertIn('id="fox-eye"', html)
        self.assertIn("Create Account", html)
        self.assertIn('href="/login"', html)
        self.assertIn("Back to Login", html)
        self.assertNotIn("Create your account", html)

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
