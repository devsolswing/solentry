"""Batch 6 — Trenches quick buy (buy-flow rebuild).

Exactly one logical change, per James's order (2026-11-27
~07:32-07:41, priority-fee cap settled ~08:45): on each Trenches
coin badge, one-tap Buy 1 (0.02 SOL worth) and Buy 2 (0.05 SOL
worth) buttons — tapping buys RIGHT AWAY, no popup, no
confirmation — plus a Sell All Now button that exits as fast as
possible and shows ONLY when the coin is held in the active mode
right now (live holdings). After the buy, a receipt popup shows
the full details (amount, coins, price, signature + Solscan link,
signing bot, venue, SOL spent, priority-fee price/cap, new
balance). No receipt popup was ordered for sells — the JSON
answer carries the same facts so the page confirms inline and
refreshes the badge.

- Both amounts are changeable in the buy popup window, as a
  setting: the two settable amount boxes are named "Buy 1" and
  "Buy 2" (GET/POST /api/quick-buy-amounts, per user, server-side
  — never localStorage). Each must be 0.001–10 SOL; a bad value
  refuses and the old amounts stay. The badge posts only its slot
  (quick_slot 1 or 2); the server resolves the SOL amount from the
  saved setting, so a stale label can never buy the wrong amount.
- Priority fee if needed to enter or exit fast — CAPPED at
  0.001 SOL max per trade (James). The fee comes on top of the
  buy: the reserve for a quick buy is the batch-1 tier PLUS the
  0.001 cap, so the wallet keeps the tier reserve intact after
  the fee is paid; the same cap is passed to the executor, whose
  fee instruction's unit price is capped so price x 200,000 units
  never exceeds it.
- Both modes (paper + real), spending that mode's funds. Manual
  User buy path (no bot). Batch 1's tiered reserve backstop +
  stale-price refusal still apply server-side; SL/TP (batch 3)
  and % buttons (batch 4) are untouched.
- "In rotation" is read as the badges rotating with the live
  Trenches feed (the 5s poll swaps rows in and out) — the buttons
  ride on the badge, so a badge that rotates in carries its
  buttons with it; no separate rotation control was ordered.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, csrf,
                     grant_fuel, make_client, signup)
from test_buy_flow_batch5 import _start_route_mocks, GATE, GATE_PRICE
from test_executor import (CURVE_PRICE_USD, TEST_SOL_PRICE_USD,
                           ExecutorBase, FakePumpRpc, make_curve,
                           parse_signed_tx)
from tradingbot import pumpfun
from tradingbot.config import Config
from tradingbot.executor import execute_real_trade
from tradingbot.quick_buy import (get_quick_buy_amounts,
                                  set_quick_buy_amounts)
from tradingbot.solana import COMPUTE_BUDGET_PROGRAM

FEE_CAP_SOL = 0.001


def _test_config():
    return Config(env={
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": tempfile.gettempdir() + "/m.db",
        "PAPER_START_CASH": "10000"})


class TestQuickBuyConfig(unittest.TestCase):
    """Defaults + env overrides + the fee-aware reserve math."""

    def test_defaults(self):
        cfg = _test_config()
        self.assertEqual(cfg.quick_buy1_sol, 0.02)
        self.assertEqual(cfg.quick_buy2_sol, 0.05)
        self.assertEqual(cfg.quick_trade_priority_fee_max_sol, 0.001)

    def test_reserve_is_tier_plus_fee_cap(self):
        cfg = _test_config()
        # 0.02 SOL at $10/SOL = $0.20 — small tier 0.0012 + cap.
        self.assertAlmostEqual(cfg.quick_buy_reserve_sol(0.20), 0.0022)
        # Unknown USD size fails safe to the large tier + cap.
        self.assertAlmostEqual(cfg.quick_buy_reserve_sol(None), 0.013)
        # $400 — large tier 0.012 + cap.
        self.assertAlmostEqual(cfg.quick_buy_reserve_sol(400.0), 0.013)
        # Exactly $5 is the large tier.
        self.assertAlmostEqual(cfg.quick_buy_reserve_sol(5.0), 0.013)

    def test_env_overrides(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": tempfile.gettempdir() + "/m.db",
            "QUICK_BUY1_SOL": "0.03", "QUICK_BUY2_SOL": "0.07",
            "QUICK_TRADE_PRIORITY_FEE_MAX_SOL": "0.0005"})
        self.assertEqual(cfg.quick_buy1_sol, 0.03)
        self.assertEqual(cfg.quick_buy2_sol, 0.07)
        self.assertEqual(cfg.quick_trade_priority_fee_max_sol, 0.0005)

    def test_bad_env_values_fall_back_to_defaults(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": tempfile.gettempdir() + "/m.db",
            "QUICK_BUY1_SOL": "abc", "QUICK_BUY2_SOL": "-1",
            "QUICK_TRADE_PRIORITY_FEE_MAX_SOL": "0"})
        self.assertEqual(cfg.quick_buy1_sol, 0.02)
        self.assertEqual(cfg.quick_buy2_sol, 0.05)
        self.assertEqual(cfg.quick_trade_priority_fee_max_sol, 0.001)


class _Batch6RouteBase(unittest.TestCase):
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

    def _post(self, path, **data):
        form = {"csrf_token": csrf(self.client, f"/coins/{GATE}")}
        form.update(data)
        return self.client.post(path, data=form)

    def _post_json(self, path, **data):
        form = {"csrf_token": csrf(self.client, f"/coins/{GATE}"),
                "format": "json"}
        form.update(data)
        resp = self.client.post(path, data=form)
        self.assertEqual(resp.status_code, 200, path)
        return json.loads(resp.get_data(as_text=True))

    def _amounts(self):
        return get_quick_buy_amounts(self.db, self.user_id)


class TestQuickBuyAmountsRoute(_Batch6RouteBase):
    """The Buy 1 / Buy 2 setting: read, save, refuse bad values."""

    def test_get_returns_defaults(self):
        payload = self._post_json("/api/quick-buy-amounts")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["buy1_sol"], 0.02)
        self.assertEqual(payload["buy2_sol"], 0.05)

    def test_save_valid_amounts(self):
        payload = self._post_json(
            "/api/quick-buy-amounts", buy1_sol="0.03",
            buy2_sol="0.07")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["buy1_sol"], 0.03)
        self.assertEqual(payload["buy2_sol"], 0.07)
        self.assertEqual(self._amounts()["buy1_sol"], 0.03)
        self.assertEqual(self._amounts()["buy2_sol"], 0.07)

    def test_save_rejects_bad_values_and_keeps_old(self):
        for bad, expected in (
                ("0", "Buy 1 must be 0.001–10 SOL."),
                ("-1", "Buy 1 must be 0.001–10 SOL."),
                ("99", "Buy 1 must be 0.001–10 SOL."),
                ("abc", "Buy 1 must be a number."),
                ("", "Buy 1 must be a number.")):
            resp = self.client.post("/api/quick-buy-amounts", data={
                "csrf_token": csrf(self.client, f"/coins/{GATE}"),
                "buy1_sol": bad, "buy2_sol": "0.05"})
            self.assertEqual(resp.status_code, 400, bad)
            payload = json.loads(resp.get_data(as_text=True))
            self.assertFalse(payload["ok"], bad)
            self.assertEqual(payload["error"], expected, bad)
        self.assertEqual(self._amounts()["buy1_sol"], 0.02)
        self.assertEqual(self._amounts()["buy2_sol"], 0.05)
        resp = self.client.post("/api/quick-buy-amounts", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "buy1_sol": "0.02", "buy2_sol": "0.0005"})
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(resp.get_data(as_text=True))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "Buy 2 must be 0.001–10 SOL.")
        self.assertEqual(self._amounts()["buy2_sol"], 0.05)

    def test_save_without_csrf_is_refused(self):
        resp = self.client.post("/api/quick-buy-amounts", data={
            "buy1_sol": "0.03", "buy2_sol": "0.07"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._amounts()["buy1_sol"], 0.02)

    def test_db_helpers_round_trip(self):
        set_quick_buy_amounts(self.db, self.user_id, 0.04, 0.09)
        amounts = self._amounts()
        self.assertEqual(amounts["buy1_sol"], 0.04)
        self.assertEqual(amounts["buy2_sol"], 0.09)


class TestQuickBuyPaper(_Batch6RouteBase):
    """Paper quick buy: slot resolves the SOL amount server-side;
    the receipt carries the facts; no bot is attached."""

    MODE = "paper"

    def test_slot1_buys_002_sol_and_receipt_says_paper(self):
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["heading"], "Bought")
        receipt = payload["receipt"]
        self.assertEqual(receipt["side"], "buy")
        self.assertEqual(receipt["mode"], "paper")
        self.assertEqual(receipt["amount_sol"], 0.02)
        self.assertIsNone(receipt["signature"])
        self.assertIsNotNone(receipt["new_paper_cash"])
        self.assertGreater(receipt["coin_amount"], 0)
        self.assertIn("User buy — no bot watching this coin.",
                      payload["lines"])

    def test_slot2_buys_005_sol(self):
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="2")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["receipt"]["amount_sol"], 0.05)

    def test_saved_amounts_are_what_the_slot_buys(self):
        set_quick_buy_amounts(self.db, self.user_id, 0.03, 0.07)
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertEqual(payload["receipt"]["amount_sol"], 0.03)
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="2")
        self.assertEqual(payload["receipt"]["amount_sol"], 0.07)

    def test_a_posted_amount_never_overrides_the_slot(self):
        # The badge posts no amount at all; a forged amount_sol=9
        # must not buy 9 SOL — the slot's 0.02 wins.
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1",
            amount_sol="9")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["receipt"]["amount_sol"], 0.02)

    def test_unknown_slot_is_refused(self):
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="3")
        self.assertFalse(payload["ok"])
        self.assertIn("Buy 1 or Buy 2", payload["lines"][0])
        self.assertNotIn("receipt", payload)
        routes.execute_real_trade.assert_not_called()

    def test_plain_form_still_gets_a_result_page(self):
        resp = self._post(f"/coins/{GATE}/buy",
                          quick_buy="1", quick_slot="1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))


class TestQuickBuyReal(_Batch6RouteBase):
    """Real quick buy: the executor gets the 0.001 SOL fee cap and
    the tier-plus-cap reserve; the receipt carries the cap."""

    MODE = "devnet"

    def test_slot1_passes_fee_cap_and_reserve(self):
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertTrue(payload["ok"], payload)
        routes.execute_real_trade.assert_called_once()
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertEqual(kwargs["priority_fee_max_sol"], 0.001)
        # 0.02 SOL at $10/SOL = $0.20: small tier 0.0012 + cap.
        self.assertAlmostEqual(kwargs["user_buy_reserve_sol"], 0.0022)
        receipt = payload["receipt"]
        self.assertEqual(receipt["mode"], "real")
        self.assertEqual(receipt["amount_sol"], 0.02)
        self.assertEqual(receipt["signature"], "sig-batch5")
        self.assertEqual(receipt["priority_fee_max_sol"], 0.001)

    def test_slot2_reserve_is_still_small_tier(self):
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="2")
        self.assertTrue(payload["ok"], payload)
        _args, kwargs = routes.execute_real_trade.call_args
        # 0.05 SOL at $10/SOL = $0.50: still the small tier.
        self.assertAlmostEqual(kwargs["user_buy_reserve_sol"], 0.0022)

    def test_ordinary_buy_gets_no_fee_cap(self):
        # A non-quick buy must not inherit the quick-buy budget:
        # no cap passed, plain batch-1 reserve.
        payload = self._post_json(f"/coins/{GATE}/buy", amount="4")
        self.assertTrue(payload["ok"], payload)
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertIsNone(kwargs["priority_fee_max_sol"])
        self.assertIsNone(payload["receipt"]["priority_fee_max_sol"])

    def test_executor_refusal_is_plain_json(self):
        routes.execute_real_trade.return_value = {
            "status": "skipped", "reason": "insufficient SOL left "
            "for fees"}
        payload = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["heading"], "Nothing was bought")
        self.assertNotIn("receipt", payload)


class TestQuickSellReal(_Batch6RouteBase):
    """Sell All Now (real): the whole real position, fee-capped;
    a plain sell gets no cap."""

    MODE = "devnet"

    def setUp(self):
        super().setUp()
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 1000000.0, GATE_PRICE))
        self.db.commit()

    def test_quick_sell_passes_fee_cap_and_sells_all(self):
        payload = self._post_json(
            f"/coins/{GATE}/sell", sell_all="1", quick_sell="1")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["heading"], "Sold")
        routes.execute_real_trade.assert_called_once()
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertEqual(kwargs["priority_fee_max_sol"], 0.001)
        self.assertEqual(_args[4], 1000000.0)
        receipt = payload["receipt"]
        self.assertEqual(receipt["side"], "sell")
        self.assertEqual(receipt["mode"], "real")
        self.assertEqual(receipt["coin_amount"], 1000000.0)
        self.assertEqual(receipt["priority_fee_max_sol"], 0.001)
        row = self.db.execute(
            "SELECT amount FROM real_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)).fetchone()
        self.assertTrue(row is None or float(row["amount"]) == 0.0)

    def test_plain_sell_gets_no_fee_cap(self):
        payload = self._post_json(
            f"/coins/{GATE}/sell", sell_all="1")
        self.assertTrue(payload["ok"], payload)
        _args, kwargs = routes.execute_real_trade.call_args
        self.assertIsNone(kwargs["priority_fee_max_sol"])

    def test_quick_sell_with_nothing_held_is_refused(self):
        self.db.execute(
            "DELETE FROM real_holdings WHERE user_id = ? AND "
            "coin_id = ?", (self.user_id, GATE))
        self.db.commit()
        payload = self._post_json(
            f"/coins/{GATE}/sell", sell_all="1", quick_sell="1")
        self.assertFalse(payload["ok"])
        routes.execute_real_trade.assert_not_called()


class TestQuickSellPaper(_Batch6RouteBase):
    """Sell All Now in paper: the whole paper position, no chain."""

    MODE = "paper"

    def test_quick_sell_sells_the_paper_position(self):
        buy = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertTrue(buy["ok"], buy)
        payload = self._post_json(
            f"/coins/{GATE}/sell", sell_all="1", quick_sell="1")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["heading"], "Sold")
        row = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, GATE)).fetchone()
        self.assertTrue(row is None or float(row["amount"]) == 0.0)


class TestTrenchesBadgeRender(_Batch6RouteBase):
    """The badge renders Buy 1 / Buy 2 on every row and Sell All
    Now only when the coin is held; the popup carries the Buy 1 /
    Buy 2 setting boxes and the receipt overlay."""

    MODE = "paper"

    def setUp(self):
        super().setUp()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan", return_value={
                "candidates": [{
                    "mint": GATE, "symbol": "GATE", "name": "Gate",
                    "price_usd": GATE_PRICE, "change_1h_pct": 12.5,
                    "change_5m_pct": 3.0}],
                "prices": {}, "source": "dexscreener",
                "as_of": "test", "error": None})
        self.scan_patch.start()
        self.addCleanup(self.scan_patch.stop)
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def test_badge_has_buy_buttons_and_no_sell_all_when_not_held(self):
        html = self._html("/trenches")
        self.assertIn("Buy 1 ·", html)
        self.assertIn("Buy 2 ·", html)
        self.assertIn('data-quick-buy', html)
        self.assertIn('data-quick-slot="1"', html)
        self.assertIn('data-quick-slot="2"', html)
        # Scope to the badges: the page's own receipt-handler script
        # mentions "Sell All Now" in a comment, so a whole-page
        # assertNotIn would always fail — the badge row is what
        # must not carry the button.
        badge_html = html[html.index('data-trenches-list'):
                          html.index('id="trenches-receipt-overlay"')]
        self.assertNotIn("Sell All Now", badge_html)
        self.assertIn('id="trenches-receipt-overlay"', html)

    def test_badge_gains_sell_all_when_held(self):
        buy = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertTrue(buy["ok"], buy)
        html = self._html("/trenches")
        self.assertIn("Sell All Now", html)
        self.assertIn('data-quick-sell-all', html)

    def test_dashboard_trenches_rows_have_the_same_buttons(self):
        html = self._html("/dashboard")
        self.assertIn("Buy 1 ·", html)
        self.assertIn('id="trenches-receipt-overlay"', html)

    def test_popup_carries_the_buy_amount_settings(self):
        html = self._html(f"/coins/{GATE}")
        self.assertIn("Buy 1 (SOL)", html)
        self.assertIn("Buy 2 (SOL)", html)
        self.assertIn('name="buy1_sol"', html)
        self.assertIn('name="buy2_sol"', html)
        self.assertIn("Save quick-buy amounts", html)
        self.assertIn("Priority fee capped at 0.001 SOL", html)

    def test_api_trenches_rows_carry_held_and_amounts(self):
        buy = self._post_json(
            f"/coins/{GATE}/buy", quick_buy="1", quick_slot="1")
        self.assertTrue(buy["ok"], buy)
        resp = self.client.get("/api/trenches")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        row = data["rows"][0]
        self.assertTrue(row["held"])
        self.assertIn("Sell All Now", row["html"])
        self.assertIn(">0.02</span> SOL", row["html"])


class TestQuickTradeFeeCapExecutor(ExecutorBase):
    """The 0.001 SOL cap at the executor: a spiking fee market is
    capped to the budget (never above it), and the fee-on-top
    reserve refuses a wallet that holds only amount + tier."""

    def _buy(self, rpc, **kwargs):
        return execute_real_trade(
            None, self.user_id, self.mint, "buy", 0.02,
            CURVE_PRICE_USD, self.db, self.cfg,
            decision_price_age_seconds=0.0,
            sol_price_usd=TEST_SOL_PRICE_USD,
            rpc_client=rpc, **kwargs)

    def test_fee_price_is_capped_at_the_001_budget(self):
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95, 20_000_000)
        # Fee market spiking: median 10,000 µL/CU would be 0.002
        # SOL at the 200,000-unit budget — over the cap.
        rpc.fee_samples = [10000, 10000, 10000]
        result = self._buy(
            rpc, user_buy_reserve_sol=0.0022,
            priority_fee_max_sol=FEE_CAP_SOL)
        self.assertEqual(result["status"], "executed")
        # 0.001 SOL -> 5,000 µL/CU at 200,000 units.
        self.assertEqual(result["priority_fee_micro_lamports"], 5000)
        parsed = parse_signed_tx(rpc.sent[0])
        fee_ix = [i for i in parsed["instructions"]
                  if i["program_id"] == COMPUTE_BUDGET_PROGRAM]
        self.assertEqual(len(fee_ix), 1)
        self.assertEqual(
            int.from_bytes(fee_ix[0]["data"][1:9], "little"), 5000)

    def test_without_the_cap_the_same_market_pays_more(self):
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95, 20_000_000)
        rpc.fee_samples = [10000, 10000, 10000]
        result = self._buy(rpc, user_buy_reserve_sol=0.0022)
        self.assertEqual(result["status"], "executed")
        expected = min(10000, self.cfg.priority_fee_cap_micro_lamports)
        self.assertEqual(result["priority_fee_micro_lamports"], expected)
        self.assertGreater(expected, 5000)

    def test_no_fee_samples_means_no_fee_instruction(self):
        curve = pumpfun.parse_bonding_curve(make_curve())
        rpc = FakePumpRpc(self.pubkey, self.mint, curve=make_curve())
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95, 20_000_000)
        rpc.fee_samples = []
        result = self._buy(
            rpc, user_buy_reserve_sol=0.0022,
            priority_fee_max_sol=FEE_CAP_SOL)
        self.assertEqual(result["status"], "executed")
        self.assertNotIn("priority_fee_micro_lamports", result)
        parsed = parse_signed_tx(rpc.sent[0])
        programs = {i["program_id"] for i in parsed["instructions"]}
        self.assertNotIn(COMPUTE_BUDGET_PROGRAM, programs)

    def test_reserve_must_hold_after_the_fee_cap(self):
        curve = pumpfun.parse_bonding_curve(make_curve())
        # Wallet holds the buy (0.02) + the small tier (0.0012) —
        # but NOT the 0.001 fee cap on top: 0.0212 SOL. The
        # tier-plus-cap reserve is 0.0022, so it must refuse
        # instead of buying and then being unable to pay the fee.
        # (ata_balance=1: the token account already exists, so no
        # ATA rent is folded into the reserve either way.)
        rpc = FakePumpRpc(
            self.pubkey, self.mint, curve=make_curve(),
            balance_lamports=21_200_000, ata_balance=1)
        rpc.fill_tokens = pumpfun.buy_quote(curve, 95, 20_000_000)
        rpc.fee_samples = [10000, 10000, 10000]
        result = self._buy(
            rpc, user_buy_reserve_sol=0.0022,
            priority_fee_max_sol=FEE_CAP_SOL)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("fees", result["reason"])
        self.assertEqual(rpc.sent, [])
        # The same wallet WITH the cap on top executes.
        rpc2 = FakePumpRpc(
            self.pubkey, self.mint, curve=make_curve(),
            balance_lamports=22_200_000, ata_balance=1)
        rpc2.fill_tokens = pumpfun.buy_quote(curve, 95, 20_000_000)
        rpc2.fee_samples = [10000, 10000, 10000]
        result2 = self._buy(
            rpc2, user_buy_reserve_sol=0.0022,
            priority_fee_max_sol=FEE_CAP_SOL)
        self.assertEqual(result2["status"], "executed")


if __name__ == "__main__":
    unittest.main()
