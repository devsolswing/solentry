"""Gnat preset (2026-11-21): James's spec — curve + buy-pressure
sniper, tiered take-profit, non-negotiable stops, circuit breakers.

Every gate, TP tier, exit, preset default, Page 6 button and the
devnet carve-out is tested here, on the same fake-RPC / fake-feed /
fake-scan harness as the tuning tests. No live network in these
tests: the fake RPC serves the bonding-curve account (at a
configured progress) and the Metaplex metadata account (with a
configured URI) for the test mint, plus the tuning-test top-10 /
authority surface.

The honest map lives in rules.GNAT_NOTES and the staging MANIFEST:
5s/10s/15s/30s velocity windows, unique-buyer counts, creator
holdings / dev history, bundler clusters, single-dump % of curve
virtual SOL and creator-sell detection are NOT enforced — the app
has no data source for them. These tests assert the closest
versions that ARE enforced, and that the not-enforced rules are
never faked (no test pretends a creator check ran).
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
from helpers import csrf, make_client, make_config, signup
from tradingbot.builderbot import review_bot
from tradingbot.coin_meta import metadata_pda
from tradingbot.ledger import record_trade
from tradingbot.pumpfun import bonding_curve_pda, curve_progress_pct
from tradingbot.rules import (GNAT_NOTES, gnat_ruleset, preset_bots,
                              summarize_rules, validate_ruleset)
from tradingbot.silent_loop import (_gnat_buy_halt, _gnat_entry_gates,
                                    _gnat_hard_exit_reason,
                                    _gnat_momentum_exhausted,
                                    _gnat_time_decay_reason, run_once)

# Valid base58 addresses (no "I"/"O"/"l"/"0") — the curve PDA
# derivation decodes the mint, so the tuning tests' MINTA ("I")
# cannot be used here.
GNAT_MINT = "C111111111111111111111111111111111111111111"
GNAT_MINT2 = "D111111111111111111111111111111111111111111"
GNAT_MINT3 = "E111111111111111111111111111111111111111111"


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


def _gnat_entry(mint, price, social_links=2, devnet_watchlist=False,
                curve_real_sol=None, **over):
    """A Gnat candidate: the base gaining entry plus the fields the
    Gnat gates read — published social/website link count, and
    (for devnet test coins) the watchlist flag + curve SOL."""
    entry = _entry(mint, price, **over)
    entry["social_links"] = social_links
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
            + _borsh_str("Gnat Coin") + _borsh_str("GNAT")
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
    surface, plus the curve + metadata account reads the Gnat
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


    def tick_age_seconds(self, coin_id, now=None):
        # The loop's SOL read: a tick the feed holds is fresh
        # (age 0.0); a coin with no tick has no age at all.
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
        # The fake feed carries no history: no change figure,
        # never a guessed one.
        return None

    def sanitize_prices_data(self, data):
        return data

    def sanitize_trenches(self, data):
        return data

    def set_tick_listener(self, callback):
        self._listener = callback


class GnatBase(unittest.TestCase):
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
        self.fake_rpc = _FakeRpc(GNAT_MINT)
        for mint in (GNAT_MINT, GNAT_MINT2, GNAT_MINT3):
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

    def _seed_meta(self, mint, name="Gnat Coin", symbol="GNAT"):
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, ?, ?, NULL, 'metaplex', ?) "
            "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
            "symbol = excluded.symbol",
            (mint, name, symbol, _now()))
        self.db.commit()

    def _insert_bot(self, rules, name="Gnat", mode="paper", silent=1,
                    coins=None, buy_size=18, stop_loss=18, max_open=3):
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
        # 1-hour trend baseline for the momentum gate: the loop
        # fills change_1h_pct from the loop's own marks, so a
        # 5-minute baseline alone leaves the 1-hour trend unknown
        # and the gate fails closed. A mark already an hour old
        # needs no companion.
        try:
            _age = (datetime.now(timezone.utc)
                    - datetime.strptime(
                        when, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc)).total_seconds()
        except Exception:
            _age = 0
        if 300 <= _age < 3600:
            self.db.execute(
                "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
                "recorded_at) VALUES (?, ?, ?, ?)",
                (bot_id, mint, price, _now(-3700)))
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
        # Single-price source: a scan entry survives the loop's
        # reprice only with a live feed tick at its price (the
        # chain cannot price these test mints). Seed ticks for
        # entries the feed has none for — never overwriting a tick
        # a test set deliberately — and never a SOL tick, so tests
        # asserting "no live SOL price" still see none.
        if feed is None:
            feed = _FakeFeed()
        for _e in entries:
            feed.ticks.setdefault(_e["mint"], _e["price_usd"])
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
        self._insert_mark(bot_id, entry["mint"], 0.01, _now(-330))
        self._run([entry], feed=feed)
        self._reset_scan(bot_id)
        return self._run([entry], feed=feed)


class TestCurveProgress(GnatBase):
    def test_progress_from_virtual_reserves(self):
        # 30% filled: 70% of the initial virtual tokens remain.
        self.assertAlmostEqual(
            curve_progress_pct(
                {"virtual_token_reserves": 751_100_000_000_000}),
            30.0, places=4)

    def test_progress_bounds(self):
        self.assertEqual(
            curve_progress_pct(
                {"virtual_token_reserves": 1_073_000_000_000_000}),
            0.0)
        self.assertEqual(
            curve_progress_pct({"virtual_token_reserves": 0}), 100.0)

    def test_progress_unreadable_is_none(self):
        self.assertIsNone(curve_progress_pct(None))
        self.assertIsNone(curve_progress_pct({}))
        self.assertIsNone(
            curve_progress_pct({"virtual_token_reserves": "x"}))


class TestGnatHardExits(GnatBase):
    def test_hard_stop_18pct(self):
        # Exactly -18% fires; -17% does not.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.01, 0.0082),
                         "stop_loss")
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.01, 0.0083))

    def test_no_exit_before_tier1(self):
        # -10%: above the -18% stop, no breakeven armed yet.
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.01, 0.009))
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.011, 0.0105))

    def test_breakeven_plus2_armed_at_tier1(self):
        # Peak +40% (0.014): the floor moves to entry +2% (0.0102).
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.014, 0.0102),
                         "breakeven stop")
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.014, 0.0105))
        # Below +40% peak the breakeven never arms.
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.0139, 0.0102))

    def test_trailing_18pct_between_100_and_250(self):
        # Peak +100% (0.02): trailing floor is 18% below the peak.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.02, 0.0164),
                         "trailing stop")
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.02, 0.017))
        # The trailing floor sits above the breakeven floor.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.02, 0.011),
                         "trailing stop")

    def test_trailing_tightens_to_12pct_above_250(self):
        # Peak +300% (0.04): trailing floor is 12% below the peak.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.04, 0.0352),
                         "trailing stop")
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.04, 0.036))
        # Exactly +250% is still the 18% tier.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.035, 0.0287),
                         "trailing stop")
        self.assertIsNone(_gnat_hard_exit_reason(0.01, 0.035, 0.029))

    def test_hard_stop_beats_raised_floors(self):
        # A crash straight through every floor is still a stop.
        self.assertEqual(_gnat_hard_exit_reason(0.01, 0.04, 0.008),
                         "stop_loss")


class TestGnatTimeDecay(GnatBase):
    def test_decay_fires_after_90s_below_15pct(self):
        now = datetime.now(timezone.utc).timestamp()
        self.assertEqual(
            _gnat_time_decay_reason(now - 120, 0.011, 0.01, now),
            "time decay: not +15% within 90s of entry")

    def test_decay_never_fires_when_peak_hit_15pct(self):
        now = datetime.now(timezone.utc).timestamp()
        self.assertIsNone(
            _gnat_time_decay_reason(now - 120, 0.0116, 0.01, now))

    def test_decay_never_fires_inside_90s(self):
        now = datetime.now(timezone.utc).timestamp()
        self.assertIsNone(
            _gnat_time_decay_reason(now - 30, 0.011, 0.01, now))

    def test_decay_needs_an_open_time(self):
        now = datetime.now(timezone.utc).timestamp()
        self.assertIsNone(
            _gnat_time_decay_reason(None, 0.011, 0.01, now))


class TestGnatMomentumExhaustion(GnatBase):
    def test_exhaustion_fires(self):
        entry = {"change_5m_pct": -6.0}
        self.assertTrue(_gnat_momentum_exhausted(entry, 0.013, 0.01))

    def test_exhaustion_needs_25pct_peak(self):
        entry = {"change_5m_pct": -6.0}
        self.assertFalse(_gnat_momentum_exhausted(entry, 0.012, 0.01))

    def test_exhaustion_needs_5m_drop(self):
        entry = {"change_5m_pct": -4.0}
        self.assertFalse(_gnat_momentum_exhausted(entry, 0.013, 0.01))

    def test_exhaustion_missing_data_never_fires(self):
        self.assertFalse(_gnat_momentum_exhausted({}, 0.013, 0.01))
        self.assertFalse(
            _gnat_momentum_exhausted({"change_5m_pct": None}, 0.013,
                                     0.01))


class TestGnatEntryGates(GnatBase):
    def _gates(self, entry, rpc=None):
        return _gnat_entry_gates(self.db, self.cfg, entry,
                                 rpc or self.fake_rpc, {})

    def test_happy_path_passes(self):
        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106))
        self.assertTrue(ok, why)

    def test_curve_band_edges_pass(self):
        for progress in (12.0, 65.0):
            rpc = _FakeRpc(GNAT_MINT, progress_pct=progress)
            ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106),
                                  rpc=rpc)
            self.assertTrue(ok, f"{progress}%: {why}")

    def test_curve_below_band_rejected(self):
        rpc = _FakeRpc(GNAT_MINT, progress_pct=5.0)
        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106), rpc=rpc)
        self.assertFalse(ok)
        self.assertIn("5.0% filled", why)

    def test_curve_above_band_rejected(self):
        rpc = _FakeRpc(GNAT_MINT, progress_pct=70.0)
        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106), rpc=rpc)
        self.assertFalse(ok)
        self.assertIn("70.0% filled", why)

    def test_curve_unreadable_rejected_fail_closed(self):
        class _NoCurveRpc(_FakeRpc):
            def get_account_info(self, pubkey):
                if pubkey == self.curve_addr:
                    return None
                return super().get_account_info(pubkey)

        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106),
                              rpc=_NoCurveRpc(GNAT_MINT))
        self.assertFalse(ok)
        self.assertIn("curve", why)

    def test_too_few_buyers_rejected(self):
        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106, buys=10))
        self.assertFalse(ok)
        self.assertIn("10 buys", why)

    def test_low_buy_sell_ratio_rejected(self):
        ok, why = self._gates(
            _gnat_entry(GNAT_MINT, 0.0106, buys=40, sells=20))
        self.assertFalse(ok)
        self.assertIn("2.0:1", why)

    def test_missing_txn_data_rejected_fail_closed(self):
        entry = _gnat_entry(GNAT_MINT, 0.0106, buys=None, sells=None)
        ok, why = self._gates(entry)
        self.assertFalse(ok)
        self.assertIn("txn data", why)

    def test_zero_sells_is_an_infinite_ratio(self):
        # 40 buys, 0 sells: pure buying — the ratio gate's intent
        # is satisfied, not failed.
        ok, why = self._gates(
            _gnat_entry(GNAT_MINT, 0.0106, buys=40, sells=0))
        self.assertTrue(ok, why)

    def test_no_social_links_rejected(self):
        ok, why = self._gates(
            _gnat_entry(GNAT_MINT, 0.0106, social_links=0))
        self.assertFalse(ok)
        self.assertIn("socials", why)

    def test_missing_metadata_uri_rejected(self):
        rpc = _FakeRpc(GNAT_MINT, uri="")
        ok, why = self._gates(_gnat_entry(GNAT_MINT, 0.0106), rpc=rpc)
        self.assertFalse(ok)
        self.assertIn("metadata", why)

    def test_devnet_watchlist_skips_all_three_gates(self):
        # No txn data, no socials, curve below the band — the
        # devnet carve-out still passes: the gates would block
        # every devnet test buy otherwise.
        entry = _gnat_entry(GNAT_MINT, 0.0106, buys=None, sells=None,
                            social_links=0, devnet_watchlist=True)
        ok, why = self._gates(entry)
        self.assertTrue(ok, why)


class TestGnatRuleset(GnatBase):
    def test_ruleset_validates(self):
        cleaned, err = validate_ruleset(gnat_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["kind"], "gnat")
        self.assertEqual(cleaned["scan_interval_seconds"], 30)
        self.assertEqual(cleaned["buy"]["window_seconds"], 180)
        self.assertEqual(cleaned["buy"]["threshold_pct"], 5.0)

    def test_tp_tiers_are_35_at_40_then_35_at_100(self):
        cleaned, err = validate_ruleset(gnat_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(
            [(r["pct_of_position"], r["profit_pct"])
             for r in cleaned["sell_ladder"]],
            [(35.0, 40.0), (35.0, 100.0)])

    def test_trailing_and_top10_and_filter(self):
        cleaned, err = validate_ruleset(gnat_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["trailing_stop_pct"], 18.0)
        self.assertEqual(cleaned["top10_rug"]["concentration_pct"],
                         25.0)
        self.assertEqual(
            cleaned["buy_filter"]["min_volume_5m_usd"], 5000.0)
        self.assertEqual(
            cleaned["buy_filter"]["min_market_cap_usd"], 0.0)

    def test_gnat_rejects_trending_ladder_and_regime(self):
        rules = gnat_ruleset()
        rules["trending_ladder"] = [{"pct_of_position": 10,
                                     "profit_pct": 31}]
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)
        rules = gnat_ruleset()
        rules["regime"] = {"volume_surge_x": 2.0}
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_gnat_requires_the_3m_window(self):
        rules = gnat_ruleset()
        rules["buy"]["window_seconds"] = 300
        rules["buy"]["metric"] = "gain_5m_pct"
        _cleaned, err = validate_ruleset(rules)
        self.assertIsNotNone(err)

    def test_preset_bots_lists_middy_last(self):
        names = [p["name"] for p in preset_bots()]
        self.assertEqual(names, ["Clyde", "Trenches", "Scanner",
                                 "Gnat", "Pit Sweeper", "Middy"])
        gnat = next(p for p in preset_bots() if p["key"] == "gnat")
        self.assertEqual(gnat["endpoint"], "bot_preset_gnat")
        self.assertEqual(gnat["notes"], GNAT_NOTES)

    def test_summary_states_the_tiers_and_breakers(self):
        text = summarize_rules(gnat_ruleset())
        self.assertIn("35% at +40%", text)
        self.assertIn("breakeven +2%", text)
        self.assertIn("35%", text)
        self.assertIn("+100% (2x)", text)
        self.assertIn("18% up to +250%, then 12%", text)
        self.assertIn("90 seconds", text)
        self.assertIn("2.0 SOL", text)
        self.assertIn("4 losing trades", text)

    def test_notes_state_what_is_not_enforced(self):
        joined = " ".join(GNAT_NOTES)
        self.assertIn("NOT ENFORCED", joined)
        self.assertIn("creator", joined)
        self.assertIn("Jito", joined)
        self.assertIn("12%/20%", joined)


class TestGnatPresetRoute(GnatBase):
    def _choose_mode(self, mode):
        resp = self.client.get("/bots",
                               query_string={"mode": mode})
        self.assertEqual(resp.status_code, 200)

    def test_create_gnat_from_preset(self):
        self._choose_mode("paper")
        resp = self.client.post(
            "/bots/preset/gnat",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        row = self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = 'Gnat'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "Gnat bot was not created")
        # Gnat's hard numbers, on the bot and in its rules.
        self.assertEqual(row["stop_loss_pct"], 18.0)
        self.assertEqual(row["buy_size_pct"], 18.0)
        self.assertEqual(row["max_open_trades"], 3)
        self.assertEqual(row["mode"], "paper")
        self.assertEqual(row["silent"], 0)
        self.assertEqual(row["platform"], "pump.fun")
        self.assertEqual(row["category"], "Gnat")
        rules = json.loads(row["rules_json"])
        self.assertEqual(rules["kind"], "gnat")
        self.assertEqual(rules["scan_interval_seconds"], 30)
        self.assertEqual(
            [(r["pct_of_position"], r["profit_pct"])
             for r in rules["sell_ladder"]],
            [(35.0, 40.0), (35.0, 100.0)])
        # The created bot's rules validate — the loop can run them.
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err, err)
        # Lands on the new bot's detail page.
        self.assertIn("/bots/", resp.headers.get("Location", ""))

    def test_create_gnat_twice_goes_to_existing(self):
        self._choose_mode("paper")
        self.client.post(
            "/bots/preset/gnat",
            data={"csrf_token": csrf(self.client, "/bots")})
        resp = self.client.post(
            "/bots/preset/gnat",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        rows = self.db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND name = 'Gnat'",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1, "duplicate Gnat bot created")

    def test_bots_page_has_the_gnat_button(self):
        self._choose_mode("paper")
        html = self.client.get("/bots").data.decode()
        start = html.find('id="active-bots-list"')
        end = html.find('id="signer-card"', start)
        window = html[start:end]
        self.assertIn('action="/bots/preset/gnat"', window)
        self.assertIn("🦟", window)
        self.assertIn("<strong>Gnat</strong>", window)
        self.assertIn("bold · Buy 18% · Stop 18% · Max 3", window)


class TestGnatLoopBuys(GnatBase):
    def test_second_scan_buys_at_fixed_025_sol(self):
        # Live SOL $71.50: 0.25 SOL = $17.88 — not 18% of the $100
        # bankroll ($18.00). The fixed size is the point.
        bot_id = self._insert_bot(rules=gnat_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _gnat_entry(GNAT_MINT, 0.0106), feed=feed)
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["usd"], 17.875, places=2)
        self.assertIsNotNone(self._position(bot_id, GNAT_MINT))
        detail = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ? AND kind = "
            "'silent_buy' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()["detail"]
        self.assertIn("0.25 SOL size", detail)

    def test_no_live_sol_price_falls_back_to_pct_and_says_so(self):
        # No feed, snapshot prices only: the 0.25 SOL conversion
        # cannot run, so the loop falls back to 18% of the $100
        # bankroll ($18.00) and labels the fallback on the buy line.
        bot_id = self._insert_bot(rules=gnat_ruleset())
        outcomes = self._buy_twice(bot_id, _gnat_entry(GNAT_MINT, 0.0106))
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["usd"], 18.0, places=2)
        detail = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ? AND kind = "
            "'silent_buy' ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()["detail"]
        self.assertIn("fallback 18% size", detail)

    def test_first_scan_only_confirms_no_buy(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._insert_mark(bot_id, GNAT_MINT, 0.01, _now(-330))
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_curve_outside_band_never_buys(self):
        self.fake_rpc = _FakeRpc(GNAT_MINT, progress_pct=5.0)
        bot_id = self._insert_bot(rules=gnat_ruleset())
        outcomes = self._buy_twice(bot_id, _gnat_entry(GNAT_MINT, 0.0106))
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_low_buy_pressure_never_buys(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        outcomes = self._buy_twice(
            bot_id, _gnat_entry(GNAT_MINT, 0.0106, buys=10))
        self.assertEqual(self._buys(outcomes), [])

    def test_no_socials_never_buys(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        outcomes = self._buy_twice(
            bot_id, _gnat_entry(GNAT_MINT, 0.0106, social_links=0))
        self.assertEqual(self._buys(outcomes), [])

    def test_devnet_watchlist_buys_without_dexscreener_data(self):
        # The carve-out: no txn data, no socials, curve below the
        # band — the devnet test coin still buys, so exits stay
        # testable on devnet.
        bot_id = self._insert_bot(rules=gnat_ruleset())
        entry = _gnat_entry(GNAT_MINT, 0.0106, buys=None, sells=None,
                            social_links=0, devnet_watchlist=True,
                            curve_real_sol=0.5, volume_m5=None,
                            volume_h1=None)
        outcomes = self._buy_twice(bot_id, entry)
        self.assertEqual(len(self._buys(outcomes)), 1)
        self.assertIsNotNone(self._position(bot_id, GNAT_MINT))


class TestGnatLoopExits(GnatBase):
    def _held(self, bot_id, price=0.01, peak=0.01, stage=0,
              amount=100.0, opened_at=None, last_price=None):
        return self._insert_position(
            bot_id, GNAT_MINT, amount, 0.01, peak=peak, orig=100.0,
            stage=stage, opened_at=opened_at or _now(-30),
            last_price=last_price)

    def test_tier1_sells_35pct_at_plus40(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.014)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("sell ladder", sells[0]["reason"])
        self.assertIn("+40%", sells[0]["reason"])
        pos = self._position(bot_id, GNAT_MINT)
        self.assertAlmostEqual(float(pos["amount"]), 65.0, places=4)
        self.assertEqual(int(pos["ladder_stage"]), 1)

    def test_tier2_sells_another_35pct_of_original_at_2x(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.014, stage=1, amount=65.0)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.02)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("+100%", sells[0]["reason"])
        pos = self._position(bot_id, GNAT_MINT)
        self.assertAlmostEqual(float(pos["amount"]), 30.0, places=4)
        self.assertEqual(int(pos["ladder_stage"]), 2)

    def test_hard_stop_fires_at_minus18(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        # last_price is set to the test price so the rug sell's
        # mark leg (drop vs last_price) does not fire first — the
        # rug sell is checked before the stop by design.
        self._held(bot_id, last_price=0.0082)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0082)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "stop_loss")
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_breakeven_plus2_fires_after_tier1(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.014, stage=1, amount=65.0)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0102)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "breakeven stop")
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_trailing_18pct_fires_on_the_moonbag(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.02, stage=2, amount=30.0)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0164)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "trailing stop")
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_trailing_12pct_above_250(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.04, stage=2, amount=30.0)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0352)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "trailing stop")
        # 0.036 is above the 12% floor (0.0352): no exit.
        bot_id2 = self._insert_bot(rules=gnat_ruleset(), name="Gnat2")
        self._held(bot_id2, peak=0.04, stage=2, amount=30.0)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.036)])
        self.assertEqual(self._sells(outcomes), [])

    def test_time_decay_exits_after_90s_below_15pct(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.011, opened_at=_now(-120))
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.011)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("time decay", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, GNAT_MINT))

    def test_time_decay_spares_a_coin_that_hit_15pct(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.0116, opened_at=_now(-120))
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.011)])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, GNAT_MINT))

    def test_momentum_exhaustion_exits_the_rest(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._held(bot_id, peak=0.013)
        # The -6% 5-minute change now comes from the loop's own
        # marks: 0.0125 / 0.94 recorded 330s ago.
        self._insert_mark(bot_id, GNAT_MINT, 0.0125 / 0.94, _now(-330))
        outcomes = self._run([
            _gnat_entry(GNAT_MINT, 0.0125, change_5m=-6.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("momentum exhaustion", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, GNAT_MINT))


class TestGnatCircuitBreakers(GnatBase):
    def _losing_sells(self, bot_id, count, pnl_each=-10.0):
        for i in range(count):
            record_trade(self.db, self.user_id, bot_id, f"L{i}",
                         "sell", 1.0, 90.0, 90.0, pnl_usd=pnl_each,
                         mode="paper", now=_now())

    def test_daily_loss_breaker_halts_new_buys(self):
        # -$150 today at SOL $71.50 is past -2.0 SOL (-$143).
        bot_id = self._insert_bot(rules=gnat_ruleset())
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-150.0, mode="paper",
                     now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _gnat_entry(GNAT_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("circuit breaker" in a["reason"] for a in skips),
            skips)

    def test_daily_loss_breaker_needs_a_live_sol_price(self):
        # Same -$150 loss, no live SOL price: the SOL-denominated
        # breaker cannot be evaluated and does NOT trip — the buy
        # proceeds. (Documented fail-open; the 4-loss breaker
        # still applies.)
        bot_id = self._insert_bot(rules=gnat_ruleset())
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-150.0, mode="paper",
                     now=_now())
        outcomes = self._buy_twice(bot_id, _gnat_entry(GNAT_MINT, 0.0106))
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_four_consecutive_losses_halt_new_buys(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._losing_sells(bot_id, 4)
        outcomes = self._buy_twice(bot_id, _gnat_entry(GNAT_MINT, 0.0106))
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("circuit breaker" in a["reason"] for a in skips),
            skips)

    def test_three_losses_do_not_halt(self):
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._losing_sells(bot_id, 3)
        outcomes = self._buy_twice(bot_id, _gnat_entry(GNAT_MINT, 0.0106))
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_breaker_halts_buys_but_not_exits(self):
        # A halted bot still stops out of an open position.
        bot_id = self._insert_bot(rules=gnat_ruleset())
        self._losing_sells(bot_id, 4)
        # last_price set to the test price so the rug sell does not
        # fire first (see test_hard_stop_fires_at_minus18).
        self._insert_position(bot_id, GNAT_MINT, 100.0, 0.01,
                              last_price=0.0082)
        outcomes = self._run([_gnat_entry(GNAT_MINT, 0.0082)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "stop_loss")


class TestGnatAutoTightenExcluded(GnatBase):
    def test_losing_gnat_bot_is_never_auto_tightened(self):
        # 6 losses, 2 wins, net negative — a scanner bot would
        # tighten; Gnat's stops are non-negotiable, so nothing
        # changes.
        bot_id = self._insert_bot(rules=gnat_ruleset())
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
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(bot["stop_loss_pct"], 18.0)
        self.assertEqual(bot["buy_size_pct"], 18.0)
        rows = self.db.execute(
            "SELECT * FROM builderbot_adjustments WHERE bot_id = ?",
            (bot_id,)).fetchall()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
