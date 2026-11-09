"""Bots: create (token shown once, hashed at rest), edit, delete."""

import hashlib
import tempfile
import unittest

from helpers import csrf, make_client, signup


class TestBots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, name="Luna", strategy="bold"):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": strategy, "coins": "SOL, BTC",
            "buy_size_pct": "25", "stop_loss_pct": "5"})
        return resp

    def test_create_stores_hash_not_token(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        db = self.app.config["DB"]
        bot = db.execute("SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["strategy"], "bold")
        self.assertEqual(bot["buy_size_pct"], 25)
        self.assertEqual(bot["stop_loss_pct"], 5)
        # Token shown once on the page; only its hash is stored.
        self.assertNotIn(bot["api_token_hash"], html)
        import re
        m = re.search(r'<p class="addr">([^<]+)</p>', html)
        self.assertIsNotNone(m, "token not shown on creation page")
        token = m.group(1).strip()
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(),
                         bot["api_token_hash"])
        self.assertNotEqual(token, bot["api_token_hash"])

    def test_create_defaults_and_clamps(self):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Bad", "strategy": "not-a-strategy", "coins": "NOPE",
            "buy_size_pct": "500", "stop_loss_pct": "99"})
        self.assertEqual(resp.status_code, 200)
        db = self.app.config["DB"]
        bot = db.execute("SELECT * FROM bots WHERE name = 'Bad'").fetchone()
        self.assertEqual(bot["strategy"], "balanced")
        self.assertEqual(bot["coins"], '["SOL"]')
        self.assertEqual(bot["buy_size_pct"], 100)
        self.assertEqual(bot["stop_loss_pct"], 50)

    def test_edit_updates_fields(self):
        self._create()
        db = self.app.config["DB"]
        bot_id = db.execute("SELECT id FROM bots WHERE name='Luna'"
                            ).fetchone()["id"]
        resp = self.client.post(f"/bots/{bot_id}/edit", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/edit"),
            "name": "Luna2", "strategy": "careful", "coins": "ETH",
            "buy_size_pct": "10", "stop_loss_pct": "3"})
        self.assertEqual(resp.status_code, 302)
        bot = db.execute("SELECT * FROM bots WHERE id = ?",
                         (bot_id,)).fetchone()
        self.assertEqual(bot["name"], "Luna2")
        self.assertEqual(bot["strategy"], "careful")
        self.assertEqual(bot["coins"], '["ETH"]')

    def test_delete_removes_bot(self):
        self._create()
        db = self.app.config["DB"]
        bot_id = db.execute("SELECT id FROM bots WHERE name='Luna'"
                            ).fetchone()["id"]
        resp = self.client.post(f"/bots/{bot_id}/delete", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/edit")})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.execute("SELECT id FROM bots WHERE id = ?",
                                     (bot_id,)).fetchone())

    def test_suggest_creates_pending_proposals_only(self):
        self._create()
        db = self.app.config["DB"]
        bot_id = db.execute("SELECT id FROM bots WHERE name='Luna'"
                            ).fetchone()["id"]
        resp = self.client.post(f"/bots/{bot_id}/suggest", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}")})
        self.assertEqual(resp.status_code, 302)
        # Suggestions exist only if the 24h signal fired; either way,
        # nothing was sent on-chain (no send intents).
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
