"""P&L data: snapshots, chart ranges, per-coin P&L, and the pump.fun
trading wallet link. Charts are built only from recorded snapshots —
tests prove empty ranges report no data and no history is invented.

NOTE (2026-11-10): the /pnl page itself was deleted; its contents
moved — the portfolio chart lives on the home page (candlestick,
snapshots only), the activity log + losing-buy analysis live on the
Logs page (/logs, Page 11). This module now tests the pnl.py data
layer and the mode separation on the home page, plus that /pnl is
really gone."""

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup
from tradingbot.db import init_db
from tradingbot.pnl import (RANGES, chart_points, chart_svg,
                            current_holdings, portfolio_totals,
                            record_snapshot)
from tradingbot.silent_loop import run_once
from tradingbot.trenches import _entry as feed_entry


class FakeRpc:
    def __init__(self, url, balance_lamports=2_000_000_000):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance


class FailingRpc:
    def __init__(self, url):
        raise RuntimeError("rpc down")


class PnlBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 2_000_000_000
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.db_path = os.path.join(self.tmp.name, "test.db")

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _insert_snapshot(self, total, created_at, cash=10000.0,
                         real_sol=None):
        self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
            "holdings_value_usd, total_value_usd, real_sol, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.user_id, cash, total - cash, total, real_sol, created_at))
        self.db.commit()


class TestSnapshots(PnlBase):
    def test_record_snapshot_stores_row(self):
        record_snapshot(self.db, self.user_id, 9000.0, 1500.0, 2.0,
                        now="2026-11-09T12:00:00Z")
        row = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["total_value_usd"], 10500.0)
        self.assertAlmostEqual(row["real_sol"], 2.0)

    def test_chart_all_returns_only_recorded_points(self):
        self._insert_snapshot(10000.0, "2026-11-01T00:00:00Z")
        self._insert_snapshot(10200.0, "2026-11-05T00:00:00Z")
        self._insert_snapshot(9800.0, "2026-11-09T00:00:00Z")
        points, has_data = chart_points(self.db, self.user_id, "all")
        self.assertTrue(has_data)
        self.assertEqual([p["total_value_usd"] for p in points],
                         [10000.0, 10200.0, 9800.0])
        self.assertEqual(points[0]["created_at"], "2026-11-01T00:00:00Z")

    def test_chart_range_filters_by_age(self):
        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert_snapshot(10000.0, old)
        self._insert_snapshot(11000.0, recent)
        points, has_data = chart_points(self.db, self.user_id, "1h")
        # Only the recent row is inside the last hour; the 400-day-old
        # row is never shown for this range.
        self.assertTrue(has_data)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["total_value_usd"], 11000.0)

    def test_chart_empty_range_reports_no_data(self):
        points, has_data = chart_points(self.db, self.user_id, "1h")
        self.assertEqual(points, [])
        self.assertFalse(has_data)
        self.assertEqual(chart_svg(points), "")

    def test_chart_svg_draws_recorded_points_only(self):
        self._insert_snapshot(10000.0, "2026-11-09T10:00:00Z")
        self._insert_snapshot(10500.0, "2026-11-09T11:00:00Z")
        self._insert_snapshot(10300.0, "2026-11-09T12:00:00Z")
        points, _ = chart_points(self.db, self.user_id, "all")
        svg = chart_svg(points)
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        # Exactly the recorded values appear as labels — nothing else.
        self.assertIn("$10,500.00", svg)
        self.assertIn("$10,000.00", svg)
        self.assertNotIn("$9,999", svg)

    def test_all_ranges_are_known(self):
        self.assertEqual(set(RANGES),
                         {"all", "1y", "1mo", "1w", "1d", "1h"})


class TestHoldingsPnl(PnlBase):
    def _add_holding(self, coin, amount, avg_price):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, coin, amount, avg_price))
        self.db.commit()

    def test_per_coin_pnl_math(self):
        self._add_holding("SOL", 2.0, 80.0)
        holdings = current_holdings(self.db, self.user_id, {"SOL": 100.0})
        self.assertEqual(len(holdings), 1)
        self.assertAlmostEqual(holdings[0]["value"], 200.0)
        self.assertAlmostEqual(holdings[0]["pnl"], 40.0)

    def test_unknown_price_valued_at_cost_never_guessed(self):
        self._add_holding("SOME_MINT", 10.0, 0.5)
        holdings = current_holdings(self.db, self.user_id, {})
        self.assertIsNone(holdings[0]["price"])
        self.assertIsNone(holdings[0]["pnl"])
        self.assertAlmostEqual(holdings[0]["value"], 5.0)

    def test_portfolio_totals(self):
        self._add_holding("SOL", 1.0, 90.0)
        totals = portfolio_totals(self.db, self.user_id, {"SOL": 100.0})
        self.assertAlmostEqual(totals["cash_usd"], 100.0)
        self.assertAlmostEqual(totals["holdings_value_usd"], 100.0)
        self.assertAlmostEqual(totals["total_value_usd"], 200.0)


class TestPnlPage(PnlBase):
    def test_pnl_route_is_gone(self):
        # The P&L page was deleted 2026-11-10 (its chart moved to
        # the home page; its log + analysis moved to /logs). The
        # route must 404 in both modes, logged in or out — never
        # redirect to a login page for a page that no longer exists.
        self.client.get("/logout")
        self.assertEqual(self.client.get("/pnl").status_code, 404)
        login(self.client, "alice")
        self.assertEqual(self.client.get("/pnl").status_code, 404)
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "devnet"})
        self.assertEqual(self.client.get("/pnl").status_code, 404)

    def test_home_shows_mode_portfolio_not_the_other(self):
        # Paper mode shows the paper portfolio only — the real
        # on-chain balance must not leak into it.
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("$100.00 CAD", html)  # paper portfolio value
        self.assertNotIn("2.0000 SOL", html)
        # Real mode shows the on-chain balance only — no paper value.
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "devnet"})
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("2.0000 SOL", html)  # mocked on-chain balance
        self.assertNotIn("$100.00 CAD", html)

    def test_paper_buy_records_snapshot_and_home_shows_it(self):
        token = csrf(self.client, "/dashboard")
        resp = self.client.post("/coins/SOL/buy", data={
            "csrf_token": token, "usd": "10", "bot_id": ""},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bought", resp.get_data(as_text=True))
        rows = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        # $10 buy: cash $90 + 0.09635 SOL at $100 ($9.635) = $99.635
        # — the 3.65% fee is a cost, so the total is under $100.
        self.assertAlmostEqual(rows[0]["total_value_usd"], 99.635,
                               places=1)
        # The home page now shows the portfolio total (the old /pnl
        # page is gone): $99.635 rounds to $99.64 CAD.
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("SOL", html)
        self.assertIn("$99.64 CAD", html)

    def test_silent_loop_evaluation_records_snapshot(self):
        self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Luna', 'balanced', '[\"SOL\"]', "
            "20, 7, 'paper', 1, 3, ?)",
            (self.user_id, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        outcomes = run_once(
            self.db, self.cfg,
            price_fetcher=lambda: {"prices": {}, "source": "snapshot",
                                   "as_of": "test"},
            rpc_factory=lambda url: FakeRpc(url))
        self.assertEqual(len(outcomes), 1)
        rows = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["real_sol"], 2.0)

    def test_snapshot_survives_rpc_failure(self):
        self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, 'Luna', 'balanced', '[\"SOL\"]', "
            "20, 7, 'paper', 1, 3, ?)",
            (self.user_id, hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        outcomes = run_once(
            self.db, self.cfg,
            price_fetcher=lambda: {"prices": {}, "source": "snapshot",
                                   "as_of": "test"},
            rpc_factory=lambda url: FailingRpc(url))
        self.assertEqual(len(outcomes), 1)
        row = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["real_sol"])


class TestPumpfunWallet(PnlBase):
    def test_signup_links_app_wallet_as_pumpfun_wallet(self):
        wallet = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()
        user = self.db.execute(
            "SELECT pumpfun_wallet FROM users WHERE id = ?",
            (self.user_id,)).fetchone()
        self.assertEqual(user["pumpfun_wallet"], wallet["pubkey"])
        self.assertTrue(user["pumpfun_wallet"])

    def test_wallet_page_shows_pumpfun_wallet(self):
        # The pump.fun wallet lives in Real trading — paper mode shows
        # the paper summary instead. The wallet itself moved to
        # /wallet/details in the fox-layout rebuild.
        self.assertNotIn(
            "Pump.fun trading wallet",
            self.client.get("/wallet").get_data(as_text=True))
        self.client.post("/mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "devnet"})
        resp = self.client.get("/wallet/details")
        html = resp.get_data(as_text=True)
        wallet = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIn("Pump.fun trading wallet", html)
        self.assertIn(wallet["pubkey"], html)
        self.assertIn("no separate accounts", html)

    def test_backfill_links_older_accounts(self):
        # Simulate a user created before the column existed.
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt, "
            "pumpfun_wallet) VALUES ('oldie', 'x', 'y', '')")
        old_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'oldie'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO wallets (user_id, pubkey, encrypted_secret) "
            "VALUES (?, 'OLDWALLETPUBKEY', 'blob')", (old_id,))
        self.db.commit()
        conn2 = init_db(self.db_path)
        try:
            row = conn2.execute(
                "SELECT pumpfun_wallet FROM users WHERE id = ?",
                (old_id,)).fetchone()
            self.assertEqual(row["pumpfun_wallet"], "OLDWALLETPUBKEY")
        finally:
            conn2.close()

    def test_other_users_pumpfun_wallet_stays_private(self):
        signup(self.client, "bob")
        bob_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'bob'"
        ).fetchone()["id"]
        bob_wallet = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (bob_id,)).fetchone()["pubkey"]
        login(self.client, "alice")
        resp = self.client.get("/wallet")
        html = resp.get_data(as_text=True)
        self.assertNotIn(bob_wallet, html)


class TestFeedMarketCap(unittest.TestCase):
    def test_entry_carries_market_cap_for_display(self):
        pair = {"baseToken": {"symbol": "TINY", "address": "MINT"},
                "priceUsd": "0.001", "dexId": "pumpfun",
                "priceChange": {"m5": 5.0}, "marketCap": 400,
                "liquidity": {"usd": 100}, "pairAddress": "pair1",
                "pairCreatedAt": 0, "chainId": "solana"}
        entry = feed_entry("MINT", pair)
        self.assertEqual(entry["market_cap_usd"], 400)
        self.assertEqual(entry["change_5m_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
