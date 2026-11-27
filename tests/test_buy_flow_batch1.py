"""Batch 1 — gas-fee logic (buy-flow rebuild, staged 2026-11-26).

Exactly two things, per James's batch order (2026-11-26 ~21:51):
1. The tiered gas reserve: a real buy must leave the reserve in
   the wallet — buy under $5 USD holds back 0.0012 SOL, buy $5 USD
   or more holds back 0.012 SOL. Applies to typed amounts as well
   as the % buttons (the buttons themselves are a later batch).
2. The real User-buy unblock: the three gates that refused a
   bot-less real buy — _real_bot_for_trade, execute_real_trade,
   execute_jupiter_swap — now let a User buy through.

No other buy-flow work lives here: no SL/TP, no failure logging,
no % buttons, no currency picker, no wallet live-or-no-data.
"""

import os
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, csrf,
                     grant_fuel, make_client, signup)
from test_executor import (CURVE_PRICE_USD, ExecutorBase, FakePumpRpc,
                           TEST_SOL_PRICE_USD, make_curve)
from tradingbot import pumpfun
from tradingbot.config import Config
from tradingbot.executor import execute_real_trade

# Gate — the devnet pump.fun mint used as the test coin throughout.
# SOL is the unit of account, never a listed/buyable coin, so every
# buy test uses this mint instead.
GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.0000005


def _test_config():
    return Config(env={
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "m.db"),
        "PAPER_START_CASH": "10000"})


class TestReserveTiers(unittest.TestCase):
    """The tier rule itself: under $5 USD -> 0.0012 SOL, $5 USD or
    more -> 0.012 SOL, unparseable -> the large tier (fail-safe)."""

    def test_under_five_usd_holds_small_reserve(self):
        cfg = _test_config()
        self.assertEqual(cfg.user_buy_gas_reserve_sol(4.99), 0.0012)
        self.assertEqual(cfg.user_buy_gas_reserve_sol(0.05), 0.0012)
        self.assertEqual(cfg.user_buy_gas_reserve_sol(0), 0.0012)

    def test_five_usd_or_more_holds_large_reserve(self):
        cfg = _test_config()
        self.assertEqual(cfg.user_buy_gas_reserve_sol(5.0), 0.012)
        self.assertEqual(cfg.user_buy_gas_reserve_sol(5.01), 0.012)
        self.assertEqual(cfg.user_buy_gas_reserve_sol(1000), 0.012)

    def test_unparseable_amount_fails_safe_to_large_tier(self):
        cfg = _test_config()
        self.assertEqual(cfg.user_buy_gas_reserve_sol(None), 0.012)
        self.assertEqual(cfg.user_buy_gas_reserve_sol("junk"), 0.012)

    def test_tiers_are_env_overridable(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "m.db"),
            "PAPER_START_CASH": "10000",
            "USER_BUY_RESERVE_THRESHOLD_USD": "10",
            "USER_BUY_RESERVE_SMALL_SOL": "0.002",
            "USER_BUY_RESERVE_LARGE_SOL": "0.02"})
        self.assertEqual(cfg.user_buy_gas_reserve_sol(9.99), 0.002)
        self.assertEqual(cfg.user_buy_gas_reserve_sol(10), 0.02)


def _start_route_mocks(tc):
    """Keep every live feed local: Gate priced from the feed tick,
    SOL $10 from the feed's SOL tick, no logo/RPC/Metaplex or
    Jupiter calls. The executors are mocked — these are route
    tests, not execution tests."""
    tc.mint_prices = {GATE: GATE_PRICE}
    tc.mint_identities = {GATE: {"name": "Gate", "symbol": "GATE"}}
    patches = [
        mock.patch.object(
            routes, "_chain_prices_for_mints",
            side_effect=lambda app, mints: {
                m: tc.mint_prices[m] for m in mints
                if m in tc.mint_prices}),
        mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints, http_get=None: {
                m: tc.mint_identities[m] for m in mints
                if m in tc.mint_identities}),
        mock.patch.object(routes, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "_default_get",
                          return_value={"pairs": []}),
        mock.patch.object(coin_meta_mod, "_from_metaplex",
                          return_value=None),
        mock.patch.object(
            jupiter_mod, "quote_buy_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None}),
        mock.patch.object(
            routes, "_feed_sol_price_usd",
            return_value=10.0),
        mock.patch.object(routes, "is_curve_complete",
                          return_value=False),
        mock.patch.object(routes, "execute_real_trade"),
        mock.patch.object(routes, "execute_jupiter_swap"),
    ]
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)
    routes.execute_real_trade.return_value = {
        "status": "executed", "signature": "sig-batch1",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    routes.execute_jupiter_swap.return_value = {
        "status": "executed", "signature": "sig-batch1-jup",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    tc.app.config["LIVE_FEED"].ingest(GATE, GATE_PRICE, "helius-stream")
    routes._TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
    home_holdings_mod.clear_image_cache()


class TestBatch1UserBuyRoute(unittest.TestCase):
    """Route level: a bot-less real buy goes through, and the
    reserve tier for its USD size reaches the executor."""

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
            "mode": "devnet"})

    def tearDown(self):
        self.tmp.cleanup()

    def _buy(self, **data):
        form = {"csrf_token": csrf(self.client, f"/coins/{GATE}"),
                "bot_id": ""}
        form.update(data)
        return self.client.post(f"/coins/{GATE}/buy", data=form)

    def _create_real_bot(self):
        self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "RealBot", "strategy": "bold", "coins": "SOL",
            "buy_size_pct": "25", "stop_loss_pct": "5", "mode": "real"})
        return self.db.execute(
            "SELECT id FROM bots WHERE name = 'RealBot'"
            ).fetchone()["id"]

    def test_user_buy_under_five_usd_holds_small_reserve(self):
        resp = self._buy(amount="4")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        routes.execute_real_trade.assert_called_once()
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertIsNone(_args[0], "User buy passes no bot")
        self.assertEqual(kwargs["user_buy_reserve_sol"], 0.0012)

    def test_user_buy_at_five_usd_holds_large_reserve(self):
        resp = self._buy(amount="5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertEqual(kwargs["user_buy_reserve_sol"], 0.012)

    def test_user_buy_typed_sol_sizes_in_usd(self):
        # 0.5 SOL at the $10 test SOL price is $5 USD — the large
        # tier, not the small one.
        resp = self._buy(amount_sol="0.5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertEqual(kwargs["user_buy_reserve_sol"], 0.012)

    def test_user_buy_james_small_trade(self):
        # James's case (2026-11-26 ~21:51): a small 0.005 SOL trade.
        # At $10 SOL that is $0.05 — well under $5, small tier.
        resp = self._buy(amount_sol="0.005")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertEqual(kwargs["user_buy_reserve_sol"], 0.0012)

    def test_user_buy_via_jupiter_gets_tier(self):
        routes.is_curve_complete.return_value = True
        resp = self._buy(amount="10")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        routes.execute_jupiter_swap.assert_called_once()
        _args, kwargs = routes.execute_jupiter_swap.call_args
        self.assertIsNone(_args[0], "User buy passes no bot")
        self.assertEqual(kwargs["user_buy_reserve_sol"], 0.012)
        routes.execute_real_trade.assert_not_called()

    def test_user_buy_with_bot_still_works(self):
        bot_id = self._create_real_bot()
        resp = self._buy(amount="4", bot_id=str(bot_id))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        _args, _kwargs = routes.execute_real_trade.call_args
        self.assertEqual(_args[0]["id"], bot_id)

    def test_reserve_refusal_surfaces_plainly(self):
        routes.execute_real_trade.return_value = {
            "status": "skipped",
            "reason": "insufficient SOL left for fees"}
        resp = self._buy(amount="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("insufficient SOL left for fees", html)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)


class TestBatch1UserBuyExecutor(ExecutorBase):
    """Executor level: a bot-less buy executes, and the reserve it
    must leave behind is enforced — the tier, not the old flat
    0.001 SOL fee reserve."""

    def _buy(self, bot, amount_sol, reserve, balance_lamports):
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          balance_lamports=balance_lamports)
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95,
                                            int(amount_sol * 1e9))
        return rpc, execute_real_trade(
            bot, self.user_id, self.mint, "buy", amount_sol,
            CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
            user_buy_reserve_sol=reserve,
            rpc_client=rpc)

    def test_user_buy_executes_without_bot(self):
        rpc, result = self._buy(None, 0.005, 0.0012, 10_000_000_000)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(rpc.sent), 1)

    def test_user_buy_refused_when_reserve_would_break(self):
        # 1.0005 SOL in, 1.0 SOL out: 0.0005 left is below the
        # 0.012 large tier (+ ATA rent) — nothing is sent.
        rpc, result = self._buy(None, 1.0, 0.012, 1_000_500_000)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_user_buy_without_tier_fails_safe_to_large(self):
        # No tier passed at all: the large tier is the fail-safe,
        # so the same edge buy is still refused.
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve(),
                          balance_lamports=1_000_500_000)
        result = execute_real_trade(
            None, self.user_id, self.mint, "buy", 1.0,
            CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
            rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_small_tier_allows_what_large_tier_refuses(self):
        # 1.01 SOL in, 1.0 SOL out: 0.01 left clears the 0.0012
        # small tier (+ ATA rent) but not the 0.012 large tier.
        rpc, result = self._buy(None, 1.0, 0.0012, 1_010_000_000)
        self.assertEqual(result["status"], "executed")
        rpc, result = self._buy(None, 1.0, 0.012, 1_010_000_000)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])


if __name__ == "__main__":
    unittest.main()
