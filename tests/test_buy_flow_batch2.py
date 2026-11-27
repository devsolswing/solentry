"""Batch 2 — failure logging (buy-flow rebuild, staged 2026-11-26).

One logical change: a failed trade now says what went wrong, how
to fix it, and the transaction ID — or says plainly there is no
transaction ID when the trade never reached the chain (James,
2026-11-26: the log must never invent a transaction ID).

Covered: real buy failures (result page + activity log), real
sell failures (result page + activity log), paper buy failures
(activity log). The executor's _skip now logs every refusal at
warning and carries a sent signature through, so a trade that was
sent but rejected or unconfirmed keeps its transaction ID.

Also a regression test proving the quick-trade `$` buy (field
`usd=`) works in real mode: the recon found it broken (the field
was ignored, "Amount must be more than 0."), batch 1's shared
amount parser accepts the `usd` field, so it buys now — this test
pins that.

No other buy-flow work lives here: no SL/TP, no % buttons, no
currency picker, no wallet live-or-no-data, no paper stale
refusal.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, grant_fuel, make_client, signup
from test_buy_flow_batch1 import GATE, _start_route_mocks
from tradingbot.executor import _skip, fix_for_skip_reason
from tradingbot.routes import _trade_failure_lines


class TestFixGuidance(unittest.TestCase):
    """fix_for_skip_reason: every known refusal names its fix;
    anything unknown gets the generic line, never silence."""

    def test_reserve_refusal_names_the_reserve(self):
        fix = fix_for_skip_reason("insufficient SOL left for fees")
        self.assertIn("0.0012", fix)
        self.assertIn("Buy less", fix)

    def test_bot_size_refusal_names_the_fix(self):
        fix = fix_for_skip_reason("amount exceeds bot buy size")
        self.assertIn("smaller amount", fix)

    def test_stale_price_refusal_says_wait(self):
        fix = fix_for_skip_reason("decision price is stale")
        self.assertIn("Wait", fix)

    def test_unconfirmed_trade_points_at_the_transaction(self):
        fix = fix_for_skip_reason("transaction not confirmed")
        self.assertIn("transaction ID", fix)

    def test_unknown_reason_gets_the_generic_line(self):
        fix = fix_for_skip_reason("something nobody has seen before")
        self.assertIn("Try again", fix)
        self.assertIn("support", fix)

    def test_matching_ignores_case(self):
        fix = fix_for_skip_reason("INSUFFICIENT SOL LEFT FOR FEES")
        self.assertIn("0.0012", fix)


class TestSkipLogging(unittest.TestCase):
    """_skip: a refused trade is never silent — it logs at
    warning, and a sent signature rides along so the caller can
    surface the transaction ID."""

    def test_skip_logs_the_reason_at_warning(self):
        with self.assertLogs("tradingbot.executor",
                             level="WARNING") as logs:
            _skip("insufficient SOL left for fees")
        self.assertTrue(any("insufficient SOL left for fees" in line
                            for line in logs.output))

    def test_skip_carries_a_sent_signature(self):
        result = _skip("transaction not confirmed",
                       signature="sig-sent-123")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["signature"], "sig-sent-123")
        with self.assertLogs("tradingbot.executor",
                             level="WARNING") as logs:
            _skip("transaction not confirmed",
                  signature="sig-sent-123")
        self.assertTrue(any("sig-sent-123" in line
                            for line in logs.output))

    def test_skip_without_a_signature_has_no_signature_key(self):
        result = _skip("decision price is stale")
        self.assertNotIn("signature", result)


class TestTradeFailureLines(unittest.TestCase):
    """_trade_failure_lines: reason + fix + transaction ID, or a
    plain statement that there is none."""

    def test_lines_with_a_signature_name_it(self):
        lines = _trade_failure_lines(
            {"reason": "insufficient SOL left for fees",
             "signature": "sig-abc123"}, "buy")
        self.assertEqual(lines[0],
                         "No buy was placed — "
                         "insufficient SOL left for fees.")
        self.assertTrue(lines[1].startswith("Fix: "))
        self.assertIn("0.0012", lines[1])
        self.assertEqual(lines[2], "Transaction: sig-abc123")

    def test_lines_without_a_signature_say_so_plainly(self):
        lines = _trade_failure_lines(
            {"reason": "decision price is stale"}, "buy")
        self.assertEqual(len(lines), 3)
        self.assertIn("No transaction reached the chain", lines[2])
        self.assertIn("no transaction ID", lines[2])

    def test_sell_side_wording(self):
        lines = _trade_failure_lines(
            {"reason": "transaction not confirmed",
             "signature": "sig-sell-9"}, "sell")
        self.assertTrue(lines[0].startswith("No sell was placed"))
        self.assertEqual(lines[2], "Transaction: sig-sell-9")

    def test_missing_reason_falls_back_to_a_generic_line(self):
        lines = _trade_failure_lines({}, "buy")
        self.assertIn("real execution failed", lines[0])


class _Batch2RouteBase(unittest.TestCase):
    """Route tests: the batch-1 harness (live feeds kept local,
    executors mocked) plus a real-mode user with fuel."""

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

    def _activity(self, kind):
        return self.db.execute(
            "SELECT kind, detail FROM activity WHERE user_id = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1",
            (self.user_id, kind)).fetchone()


class TestBatch2RealBuyFailure(_Batch2RouteBase):
    """A failed real buy: the page and the activity log both state
    the reason, the fix, and the transaction ID — or that there is
    none."""

    def test_failure_with_signature_shows_fix_and_tx(self):
        routes.execute_real_trade.return_value = {
            "status": "skipped",
            "reason": "insufficient SOL left for fees",
            "signature": "sig-abc123"}
        resp = self._buy(amount="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("insufficient SOL left for fees", html)
        self.assertIn("Fix: ", html)
        self.assertIn("0.0012", html)
        self.assertIn("Transaction: sig-abc123", html)
        row = self._activity("real_buy_failed")
        self.assertIsNotNone(row, "no real_buy_failed log row")
        self.assertIn("insufficient SOL left for fees", row["detail"])
        self.assertIn("Fix: ", row["detail"])
        self.assertIn("sig-abc123", row["detail"])

    def test_failure_without_signature_says_no_tx(self):
        routes.execute_real_trade.return_value = {
            "status": "skipped",
            "reason": "decision price is stale"}
        resp = self._buy(amount="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("No transaction reached the chain", html)
        self.assertIn("no transaction ID", html)
        row = self._activity("real_buy_failed")
        self.assertIsNotNone(row, "no real_buy_failed log row")
        self.assertIn("No transaction reached the chain",
                      row["detail"])

    def test_quick_trade_usd_field_buys_in_real_mode(self):
        # Regression: the recon found real quick-trade $ buys
        # broken — quick_trade.js posts the buy as `usd=`, and the
        # old coin_buy ignored that field ("Amount must be more
        # than 0."). Batch 1's shared amount parser accepts `usd`,
        # so the same post buys now.
        resp = self._buy(usd="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertNotIn("Amount must be more than 0.", html)
        routes.execute_real_trade.assert_called_once()


class TestBatch2RealSellFailure(_Batch2RouteBase):
    """A failed real sell gets the same treatment as a failed buy:
    reason + fix + transaction ID on the page and in the log."""

    def _mocked_sell(self, mint, form, executor_result):
        def fake_chain_balances(rpc, pubkey):
            return ([{"mint": mint, "amount": "1000000000",
                      "ui_amount": 1000000000.0}], True)

        with mock.patch.object(routes, "is_curve_complete",
                               return_value=False), \
                mock.patch.object(routes, "fetch_chain_token_balances",
                                  side_effect=fake_chain_balances), \
                mock.patch.object(routes, "execute_jupiter_swap"), \
                mock.patch.object(routes, "execute_real_trade",
                                  return_value=executor_result):
            return self.client.post(f"/coins/{mint}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{mint}"),
                **form})

    def test_sell_failure_with_signature_shows_fix_and_tx(self):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 10.0, 0.01))
        self.db.commit()
        resp = self._mocked_sell(GATE, {"amount": "4"}, {
            "status": "skipped",
            "reason": "transaction not confirmed",
            "signature": "sig-sell-9"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was sold", html)
        self.assertIn("transaction not confirmed", html)
        self.assertIn("Fix: ", html)
        self.assertIn("Transaction: sig-sell-9", html)
        row = self._activity("real_sell_failed")
        self.assertIsNotNone(row, "no real_sell_failed log row")
        self.assertIn("transaction not confirmed", row["detail"])
        self.assertIn("sig-sell-9", row["detail"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM activity WHERE user_id = ? "
            "AND kind = 'real_sell'",
            (self.user_id,)).fetchone()["c"], 0,
            "a failed sell must not log a successful sell")

    def test_sell_failure_without_signature_says_no_tx(self):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, 10.0, 0.01))
        self.db.commit()
        resp = self._mocked_sell(GATE, {"amount": "4"}, {
            "status": "skipped",
            "reason": "bonding curve unavailable"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was sold", html)
        self.assertIn("No transaction reached the chain", html)
        row = self._activity("real_sell_failed")
        self.assertIsNotNone(row, "no real_sell_failed log row")
        self.assertIn("bonding curve", row["detail"])


class TestBatch2PaperBuyFailure(unittest.TestCase):
    """A failed paper buy is logged too — with the plain statement
    that paper trades never reach the chain, so there is no
    transaction ID to look up."""

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
            "mode": "paper"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_paper_buy_over_cash_logs_the_failure(self):
        # $100 paper cash (test harness default); a $1000 paper
        # buy cannot fill, so the ledger refuses it.
        resp = self.client.post(f"/coins/{GATE}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GATE}"),
            "bot_id": "", "usd": "1000"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Not enough paper cash", html)
        row = self.db.execute(
            "SELECT kind, detail FROM activity WHERE user_id = ? "
            "AND kind = 'paper_buy_failed' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "no paper_buy_failed log row")
        self.assertIn("Not enough paper cash", row["detail"])
        self.assertIn("No transaction", row["detail"])
        self.assertIn("never reach the chain", row["detail"])


if __name__ == "__main__":
    unittest.main()
