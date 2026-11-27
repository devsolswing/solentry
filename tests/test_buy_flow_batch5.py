"""Batch 5 — bot-picker removal, homepage popup only (buy-flow rebuild).

Exactly one logical change, per James's order (2026-11-27 ~07:26,
scope settled ~07:28, refinement ~07:30): on the homepage BUY
button's pop-up (the quick-trade popup as rendered on the homepage,
wallet.html), there is NO bot selection at all — not even a "no bot /
don't watch" option. The buy is manual by default; nothing to
select. A plain rug warning sits near the Buy button instead.

- The quick-trade partial (_quick_trade.html) is shared with the
  coins page (dashboard.html), so the removal is scoped with a
  context flag: wallet.html includes it inside
  `{% with manual_buy_only=True %}`, the coins page includes it
  plain. Only the homepage instance loses the picker and gains the
  warning; the coins page, the coin popup ticket and the coin detail
  overlay keep their pickers.
- The sell forms in the same popup are untouched (their bot pickers
  stay, real mode only) — this batch is about the BUY form.
- SL/TP stay exactly as batch 3 built them: optional checkboxes,
  unchecked by default, nothing required on a manual trade.
- No route change was needed: batch 1 already made a buy with no
  bot_id a User buy (`request.form.get("bot_id") or ""` -> blank
  -> (None, None)), and the homepage popup now submits no bot_id
  field at all. The real form's `required` bot select is gone with
  the picker, so the form can submit.

Warning wording (exact, also in the MANIFEST):
"Manual trade — this one is on you. Be careful of rugs: a new coin
can go to zero fast."

No other buy-flow work lives here: no % buttons change (batch 4),
no wallet SOL live-or-"no data", no paper stale refusal, no
_parse_buy_amount unification, no currency picker, no Trenches
quick buy (ordered separately 2026-11-27 ~07:32, its own batch).
"""

import os
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as coin_meta_mod
import tradingbot.home_holdings as home_holdings_mod
import tradingbot.jupiter as jupiter_mod
import tradingbot.routes as routes
from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET, csrf,
                     grant_fuel, make_client, signup)
from tradingbot.config import Config

# Gate — the devnet pump.fun mint used as the test coin throughout.
GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PRICE = 0.0000005

WARNING = "Manual trade — this one is on you."
WARNING_RUGS = "Be careful of rugs: a new coin can go to zero fast."


def _test_config():
    return Config(env={
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "m.db"),
        "PAPER_START_CASH": "10000"})


def _start_route_mocks(tc):
    """Same local-feed mocks as batch 4 (batch 1's route mocks plus
    the detail page's chart + trade-flow mocks)."""
    tc.mint_prices = {GATE: GATE_PRICE}
    tc.mint_identities = {GATE: {"name": "Gate", "symbol": "GATE"}}
    patches = [
        mock.patch.object(
            routes, "_chain_prices_for_mints",
            side_effect=lambda app, mints: {
                m: tc.mint_prices[m] for m in mints
                if m in tc.mint_prices}),
        mock.patch.object(
            routes, "fetch_mint_identities",
            side_effect=lambda mints, http_get=None: {
                m: tc.mint_identities[m] for m in mints
                if m in tc.mint_identities}),
        mock.patch.object(routes, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "ensure_coin_meta",
                          return_value=None),
        mock.patch.object(home_holdings_mod, "_default_get",
                          return_value={"pairs": []}),
        mock.patch.object(coin_meta_mod, "_from_metaplex",
                          return_value=None),
        mock.patch.object(
            jupiter_mod, "quote_buy_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            jupiter_mod, "quote_sell_mint",
            side_effect=jupiter_mod.JupiterError("no route in tests")),
        mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None}),
        mock.patch.object(
            routes, "_feed_sol_price_usd",
            return_value=10.0),
        mock.patch.object(routes, "is_curve_complete",
                          return_value=False),
        mock.patch.object(routes, "execute_real_trade"),
        mock.patch.object(routes, "execute_jupiter_swap"),
        mock.patch.object(
            routes, "fetch_market_chart",
            return_value={"points": [], "error": "no chart history"}),
        mock.patch.object(routes, "fetch_trade_flow",
                          return_value=None),
    ]
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)
    routes.execute_real_trade.return_value = {
        "status": "executed", "signature": "sig-batch5",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    routes.execute_jupiter_swap.return_value = {
        "status": "executed", "signature": "sig-batch5-jup",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    tc.app.config["LIVE_FEED"].ingest(GATE, GATE_PRICE, "helius-stream")
    routes._TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
    home_holdings_mod.clear_image_cache()


def _buy_form(html):
    """The quick-trade BUY form's HTML, so picker assertions can't
    be fooled by the sell forms on the same page."""
    start = html.index('id="quick-trade-buy-form"')
    end = html.index("</form>", start)
    return html[start:end]


class TestBatch5HomepagePopupRender(unittest.TestCase):
    """Paper mode (the default after signup): the homepage
    quick-trade buy form has no bot picker at all and shows the rug
    warning; the coins page keeps the picker; SL/TP stay optional."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def test_homepage_buy_form_has_no_bot_picker(self):
        html = self._html("/wallet")
        buy = _buy_form(html)
        self.assertNotIn('name="bot_id"', buy)
        self.assertNotIn("<select", buy)
        self.assertNotIn("Bot to look after", buy)
        self.assertNotIn("Real bot to manage this buy", buy)

    def test_homepage_buy_form_shows_the_rug_warning(self):
        html = self._html("/wallet")
        buy = _buy_form(html)
        self.assertIn(WARNING, buy)
        self.assertIn(WARNING_RUGS, buy)

    def test_warning_sits_before_the_buy_button(self):
        html = self._html("/wallet")
        buy = _buy_form(html)
        self.assertLess(buy.index("Manual trade"),
                        buy.index('id="quick-trade-buy-btn"'))

    def test_homepage_sltp_still_optional(self):
        html = self._html("/wallet")
        buy = _buy_form(html)
        self.assertIn('name="stop_loss"', buy)
        self.assertIn('name="take_profit"', buy)
        self.assertIn('name="stop_loss_pct"', buy)
        self.assertIn('name="take_profit_pct"', buy)

    def test_coins_page_keeps_the_picker(self):
        html = self._html("/dashboard")
        buy = _buy_form(html)
        self.assertIn("Bot to look after this coin", buy)
        self.assertIn('name="bot_id"', buy)
        self.assertNotIn(WARNING, html)

    def test_coin_popup_keeps_the_picker(self):
        html = self._html(f"/coins/{GATE}")
        self.assertIn("Bot to look after this coin", html)

    def test_coin_detail_keeps_the_picker(self):
        html = self._html(f"/coins/{GATE}/detail")
        self.assertIn("Bot to look after this coin", html)


class TestBatch5HomepagePopupRealRender(unittest.TestCase):
    """Real mode: the homepage buy form loses the REQUIRED bot
    select too; the sell forms keep their pickers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
        grant_fuel(self.db, self.user_id, 100)
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": "devnet"})
        self.balance_patch = mock.patch.object(
            routes, "_real_balance", return_value=0.0471)
        self.balance_patch.start()
        self.addCleanup(self.balance_patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def test_homepage_buy_form_has_no_bot_picker(self):
        html = self._html("/wallet")
        buy = _buy_form(html)
        self.assertNotIn('name="bot_id"', buy)
        self.assertNotIn("<select", buy)
        self.assertNotIn("Real bot to manage this buy", buy)
        self.assertIn(WARNING, buy)

    def test_homepage_sell_forms_keep_their_picker(self):
        html = self._html("/wallet")
        self.assertIn("Real bot to manage this sell (required)", html)

    def test_coins_page_keeps_real_picker(self):
        html = self._html("/dashboard")
        buy = _buy_form(html)
        self.assertIn("Real bot to manage this buy", buy)
        self.assertIn("<select", buy)


class _Batch5RouteBase(unittest.TestCase):
    """Route tests: the homepage popup now submits no bot_id field
    at all — not even an empty one."""

    MODE = "paper"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        _start_route_mocks(self)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]
        grant_fuel(self.db, self.user_id, 100)
        self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/dashboard"),
            "mode": self.MODE})

    def tearDown(self):
        self.tmp.cleanup()

    def _buy_no_bot_field(self, **data):
        form = {"csrf_token": csrf(self.client, f"/coins/{GATE}")}
        form.update(data)
        return self.client.post(f"/coins/{GATE}/buy", data=form)

    def _exit_row(self, mode):
        return self.db.execute(
            "SELECT * FROM user_exits WHERE user_id = ? "
            "AND coin_id = ? AND mode = ?",
            (self.user_id, GATE, mode)).fetchone()


class TestBatch5PaperBuyNoBotField(_Batch5RouteBase):
    """A paper buy from the homepage popup shape (no bot_id field)
    is a User buy; SL/TP still work from it when ticked."""

    MODE = "paper"

    def test_buy_without_bot_field_succeeds_and_stores_no_exit(self):
        resp = self._buy_no_bot_field(usd="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("User buy — no bot watching this coin.", html)
        self.assertIsNone(self._exit_row("paper"))

    def test_buy_without_bot_field_with_sl_still_stores_exit(self):
        resp = self._buy_no_bot_field(usd="4", stop_loss="1",
                                      stop_loss_pct="5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        row = self._exit_row("paper")
        self.assertIsNotNone(row, "no user_exits row stored")
        self.assertEqual(row["stop_loss_pct"], 5)
        self.assertIsNone(row["take_profit_pct"])


class TestBatch5RealBuyNoBotField(_Batch5RouteBase):
    """A real buy from the homepage popup shape (no bot_id field)
    goes through the mocked executor with no bot."""

    MODE = "devnet"

    def test_real_buy_without_bot_field_succeeds(self):
        resp = self._buy_no_bot_field(amount="4")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bought", html)
        self.assertIn("User buy — no bot watching this coin.", html)
        routes.execute_real_trade.assert_called_once()
        _args, _kwargs = routes.execute_real_trade.call_args
        self.assertIsNone(_args[0], "no bot on a manual buy")
        self.assertIsNone(self._exit_row("real"))


if __name__ == "__main__":
    unittest.main()
