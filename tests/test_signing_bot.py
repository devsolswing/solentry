"""Per-account signing bot + immutable 8-digit support ID.

Covers: format (exactly 8 digits, first digit never 0), uniqueness,
creation at signup, idempotent backfill for existing users, display
on the dashboard and in settings, and immutability — there is no
route, form, or code path anywhere in this app that can change a
support ID.
"""

import os
import re
import tempfile
import unittest

from helpers import csrf, login, make_client, signup
from tradingbot import signing_bot
from tradingbot.db import _backfill_signing_bots, init_db

PACKAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "tradingbot")


class TestSupportIdGeneration(unittest.TestCase):
    def test_format_exactly_8_digits_first_never_zero(self):
        for _ in range(500):
            value = signing_bot.generate_support_id()
            self.assertEqual(len(value), 8)
            self.assertTrue(value.isdigit())
            self.assertNotEqual(value[0], "0")
            self.assertTrue(signing_bot.is_valid_support_id(value))

    def test_unique_across_many(self):
        seen = {signing_bot.generate_support_id() for _ in range(2000)}
        self.assertEqual(len(seen), 2000)

    def test_invalid_values_rejected(self):
        for bad in ("", "1234567", "123456789", "01234567", "1234567a",
                    "abcdefgh", 12345678, None, " 12345678"):
            self.assertFalse(signing_bot.is_valid_support_id(bad),
                             repr(bad))


class SigningBotBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()


class TestSigningBotCreation(SigningBotBase):
    def test_created_at_signup(self):
        bot = signing_bot.get_signing_bot(self.db, self.user_id)
        self.assertIsNotNone(bot)
        self.assertTrue(signing_bot.is_valid_support_id(
            bot["support_id"]))
        self.assertEqual(bot["user_id"], self.user_id)

    def test_ensure_is_idempotent(self):
        first = signing_bot.ensure_signing_bot(self.db, self.user_id)
        second = signing_bot.ensure_signing_bot(self.db, self.user_id)
        self.assertEqual(first["support_id"], second["support_id"])
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM signing_bots WHERE user_id = ?",
            (self.user_id,)).fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_two_accounts_get_different_ids(self):
        signup(self.client, "bob")
        bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]
        alice = signing_bot.get_signing_bot(self.db, self.user_id)
        bob = signing_bot.get_signing_bot(self.db, bob_id)
        self.assertNotEqual(alice["support_id"], bob["support_id"])

    def test_backfill_for_existing_user(self):
        # A user created before signing bots existed has no row until
        # the backfill runs; afterwards it has exactly one, and a
        # second backfill changes nothing.
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt, "
            "created_at) VALUES ('carol', 'x', 'y', datetime('now'))")
        self.db.commit()
        carol_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'carol'"
        ).fetchone()["id"]
        self.assertIsNone(signing_bot.get_signing_bot(self.db, carol_id))
        _backfill_signing_bots(self.db)
        first = signing_bot.get_signing_bot(self.db, carol_id)
        self.assertIsNotNone(first)
        self.assertTrue(signing_bot.is_valid_support_id(
            first["support_id"]))
        _backfill_signing_bots(self.db)
        second = signing_bot.get_signing_bot(self.db, carol_id)
        self.assertEqual(first["support_id"], second["support_id"])

    def test_init_db_backfills(self):
        path = os.path.join(self.tmp.name, "backfill.db")
        db = init_db(path)
        db.execute(
            "INSERT INTO users (username, password_hash, salt, "
            "created_at) VALUES ('dave', 'x', 'y', datetime('now'))")
        db.commit()
        dave_id = db.execute(
            "SELECT id FROM users WHERE username = 'dave'"
        ).fetchone()["id"]
        # Re-opening the database (as an app start does) backfills.
        db.close()
        db = init_db(path)
        bot = signing_bot.get_signing_bot(db, dave_id)
        self.assertIsNotNone(bot)
        self.assertTrue(signing_bot.is_valid_support_id(
            bot["support_id"]))
        db.close()


class TestSupportIdDisplay(SigningBotBase):
    def test_shown_on_dashboard_and_settings(self):
        bot = signing_bot.get_signing_bot(self.db, self.user_id)
        support_id = bot["support_id"]
        dash = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Support ID", dash)
        self.assertIn(support_id, dash)
        settings = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Support ID", settings)
        self.assertIn(support_id, settings)
        self.assertIn("can never be changed", settings)


class TestSupportIdImmutable(unittest.TestCase):
    def test_no_update_or_delete_path_in_code(self):
        """The only write to signing_bots anywhere in the app is the
        INSERT in signing_bot.py. No UPDATE, no DELETE, no
        support_id assignment from a request exists."""
        writes = []
        for root, _dirs, files in os.walk(PACKAGE_DIR):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                for needle in ("UPDATE signing_bots",
                               "DELETE FROM signing_bots",
                               "INSERT INTO signing_bots"):
                    if needle in source:
                        writes.append((name, needle))
        self.assertEqual(writes,
                         [("signing_bot.py", "INSERT INTO signing_bots")])

    def test_attempts_to_change_it_are_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            app, client = make_client(tmp.name)
            signup(client, "alice")
            db = app.config["DB"]
            user_id = db.execute(
                "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
            original = signing_bot.get_signing_bot(
                db, user_id)["support_id"]
            # Try every form that accepts input: settings, bot create,
            # bot edit — each with a support_id field smuggled in.
            login(client, "alice")
            token = csrf(client, "/settings")
            client.post("/settings", data={
                "csrf_token": token, "support_id": "99999999"})
            client.post("/bots/create", data={
                "csrf_token": csrf(client, "/bots/create"),
                "name": "Luna", "strategy": "bold", "coins": "SOL",
                "buy_size_pct": "20", "stop_loss_pct": "7",
                "support_id": "99999999"})
            after = signing_bot.get_signing_bot(db, user_id)
            self.assertEqual(after["support_id"], original)
            self.assertNotEqual(after["support_id"], "99999999")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
