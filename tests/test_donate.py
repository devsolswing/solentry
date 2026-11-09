"""Donations: donation wallet, record endpoint, admin page."""

import tempfile
import unittest

from helpers import csrf, make_client, signup
from tradingbot import donations as donations_mod
from tradingbot.security import decrypt_secret
from tradingbot.solana import base58_encode, publickey_from_secret

# A well-formed fake 64-byte signature (base58 of 64 bytes whose first
# byte is nonzero, so it encodes to 87 chars like a real signature).
# It is NOT a real signature — only its shape is used.
FAKE_SIG = base58_encode(b"\x01" * 64)
FAKE_DONOR = "9WMzFGfMAiPUzNhTJU2jdyJQb5AM3GWroKBS7LCDVQ3H"


class TestDonationWallet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_wallet_generated_once_and_decrypts(self):
        first = donations_mod.get_or_create_donation_wallet(self.app)
        second = donations_mod.get_or_create_donation_wallet(self.app)
        self.assertEqual(first, second)
        self.assertTrue(30 <= len(first) <= 44)
        db = self.app.config["DB"]
        rows = db.execute("SELECT COUNT(*) c FROM donation_wallet"
                          ).fetchone()["c"]
        self.assertEqual(rows, 1)
        row = db.execute("SELECT pubkey, encrypted_secret FROM "
                         "donation_wallet WHERE id = 1").fetchone()
        # Encrypted blob must not contain the pubkey or key material.
        self.assertNotIn(row["pubkey"], row["encrypted_secret"])
        self.assertTrue(donations_mod.donation_wallet_decrypts(self.app))

    def test_wallet_secret_matches_pubkey(self):
        pubkey = donations_mod.get_or_create_donation_wallet(self.app)
        db = self.app.config["DB"]
        row = db.execute("SELECT encrypted_secret FROM donation_wallet "
                         "WHERE id = 1").fetchone()
        secret = decrypt_secret(row["encrypted_secret"],
                                self.app.config["TRADINGBOT_CONFIG"]
                                .encryption_key)
        self.assertEqual(
            base58_encode(publickey_from_secret(secret)), pubkey)

    def test_donate_info_endpoint(self):
        resp = self.client.get("/api/donate/info")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["network"], "devnet")
        self.assertTrue(30 <= len(body["donation_address"]) <= 44)
        self.assertIn("api.devnet.solana.com", body["rpc_url"])


class TestDonationRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, **overrides):
        body = {
            "csrf_token": csrf(self.client, "/login"),
            "donor_address": FAKE_DONOR,
            "amount_sol": 0.5,
            "signature": FAKE_SIG,
            "network": "devnet",
        }
        body.update(overrides)
        return self.client.post("/api/donate/record", json=body)

    def test_valid_record_accepted(self):
        resp = self._record()
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.get_json()["ok"])
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM donations").fetchone()
        self.assertEqual(row["donor_address"], FAKE_DONOR)
        self.assertEqual(row["amount_lamports"], 500_000_000)
        self.assertEqual(row["signature"], FAKE_SIG)

    def test_duplicate_signature_rejected(self):
        self.assertEqual(self._record().status_code, 201)
        resp = self._record()
        self.assertEqual(resp.status_code, 409)
        db = self.app.config["DB"]
        count = db.execute("SELECT COUNT(*) c FROM donations"
                           ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_bad_signature_rejected(self):
        self.assertEqual(
            self._record(signature="not-a-signature").status_code, 400)
        self.assertEqual(self._record(signature="").status_code, 400)

    def test_bad_amount_rejected(self):
        self.assertEqual(self._record(amount_sol=0).status_code, 400)
        self.assertEqual(self._record(amount_sol=-1).status_code, 400)
        self.assertEqual(self._record(amount_sol="lots").status_code, 400)

    def test_bad_donor_rejected(self):
        self.assertEqual(
            self._record(donor_address="not-an-address").status_code, 400)

    def test_wrong_network_rejected(self):
        self.assertEqual(
            self._record(network="mainnet").status_code, 400)

    def test_missing_csrf_rejected(self):
        resp = self.client.post("/api/donate/record", json={
            "donor_address": FAKE_DONOR, "amount_sol": 0.5,
            "signature": FAKE_SIG, "network": "devnet"})
        self.assertEqual(resp.status_code, 400)


class TestAdminDonations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_donation(self):
        donations_mod.get_or_create_donation_wallet(self.app)
        donations_mod.record_donation(self.app, FAKE_DONOR, 500_000_000,
                                      FAKE_SIG, "devnet")

    def test_anonymous_redirected_to_login(self):
        self.assertEqual(self.client.get("/admin/donations").status_code,
                         302)

    def test_first_user_is_admin_by_default(self):
        self._seed_donation()
        signup(self.client, "alice")
        resp = self.client.get("/admin/donations")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Donations", html)
        self.assertIn("0.5000 SOL", html)
        self.assertIn("Email notification is not configured", html)

    def test_second_user_is_not_admin(self):
        self._seed_donation()
        signup(self.client, "alice")
        self.client.get("/logout")
        signup(self.client, "bob")
        resp = self.client.get("/admin/donations")
        self.assertEqual(resp.status_code, 403)

    def test_admin_username_env_gates_page(self):
        # Rebuild the app with ADMIN_USERNAME set to bob.
        import os
        from tradingbot import create_app
        tmp = tempfile.TemporaryDirectory()
        env = {
            "SESSION_SECRET": "test-session-secret-" + "x" * 40,
            "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(tmp.name, "test2.db"),
            "ADMIN_USERNAME": "bob",
        }
        from tradingbot.config import Config
        app2 = create_app(Config(env=env))
        app2.testing = True
        client2 = app2.test_client()
        self._seed_donation_on(app2)
        signup(client2, "alice")
        self.assertEqual(client2.get("/admin/donations").status_code, 403)
        client2.get("/logout")
        signup(client2, "bob")
        self.assertEqual(client2.get("/admin/donations").status_code, 200)
        tmp.cleanup()

    def _seed_donation_on(self, app):
        donations_mod.get_or_create_donation_wallet(app)
        donations_mod.record_donation(app, FAKE_DONOR, 500_000_000,
                                      FAKE_SIG, "devnet")


if __name__ == "__main__":
    unittest.main()
