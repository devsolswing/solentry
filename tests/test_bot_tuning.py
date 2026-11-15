"""Bot-tuning build (2026-11-15, + the four add-ons): momentum-only
entries, strict loss prevention, pre-buy scam gates, profit lock,
tick-driven exits, and the real-signal four-part filter — for the
Scanner/Clyde presets and builderBot-created bots (scanner + signals
kinds). Trenches is NOT covered here (Trenches v2 owns it), and no
live bot row is retuned by any of these tests.

The scam gate reads the top-10 share and the mint authorities
through the injected fake RPC, and metadata through the stored
coin_meta row (pre-seeded) — no live network in these tests.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
import tradingbot.silent_loop as sl
from helpers import make_client, signup
from tradingbot.builderbot import review_bot
from tradingbot.config import Config
from tradingbot.ledger import record_trade
from tradingbot.paper import paper_buy, paper_cash, paper_sell
from tradingbot.rules import (CLYDE_MIN_MARKET_CAP, clyde_ruleset,
                              scanner_ruleset)
from tradingbot.scanner import (BUY_FILTER_DEFAULT, detect_regime,
                                passes_momentum_gate, signal_dead,
                                SLOW_LADDER)
from tradingbot.silent_loop import _fast_exit_once, _on_live_tick, run_once
from tradingbot.solana import parse_mint_authorities

MINTA = "MINTA11111111111111111111111111111111111111"
MINTB = "MINTB11111111111111111111111111111111111111"
MINTC = "MINTC11111111111111111111111111111111111111"
# A valid base58 address (MINTA contains "I", which base58
# excludes) for the signals-kind tests — signals bots only price
# mints that pass is_valid_address.
SIGMINT = "B111111111111111111111111111111111111111111"


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _entry(mint, price, change_5m=6.0, change_1h=25.0,
           volume_m5=5000, volume_h1=12000, buys=40, sells=10,
           mcap=None):
    """A realistic gaining feed entry: all four signal checks agree
    (5-min gain, rising volume, buyers outnumber sellers, positive
    1-hour trend)."""
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": change_1h, "change_6h_pct": None,
            "change_24h_pct": None, "liquidity_usd": 1000,
            "market_cap_usd": mcap,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": 0,
            "volume_m5_usd": volume_m5, "volume_h1_usd": volume_h1,
            "txns_m5_buys": buys, "txns_m5_sells": sells}


def _flat_entry(mint, price, **over):
    """A coin spiking on the short window but flat everywhere else:
    no 5-minute gain, no 1-hour trend."""
    kw = dict(change_5m=0.0, change_1h=0.0)
    kw.update(over)
    return _entry(mint, price, **kw)


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
    """Solana RPC stand-in for the scam gates + top-10 watch.

    totals: {mint: top10_total}; supplies: {mint: total_supply};
    authorities: {mint: (mint_authority_on, freeze_authority_on)};
    unserved mints raise, like a real RPC that cannot serve them.
    """

    def __init__(self, totals=None, supplies=None, authorities=None):
        self.totals = totals or {}
        self.supplies = supplies or {}
        self.authorities = authorities or {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self

    def get_token_largest_accounts(self, mint):
        if mint not in self.totals:
            raise RuntimeError("mint not served")
        total = self.totals[mint]
        share = total // 10
        return [{"address": f"acct{i}", "amount": str(share),
                 "decimals": 6, "uiAmount": 0.0} for i in range(10)]

    def get_token_supply(self, mint):
        if mint not in self.supplies:
            raise RuntimeError("mint not served")
        return self.supplies[mint]

    def get_mint_authorities(self, mint):
        if mint not in self.authorities:
            raise RuntimeError("mint not served")
        return self.authorities[mint]

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


class TuningBase(unittest.TestCase):
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
        # metadata on file — passes every scam gate.
        self.fake_rpc = _FakeRpc(
            totals={MINTA: 50_000_000, MINTB: 50_000_000,
                    MINTC: 50_000_000, SIGMINT: 50_000_000},
            supplies={MINTA: 1_000_000_000, MINTB: 1_000_000_000,
                      MINTC: 1_000_000_000, SIGMINT: 1_000_000_000},
            authorities={MINTA: (False, False), MINTB: (False, False),
                         MINTC: (False, False),
                         SIGMINT: (False, False)})
        for mint in (MINTA, MINTB, MINTC, SIGMINT):
            self._seed_meta(mint)

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()
        sl._SELL_IN_FLIGHT.clear()
        sl._TOP10_SKIP_LOGGED_AT.clear()
        with sl._TICK_DIRTY_LOCK:
            sl._TICK_DIRTY_MINTS.clear()
        sl._TICK_EVENT.clear()

    def _seed_meta(self, mint, name="Mint A", symbol="MINTA"):
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, ?, ?, NULL, 'metaplex', ?) "
            "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
            "symbol = excluded.symbol",
            (mint, name, symbol, _now()))
        self.db.commit()

    def _insert_bot(self, rules, name="Tuned", mode="paper", silent=1,
                    coins=None, buy_size=20, stop_loss=7, max_open=3):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, rules_json) VALUES (?, ?, 'bold', ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (self.user_id, name, json.dumps(coins or []), buy_size,
             stop_loss, mode, silent, max_open,
             hashlib.sha256(b"tok").hexdigest(),
             json.dumps(rules)))
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


class TestMomentumGate(TuningBase):
    def test_all_four_checks_agree_passes(self):
        ok, why = passes_momentum_gate(_entry(MINTA, 0.0106))
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_flat_coin_rejected_even_with_short_spike(self):
        # A 5-minute gain of zero: flat, not gaining — never bought
        # even though the 3-minute window spiked.
        ok, why = passes_momentum_gate(_flat_entry(MINTA, 0.0106))
        self.assertFalse(ok)
        self.assertIn("5-minute gain", why)

    def test_falling_coin_rejected(self):
        ok, why = passes_momentum_gate(
            _entry(MINTA, 0.0106, change_5m=-2.0, change_1h=-5.0))
        self.assertFalse(ok)

    def test_volume_less_spike_rejected_as_fake(self):
        # Price up, volume flat vs the hourly pace: a manufactured
        # spike, not a signal.
        ok, why = passes_momentum_gate(
            _entry(MINTA, 0.0106, volume_m5=500, volume_h1=12000))
        self.assertFalse(ok)
        self.assertIn("volume not rising", why)

    def test_sellers_outnumbering_rejected(self):
        ok, why = passes_momentum_gate(
            _entry(MINTA, 0.0106, buys=10, sells=40))
        self.assertFalse(ok)
        self.assertIn("sellers outnumber buyers", why)

    def test_negative_1h_trend_rejected(self):
        ok, why = passes_momentum_gate(
            _entry(MINTA, 0.0106, change_1h=-1.0))
        self.assertFalse(ok)
        self.assertIn("1-hour trend", why)

    def test_missing_data_rejected_fail_closed(self):
        ok, why = passes_momentum_gate(
            _entry(MINTA, 0.0106, change_5m=None, change_1h=None))
        self.assertFalse(ok)

    def test_watchlist_entry_uses_marks_for_5m_gain(self):
        import time

        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        entry = {"mint": MINTA, "price_usd": 0.0106,
                 "devnet_watchlist": True}
        ok, _why = passes_momentum_gate(entry, self.db, bot_id,
                                        time.time())
        self.assertTrue(ok)

    def test_signal_dead_requires_all_three_dead(self):
        entry = _entry(MINTA, 0.0106)
        self.assertFalse(signal_dead(entry, self.db, 1, MINTA, 0.0106,
                                    0))
        dead = _entry(MINTA, 0.009, change_5m=-5.0, change_1h=-2.0,
                      volume_m5=500, volume_h1=12000, buys=10, sells=40)
        self.assertTrue(signal_dead(dead, self.db, 1, MINTA, 0.009, 0))
        # Gain dead but buyers still winning: not dead, hold.
        partial = _entry(MINTA, 0.009, change_5m=-5.0, volume_m5=500,
                         volume_h1=12000, buys=40, sells=10)
        self.assertFalse(signal_dead(partial, self.db, 1, MINTA, 0.009,
                                    0))


class TestRegimeAndLadder(TuningBase):
    def test_fresh_coin_with_no_h1_volume_is_slow(self):
        entry = _entry(MINTA, 0.0106, volume_m5=5000, volume_h1=0)
        self.assertEqual(detect_regime(entry), "slow")

    def test_slow_ladder_first_rung_is_5pct(self):
        self.assertEqual(SLOW_LADDER[0],
                         {"pct_of_position": 25, "profit_pct": 5})

    def test_preset_volume_floors_are_5000(self):
        self.assertEqual(
            scanner_ruleset()["buy_filter"]["min_volume_5m_usd"], 5000.0)
        self.assertEqual(
            clyde_ruleset()["buy_filter"]["min_volume_5m_usd"], 5000.0)
        # The shared default is untouched — Trenches keeps $1,000.
        self.assertEqual(BUY_FILTER_DEFAULT["min_volume_5m_usd"], 1000.0)

    def test_clyde_mcap_still_45000_usd(self):
        # The USD-vs-CAD question is still open with James — the
        # stored unit is NOT silently changed.
        self.assertEqual(CLYDE_MIN_MARKET_CAP, 45000.0)
        self.assertEqual(
            clyde_ruleset()["buy_filter"]["min_market_cap_usd"],
            45000.0)

    def test_loop_tick_default_is_30s(self):
        self.assertEqual(Config().silent_loop_interval_seconds, 30)


class TestPaperHaircut(TuningBase):
    def test_buy_fills_1pct_above_mark(self):
        amount, usd = paper_buy(self.db, self.user_id, "SOL", 100.0,
                                1.00, _now())
        self.assertAlmostEqual(amount, 100.0 / 1.01, places=4)
        self.assertEqual(usd, 100.0)

    def test_sell_fills_1pct_below_mark(self):
        paper_buy(self.db, self.user_id, "SOL", 100.0, 1.00, _now())
        amount = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE user_id = ? "
            "AND coin_id = 'SOL'", (self.user_id,)).fetchone()["amount"]
        usd = paper_sell(self.db, self.user_id, "SOL", amount, 1.00,
                         _now())
        self.assertAlmostEqual(usd, amount * 0.99, places=4)
        # Round trip at a flat price loses ~2% to the haircut.
        self.assertLess(paper_cash(self.db, self.user_id), 100.0)
        self.assertAlmostEqual(paper_cash(self.db, self.user_id),
                               100.0 * 0.99 / 1.01 * 1.0, places=2)


class TestTwoScanConfirmation(TuningBase):
    def test_first_scan_records_no_buy(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, MINTA))
        row = self.db.execute(
            "SELECT * FROM scanner_signal_confirmations WHERE bot_id = ? "
            "AND mint = ?", (bot_id, MINTA)).fetchone()
        self.assertIsNotNone(row)

    def test_second_scan_buys(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(len(self._buys(outcomes)), 1)
        self.assertIsNotNone(self._position(bot_id, MINTA))

    def test_faded_signal_never_buys(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        # Second scan: the gain faded below the 5% threshold.
        outcomes = self._run([_entry(MINTA, 0.0104)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, MINTA))


class TestScamGates(TuningBase):
    def test_clean_coin_buys_after_confirmation(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_high_top10_rejected(self):
        # Top-10 owns 30% — over the Scanner 21% bar.
        self.fake_rpc.totals[MINTA] = 300_000_000
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, MINTA))

    def test_clyde_12pct_bar_rejects_what_scanner_allows(self):
        # Top-10 owns 15%: under Scanner's 21% bar, over Clyde's 12%.
        self.fake_rpc.totals[MINTA] = 150_000_000
        bot_id = self._insert_bot(rules=clyde_ruleset(), name="Clyde")
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])

    def test_unserved_mint_rejected_fail_closed(self):
        del self.fake_rpc.totals[MINTA]
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])

    def test_mint_authority_enabled_rejected(self):
        self.fake_rpc.authorities[MINTA] = (True, False)
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])

    def test_freeze_authority_enabled_rejected(self):
        self.fake_rpc.authorities[MINTA] = (False, True)
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])

    def test_metadata_less_coin_rejected(self):
        self.db.execute("DELETE FROM coin_meta WHERE mint = ?",
                        (MINTA,))
        self.db.commit()
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        with mock.patch.object(sl, "ensure_coin_meta",
                              return_value=None):
            self._run([_entry(MINTA, 0.0106)])
            self._reset_scan(bot_id)
            outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])

    def test_parse_mint_authorities(self):
        # SPL mint layout: option(u32) + pubkey, supply, decimals,
        # is_initialized, freeze option(u32) + pubkey.
        data = bytearray(82)
        data[0:4] = (1).to_bytes(4, "little")   # mint authority on
        data[46:50] = (0).to_bytes(4, "little")  # freeze off
        self.assertEqual(parse_mint_authorities(bytes(data)),
                         (True, False))
        data[0:4] = (0).to_bytes(4, "little")
        data[46:50] = (1).to_bytes(4, "little")
        self.assertEqual(parse_mint_authorities(bytes(data)),
                         (False, True))
        self.assertIsNone(parse_mint_authorities(b"\x00" * 10))
        self.assertIsNone(parse_mint_authorities(b""))


class TestProfitLock(TuningBase):
    def test_breakeven_stop_exits_at_entry_not_a_loss(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Bought at 0.01, peaked at +10% (0.011) — breakeven armed.
        self._insert_position(bot_id, MINTA, 100, 0.01, peak=0.011)
        outcomes = self._run([_entry(MINTA, 0.01)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "breakeven stop")
        self.assertIsNone(self._position(bot_id, MINTA))

    def test_below_breakeven_no_exit_at_entry(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Peaked at +5% only — breakeven not armed; the 7% stop sits
        # at 0.0093, so a drop to entry (0.01) must NOT sell.
        self._insert_position(bot_id, MINTA, 100, 0.01, peak=0.0105)
        outcomes = self._run([_entry(MINTA, 0.01)])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, MINTA))

    def test_trailing_stop_still_banks_gains(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Peaked at +20% (0.012); the 7% trailing stop sits at
        # 0.01116 — a fall to 0.011 fires it.
        self._insert_position(bot_id, MINTA, 100, 0.01, peak=0.012)
        outcomes = self._run([_entry(MINTA, 0.011)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "trailing stop")


class TestMaxHoldAndCooldown(TuningBase):
    def test_max_hold_exits_flat_position_at_30min(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01,
                              opened_at=_now(-31 * 60))
        outcomes = self._run([_entry(MINTA, 0.0102)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("max hold time", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, MINTA))

    def test_max_hold_does_not_fire_before_30min(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01,
                              opened_at=_now(-10 * 60))
        outcomes = self._run([_entry(MINTA, 0.0102)])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, MINTA))

    def test_max_hold_skipped_after_a_rung_fired(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Old position, already sold one rung (stage 1): the
        # position is working, so max hold does not apply.
        self._insert_position(bot_id, MINTA, 100, 0.01, stage=1,
                              opened_at=_now(-31 * 60))
        outcomes = self._run([_entry(MINTA, 0.0102)])
        self.assertEqual(self._sells(outcomes), [])

    def test_stop_loss_sets_30min_rebuy_cooldown(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        # Price crashes through the 7% stop: stop-loss sell.
        outcomes = self._run([_entry(MINTA, 0.009)])
        self.assertEqual(len(self._sells(outcomes)), 1)
        # Fresh signal right after the loss: the coin is NOT
        # re-bought while the cooldown runs.
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        # Cooldown expired: the same signal buys again. The
        # confirmation recorded on the blocked scan is not consumed
        # by the block — this run is the confirming second scan.
        self.db.execute(
            "UPDATE bot_buy_cooldowns SET blocked_until = ? "
            "WHERE bot_id = ? AND mint = ?",
            (_now(-60), bot_id, MINTA))
        self.db.commit()
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.0106)])
        self.assertEqual(len(self._buys(outcomes)), 1)


class TestLossPrevention(TuningBase):
    def _losing_sells(self, bot_id, count, pnl_each=-10.0, when=None):
        for i in range(count):
            record_trade(self.db, self.user_id, bot_id, f"L{i}",
                         "sell", 1.0, 90.0, 90.0, pnl_usd=pnl_each,
                         mode="paper", now=when or _now())

    def _seed_snapshot(self, total_value_usd, when):
        self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
            "holdings_value_usd, total_value_usd, real_sol, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (self.user_id, total_value_usd, 0.0, total_value_usd, when))
        self.db.commit()

    def _signal_run(self, bot_id):
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        return self._run([_entry(MINTA, 0.0106)])

    def test_drawdown_breaker_halts_buys_at_minus_10pct(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Starting capital $1,000 (snapshot before midnight);
        # today's realized P&L is -$100: exactly -10%.
        self._seed_snapshot(1000.0, _now(-26 * 3600))
        self._losing_sells(bot_id, 10, pnl_each=-10.0)
        outcomes = self._signal_run(bot_id)
        self.assertEqual(self._buys(outcomes), [])
        rows = self.db.execute(
            "SELECT * FROM activity WHERE user_id = ? AND kind = "
            "'silent_breaker'", (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("drawdown circuit breaker", rows[0]["detail"])

    def test_drawdown_breaker_does_not_trip_below_10pct(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._seed_snapshot(1000.0, _now(-26 * 3600))
        # -$50 realized (-5%) but never 3 consecutive losses: the
        # loss-streak halt must not be the thing that decides this.
        for i, pnl in enumerate([-20.0, 5.0, -20.0, 5.0, -20.0]):
            record_trade(self.db, self.user_id, bot_id, f"T{i}", "sell",
                         1.0, 90.0, 90.0, pnl_usd=pnl, mode="paper",
                         now=_now())
        outcomes = self._signal_run(bot_id)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_drawdown_breaker_resumes_next_day(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._seed_snapshot(1000.0, _now(-26 * 3600))
        # The losses are yesterday's — today's realized P&L is $0.
        self._losing_sells(bot_id, 10, pnl_each=-10.0,
                           when=_now(-26 * 3600))
        outcomes = self._signal_run(bot_id)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_loss_streak_halt_after_3_consecutive_losses(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        # Big starting capital so the -10% drawdown breaker cannot
        # be the thing that decides this test.
        self._seed_snapshot(10000.0, _now(-26 * 3600))
        self._losing_sells(bot_id, 3, pnl_each=-10.0)
        outcomes = self._signal_run(bot_id)
        self.assertEqual(self._buys(outcomes), [])

    def test_two_losses_do_not_halt(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._seed_snapshot(10000.0, _now(-26 * 3600))
        self._losing_sells(bot_id, 2, pnl_each=-10.0)
        outcomes = self._signal_run(bot_id)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_win_breaks_the_loss_streak(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._seed_snapshot(10000.0, _now(-26 * 3600))
        self._losing_sells(bot_id, 2, pnl_each=-10.0)
        record_trade(self.db, self.user_id, bot_id, "W0", "sell", 1.0,
                     110.0, 110.0, pnl_usd=10.0, mode="paper",
                     now=_now())
        self._losing_sells(bot_id, 2, pnl_each=-10.0)
        # Last three closes: loss, loss, win — the win is most
        # recent, so the streak is 0.
        outcomes = self._signal_run(bot_id)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_loss_streak_halt_expires_after_30min(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._seed_snapshot(10000.0, _now(-26 * 3600))
        self._losing_sells(bot_id, 3, pnl_each=-10.0,
                           when=_now(-31 * 60))
        outcomes = self._signal_run(bot_id)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_stop_fires_on_the_next_loop_pass(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        outcomes = self._run([_entry(MINTA, 0.009)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "stop_loss")


class TestSignalDeathAndLogging(TuningBase):
    def test_signal_death_exits_when_all_three_die(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        # -5%: above the 7% stop, so the stop is not what exits —
        # the dead signal is.
        dead = _entry(MINTA, 0.0095, change_5m=-5.0, change_1h=-2.0,
                      volume_m5=500, volume_h1=12000, buys=10, sells=40)
        outcomes = self._run([dead])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("signal died", sells[0]["reason"])

    def test_signal_death_does_not_fire_on_partial_death(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        # Gain dead, but buyers still winning: hold.
        partial = _entry(MINTA, 0.0095, change_5m=-5.0, volume_m5=500,
                         volume_h1=12000, buys=40, sells=10)
        outcomes = self._run([partial])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, MINTA))

    def test_signal_features_and_outcome_logged(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_mark(bot_id, MINTA, 0.01, _now(-200))
        self._run([_entry(MINTA, 0.0106)])
        self._reset_scan(bot_id)
        self._run([_entry(MINTA, 0.0106)])
        row = self.db.execute(
            "SELECT * FROM bot_signal_log WHERE bot_id = ? AND mint = ?",
            (bot_id, MINTA)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["gain_pct"], 6.0, places=1)
        self.assertEqual(row["gain_window"], "3m")
        self.assertIsNotNone(row["volume_ratio"])
        self.assertGreater(row["volume_ratio"], 1.0)
        self.assertIsNotNone(row["buyer_ratio"])
        self.assertGreater(row["buyer_ratio"], 1.0)
        self.assertAlmostEqual(row["trend_1h_pct"], 25.0)
        self.assertIsNone(row["outcome_pnl_usd"])
        # Close the position at a loss: the outcome fills in.
        self._reset_scan(bot_id)
        outcomes = self._run([_entry(MINTA, 0.009)])
        self.assertEqual(len(self._sells(outcomes)), 1)
        row = self.db.execute(
            "SELECT * FROM bot_signal_log WHERE bot_id = ? AND mint = ?",
            (bot_id, MINTA)).fetchone()
        self.assertIsNotNone(row["outcome_pnl_usd"])
        self.assertLess(row["outcome_pnl_usd"], 0)
        self.assertEqual(row["outcome_win"], 0)
        self.assertIsNotNone(row["outcome_at"])


class TestTickDrivenExits(TuningBase):
    def test_tick_crossing_stop_sells_without_the_loop(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        feed = _FakeFeed({MINTA: 0.009})
        _on_live_tick(MINTA, 0.009, 0)
        actions = _fast_exit_once(self.db, self.cfg, feed,
                                  rpc_factory=self.fake_rpc)
        sells = [a for a in actions if a["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "stop_loss")
        self.assertIsNone(self._position(bot_id, MINTA))

    def test_stale_tick_does_not_sell_loop_backstop_does(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        feed = _FakeFeed({MINTA: 0.009})
        feed.ticks[MINTA] = 0.009
        # Simulate a stale tick by making snapshot() report stale.
        orig = feed.snapshot

        def stale_snapshot(coin_ids=None):
            out = orig(coin_ids)
            for tick in out.values():
                tick["stale"] = True
            return out

        feed.snapshot = stale_snapshot
        _on_live_tick(MINTA, 0.009, 0)
        actions = _fast_exit_once(self.db, self.cfg, feed,
                                  rpc_factory=self.fake_rpc)
        self.assertEqual(
            [a for a in actions if a["action"] == "sell"], [])
        self.assertIsNotNone(self._position(bot_id, MINTA))
        # The loop backstop still fires on its next pass.
        outcomes = self._run([_entry(MINTA, 0.009)])
        self.assertEqual(len(self._sells(outcomes)), 1)

    def test_no_double_sell_from_tick_and_loop(self):
        bot_id = self._insert_bot(rules=scanner_ruleset())
        self._insert_position(bot_id, MINTA, 100, 0.01)
        feed = _FakeFeed({MINTA: 0.009})
        _on_live_tick(MINTA, 0.009, 0)
        actions = _fast_exit_once(self.db, self.cfg, feed,
                                  rpc_factory=self.fake_rpc)
        self.assertEqual(
            len([a for a in actions if a["action"] == "sell"]), 1)
        # The loop runs right after: the position is already gone,
        # so there is no second sell.
        outcomes = self._run([_entry(MINTA, 0.009)])
        self.assertEqual(self._sells(outcomes), [])
        sells = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side = "
            "'sell'", (bot_id,)).fetchall()
        self.assertEqual(len(sells), 1)

    def test_sell_in_flight_claim_blocks_second_seller(self):
        pos_id = 4242
        self.assertTrue(sl._claim_position_for_sell(pos_id))
        self.assertFalse(sl._claim_position_for_sell(pos_id))
        sl._release_position_for_sell(pos_id)
        self.assertTrue(sl._claim_position_for_sell(pos_id))
        sl._release_position_for_sell(pos_id)

    def test_livefeed_tick_listener_fires(self):
        from tradingbot.livefeed import LiveFeed

        feed = LiveFeed()
        seen = []
        feed.set_tick_listener(lambda cid, price, now: seen.append(cid))
        self.assertTrue(feed.ingest(MINTA, 0.01, "test"))
        self.assertEqual(seen, [MINTA])
        # A rejected tick never reaches the listener.
        feed.ingest(MINTA, 999.0, "test")
        self.assertEqual(seen, [MINTA])


class TestTop10MainnetClient(TuningBase):
    def test_mainnet_mint_uses_mainnet_rpc(self):
        cache: dict = {}
        client = sl._top10_client_for_mint(
            self.cfg, MINTA, self.fake_rpc, cache)
        self.assertIs(client, self.fake_rpc)
        self.assertEqual(self.fake_rpc.urls,
                         [self.cfg.mainnet_rpc_url])

    def test_watchlist_mint_uses_devnet_rpc(self):
        from tradingbot.devnet_watchlist import active_mints

        watchlist = active_mints(self.cfg)
        self.assertTrue(watchlist)
        cache: dict = {}
        client = sl._top10_client_for_mint(
            self.cfg, watchlist[0], self.fake_rpc, cache)
        self.assertIs(client, self.fake_rpc)
        self.assertEqual(self.fake_rpc.urls, [self.cfg.rpc_url])


class TestAutoTighten(TuningBase):
    def _losing_ledger(self, bot_id, losses=6, wins=2, loss_pnl=-10.0,
                       win_pnl=10.0):
        for i in range(losses):
            record_trade(self.db, self.user_id, bot_id, f"L{i}",
                         "sell", 1.0, 90.0, 90.0, pnl_usd=loss_pnl,
                         mode="paper", now=f"2026-01-01T00:{i:02d}:00Z")
        for i in range(wins):
            record_trade(self.db, self.user_id, bot_id, f"W{i}",
                         "sell", 1.0, 110.0, 110.0, pnl_usd=win_pnl,
                         mode="paper",
                         now=f"2026-01-01T00:{10 + i:02d}:00Z")

    def _bot(self, stop_loss=7.0, buy_size=20.0, threshold=5.0):
        rules = scanner_ruleset()
        rules["buy"]["threshold_pct"] = threshold
        return self._insert_bot(rules=rules, stop_loss=stop_loss,
                                buy_size=buy_size)

    def test_threshold_tightens_first(self):
        bot_id = self._bot()
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        adj = review_bot(self.db, bot, self.cfg.encryption_key)
        self.assertIsNotNone(adj)
        self.assertEqual(adj["field"], "buy.threshold_pct")
        self.assertEqual(adj["old_value"], 5.0)
        self.assertEqual(adj["new_value"], 5.5)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["stop_loss_pct"], 7.0)
        self.assertEqual(bot["buy_size_pct"], 20.0)

    def test_stop_loss_tightens_last(self):
        bot_id = self._bot(threshold=15.0, buy_size=5.0)
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        adj = review_bot(self.db, bot, self.cfg.encryption_key)
        self.assertIsNotNone(adj)
        self.assertEqual(adj["field"], "stop_loss_pct")
        self.assertEqual(adj["new_value"], 6.0)

    def test_net_positive_bot_not_tightened(self):
        # Win rate 25% (2 wins / 8 trades) but net +$40: the old
        # rule tightened this bot; the net-negative gate must not.
        bot_id = self._bot()
        self._losing_ledger(bot_id, losses=6, wins=2, loss_pnl=-10.0,
                            win_pnl=50.0)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNone(review_bot(self.db, bot,
                                     self.cfg.encryption_key))


class TestSignalsKindGuards(TuningBase):
    def _signals_bot(self, coins, threshold=5.0):
        rules = {"version": 1, "kind": "signals",
                 "scan_sources": [],
                 "scan_interval_seconds": 3600,
                 "buy": {"metric": "gain_24h_pct",
                         "window_seconds": 86400,
                         "threshold_pct": threshold, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 100,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0,
                 "rug_sell": {"drop_pct": 15.0, "window_seconds": 60,
                              "drop_5m_pct": 25.0},
                 "max_new_positions_per_scan": 1}
        return self._insert_bot(rules=rules, coins=coins)

    def test_signals_bot_gets_confirmation_and_scam_gates(self):
        # A builderBot-created signals bot (long window): the
        # 2-scan confirmation + scam gates still apply, even though
        # the four-part momentum gate has no data for its kind.
        # Signals bots price their mints from the trenches feed
        # (fetch_prices_for_mints / fetch_mint_24h_changes) — mock
        # it so the test never hits live DexScreener.
        bot_id = self._signals_bot([SIGMINT])
        with mock.patch.object(
                sl, "fetch_prices_for_mints",
                return_value={SIGMINT: 0.0106}), mock.patch.object(
                sl, "fetch_mint_24h_changes",
                return_value={SIGMINT: 6.0}):
            outcomes = self._run([_entry(SIGMINT, 0.0106)])
            self.assertEqual(self._buys(outcomes), [])
            self._reset_scan(bot_id)
            outcomes = self._run([_entry(SIGMINT, 0.0106)])
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_signals_bot_scam_gate_blocks(self):
        self.fake_rpc.authorities[SIGMINT] = (True, False)
        bot_id = self._signals_bot([SIGMINT])
        with mock.patch.object(
                sl, "fetch_prices_for_mints",
                return_value={SIGMINT: 0.0106}), mock.patch.object(
                sl, "fetch_mint_24h_changes",
                return_value={SIGMINT: 6.0}):
            self._run([_entry(SIGMINT, 0.0106)])
            self._reset_scan(bot_id)
            outcomes = self._run([_entry(SIGMINT, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])


if __name__ == "__main__":
    unittest.main()
