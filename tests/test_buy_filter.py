"""Buy filter: only buy a real signal — minimum 5-minute volume,
buys outnumbering sells, minimum signal % — fail-closed, tunable
rule keys, applied to the Scanner and Trenches buy paths.

Defaults: $1,000 of 5-minute volume, buys > sells, signal >= 5%
(the scanner's own buy trigger; Trenches' 3.4% bar is raised to 5%
unless tuned down).
"""

import hashlib
import json
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.assistant import parse_message
from tradingbot.rules import (load_rules, scanner_ruleset, summarize_rules,
                              trenches_ruleset, validate_ruleset)
from tradingbot.scanner import BUY_FILTER_DEFAULT, passes_buy_filter
from tradingbot.silent_loop import run_once


def _entry(mint="MINTA", price=0.01, change_5m=6.0, volume_m5=5000,
           volume_h1=12000, buys=40, sells=10):
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": 25.0, "change_6h_pct": None,
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


class TestBuyFilterUnit(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(BUY_FILTER_DEFAULT, {
            "min_volume_5m_usd": 1000.0,
            "require_buy_dominance": True,
            "min_signal_pct": 5.0,
        })

    def test_strong_signal_passes(self):
        ok, why = passes_buy_filter(_entry())
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_low_volume_rejected(self):
        ok, why = passes_buy_filter(_entry(volume_m5=500))
        self.assertFalse(ok)
        self.assertIn("volume", why)
        self.assertIn("500", why)

    def test_missing_volume_rejected_fail_closed(self):
        entry = _entry()
        del entry["volume_m5_usd"]
        ok, why = passes_buy_filter(entry)
        self.assertFalse(ok)
        self.assertIn("volume data", why)

    def test_unparseable_volume_rejected_fail_closed(self):
        ok, _why = passes_buy_filter(_entry(volume_m5="lots"))
        self.assertFalse(ok)

    def test_sellers_in_control_rejected(self):
        ok, why = passes_buy_filter(_entry(buys=10, sells=40))
        self.assertFalse(ok)
        self.assertIn("sells", why)

    def test_equal_buys_sells_rejected(self):
        ok, _why = passes_buy_filter(_entry(buys=10, sells=10))
        self.assertFalse(ok)

    def test_dominance_off_lets_equal_through(self):
        cfg = dict(BUY_FILTER_DEFAULT, require_buy_dominance=False)
        ok, _why = passes_buy_filter(_entry(buys=10, sells=10), cfg)
        self.assertTrue(ok)

    def test_missing_counts_rejected_fail_closed(self):
        entry = _entry()
        del entry["txns_m5_buys"]
        ok, why = passes_buy_filter(entry)
        self.assertFalse(ok)
        self.assertIn("buy/sell counts", why)

    def test_weak_signal_rejected(self):
        ok, why = passes_buy_filter(_entry(), signal_pct=4.9)
        self.assertFalse(ok)
        self.assertIn("signal", why)

    def test_missing_signal_rejected_fail_closed(self):
        ok, why = passes_buy_filter(_entry(change_5m=None),
                                    signal_pct=None)
        self.assertFalse(ok)
        self.assertIn("signal strength", why)

    def test_numeric_string_signal_accepted(self):
        ok, _why = passes_buy_filter(_entry(), signal_pct="6.0")
        self.assertTrue(ok)

    def test_custom_thresholds_respected(self):
        cfg = {"min_volume_5m_usd": 10000.0,
               "require_buy_dominance": True, "min_signal_pct": 8.0}
        ok, _why = passes_buy_filter(_entry(), cfg)
        self.assertFalse(ok)  # volume 5000 < 10000
        ok, _why = passes_buy_filter(_entry(volume_m5=20000), cfg)
        self.assertFalse(ok)  # signal 6.0 < 8.0
        ok, _why = passes_buy_filter(
            _entry(volume_m5=20000), cfg, signal_pct=9.0)
        self.assertTrue(ok)

    def test_unknown_filter_key_rejected_fail_closed(self):
        ok, why = passes_buy_filter(
            _entry(), {"min_volume_5m_usd": 1000.0,
                       "require_buy_dominance": True,
                       "min_signal_pct": 5.0, "surprise": 1})
        self.assertFalse(ok)
        self.assertIn("Unknown", why)


class TestBuyFilterValidation(unittest.TestCase):
    def test_presets_carry_filter_defaults(self):
        for rules in (trenches_ruleset(), scanner_ruleset()):
            cleaned, err = validate_ruleset(rules)
            self.assertIsNone(err)
            self.assertEqual(cleaned["buy_filter"], BUY_FILTER_DEFAULT)

    def test_old_rules_without_filter_get_defaults(self):
        rules = trenches_ruleset()
        del rules["buy_filter"]
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned["buy_filter"], BUY_FILTER_DEFAULT)

    def test_signals_kind_rejects_filter(self):
        rules = {"version": 1, "kind": "signals", "scan_sources": [],
                 "scan_interval_seconds": 60,
                 "buy": {"metric": "gain_24h_pct", "window_seconds": 86400,
                         "threshold_pct": 2.0, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 100,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0,
                 "buy_filter": dict(BUY_FILTER_DEFAULT)}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("scan the feed", err)

    def test_rejects_bad_volume(self):
        for bad in (-1, 1_000_001, "lots"):
            rules = trenches_ruleset()
            rules["buy_filter"]["min_volume_5m_usd"] = bad
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, bad)

    def test_rejects_bad_signal(self):
        for bad in (0.05, 101, "strong"):
            rules = trenches_ruleset()
            rules["buy_filter"]["min_signal_pct"] = bad
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, bad)

    def test_rejects_non_bool_dominance(self):
        rules = trenches_ruleset()
        rules["buy_filter"]["require_buy_dominance"] = "yes"
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_rejects_limit_field_in_filter(self):
        rules = trenches_ruleset()
        rules["buy_filter"]["buy_size_pct"] = 50
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("limit", err)

    def test_summarize_includes_filter_line(self):
        text = summarize_rules(trenches_ruleset())
        self.assertIn("Buy filter", text)
        self.assertIn("$1,000", text)
        self.assertIn("buys outnumbering sells", text)
        self.assertIn("5.0%", text)


class TestBuyFilterParser(unittest.TestCase):
    def test_volume_phrasing(self):
        p = parse_message(
            "only buy coins with at least $5000 volume in 5 minutes")
        self.assertEqual(p["intent"], "rules_filter")
        self.assertEqual(p["filter"]["min_volume_5m_usd"], 5000.0)

    def test_min_volume_and_signal_phrasing(self):
        p = parse_message(
            "set the buy filter for bot Luna: min volume 2000, "
            "min signal 6%")
        self.assertEqual(p["intent"], "rules_filter")
        self.assertEqual(p["bot_name"], "Luna")
        self.assertEqual(p["filter"]["min_volume_5m_usd"], 2000.0)
        self.assertEqual(p["filter"]["min_signal_pct"], 6.0)

    def test_dominance_phrasing(self):
        p = parse_message(
            "require buys to outnumber sells for bot Luna")
        self.assertEqual(p["intent"], "rules_filter")
        self.assertTrue(p["filter"]["require_buy_dominance"])

    def test_signal_phrasing(self):
        p = parse_message("only buy if the signal is at least 6%")
        self.assertEqual(p["intent"], "rules_filter")
        self.assertEqual(p["filter"]["min_signal_pct"], 6.0)

    def test_filter_without_numbers_asks_for_them(self):
        p = parse_message("set the buy filter for bot Luna")
        self.assertEqual(p["intent"], "rules_unsupported")
        self.assertIn("numbers", p["reply"])

    def test_plain_rules_command_unaffected(self):
        p = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "5 minutes")
        self.assertEqual(p["intent"], "rules_propose")

    def test_show_rules_unaffected(self):
        self.assertEqual(parse_message("show rules for Luna")["intent"],
                         "rules_show")


class TestBuyFilterRoutes(unittest.TestCase):
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
        token = csrf(self.client, "/wallet")
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

    def test_filter_propose_and_confirm(self):
        self._insert_bot(rules=trenches_ruleset())
        data = self._ask(
            "set the buy filter for bot Trenches: min volume 5000, "
            "min signal 6%")
        self.assertIn("Buy filter", data["reply"])
        self.assertIn("$5,000", data["reply"])
        self.assertIn("6.0%", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        rules, err = load_rules(self._bot())
        self.assertIsNone(err)
        self.assertEqual(rules["buy_filter"], BUY_FILTER_DEFAULT)
        data = self._ask("yes")
        self.assertIn("now follows", data["reply"])
        rules, _err = load_rules(self._bot())
        self.assertEqual(rules["buy_filter"]["min_volume_5m_usd"], 5000.0)
        self.assertEqual(rules["buy_filter"]["min_signal_pct"], 6.0)
        self.assertEqual(rules["buy"]["threshold_pct"], 3.4)

    def test_filter_needs_bot_name(self):
        self._insert_bot(rules=trenches_ruleset())
        data = self._ask("set the buy filter: min volume 5000")
        self.assertIn("Which bot", data["reply"])

    def test_filter_needs_existing_rules(self):
        self._insert_bot(rules=None)
        data = self._ask(
            "set the buy filter for bot Trenches: min volume 5000")
        self.assertIn("no custom rules yet", data["reply"])


class TestBuyFilterLoop(unittest.TestCase):
    """The filter runs in the silent loop's Trenches and Scanner
    buy paths."""

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

    def _insert_bot(self, name="Trenches", rules=None, category=None):
        rules_json = json.dumps(rules) if rules is not None else ""
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', 1, 3, ?, ?, ?)",
            (self.user_id, name,
             hashlib.sha256(b"tok").hexdigest(),
             category or "Memecoin build", rules_json))
        self.db.commit()
        return cur.lastrowid

    def _position(self, bot_id, coin):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, coin)).fetchone()

    def _skips(self):
        return self.db.execute(
            "SELECT * FROM activity WHERE user_id = ? AND kind = ?",
            (self.user_id, "silent_skip")).fetchall()

    def test_trenches_weak_gain_filtered_not_bought(self):
        """A 3.5% gainer passes the 3.4% buy threshold but fails the
        5% filter bar — the effective bar is 5% unless tuned down."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = run_once(self.db, self.cfg,
                            price_fetcher=_empty_prices,
                            trenches_fetcher=_FakeScan(
                                [_entry(change_5m=3.5)]))
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertIn("buy filter",
                      outcomes[0]["actions"][0]["reason"])
        skips = [a for a in self._skips() if "buy filter" in a["detail"]]
        self.assertEqual(len(skips), 1)
        self.assertIn("MINTA", skips[0]["detail"])

    def test_trenches_low_volume_filtered_not_bought(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = run_once(
            self.db, self.cfg, price_fetcher=_empty_prices,
            trenches_fetcher=_FakeScan(
                [_entry(change_5m=8.0, volume_m5=50)]))
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")

    def test_trenches_sellers_in_control_filtered(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = run_once(
            self.db, self.cfg, price_fetcher=_empty_prices,
            trenches_fetcher=_FakeScan(
                [_entry(change_5m=8.0, buys=5, sells=50)]))
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_trenches_strong_signal_buys_and_records_signals(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = run_once(
            self.db, self.cfg, price_fetcher=_empty_prices,
            trenches_fetcher=_FakeScan(
                [_entry(change_5m=8.0, volume_m5=5000, buys=40,
                        sells=10)]))
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, "MINTA"))
        trade = self.db.execute(
            "SELECT * FROM paper_trades WHERE user_id = ? "
            "AND coin_id = 'MINTA' AND side = 'buy'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(trade)
        self.assertEqual(trade["bot_id"], bot_id)
        self.assertAlmostEqual(trade["buy_m5_pct"], 8.0)
        self.assertAlmostEqual(trade["buy_volume_5m_usd"], 5000.0)
        self.assertEqual(trade["buy_buys_5m"], 40)
        self.assertEqual(trade["buy_sells_5m"], 10)
        # 5-minute volume 5000 vs an hourly pace of 1000 (2x+),
        # buys outnumbering sells, +25% in the last hour: trending.
        self.assertEqual(trade["buy_regime"], "trending")

    def test_tuned_down_filter_lets_trenches_buy_at_3_5(self):
        """The filter is a rule key, not a constant: tuning the
        minimum signal down to the 3.4% threshold restores the old
        behaviour for that bot."""
        rules = trenches_ruleset()
        rules["buy_filter"]["min_signal_pct"] = 3.4
        bot_id = self._insert_bot(rules=rules)
        outcomes = run_once(self.db, self.cfg,
                            price_fetcher=_empty_prices,
                            trenches_fetcher=_FakeScan(
                                [_entry(change_5m=3.5)]))
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, "MINTA"))


if __name__ == "__main__":
    unittest.main()
