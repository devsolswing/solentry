"""Security hardening tests: key wiping, anti-bot, sessions, wallet
protections, audit log.

Key material in these tests is always throwaway (generated here, never
funded, never on-chain). No real SOL moves.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from unittest import mock

from helpers import csrf, login, make_client, signup
from test_executor import ExecutorBase, FakePumpRpc, make_curve
from tradingbot import rate_limit as rate_limit_mod
from tradingbot.executor import _execute_buy, execute_real_trade
from tradingbot.security import (decrypt_secret_mutable, encrypt_secret,
                                 temporary_secret, verify_turnstile,
                                 wipe_secret)
from tradingbot.solana import generate_keypair


class TestKeyWipe(unittest.TestCase):
    def test_wipe_secret_zeroes_bytearray_in_place(self):
        buf = bytearray(b"top-secret-key-material")
        wipe_secret(buf)
        self.assertEqual(len(buf), 23)
        self.assertEqual(bytes(buf), b"\x00" * 23)

    def test_temporary_secret_wipes_on_success(self):
        key = b"k" * 32
        blob = encrypt_secret(b"a" * 64, key)
        seen = []
        with temporary_secret(blob, key) as secret:
            self.assertIsInstance(secret, bytearray)
            self.assertEqual(len(secret), 64)
            seen.append(secret)
        self.assertEqual(bytes(seen[0]), b"\x00" * 64)

    def test_temporary_secret_wipes_on_exception(self):
        key = b"k" * 32
        blob = encrypt_secret(b"b" * 64, key)
        seen = []
        with self.assertRaises(RuntimeError):
            with temporary_secret(blob, key) as secret:
                seen.append(secret)
                raise RuntimeError("boom")
        self.assertEqual(bytes(seen[0]), b"\x00" * 64)

    def test_decrypt_secret_mutable_is_wipable(self):
        key = b"k" * 32
        blob = encrypt_secret(b"c" * 64, key)
        secret = decrypt_secret_mutable(blob, key)
        self.assertIsInstance(secret, bytearray)
        self.assertEqual(bytes(secret), b"c" * 64)
        wipe_secret(secret)
        self.assertEqual(bytes(secret), b"\x00" * 64)

    def test_generate_keypair_returns_wipable_buffers(self):
        seed, _pubkey, secret64 = generate_keypair()
        self.assertIsInstance(seed, bytearray)
        self.assertIsInstance(secret64, bytearray)
        wipe_secret(seed)
        wipe_secret(secret64)
        self.assertEqual(bytes(seed), b"\x00" * 32)
        self.assertEqual(bytes(secret64), b"\x00" * 64)


class TestExecutorWipe(ExecutorBase):
    """The seed the executor signs with must be zeroed in place after
    the call — on success, on refusal, and on failure."""

    def _capture_seed(self):
        captured = {}

        def fake_sign(seed, message):
            captured["seed"] = seed
            from tradingbot.solana import sign_message as real_sign
            return real_sign(bytes(seed), message)

        return captured, fake_sign

    def test_seed_wiped_after_successful_buy(self):
        captured, fake_sign = self._capture_seed()
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fill_tokens = 1_000_000
        with mock.patch("tradingbot.executor.sign_message", fake_sign):
            result = execute_real_trade(
                self._bot(), self.user_id, self.mint, "buy", 0.01,
                0.0, self.db, self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertIn("seed", captured)
        self.assertEqual(bytes(captured["seed"]), b"\x00" * 32)

    def test_seed_wiped_when_transaction_not_confirmed(self):
        captured, fake_sign = self._capture_seed()

        class FailingConfirmRpc(FakePumpRpc):
            def confirm_transaction(self, signature):
                return False

        rpc = FailingConfirmRpc(self.pubkey, self.mint,
                                curve=make_curve())
        rpc.fill_tokens = 1_000_000
        with mock.patch("tradingbot.executor.sign_message", fake_sign):
            result = execute_real_trade(
                self._bot(), self.user_id, self.mint, "buy", 0.01,
                0.0, self.db, self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("seed", captured)
        self.assertEqual(bytes(captured["seed"]), b"\x00" * 32)

    def test_seed_wiped_on_refusal_path(self):
        # Migrated curve: the buy is refused before signing, but the
        # loaded seed must still be scrubbed.
        seed = bytearray(b"\x07" * 32)
        rpc = FakePumpRpc(self.pubkey, self.mint,
                          curve=make_curve(complete=True))
        with mock.patch("tradingbot.executor._load_wallet",
                        return_value=(self.pubkey, seed)):
            result = _execute_buy(self._bot(), self.user_id, self.mint,
                                  0.01, self.db, self.cfg, rpc,
                                  {"support_id": self.support_id})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(bytes(seed), b"\x00" * 32)


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_hit_window_and_reset(self):
        key = "test:key"
        for _ in range(3):
            self.assertTrue(
                rate_limit_mod.record_hit(self.db, key, 3, 60))
        self.assertFalse(
            rate_limit_mod.record_hit(self.db, key, 3, 60))
        self.assertEqual(
            rate_limit_mod.hits_in_window(self.db, key, 60), 3)
        rate_limit_mod.reset_key(self.db, key)
        self.assertEqual(
            rate_limit_mod.hits_in_window(self.db, key, 60), 0)
        self.assertTrue(
            rate_limit_mod.record_hit(self.db, key, 3, 60))

    def _create_user(self, username, password="password123"):
        from tradingbot.security import hash_password
        salt, pw_hash = hash_password(password)
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt) "
            "VALUES (?, ?, ?)", (username, pw_hash, salt))
        self.db.commit()

    def test_login_ip_fail_limit(self):
        # 10 accounts x 2 failed logins = 20 IP failures (each
        # account stays under its own 5-fail lockout and the
        # 3-fail anomaly threshold). The 21st attempt from the same
        # IP — even with the right password — is refused.
        for i in range(10):
            self._create_user(f"u{i}")
        for i in range(10):
            for _ in range(2):
                token = csrf(self.client, "/login")
                resp = self.client.post("/login", data={
                    "csrf_token": token, "username": f"u{i}",
                    "password": "wrong-password",
                }, follow_redirects=True)
                self.assertIn(b"Wrong username or password",
                              resp.get_data())
        self._create_user("u10")
        token = csrf(self.client, "/login")
        resp = self.client.post("/login", data={
            "csrf_token": token, "username": "u10",
            "password": "password123",
        }, follow_redirects=True)
        self.assertIn(b"Too many failed tries from this address",
                      resp.get_data())
        # Not logged in: the dashboard sends us back to login.
        resp = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_signup_ip_limit(self):
        for i in range(10):
            token = csrf(self.client, "/signup")
            resp = self.client.post("/signup", data={
                "csrf_token": token, "username": f"s{i}",
                "password": "password123",
            }, follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
        token = csrf(self.client, "/signup")
        resp = self.client.post("/signup", data={
            "csrf_token": token, "username": "s10",
            "password": "password123",
        }, follow_redirects=True)
        self.assertIn(b"Too many signups from this address",
                      resp.get_data())
        row = self.db.execute(
            "SELECT id FROM users WHERE username = 's10'").fetchone()
        self.assertIsNone(row)


class TestHoneypot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_signup_honeypot_rejected(self):
        token = csrf(self.client, "/signup")
        resp = self.client.post("/signup", data={
            "csrf_token": token, "username": "botty",
            "password": "password123", "website": "http://spam.example",
        }, follow_redirects=True)
        self.assertIn(b"Something looks wrong", resp.get_data())
        row = self.db.execute(
            "SELECT id FROM users WHERE username = 'botty'").fetchone()
        self.assertIsNone(row)

    def test_login_honeypot_rejected(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        token = csrf(self.client, "/login")
        resp = self.client.post("/login", data={
            "csrf_token": token, "username": "alice",
            "password": "password123",
            "website": "http://spam.example",
        }, follow_redirects=True)
        self.assertIn(b"Something looks wrong", resp.get_data())
        resp = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_honeypot_field_present_on_forms(self):
        for path in ("/login", "/signup"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('name="website"', html)


class TestSecurityHeaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_headers_present_on_login(self):
        resp = self.client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("nonce-", csp)
        self.assertEqual(
            resp.headers.get("Strict-Transport-Security"),
            "max-age=31536000; includeSubDomains")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"),
                         "nosniff")
        self.assertEqual(resp.headers.get("Referrer-Policy"),
                         "strict-origin-when-cross-origin")

    def test_csp_nonce_changes_per_request(self):
        def nonce(resp):
            csp = resp.headers.get("Content-Security-Policy", "")
            m = re.search(r"'nonce-([^']+)'", csp)
            return m.group(1) if m else None
        n1 = nonce(self.client.get("/login"))
        n2 = nonce(self.client.get("/login"))
        self.assertTrue(n1)
        self.assertTrue(n2)
        self.assertNotEqual(n1, n2)

    def test_no_inline_onclick_handlers(self):
        for path in ("/login", "/signup"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("onclick=", html)


class TestSessionHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _session_cookie(self, resp):
        for header in resp.headers.getlist("Set-Cookie"):
            m = re.match(r"session=([^;]+)", header)
            if m:
                return header, m.group(1)
        return None, None

    def test_session_rotates_on_login(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        _h1, before = self._session_cookie(self.client.get("/login"))
        self.assertTrue(before)
        resp = login(self.client, "alice")
        _h2, after = self._session_cookie(resp)
        self.assertTrue(after)
        self.assertNotEqual(before, after)

    def test_session_cookie_flags_and_lifetime(self):
        signup(self.client, "alice")
        header, _value = self._session_cookie(
            self.client.get("/dashboard"))
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)
        self.assertIn("Secure", header)
        # Permanent session: the cookie carries an expiry.
        self.assertIn("Expires=", header)


class TestAnomalyLockout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def _fail_login(self, username, ip):
        token = csrf(self.client, "/login")
        return self.client.post("/login", data={
            "csrf_token": token, "username": username,
            "password": "wrong-password",
        }, follow_redirects=True,
            headers={"X-Forwarded-For": ip})

    def test_three_fails_from_new_ip_locks_account(self):
        signup(self.client, "alice")
        self.client.get("/logout")
        for _ in range(3):
            resp = self._fail_login("alice", "203.0.113.7")
            self.assertIn(b"Wrong username or password",
                          resp.get_data())
        # The 3rd failure tripped the anomaly lock: even the right
        # password is now refused.
        token = csrf(self.client, "/login")
        resp = self.client.post("/login", data={
            "csrf_token": token, "username": "alice",
            "password": "password123",
        }, follow_redirects=True,
            headers={"X-Forwarded-For": "203.0.113.8"})
        self.assertIn(b"Too many failed tries. Locked for 15 minutes.",
                      resp.get_data())
        # And the lock left a suspicious-activity audit row.
        row = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = "
            "(SELECT id FROM users WHERE username = 'alice') AND "
            "kind = 'suspicious_activity'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("failed logins", row["detail"])

    def test_fails_from_known_ip_do_not_lock(self):
        # One good login from this IP first, so it is recognised.
        signup(self.client, "alice")
        resp = login(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.client.get("/logout")
        for _ in range(3):
            resp = self._fail_login("alice", "127.0.0.1")
            self.assertIn(b"Wrong username or password",
                          resp.get_data())
        # No anomaly lock: the right password still works.
        resp = login(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])


class TestDailySendLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]
        signup(self.client, "alice")
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.dest = "BaeDmurhWuyNsvnXKXfqb9oiT97cPtDmNHLN4BZFZ1Gi"

    def tearDown(self):
        self.tmp.cleanup()

    def _create_intent(self, amount):
        token = csrf(self.client, "/wallet/send")
        return self.client.post("/wallet/send", data={
            "csrf_token": token, "destination": self.dest,
            "amount": str(amount), "token": "SOL",
        }, follow_redirects=True)

    def test_daily_limit_enforced_at_creation(self):
        self._create_intent(2.5)
        self._create_intent(2.5)
        resp = self._create_intent(0.1)
        self.assertIn(b"Daily send limit is 5 SOL", resp.get_data())
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM send_intents WHERE user_id = ?",
            (self.user_id,)).fetchall()
        self.assertEqual(rows[0]["n"], 2)

    def test_daily_limit_rechecked_at_confirm(self):
        self._create_intent(2.0)
        # A second intent slipped past creation (inserted directly,
        # as if created before the limit existed): 4 SOL more today.
        self.db.execute(
            "INSERT INTO send_intents (user_id, destination, "
            "amount_lamports, token, status, created_at) "
            "VALUES (?, ?, 4000000000, 'SOL', 'pending', "
            "datetime('now'))",
            (self.user_id, self.dest))
        self.db.commit()
        intent_id = self.db.execute(
            "SELECT id FROM send_intents WHERE user_id = ? ORDER BY "
            "id ASC LIMIT 1", (self.user_id,)).fetchone()["id"]
        token = csrf(
            self.client, f"/wallet/send/{intent_id}/confirm")
        resp = self.client.post(
            f"/wallet/send/{intent_id}/confirm", data={
                "csrf_token": token,
            }, follow_redirects=True)
        self.assertIn(b"Daily send limit is 5 SOL", resp.get_data())
        row = self.db.execute(
            "SELECT status FROM send_intents WHERE id = ?",
            (intent_id,)).fetchone()
        self.assertEqual(row["status"], "failed")


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_wallet_actions_carry_ip_and_device(self):
        signup(self.client, "alice")
        user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        token = csrf(self.client, "/dashboard")
        self.client.post("/mode", data={
            "csrf_token": token, "mode": "devnet",
        }, follow_redirects=True)
        row = self.db.execute(
            "SELECT ip, device FROM activity WHERE user_id = ? AND "
            "kind = 'mode_switch' ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ip"], "127.0.0.1")
        self.assertIn("Werkzeug", row["device"])


class TestTurnstile(unittest.TestCase):
    def test_off_without_keys(self):
        # No Turnstile keys in the test config: login works and no
        # challenge widget renders.
        with tempfile.TemporaryDirectory() as tmp:
            _app, client = make_client(tmp)
            html = client.get("/login").get_data(as_text=True)
            self.assertNotIn("cf-turnstile", html)
            signup(client, "alice")
            resp = client.get("/dashboard", follow_redirects=False)
            self.assertEqual(resp.status_code, 200)

    def test_verify_turnstile_network_failure_is_false(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=Exception("no network")):
            self.assertFalse(verify_turnstile("tok", "secret"))

    def test_verify_turnstile_success_response(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"success": true}'

        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp()):
            self.assertTrue(verify_turnstile("tok", "secret"))

    def test_verify_turnstile_rejected_response(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"success": false}'

        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp()):
            self.assertFalse(verify_turnstile("tok", "secret"))


if __name__ == "__main__":
    unittest.main()
