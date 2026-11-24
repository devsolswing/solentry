"""$GATE trade packages tests (James, 2026-11-23).

Packages only, at the live market price; the purchased $GATE goes
straight to the dev wallet; one confirmed executed trade costs one
credit; at zero fuel real trades stop and real bots turn off; paper
stays free. The ledger (purchases minus uses) is the truth.
Pricing: exact coin amounts — the user always receives exactly the
package's coins and pays the live SOL quote for them. $GATE bought
outside the app can never be fuel.
"""

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

import tradingbot.gate_fuel as gate_fuel_mod
import tradingbot.routes as routes
from helpers import csrf, grant_fuel, make_client, signup
from tradingbot.ledger import record_trade
from tradingbot.silent_loop import _buy_coin

GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
TEST_DEV_WALLET = "4XWpRuKMH5uVGiXkvV5PSKSBi36dBQVSy9QJ59Kvda7Q"
ZERO_BLOCKHASH = "11111111111111111111111111111111"


class FakeRpc:
    """RPC client serving a fat SOL balance and a zero blockhash —
    never a real network call."""

    def __init__(self, url=None, balance_lamports=250_000_000_000):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        return ZERO_BLOCKHASH


def _fake_inspection(wallet_pubkey, balance_base_units, decimals=6):
    """A token_send inspection dict with a full bundle on hand."""
    return {
        "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "payer": wallet_pubkey,
        "mint": GATE,
        "destination": TEST_DEV_WALLET,
        "sender_ata": ZERO_BLOCKHASH,
        "dest_ata": ZERO_BLOCKHASH,
        "create_ata": False,
        "balance": balance_base_units,
        "decimals": decimals,
        "rent_lamports": 0,
    }


class FuelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.wallet_pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]
        self.feed = self.app.config["LIVE_FEED"]

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_logo(self):
        """Patch the $GATE logo fetch (DexScreener) so page renders
        never touch the network in tests."""
        patch = mock.patch.object(
            gate_fuel_mod, "get_gate_logo_url",
            lambda mint: "/static/fox-gold-480.jpg")
        patch.start()
        self.addCleanup(patch.stop)

    def _insert_bot(self, mode="real", silent=1, name="Trenches"):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, "balanced", json.dumps([GATE]),
             20, 7, mode, silent, 3,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?",
            (cur.lastrowid,)).fetchone()

    def _stub_purchase(self, coins, inspection_balance=None):
        """Stub the pump.fun buy + the dev-wallet send. Returns the
        call record. The buy executes; the send lands."""
        calls = {}

        def fake_buy(bot, uid, coin, side, amount, price, db, cfg):
            calls["buy"] = {"bot": bot["name"], "mode": bot["mode"],
                            "buy_size": bot["buy_size_pct"],
                            "coin": coin, "side": side,
                            "amount": amount}
            return {"status": "executed", "signature": "BUYSIG111",
                    "coin_amount": float(coins), "sol_spent": 20.2,
                    "signing_bot": "test-bot"}

        def fake_inspect(client, wallet, mint, dest):
            calls["inspect_dest"] = dest
            calls["inspect_mint"] = mint
            return _fake_inspection(
                wallet,
                inspection_balance if inspection_balance is not None
                else coins * 10 ** 6)

        patches = [
            mock.patch.object(gate_fuel_mod, "execute_real_trade",
                              fake_buy),
            mock.patch.object(gate_fuel_mod.token_send_mod,
                              "inspect_token", fake_inspect),
            mock.patch.object(
                gate_fuel_mod, "send_and_confirm",
                lambda rpc, b64, sig: ("SENDSIG222", None)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return calls

    def _buy(self, bundle_id, price_usd=0.003, sol_usd=150.0):
        # $GATE is still bonding in tests: the pump.fun executor
        # buys, never Jupiter (curve_complete=False).
        return gate_fuel_mod.buy_gate_bundle(
            self.db, self.cfg, self.user_id, bundle_id, price_usd,
            sol_usd, rpc_client=FakeRpc(), curve_complete=False)


class TestBundlesAndPricing(FuelBase):
    def test_bundles_exactly_three(self):
        bundles = gate_fuel_mod.bundles()
        self.assertEqual(
            [(b["id"], b["coins"], b["trades"], b["cad_price"])
             for b in bundles],
            [("bundle_1m", 1_000_000, 100, 3),
             ("bundle_5m", 5_000_000, 550, 15),
             ("bundle_10m", 10_000_000, 1450, 30)])
        self.assertIsNone(gate_fuel_mod.get_bundle("bundle_999"))
        self.assertIsNone(gate_fuel_mod.get_bundle(None))
        # Copies, not the live table: mutating one must not change
        # the next call.
        bundles[0]["trades"] = 999
        self.assertEqual(gate_fuel_mod.get_bundle("bundle_1m")["trades"],
                         100)

    def test_pricing_rule_exact_coins_resolved(self):
        """Pricing RESOLVED (James, 2026-11-13 ~15:20): exact coin
        amounts. The user always receives exactly the bundle's
        coins; the SOL paid is whatever the live quote says. The
        $3/$15/$30 CAD figures are expected launch prices,
        reference only — no fixed-CAD rule exists anymore."""
        bundle = gate_fuel_mod.get_bundle("bundle_1m")
        # Exact coins: 1M x ($0.003 / $150) x 1.01 buffer = 20.2 SOL.
        self.assertAlmostEqual(
            gate_fuel_mod.price_bundle_sol(bundle, 0.003, 150.0), 20.2)
        # No live price at all refuses, in either input.
        self.assertIsNone(
            gate_fuel_mod.price_bundle_sol(bundle, None, 150.0))
        self.assertIsNone(
            gate_fuel_mod.price_bundle_sol(bundle, 0.003, None))
        # Send rule: exactly the bundle amount sends, a short fill
        # fails closed (None).
        self.assertEqual(
            gate_fuel_mod.bundle_send_base_units(
                bundle, 1_000_000 * 10 ** 6, 6), 1_000_000 * 10 ** 6)
        self.assertIsNone(
            gate_fuel_mod.bundle_send_base_units(
                bundle, 999_999 * 10 ** 6, 6))
        # The old fixed-CAD path is gone: no fixed-price rule can
        # run by accident.
        self.assertFalse(hasattr(gate_fuel_mod, "PRICING_MODE"))
        self.assertFalse(hasattr(gate_fuel_mod,
                                 "bundle_fixed_cad_sol"))


class TestPurchase(FuelBase):
    def test_purchase_1m_grants_100_trades(self):
        calls = self._stub_purchase(1_000_000)
        result = self._buy("bundle_1m")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["trades_credited"], 100)
        self.assertEqual(result["trades_remaining"], 100)
        self.assertEqual(result["send_signature"], "SENDSIG222")
        # GATEkeeper ran the buy, on pump.fun, for the $GATE mint.
        self.assertEqual(calls["buy"]["bot"], "GATEkeeper")
        self.assertEqual(calls["buy"]["mode"], "real")
        self.assertEqual(calls["buy"]["buy_size"], 100)
        self.assertEqual(calls["buy"]["coin"], GATE)
        self.assertEqual(calls["buy"]["side"], "buy")
        # The FULL bundle went to the dev wallet.
        self.assertEqual(calls["inspect_dest"], TEST_DEV_WALLET)
        self.assertEqual(calls["inspect_mint"], GATE)
        row = self.db.execute(
            "SELECT * FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertEqual(row["bundle_id"], "bundle_1m")
        self.assertEqual(row["coins"], 1_000_000)
        self.assertEqual(row["trades"], 100)
        self.assertEqual(row["send_signature"], "SENDSIG222")
        self.assertEqual(row["status"], "completed")

    def test_purchase_5m_and_10m_grant_550_and_1450(self):
        self._stub_purchase(5_000_000)
        result = self._buy("bundle_5m")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["trades_remaining"], 550)
        self._stub_purchase(10_000_000)
        result = self._buy("bundle_10m")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["trades_remaining"], 2000)

    def test_unknown_bundle_refused(self):
        def _no_executor(*args, **kwargs):
            raise AssertionError("no swap for an unknown bundle")

        with mock.patch.object(gate_fuel_mod, "execute_real_trade",
                               _no_executor):
            result = self._buy("bundle_999")
        self.assertFalse(result["ok"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()[0], 0)

    def test_no_live_price_refuses(self):
        def _no_executor(*args, **kwargs):
            raise AssertionError("never buy at a guessed price")

        with mock.patch.object(gate_fuel_mod, "execute_real_trade",
                               _no_executor):
            result = gate_fuel_mod.buy_gate_bundle(
                self.db, self.cfg, self.user_id, "bundle_1m", None,
                150.0, rpc_client=FakeRpc())
        self.assertFalse(result["ok"])
        self.assertIn("price", result["error"].lower())
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()[0], 0)

    def test_short_fill_fails_closed(self):
        # The buy filled 999,999 of the 1M bundle: nothing is sent,
        # nothing is credited, and the bought $GATE stays put.
        self._stub_purchase(1_000_000,
                            inspection_balance=999_999 * 10 ** 6)
        result = self._buy("bundle_1m")
        self.assertFalse(result["ok"])
        self.assertIn("short", result["error"])
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()[0], 0)

    def test_failed_send_retried_once_then_fail_closed(self):
        self._stub_purchase(1_000_000)
        attempts = {"n": 0}

        def flaky_send(rpc, b64, sig):
            attempts["n"] += 1
            return None, "transaction not confirmed"

        with mock.patch.object(gate_fuel_mod, "send_and_confirm",
                               flaky_send):
            result = self._buy("bundle_1m")
        # One retry, then fail closed: no credits, no purchase row.
        self.assertEqual(attempts["n"], 2)
        self.assertFalse(result["ok"])
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()[0], 0)

    def test_transient_send_failure_recovers_on_retry(self):
        self._stub_purchase(1_000_000)
        attempts = {"n": 0}

        def flaky_once(rpc, b64, sig):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return None, "transaction not confirmed"
            return "SENDSIG-RETRY", None

        with mock.patch.object(gate_fuel_mod, "send_and_confirm",
                               flaky_once):
            result = self._buy("bundle_1m")
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["trades_remaining"], 100)
        self.assertEqual(result["send_signature"], "SENDSIG-RETRY")

    def test_key_wiped_after_send(self):
        """The wallet key is wiped in place in a finally block the
        moment signing is done — the test records the buffer
        wipe_secret was called with and checks it is all zeros
        after the purchase returns."""
        self._stub_purchase(1_000_000)
        wiped = {}

        real_wipe = gate_fuel_mod.wipe_secret

        def recording_wipe(buf):
            real_wipe(buf)
            wiped["buf"] = bytes(buf)

        with mock.patch.object(gate_fuel_mod, "wipe_secret",
                               recording_wipe):
            result = self._buy("bundle_1m")
        self.assertTrue(result["ok"], result)
        self.assertIn("buf", wiped)
        self.assertEqual(len(wiped["buf"]), 32)
        self.assertEqual(wiped["buf"], b"\x00" * 32)


class TestLedgerTruth(FuelBase):
    def _buy_100(self):
        self._stub_purchase(1_000_000)
        result = self._buy("bundle_1m")
        self.assertTrue(result["ok"], result)

    def test_consume_counts_once_per_signature(self):
        self._buy_100()
        self.assertTrue(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, "sig-1"))
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)
        # Same signature again: already counted, no second credit.
        self.assertTrue(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, "sig-1"))
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)
        self.assertTrue(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, "sig-2"))
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 98)

    def test_ledger_truth_beats_cached_count(self):
        self._buy_100()
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
        rows = self.db.execute(
            "SELECT kind FROM activity WHERE user_id = ? AND kind = "
            "'gate_fuel_reconcile'", (self.user_id,)).fetchall()
        self.assertTrue(len(rows) >= 2)

    def test_backfill_from_trade_ledger(self):
        """A confirmed real trade in the ledger with no fuel-use row
        is counted by reconciliation — the ledger is the truth."""
        self._buy_100()
        # Stamped after the purchase (second-precision ledger, the
        # backfill counts trades strictly after the first purchase).
        later = (datetime.now(timezone.utc)
                 + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        trade_id = record_trade(self.db, self.user_id, None, GATE,
                                "buy", 100.0, 0.003, 0.30, mode="real",
                                now=later)
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 99)
        use = self.db.execute(
            "SELECT * FROM gate_fuel_uses WHERE user_id = ? AND "
            "trade_id = ?", (self.user_id, trade_id)).fetchone()
        self.assertIsNotNone(use)

    def test_pre_fuel_trades_never_backfilled(self):
        """A real trade from before the first bundle purchase is not
        fuel — reconciliation must not count it."""
        trade_id = record_trade(self.db, self.user_id, None, GATE,
                                "buy", 100.0, 0.003, 0.30, mode="real")
        self._buy_100()
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 100)
        self.assertIsNone(self.db.execute(
            "SELECT * FROM gate_fuel_uses WHERE user_id = ? AND "
            "trade_id = ?", (self.user_id, trade_id)).fetchone())

    def test_consume_without_signature_consumes_nothing(self):
        self._buy_100()
        self.assertFalse(gate_fuel_mod.consume_fuel(
            self.db, self.user_id, None, None))
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 100)


class TestGate(FuelBase):
    def test_trade_blocked_at_zero(self):
        """A real buy with 0 fuel is refused with the no-fuel page:
        logo, countdown at 0, Buy $GATE button, support ID. The
        executor is never called and the bot is turned off."""
        self._patch_logo()
        bot = self._insert_bot(mode="real", silent=1)
        # Real-mode buy: switch off paper first, or the paper/real
        # bot check refuses before the fuel gate is reached.
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "mode": "devnet"})
        self.assertEqual(resp.status_code, 302)
        calls = []

        def _no_executor(*args, **kwargs):
            calls.append(1)
            raise AssertionError("no trade without fuel")

        with mock.patch.object(routes, "execute_real_trade",
                               _no_executor), \
            mock.patch.object(routes, "fetch_prices_for_mints",
                              lambda mints: {}):
            token = csrf(self.client, "/wallet")
            resp = self.client.post(
                f"/coins/{GATE}/buy",
                data={"csrf_token": token, "amount_sol": "0.01",
                      "bot_id": str(bot["id"])},
                follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Out of fuel", html)
        self.assertIn("Buy $GATE", html)
        self.assertIn("data-gate-no-fuel-show", html)
        self.assertIn("gate-no-fuel-banner", html)
        self.assertEqual(calls, [])
        # The gate turned the real bot off.
        row = self.db.execute(
            "SELECT silent FROM bots WHERE id = ?",
            (bot["id"],)).fetchone()
        self.assertEqual(row["silent"], 0)

    def test_bot_turns_off_at_zero_paper_untouched(self):
        real_bot = self._insert_bot(mode="real", silent=1,
                                    name="RealBot")
        paper_bot = self._insert_bot(mode="paper", silent=1,
                                     name="PaperBot")
        self.assertFalse(gate_fuel_mod.enforce_fuel_gate(
            self.db, self.user_id))
        self.assertEqual(self.db.execute(
            "SELECT silent FROM bots WHERE id = ?",
            (real_bot["id"],)).fetchone()["silent"], 0)
        self.assertEqual(self.db.execute(
            "SELECT silent FROM bots WHERE id = ?",
            (paper_bot["id"],)).fetchone()["silent"], 1)

    def test_silent_buy_blocked_at_zero(self):
        """The silent loop never calls the executor without fuel."""
        bot = self._insert_bot(mode="real", silent=1)

        def _no_executor(*args, **kwargs):
            raise AssertionError("silent trade without fuel")

        actions = []
        _buy_coin(self.db, self.cfg, bot, GATE, 0.003, {},
                  lambda url: FakeRpc(url), _no_executor, actions)
        self.assertEqual(actions[0]["action"], "skip")
        self.assertEqual(actions[0]["reason"], "out of fuel")
        self.assertEqual(self.db.execute(
            "SELECT silent FROM bots WHERE id = ?",
            (bot["id"],)).fetchone()["silent"], 0)

    def test_paper_trades_stay_free(self):
        """Paper trading costs no fuel: a paper buy works at 0 fuel
        and consumes nothing."""
        self._patch_logo()
        self.feed.ingest(GATE, 0.003, "curve")
        token = csrf(self.client, "/wallet")
        resp = self.client.post(
            f"/coins/{GATE}/buy",
            data={"csrf_token": token, "amount_tokens": "1000"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        self.assertEqual(gate_fuel_mod.get_fuel(
            self.db, self.user_id)["trades_remaining"], 0)


class TestCountdownAndSupportId(FuelBase):
    def test_countdown_endpoint_counts_down_live(self):
        self._stub_purchase(1_000_000)
        result = self._buy("bundle_1m")
        self.assertTrue(result["ok"], result)
        resp = self.client.get("/api/gate-fuel")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["trades_remaining"], 100)
        self.assertEqual(len(data["support_id"]), 8)
        gate_fuel_mod.consume_fuel(self.db, self.user_id, None,
                                   "sig-live-1")
        resp = self.client.get("/api/gate-fuel")
        self.assertEqual(resp.get_json()["trades_remaining"], 99)

    def test_quote_endpoint(self):
        """The quote comes from the one pricing rule: the bundle's
        trades plus a live SOL estimate, or price_available false
        when no live price exists. The quote is available on
        mainnet too — packages buy through the pump.fun executor
        while $GATE is still bonding."""
        self.feed.ingest(GATE, 0.003, "curve")
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               lambda mints: {}):
            resp = self.client.get(
                "/api/gate-quote?bundle=bundle_1m")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["bundle_id"], "bundle_1m")
        self.assertEqual(data["trades"], 100)
        self.assertEqual(data["bonus"], 0)
        self.assertEqual(data["coins"], 1_000_000)
        self.assertTrue(data["price_available"])
        self.assertIsNotNone(data["sol_estimate"])
        self.assertGreater(data["sol_estimate"], 0)
        resp = self.client.get("/api/gate-quote?bundle=bundle_999")
        self.assertEqual(resp.status_code, 400)
        # The 5M bundle carries its 50-trade bonus.
        resp = self.client.get("/api/gate-quote?bundle=bundle_5m")
        data = resp.get_json()
        self.assertEqual(data["trades"], 550)
        self.assertEqual(data["bonus"], 50)

    def test_support_id_reused_not_reinvented(self):
        """The 8-digit support ID is the signing bot's existing ID:
        same value on every call, unique per user, immutable."""
        first = gate_fuel_mod.get_support_id(self.db, self.user_id)
        self.assertEqual(len(first), 8)
        self.assertTrue(first.isdigit())
        self.assertNotEqual(first[0], "0")
        self.assertEqual(
            gate_fuel_mod.get_support_id(self.db, self.user_id), first)
        # Shown on the wallet page already.
        self._patch_logo()
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn(first, html)
        found = gate_fuel_mod.lookup_user_by_support_id(
            self.db, first)
        self.assertEqual(found["user_id"], self.user_id)
        self.assertEqual(found["username"], "alice")
        self.assertEqual(found["fuel"]["trades_remaining"], 0)
        self.assertIsNone(
            gate_fuel_mod.lookup_user_by_support_id(self.db, "123"))
        self.assertIsNone(
            gate_fuel_mod.lookup_user_by_support_id(self.db, "99999999"))

    def test_admin_support_lookup(self):
        support_id = gate_fuel_mod.get_support_id(
            self.db, self.user_id)
        resp = self.client.get(f"/admin/gate-support/{support_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["username"], "alice")
        self.assertEqual(data["fuel"]["trades_remaining"], 0)
        resp = self.client.get("/admin/gate-support/99999999")
        self.assertEqual(resp.status_code, 404)

    def test_home_wallet_bots_show_button_and_countdown(self):
        """Buy $GATE button + live countdown on all three pages
        (real mode, funded — paper mode shows "free ~ ∞")."""
        self._patch_logo()
        grant_fuel(self.db, self.user_id, 100)
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"),
            "mode": "devnet"})
        self.assertEqual(resp.status_code, 302)
        for path in ("/wallet", "/wallet/details", "/bots"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("Buy $GATE", html, path)
            self.assertIn("data-gate-fuel-count", html, path)
            self.assertIn("trades left", html, path)
            self.assertIn("gate-buy-popup", html, path)

    def test_paper_pages_show_free_not_a_count(self):
        """Paper mode: the chip and the fuel rows say "free ~ ∞"
        — paper trades are free, no count."""
        self._patch_logo()
        for path in ("/wallet", "/bots"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("free ~ ∞", html, path)
            self.assertIn("Buy $GATE", html, path)

    def test_buy_popup_links_pumpfun(self):
        """The Buy popup links straight to the coin on pump.fun
        and lists the three packages with their trade counts."""
        self._patch_logo()
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn(
            f"https://pump.fun/coin/{GATE}", html)
        self.assertIn("gate-buy-popup", html)
        self.assertIn("1M $GATE", html)
        self.assertIn("100 trades", html)
        self.assertIn("550", html)
        self.assertIn("1450", html)
        # The live price refreshes while the popup is open.
        self.assertIn("setInterval", html)
        self.assertIn("/api/gate-quote", html)


class TestLogo(FuelBase):
    def test_logo_from_pumpfun_metadata_then_fox_fallback(self):
        gate_fuel_mod._logo_cache.clear()

        def fake_get(url):
            self.assertIn(GATE, url)
            return [{"info": {"imageUrl":
                              "https://example.com/gate.png"}}]

        url = gate_fuel_mod.get_gate_logo_url(GATE, http_get=fake_get)
        self.assertEqual(url, "https://example.com/gate.png")
        gate_fuel_mod._logo_cache.clear()

        def failing_get(url):
            raise ConnectionError("down")

        url = gate_fuel_mod.get_gate_logo_url(GATE,
                                              http_get=failing_get)
        self.assertEqual(url, "/static/fox-gold-480.jpg")


if __name__ == "__main__":
    unittest.main()
