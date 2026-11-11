"""Security bot: email-linked accounts, 4-digit signup/recovery
codes, password+email encryption, Recover Account, admin override.

The Security bot is server-side only — it has no user-facing route
— and it only ever sends the two codes (signup confirmation +
account recovery). Tests use the dev-echo flag (set in
helpers.make_config) so the codes can be read from the popup
without SMTP.
"""

import re
import tempfile
import time
import unittest

from helpers import csrf, dev_code_from_signup, login, make_client, signup
from tradingbot.config import Config
from tradingbot.security import (decrypt_secret, hash_password,
                                 verify_account_password)
from tradingbot.wallet_keys import decrypt_wallet_secret
from tradingbot.executor import _load_wallet
from tradingbot import security_bot as security_bot_mod


class SecurityBotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return self.app.config["DB"]

    def cfg(self):
        return self.app.config["TRADINGBOT_CONFIG"]

    # -- signup helpers -------------------------------------------------
    def start_signup(self, username="alice", email="alice@example.com",
                     password="password123", question="First pet?",
                     answer="fluffy", hint="the dog"):
        token = csrf(self.client, "/signup")
        return self.client.post("/signup", data={
            "csrf_token": token, "username": username,
            "password": password, "email": email,
            "question": question, "answer": answer, "hint": hint},
            follow_redirects=False)

    def verify_signup_code(self, code):
        token = csrf(self.client, "/signup")
        return self.client.post("/signup/verify-code", data={
            "csrf_token": token, "code": code},
            follow_redirects=False)

    # -- recover helpers ------------------------------------------------
    def open_recover(self):
        return self.client.get("/recover-account", follow_redirects=False)

    def request_recover_code(self, identifier):
        token = csrf(self.client, "/login")
        return self.client.post("/recover-account", data={
            "csrf_token": token, "identifier": identifier},
            follow_redirects=False)

    def recover_code_from_login_page(self):
        page = self.client.get("/login")
        html = page.get_data(as_text=True)
        m = re.search(r"your code is (\d{4})", html)
        assert m, "no dev code on recover popup"
        return m.group(1)

    def verify_recover_code(self, code):
        token = csrf(self.client, "/login")
        return self.client.post("/recover-account/verify", data={
            "csrf_token": token, "code": code},
            follow_redirects=False)

    def reset_via_recover(self, password="newpassword123",
                          question="New question?", answer="newanswer",
                          hint=""):
        token = csrf(self.client, "/login")
        return self.client.post("/recover-account/reset", data={
            "csrf_token": token, "password": password,
            "question": question, "answer": answer, "hint": hint},
            follow_redirects=False)


class TestSignupCodeFlow(SecurityBotTestCase):
    def test_code_popup_shown_after_create(self):
        resp = self.start_signup()
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Check your email", html)
        self.assertIn("alice@example.com", html)
        self.assertIn("4-digit code", html)
        # No account exists yet — creation waits on the code.
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)
        pending = self.db().execute(
            "SELECT id FROM pending_signups WHERE username = 'alice'"
            ).fetchone()
        self.assertIsNotNone(pending)

    def test_wrong_code_rejects_and_creates_nothing(self):
        resp = self.start_signup()
        code = dev_code_from_signup(resp)
        wrong = "0000" if code != "0000" else "0001"
        resp = self.verify_signup_code(wrong)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("That code is wrong.",
                      resp.get_data(as_text=True))
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)

    def test_correct_code_creates_v2_account(self):
        resp = self.start_signup()
        code = dev_code_from_signup(resp)
        resp = self.verify_signup_code(code)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        row = self.db().execute(
            "SELECT id, email, pw_version, user_key_salt, "
            "wrapped_user_key FROM users WHERE username = 'alice'"
            ).fetchone()
        self.assertEqual(row["email"], "alice@example.com")
        self.assertEqual(row["pw_version"], 2)
        self.assertTrue(row["user_key_salt"])
        self.assertTrue(row["wrapped_user_key"])
        wallet = self.db().execute(
            "SELECT enc_version, encrypted_secret FROM wallets "
            "WHERE user_id = ?", (row["id"],)).fetchone()
        self.assertEqual(wallet["enc_version"], 2)
        # The wallet blob does NOT decrypt with the server key
        # alone — it needs the password+email user key.
        with self.assertRaises(ValueError):
            decrypt_secret(wallet["encrypted_secret"],
                           self.cfg().encryption_key)
        # It does decrypt through the user-key layer.
        secret = decrypt_wallet_secret(
            self.db(), row["id"], self.cfg().encryption_key)
        self.assertIsNotNone(secret)
        self.assertEqual(len(secret), 64)

    def test_expired_code_rejects(self):
        resp = self.start_signup()
        code = dev_code_from_signup(resp)
        self.db().execute(
            "UPDATE email_codes SET expires_at = ? "
            "WHERE purpose = 'signup' AND email = 'alice@example.com'",
            (int(time.time()) - 1,))
        self.db().commit()
        resp = self.verify_signup_code(code)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("expired", resp.get_data(as_text=True))
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)

    def test_five_wrong_tries_voids_the_code(self):
        resp = self.start_signup()
        code = dev_code_from_signup(resp)
        wrong = "0000" if code != "0000" else "0001"
        for _ in range(5):
            resp = self.verify_signup_code(wrong)
        self.assertIn("Too many wrong tries",
                      resp.get_data(as_text=True))
        # Even the right code is now dead.
        resp = self.verify_signup_code(code)
        self.assertIn("That code has already been used.",
                      resp.get_data(as_text=True))
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)

    def test_email_already_registered_rejected(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        resp = self.start_signup(username="alice2",
                                 email="alice@example.com")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/signup", resp.headers["Location"])
        page = self.client.get("/signup", follow_redirects=True)
        self.assertIn("already registered",
                      page.get_data(as_text=True))

    def test_signup_fails_closed_without_smtp(self):
        # No dev echo and no SMTP: the signup says plainly that
        # email is not set up, and no account (or pending signup)
        # is created.
        import os
        env = {
            "SESSION_SECRET": "test-session-secret-" + "x" * 40,
            "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(self.tmp.name, "nosmtp.db"),
            "PAPER_START_CASH": "100",
            "FEE_RECIPIENT_PUBKEY":
                "4XWpRuKMH5uVGiXkvV5PSKSBi36dBQVSy9QJ59Kvda7Q",
        }
        from tradingbot import create_app
        app = create_app(Config(env=env))
        app.testing = True
        client = app.test_client()
        token = csrf(client, "/signup")
        resp = client.post("/signup", data={
            "csrf_token": token, "username": "alice",
            "password": "password123", "email": "alice@example.com"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/signup", resp.headers["Location"])
        page = client.get("/signup", follow_redirects=True)
        self.assertIn("Email is not set up yet",
                      page.get_data(as_text=True))
        row = app.config["DB"].execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)
        pending = app.config["DB"].execute(
            "SELECT id FROM pending_signups WHERE username = 'alice'"
            ).fetchone()
        self.assertIsNone(pending)


class TestPasswordEmailBinding(SecurityBotTestCase):
    def test_password_hash_is_bound_to_the_email(self):
        signup(self.client, "alice", email="alice@example.com")
        row = self.db().execute(
            "SELECT password_hash, salt FROM users "
            "WHERE username = 'alice'").fetchone()
        self.assertTrue(verify_account_password(
            "password123", "alice@example.com",
            row["salt"], row["password_hash"]))
        # The same password with a different email does NOT verify.
        self.assertFalse(verify_account_password(
            "password123", "someone-else@example.com",
            row["salt"], row["password_hash"]))
        # And the plain password-only check does not match the v2 hash.
        from tradingbot.security import verify_password
        self.assertFalse(verify_password(
            "password123", row["salt"], row["password_hash"]))

    def test_legacy_v1_user_upgrades_to_v2_on_login(self):
        # A user from before the email layer: password-only hash,
        # server-key-only wallet.
        from tradingbot.security import encrypt_secret
        from tradingbot.solana import generate_keypair
        salt, pw_hash = hash_password("password123")
        cur = self.db().execute(
            "INSERT INTO users (username, password_hash, salt, "
            "pw_version, email) VALUES ('olduser', ?, ?, 1, "
            "'old@example.com')", (pw_hash, salt))
        user_id = cur.lastrowid
        seed, pubkey, secret64 = generate_keypair()
        blob = encrypt_secret(secret64, self.cfg().encryption_key)
        self.db().execute(
            "INSERT INTO wallets (user_id, pubkey, encrypted_secret, "
            "enc_version) VALUES (?, ?, ?, 1)",
            (user_id, pubkey, blob))
        self.db().commit()
        resp = login(self.client, "olduser", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        row = self.db().execute(
            "SELECT pw_version, wrapped_user_key FROM users "
            "WHERE username = 'olduser'").fetchone()
        self.assertEqual(row["pw_version"], 2)
        self.assertTrue(row["wrapped_user_key"])
        wallet = self.db().execute(
            "SELECT enc_version FROM wallets WHERE user_id = ?",
            (user_id,)).fetchone()
        self.assertEqual(wallet["enc_version"], 2)
        # The executor (signing bot) still loads the wallet.
        pubkey_out, seed_out = _load_wallet(
            self.db(), user_id, self.cfg())
        self.assertEqual(pubkey_out, pubkey)
        self.assertEqual(len(seed_out), 32)

    def test_settings_shows_email_under_personal_details(self):
        signup(self.client, "alice", email="alice@example.com")
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Personal Details", html)
        self.assertIn("alice@example.com", html)
        self.assertIn("alice", html)


class TestRecoverAccount(SecurityBotTestCase):
    def test_full_flow_end_to_end(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        # Step 1: open the popup, enter the email.
        resp = self.open_recover()
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/login")
        self.assertIn("Recover Account", page.get_data(as_text=True))
        resp = self.request_recover_code("alice@example.com")
        self.assertEqual(resp.status_code, 302)
        code = self.recover_code_from_login_page()
        page = self.client.get("/login")
        self.assertIn("Enter the code we've sent to your email",
                      page.get_data(as_text=True))
        # Step 2: enter the code.
        resp = self.verify_recover_code(code)
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/login")
        self.assertIn("Set a new password",
                      page.get_data(as_text=True))
        # Step 3: new password + new security question.
        resp = self.reset_via_recover(
            password="newpassword123", question="New question?",
            answer="newanswer")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        page = self.client.get("/login", follow_redirects=True)
        self.assertIn("Password changed",
                      page.get_data(as_text=True))
        # The new password works; the old one does not.
        resp = login(self.client, "alice", "newpassword123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        self.client.get("/logout")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        # The security question is the new one.
        row = self.db().execute(
            "SELECT recovery_question FROM users "
            "WHERE username = 'alice'").fetchone()
        self.assertEqual(row["recovery_question"], "New question?")
        # The wallet was re-encrypted with the new password+email
        # and still decrypts through the user-key layer.
        user = self.db().execute(
            "SELECT id, pw_version FROM users WHERE username = 'alice'"
            ).fetchone()
        self.assertEqual(user["pw_version"], 2)
        secret = decrypt_wallet_secret(
            self.db(), user["id"], self.cfg().encryption_key)
        self.assertIsNotNone(secret)
        self.assertEqual(len(secret), 64)

    def test_recover_by_username_works(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        self.request_recover_code("alice")
        code = self.recover_code_from_login_page()
        resp = self.verify_recover_code(code)
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/login")
        self.assertIn("Set a new password",
                      page.get_data(as_text=True))

    def test_recover_unknown_email_is_generic(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        resp = self.request_recover_code("ghost@example.com")
        self.assertEqual(resp.status_code, 302)
        # The popup still advances to the code step (no reveal).
        page = self.client.get("/login")
        self.assertIn("Enter the code we've sent to your email",
                      page.get_data(as_text=True))
        # No code was sent, so no code can match.
        resp = self.verify_recover_code("1234")
        page = self.client.get("/login")
        self.assertIn("That code is wrong.",
                      page.get_data(as_text=True))

    def test_recover_wrong_code_rejected(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        self.request_recover_code("alice@example.com")
        code = self.recover_code_from_login_page()
        wrong = "0000" if code != "0000" else "0001"
        resp = self.verify_recover_code(wrong)
        page = self.client.get("/login")
        self.assertIn("That code is wrong.",
                      page.get_data(as_text=True))
        # Still on the code step, not the reset step.
        self.assertNotIn("Set a new password",
                         page.get_data(as_text=True))

    def test_recover_reset_needs_the_code_first(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        resp = self.reset_via_recover()
        # No verified code: bounced back to login, password unchanged.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        resp = login(self.client, "alice", "password123")
        self.assertIn("/wallet", resp.headers["Location"])


class TestSecurityBotIsServerSideOnly(SecurityBotTestCase):
    def test_no_user_route_exposes_the_security_bot(self):
        # No URL rule is the Security bot — it is only ever called
        # from the signup / recover routes, server-side.
        endpoints = {r.endpoint for r in self.app.url_map.iter_rules()}
        self.assertNotIn("security_bot", endpoints)
        for rule in self.app.url_map.iter_rules():
            self.assertNotIn("security-bot", str(rule))
            self.assertNotIn("security_bot", str(rule))

    def test_issue_code_rejects_other_purposes(self):
        result = security_bot_mod.issue_code(
            self.db(), self.cfg(), purpose="notify",
            email="alice@example.com")
        self.assertFalse(result["ok"])

    def test_codes_are_never_stored_in_plaintext(self):
        resp = self.start_signup()
        code = dev_code_from_signup(resp)
        row = self.db().execute(
            "SELECT code_hash FROM email_codes "
            "WHERE purpose = 'signup'").fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(code, row["code_hash"])
        self.assertEqual(len(row["code_hash"]), 64)


class TestAdminOverride(SecurityBotTestCase):
    def _make_admin_and_user(self):
        # The first registered user is the admin (ADMIN_USERNAME
        # unset in tests). Create the admin first, then the user.
        signup(self.client, "admin", email="admin@example.com")
        self.client.get("/logout")
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        login(self.client, "admin")

    def test_admin_page_requires_admin(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        signup(self.client, "bob", email="bob@example.com")
        # bob is not the admin (alice was first).
        self.assertEqual(self.client.get("/admin/security").status_code,
                         403)
        token = csrf(self.client, "/login")
        resp = self.client.post(
            "/admin/security/clear-lockout",
            data={"csrf_token": token, "username": "alice"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

    def test_admin_reset_password(self):
        self._make_admin_and_user()
        token = csrf(self.client, "/admin/security")
        resp = self.client.post(
            "/admin/security/reset-password",
            data={"csrf_token": token, "username": "alice",
                  "new_password": "adminset123"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        # Logged as an admin override.
        row = self.db().execute(
            "SELECT kind, detail FROM activity WHERE kind = "
            "'admin_override'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("alice", row["detail"])
        self.assertIn("reset password", row["detail"])
        # Alice logs in with the new password (wallet re-encrypted).
        self.client.get("/logout")
        resp = login(self.client, "alice", "adminset123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_admin_clear_lockout(self):
        self._make_admin_and_user()
        self.client.get("/logout")
        for _ in range(5):
            login(self.client, "alice", "wrongpassword")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        login(self.client, "admin")
        token = csrf(self.client, "/admin/security")
        resp = self.client.post(
            "/admin/security/clear-lockout",
            data={"csrf_token": token, "username": "alice"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.client.get("/logout")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_admin_approve_signup_bypasses_code(self):
        self._make_admin_and_user()
        self.client.get("/logout")
        # Start a signup but never enter the code.
        resp = self.start_signup(username="carol",
                                 email="carol@example.com")
        self.assertEqual(resp.status_code, 200)
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'carol'").fetchone()
        self.assertIsNone(row)
        pending = self.db().execute(
            "SELECT id FROM pending_signups WHERE username = 'carol'"
            ).fetchone()
        login(self.client, "admin")
        token = csrf(self.client, "/admin/security")
        resp = self.client.post(
            "/admin/security/approve-signup",
            data={"csrf_token": token, "pending_id": pending["id"]},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'carol'").fetchone()
        self.assertIsNotNone(row)
        self.client.get("/logout")
        resp = login(self.client, "carol")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_admin_void_codes(self):
        self._make_admin_and_user()
        self.client.get("/logout")
        self.request_recover_code("alice@example.com")
        code = self.recover_code_from_login_page()
        login(self.client, "admin")
        token = csrf(self.client, "/admin/security")
        resp = self.client.post(
            "/admin/security/void-codes",
            data={"csrf_token": token, "username": "alice"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.client.get("/logout")
        self.request_recover_code("alice@example.com")
        # The old code is void; a fresh code is needed (the new
        # one works — proving the void only killed the old code).
        new_code = self.recover_code_from_login_page()
        resp = self.verify_recover_code(code)
        page = self.client.get("/login")
        self.assertIn("That code is wrong.",
                      page.get_data(as_text=True))
        resp = self.verify_recover_code(new_code)
        page = self.client.get("/login")
        self.assertIn("Set a new password",
                      page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
