"""Manual sell without a bot: the Sell button a user taps overrides
the bot managing the coin and exits cleanly.

Rules under test (James's words):
- "when I sell any coin take away the requirement for a bot to
  manage sell" / "Make it so my commands make the exit" / "No need
  to have a bot oversee my desires in real time" (2026-11-24).
- "Sell button a user uses overrides the bot managing it and exit
  cleanly" (2026-11-25) — the override covers the bot's RULES and
  its SELLING POINT: the user sells at their own point, by their
  own rules, at the live market price. No bot exit rule (TP tiers,
  trailing stop, stop loss, time caps) is consulted on the sell
  path.
- The ONLY rule the override does NOT touch is the last-credit
  reserve (fuel==1): manual sells stay refused there and the last
  credit sweeps every open position, so the user is never stuck
  in positions with no fuel left to act.

No SOL moves in these tests; real execution is a recording mock
and the trenches / Jupiter feeds are mocked.
"""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
from helpers import csrf, grant_fuel, login, make_client, signup
from tradingbot import gate_fuel as gate_fuel_mod
from tradingbot.solana import generate_keypair, sol_to_lamports

GRAD_MINT = "Grad1111111111111111111111111111111111111111"
BONDING_MINT = "Bond1111111111111111111111111111111111111111"
MINT_PRICES = {GRAD_MINT: 0.002, BONDING_MINT: 0.001}


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def get_account_info(self, pubkey):
        return None

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes=None):
    prices = {"SOL": {"price": 100.0, "change_24h": 0.0,
                      "change_1h": 0.0}}
    if changes:
        prices.update({c: {"price": p, "change_24h": ch}
                       for c, (p, ch) in changes.items()})
    return {"prices": prices, "source": "live", "as_of": "test"}


def set_fuel(db, user_id, n):
    """Set fuel to exactly `n` credits by consuming down from the
    current balance with distinct fake signatures (the honest
    pattern from tests/test_exit_all.py)."""
    current = gate_fuel_mod.get_fuel(
        db, user_id)["trades_remaining"]
    assert current >= n, f"fuel {current} < target {n}"
    for i in range(current - n):
        assert gate_fuel_mod.consume_fuel(
            db, user_id, None, f"setup-sig-{i:03d}"), \
            f"setup consume {i} failed"
    assert gate_fuel_mod.get_fuel(
        db, user_id)["trades_remaining"] == n


class ManualSellBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self._patches = [
            mock.patch.object(routes, "SolanaRpcClient", FakeRpc),
            mock.patch.object(routes, "get_prices",
                              return_value=make_prices()),
            mock.patch.object(routes, "fetch_prices_for_mints",
                              return_value=dict(MINT_PRICES)),
            mock.patch.object(routes, "fetch_mint_1h_changes",
                              return_value={}),
            mock.patch.object(routes, "fetch_mint_identities",
                              return_value={}),
            mock.patch.object(routes, "ensure_coin_meta",
                              return_value=None),
            mock.patch.object(routes, "enrich_row",
                              side_effect=lambda row, **kw: row),
            mock.patch.object(routes, "fetch_trenches_scan",
                              return_value={"candidates": [],
                                            "prices": {},
                                            "source": "dexscreener",
                                            "as_of": "test",
                                            "error": None}),
            mock.patch.object(jupiter_mod, "quote_sell_mint",
                              side_effect=jupiter_mod.JupiterError(
                                  "no route")),
        ]
        for p in self._patches:
            p.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        grant_fuel(self.db, self.user_id, 100)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _create_bot(self, name="Nova", mode="real", coins=BONDING_MINT,
                    rules_json=None):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": name, "strategy": "balanced",
            "platform": "pump.fun",
            "coins": coins, "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": mode})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = ? AND user_id = ?",
            (name, self.user_id)).fetchone()
        self.assertIsNotNone(bot, f"bot {name} was not created")
        if rules_json is not None:
            self.db.execute(
                "UPDATE bots SET rules_json = ? WHERE id = ?",
                (json.dumps(rules_json), bot["id"]))
            self.db.commit()
        return bot["id"]

    def _mocked_sell(self, mint, form, curve_complete=False):
        """POST a real sell with the executor mocked to execute and
        the on-chain read reporting the coins as held (the chain
        pre-check itself is covered by tests/test_balance_read.py)."""
        def fake_execute(*args, **kwargs):
            return {"status": "executed", "signature": "fakesigSELL",
                    "coin_amount": 1.0}

        def fake_chain_balances(rpc, pubkey):
            return ([{"mint": mint, "amount": "1000000000",
                      "ui_amount": 1000000000.0}], True)

        with mock.patch.object(routes, "is_curve_complete",
                               return_value=curve_complete), \
                mock.patch.object(routes, "fetch_chain_token_balances",
                                  side_effect=fake_chain_balances), \
                mock.patch.object(routes, "execute_jupiter_swap") \
                as jup_exec, \
                mock.patch.object(routes, "execute_real_trade",
                                  fake_execute):
            resp = self.client.post(f"/coins/{mint}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{mint}"),
                **form})
        return resp, jup_exec

    def _bot_position(self, bot_id, mint, amount=5.0, price=0.01):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode) VALUES (?, ?, ?, ?, ?, 'real')",
            (self.user_id, bot_id, mint, amount, price))
        self.db.commit()


class TestManualSellNoBot(ManualSellBase):
    def test_manual_real_sell_with_no_bot_succeeds(self):
        # (a) No bot exists at all, no bot picked: the sell still
        # executes for the amount entered.
        self._choose_mode("devnet")
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, BONDING_MINT, 10.0, 0.01))
        self.db.commit()
        resp, jup_exec = self._mocked_sell(
            BONDING_MINT, {"amount": "4"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(jup_exec.call_count, 0)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("fakesigSELL", html)
        self.assertNotIn("Pick one of your real bots", html)
        row = self.db.execute(
            "SELECT amount FROM real_holdings WHERE coin_id = ?",
            (BONDING_MINT,)).fetchone()
        self.assertAlmostEqual(row["amount"], 6.0)

    def test_manual_sell_of_bot_position_shrinks_row(self):
        # (b) A bot-managed position: partial manual sell shrinks
        # the bot row; the bot stops trying to sell what is gone.
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        self._bot_position(bot_id, mint, amount=5.0)
        resp, _ = self._mocked_sell(mint, {"amount": "2"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        row = self.db.execute(
            "SELECT amount FROM bot_positions WHERE bot_id = ? "
            "AND coin_id = ?", (bot_id, mint)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["amount"], 3.0)

    def test_manual_sell_all_of_bot_position_deletes_row(self):
        # (b) Sell-all of a bot-managed position: the bot row is
        # deleted, no ghost row left behind.
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        self._bot_position(bot_id, mint, amount=5.0)
        resp, _ = self._mocked_sell(mint, {"sell_all": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        row = self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, mint)).fetchone()
        self.assertIsNone(row)

    def test_manual_sell_at_fuel_one_still_refused(self):
        # (c) The last-credit reserve is the ONE rule the override
        # does not touch: at fuel==1 a manual sell stays refused.
        self._choose_mode("devnet")
        bot_id = self._create_bot("Nova", mode="real")
        mint = generate_keypair()[1]
        self._bot_position(bot_id, mint, amount=5.0)
        set_fuel(self.db, self.user_id, 1)
        resp, _ = self._mocked_sell(mint, {"amount": "1"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Last trade reserved", html)
        self.assertNotIn("Sold", html)
        row = self.db.execute(
            "SELECT amount FROM bot_positions WHERE bot_id = ? "
            "AND coin_id = ?", (bot_id, mint)).fetchone()
        self.assertAlmostEqual(row["amount"], 5.0)

    def test_sell_screen_prefill_and_quote_still_work(self):
        # (d) The sell ticket still prefills from the holding and
        # shows the live quote after the bot picker is gone.
        self._choose_mode("paper")
        self.client.post(f"/coins/{GRAD_MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "usd": "10", "bot_id": ""})
        html = self.client.get(f"/coins/{GRAD_MINT}").get_data(as_text=True)
        self.assertIn("Sell (paper)", html)
        self.assertIn("You hold", html)
        self.assertIn('name="amount_sol"', html)

    def test_buy_without_bot_still_refused(self):
        # (e) Guard against over-removal: buys still require a
        # real bot — only sells lost the bot requirement.
        self._choose_mode("devnet")
        resp = self.client.post(f"/coins/{GRAD_MINT}/buy", data={
            "csrf_token": csrf(self.client, f"/coins/{GRAD_MINT}"),
            "amount_sol": "0.1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was bought", html)
        self.assertIn("Pick one of your real bots", html)

    def test_manual_sell_overrides_bot_rules_and_selling_point(self):
        # (f) Refinement 2026-11-25: the manual sell overrides the
        # bot's RULES and its SELLING POINT. This bot would NOT
        # sell on its own rules — the position sits at +0% (below
        # its +10% TP rung, above its -5% stop) — yet the user's
        # Sell button exits cleanly at the live price anyway.
        self._choose_mode("devnet")
        rules = {"kind": "custom",
                 "sell_ladder": [{"pct": 10, "sell_pct": 25},
                                 {"pct": 25, "sell_pct": 50}],
                 "stop_trigger_pct": 5.0,
                 "trailing_stop_pct": 7.0}
        bot_id = self._create_bot("Nova", mode="real",
                                  coins=GRAD_MINT, rules_json=rules)
        # Position at exactly the current price (0.002): +0%,
        # below TP, above stop — the bot's rules say hold.
        self._bot_position(bot_id, GRAD_MINT, amount=5.0, price=0.002)
        resp, _ = self._mocked_sell(GRAD_MINT, {"amount": "2"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertNotIn("bot is managing it", html)
        row = self.db.execute(
            "SELECT amount FROM bot_positions WHERE bot_id = ? "
            "AND coin_id = ?", (bot_id, GRAD_MINT)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["amount"], 3.0)


if __name__ == "__main__":
    unittest.main()
