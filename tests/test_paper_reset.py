"""Paper trading is $50 CAD: default, display, and the one-time reset.

The old default was $10,000 fake USD. New accounts start at $50 CAD
and every paper amount on screen says CAD. Existing paper accounts
are reset once, on the next app start after this lands: $50 cash and
no open positions (the old $10,000-scale holdings do not fit a $50
account). Paper trade history and P&L snapshots are kept as records;
only the live balance and open positions are reset. Real SOL is
never touched.
"""

import os
import tempfile
import unittest

from tradingbot import create_app
from tradingbot.config import Config
from tradingbot.db import init_db
from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, csrf,
                     make_client, signup)


def make_default_config(tmpdir: str) -> Config:
    """A config with no PAPER_START_CASH set — the production default."""
    env = {
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tmpdir, "test.db"),
    }
    return Config(env=env)


class TestPaperDefault(unittest.TestCase):
    def test_default_start_cash_is_50(self):
        self.assertEqual(make_default_config("/tmp").paper_start_cash, 50.0)

    def test_env_override_still_respected(self):
        env = {
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": "/tmp/test.db",
            "PAPER_START_CASH": "200",
        }
        self.assertEqual(Config(env=env).paper_start_cash, 200.0)


class TestPaperReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(make_default_config(self.tmp.name))
        self.app.testing = True
        self.client = self.app.test_client()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_signup_starts_at_50_cad(self):
        cash = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
            (self.user_id,)).fetchone()["cash_usd"]
        self.assertEqual(cash, 50.0)
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("$50.00 CAD", html)
        self.assertNotIn("$10,000", html)

    def test_reset_migration_resets_old_accounts_once(self):
        # Simulate a pre-reset database: $10,000 cash, an open
        # position and a trade, with no reset marker yet.
        self.db.execute(
            "UPDATE paper_portfolio SET cash_usd = 10000 WHERE user_id = ?",
            (self.user_id,))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 10.0, 100.0)",
            (self.user_id,))
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, amount, "
            "price, total_usd, created_at) VALUES (?, 'SOL', 'buy', 10.0, "
            "100.0, 1000.0, datetime('now'))",
            (self.user_id,))
        self.db.execute(
            "DELETE FROM meta WHERE key = 'paper_reset_50_cad'")
        self.db.commit()
        init_db(self.db_path)
        db = self.app.config["DB"]
        cash = db.execute(
            "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
            (self.user_id,)).fetchone()["cash_usd"]
        self.assertEqual(cash, 50.0)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 0)
        # Trade history is kept as a record — only the live balance
        # and open positions are reset.
        self.assertEqual(db.execute(
            "SELECT COUNT(*) c FROM paper_trades WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 1)
        # The marker is set, so a later restart never touches the
        # balance again: trade down to $40 and re-init, still $40.
        db.execute(
            "UPDATE paper_portfolio SET cash_usd = 40 WHERE user_id = ?",
            (self.user_id,))
        db.commit()
        init_db(self.db_path)
        cash = db.execute(
            "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
            (self.user_id,)).fetchone()["cash_usd"]
        self.assertEqual(cash, 40.0)


if __name__ == "__main__":
    unittest.main()
