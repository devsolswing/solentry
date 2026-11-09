"""∆scanner∆ sniper strategy: 5% gain in 3 minutes, 30-second scans,
improving-coins tracking, and the top-10 wallets rug sense.

Buy signal: any coin gaining 5% in 3 minutes (strongest gainer wins,
at most one new position per scan), computed from price marks the
loop records itself — the DexScreener feed only provides 5-minute
changes, so a coin with no mark at least 3 minutes old has no
3-minute gain yet and is not bought, never guessed.

Rug sense: the top 10 holders' combined holdings (Solana RPC
getTokenLargestAccounts, public, no key) dropping 10% from the peak
since holding sells everything — the pump.fun sell pays out in SOL,
so the proceeds stay in SOL. An RPC failure skips the watch for that
scan and is logged — never faked, never guessed. The trigger runs
alongside the price-based rug sell (either one fires the exit).
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
from tradingbot.rules import (SCANNER_DISPLAY, load_rules,
                              scanner_ruleset, summarize_rules,
                              trenches_ruleset, validate_ruleset)
from tradingbot.scanner import (check_top10_rug, gain_3m_pct,
                                record_marks, top10_total)
from tradingbot.silent_loop import run_once


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _entry(mint, price, change_5m=None):
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": None, "change_6h_pct": None,
            "change_24h_pct": None, "liquidity_usd": 1000,
            "market_cap_usd": None,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": 0}


class _FakeScan:
    def __init__(self, entries):
        self.entries = list(entries)

    def __call__(self, mints=None):
        prices = {e["mint"]: e for e in self.entries}
        return {"candidates": list(self.entries), "prices": prices,
                "source": "dexscreener", "as_of": "test", "error": None}


def _empty_prices():
    return {"prices": {}, "source": "snapshot", "as_of": "test"}


class _FakeRpc:
    """Solana RPC stand-in: top-10 totals per mint, or a failure."""

    def __init__(self, totals=None, fail=False):
        self.totals = totals or {}
        self.fail = fail

    def get_token_largest_accounts(self, mint):
        if self.fail:
            raise RuntimeError("rpc down")
        if mint not in self.totals:
            raise RuntimeError("mint not served")
        total = self.totals[mint]
        share = total // 10
        return [{"address": f"acct{i}", "amount": str(share),
                 "decimals": 6, "uiAmount": 0.0} for i in range(10)]

    def get_balance_lamports(self, pubkey):
        return 0


class TestScannerValidation(unittest.TestCase):
    def test_preset_shape(self):
        rules = scanner_ruleset()
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["scan_sources"], ["pump.fun", "pumpswap"])
        self.assertEqual(rules["scan_interval_seconds"], 30)
        self.assertEqual(rules["buy"]["metric"], "gain_3m_pct")
        self.assertEqual(rules["buy"]["window_seconds"], 180)
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(rules["sell_ladder"], [])
        self.assertEqual(rules["top10_rug"], {"drop_pct": 10.0})
        self.assertEqual(rules["max_new_positions_per_scan"], 1)
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err)
        self.assertEqual(cleaned, rules)

    def test_scanner_allows_empty_ladder(self):
        cleaned, err = validate_ruleset(scanner_ruleset())
        self.assertIsNone(err)
        self.assertEqual(cleaned["sell_ladder"], [])

    def test_trenches_still_needs_a_ladder(self):
        rules = trenches_ruleset()
        rules["sell_ladder"] = []
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_rejects_scanner_window_other_than_3min(self):
        for window in (300, 3600, 86400):
            rules = scanner_ruleset()
            rules["buy"]["window_seconds"] = window
            rules["buy"]["metric"] = {300: "gain_5m_pct",
                                      3600: "gain_1h_pct",
                                      86400: "gain_24h_pct"}[window]
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, f"window {window} accepted")

    def test_rejects_top10_on_non_scanner(self):
        rules = trenches_ruleset()
        rules["top10_rug"] = {"drop_pct": 10.0}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("scanner", err)

    def test_rejects_bad_top10_drop(self):
        for bad in (0, 0.5, 91, "10"):
            rules = scanner_ruleset()
            rules["top10_rug"] = {"drop_pct": bad}
            _cleaned, err = validate_ruleset(rules)
            self.assertIsNotNone(err, f"drop {bad!r} accepted")

    def test_rejects_unknown_top10_key(self):
        rules = scanner_ruleset()
        rules["top10_rug"] = {"dev_wallet": True}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("Unknown top-10 wallets setting", err)

    def test_rejects_limit_field_in_top10(self):
        rules = scanner_ruleset()
        rules["top10_rug"] = {"buy_size_pct": 50}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        self.assertIn("bot limit", err)

    def test_rejects_scanner_without_sources(self):
        rules = scanner_ruleset()
        rules["scan_sources"] = []
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_rejects_scanner_interval_below_10(self):
        rules = scanner_ruleset()
        rules["scan_interval_seconds"] = 5
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_summarize_includes_scanner_lines(self):
        summary = summarize_rules(scanner_ruleset())
        self.assertIn("every 30 seconds", summary)
        self.assertIn("5.0% in 3 minutes", summary)
        self.assertIn("strongest gainer wins", summary)
        self.assertIn("tracked from recorded prices", summary)
        self.assertIn("Sell ladder: none", summary)
        self.assertIn("Top-10 wallets", summary)
        self.assertIn("10%", summary)
        self.assertIn("proceeds stay in SOL", summary)


class TestScannerMarks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.db = self.app.config["DB"]
        # scanner_marks / scanner_top10 reference bots(id), so the
        # marks tests need a real bot row (id 1).
        signup(self.client, "alice")
        user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Scanner', 'bold', '[]', 20, 7, "
            "'paper', 0, 3, ?)",
            (user_id, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_gain_from_mark_3min_old(self):
        record_marks(self.db, 1, [{"mint": "MINTA", "price_usd": 0.01}],
                     _now(-200))
        now_ts = datetime.now(timezone.utc).timestamp()
        self.assertAlmostEqual(gain_3m_pct(self.db, 1, "MINTA", 0.0106,
                                           now_ts), 6.0, places=4)

    def test_no_gain_without_old_mark(self):
        record_marks(self.db, 1, [{"mint": "MINTA", "price_usd": 0.01}],
                     _now())
        now_ts = datetime.now(timezone.utc).timestamp()
        self.assertIsNone(gain_3m_pct(self.db, 1, "MINTA", 0.0106,
                                      now_ts))

    def test_gain_uses_latest_mark_at_least_3min_old(self):
        record_marks(self.db, 1, [{"mint": "MINTA", "price_usd": 0.01}],
                     _now(-400))
        record_marks(self.db, 1, [{"mint": "MINTA", "price_usd": 0.0103}],
                     _now(-200))
        now_ts = datetime.now(timezone.utc).timestamp()
        # Latest mark >= 3 min old is 0.0103 (-200s), not 0.01.
        self.assertAlmostEqual(gain_3m_pct(self.db, 1, "MINTA", 0.0106,
                                           now_ts), 2.9126, places=3)

    def test_top10_total_sums_first_10(self):
        client = _FakeRpc(totals={"MINTA": 1_000_000})
        self.assertEqual(top10_total(client, "MINTA"), 1_000_000)

    def test_top10_total_none_on_rpc_failure(self):
        client = _FakeRpc(fail=True)
        self.assertIsNone(top10_total(client, "MINTA"))

    def test_top10_total_none_when_mint_not_served(self):
        client = _FakeRpc(totals={})
        self.assertIsNone(top10_total(client, "MINTA"))

    def test_top10_rug_baseline_then_fires_at_10pct(self):
        fired, reason = check_top10_rug(self.db, 1, "MINTA", 1_000_000,
                                        10.0, _now())
        self.assertFalse(fired)
        self.assertIsNone(reason)
        fired, reason = check_top10_rug(self.db, 1, "MINTA", 900_000,
                                        10.0, _now())
        self.assertTrue(fired)
        self.assertIn("top-10 wallets dumped", reason)
        self.assertIn("10.0%", reason)

    def test_top10_rug_no_fire_below_threshold(self):
        check_top10_rug(self.db, 1, "MINTA", 1_000_000, 10.0, _now())
        fired, _reason = check_top10_rug(self.db, 1, "MINTA", 910_000,
                                         10.0, _now())
        self.assertFalse(fired)

    def test_top10_rug_baseline_tracks_peak(self):
        check_top10_rug(self.db, 1, "MINTA", 1_000_000, 10.0, _now())
        fired, _reason = check_top10_rug(self.db, 1, "MINTA", 1_100_000,
                                         10.0, _now())
        self.assertFalse(fired)
        # 990k is only 1% below the original baseline but 10% below
        # the 1.1M peak — the peak is the baseline, so it fires.
        fired, _reason = check_top10_rug(self.db, 1, "MINTA", 990_000,
                                         10.0, _now())
        self.assertTrue(fired)


class TestScannerParser(unittest.TestCase):
    def test_create_scanner_bot(self):
        parsed = parse_message("create a scanner bot")
        self.assertEqual(parsed["intent"], "preset_create")
        self.assertEqual(parsed["preset"], "scanner")

    def test_make_scanner_bot_top_wallets(self):
        parsed = parse_message(
            "make a scanner bot that sells if the top wallets dump")
        self.assertEqual(parsed["intent"], "preset_create")
        self.assertEqual(parsed["preset"], "scanner")

    def test_create_sniper_bot(self):
        parsed = parse_message("create a sniper bot")
        self.assertEqual(parsed["intent"], "preset_create")
        self.assertEqual(parsed["preset"], "scanner")

    def test_snipe_5pct_3min(self):
        parsed = parse_message("snipe coins gaining 5% in 3 minutes")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertIsNone(parsed["bot_name"])
        rules = parsed["rules"]
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(rules["buy"]["window_seconds"], 180)
        self.assertEqual(rules["scan_interval_seconds"], 30)
        self.assertEqual(rules["sell_ladder"], [])
        self.assertEqual(rules["scan_sources"], ["pump.fun", "pumpswap"])

    def test_snipe_window_by_phrasing(self):
        parsed = parse_message(
            "snipe coins gaining in a 3 min window by 5%")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["rules"]["kind"], "scanner")
        self.assertEqual(parsed["rules"]["buy"]["threshold_pct"], 5.0)

    def test_scanner_rules_for_named_bot(self):
        parsed = parse_message(
            "bot Luna scan pump.fun and pumpswap, buy when a coin "
            "gains 5% in 3 minutes, sell if the top wallets dump")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["bot_name"], "Luna")
        rules = parsed["rules"]
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["top10_rug"], {"drop_pct": 10.0})

    def test_scanner_top10_custom_drop(self):
        parsed = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "3 minutes, sell if the top 10 wallets drop 12%")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["rules"]["top10_rug"],
                         {"drop_pct": 12.0})

    def test_scanner_explicit_30s_interval(self):
        parsed = parse_message(
            "bot Luna scan pump.fun every 30 seconds, buy when a coin "
            "gains 5% in 3 minutes")
        self.assertEqual(parsed["rules"]["scan_interval_seconds"], 30)

    def test_unsupported_2min_window(self):
        parsed = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "2 minutes")
        self.assertEqual(parsed["intent"], "rules_unsupported")
        self.assertIn("3-minute", parsed["reply"])

    def test_trenches_still_parses(self):
        parsed = parse_message(
            "bot Luna scan pump.fun, buy when a coin gains 5% in "
            "5 minutes")
        self.assertEqual(parsed["intent"], "rules_propose")
        self.assertEqual(parsed["rules"]["kind"], "trenches")


class ScannerEngineBase(unittest.TestCase):
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
        self.fake_rpc = _FakeRpc()

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _insert_bot(self, name="Scanner", mode="paper", silent=1,
                    buy_size=20, stop_loss=7, max_open=3, rules=None,
                    category="scanner"):
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

    def _insert_mark(self, bot_id, mint, price, when):
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, when))
        self.db.commit()

    def _reset_scan(self, bot_id):
        self.db.execute("UPDATE bots SET last_scan_at = '' WHERE id = ?",
                        (bot_id,))
        self.db.commit()

    def _set_scan_at(self, bot_id, when):
        self.db.execute("UPDATE bots SET last_scan_at = ? WHERE id = ?",
                        (when, bot_id))
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

    def _run(self, entries, real_executor=None):
        scan = _FakeScan(entries)
        return run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                        trenches_fetcher=scan,
                        rpc_factory=lambda url: self.fake_rpc,
                        real_executor=real_executor)


class TestScannerEngine(ScannerEngineBase):
    def test_buy_fires_on_5pct_3min_gain(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, "MINTA", 0.01, _now(-200))
        outcomes = self._run([_entry("MINTA", 0.0106)])
        buys = [a for a in outcomes[0]["actions"] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], "MINTA")
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_no_buy_below_threshold(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, "MINTA", 0.01, _now(-200))
        outcomes = self._run([_entry("MINTA", 0.0104)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_no_buy_without_old_mark(self):
        """A coin first seen this scan has no 3-minute gain yet —
        the mark recorded this scan is too fresh to qualify."""
        bot_id = self._insert_bot(rules=scanner_ruleset())
        outcomes = self._run([_entry("MINTA", 0.0106)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNone(self._position(bot_id, "MINTA"))

    def test_strongest_gainer_wins(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, "MINTA", 0.01, _now(-200))
        self._insert_mark(bot_id, "MINTB", 0.01, _now(-200))
        outcomes = self._run([_entry("MINTA", 0.0106),
                              _entry("MINTB", 0.0110)])
        buys = [a for a in outcomes[0]["actions"] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], "MINTB")

    def test_max_one_new_position_per_scan(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, "MINTA", 0.01, _now(-200))
        self._insert_mark(bot_id, "MINTB", 0.01, _now(-200))
        outcomes = self._run([_entry("MINTA", 0.0106),
                              _entry("MINTB", 0.0106)])
        buys = [a for a in outcomes[0]["actions"] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)

    def test_improving_coins_tracked_across_scans(self):
        """A coin gaining 3% first (no buy), then 6% over the same
        3-minute window, is bought on the later scan — the marks
        keep its history."""
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, "MINTA", 0.01, _now(-200))
        outcomes = self._run([_entry("MINTA", 0.0103)])
        self.assertEqual(outcomes[0]["actions"], [])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry("MINTA", 0.0106)])
        buys = [a for a in outcomes[0]["actions"] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], "MINTA")

    def test_cadence_30s_per_bot(self):
        """The scanner re-evaluates 31 seconds later; a Trenches bot
        (60s) in the same loop still waits — the 30s cadence is
        per-bot, not global."""
        scanner_id = self._insert_bot(rules=scanner_ruleset())
        trenches_id = self._insert_bot(name="Trenches",
                                       rules=trenches_ruleset(),
                                       category="Memecoin build")
        self._run([_entry("MINTA", 0.01, change_5m=3.5)])
        # Immediately again: both skip on their own interval.
        outcomes = self._run([_entry("MINTA", 0.01, change_5m=3.5)])
        by_bot = {o["bot_id"]: o["actions"] for o in outcomes}
        self.assertEqual(by_bot[scanner_id],
                         [{"action": "skip", "coin": "",
                           "reason": "scan interval not reached"}])
        self.assertEqual(by_bot[trenches_id],
                         [{"action": "skip", "coin": "",
                           "reason": "scan interval not reached"}])
        # 31 seconds later: the scanner evaluates, Trenches waits.
        self._insert_mark(scanner_id, "MINTB", 0.01, _now(-200))
        self._set_scan_at(scanner_id, _now(-31))
        self._set_scan_at(trenches_id, _now(-31))
        outcomes = self._run([_entry("MINTB", 0.0106, change_5m=3.5)])
        by_bot = {o["bot_id"]: o["actions"] for o in outcomes}
        buys = [a for a in by_bot[scanner_id] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], "MINTB")
        self.assertEqual(by_bot[trenches_id],
                         [{"action": "skip", "coin": "",
                           "reason": "scan interval not reached"}])

    def test_top10_baseline_then_no_fire_at_9pct(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)])
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 910_000}
        outcomes = self._run([_entry("MINTA", 0.01)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_top10_drop_10pct_sells_all(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)])
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 900_000}
        outcomes = self._run([_entry("MINTA", 0.01)])
        sells = [a for a in outcomes[0]["actions"]
                 if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("top-10 wallets dumped", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        detail = self._activity("silent_sell")[0]["detail"]
        self.assertIn("top-10 wallets dumped", detail)

    def test_top10_baseline_tracks_peak(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)])
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 1_100_000}
        self._run([_entry("MINTA", 0.01)])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 990_000}
        outcomes = self._run([_entry("MINTA", 0.01)])
        sells = [a for a in outcomes[0]["actions"]
                 if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("top-10 wallets dumped", sells[0]["reason"])

    def test_top10_rpc_failure_skips_watch(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.fail = True
        outcomes = self._run([_entry("MINTA", 0.01)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))
        skips = [a for a in self._activity("silent_skip")
                 if "top-10 wallet watch unavailable" in a["detail"]]
        self.assertEqual(len(skips), 1)

    def test_top10_mint_not_served_skips_watch(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {}
        outcomes = self._run([_entry("MINTA", 0.01)])
        self.assertEqual(outcomes[0]["actions"], [])
        self.assertIsNotNone(self._position(bot_id, "MINTA"))

    def test_rug_sell_fires_before_top10(self):
        """Both triggers fire on the same scan — the fast-drop rug
        sell is checked first, so it is the reason."""
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)])
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 800_000}
        outcomes = self._run([_entry("MINTA", 0.006, change_5m=-40.0)])
        sells = [a for a in outcomes[0]["actions"]
                 if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("rug sell", sells[0]["reason"])

    def test_top10_watch_cleared_on_close(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)])
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 900_000}
        self._run([_entry("MINTA", 0.01)])
        self.assertIsNone(self._position(bot_id, "MINTA"))
        top10 = self.db.execute(
            "SELECT * FROM scanner_top10 WHERE bot_id = ? AND mint = ?",
            (bot_id, "MINTA")).fetchone()
        self.assertIsNone(top10)

    def test_top10_real_mode_proceeds_in_sol(self):
        """Real mode: the sell reason says the proceeds are in SOL —
        the pump.fun sell pays out in SOL by construction."""
        bot_id = self._insert_bot(rules=scanner_ruleset(), mode="real")

        def fake_real(bot, user_id, coin, side, amount, price, db, cfg):
            return {"status": "executed",
                    "signature": "sig1234567890abcdef",
                    "signing_bot": "test-bot"}

        self._insert_position(bot_id, "MINTA", 100, 0.01)
        self.fake_rpc.totals = {"MINTA": 1_000_000}
        self._run([_entry("MINTA", 0.01)], real_executor=fake_real)
        self._reset_scan(bot_id)
        self.fake_rpc.totals = {"MINTA": 900_000}
        outcomes = self._run([_entry("MINTA", 0.01)],
                             real_executor=fake_real)
        sells = [a for a in outcomes[0]["actions"]
                 if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("top-10 wallets dumped", sells[0]["reason"])
        self.assertIn("proceeds in SOL", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, "MINTA"))


class TestScannerRoutes(unittest.TestCase):
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

    def _bots(self, category="scanner"):
        return self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND category = ?",
            (self.user_id, category)).fetchall()

    def test_preset_route_creates_scanner(self):
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/scanner",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        bots = self._bots()
        self.assertEqual(len(bots), 1)
        bot = bots[0]
        self.assertEqual(bot["name"], "Scanner")
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["silent"], 0)
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules, scanner_ruleset())

    def test_preset_route_duplicate_guard(self):
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/scanner",
                         data={"csrf_token": token}, follow_redirects=False)
        token = csrf(self.client, "/bots")
        resp = self.client.post("/bots/preset/scanner",
                                data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._bots()), 1)

    def test_bots_page_has_no_standalone_scanner_card(self):
        # The scanner lives in Edit Bots under the Memecoin build
        # category now — its standalone card on the Bots page is
        # gone, preset route and agent flow unchanged.
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/scanner",
                         data={"csrf_token": token}, follow_redirects=False)
        # First load consumes the creation flash (which names the
        # scanner); the page itself must not carry the card.
        self.client.get("/bots")
        resp = self.client.get("/bots")
        html = resp.get_data(as_text=True)
        self.assertNotIn("Create the Scanner bot", html)
        self.assertNotIn(SCANNER_DISPLAY, html)
        # The scanner bot itself still lists with the other bots.
        self.assertIn("Scanner", html)

    def test_edit_form_offers_scanner_category(self):
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/scanner",
                         data={"csrf_token": token}, follow_redirects=False)
        bot = self._bots()[0]
        # Consume the creation flash so only the form can put the
        # scanner name on the edit page.
        self.client.get("/bots")
        resp = self.client.get(f"/bots/{bot['id']}/edit")
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('value="scanner"', html)
        self.assertIn(SCANNER_DISPLAY, html)
        self.assertIn("Memecoin build", html)

    def test_agent_create_scanner_needs_yes(self):
        data = self._ask("create a scanner bot")
        self.assertIn("Scanner bot", data["reply"])
        self.assertIn("5.0% in 3 minutes", data["reply"])
        self.assertIn("Top-10 wallets", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        self.assertEqual(len(self._bots()), 0)
        data = self._ask("yes")
        self.assertIn("Done", data["reply"])
        bots = self._bots()
        self.assertEqual(len(bots), 1)
        rules, err = load_rules(bots[0])
        self.assertIsNone(err)
        self.assertEqual(rules, scanner_ruleset())

    def test_agent_create_scanner_duplicate(self):
        self._ask("create a scanner bot")
        self._ask("yes")
        data = self._ask("create a scanner bot")
        self.assertIn("already have a Scanner bot", data["reply"])
        self.assertEqual(len(self._bots()), 1)

    def test_agent_snipe_creates_scanner(self):
        data = self._ask("snipe coins gaining 5% in 3 minutes")
        self.assertIn("Scanner bot", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        self.assertEqual(len(self._bots()), 0)
        data = self._ask("yes")
        self.assertIn("Done", data["reply"])
        self.assertEqual(len(self._bots()), 1)

    def test_agent_snipe_no_means_no_bot(self):
        self._ask("snipe coins gaining 5% in 3 minutes")
        data = self._ask("no")
        self.assertIn("Cancelled", data["reply"])
        self.assertEqual(len(self._bots()), 0)

    def test_agent_scanner_rules_for_named_bot(self):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Luna', 'bold', '[\"SOL\"]', "
            "20, 7, 'paper', 0, 3, ?)",
            (self.user_id, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        bot_id = cur.lastrowid
        data = self._ask("bot Luna scan pump.fun, buy when a coin "
                         "gains 5% in 3 minutes, sell if the top "
                         "wallets dump")
        self.assertIn("5.0% in 3 minutes", data["reply"])
        self.assertIn("Top-10 wallets", data["reply"])
        self.assertIn("Reply \"yes\"", data["reply"])
        data = self._ask("yes")
        self.assertIn("now follows", data["reply"])
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        rules, err = load_rules(bot)
        self.assertIsNone(err)
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["top10_rug"], {"drop_pct": 10.0})


if __name__ == "__main__":
    unittest.main()
