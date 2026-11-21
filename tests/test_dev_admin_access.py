"""Dev_SolEntry admin access (James, 2026-11-15): the owner's
Dev_SolEntry account reaches the existing admin pages
(/admin/security, /admin/donations). Everyone else still gets 403,
and admin actions stay logged as before.

Why this exists: live, Dev_SolEntry got 403 on both admin pages —
the ADMIN_USERNAME env var / first-registered-user fallback pointed
elsewhere. The fix makes the owner's account pass the _is_admin
gate outright, in code, so no Railway variable has to change.
"""

import os
import tempfile
import unittest

from helpers import csrf, login, make_client, signup
from tradingbot import create_app
from tradingbot.config import Config


class DevAdminAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dev_solentry_reaches_admin_pages_even_when_not_first_user(self):
        # alice registers first, so the first-user fallback would
        # make HER the admin — Dev_SolEntry must still get in.
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        signup(self.client, "Dev_SolEntry", email="dev@example.com")
        self.client.get("/logout")
        login(self.client, "Dev_SolEntry")
        self.assertEqual(self.client.get("/admin/security").status_code,
                         200)
        self.assertEqual(self.client.get("/admin/donations").status_code,
                         200)

    def test_dev_solentry_reaches_admin_pages_when_env_names_someone_else(self):
        # The live failure mode: ADMIN_USERNAME set to another name.
        tmp = tempfile.TemporaryDirectory()
        env = {
            "SESSION_SECRET": "test-session-secret-" + "x" * 40,
            "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(tmp.name, "test2.db"),
            "ADMIN_USERNAME": "bob",
            # Tests only: the Security bot's codes are echoed in the
            # popup so signup can complete without SMTP.
            "SECURITY_CODE_DEV_ECHO": "1",
        }
        app2 = create_app(Config(env=env))
        app2.testing = True
        client2 = app2.test_client()
        signup(client2, "alice", email="alice@example.com")
        client2.get("/logout")
        signup(client2, "Dev_SolEntry", email="dev@example.com")
        client2.get("/logout")
        login(client2, "Dev_SolEntry")
        self.assertEqual(client2.get("/admin/security").status_code, 200)
        self.assertEqual(client2.get("/admin/donations").status_code, 200)
        tmp.cleanup()

    def test_other_users_still_get_403(self):
        signup(self.client, "alice", email="alice@example.com")
        self.client.get("/logout")
        signup(self.client, "bob", email="bob@example.com")
        # bob is not the admin (alice was first) and is not
        # Dev_SolEntry — both admin pages stay closed to him.
        self.assertEqual(self.client.get("/admin/security").status_code,
                         403)
        self.assertEqual(self.client.get("/admin/donations").status_code,
                         403)
        # The admin POST actions stay closed too.
        token = csrf(self.client, "/login")
        resp = self.client.post(
            "/admin/security/clear-lockout",
            data={"csrf_token": token, "username": "alice"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 403)
