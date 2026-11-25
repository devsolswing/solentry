"""Home page redesign (2026-11-24): the owner's screenshot, cloned.

Rules under test — the design is the screenshot
(user/files/C5C4EE93-...jpg): cigar fox in a gold circle, big gold
"SolEntry", red "HOME of the $GATE"; BUY / SELL / Send / Receive;
BALANCE card with time pills, CAD selector, total in SOL, % change,
candlestick chart; HOLDINGS rows with a true market chart and the
position value in SOL to 5 decimals; bottom nav. The photo's coins
and numbers are never the data — rows are the user's real holdings.
- Rows show the position value in SOL to 5 decimals (00.00000),
  never the unit price. The value is the row's USD value (live
  price, or cost when there is no live price) divided by the live
  SOL price; no SOL price or no value means "—", never $0.
- Coin names render IN FULL — never truncated or cut off (wrap +
  a smaller size for very long names).
- Every row's chart is true market data: GeckoTerminal OHLCV
  closes first, the feed's recorded ticks as fallback, nothing
  when neither has 2 points — never a fake line.
- Holdings come from LIVE on-chain balances in real mode: when
  BOTH token programs answered, a coin the chain no longer lists
  leaves the list; a partial read never hides a holding.
- The holdings list auto-refreshes every 60s (/api/home-holdings):
  rows update in place, exited coins drop, new coins insert, no
  manual reload; live.js re-subscribes after a set change.
- The balance chart is the portfolio's recorded-snapshot history:
  no snapshots at all says "History recording started today";
  snapshots but too few candles for the range says "Not enough
  history yet" — never a backfilled or flat fake line.
- The trades-left fuel chip + jerry-can Buy $GATE button and the
  Paper/Real mode indicator stay visible at the top of the home
  page; the four action buttons keep their existing actions.
No SOL moves in these tests; sends are never confirmed. The Gate
mint address below is a public token address, not a secret.
"""

import re
import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts
import tradingbot.home_holdings as hh
import tradingbot.routes as routes
from tradingbot import pnl as pnl_mod
from helpers import csrf, make_client, signup
from test_balance_read import MINT as FAKE_MINT
from test_balance_read import _RealModeBase
from test_home_mockup import GATE_MINT, HomeMockupBase

LIVE_SOL_PRICES = {"prices": {"SOL": {"price": 10.0}},
                   "source": "live", "as_of": "test"}


def _css():
    with open("tradingbot/static/style.css", encoding="utf-8") as f:
        return f.read()


def _rule(css, selector):
    matches = re.findall(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert matches, "%s rule missing from style.css" % selector
    return matches[-1]


def _row(coin_id=GATE_MINT, name="Gate", ticker="GATE", price=0.01,
         held=1000.0, avg_price=0.01, value=10.0, change_1h=2.5):
    return {"coin_id": coin_id, "name": name, "ticker": ticker,
            "price": price, "change_24h": None, "change_1h": change_1h,
            "held": held, "avg_price": avg_price, "pnl": 0.0,
            "is_mint": True, "value": value, "image_url": None,
            "sparkline_svg": ""}


class _LiveSol:
    """Stack a live SOL price over the base's snapshot prices, so
    _dashboard_data can express row values in SOL. Usable as a
    context manager or via start()/stop()."""

    def __init__(self, test=None):
        self._patch = mock.patch.object(
            routes, "get_prices", return_value=LIVE_SOL_PRICES)

    def start(self):
        return self._patch.start()

    def stop(self):
        return self._patch.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class TestHomeRedesignLayout(HomeMockupBase):
    def _home_with_rows(self, rows, mode="paper"):
        self._choose_mode(mode)
        with mock.patch.object(routes, "_coin_rows",
                               return_value=(rows, False)), _LiveSol(self):
            return self._html("/wallet")

    def test_rows_show_sol_value_to_5_decimals_not_the_price(self):
        html = self._home_with_rows([_row(value=9.63, held=0.9635)])
        row = html.split("holdings-row")[1]
        self.assertIn("0.96300 SOL", row)
        # The big number on the row is the SOL value: no unit price
        # span, and the SOL span itself carries no $ sign.
        self.assertNotIn('class="holdings-price"', html)
        sol = re.search(
            r'<span class="holdings-sol"[^>]*>([^<]*)</span>', row)
        self.assertIsNotNone(sol)
        self.assertNotIn("$", sol.group(1))
        self.assertIn("holdings-sol", row)

    def test_row_without_a_sol_value_shows_a_dash_not_zero(self):
        html = self._home_with_rows(
            [_row(value=None, avg_price=None, price=None)])
        row = html.split("holdings-row")[1]
        self.assertIn("—", row)
        self.assertNotIn("0.00000 SOL", row)

    def test_long_names_render_in_full_with_the_name_long_tier(self):
        long_name = "SolEntry Trading Tool Extended Name"
        html = self._home_with_rows(
            [_row(name=long_name, ticker="GATE")])
        self.assertIn(long_name, html)
        self.assertIn("name-long", html)

    def test_fuel_row_has_mode_pill_fuel_and_jerry_can(self):
        html = self._home_with_rows([], mode="paper")
        self.assertIn("home-mode-pill", html)
        self.assertIn("Demo trading (paper)", html)
        self.assertIn("free ~ ∞", html)
        self.assertIn('data-gate-buy-open', html)
        self.assertIn('aria-label="Buy $GATE"', html)

    def test_real_mode_pill_names_the_network(self):
        # The test app's network is devnet, so the pill reads
        # "Real trading — devnet" here; live (config network
        # mainnet) it reads "Real trading — mainnet".
        html = self._home_with_rows([], mode="devnet")
        self.assertIn("Real trading — devnet", html)
        self.assertIn("trades left", html)

    def test_four_action_buttons_keep_their_actions(self):
        html = self._home_with_rows([])
        self.assertIn('data-quick-trade-open', html)
        self.assertIn('data-trade="buy"', html)
        self.assertIn('data-trade="sell"', html)
        self.assertIn('href="/wallet/send"', html)
        self.assertIn('data-overlay-open="home-receive-overlay"', html)

    def test_balance_card_has_pills_cad_and_chart(self):
        html = self._home_with_rows([])
        for label in ("1h", "1d", "1w", "1m", "1y", "All"):
            self.assertIn(">%s<" % label, html)
        self.assertIn("TOTAL PORTFOLIO VALUE", html)
        self.assertIn("CAD", html)
        self.assertIn('id="home-chart"', html)

    def test_header_is_the_photo_header_with_no_topbar(self):
        html = self._home_with_rows([])
        self.assertIn("fox-red-480.jpg", html)
        self.assertIn("HOME of the $GATE", html)
        # The small topbar strip is gone from Home only: the big
        # header IS the top of the page.
        self.assertNotIn("topbar", html)


class TestValueSol(HomeMockupBase):
    """value_sol = USD value / live SOL price, read off the rendered
    home page (the math lives in _dashboard_data, which is a
    closure inside create_app — the route is the way in)."""

    def _home_with_rows(self, rows, live_sol=True):
        self._choose_mode("paper")
        patches = [mock.patch.object(
            routes, "_coin_rows", return_value=(rows, False))]
        if live_sol:
            patches.append(_LiveSol(self))
        for p in patches:
            p.start()
        try:
            return self._html("/wallet")
        finally:
            for p in reversed(patches):
                p.stop()

    def test_value_sol_is_usd_value_over_live_sol_price(self):
        html = self._home_with_rows([_row(value=9.63, held=0.9635)])
        self.assertIn("0.96300 SOL", html)

    def test_value_sol_falls_back_to_cost_without_a_live_price(self):
        html = self._home_with_rows(
            [_row(value=None, price=None, avg_price=0.01, held=1000.0)])
        self.assertIn("1.00000 SOL", html)

    def test_value_sol_is_none_without_value_or_cost(self):
        html = self._home_with_rows(
            [_row(value=None, price=None, avg_price=None, held=5.0)])
        row = html.split("holdings-row")[1]
        self.assertIn("—", row)
        self.assertNotIn("0.00000 SOL", row)

    def test_value_sol_is_none_without_a_live_sol_price(self):
        # Snapshot SOL price only: no live SOL price, so no SOL
        # value — the page shows "—", never a stale conversion.
        html = self._home_with_rows([_row(value=10.0, held=1000.0)],
                                    live_sol=False)
        row = html.split("holdings-row")[1]
        self.assertIn("—", row)
        self.assertNotIn("1.00000 SOL", row)


class TestGeckoTerminal(unittest.TestCase):
    def setUp(self):
        charts._gt_pool_cache.clear()
        charts._gt_ohlcv_cache.clear()

    def tearDown(self):
        charts._gt_pool_cache.clear()
        charts._gt_ohlcv_cache.clear()

    def _fake_get(self, url):
        if "/tokens/" in url:
            return {"data": [
                {"attributes": {"address": "POOLPUMP",
                                "reserve_in_usd": "100.0"},
                 "relationships": {"dex": {"data": {"id": "solana_pump-fun"}}}},
                {"attributes": {"address": "POOLRAY",
                                "reserve_in_usd": "99999.0"},
                 "relationships": {"dex": {"data": {"id": "solana_raydium"}}}},
            ]}
        if "/ohlcv/" in url:
            return {"data": {"attributes": {"ohlcv_list": [
                [1700007200, 3.0, 3.1, 2.9, 3.0, 5.0],
                [1700003600, 2.0, 2.1, 1.9, 2.0, 4.0],
                [1700000000, 1.0, 1.1, 0.9, 1.0, 3.0]]}}}
        raise AssertionError("unexpected url %s" % url)

    def test_pool_pick_prefers_the_pump_pool_over_a_deeper_one(self):
        pool = charts.geckoterminal_pool_for_mint(
            GATE_MINT, http_get=self._fake_get)
        self.assertEqual(pool, "POOLPUMP")

    def test_pool_pick_falls_back_to_deepest_reserve(self):
        def no_pump(url):
            if "/tokens/" in url:
                return {"data": [
                    {"attributes": {"address": "POOLRAY",
                                    "reserve_in_usd": "99999.0"},
                     "relationships": {"dex": {"data": {"id": "solana_raydium"}}}},
                    {"attributes": {"address": "POOLSMALL",
                                    "reserve_in_usd": "10.0"},
                     "relationships": {"dex": {"data": {"id": "solana_raydium"}}}},
                ]}
            raise AssertionError(url)
        self.assertEqual(
            charts.geckoterminal_pool_for_mint(
                GATE_MINT, http_get=no_pump), "POOLRAY")

    def test_closes_are_oldest_first_from_real_candles(self):
        closes = charts.geckoterminal_closes_1h(
            GATE_MINT, http_get=self._fake_get)
        self.assertEqual(closes, [1.0, 2.0, 3.0])

    def test_non_mint_and_failures_return_empty(self):
        self.assertEqual(
            charts.geckoterminal_closes_1h("SOL"), [])
        self.assertIsNone(
            charts.geckoterminal_pool_for_mint("SOL"))
        self.assertEqual(
            charts.geckoterminal_closes_1h(
                GATE_MINT, http_get=_raise), [])

    def test_closes_are_cached(self):
        calls = {"n": 0}

        def counting(url):
            calls["n"] += 1
            return self._fake_get(url)
        first = charts.geckoterminal_closes_1h(
            GATE_MINT, http_get=counting)
        second = charts.geckoterminal_closes_1h(
            GATE_MINT, http_get=_raise)
        self.assertEqual(first, [1.0, 2.0, 3.0])
        self.assertEqual(second, [1.0, 2.0, 3.0])
        self.assertEqual(calls["n"], 2)  # pool lookup + OHLCV, once

    def test_sparkline_prefers_geckoterminal_over_feed_ticks(self):
        class Feed:
            def history(self, coin_id):
                return [(1700000000, 9.0), (1700003600, 9.5)]

        with mock.patch.object(
                hh, "geckoterminal_closes_1h",
                return_value=[1.0, 2.0, 3.0]):
            self.assertEqual(
                hh.sparkline_points_1h(GATE_MINT, is_mint=True,
                                       feed=Feed()),
                [1.0, 2.0, 3.0])

    def test_sparkline_falls_back_to_feed_when_geckoterminal_empty(self):
        class Feed:
            def history(self, coin_id):
                return [(1700000000, 1.0), (1700003600, 2.0),
                        (1700007200, 3.0)]

        with mock.patch.object(
                hh, "geckoterminal_closes_1h", return_value=[]):
            self.assertEqual(
                hh.sparkline_points_1h(GATE_MINT, is_mint=True,
                                       feed=Feed()),
                [2.0, 3.0])


def _raise(url):
    raise RuntimeError("down")


class TestChainOnlyHoldings(_RealModeBase):
    def test_reconcile_drops_a_gone_coin_only_on_a_complete_read(self):
        db_rows = [{"coin_id": FAKE_MINT, "amount": 10.0,
                    "avg_price": 1.0},
                   {"coin_id": GATE_MINT, "amount": 5.0,
                    "avg_price": 0.5}]
        chain = [{"mint": FAKE_MINT, "amount": 10000000,
                  "decimals": 6, "ui_amount": 10.0}]
        merged = hh.reconcile_holdings(db_rows, chain,
                                       chain_complete=True)
        self.assertEqual([r["coin_id"] for r in merged], [FAKE_MINT])
        kept = hh.reconcile_holdings(db_rows, chain,
                                     chain_complete=False)
        self.assertEqual(
            sorted(r["coin_id"] for r in kept), sorted(
                [FAKE_MINT, GATE_MINT]))

    def _acct(self, mint, ui, decimals=6):
        return {"account": {"data": {"parsed": {"info": {
            "mint": mint,
            "tokenAmount": {
                "amount": str(int(ui * (10 ** decimals))),
                "decimals": decimals,
                "uiAmount": float(ui)}}}}}}

    def _rpc(self, token_2022):
        outer = self

        class Rpc:
            def _call(self, method, params):
                program = (params[1] or {}).get("programId")
                if program == hh.TOKEN_PROGRAM_ID:
                    return {"value": [outer._acct(FAKE_MINT, 10.0)]}
                if program == hh.TOKEN_2022_PROGRAM_ID:
                    if isinstance(token_2022, Exception):
                        raise token_2022
                    return {"value": token_2022}
                raise AssertionError(program)
        return Rpc()

    def test_coin_rows_drops_an_exited_coin_on_a_complete_read(self):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 5.0, 0.5)", (self.user_id, GATE_MINT))
        self.db.commit()
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._rpc([])), \
                mock.patch.object(hh, "geckoterminal_closes_1h",
                                  return_value=[]):
            rows, stale = routes._coin_rows(
                self.db, self.user_id, "devnet",
                {"prices": {"SOL": {"price": 100.0}},
                 "source": "live", "as_of": "test"},
                feed=None, app=self.app)
        self.assertFalse(stale)
        self.assertEqual([r["coin_id"] for r in rows], [FAKE_MINT])

    def test_coin_rows_keeps_holdings_on_a_partial_read(self):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 5.0, 0.5)", (self.user_id, GATE_MINT))
        self.db.commit()
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._rpc(RuntimeError("2022 down"))), \
                mock.patch.object(hh, "geckoterminal_closes_1h",
                                  return_value=[]):
            rows, stale = routes._coin_rows(
                self.db, self.user_id, "devnet",
                {"prices": {"SOL": {"price": 100.0}},
                 "source": "live", "as_of": "test"},
                feed=None, app=self.app)
        self.assertFalse(stale)
        self.assertEqual(sorted(r["coin_id"] for r in rows),
                         sorted([FAKE_MINT, GATE_MINT]))


class TestHomeHoldingsEndpoint(HomeMockupBase):
    def _get(self, rows):
        self._choose_mode("paper")
        with mock.patch.object(routes, "_coin_rows",
                               return_value=(rows, False)), _LiveSol(self):
            return self.client.get("/api/home-holdings")

    def test_returns_rows_with_sol_values_and_html(self):
        resp = self._get([_row(value=10.0, held=1000.0)])
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["balances_stale"])
        self.assertEqual(len(data["rows"]), 1)
        row = data["rows"][0]
        self.assertEqual(row["coin_id"], GATE_MINT)
        self.assertEqual(row["value_sol"], "1.00000 SOL")
        self.assertEqual(row["change_1h"], 2.5)
        self.assertIn("holdings-sol", row["html"])
        self.assertIn("Gate", row["html"])

    def test_rows_shrink_when_a_coin_exits(self):
        self._choose_mode("paper")
        with mock.patch.object(
                routes, "_coin_rows",
                side_effect=[([_row(), _row(coin_id=FAKE_MINT,
                                            name="Fake", ticker="FAKE")],
                              False),
                             ([_row()], False)]), _LiveSol(self):
            first = self.client.get("/api/home-holdings").get_json()
            second = self.client.get("/api/home-holdings").get_json()
        self.assertEqual(len(first["rows"]), 2)
        self.assertEqual(len(second["rows"]), 1)
        self.assertEqual(second["rows"][0]["coin_id"], GATE_MINT)

    def test_empty_holdings_returns_empty_rows(self):
        resp = self._get([])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["rows"], [])

    def test_requires_login(self):
        tmp = tempfile.TemporaryDirectory()
        _app, anon = make_client(tmp.name)
        resp = anon.get("/api/home-holdings")
        self.assertIn(resp.status_code, (302, 401))
        tmp.cleanup()


class TestPortfolioHistoryEmptyState(HomeMockupBase):
    def test_no_snapshots_says_recording_started_today(self):
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn("History recording started today", html)
        self.assertNotIn("Not enough history yet", html)

    def test_one_snapshot_says_not_enough_history_yet(self):
        pnl_mod.record_snapshot(self.db, self.user_id, 100.0, 0.0, None)
        self._choose_mode("paper")
        html = self._html("/wallet")
        self.assertIn(
            "Not enough history yet for candles in this range", html)
        self.assertNotIn("History recording started today", html)


class TestLiveJsHomeRefresh(unittest.TestCase):
    """live.js ticks SOL values and refreshes the holdings list."""

    def _js(self):
        with open("tradingbot/static/live.js", encoding="utf-8") as f:
            return f.read()

    def test_sol_values_tick_live(self):
        js = self._js()
        self.assertIn("data-live-value-sol", js)
        self.assertIn("recomputeSolValues", js)
        self.assertIn("addCoin('SOL')", js)
        self.assertIn("toFixed(5) + ' SOL'", js)

    def test_holdings_list_refreshes_every_minute(self):
        js = self._js()
        self.assertIn("HOLDINGS_POLL_MS = 60000", js)
        self.assertIn("fetch('/api/home-holdings'", js)
        self.assertIn("setInterval(fetchHomeHoldings, HOLDINGS_POLL_MS)", js)
        self.assertIn("refreshLiveCollections", js)
        self.assertIn("data-home-holdings-list", js)
        self.assertIn("data-home-empty", js)


class TestHomeNameFit(unittest.TestCase):
    """Coin names render in full — never truncated or cut off."""

    def test_holdings_name_wraps_instead_of_ellipsising(self):
        css = _css()
        name_rule = _rule(css, ".holdings-row .coin-name")
        self.assertIn("white-space: normal", name_rule)
        self.assertIn("overflow: visible", name_rule)

    def test_very_long_names_drop_a_step_in_size(self):
        css = _css()
        self.assertIn("font-size: 13px",
                      _rule(css, ".holdings-row .name-long strong"))

    def test_sol_value_is_16px_like_the_price_was(self):
        css = _css()
        self.assertIn("font-size: 16px", _rule(css, ".holdings-sol"))


if __name__ == "__main__":
    unittest.main()
