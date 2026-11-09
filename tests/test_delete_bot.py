"""Delete bot actually works: the dead Delete button, permanently.

Root cause (reproduced 2026-11-09): deleting a bot with rows in
scanner_marks / scanner_top10 raised sqlite3.IntegrityError
(foreign keys enforced, no cascade) at DELETE FROM bots, so the POST
500'd and the bot stayed — from the phone the Delete button looked
like a dead link. The fix deletes every bot-owned row first (scanner
marks, top-10 watches, proposals, positions), then the bot row
itself, in both the page flow and the assistant flow. The bot's API
token hash and rules live on the bots row, so they go with it. Paper
holdings the bot managed stay put — only the management record goes.
No JavaScript is involved: the confirm page is a plain server-rendered
form, so it works in Phantom's in-app browser.
"""

import tempfile
import unittest

from helpers import csrf, make_client, signup


class DeleteBotBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _create_scanner(self):
        resp = self.client.post(
            "/bots/preset/scanner",
            data={"csrf_token": csrf(self.client, "/bots")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        return self.db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = 'scanner'",
            (self.user_id,)).fetchone()["id"]

    def _seed_bot_rows(self, bot_id):
        """Every kind of row a bot can own, including a holding."""
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, 'MINTA', 0.01, datetime('now'))",
            (bot_id,))
        self.db.execute(
            "INSERT INTO scanner_top10 (bot_id, mint, top10_total, "
            "recorded_at) VALUES (?, 'MINTA', 1000000, datetime('now'))",
            (bot_id,))
        self.db.execute(
            "INSERT INTO bot_proposals (user_id, bot_id, kind, coin_id, "
            "side, detail) VALUES (?, ?, 'trade', 'SOL', 'buy', 'test')",
            (self.user_id, bot_id))
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, 'SOL', 1.0, 100.0, 'paper')",
            (self.user_id, bot_id))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 1.0, 100.0)",
            (self.user_id,))
        self.db.commit()

    def _delete(self, bot_id):
        token = csrf(self.client, f"/bots/{bot_id}/delete/confirm")
        return self.client.post(f"/bots/{bot_id}/delete",
                                data={"csrf_token": token},
                                follow_redirects=False)

    def _count(self, table, bot_id):
        return self.db.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE bot_id = ?",
            (bot_id,)).fetchone()["c"]


class TestDeleteScannerBot(DeleteBotBase):
    def test_delete_scanner_bot_with_marks_removes_everything(self):
        # Before the fix this POST 500'd with a foreign-key error.
        bot_id = self._create_scanner()
        self._seed_bot_rows(bot_id)
        resp = self._delete(bot_id)
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self.db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())
        self.assertEqual(self._count("scanner_marks", bot_id), 0)
        self.assertEqual(self._count("scanner_top10", bot_id), 0)
        self.assertEqual(self._count("bot_proposals", bot_id), 0)
        self.assertEqual(self._count("bot_positions", bot_id), 0)
        # The coins stay in the paper holdings — only the bot's
        # management record is gone.
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE coin_id = 'SOL'"
        ).fetchone()
        self.assertIsNotNone(holding)
        self.assertEqual(holding["amount"], 1.0)

    def test_delete_confirm_page_is_plain_form_no_js(self):
        bot_id = self._create_scanner()
        resp = self.client.get(f"/bots/{bot_id}/delete/confirm")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("confirm(", html)
        self.assertIn(f"/bots/{bot_id}/delete", html)
        self.assertIn('name="csrf_token"', html)

    def test_delete_silent_bot_is_gone_for_good(self):
        bot_id = self._create_scanner()
        self.db.execute("UPDATE bots SET silent = 1 WHERE id = ?",
                        (bot_id,))
        self.db.commit()
        self._seed_bot_rows(bot_id)
        resp = self._delete(bot_id)
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self.db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())
        # The silent loop only trades bots present in the database,
        # so a deleted bot can never trade again.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE silent = 1"
        ).fetchone()["c"], 0)

    def test_delete_says_so_on_screen(self):
        bot_id = self._create_scanner()
        resp = self.client.post(
            f"/bots/{bot_id}/delete",
            data={"csrf_token": csrf(
                self.client, f"/bots/{bot_id}/delete/confirm")},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Scanner", resp.get_data(as_text=True))
        self.assertIn("deleted permanently", resp.get_data(as_text=True))

    def test_other_users_bot_cannot_be_deleted(self):
        bot_id = self._create_scanner()
        self._seed_bot_rows(bot_id)
        bob = self.app.test_client()
        signup(bob, "bob")
        token = csrf(bob, "/bots")
        resp = bob.post(f"/bots/{bot_id}/delete",
                        data={"csrf_token": token},
                        follow_redirects=False)
        self.assertEqual(resp.status_code, 404)
        self.assertIsNotNone(self.db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())
        self.assertEqual(self._count("scanner_marks", bot_id), 1)


class TestAssistantDeleteScannerBot(DeleteBotBase):
    def _ask(self, message):
        token = csrf(self.client, "/bots")
        resp = self.client.post("/api/assistant", json={
            "message": message, "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_assistant_delete_removes_scanner_rows(self):
        bot_id = self._create_scanner()
        self._seed_bot_rows(bot_id)
        data = self._ask("delete bot Scanner")
        self.assertIn("Are you sure", data["reply"])
        data = self._ask("yes")
        self.assertIn("deleted", data["reply"])
        self.assertIsNone(self.db.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone())
        self.assertEqual(self._count("scanner_marks", bot_id), 0)
        self.assertEqual(self._count("scanner_top10", bot_id), 0)
        self.assertEqual(self._count("bot_positions", bot_id), 0)


if __name__ == "__main__":
    unittest.main()
