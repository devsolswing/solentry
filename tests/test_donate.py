"""Donations: the FEE_RECIPIENT_PUBKEY recipient, masked display,
record endpoint, admin page."""

import tempfile
import unittest

from helpers import TEST_FEE_RECIPIENT, csrf, make_client, signup
from tradingbot import donations as donations_mod
from tradingbot.solana import base58_encode

# A well-formed fake 64-byte signature (base58 of 64 bytes whose first
# byte is nonzero, so it encodes to 87 chars like a real signature).
# It is NOT a real signature — only its shape is used.
FAKE_SIG = base58_encode(b"\x01" * 64)
FAKE_DONOR = "9WMzFGfMAiPUzNhTJU2jdyJQb5AM3GWroKBS7LCDVQ3H"


class TestDonationRecipient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recipient_is_the_fee_recipient(self):
        # Donations go ONLY to the same single wallet as the
        # trading fee — the throwaway test address from
        # FEE_RECIPIENT_PUBKEY. No wallet is generated.
        self.assertEqual(
            donations_mod.get_donation_recipient(
                self.app.config["TRADINGBOT_CONFIG"]),
            TEST_FEE_RECIPIENT)
        db = self.app.config["DB"]
        self.assertEqual(
            db.execute("SELECT COUNT(*) c FROM donation_wallet"
                       ).fetchone()["c"], 0)

    def test_unset_recipient_fails_closed(self):
        from tradingbot.config import Config
        cfg = Config(env={
            "SESSION_SECRET": "test-session-secret-" + "x" * 40,
            "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet",
            "DATABASE_PATH": self.tmp.name + "/other.db",
        })
        with self.assertRaises(ValueError):
            donations_mod.get_donation_recipient(cfg)

    def test_mask_address(self):
        self.assertEqual(donations_mod.mask_address(TEST_FEE_RECIPIENT),
                         TEST_FEE_RECIPIENT[:5] + "*****")
        self.assertEqual(donations_mod.mask_address(""), "")

    def test_donate_info_endpoint(self):
        resp = self.client.get("/api/donate/info")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["network"], "devnet")
        # The full address is returned ONLY here, for the donor's
        # browser to build the transfer — plus the masked value
        # the page shows.
        self.assertEqual(body["donation_address"], TEST_FEE_RECIPIENT)
        self.assertEqual(body["donation_address_masked"],
                         TEST_FEE_RECIPIENT[:5] + "*****")
        self.assertIn("api.devnet.solana.com", body["rpc_url"])

    def test_donate_info_refuses_when_unconfigured(self):
        # Rebuild the app without FEE_RECIPIENT_PUBKEY: donations
        # refuse plainly (503) instead of falling back to any
        # wallet.
        import os
        from tradingbot import create_app
        from tradingbot.config import Config
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {
            "SESSION_SECRET": "test-session-secret-" + "x" * 40,
            "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(tmp.name, "test2.db"),
        }
        app2 = create_app(Config(env=env))
        app2.testing = True
        client2 = app2.test_client()
        resp = client2.get("/api/donate/info")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not configured", resp.get_json()["error"])

    def test_login_page_shows_only_masked_recipient(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn(TEST_FEE_RECIPIENT[:5] + "*****", html)
        # The full address never reaches a template.
        self.assertNotIn(TEST_FEE_RECIPIENT, html)
        self.assertNotIn("data-donation-address", html)

    def test_donate_js_fetches_info_at_send_time(self):
        with open("tradingbot/static/donate.js",
                  encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/api/donate/info", js)
        self.assertNotIn("data-donation-address", js)


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

    def test_admin_page_masks_recipient(self):
        self._seed_donation()
        signup(self.client, "alice")
        resp = self.client.get("/admin/donations")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(TEST_FEE_RECIPIENT[:5] + "*****", html)
        # The full address is never shown, even to the admin.
        self.assertNotIn(TEST_FEE_RECIPIENT, html)

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
            # Tests only: the Security bot's codes are echoed in the
            # popup so signup can complete without SMTP.
            "SECURITY_CODE_DEV_ECHO": "1",
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
        donations_mod.record_donation(app, FAKE_DONOR, 500_000_000,
                                      FAKE_SIG, "devnet")


if __name__ == "__main__":
    unittest.main()
