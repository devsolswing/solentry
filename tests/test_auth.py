"""Auth: signup, login, wrong-password rejection, lockout."""

import tempfile
import unittest

from helpers import csrf, login, make_client, signup


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_signup_creates_user_wallet_and_paper_cash(self):
        resp = signup(self.client, "alice")
        self.assertEqual(resp.status_code, 302)  # redirect to wallet
        self.assertIn("/wallet", resp.headers["Location"])
        db = self.app.config["DB"]
        user = db.execute("SELECT id FROM users WHERE username = 'alice'"
                          ).fetchone()
        self.assertIsNotNone(user)
        wallet = db.execute(
            "SELECT pubkey, encrypted_secret FROM wallets WHERE user_id = ?",
            (user["id"],)).fetchone()
        self.assertIsNotNone(wallet)
        self.assertTrue(30 <= len(wallet["pubkey"]) <= 44)
        # Encrypted blob must not contain the pubkey or any key material.
        self.assertNotIn(wallet["pubkey"], wallet["encrypted_secret"])
        cash = db.execute(
            "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
            (user["id"],)).fetchone()
        self.assertEqual(cash["cash_usd"], 10000.0)

    def test_signup_rejects_duplicate_username(self):
        self.assertEqual(signup(self.client, "alice").status_code, 302)
        self.client.get("/logout")
        resp = signup(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        count = db.execute("SELECT COUNT(*) c FROM users WHERE username='alice'"
                           ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_signup_rejects_short_password_and_bad_username(self):
        resp = self.client.post("/signup", data={
            "csrf_token": csrf(self.client, "/signup"),
            "username": "al", "password": "password123"})
        self.assertEqual(resp.status_code, 302)
        resp = self.client.post("/signup", data={
            "csrf_token": csrf(self.client, "/signup"),
            "username": "alice", "password": "short"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM users"
                                    ).fetchone()["c"], 0)

    def test_login_wrong_password_rejected(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        resp = login(self.client, "alice", "wrongpassword")
        self.assertEqual(resp.status_code, 302)
        # Still logged out: dashboard redirects to login.
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_login_lockout_after_five_fails(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        for _ in range(5):
            login(self.client, "alice", "wrongpassword")
        # Even the right password is now refused during lockout.
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        db = self.app.config["DB"]
        row = db.execute("SELECT locked_until FROM login_attempts "
                         "WHERE username = 'alice'").fetchone()
        self.assertIsNotNone(row["locked_until"])

    def test_login_success_clears_failures(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        login(self.client, "alice", "wrongpassword")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)


if __name__ == "__main__":
    unittest.main()
