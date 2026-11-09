"""Recovery question + answer and passkey-related account flows.

The recovery answer is hashed with scrypt, like a password — never
stored in plaintext, never shown on any page, never logged. Tests
prove the hash is not the answer, the forgot-password flow works
end-to-end, wrong answers lock the account like wrong passwords, and
an account with no question set (or no such account at all) gets one
generic message that reveals nothing.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from helpers import csrf, login, make_client, signup
from tradingbot.security import verify_password


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return self.app.config["DB"]

    def set_question(self, username="alice", question="First pet?",
                     answer="fluffy", hint="the dog"):
        signup(self.client, username)
        self.client.post("/settings/recovery-question", data={
            "csrf_token": csrf(self.client, "/settings"),
            "question": question, "answer": answer, "hint": hint},
            follow_redirects=False)

    def start_forgot(self, username="alice"):
        token = csrf(self.client, "/forgot-password")
        return self.client.post("/forgot-password", data={
            "csrf_token": token, "username": username},
            follow_redirects=False)

    def answer_question(self, answer):
        token = csrf(self.client, "/forgot-password/answer")
        return self.client.post("/forgot-password/answer", data={
            "csrf_token": token, "answer": answer},
            follow_redirects=False)

    def reset_password(self, password="newpassword1"):
        token = csrf(self.client, "/forgot-password/reset")
        return self.client.post("/forgot-password/reset", data={
            "csrf_token": token, "password": password},
            follow_redirects=False)


class TestRecoveryQuestionSettings(RecoveryTestCase):
    def test_set_question_stores_hash_not_plaintext(self):
        self.set_question()
        row = self.db().execute(
            "SELECT recovery_question, recovery_answer_hash, "
            "recovery_answer_salt, recovery_hint FROM users "
            "WHERE username = 'alice'").fetchone()
        self.assertEqual(row["recovery_question"], "First pet?")
        self.assertEqual(row["recovery_hint"], "the dog")
        self.assertNotIn("fluffy", row["recovery_answer_hash"])
        self.assertNotIn("fluffy", row["recovery_answer_salt"])
        self.assertTrue(verify_password(
            "fluffy", row["recovery_answer_salt"],
            row["recovery_answer_hash"]))
        self.assertFalse(verify_password(
            "spot", row["recovery_answer_salt"],
            row["recovery_answer_hash"]))

    def test_settings_page_shows_question_and_hint(self):
        self.set_question()
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("First pet?", html)
        self.assertIn("the dog", html)
        self.assertNotIn("fluffy", html)

    def test_question_too_short_rejected(self):
        signup(self.client, "alice")
        resp = self.client.post("/settings/recovery-question", data={
            "csrf_token": csrf(self.client, "/settings"),
            "question": "Hi", "answer": "x", "hint": ""},
            follow_redirects=True)
        self.assertIn("3-200 characters",
                      resp.get_data(as_text=True))

    def test_empty_answer_rejected(self):
        signup(self.client, "alice")
        resp = self.client.post("/settings/recovery-question", data={
            "csrf_token": csrf(self.client, "/settings"),
            "question": "First pet?", "answer": "   ", "hint": ""},
            follow_redirects=True)
        self.assertIn("must not be empty",
                      resp.get_data(as_text=True))

    def test_settings_requires_login(self):
        resp = self.client.get("/settings", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)


class TestForgotPasswordFlow(RecoveryTestCase):
    def test_full_flow_end_to_end(self):
        self.set_question()
        self.client.get("/logout", follow_redirects=False)
        resp = self.start_forgot()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password/answer", resp.headers["Location"])
        page = self.client.get("/forgot-password/answer")
        html = page.get_data(as_text=True)
        self.assertIn("First pet?", html)
        self.assertIn("the dog", html)
        self.assertNotIn("fluffy", html)
        resp = self.answer_question("fluffy")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password/reset", resp.headers["Location"])
        resp = self.reset_password("newpassword1")
        self.assertEqual(resp.status_code, 302)
        # New password works, old one does not.
        resp = login(self.client, "alice", "newpassword1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        self.client.get("/logout", follow_redirects=False)
        resp = login(self.client, "alice", "password123")
        self.assertNotIn("/dashboard", resp.headers.get("Location", ""))

    def test_wrong_answer_is_generic_and_stays_on_question(self):
        self.set_question()
        self.client.get("/logout", follow_redirects=False)
        self.start_forgot()
        resp = self.answer_question("wrong")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password/answer",
                      resp.headers["Location"])
        page = self.client.get("/forgot-password/answer",
                               follow_redirects=True)
        self.assertIn("That answer is wrong.",
                      page.get_data(as_text=True))

    def test_five_wrong_answers_lock_the_account(self):
        self.set_question()
        self.client.get("/logout", follow_redirects=False)
        self.start_forgot()
        for _ in range(5):
            self.answer_question("wrong")
        resp = self.answer_question("wrong")
        self.assertIn("/login", resp.headers["Location"])
        # The lockout also blocks password login, like a login lockout.
        resp = login(self.client, "alice", "password123")
        self.assertIn("Too many failed tries",
                      self.client.get("/login").get_data(as_text=True)
                      if resp.status_code == 302 else
                      resp.get_data(as_text=True))

    def test_correct_answer_after_lockout_clears_it(self):
        self.set_question()
        self.client.get("/logout", follow_redirects=False)
        self.start_forgot()
        for _ in range(5):
            self.answer_question("wrong")
        # Backdate the lockout so it has expired, then the right
        # answer works and the lockout is gone.
        self.db().execute(
            "UPDATE login_attempts SET locked_until = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1))
             .strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.db().commit()
        self.start_forgot()
        resp = self.answer_question("fluffy")
        self.assertIn("/forgot-password/reset", resp.headers["Location"])
        self.reset_password("newpassword1")
        resp = login(self.client, "alice", "newpassword1")
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_no_question_set_is_one_generic_message(self):
        signup(self.client, "alice")
        self.client.get("/logout", follow_redirects=False)
        resp = self.start_forgot()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password", resp.headers["Location"])
        page = self.client.get("/forgot-password", follow_redirects=True)
        self.assertIn("Account recovery is not set up for this account.",
                      page.get_data(as_text=True))

    def test_unknown_username_gets_the_same_message(self):
        self.start_forgot(username="nobody")
        page = self.client.get("/forgot-password", follow_redirects=True)
        self.assertIn("Account recovery is not set up for this account.",
                      page.get_data(as_text=True))

    def test_answer_page_needs_the_username_step(self):
        resp = self.client.get("/forgot-password/answer",
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password", resp.headers["Location"])

    def test_reset_page_needs_a_verified_answer(self):
        resp = self.client.get("/forgot-password/reset",
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password", resp.headers["Location"])

    def test_reset_password_too_short_rejected(self):
        self.set_question()
        self.client.get("/logout", follow_redirects=False)
        self.start_forgot()
        self.answer_question("fluffy")
        resp = self.reset_password("short")
        self.assertIn("/forgot-password/reset",
                      resp.headers["Location"])

    def test_password_login_still_works(self):
        signup(self.client, "alice")
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])


class TestRecoverySchemaMigration(RecoveryTestCase):
    def test_migration_adds_columns_to_an_old_database(self):
        # Simulate a database created before the recovery columns
        # existed, then re-initialise the app against it.
        db = self.db()
        db.execute("ALTER TABLE users DROP COLUMN recovery_question")
        db.execute("ALTER TABLE users DROP COLUMN recovery_answer_hash")
        db.execute("ALTER TABLE users DROP COLUMN recovery_answer_salt")
        db.execute("ALTER TABLE users DROP COLUMN recovery_hint")
        db.execute("DROP TABLE passkey_credentials")
        db.execute("DROP TABLE webauthn_challenges")
        db.commit()
        from tradingbot import create_app
        from helpers import make_config
        app2 = create_app(make_config(self.tmp.name))
        cols = {r[1] for r in app2.config["DB"].execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertTrue(
            {"recovery_question", "recovery_answer_hash",
             "recovery_answer_salt", "recovery_hint"} <= cols)
        tables = {r[0] for r in app2.config["DB"].execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"passkey_credentials",
                         "webauthn_challenges"} <= tables)
