"""Buy $GATE packages + package fuel (2026-11-23).

The Buy $GATE button opens the shared package popup: 1M / 5M /
10M $GATE packages at the live curve price. The purchased $GATE
goes straight to the dev wallet and the trades are credited
automatically once the transfer is confirmed safe on-chain. One
confirmed executed real trade costs 1 trade credit; at zero fuel
real trades are refused with the no-fuel page and real silent bots
turn off. Paper trades stay free. $GATE bought outside the app can
never be fuel — the balance starts at 0 and grows only from
packages bought in the app. The holdings-funded fuel model is
gone: no enrollment, no 10,000-$GATE-per-trade drawdown, no
floor division, no cached counter.
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
from helpers import csrf, grant_fuel, make_client, signup
from tradingbot import create_app
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



class TestGateMint(unittest.TestCase):
    """The package mint is network-aware, with no buyability gate:
    packages buy through the pump.fun executor while $GATE is still
    bonding, so the mint resolves on mainnet whether or not a
    Jupiter path exists."""

    def test_devnet_default_is_devnet_mint(self):
        cfg = Config(env={"NETWORK": "devnet"})
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), GATE)

    def test_mainnet_mint_never_used_on_devnet(self):
        cfg = Config(env={"NETWORK": "devnet", "GATE_MINT": MAINNET_GATE})
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), GATE)

    def test_mainnet_default_is_mainnet_mint(self):
        cfg = Config(env={"NETWORK": "mainnet"})
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), MAINNET_GATE)

    def test_mainnet_without_jupiter_still_resolves(self):
        cfg = Config(env={"NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "0"})
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), MAINNET_GATE)

    def test_mainnet_env_mint_wins(self):
        cfg = Config(env={"NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "1",
                          "GATE_MINT": MAINNET_GATE})
        self.assertEqual(gate_fuel_mod.gate_mint(cfg), MAINNET_GATE)

    def test_delegates_agree_with_gate_mint(self):
        for env in ({"NETWORK": "devnet"},
                    {"NETWORK": "mainnet"}):
            cfg = Config(env=env)
            self.assertEqual(gate_fuel_mod.effective_gate_mint(cfg),
                             gate_fuel_mod.gate_mint(cfg))
            self.assertEqual(gate_fuel_mod.held_gate_mint(cfg),
                             gate_fuel_mod.gate_mint(cfg))


class TestGateQuoteEndpoint(unittest.TestCase):
    """GET /api/gate-quote: the package quote at the live price."""

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

    def test_quote_lists_live_sol_for_each_package(self):
        resp = self.client.get("/api/gate-quote?bundle=bundle_1m")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["price_available"])
        self.assertEqual(data["bundle_id"], "bundle_1m")
        self.assertEqual(data["trades"], 100)
        self.assertEqual(data["coins"], 1_000_000)
        self.assertIsNotNone(data["sol_estimate"])
        self.assertGreater(data["sol_estimate"], 0)
        # Bigger packages cost more SOL, with the bonus trades.
        for bundle_id, trades, bonus in (
                ("bundle_5m", 550, 50), ("bundle_10m", 1450, 450)):
            data = self.client.get(
                f"/api/gate-quote?bundle={bundle_id}").get_json()
            self.assertTrue(data["price_available"])
            self.assertEqual(data["trades"], trades)
            self.assertEqual(data["bonus"], bonus)
            self.assertGreater(data["sol_estimate"], 0)

    def test_quote_unknown_bundle_is_400(self):
        resp = self.client.get("/api/gate-quote?bundle=bundle_999")
        self.assertEqual(resp.status_code, 400)

    def test_quote_fail_closed_when_no_live_price(self):
        # Curve unreadable AND the price feed empty: no quote.
        self.rpc._curves = _rpc_curves(None)
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               lambda mints: {}):
            data = self.client.get(
                "/api/gate-quote?bundle=bundle_1m").get_json()
        self.assertFalse(data["price_available"])
        self.assertIsNone(data["sol_estimate"])

    def test_quote_available_on_mainnet(self):
        """Packages are buyable on mainnet — the quote does not
        refuse for the network. (The old /api/gate-buy-quote
        refused on mainnet; that endpoint is gone.)"""
        env = {"SESSION_SECRET": "x" * 64, "ENCRYPTION_KEY": "ab" * 32,
               "NETWORK": "mainnet", "JUPITER_REAL_SWAPS": "1",
               "DATABASE_PATH": os.path.join(self.tmp.name, "main.db")}
        from tradingbot import create_app
        main_app = create_app(Config(env=env))
        main_app.testing = True
        main_client = main_app.test_client()
        signup(main_client, "alice")
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               lambda mints: {MAINNET_GATE: 0.000002}), \
            mock.patch.object(
                routes, "get_prices",
                return_value={"prices": {"SOL": {
                    "price": 150.0, "change_24h": 0,
                    "change_1h": None}},
                    "source": "live", "as_of": "test"}):
            data = main_client.get(
                "/api/gate-quote?bundle=bundle_1m").get_json()
        self.assertTrue(data["price_available"])
        self.assertGreater(data["sol_estimate"], 0)


class TestBuyGatePopup(unittest.TestCase):
    """The Buy $GATE button opens the shared package popup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wallet_page_has_buy_gate_button_and_popup(self):
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("Buy $GATE", html)
        self.assertIn("gate-buy-popup", html)
        self.assertIn("data-gate-buy-open", html)
        for amount, trades in (("1M $GATE", "100 trades"),
                               ("5M $GATE", "550 trades"),
                               ("10M $GATE", "1450 trades")):
            self.assertIn(amount, html)
            self.assertIn(trades, html)
        self.assertIn("/api/gate-quote", html)
        # The live price refreshes while the popup is open.
        self.assertIn("setInterval", html)
        self.assertIn(GATE, html)

    def test_old_purchase_screen_is_gone(self):
        """No in-overlay purchase screen, no preset SOL buttons,
        no custom-SOL buy, no enroll button anywhere."""
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertNotIn('id="quick-trade-gate"', html)
        self.assertNotIn('id="quick-trade-gate-open"', html)
        self.assertNotIn("data-gate-tokens=", html)
        self.assertNotIn("quick-trade-gate-custom-sol", html)
        self.assertNotIn("/api/gate-buy-quote", html)
        self.assertNotIn("gate-fuel-strip", html)
        self.assertNotIn("fuel trades with Gate", html)
        self.assertNotIn("gate-fuel/enroll", html)


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
    """The manual real buy/sell gate + drawdown (package model)."""

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

    def tearDown(self):
        self.tmp.cleanup()

    def _switch_real(self):
        _set_mode(self.app, self.user_id, "devnet")

    def _buy(self):
        return self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "amount_sol": "0.03", "bot_id": str(self.bot_id)},
            follow_redirects=True)

    def test_real_buy_refused_at_zero_fuel(self):
        """No package credits: the buy never executes, the page
        shows the no-fuel banner, and the real bot turns off."""
        self._switch_real()
        resp = self._buy()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.mock_execute.called)
        html = resp.get_data(as_text=True)
        self.assertIn("Out of fuel", html)
        self.assertIn("Buy $GATE", html)
        self.assertIn("data-gate-no-fuel-show", html)
        self.assertIn("gate-no-fuel-banner", html)
        row = self.app.config["DB"].execute(
            "SELECT silent FROM bots WHERE id = ?",
            (self.bot_id,)).fetchone()
        self.assertEqual(row["silent"], 0)

    def test_real_buy_consumes_one_credit_when_funded(self):
        self._switch_real()
        grant_fuel(self.app.config["DB"], self.user_id, 100)
        resp = self._buy()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.mock_execute.called)
        rows = _fuel_rows(self.app.config["DB"], self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-manual-1")
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.app.config["DB"], self.user_id)["trades_remaining"], 99)

    def test_real_buy_twice_with_same_signature_consumes_once(self):
        """A retried buy with the same confirmed signature cannot
        double-spend fuel: the second consume is a no-op."""
        self._switch_real()
        grant_fuel(self.app.config["DB"], self.user_id, 100)
        self.assertEqual(self._buy().status_code, 200)
        self.assertEqual(self._buy().status_code, 200)
        self.assertEqual(len(_fuel_rows(self.app.config["DB"],
                                        self.user_id)), 1)
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.app.config["DB"], self.user_id)["trades_remaining"], 99)

    def test_real_sell_consumes_one_credit_when_funded(self):
        self._switch_real()
        grant_fuel(self.app.config["DB"], self.user_id, 100)
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
        self.assertEqual(gate_fuel_mod.get_fuel(
            db, self.user_id)["trades_remaining"], 99)

    def test_real_sell_refused_at_zero_fuel(self):
        self._switch_real()
        db = self.app.config["DB"]
        db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 100.0, GATE_PRICE))
        db.commit()
        resp = self.client.post(f"/coins/{GATE}/sell", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "sell_all": "1", "bot_id": str(self.bot_id)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.mock_execute.called)
        self.assertIn("Out of fuel", resp.get_data(as_text=True))


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

    def test_loop_buy_refused_at_zero_fuel(self):
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertFalse(self.executor.called)
        self.assertEqual(actions[0]["action"], "skip")
        self.assertEqual(actions[0]["reason"], "out of fuel")
        self.assertEqual(_fuel_rows(self.db, self.user_id), [])
        self.assertEqual(self.db.execute(
            "SELECT silent FROM bots WHERE id = ?",
            (self.bot_id,)).fetchone()["silent"], 0)

    def test_loop_buy_consumes_after_confirmed_execution(self):
        grant_fuel(self.db, self.user_id, 100)
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertTrue(self.executor.called)
        self.assertTrue(any(a.get("action") == "buy" for a in actions))
        rows = _fuel_rows(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-loop-1")
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)

    def test_loop_buy_zero_coin_fill_still_consumes_by_signature(self):
        """An executed buy that filled zero tokens still counts:
        the transaction executed on-chain, so the credit is owed
        by signature even with no ledger row."""
        grant_fuel(self.db, self.user_id, 100)
        self.executor.return_value = {
            "status": "executed", "signature": "sig-loop-zero",
            "coin_amount": 0.0, "sol_spent": 0.02,
            "signing_bot": "test"}
        actions = []
        _buy_coin(self.db, self.cfg, self.bot, GATE, GATE_PRICE, {},
                  self._rpc_factory, self.executor, actions)
        self.assertTrue(self.executor.called)
        rows = _fuel_rows(self.db, self.user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-loop-zero")
        self.assertIsNone(rows[0]["trade_id"])
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)

    def test_loop_sell_refused_and_consumed(self):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, "
            "amount, avg_price, mode) VALUES (?, ?, ?, ?, ?, 'real')",
            (self.user_id, self.bot_id, GATE, 500.0, GATE_PRICE))
        self.db.commit()
        # Refused at zero fuel.
        actions = []
        _sell_position_claimed(self.db, self.cfg, self.bot, GATE,
                               GATE_PRICE, "stop_loss",
                               self._rpc_factory, self.executor, actions,
                               amount=500.0)
        self.assertFalse(self.executor.called)
        # Funded: executes and consumes once, keyed by signature.
        grant_fuel(self.db, self.user_id, 100)
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
    """Packages, idempotent consumption, ledger truth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.user_id = _user_id(self.app)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_balance_starts_at_zero(self):
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)

    def test_held_gate_never_becomes_fuel(self):
        """1,000,000 $GATE held in the wallet is NOT fuel — only
        packages bought in the app create credits."""
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 1_000_000.0, GATE_PRICE))
        self.db.commit()
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)

    def test_consume_is_idempotent_per_signature(self):
        grant_fuel(self.db, self.user_id, 100)
        self.assertTrue(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, "sig-1"))
        # Same signature again: already counted, no second credit.
        self.assertTrue(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, "sig-1"))
        self.assertEqual(len(_fuel_rows(self.db, self.user_id)), 1)
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)

    def test_consume_needs_a_signature(self):
        grant_fuel(self.db, self.user_id, 100)
        self.assertFalse(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, None))
        self.assertFalse(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, ""))
        self.assertEqual(_fuel_rows(self.db, self.user_id), [])

    def test_gate_passes_after_purchase_not_before(self):
        self.assertFalse(gate_fuel_mod.enforce_fuel_gate(
            self.db, self.user_id))
        grant_fuel(self.db, self.user_id, 100)
        self.assertTrue(gate_fuel_mod.enforce_fuel_gate(
            self.db, self.user_id))

    def test_ledger_truth_beats_stale_balance_row(self):
        """The gate_fuel balance row is a cache: corrupting it does
        not change the ledger-truth count."""
        grant_fuel(self.db, self.user_id, 100)
        for sig in ("sig-1", "sig-2", "sig-3"):
            gate_fuel_mod.consume_fuel(self.db, self.user_id, None, sig)
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 97)
        for corrupt in (50, 150):
            self.db.execute(
                "UPDATE gate_fuel SET trades_remaining = ? "
                "WHERE user_id = ?", (corrupt, self.user_id))
            self.db.commit()
            self.assertEqual(gate_fuel_mod.get_fuel(
                self.db, self.user_id)["trades_remaining"], 97)


class TestWalletFuelRow(unittest.TestCase):
    """$GATE first in holdings, fuel row + topbar chip in devnet."""

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

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_row_first_with_fuel_row_and_chip(self):
        db = self.app.config["DB"]
        grant_fuel(db, self.user_id, 100)
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
        self.assertIn("gate-top-row", html)
        self.assertIn("Buy $GATE", html)
        self.assertIn("trades left", html)
        self.assertIn("data-gate-fuel-count", html)
        # The topbar chip shows the ledger count (100).
        self.assertIn("fuel-chip", html)
        self.assertIn("Trades Left", html)

    def test_mode_switch_is_gone(self):
        """Mode is locked at sign-in: no switch form, no /mode
        route, no mode_switch anywhere in the rendered page."""
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertNotIn('action="/mode"', html)
        self.assertNotIn("mode_switch", html)
        self.assertNotIn("hud-mode", html)
        resp = self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "mode": "paper"})
        self.assertEqual(resp.status_code, 404)

    def test_chip_shows_free_in_paper_mode(self):
        _set_mode(self.app, self.user_id, "paper")
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("free ~ ∞", html)
        self.assertNotIn("Trades Left:", html)


class TestGateFuelMigration(unittest.TestCase):
    """The holdings-era gate_fuel_uses table migrates to the
    package shape on init: old rows stay as history, package
    balance starts at 0."""

    def test_holdings_era_rows_migrate_and_never_convert(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "old.db")
        # Build a holdings-era DB by hand: the old uses table has
        # no trade_id column.
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username "
            "TEXT, password_hash TEXT, security_question TEXT, "
            "security_answer_hash TEXT, email TEXT, active_mode TEXT, "
            "mode_chosen INT, created_at TEXT);"
            "INSERT INTO users (id, username, active_mode) VALUES "
            "(1, 'olduser', 'devnet');"
            "CREATE TABLE gate_fuel_uses (id INTEGER PRIMARY KEY, "
            "user_id INTEGER, signature TEXT UNIQUE, tokens REAL, "
            "created_at TEXT);"
            "INSERT INTO gate_fuel_uses (user_id, signature, tokens, "
            "created_at) VALUES (1, 'old-sig-1', 10000, "
            "'2026-11-20T00:00:00Z');")
        conn.commit()
        conn.close()
        app = create_app(Config(env={
            "SESSION_SECRET": "x" * 64, "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet", "DATABASE_PATH": db_path}))
        db = app.config["DB"]
        cols = [r[1] for r in db.execute(
            "PRAGMA table_info(gate_fuel_uses)").fetchall()]
        self.assertIn("trade_id", cols)
        self.assertIn("tokens", cols)
        self.assertIn("signature", cols)
        # The old row survived the migration, still keyed by its
        # signature — and it is NOT fuel: the package balance
        # starts at 0.
        old = db.execute(
            "SELECT * FROM gate_fuel_uses WHERE signature = ?",
            ("old-sig-1",)).fetchone()
        self.assertIsNotNone(old)
        self.assertIsNone(old["trade_id"])
        self.assertEqual(gate_fuel_mod.get_fuel(
            db, 1)["trades_remaining"], 0)
        # A second init is a no-op (idempotent migration).
        app2 = create_app(Config(env={
            "SESSION_SECRET": "x" * 64, "ENCRYPTION_KEY": "ab" * 32,
            "NETWORK": "devnet", "DATABASE_PATH": db_path}))
        db2 = app2.config["DB"]
        self.assertEqual(len(db2.execute(
            "SELECT * FROM gate_fuel_uses").fetchall()), 1)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
