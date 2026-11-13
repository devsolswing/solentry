"""Devnet-mint watchlist for the silent loop (devnet proof).

TEST-ONLY scaffolding, never for mainnet: on devnet the loop also
watches Gate + AuditTest as trenches candidates, priced from
their bonding curves. Proves (a) watchlist mints become
candidates on devnet with curve pricing, (b) the watchlist is
fully inert on mainnet, (c) the buy-filter carve-out, (d) no
DexScreener data is needed for them — and (e) the gains come
from the loop's own price marks.
"""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.config import (DEVNET_TRENCH_MINTS_DEFAULT, Config)
from tradingbot.devnet_watchlist import (active_mints, curve_price_usd,
                                         enrich_scan, watchlist_entry)
from tradingbot.pumpfun import TOKEN_BASE, bonding_curve_pda
from tradingbot.rules import scanner_ruleset, trenches_ruleset
from tradingbot.scanner import (BUY_FILTER_DEFAULT, gain_3m_pct,
                                gain_window_pct, passes_buy_filter)
from tradingbot.silent_loop import run_once

GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
AUDT = "3k6ZYa2Wh6P92VN2uGvKsmHiw2RQpcVgxpb447raf2kC"

LAMPORTS_PER_SOL = 1_000_000_000
SOL_PRICE_USD = 100.0


def _curve_bytes(vt, vs, rt, rs, supply, complete=False):
    """Bonding-curve account data per pumpfun.parse_bonding_curve."""
    return (b"\x00" * 8
            + int(vt).to_bytes(8, "little")
            + int(vs).to_bytes(8, "little")
            + int(rt).to_bytes(8, "little")
            + int(rs).to_bytes(8, "little")
            + int(supply).to_bytes(8, "little")
            + bytes([1 if complete else 0]))


def _gate_curve(vt=10 ** 12, vs=324 * 10 ** 8, rt=5 * 10 ** 14,
                rs=3 * 10 ** 8, supply=10 ** 15, complete=False):
    """Gate curve: spot (vs/vt)*1e-3 SOL/token = 3.24e-5 SOL,
    so $0.00324/token at SOL $100; real SOL in curve 0.3."""
    return _curve_bytes(vt, vs, rt, rs, supply, complete)


class _FakeRpc:
    """RPC client serving canned bonding-curve accounts."""

    def __init__(self, url, curves=None, balance_lamports=0):
        self.url = url
        self._curves = curves or {}
        self._balance = balance_lamports

    def get_account_info(self, pubkey):
        return self._curves.get(pubkey)

    def get_balance_lamports(self, pubkey):
        return self._balance


class _FailingRpc:
    def __init__(self, url):
        raise AssertionError("RPC must not be reached")

    def get_account_info(self, pubkey):
        raise AssertionError("RPC must not be reached")


class _FakeScan:
    def __init__(self, entries=None):
        self.entries = list(entries or [])

    def __call__(self, mints=None):
        prices = {e["mint"]: e for e in self.entries}
        return {"candidates": list(self.entries), "prices": prices,
                "source": "dexscreener", "as_of": "test", "error": None}


def _prices_with_sol(price=SOL_PRICE_USD):
    return {"prices": {"SOL": {"price": price, "change_24h": 0.0}},
            "source": "test", "as_of": "test"}


def _empty_prices():
    return {"prices": {}, "source": "snapshot", "as_of": "test"}


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class TestWatchlistConfig(unittest.TestCase):
    def test_defaults_are_gate_and_audittest(self):
        self.assertEqual(DEVNET_TRENCH_MINTS_DEFAULT, (GATE, AUDT))
        cfg = Config(env={})
        self.assertEqual(cfg.devnet_trench_mints, [GATE, AUDT])

    def test_env_override_comma_separated(self):
        cfg = Config(env={"DEVNET_TRENCH_MINTS": GATE + ", " + AUDT})
        self.assertEqual(cfg.devnet_trench_mints, [GATE, AUDT])
        cfg = Config(env={"DEVNET_TRENCH_MINTS": GATE})
        self.assertEqual(cfg.devnet_trench_mints, [GATE])

    def test_env_empty_means_no_watchlist(self):
        cfg = Config(env={"DEVNET_TRENCH_MINTS": ""})
        self.assertEqual(cfg.devnet_trench_mints, [])

    def test_active_mints_devnet_only(self):
        devnet = Config(env={"NETWORK": "devnet"})
        self.assertEqual(active_mints(devnet), [GATE, AUDT])
        mainnet = Config(env={"NETWORK": "mainnet"})
        self.assertEqual(active_mints(mainnet), [])
        self.assertEqual(
            active_mints(Config(env={"NETWORK": "devnet",
                                     "DEVNET_TRENCH_MINTS": ""})), [])


class TestCurvePricing(unittest.TestCase):
    def setUp(self):
        self.curve_addr = bonding_curve_pda(GATE)[0]
        self.rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": _gate_curve()}})

    def test_spot_price_from_curve_reserves(self):
        price, curve = curve_price_usd(self.rpc, GATE, SOL_PRICE_USD)
        # (3.24e10 / 1e12) * 1e-3 SOL/token * $100 = $0.00324.
        self.assertAlmostEqual(price, 0.00324, places=8)
        self.assertFalse(curve["complete"])

    def test_missing_curve_is_none(self):
        rpc = _FakeRpc("https://api.devnet.solana.com", {})
        price, curve = curve_price_usd(rpc, GATE, SOL_PRICE_USD)
        self.assertIsNone(price)
        self.assertIsNone(curve)

    def test_complete_curve_is_none(self):
        rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": _gate_curve(complete=True)}})
        price, _curve = curve_price_usd(rpc, GATE, SOL_PRICE_USD)
        self.assertIsNone(price)

    def test_unreadable_curve_is_none(self):
        rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": b"short"}})
        price, _curve = curve_price_usd(rpc, GATE, SOL_PRICE_USD)
        self.assertIsNone(price)

    def test_no_sol_price_is_none(self):
        price, _curve = curve_price_usd(self.rpc, GATE, None)
        self.assertIsNone(price)
        price, _curve = curve_price_usd(self.rpc, GATE, 0)
        self.assertIsNone(price)

    def test_rpc_error_is_none(self):
        class _Boom:
            def get_account_info(self, pubkey):
                raise RuntimeError("boom")
        price, _curve = curve_price_usd(_Boom(), GATE, SOL_PRICE_USD)
        self.assertIsNone(price)

    def test_entry_shape(self):
        entry = watchlist_entry(self.rpc, GATE, SOL_PRICE_USD)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["mint"], GATE)
        self.assertEqual(entry["symbol"], "GATE")
        self.assertEqual(entry["name"], "Gate")
        self.assertAlmostEqual(entry["price_usd"], 0.00324, places=8)
        self.assertIsNone(entry["change_5m_pct"])
        self.assertIsNone(entry["volume_m5_usd"])
        self.assertIsNone(entry["txns_m5_buys"])
        self.assertTrue(entry["devnet_watchlist"])
        self.assertAlmostEqual(entry["curve_real_sol"], 0.3)
        # Market cap from the curve's total supply: $0.00324 x 1e9.
        self.assertAlmostEqual(entry["market_cap_usd"], 3240000.0)

    def test_entry_none_when_unpriceable(self):
        rpc = _FakeRpc("https://api.devnet.solana.com", {})
        self.assertIsNone(watchlist_entry(rpc, GATE, SOL_PRICE_USD))


class TestEnrichScan(unittest.TestCase):
    def setUp(self):
        self.curve_addr = bonding_curve_pda(GATE)[0]
        self.audt_addr = bonding_curve_pda(AUDT)[0]
        self.rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": _gate_curve()},
            self.audt_addr: {"data": _gate_curve()}})
        self.devnet = Config(env={"NETWORK": "devnet"})
        self.mainnet = Config(env={"NETWORK": "mainnet"})
        self.scan = {"candidates": [], "prices": {},
                     "source": "dexscreener", "as_of": "test",
                     "error": None}

    def test_devnet_adds_both_mints(self):
        # _FakeRpc needs the canned curves: build a factory.
        factory = lambda url: self.rpc  # noqa: E731
        out = enrich_scan(self.scan, self.devnet, factory, SOL_PRICE_USD)
        self.assertEqual({c["mint"] for c in out["candidates"]},
                         {GATE, AUDT})
        self.assertEqual(set(out["prices"]), {GATE, AUDT})
        self.assertTrue(all(c.get("devnet_watchlist")
                            for c in out["candidates"]))

    def test_mainnet_is_fully_inert(self):
        factory = lambda url: self.rpc  # noqa: E731
        out = enrich_scan(self.scan, self.mainnet, factory, SOL_PRICE_USD)
        self.assertEqual(out, self.scan)
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["prices"], {})

    def test_rpc_never_touched_on_mainnet(self):
        out = enrich_scan(self.scan, self.mainnet, _FailingRpc,
                          SOL_PRICE_USD)
        self.assertEqual(out, self.scan)

    def test_no_sol_price_leaves_scan_unchanged(self):
        factory = lambda url: self.rpc  # noqa: E731
        out = enrich_scan(self.scan, self.devnet, factory, None)
        self.assertEqual(out, self.scan)

    def test_errored_scan_unchanged(self):
        factory = lambda url: self.rpc  # noqa: E731
        bad = dict(self.scan, error="feed down")
        self.assertEqual(enrich_scan(bad, self.devnet, factory,
                                     SOL_PRICE_USD), bad)

    def test_dexscreener_entry_is_never_overwritten(self):
        dex = {"mint": GATE, "symbol": "GATE", "name": "Gate",
               "price_usd": 0.009, "change_5m_pct": 7.0,
               "volume_m5_usd": 5000.0, "volume_h1_usd": 10000.0,
               "txns_m5_buys": 30, "txns_m5_sells": 5,
               "market_cap_usd": None, "dex_id": "pumpfun",
               "pair_address": "pair", "pair_created_at": 0,
               "change_1h_pct": None, "change_6h_pct": None,
               "change_24h_pct": None, "liquidity_usd": 1000}
        scan = {"candidates": [dex], "prices": {GATE: dex},
                "source": "dexscreener", "as_of": "test", "error": None}
        factory = lambda url: self.rpc  # noqa: E731
        out = enrich_scan(scan, self.devnet, factory, SOL_PRICE_USD)
        # Gate keeps its DexScreener entry; AuditTest is added.
        self.assertEqual(out["prices"][GATE]["price_usd"], 0.009)
        self.assertNotIn("devnet_watchlist", out["prices"][GATE])
        self.assertEqual({c["mint"] for c in out["candidates"]},
                         {GATE, AUDT})

    def test_rpc_failure_leaves_scan_unchanged(self):
        def _boom(url):
            raise RuntimeError("rpc down")
        out = enrich_scan(self.scan, self.devnet, _boom, SOL_PRICE_USD)
        self.assertEqual(out, self.scan)

    def test_unpriceable_mint_skipped_others_kept(self):
        rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": _gate_curve()}})
        factory = lambda url: rpc  # noqa: E731
        out = enrich_scan(self.scan, self.devnet, factory, SOL_PRICE_USD)
        self.assertEqual({c["mint"] for c in out["candidates"]}, {GATE})

    def test_input_is_not_mutated(self):
        factory = lambda url: self.rpc  # noqa: E731
        before = copy.deepcopy(self.scan)
        enrich_scan(self.scan, self.devnet, factory, SOL_PRICE_USD)
        self.assertEqual(self.scan, before)


class TestWatchlistFilter(unittest.TestCase):
    def _entry(self, curve_sol=0.3, mcap=3240000.0):
        return {"mint": GATE, "symbol": "GATE", "name": "Gate",
                "price_usd": 0.00324, "change_5m_pct": None,
                "volume_m5_usd": None, "volume_h1_usd": None,
                "txns_m5_buys": None, "txns_m5_sells": None,
                "market_cap_usd": mcap, "devnet_watchlist": True,
                "curve_real_sol": curve_sol}

    def test_flagged_passes_when_active(self):
        ok, why = passes_buy_filter(self._entry(), None, 6.0,
                                    devnet_watchlist_active=True)
        self.assertTrue(ok, why)

    def test_flagged_fails_when_inactive(self):
        # No carve-out on mainnet: the normal filter rejects it
        # for its missing 5-minute volume data.
        ok, why = passes_buy_filter(self._entry(), None, 6.0,
                                    devnet_watchlist_active=False)
        self.assertFalse(ok)
        self.assertIn("volume", why)

    def test_flagged_fails_without_curve_liquidity(self):
        ok, why = passes_buy_filter(self._entry(curve_sol=0.0), None,
                                    6.0, devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("SOL", why)
        entry = self._entry()
        del entry["curve_real_sol"]
        ok, why = passes_buy_filter(entry, None, 6.0,
                                    devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("liquidity", why)

    def test_flagged_weak_signal_rejected(self):
        ok, why = passes_buy_filter(self._entry(), None, 4.0,
                                    devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("signal", why)

    def test_flagged_no_signal_rejected(self):
        ok, why = passes_buy_filter(self._entry(), None, None,
                                    devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("signal strength", why)

    def test_flagged_mcap_minimum_enforced(self):
        cfg = dict(BUY_FILTER_DEFAULT, min_market_cap_usd=45000.0)
        ok, _why = passes_buy_filter(self._entry(), cfg, 6.0,
                                     devnet_watchlist_active=True)
        self.assertTrue(ok)
        cfg = dict(BUY_FILTER_DEFAULT, min_market_cap_usd=5000000.0)
        ok, why = passes_buy_filter(self._entry(), cfg, 6.0,
                                    devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("market cap", why)

    def test_unflagged_entry_unchanged(self):
        entry = self._entry()
        del entry["devnet_watchlist"]
        del entry["curve_real_sol"]
        ok, why = passes_buy_filter(entry, None, 6.0,
                                    devnet_watchlist_active=True)
        self.assertFalse(ok)
        self.assertIn("volume", why)


class TestGainWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Marks', 'bold', '[]', 20, 7, "
            "'paper', 0, 3, ?)",
            (self.user_id, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        self.bot_id = cur.lastrowid

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_mark(self, bot_id, mint, price, seconds_ago):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, _fmt(ts.timestamp())))
        self.db.commit()

    def test_gain_5m_from_old_mark(self):
        self._seed_mark(self.bot_id, GATE, 0.003, 360)
        gain = gain_window_pct(self.db, self.bot_id, GATE, 0.00324,
                               datetime.now(timezone.utc).timestamp(), 300)
        self.assertAlmostEqual(gain, 8.0, places=5)

    def test_no_mark_old_enough_is_none(self):
        self._seed_mark(self.bot_id, GATE, 0.003, 60)
        gain = gain_window_pct(self.db, self.bot_id, GATE, 0.00324,
                               datetime.now(timezone.utc).timestamp(), 300)
        self.assertIsNone(gain)

    def test_gain_3m_still_works(self):
        self._seed_mark(self.bot_id, GATE, 0.003, 200)
        gain = gain_3m_pct(self.db, self.bot_id, GATE, 0.00324,
                           datetime.now(timezone.utc).timestamp())
        self.assertAlmostEqual(gain, 8.0, places=5)


class TestWatchlistLoop(unittest.TestCase):
    """End to end in the silent loop: a watchlist mint becomes a
    buy on devnet once its marks show the gain, and never on
    mainnet."""

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
        self.curve_addr = bonding_curve_pda(GATE)[0]
        self.rpc = _FakeRpc("https://api.devnet.solana.com", {
            self.curve_addr: {"data": _gate_curve()}})

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _insert_bot(self, rules, mode="paper"):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json) "
            "VALUES (?, ?, 'bold', '[]', 20, 7, ?, 1, 3, ?, "
            "'Memecoin build', ?)",
            (self.user_id, "Trenches",
             mode, hashlib.sha256(b"tok").hexdigest(),
             json.dumps(rules)))
        self.db.commit()
        return cur.lastrowid

    def _seed_mark(self, bot_id, mint, price, seconds_ago):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, _fmt(ts.timestamp())))
        self.db.commit()

    def _position(self, bot_id, coin):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, coin)).fetchone()

    def _run(self, cfg=None):
        rpc = self.rpc
        return run_once(
            self.db, cfg or self.cfg,
            price_fetcher=lambda: _prices_with_sol(),
            rpc_factory=lambda url: rpc,
            trenches_fetcher=_FakeScan([]))

    def test_trenches_buys_watchlist_mint_on_devnet(self):
        bot_id = self._insert_bot(trenches_ruleset())
        # Mark 6 minutes old at $0.003; curve now $0.00324 (+8%).
        self._seed_mark(bot_id, GATE, 0.003, 360)
        outcomes = self._run()
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, GATE))

    def test_trenches_no_buy_without_old_mark(self):
        # First sighting: marks exist only from this scan, so the
        # 5-minute gain is None — no buy, but a mark is recorded.
        bot_id = self._insert_bot(trenches_ruleset())
        outcomes = self._run()
        self.assertIsNone(self._position(bot_id, GATE))
        marks = self.db.execute(
            "SELECT * FROM scanner_marks WHERE bot_id = ? AND mint = ?",
            (bot_id, GATE)).fetchall()
        self.assertEqual(len(marks), 1)
        self.assertEqual(outcomes[0]["actions"], [])

    def test_trenches_no_buy_when_gain_below_bar(self):
        bot_id = self._insert_bot(trenches_ruleset())
        # +2% in 5 minutes: below the 3.4% threshold and the 5%
        # filter bar alike.
        self._seed_mark(bot_id, GATE, 0.00318, 360)
        self._run()
        self.assertIsNone(self._position(bot_id, GATE))

    def test_mainnet_loop_never_buys_watchlist_mint(self):
        bot_id = self._insert_bot(trenches_ruleset())
        self._seed_mark(bot_id, GATE, 0.003, 360)
        mainnet_cfg = Config(env={**os.environ, "NETWORK": "mainnet"})
        self._run(cfg=mainnet_cfg)
        self.assertIsNone(self._position(bot_id, GATE))

    def test_no_sol_price_means_no_watchlist_buy(self):
        bot_id = self._insert_bot(trenches_ruleset())
        self._seed_mark(bot_id, GATE, 0.003, 360)
        run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                 rpc_factory=lambda url: self.rpc,
                 trenches_fetcher=_FakeScan([]))
        self.assertIsNone(self._position(bot_id, GATE))

    def test_scanner_buys_watchlist_mint_on_devnet(self):
        bot_id = self._insert_bot(scanner_ruleset())
        # Mark 200s old at $0.003; curve now $0.00324 (+8% in 3m).
        self._seed_mark(bot_id, GATE, 0.003, 200)
        outcomes = self._run()
        self.assertEqual(outcomes[0]["actions"][0]["action"], "buy")
        self.assertIsNotNone(self._position(bot_id, GATE))


if __name__ == "__main__":
    unittest.main()
