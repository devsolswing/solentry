"""Batch 4 — quick-buy % buttons (buy-flow rebuild).

Exactly one logical change, per James's spec (2026-11-26 ~21:19):
25% / 50% / 75% / 100% buttons on the buy screens, each buying
that % of the SOL in the wallet but always leaving the tiered gas
reserve behind (batch 1: under $5 USD -> 0.0012 SOL, $5 USD or
more -> 0.012 SOL, threshold in USD at the live SOL price).
100% = wallet minus reserve. The button shows the SOL amount it
will buy. The math lives in exactly one place
(Config.pct_buy_amounts_sol) and reuses batch 1's tier helper —
the templates and the route never re-implement the tier, and the
server-side reserve refusal in the executors stays as the
backstop (already covered by batch 1's executor + route refusal
tests — not duplicated here).

No other buy-flow work lives here: no wallet SOL live-or-"no
data" (home card), no paper stale refusal, no _parse_buy_amount
unification, no bot-picker removal, no SL/TP changes (batch 3),
no currency picker.
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


def _test_config():
    return Config(env={
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "m.db"),
        "PAPER_START_CASH": "10000"})


class TestPctBuyAmounts(unittest.TestCase):
    """Config.pct_buy_amounts_sol: the % math, the tier, the cap,
    and the fail-safes. SOL $100 unless stated, so a % of the
    wallet converts to USD at a round rate."""

    def test_each_button_buys_its_pct_of_the_wallet(self):
        cfg = _test_config()
        amounts = cfg.pct_buy_amounts_sol(0.1, 100.0)
        self.assertEqual(amounts, {25: 0.025, 50: 0.05,
                                   75: 0.075, 100: 0.088})

    def test_hundred_pct_leaves_exactly_the_reserve(self):
        cfg = _test_config()
        # 0.1 SOL at $100 is a $10 buy -> large tier: 0.1 - 0.012.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.1, 100.0)[100],
                         0.088)
        # 0.0471 SOL at $73 is a $3.44 buy -> small tier:
        # 0.0471 - 0.0012 (James's app-wallet shape).
        self.assertEqual(cfg.pct_buy_amounts_sol(0.0471, 73.0)[100],
                         0.0459)

    def test_tier_flips_at_five_usd(self):
        cfg = _test_config()
        # Exactly $5.00 -> the large tier (boundary is >= $5).
        self.assertEqual(cfg.pct_buy_amounts_sol(0.05, 100.0)[100],
                         0.038)
        # $4.00 -> the small tier.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.04, 100.0)[100],
                         0.0388)

    def test_no_sol_price_fails_safe_to_large_tier(self):
        cfg = _test_config()
        # No live SOL price: the USD size is unknown, so the large
        # tier is the fail-safe — same as batch 1's tier helper.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.1, None)[100],
                         0.088)
        self.assertEqual(cfg.pct_buy_amounts_sol(0.1, None)[25],
                         0.025)

    def test_tiny_wallet_disables_every_button(self):
        cfg = _test_config()
        # 0.001 SOL cannot cover even the 0.0012 small tier.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.001, 100.0),
                         {25: None, 50: None, 75: None, 100: None})
        # Exactly the small tier leaves nothing behind.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.0012, 100.0),
                         {25: None, 50: None, 75: None, 100: None})
        # A hair above the tier: every button caps at what is left.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.0013, 100.0),
                         {25: 0.0001, 50: 0.0001,
                          75: 0.0001, 100: 0.0001})

    def test_mid_pct_caps_at_wallet_minus_reserve(self):
        cfg = _test_config()
        # 0.013 SOL at $1000: 75% is $9.75 -> large tier, and the
        # raw 0.00975 would leave only 0.00325 behind — below the
        # 0.012 tier. The button caps at wallet minus reserve.
        amounts = cfg.pct_buy_amounts_sol(0.013, 1000.0)
        # 0.013 - 0.012 is a hair under 0.001 in floating point,
        # and the floor keeps it there — the safe direction, 1e-6
        # SOL under the cap, never over it.
        self.assertEqual(amounts[75], 0.000999)
        self.assertEqual(amounts[100], 0.000999)
        # 25% is $3.25 -> small tier, and its raw amount leaves
        # more than the tier behind, so it is not capped.
        self.assertEqual(amounts[25], 0.00325)

    def test_ata_rent_included_when_asked(self):
        cfg = _test_config()
        # A first buy of a coin with no token account yet costs
        # the ~0.00204 SOL rent on top of the tier, or the executor
        # refuses the button's amount at the edge.
        self.assertEqual(
            cfg.pct_buy_amounts_sol(0.1, 100.0,
                                    include_ata_rent=True)[100],
            0.08596)
        self.assertEqual(
            cfg.pct_buy_amounts_sol(0.1, 100.0,
                                    include_ata_rent=False)[100],
            0.088)
        # James's wallet shape with rent: 0.0471 - 0.0012 - 0.00204.
        self.assertEqual(
            cfg.pct_buy_amounts_sol(0.0471, 73.0,
                                    include_ata_rent=True)[100],
            0.04386)

    def test_missing_or_bad_wallet_disables_every_button(self):
        cfg = _test_config()
        for bad in (None, 0, -1, "junk"):
            self.assertEqual(cfg.pct_buy_amounts_sol(bad, 100.0),
                             {25: None, 50: None, 75: None,
                              100: None}, bad)

    def test_tiers_are_env_overridable(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "m.db"),
            "PAPER_START_CASH": "10000",
            "USER_BUY_RESERVE_THRESHOLD_USD": "10",
            "USER_BUY_RESERVE_SMALL_SOL": "0.002",
            "USER_BUY_RESERVE_LARGE_SOL": "0.02"})
        # $3.44 < $10 threshold -> the overridden small tier.
        self.assertEqual(cfg.pct_buy_amounts_sol(0.0471, 73.0)[100],
                         0.0451)


class TestPctBuyButtonsRow(unittest.TestCase):
    """routes._pct_buy_buttons: the template row — SOL amounts,
    $ amounts floored to cents, labels, and the plain reason when
    the row cannot work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sol_row_amounts_labels_and_usd(self):
        row = routes._pct_buy_buttons(self.app, 0.1, 100.0, False)
        self.assertIsNone(row["disabled_reason"])
        self.assertEqual(row["wallet_sol"], 0.1)
        self.assertEqual([a["pct"] for a in row["amounts"]],
                         [25, 50, 75, 100])
        self.assertEqual([a["sol"] for a in row["amounts"]],
                         [0.025, 0.05, 0.075, 0.088])
        # 0.088 SOL at $100 floors to $8.79 in floating point
        # (0.088 * 100 * 100 is a hair under 880) — the safe
        # direction: the fill buys a tenth of a cent less SOL than
        # the label, never more.
        self.assertEqual([a["usd"] for a in row["amounts"]],
                         [2.5, 5.0, 7.5, 8.79])
        self.assertEqual([a["label"] for a in row["amounts"]],
                         ["0.025", "0.05", "0.075", "0.088"])

    def test_usd_amounts_floor_to_cents(self):
        # 9.98596 SOL at $10 is $99.8596 — floored to $99.85, never
        # rounded up past the button's SOL amount.
        row = routes._pct_buy_buttons(self.app, 10.0, 10.0, True,
                                      target="usd")
        self.assertEqual(row["amounts"][3]["sol"], 9.98596)
        self.assertEqual(row["amounts"][3]["usd"], 99.85)

    def test_usd_target_without_sol_price_disables(self):
        row = routes._pct_buy_buttons(self.app, 0.1, None, True,
                                      target="usd")
        # The SOL amounts still compute (no price fails safe to the
        # large tier); the rent is included in this call, so 100%
        # is 0.1 - 0.012 - 0.00204 = 0.08596.
        self.assertEqual([a["sol"] for a in row["amounts"]],
                         [0.025, 0.05, 0.075, 0.08596])
        self.assertEqual([a["usd"] for a in row["amounts"]],
                         [None, None, None, None])
        self.assertEqual(row["disabled_reason"],
                         "Live SOL price unavailable right now.")

    def test_missing_wallet_balance_disables(self):
        row = routes._pct_buy_buttons(self.app, None, 100.0, True)
        self.assertEqual([a["sol"] for a in row["amounts"]],
                         [None, None, None, None])
        self.assertEqual(row["disabled_reason"],
                         "Wallet balance unavailable right now.")

    def test_tiny_wallet_disables_with_plain_reason(self):
        row = routes._pct_buy_buttons(self.app, 0.001, 100.0, True)
        self.assertEqual([a["sol"] for a in row["amounts"]],
                         [None, None, None, None])
        self.assertIn("gas reserve", row["disabled_reason"])


def _start_route_mocks(tc):
    """Same local-feed mocks as batch 1, plus the detail page's
    chart + trade-flow mocks (test_coins_page pattern)."""
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
        "status": "executed", "signature": "sig-batch4",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    routes.execute_jupiter_swap.return_value = {
        "status": "executed", "signature": "sig-batch4-jup",
        "coin_amount": 1000000.0, "sol_spent": 0.005,
        "signing_bot": "test"}
    tc.app.config["LIVE_FEED"].ingest(GATE, GATE_PRICE, "helius-stream")
    routes._TRENCHES_CACHE.update({"at": 0.0, "rows": None, "error": None})
    home_holdings_mod.clear_image_cache()


class TestBatch4Render(unittest.TestCase):
    """The buttons render on every buy screen, with the amounts
    the server computed — paper cash $100 at $10 SOL is a 10 SOL
    wallet, so the quick-trade row (rent always) reads
    2.5 / 5 / 7.5 / 9.98596 SOL."""

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

    def test_quick_trade_buttons_on_dashboard(self):
        html = self._html("/dashboard")
        for label in ("25% · 2.5 SOL", "50% · 5 SOL",
                      "75% · 7.5 SOL", "100% · 9.98596 SOL"):
            self.assertIn(label, html)
        self.assertIn('data-amount-sol="9.98596"', html)
        self.assertIn('data-amount-usd="99.85"', html)
        self.assertNotIn("Wallet balance unavailable", html)

    def test_quick_trade_buttons_on_wallet_page(self):
        html = self._html("/wallet")
        self.assertIn("100% · 9.98596 SOL", html)
        self.assertIn('data-amount-usd="99.85"', html)

    def test_ticket_buttons_on_coin_popup(self):
        html = self._html(f"/coins/{GATE}")
        # The popup ticket (no holding yet, so the rent applies)
        # shows the same row as quick trade for this wallet.
        self.assertIn("100% · 9.98596 SOL", html)
        self.assertIn('data-amount-sol="2.5"', html)

    def test_buttons_on_coin_detail_page(self):
        html = self._html(f"/coins/{GATE}/detail")
        self.assertIn("100% · 9.98596 SOL", html)
        self.assertIn('data-amount-sol="9.98596"', html)

    def test_no_sol_price_disables_paper_buttons(self):
        with mock.patch.object(routes, "_feed_sol_price_usd",
                               return_value=None):
            html = self._html("/dashboard")
        self.assertIn("Wallet balance unavailable right now.", html)
        self.assertNotIn("25% ·", html)

    def test_tiny_paper_wallet_disables_with_plain_reason(self):
        cur = self.db.execute(
            "UPDATE paper_portfolio SET cash_usd = 0.01 "
            "WHERE user_id = ?", (self.user_id,))
        self.assertEqual(cur.rowcount, 1)
        self.db.commit()
        html = self._html("/dashboard")
        self.assertIn("Not enough SOL in the wallet to leave the "
                      "gas reserve behind", html)
        self.assertNotIn("25% ·", html)


class TestBatch4RealRender(unittest.TestCase):
    """Real mode: the buttons spend on-chain SOL. The RPC read is
    mocked to James's app-wallet shape (0.0471 SOL at $10)."""

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

    def test_ticket_buttons_use_on_chain_sol(self):
        resp = self.client.get(f"/coins/{GATE}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # 0.0471 SOL at $10 is $0.47 -> small tier + rent:
        # 100% = 0.0471 - 0.0012 - 0.00204 = 0.04386.
        self.assertIn("100% · 0.04386 SOL", html)
        self.assertIn("25% · 0.011775 SOL", html)
        self.assertIn('data-amount-sol="0.04386"', html)


if __name__ == "__main__":
    unittest.main()
