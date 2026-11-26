"""Chain-read fallback fix (2026-11-25): the backup path that never
fired.

The app-wide price fix shipped a chain-read fallback (one batched
Helius RPC read of the bonding curves) that never executed in the
16:00-17:30 MST window on 2026-11-25 — zero log lines, rows frozen
while the Helius stream stalled 58x in 90 minutes. Three silent
kill switches, each pinned here:

1. SOL-tick gate — the fallback needed a SOL tick <=90s old from
   the feed, which comes from a separate Jupiter thread. If that
   loop was dead, every fallback path silently returned {}. Now
   the SOL rate is decoupled (sol_rate_for_conversion): fresh feed
   tick, else a direct Jupiter fetch (5s timeout), else the
   last-known-good tick labeled stale in the logs.
2. Silent skips — no log line anywhere when the fallback gave up.
   Now every skip logs a warning: no SOL rate at all, ingest skip,
   and the per-poll count of chain prices the jump guard would
   have dropped.
3. Jump-guard veto — _coin_rows dropped chain prices >50% from the
   last good tick, so a genuine step change after a stall was
   dropped forever. Now the chain price is SHOWN (matching the
   trenches path, which never had the veto), marked price_moved,
   and logged. Feed-sourced prices still pass the guard.
4. Server-driven refresh — _refresh_stale_mints ran only inside
   the websocket recv loop, which the 45s watchdog kills (then up
   to 60s reconnect backoff). Now it runs on its own timer thread.
5. /api/live — the 4s coin-detail snapshot had no chain read at
   all. Now it chain-reads missing or stale coins (valid mint
   addresses only).

No live upstream is contacted here: the Jupiter fetch is always
patched (raise or return a canned rate), the RPC is always FakeRpc
(the canned live curve), and _chain_prices_for_mints is mocked
where the route is the subject.
"""

import tempfile
import time
import unittest
from unittest import mock

import tradingbot.routes as routes
import tradingbot.streaming as streaming_mod
from helpers import make_client, signup
from test_helius_stream import (FakeRpc, GATE_MAINNET, SOL_PRICE,
                                _live_curve)
from test_livefeed import make_prices
from tradingbot.config import Config
from tradingbot.livefeed import LiveFeed
from tradingbot.pumpfun import curve_spot_price_sol, parse_bonding_curve
from tradingbot.routes import _coin_rows
from tradingbot.streaming import HeliusStream


def _curve_sol():
    """The canned live curve's spot price, in SOL."""
    return curve_spot_price_sol(parse_bonding_curve(_live_curve()))


class _StreamBase(unittest.TestCase):
    """Fresh feed + stream, the canned curve as the only RPC."""

    def _stream(self, feed, sol_age=None, sol_price=SOL_PRICE):
        if sol_age is not None:
            feed.ingest("SOL", sol_price, "jupiter",
                        now=time.time() - sol_age)
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        stream = HeliusStream(
            feed, cfg, rpc_factory=lambda url: FakeRpc(_live_curve()))
        stream._subs[GATE_MAINNET] = "curveaddr"
        stream._sub_id[42] = GATE_MAINNET
        stream._mint_sub_id[GATE_MAINNET] = 42
        return stream


class TestChainPricesSolDecoupled(_StreamBase):
    """Fix 1: _chain_prices_for_mints converts at a SOL rate that
    does not depend on the feed's SOL tick being fresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.feed = self.app.config["LIVE_FEED"]

    def tearDown(self):
        self.tmp.cleanup()

    def _chain(self):
        with mock.patch.object(routes, "rpc_client_for_config",
                               return_value=FakeRpc(_live_curve())):
            return routes._chain_prices_for_mints(self.app,
                                                  [GATE_MAINNET])

    def test_stale_sol_tick_uses_jupiter_fetch(self):
        # SOL tick 91s old (past the 90s bound): the direct Jupiter
        # fetch converts the chain read instead of returning {}.
        self.feed.ingest("SOL", SOL_PRICE, "jupiter",
                         now=time.time() - 91)
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               return_value=100.0) as fetch:
            prices = self._chain()
        fetch.assert_called_once_with(timeout=5.0)
        self.assertEqual(prices, {GATE_MAINNET: _curve_sol() * 100.0})

    def test_stale_sol_tick_uses_last_good_when_jupiter_fails(self):
        # SOL tick 91s old AND the Jupiter fetch down: the
        # last-known-good tick converts, and the staleness is
        # labeled in the logs instead of failing silently.
        self.feed.ingest("SOL", SOL_PRICE, "jupiter",
                         now=time.time() - 91)
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               side_effect=Exception("down")), \
                self.assertLogs("tradingbot.routes",
                                level="WARNING") as logs:
            prices = self._chain()
        self.assertEqual(prices, {GATE_MAINNET: _curve_sol() * SOL_PRICE})
        self.assertTrue(any("last-known-good" in m
                            for m in logs.output))

    def test_no_sol_rate_returns_empty_and_logs(self):
        # No SOL tick anywhere and the Jupiter fetch down: no rate
        # at all, so the read returns {} — and the skip is logged,
        # never silent (fix 2).
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               side_effect=Exception("down")), \
                self.assertLogs("tradingbot.routes",
                                level="WARNING") as logs:
            prices = self._chain()
        self.assertEqual(prices, {})
        self.assertTrue(any("no SOL rate" in m for m in logs.output))

    def test_fresh_sol_tick_never_fetches(self):
        # Happy path unchanged: a fresh feed tick converts, and no
        # Jupiter fetch fires at all.
        self.feed.ingest("SOL", SOL_PRICE, "jupiter")
        fetch = mock.Mock(side_effect=AssertionError(
            "fresh tick must not fetch"))
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               fetch):
            prices = self._chain()
        fetch.assert_not_called()
        self.assertEqual(prices, {GATE_MAINNET: _curve_sol() * SOL_PRICE})


class TestCoinRowsJumpGuard(unittest.TestCase):
    """Fix 3: the jump guard never vetoes a chain price on the poll
    path — it is shown, marked price_moved, and logged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 1000.0, 0.002)",
            (self.user_id, GATE_MAINNET))
        self.db.commit()
        self.prices = make_prices({"SOL": (10.0, 0.0)})

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, feed):
        patches = [
            mock.patch.object(routes, "_chain_prices_for_mints",
                              return_value={GATE_MAINNET: 400.0}),
            mock.patch.object(routes, "fetch_mint_identities",
                              return_value={}),
            mock.patch.object(routes, "ensure_coin_meta",
                              return_value={"name": "Gate",
                                            "symbol": "GATE"}),
            mock.patch.object(routes, "enrich_row",
                              side_effect=lambda row, **kw: row),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return _coin_rows(self.db, self.user_id, "paper", self.prices,
                          feed=feed, app=self.app)

    def test_shows_chain_price_after_big_move(self):
        # Stale feed tick ($0.002, 120s old) + chain read at $400:
        # the chain price IS the row now — $400,000 of value, flagged
        # price_moved, and the would-have-dropped count is logged.
        stale_feed = LiveFeed()
        stale_feed.ingest(GATE_MAINNET, 0.002, "helius-stream",
                          now=time.time() - 120)
        with self.assertLogs("tradingbot.routes",
                             level="WARNING") as logs:
            rows, _stale = self._rows(stale_feed)
        row = next(r for r in rows if r["coin_id"] == GATE_MAINNET)
        self.assertEqual(row["price"], 400.0)
        self.assertAlmostEqual(row["value"], 400000.0)
        self.assertTrue(row["price_moved"])
        self.assertTrue(any("would have dropped" in m
                            for m in logs.output))

    def test_fresh_feed_tick_still_wins_over_chain_spike(self):
        # The guard still applies to feed-sourced prices: a fresh
        # feed tick at $0.002 wins over the $400 chain read, and
        # nothing is flagged.
        fresh_feed = LiveFeed()
        fresh_feed.ingest(GATE_MAINNET, 0.002, "helius-stream")
        rows, _stale = self._rows(fresh_feed)
        row = next(r for r in rows if r["coin_id"] == GATE_MAINNET)
        self.assertEqual(row["price"], 0.002)
        self.assertAlmostEqual(row["value"], 2.0)
        self.assertFalse(row["price_moved"])


class TestStreamRefreshSolDecoupled(_StreamBase):
    """Fixes 1 + 2 on the stream side: _refresh_stale_mints and
    _ingest_sol_price convert without a fresh feed SOL tick, and
    the skip is logged when no rate exists at all."""

    def test_refresh_ingests_with_stale_sol_tick(self):
        feed = LiveFeed()
        stream = self._stream(feed, sol_age=91)
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               return_value=100.0):
            stream._refresh_stale_mints()
        self.assertAlmostEqual(feed.live_price(GATE_MAINNET),
                               _curve_sol() * 100.0)

    def test_refresh_uses_last_good_when_jupiter_fails(self):
        feed = LiveFeed()
        stream = self._stream(feed, sol_age=91)
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               side_effect=Exception("down")), \
                self.assertLogs("tradingbot.streaming",
                                level="WARNING") as logs:
            stream._refresh_stale_mints()
        self.assertAlmostEqual(feed.live_price(GATE_MAINNET),
                               _curve_sol() * SOL_PRICE)
        self.assertTrue(any("last-known-good" in m
                            for m in logs.output))

    def test_ingest_skips_and_logs_when_no_sol_rate(self):
        feed = LiveFeed()
        stream = self._stream(feed)  # no SOL tick at all
        with mock.patch.object(streaming_mod, "get_sol_price_usd",
                               side_effect=Exception("down")), \
                self.assertLogs("tradingbot.streaming",
                                level="WARNING") as logs:
            self.assertFalse(stream._ingest_sol_price(
                GATE_MAINNET, _curve_sol(), now=time.time()))
        self.assertEqual(feed.snapshot([GATE_MAINNET]), {})
        self.assertTrue(any("no SOL rate" in m for m in logs.output))


class TestRefreshThreadIndependent(_StreamBase):
    """Fix 4: the stale refresh runs on its own timer thread — a
    dead websocket can no longer put it to sleep with it."""

    def test_refresh_thread_ingests_without_a_socket(self):
        feed = LiveFeed()
        feed.ingest("SOL", SOL_PRICE, "jupiter")
        # The mint is WATCHED in the feed (not assigned to _subs
        # directly): the running loop re-derives _subs from watched
        # + held mints on every pass, so a direct assignment would
        # be wiped by _refresh_subscriptions before the timer
        # thread ever saw it.
        feed.subscribe_mint(GATE_MAINNET)
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        stream = HeliusStream(
            feed, cfg,
            rpc_factory=lambda url: FakeRpc(_live_curve()),
            ws_factory=lambda url, timeout=None: (
                _ for _ in ()).throw(Exception("socket down")))
        try:
            with mock.patch.object(streaming_mod,
                                   "STALE_REFRESH_SECONDS", 0.05):
                stream.start()
                # The recv loop never connects (every attempt
                # raises); the timer thread still chain-reads the
                # mint within a few refresh cycles.
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if feed.live_price(GATE_MAINNET) is not None:
                        break
                    time.sleep(0.05)
                self.assertAlmostEqual(
                    feed.live_price(GATE_MAINNET), _curve_sol() * SOL_PRICE)
                self.assertIsNotNone(stream._refresh_thread)
                self.assertTrue(stream._refresh_thread.is_alive())
        finally:
            stream.stop()
            stream._thread.join(timeout=5)
            stream._refresh_thread.join(timeout=5)
        self.assertFalse(stream._thread.is_alive())
        self.assertFalse(stream._refresh_thread.is_alive())


class TestApiLiveFallback(unittest.TestCase):
    """Fix 5: /api/live chain-reads coins the snapshot cannot price,
    so the coin-detail page has a fallback path too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", SOL_PRICE, "jupiter")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain_read_for_missing_coin(self):
        with mock.patch.object(routes, "_chain_prices_for_mints",
                               return_value={GATE_MAINNET: 0.0042}
                               ) as chain:
            resp = self.client.get(f"/api/live?coins={GATE_MAINNET}")
        self.assertEqual(resp.status_code, 200)
        tick = resp.get_json()["ticks"][GATE_MAINNET]
        self.assertEqual(tick["price"], 0.0042)
        self.assertEqual(tick["source"], "chain")
        self.assertFalse(tick["stale"])
        chain.assert_called_once()
        self.assertEqual(chain.call_args.args[1], [GATE_MAINNET])

    def test_chain_read_for_stale_tick(self):
        self.feed.ingest(GATE_MAINNET, 0.001, "helius-stream",
                         now=time.time() - 120)
        with mock.patch.object(routes, "_chain_prices_for_mints",
                               return_value={GATE_MAINNET: 0.0042}):
            resp = self.client.get(f"/api/live?coins={GATE_MAINNET}")
        tick = resp.get_json()["ticks"][GATE_MAINNET]
        self.assertEqual(tick["price"], 0.0042)
        self.assertEqual(tick["source"], "chain")

    def test_fresh_tick_is_not_chain_read(self):
        self.feed.ingest(GATE_MAINNET, 0.001, "helius-stream")
        with mock.patch.object(routes, "_chain_prices_for_mints"
                               ) as chain:
            resp = self.client.get(f"/api/live?coins={GATE_MAINNET}")
        tick = resp.get_json()["ticks"][GATE_MAINNET]
        self.assertEqual(tick["price"], 0.001)
        self.assertEqual(tick["source"], "helius-stream")
        chain.assert_not_called()

    def test_no_coins_param_skips_chain_read(self):
        with mock.patch.object(routes, "_chain_prices_for_mints"
                               ) as chain:
            resp = self.client.get("/api/live")
        self.assertEqual(resp.status_code, 200)
        chain.assert_not_called()

    def test_symbol_is_not_chain_read(self):
        # A symbol like BTC has no curve to read — it is never
        # passed to the chain read.
        with mock.patch.object(routes, "_chain_prices_for_mints"
                               ) as chain:
            resp = self.client.get("/api/live?coins=BTC")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("BTC", resp.get_json()["ticks"])
        chain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
