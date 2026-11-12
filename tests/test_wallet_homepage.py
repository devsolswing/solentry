"""Wallet-homepage redesign + home lift.

Rules under test:
- The wallet is the homepage: sign-up, login (password and passkey)
  and the mode chooser all land on /wallet, and the wallet page
  shows the user's coins as normal-size buttons with ticker, amount
  held, value and 1h % (home lift), the Dogewifhat door art
  (embedded base64), and a Canadian flag. Position P&L and 24h %
  live on the coins page and the coin detail page.
- Coin detail page: tap a coin and get a market chart with 24hr /
  1day / 1w / 1m / 1y / all-time ranges, 24h high and low, buyers and
  sellers %, and Buy / Sell / Sell all — all in SOL. Chart data is
  real (CoinGecko market_chart) or the page says "No data yet";
  buyers/sellers % comes from DexScreener 24h txn counts, "—" when
  the feed has none.
- Trenches list under the coins list: price + 1h gain per row, and
  tapping a trenches token opens the same Buy / Sell / Sell all
  popup as any other coin.
- The redundant dashboard trade form is gone (see test_buy_mode);
  every page fits one screen — the page itself never scrolls, lists
  scroll only inside a fixed-height panel.
No SOL moves in these tests; real execution is a recording mock.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.solana import generate_keypair

DEST = "11111111111111111111111111111111"


class FakeRpc:
    instances = []
    balance_lamports = 0

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return FakeRpc.balance_lamports

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes):
    """changes: {coin: (price, change_24h)} or
    (price, change_24h, change_1h)"""
    out = {}
    for c, tup in changes.items():
        price, ch24 = tup[0], tup[1]
        ch1h = tup[2] if len(tup) > 2 else None
        out[c] = {"price": price, "change_24h": ch24,
                  "change_1h": ch1h}
    return {"prices": out, "source": "snapshot", "as_of": "test"}


def fake_chart(points):
    return {"points": [{"ts": 1790000000000 + i * 60000, "price": p}
                       for i, p in enumerate(points)], "error": None}


class HomeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
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
        # _paper_price (single-coin price) must agree with the mocked
        # list prices, or SOL conversions drift between the two.
        self.price_patch = mock.patch.object(
            prices, "get_price", return_value=(10.0, "snapshot", "test"))
        self.price_patch.start()
        # The coins page fetches the trenches feed; keep it local.
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

    def _reset_trenches_cache(self):
        routes._TRENCHES_CACHE.update(
            {"at": 0.0, "rows": None, "error": None})

    def _choose_mode(self, mode):
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": mode})
        self.assertEqual(resp.status_code, 302)
        return resp

    def _buy_sol(self, usd="10"):
        return self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "usd": usd, "bot_id": ""})


class TestWalletIsHomepage(HomeBase):
    def test_signup_lands_on_wallet(self):
        resp = signup(self.client, "carol")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_login_lands_on_wallet(self):
        self.client.get("/logout")
        resp = login(self.client, "alice")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wallet", resp.headers["Location"])

    def test_chooser_lands_on_wallet(self):
        resp = self._choose_mode("paper")
        self.assertIn("/wallet", resp.headers["Location"])

    def test_chooser_shows_on_wallet_after_signup(self):
        html = self._html("/wallet")
        self.assertIn("How do you want to trade?", html)
        self.assertIn("Demo trading", html)
        self.assertIn("Real trading — devnet", html)

    def test_choose_mode_persists_and_chooser_goes_away(self):
        self._choose_mode("paper")
        self.assertEqual(self.db.execute(
            "SELECT active_mode FROM users WHERE id = ?",
            (self.user_id,)).fetchone()["active_mode"], "paper")
        self.assertNotIn("How do you want to trade?", self._html("/wallet"))

    def test_wallet_holdings_normal_size_name_ticker_amount_value_1h(self):
        # Home lift: holdings are a scrollable list of NORMAL-SIZE
        # coin buttons — real name + ticker, amount held, value,
        # and the 1-hour % from the feed. The old 24h % and P&L
        # columns still live on the coins page and the coin detail
        # page; the home rows do not carry them. Compact build
        # (2026-11-11): no HOLDINGS heading, no "All coins" link,
        # no holdings-summary line — the list follows the balance
        # box immediately.
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertNotIn("HOLDINGS", html)
        self.assertNotIn("All coins", html)
        self.assertNotIn("holdings-summary", html)
        self.assertIn("Solana", html)
        self.assertIn("SOL", html)
        self.assertIn("holdings-row", html)
        self.assertIn("holdings-panel", html)
        # 1 SOL bought at the mocked $10 price: the 3.65% fee leaves
        # 0.9635 SOL worth $9.63.
        self.assertIn("0.9635 SOL", html)
        self.assertIn("$9.63", html)
        # 1-hour % (mocked +2.5%) on the row.
        self.assertIn("holdings-1h", html)
        self.assertIn("+2.50%", html)
        self.assertNotIn("coin-pnl", html)
        self.assertNotIn("coin-change", html)
        # Tapping a coin goes to its detail page.
        self.assertIn("/coins/SOL/detail", html)
        # Portfolio header + support ID are on the homepage.
        self.assertIn("portfolio-head", html)
        self.assertIn("Support ID", html)

    def test_wallet_has_dogewifhat_art_and_canadian_flag(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        # Home is the fox layout now; the door art lives on /wallet/details.
        # The home fox is the static file (fox-red-480.jpg), not an
        # embedded base64 blob — no base64 image on the home page.
        self.assertNotIn("data:image/jpeg;base64,", html)
        self.assertIn("fox-red-480.jpg", html)
        self.assertIn("SolEntry fox logo", html)
        self.assertIn("🇨🇦", html)
        details = self._html("/wallet/details")
        self.assertIn("data:image/jpeg;base64,", details)
        self.assertIn("Dogewifhat walking through a door", details)
        self.assertIn("🇨🇦", details)

    def test_wallet_paper_and_real_branches(self):
        self._choose_mode("paper")
        paper = self._html("/wallet")
        # The old "Paper balance: $X CAD cash" summary line is
        # gone (2026-11-11 compact): the paper cash is the
        # portfolio total in the balance box.
        self.assertIn("$100.00 CAD", paper)
        self.assertIn('data-live-cash="100.0"', paper)
        self.assertNotIn("Send history", paper)
        self._choose_mode("devnet")
        wallet = self._html("/wallet")
        self.assertNotIn("Deposit", wallet)
        self.assertNotIn("Send history", wallet)
        details = self._html("/wallet/details")
        self.assertIn("Deposit", details)
        self.assertIn("Platform wallets", details)
        self.assertIn("Send history", details)

    def test_brand_link_goes_to_wallet(self):
        html = self._html("/wallet")
        self.assertIn('href="/wallet"', html)
        # The original SolEntry logo wordmark is untouched.
        self.assertIn('class="brand"', html)
        self.assertIn(">SolEntry</a>", html)


class TestCoinDetail(HomeBase):
    def setUp(self):
        super().setUp()
        self._choose_mode("paper")
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart",
            side_effect=lambda coin, days: fake_chart(
                [80.0, 84.0, 86.0, 90.0, 88.0]))
        self.chart_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow",
            return_value={"buys": 120, "sells": 80,
                          "buys_pct": 60.0, "sells_pct": 40.0})
        self.flow_patch.start()
        self.ohlc_patch = mock.patch.object(
            routes, "fetch_ohlc",
            return_value={"candles": [
                {"ts": 1790000000000, "open": 80.0, "high": 82.0,
                 "low": 79.0, "close": 81.0},
                {"ts": 1790001800000, "open": 81.0, "high": 83.0,
                 "low": 80.5, "close": 82.0}],
                "interval": 1800, "error": None})
        self.ohlc_patch.start()
        self.addCleanup(self.chart_patch.stop)
        self.addCleanup(self.flow_patch.stop)
        self.addCleanup(self.ohlc_patch.stop)

    def test_detail_renders_ranges_high_low_and_flow(self):
        html = self._html("/coins/SOL/detail")
        for label in ("24hr", "1day", "1w", "1m", "1y", "all-time"):
            self.assertIn(label, html)
        self.assertIn("24h high", html)
        self.assertIn("$90.0000", html)
        self.assertIn("24h low", html)
        self.assertIn("$80.0000", html)
        self.assertIn("Buyers / sellers", html)
        self.assertIn("Buyers 60%", html)
        self.assertIn("Sellers 40%", html)
        self.assertIn("<svg", html)
        self.assertIn("Solana", html)

    def test_detail_range_buttons_switch_range(self):
        html = self._html("/coins/SOL/detail?range=1w")
        self.assertIn('class="range-btn active"', html)
        self.assertIn("1w", html)
        html = self._html("/coins/SOL/detail?range=bogus")
        self.assertEqual(self.client.get(
            "/coins/SOL/detail?range=bogus").status_code, 200)
        # Unknown range falls back to 24h.
        self.assertIn('href="/coins/SOL/detail?range=24h"', html)

    def test_detail_unknown_coin_404(self):
        self.assertEqual(
            self.client.get("/coins/NOPE/detail").status_code, 404)

    def test_detail_has_buy_sell_sellall_in_sol(self):
        self._buy_sol()  # hold some SOL so Sell all has a form
        html = self._html("/coins/SOL/detail")
        for word in ("Buy", "Sell", "Sell all"):
            self.assertIn(word, html)
        self.assertIn('action="/coins/SOL/buy"', html)
        self.assertIn('action="/coins/SOL/sell"', html)
        self.assertIn('name="amount_sol"', html)
        self.assertIn('name="sell_all"', html)
        self.assertIn("All amounts are in SOL", html)

    def test_detail_no_chart_data_says_no_data_yet(self):
        with mock.patch.object(routes, "fetch_market_chart",
                               return_value={"points": [], "error":
                                             "no chart history"}), \
                mock.patch.object(
                    routes, "fetch_ohlc",
                    return_value={"candles": [], "interval": None,
                                  "error": "no chart history"}):
            html = self._html("/coins/SOL/detail")
        self.assertIn("No data yet", html)
        # Neither chart view draws anything when it has no candles
        # (the candlestick SVG carries the Candlestick-chart label;
        # the line SVG carries the Price-chart label). The live
        # price block is still on the page — a price that fills with
        # real ticks once they arrive, never invented history.
        self.assertNotIn('aria-label="Candlestick chart"', html)
        self.assertNotIn('aria-label="Price chart"', html)
        self.assertIn('id="live-chart"', html)
        self.assertIn("Waiting for the first live tick", html)
        self.assertIn("24h high", html)
        self.assertIn("—", html)

    def test_detail_no_trade_flow_shows_dash_not_a_guess(self):
        with mock.patch.object(routes, "fetch_trade_flow",
                               return_value=None):
            html = self._html("/coins/SOL/detail")
        self.assertIn("Buyers / sellers", html)
        self.assertIn("—", html)
        self.assertNotIn("Buyers 0%", html)


class TestDetailTradingSol(HomeBase):
    def test_paper_buy_in_sol_converts_at_sol_price(self):
        # 1 SOL at the mocked $10 SOL price buys $10 of SOL — the
        # 3.65% fee leaves 0.9635 SOL held.
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_sol": "1", "bot_id": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("Back to coins", html)
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE coin_id = 'SOL'"
        ).fetchone()
        self.assertIsNotNone(holding)
        self.assertAlmostEqual(holding["amount"], 0.9635, places=9)
        cash = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        self.assertAlmostEqual(cash, 100.0 - 10.0)

    def test_paper_sell_in_sol_converts_at_prices(self):
        self._buy_sol()  # 1 SOL at $10 -> 0.9635 SOL held
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "amount_sol": "0.5"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        holding = self.db.execute(
            "SELECT amount FROM paper_holdings WHERE coin_id = 'SOL'"
        ).fetchone()
        self.assertAlmostEqual(holding["amount"], 0.4635, places=9)

    def test_paper_sell_all_sells_whole_holding(self):
        self._buy_sol()
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "sell_all": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Sold", resp.get_data(as_text=True))
        self.assertIsNone(self.db.execute(
            "SELECT * FROM paper_holdings WHERE coin_id = 'SOL'"
        ).fetchone())
        cash = self.db.execute(
            "SELECT cash_usd FROM paper_portfolio").fetchone()["cash_usd"]
        # Sell all 0.9635 SOL at $10: gross $9.635, fee $0.3517,
        # net $9.2833, on top of the $90 cash left after the buy.
        self.assertAlmostEqual(cash, 99.2833225, places=6)

    def test_sell_all_with_nothing_held_refuses_plainly(self):
        resp = self.client.post("/coins/SOL/sell", data={
            "csrf_token": csrf(self.client, "/coins/SOL"),
            "sell_all": "1"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Nothing was sold", html)
        self.assertIn("hold none", html)

    def test_real_sell_all_with_mocked_executor(self):
        self._choose_mode("devnet")
        self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Nova", "strategy": "balanced", "platform": "pump.fun",
            "coins": "SOL", "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": "real"})
        bot_id = self.db.execute(
            "SELECT id FROM bots WHERE name = 'Nova'").fetchone()["id"]
        mint = generate_keypair()[1]
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, 10.0, 0.01))
        self.db.commit()

        def fake_execute(*args, **kwargs):
            return {"status": "executed", "signature": "fakesigALL",
                    "coin_amount": 10.0}

        with mock.patch.object(routes, "execute_real_trade", fake_execute):
            resp = self.client.post(f"/coins/{mint}/sell", data={
                "csrf_token": csrf(self.client, f"/coins/{mint}"),
                "sell_all": "1", "bot_id": str(bot_id)})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sold", html)
        self.assertIn("fakesigALL", html)
        self.assertIsNone(self.db.execute(
            "SELECT * FROM real_holdings WHERE coin_id = ?",
            (mint,)).fetchone())


class TestTrenchesList(HomeBase):
    def test_trenches_list_under_coins_with_price_and_1h_gain(self):
        # The trenches list moved off the home page onto its own page.
        mint = generate_keypair()[1]
        self.trenches_patch.stop()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [{
                "mint": mint, "symbol": "MOON", "name": "Moon Coin",
                "price_usd": 0.00123, "change_1h_pct": 12.5,
                "change_5m_pct": 4.0, "change_24h_pct": 30.0,
                "liquidity_usd": 50000, "market_cap_usd": 120000,
                "dex_id": "pumpfun", "pair_address": "pair1",
                "pair_created_at": 1790000000000}],
                "prices": {}, "source": "dexscreener", "as_of": "test",
                "error": None})
        self.trenches_patch.start()
        self._reset_trenches_cache()
        html = self._html("/trenches")
        self.assertIn("Trenches", html)
        self.assertIn("Moon Coin", html)
        self.assertIn("MOON", html)
        self.assertIn("$0.0012", html)
        self.assertIn("+12.5% 1h", html)
        self.assertIn(f"/coins/{mint}", html)

    def test_trenches_popup_has_buy_sell_sellall(self):
        mint = generate_keypair()[1]
        self.trenches_patch.stop()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [{
                "mint": mint, "symbol": "MOON", "name": "Moon Coin",
                "price_usd": 0.00123, "change_1h_pct": 12.5,
                "change_5m_pct": 4.0, "change_24h_pct": 30.0,
                "liquidity_usd": 50000, "market_cap_usd": 120000,
                "dex_id": "pumpfun", "pair_address": "pair1",
                "pair_created_at": 1790000000000}],
                "prices": {}, "source": "dexscreener", "as_of": "test",
                "error": None})
        self.trenches_patch.start()
        self._reset_trenches_cache()
        # Hold some of the token so the Sell all form renders.
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, mint, 5.0, 0.001))
        self.db.commit()
        html = self._html(f"/coins/{mint}")
        self.assertIn("Moon Coin", html)
        self.assertIn("Buy", html)
        self.assertIn("Sell", html)
        self.assertIn("Sell all", html)
        self.assertIn('name="amount_sol"', html)
        self.assertIn('name="sell_all"', html)

    def test_trenches_feed_failure_says_unavailable(self):
        self.trenches_patch.stop()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": "feed down"})
        self.trenches_patch.start()
        self._reset_trenches_cache()
        html = self._html("/trenches")
        self.assertIn("Trenches unavailable right now", html)


class TestOneScreen(HomeBase):
    def test_one_screen_styles_exist(self):
        css = open("tradingbot/static/style.css").read()
        for token in (".one-screen", ".wallet-hero", ".wallet-flag",
                      ".coin-list-panel", ".chart-ranges", ".range-btn",
                      ".detail-chart", ".detail-stats", ".section-title",
                      ".trenches-list", ".ticket-sellall-panel",
                      "body.app-shell", "overflow: hidden"):
            self.assertIn(token, css, f"missing {token} in CSS")

    def test_red_and_yellow_theme_kept(self):
        css = open("tradingbot/static/style.css").read()
        for token in ("#2a0705", "#160b08", "#ffb300", "#ffd700",
                      "#e62e1b"):
            self.assertIn(token, css, f"red/yellow {token} missing")

    def test_coin_rows_link_to_detail_not_popup(self):
        # The coins list shows held coins only — hold both first.
        self._buy_sol()
        self.client.post("/coins/BTC/buy", data={
            "csrf_token": csrf(self.client, "/coins/BTC"),
            "usd": "10", "bot_id": ""})
        html = self._html("/dashboard")
        self.assertIn("/coins/SOL/detail", html)
        self.assertIn("/coins/BTC/detail", html)


class TestChartsModule(unittest.TestCase):
    def test_ranges_cover_all_labels(self):
        from tradingbot.charts import CHART_RANGES
        self.assertEqual(list(CHART_RANGES), ["24h", "1d", "1w", "1m",
                                              "1y", "all"])
        self.assertEqual(CHART_RANGES["24h"][0], "24hr")
        self.assertEqual(CHART_RANGES["1d"][0], "1day")
        self.assertEqual(CHART_RANGES["24h"][1], CHART_RANGES["1d"][1])

    def test_market_chart_uses_coingecko_for_known_coins(self):
        from tradingbot.charts import fetch_market_chart
        calls = []

        def fake_get(url):
            calls.append(url)
            return {"prices": [[1790000000000, 86.0],
                               [1790000060000, 87.5]]}

        out = fetch_market_chart("SOL", 1, http_get=fake_get)
        self.assertIsNone(out["error"])
        self.assertEqual(len(out["points"]), 2)
        self.assertEqual(out["points"][0]["price"], 86.0)
        self.assertIn("solana", calls[0])
        self.assertIn("days=1", calls[0])

    def test_market_chart_mint_has_no_history(self):
        from tradingbot.charts import fetch_market_chart
        out = fetch_market_chart(generate_keypair()[1], 1,
                                 http_get=lambda url: (_ for _ in ()).throw(
                                     AssertionError("no fetch for mints")))
        self.assertEqual(out["points"], [])
        self.assertIsNotNone(out["error"])

    def test_market_chart_failure_is_an_error_not_fake_data(self):
        from tradingbot.charts import fetch_market_chart

        def boom(url):
            raise RuntimeError("network down")

        out = fetch_market_chart("SOL", 7, http_get=boom)
        self.assertEqual(out["points"], [])
        self.assertIn("network down", out["error"])

    def test_high_low_from_series(self):
        from tradingbot.charts import high_low_24h
        self.assertEqual(high_low_24h([{"price": 80.0}, {"price": 90.0},
                                       {"price": 84.0}]),
                         (90.0, 80.0))
        self.assertEqual(high_low_24h([]), (None, None))

    def test_trade_flow_from_dexscreener_counts(self):
        from tradingbot.charts import fetch_trade_flow

        def fake_get(url):
            return {"pairs": [{
                "chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.001",
                "txns": {"h24": {"buys": 300, "sells": 100}},
                "liquidity": {"usd": 1000}}]}

        flow = fetch_trade_flow(generate_keypair()[1], http_get=fake_get)
        self.assertIsNotNone(flow)
        self.assertEqual(flow["buys"], 300)
        self.assertEqual(flow["sells"], 100)
        self.assertAlmostEqual(flow["buys_pct"], 75.0)
        self.assertAlmostEqual(flow["sells_pct"], 25.0)

    def test_trade_flow_missing_counts_is_none(self):
        from tradingbot.charts import fetch_trade_flow

        def fake_get(url):
            return {"pairs": [{
                "chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.001", "txns": {"h24": {}},
                "liquidity": {"usd": 1000}}]}

        self.assertIsNone(fetch_trade_flow(generate_keypair()[1],
                                           http_get=fake_get))

    def test_trade_flow_failure_is_none(self):
        from tradingbot.charts import fetch_trade_flow

        def boom(url):
            raise RuntimeError("down")

        self.assertIsNone(fetch_trade_flow("SOL", http_get=boom))


if __name__ == "__main__":
    unittest.main()
