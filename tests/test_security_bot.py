"""Password+email crypto binding and admin override — what remains
after the email removal (owner, 2026-11-20).

The Security bot, its emailed 4-digit codes, Recover Account, and
the approve-signup / void-codes admin actions are gone, and this
file no longer tests them. What survives here:
- the v2 password hash / wallet key are still derived from
  password+email: existing accounts keep their stored email and
  must still log in and decrypt; new accounts store email='' and
  derive from password+"".
- a legacy v1 account still upgrades to v2 transparently at login.
- the admin override (reset password / clear lockout) still works.
"""

import tempfile
import unittest

from helpers import csrf, login, make_client, signup
from tradingbot.security import (
    decrypt_secret, encrypt_secret, hash_account_password, hash_password,
    verify_account_password, verify_password, wipe_secret)
from tradingbot.wallet_keys import (
    decrypt_wallet_secret, derive_user_key, new_user_key_salt,
    wrap_user_key)
from tradingbot.executor import _load_wallet
from tradingbot.solana import generate_keypair


class _AuthTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return self.app.config["DB"]

    def cfg(self):
        return self.app.config["TRADINGBOT_CONFIG"]


class PasswordBindingTestCase(_AuthTestBase):

    def test_new_signup_stores_empty_email_and_logs_in(self):
        # Signup collects no email any more: the account is created
        # directly, email is '', and the v2 hash / wallet key derive
        # from password+"" through the same code.
        resp = signup(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        row = self.db().execute(
            "SELECT id, email, pw_version, user_key_salt, "
            "wrapped_user_key FROM users WHERE username = 'alice'"
            ).fetchone()
        self.assertEqual(row["email"], "")
        self.assertEqual(row["pw_version"], 2)
        self.assertTrue(row["user_key_salt"])
        self.assertTrue(row["wrapped_user_key"])
        wallet = self.db().execute(
            "SELECT enc_version, encrypted_secret FROM wallets "
            "WHERE user_id = ?", (row["id"],)).fetchone()
        self.assertEqual(wallet["enc_version"], 2)
        # The wallet blob does NOT decrypt with the server key
        # alone — it needs the password+"" user key.
        with self.assertRaises(ValueError):
            decrypt_secret(wallet["encrypted_secret"],
                           self.cfg().encryption_key)
        # It does decrypt through the user-key layer.
        secret = decrypt_wallet_secret(
            self.db(), row["id"], self.cfg().encryption_key)
        self.assertIsNotNone(secret)
        self.assertEqual(len(secret), 64)
        # And the login round-trips on password+"".
        self.client.get("/logout")
        resp = login(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_password_hash_is_bound_to_the_email(self):
        # Unit-level: the v2 hash still binds password+email for
        # accounts that stored one.
        salt, pw_hash = hash_account_password(
            "password123", "alice@example.com")
        self.assertTrue(verify_account_password(
            "password123", "alice@example.com", salt, pw_hash))
        # The same password with a different email does NOT verify.
        self.assertFalse(verify_account_password(
            "password123", "someone-else@example.com", salt, pw_hash))
        # And the plain password-only check does not match the v2 hash.
        self.assertFalse(verify_password(
            "password123", salt, pw_hash))
        # A new account's hash binds password+"" instead.
        salt2, pw_hash2 = hash_account_password("password123", "")
        self.assertTrue(verify_account_password(
            "password123", "", salt2, pw_hash2))
        self.assertFalse(verify_account_password(
            "password123", "alice@example.com", salt2, pw_hash2))

    def test_existing_v2_account_with_email_still_logs_in(self):
        # Regression guard for the removal: an account created
        # BEFORE the email field went away (stored email, v2 hash
        # and wallet key bound to password+email) must still log
        # in and its wallet must still decrypt. Built by direct
        # insert, the way the old signup built it.
        email = "alice@example.com"
        salt, pw_hash = hash_account_password("password123", email)
        user_key_salt = new_user_key_salt()
        user_key = derive_user_key("password123", email, user_key_salt)
        seed, pubkey, secret64 = generate_keypair()
        try:
            wrapped_user_key = wrap_user_key(
                user_key, self.cfg().encryption_key)
            blob = encrypt_secret(secret64, bytes(user_key))
        finally:
            wipe_secret(user_key)
            wipe_secret(seed)
            wipe_secret(secret64)
        cur = self.db().execute(
            "INSERT INTO users (username, password_hash, salt, "
            "pw_version, email, user_key_salt, wrapped_user_key) "
            "VALUES ('alice', ?, ?, 2, ?, ?, ?)",
            (pw_hash, salt, email, user_key_salt, wrapped_user_key))
        user_id = cur.lastrowid
        self.db().execute(
            "INSERT INTO wallets (user_id, pubkey, encrypted_secret, "
            "enc_version) VALUES (?, ?, ?, 2)",
            (user_id, pubkey, blob))
        self.db().commit()
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        secret = decrypt_wallet_secret(
            self.db(), user_id, self.cfg().encryption_key)
        self.assertIsNotNone(secret)
        self.assertEqual(len(secret), 64)

    def test_legacy_v1_user_upgrades_to_v2_on_login(self):
        # A user from before the email layer: password-only hash,
        # server-key-only wallet.
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


class TestAdminOverride(_AuthTestBase):
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


if __name__ == "__main__":
    unittest.main()
