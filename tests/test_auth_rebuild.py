"""Fox auth rebuild (Pages 1 and 2): sign-up now collects the email
and the recovery question/answer/hint, and the new forgot-username
flow reveals the username on screen only after the signup email
plus the security answer both match — never by mail, never
revealing whether an email is registered. Wrong answers lock the
account like wrong passwords.
"""

import os
import re
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


class TestAuthOneScreen(AuthRebuildTestCase):
    """Follow-up: Pages 1 and 2 fit on ONE screen with no scrolling,
    and the fox images ship in the repo so the live app serves them.
    Long pages are untouched and keep scrolling."""

    def css(self):
        path = os.path.join(self.app.static_folder, "style.css")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def rule(self, css, selector):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "%s rule missing from style.css"
                             % selector)
        return m.group(1)

    def test_auth_pages_use_fixed_topbar(self):
        for path in ("/login", "/signup", "/forgot-username"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertIn('class="topbar fox-topbar"',
                          resp.get_data(as_text=True))

    def test_auth_column_is_exactly_one_screen(self):
        body = self.rule(self.css(), ".fox-auth")
        self.assertIn("height: calc(100dvh - 40px)", body)
        self.assertIn("display: flex", body)
        self.assertIn("overflow: hidden", body)

    def test_hero_is_the_only_flexible_element(self):
        body = self.rule(self.css(), ".fox-auth > .fox-hero")
        self.assertIn("flex: 1 1 0", body)
        # The hero is HALF the size it was (owner 2026-11-11 ~05:52)
        # and raised off the very bottom: a small floor, a cap at
        # about half the old grown size, and a bottom margin.
        self.assertIn("min-height: clamp(70px, 12dvh, 120px)", body)
        self.assertIn("max-height: clamp(110px, 16dvh, 160px)", body)
        self.assertIn("margin-bottom: 2dvh", body)
        img = self.rule(self.css(), ".fox-hero img")
        self.assertIn("max-height: 100%", img)
        self.assertIn("max-width: 46%", img)

    def test_header_logo_and_wordmark_are_large(self):
        # Photo: LARGE red fox logo + VERY LARGE gold SolEntry wordmark.
        logo = self.rule(self.css(), ".fox-auth .fox-logo")
        self.assertIn("clamp(72px, 12dvh, 112px)", logo)
        wordmark = self.rule(self.css(), ".fox-wordmark")
        self.assertIn("clamp(40px, 8dvh, 66px)", wordmark)

    def test_motto_is_large_red_italic(self):
        tagline = self.rule(self.css(), ".fox-tagline")
        # Half the size it was (owner 2026-11-11 ~05:52) — still
        # large red italic letters beside the fox.
        self.assertIn("clamp(20px, 3.5dvh, 30px)", tagline)
        self.assertIn("#ff2d20", tagline)
        self.assertIn("font-style: italic", tagline)
        self.assertIn("font-weight: 900", tagline)
        # The motto is exactly as written on both auth pages.
        for path in ("/login", "/signup"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("Still on<br>the $GATE?", html)

    def test_short_screens_still_fit_one_screen(self):
        # Page 2 has six fields: on a short viewport the brand and
        # the hero floor shrink so the page still does not scroll.
        css = self.css()
        self.assertIn("@media (max-height: 700px)", css)
        self.assertIn("min-height: clamp(60px, 10dvh, 100px)", css)
        self.assertIn("max-height: clamp(80px, 12dvh, 120px)", css)
        # The one-screen rules themselves are unchanged.
        body = self.rule(css, ".fox-auth")
        self.assertIn("height: calc(100dvh - 40px)", body)
        self.assertIn("overflow: hidden", body)

    def test_logo_is_viewport_relative(self):
        body = self.rule(self.css(), ".fox-auth .fox-logo")
        self.assertIn("clamp(", body)
        self.assertIn("dvh", body)

    def test_fox_images_exist_in_the_repo(self):
        for name in ("fox-red-480.jpg", "fox-gold-480.jpg"):
            path = os.path.join(self.app.static_folder, name)
            self.assertTrue(os.path.exists(path), "%s missing" % name)
            with open(path, "rb") as f:
                head = f.read(3)
            self.assertEqual(head, b"\xff\xd8\xff",
                             "%s is not a JPEG" % name)

    def test_fox_images_are_the_realistic_photo_foxes(self):
        # The photo's realistic foxes are the official mascots:
        # red 34,875 bytes — the yellow fox's face recolored red
        # (2026-11-11, a 308x308 square with NO baked-in gold
        # circle; the CSS border draws the single yellow circle),
        # gold 38,514 bytes. The flat logo foxes (red 21,142 /
        # gold 41,204 bytes) were deleted — these exact sizes prove
        # they did not come back.
        sizes = {"fox-red-480.jpg": 34875, "fox-gold-480.jpg": 38514}
        for name, size in sizes.items():
            path = os.path.join(self.app.static_folder, name)
            self.assertEqual(os.path.getsize(path), size,
                             "%s is not the realistic photo fox" % name)

    def test_red_fox_is_square_so_the_circle_shows_the_whole_face(self):
        # The top logo is a circle (border-radius: 50% + object-fit:
        # cover). A rectangular fox gets cropped to half a face; the
        # square 308x308 recolored fox shows the full face + cigar,
        # and the CSS border draws the single yellow circle. Read
        # the JPEG dimensions from the SOF marker.
        path = os.path.join(self.app.static_folder, "fox-red-480.jpg")
        with open(path, "rb") as f:
            data = f.read()
        idx = data.find(b"\xff\xc0")
        self.assertGreater(idx, 0, "no JPEG SOF marker in fox-red-480.jpg")
        height = int.from_bytes(data[idx + 5:idx + 7], "big")
        width = int.from_bytes(data[idx + 7:idx + 9], "big")
        self.assertEqual((width, height), (308, 308),
                         "red fox is not the square crop")

    def test_auth_pages_reference_both_foxes(self):
        for path in ("/login", "/signup", "/forgot-username"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("fox-red-480.jpg", html)
            self.assertIn("fox-gold-480.jpg", html)

    def test_long_pages_still_scroll(self):
        css = self.css()
        self.assertNotIn("overflow", self.rule(css, "body"))
        self.assertNotIn("overflow", self.rule(css, ".wrap"))


if __name__ == "__main__":
    unittest.main()
