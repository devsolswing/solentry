"""Live price feed: tick validation, the app WebSocket, the REST
snapshot, and the silent loop reading only validated prices.

No live upstream is contacted here except the app's own WebSocket
test server on localhost: Kraken/DexScreener/Jupiter threads are
never started in tests (only run.py starts them). The WebSocket
test spins the real app on a real port and speaks to it with
websocket-client.
"""

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.livefeed import LiveFeed, parse_kraken_ticker
from tradingbot.routes import _coin_rows
from tradingbot.silent_loop import run_once

MINT = "So11111111111111111111111111111111111111112"


class FakeRpc:
    def __init__(self, url, balance_lamports=0):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        raise AssertionError("no sends in these tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("no sends in these tests")


def make_prices(changes):
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "snapshot", "as_of": "test",
    }


class TestValidation(unittest.TestCase):
    def test_first_tick_accepted(self):
        feed = LiveFeed()
        self.assertTrue(feed.ingest("SOL", 10.0, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 10.0)

    def test_small_move_accepted(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken")
        self.assertTrue(feed.ingest("SOL", 14.0, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 14.0)

    def test_absurd_jump_rejected_last_good_stands(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken")
        # The $709,599.60 shape: one tick, ~200x the last good price.
        self.assertFalse(feed.ingest("SOL", 2000.0, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 10.0)
        # A second absurd tick at a different level is rejected too.
        self.assertFalse(feed.ingest("SOL", 1500.0, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 10.0)

    def test_non_positive_and_non_finite_rejected(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken")
        for bad in (0, -5.0, float("nan"), float("inf"), "not-a-price"):
            self.assertFalse(feed.ingest("SOL", bad, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 10.0)

    def test_genuine_step_change_rebases_after_agreeing_ticks(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken")
        # A real move persists: three ticks agreeing at the new level
        # re-base the reference instead of being rejected forever.
        self.assertFalse(feed.ingest("SOL", 20.0, "kraken"))
        self.assertFalse(feed.ingest("SOL", 20.1, "kraken"))
        self.assertTrue(feed.ingest("SOL", 20.0, "kraken"))
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 20.0)
        self.assertTrue(feed.ingest("SOL", 21.0, "kraken"))

    def test_would_accept_is_read_only(self):
        feed = LiveFeed()
        self.assertTrue(feed.would_accept("SOL", 999999.0))
        feed.ingest("SOL", 10.0, "kraken")
        self.assertTrue(feed.would_accept("SOL", 14.0))
        self.assertFalse(feed.would_accept("SOL", 2000.0))
        # Asking did not move the reference.
        self.assertEqual(feed.snapshot(["SOL"])["SOL"]["price"], 10.0)

    def test_stale_flag(self):
        feed = LiveFeed()
        now = time.time()
        feed.ingest("SOL", 10.0, "kraken", now=now - 60)
        snap = feed.snapshot(["SOL"], now=now)
        self.assertTrue(snap["SOL"]["stale"])
        self.assertTrue(feed.is_stale("SOL", now=now))
        feed.ingest("SOL", 10.0, "kraken", now=now)
        snap = feed.snapshot(["SOL"], now=now)
        self.assertFalse(snap["SOL"]["stale"])
        self.assertFalse(feed.is_stale("SOL", now=now))

    def test_unknown_coin_has_no_tick(self):
        feed = LiveFeed()
        self.assertEqual(feed.snapshot(["NOPE"]), {})
        self.assertTrue(feed.is_stale("NOPE"))

    def test_changes_kept_when_new_tick_lacks_them(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken", change_24h=5.0)
        feed.ingest("SOL", 10.5, "kraken")
        snap = feed.snapshot(["SOL"])["SOL"]
        self.assertEqual(snap["change_24h"], 5.0)


class TestKrakenParsing(unittest.TestCase):
    def test_ticker_frame(self):
        raw = json.dumps([123, {"c": ["83.26", "1.5"], "o": "86.00"},
                          "ticker", "SOL/USD"])
        self.assertEqual(parse_kraken_ticker(raw),
                         ("SOL", 83.26, (83.26 / 86.00 - 1.0) * 100.0))

    def test_non_ticker_frames_ignored(self):
        self.assertIsNone(parse_kraken_ticker(json.dumps(
            {"event": "heartbeat"})))
        self.assertIsNone(parse_kraken_ticker(json.dumps(
            [1, {"c": ["1", "1"], "o": "1"}, "ticker", "DOGE/USD"])))
        self.assertIsNone(parse_kraken_ticker("not json"))


class TestSanitizers(unittest.TestCase):
    def test_sanitize_trenches_drops_absurd_mint(self):
        feed = LiveFeed()
        feed.ingest(MINT, 0.002, "dexscreener")
        data = {"candidates": [
            {"mint": MINT, "price_usd": 400.0},
            {"mint": "OtherMint11111111111111111111111111111",
             "price_usd": 0.01}],
            "prices": {MINT: {"price_usd": 400.0},
                       "OtherMint11111111111111111111111111111":
                           {"price_usd": 0.01}},
            "source": "dexscreener", "as_of": "test", "error": None}
        out = feed.sanitize_trenches(data)
        self.assertNotIn(MINT, out["prices"])
        self.assertIn("OtherMint11111111111111111111111111111",
                      out["prices"])
        # The absurd mint is dropped from the candidates too; the
        # good mint stays a candidate.
        self.assertEqual(
            [c["mint"] for c in out["candidates"]],
            ["OtherMint11111111111111111111111111111"])
        # The input was not mutated.
        self.assertIn(MINT, data["prices"])
        self.assertEqual(len(data["candidates"]), 2)

    def test_sanitize_prices_data_drops_absurd_symbol(self):
        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "kraken")
        data = make_prices({"SOL": (2000.0, 0.0), "BTC": (74000.0, 0.0)})
        out = feed.sanitize_prices_data(data)
        self.assertNotIn("SOL", out["prices"])
        self.assertIn("BTC", out["prices"])
        self.assertIn("SOL", data["prices"])


class LiveFeedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeRpc)
        self.rpc_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
        self.feed = self.app.config["LIVE_FEED"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _insert_bot(self, name="Luna", silent=1):
        import hashlib
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, "balanced", json.dumps(["SOL"]),
             20, 7, "paper", silent, 3,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid

    def _latest_snapshot(self):
        return self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 1", (self.user_id,)).fetchone()


class TestLoopSanitization(LiveFeedBase):
    def test_absurd_price_never_reaches_loop_or_snapshot(self):
        self._insert_bot()
        # 2 SOL held at $10 cost.
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 2.0, 10.0)", (self.user_id,))
        self.db.commit()
        spiked = make_prices({"SOL": (2000.0, 0.0),
                              "BTC": (74000.0, 0.0)})
        # Without a feed reference, the loop sees the spike and the
        # snapshot records it: 2 SOL x $2,000 = $4,000.
        run_once(self.db, self.cfg,
                 price_fetcher=lambda: spiked,
                 rpc_factory=lambda url: FakeRpc(url, 0))
        snap = self._latest_snapshot()
        self.assertAlmostEqual(snap["holdings_value_usd"], 4000.0)
        # With the feed seeded at the good price, the same spike is
        # dropped: the holding is valued at cost, $20, and no trade
        # is made on the absurd price.
        self.feed.ingest("SOL", 10.0, "kraken")
        before_trades = self.db.execute(
            "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
        run_once(self.db, self.cfg,
                 price_fetcher=lambda: spiked,
                 rpc_factory=lambda url: FakeRpc(url, 0),
                 feed=self.feed)
        snap = self._latest_snapshot()
        self.assertAlmostEqual(snap["holdings_value_usd"], 20.0)
        after_trades = self.db.execute(
            "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
        self.assertEqual(after_trades, before_trades)

    def test_render_drops_absurd_mint_price(self):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 1000.0, 0.002)",
            (self.user_id, MINT))
        self.db.commit()
        prices = make_prices({"SOL": (10.0, 0.0)})
        with mock.patch.object(routes, "fetch_prices_for_mints",
                               return_value={MINT: 400.0}), \
                mock.patch.object(routes, "fetch_mint_1h_changes",
                                  return_value={}):
            # No feed: the spiked price inflates the row value.
            rows = _coin_rows(self.db, self.user_id, "paper", prices)
            row = next(r for r in rows if r["coin_id"] == MINT)
            self.assertAlmostEqual(row["value"], 1000.0 * 400.0)
            # Feed seeded at the good price: the spike is dropped,
            # the row has no price and no inflated value.
            self.feed.ingest(MINT, 0.002, "dexscreener")
            rows = _coin_rows(self.db, self.user_id, "paper", prices,
                              feed=self.feed)
            row = next(r for r in rows if r["coin_id"] == MINT)
            self.assertIsNone(row["price"])
            self.assertIsNone(row["value"])


class TestApiLive(LiveFeedBase):
    def test_snapshot_returns_seeded_ticks(self):
        self.feed.ingest("SOL", 10.5, "kraken", change_24h=5.0)
        resp = self.client.get("/api/live?coins=SOL,BTC")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["ticks"]["SOL"]["price"], 10.5)
        self.assertEqual(data["ticks"]["SOL"]["source"], "kraken")
        self.assertEqual(data["ticks"]["SOL"]["change_24h"], 5.0)
        self.assertNotIn("BTC", data["ticks"])

    def test_snapshot_requires_login(self):
        anon_app, anon = make_client(self.tmp.name)
        resp = anon.get("/api/live?coins=SOL")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestWebSocket(LiveFeedBase):
    def _start_server(self, port):
        def run():
            self.app.run(host="127.0.0.1", port=port, threaded=True,
                         use_reloader=False)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health",
                        timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.2)
        self.fail("test server did not come up")

    def _session_cookie(self):
        cookie = self.client.get_cookie("session")
        if cookie is None:
            self.fail("no session cookie after signup")
        return getattr(cookie, "value", cookie)

    def test_subscribe_snapshot_then_tick(self):
        import websocket

        port = _free_port()
        self._start_server(port)
        self.feed.ingest("SOL", 10.0, "kraken", change_24h=5.0)
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/ws/live",
            timeout=10,
            cookie=f"session={self._session_cookie()}")
        try:
            ws.send(json.dumps({"subscribe": ["SOL"]}))
            ws.settimeout(10)
            snapshot = json.loads(ws.recv())
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["ticks"]["SOL"]["price"], 10.0)
            # A fresh tick is pushed without another subscribe.
            self.feed.ingest("SOL", 10.5, "kraken")
            deadline = time.time() + 10
            tick = None
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("type") == "tick" \
                        and msg.get("coin_id") == "SOL":
                    tick = msg
                    break
            self.assertIsNotNone(tick, "no tick pushed")
            self.assertEqual(tick["price"], 10.5)
            self.assertEqual(tick["source"], "kraken")
        finally:
            ws.close()

    def test_same_second_price_change_is_pushed(self):
        """Regression: as_of is second-precision, so a price change
        in the same second as the snapshot must still be pushed —
        the old dedup (as_of only) silently dropped it."""
        import websocket

        port = _free_port()
        self._start_server(port)
        now = time.time()
        self.feed.ingest("SOL", 10.0, "kraken", now=now)
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/ws/live",
            timeout=10,
            cookie=f"session={self._session_cookie()}")
        try:
            ws.send(json.dumps({"subscribe": ["SOL"]}))
            ws.settimeout(10)
            snapshot = json.loads(ws.recv())
            self.assertEqual(snapshot["ticks"]["SOL"]["price"], 10.0)
            # Same second, different price.
            self.feed.ingest("SOL", 10.5, "kraken", now=now)
            deadline = time.time() + 10
            tick = None
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("type") == "tick" \
                        and msg.get("coin_id") == "SOL":
                    tick = msg
                    break
            self.assertIsNotNone(
                tick, "same-second price change was not pushed")
            self.assertEqual(tick["price"], 10.5)
        finally:
            ws.close()

    def test_unauthenticated_socket_gets_error(self):
        import websocket

        port = _free_port()
        self._start_server(port)
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/ws/live", timeout=10)
        try:
            ws.settimeout(10)
            msg = json.loads(ws.recv())
            self.assertEqual(msg["type"], "error")
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
