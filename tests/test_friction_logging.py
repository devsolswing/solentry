"""Friction logging for real trades: every real fill records the
full on-chain signature and the executed SOL (spent on buys,
received on sells) in trade_ledger, so round-trip friction can be
measured from the ledger alone.

Covers: the pump.fun executor returns sol_spent / sol_received on
confirmed trades; record_trade stores a full signature + sol_amount;
paper trades store NULL for both; the migration adds the columns to
an existing database without touching its rows; and the silent loop
passes the executor's signature + SOL through to the ledger on both
sides (a stub executor is used for the loop tests, so no RPC is
touched — and per the test-mock lesson, any test that fakes RPC for
the silent loop must patch silent_loop.SolanaRpcClient as well as
executor.SolanaRpcClient, or the loop silently hits live devnet).
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import grant_fuel, make_client, signup
from test_executor import (CURVE_PRICE_USD, FAKE_SIGNATURE, ExecutorBase,
                           FakePumpRpc, TEST_SOL_PRICE_USD, make_curve)
from tradingbot import db as db_mod
from tradingbot import pumpfun
from tradingbot.executor import execute_real_trade
from tradingbot.ledger import record_trade
from tradingbot.silent_loop import _buy_coin, _sell_position

GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
FULL_SIG = "5" * 88  # a real Solana signature is 88 base58 chars


class SpendingRpc(FakePumpRpc):
    """FakePumpRpc whose wallet balance drops by the trade amount
    plus the 5,000-lamport network fee when a transaction is sent —
    the chain's-eye view the executor's balance-delta math reads."""

    def send_transaction(self, signed_b64):
        sent = super().send_transaction(signed_b64)
        self.balance -= 1_000_000_000 + 5_000
        return sent


class ProceedsRpc(FakePumpRpc):
    """FakePumpRpc whose wallet balance grows by the sale proceeds
    (0.12 SOL) when a transaction is sent."""

    def send_transaction(self, signed_b64):
        sent = super().send_transaction(signed_b64)
        self.balance += 120_000_000
        return sent


class TestExecutorSolAmounts(ExecutorBase):
    def test_buy_returns_sol_spent(self):
        rpc = SpendingRpc(self.pubkey, self.mint, curve=make_curve())
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95, 1_000_000_000)
        result = execute_real_trade(
            self._bot(), self.user_id, self.mint, "buy", 1.0,
            CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
 rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        # 1.0 SOL swapped + 5,000 lamports network fee, from the
        # wallet's own balance delta — the executed spend, not the
        # amount asked for.
        self.assertAlmostEqual(result["sol_spent"], 1.000005, places=6)

    def test_sell_returns_sol_received(self):
        rpc = ProceedsRpc(self.pubkey, self.mint, curve=make_curve(),
                          ata_balance=2_000_000)
        result = execute_real_trade(self._bot(), self.user_id, self.mint,
                                    "sell", 1.0, 100.0, self.db, self.cfg,
                                    rpc_client=rpc)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["signature"], FAKE_SIGNATURE)
        # The SOL the wallet actually gained, from the balance delta.
        self.assertAlmostEqual(result["sol_received"], 0.12, places=6)


class TestLedgerFrictionColumns(unittest.TestCase):
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

    def test_fresh_db_has_the_columns(self):
        cols = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(trade_ledger)").fetchall()}
        self.assertIn("signature", cols)
        self.assertIn("sol_amount", cols)

    def test_record_trade_stores_full_signature_and_sol_amount(self):
        record_trade(self.db, self.user_id, None, GATE, "buy", 1000.0,
                     0.0104, 10.40, mode="real", signature=FULL_SIG,
                     sol_amount=0.1277)
        row = self.db.execute(
            "SELECT signature, sol_amount FROM trade_ledger "
            "WHERE coin = ?", (GATE,)).fetchone()
        # The FULL signature — never the 12-char prefix the activity
        # log shows. The signature alone re-derives the leg on-chain.
        self.assertEqual(row["signature"], FULL_SIG)
        self.assertEqual(len(row["signature"]), 88)
        self.assertAlmostEqual(row["sol_amount"], 0.1277)

    def test_paper_trade_stores_nulls(self):
        record_trade(self.db, self.user_id, None, GATE, "buy", 1000.0,
                     0.02, 20.0, mode="paper")
        row = self.db.execute(
            "SELECT signature, sol_amount FROM trade_ledger "
            "WHERE coin = ?", (GATE,)).fetchone()
        self.assertIsNone(row["signature"])
        self.assertIsNone(row["sol_amount"])

    def test_migration_adds_columns_without_touching_rows(self):
        """An existing database built from the pre-change schema
        gets the two columns from _migrate, and its rows survive
        untouched with NULLs in the new columns."""
        old_schema = """
        CREATE TABLE trade_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            bot_id INTEGER,
            coin TEXT NOT NULL,
            side TEXT NOT NULL,
            amount REAL NOT NULL,
            price REAL NOT NULL,
            total_usd REAL NOT NULL,
            pnl_usd REAL,
            win INTEGER,
            exit_reason TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'paper',
            created_at TEXT NOT NULL
        )
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # _migrate walks every table in _MIGRATIONS: give each one
        # an empty shell so only trade_ledger carries real data.
        tables = {t for t, _c, _d in db_mod._MIGRATIONS}
        for table in tables:
            if table == "trade_ledger":
                conn.execute(old_schema)
            else:
                conn.execute(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.execute(
            "INSERT INTO trade_ledger (trade_id, user_id, bot_id, coin, "
            "side, amount, price, total_usd, mode, created_at) "
            "VALUES ('12345678', 1, 1, ?, 'buy', 1000.0, 0.02, 20.0, "
            "'real', '2026-11-13T16:54:39Z')", (GATE,))
        conn.commit()
        db_mod._migrate(conn)
        conn.commit()
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(trade_ledger)").fetchall()}
        self.assertIn("signature", cols)
        self.assertIn("sol_amount", cols)
        row = conn.execute(
            "SELECT * FROM trade_ledger WHERE trade_id = '12345678'"
        ).fetchone()
        self.assertEqual(row["coin"], GATE)
        self.assertAlmostEqual(row["amount"], 1000.0)
        self.assertAlmostEqual(row["total_usd"], 20.0)
        self.assertIsNone(row["signature"])
        self.assertIsNone(row["sol_amount"])
        # Migrating twice is a no-op, never an error.
        db_mod._migrate(conn)
        conn.close()


class FakeRpc:
    """Balance-only RPC for the loop tests: the stub executor does
    the trading, this only answers the loop's balance read."""

    def __init__(self, url, balance_lamports=1_000_000_000):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        raise AssertionError("no sends in these tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("no sends in these tests")


def _live_prices(sol=80.0):
    return {"prices": {"SOL": {"price": sol, "change_24h": 0.0}},
            "source": "live", "as_of": "test"}


class TestLoopRecordsFrictionFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.feed = self.app.config["LIVE_FEED"]
        # Package fuel (2026-11-23): real trades cost 1 trade
        # credit — the friction tests drive real execution.
        grant_fuel(self.db, self.user_id, 100)

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_bot(self, mode="real", buy_size=20, stop_loss=7,
                    max_open=3):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, "Trenches", "balanced", json.dumps([GATE]),
             buy_size, stop_loss, mode, 1, max_open,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (cur.lastrowid,)).fetchone()

    def test_real_buy_and_sell_rows_carry_signature_and_sol(self):
        bot = self._insert_bot(mode="real")
        self.feed.ingest("SOL", 80.0, "kraken")
        prices_data = _live_prices(80.0)

        def fake_buy(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            return {"status": "executed", "signature": "B" * 88,
                    "coin_amount": 1000.0, "sol_spent": 0.13,
                    "signing_bot": "test-bot"}

        _buy_coin(self.db, self.cfg, bot, GATE, 0.02, {},
                  lambda url: FakeRpc(url), fake_buy, [],
                  feed=self.feed, prices_data=prices_data)
        buy_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side = 'buy'",
            (bot["id"],)).fetchone()
        self.assertIsNotNone(buy_row)
        self.assertEqual(buy_row["signature"], "B" * 88)
        self.assertEqual(len(buy_row["signature"]), 88)
        self.assertAlmostEqual(buy_row["sol_amount"], 0.13)

        def fake_sell(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            return {"status": "executed", "signature": "S" * 88,
                    "sol_received": 0.12, "signing_bot": "test-bot"}

        actions = []
        _sell_position(self.db, self.cfg, bot, GATE, 0.011, "stop_loss",
                       lambda url: FakeRpc(url), fake_sell, actions,
                       feed=self.feed, prices_data=prices_data)
        self.assertEqual(actions[-1]["action"], "sell")
        sell_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side='sell'",
            (bot["id"],)).fetchone()
        self.assertIsNotNone(sell_row)
        self.assertEqual(sell_row["signature"], "S" * 88)
        self.assertEqual(len(sell_row["signature"]), 88)
        self.assertAlmostEqual(sell_row["sol_amount"], 0.12)

    def test_executor_without_sol_amounts_still_records_signature(self):
        """An executor that reports a signature but no SOL amounts
        (e.g. a balance read failed) still gets its signature
        stored; sol_amount stays NULL rather than guessed."""
        bot = self._insert_bot(mode="real")
        self.feed.ingest("SOL", 80.0, "kraken")
        prices_data = _live_prices(80.0)

        def fake_buy(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            return {"status": "executed", "signature": "B" * 88,
                    "coin_amount": 1000.0, "signing_bot": "test-bot"}

        _buy_coin(self.db, self.cfg, bot, GATE, 0.02, {},
                  lambda url: FakeRpc(url), fake_buy, [],
                  feed=self.feed, prices_data=prices_data)
        buy_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side = 'buy'",
            (bot["id"],)).fetchone()
        self.assertEqual(buy_row["signature"], "B" * 88)
        self.assertIsNone(buy_row["sol_amount"])

    def test_paper_rows_carry_no_signature_or_sol(self):
        bot = self._insert_bot(mode="paper")
        self.feed.ingest("SOL", 80.0, "kraken")
        prices_data = _live_prices(80.0)
        actions = []
        _buy_coin(self.db, self.cfg, bot, GATE, 0.02, {"SOL": 80.0},
                  lambda url: FakeRpc(url),
                  mock.Mock(side_effect=AssertionError("no real trades")),
                  actions, feed=self.feed, prices_data=prices_data)
        self.assertEqual(actions[-1]["action"], "buy")
        buy_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side = 'buy'",
            (bot["id"],)).fetchone()
        self.assertIsNotNone(buy_row)
        self.assertIsNone(buy_row["signature"])
        self.assertIsNone(buy_row["sol_amount"])


if __name__ == "__main__":
    unittest.main()
