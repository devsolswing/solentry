"""Per-user isolation: A can never read B's wallet, bots, or activity."""

import tempfile
import unittest

from helpers import csrf, login, make_client, signup


class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.client.get("/logout")
        signup(self.client, "bob")
        self.client.get("/logout")
        login(self.client, "alice")
        # Alice creates a bot.
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "AliceBot", "strategy": "balanced", "coins": "SOL",
            "buy_size_pct": "20", "stop_loss_pct": "7"})
        self.assertEqual(resp.status_code, 200)  # bot_created page
        db = self.app.config["DB"]
        self.alice_bot = db.execute(
            "SELECT id FROM bots WHERE name = 'AliceBot'").fetchone()["id"]
        self.client.get("/logout")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bob_cannot_open_alice_bot(self):
        login(self.client, "bob")
        resp = self.client.get(f"/bots/{self.alice_bot}")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get(f"/bots/{self.alice_bot}/edit")
        self.assertEqual(resp.status_code, 404)

    def test_bob_cannot_delete_alice_bot(self):
        login(self.client, "bob")
        resp = self.client.post(f"/bots/{self.alice_bot}/delete", data={
            "csrf_token": csrf(self.client, "/dashboard")})
        self.assertEqual(resp.status_code, 404)
        db = self.app.config["DB"]
        self.assertIsNotNone(db.execute(
            "SELECT id FROM bots WHERE id = ?", (self.alice_bot,)).fetchone())

    def test_bob_dashboard_shows_only_bob_data(self):
        login(self.client, "bob")
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn("AliceBot", html)
        db = self.app.config["DB"]
        alice_wallet = db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = (SELECT id FROM "
            "users WHERE username = 'alice')").fetchone()["pubkey"]
        self.assertNotIn(alice_wallet, html)

    def test_bob_cannot_confirm_alice_send_intent(self):
        db = self.app.config["DB"]
        alice_id = db.execute(
            "SELECT id FROM users WHERE username = 'alice'").fetchone()["id"]
        cur = db.execute(
            "INSERT INTO send_intents (user_id, destination, "
            "amount_lamports, token, status, created_at) "
            "VALUES (?, ?, ?, 'SOL', 'pending', '2026-01-01T00:00:00Z')",
            (alice_id, "11111111111111111111111111111111", 1000))
        db.commit()
        intent_id = cur.lastrowid
        login(self.client, "bob")
        resp = self.client.get(
            f"/wallet/send/{intent_id}/confirm")
        self.assertEqual(resp.status_code, 404)

    def test_bob_activity_log_has_no_alice_entries(self):
        login(self.client, "bob")
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn("AliceBot", html)


if __name__ == "__main__":
    unittest.main()
