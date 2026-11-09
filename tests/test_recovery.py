"""Phone + email linking and two-code account recovery.

Senders are faked (no network, no provider keys): the fake records
what would have been sent, and tests extract the 6-digit code from
the recorded message — exactly as a user would read it from their
phone or inbox. Codes are never asserted from the database row.
"""

import re
import tempfile
import unittest
from unittest import mock

from helpers import TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, csrf, login
from helpers import make_config, signup
from tradingbot import create_app
from tradingbot.config import Config

import os
from datetime import datetime, timedelta, timezone

CODE_RE = re.compile(r"(\d{6})")


def _past_iso(minutes: int = 0, seconds: int = 0) -> str:
    """An app-format UTC timestamp that far in the past."""
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes, seconds=seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def make_config_with_providers(tmpdir: str) -> Config:
    env = {
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tmpdir, "test.db"),
        "PAPER_START_CASH": "10000",
        "TWILIO_ACCOUNT_SID": "ACfake",
        "TWILIO_AUTH_TOKEN": "fake-token",
        "TWILIO_FROM_NUMBER": "+15550001000",
        "RESEND_API_KEY": "re_fake",
        "RESEND_FROM_EMAIL": "SolEntry <test@example.com>",
    }
    return Config(env=env)


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make_client(self, providers: bool = True):
        cfg = (make_config_with_providers(self.tmp.name) if providers
               else make_config(self.tmp.name))
        app = create_app(cfg)
        app.testing = True
        self.app = app
        self.client = app.test_client()
        self.sent_sms = []
        self.sent_email = []

        def fake_sms(cfg_arg, to, body):
            self.sent_sms.append((to, body))
            return True

        def fake_email(cfg_arg, to, subject, text):
            self.sent_email.append((to, subject, text))
            return True

        self._sms_patch = mock.patch(
            "tradingbot.notify.send_sms", fake_sms)
        self._email_patch = mock.patch(
            "tradingbot.notify.send_email", fake_email)
        self._sms_patch.start()
        self._email_patch.start()
        self.addCleanup(self._sms_patch.stop)
        self.addCleanup(self._email_patch.stop)
        return app, self.client

    def db(self):
        return self.app.config["DB"]

    def seed_user(self):
        """A bare user row (verification_codes FK-points at users)."""
        cur = self.db().execute(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES ('seed', 'hash', 'salt')")
        self.db().commit()
        return cur.lastrowid

    def link_both(self, username="alice"):
        """Sign up and verify both contacts via the faked senders."""
        signup(self.client, username)
        self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/settings"),
            "phone": "555-010-1000"}, follow_redirects=False)
        code = CODE_RE.search(self.sent_sms[-1][1]).group(1)
        self.client.post("/settings/verify", data={
            "csrf_token": csrf(self.client, "/settings"),
            "kind": "phone", "code": code}, follow_redirects=False)
        self.client.post("/settings/email", data={
            "csrf_token": csrf(self.client, "/settings"),
            "email": "alice@example.com"}, follow_redirects=False)
        code = CODE_RE.search(self.sent_email[-1][2]).group(1)
        self.client.post("/settings/verify", data={
            "csrf_token": csrf(self.client, "/settings"),
            "kind": "email", "code": code}, follow_redirects=False)


class TestCodeUnit(RecoveryTestCase):
    def test_generate_code_is_six_digits(self):
        from tradingbot.recovery import generate_code
        for _ in range(20):
            code = generate_code()
            self.assertTrue(re.fullmatch(r"\d{6}", code))

    def test_hash_is_salted_sha256_not_plaintext(self):
        from tradingbot.recovery import generate_code, hash_code
        code = generate_code()
        h1 = hash_code(code, "salt-one")
        h2 = hash_code(code, "salt-two")
        self.assertNotEqual(h1, h2)
        self.assertNotIn(code, h1)
        self.assertEqual(len(h1), 64)

    def test_request_and_verify_round_trip(self):
        from tradingbot.recovery import request_code, verify_code
        self.make_client()
        uid = self.seed_user()
        db = self.db()
        code, error = request_code(db, uid, "link", "+15550101000")
        self.assertIsNone(error)
        self.assertTrue(re.fullmatch(r"\d{6}", code))
        row = db.execute("SELECT code_hash FROM verification_codes"
                         ).fetchone()
        self.assertNotIn(code, row["code_hash"])
        self.assertTrue(verify_code(db, uid, "link", "+15550101000", code))
        # A code works exactly once.
        self.assertFalse(verify_code(db, uid, "link", "+15550101000", code))

    def test_wrong_code_fails_and_five_voids(self):
        from tradingbot.recovery import request_code, verify_code
        self.make_client()
        uid = self.seed_user()
        db = self.db()
        code, _ = request_code(db, uid, "link", "+15550101000")
        for _ in range(5):
            self.assertFalse(verify_code(db, uid, "link", "+15550101000",
                                         "000000"))
        # Even the right code is dead after 5 wrong tries.
        self.assertFalse(verify_code(db, uid, "link", "+15550101000", code))

    def test_expired_code_fails(self):
        from tradingbot.recovery import request_code, verify_code
        self.make_client()
        uid = self.seed_user()
        db = self.db()
        code, _ = request_code(db, uid, "link", "+15550101000")
        db.execute("UPDATE verification_codes SET expires_at = ?",
                   (_past_iso(minutes=1),))
        db.commit()
        self.assertFalse(verify_code(db, uid, "link", "+15550101000", code))

    def test_resend_rate_limit(self):
        from tradingbot.recovery import request_code, verify_code
        self.make_client()
        uid = self.seed_user()
        db = self.db()
        code1, _ = request_code(db, uid, "link", "+15550101000")
        code2, error = request_code(db, uid, "link", "+15550101000")
        self.assertIsNone(code2)
        self.assertEqual(error, "rate_limited")
        # After 61 seconds a fresh code issues and the old one is dead.
        db.execute("UPDATE verification_codes SET created_at = ?",
                   (_past_iso(seconds=61),))
        db.commit()
        code3, error = request_code(db, uid, "link", "+15550101000")
        self.assertIsNone(error)
        self.assertTrue(verify_code(db, uid, "link", "+15550101000", code3))
        self.assertFalse(verify_code(db, uid, "link", "+15550101000", code1))

    def test_normalize_phone(self):
        from tradingbot.recovery import normalize_phone
        self.assertEqual(normalize_phone("555-010-1000"), "+15550101000")
        self.assertEqual(normalize_phone("(555) 010-1000"), "+15550101000")
        self.assertEqual(normalize_phone("+44 20 7946 0958"),
                         "+442079460958")
        self.assertEqual(normalize_phone("15550101000"), "+15550101000")
        self.assertIsNone(normalize_phone("123"))
        self.assertIsNone(normalize_phone("not-a-number"))
        self.assertIsNone(normalize_phone(""))

    def test_email_validation_and_masking(self):
        from tradingbot.recovery import (is_valid_email, mask_email,
                                         mask_phone)
        self.assertTrue(is_valid_email("recovery@example.com"))
        self.assertTrue(is_valid_email("a.b+tag@example.co.uk"))
        self.assertFalse(is_valid_email("no-at-sign"))
        self.assertFalse(is_valid_email("a@b"))
        self.assertFalse(is_valid_email(""))
        self.assertEqual(mask_email("recovery@example.com"),
                         "re***@example.com")
        self.assertEqual(mask_phone("+15550101000"), "+*** *** 1000")


class TestLinkFlow(RecoveryTestCase):
    def test_link_phone_happy_path(self):
        self.make_client()
        signup(self.client, "alice")
        resp = self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/settings"),
            "phone": "555-010-1000"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.sent_sms), 1)
        self.assertEqual(self.sent_sms[0][0], "+15550101000")
        # The code must not appear anywhere on the page.
        code = CODE_RE.search(self.sent_sms[0][1]).group(1)
        self.assertNotIn(code, resp.get_data(as_text=True))
        resp = self.client.post("/settings/verify", data={
            "csrf_token": csrf(self.client, "/settings"),
            "kind": "phone", "code": code}, follow_redirects=True)
        self.assertIn("verified", resp.get_data(as_text=True))
        row = self.db().execute(
            "SELECT phone, phone_verified FROM users WHERE username='alice'"
            ).fetchone()
        self.assertEqual(row["phone"], "+15550101000")
        self.assertEqual(row["phone_verified"], 1)

    def test_link_phone_wrong_code_stays_unverified(self):
        self.make_client()
        signup(self.client, "alice")
        self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/settings"),
            "phone": "5550101000"}, follow_redirects=False)
        resp = self.client.post("/settings/verify", data={
            "csrf_token": csrf(self.client, "/settings"),
            "kind": "phone", "code": "000000"}, follow_redirects=True)
        self.assertIn("wrong or expired", resp.get_data(as_text=True))
        row = self.db().execute(
            "SELECT phone_verified FROM users WHERE username='alice'"
            ).fetchone()
        self.assertEqual(row["phone_verified"], 0)

    def test_link_email_happy_path(self):
        self.make_client()
        signup(self.client, "alice")
        self.client.post("/settings/email", data={
            "csrf_token": csrf(self.client, "/settings"),
            "email": "alice@example.com"}, follow_redirects=False)
        self.assertEqual(len(self.sent_email), 1)
        self.assertEqual(self.sent_email[0][0], "alice@example.com")
        code = CODE_RE.search(self.sent_email[0][2]).group(1)
        self.client.post("/settings/verify", data={
            "csrf_token": csrf(self.client, "/settings"),
            "kind": "email", "code": code}, follow_redirects=False)
        row = self.db().execute(
            "SELECT email, email_verified FROM users WHERE username='alice'"
            ).fetchone()
        self.assertEqual(row["email"], "alice@example.com")
        self.assertEqual(row["email_verified"], 1)

    def test_link_phone_bad_number_rejected(self):
        self.make_client()
        signup(self.client, "alice")
        resp = self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/settings"),
            "phone": "123"}, follow_redirects=True)
        # Apostrophe is HTML-escaped in the rendered flash message.
        self.assertIn("That phone number", resp.get_data(as_text=True))
        self.assertEqual(len(self.sent_sms), 0)

    def test_link_resend_within_a_minute_blocked(self):
        self.make_client()
        signup(self.client, "alice")
        data = {"csrf_token": csrf(self.client, "/settings"),
                "phone": "5550101000"}
        self.client.post("/settings/phone", data=data,
                         follow_redirects=False)
        resp = self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/settings"),
            "phone": "5550101000"}, follow_redirects=True)
        self.assertIn("Wait a minute", resp.get_data(as_text=True))
        self.assertEqual(len(self.sent_sms), 1)

    def test_link_phone_unconfigured_provider_plain_error(self):
        self.make_client(providers=False)
        signup(self.client, "alice")
        # No provider configured means the settings page renders no
        # send-code forms at all, so take the csrf token from the
        # dashboard instead.
        resp = self.client.post("/settings/phone", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "phone": "5550101000"}, follow_redirects=True)
        html = resp.get_data(as_text=True)
        self.assertIn("not set up yet", html)
        self.assertEqual(len(self.sent_sms), 0)
        count = self.db().execute(
            "SELECT COUNT(*) c FROM verification_codes").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_link_email_unconfigured_provider_plain_error(self):
        self.make_client(providers=False)
        signup(self.client, "alice")
        resp = self.client.post("/settings/email", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "email": "alice@example.com"}, follow_redirects=True)
        self.assertIn("not set up yet", resp.get_data(as_text=True))
        self.assertEqual(len(self.sent_email), 0)

    def test_settings_requires_login(self):
        self.make_client()
        self.assertEqual(self.client.get("/settings").status_code, 302)


class TestRecoveryFlow(RecoveryTestCase):
    def test_full_recovery_both_codes(self):
        self.make_client()
        self.link_both("alice")
        self.client.get("/logout")
        resp = self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.sent_email), 2)  # link + recovery
        self.assertEqual(len(self.sent_sms), 2)
        email_code = CODE_RE.search(self.sent_email[-1][2]).group(1)
        sms_code = CODE_RE.search(self.sent_sms[-1][1]).group(1)
        resp = self.client.post("/forgot-password/verify", data={
            "csrf_token": csrf(self.client, "/forgot-password/verify"),
            "email_code": email_code, "sms_code": sms_code},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        resp = self.client.post("/forgot-password/reset", data={
            "csrf_token": csrf(self.client, "/forgot-password/reset"),
            "password": "newpassword123"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        # New password works, old one does not.
        resp = login(self.client, "alice", "newpassword123")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.client.get("/logout")
        login(self.client, "alice", "password123")
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_recovery_one_code_insufficient(self):
        self.make_client()
        self.link_both("alice")
        self.client.get("/logout")
        self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=False)
        email_code = CODE_RE.search(self.sent_email[-1][2]).group(1)
        resp = self.client.post("/forgot-password/verify", data={
            "csrf_token": csrf(self.client, "/forgot-password/verify"),
            "email_code": email_code, "sms_code": "000000"},
            follow_redirects=True)
        self.assertIn("wrong or expired", resp.get_data(as_text=True))
        # Password unchanged: old password still logs in.
        self.client.get("/logout")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_recovery_missing_contact_rejected_generically(self):
        self.make_client()
        signup(self.client, "alice")  # no contacts linked at all
        self.client.get("/logout")
        resp = self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=True)
        html = resp.get_data(as_text=True)
        self.assertIn("not set up for this account", html)
        self.assertEqual(len(self.sent_email), 0)
        self.assertEqual(len(self.sent_sms), 0)

    def test_recovery_unknown_user_same_generic_message(self):
        self.make_client()
        resp = self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "nobody"}, follow_redirects=True)
        self.assertIn("not set up for this account",
                      resp.get_data(as_text=True))
        self.assertEqual(len(self.sent_email), 0)
        self.assertEqual(len(self.sent_sms), 0)

    def test_recovery_unconfigured_provider_plain_error(self):
        # Account verified earlier (rows set directly), providers absent.
        self.make_client(providers=False)
        signup(self.client, "alice")
        self.db().execute(
            "UPDATE users SET phone = '+15550101000', phone_verified = 1, "
            "email = 'alice@example.com', email_verified = 1 "
            "WHERE username = 'alice'")
        self.db().commit()
        self.client.get("/logout")
        resp = self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=True)
        self.assertIn("not available yet", resp.get_data(as_text=True))
        self.assertEqual(len(self.sent_email), 0)
        self.assertEqual(len(self.sent_sms), 0)

    def test_verify_and_reset_require_session_steps(self):
        self.make_client()
        self.assertEqual(
            self.client.get("/forgot-password/verify").status_code, 302)
        self.assertEqual(
            self.client.get("/forgot-password/reset").status_code, 302)
        # Verify page shows masked contacts, never full ones.
        self.link_both("alice")
        self.client.get("/logout")
        self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=False)
        resp = self.client.get("/forgot-password/verify")
        html = resp.get_data(as_text=True)
        self.assertIn("al***@example.com", html)
        self.assertIn("*** 1000", html)
        self.assertNotIn("alice@example.com", html)
        self.assertNotIn("+15550101000", html)

    def test_codes_never_appear_in_pages(self):
        self.make_client()
        self.link_both("alice")
        self.client.get("/logout")
        self.client.post("/forgot-password", data={
            "csrf_token": csrf(self.client, "/forgot-password"),
            "username": "alice"}, follow_redirects=False)
        email_code = CODE_RE.search(self.sent_email[-1][2]).group(1)
        sms_code = CODE_RE.search(self.sent_sms[-1][1]).group(1)
        html = self.client.get(
            "/forgot-password/verify").get_data(as_text=True)
        self.assertNotIn(email_code, html)
        self.assertNotIn(sms_code, html)


class TestMigration(RecoveryTestCase):
    def test_old_database_migrates_without_losing_users(self):
        """A database created with the old users table gains the new
        columns on open and keeps its rows (the live-database case)."""
        self.make_client()
        db = self.db()
        db.execute("DROP TABLE verification_codes")
        db.execute("ALTER TABLE users DROP COLUMN phone")
        db.execute("ALTER TABLE users DROP COLUMN email")
        db.execute("ALTER TABLE users DROP COLUMN phone_verified")
        db.execute("ALTER TABLE users DROP COLUMN email_verified")
        db.commit()
        path = self.app.config["TRADINGBOT_CONFIG"].database_path
        # Re-open through the real init path (fresh app, same file).
        from tradingbot.db import init_db
        conn = init_db(path)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES ('legacy', 'hash', 'salt')")
        conn.commit()
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertTrue({"phone", "email", "phone_verified",
                         "email_verified"} <= cols)
        row = conn.execute(
            "SELECT username, phone, email FROM users "
            "WHERE username = 'legacy'").fetchone()
        self.assertEqual(row["phone"], "")
        self.assertEqual(row["email"], "")
        conn.close()


if __name__ == "__main__":
    unittest.main()
