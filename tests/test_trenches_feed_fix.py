"""Trenches feed fix (2026-11-26).

James: "Fix the trenches feed it's stale and no new coins" +
"Keep it update in real time."

Diagnosis (Railway logs 2026-11-26 ~12:38-13:39, deployment
e65143de, commit c40a8767 — app up, feed dead):
- Helius stream delivered NO data all hour: 39x "no stream data
  for 45s". The reconnect loop works (the 39 lines ARE the cycle);
  the subscribed set was held + browser-watched mints only, and
  with no browser on the page and quiet holdings the curves sent
  nothing. The fresh coins on the Trenches page were never
  subscribed at all.
- The page's 5s poll never fired: ZERO /api/trenches hits in the
  hour (one GET /trenches at 13:35:19). The endpoint and the JS
  exist at head; the poll not firing is client-side (stale cached
  live.js is the prime suspect), so the fix must not depend on the
  phone's JS: cache-busted shell, no-store /sw.js, and a
  meta-refresh backstop on the page.
- The list ranked by 1-hour gain, so a coin that pumped hours ago
  held its slot (live data 2026-11-26: +488% 1h with a +0.6% 5m
  sat #2) and fresh coins never appeared.

What this file pins:
- the stream server-watches the trenches candidates (loop + page),
  held mints first under the subscription cap;
- the list ranks by 5-minute gain (the bot's own buy metric);
- a failed / empty scan logs a warning, never silently;
- the app shell loads cache-busted (?v=<mtime>), /sw.js is
  no-store, and the Trenches page carries a 30s meta-refresh
  backstop that live.js removes once its poll starts.
"""

import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.config import Config
from tradingbot.livefeed import LiveFeed
from tradingbot.rules import trenches_ruleset
from tradingbot.silent_loop import run_once
from tradingbot.solana import generate_keypair
from tradingbot.streaming import HeliusStream

ROOT = pathlib.Path(__file__).resolve().parent.parent

MINT1 = generate_keypair()[1]
MINT2 = generate_keypair()[1]
MINT3 = generate_keypair()[1]


def _scan(candidates):
    # The real fetch_trenches_scan populates BOTH the candidates
    # list and the prices map (the loop's reprice keys off the map);
    # a scan with an empty prices map reprices to zero candidates.
    return {"candidates": candidates,
            "prices": {c["mint"]: c for c in candidates},
            "source": "test", "as_of": "test", "error": None}


def _row(mint, symbol, name, m5=None, h1=5.0):
    return {"mint": mint, "symbol": symbol, "name": name,
            "price_usd": 0.0009, "change_1h_pct": h1,
            "change_5m_pct": m5}


class FakeRpc:
    def __init__(self, url, balance_lamports=0):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        raise AssertionError("must not send in tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("must not send in tests")


class TestServerWatchedFeed(unittest.TestCase):
    def test_set_replace_and_clear(self):
        feed = LiveFeed()
        self.assertEqual(feed.server_watched_mints(), set())
        feed.set_server_watched([MINT1, MINT2])
        self.assertEqual(feed.server_watched_mints(), {MINT1, MINT2})
        feed.set_server_watched([MINT3])
        self.assertEqual(feed.server_watched_mints(), {MINT3})
        feed.set_server_watched([])
        self.assertEqual(feed.server_watched_mints(), set())

    def test_browser_watched_is_untouched(self):
        feed = LiveFeed()
        feed.subscribe_mint(MINT1)
        feed.set_server_watched([MINT2])
        self.assertEqual(feed.watched_mints(), {MINT1})
        self.assertEqual(feed.server_watched_mints(), {MINT2})


class TestDesiredMintsPriority(unittest.TestCase):
    def _stream(self, cap=2):
        feed = LiveFeed()
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "k",
                          "HELIUS_MAX_STREAM_MINTS": str(cap),
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        return feed, HeliusStream(feed, cfg)

    def test_server_mints_join_the_set(self):
        feed, stream = self._stream(cap=10)
        feed.set_server_watched([MINT1, MINT2])
        self.assertEqual(stream._desired_mints(), {MINT1, MINT2})

    def test_held_first_then_watched_then_server(self):
        feed, stream = self._stream(cap=2)
        held = generate_keypair()[1]
        watched = generate_keypair()[1]
        with mock.patch.object(feed, "_held_mints_from_db",
                               return_value={held}):
            feed.subscribe_mint(watched)
            feed.set_server_watched([MINT1, MINT2])
            # Cap 2: the held mint and the browser-watched mint win;
            # the server candidates are cut, never the holdings.
            self.assertEqual(stream._desired_mints(), {held, watched})

    def test_cap_is_deterministic(self):
        feed, stream = self._stream(cap=2)
        feed.set_server_watched([MINT1, MINT2, MINT3])
        self.assertEqual(len(stream._desired_mints()), 2)
        self.assertEqual(stream._desired_mints(),
                         stream._desired_mints())


class FeedFixBase(unittest.TestCase):
    """Page harness: app + mocked scan/chain/sparkline, like
    test_trenches_page10's Page10Base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", 10.0, "jupiter")
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan", return_value=_scan([]))
        self.scan_patch.start()
        self.chain_prices = {}
        self.chain_patch = mock.patch.object(
            routes, "_chain_prices_for_mints",
            side_effect=lambda app, mints: {
                m: self.chain_prices[m] for m in mints
                if m in self.chain_prices})
        self.chain_patch.start()
        self.spark_patch = mock.patch.object(
            routes, "sparkline_points_1h",
            side_effect=lambda coin_id, is_mint=False, feed=None,
            http_get=None: [])
        self.spark_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.spark_patch.stop()
        self.chain_patch.stop()
        self.scan_patch.stop()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        self.tmp.cleanup()

    def _set_scan(self, candidates):
        self.scan_patch.stop()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value=_scan(candidates))
        self.scan_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})


class TestTrenchesListRanksBy5m(FeedFixBase):
    def test_five_minute_gain_beats_one_hour_gain(self):
        # Under the old 1h ranking this order was B, C, A: B's
        # +900% 1h held its slot for an hour while A (the coin
        # moving NOW) sat below it. By 5m gain A leads.
        self._set_scan([
            _row(MINT1, "HOT", "Hot Coin", m5=121.0, h1=10.0),
            _row(MINT2, "OLD", "Old Pumper", m5=0.6, h1=900.0),
            _row(MINT3, "DEAD", "Dead Coin", m5=None, h1=500.0),
        ])
        # The page render drops rows neither source can price, so
        # give each row a live tick (the chain read would do the
        # same in production).
        for mint, price in ((MINT1, 0.001), (MINT2, 0.002),
                            (MINT3, 0.003)):
            self.feed.ingest(mint, price, "helius-stream")
        rows, _err = routes._trenches_list(app=self.app)
        self.assertEqual([r["mint"] for r in rows],
                         [MINT1, MINT2, MINT3])

    def test_row_carries_the_5m_figure(self):
        self._set_scan([_row(MINT1, "HOT", "Hot Coin", m5=42.0)])
        self.feed.ingest(MINT1, 0.001, "helius-stream")
        rows, _err = routes._trenches_list(app=self.app)
        self.assertEqual(rows[0]["change_5m_pct"], 42.0)

    def test_page_watch_follows_the_rows(self):
        self._set_scan([
            _row(MINT1, "HOT", "Hot Coin", m5=10.0),
            _row(MINT2, "WARM", "Warm Coin", m5=5.0),
        ])
        routes._trenches_list(app=self.app)
        self.assertEqual(self.feed.server_watched_mints(),
                         {MINT1, MINT2})

    def test_api_returns_rows_in_5m_order(self):
        self._set_scan([
            _row(MINT1, "HOT", "Hot Coin", m5=121.0, h1=10.0),
            _row(MINT2, "OLD", "Old Pumper", m5=0.6, h1=900.0),
        ])
        self.feed.ingest(MINT1, 0.001, "helius-stream")
        self.feed.ingest(MINT2, 0.002, "helius-stream")
        data = self.client.get("/api/trenches").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual([r["mint"] for r in data["rows"]],
                         [MINT1, MINT2])


class TestTrenchesScanLogging(FeedFixBase):
    def test_scan_failure_logs_a_warning(self):
        self.scan_patch.stop()
        self.scan_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            side_effect=RuntimeError("dexscreener down"))
        self.scan_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        with self.assertLogs("tradingbot.routes", level="WARNING") as cm:
            rows, error = routes._trenches_list()
        self.assertEqual(rows, [])
        self.assertTrue(any("scan failed" in line for line in cm.output))

    def test_zero_candidates_without_error_logs_a_warning(self):
        with self.assertLogs("tradingbot.routes", level="WARNING") as cm:
            rows, error = routes._trenches_list()
        self.assertEqual(rows, [])
        self.assertIsNone(error)
        self.assertTrue(any("zero candidates" in line
                            for line in cm.output))


class TestStaticCacheBusting(FeedFixBase):
    def test_shell_urls_carry_a_version(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertRegex(html, r"/static/live\.js\?v=\d+")
        self.assertRegex(html, r"/static/style\.css\?v=\d+")
        self.assertRegex(html, r"/static/ui\.js\?v=\d+")

    def test_service_worker_is_no_store(self):
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store",
                      resp.headers.get("Cache-Control", ""))


class TestTrenchesPageBackstop(FeedFixBase):
    def test_page_carries_meta_refresh_and_5m_subtitle(self):
        self._set_scan([_row(MINT1, "HOT", "Hot Coin", m5=10.0)])
        self.feed.ingest(MINT1, 0.001, "helius-stream")
        html = self.client.get("/trenches").get_data(as_text=True)
        self.assertIn('<meta http-equiv="refresh" content="30">', html)
        self.assertIn("top 10 by 5-minute gain", html)

    def test_live_js_removes_the_backstop_once_polling(self):
        js = (ROOT / "tradingbot" / "static" / "live.js").read_text()
        self.assertIn("/api/trenches", js)
        self.assertIn('meta[http-equiv="refresh"]', js)
        self.assertIn("staleMeta.remove()", js)


class TestLoopSetsServerWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", 10.0, "jupiter")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_once_watches_the_scan_candidates(self):
        import hashlib

        self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, "
            "max_open_trades, api_token_hash, rules_json) "
            "VALUES (?, ?, 'bold', ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, "Trenches", json.dumps([]), 20, 7,
             "paper", 1, 3, hashlib.sha256(b"tok").hexdigest(),
             json.dumps(trenches_ruleset())))
        self.db.commit()
        # Fresh ticks so the reprice never needs the RPC client.
        self.feed.ingest(MINT1, 0.001, "helius-stream")
        self.feed.ingest(MINT2, 0.002, "helius-stream")

        def _fetch(mints=None):
            return _scan([
                _row(MINT1, "HOT", "Hot Coin", m5=10.0),
                _row(MINT2, "WARM", "Warm Coin", m5=5.0),
            ])

        run_once(
            self.db, self.cfg,
            price_fetcher=lambda: {
                "prices": {"SOL": {"price": 10.0, "change_24h": 0.0}},
                "source": "snapshot", "as_of": "test"},
            rpc_factory=lambda url: FakeRpc(url, 0),
            trenches_fetcher=_fetch,
            feed=self.feed)
        self.assertEqual(self.feed.server_watched_mints(),
                         {MINT1, MINT2})


if __name__ == "__main__":
    unittest.main()
