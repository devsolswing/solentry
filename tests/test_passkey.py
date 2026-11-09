"""Passkey (WebAuthn) registration and sign-in.

A real authenticator cannot run in a test, so the library's
verification step is mocked — but everything around it is real: the
library's own option generation (which proves the library installs
and works with this config), the server-issued single-use challenges
stored in the database, the credential storage, the sign-count
update, the session login, and the failure paths. Password login is
also covered here to prove it still works alongside passkeys.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from helpers import csrf, login, make_client, signup
from tradingbot.config import Config
from webauthn.helpers import bytes_to_base64url

FAKE_CREDENTIAL_ID = b"fake-credential-id-1"
FAKE_PUBLIC_KEY = b"fake-public-key-bytes"


def _fake_registration(*args, **kwargs):
    return SimpleNamespace(
        credential_id=FAKE_CREDENTIAL_ID,
        credential_public_key=FAKE_PUBLIC_KEY,
        sign_count=0)


def _fake_authentication(*args, **kwargs):
    return SimpleNamespace(
        credential_id=FAKE_CREDENTIAL_ID,
        new_sign_count=1)


class PasskeyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return self.app.config["DB"]

    def user_id(self, username="alice"):
        return self.db().execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)).fetchone()["id"]

    def json_csrf(self, path):
        return csrf(self.client, path)

    def begin_register(self):
        token = self.json_csrf("/settings")
        return self.client.post(
            "/settings/passkey/register/begin", json={
                "csrf_token": token})

    def finish_register(self, credential=None):
        token = self.json_csrf("/settings")
        return self.client.post(
            "/settings/passkey/register/finish", json={
                "csrf_token": token,
                "credential": credential or {
                    "id": bytes_to_base64url(FAKE_CREDENTIAL_ID),
                    "rawId": bytes_to_base64url(FAKE_CREDENTIAL_ID),
                    "type": "public-key",
                    "response": {"clientDataJSON": "AAAA",
                                 "attestationObject": "AAAA"}},
                "transports": ["internal"]})

    def register_passkey(self, username="alice"):
        signup(self.client, username)
        with mock.patch("tradingbot.routes.verify_registration_response",
                        _fake_registration):
            resp = self.begin_register()
            self.assertEqual(resp.status_code, 200)
            resp = self.finish_register()
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])

    def begin_login(self, username="alice"):
        token = self.json_csrf("/login")
        return self.client.post(
            "/login/passkey/begin", json={
                "csrf_token": token, "username": username})

    def finish_login(self, username="alice"):
        token = self.json_csrf("/login")
        uid = self.user_id(username)
        return self.client.post(
            "/login/passkey/finish", json={
                "csrf_token": token,
                "credential": {
                    "id": bytes_to_base64url(FAKE_CREDENTIAL_ID),
                    "rawId": bytes_to_base64url(FAKE_CREDENTIAL_ID),
                    "type": "public-key",
                    "response": {
                        "clientDataJSON": "AAAA",
                        "authenticatorData": "AAAA",
                        "signature": "AAAA",
                        "userHandle": bytes_to_base64url(
                            str(uid).encode("utf-8"))}}})


class TestPasskeyLibrary(unittest.TestCase):
    """The webauthn library installs, imports, and generates real
    ceremony options with this app's config (no authenticator needed
    for option generation)."""

    def test_options_generation(self):
        from webauthn import (generate_authentication_options,
                              generate_registration_options)
        from webauthn.helpers import options_to_json_dict
        reg = generate_registration_options(
            rp_id="localhost", rp_name="SolEntry",
            user_name="alice", user_id=b"1")
        reg_json = options_to_json_dict(reg)
        self.assertEqual(reg_json["rp"]["id"], "localhost")
        self.assertEqual(reg_json["rp"]["name"], "SolEntry")
        self.assertIn("challenge", reg_json)
        auth = generate_authentication_options(rp_id="localhost")
        self.assertIn("challenge", options_to_json_dict(auth))

    def test_config_defaults_and_overrides(self):
        cfg = Config(env={})
        self.assertEqual(cfg.webauthn_rp_id, "localhost")
        self.assertEqual(cfg.webauthn_rp_name, "SolEntry")
        self.assertEqual(cfg.webauthn_origin, "http://localhost:5000")
        cfg = Config(env={
            "WEBAUTHN_RP_ID": "solentry-production.up.railway.app",
            "WEBAUTHN_RP_NAME": "SolEntry",
            "WEBAUTHN_ORIGIN":
                "https://solentry-production.up.railway.app"})
        self.assertEqual(cfg.webauthn_rp_id,
                         "solentry-production.up.railway.app")
        self.assertEqual(cfg.webauthn_origin,
                         "https://solentry-production.up.railway.app")


class TestPasskeyChallenges(PasskeyTestCase):
    def test_challenge_is_single_use(self):
        from tradingbot import passkey as passkey_mod
        signup(self.client, "alice")
        db = self.db()
        uid = self.user_id()
        challenge = passkey_mod.issue_challenge(db, uid, "login")
        self.assertTrue(passkey_mod.take_challenge(db, uid, "login"))
        self.assertIsNone(passkey_mod.take_challenge(db, uid, "login"))
        self.assertIsNotNone(challenge)

    def test_challenge_expires(self):
        from tradingbot import passkey as passkey_mod
        signup(self.client, "alice")
        db = self.db()
        passkey_mod.issue_challenge(db, self.user_id(), "login")
        db.execute(
            "UPDATE webauthn_challenges SET expires_at = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1))
             .strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.commit()
        self.assertIsNone(passkey_mod.take_challenge(
            db, self.user_id(), "login"))

    def test_challenge_is_scoped_by_purpose(self):
        from tradingbot import passkey as passkey_mod
        signup(self.client, "alice")
        db = self.db()
        uid = self.user_id()
        passkey_mod.issue_challenge(db, uid, "register")
        self.assertIsNone(passkey_mod.take_challenge(db, uid, "login"))
        self.assertIsNotNone(passkey_mod.take_challenge(db, uid, "register"))


class TestPasskeyRegistration(PasskeyTestCase):
    def test_register_stores_public_key_only(self):
        self.register_passkey()
        row = self.db().execute(
            "SELECT credential_id, public_key, sign_count, transports "
            "FROM passkey_credentials WHERE user_id = ?",
            (self.user_id(),)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row["credential_id"],
            bytes_to_base64url(FAKE_CREDENTIAL_ID))
        self.assertEqual(
            row["public_key"], bytes_to_base64url(FAKE_PUBLIC_KEY))
        self.assertEqual(row["sign_count"], 0)
        self.assertEqual(row["transports"], '["internal"]')

    def test_register_requires_login(self):
        resp = self.client.post(
            "/settings/passkey/register/begin", json={"csrf_token": "x"})
        self.assertEqual(resp.status_code, 302)

    def test_register_finish_without_begin_fails(self):
        signup(self.client, "alice")
        with mock.patch("tradingbot.routes.verify_registration_response",
                        _fake_registration):
            resp = self.finish_register()
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Challenge expired",
                          resp.get_json()["error"])

    def test_register_finish_replays_challenge_fails(self):
        signup(self.client, "alice")
        with mock.patch("tradingbot.routes.verify_registration_response",
                        _fake_registration):
            self.begin_register()
            self.assertEqual(self.finish_register().status_code, 200)
            # Same ceremony cannot be finished twice: the challenge
            # was consumed on the first finish.
            resp = self.finish_register()
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Challenge expired",
                          resp.get_json()["error"])

    def test_register_verify_failure_is_generic(self):
        signup(self.client, "alice")
        with mock.patch(
                "tradingbot.routes.verify_registration_response",
                side_effect=Exception("bad attestation")):
            self.begin_register()
            resp = self.finish_register()
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"],
                             "Passkey verification failed.")

    def test_register_same_passkey_twice_fails(self):
        signup(self.client, "alice")
        with mock.patch("tradingbot.routes.verify_registration_response",
                        _fake_registration):
            self.begin_register()
            self.assertEqual(self.finish_register().status_code, 200)
            self.begin_register()
            resp = self.finish_register()
            self.assertEqual(resp.status_code, 400)
            self.assertIn("already registered",
                          resp.get_json()["error"])

    def test_delete_passkey(self):
        self.register_passkey()
        row = self.db().execute(
            "SELECT id FROM passkey_credentials").fetchone()
        resp = self.client.post(
            f"/settings/passkey/{row['id']}/delete", data={
                "csrf_token": csrf(self.client, "/settings")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        count = self.db().execute(
            "SELECT COUNT(*) c FROM passkey_credentials"
            ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_delete_only_own_passkey(self):
        self.register_passkey("alice")
        row = self.db().execute(
            "SELECT id FROM passkey_credentials").fetchone()
        self.client.get("/logout", follow_redirects=False)
        signup(self.client, "bob")
        self.client.post(
            f"/settings/passkey/{row['id']}/delete", data={
                "csrf_token": csrf(self.client, "/settings")},
            follow_redirects=False)
        count = self.db().execute(
            "SELECT COUNT(*) c FROM passkey_credentials"
            ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_settings_page_lists_passkey(self):
        self.register_passkey()
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Register a passkey", html)
        self.assertIn("Passkey", html)

    def test_login_page_has_passkey_button(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Sign in with passkey", html)
        self.assertIn("passkey.js", html)


class TestPasskeyLogin(PasskeyTestCase):
    def test_full_login_flow(self):
        self.register_passkey()
        self.client.get("/logout", follow_redirects=False)
        resp = self.begin_login()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("challenge", data)
        self.assertTrue(data["allowCredentials"])
        with mock.patch("tradingbot.routes.verify_authentication_response",
                        _fake_authentication):
            resp = self.finish_login()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        # Signed in: dashboard loads, and the sign count advanced.
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        row = self.db().execute(
            "SELECT sign_count FROM passkey_credentials").fetchone()
        self.assertEqual(row["sign_count"], 1)

    def test_login_begin_unknown_user(self):
        resp = self.begin_login(username="nobody")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No passkey is registered",
                      resp.get_json()["error"])

    def test_login_begin_user_without_passkey(self):
        signup(self.client, "alice")
        resp = self.begin_login()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No passkey is registered",
                      resp.get_json()["error"])

    def test_login_finish_without_begin_fails(self):
        self.register_passkey()
        self.client.get("/logout", follow_redirects=False)
        with mock.patch("tradingbot.routes.verify_authentication_response",
                        _fake_authentication):
            resp = self.finish_login()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Challenge expired", resp.get_json()["error"])

    def test_login_verify_failure_is_generic(self):
        self.register_passkey()
        self.client.get("/logout", follow_redirects=False)
        self.begin_login()
        with mock.patch(
                "tradingbot.routes.verify_authentication_response",
                side_effect=Exception("bad signature")):
            resp = self.finish_login()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "Passkey verification failed.")

    def test_login_unknown_credential_fails(self):
        self.register_passkey()
        self.client.get("/logout", follow_redirects=False)
        self.begin_login()
        token = self.json_csrf("/login")
        uid = self.user_id()
        resp = self.client.post(
            "/login/passkey/finish", json={
                "csrf_token": token,
                "credential": {
                    "id": bytes_to_base64url(b"some-other-credential"),
                    "rawId": bytes_to_base64url(b"some-other-credential"),
                    "type": "public-key",
                    "response": {
                        "clientDataJSON": "AAAA",
                        "authenticatorData": "AAAA",
                        "signature": "AAAA",
                        "userHandle": bytes_to_base64url(
                            str(uid).encode("utf-8"))}}})
        self.assertEqual(resp.status_code, 400)

    def test_password_login_still_works(self):
        self.register_passkey()
        self.client.get("/logout", follow_redirects=False)
        resp = login(self.client, "alice", "password123")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
