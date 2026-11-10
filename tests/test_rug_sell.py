"""Rug sell rule: fast-drop exit, Trenches default, agent commands.

The rug sell sells ALL on a collapse — a 15% drop in about a minute
(price-mark comparison) or a 25% fall in 5 minutes (feed change) —
checked first on every price update, before the stop loss, trailing
stop and ladder. It is price-based: the feed cannot see the dev's
wallet, so a dev selling shows up only as the price falling.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.assistant import parse_message
from tradingbot.rules import (RUG_SELL_DEFAULT, load_rules,
                              summarize_rules, trenches_ruleset,
                              validate_ruleset)
from tradingbot.silent_loop import run_once


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _entry(mint, price, change_5m=None, volume_m5=5000, volume_h1=12000,
           buys=40, sells=10):
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": None, "change_6h_pct": None,
            "change_24h_pct": None, "liquidity_usd": 1000,
            "market_cap_usd": None,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": 0,
            "volume_m5_usd": volume_m5, "volume_h1_usd": volume_h1,
            "txns_m5_buys": buys, "txns_m5_sells": sells}


class _FakeScan:
    def __init__(self, entries):
        self.entries = list(entries)

    def __call__(self, mints=None):
        prices = {e["mint"]: e for e in self.entries}
        return {"candidates": list(self.entries), "prices": prices,
                "source": "dexscreener", "as_of": "test", "error": None}


def _empty_prices():
    return {"prices": {}, "source": "snapshot", "as_of": "test"}


class TestRugValidation(unittest.TestCase):
    def test_trenches_preset_carries_rug_sell(self):
        rules = trenches_ruleset()
        self.assertEqual(rules["rug_sell"], RUG_SELL_DEFAULT)
        self.assertEqual(RUG_SELL_DEFAULT,
                         {"drop_pct": 15.0, "window_seconds": 60,
                          "drop_5m_pct": 25.0})
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned["rug_sell"], RUG_SELL_DEFAULT)

    def test_rug_defaults_filled_from_empty_dict(self):
        rules = trenches_ruleset()
        rules["rug_sell"] = {}
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned["rug_sell"], RUG_SELL_DEFAULT)

    def test_rug_partial_overrides(self):
        rules = trenches_ruleset()
        rules["rug_sell"] = {"drop_pct": 10}
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned["rug_sell"],
                         {"drop_pct": 10.0, "window_seconds": 60,
                          "drop_5m_pct": 25.0})

    def test_rug_absent_means_no_rug_trigger(self):
        rules = trenches_ruleset()
        del rules["rug_sell"]
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertIsNone(cleaned["rug_sell"])

    def test_rejects_unknown_rug_key(self):
        rules = trenches_ruleset()
        rules["rug_sell"] = {"dev_wallet": True}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("Unknown rug sell setting", err)

    def test_rejects_bad_drop(self):
        for bad in (0, 0.5, 91, "15"):
            rules = trenches_ruleset()
            rules["rug_sell"] = {"drop_pct": bad}
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, f"drop {bad!r} accepted")

    def test_rejects_bad_window(self):
        for bad in (30, 120, 3600, "60"):
            rules = trenches_ruleset()
            rules["rug_sell"] = {"window_seconds": bad}
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, f"window {bad!r} accepted")

    def test_rejects_bad_drop_5m(self):
        rules = trenches_ruleset()
        rules["rug_sell"] = {"drop_5m_pct": 0}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_rejects_limit_field_in_rug(self):
        rules = trenches_ruleset()
        rules["rug_sell"] = {"buy_size_pct": 50}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("bot limit", err)

    def test_summarize_includes_rug_line(self):
        summary = summarize_rules(trenches_ruleset())
        self.assertIn("Rug sell", summary)
        self.assertIn("15%", summary)
        self.assertIn("25%", summary)
        self.assertIn("price-based", summary)
        self.assertIn("cannot see the dev's wallet", summary)

    def test_summarize_without_rug_has_no_rug_line(self):
        rules = trenches_ruleset()
        del rules["rug_sell"]
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertNotIn("Rug sell", summarize_rules(cleaned))


class TestRugParser(unittest.TestCase):
    def test_dev_selling_phrase(self):
        parsed = parse_message(
            "sell everything if the dev starts selling for bot Luna")
        self.assertEqual(parsed["intent"], "rules_rug")
        self.assertEqual(parsed["bot_name"], "Luna")
        self.assertEqual(parsed["rug"], RUG_SELL_DEFAULT)

    def test_dev_selling_no_bot(self):
        parsed = parse_message("sell everything if the dev starts selling")
        self.assertEqual(parsed["intent"], "rules_rug")
        self.assertIsNone(parsed["bot_name"])

    def test_creator_dump_phrase(self):
        parsed = parse_message("add a rug rule to bot Luna")
        self.assertEqual(parsed["intent"], "rules_rug")
        self.assertEqual(parsed["bot_name"], "Luna")
        self.assertEqual(parsed["rug"], RUG_SELL_DEFAULT)

    def test_drop_phrase_one_minute(self):
        parsed = parse_message(
            "sell if the price drops 15% in a minute for bot Luna")
        self.assertEqual(parsed["intent"], "rules_rug")
        self.assertEqual(parsed["rug"]["drop_pct"], 15.0)
        self.assertEqual(parsed["rug"]["window_seconds"], 60)

    def test_drop_phrase_five_minutes(self):
        parsed = parse_message(
            "sell everything if it falls 25% in 5 minutes for bot Luna")
        self.assertEqual(parsed["intent"], "rules_rug")
        self.assertEqual(parsed["rug"]["drop_pct"], 25.0)
        self.assertEqual(parsed["rug"]["window_seconds"], 300)

    def test_combined_buy_and_rug(self):
        parsed = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "5 minutes, sell everything if the dev starts selling")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["rules"]["rug_sell"], RUG_SELL_DEFAULT)
        self.assertEqual(parsed["rules"]["buy"]["threshold_pct"], 5.0)

    def test_unsupported_rug_window(self):
        parsed = parse_message(
            "sell if the price drops 20% in 2 minutes for bot Luna")
        self.assertEqual(parsed["intent"], "rules_unsupported")
        self.assertIn("1-minute or a 5-minute", parsed["reply"])

    def test_sell_everything_alone_is_not_rug(self):
        parsed = parse_message("sell everything")
        self.assertNotEqual(parsed["intent"], "rules_rug")
        self.assertNotEqual(parsed["intent"], "rules_propose")

    def test_trailing_stop_still_parses_without_rug(self):
        parsed = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "5 minutes, sell all if it falls 7% below the highest price")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["rules"]["trailing_stop_pct"], 7.0)
        self.assertIsNone(parsed["rules"]["rug_sell"])


class RugEngineBase(unittest.TestCase):
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

    def _insert_bot(self, name="Trenches", mode="paper", silent=1,
                    buy_size=20, stop_loss=7, max_open=3,
                    rules=None):
        rules_json = json.dumps(rules) if rules is not None else ""
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'bold', '[]', ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, buy_size, stop_loss, mode, silent,
             max_open, hashlib.sha256(b"tok").hexdigest(),
             "Memecoin build", rules_json))
        self.db.commit()
        return cur.lastrowid

    def _insert_position(self, bot_id, coin, amount, avg_price,
                         peak=None, orig=None, stage=0,
                         last_price=None, last_price_at=None):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at, peak_price, orig_amount, "
            "ladder_stage, last_price, last_price_at) "
            "VALUES (?, ?, ?, ?, ?, 'paper', 'now', ?, ?, ?, ?, ?)",
            (self.user_id, bot_id, coin, amount, avg_price,
             peak if peak is not None else avg_price,
             orig if orig is not None else amount, stage,
             last_price if last_price is not None else avg_price,
             last_price_at if last_price_at is not None else _now()))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, coin, amount, avg_price))
        self.db.commit()

    def _reset_scan(self, bot_id):
        self.db.execute("UPDATE bots SET last_scan_at = '' WHERE id = ?",
                        (bot_id,))
        self.db.commit()

    def _position(self, bot_id, coin):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, coin)).fetchone()

    def _activity(self, kind=None):
        q = "SELECT * FROM activity WHERE user_id = ?"
        args = [self.user_id]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return self.db.execute(q, args).fetchall()

    def _run(self, entries):
        scan = _FakeScan(entries)
        return run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                        trenches_fetcher=scan)


class TestRugEngine(RugEngineBase):
    def test_rug_fires_on_5m_collapse(self):
        """Feed says -30% in 5 min, price only -5% from peak: the rug
        trigger fires, not the trailing stop."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        outcomes = self._run([_entry("MINTA", 0.0095, change_5m=-30.0)])
        sell = [a for a in outcomes[0]["actions"]
                if a["action"] == "sell"]
        self.assertEqual(len(sell), 1)
        self.assertIn("rug sell", sell[0]["reason"])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        detail = self._activity("silent_sell")[0]["detail"]
        self.assertIn("rug sell", detail)

    def test_no_rug_on_small_5m_drop(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        outcomes = self._run([_entry("MINTA", 0.0095, change_5m=-10.0)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_rug_fires_on_1m_mark_drop(self):
        """Price fell 16.7% since the last mark (~a minute ago) while
        the 5-minute feed is flat: the mark-based trigger fires."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01, peak=0.0102,
                              last_price=0.012, last_price_at=_now())
        outcomes = self._run([_entry("MINTA", 0.010, change_5m=0.0)])
        sell = [a for a in outcomes[0]["actions"]
                if a["action"] == "sell"]
        self.assertEqual(len(sell), 1)
        self.assertIn("rug sell", sell[0]["reason"])
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_slow_drift_does_not_fire_rug(self):
        """A 4.8% drift since the mark is not a rug — and the mark is
        refreshed to the new price for the next scan."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01,
                              last_price=0.0105, last_price_at=_now())
        outcomes = self._run([_entry("MINTA", 0.0100, change_5m=-2.0)])
        self.assertEqual(outcomes[0]["actions"], [])
        pos = self._position(bot_id, "MINTA")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["last_price"], 0.0100)

    def test_stale_mark_does_not_fire(self):
        """A 16.7% drop against a mark 10 minutes old is not a
        one-minute drop: no fire, mark refreshed."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01, peak=0.0102,
                              last_price=0.012,
                              last_price_at=_now(-600))
        outcomes = self._run([_entry("MINTA", 0.010, change_5m=0.0)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_rug_fires_before_trailing_stop(self):
        """A 45% crash would also trip the trailing stop — the sell
        reason must be the rug sell, which is checked first."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01, peak=0.011)
        outcomes = self._run([_entry("MINTA", 0.006, change_5m=-40.0)])
        sell = [a for a in outcomes[0]["actions"]
                if a["action"] == "sell"]
        self.assertEqual(len(sell), 1)
        self.assertIn("rug sell", sell[0]["reason"])

    def test_trenches_preset_rug_sells_all(self):
        """The preset carries the rug trigger on by default."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        outcomes = self._run([_entry("MINTA", 0.0095, change_5m=-30.0)])
        self.assertEqual(len([a for a in outcomes[0]["actions"]
                              if a["action"] == "sell"]), 1)
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_no_rug_without_rule(self):
        """A rule set with no rug_sell does not rug-sell, even on a
        -40% 5-minute feed move (nothing else fires either: the price
        is only 5% below the peak)."""
        rules = trenches_ruleset()
        del rules["rug_sell"]
        bot_id = self._insert_bot(rules=rules)
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        outcomes = self._run([_entry("MINTA", 0.0095, change_5m=-40.0)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_new_position_gets_price_mark(self):
        """A buy opens the position with last_price set, so the rug
        check has a baseline on the next scan."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=6.0)])
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        pos = self._position(bot_id, "MINTA")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["last_price"], 0.01)


class TestRugRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _ask(self, message):
        token = csrf(self.client, "/dashboard")
        resp = self.client.post("/api/assistant", json={
            "message": message, "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _insert_bot(self, name="Trenches", rules=None):
        rules_json = json.dumps(rules) if rules is not None else ""
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 0, 3, ?, "
            "'Memecoin build', ?)",
            (self.user_id, name,
             hashlib.sha256(b"tok").hexdigest(), rules_json))
        self.db.commit()
        return cur.lastrowid

    def _bot(self, name="Trenches"):
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = ?",
            (self.user_id, name)).fetchone()

    def test_rug_rule_propose_and_confirm(self):
        rules = trenches_ruleset()
        del rules["rug_sell"]
        self._insert_bot(rules=rules)
        data = self._ask("sell everything if the dev starts selling "
                         "for bot Trenches")
        self.assertIn("Rug sell", data["reply"])
        self.assertIn("15%", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        # Nothing stored yet — proposal only.
        rules, err = load_rules(self._bot())
        self.assertIsNone(err)
        self.assertIsNone(rules["rug_sell"])
        # "yes" applies (merged rules keep the buy/ladder/trailing).
        data = self._ask("yes")
        self.assertIn("now follows", data["reply"])
        rules, err = load_rules(self._bot())
        self.assertIsNone(err)
        self.assertEqual(rules["rug_sell"], RUG_SELL_DEFAULT)
        self.assertEqual(rules["buy"]["threshold_pct"], 3.4)
        self.assertEqual(rules["trailing_stop_pct"], 7.0)

    def test_rug_rule_with_custom_drop(self):
        self._insert_bot(rules=trenches_ruleset())
        data = self._ask("sell if the price drops 10% in a minute "
                         "for bot Trenches")
        self.assertIn("Rug sell", data["reply"])
        self.assertIn("10%", data["reply"])
        data = self._ask("yes")
        rules, _err = load_rules(self._bot())
        self.assertEqual(rules["rug_sell"]["drop_pct"], 10.0)
        self.assertEqual(rules["rug_sell"]["window_seconds"], 60)

    def test_rug_rule_needs_existing_rules(self):
        self._insert_bot(rules=None)
        data = self._ask("sell everything if the dev starts selling "
                         "for bot Trenches")
        self.assertIn("no custom rules yet", data["reply"])
        self.assertIn("Nothing was changed", data["reply"])
        self.assertEqual(self._bot()["rules_json"], "")

    def test_rug_rule_unknown_bot(self):
        self._insert_bot(rules=trenches_ruleset())
        data = self._ask("sell everything if the dev starts selling "
                         "for bot Nope")
        self.assertIn("couldn't find a bot", data["reply"])

    def test_rug_rule_no_bot_named(self):
        self._insert_bot(rules=trenches_ruleset())
        data = self._ask("sell everything if the dev starts selling")
        self.assertIn("Which bot", data["reply"])


if __name__ == "__main__":
    unittest.main()
