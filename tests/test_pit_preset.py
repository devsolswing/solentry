"""Pit Sweeper preset (2026-11-23): James's spec — fast in/out
real-money sniper, top-5 watch list, +6.3% in 5 minutes, sell all
at +12%, stop loss 4.9%, shared brain that rewrites its own rules
from missed coins, versioned (1.0, 1.1, 1.2...).

Every gate, exit, preset default, Page 6 button, versioning step
and the devnet carve-out is tested here, on the same fake-RPC /
fake-feed / fake-scan harness as the Gnat tests. No live network
in these tests: the fake RPC serves the bonding-curve account (at
a configured progress) and the Metaplex metadata account (with a
configured URI) for the test mint, plus the tuning-test top-10 /
authority surface.

The honest map lives in rules.PIT_NOTES and the staging MANIFEST:
the 5-second scan, congestion and rug-time data, sub-5-minute
flow, unique-wallet counts and creator/bundler checks are NOT
enforced — the app has no data source for them. These tests assert
the closest versions that ARE enforced, and that the not-enforced
rules are never faked.
"""

import hashlib
import json
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
import tradingbot.silent_loop as sl
from helpers import csrf, grant_fuel, make_client, make_config, signup
from tradingbot.builderbot import review_bot
from tradingbot.coin_meta import metadata_pda
from tradingbot.ledger import record_trade, version_wl_summary
from tradingbot.pit import (current_pit_version, ensure_pit_version_1_0,
                            maybe_adopt_pit_version, pit_changelog,
                            pit_review_missed_coins,
                            publish_pit_version, record_pit_missed_coin,
                            rollback_pit_version, update_pit_watchlist)
from tradingbot.pumpfun import bonding_curve_pda, curve_progress_pct
from tradingbot.rules import (PIT_NOTES, pit_ruleset, preset_bots,
                              summarize_rules, validate_ruleset)
from tradingbot.silent_loop import (_pit_buy_halt, _pit_debounced_exit,
                                    _pit_entry_gates, _pit_flat_fires,
                                    _pit_hard_exit_reason,
                                    _pit_pullback_fires, run_once)

# Valid base58 addresses (no "I"/"O"/"l"/"0") — the curve PDA
# derivation decodes the mint, so the tuning tests' MINTA ("I")
# cannot be used here.
PIT_MINT = "C111111111111111111111111111111111111111111"
PIT_MINT2 = "D111111111111111111111111111111111111111111"
PIT_MINT3 = "E111111111111111111111111111111111111111111"


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _entry(mint, price, change_5m=6.3, change_1h=25.0,
           volume_m5=5000, volume_h1=12000, buys=40, sells=10,
           mcap=15000):
    """A realistic gaining feed entry in the Pit band: +6.3% in
    5 minutes, $15k mcap, volume rising (5x the hourly pace),
    buys 4x sells."""
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": change_1h, "change_6h_pct": None,
            "change_24h_pct": None, "liquidity_usd": 1000,
            "market_cap_usd": mcap,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": 0,
            "volume_m5_usd": volume_m5, "volume_h1_usd": volume_h1,
            "txns_m5_buys": buys, "txns_m5_sells": sells}


def _pit_entry(mint, price, devnet_watchlist=False,
               curve_real_sol=None, **over):
    """A Pit candidate: the base gaining entry plus (for devnet
    test coins) the watchlist flag + curve SOL."""
    entry = _entry(mint, price, **over)
    if devnet_watchlist:
        entry["devnet_watchlist"] = True
    if curve_real_sol is not None:
        entry["curve_real_sol"] = curve_real_sol
    return entry


def _curve_bytes(progress_pct):
    """49-byte bonding-curve account at `progress_pct`% filled:
    discriminator (8), virtual token reserves, virtual SOL (30),
    real token (0), real SOL (0), total supply, complete=False."""
    virtual = int(round((1.0 - progress_pct / 100.0)
                        * 1_073_000_000_000_000))
    return (b"\x00" * 8
            + struct.pack("<Q", virtual)
            + struct.pack("<Q", 30_000_000_000)
            + struct.pack("<Q", 0)
            + struct.pack("<Q", 0)
            + struct.pack("<Q", 1_000_000_000_000_000)
            + b"\x00")


def _borsh_str(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _metadata_bytes(uri="https://example.com/meta.json"):
    """Metaplex metadata account bytes: key (1), update authority
    (32), mint (32), name, symbol, uri as Borsh strings."""
    return (b"\x04" + b"\x00" * 32 + b"\x00" * 32
            + _borsh_str("Pit Coin") + _borsh_str("PIT")
            + _borsh_str(uri))


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
    """Solana RPC stand-in: the tuning-test top-10 / authority
    surface, plus the curve + metadata account reads the Pit
    gates need. `progress_pct` is the bonding-curve fill for the
    test mint; `uri` is the metadata URI it carries."""

    def __init__(self, mint, progress_pct=30.0,
                 uri="https://example.com/meta.json"):
        self.mint = mint
        self.progress_pct = progress_pct
        self.uri = uri
        self.curve_addr, _ = bonding_curve_pda(mint)
        self.meta_addr = metadata_pda(mint)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self

    def get_account_info(self, pubkey):
        if pubkey == self.curve_addr:
            return {"data": _curve_bytes(self.progress_pct)}
        if pubkey == self.meta_addr:
            return {"data": _metadata_bytes(self.uri)}
        return None

    def get_token_largest_accounts(self, mint):
        total = 50_000_000
        share = total // 10
        return [{"address": f"acct{i}", "amount": str(share),
                 "decimals": 6, "uiAmount": 0.0} for i in range(10)]

    def get_token_supply(self, mint):
        return 1_000_000_000

    def get_mint_authorities(self, mint):
        return (False, False)

    def get_balance_lamports(self, pubkey):
        return 0


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

    def sanitize_prices_data(self, data):
        return data

    def sanitize_trenches(self, data):
        return data

    def set_tick_listener(self, callback):
        self._listener = callback


class PitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        self.rpc_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        # A clean coin: top-10 5% of supply, authorities revoked,
        # metadata on file, curve 30% filled — passes every gate.
        self.fake_rpc = _FakeRpc(PIT_MINT)
        for mint in (PIT_MINT, PIT_MINT2, PIT_MINT3):
            self._seed_meta(mint)
        # The loop reads the top-10 / curve / metadata through the
        # injected rpc_factory, and the scam gate through the same
        # fake — one fake serves every RPC read in these tests.
        sl._SELL_IN_FLIGHT.clear()
        sl._TOP10_SKIP_LOGGED_AT.clear()
        with sl._TICK_DIRTY_LOCK:
            sl._TICK_DIRTY_MINTS.clear()
        sl._TICK_EVENT.clear()

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _seed_meta(self, mint, name="Pit Coin", symbol="PIT"):
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, ?, ?, NULL, 'metaplex', ?) "
            "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
            "symbol = excluded.symbol",
            (mint, name, symbol, _now()))
        self.db.commit()

    def _insert_bot(self, rules, name="Pit Sweeper v1.0", mode="paper",
                    silent=1, coins=None, buy_size=25, stop_loss=4.9,
                    max_open=1, preset_version="1.0",
                    bankroll_sol=0.20):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, rules_json, preset_version, bankroll_sol) "
            "VALUES (?, ?, 'bold', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, json.dumps(coins or []), buy_size,
             stop_loss, mode, silent, max_open,
             hashlib.sha256(b"tok").hexdigest(),
             json.dumps(rules), preset_version, bankroll_sol))
        self.db.commit()
        return cur.lastrowid

    def _insert_position(self, bot_id, coin, amount, avg_price,
                         peak=None, orig=None, stage=0, opened_at=None,
                         last_price=None, last_price_at=None):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at, peak_price, orig_amount, "
            "ladder_stage, last_price, last_price_at) "
            "VALUES (?, ?, ?, ?, ?, 'paper', ?, ?, ?, ?, ?, ?)",
            (self.user_id, bot_id, coin, amount, avg_price,
             opened_at or _now(),
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

    def _position(self, bot_id, coin):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, coin)).fetchone()

    def _run(self, entries, feed=None, executor=None):
        scan = _FakeScan(entries)
        return run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                        trenches_fetcher=scan,
                        rpc_factory=self.fake_rpc,
                        real_executor=executor, feed=feed)

    def _buys(self, outcomes):
        return [a for o in outcomes for a in o["actions"]
                if a["action"] == "buy"]

    def _sells(self, outcomes):
        return [a for o in outcomes for a in o["actions"]
                if a["action"] == "sell"]

    def _buy_twice(self, bot_id, entry, feed=None):
        """Run two scans (2-scan confirmation) and return the
        outcomes of the second scan."""
        self._insert_mark(bot_id, entry["mint"], 0.01, _now(-200))
        self._run([entry], feed=feed)
        self._reset_scan(bot_id)
        return self._run([entry], feed=feed)

    def _stub_executor(self):
        """A real executor that always 'executes': the loop's
        real-mode sells need an executor result, and the default
        would try a real swap. The stub stands in for the signing
        bot — no chain, no keys."""
        return mock.Mock(return_value={
            "status": "executed", "signature": "sig-pit-test",
            "coin_amount": 500.0, "sol_spent": 0.02,
            "sol_received": 0.02, "signing_bot": "test"})


class TestPitRuleset(PitBase):
    def test_ruleset_validates(self):
        cleaned, err = validate_ruleset(pit_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["kind"], "pit")
        # 10-second cadence — the live loop floor, never 5s.
        self.assertEqual(cleaned["scan_interval_seconds"], 10)
        self.assertEqual(cleaned["buy"]["threshold_pct"], 6.3)
        self.assertEqual(cleaned["buy"]["window_seconds"], 300)
        # Mcap band $9k-$25k, enforced by the buy filter.
        self.assertEqual(cleaned["buy_filter"]["min_market_cap_usd"],
                         9000.0)
        self.assertEqual(cleaned["buy_filter"]["max_market_cap_usd"],
                         25000.0)
        # Sell all at +12%: one rung, 100% of the position.
        self.assertEqual(cleaned["sell_ladder"],
                         [{"profit_pct": 12.0, "pct_of_position": 100.0}])
        # The versioned 'pit' block carries every Pit threshold.
        pit = cleaned["pit"]
        self.assertEqual(pit["stop_trigger_pct"], 4.9)
        self.assertEqual(pit["breakeven_trigger_pct"], 6.0)
        self.assertEqual(pit["pullback_pct"], 2.0)
        self.assertEqual(pit["flat_pct"], 0.5)
        self.assertEqual(pit["max_hold_seconds"], 600)
        self.assertEqual(pit["daily_loss_sol"], 0.025)
        self.assertEqual(pit["buy_ratio_min"], 2.0)
        self.assertEqual(pit["curve_min_pct"], 15.0)
        self.assertEqual(pit["curve_max_pct"], 65.0)
        self.assertEqual(pit["buy_size_sol"], 0.05)
        self.assertEqual(pit["bankroll_sol"], 0.20)
        self.assertEqual(pit["min_top5_avg_buys_5m"], 10)
        # Max 1 open is a bot limit, not a rule — it lives on the
        # bot row, not in the cleaned rule set (asserted by the
        # preset-route test instead).

    def test_preset_bots_order(self):
        names = [p["name"] for p in preset_bots()]
        self.assertEqual(names, ["Clyde", "Trenches", "Scanner",
                                 "Gnat", "Pit Sweeper", "Middy"])
        pit = next(p for p in preset_bots() if p["key"] == "pit")
        self.assertEqual(pit["endpoint"], "bot_preset_pit")
        self.assertEqual(preset_bots()[-1]["key"], "middy")

    def test_summary_states_the_worst_case(self):
        # The summary never promises "never lose more than the
        # stop" — it states the trigger AND the worst case.
        text = summarize_rules(pit_ruleset())
        self.assertIn("0.05 SOL", text)
        self.assertIn("4.9%", text)
        self.assertIn("+12%", text)
        self.assertIn("9.9%", text)

    def test_notes_are_honest(self):
        joined = "\n".join(PIT_NOTES)
        self.assertIn("NOT enforced", joined)
        self.assertIn("closest version", joined)
        self.assertIn("5-second", joined)


class TestPitHardExits(PitBase):
    def test_stop_fires_at_minus_4_9(self):
        # Exactly -4.9% fires; -4.8% does not.
        self.assertTrue(_pit_hard_exit_reason(0.01, 0.01, 0.00951,
                                              4.9, 6.0))
        self.assertEqual(_pit_hard_exit_reason(0.01, 0.01, 0.00952,
                                               4.9, 6.0), "")

    def test_stop_is_a_trigger_not_a_promise(self):
        # The reason says the worst case out loud.
        reason = _pit_hard_exit_reason(0.01, 0.01, 0.0095, 4.9, 6.0)
        self.assertIn("worst case", reason)
        self.assertIn("9.9%", reason)

    def test_breakeven_armed_at_plus_6(self):
        # Peak +6% (0.0106), price back to the entry: sell at cost.
        self.assertTrue(_pit_hard_exit_reason(0.01, 0.0106, 0.01,
                                              4.9, 6.0))
        # Price still above the entry: no exit.
        self.assertEqual(_pit_hard_exit_reason(0.01, 0.0106, 0.0101,
                                               4.9, 6.0), "")
        # Peak below +6%: the breakeven never arms.
        self.assertEqual(_pit_hard_exit_reason(0.01, 0.0105, 0.01,
                                               4.9, 6.0), "")

    def test_stop_beats_breakeven(self):
        # A crash straight through the breakeven floor to -5% is
        # still a stop loss, not a breakeven sell.
        reason = _pit_hard_exit_reason(0.01, 0.0106, 0.0095, 4.9, 6.0)
        self.assertIn("stop loss", reason)

    def test_no_exit_while_rising(self):
        self.assertEqual(_pit_hard_exit_reason(0.01, 0.011, 0.011,
                                               4.9, 6.0), "")


class TestPitPullbackAndFlat(PitBase):
    def test_pullback_fires_at_minus_2(self):
        settings = pit_ruleset()["pit"]
        self.assertTrue(_pit_pullback_fires({"change_5m_pct": -2.0},
                                            settings))
        self.assertFalse(_pit_pullback_fires({"change_5m_pct": -1.9},
                                             settings))

    def test_pullback_missing_data_never_fires(self):
        settings = pit_ruleset()["pit"]
        self.assertFalse(_pit_pullback_fires({}, settings))
        self.assertFalse(_pit_pullback_fires({"change_5m_pct": None},
                                             settings))

    def test_flat_fires_inside_half_a_percent(self):
        settings = pit_ruleset()["pit"]
        self.assertTrue(_pit_flat_fires({"change_5m_pct": 0.3}, settings))
        self.assertTrue(_pit_flat_fires({"change_5m_pct": -0.5},
                                        settings))
        self.assertFalse(_pit_flat_fires({"change_5m_pct": 0.6},
                                         settings))
        self.assertFalse(_pit_flat_fires({"change_5m_pct": -2.0},
                                         settings))

    def test_flat_missing_data_never_fires(self):
        settings = pit_ruleset()["pit"]
        self.assertFalse(_pit_flat_fires({}, settings))


class TestPitDebouncedExit(PitBase):
    def test_two_consecutive_ticks_fire(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))
        self.assertTrue(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))

    def test_a_clearing_tick_resets_the_votes(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", False))
        # One vote again after the reset — not two.
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))

    def test_kinds_do_not_share_votes(self):
        # A pullback vote can never be spent on a flat exit: the
        # flat exit needs two flat ticks of its own, even after a
        # pullback vote was already banked.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "flat", True))
        self.assertTrue(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "flat", True))

    def test_missing_position_never_fires(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self.assertFalse(_pit_debounced_exit(
            self.db, bot_id, PIT_MINT, "pullback", True))


class TestPitEntryGates(PitBase):
    def test_clean_coin_passes(self):
        ok, why = _pit_entry_gates(
            self.db, self.cfg, _pit_entry(PIT_MINT, 0.01),
            self.fake_rpc, pit_ruleset()["pit"])
        self.assertTrue(ok, why)

    def test_buys_below_2x_sells_fail(self):
        ok, why = _pit_entry_gates(
            self.db, self.cfg,
            _pit_entry(PIT_MINT, 0.01, buys=15, sells=10),
            self.fake_rpc, pit_ruleset()["pit"])
        self.assertFalse(ok)
        self.assertIn("2 to 1", why)

    def test_no_txn_counts_fail(self):
        ok, why = _pit_entry_gates(
            self.db, self.cfg,
            _pit_entry(PIT_MINT, 0.01, buys=None, sells=None),
            self.fake_rpc, pit_ruleset()["pit"])
        self.assertFalse(ok)

    def test_curve_below_15pct_fails(self):
        rpc = _FakeRpc(PIT_MINT, progress_pct=10.0)
        ok, why = _pit_entry_gates(
            self.db, self.cfg, _pit_entry(PIT_MINT, 0.01), rpc,
            pit_ruleset()["pit"])
        self.assertFalse(ok)
        self.assertIn("15-65%", why)

    def test_curve_above_65pct_fails(self):
        rpc = _FakeRpc(PIT_MINT, progress_pct=70.0)
        ok, why = _pit_entry_gates(
            self.db, self.cfg, _pit_entry(PIT_MINT, 0.01), rpc,
            pit_ruleset()["pit"])
        self.assertFalse(ok)

    def test_no_rpc_fails_closed(self):
        ok, why = _pit_entry_gates(
            self.db, self.cfg, _pit_entry(PIT_MINT, 0.01), None,
            pit_ruleset()["pit"])
        self.assertFalse(ok)


class TestPitLoopBuys(PitBase):
    def test_second_scan_buys_at_fixed_005_sol(self):
        # Live SOL $71.50: 0.05 SOL = $3.58 — not 25% of the $100
        # bankroll ($25.00). The fixed size is the point.
        bot_id = self._insert_bot(rules=pit_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106), feed=feed)
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["usd"], 3.575, places=2)
        self.assertIsNotNone(self._position(bot_id, PIT_MINT))
        detail = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ? AND kind = "
            "'silent_buy' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()["detail"]
        self.assertIn("0.05 SOL size", detail)

    def test_no_live_sol_price_falls_back_to_pct_and_says_so(self):
        # No feed, snapshot prices only: the 0.05 SOL conversion
        # cannot run, so the loop falls back to 25% of the $100
        # bankroll ($25.00) and labels the fallback on the buy line.
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(bot_id, _pit_entry(PIT_MINT, 0.0106))
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["usd"], 25.0, places=2)
        detail = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ? AND kind = "
            "'silent_buy' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()["detail"]
        self.assertIn("fallback 25% size", detail)

    def test_first_scan_only_confirms_no_buy(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_mark(bot_id, PIT_MINT, 0.01, _now(-200))
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, PIT_MINT))

    def test_below_6_3pct_never_buys(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, change_5m=6.2))
        self.assertEqual(self._buys(outcomes), [])

    def test_above_mcap_cap_never_buys_but_logs_a_miss(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, mcap=30000))
        self.assertEqual(self._buys(outcomes), [])
        row = self.db.execute(
            "SELECT * FROM pit_missed_coins WHERE mint = ?",
            (PIT_MINT,)).fetchone()
        self.assertIsNotNone(row, "missed coin was not logged")
        self.assertEqual(row["status"], "missed")
        self.assertAlmostEqual(float(row["mcap_usd"]), 30000.0)

    def test_below_mcap_floor_never_buys(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, mcap=8000))
        self.assertEqual(self._buys(outcomes), [])

    def test_low_buy_ratio_never_buys(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, buys=15, sells=10))
        self.assertEqual(self._buys(outcomes), [])

    def test_flat_volume_never_buys(self):
        # 5-minute volume below 2x the hourly pace: the volume
        # confirmation fails, even though the gain is 6.3%.
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, volume_m5=1500))
        self.assertEqual(self._buys(outcomes), [])

    def test_slow_market_never_buys(self):
        # The top 5 movers average 5 buys in 5 minutes — under the
        # 10-buy floor, so the scan is a skip, not a smaller trade.
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106, buys=5, sells=1))
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("too slow" in a["reason"] for a in skips), skips)

    def test_curve_outside_band_never_buys(self):
        self.fake_rpc = _FakeRpc(PIT_MINT, progress_pct=70.0)
        bot_id = self._insert_bot(rules=pit_ruleset())
        outcomes = self._buy_twice(bot_id, _pit_entry(PIT_MINT, 0.0106))
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, PIT_MINT))

    def test_second_open_trade_is_refused(self):
        # Max 1 open: a second coin is refused while one is held.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._buy_twice(bot_id, _pit_entry(PIT_MINT, 0.0106))
        self._reset_scan(bot_id)
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT2, 0.0106))
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, PIT_MINT2))

    def test_devnet_watchlist_buys_without_dexscreener_data(self):
        # The carve-out: no txn data, no volume, curve below the
        # band — the devnet test coin still buys, so exits stay
        # testable on devnet.
        bot_id = self._insert_bot(rules=pit_ruleset())
        entry = _pit_entry(PIT_MINT, 0.0106, buys=None, sells=None,
                           devnet_watchlist=True, curve_real_sol=0.5,
                           volume_m5=None, volume_h1=None)
        outcomes = self._buy_twice(bot_id, entry)
        self.assertEqual(len(self._buys(outcomes)), 1)
        self.assertIsNotNone(self._position(bot_id, PIT_MINT))


class TestPitLoopExits(PitBase):
    def _held(self, bot_id, price=0.01, peak=0.01, stage=0,
              amount=100.0, opened_at=None, last_price=None):
        return self._insert_position(
            bot_id, PIT_MINT, amount, 0.01, peak=peak, orig=100.0,
            stage=stage, opened_at=opened_at,
            last_price=last_price if last_price is not None else price)

    def test_take_profit_sells_all_at_plus_12(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0112)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("+12%", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, PIT_MINT))

    def test_hard_stop_fires_at_minus_4_9(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0095,
                                         change_5m=-4.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("stop loss", sells[0]["reason"])

    def test_pullback_needs_two_ticks(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id)
        # First tick at -2% in 5 minutes: one vote, no sell.
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0098,
                                         change_5m=-2.0)])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, PIT_MINT))
        # Second consecutive tick: the exit fires.
        self._reset_scan(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0098,
                                         change_5m=-2.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("pullback", sells[0]["reason"])

    def test_pullback_fires_before_take_profit(self):
        # A coin at +11% that dips 2% in 5 minutes sells at +9% and
        # never reaches +12% — the exit precedence from the spec.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id, price=0.0111, peak=0.0111)
        self._run([_pit_entry(PIT_MINT, 0.0111, change_5m=-2.0)])
        self._reset_scan(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0111,
                                         change_5m=-2.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("pullback", sells[0]["reason"])
        self.assertNotIn("+12%", sells[0]["reason"])

    def test_flat_dead_needs_two_ticks(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.01,
                                         change_5m=0.3)])
        self.assertEqual(self._sells(outcomes), [])
        self._reset_scan(bot_id)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.01,
                                         change_5m=0.3)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("flat", sells[0]["reason"])

    def test_time_cap_closes_at_ten_minutes(self):
        # Opened 11 minutes ago, up 2% — not flat, not +12%, not
        # stopped: the 10-minute cap closes it at market.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id, opened_at=_now(-660))
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0102,
                                         change_5m=2.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("time cap", sells[0]["reason"])

    def test_breakeven_stop_sells_at_cost(self):
        # Peak +6%, price back to the entry: sell at cost.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._held(bot_id, peak=0.0106)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.01,
                                         change_5m=0.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("breakeven", sells[0]["reason"])


class TestPitFastExits(PitBase):
    def test_tick_crossing_stop_sells_without_the_loop(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        feed = _FakeFeed({PIT_MINT: 0.0095})
        sl._on_live_tick(PIT_MINT, 0.0095, 0)
        actions = sl._fast_exit_once(self.db, self.cfg, feed,
                                     rpc_factory=self.fake_rpc)
        sells = [a for a in actions if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("stop loss", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, PIT_MINT))

    def test_tick_pullback_sells_without_the_loop(self):
        # A tick 2% below the last loop mark closes the position
        # at once — the fast pullback, with the loop's debounced
        # 5-minute-bucket pullback as the backstop.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        feed = _FakeFeed({PIT_MINT: 0.0097})
        sl._on_live_tick(PIT_MINT, 0.0097, 0)
        actions = sl._fast_exit_once(self.db, self.cfg, feed,
                                     rpc_factory=self.fake_rpc)
        sells = [a for a in actions if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertIn("pullback", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, PIT_MINT))

    def test_tick_inside_pullback_does_not_sell(self):
        # A tick 1% below the last mark is not a pullback — the
        # position stays open for the loop to judge.
        bot_id = self._insert_bot(rules=pit_ruleset())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01)
        feed = _FakeFeed({PIT_MINT: 0.0099})
        sl._on_live_tick(PIT_MINT, 0.0099, 0)
        actions = sl._fast_exit_once(self.db, self.cfg, feed,
                                     rpc_factory=self.fake_rpc)
        self.assertEqual([a for a in actions if a["action"] == "sell"],
                         [])
        self.assertIsNotNone(self._position(bot_id, PIT_MINT))


class TestPitDailyLossBreaker(PitBase):
    def test_breaker_halts_new_buys(self):
        # Real mode: -$2.00 today at SOL $71.50 is past -0.025 SOL
        # (-$1.79) — no new buys until tomorrow.
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-2.0, mode="real",
                     now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("daily-loss breaker" in a["reason"] for a in skips),
            skips)

    def test_breaker_needs_a_live_sol_price(self):
        # Same -$2.00 loss, no live SOL price: the breaker cannot
        # be computed and does NOT trip. (Documented fail-open —
        # tested at the unit level: a real-mode loop buy would also
        # need fuel enrollment, a funded wallet and an executor.)
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-2.0, mode="real",
                     now=_now())
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, _reason = _pit_buy_halt(
            self.db, bot, now_ts, None, _empty_prices(), 0.025)
        self.assertFalse(halted)
        # With a live SOL price the same loss trips it.
        feed = _FakeFeed({"SOL": 71.50})
        halted, reason = _pit_buy_halt(
            self.db, bot, now_ts, feed, _empty_prices(), 0.025)
        self.assertTrue(halted)
        self.assertIn("daily-loss breaker", reason)

    def test_paper_mode_never_trips_the_breaker(self):
        # A paper loss is play money — the paper test measures the
        # rule, not the breaker.
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="paper")
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-2.0, mode="paper",
                     now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _pit_entry(PIT_MINT, 0.0106), feed=feed)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_breaker_halts_buys_but_not_exits(self):
        # A halted bot still stops out of an open position.
        # Package fuel (2026-11-23): the exit is a real trade and
        # costs 1 credit — funded here so the breaker, not the
        # fuel gate, is what is being tested.
        grant_fuel(self.db, self.user_id, 100)
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-2.0, mode="real",
                     now=_now())
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01,
                              last_price=0.0095)
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0095,
                                         change_5m=-4.0)], feed=feed,
                             executor=self._stub_executor())
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("stop loss", sells[0]["reason"])


class TestPitFuelPackages(PitBase):
    """$GATE package fuel (2026-11-23): a Pit bot trades real only
    while its owner holds package credits. No package: no buy, a
    plain message saying so — never silently free. The gate covers
    ALL real trades, exits included: at zero fuel even a stop-loss
    exit is refused until the user refills."""

    def test_no_package_real_bot_is_blocked_plainly(self):
        # Real mode, no $GATE package bought: no buy, a plain
        # message saying to buy a package — never silently free.
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        outcomes = self._buy_twice(bot_id, _pit_entry(PIT_MINT, 0.0106))
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("$GATE package" in a["reason"] for a in skips),
            skips)

    def test_no_package_real_bot_exit_refused(self):
        # The gate covers exits too: at zero fuel an open position
        # does NOT stop out — the user refills first.
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01,
                              last_price=0.0095)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0095,
                                         change_5m=-4.0)],
                             executor=self._stub_executor())
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 0)

    def test_funded_real_bot_still_exits(self):
        # With a package bought, the same open position stops out.
        grant_fuel(self.db, self.user_id, 100)
        bot_id = self._insert_bot(rules=pit_ruleset(), mode="real")
        self._insert_position(bot_id, PIT_MINT, 100.0, 0.01,
                              last_price=0.0095)
        outcomes = self._run([_pit_entry(PIT_MINT, 0.0095,
                                         change_5m=-4.0)],
                             executor=self._stub_executor())
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)


class TestPitSharedBrain(PitBase):
    def test_seed_creates_v1_0(self):
        version = ensure_pit_version_1_0(self.db)
        self.assertEqual(version, "1.0")
        active = current_pit_version(self.db)
        self.assertIsNotNone(active)
        self.assertEqual(active["version"], "1.0")
        self.assertEqual(active["active"], 1)
        # Seeding twice does not duplicate the version.
        self.assertEqual(ensure_pit_version_1_0(self.db), "1.0")
        rows = self.db.execute(
            "SELECT COUNT(*) FROM preset_versions WHERE preset_key = 'pit'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_publish_bumps_by_point_one(self):
        ensure_pit_version_1_0(self.db)
        rules = pit_ruleset()
        rules["buy_filter"]["max_market_cap_usd"] = 30000.0
        version = publish_pit_version(
            self.db, rules, ["Max market cap $25,000 -> $30,000"],
            "auto-widen")
        self.assertEqual(version, "1.1")
        active = current_pit_version(self.db)
        self.assertEqual(active["version"], "1.1")
        self.assertEqual(float(json.loads(active["rules_json"])
                         ["buy_filter"]["max_market_cap_usd"]), 30000.0)
        # The old version stays as history, inactive.
        old = self.db.execute(
            "SELECT active FROM preset_versions WHERE version = '1.0'"
        ).fetchone()
        self.assertEqual(old["active"], 0)

    def test_publish_rejects_invalid_rules(self):
        ensure_pit_version_1_0(self.db)
        rules = pit_ruleset()
        rules["buy"]["threshold_pct"] = 150.0
        with self.assertRaises(ValueError):
            publish_pit_version(self.db, rules, ["bad"], "test")
        self.assertEqual(current_pit_version(self.db)["version"], "1.0")

    def test_changelog_is_numbered_in_order(self):
        ensure_pit_version_1_0(self.db)
        rules = pit_ruleset()
        rules["buy_filter"]["max_market_cap_usd"] = 30000.0
        publish_pit_version(self.db, rules, ["Cap raised"], "auto-widen")
        lines = pit_changelog(self.db)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("1. "))
        self.assertTrue(lines[1].startswith("2. "))
        self.assertIn("Cap raised", lines[1])

    def test_rollback_restores_the_previous_version(self):
        ensure_pit_version_1_0(self.db)
        rules = pit_ruleset()
        rules["buy_filter"]["max_market_cap_usd"] = 30000.0
        publish_pit_version(self.db, rules, ["Cap raised"], "auto-widen")
        ok, note = rollback_pit_version(self.db, "1.0")
        self.assertTrue(ok, note)
        self.assertEqual(current_pit_version(self.db)["version"], "1.0")
        self.assertEqual(
            float(json.loads(current_pit_version(self.db)["rules_json"])
                  ["buy_filter"]["max_market_cap_usd"]), 25000.0)
        # The rollback itself is logged, numbered after the rest.
        lines = pit_changelog(self.db)
        self.assertTrue(any("Rolled back" in line for line in lines))
        self.assertTrue(lines[-1].startswith("3. "))
        # Rolling back to a version that does not exist fails plainly.
        ok, note = rollback_pit_version(self.db, "9.9")
        self.assertFalse(ok)

    def test_adoption_updates_bot_row_and_name_tag(self):
        ensure_pit_version_1_0(self.db)
        bot_id = self._insert_bot(rules=pit_ruleset())
        rules = pit_ruleset()
        rules["buy_filter"]["max_market_cap_usd"] = 30000.0
        publish_pit_version(self.db, rules, ["Cap raised"], "auto-widen")
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        fresh = maybe_adopt_pit_version(self.db, bot)
        self.assertEqual(fresh["preset_version"], "1.1")
        self.assertEqual(fresh["name"], "Pit Sweeper v1.1")
        self.assertEqual(
            float(json.loads(fresh["rules_json"])
                  ["buy_filter"]["max_market_cap_usd"]), 30000.0)
        # Adopting again is a no-op.
        self.assertEqual(
            maybe_adopt_pit_version(self.db, fresh)["preset_version"],
            "1.1")

    def test_two_missed_coins_raise_the_cap(self):
        ensure_pit_version_1_0(self.db)
        record_pit_missed_coin(self.db, PIT_MINT, 27000.0, 8.0)
        record_pit_missed_coin(self.db, PIT_MINT2, 28500.0, 9.0)
        note = pit_review_missed_coins(self.db, {})
        self.assertIn("v1.1", note)
        self.assertEqual(current_pit_version(self.db)["version"], "1.1")
        # Rounded up to the next $5,000 above the highest miss.
        self.assertEqual(
            float(json.loads(current_pit_version(self.db)["rules_json"])
                  ["buy_filter"]["max_market_cap_usd"]), 30000.0)

    def test_one_missed_coin_does_not_raise_the_cap(self):
        ensure_pit_version_1_0(self.db)
        record_pit_missed_coin(self.db, PIT_MINT, 27000.0, 8.0)
        note = pit_review_missed_coins(self.db, {})
        self.assertEqual(note, "")
        self.assertEqual(current_pit_version(self.db)["version"], "1.0")

    def test_rugged_missed_coin_is_negative_evidence(self):
        ensure_pit_version_1_0(self.db)
        record_pit_missed_coin(self.db, PIT_MINT, 27000.0, 8.0)
        # The coin has since fallen 30% in 5 minutes — it rugged,
        # so it is marked, not counted as a reason to widen.
        note = pit_review_missed_coins(
            self.db, {PIT_MINT: {"change_5m_pct": -30.0}})
        self.assertEqual(note, "")
        row = self.db.execute(
            "SELECT status FROM pit_missed_coins WHERE mint = ?",
            (PIT_MINT,)).fetchone()
        self.assertEqual(row["status"], "rugged")

    def test_watchlist_ranks_and_scraps(self):
        ensure_pit_version_1_0(self.db)
        bot_id = self._insert_bot(rules=pit_ruleset())
        settings = pit_ruleset()["pit"]
        movers = [_pit_entry(PIT_MINT, 0.01, change_5m=9.0),
                  _pit_entry(PIT_MINT2, 0.01, change_5m=7.0)]
        update_pit_watchlist(self.db, bot_id, movers,
                             {e["mint"]: e for e in movers}, settings,
                             _now())
        rows = self.db.execute(
            "SELECT * FROM pit_watchlist WHERE bot_id = ? ORDER BY rank",
            (bot_id,)).fetchall()
        self.assertEqual([r["mint"] for r in rows],
                         [PIT_MINT, PIT_MINT2])
        self.assertEqual(rows[0]["rank"], 1)
        # PIT_MINT2 drops off the movers and shows resistance
        # (-2.5% in 5 minutes) on two consecutive scans: dead.
        feed = {PIT_MINT: _pit_entry(PIT_MINT, 0.01, change_5m=9.0),
                PIT_MINT2: _pit_entry(PIT_MINT2, 0.01, change_5m=-2.5)}
        update_pit_watchlist(self.db, bot_id,
                             [feed[PIT_MINT]], feed, settings, _now())
        update_pit_watchlist(self.db, bot_id,
                             [feed[PIT_MINT]], feed, settings, _now())
        row = self.db.execute(
            "SELECT * FROM pit_watchlist WHERE bot_id = ? AND mint = ?",
            (bot_id, PIT_MINT2)).fetchone()
        self.assertEqual(row["status"], "dead")
        self.assertIn("resistance", row["dead_reason"])


class TestPitVersionLedger(PitBase):
    def test_trades_are_tagged_with_the_version_they_ran_under(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        record_trade(self.db, self.user_id, bot_id, PIT_MINT, "buy",
                     100.0, 0.01, 1.0, mode="paper", now=_now())
        record_trade(self.db, self.user_id, bot_id, PIT_MINT, "sell",
                     100.0, 0.0112, 1.12, pnl_usd=0.12, mode="paper",
                     now=_now())
        row = self.db.execute(
            "SELECT preset_version FROM trade_ledger WHERE bot_id = ? "
            "AND side = 'sell'", (bot_id,)).fetchone()
        # Tagged from the bot row — paper.py never sees a version.
        self.assertEqual(row["preset_version"], "1.0")

    def test_per_version_win_rate_groups_by_version(self):
        bot_id = self._insert_bot(rules=pit_ruleset())
        # v1.0: 1 win, 1 loss, net -$1.00.
        record_trade(self.db, self.user_id, bot_id, "A", "sell", 1.0,
                     1.0, 1.0, pnl_usd=1.0, mode="paper", now=_now(),
                     preset_version="1.0")
        record_trade(self.db, self.user_id, bot_id, "B", "sell", 1.0,
                     1.0, 1.0, pnl_usd=-2.0, mode="paper", now=_now(),
                     preset_version="1.0")
        # v1.1: 1 win, net +$3.00.
        record_trade(self.db, self.user_id, bot_id, "C", "sell", 1.0,
                     1.0, 1.0, pnl_usd=3.0, mode="paper", now=_now(),
                     preset_version="1.1")
        summary = version_wl_summary(self.db, bot_id)
        self.assertEqual(len(summary), 2)
        v10, v11 = summary
        self.assertEqual(v10["version"], "1.0")
        self.assertEqual(v10["trades"], 2)
        self.assertEqual(v10["wins"], 1)
        self.assertEqual(v10["losses"], 1)
        self.assertAlmostEqual(v10["win_rate"], 0.5)
        self.assertAlmostEqual(v10["net_pnl_usd"], -1.0)
        self.assertEqual(v11["version"], "1.1")
        self.assertEqual(v11["trades"], 1)
        self.assertAlmostEqual(v11["win_rate"], 1.0)
        self.assertAlmostEqual(v11["net_pnl_usd"], 3.0)


class TestPitAutoTightenExcluded(PitBase):
    def test_losing_pit_bot_is_never_auto_tightened(self):
        # 6 losses, 2 wins, net negative — a scanner bot would
        # tighten; Pit's rules change only through the shared
        # brain, so nothing changes.
        bot_id = self._insert_bot(rules=pit_ruleset())
        for i in range(6):
            record_trade(self.db, self.user_id, bot_id, f"L{i}",
                         "sell", 1.0, 90.0, 90.0, pnl_usd=-10.0,
                         mode="paper", now="2026-01-01T00:00:00Z")
        for i in range(2):
            record_trade(self.db, self.user_id, bot_id, f"W{i}",
                         "sell", 1.0, 110.0, 110.0, pnl_usd=10.0,
                         mode="paper", now="2026-01-01T00:10:00Z")
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        adj = review_bot(self.db, bot, self.cfg.encryption_key)
        self.assertIsNone(adj)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        rules = json.loads(bot["rules_json"])
        self.assertEqual(rules["buy"]["threshold_pct"], 6.3)
        self.assertEqual(bot["stop_loss_pct"], 4.9)
        self.assertEqual(bot["buy_size_pct"], 25.0)
        rows = self.db.execute(
            "SELECT * FROM builderbot_adjustments WHERE bot_id = ?",
            (bot_id,)).fetchall()
        self.assertEqual(rows, [])


class TestPitPresetRoute(PitBase):
    def _choose_mode(self, mode):
        resp = self.client.get("/bots",
                               query_string={"mode": mode})
        self.assertEqual(resp.status_code, 200)

    def test_create_pit_from_preset(self):
        self._choose_mode("paper")
        resp = self.client.post(
            "/bots/preset/pit",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        row = self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name LIKE ?",
            (self.user_id, "Pit Sweeper%")).fetchone()
        self.assertIsNotNone(row, "Pit bot was not created")
        self.assertEqual(row["name"], "Pit Sweeper v1.0")
        self.assertEqual(row["preset_version"], "1.0")
        self.assertAlmostEqual(float(row["bankroll_sol"]), 0.20)
        self.assertEqual(row["stop_loss_pct"], 4.9)
        self.assertEqual(row["buy_size_pct"], 25.0)
        self.assertEqual(row["max_open_trades"], 1)
        self.assertEqual(row["mode"], "paper")
        self.assertEqual(row["silent"], 0)
        self.assertEqual(row["platform"], "pump.fun")
        self.assertEqual(row["category"], "Pit Sweeper")
        rules = json.loads(row["rules_json"])
        self.assertEqual(rules["kind"], "pit")
        self.assertEqual(rules["scan_interval_seconds"], 10)
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err, err)
        # The v1.0 version row was seeded by the create route.
        self.assertEqual(current_pit_version(self.db)["version"], "1.0")
        # Lands on the new bot's detail page.
        self.assertIn("/bots/", resp.headers.get("Location", ""))

    def test_create_pit_twice_goes_to_existing(self):
        self._choose_mode("paper")
        self.client.post(
            "/bots/preset/pit",
            data={"csrf_token": csrf(self.client, "/bots")})
        resp = self.client.post(
            "/bots/preset/pit",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        rows = self.db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND name LIKE ?",
            (self.user_id, "Pit Sweeper%")).fetchall()
        self.assertEqual(len(rows), 1, "duplicate Pit bot created")

    def test_bots_page_has_the_pit_button(self):
        self._choose_mode("paper")
        html = self.client.get("/bots").data.decode()
        start = html.find('id="active-bots-list"')
        end = html.find('id="signer-card"', start)
        window = html[start:end]
        self.assertIn('action="/bots/preset/pit"', window)
        self.assertIn("⛏️", window)
        self.assertIn("<strong>Pit Sweeper v1.0</strong>", window)
        self.assertIn("bold · Buy 25% · Stop 4.9% · Max 1", window)


if __name__ == "__main__":
    unittest.main()
