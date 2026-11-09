"""Silent trading: defaults off, confirm-gated enable, loop limits,
stop loss, balance checks, no fake fills, assistant confirm flow."""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.assistant import parse_message
from tradingbot.silent_loop import run_once

DEST = "11111111111111111111111111111111"


class FakeRpc:
    def __init__(self, url, balance_lamports=0):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_recent_blockhash(self):
        raise AssertionError("silent loop must not send in tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("silent loop must not send in tests")


def make_prices(changes):
    """changes: {coin: (price, change_24h)}"""
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


class SilentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _insert_bot(self, name="Luna", mode="paper", silent=1, coins=None,
                    buy_size=20, stop_loss=7, max_open=3, strategy="balanced"):
        import hashlib
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, strategy,
             json.dumps(coins or ["SOL"]), buy_size, stop_loss, mode,
             silent, max_open, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid

    def _activity(self, kind=None):
        q = "SELECT * FROM activity WHERE user_id = ?"
        args = [self.user_id]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return self.db.execute(q, args).fetchall()

    def _silent_activity(self):
        """Activity rows the silent loop wrote (excludes the signup row)."""
        return [a for a in self._activity()
                if a["kind"].startswith("silent")]

    def _run(self, prices, rpc_balance=0, executor=None):
        rpc_factory = lambda url: FakeRpc(url, rpc_balance)  # noqa: E731
        return run_once(self.db, self.cfg,
                        price_fetcher=lambda: prices,
                        rpc_factory=rpc_factory,
                        real_executor=executor)


class TestSilentDefaults(SilentBase):
    def test_create_defaults_off_paper_max3(self):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Luna", "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5"})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        self.assertEqual(bot["silent"], 0)
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["max_open_trades"], 3)

    def test_create_ignores_silent_on_param(self):
        self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Sneaky", "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5",
            "silent": "on"})
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Sneaky'").fetchone()
        self.assertEqual(bot["silent"], 0)

    def test_edit_flip_redirects_to_confirm_and_flag_unchanged(self):
        bot_id = self._insert_bot(silent=0)
        resp = self.client.post(f"/bots/{bot_id}/edit", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/edit"),
            "name": "Luna", "strategy": "balanced", "coins": "SOL",
            "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper", "silent": "on"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/bots/{bot_id}/silent?to=1", resp.location)
        bot = self.db.execute("SELECT silent FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 0)

    def test_confirm_enables_and_unconfirmed_post_does_not(self):
        bot_id = self._insert_bot(silent=0)
        resp = self.client.get(f"/bots/{bot_id}/silent?to=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("turn <strong>silent trading ON</strong>",
                      resp.get_data(as_text=True))
        # POST without confirmed=yes leaves it off.
        resp = self.client.post(f"/bots/{bot_id}/silent", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/silent?to=1"),
            "silent": "1"})
        bot = self.db.execute("SELECT silent FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 0)
        # POST with confirmed=yes enables it.
        resp = self.client.post(f"/bots/{bot_id}/silent", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/silent?to=1"),
            "silent": "1", "confirmed": "yes"})
        self.assertEqual(resp.status_code, 302)
        bot = self.db.execute("SELECT silent FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 1)
        self.assertEqual(len(self._activity("silent_on")), 1)

    def test_confirm_turns_off(self):
        bot_id = self._insert_bot(silent=1)
        self.client.post(f"/bots/{bot_id}/silent", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/silent?to=0"),
            "silent": "0", "confirmed": "yes"})
        bot = self.db.execute("SELECT silent FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 0)
        self.assertEqual(len(self._activity("silent_off")), 1)

    def test_mode_change_blocked_with_open_positions(self):
        bot_id = self._insert_bot(mode="paper")
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, 'SOL', 1, 100, 'paper')",
            (self.user_id, bot_id))
        self.db.commit()
        self.client.post(f"/bots/{bot_id}/edit", data={
            "csrf_token": csrf(self.client, f"/bots/{bot_id}/edit"),
            "name": "Luna", "strategy": "balanced", "coins": "SOL",
            "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "real"})
        bot = self.db.execute("SELECT mode FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["mode"], "paper")


class TestSilentLoop(SilentBase):
    def test_silent_off_bot_not_processed(self):
        self._insert_bot(silent=0)
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}))
        self.assertEqual(outcomes, [])
        self.assertEqual(len(self._silent_activity()), 0)

    def test_no_signal_logs_nothing(self):
        self._insert_bot(silent=1)
        outcomes = self._run(make_prices({"SOL": (100.0, 0.0)}))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertEqual(len(self._silent_activity()), 0)

    def test_paper_buy_within_limits(self):
        bot_id = self._insert_bot(silent=1, buy_size=20)
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}))
        self.assertEqual(len(outcomes[0]["actions"]), 1)
        act = outcomes[0]["actions"][0]
        self.assertEqual(act["action"], "buy")
        self.assertEqual(act["usd"], 2000.0)  # 20% of $10,000 bankroll
        self.assertEqual(act["amount"], 20.0)
        pos = self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["amount"], 20.0)
        self.assertEqual(pos["avg_price"], 100.0)
        cash = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        self.assertEqual(cash, 8000.0)
        self.assertEqual(len(self._activity("silent_buy")), 1)
        trade = self.db.execute(
            "SELECT * FROM paper_trades").fetchone()
        self.assertEqual(trade["side"], "buy")

    def test_second_buy_same_coin_skipped_already_holding(self):
        bot_id = self._insert_bot(silent=1)
        self._run(make_prices({"SOL": (100.0, 5.0)}))
        outcomes = self._run(make_prices({"SOL": (101.0, 6.0)}))
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone()["c"], 1)

    def test_max_open_trades_blocks_second_coin(self):
        self._insert_bot(silent=1, max_open=1, coins=["SOL", "BTC"])
        self._run(make_prices({"SOL": (100.0, 5.0), "BTC": (50000.0, 0.0)}))
        outcomes = self._run(make_prices({"SOL": (100.0, 0.0),
                                          "BTC": (50000.0, 5.0)}))
        skips = [a for a in outcomes[0]["actions"]
                 if a["action"] == "skip"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["reason"], "max open trades reached")
        self.assertEqual(len(self._activity("silent_skip")), 1)
        self.assertIn("max open trades", self._activity("silent_skip")[0]["detail"])

    def test_stop_loss_sells(self):
        bot_id = self._insert_bot(silent=1, stop_loss=7)
        # Position: 10 SOL @ $100, cash $9,000 after the buy.
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, 'SOL', 10, 100, 'paper')",
            (self.user_id, bot_id))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 10, 100)", (self.user_id,))
        self.db.execute(
            "UPDATE paper_portfolio SET cash_usd = 9000 WHERE user_id = ?",
            (self.user_id,))
        self.db.commit()
        outcomes = self._run(make_prices({"SOL": (90.0, 0.0)}))
        acts = outcomes[0]["actions"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["action"], "sell")
        self.assertEqual(acts[0]["reason"], "stop_loss")
        self.assertEqual(acts[0]["usd"], 900.0)
        self.assertIsNone(self.db.execute(
            "SELECT id FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())
        cash = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        self.assertEqual(cash, 9900.0)
        self.assertEqual(len(self._activity("silent_stop_loss")), 1)

    def test_sell_signal_sells_position(self):
        bot_id = self._insert_bot(silent=1)
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, 'SOL', 5, 100, 'paper')",
            (self.user_id, bot_id))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 5, 100)", (self.user_id,))
        self.db.execute(
            "UPDATE paper_portfolio SET cash_usd = 9500 WHERE user_id = ?",
            (self.user_id,))
        self.db.commit()
        outcomes = self._run(make_prices({"SOL": (94.0, -6.0)}))
        self.assertEqual(outcomes[0]["actions"][0]["action"], "sell")
        self.assertEqual(outcomes[0]["actions"][0]["reason"], "sell signal")
        self.assertIsNone(self.db.execute(
            "SELECT id FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())

    def test_real_zero_balance_skips_without_executor_call(self):
        calls = []

        def executor(*args, **kwargs):
            calls.append(True)
            return {"status": "executed", "signature": "x"}

        self._insert_bot(silent=1, mode="real")
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}),
                             rpc_balance=0, executor=executor)
        self.assertEqual(calls, [])
        skips = [a for a in outcomes[0]["actions"]
                 if a["action"] == "skip"]
        self.assertEqual(skips[0]["reason"], "insufficient real balance")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"], 0)

    def test_real_trade_skipped_no_swap_path_no_fake_fill(self):
        self._insert_bot(silent=1, mode="real")
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}),
                             rpc_balance=5_000_000_000)
        skips = [a for a in outcomes[0]["actions"]
                 if a["action"] == "skip"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["reason"], "real swap not available in v1")
        # No fill anywhere: no position, no paper trade, no send intent.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM bot_positions").fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM send_intents").fetchone()["c"], 0)
        self.assertIn("real swap not available",
                      self._activity("silent_skip")[0]["detail"])

    def test_injected_executor_wiring_records_fill(self):
        def executor(bot, user_id, coin, side, amount, price, db, cfg):
            return {"status": "executed", "signature": "fakesig1234567890",
                    "coin_amount": 0.001}

        self._insert_bot(silent=1, mode="real", coins=["BTC"])
        outcomes = self._run(make_prices({"BTC": (50000.0, 5.0)}),
                             rpc_balance=5_000_000_000, executor=executor)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        pos = self.db.execute(
            "SELECT * FROM bot_positions").fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["coin_id"], "BTC")
        self.assertEqual(pos["amount"], 0.001)
        self.assertEqual(len(self._activity("silent_buy")), 1)

    def test_one_bot_failure_does_not_stop_others(self):
        self._insert_bot(name="Bad", silent=1, coins=["SOL"])
        # Corrupt the first bot's coin list directly.
        self.db.execute("UPDATE bots SET coins = 'not-json' WHERE name='Bad'")
        self.db.commit()
        self._insert_bot(name="Good", silent=1, coins=["SOL"])
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}))
        self.assertEqual(len(outcomes), 2)
        bad = [o for o in outcomes if o["bot_name"] == "Bad"][0]
        good = [o for o in outcomes if o["bot_name"] == "Good"][0]
        self.assertIsNotNone(bad["error"])
        self.assertEqual(good["actions"][0]["action"], "buy")
        self.assertEqual(len(self._activity("silent_buy")), 1)

    def test_bots_page_shows_silent_badge(self):
        self._insert_bot(silent=1)
        # Bots live on the Bots page now — the dashboard is the Coins
        # page and does not list bots.
        self.assertNotIn("Silent ON",
                         self.client.get("/dashboard").get_data(as_text=True))
        resp = self.client.get("/bots")
        self.assertIn("Silent ON", resp.get_data(as_text=True))


class TestAssistantSilent(SilentBase):
    def test_parse_silent_commands(self):
        p = parse_message("turn on silent trading for Luna")
        self.assertEqual(p["intent"], "silent_on")
        self.assertEqual(p["bot_name"], "Luna")
        p = parse_message("turn off silent trading for Luna")
        self.assertEqual(p["intent"], "silent_off")
        self.assertEqual(p["bot_name"], "Luna")
        p = parse_message("is silent trading on")
        self.assertEqual(p["intent"], "silent_status")
        p = parse_message("what is silent trading")
        self.assertEqual(p["intent"], "silent_help")
        self.assertIn("trades on its own", p["reply"])

    def _ask(self, message):
        token = csrf(self.client, "/dashboard")
        resp = self.client.post("/api/assistant", json={
            "message": message, "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_assistant_silent_on_needs_yes(self):
        self._ask("create bot named Luna")
        data = self._ask("turn on silent trading for Luna")
        self.assertIn("Reply \"yes\"", data["reply"])
        bot = self.db.execute("SELECT silent FROM bots WHERE name='Luna'"
                              ).fetchone()
        self.assertEqual(bot["silent"], 0)
        data = self._ask("no")
        bot = self.db.execute("SELECT silent FROM bots WHERE name='Luna'"
                              ).fetchone()
        self.assertEqual(bot["silent"], 0)
        self._ask("turn on silent trading for Luna")
        data = self._ask("yes")
        self.assertIn("silent trading is now ON", data["reply"])
        bot = self.db.execute("SELECT silent FROM bots WHERE name='Luna'"
                              ).fetchone()
        self.assertEqual(bot["silent"], 1)
        self.assertEqual(len(self._activity("silent_on")), 1)

    def test_assistant_silent_off_applies_at_once(self):
        bot_id = self._insert_bot(silent=1)
        data = self._ask("turn off silent trading for Luna")
        self.assertIn("silent trading is OFF", data["reply"])
        bot = self.db.execute("SELECT silent FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 0)

    def test_assistant_silent_status(self):
        self._insert_bot(name="Luna", silent=1)
        self._insert_bot(name="Nova", silent=0)
        data = self._ask("is silent trading on")
        self.assertIn("Luna", data["reply"])
        self.assertIn("ON", data["reply"])
        self.assertIn("Nova", data["reply"])
        self.assertIn("OFF", data["reply"])

    def test_assistant_silent_on_unknown_bot(self):
        data = self._ask("turn on silent trading for Ghost")
        self.assertIn("couldn't find a bot", data["reply"])


if __name__ == "__main__":
    unittest.main()
