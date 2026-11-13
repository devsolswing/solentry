"""Home mockup (owner's screenshots, 2026-11-11) + home_holdings helper.

Page 1 (Login): the big gold fox sits at the bottom and the red
"Still on the $GATE?" motto is raised to the fox's vertical centre
on its right, in large red italic letters. Page 1 still fits one
screen with no scrolling, keeps the Create Account button (no Back),
and the fields start empty.

Page 3 (Home, wallet.html): rebuilt to match the Home screenshot —
fox logo + "SolEntry" + "HOME of the $GATE" header; BUY (gold) /
SELL (red) / Send / Receive buttons, all wired to the existing
flows; balance window with BALANCE + duration pills + CAD selector,
total SOL, ≈ $ CAD, % change, and the candlestick chart; holdings
rows with the coin's real token image (or the ticker-initial icon),
name + ticker, amount held, sparkline (only with real history),
price, total value, 1h %.

REAL DATA ONLY: the screenshots' numbers (2.45 SOL, $185.20 CAD,
+5.82%, Pepe / Bitcoin / Solana / Dogecoin / Ethereum) are the
mockup's examples, never the user's data — no test here (and no
template) may hardcode them. Holdings come from the route's real
coin_rows; a missing price is "—", never invented.

The home_holdings helper (image resolution, sparkline SVG, chain
reconciliation) is NOT wired into the route yet — routes.py is owned
by the Android PWA build — so these tests cover the helper directly
plus the template branches it will feed (rendered with fabricated
rows), and report the wiring as a follow-up.
No SOL moves in these tests; sends are never confirmed.
"""

import tempfile
import unittest
from unittest import mock

from flask import render_template

import tradingbot.home_holdings as hh
import tradingbot.prices as prices
import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup


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


class FakeRpc:
    """A Solana RPC client stub: _call returns canned results."""

    def __init__(self, results):
        # results: {(method, program_id): result-dict} or
        # {(method, None): result-dict}; a value of Exception means
        # the call raises.
        self.results = results
        self.calls = []

    def _call(self, method, params):
        self.calls.append((method, params))
        key = (method, None)
        if isinstance(params, list) and len(params) > 1:
            key = (method, (params[1] or {}).get("programId"))
        result = self.results.get(key, self.results.get((method, None)))
        if isinstance(result, Exception):
            raise result
        return result


def fake_head_ok(url):
    return (200, "image/png")


def fake_head_not_found(url):
    return (404, "text/html")


# ------------------------------------------------------------------
# Helper: sparkline SVG
# ------------------------------------------------------------------

class TestSparklineSvg(unittest.TestCase):
    def test_empty_and_single_point_draw_nothing(self):
        self.assertEqual(hh.sparkline_svg([]), "")
        self.assertEqual(hh.sparkline_svg(None), "")
        self.assertEqual(hh.sparkline_svg([10.0]), "")

    def test_two_points_draw_a_gold_polyline(self):
        svg = hh.sparkline_svg([1.0, 2.0])
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        self.assertIn('stroke="#ffd700"', svg)
        # Rising series: the last point sits higher on screen
        # (smaller y) than the first.
        pts = svg.split('points="')[1].split('"')[0].split()
        y_first = float(pts[0].split(",")[1])
        y_last = float(pts[-1].split(",")[1])
        self.assertLess(y_last, y_first)

    def test_flat_series_draws_a_horizontal_line(self):
        svg = hh.sparkline_svg([5.0, 5.0, 5.0])
        pts = svg.split('points="')[1].split('"')[0].split()
        ys = {float(p.split(",")[1]) for p in pts}
        self.assertEqual(len(ys), 1)

    def test_many_points_are_downsampled(self):
        svg = hh.sparkline_svg(list(range(200)))
        pts = svg.split('points="')[1].split('"')[0].split()
        self.assertLessEqual(len(pts), 48)
        self.assertGreaterEqual(len(pts), 2)


# ------------------------------------------------------------------
# Helper: token images
# ------------------------------------------------------------------

class TestResolveTokenImage(unittest.TestCase):
    def setUp(self):
        hh.clear_image_cache()

    def tearDown(self):
        hh.clear_image_cache()

    def test_mint_image_from_dexscreener_info(self):
        def fake_get(url):
            self.assertIn("api.dexscreener.com", url)
            return {"pairs": [{
                "chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.001",
                "liquidity": {"usd": 1000},
                "baseToken": {"address": "MINT",
                              "name": "Gate", "symbol": "GATE"},
                "info": {"imageUrl": "https://dd.dexscreener.com/x.png"}}]}
        url = hh.resolve_token_image(
            "MINT", is_mint=True, http_get=fake_get,
            http_head=fake_head_ok)
        self.assertEqual(url, "https://dd.dexscreener.com/x.png")

    def test_mint_without_image_returns_none(self):
        def fake_get(url):
            return {"pairs": [{
                "chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.001",
                "liquidity": {"usd": 1000},
                "baseToken": {"address": "MINT",
                              "name": "Gate", "symbol": "GATE"},
                "info": {}}]}
        self.assertIsNone(hh.resolve_token_image(
            "MINT", is_mint=True, http_get=fake_get,
            http_head=fake_head_ok))

    def test_mint_fetch_failure_returns_none(self):
        def fake_get(url):
            raise RuntimeError("feed down")
        self.assertIsNone(hh.resolve_token_image(
            "MINT", is_mint=True, http_get=fake_get,
            http_head=fake_head_ok))

    def test_dead_image_url_returns_none(self):
        def fake_get(url):
            return {"pairs": [{
                "chainId": "solana", "dexId": "pumpfun",
                "priceUsd": "0.001",
                "liquidity": {"usd": 1000},
                "baseToken": {"address": "MINT"},
                "info": {"imageUrl": "https://dd.dexscreener.com/x.png"}}]}
        self.assertIsNone(hh.resolve_token_image(
            "MINT", is_mint=True, http_get=fake_get,
            http_head=fake_head_not_found))

    def test_major_image_from_coingecko(self):
        def fake_get(url):
            self.assertIn("api.coingecko.com", url)
            self.assertIn("/coins/bitcoin", url)
            return {"image": {
                "large": "https://assets.coingecko.com/btc.png",
                "small": "https://assets.coingecko.com/btc-s.png"}}
        url = hh.resolve_token_image(
            "BTC", is_mint=False, http_get=fake_get,
            http_head=fake_head_ok)
        self.assertEqual(url, "https://assets.coingecko.com/btc.png")

    def test_image_is_cached(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            return {"image": {"large": "https://assets.coingecko.com/e.png"}}
        hh.resolve_token_image("ETH", is_mint=False, http_get=fake_get,
                               http_head=fake_head_ok)
        hh.resolve_token_image("ETH", is_mint=False, http_get=fake_get,
                               http_head=fake_head_ok)
        self.assertEqual(len(calls), 1)


# ------------------------------------------------------------------
# Helper: sparkline points + chain balances + reconciliation
# ------------------------------------------------------------------

class TestSparklinePoints(unittest.TestCase):
    def test_major_points_from_coingecko_market_chart(self):
        def fake_get(url):
            return {"points": [
                {"ts": 1, "price": 10.0}, {"ts": 2, "price": 11.0},
                {"ts": 3, "price": 10.5}],
                "error": None}
        with mock.patch.object(
                hh, "fetch_market_chart",
                return_value={"points": [
                    {"ts": 1, "price": 10.0},
                    {"ts": 2, "price": 11.0},
                    {"ts": 3, "price": 10.5}], "error": None}):
            points = hh.sparkline_points_for_coin("SOL")
        self.assertEqual(points, [10.0, 11.0, 10.5])

    def test_mint_points_from_feed_history(self):
        seen = []

        class Feed:
            def history(self, coin_id):
                seen.append(coin_id)
                return [(1000.0, 1.0), (1005.0, 1.5), (1010.0, 1.2)]
        self.assertEqual(
            hh.sparkline_points_for_coin("MINT", is_mint=True,
                                         feed=Feed()),
            [1.0, 1.5, 1.2])
        self.assertEqual(seen, ["MINT"])

    def test_mint_without_feed_has_no_points(self):
        self.assertEqual(
            hh.sparkline_points_for_coin("MINT", is_mint=True), [])


class TestFetchChainTokenBalances(unittest.TestCase):
    def test_parses_both_token_programs_and_skips_zero(self):
        rpc = FakeRpc({
            ("getTokenAccountsByOwner", hh.TOKEN_PROGRAM_ID): {
                "value": [{
                    "account": {"data": {"parsed": {"info": {
                        "mint": "MINTA",
                        "tokenAmount": {"amount": "550340693346715",
                                        "decimals": 6,
                                        "uiAmount": 550340693.346715}}}}}},
                    {"account": {"data": {"parsed": {"info": {
                        "mint": "ZERO",
                        "tokenAmount": {"amount": "0",
                                        "decimals": 6,
                                        "uiAmount": 0.0}}}}}}]},
            ("getTokenAccountsByOwner", hh.TOKEN_2022_PROGRAM_ID): {
                "value": [{
                    "account": {"data": {"parsed": {"info": {
                        "mint": "MINTB",
                        "tokenAmount": {"amount": "2500000000",
                                        "decimals": 9,
                                        "uiAmount": 2.5}}}}}}]},
        })
        balances = hh.fetch_chain_token_balances(rpc, "WALLET")
        by_mint = {b["mint"]: b for b in balances}
        self.assertEqual(
            by_mint["MINTA"]["ui_amount"], 550340693.346715)
        self.assertEqual(by_mint["MINTA"]["decimals"], 6)
        self.assertEqual(by_mint["MINTB"]["ui_amount"], 2.5)
        self.assertNotIn("ZERO", by_mint)

    def test_rpc_failure_returns_empty(self):
        rpc = FakeRpc({
            ("getTokenAccountsByOwner", None): RuntimeError("rpc down")})
        self.assertEqual(hh.fetch_chain_token_balances(rpc, "W"), [])


class TestReconcileHoldings(unittest.TestCase):
    GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"

    def test_chain_only_coin_is_added_with_no_cost_basis(self):
        # Gate: the wallet holds it on-chain, the DB never recorded
        # it (its buys were recorded as failed).
        merged = hh.reconcile_holdings(
            [], [{"mint": self.GATE, "amount": 550340693346715,
                  "decimals": 6, "ui_amount": 550340693.346715}])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["coin_id"], self.GATE)
        self.assertEqual(merged[0]["amount"], 550340693.346715)
        self.assertIsNone(merged[0]["avg_price"])
        self.assertTrue(merged[0]["on_chain_only"])

    def test_chain_amount_wins_but_db_cost_basis_is_kept(self):
        merged = hh.reconcile_holdings(
            [{"coin_id": "SOL", "amount": 1.0, "avg_price": 10.0}],
            [{"mint": "SOL", "amount": 900000000,
              "decimals": 9, "ui_amount": 0.9}])
        self.assertEqual(merged[0]["amount"], 0.9)
        self.assertEqual(merged[0]["avg_price"], 10.0)
        self.assertFalse(merged[0]["on_chain_only"])

    def test_db_row_the_chain_does_not_list_is_kept(self):
        # A partial chain read must never hide a holding.
        merged = hh.reconcile_holdings(
            [{"coin_id": "SOL", "amount": 1.0, "avg_price": 10.0}], [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["coin_id"], "SOL")


class TestEnrichRow(unittest.TestCase):
    def setUp(self):
        hh.clear_image_cache()

    def tearDown(self):
        hh.clear_image_cache()

    def test_attaches_image_and_sparkline(self):
        def fake_get(url):
            if "dexscreener" in url:
                return {"pairs": [{
                    "chainId": "solana", "dexId": "pumpfun",
                    "priceUsd": "0.001",
                    "liquidity": {"usd": 5},
                    "baseToken": {"address": "MINT"},
                    "info": {"imageUrl": "https://dd.dexscreener.com/m.png"}}]}
            return {"points": [{"ts": 1, "price": 1.0},
                               {"ts": 2, "price": 2.0}],
                    "error": None}

        class Feed:
            def history(self, coin_id):
                return [(1000.0, 1.0), (1005.0, 2.0)]

        row = {"coin_id": "MINT", "is_mint": True, "ticker": "GATE"}
        with mock.patch.object(
                hh, "fetch_market_chart",
                return_value={"points": [], "error": None}):
            out = hh.enrich_row(row, feed=Feed(), http_get=fake_get,
                                http_head=fake_head_ok)
        self.assertEqual(out["image_url"],
                         "https://dd.dexscreener.com/m.png")
        self.assertIn("<svg", out["sparkline_svg"])
        self.assertIn("<polyline", out["sparkline_svg"])

    def test_failures_leave_the_row_unchanged(self):
        def fake_get(url):
            raise RuntimeError("down")
        row = {"coin_id": "MINT", "is_mint": True}
        out = hh.enrich_row(row, http_get=fake_get,
                            http_head=fake_head_ok)
        self.assertNotIn("image_url", out)
        self.assertNotIn("sparkline_svg", out)


# ------------------------------------------------------------------
# Page 3 (Home): the mockup layout, real data only
# ------------------------------------------------------------------

class HomeMockupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25, 2.5), "USDC": (1.0, 0.0)}))
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


class TestHomeMockupLayout(HomeMockupBase):
    def test_header_matches_the_screenshot(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("fox-red-480.jpg", html)
        self.assertIn("SolEntry fox logo", html)
        self.assertIn("HOME of the $GATE", html)
        # The old embedded base64 fox is gone from home.
        self.assertNotIn("data:image/jpeg;base64,", html)

    def test_action_buttons_are_the_screenshot_four(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("BUY", html)
        self.assertIn("SELL", html)
        # Clean inline SVG icons, not emoji: cart (BUY), download
        # (SELL + Receive), paper plane (Send).
        actions = html.split('class="home-actions-row"')[1].split("</div>")[0]
        self.assertEqual(actions.count("<svg"), 4)
        for emoji in ("🛒", "⬇", "✈"):
            self.assertNotIn(emoji, actions)
        self.assertIn("M1 1h4l2.68 13.39", actions)  # cart
        self.assertIn("M21 15v4a2 2 0 0 1-2 2", actions)  # download
        self.assertIn("polygon points=\"22 2 15 22", actions)  # plane
        # All four are wired to the existing flows.
        self.assertIn('data-quick-trade-open', html)
        self.assertIn('data-trade="buy"', html)
        self.assertIn('data-trade="sell"', html)
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)
        # The row sits under the logo, above the balance + holdings.
        self.assertLess(html.index("home-actions-row"),
                        html.index("home-total"))
        self.assertLess(html.index("home-actions-row"),
                        html.index("holdings-panel"))

    def test_balance_window_has_label_durations_and_cad(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("BALANCE", html)
        self.assertIn("TOTAL PORTFOLIO VALUE", html)
        for label in ("1h", "1d", "1w", "1m", "1y", "All"):
            self.assertIn(f">{label}<", html)
        self.assertIn("home-currency", html)
        self.assertIn("🇨🇦", html)
        self.assertIn("CAD", html)
        self.assertIn('id="home-chart"', html)
        self.assertIn("home-chart-durations", html)

    def test_holdings_row_shows_real_data_not_the_mockup(self):
        self._choose_mode("paper")
        self._buy_sol()
        html = self._html("/wallet")
        self.assertIn("Solana", html)
        self.assertIn("(SOL)", html)
        # 1 SOL bought at the mocked $10 price: the 3.65% fee leaves
        # 0.9635 SOL worth $9.63; the price is $10.00.
        self.assertIn("0.9635 SOL", html)
        self.assertIn("$9.63", html)
        self.assertIn("$10.00", html)
        self.assertIn("+2.50%", html)
        # The screenshot's figures are examples, never the data:
        # none of them may appear on the real page.
        for fake in ("2.45 SOL", "$185.20", "+5.82%", "0.0731 SOL",
                     "$84,652.15", "0.9987 SOL", "$3,450.00"):
            self.assertNotIn(fake, html)
        for fake_coin in ("Pepe", "Dogecoin", "Ethereum"):
            self.assertNotIn(fake_coin, html)

    def test_holdings_row_with_image_and_sparkline(self):
        # Fabricated rows (the helper's output, not yet wired into
        # the route): the template must render the real image and
        # the real sparkline when the row carries them.
        row = {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
               "price": 10.0, "change_1h": 2.5, "held": 0.9635,
               "value": 9.63, "is_mint": False,
               "image_url": "https://assets.coingecko.com/sol.png",
               "sparkline_svg": hh.sparkline_svg([9.0, 10.0, 11.0])}
        with mock.patch.object(routes, "_coin_rows",
                               return_value=[row]):
            self._choose_mode("paper")
            html = self._html("/wallet")
        self.assertIn(
            '<img class="coin-img" src="https://assets.coingecko.com/sol.png"',
            html)
        self.assertIn("holdings-spark", html)
        self.assertIn("<polyline", html)

    def test_holdings_row_without_image_falls_back_to_initial(self):
        row = {"coin_id": "SOL", "name": "Solana", "ticker": "SOL",
               "price": 10.0, "change_1h": 2.5, "held": 0.9635,
               "value": 9.63, "is_mint": False}
        with mock.patch.object(routes, "_coin_rows",
                               return_value=[row]):
            self._choose_mode("paper")
            html = self._html("/wallet")
        self.assertNotIn("coin-img", html)
        self.assertIn("coin-ico", html)
        self.assertIn(">S<", html)


class TestHomeRealModeBalance(unittest.TestCase):
    """The real-mode balance window: big SOL total, ≈ $ CAD, %.

    Rendered directly (the test client cannot reach real mode
    without an RPC): the figures here are arbitrary test values,
    deliberately NOT the screenshot's (2.45 SOL / $185.20 /
    +5.82%), proving the template renders whatever the route
    passes and hardcodes nothing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_total_and_cad_and_pct_render(self):
        ctx = {
            "mode": "devnet", "mode_label": "Real trading — devnet",
            "paper_cash": 100.0, "portfolio_usd": 106.30,
            "portfolio_sol": 1.2345,
            "change_24h": {"delta": 0.04, "pct": 3.25},
            "chart_range": "1d",
            "home_range_labels": {"1h": "1h", "1d": "1d", "1w": "1w",
                                  "1mo": "1m", "1y": "1y", "all": "All"},
            "portfolio_candles_svg": "", "portfolio_line_svg": "",
            "coin_rows": [], "holdings_value": 0.0,
            "fees_total_sol": 0.0, "fees_pump_total_sol": 0.0,
            "fees_burn_total_sol": 0.0, "fees_rows": [],
            "sol_price_usd": 86.14, "support_id": "",
            "receive_address": "11111111111111111111111111111111",
            "bots": [],
        }
        with self.app.test_request_context("/wallet"):
            html = render_template("wallet.html", **ctx)
        self.assertIn("1.2345 SOL", html)
        self.assertIn("≈ $106.30 CAD", html)
        self.assertIn("+3.25%", html)
        self.assertNotIn("2.45 SOL", html)
        self.assertNotIn("$185.20", html)
        self.assertNotIn("+5.82%", html)


# ------------------------------------------------------------------
# Page 1 (Login): gold fox at the bottom, motto raised
# ------------------------------------------------------------------

class TestLoginFoxMotto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _page(self, path="/login"):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def test_login_has_bottom_fox_and_raised_motto(self):
        html = self._page()
        self.assertIn("fox-hero", html)
        self.assertIn("fox-gold-480.jpg", html)
        self.assertIn("Still on", html)
        self.assertIn("$GATE?", html)
        css = open("tradingbot/static/style.css").read()
        # The motto is raised to the fox's vertical centre (not
        # sitting at the bottom) and set large, like the screenshot
        # — half the size it was (owner 2026-11-11 ~05:52), raised.
        self.assertIn(".fox-hero { display: flex; align-items: center;",
                      css)
        self.assertIn("font-size: clamp(20px, 3.5dvh, 30px);", css)

    def test_login_still_has_create_account_not_back(self):
        html = self._page()
        self.assertIn('href="/signup"', html)
        self.assertIn(">Create Account</a>", html)
        self.assertNotIn(">Back</button>", html)
        self.assertNotIn('id="fox-back"', html)

    def test_login_fields_still_start_empty(self):
        html = self._page()
        self.assertIn('placeholder="Username"', html)
        self.assertIn('placeholder="Password"', html)
        self.assertNotIn('value="Dev_SolEntry"', html)
        self.assertNotIn('value="alice"', html)
