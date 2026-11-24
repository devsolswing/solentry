"""Wallet export (Settings -> Wallet backup): the owner's own
private key, shown once, only after re-entering their password.

Security properties under test:
- GET /settings never carries the key.
- The key appears only on a POST with the correct password + CSRF.
- A wrong password refuses, records a login failure (same lockout
  as a wrong login), and shows nothing.
- The shown key is the wallet's real key (it derives the wallet
  pubkey) and it never appears in the activity log or in a cached
  response (no-store).
- Logged-out requests never reach the route.
"""
from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest

from helpers import csrf, make_client, signup
from tradingbot.solana import (
    base58_decode,
    base58_encode,
    publickey_from_secret,
)

KEY_INPUT = re.compile(
    r'value="([1-9A-HJ-NP-Za-z]+)" id="wallet-export-key"')


class WalletExportBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.app, self.client = make_client(self._tmp)
        self.db_path = f"{self._tmp}/test.db"
        signup(self.client, "exporter", "password123")

    def _db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _wallet_pubkey(self):
        with self._db() as conn:
            row = conn.execute(
                "SELECT w.pubkey FROM wallets w JOIN users u "
                "ON u.id = w.user_id WHERE u.username = ?",
                ("exporter",)).fetchone()
        return row["pubkey"] if row else None

    def _export(self, password, token=None):
        if token is None:
            token = csrf(self.client, "/settings")
        return self.client.post(
            "/settings/wallet-export",
            data={"csrf_token": token, "password": password},
            follow_redirects=False)


class TestWalletExport(WalletExportBase):
    def test_settings_shows_backup_section_but_no_key(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Wallet backup", html)
        self.assertIn("Show wallet key", html)
        self.assertNotIn("wallet-export-key", html)
        self.assertNotIn("wallet-export-revealed", html)

    def test_correct_password_shows_the_real_key(self):
        resp = self._export("password123")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        m = KEY_INPUT.search(html)
        self.assertIsNotNone(m, "no key shown after correct password")
        key = m.group(1)
        self.assertIn("Copy key", html)
        self.assertIn("data-copy", html)
        # The shown key IS this wallet's key: it derives the pubkey.
        pubkey = base58_encode(
            publickey_from_secret(base58_decode(key)))
        self.assertEqual(pubkey, self._wallet_pubkey())
        # The response must never sit in a cache.
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        # The audit row says the key was shown — without the key.
        with self._db() as conn:
            rows = conn.execute(
                "SELECT detail FROM activity WHERE kind = ?",
                ("wallet_export",)).fetchall()
        self.assertTrue(rows, "no wallet_export audit row")
        for row in rows:
            self.assertNotIn(key, row["detail"])

    def test_wrong_password_refuses_and_records_a_failure(self):
        resp = self._export("not-the-password")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/settings", resp.headers.get("Location", ""))
        follow = self.client.get("/settings")
        self.assertNotIn("wallet-export-key",
                         follow.get_data(as_text=True))
        with self._db() as conn:
            row = conn.execute(
                "SELECT fail_count FROM login_attempts "
                "WHERE username = ?", ("exporter",)).fetchone()
        self.assertIsNotNone(row, "wrong export password not recorded")
        self.assertGreaterEqual(row["fail_count"], 1)

    def test_missing_password_refuses(self):
        resp = self._export("")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("wallet-export-key",
                         self.client.get("/settings")
                         .get_data(as_text=True))

    def test_missing_csrf_is_rejected(self):
        resp = self.client.post(
            "/settings/wallet-export",
            data={"password": "password123"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 400)

    def test_logged_out_cannot_export(self):
        self.client.get("/logout", follow_redirects=False)
        # No fresh CSRF token is fetchable logged out; any token
        # works here because login_required redirects first.
        resp = self.client.post(
            "/settings/wallet-export",
            data={"csrf_token": "x", "password": "password123"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
