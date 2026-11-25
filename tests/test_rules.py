"""Rules engine + in-app agent rules + Trenches preset + Memecoin build.

Covers: rule validation (reject widening/malformed rules), the
deterministic parser (including James's phrasings), the silent-loop
rules engine against a fake trenches feed (buy threshold, strongest
gainer, sell ladder, trailing stop, fail-closed behaviour), and the
preset/UI creation paths.
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
from tradingbot.rules import (TRENCHES_CATEGORY, load_rules, summarize_rules,
                              trenches_ruleset, validate_ruleset)
from tradingbot import silent_loop
from tradingbot.silent_loop import run_once

# Gate (the devnet pump.fun coin): signals bots watch mint
# addresses, never symbols — "SOL" has no price in the mint map.
GATE_MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"

TRENCHES_SPEC = (
    "scan the trenches every minute and buy any coin gaining 3.4% in "
    "5 min intervals, sell 10% of total coins at 3% profit and the 90 "
    "at 10%. Sell all if it declines 7% of highest value since holding"
)


def _trenches_rules(**overrides):
    rules = trenches_ruleset()
    rules.update(overrides)
    return rules


class TestRuleValidation(unittest.TestCase):
    def test_trenches_preset_validates(self):
        cleaned, err = validate_ruleset(trenches_ruleset())
        self.assertIsNone(err)
        self.assertEqual(cleaned["buy"]["threshold_pct"], 3.4)
        self.assertEqual(cleaned["sell_ladder"],
                         [{"pct_of_position": 10.0, "profit_pct": 3.0},
                          {"pct_of_position": 90.0, "profit_pct": 10.0}])

    def test_signals_kind_validates(self):
        rules = {"version": 1, "kind": "signals", "scan_sources": [],
                 "scan_interval_seconds": 60,
                 "buy": {"metric": "gain_24h_pct", "window_seconds": 86400,
                         "threshold_pct": 2.0, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 100,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0}
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned["kind"], "signals")

    def test_trailing_defaults_to_bot_stop_loss(self):
        rules = _trenches_rules()
        del rules["trailing_stop_pct"]
        cleaned, err = validate_ruleset(rules, bot={"stop_loss_pct": 5})
        self.assertIsNone(err)
        self.assertEqual(cleaned["trailing_stop_pct"], 5.0)

    def test_rejects_non_dict(self):
        for bad in (None, "buy the dip", [1, 2], 42):
            cleaned, err = validate_ruleset(bad)
            self.assertIsNone(cleaned)
            self.assertTrue(err)

    def test_rejects_unknown_keys(self):
        rules = _trenches_rules()
        rules["moon_shot"] = True
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(cleaned)
        self.assertIn("moon_shot", err)

    def test_rejects_limit_fields(self):
        for field, value in (("buy_size_pct", 50), ("max_open_trades", 5),
                             ("stop_loss_pct", 3), ("balance_cap", 100)):
            rules = _trenches_rules()
            rules[field] = value
            cleaned, err = validate_ruleset(rules)
            self.assertIsNone(cleaned, field)
            self.assertIn("limit", err, field)

    def test_rejects_bad_version_kind_interval(self):
        for mutate in (
                lambda r: r.update(version=2),
                lambda r: r.update(kind="moon"),
                lambda r: r.update(scan_interval_seconds=5),
                lambda r: r.update(scan_interval_seconds=7200)):
            rules = _trenches_rules()
            mutate(rules)
            cleaned, err = validate_ruleset(rules)
            self.assertIsNone(cleaned)
            self.assertTrue(err)

    def test_rejects_bad_sources(self):
        rules = _trenches_rules()
        rules["scan_sources"] = []
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(cleaned)
        rules = _trenches_rules()
        rules["scan_sources"] = ["pump.fun", "mars"]
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(cleaned)
        self.assertIn("mars", err)

    def test_rejects_bad_buy(self):
        cases = [
            lambda r: r["buy"].update(window_seconds=900),
            lambda r: r["buy"].update(metric="gain_24h_pct"),
            lambda r: r["buy"].update(threshold_pct=0),
            lambda r: r["buy"].update(threshold_pct=101),
            lambda r: r["buy"].update(pick="first"),
        ]
        for mutate in cases:
            rules = _trenches_rules()
            mutate(rules)
            cleaned, err = validate_ruleset(rules)
            self.assertIsNone(cleaned)
            self.assertTrue(err)

    def test_rejects_bad_ladder(self):
        cases = [
            lambda r: r.update(sell_ladder=[]),
            lambda r: r.update(sell_ladder=[
                {"pct_of_position": 60, "profit_pct": 3},
                {"pct_of_position": 60, "profit_pct": 10}]),
            lambda r: r.update(sell_ladder=[
                {"pct_of_position": 50, "profit_pct": 10},
                {"pct_of_position": 50, "profit_pct": 3}]),
            lambda r: r.update(sell_ladder=[
                {"pct_of_position": 50, "profit_pct": 10},
                {"pct_of_position": 50}]),
        ]
        for mutate in cases:
            rules = _trenches_rules()
            mutate(rules)
            cleaned, err = validate_ruleset(rules)
            self.assertIsNone(cleaned)
            self.assertTrue(err)

    def test_rejects_bad_trailing_and_max_new(self):
        rules = _trenches_rules()
        rules["trailing_stop_pct"] = 0.1
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(cleaned)
        rules = _trenches_rules()
        rules["max_new_positions_per_scan"] = 0
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(cleaned)

    def test_load_rules(self):
        bot = {"rules_json": "", "stop_loss_pct": 7}
        rules, err = load_rules(bot)
        self.assertIsNone(rules)
        self.assertIsNone(err)
        bot = {"rules_json": "{not json", "stop_loss_pct": 7}
        rules, err = load_rules(bot)
        self.assertIsNone(rules)
        self.assertTrue(err)
        bot = {"rules_json": json.dumps(trenches_ruleset()),
               "stop_loss_pct": 7}
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules["buy"]["threshold_pct"], 3.4)
        bot = {"rules_json": json.dumps({"version": 1, "kind": "nope"}),
               "stop_loss_pct": 7}
        rules, err = load_rules(bot)
        self.assertIsNone(rules)
        self.assertTrue(err)


class TestRulesParser(unittest.TestCase):
    def test_five_percent_example(self):
        p = parse_message(
            "bot 1 scan pump.fun and pumpswap, buy when a coin gains 5% "
            "in 5 minutes")
        self.assertEqual(p["intent"], "rules_propose")
        self.assertEqual(p["bot_name"], "1")
        rules = p["rules"]
        self.assertEqual(rules["kind"], "trenches")
        self.assertEqual(rules["scan_sources"], ["pump.fun", "pumpswap"])
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(rules["buy"]["window_seconds"], 300)
        self.assertEqual(rules["buy"]["metric"], "gain_5m_pct")
        self.assertIsNone(rules["trailing_stop_pct"])

    def test_james_full_example(self):
        p = parse_message(
            "bot 1 scan pump.fun and pumpswap for new tokens that show "
            "incredible data. Only one Signal gaining in a 5 min window "
            "by 5% get approval to silent trade")
        self.assertEqual(p["intent"], "rules_propose")
        self.assertEqual(p["bot_name"], "1")
        self.assertEqual(p["rules"]["buy"]["threshold_pct"], 5.0)
        self.assertEqual(p["rules"]["buy"]["window_seconds"], 300)

    def test_trenches_spec_phrasing(self):
        p = parse_message(TRENCHES_SPEC)
        self.assertEqual(p["intent"], "rules_propose")
        cleaned, err = validate_ruleset(p["rules"])
        self.assertIsNone(err)
        # The spec text predates the rug rule and does not mention
        # one, so parsing it yields the preset without a rug trigger
        # — a command that does not ask for a rug rule does not
        # invent one. (The preset itself carries the rug rule on by
        # default; see test_trenches_preset_carries_rug_sell.)
        expected = trenches_ruleset()
        expected["rug_sell"] = None
        self.assertEqual(cleaned, expected)
        self.assertIsNone(cleaned["rug_sell"])
        self.assertIn("3.4%", summarize_rules(cleaned))
        self.assertIn("10%", summarize_rules(cleaned))

    def test_unsupported_window(self):
        p = parse_message(
            "bot Luna buy when a coin gains 5% in 15 minutes")
        self.assertEqual(p["intent"], "rules_unsupported")
        self.assertIn("5-minute", p["reply"])

    def test_scan_without_buy_is_not_rules(self):
        p = parse_message("bot Luna scan pump.fun")
        self.assertNotEqual(p["intent"], "rules_propose")

    def test_preset_intents(self):
        for text in ("create the trenches bot", "make a trenches bot",
                     "create the memecoin trenches bot"):
            p = parse_message(text)
            self.assertEqual(p["intent"], "preset_create", text)

    def test_rules_show_and_clear(self):
        p = parse_message("show rules for Luna")
        self.assertEqual(p["intent"], "rules_show")
        self.assertEqual(p["bot_name"], "Luna")
        p = parse_message("remove rules for Luna")
        self.assertEqual(p["intent"], "rules_clear")
        self.assertEqual(p["bot_name"], "Luna")

    def test_existing_intents_unaffected(self):
        self.assertEqual(
            parse_message("edit bot Luna buy size 25%")["intent"],
            "bot_edit")
        self.assertEqual(
            parse_message("turn on silent trading for Luna")["intent"],
            "silent_on")
        self.assertEqual(parse_message("show my bots")["intent"],
                         "bots_list")


def _entry(mint, price, change_5m=None, change_24h=None, cap=None,
           volume_m5=5000, volume_h1=12000, buys=40, sells=10):
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": None, "change_6h_pct": None,
            "change_24h_pct": change_24h, "liquidity_usd": 1000,
            "market_cap_usd": cap,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": 0,
            "volume_m5_usd": volume_m5, "volume_h1_usd": volume_h1,
            "txns_m5_buys": buys, "txns_m5_sells": sells}


class _FakeScan:
    """Stateful fake trenches feed: swap .scan between iterations."""

    def __init__(self, entries):
        self.entries = list(entries)

    def __call__(self, mints=None):
        prices = {e["mint"]: e for e in self.entries}
        return {"candidates": list(self.entries), "prices": prices,
                "source": "dexscreener", "as_of": "test", "error": None}


def _error_scan(mints=None):
    return {"candidates": [], "prices": {}, "source": "dexscreener",
            "as_of": "test", "error": "feed down"}


def _empty_prices():
    return {"prices": {}, "source": "snapshot", "as_of": "test"}


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


class _FakeFeed:
    """Live-feed stand-in: snapshot() returns the tick per mint."""

    def __init__(self, ticks=None):
        self.ticks = ticks or {}

    def snapshot(self, coin_ids=None):
        out = {}
        for cid in (coin_ids or list(self.ticks)):
            if cid in self.ticks:
                out[cid] = {"coin_id": cid, "price": self.ticks[cid],
                            "source": "test", "stale": False}
        return out

    def tick_age_seconds(self, coin_id, now=None):
        return 0.0 if coin_id in self.ticks else None

    def live_price(self, coin_id, now=None):
        price = self.ticks.get(coin_id)
        return float(price) if price is not None else None

    def live_tick(self, coin_id, now=None):
        if coin_id not in self.ticks:
            return None
        return {"coin_id": coin_id, "price": self.ticks[coin_id],
                "source": "test", "stale": False}

    def change_over(self, coin_id, window_seconds, now=None):
        return None

    def sanitize_prices_data(self, data):
        return data

    def sanitize_trenches(self, data):
        return data

    def set_tick_listener(self, callback):
        self._listener = callback




class RulesEngineBase(unittest.TestCase):
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
                    rules=None, category=TRENCHES_CATEGORY):
        rules_json = json.dumps(rules) if rules is not None else ""
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'bold', '[]', ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, buy_size, stop_loss, mode, silent,
             max_open, hashlib.sha256(b"tok").hexdigest(), category,
             rules_json))
        self.db.commit()
        return cur.lastrowid

    def _insert_position(self, bot_id, coin, amount, avg_price,
                         peak=None, orig=None, stage=0):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at, peak_price, orig_amount, "
            "ladder_stage) VALUES (?, ?, ?, ?, ?, 'paper', 'now', ?, ?, ?)",
            (self.user_id, bot_id, coin, amount, avg_price,
             peak if peak is not None else avg_price,
             orig if orig is not None else amount, stage))
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

    def _seed_gain_mark(self, bot_id, mint, price, change_5m):
        # The loop's own marks, encoding the entry's stated
        # 5-minute change: a mark 330s old at price / (1 + chg).
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint,
             price / (1.0 + (float(change_5m) + 0.05) / 100.0),
             _now(-330)))
        self.db.commit()

    def _run(self, entries, bot_id=None):
        # Single-price source: a scan entry survives the loop's
        # reprice only with a live feed tick at its price. Seed
        # ticks for entries the feed has none for.
        feed = _FakeFeed()
        for _e in entries:
            feed.ticks.setdefault(_e["mint"], _e["price_usd"])
        scan = _FakeScan(entries)
        return run_once(self.db, self.cfg,
                        price_fetcher=_empty_prices,
                        trenches_fetcher=scan, feed=feed)


class TestRulesEngine(RulesEngineBase):
    def test_buy_on_threshold(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "MINTA", 0.01, 6.0)
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=6.0)])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertEqual(outcomes[0]["actions"][0]["coin"], "MINTA")
        self.assertIsNotNone(self._position(bot_id, "MINTA"))
        self.assertTrue([a for a in self._activity("silent_buy")])

    def test_no_buy_below_threshold(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "MINTA", 0.01, 3.0)
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=3.0)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_tiny_market_cap_coin_qualifies(self):
        """No market-cap floor: a $400 coin gaining 3.5% in 5 min buys."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "TINY", 0.001, 6.0)
        outcomes = self._run(
            [_entry("TINY", 0.001, change_5m=6.0, cap=400)])
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertEqual(outcomes[0]["actions"][0]["coin"], "TINY")
        self.assertIsNotNone(self._position(bot_id, "TINY"))

    def test_huge_market_cap_coin_qualifies(self):
        """No market-cap cap: a $500M coin gaining 3.5% in 5 min buys."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "HUGE", 2.5, 6.0)
        outcomes = self._run([_entry("HUGE", 2.5, change_5m=6.0,
                                     cap=500_000_000)])
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertEqual(outcomes[0]["actions"][0]["coin"], "HUGE")
        self.assertIsNotNone(self._position(bot_id, "HUGE"))

    def test_strongest_gainer_wins_regardless_of_cap(self):
        """Ranking reads the 5-minute gain alone: the tiny-cap coin
        gaining 9% beats the huge-cap coin gaining 4%."""
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "HUGE", 2.5, 6.0)
        self._seed_gain_mark(bot_id, "TINY", 0.001, 9.0)
        outcomes = self._run([
            _entry("HUGE", 2.5, change_5m=6.0, cap=500_000_000),
            _entry("TINY", 0.001, change_5m=9.0, cap=400),
        ])
        buys = [a for a in outcomes[0]["actions"]
                if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], "TINY")
        self.assertIsNotNone(self._position(bot_id, "TINY"))
        self.assertIsNone(self._position(bot_id, "HUGE"))

    def test_strongest_gainer_wins_one_position(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "MINTA", 0.01, 3.5)
        self._seed_gain_mark(bot_id, "MINTB", 0.01, 8.0)
        self._seed_gain_mark(bot_id, "MINTC", 0.01, 12.0)
        self._run([
            _entry("MINTA", 0.01, change_5m=3.5),
            _entry("MINTB", 0.01, change_5m=8.0),
            _entry("MINTC", 0.01, change_5m=12.0),
        ])
        positions = self.db.execute(
            "SELECT coin_id FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchall()
        self.assertEqual([r["coin_id"] for r in positions], ["MINTC"])

    def test_sell_ladder_partial_then_rest(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 1000, 0.01)
        # +3% -> sell 10% (100 coins).
        self._run([_entry("MINTA", 0.0103, change_5m=0.0)])
        pos = self._position(bot_id, "MINTA")
        self.assertAlmostEqual(pos["amount"], 900.0)
        self.assertEqual(pos["ladder_stage"], 1)
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, "MINTA")).fetchone()
        self.assertAlmostEqual(holding["amount"], 900.0)
        # +10% -> sell remaining 90% (900 coins), position closed.
        self._reset_scan(bot_id)
        self._run([_entry("MINTA", 0.011, change_5m=0.0)])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertEqual(len(self._activity("silent_sell")), 2)

    def test_trailing_stop_from_peak_not_entry(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        # Bought at 0.01, peaked at 0.02. A fall to 0.0186 is 7% below
        # the peak (but still +86% on entry) — must sell everything.
        self._insert_position(bot_id, "MINTA", 1000, 0.01, peak=0.02)
        self._run([_entry("MINTA", 0.0186, change_5m=0.0)])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertTrue([a for a in self._activity("silent_sell")
                         if "trailing stop" in a["detail"]])

    def test_peak_tracks_upward(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._insert_position(bot_id, "MINTA", 1000, 0.01, peak=0.01)
        self._run([_entry("MINTA", 0.021, change_5m=0.0)])
        pos = self._position(bot_id, "MINTA")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["peak_price"], 0.021)
        # Now 7% below the new peak sells all.
        self._reset_scan(bot_id)
        self._run([_entry("MINTA", 0.0195, change_5m=0.0)])
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_stop_loss_still_enforced(self):
        rules = _trenches_rules()
        rules["trailing_stop_pct"] = 20.0  # looser than the 7% stop loss
        bot_id = self._insert_bot(rules=rules, stop_loss=7)
        self._insert_position(bot_id, "MINTA", 1000, 0.01, peak=0.01)
        self._run([_entry("MINTA", 0.0092, change_5m=0.0)])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertTrue([a for a in self._activity("silent_stop_loss")])

    def test_invalid_rules_make_no_trades(self):
        bad = _trenches_rules()
        bad["sell_ladder"] = [{"pct_of_position": 200, "profit_pct": 3}]
        bot_id = self._insert_bot(rules=bad)
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=50.0)])
        self.assertEqual(outcomes[0]["actions"][0]["reason"],
                         "invalid rules")
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertTrue([a for a in self._activity("silent_skip")
                         if "rules are invalid" in a["detail"]])

    def test_feed_error_makes_no_trades(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        outcomes = run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                            trenches_fetcher=_error_scan)
        self.assertEqual(outcomes[0]["actions"][0]["reason"],
                         "trenches feed unavailable")
        self.assertIsNone(self._position(bot_id, "MINTA"))
        self.assertTrue([a for a in self._activity("silent_skip")
                         if "feed unavailable" in a["detail"]])

    def test_scan_interval_cadence(self):
        bot_id = self._insert_bot(rules=trenches_ruleset())
        self._seed_gain_mark(bot_id, "MINTA", 0.01, 6.0)
        self._run([_entry("MINTA", 0.01, change_5m=6.0)])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))
        # Second iteration inside the 60s interval: no new scan.
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=6.0)])
        self.assertEqual(outcomes[0]["actions"][0]["reason"],
                         "scan interval not reached")

    def test_signals_kind_rules(self):
        rules = {"version": 1, "kind": "signals", "scan_sources": [],
                 "scan_interval_seconds": 60,
                 "buy": {"metric": "gain_24h_pct", "window_seconds": 86400,
                         "threshold_pct": 2.0, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 100,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0}
        bot_id = self._insert_bot(name="Majors", rules=rules, category="")
        self.db.execute("UPDATE bots SET coins = ? WHERE id = ?",
                        (json.dumps([GATE_MINT]), bot_id))
        self.db.commit()
        prices = {"prices": {"SOL": {"price": 100.0, "change_24h": 2.5}},
                  "source": "snapshot", "as_of": "test"}
        # Signals bots price their mints from the trenches feed, not
        # the SOL-only price list: mock the feed's mint prices so
        # the test never hits live DexScreener.
        #
        # The 2-scan confirmation means the first scan only records
        # the signal — run twice to buy. The pre-buy scam gate reads
        # the top-10 share through the RPC, so serve Gate there (a
        # clean coin: 5% top-10, authorities revoked) and seed its
        # metadata so the gate never reaches live network.
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, 'Gate', 'GATE', NULL, "
            "'metaplex', '2026-01-01T00:00:00Z')", (GATE_MINT,))
        self.db.commit()

        class _GateRpc:
            def __call__(self, url):
                return self

            def get_token_largest_accounts(self, mint):
                return [{"address": f"acct{i}", "amount": "5000000",
                         "decimals": 6, "uiAmount": 0.0}
                        for i in range(10)]

            def get_token_supply(self, mint):
                return 1_000_000_000

            def get_mint_authorities(self, mint):
                return (False, False)

            def get_balance_lamports(self, pubkey):
                return 0

        # Signals bots price from the single source now: a live
        # feed tick at the signal price, and the 24-hour change
        # from the loop's own marks — a mark 24.5 hours old at
        # 2.5% below the tick reads +2.5%, above the +2% buy line.
        from tradingbot.livefeed import LiveFeed

        feed = LiveFeed()
        feed.ingest(GATE_MINT, 100.0, "test")
        feed.ingest("SOL", 100.0, "test")
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, GATE_MINT, 100.0 / 1.025, _now(-24 * 3600 - 1800)))
        self.db.commit()
        outcomes = run_once(self.db, self.cfg,
                            price_fetcher=lambda: prices,
                            rpc_factory=_GateRpc(), feed=feed)
        self.assertEqual(len(outcomes[0]["actions"]), 1)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "skip")
        self.assertIn("signal not yet confirmed",
                      outcomes[0]["actions"][0]["reason"])
        self._reset_scan(bot_id)
        outcomes = run_once(self.db, self.cfg,
                            price_fetcher=lambda: prices,
                            rpc_factory=_GateRpc(), feed=feed)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, GATE_MINT))

    def test_legacy_bot_unchanged(self):
        bot_id = self._insert_bot(name="Legacy", rules=None, category="")
        self.db.execute("UPDATE bots SET coins = '[\"SOL\"]' WHERE id = ?",
                        (bot_id,))
        self.db.commit()
        prices = {"prices": {"SOL": {"price": 100.0, "change_24h": 2.5}},
                  "source": "snapshot", "as_of": "test"}
        outcomes = run_once(self.db, self.cfg,
                            price_fetcher=lambda: prices)
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, "SOL"))


class TestRulesRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 500_000_000
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

    def _bot(self, name="Trenches", mode=None):
        if mode is None:
            return self.db.execute(
                "SELECT * FROM bots WHERE user_id = ? AND name = ?",
                (self.user_id, name)).fetchone()
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = ? "
            "AND mode = ?",
            (self.user_id, name, mode)).fetchone()

    def _set_mode(self, mode):
        # Switch the session's trading mode the way the real
        # post-sign-in chooser does (POST /choose-mode): 'paper' for
        # Demo trading, 'devnet' for Real trading.
        token = csrf(self.client, "/bots")
        resp = self.client.post("/choose-mode",
                                data={"mode": mode, "csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        row = self.db.execute(
            "SELECT active_mode FROM users WHERE id = ?",
            (self.user_id,)).fetchone()
        self.assertEqual(row["active_mode"], mode)

    def test_preset_route_creates_trenches(self):
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        bot = self._bot()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["category"], TRENCHES_CATEGORY)
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["silent"], 0)
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules, trenches_ruleset())
        # Second create goes to the existing bot, no duplicate.
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 1)

    def _insert_named_bot(self, name, rules=None):
        # A bot carrying Trenches-kind rules under a given name — the
        # live shape (Luna, bot 3, 2026-11-24) that the old kind-based
        # duplicate check wrongly treated as a Trenches bot.
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'balanced', '[]', 10, 5, 'paper', 1, 3, ?, ?, ?)",
            (self.user_id, name, hashlib.sha256(b"tok").hexdigest(),
             TRENCHES_CATEGORY,
             json.dumps(rules if rules is not None
                        else trenches_ruleset())))
        self.db.commit()
        return cur.lastrowid

    def test_trenches_preset_not_blocked_by_other_trenches_kind_bot(self):
        # A bot with category 'Memecoin build' + rules kind
        # 'trenches' but a different name (Luna) must NOT block
        # creating a Trenches bot.
        luna_id = self._insert_named_bot("Luna")
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        bot = self._bot()
        self.assertIsNotNone(bot)
        self.assertNotEqual(bot["id"], luna_id)
        self.assertEqual(bot["category"], TRENCHES_CATEGORY)
        self.assertEqual(bot["mode"], "paper")
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 2)
        # Lands on the NEW bot, not on Luna.
        self.assertIn(f"/bots/{bot['id']}", resp.headers["Location"])

    def test_trenches_preset_blocked_by_existing_trenches_bot(self):
        # An existing bot actually named 'Trenches' in the category
        # DOES block a duplicate, with the 'already have' flash.
        trenches_id = self._insert_named_bot("Trenches")
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/bots/{trenches_id}", resp.headers["Location"])
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 1)
        followed = self.client.post("/bots/preset/trenches",
                                    data={"csrf_token": token},
                                    follow_redirects=True)
        self.assertIn("You already have a Trenches bot.",
                      followed.get_data(as_text=True))

    def test_delete_confirm_cancel_goes_back_to_bots(self):
        # The Cancel link on the delete-confirm page must go back to
        # the bots list, not to the edit page (2026-11-24: it pointed
        # at bot_edit, so cancelling landed on Edit).
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/trenches",
                         data={"csrf_token": token})
        bot = self._bot()
        resp = self.client.get(f"/bots/{bot['id']}/delete/confirm")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('href="/bots">Cancel — keep the bot', html)
        self.assertNotIn(f'href="/bots/{bot["id"]}/edit">Cancel', html)

    def test_trenches_paper_does_not_block_real(self):
        # A paper Trenches (live shape: bot 9, 2026-11-24) must NOT
        # block creating a Trenches bot in a real session — the
        # duplicate check is scoped to the session's mode.
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        paper_bot = self._bot(mode="paper")
        self.assertIsNotNone(paper_bot)
        self._set_mode("devnet")
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        real_bot = self._bot(mode="real")
        self.assertIsNotNone(real_bot)
        self.assertNotEqual(real_bot["id"], paper_bot["id"])
        self.assertEqual(real_bot["mode"], "real")
        self.assertEqual(real_bot["silent"], 0)
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ? "
            "AND name = 'Trenches'",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 2)
        # Lands on the NEW (real) bot, not on the paper one.
        self.assertIn(f"/bots/{real_bot['id']}", resp.headers["Location"])

    def test_trenches_real_blocks_second_real(self):
        # A real Trenches DOES block a second real one: redirect to
        # the existing bot with the 'already have' flash, count stays 1.
        self._set_mode("devnet")
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        first = self._bot(mode="real")
        self.assertIsNotNone(first)
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/bots/{first['id']}", resp.headers["Location"])
        count = self.db.execute(
            "SELECT COUNT(*) c FROM bots WHERE user_id = ? "
            "AND name = 'Trenches' AND mode = 'real'",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(count, 1)
        followed = self.client.post("/bots/preset/trenches",
                                    data={"csrf_token": token},
                                    follow_redirects=True)
        self.assertIn("You already have a Trenches bot.",
                      followed.get_data(as_text=True))

    def test_trenches_created_in_real_session_is_real(self):
        # A bot created in a real session is a real bot, silent off,
        # with the preset's own buy/stop/max — and no paper twin.
        # The flash says real, not paper.
        self._set_mode("devnet")
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("(Memecoin build, real)",
                      resp.get_data(as_text=True))
        bot = self._bot(mode="real")
        self.assertIsNotNone(bot)
        self.assertEqual(bot["mode"], "real")
        self.assertEqual(bot["silent"], 0)
        self.assertEqual(bot["buy_size_pct"], 20)
        self.assertEqual(bot["stop_loss_pct"], 7)
        self.assertEqual(bot["max_open_trades"], 3)
        self.assertIsNone(self._bot(mode="paper"))

    def test_trenches_created_in_paper_session_is_paper(self):
        # The default session after sign-up is paper (fail-closed):
        # a bot created there is a paper bot, silent off, no real twin.
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/trenches",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        bot = self._bot(mode="paper")
        self.assertIsNotNone(bot)
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["silent"], 0)
        self.assertIsNone(self._bot(mode="real"))

    def test_bots_page_memecoin_section_removed(self):
        # Page 6 rebuild: the Memecoin build section (Trenches
        # description + "+ Create the Trenches bot" button) is gone
        # from the Bots page — the lower scrolling section was
        # removed. The Trenches preset itself is unchanged and is
        # created through the assistant endpoint (see
        # test_assistant_preset_create). (Owner, 2026-11-11: the
        # Page 6 assistant box is deleted, so its "create the
        # trenches bot" hint is gone from the page too.)
        # Trenches returned 2026-11-24 (James) as a preset-window
        # card — so its aria-label "Create the Trenches bot" now
        # appears on the page, but only as that preset card: the
        # old Memecoin build section is still gone.
        resp = self.client.get("/bots")
        html = resp.get_data(as_text=True)
        self.assertNotIn("memecoin-card", html)
        self.assertIn('action="/bots/preset/trenches"', html)
        self.assertIn('aria-label="Create the Trenches bot"', html)
        self.assertNotIn("create the trenches bot", html)

    def test_assistant_preset_create(self):
        data = self._ask("create the trenches bot")
        self.assertIn("Trenches", data["reply"])
        self.assertIn("3.4%", data["reply"])
        self.assertIsNotNone(self._bot())

    def test_assistant_rules_propose_confirm_flow(self):
        self._ask("create bot named Luna bold")
        data = self._ask(
            "bot Luna scan pump.fun and pumpswap, buy when a coin gains "
            "5% in 5 minutes")
        self.assertIn("5.0%", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        # Nothing stored yet — proposal only.
        bot = self._bot("Luna")
        self.assertEqual(bot["rules_json"], "")
        # "no" cancels.
        data = self._ask("no")
        self.assertIsNotNone(data)
        self.assertEqual(self._bot("Luna")["rules_json"], "")
        # Propose again, "yes" applies.
        self._ask("bot Luna scan pump.fun and pumpswap, buy when a coin "
                  "gains 5% in 5 minutes")
        data = self._ask("yes")
        self.assertIn("now follows", data["reply"])
        bot = self._bot("Luna")
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(rules["trailing_stop_pct"], 7.0)  # bot's stop loss

    def test_assistant_rules_show_and_clear(self):
        self._ask("create the trenches bot")
        data = self._ask("show rules for Trenches")
        self.assertIn("3.4%", data["reply"])
        self.assertIn("Trenches", data["reply"])
        data = self._ask("remove rules for Trenches")
        self.assertIn("Reply \"yes\"", data["reply"])
        data = self._ask("yes")
        self.assertIn("standard signals", data["reply"])
        self.assertEqual(self._bot()["rules_json"], "")
        data = self._ask("show rules for Trenches")
        self.assertIn("standard signals", data["reply"])

    def test_rules_clear_route(self):
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/trenches",
                         data={"csrf_token": token})
        bot = self._bot()
        token = csrf(self.client, f"/bots/{bot['id']}")
        resp = self.client.post(f"/bots/{bot['id']}/rules/clear",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._bot()["rules_json"], "")

    def test_bot_detail_shows_rules(self):
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/trenches",
                         data={"csrf_token": token})
        bot = self._bot()
        resp = self.client.get(f"/bots/{bot['id']}")
        html = resp.get_data(as_text=True)
        self.assertIn("Memecoin build", html)
        self.assertIn("3.4%", html)
        self.assertIn("Sell ladder", html)

    def test_bot_create_category_ignored(self):
        # The category picker is gone (builderBot builds bots now):
        # a category posted to /bots/create is ignored - every bot
        # created this way carries no category. The Trenches and
        # Scanner presets live on their own routes.
        token = csrf(self.client, "/bots/create")
        self.client.post("/bots/create", data={
            "csrf_token": token, "name": "Meme1", "strategy": "bold",
            "coins": "SOL", "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper",
            "category": "Memecoin build"})
        self.assertEqual(self._bot("Meme1")["category"], "")
        token = csrf(self.client, "/bots/create")
        self.client.post("/bots/create", data={
            "csrf_token": token, "name": "Meme2", "strategy": "bold",
            "coins": "SOL", "buy_size_pct": "20", "stop_loss_pct": "7",
            "max_open_trades": "3", "mode": "paper",
            "category": "Bogus"})
        self.assertEqual(self._bot("Meme2")["category"], "")


if __name__ == "__main__":
    unittest.main()
