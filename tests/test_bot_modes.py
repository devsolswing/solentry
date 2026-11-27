"""Same preset bot in paper AND real at the same time (James, 2026-11-26).

- One preset can be created once per mode: the second-mode tap
  creates a second bot instead of erroring with "already created".
  A second tap in the SAME mode still goes to the existing bot.
- The stored name carries an invisible mode suffix — paper
  "demobot", real "realsol" ("Middydemobot" / "Middyrealsol") —
  which is NEVER shown in the app: the Bots page, the bot detail
  page and the assistant all show the plain name ("Middy").
- Existing preset bots get the suffix from a one-time idempotent
  backfill; custom-named bots are untouched.
- The assistant looks a bot up in the user's current mode, so
  "turn on silent for Middy" hits the copy the user is looking at.
No SOL moves in these tests.
"""

import unittest

from helpers import csrf
from test_ui_rebuild import UiBase
from tradingbot.bot_names import (
    backfill_bot_mode_suffixes,
    display_bot_name,
    preset_base_name,
    suffixed_name,
)

PRESETS = [
    ("trenches", "Trenches", "Trenchesdemobot", "Trenchesrealsol"),
    ("scanner", "Scanner", "Scannerdemobot", "Scannerrealsol"),
    ("clyde", "Clyde", "Clydedemobot", "Clyderealsol"),
    ("gnat", "Gnat", "Gnatdemobot", "Gnatrealsol"),
    ("pit", "Pit Sweeper", "Pit Sweeper v1.0demobot",
     "Pit Sweeper v1.0realsol"),
    ("middy", "Middy", "Middydemobot", "Middyrealsol"),
]


class TestBotNames(unittest.TestCase):
    def test_display_strips_one_suffix_only(self):
        self.assertEqual(display_bot_name("Middydemobot"), "Middy")
        self.assertEqual(display_bot_name("Middyrealsol"), "Middy")
        self.assertEqual(display_bot_name("Pit Sweeper v1.0demobot"),
                         "Pit Sweeper v1.0")
        self.assertEqual(display_bot_name("Middy"), "Middy")
        self.assertEqual(display_bot_name("My Bot"), "My Bot")
        # A bare suffix is never stripped to empty.
        self.assertEqual(display_bot_name("demobot"), "demobot")
        self.assertEqual(display_bot_name("realsol"), "realsol")
        self.assertEqual(display_bot_name(""), "")
        self.assertEqual(display_bot_name(None), "")

    def test_suffixed_name(self):
        self.assertEqual(suffixed_name("Middy", "paper"), "Middydemobot")
        self.assertEqual(suffixed_name("Middy", "real"), "Middyrealsol")
        self.assertEqual(suffixed_name("Middy", "bogus"), "Middy")

    def test_preset_base_name(self):
        self.assertEqual(preset_base_name("Middydemobot"), "Middy")
        self.assertEqual(preset_base_name("Middy"), "Middy")
        self.assertEqual(preset_base_name("Pit Sweeper v1.0demobot"),
                         "Pit Sweeper v1.0")
        self.assertEqual(preset_base_name("Pit Sweeper v1.0"),
                         "Pit Sweeper v1.0")
        self.assertIsNone(preset_base_name("My Bot"))
        self.assertIsNone(preset_base_name("Luna"))


class TestBotModes(UiBase):
    def _create_preset(self, slug):
        resp = self.client.post(
            f"/bots/preset/{slug}",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302, slug)
        return resp

    def _bot_row(self, name):
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = ?",
            (self.user_id, name)).fetchone()

    def test_each_preset_creatable_once_per_mode(self):
        for slug, _plain, paper_name, real_name in PRESETS:
            self._choose_mode("paper")
            self._create_preset(slug)
            paper = self._bot_row(paper_name)
            self.assertIsNotNone(paper, f"{slug} paper not created")
            self.assertEqual(paper["mode"], "paper")
            self.assertEqual(paper["silent"], 0)
            self._choose_mode("devnet")
            resp = self._create_preset(slug)
            real = self._bot_row(real_name)
            self.assertIsNotNone(real, f"{slug} real not created")
            self.assertEqual(real["mode"], "real")
            self.assertEqual(real["silent"], 0)
            self.assertNotEqual(real["id"], paper["id"])
            # Lands on the NEW (real) bot, not on the paper one.
            self.assertIn(f"/bots/{real['id']}",
                          resp.headers.get("Location", ""))

    def test_second_tap_same_mode_goes_to_existing(self):
        self._choose_mode("paper")
        self._create_preset("middy")
        paper = self._bot_row("Middydemobot")
        resp = self._create_preset("middy")
        self.assertIn(f"/bots/{paper['id']}",
                      resp.headers.get("Location", ""))
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) c FROM bots WHERE user_id = ? "
                "AND name = 'Middydemobot'",
                (self.user_id,)).fetchone()["c"], 1)
        self._choose_mode("devnet")
        self._create_preset("middy")
        real = self._bot_row("Middyrealsol")
        resp = self._create_preset("middy")
        self.assertIn(f"/bots/{real['id']}",
                      resp.headers.get("Location", ""))
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) c FROM bots WHERE user_id = ? "
                "AND name LIKE 'Middy%'",
                (self.user_id,)).fetchone()["c"], 2)

    def test_bots_page_and_detail_hide_suffix(self):
        self._choose_mode("paper")
        self._create_preset("middy")
        paper = self._bot_row("Middydemobot")
        html = self._html("/bots")
        self.assertIn("Middy", html)
        self.assertNotIn("demobot", html)
        self.assertNotIn("realsol", html)
        detail = self._html(f"/bots/{paper['id']}")
        self.assertIn("Middy", detail)
        self.assertNotIn("demobot", detail)
        self.assertNotIn("Middydemobot", detail)
        # The detail page still shows which mode this copy is.
        self.assertIn("paper", detail)

    def test_assistant_silent_hits_active_mode_copy(self):
        self._choose_mode("paper")
        self._create_preset("middy")
        self._choose_mode("devnet")
        self._create_preset("middy")

        def ask(message):
            token = csrf(self.client, "/wallet")
            resp = self.client.post("/api/assistant", json={
                "message": message, "csrf_token": token})
            self.assertEqual(resp.status_code, 200, message)
            return resp.get_json()

        # In paper mode, "turn on silent for Middy" hits the paper copy.
        self._choose_mode("paper")
        data = ask("turn on silent trading for Middy")
        self.assertIn("Middy", data["reply"])
        self.assertNotIn("demobot", data["reply"])
        data = ask("yes")
        self.assertIn("Middy", data["reply"])
        paper = self._bot_row("Middydemobot")
        real = self._bot_row("Middyrealsol")
        self.assertEqual(paper["silent"], 1)
        self.assertEqual(real["silent"], 0)
        # In real mode, the same words hit the real copy.
        self._choose_mode("devnet")
        data = ask("turn on silent trading for Middy")
        data = ask("yes")
        paper = self._bot_row("Middydemobot")
        real = self._bot_row("Middyrealsol")
        self.assertEqual(paper["silent"], 1)
        self.assertEqual(real["silent"], 1)

    def test_assistant_silent_status_hides_suffix(self):
        self._choose_mode("paper")
        self._create_preset("middy")
        token = csrf(self.client, "/wallet")
        resp = self.client.post("/api/assistant", json={
            "message": "silent status", "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        reply = resp.get_json()["reply"]
        self.assertIn("Middy", reply)
        self.assertNotIn("demobot", reply)

    def test_builderbot_scanner_one_per_mode(self):
        # Same shape as test_builderbot.TestConversation.test_scanner_flow:
        # the 5%-in-3-minutes buy signal makes a scanner bot.
        from tradingbot import builderbot as bb

        answers = ["Sniper", "Paper", "$10,000", "5% in 3 minutes",
                   "20%", "3", "7%", "Default ladder", "7%", "Yes, both",
                   "30 seconds"]
        enc_key = self.app.config["TRADINGBOT_CONFIG"].encryption_key

        def run_chat(chat_answers):
            bb.start_conversation(self.db, self.user_id, enc_key)
            for answer in chat_answers:
                bb.answer(self.db, self.user_id, enc_key, answer)
            return bb.answer(self.db, self.user_id, enc_key, "yes")

        result = run_chat(answers)
        self.assertIn("created", result)
        row = self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? "
            "AND name = 'Sniperdemobot'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "paper scanner not created")
        self.assertEqual(row["mode"], "paper")
        # Same answers in real mode create a second scanner.
        real_answers = list(answers)
        real_answers[1] = "Real (devnet)"
        result = run_chat(real_answers)
        self.assertIn("created", result)
        row2 = self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? "
            "AND name = 'Sniperrealsol'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row2, "real scanner not created")
        self.assertEqual(row2["mode"], "real")
        self.assertNotEqual(row2["id"], row["id"])
        # A third scanner in the same mode is still refused.
        bb.start_conversation(self.db, self.user_id, enc_key)
        for answer in real_answers:
            bb.answer(self.db, self.user_id, enc_key, answer)
        result = bb.answer(self.db, self.user_id, enc_key, "yes")
        self.assertIn("already have a Scanner bot",
                      result["messages"][-1]["text"])

    def test_backfill_suffixes_existing_preset_bots_once(self):
        def insert(name, mode):
            cur = self.db.execute(
                "INSERT INTO bots (user_id, name, strategy, coins, "
                "buy_size_pct, stop_loss_pct, mode, silent, "
                "max_open_trades, api_token_hash, category, rules_json) "
                "VALUES (?, ?, 'bold', '[]', 20, 7, ?, 0, 3, 'x', '', '')",
                (self.user_id, name, mode))
            self.db.commit()
            return cur.lastrowid

        middy = insert("Middy", "paper")
        clyde = insert("Clyde", "real")
        pit = insert("Pit Sweeper v1.0", "paper")
        custom = insert("My Bot", "paper")
        already = insert("Gnatdemobot", "paper")
        renamed = backfill_bot_mode_suffixes(self.db)
        self.assertEqual(renamed, 3)
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (middy,)).fetchone()["name"],
            "Middydemobot")
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (clyde,)).fetchone()["name"],
            "Clyderealsol")
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (pit,)).fetchone()["name"],
            "Pit Sweeper v1.0demobot")
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (custom,)).fetchone()["name"], "My Bot")
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (already,)).fetchone()["name"], "Gnatdemobot")
        # Second run is a no-op (idempotent, never double-suffixes).
        self.assertEqual(backfill_bot_mode_suffixes(self.db), 0)

    def test_bot_edit_keeps_suffix(self):
        self._choose_mode("paper")
        self._create_preset("middy")
        paper = self._bot_row("Middydemobot")
        edit_url = f"/bots/{paper['id']}/edit"
        html = self._html(edit_url)
        # The form shows the plain name, never the suffix.
        self.assertIn('value="Middy"', html)
        self.assertNotIn("Middydemobot", html)
        # Submitting the plain name keeps the stored suffix.
        token = csrf(self.client, edit_url)
        resp = self.client.post(edit_url, data={
            "csrf_token": token, "name": "Middy", "mode": "paper",
            "coins": "", "buy_size_pct": "20", "stop_loss_pct": "5.5",
            "max_open_trades": "2"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (paper["id"],)).fetchone()["name"],
            "Middydemobot")
        # Renaming to a custom name stores it exactly as typed.
        token = csrf(self.client, edit_url)
        self.client.post(edit_url, data={
            "csrf_token": token, "name": "My Middy", "mode": "paper",
            "coins": "", "buy_size_pct": "20", "stop_loss_pct": "5.5",
            "max_open_trades": "2"})
        self.assertEqual(
            self.db.execute("SELECT name FROM bots WHERE id = ?",
                            (paper["id"],)).fetchone()["name"], "My Middy")
        # Switching a preset bot to real re-suffixes it for real.
        token = csrf(self.client, edit_url)
        self.client.post(edit_url, data={
            "csrf_token": token, "name": "Middy", "mode": "real",
            "coins": "", "buy_size_pct": "20", "stop_loss_pct": "5.5",
            "max_open_trades": "2"})
        row = self.db.execute(
            "SELECT name, mode FROM bots WHERE id = ?",
            (paper["id"],)).fetchone()
        self.assertEqual(row["name"], "Middyrealsol")
        self.assertEqual(row["mode"], "real")

    def test_bot_create_typed_name_not_suffixed(self):
        # A name typed into /bots/create is stored exactly as typed
        # (the mode column already tells those apart).
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Middy", "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5"})
        self.assertEqual(resp.status_code, 200)
        row = self._bot_row("Middy")
        self.assertIsNotNone(row, "typed-name bot was not created")
        self.assertEqual(row["mode"], "paper")
        html = self._html("/bots")
        self.assertNotIn("Middydemobot", html)


if __name__ == "__main__":
    unittest.main()
