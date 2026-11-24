"""Promo / one-time codes tests (James, 2026-11-24).

A coupon code box in Settings grants fuel credits. Two scopes:
- global_once: BetaGate1994 grants 1000 trades, usable ONCE IN
  TOTAL across all users — the first redeemer gets the trades,
  then the code is dead.
- per_account_once: "Still on the gate" (the signup code, the
  motto phrase itself) grants 50 trades, usable ONCE PER ACCOUNT.
One-time codes only: that is the anti-bypass rule, so a single
user cannot mint fuel by making multiple accounts. The grant
lands in the fuel ledger as a completed purchase marked source
'promo' — the chip reads it, reconcile never flags it as
overdraw, and it is never disguised as a paid package.
"""

import tempfile
import threading
import unittest

import tradingbot.gate_fuel as gate_fuel_mod
from helpers import csrf, make_client, signup
from tradingbot.db import connect


class PromoBase(unittest.TestCase):
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

    def _redeem(self, code, client=None):
        client = client or self.client
        token = csrf(client, "/settings")
        return client.post("/settings/promo-code", data={
            "csrf_token": token, "code": code}, follow_redirects=True)

    def _fuel(self):
        return gate_fuel_mod.get_fuel(self.db, self.user_id)[
            "trades_remaining"]


class TestPromoSeed(PromoBase):
    def test_beta_gate_seeded_once_in_total(self):
        row = self.db.execute(
            "SELECT code, trades, scope, max_uses, uses FROM "
            "promo_codes WHERE code = 'BetaGate1994'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["trades"], 1000)
        self.assertEqual(row["scope"], "global_once")
        self.assertEqual(row["max_uses"], 1)
        self.assertEqual(row["uses"], 0)

    def test_signup_code_seeded_once_per_account(self):
        row = self.db.execute(
            "SELECT code, trades, scope, max_uses, uses FROM "
            "promo_codes WHERE code = 'Still on the gate'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["trades"], 50)
        self.assertEqual(row["scope"], "per_account_once")
        self.assertIsNone(row["max_uses"])
        self.assertEqual(row["uses"], 0)

    def test_settings_page_renders_coupon_box(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Promo code", html)
        self.assertIn('action="/settings/promo-code"', html)
        self.assertIn('name="code"', html)
        self.assertIn("Redeem code", html)


class TestPromoRedeem(PromoBase):
    def test_redeem_happy_path_credits_1000(self):
        resp = self._redeem("BetaGate1994")
        html = resp.get_data(as_text=True)
        self.assertIn("Promo code accepted", html)
        self.assertIn("1000 trades added", html)
        self.assertIn("1000 trades left", html)
        self.assertEqual(self._fuel(), 1000)
        row = self.db.execute(
            "SELECT uses FROM promo_codes WHERE code = 'BetaGate1994'"
        ).fetchone()
        self.assertEqual(row["uses"], 1)
        red = self.db.execute(
            "SELECT code, user_id, trades FROM promo_redemptions "
            "WHERE code = 'BetaGate1994'").fetchall()
        self.assertEqual(len(red), 1)
        self.assertEqual(red[0]["user_id"], self.user_id)
        self.assertEqual(red[0]["trades"], 1000)
        buy = self.db.execute(
            "SELECT bundle_id, coins, trades, sol_spent, status, "
            "source, promo_code, send_signature "
            "FROM gate_fuel_purchases WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertEqual(buy["bundle_id"], "promo")
        self.assertEqual(buy["coins"], 0)
        self.assertEqual(buy["trades"], 1000)
        self.assertEqual(buy["sol_spent"], 0)
        self.assertEqual(buy["status"], "completed")
        self.assertEqual(buy["source"], "promo")
        self.assertEqual(buy["promo_code"], "BetaGate1994")
        self.assertEqual(buy["send_signature"], "promo:BetaGate1994")

    def test_second_redeem_fails_generic_error(self):
        self._redeem("BetaGate1994")
        resp = self._redeem("BetaGate1994")
        html = resp.get_data(as_text=True)
        self.assertIn("Invalid or already used code.", html)
        self.assertNotIn("Promo code accepted", html)
        self.assertEqual(self._fuel(), 1000)

    def test_other_user_cannot_redeem_after_first(self):
        self._redeem("BetaGate1994")
        _, client2 = make_client(self.tmp.name)
        # Same database file, second account: the code is dead.
        signup(client2, "bob")
        resp = self._redeem("BetaGate1994", client=client2)
        html = resp.get_data(as_text=True)
        self.assertIn("Invalid or already used code.", html)
        bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]
        self.assertEqual(
            gate_fuel_mod.get_fuel(self.db, bob_id)["trades_remaining"],
            0)

    def test_unknown_code_fails_same_generic_error(self):
        resp = self._redeem("NotACode")
        html = resp.get_data(as_text=True)
        self.assertIn("Invalid or already used code.", html)
        self.assertNotIn("Promo code accepted", html)
        self.assertEqual(self._fuel(), 0)

    def test_code_match_is_exact_case_sensitive(self):
        resp = self._redeem("betagate1994")
        html = resp.get_data(as_text=True)
        self.assertIn("Invalid or already used code.", html)
        self.assertEqual(self._fuel(), 0)

    def test_reconcile_does_not_flag_promo_grant(self):
        self._redeem("BetaGate1994")
        fuel = gate_fuel_mod.get_fuel(self.db, self.user_id)
        self.assertEqual(fuel["trades_remaining"], 1000)
        self.assertEqual(fuel["total_purchased"], 1000)
        self.assertEqual(fuel["total_used"], 0)
        overdrawn = self.db.execute(
            "SELECT COUNT(*) AS n FROM activity WHERE user_id = ? "
            "AND kind = 'gate_fuel_overdrawn'",
            (self.user_id,)).fetchone()["n"]
        self.assertEqual(overdrawn, 0)

    def test_chip_reflects_grant(self):
        self._redeem("BetaGate1994")
        # The chip only shows in Real mode; paper shows "free ~ ∞".
        self.db.execute(
            "UPDATE users SET active_mode = 'devnet' WHERE id = ?",
            (self.user_id,))
        self.db.commit()
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("data-gate-fuel-count", html)
        self.assertIn("Trades Left", html)
        self.assertIn('data-gate-fuel-count>1000<', html)


class TestPromoConcurrency(PromoBase):
    def test_simultaneous_redeem_exactly_one_succeeds(self):
        import os
        db_path = os.path.join(self.tmp.name, "test.db")
        results = []

        def attempt():
            conn = connect(db_path)
            try:
                results.append(
                    gate_fuel_mod.redeem_promo_code(
                        conn, self.user_id, "BetaGate1994"))
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        successes = [r for r in results if r is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["trades"], 1000)
        row = self.db.execute(
            "SELECT uses FROM promo_codes WHERE code = 'BetaGate1994'"
        ).fetchone()
        self.assertEqual(row["uses"], 1)
        self.assertEqual(self._fuel(), 1000)
        reds = self.db.execute(
            "SELECT COUNT(*) AS n FROM promo_redemptions "
            "WHERE code = 'BetaGate1994'").fetchone()["n"]
        self.assertEqual(reds, 1)


class TestPromoPerAccount(PromoBase):
    """The signup code "Still on the gate": 50 trades, once per
    account. The motto phrase itself — exact match, spaces
    significant, lowercase gate, no $ and no question mark."""

    def test_signup_code_grants_50_once_per_account(self):
        resp = self._redeem("Still on the gate")
        html = resp.get_data(as_text=True)
        self.assertIn("Promo code accepted", html)
        self.assertIn("50 trades added", html)
        self.assertIn("50 trades left", html)
        self.assertEqual(self._fuel(), 50)

    def test_same_account_cannot_redeem_signup_code_twice(self):
        self._redeem("Still on the gate")
        resp = self._redeem("Still on the gate")
        html = resp.get_data(as_text=True)
        self.assertIn("Invalid or already used code.", html)
        self.assertEqual(self._fuel(), 50)

    def test_other_accounts_can_each_redeem_signup_code(self):
        self._redeem("Still on the gate")
        _, client2 = make_client(self.tmp.name)
        signup(client2, "bob")
        resp = self._redeem("Still on the gate", client=client2)
        html = resp.get_data(as_text=True)
        self.assertIn("Promo code accepted", html)
        self.assertIn("50 trades added", html)
        bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]
        self.assertEqual(
            gate_fuel_mod.get_fuel(self.db, bob_id)["trades_remaining"],
            50)
        uses = self.db.execute(
            "SELECT uses FROM promo_codes "
            "WHERE code = 'Still on the gate'").fetchone()["uses"]
        self.assertEqual(uses, 2)

    def test_signup_code_match_is_exact(self):
        for wrong in ("still on the gate", "Still on the Gate",
                      "Still on the gate?", "Still on the $GATE?",
                      "Still on the gate "):
            resp = self._redeem(wrong)
            html = resp.get_data(as_text=True)
            self.assertIn("Invalid or already used code.", html)
        self.assertEqual(self._fuel(), 0)

    def test_signup_code_concurrent_double_redeem_one_succeeds(self):
        import os
        db_path = os.path.join(self.tmp.name, "test.db")
        results = []

        def attempt():
            conn = connect(db_path)
            try:
                results.append(
                    gate_fuel_mod.redeem_promo_code(
                        conn, self.user_id, "Still on the gate"))
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        successes = [r for r in results if r is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["trades"], 50)
        self.assertEqual(self._fuel(), 50)
        reds = self.db.execute(
            "SELECT COUNT(*) AS n FROM promo_redemptions "
            "WHERE code = 'Still on the gate'").fetchone()["n"]
        self.assertEqual(reds, 1)


if __name__ == "__main__":
    unittest.main()
