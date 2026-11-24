"""Last-credit exit-all tests (James, 2026-11-24, revised — NO free
exits).

When fuel reaches its last trade left, that credit is reserved to
exit EVERY open real position in one sweep — one credit closes all
positions; buys can never consume the last credit. Manual sells
can't either: a single sell would eat the credit the sweep needs.
The sweep is the only spender of the last credit. After the sweep
fuel is 0 and the existing gate turns all real silent bots off
(paper untouched).
"""

import hashlib
import json
import tempfile
import unittest
from unittest import mock

import tradingbot.gate_fuel as gate_fuel_mod
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.silent_loop import (
    _open_position, run_exit_all_sweep, run_once,
)

# Gate — the devnet pump.fun mint used as the test coin throughout
# the manual-trade tests (SOL is the unit of account, never a
# listed/buyable coin). The silent-loop tests use plain coin names
# instead, so no live feed is ever consulted there.
GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.0000005


class FakeRpc:
    """RPC client serving a fat SOL balance — never a real network
    call. No get_transaction: _reconcile_sell_claim returns "ok"
    for a fresh DB (no open claim) without touching the RPC client."""

    def __init__(self, url, balance_lamports=0):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        raise AssertionError("silent loop must not send in tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("silent loop must not send in tests")


class FakeExecutor:
    """Records every call. Coins in `refusals` are refused with the
    given reason (e.g. "insufficient token balance"); every other
    coin executes with a unique fake signature."""

    def __init__(self, refusals=None):
        self.calls = []
        self.refusals = refusals or {}

    def __call__(self, bot, user_id, coin, side, amount, price, db, cfg):
        self.calls.append((coin, side, amount))
        if coin in self.refusals:
            return {"status": "refused", "reason": self.refusals[coin]}
        return {"status": "executed",
                "signature": f"fakesig-{coin}-{side}",
                "sol_received": 0.01, "coin_amount": amount}


def make_prices(changes):
    """changes: {coin: (price, change_24h)}"""
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


def set_fuel(db, user_id, n):
    """Set fuel to exactly `n` credits the honest way: credit a
    100-trade purchase (sets the ledger anchor), then consume
    100 - n with distinct fake signatures. Never grant_fuel with
    trades=1 — its fallback INSERT uses a nonexistent `sol_paid`
    column."""
    gate_fuel_mod.credit_purchase(
        db, user_id, gate_fuel_mod.bundles()[0], 0,
        "test-buy", "test-purchase")
    for i in range(100 - n):
        assert gate_fuel_mod.consume_fuel(
            db, user_id, None, f"setup-sig-{i:03d}"), \
            f"setup consume {i} failed"
    assert gate_fuel_mod.get_fuel(
        db, user_id)["trades_remaining"] == n


class ExitAllBase(unittest.TestCase):
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

    # -- setup helpers ------------------------------------------------

    def _insert_bot(self, name="Luna", mode="real", silent=1, coins=None,
                    buy_size=20, stop_loss=7, max_open=3,
                    strategy="balanced"):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, strategy,
             json.dumps(coins or ["AAA"]), buy_size, stop_loss, mode,
             silent, max_open, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid

    def _open_real_position(self, bot_id, coin, amount, price):
        bot = dict(self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone())
        _open_position(self.db, bot, coin, amount, price)

    def _positions(self):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE user_id = ?",
            (self.user_id,)).fetchall()

    def _fuel(self):
        return gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"]

    def _uses(self):
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM gate_fuel_uses WHERE user_id = ?",
            (self.user_id,)).fetchone()["n"]

    def _uses_delta(self, before):
        """Uses added since `before` — set_fuel's own 99 setup
        consumes are already in the table, so every sweep
        assertion is a delta, not an absolute."""
        return self._uses() - before

    def _sweep_trades(self):
        return self.db.execute(
            "SELECT * FROM gate_fuel_sweep_trades WHERE user_id = ?",
            (self.user_id,)).fetchall()

    def _run(self, prices, rpc_balance=0, executor=None,
             sweep_price_fetcher=None):
        return run_once(
            self.db, self.cfg,
            price_fetcher=lambda: prices,
            rpc_factory=lambda url: FakeRpc(url, rpc_balance),
            real_executor=executor,
            sweep_price_fetcher=sweep_price_fetcher)

    @staticmethod
    def _actions(outcomes):
        """Flatten run_once's per-bot outcome dicts into one action
        list."""
        return [a for o in outcomes for a in o.get("actions", [])]

    def _fetcher(self, price=0.01):
        """Price fetcher for the sweep: every mint at one price."""
        return lambda mints: {m: {"price_usd": price} for m in mints}

    # -- manual-trade (client) helpers --------------------------------

    def _set_real_mode(self):
        self.db.execute(
            "UPDATE users SET active_mode = 'devnet' WHERE id = ?",
            (self.user_id,))
        self.db.commit()

    def _mock_price_routes(self):
        """Keep the coin price local: the trenches feed prices GATE
        at GATE_PRICE, SOL is $10 — no DexScreener/Jupiter/RPC."""
        patches = [
            mock.patch.object(
                routes, "fetch_prices_for_mints",
                side_effect=lambda mints, http_get=None: {
                    m: GATE_PRICE for m in mints if m == GATE}),
            mock.patch.object(
                routes, "get_prices",
                return_value={"prices": {"SOL": {
                    "price": 10.0, "change_24h": 5.25,
                    "change_1h": None}},
                    "source": "live", "as_of": "test"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestSilentBuyRefusedAtLastCredit(ExitAllBase):
    """(a) A silent buy that would consume the last credit is
    refused with "last trade reserved" — the credit stays 1 and
    the executor is never called."""

    def test_buy_refused_at_fuel_1_with_open_position(self):
        bot_id = self._insert_bot(coins=["AAA", "BBB"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        set_fuel(self.db, self.user_id, 1)
        executor = FakeExecutor()
        outcomes = self._run(
            make_prices({"AAA": (0.02, 0.0), "BBB": (0.01, 5.0)}),
            rpc_balance=5_000_000_000, executor=executor)
        skips = [a for a in self._actions(outcomes)
                 if a.get("action") == "skip"
                 and a.get("reason") == "last trade reserved"]
        self.assertEqual(len(skips), 1, outcomes)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self._fuel(), 1)

    def test_buy_goes_through_at_fuel_1_with_no_open_position(self):
        """The reserve binds only while real positions are open —
        with no position to exit, the last credit is spendable."""
        bot_id = self._insert_bot(coins=["AAA"])
        set_fuel(self.db, self.user_id, 1)
        executor = FakeExecutor()
        self._run(make_prices({"AAA": (0.01, 5.0)}),
                  rpc_balance=5_000_000_000, executor=executor)
        self.assertEqual(len(executor.calls), 1, executor.calls)
        self.assertEqual(executor.calls[0][1], "buy")
        self.assertEqual(self._fuel(), 0)


class TestSweepClosesAllForOneCredit(ExitAllBase):
    """(b) The sweep sells every open real position and consumes
    exactly ONE credit for the whole sweep; the ledger backfill
    must not count the sweep sells as further uses."""

    def test_sweep_consumes_exactly_one_credit(self):
        bot_id = self._insert_bot(coins=["AAA", "BBB"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        self._open_real_position(bot_id, "BBB", 50.0, 0.02)
        set_fuel(self.db, self.user_id, 1)
        uses_before = self._uses()
        executor = FakeExecutor()
        result = run_exit_all_sweep(
            self.db, self.cfg, self.user_id,
            lambda url: FakeRpc(url, 5_000_000_000), executor,
            prices_data=make_prices({"SOL": (10.0, 0.0)}),
            price_fetcher=self._fetcher())
        self.assertTrue(result["ran"], result)
        self.assertTrue(result["consumed"], result)
        self.assertEqual(len(result["sold"]), 2, result)
        self.assertEqual(self._positions(), [])
        self.assertEqual(self._fuel(), 0)
        self.assertEqual(self._uses_delta(uses_before), 1)
        # Both sells are recorded as sweep trades, keyed by the
        # first executed sell's trade_id.
        sweep_rows = self._sweep_trades()
        self.assertEqual(len(sweep_rows), 2)
        use = self.db.execute(
            "SELECT trade_id FROM gate_fuel_uses WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()
        self.assertEqual(use["trade_id"], result["sold"][0]["trade_id"])
        # A later fuel read must NOT backfill the sweep sells as
        # extra uses — the ledger stays at exactly one sweep use.
        self.assertEqual(self._fuel(), 0)
        self.assertEqual(self._uses_delta(uses_before), 1)
        # The sweep ran while fuel was 1, so the gate's 0-fuel
        # turn-off fired for the real silent bot.
        bot = self.db.execute(
            "SELECT silent FROM bots WHERE id = ?", (bot_id,)).fetchone()
        self.assertEqual(bot["silent"], 0)

    def test_no_sweep_when_not_pending(self):
        bot_id = self._insert_bot(coins=["AAA"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        set_fuel(self.db, self.user_id, 5)
        executor = FakeExecutor()
        result = run_exit_all_sweep(
            self.db, self.cfg, self.user_id,
            lambda url: FakeRpc(url, 5_000_000_000), executor,
            price_fetcher=self._fetcher())
        self.assertFalse(result["ran"], result)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(self._positions()), 1)
        self.assertEqual(self._fuel(), 5)


class TestManualTradesRefusedAtLastCredit(ExitAllBase):
    """(c) Manual buy and manual sell are both refused at fuel == 1
    while real positions are open — the last credit is the sweep's
    alone."""

    def _setup_manual(self):
        self._set_real_mode()
        self._mock_price_routes()
        bot_id = self._insert_bot(coins=[GATE])
        self._open_real_position(bot_id, GATE, 100.0, GATE_PRICE)
        set_fuel(self.db, self.user_id, 1)
        return bot_id

    def test_manual_sell_refused_at_fuel_1(self):
        bot_id = self._setup_manual()
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 100.0, GATE_PRICE))
        self.db.commit()
        resp = self.client.post(f"/coins/{GATE}/sell", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "sell_all": "1", "bot_id": str(bot_id)})
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Last trade reserved", html)
        self.assertIn("This sell would use it up", html)
        # Nothing sold, nothing consumed.
        self.assertEqual(self._fuel(), 1)
        held = self.db.execute(
            "SELECT amount FROM real_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)).fetchone()
        self.assertEqual(held["amount"], 100.0)
        self.assertEqual(len(self._positions()), 1)

    def test_manual_buy_refused_at_fuel_1(self):
        bot_id = self._setup_manual()
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount_sol": "0.1", "bot_id": str(bot_id)})
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Last trade reserved", html)
        self.assertIn("This buy would use it up", html)
        self.assertEqual(self._fuel(), 1)
        self.assertEqual(len(self._positions()), 1)


class TestFuelZeroUnchanged(ExitAllBase):
    """(d) At fuel == 0 the old behaviour is untouched: the gate
    refuses with "Out of fuel" — never "Last trade reserved"."""

    def test_manual_buy_at_fuel_0_is_out_of_fuel(self):
        self._set_real_mode()
        self._mock_price_routes()
        bot_id = self._insert_bot(coins=[GATE])
        set_fuel(self.db, self.user_id, 0)
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "amount_sol": "0.1", "bot_id": str(bot_id)})
        html = resp.get_data(as_text=True)
        self.assertIn("Out of fuel", html)
        self.assertNotIn("Last trade reserved", html)

    def test_silent_buy_at_fuel_0_is_out_of_fuel(self):
        # Fuel first: the last setup consume fires the gate and
        # turns real bots off — the bot is inserted after, silent.
        set_fuel(self.db, self.user_id, 0)
        self._insert_bot(coins=["AAA"])
        executor = FakeExecutor()
        outcomes = self._run(
            make_prices({"AAA": (0.01, 5.0)}),
            rpc_balance=5_000_000_000, executor=executor)
        skips = [a for a in self._actions(outcomes)
                 if a.get("action") == "skip"
                 and a.get("reason") == "out of fuel"]
        self.assertEqual(len(skips), 1, outcomes)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self._fuel(), 0)


class TestPaperTradesUntouched(ExitAllBase):
    """(e) Paper trades never touch fuel — a paper buy and a paper
    stop-loss sell both execute at fuel == 1 and fuel stays 1."""

    def test_paper_buy_and_sell_execute_at_fuel_1(self):
        self._insert_bot(name="PaperLuna", mode="paper", coins=["SOL"])
        set_fuel(self.db, self.user_id, 1)
        outcomes = self._run(make_prices({"SOL": (100.0, 5.0)}))
        buys = [a for a in self._actions(outcomes)
                if a.get("action") == "buy"]
        self.assertEqual(len(buys), 1, outcomes)
        self.assertEqual(self._fuel(), 1)
        # Price crashes through the 7% stop — the paper sell fires.
        outcomes = self._run(make_prices({"SOL": (50.0, -50.0)}))
        sells = [a for a in self._actions(outcomes)
                if a.get("action") == "sell"]
        self.assertEqual(len(sells), 1, outcomes)
        self.assertEqual(self._fuel(), 1)


class TestSweepPastRefusedPosition(ExitAllBase):
    """(f) A refused position is logged and skipped; the sweep
    continues with the rest. When NOTHING executes, the credit is
    NOT consumed and the next scan retries."""

    def test_sweep_continues_past_refused_position(self):
        bot_id = self._insert_bot(coins=["AAA", "BBB"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        self._open_real_position(bot_id, "BBB", 50.0, 0.02)
        set_fuel(self.db, self.user_id, 1)
        uses_before = self._uses()
        executor = FakeExecutor(
            refusals={"BBB": "insufficient token balance"})
        result = run_exit_all_sweep(
            self.db, self.cfg, self.user_id,
            lambda url: FakeRpc(url, 5_000_000_000), executor,
            prices_data=make_prices({"SOL": (10.0, 0.0)}),
            price_fetcher=self._fetcher())
        self.assertTrue(result["ran"], result)
        self.assertTrue(result["consumed"], result)
        self.assertEqual(len(result["sold"]), 1, result)
        self.assertEqual(result["sold"][0]["coin"], "AAA")
        self.assertEqual(
            [s["coin"] for s in result["skipped"]], ["BBB"])
        # AAA is gone; BBB is still open, waiting for the self-heal.
        remaining = [p["coin_id"] for p in self._positions()]
        self.assertEqual(remaining, ["BBB"])
        self.assertEqual(self._fuel(), 0)
        self.assertEqual(self._uses_delta(uses_before), 1)

    def test_sweep_consumes_nothing_when_nothing_executes(self):
        bot_id = self._insert_bot(coins=["AAA"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        set_fuel(self.db, self.user_id, 1)
        uses_before = self._uses()
        executor = FakeExecutor(
            refusals={"AAA": "insufficient token balance"})
        result = run_exit_all_sweep(
            self.db, self.cfg, self.user_id,
            lambda url: FakeRpc(url, 5_000_000_000), executor,
            prices_data=make_prices({"SOL": (10.0, 0.0)}),
            price_fetcher=self._fetcher())
        self.assertTrue(result["ran"], result)
        self.assertFalse(result["consumed"], result)
        self.assertEqual(result["sold"], [])
        self.assertEqual(len(self._positions()), 1)
        self.assertEqual(self._fuel(), 1)
        self.assertEqual(self._uses_delta(uses_before), 0)
        # The retry: a second sweep on the next scan tries again
        # and still consumes nothing while the sell refuses.
        again = run_exit_all_sweep(
            self.db, self.cfg, self.user_id,
            lambda url: FakeRpc(url, 5_000_000_000), executor,
            prices_data=make_prices({"SOL": (10.0, 0.0)}),
            price_fetcher=self._fetcher())
        self.assertTrue(again["ran"], again)
        self.assertFalse(again["consumed"], again)
        self.assertEqual(self._fuel(), 1)
        self.assertEqual(self._uses_delta(uses_before), 0)


class TestRunOnceTriggersSweep(ExitAllBase):
    """The loop itself fires the sweep: when the bot evaluation
    pass ends with fuel at 1 and real positions open, the sweep
    closes them for one credit — the evaluation pass alone does
    not sell (the price is above the stop), the sweep does."""

    def test_run_once_sweeps_after_evaluation(self):
        bot_id = self._insert_bot(coins=["AAA"])
        self._open_real_position(bot_id, "AAA", 100.0, 0.01)
        set_fuel(self.db, self.user_id, 1)
        uses_before = self._uses()
        executor = FakeExecutor()
        outcomes = self._run(
            make_prices({"AAA": (0.02, 0.0)}),
            rpc_balance=5_000_000_000, executor=executor,
            sweep_price_fetcher=self._fetcher())
        # The evaluation pass sold nothing (price above the stop);
        # the sweep sold the one position.
        eval_sells = [a for a in self._actions(outcomes)
                      if a.get("action") == "sell"]
        self.assertEqual(eval_sells, [], outcomes)
        self.assertEqual(len(executor.calls), 1, executor.calls)
        self.assertEqual(executor.calls[0][0], "AAA")
        self.assertEqual(executor.calls[0][1], "sell")
        self.assertEqual(self._positions(), [])
        self.assertEqual(self._fuel(), 0)
        self.assertEqual(self._uses_delta(uses_before), 1)


if __name__ == "__main__":
    unittest.main()
