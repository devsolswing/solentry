"""Helius live-price stream: curve prices off the chain into the
live feed.

The stream subscribes (Helius free-tier WSS, standard Solana
accountSubscribe) to the bonding-curve accounts of held + watched
mints only, derives the price from the virtual reserves, and feeds
it through the existing LiveFeed.ingest validation. No key =
streaming off and the app runs on the DexScreener poll exactly as
before.

No live Helius connection is opened here: the socket and the RPC
are faked, and the whole reconnect loop is driven with a scripted
fake WebSocket. Helius endpoints + subscription format follow the
official docs (helius.dev/docs/api-reference/rpc/websocket/
accountsubscribe): wss://mainnet.helius-rpc.com/?api-key=<key>,
accountSubscribe [curve_account, {encoding: base64, commitment:
confirmed}].
"""

import base64
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

import websocket

import tradingbot.streaming as streaming_mod
from helpers import make_client
from tradingbot.config import Config
from tradingbot.livefeed import LiveFeed
from tradingbot.pumpfun import curve_spot_price_sol
from tradingbot.streaming import HeliusStream, start_helius_stream

GATE_DEVNET = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_MAINNET = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"
SOL_PRICE = 78.5


def _curve_bytes(vt, vs, complete=False):
    """Bonding-curve account bytes: discriminator (8), virtual
    token reserves, virtual SOL reserves, real token, real SOL,
    total supply (u64s), complete flag — the layout
    parse_bonding_curve reads."""
    data = b"\x00" * 8
    for value in (vt, vs, 0, 0, 10 ** 15):
        data += int(value).to_bytes(8, "little")
    data += b"\x01" if complete else b"\x00"
    return data


def _notification(sub_id, slot, data_b64):
    return json.dumps({
        "jsonrpc": "2.0", "method": "accountNotification",
        "params": {"result": {
            "context": {"slot": slot},
            "value": {"data": [data_b64, "base64"],
                      "lamports": 1000,
                      "owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}},
            "subscription": sub_id}})


def _curve_b64(vt, vs, complete=False):
    return base64.b64encode(_curve_bytes(vt, vs, complete)).decode()


class FakeRpc:
    """get_account_info / get_multiple_accounts, returning one
    canned curve for any address."""

    def __init__(self, curve_bytes=None):
        self._curve_bytes = curve_bytes

    def get_account_info(self, pubkey):
        if self._curve_bytes is None:
            return None
        return {"data": self._curve_bytes, "lamports": 1000,
                "executable": False, "owner": "pump"}

    def get_multiple_accounts(self, pubkeys):
        return {p: self.get_account_info(p) for p in pubkeys}


class FakeWS:
    """Scripted websocket-client stand-in. Each recv() pops the
    next scripted action: ("msg", raw), ("timeout",) or
    ("raise", exception)."""

    def __init__(self, script):
        self.sent = []
        self.pings = 0
        self.closed = False
        self._script = list(script)

    def settimeout(self, seconds):
        pass

    def send(self, data):
        self.sent.append(data)

    def ping(self):
        self.pings += 1

    def recv(self):
        if not self._script:
            time.sleep(0.01)
            raise websocket.WebSocketTimeoutException("idle")
        action, payload = self._script.pop(0)
        if action == "msg":
            return payload
        if action == "timeout":
            time.sleep(0.01)
            raise websocket.WebSocketTimeoutException("idle")
        if action == "raise":
            raise payload
        raise AssertionError(action)

    def close(self):
        self.closed = True


def _live_curve(vt=1_073_000_000_000_000, vs=30_000_000_000):
    return _curve_bytes(vt, vs, complete=False)


class TestCurveSpotPrice(unittest.TestCase):
    def test_price_matches_reserve_math(self):
        from tradingbot.pumpfun import parse_bonding_curve
        curve = parse_bonding_curve(_live_curve())
        price = curve_spot_price_sol(curve)
        # (vs / vt) * (TOKEN_BASE / LAMPORTS_PER_SOL), in SOL.
        expected = (30_000_000_000 / 1_073_000_000_000_000) * (10 ** 6
                                                              / 10 ** 9)
        self.assertAlmostEqual(price, expected, places=15)

    def test_complete_curve_has_no_spot_price(self):
        from tradingbot.pumpfun import parse_bonding_curve
        curve = parse_bonding_curve(
            _curve_bytes(1_073_000_000_000_000, 30_000_000_000,
                         complete=True))
        self.assertIsNone(curve_spot_price_sol(curve))

    def test_bad_curve_has_no_spot_price(self):
        self.assertIsNone(curve_spot_price_sol(None))
        self.assertIsNone(curve_spot_price_sol({}))
        self.assertIsNone(curve_spot_price_sol(
            {"virtual_token_reserves": 0,
             "virtual_sol_reserves": 30_000_000_000}))
        self.assertIsNone(curve_spot_price_sol(
            {"virtual_token_reserves": 1_073_000_000_000_000,
             "virtual_sol_reserves": 0}))


class TestConfig(unittest.TestCase):
    def _cfg(self, **extra):
        env = {"NETWORK": "mainnet", "SESSION_SECRET": "x",
               "ENCRYPTION_KEY": "y"}
        env.update(extra)
        return Config(env=env)

    def test_no_key_means_off(self):
        cfg = self._cfg()
        self.assertEqual(cfg.helius_api_key, "")
        self.assertTrue(cfg.helius_stream_enabled)
        self.assertEqual(cfg.helius_max_stream_mints, 50)
        self.assertFalse(HeliusStream(LiveFeed(), cfg).enabled)

    def test_key_means_on(self):
        cfg = self._cfg(HELIUS_API_KEY="  test-key-123  ")
        self.assertEqual(cfg.helius_api_key, "test-key-123")
        self.assertTrue(HeliusStream(LiveFeed(), cfg).enabled)

    def test_flag_can_turn_it_off_with_a_key(self):
        cfg = self._cfg(HELIUS_API_KEY="k", HELIUS_STREAM_ENABLED="0")
        self.assertFalse(HeliusStream(LiveFeed(), cfg).enabled)

    def test_max_mints_env_override(self):
        cfg = self._cfg(HELIUS_API_KEY="k",
                        HELIUS_MAX_STREAM_MINTS="3")
        self.assertEqual(cfg.helius_max_stream_mints, 3)

    def test_wss_url_by_network(self):
        cfg = self._cfg(HELIUS_API_KEY="k")
        stream = HeliusStream(LiveFeed(), cfg)
        self.assertEqual(
            stream._wss_url(),
            "wss://mainnet.helius-rpc.com/?api-key=k")
        cfg_dev = self._cfg(HELIUS_API_KEY="k", NETWORK="devnet")
        self.assertEqual(
            HeliusStream(LiveFeed(), cfg_dev)._wss_url(),
            "wss://devnet.helius-rpc.com/?api-key=k")
        self.assertIsNone(HeliusStream(LiveFeed(), self._cfg())
                          ._wss_url())


class TestNotificationHandling(unittest.TestCase):
    def setUp(self):
        self.feed = LiveFeed()
        self.now = time.time()
        self.feed.ingest("SOL", SOL_PRICE, "jupiter", now=self.now)
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        self.stream = HeliusStream(
            self.feed, cfg,
            rpc_factory=lambda url: FakeRpc(_live_curve()))
        self.stream._subs[GATE_MAINNET] = "curveaddr"
        self.stream._sub_id[42] = GATE_MAINNET
        self.stream._mint_sub_id[GATE_MAINNET] = 42

    def _notify(self, slot, vt=1_073_000_000_000_000,
                vs=30_000_000_000, complete=False, now=None):
        msg = _notification(42, slot, _curve_b64(vt, vs, complete))
        params = json.loads(msg)["params"]
        return self.stream._handle_account_notification(
            params, now=self.now if now is None else now)

    def test_curve_update_ingests_helius_price(self):
        self.assertTrue(self._notify(slot=100))
        snap = self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]
        expected = (30_000_000_000 / 1_073_000_000_000_000) * 1e-3 \
            * SOL_PRICE
        self.assertAlmostEqual(snap["price"], expected, places=12)
        self.assertEqual(snap["source"], "helius-stream")

    def test_bad_tick_rejected_by_ingest_guard(self):
        # Seed a good price, then a curve update 2x higher: the
        # 50% single-tick guard rejects it and the last good
        # price stands.
        self.assertTrue(self._notify(slot=100))
        good = self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"]
        self.assertFalse(self._notify(
            slot=101, vs=60_000_000_000))
        self.assertEqual(
            self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"],
            good)

    def test_genuine_move_rebases_after_agreeing_ticks(self):
        self.assertTrue(self._notify(slot=100))
        # Three agreeing ticks at the new (2x) level re-base.
        self.assertFalse(self._notify(slot=101, vs=60_000_000_000))
        self.assertFalse(self._notify(slot=102, vs=60_000_000_000))
        self.assertTrue(self._notify(slot=103, vs=60_000_000_000))
        expected = (60_000_000_000 / 1_073_000_000_000_000) * 1e-3 \
            * SOL_PRICE
        self.assertAlmostEqual(
            self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"],
            expected, places=12)

    def test_older_slot_is_a_replay_and_is_ignored(self):
        self.assertTrue(self._notify(slot=100))
        price = self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"]
        # Same slot again, and an older slot, at a different price.
        self.assertFalse(self._notify(slot=100, vs=31_000_000_000))
        self.assertFalse(self._notify(slot=99, vs=31_000_000_000))
        self.assertEqual(
            self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"],
            price)

    def test_streamed_price_never_overwrites_fresher_rest_price(self):
        # A REST tick lands at now=1000. A stream notification
        # stamped older (receipt time 999) must not rewind it.
        self.feed.ingest(GATE_MAINNET, 0.002, "dexscreener",
                         now=1000.0)
        self.assertFalse(self._notify(slot=100, now=999.0))
        self.assertEqual(
            self.feed.snapshot([GATE_MAINNET])[GATE_MAINNET]["price"],
            0.002)

    def test_no_sol_tick_means_no_stream_price(self):
        feed = LiveFeed()
        stream = HeliusStream(
            feed, self.stream._config,
            rpc_factory=lambda url: FakeRpc(_live_curve()))
        stream._subs[GATE_MAINNET] = "curveaddr"
        stream._sub_id[42] = GATE_MAINNET
        params = json.loads(_notification(
            42, 100, _curve_b64(1_073_000_000_000_000,
                                30_000_000_000)))["params"]
        self.assertFalse(stream._handle_account_notification(params))
        self.assertEqual(feed.snapshot([GATE_MAINNET]), {})

    def _stream_with_sol_age(self, age_seconds):
        """Fresh feed + stream whose only SOL tick is `age_seconds`
        old — the setUp feed already has a fresh SOL tick, so the
        age tests need their own feed."""
        feed = LiveFeed()
        now = time.time()
        feed.ingest("SOL", SOL_PRICE, "jupiter", now=now - age_seconds)
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        stream = HeliusStream(
            feed, cfg, rpc_factory=lambda url: FakeRpc(_live_curve()))
        stream._subs[GATE_MAINNET] = "curveaddr"
        stream._sub_id[42] = GATE_MAINNET
        stream._mint_sub_id[GATE_MAINNET] = 42
        return feed, stream, now

    def _notify_on(self, stream, slot, now, vt=1_073_000_000_000_000,
                   vs=30_000_000_000, complete=False):
        params = json.loads(_notification(
            42, slot, _curve_b64(vt, vs, complete)))["params"]
        return stream._handle_account_notification(params, now=now)

    def test_stale_sol_tick_means_no_stream_price(self):
        # SOL tick 91s old (past the 90s bound): the update is
        # skipped rather than converted on a stale reference.
        feed, stream, now = self._stream_with_sol_age(91)
        self.assertFalse(self._notify_on(stream, 100, now))
        self.assertEqual(feed.snapshot([GATE_MAINNET]), {})

    def test_sol_tick_within_bound_is_used(self):
        # SOL tick 89s old (inside the 90s bound): the update
        # converts on it.
        feed, stream, now = self._stream_with_sol_age(89)
        self.assertTrue(self._notify_on(stream, 100, now))
        self.assertIn(GATE_MAINNET, feed.snapshot([GATE_MAINNET]))

    def test_complete_curve_drops_the_subscription(self):
        ws = FakeWS([])
        self.stream._ws = ws
        self.assertFalse(self._notify(slot=100, complete=True))
        self.assertNotIn(GATE_MAINNET, self.stream._subs)
        self.assertEqual(self.feed.snapshot([GATE_MAINNET]), {})
        # An unsubscribe for the confirmed subscription id went out.
        unsub = [json.loads(s) for s in ws.sent
                 if json.loads(s).get("method") == "accountUnsubscribe"]
        self.assertEqual(len(unsub), 1)
        self.assertEqual(unsub[0]["params"], [42])

    def test_unknown_subscription_id_is_ignored(self):
        params = json.loads(_notification(
            999, 100, _curve_b64(1_073_000_000_000_000,
                                 30_000_000_000)))["params"]
        self.assertFalse(
            self.stream._handle_account_notification(params))
        self.assertEqual(self.feed.snapshot([GATE_MAINNET]), {})

    def test_malformed_notification_is_ignored(self):
        self.assertFalse(
            self.stream._handle_account_notification({}))
        self.assertFalse(
            self.stream._handle_account_notification(
                {"result": {"value": {"data": ["!!!not-base64!!!",
                                               "base64"]}},
                 "subscription": 42}))
        self.assertFalse(
            self.stream._handle_account_notification(
                {"result": {"value": {"data": ["AAAA", "base64"]}},
                 "subscription": 42}))
        self.assertEqual(self.feed.snapshot([GATE_MAINNET]), {})


class TestSubscriptionSet(unittest.TestCase):
    def setUp(self):
        self.feed = LiveFeed()
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "k",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        self.stream = HeliusStream(
            self.feed, cfg,
            rpc_factory=lambda url: FakeRpc(_live_curve()))

    def test_desired_mints_are_held_plus_watched(self):
        self.feed.subscribe_mint(GATE_MAINNET)
        self.assertEqual(self.stream._desired_mints(), {GATE_MAINNET})

    def test_curve_check_skips_graduated_mints(self):
        stream = HeliusStream(
            self.feed, self.stream._config,
            rpc_factory=lambda url: FakeRpc(
                _curve_bytes(1_073_000_000_000_000, 30_000_000_000,
                             complete=True)))
        self.assertFalse(stream._curve_is_live(GATE_MAINNET))

    def test_curve_check_skips_missing_curve(self):
        stream = HeliusStream(
            self.feed, self.stream._config,
            rpc_factory=lambda url: FakeRpc(None))
        self.assertFalse(stream._curve_is_live(GATE_MAINNET))

    def test_refresh_subscribes_new_mints_on_a_live_socket(self):
        self.feed.subscribe_mint(GATE_MAINNET)
        ws = FakeWS([])
        self.stream._refresh_subscriptions(ws)
        self.assertIn(GATE_MAINNET, self.stream._subs)
        subs = [json.loads(s) for s in ws.sent
                if json.loads(s).get("method") == "accountSubscribe"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["params"][1],
                         {"encoding": "base64",
                          "commitment": "confirmed"})
        # The subscribed account is the mint's bonding-curve PDA.
        from tradingbot.pumpfun import bonding_curve_pda
        curve_addr, _ = bonding_curve_pda(GATE_MAINNET)
        self.assertEqual(subs[0]["params"][0], curve_addr)

    def test_refresh_drops_mints_no_longer_watched(self):
        self.feed.subscribe_mint(GATE_MAINNET)
        ws = FakeWS([])
        self.stream._refresh_subscriptions(ws)
        self.assertIn(GATE_MAINNET, self.stream._subs)
        self.feed.unsubscribe_mint(GATE_MAINNET)
        self.stream._refresh_subscriptions(ws)
        self.assertNotIn(GATE_MAINNET, self.stream._subs)

    def test_max_mints_cap_is_deterministic(self):
        cfg = Config(env={"NETWORK": "mainnet", "HELIUS_API_KEY": "k",
                          "HELIUS_MAX_STREAM_MINTS": "2",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        stream = HeliusStream(self.feed, cfg)
        for mint in (GATE_MAINNET, GATE_DEVNET,
                     "So11111111111111111111111111111111111111112"):
            self.feed.subscribe_mint(mint)
        self.assertEqual(len(stream._desired_mints()), 2)


class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.feed = LiveFeed()
        self.feed.ingest("SOL", SOL_PRICE, "jupiter")
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "k",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        self.cfg = cfg
        self.created = []
        self.scripts = []
        self.stream = None

    def _ws_factory(self, url, timeout=None):
        ws = FakeWS(self.scripts.pop(0) if self.scripts else [])
        self.created.append(ws)
        return ws

    def test_drop_then_reconnect_resubscribes(self):
        self.feed.subscribe_mint(GATE_MAINNET)
        # First connection: subscribes, then the socket dies.
        # Second connection: stays up on timeouts.
        self.scripts = [
            [("raise", websocket.WebSocketConnectionClosedException(
                "dropped"))],
            [("timeout", None)] * 50,
        ]
        stream = HeliusStream(
            self.feed, self.cfg, ws_factory=self._ws_factory,
            rpc_factory=lambda url: FakeRpc(_live_curve()))
        self.stream = stream
        with mock.patch.object(streaming_mod,
                               "RECONNECT_BASE_SECONDS", 0.01), \
                mock.patch.object(streaming_mod,
                                  "RECONNECT_MAX_SECONDS", 0.02), \
                mock.patch.object(streaming_mod,
                                  "WATCHDOG_SILENCE_SECONDS", 3600.0), \
                mock.patch.object(streaming_mod,
                                  "SUBSCRIPTION_REFRESH_SECONDS",
                                  3600.0), \
                mock.patch.object(stream, "_backfill") as backfill:
            stream.start()
            deadline = time.time() + 10
            while time.time() < deadline and len(self.created) < 2:
                time.sleep(0.05)
            self.assertEqual(len(self.created), 2,
                             "stream did not reconnect")
            # Both connections sent an accountSubscribe for the
            # mint's curve account.
            for ws in self.created:
                subs = [json.loads(s) for s in ws.sent
                        if json.loads(s).get("method")
                        == "accountSubscribe"]
                self.assertEqual(len(subs), 1)
            # Backfill ran on (re)connect.
            self.assertGreaterEqual(backfill.call_count, 1)
            stream.stop()
            stream._thread.join(timeout=5)
            self.assertFalse(stream._thread.is_alive())
            for ws in self.created:
                self.assertTrue(ws.closed)

    def test_no_subscriptions_means_no_connection(self):
        # Nothing held or watched: the loop idles without ever
        # opening a socket.
        stream = HeliusStream(
            self.feed, self.cfg, ws_factory=self._ws_factory,
            rpc_factory=lambda url: FakeRpc(_live_curve()))
        self.stream = stream
        with mock.patch.object(streaming_mod,
                               "SUBSCRIPTION_REFRESH_SECONDS", 0.05):
            stream.start()
            time.sleep(0.3)
            stream.stop()
            stream._thread.join(timeout=5)
        self.assertEqual(self.created, [])
        self.assertFalse(stream._thread.is_alive())


class TestBackfill(unittest.TestCase):
    def _stream(self, feed, rpc_factory=None):
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "k",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        return HeliusStream(
            feed, cfg,
            rpc_factory=rpc_factory or
            (lambda url: FakeRpc(_live_curve())))

    def test_backfill_re_reads_curves_from_the_chain(self):
        """After (re)connect the stream re-reads subscribed mints
        straight from the chain — a direct curve read, never the
        deleted DexScreener poll — and ingests the price."""
        from tradingbot.pumpfun import parse_bonding_curve
        feed = LiveFeed()
        feed.ingest("SOL", SOL_PRICE, "test")
        stream = self._stream(feed)
        stream._subs[GATE_MAINNET] = "curveaddr"
        stream._backfill()
        snap = feed.snapshot([GATE_MAINNET])[GATE_MAINNET]
        expected = (curve_spot_price_sol(
            parse_bonding_curve(_live_curve())) * SOL_PRICE)
        self.assertAlmostEqual(snap["price"], expected, places=9)
        self.assertEqual(snap["source"], "helius-stream")

    def test_backfill_failure_never_raises(self):
        feed = LiveFeed()
        feed.ingest("SOL", SOL_PRICE, "test")
        stream = self._stream(
            feed,
            rpc_factory=mock.Mock(side_effect=Exception("down")))
        stream._subs[GATE_MAINNET] = "curveaddr"
        stream._backfill()  # must not raise
        self.assertEqual(feed.snapshot([GATE_MAINNET]), {})


class TestStartHeliusStream(unittest.TestCase):
    def test_no_key_returns_none_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _client = make_client(tmpdir)
            self.assertIsNone(start_helius_stream(app))
            feed = app.config["LIVE_FEED"]
            self.assertEqual(feed._threads, [])

    def test_key_without_start_opens_nothing(self):
        # A key makes the stream enabled, but nothing connects
        # until start() is called (run.py only).
        feed = LiveFeed()
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "k",
                          "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})
        stream = HeliusStream(feed, cfg)
        self.assertTrue(stream.enabled)
        self.assertIsNone(stream._thread)
        self.assertFalse(stream._started)


if __name__ == "__main__":
    unittest.main()
