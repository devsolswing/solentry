"""Fox auth rebuild (Pages 1 and 2): sign-up now collects the email
and the recovery question/answer/hint, and the new forgot-username
flow reveals the username on screen only after the signup email
plus the security answer both match — never by mail, never
revealing whether an email is registered. Wrong answers lock the
account like wrong passwords.
"""

import tempfile
import unittest

from helpers import csrf, login, make_client, signup


class AuthRebuildTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return self.app.config["DB"]

    def signup_full(self, username="alice", email="alice@example.com",
                    question="First pet?", answer="fluffy", hint="the dog"):
        token = csrf(self.client, "/signup")
        return self.client.post("/signup", data={
            "csrf_token": token, "username": username,
            "password": "password123", "email": email,
            "question": question, "answer": answer, "hint": hint},
            follow_redirects=False)

    def start_forgot_username(self, email="alice@example.com"):
        token = csrf(self.client, "/forgot-username")
        return self.client.post("/forgot-username", data={
            "csrf_token": token, "email": email},
            follow_redirects=False)

    def answer_username_question(self, answer):
        token = csrf(self.client, "/forgot-username")
        return self.client.post("/forgot-username", data={
            "csrf_token": token, "answer": answer},
            follow_redirects=False)


class TestSignupFields(AuthRebuildTestCase):
    def test_signup_stores_email_and_recovery(self):
        resp = self.signup_full()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])
        row = self.db().execute(
            "SELECT email, recovery_question, recovery_answer_hash, "
            "recovery_hint FROM users WHERE username = 'alice'"
            ).fetchone()
        self.assertEqual(row["email"], "alice@example.com")
        self.assertEqual(row["recovery_question"], "First pet?")
        self.assertEqual(row["recovery_hint"], "the dog")
        # The answer is hashed, never stored in plaintext.
        self.assertNotEqual(row["recovery_answer_hash"], "fluffy")
        self.assertNotIn("fluffy", row["recovery_answer_hash"])

    def test_signup_username_password_only_still_works(self):
        signup(self.client, "bob")
        self.client.get("/logout")
        resp = login(self.client, "bob")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_signup_bad_email_rejected(self):
        resp = self.signup_full(email="not-an-email")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/signup", resp.headers["Location"])
        page = self.client.get("/signup", follow_redirects=True)
        self.assertIn("Enter a valid email address.",
                      page.get_data(as_text=True))
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)

    def test_signup_question_without_answer_rejected(self):
        resp = self.signup_full(answer="")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/signup", resp.headers["Location"])
        page = self.client.get("/signup", follow_redirects=True)
        self.assertIn("security answer must not be empty",
                      page.get_data(as_text=True))
        row = self.db().execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNone(row)


class TestForgotUsername(AuthRebuildTestCase):
    def test_page_renders_with_page_19_chip(self):
        resp = self.client.get("/forgot-username")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('class="page-id">Page 19<', html)
        self.assertIn("Forgot username", html)
        self.assertIn('name="email"', html)

    def test_full_flow_reveals_username(self):
        self.signup_full()
        self.client.get("/logout")
        resp = self.start_forgot_username()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-username", resp.headers["Location"])
        page = self.client.get("/forgot-username")
        html = page.get_data(as_text=True)
        self.assertIn("First pet?", html)
        self.assertIn("the dog", html)
        resp = self.answer_username_question("fluffy")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('class="fox-username"', html)
        self.assertIn("alice", html)
        # One-shot reveal: the next visit is back to the email form.
        page = self.client.get("/forgot-username")
        self.assertIn('name="email"',
                      page.get_data(as_text=True))

    def test_wrong_answer_does_not_reveal(self):
        self.signup_full()
        self.client.get("/logout")
        self.start_forgot_username()
        resp = self.answer_username_question("wrong")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-username", resp.headers["Location"])
        page = self.client.get("/forgot-username", follow_redirects=True)
        html = page.get_data(as_text=True)
        self.assertIn("That answer is wrong.", html)
        self.assertNotIn("alice", html)
        self.assertIn("First pet?", html)

    def test_five_wrong_answers_lock_the_account(self):
        self.signup_full()
        self.client.get("/logout")
        self.start_forgot_username()
        for _ in range(5):
            self.answer_username_question("wrong")
        resp = self.answer_username_question("wrong")
        self.assertIn("/login", resp.headers["Location"])
        resp = login(self.client, "alice", "password123")
        page = self.client.get("/login") if resp.status_code == 302 \
            else resp
        self.assertIn("Too many failed tries",
                      page.get_data(as_text=True))

    def test_unknown_email_is_one_generic_message(self):
        self.signup_full()
        self.client.get("/logout")
        resp = self.start_forgot_username("nobody@example.com")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-username", resp.headers["Location"])
        page = self.client.get("/forgot-username", follow_redirects=True)
        html = page.get_data(as_text=True)
        self.assertIn("Account recovery is not set up for this account.",
                      html)
        self.assertNotIn("First pet?", html)
        self.assertNotIn("alice", html)

    def test_email_without_question_is_one_generic_message(self):
        token = csrf(self.client, "/signup")
        self.client.post("/signup", data={
            "csrf_token": token, "username": "carol",
            "password": "password123",
            "email": "carol@example.com"}, follow_redirects=False)
        self.client.get("/logout")
        resp = self.start_forgot_username("carol@example.com")
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/forgot-username", follow_redirects=True)
        html = page.get_data(as_text=True)
        self.assertIn("Account recovery is not set up for this account.",
                      html)
        self.assertNotIn("carol", html)


if __name__ == "__main__":
    unittest.main()
