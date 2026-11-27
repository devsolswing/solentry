"""Batch 3 — stop loss / take profit on User holdings (buy-flow rebuild).

One logical change: a User buy can carry a stop loss and a take
profit, and the silent loop enforces them — a live price at or
beyond a trigger sells the whole holding, even when no bot is
running. Take profit always means a positive profit % (James,
2026-11-26): a zero or negative number is refused at the route
and in storage, so a loss can never be stored as a take profit.

Covered: user_exits storage (trigger math, TP-positive
enforcement, reason), the buy route (paper + real: storing the
exit off the executed average cost, result-page lines showing
both the target price and the %, TP/SL refusal), and the loop
(paper + real exits firing, waiting without a live price, stale
rules cleared, a failed real sell keeping its rule, the sold
real holding leaving real_holdings).

No other buy-flow work lives here: no % buttons, no wallet
live-or-"no data", no paper stale refusal, no bot-picker removal,
no currency picker.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
import tradingbot.silent_loop as silent_loop
from helpers import csrf, grant_fuel, make_client, signup
from test_buy_flow_batch1 import GATE, GATE_PRICE, _start_route_mocks
from tradingbot.silent_loop import _check_user_exits
from tradingbot.user_exits import (
    clear_user_exit, get_user_exit, set_user_exit, trigger_prices,
    user_exit_reason)


class TestTriggerPrices(unittest.TestCase):
    """trigger_prices: the % off the entry price, nothing else."""

    def test_stop_and_take_profit_from_entry(self):
        stop, tp = trigger_prices(0.01, 5, 10)
        self.assertAlmostEqual(stop, 0.0095)
        self.assertAlmostEqual(tp, 0.011)

    def test_unset_side_has_no_trigger(self):
        stop, tp = trigger_prices(0.01, 5, None)
        self.assertAlmostEqual(stop, 0.0095)
        self.assertIsNone(tp)
        stop, tp = trigger_prices(0.01, None, None)
        self.assertIsNone(stop)
        self.assertIsNone(tp)

    def test_bad_entry_or_pct_yields_no_trigger(self):
        self.assertEqual(trigger_prices(0, 5, 10), (None, None))
        self.assertEqual(trigger_prices(None, 5, 10), (None, None))
        stop, tp = trigger_prices(0.01, 0, 10)
        self.assertIsNone(stop)
        self.assertAlmostEqual(tp, 0.011)
        stop, tp = trigger_prices(0.01, -5, 10)
        self.assertIsNone(stop)
        self.assertAlmostEqual(tp, 0.011)


class _ExitsDbBase(unittest.TestCase):
    """A throwaway DB with the user_exits table (init_db)."""

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


class TestSetUserExit(_ExitsDbBase):
    """set_user_exit: stores the rule, refuses a non-positive
    take profit — a loss is never a take profit."""

    def test_stores_pcts_and_trigger_prices(self):
        row = set_user_exit(self.db, self.user_id, GATE, "paper",
                            0.0000005, 5, 10)
        self.assertIsNotNone(row)
        self.assertEqual(row["stop_loss_pct"], 5)
        self.assertEqual(row["take_profit_pct"], 10)
        self.assertAlmostEqual(row["stop_price"], 0.000000475)
        self.assertAlmostEqual(row["take_profit_price"], 0.00000055)
        self.assertEqual(row["entry_price"], 0.0000005)
        self.assertEqual(
            get_user_exit(self.db, self.user_id, GATE, "paper")["id"],
            row["id"])

    def test_zero_or_negative_take_profit_is_refused(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                set_user_exit(self.db, self.user_id, GATE, "paper",
                              0.0000005, 5, bad)
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_zero_or_negative_stop_loss_is_refused(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                set_user_exit(self.db, self.user_id, GATE, "paper",
                              0.0000005, bad, 10)
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_neither_pct_deletes_the_row(self):
        set_user_exit(self.db, self.user_id, GATE, "paper",
                      0.0000005, 5, 10)
        self.assertIsNotNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))
        self.assertIsNone(set_user_exit(
            self.db, self.user_id, GATE, "paper", 0.0000005, None, None))
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_clear_deletes_the_row(self):
        set_user_exit(self.db, self.user_id, GATE, "paper",
                      0.0000005, 5, None)
        clear_user_exit(self.db, self.user_id, GATE, "paper")
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))


class TestUserExitReason(unittest.TestCase):
    """user_exit_reason: which exit a live price triggers, if any."""

    def _row(self):
        return {"stop_price": 0.000000475,
                "take_profit_price": 0.00000055}

    def test_price_at_or_below_stop_fires_stop_loss(self):
        self.assertEqual(user_exit_reason(self._row(), 0.000000475),
                         "stop_loss")
        self.assertEqual(user_exit_reason(self._row(), 0.0000004),
                         "stop_loss")

    def test_price_at_or_above_take_profit_fires_it(self):
        self.assertEqual(user_exit_reason(self._row(), 0.00000055),
                         "take_profit")
        self.assertEqual(user_exit_reason(self._row(), 0.0000006),
                         "take_profit")

    def test_price_between_triggers_fires_nothing(self):
        self.assertIsNone(user_exit_reason(self._row(), 0.0000005))

    def test_missing_or_bad_price_fires_nothing(self):
        self.assertIsNone(user_exit_reason(self._row(), None))
        self.assertIsNone(user_exit_reason(self._row(), 0))
        self.assertIsNone(user_exit_reason(None, 0.0000004))


class _Batch3RouteBase(unittest.TestCase):
    """Route tests: the batch-1 harness (live feeds kept local,
    executors mocked)."""

    MODE = "paper"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
        grant_fuel(self.db, self.user_id, 100)
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": self.MODE})

    def tearDown(self):
        self.tmp.cleanup()

    def _buy(self, **data):
        form = {"csrf_token": csrf(self.client, f"/coins/{GATE}"),
                "bot_id": ""}
        form.update(data)
        return self.client.post(f"/coins/{GATE}/buy", data=form)

    def _exit_row(self, mode):
        return self.db.execute(
            "SELECT * FROM user_exits WHERE user_id = ? "
            "AND coin_id = ? AND mode = ?",
            (self.user_id, GATE, mode)).fetchone()


class TestBatch3PaperBuyExits(_Batch3RouteBase):
    """A paper User buy with SL/TP ticked stores the exit off the
    executed average cost and shows both the target price and the
    % on the result page."""

    MODE = "paper"

    def test_sl_tp_stored_and_shown_with_price_and_pct(self):
        resp = self._buy(usd="4", stop_loss="1", stop_loss_pct="5",
                         take_profit="1", take_profit_pct="10")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("User buy — no bot watching this coin.", html)
        self.assertIn("Stop loss set: sells at", html)
        self.assertIn("5% below", html)
        self.assertIn("Take profit set: sells at", html)
        self.assertIn("10% above", html)
        held = self.db.execute(
            "SELECT avg_price FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)).fetchone()
        self.assertIsNotNone(held, "no paper holding after the buy")
        avg = float(held["avg_price"])
        self.assertAlmostEqual(avg, GATE_PRICE, delta=GATE_PRICE * 0.02)
        row = self._exit_row("paper")
        self.assertIsNotNone(row, "no user_exits row stored")
        self.assertEqual(row["stop_loss_pct"], 5)
        self.assertEqual(row["take_profit_pct"], 10)
        self.assertAlmostEqual(row["entry_price"], avg)
        self.assertAlmostEqual(row["stop_price"], avg * 0.95)
        self.assertAlmostEqual(row["take_profit_price"], avg * 1.10)

    def test_negative_take_profit_refuses_the_whole_buy(self):
        resp = self._buy(usd="4", take_profit="1",
                         take_profit_pct="-5")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Take profit must be a positive %", html)
        self.assertIn("a loss can never be a take profit", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 0,
            "a refused buy must not fill")
        self.assertIsNone(self._exit_row("paper"))

    def test_zero_take_profit_is_refused_too(self):
        resp = self._buy(usd="4", take_profit="1",
                         take_profit_pct="0")
        self.assertIn("Nothing was bought",
                      resp.get_data(as_text=True))
        self.assertIn("Take profit must be a positive %",
                      resp.get_data(as_text=True))
        self.assertIsNone(self._exit_row("paper"))

    def test_zero_stop_loss_is_refused(self):
        resp = self._buy(usd="4", stop_loss="1", stop_loss_pct="0")
        self.assertIn("Nothing was bought",
                      resp.get_data(as_text=True))
        self.assertIn("Stop loss must be a positive %",
                      resp.get_data(as_text=True))
        self.assertIsNone(self._exit_row("paper"))

    def test_non_numeric_pct_is_refused(self):
        resp = self._buy(usd="4", stop_loss="1",
                         stop_loss_pct="abc")
        self.assertIn("Nothing was bought",
                      resp.get_data(as_text=True))
        self.assertIn("Stop loss must be a number.",
                      resp.get_data(as_text=True))

    def test_no_checkbox_means_no_exit_row(self):
        resp = self._buy(usd="4")
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertIsNone(self._exit_row("paper"))


class TestBatch3RealBuyExits(_Batch3RouteBase):
    """A real User buy with SL/TP ticked stores the exit off the
    executed cost; a negative take profit refuses the buy."""

    MODE = "devnet"

    def test_sl_tp_stored_off_executed_cost(self):
        resp = self._buy(amount="4", stop_loss="1", stop_loss_pct="5",
                         take_profit="1", take_profit_pct="10")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("User buy — no bot watching this coin.", html)
        self.assertIn("Stop loss set: sells at", html)
        self.assertIn("5% below", html)
        self.assertIn("Take profit set: sells at", html)
        self.assertIn("10% above", html)
        # Mocked executor: 0.005 SOL spent for 1,000,000 tokens
        # at a $10 SOL price -> entry 0.00000005.
        row = self._exit_row("real")
        self.assertIsNotNone(row, "no user_exits row stored")
        self.assertAlmostEqual(row["entry_price"], 0.00000005)
        self.assertAlmostEqual(row["stop_price"], 0.0000000475)
        self.assertAlmostEqual(row["take_profit_price"], 0.000000055)

    def test_negative_take_profit_refuses_the_buy(self):
        resp = self._buy(amount="4", take_profit="1",
                         take_profit_pct="-10")
        self.assertIn("Nothing was bought",
                      resp.get_data(as_text=True))
        self.assertIn("Take profit must be a positive %",
                      resp.get_data(as_text=True))
        routes.execute_real_trade.assert_not_called()
        self.assertIsNone(self._exit_row("real"))


class _Batch3LoopBase(_ExitsDbBase):
    """Loop tests: _check_user_exits against a real harness DB,
    prices from a patched chain read (no feed, no chain)."""

    def setUp(self):
        super().setUp()
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self._price_patch = mock.patch.object(
            silent_loop, "_chain_price_usd", return_value={})
        self._price_patch.start()
        self.addCleanup(self._price_patch.stop)

    def _set_price(self, price):
        silent_loop._chain_price_usd.return_value = (
            {GATE: price} if price is not None else {})

    def _check(self, real_executor=None):
        return _check_user_exits(
            self.db, self.cfg, None, real_executor,
            feed=None, prices_data=None)

    def _activity(self, kind):
        return self.db.execute(
            "SELECT kind, detail FROM activity WHERE user_id = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1",
            (self.user_id, kind)).fetchone()


class TestBatch3LoopPaperExits(_Batch3LoopBase):
    """The loop sells a paper User holding when its stop loss or
    take profit fires — and only then."""

    def _hold(self, amount=1000.0, avg=GATE_PRICE):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, amount, avg))
        self.db.commit()
        set_user_exit(self.db, self.user_id, GATE, "paper",
                      avg, 5, 10)

    def test_stop_loss_fires_and_clears_the_rule(self):
        self._hold()
        self._set_price(GATE_PRICE * 0.9)  # below the 5% stop
        outcomes = self._check()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "sell")
        self.assertIn("stop loss", outcomes[0]["actions"][0]["reason"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM paper_holdings WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 0,
            "the fired holding must be sold")
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))
        row = self._activity("user_exit")
        self.assertIsNotNone(row, "no user_exit log row")
        self.assertIn("stop loss", row["detail"])

    def test_take_profit_fires_and_clears_the_rule(self):
        self._hold()
        self._set_price(GATE_PRICE * 1.2)  # above the 10% take profit
        outcomes = self._check()
        self.assertEqual(outcomes[0]["actions"][0]["action"], "sell")
        self.assertIn("take profit",
                      outcomes[0]["actions"][0]["reason"])
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_price_between_triggers_fires_nothing(self):
        self._hold()
        self._set_price(GATE_PRICE)
        outcomes = self._check()
        self.assertEqual(outcomes, [])
        self.assertEqual(self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)
            ).fetchone()["amount"], 1000.0)
        self.assertIsNotNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_no_live_price_waits_for_the_next_pass(self):
        self._hold()
        self._set_price(None)
        outcomes = self._check()
        # The pass reports the skip (no silent skip) but sells
        # nothing — the rule waits for the next pass.
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertIn("no live price",
                      outcomes[0]["actions"][0]["reason"])
        self.assertEqual(self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)
            ).fetchone()["amount"], 1000.0)
        self.assertIsNotNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))

    def test_stale_rule_clears_when_the_holding_is_gone(self):
        set_user_exit(self.db, self.user_id, GATE, "paper",
                      GATE_PRICE, 5, 10)
        self._set_price(GATE_PRICE * 0.9)
        outcomes = self._check()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "paper"))


class TestBatch3LoopRealExits(_Batch3LoopBase):
    """The loop sells a real User holding when its stop loss fires
    — through the same executor and fuel gates as a bot sell — and
    the sold holding leaves real_holdings."""

    def _hold(self, amount=1000.0, avg=GATE_PRICE):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, amount, avg))
        self.db.commit()
        grant_fuel(self.db, self.user_id, 100)
        set_user_exit(self.db, self.user_id, GATE, "real",
                      avg, 5, 10)

    def _mock_executor(self, result):
        return mock.Mock(return_value=result)

    def test_stop_loss_fires_removes_holding_and_clears_rule(self):
        self._hold()
        self._set_price(GATE_PRICE * 0.9)
        executor = self._mock_executor({
            "status": "executed", "signature": "sig-exit-1",
            "sol_received": 0.004, "signing_bot": "test"})
        outcomes = self._check(real_executor=executor)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "sell")
        self.assertIn("stop loss", outcomes[0]["actions"][0]["reason"])
        executor.assert_called_once()
        _args, _kwargs = executor.call_args
        self.assertEqual(_args[3], "sell")  # side
        self.assertEqual(_args[2], GATE)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 0,
            "the fired holding must leave real_holdings")
        self.assertIsNone(
            get_user_exit(self.db, self.user_id, GATE, "real"))
        row = self._activity("user_exit")
        self.assertIsNotNone(row, "no user_exit log row")
        self.assertIn("stop loss", row["detail"])
        self.assertIn("sig-exit-1", row["detail"])

    def test_failed_real_sell_keeps_its_rule(self):
        self._hold()
        self._set_price(GATE_PRICE * 0.9)
        executor = self._mock_executor({
            "status": "skipped", "reason": "decision price is stale"})
        outcomes = self._check(real_executor=executor)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertEqual(self.db.execute(
            "SELECT amount FROM real_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)
            ).fetchone()["amount"], 1000.0,
            "a failed sell must not remove the holding")
        self.assertIsNotNone(
            get_user_exit(self.db, self.user_id, GATE, "real"),
            "a failed sell must keep its rule for the next pass")
        row = self._activity("user_exit")
        self.assertIsNotNone(row)
        self.assertIn("decision price is stale", row["detail"])
        self.assertIn("Fix: ", row["detail"])
        self.assertIn("No transaction reached the chain", row["detail"])


if __name__ == "__main__":
    unittest.main()
