"""Candlestick charts: Page 5 coin detail + Page 3 homepage chart
+ Page 4 name/ticker rows.

Rules under test:
- Candles are real OHLC buckets of real points: open is the first
  price in the bucket, close the last, high/low the extremes. A
  bucket with one point is a real candle (open = high = low =
  close). Empty input builds no candles — the page says the candles
  are still building (or there is no data yet), never a made-up
  chart. Fewer than two candles draws nothing.
- Page 5: the detail chart is a real candlestick chart by default
  (CoinGecko OHLC for known coins, live-feed tick history for
  mints), with a button in the corner switching to the regular line
  chart. Both views come from the SAME candles, so they can never
  disagree. The chart does not redraw every second: live ticks
  re-render it at most once per candle interval, via the candles
  endpoint. The price number still updates every second — that is
  the price, not the chart.
- Page 3: the homepage balance area has a compact candlestick chart
  with duration options (1h / 1d / 1w / 1m / 1y / All) and the same
  candle/line toggle. Its candles come ONLY from the recorded
  portfolio snapshots — thin history says so plainly.
- Page 4: every coins-list row shows the coin's name and its ticker
  in its own span, so a long name can truncate but the ticker never
  can. Held mints show their real name + symbol from the feed when
  it names them, and the honest "Pump.fun token …" label when it
  does not.
No SOL moves in these tests; real execution is a recording mock.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tradingbot import candles as candles_mod
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.pnl import record_snapshot

MINT = "11111111111111111111111111111111"

# One day of CoinGecko OHLC: 30-minute candles [ts, o, h, l, c].
BASE_TS = 1790000000000


def fake_ohlc_rows():
    rows = []
    price = 80.0
    for i in range(48):
        rows.append([BASE_TS + i * 1800 * 1000, price, price + 2.0,
                     price - 1.0, price + 1.0])
        price += 0.5
    return rows


def fake_ohlc(*args, **kwargs):
    return {"candles": [{"ts": r[0], "open": r[1], "high": r[2],
                         "low": r[3], "close": r[4]}
                        for r in fake_ohlc_rows()],
            "interval": 1800, "error": None}


def fake_chart(points):
    return {"points": [{"ts": 1790000000000 + i * 60000, "price": p}
                       for i, p in enumerate(points)], "error": None}


def make_prices(changes):
    out = {}
    for c, tup in changes.items():
        price, ch24 = tup[0], tup[1]
        ch1h = tup[2] if len(tup) > 2 else None
        out[c] = {"price": price, "change_24h": ch24,
                  "change_1h": ch1h}
    return {"prices": out, "source": "snapshot", "as_of": "test"}


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_recent_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


class CandleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        FakeRpc.balance_lamports = 0
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25, 2.5), "USDC": (1.0, 0.0),
                "DOGE": (0.07, 3.0), "PEPE": (0.000005, -1.0),
                "BONK": (0.000003, 8.0), "JUP": (0.23, -0.5)}))
        self.prices_patch.start()
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(10.0, "snapshot", "test"))
        self.price_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.trenches_patch.stop()
        self.price_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _buy_sol(self, usd="10"):
        return self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": usd, "bot_id": ""})

    def _hold_mint(self, amount=100.0, avg_price=0.01):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, MINT, amount, avg_price))
        self.db.commit()


class TestBuildCandles(unittest.TestCase):
    def test_ohlc_aggregation_across_buckets(self):
        points = [
            {"ts": 0, "price": 10.0},
            {"ts": 30_000, "price": 12.0},
            {"ts": 59_000, "price": 9.0},
            {"ts": 60_000, "price": 11.0},
            {"ts": 90_000, "price": 15.0},
            {"ts": 119_000, "price": 13.0},
        ]
        candles = candles_mod.build_candles(points, 60)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0], {"ts": 0, "open": 10.0,
                                      "high": 12.0, "low": 9.0,
                                      "close": 9.0})
        self.assertEqual(candles[1], {"ts": 60_000, "open": 11.0,
                                      "high": 15.0, "low": 11.0,
                                      "close": 13.0})

    def test_point_on_bucket_edge_starts_new_bucket(self):
        points = [{"ts": 59_999, "price": 10.0},
                  {"ts": 60_000, "price": 11.0}]
        candles = candles_mod.build_candles(points, 60)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[1]["open"], 11.0)

    def test_single_point_bucket_is_a_real_candle(self):
        candles = candles_mod.build_candles(
            [{"ts": 0, "price": 10.0}], 60)
        self.assertEqual(candles, [{"ts": 0, "open": 10.0,
                                    "high": 10.0, "low": 10.0,
                                    "close": 10.0}])

    def test_empty_input_builds_no_candles(self):
        self.assertEqual(candles_mod.build_candles([], 60), [])
        self.assertEqual(candles_mod.build_candles(None, 60), [])

    def test_unsorted_points_are_sorted_oldest_first(self):
        points = [{"ts": 120_000, "price": 13.0},
                  {"ts": 0, "price": 10.0},
                  {"ts": 60_000, "price": 11.0}]
        candles = candles_mod.build_candles(points, 60)
        self.assertEqual([c["ts"] for c in candles],
                         [0, 60_000, 120_000])

    def test_snapshot_rows_normalize(self):
        points = [
            {"created_at": "2026-11-10T00:00:00Z",
             "total_value_usd": 100.0},
            {"created_at": "2026-11-10T00:01:00Z",
             "total_value_usd": 102.0},
            {"created_at": "2026-11-10T00:02:00Z",
             "total_value_usd": 101.0},
        ]
        candles = candles_mod.build_candles(points, 60)
        self.assertEqual(len(candles), 3)
        self.assertEqual(candles[0]["open"], 100.0)
        self.assertEqual(candles[2]["close"], 101.0)

    def test_bad_points_are_dropped_never_guessed(self):
        points = [{"ts": 0, "price": 10.0},
                  {"ts": 60_000, "price": None},
                  {"ts": 120_000, "price": "nope"},
                  {"ts": None, "price": 12.0}]
        candles = candles_mod.build_candles(points, 60)
        self.assertEqual(len(candles), 1)

    def test_intervals_fit_the_range(self):
        self.assertEqual(candles_mod.interval_for_home("1h"), 300)
        self.assertEqual(candles_mod.interval_for_home("1d"), 3600)
        self.assertEqual(candles_mod.interval_for_home("1w"), 21600)
        self.assertEqual(candles_mod.interval_for_home("all"), 86400)
        self.assertEqual(candles_mod.interval_for_home(
            "all", 400 * 86400), 604800)
        self.assertEqual(candles_mod.interval_for_mint("24h"), 60)
        self.assertEqual(candles_mod.interval_for_mint("1w"), 900)

    def test_interval_labels_are_plain_words(self):
        self.assertEqual(candles_mod.interval_label(60), "1 minute")
        self.assertEqual(candles_mod.interval_label(1800), "30 minutes")
        self.assertEqual(candles_mod.interval_label(14400), "4 hours")
        self.assertEqual(candles_mod.interval_label(345600), "4 days")


class TestCandlesSvg(unittest.TestCase):
    def _candles(self):
        return [
            {"ts": 0, "open": 10.0, "high": 12.0, "low": 9.0,
             "close": 11.0},
            {"ts": 60_000, "open": 11.0, "high": 11.5, "low": 10.0,
             "close": 10.5},
            {"ts": 120_000, "open": 10.5, "high": 13.0, "low": 10.5,
             "close": 13.0},
        ]

    def test_candle_svg_has_wicks_and_bodies(self):
        svg = candles_mod.candles_svg(self._candles())
        self.assertIn("<svg", svg)
        self.assertIn('aria-label="Candlestick chart"', svg)
        # One wick line + one body rect per candle.
        self.assertEqual(svg.count("<line"), 3)
        self.assertEqual(svg.count("<rect"), 3)
        # Up candles green, down candles red (the app's data colours).
        self.assertIn("#4ade80", svg)
        self.assertIn("#f87171", svg)

    def test_line_svg_uses_the_same_candles(self):
        line = candles_mod.line_svg(self._candles())
        self.assertIn("<svg", line)
        self.assertIn('aria-label="Price chart"', line)
        self.assertIn("<polyline", line)
        # The line passes through the closes: 11.0, 10.5, 13.0.
        # It is drawn from the candle data, not a second fetch.
        self.assertNotIn("Candlestick", line)

    def test_fewer_than_two_candles_draws_nothing(self):
        self.assertEqual(candles_mod.candles_svg([]), "")
        self.assertEqual(candles_mod.candles_svg(
            [{"ts": 0, "open": 10.0, "high": 10.0, "low": 10.0,
              "close": 10.0}]), "")
        self.assertEqual(candles_mod.line_svg([]), "")


class TestFetchOhlc(unittest.TestCase):
    def test_ohlc_parses_real_candles_and_interval(self):
        from tradingbot.charts import fetch_ohlc

        def fake_get(url):
            self.assertIn("/ohlc?", url)
            self.assertIn("solana", url)
            return fake_ohlc_rows()

        out = fetch_ohlc("SOL", 1, http_get=fake_get)
        self.assertIsNone(out["error"])
        self.assertEqual(len(out["candles"]), 48)
        self.assertEqual(out["candles"][0]["open"], 80.0)
        self.assertEqual(out["candles"][0]["close"], 81.0)
        self.assertEqual(out["interval"], 1800)

    def test_ohlc_mint_has_no_history(self):
        from tradingbot.charts import fetch_ohlc

        out = fetch_ohlc(MINT, 1, http_get=lambda url: [])
        self.assertEqual(out["candles"], [])
        self.assertIsNotNone(out["error"])

    def test_ohlc_failure_is_an_error_not_fake_candles(self):
        from tradingbot.charts import fetch_ohlc

        def boom(url):
            raise RuntimeError("feed down")

        out = fetch_ohlc("SOL", 1, http_get=boom)
        self.assertEqual(out["candles"], [])
        self.assertIn("feed down", out["error"])


class TestMintIdentities(unittest.TestCase):
    def test_identity_from_best_pair(self):
        from tradingbot.trenches import fetch_mint_identities

        def fake_get(url):
            return {"pairs": [
                {"chainId": "solana", "dexId": "pumpfun",
                 "priceUsd": "0.5",
                 "liquidity": {"usd": 100},
                 "baseToken": {"address": MINT, "name": "Test Coin",
                               "symbol": "TEST"}},
                {"chainId": "solana", "dexId": "pumpfun",
                 "priceUsd": "0.5",
                 "liquidity": {"usd": 50},
                 "baseToken": {"address": MINT, "name": "Other",
                               "symbol": "OTHR"}},
            ]}

        out = fetch_mint_identities([MINT], http_get=fake_get)
        # Highest-liquidity pair wins.
        self.assertEqual(out[MINT], {"name": "Test Coin",
                                     "symbol": "TEST"})

    def test_unnamed_mint_is_left_out(self):
        from tradingbot.trenches import fetch_mint_identities

        def fake_get(url):
            return {"pairs": [
                {"chainId": "solana", "dexId": "pumpfun",
                 "priceUsd": "0.5", "liquidity": {"usd": 100},
                 "baseToken": {"address": MINT}},
            ]}

        self.assertEqual(
            fetch_mint_identities([MINT], http_get=fake_get), {})

    def test_identity_failure_returns_empty(self):
        from tradingbot.trenches import fetch_mint_identities

        def boom(url):
            raise RuntimeError("feed down")

        self.assertEqual(
            fetch_mint_identities([MINT], http_get=boom), {})


class TestLiveFeedHistory(unittest.TestCase):
    def test_ingest_records_ticks_oldest_first(self):
        from tradingbot.livefeed import LiveFeed

        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "test", now=1000.0)
        feed.ingest("SOL", 11.0, "test", now=1005.0)
        feed.ingest("SOL", 12.0, "test", now=1010.0)
        self.assertEqual(feed.history("SOL"),
                         [(1000.0, 10.0), (1005.0, 11.0),
                          (1010.0, 12.0)])

    def test_rejected_ticks_never_enter_history(self):
        from tradingbot.livefeed import LiveFeed

        feed = LiveFeed()
        feed.ingest("SOL", 10.0, "test", now=1000.0)
        # A 100x jump is rejected by validation...
        self.assertFalse(feed.ingest("SOL", 1000.0, "test",
                                     now=1005.0))
        self.assertEqual(feed.history("SOL"), [(1000.0, 10.0)])

    def test_unknown_coin_has_no_history(self):
        from tradingbot.livefeed import LiveFeed

        self.assertEqual(LiveFeed().history("NOPE"), [])


class TestCoinDetailCandles(CandleBase):
    def setUp(self):
        super().setUp()
        self._choose_mode("paper")
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart",
            side_effect=lambda coin, days: fake_chart(
                [80.0, 84.0, 86.0, 90.0, 88.0]))
        self.chart_patch.start()
        self.ohlc_patch = mock.patch.object(
            routes, "fetch_ohlc", side_effect=fake_ohlc)
        self.ohlc_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow",
            return_value={"buys": 120, "sells": 80,
                          "buys_pct": 60.0, "sells_pct": 40.0})
        self.flow_patch.start()
        self.addCleanup(self.chart_patch.stop)
        self.addCleanup(self.ohlc_patch.stop)
        self.addCleanup(self.flow_patch.stop)

    def test_detail_shows_candlesticks_with_toggle_to_line(self):
        html = self._html("/coins/SOL/detail")
        # Candlestick view is the default; the line view is hidden
        # behind the corner button.
        self.assertIn('aria-label="Candlestick chart"', html)
        self.assertIn('aria-label="Price chart"', html)
        self.assertIn('id="chart-view-toggle"', html)
        self.assertIn("data-chart-toggle", html)
        self.assertIn('data-chart-view="candles"', html)
        self.assertIn('data-chart-view="line"', html)
        # The line view is hidden until the button is tapped.
        self.assertIn('chart-view chart-view-line hidden', html)
        # The caption says how wide each candle is.
        self.assertIn("30 minutes each", html)
        # The range buttons are untouched.
        for label in ("24hr", "1day", "1w", "1m", "1y", "all-time"):
            self.assertIn(label, html)
        # 24h high/low still come from the market chart.
        self.assertIn("$90.0000", html)
        self.assertIn("$80.0000", html)

    def test_detail_chart_no_longer_redraws_every_second(self):
        html = self._html("/coins/SOL/detail")
        self.assertNotIn("updating every second", html)
        self.assertNotIn('id="live-chart-svg"', html)
        self.assertNotIn('id="live-chart-line"', html)
        # The live price readout + last-updated time stay.
        self.assertIn('id="live-price"', html)
        self.assertIn('id="live-updated"', html)
        self.assertIn('id="live-chart"', html)
        self.assertIn('data-tick-url="/api/coins/SOL/tick"', html)

    def test_detail_no_ohlc_data_says_no_data_yet(self):
        with mock.patch.object(
                routes, "fetch_ohlc",
                return_value={"candles": [], "interval": None,
                              "error": "no chart history"}), \
                mock.patch.object(
                    routes, "fetch_market_chart",
                    return_value={"points": [], "error":
                                  "no chart history"}):
            html = self._html("/coins/SOL/detail")
        self.assertIn("No data yet", html)
        self.assertNotIn('aria-label="Candlestick chart"', html)
        self.assertNotIn('id="chart-view-toggle"', html)
        # The live price block is still on the page.
        self.assertIn('id="live-chart"', html)
        self.assertIn("Waiting for the first live tick", html)
        self.assertIn("24h high", html)
        self.assertIn("—", html)

    def test_detail_mint_with_no_feed_history_is_still_building(self):
        self._hold_mint()
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5}):
            html = self._html(f"/coins/{MINT}/detail")
        self.assertIn("Candles are still building", html)
        self.assertNotIn('aria-label="Candlestick chart"', html)
        self.assertNotIn('id="chart-view-toggle"', html)

    def test_detail_mint_with_feed_history_shows_candles(self):
        self._hold_mint()
        feed = self.app.config["LIVE_FEED"]
        base = 1790000000000 / 1000.0
        for i in range(30):
            feed.ingest(MINT, 0.5 + (i % 5) * 0.01, "test",
                        now=base + i * 5.0)
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.5}):
            html = self._html(f"/coins/{MINT}/detail")
        self.assertIn('aria-label="Candlestick chart"', html)
        self.assertIn('id="chart-view-toggle"', html)
        self.assertIn("1 minute each", html)

    def test_candles_endpoint_returns_fresh_svgs(self):
        resp = self.client.get("/api/coins/SOL/candles?range=24h")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 48)
        self.assertEqual(data["interval"], 1800)
        self.assertIn("Candlestick chart", data["candles_svg"])
        self.assertIn("Price chart", data["line_svg"])
        self.assertIn("updated", data)

    def test_candles_endpoint_mint_uses_feed_history(self):
        feed = self.app.config["LIVE_FEED"]
        base = 1790000000000 / 1000.0
        for i in range(30):
            feed.ingest(MINT, 0.5 + (i % 5) * 0.01, "test",
                        now=base + i * 5.0)
        resp = self.client.get(f"/api/coins/{MINT}/candles?range=24h")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        # 30 ticks over 150s in 1-minute buckets: 3 candles.
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["interval"], 60)

    def test_candles_endpoint_needs_login(self):
        self.client.get("/logout")
        resp = self.client.get("/api/coins/SOL/candles")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_candles_endpoint_unknown_coin_404(self):
        resp = self.client.get("/api/coins/NOPE/candles")
        self.assertEqual(resp.status_code, 404)

    def test_tick_js_still_polls_but_chart_refresh_is_throttled(self):
        resp = self.client.get("/static/coin_tick.js")
        self.assertEqual(resp.status_code, 200)
        js = resp.get_data(as_text=True)
        self.assertIn("setInterval(tick, 1000)", js)
        self.assertIn("visibilitychange", js)
        # No more every-second polyline redraw.
        self.assertNotIn("live-chart-line", js)
        self.assertNotIn("MAX_POINTS", js)
        self.assertIn("maybeRefreshChart", js)
        self.assertIn("chartInterval", js)

    def test_chart_js_is_served_and_toggles(self):
        resp = self.client.get("/static/chart.js")
        self.assertEqual(resp.status_code, 200)
        js = resp.get_data(as_text=True)
        self.assertIn("solentry-chart-view", js)
        self.assertIn("data-chart-toggle", js)
        self.assertIn("sessionStorage", js)


class TestHomeChart(CandleBase):
    def _seed_snapshots(self, start, count, step_seconds, base_value=100.0):
        for i in range(count):
            at = (start + timedelta(seconds=i * step_seconds)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
            record_snapshot(self.db, self.user_id, base_value,
                            i * 0.5, None, now=at)

    def test_home_chart_has_durations_and_toggle(self):
        self._choose_mode("paper")
        now = datetime.now(timezone.utc)
        # Two days of snapshots, 2 hours apart: the 1d range gets
        # 12 hourly candles.
        self._seed_snapshots(now - timedelta(days=2), 25, 2 * 3600)
        html = self._html("/wallet?range=1d")
        self.assertIn('id="home-chart"', html)
        for label in ("1h", "1d", "1w", "1m", "1y", "All"):
            self.assertIn(f">{label}<", html)
        self.assertIn('class="home-range-btn active"', html)
        self.assertIn('aria-label="Candlestick chart"', html)
        self.assertIn('aria-label="Price chart"', html)
        self.assertIn('data-chart-toggle', html)
        # The home chart and the holdings list stay. The old "Full
        # chart ›" link to the deleted P&L page is gone, never a
        # dead link.
        self.assertNotIn("Full chart ›", html)
        self.assertNotIn('href="/pnl"', html)
        self.assertIn("holdings-panel", html)

    def test_home_chart_duration_selects_the_range(self):
        self._choose_mode("paper")
        now = datetime.now(timezone.utc)
        self._seed_snapshots(now - timedelta(hours=1), 13, 300)
        html = self._html("/wallet?range=1h")
        self.assertIn('href="/wallet?range=1h"', html)
        self.assertIn('aria-label="Candlestick chart"', html)

    def test_home_chart_one_bucket_is_not_enough_history(self):
        self._choose_mode("paper")
        now = datetime.now(timezone.utc)
        # Three snapshots inside one hour bucket (aligned to the
        # previous hour, so the bucket cannot straddle): the 1-day
        # range has hourly candles, so this is one candle — not a
        # chart — and the page says so instead of drawing it.
        base = (now.replace(minute=0, second=0, microsecond=0)
                - timedelta(hours=1))
        for i, minutes in enumerate((5, 10, 15)):
            at = (base + timedelta(minutes=minutes)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
            record_snapshot(self.db, self.user_id, 100.0, i * 0.5,
                            None, now=at)
        html = self._html("/wallet?range=1d")
        self.assertIn("Not enough history yet", html)
        self.assertNotIn('aria-label="Candlestick chart"', html)

    def test_home_chart_thin_history_says_so_plainly(self):
        self._choose_mode("paper")
        now = datetime.now(timezone.utc)
        self._seed_snapshots(now - timedelta(minutes=5), 1, 60)
        html = self._html("/wallet?range=1h")
        self.assertIn("Not enough history yet for candles", html)
        self.assertNotIn('aria-label="Candlestick chart"', html)
        self.assertNotIn('data-chart-toggle', html)


class TestCoinsListNameTicker(CandleBase):
    def test_known_coin_row_shows_name_and_ticker_separately(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/dashboard")
        # The ticker is its own span, a flex sibling of the name —
        # a long name truncates, the ticker never can.
        self.assertIn(
            '<span class="coin-name"><strong>Solana</strong></span>'
            '<span class="coin-ticker muted small">SOL</span>', html)

    def test_held_mint_shows_real_name_and_symbol_from_feed(self):
        self._choose_mode("paper")
        self._hold_mint()
        with mock.patch.object(
                routes, "fetch_mint_identities",
                return_value={MINT: {"name": "Test Coin",
                                     "symbol": "TEST"}}):
            html = self._html("/dashboard")
        self.assertIn("Test Coin", html)
        self.assertIn(
            '<span class="coin-ticker muted small">TEST</span>', html)
        self.assertNotIn("Pump.fun token 1111", html)

    def test_held_mint_without_feed_identity_keeps_honest_label(self):
        self._choose_mode("paper")
        self._hold_mint()
        with mock.patch.object(
                routes, "fetch_mint_identities", return_value={}):
            html = self._html("/dashboard")
        self.assertIn("Pump.fun token 1111…1111", html)
        self.assertIn(
            '<span class="coin-ticker muted small">1111…</span>', html)

    def test_home_holdings_row_keeps_ticker_visible(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn(
            '<span class="coin-ticker muted">(SOL)</span>', html)


if __name__ == "__main__":
    unittest.main()
