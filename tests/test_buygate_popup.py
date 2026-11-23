"""Buy $GATE button + holdings-funded fuel (2026-11-22).

The Buy $GATE button on the homepage Buy popup buys $GATE to HOLD
(presets 1M / 5M / 10M at the live curve price, or a custom SOL
amount). The wallet page's "fuel trades with Gate" button enrolls
held $GATE as fuel: exactly 10,000 $GATE per confirmed executed
real trade, trades left = floor(held / 10,000), live balance check
before every real trade (below 10,000 it stops, topping up
resumes it automatically — no latch). Paper trades stay free.
The bundle model is SUPERSEDED — nothing here touches it.
"""
import os
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.gate_fuel as gate_fuel_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
import tradingbot.silent_loop as silent_loop_mod
import tradingbot.solana as solana_mod
from helpers import csrf, make_client, signup
from tradingbot.config import Config
from tradingbot.pumpfun import (TOKEN_BASE, bonding_curve_pda, buy_quote,
                                global_pda, sol_for_tokens)
from tradingbot.silent_loop import _buy_coin, _sell_position_claimed

GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
MAINNET_GATE = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"
GATE_PRICE = 0.0000005
LAMPORTS_PER_SOL = 1_000_000_000


def _curve_bytes(vt, vs, rt, rs, supply, complete=False):
    """Bonding-curve account data per pumpfun.parse_bonding_curve."""
    return (b"\x00" * 8
            + int(vt).to_bytes(8, "little")
            + int(vs).to_bytes(8, "little")
            + int(rt).to_bytes(8, "little")
            + int(rs).to_bytes(8, "little")
            + int(supply).to_bytes(8, "little")
            + bytes([1 if complete else 0]))


def _global_bytes(fee_bps=100):
    """Global account data per pumpfun.parse_global (113 bytes)."""
    return (b"\x00" * 8 + b"\x00" + b"\x00" * 32 + b"\x00" * 32
            + (0).to_bytes(8, "little") * 4
            + int(fee_bps).to_bytes(8, "little"))


def _gate_curve(vt=1_073_000_000_000_000, vs=30 * 10 ** 9,
                rt=5 * 10 ** 14, rs=3 * 10 ** 8, supply=10 ** 15,
                complete=False):
    """A live $GATE curve: 1,073,000,000 whole tokens virtual,
    30 SOL virtual — the mainnet-like starting shape, so all
    three presets are buyable."""
    return _curve_bytes(vt, vs, rt, rs, supply, complete)


class _FakeRpc:
    """RPC client serving a canned global + curve account, a canned
    $GATE token balance, and a canned SOL balance."""

    def __init__(self, curves=None, gate_tokens=0.0,
                 balance_lamports=10 * LAMPORTS_PER_SOL):
        self._curves = curves or {}
        self._gate_tokens = gate_tokens
        self._balance = balance_lamports

    def get_account_info(self, pubkey):
        return self._curves.get(pubkey)

    def get_balance_lamports(self, pubkey):
        return self._balance

    def _call(self, method, params):
        if method != "getTokenAccountsByOwner":
            return {"value": []}
        # A token account lives under one program only; the other
        # program returns nothing (else the balance would double).
        program = (params[1] or {}).get("programId")
        if program != home_holdings_mod.TOKEN_PROGRAM_ID:
            return {"value": []}
        if self._gate_tokens <= 0:
            return {"value": []}
        base = int(self._gate_tokens * TOKEN_BASE)
        return {"value": [{
            "account": {"data": {"parsed": {"info": {
                "mint": GATE,
                "tokenAmount": {"amount": str(base), "decimals": 6,
                                "uiAmount": self._gate_tokens}}}}}}]}


def _rpc_curves(curve_data=None, fee_bps=100):
    curves = {global_pda()[0]: {"data": _global_bytes(fee_bps)}}
    if curve_data is not None:
        curves[bonding_curve_pda(GATE)[0]] = {"data": curve_data}
    return curves


def _start_route_mocks(tc):
    """Keep every live feed local (same shape as test_buy_mode)."""
    tc.mint_prices = {GATE: GATE_PRICE}
    tc.mint_identities = {GATE: {"name": "Gate", "symbol": "GATE"}}
    patches = [
        mock.patch.object(
            routes, "fetch_prices_for_mints",
            side_effect=lambda mints, http_get=None: {
                m: tc.mint_prices[m] for m in mints
                if m in tc.mint_prices}),
        mock.patch.object(routes, "fetch_mint_1h_changes",
                          return_value={}),
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
            routes, "get_prices",
            return_value={"prices": {"SOL": {
                "price": 10.0, "change_24h": 5.25,
                "change_1h": None}},
                "source": "live", "as_of": "test"}),
    ]
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)
    routes._TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
    home_holdings_mod.clear_image_cache()


def _user_id(app):
    return app.config["DB"].execute(
        "SELECT id FROM users WHERE username = 'alice'").fetchone()["id"]


def _set_mode(app, user_id, mode):
    db = app.config["DB"]
    db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
               (mode, user_id))
    db.commit()


def _create_real_bot(client, app, name="RealBot"):
    resp = client.post("/bots/create", data={
        "csrf_token": csrf(client, "/bots/create"),
        "name": name, "strategy": "bold", "coins": "SOL",
        "buy_size_pct": "25", "stop_loss_pct": "5", "mode": "real"})
    assert resp.status_code == 200, resp.status_code
    return app.config["DB"].execute(
        "SELECT id FROM bots WHERE name = ?",
        (name,)).fetchone()["id"]


def _fuel_rows(db, user_id):
    return db.execute(
        "SELECT * FROM gate_fuel_uses WHERE user_id = ?",
        (user_id,)).fetchall()


class TestSolForTokens(unittest.TestCase):
    """sol_for_tokens is the exact inverse of buy_quote."""

    def setUp(self):
        self.curve = {"virtual_token_reserves": 1_073_000_000_000_000,
                      "virtual_sol_reserves": 30 * 10 ** 9,
                      "real_token_reserves": 5 * 10 ** 14,
                      "real_sol_reserves": 3 * 10 ** 8,
                      "token_total_supply": 10 ** 15,
                      "complete": False}

    def test_minimal_sol_covers_preset_and_one_less_does_not(self):
        for tokens in (1_000_000, 5_000_000, 10_000_000):
            sol = sol_for_tokens(self.curve, 100, tokens * TOKEN_BASE)
            self.assertIsNotNone(sol, tokens)
            self.assertGreater(sol, 0)
            self.assertGreaterEqual(
                buy_quote(self.curve, 100, sol), tokens * TOKEN_BASE)
            self.assertLess(
                buy_quote(self.curve, 100, sol - 1), tokens * TOKEN_BASE)

    def test_cost_grows_faster_than_linear_curve_impact(self):
        sol_1m = sol_for_tokens(self.curve, 100, 1_000_000 * TOKEN_BASE)
        sol_10m = sol_for_tokens(self.curve, 100, 10_000_000 * TOKEN_BASE)
        self.assertGreater(sol_10m, 10 * sol_1m)

    def test_impossible_amounts_return_none(self):
        self.assertIsNone(sol_for_tokens(self.curve, 100, 0))
        self.assertIsNone(sol_for_tokens(self.curve, 100, -5))
        # The whole virtual reserve (and more) cannot be bought.
        self.assertIsNone(sol_for_tokens(
            self.curve, 100, 1_073_000_000 * TOKEN_BASE))
        self.assertIsNone(sol_for_tokens(self.curve, 10000, 1000))
        self.assertIsNone(sol_for_tokens(
            {"virtual_token_reserves": 0, "virtual_sol_reserves": 0},
            100, 1000))

    def test_tokens_out_round_trips_the_inverse(self):
        sol = sol_for_tokens(self.curve, 100, 5_000_000 * TOKEN_BASE)
        out = gate_fuel_mod.tokens_out_for_sol(
            self.curve, 100, sol / LAMPORTS_PER_SOL)
        self.assertIsNotNone(out)
        self.assertGreaterEqual(out, 5_000_000)
        self.assertIsNone(
            gate_fuel_mod.tokens_out_for_sol(self.curve, 100, 0))
        self.assertIsNone(
            gate_fuel_mod.tokens_out_for_sol(self.curve, 100, -1))


class TestEffectiveGateMint(unittest.TestCase):
    def test_devnet_default_is_devnet_mint(self):
        cfg = Config(env={"NETWORK": "devnet"})
        self.assertEqual(gate_fuel_mod.effective_gate_mint(cfg), GATE)

    def test_mainnet_mint_never_used_on_devnet(self):
        cfg = Config(env={"NETWORK": "devnet", "GATE_MINT": MAINNET_GATE})
        self.assertIsNone(gate_fuel_mod.effective_gate_mint(cfg))

    def test_mainnet_without_the_gate_is_none(self):
        cfg = Config(env={"NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "0"})
        self.assertIsNone(gate_fuel_mod.effective_gate_mint(cfg))

    def test_mainnet_with_the_gate_is_mainnet_mint(self):
        cfg = Config(env={"NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "1"})
        self.assertEqual(gate_fuel_mod.effective_gate_mint(cfg),
                         MAINNET_GATE)

    def test_mainnet_env_mint_wins_over_devnet_default(self):
        cfg = Config(env={"NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "1",
                          "GATE_MINT": MAINNET_GATE})
        self.assertEqual(gate_fuel_mod.effective_gate_mint(cfg),
                         MAINNET_GATE)


class TestGateBuyQuoteEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.rpc = _FakeRpc(curves=_rpc_curves(_gate_curve()))
        self.patch = mock.patch.object(
            routes, "SolanaRpcClient", lambda url, **kwargs: self.rpc)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quote_lists_live_sol_for_each_preset(self):
        resp = self.client.get("/api/gate-buy-quote")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["available"])
        self.assertEqual(data["mint"], GATE)
        self.assertEqual([p["tokens"] for p in data["presets"]],
                         [1_000_000, 5_000_000, 10_000_000])
        sols = [p["sol"] for p in data["presets"]]
        self.assertTrue(all(s is not None and s > 0 for s in sols))
        self.assertLess(sols[0], sols[1])
        self.assertLess(sols[1], sols[2])
        self.assertGreater(sols[2], 10 * sols[0])

    def test_quote_with_sol_returns_gate_out(self):
        resp = self.client.get("/api/gate-buy-quote?sol=0.05")
        data = resp.get_json()
        self.assertTrue(data["available"])
        self.assertGreater(data["tokens_out"], 0)

    def test_quote_fail_closed_when_curve_unreadable(self):
        self.rpc._curves = _rpc_curves(None)
        data = self.client.get("/api/gate-buy-quote").get_json()
        self.assertFalse(data["available"])
        self.assertIn("unavailable", data["reason"])

    def test_quote_fail_closed_when_curve_complete(self):
        self.rpc._curves = _rpc_curves(_gate_curve(complete=True))
        data = self.client.get("/api/gate-buy-quote").get_json()
        self.assertFalse(data["available"])

    def test_quote_unavailable_on_mainnet_even_with_the_gate(self):
        env = {"SESSION_SECRET": "x" * 64, "ENCRYPTION_KEY": "ab" * 32,
               "NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "1",
               "DATABASE_PATH": os.path.join(self.tmp.name, "main.db")}
        from tradingbot import create_app
        main_app = create_app(Config(env=env))
        main_app.testing = True
        main_client = main_app.test_client()
        signup(main_client, "alice")
        data = main_client.get("/api/gate-buy-quote").get_json()
        self.assertFalse(data["available"])
        self.assertIn("mainnet", data["reason"])


class TestBuyGatePopup(unittest.TestCase):
    """The Buy $GATE button + screen are in the popup template."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wallet_page_has_buy_gate_button_and_screen(self):
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn('id="quick-trade-gate-open"', html)
        self.assertIn("Buy $GATE", html)
        self.assertIn('id="quick-trade-gate"', html)
        for preset in (1000000, 5000000, 10000000):
            self.assertIn(f'data-gate-tokens="{preset}"', html)
        self.assertIn('id="quick-trade-gate-custom-sol"', html)
        self.assertIn("/api/gate-buy-quote", html)
        self.assertIn(GATE, html)


class TestPaperBuyPreset(unittest.TestCase):
    """A paper buy of a preset lands exactly that many $GATE."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_paper_preset_lands_exact_tokens(self):
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_tokens": "1000000", "bot_id": ""},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        row = self.app.config["DB"].execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)).fetchone()
        self.assertIsNotNone(row)
        # Exact to within float dust from the existing paper fill
        # (usd / (mark x 1.01)): 1,000,000.0000000001, not a short
        # or over fill.
        self.assertAlmostEqual(float(row["amount"]), 1_000_000, delta=0.01)
        # Paper trades never consume fuel.
        self.assertEqual(_fuel_rows(self.app.config["DB"], self.user_id),
                         [])


class TestManualRealFuel(unittest.TestCase):
    """The manual real buy/sell gate + drawdown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)
        self.bot_id = _create_real_bot(self.client, self.app)
        self.rpc = _FakeRpc(curves=_rpc_curves(_gate_curve()),
                            gate_tokens=0.0)
        patches = [
            mock.patch.object(routes, "SolanaRpcClient",
                              lambda url, **kwargs: self.rpc),
            mock.patch.object(solana_mod, "SolanaRpcClient",
                              lambda url, **kwargs: self.rpc),
            mock.patch.object(routes, "execute_real_trade"),
            mock.patch.object(routes, "execute_jupiter_swap"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mock_execute = routes.execute_real_trade
        self.mock_execute.return_value = {
            "status": "executed", "signature": "sig-manual-1",
            "coin_amount": 1000000.0, "sol_spent": 0.03,
            "signing_bot": "test"}
        routes.execute_jupiter_swap.return_value = {
            "status": "skipped", "reason": "no jupiter in tests"}
        gate_fuel_mod.clear_fuel_counter_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def _switch_real(self):
        _set_mode(self.app, self.user_id, "devnet")

    def test_real_buy_without_enrollment_ignores_fuel(self):
        self._switch_real()
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_sol": "0.03", "bot_id": str(self.bot_id)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.mock_execute.called)
        self.assertEqual(_fuel_rows(self.app.config["DB"], self.user_id),
                         [])

    def test_real_buy_refused_when_enrolled_and_below_10000(self):
        self._switch_real()
        gate_fuel_mod.set_fuel_enrolled(
            self.app.config["DB"], self.user_id, True)
        self.rpc._gate_tokens = 5000.0
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_sol": "0.03", "bot_id": str(self.bot_id)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.mock_execute.called)
        self.assertIn("No fuel", resp.get_data(as_text=True))

    def test_real_buy_consumes_10000_when_enrolled_and_funded(self):
        self._switch_real()
        gate_fuel_mod.set_fuel_enrolled(
            self.app.config["DB"], self.user_id, True)
        self.rpc._gate_tokens = 25000.0
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_sol": "0.03", "bot_id": str(self.bot_id)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.mock_execute.called)
        rows = _fuel_rows(self.app.config["DB"], self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-manual-1")
        self.assertEqual(float(rows[0]["tokens"]), 10000)
        # 25,000 held - 10,000 consumed = 15,000 left = 1 trade.
        left = gate_fuel_mod.fuel_trades_left(
            self.app.config["DB"], self.user_id, 25000.0)
        self.assertEqual(left, 1)

    def test_real_sell_consumes_10000_when_enrolled_and_funded(self):
        self._switch_real()
        gate_fuel_mod.set_fuel_enrolled(
            self.app.config["DB"], self.user_id, True)
        self.rpc._gate_tokens = 25000.0
        db = self.app.config["DB"]
        db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 100.0, GATE_PRICE))
        db.commit()
        self.mock_execute.return_value = {
            "status": "executed", "signature": "sig-sell-1",
            "coin_amount": 100.0, "sol_received": 0.01,
            "signing_bot": "test"}
        resp = self.client.post(f"/coins/{GATE}/sell", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "sell_all": "1", "bot_id": str(self.bot_id)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.mock_execute.called)
        rows = _fuel_rows(db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-sell-1")


class TestLoopFuel(unittest.TestCase):
    """The silent loop's fuel gate + drawdown on real trades."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)
        self.db = self.app.config["DB"]
        self.bot_id = _create_real_bot(self.client, self.app)
        self.bot = self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (self.bot_id,)).fetchone()
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.rpc = _FakeRpc(curves=_rpc_curves(_gate_curve()),
                            gate_tokens=0.0)
        self.executor = mock.Mock(return_value={
            "status": "executed", "signature": "sig-loop-1",
            "coin_amount": 500.0, "sol_spent": 0.02,
            "sol_received": 0.02, "signing_bot": "test"})
        self.meta_patch = mock.patch.object(
            silent_loop_mod, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        self.addCleanup(self.meta_patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _rpc_factory(self, url):
        return self.rpc

    def test_loop_buy_refused_when_enrolled_and_below_10000(self):
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.rpc._gate_tokens = 9999.0
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertFalse(self.executor.called)
        self.assertTrue(any(a.get("action") == "skip"
                            and "fuel" in str(a.get("reason", "")).lower()
                            for a in actions))
        self.assertEqual(_fuel_rows(self.db, self.user_id), [])

    def test_loop_buy_consumes_after_confirmed_execution(self):
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.rpc._gate_tokens = 25000.0
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertTrue(self.executor.called)
        self.assertTrue(any(a.get("action") == "buy" for a in actions))
        rows = _fuel_rows(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-loop-1")

    def test_loop_buy_without_enrollment_ignores_fuel(self):
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertTrue(self.executor.called)
        self.assertEqual(_fuel_rows(self.db, self.user_id), [])

    def test_loop_sell_refused_and_consumed(self):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, "
            "amount, avg_price, mode) VALUES (?, ?, ?, ?, ?, 'real')",
            (self.user_id, self.bot_id, GATE, 500.0, GATE_PRICE))
        self.db.commit()
        # Refused when enrolled and dry.
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.rpc._gate_tokens = 0.0
        actions = []
        _sell_position_claimed(self.db, self.cfg, self.bot, GATE,
                               GATE_PRICE, "stop_loss",
                               self._rpc_factory, self.executor, actions,
                               amount=500.0)
        self.assertFalse(self.executor.called)
        # Funded: executes and consumes once, keyed by signature.
        self.rpc._gate_tokens = 20000.0
        self.executor.return_value = {
            "status": "executed", "signature": "sig-loop-sell",
            "coin_amount": 500.0, "sol_received": 0.02,
            "signing_bot": "test"}
        actions = []
        _sell_position_claimed(self.db, self.cfg, self.bot, GATE,
                               GATE_PRICE, "stop_loss",
                               self._rpc_factory, self.executor, actions,
                               amount=500.0)
        self.assertTrue(self.executor.called)
        rows = _fuel_rows(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-loop-sell")


class TestFuelAccounting(unittest.TestCase):
    """Enrollment, idempotent consumption, trades-left math."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_enroll_toggle(self):
        self.assertFalse(gate_fuel_mod.is_fuel_enrolled(
            self.db, self.user_id))
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.assertTrue(gate_fuel_mod.is_fuel_enrolled(
            self.db, self.user_id))
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, False)
        self.assertFalse(gate_fuel_mod.is_fuel_enrolled(
            self.db, self.user_id))

    def test_consume_is_idempotent_per_signature(self):
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.assertTrue(gate_fuel_mod.consume_gate_fuel(
            self.db, self.user_id, "sig-1"))
        self.assertFalse(gate_fuel_mod.consume_gate_fuel(
            self.db, self.user_id, "sig-1"))
        self.assertEqual(len(_fuel_rows(self.db, self.user_id)), 1)

    def test_consume_needs_enrollment_and_signature(self):
        self.assertFalse(gate_fuel_mod.consume_gate_fuel(
            self.db, self.user_id, "sig-1"))
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        self.assertFalse(gate_fuel_mod.consume_gate_fuel(
            self.db, self.user_id, None))
        self.assertFalse(gate_fuel_mod.consume_gate_fuel(
            self.db, self.user_id, ""))
        self.assertEqual(_fuel_rows(self.db, self.user_id), [])

    def test_trades_left_is_floor_division(self):
        self.assertEqual(
            gate_fuel_mod.fuel_trades_left(self.db, self.user_id, 0), 0)
        self.assertEqual(
            gate_fuel_mod.fuel_trades_left(self.db, self.user_id, 9999), 0)
        self.assertEqual(
            gate_fuel_mod.fuel_trades_left(self.db, self.user_id, 10000), 1)
        self.assertEqual(
            gate_fuel_mod.fuel_trades_left(self.db, self.user_id, 1000000),
            100)
        # Consumed fuel reduces what is left.
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        gate_fuel_mod.consume_gate_fuel(self.db, self.user_id, "sig-1")
        self.assertEqual(
            gate_fuel_mod.fuel_trades_left(self.db, self.user_id, 25000), 1)

    def test_held_balance_from_chain(self):
        rpc = _FakeRpc(gate_tokens=12345.678)
        held = gate_fuel_mod.held_gate_tokens_from_chain(
            rpc, "any-pubkey", GATE)
        self.assertAlmostEqual(held, 12345.678)
        rpc._gate_tokens = 0.0
        self.assertEqual(
            gate_fuel_mod.held_gate_tokens_from_chain(
                rpc, "any-pubkey", GATE), 0.0)

    def test_gate_check_auto_resumes_on_top_up(self):
        # Live check, not a latch: dry refuses, topping up passes
        # with no re-enable step.
        gate_fuel_mod.set_fuel_enrolled(self.db, self.user_id, True)
        cfg = self.app.config["TRADINGBOT_CONFIG"]
        pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]
        rpc = _FakeRpc(gate_tokens=5000.0)
        ok, _reason, _held, left = gate_fuel_mod.fuel_gate_check(
            self.db, cfg, rpc, pubkey, self.user_id)
        self.assertFalse(ok)
        self.assertEqual(left, 0)
        rpc._gate_tokens = 10000.0
        ok, _reason, _held, left = gate_fuel_mod.fuel_gate_check(
            self.db, cfg, rpc, pubkey, self.user_id)
        self.assertTrue(ok)
        self.assertEqual(left, 1)


class TestWalletFuelStrip(unittest.TestCase):
    """$GATE first in holdings, fuel strip + topbar chip in devnet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)
        _set_mode(self.app, self.user_id, "devnet")
        self.rpc = _FakeRpc(curves=_rpc_curves(_gate_curve()),
                            gate_tokens=1000000.0)
        patches = [
            mock.patch.object(routes, "SolanaRpcClient",
                              lambda url, **kwargs: self.rpc),
            mock.patch.object(solana_mod, "SolanaRpcClient",
                              lambda url, **kwargs: self.rpc),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        gate_fuel_mod.clear_fuel_counter_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_row_first_with_fuel_strip_and_chip(self):
        db = self.app.config["DB"]
        # A second holding whose mint sorts BEFORE Gate's
        # alphabetically: without the $GATE-first rule it would
        # list first. Gate must still come first.
        other = "11111111111111111111111111111111"
        self.mint_prices[other] = 0.000001
        self.mint_identities[other] = {"name": "System", "symbol": "SYS"}
        db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, other, 5.0, 0.000001))
        db.commit()
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertLess(html.index("/coins/" + GATE),
                        html.index("/coins/" + other))
        self.assertIn("gate-fuel-strip", html)
        self.assertIn("fuel trades with Gate", html)
        self.assertIn("100 trades left", html)
        # The topbar chip is on the page too (1,000,000 / 10,000).
        self.assertIn("fuel-chip", html)
        self.assertIn("100 trades", html)

    def test_enroll_button_flips_enrollment(self):
        db = self.app.config["DB"]
        self.assertFalse(gate_fuel_mod.is_fuel_enrolled(db, self.user_id))
        resp = self.client.post("/gate-fuel/enroll", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "enrolled": "1"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(gate_fuel_mod.is_fuel_enrolled(db, self.user_id))
        html = resp.get_data(as_text=True)
        self.assertIn("Stop fueling with Gate", html)

    def test_chip_hidden_in_paper_mode(self):
        _set_mode(self.app, self.user_id, "paper")
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertNotIn("fuel-chip", html)


if __name__ == "__main__":
    unittest.main()
