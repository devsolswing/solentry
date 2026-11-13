"""P&L / live-price accuracy: P&L from executed on-chain amounts, live
mark prices only, no invented numbers.

The 2026-11-13 lesson: Trenches Real bought Gate + AuditTest and sold
both out; the app called it "5 wins, +$5.78" while on-chain math says
both round-trips lost on price. The ledger used loop-seen mark prices
for cost and proceeds. These tests pin the fix: cost from the SOL the
chain actually took, proceeds from the SOL actually received, and no
win/loss claimed when executed amounts are unknown.
"""

import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
from helpers import make_client, signup
from tradingbot.ledger import bot_wl_summary
from tradingbot.livefeed import LiveFeed
from tradingbot.paper import holdings_value
from tradingbot.pnl import change_24h, current_holdings, record_snapshot
from tradingbot.routes import _coin_rows, _real_avg_price
from tradingbot.silent_loop import (
    _buy_coin,
    _live_sol_price_usd,
    _positions,
    _sell_position,
)

GATE = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
AUDT = "3k6ZYa2Wh6P92VN2uGvKsmHiw2RQpcVgxpb447raf2kC"
WALLET = "11111111111111111111111111111111"

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _fmt(dt):
    return dt.strftime(_FMT)


class FakeRpc:
    def __init__(self, url, balance_lamports=1_000_000_000):
        self._balance = balance_lamports

    def get_balance_lamports(self, pubkey):
        return self._balance

    def get_latest_blockhash(self):
        raise AssertionError("no sends in these tests")

    def send_transaction(self, signed_b64):
        raise AssertionError("no sends in these tests")


def _live_prices(sol=80.0):
    return {"prices": {"SOL": {"price": sol, "change_24h": 0.0}},
            "source": "live", "as_of": "test"}


class AccuracyBase(unittest.TestCase):
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

    def _insert_bot(self, mode="real", buy_size=20, stop_loss=7,
                    max_open=3):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, "Trenches", "balanced", json.dumps([GATE]),
             buy_size, stop_loss, mode, 1, max_open,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (cur.lastrowid,)).fetchone()

    def _insert_wallet(self):
        # Signup already creates the app wallet; only insert when
        # the row is missing.
        row = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO wallets (user_id, pubkey, encrypted_secret) "
                "VALUES (?, ?, ?)", (self.user_id, WALLET, "x"))
            self.db.commit()


class TestExecutedPnl(AccuracyBase):
    def test_losing_round_trip_is_a_loss_not_a_win(self):
        """The Gate lesson in miniature: mark prices say the sell
        wins (+$0.60), executed SOL says it loses (-$0.80). The
        ledger must record the executed loss."""
        bot = self._insert_bot(mode="real")
        self._insert_wallet()
        self.feed.ingest("SOL", 80.0, "kraken")
        prices_data = _live_prices(80.0)

        def fake_buy(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            self.assertEqual(side, "buy")
            return {"status": "executed", "signature": "sigbuy",
                    "coin_amount": 1000.0, "sol_spent": 0.13,
                    "signing_bot": "test-bot"}

        actions = []
        # Mark price at buy time is $0.02; the chain took 0.13 SOL
        # for 1000 tokens at $80/SOL = $0.0104 executed cost.
        _buy_coin(self.db, self.cfg, bot, GATE, 0.02, {},
                  lambda url: FakeRpc(url), fake_buy, actions,
                  feed=self.feed, prices_data=prices_data)
        self.assertEqual(actions[-1]["action"], "buy")
        pos = _positions(self.db, bot["id"])[GATE]
        self.assertAlmostEqual(pos["avg_price"], 0.0104, places=4)
        buy_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side = 'buy'",
            (bot["id"],)).fetchone()
        self.assertAlmostEqual(buy_row["price"], 0.0104, places=4)

        def fake_sell(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            self.assertEqual(side, "sell")
            return {"status": "executed", "signature": "sigsell",
                    "sol_received": 0.12, "signing_bot": "test-bot"}

        actions = []
        # Mark price at sell time is $0.011 — above the $0.0104 cost,
        # so mark math would call this a win. Executed: 0.12 SOL in
        # = $9.60 against $10.40 cost = -$0.80, a loss.
        _sell_position(self.db, self.cfg, bot, GATE, 0.011,
                       "sell ladder: 100% at +6% profit",
                       lambda url: FakeRpc(url), fake_sell, actions,
                       feed=self.feed, prices_data=prices_data)
        self.assertEqual(actions[-1]["action"], "sell")
        sell_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side='sell'",
            (bot["id"],)).fetchone()
        self.assertAlmostEqual(sell_row["pnl_usd"], -0.80, places=2)
        self.assertEqual(sell_row["win"], 0)
        summary = bot_wl_summary(self.db, bot["id"])
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["net_pnl_usd"], -0.80, places=2)

    def test_unknown_proceeds_means_no_win_or_loss(self):
        """An executor that does not report SOL received leaves the
        trade recorded with no USD P&L — never a mark-based win."""
        bot = self._insert_bot(mode="real")
        self._insert_wallet()
        self.feed.ingest("SOL", 80.0, "kraken")
        prices_data = _live_prices(80.0)

        def fake_buy(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            return {"status": "executed", "signature": "sigbuy",
                    "coin_amount": 1000.0, "sol_spent": 0.13,
                    "signing_bot": "test-bot"}

        _buy_coin(self.db, self.cfg, bot, GATE, 0.02, {},
                  lambda url: FakeRpc(url), fake_buy, [],
                  feed=self.feed, prices_data=prices_data)

        def fake_sell(_bot, _uid, _coin, side, _amount, _price, _db, _cfg):
            return {"status": "executed", "signature": "sigsell",
                    "signing_bot": "test-bot"}

        actions = []
        _sell_position(self.db, self.cfg, bot, GATE, 0.011, "stop_loss",
                       lambda url: FakeRpc(url), fake_sell, actions,
                       feed=self.feed, prices_data=prices_data)
        self.assertEqual(actions[-1]["action"], "sell")
        sell_row = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ? AND side='sell'",
            (bot["id"],)).fetchone()
        self.assertIsNone(sell_row["pnl_usd"])
        self.assertIsNone(sell_row["win"])
        summary = bot_wl_summary(self.db, bot["id"])
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 0)


class TestLiveSolPrice(unittest.TestCase):
    def test_feed_tick_wins_over_rest(self):
        feed = LiveFeed()
        feed.ingest("SOL", 80.0, "kraken")
        self.assertEqual(
            _live_sol_price_usd(feed, _live_prices(79.0)), 80.0)

    def test_stale_feed_tick_is_ignored(self):
        feed = LiveFeed()
        feed.ingest("SOL", 80.0, "kraken", now=time.time() - 120)
        self.assertEqual(
            _live_sol_price_usd(feed, _live_prices(79.0)), 79.0)

    def test_snapshot_price_is_never_live(self):
        snap = {"prices": {"SOL": {"price": 86.14}},
                "source": "snapshot", "as_of": "2026-11-09T10:10:11Z"}
        self.assertIsNone(_live_sol_price_usd(None, snap))
        self.assertIsNone(_live_sol_price_usd(LiveFeed(), snap))

    def test_live_rest_price_used_without_feed(self):
        self.assertEqual(
            _live_sol_price_usd(None, _live_prices(79.0)), 79.0)


class TestFeedCurvePrices(AccuracyBase):
    def test_poll_curves_ingests_devnet_watchlist(self):
        """Gate/AuditTest have no DEX pool — the feed prices them
        from their bonding curves, labelled source 'curve'."""
        self.feed.ingest("SOL", 80.0, "kraken")
        with mock.patch(
                "tradingbot.devnet_watchlist.curve_price_usd",
                return_value=(0.00012, {})), \
                mock.patch("tradingbot.solana.SolanaRpcClient", FakeRpc):
            self.feed._poll_curves()
        snap = self.feed.snapshot([GATE, AUDT])
        self.assertIn(GATE, snap)
        self.assertEqual(snap[GATE]["price"], 0.00012)
        self.assertEqual(snap[GATE]["source"], "curve")
        self.assertFalse(snap[GATE]["stale"])
        self.assertIn(AUDT, snap)

    def test_poll_curves_needs_a_fresh_sol_tick(self):
        """No live SOL price, no curve price — fail closed, never a
        guess from a stale SOL."""
        with mock.patch(
                "tradingbot.devnet_watchlist.curve_price_usd",
                return_value=(0.00012, {})), \
                mock.patch("tradingbot.solana.SolanaRpcClient", FakeRpc):
            self.feed._poll_curves()
        self.assertEqual(self.feed.snapshot([GATE]), {})

    def test_poll_curves_inert_off_devnet(self):
        """active_mints returns [] unless NETWORK is devnet."""
        feed = LiveFeed(config=mock.Mock(network="mainnet"))
        feed.ingest("SOL", 80.0, "kraken")
        with mock.patch(
                "tradingbot.devnet_watchlist.curve_price_usd",
                return_value=(0.00012, {})) as curve_fn, \
                mock.patch("tradingbot.solana.SolanaRpcClient", FakeRpc):
            feed._poll_curves()
        curve_fn.assert_not_called()
        self.assertEqual(feed.snapshot([GATE]), {})


class TestChange24h(AccuracyBase):
    def _snap(self, when, total):
        record_snapshot(self.db, self.user_id, total, 0.0, None,
                        now=_fmt(when))

    def test_baseline_is_nearest_to_24h_ago(self):
        """A later snapshot must not become the '24h' baseline: the
        % is measured against the value ~24h ago."""
        now = datetime.now(timezone.utc)
        self._snap(now - timedelta(hours=23, minutes=59), 100.0)
        self._snap(now - timedelta(hours=20), 500.0)
        result = change_24h(self.db, self.user_id, 640.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["pct"], 540.0, places=1)
        self.assertAlmostEqual(result["delta"], 540.0, places=1)

    def test_no_snapshot_near_24h_ago_is_none(self):
        """Snapshots only a few hours old cannot support a '24h'
        change — the page shows '—' instead of a % measured over
        the wrong window."""
        now = datetime.now(timezone.utc)
        self._snap(now - timedelta(hours=5), 300.0)
        self._snap(now - timedelta(hours=1), 2.48)
        self.assertIsNone(change_24h(self.db, self.user_id, 640.0))

    def test_zero_baseline_is_none(self):
        now = datetime.now(timezone.utc)
        self._snap(now - timedelta(hours=23, minutes=59), 0.0)
        self.assertIsNone(change_24h(self.db, self.user_id, 640.0))

    def test_no_current_total_is_none(self):
        self.assertIsNone(change_24h(self.db, self.user_id, None))


class TestZeroPriceIsMissing(AccuracyBase):
    def test_holding_at_zero_price_values_at_cost(self):
        """A $0 quote is not a price: value at cost, P&L '—'."""
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, 'SOL', 10.0, 5.0)",
            (self.user_id,))
        self.db.commit()
        rows = current_holdings(self.db, self.user_id, {"SOL": 0.0})
        self.assertEqual(rows[0]["value"], 50.0)
        self.assertIsNone(rows[0]["pnl"])
        self.assertIsNone(rows[0]["price"])
        rows = current_holdings(self.db, self.user_id, {"SOL": 6.0})
        self.assertEqual(rows[0]["value"], 60.0)
        self.assertEqual(rows[0]["pnl"], 10.0)

    def test_bankroll_values_unpriced_holding_at_cost(self):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.50)",
            (self.user_id, GATE))
        self.db.commit()
        self.assertEqual(holdings_value(self.db, self.user_id, {}), 50.0)
        self.assertEqual(
            holdings_value(self.db, self.user_id, {GATE: 2.0}), 200.0)


class TestCoinRowsFeedFirst(AccuracyBase):
    def _prices(self):
        return {"prices": {"SOL": {"price": 80.0, "change_24h": 0.0}},
                "source": "live", "as_of": "test"}

    def _hold_gate(self, amount=100.0, avg=0.01):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, GATE, amount, avg))
        self.db.commit()

    def _no_dex(self):
        return (
            mock.patch.object(routes, "fetch_prices_for_mints",
                              return_value={}),
            mock.patch.object(routes, "fetch_mint_identities",
                              return_value={}),
            mock.patch.object(routes, "fetch_mint_1h_changes",
                              return_value={}),
        )

    def test_mint_priced_from_feed_when_dexscreener_has_nothing(self):
        """Gate has no DEX pool: the feed's curve tick is the only
        live price, and the row uses it even when DexScreener
        returns nothing."""
        self._hold_gate()
        self.feed.ingest(GATE, 0.05, "curve")
        p1, p2, p3 = self._no_dex()
        with p1, p2, p3:
            rows = _coin_rows(self.db, self.user_id, "paper",
                              self._prices(), feed=self.feed)
        row = next(r for r in rows if r["coin_id"] == GATE)
        self.assertEqual(row["price"], 0.05)
        self.assertEqual(row["value"], 5.0)
        self.assertAlmostEqual(row["pnl"], 4.0)

    def test_stale_feed_tick_is_not_a_live_price(self):
        self._hold_gate()
        self.feed.ingest(GATE, 0.05, "curve", now=time.time() - 120)
        p1, p2, p3 = self._no_dex()
        with p1, p2, p3:
            rows = _coin_rows(self.db, self.user_id, "paper",
                              self._prices(), feed=self.feed)
        row = next(r for r in rows if r["coin_id"] == GATE)
        self.assertIsNone(row["price"])
        self.assertIsNone(row["value"])
        self.assertIsNone(row["pnl"])


class TestRealAvgPrice(AccuracyBase):
    def test_executed_cost_from_live_sol(self):
        """Cost = SOL actually spent x live SOL / tokens received —
        never the mint's current price."""
        self.feed.ingest("SOL", 80.0, "kraken")
        self.assertAlmostEqual(
            _real_avg_price(self.app, GATE, 0.13, 1000.0), 0.0104,
            places=4)

    def test_snapshot_sol_price_is_never_used(self):
        self.feed.ingest("SOL", 80.0, "kraken", now=time.time() - 120)
        with mock.patch.object(
                routes, "_paper_price",
                return_value=(86.14, "snapshot", "2026-11-09T10:10:11Z")):
            self.assertEqual(
                _real_avg_price(self.app, GATE, 0.13, 1000.0), 0.0)

    def test_live_rest_sol_price_used_without_feed_tick(self):
        self.feed.ingest("SOL", 80.0, "kraken", now=time.time() - 120)
        with mock.patch.object(
                routes, "_paper_price",
                return_value=(80.0, "live", "test")):
            self.assertAlmostEqual(
                _real_avg_price(self.app, GATE, 0.13, 1000.0), 0.0104,
                places=4)

    def test_zero_amounts_are_unknown(self):
        self.feed.ingest("SOL", 80.0, "kraken")
        self.assertEqual(_real_avg_price(self.app, GATE, 0.0, 1000.0),
                         0.0)
        self.assertEqual(_real_avg_price(self.app, GATE, 0.13, 0.0), 0.0)


class TestHomeAccuracy(AccuracyBase):
    def test_unpriced_holding_counts_at_cost_and_label_shows(self):
        """Home total values an unpriced mint at cost (not $0) and
        the page says when prices are a snapshot, never live."""
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.50)",
            (self.user_id, GATE))
        self.db.commit()
        prices = {"prices": {"SOL": {"price": 80.0, "change_24h": 0.0}},
                  "source": "snapshot", "as_of": "2026-11-09T10:10:11Z"}
        with mock.patch.object(routes, "get_prices",
                              return_value=prices), \
                mock.patch.object(routes, "fetch_prices_for_mints",
                                  return_value={}), \
                mock.patch.object(routes, "fetch_mint_identities",
                                  return_value={}), \
                mock.patch.object(routes, "fetch_mint_1h_changes",
                                  return_value={}):
            resp = self.client.get("/wallet")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # $100 paper cash + 100 Gate at $0.50 cost = $150.00.
        self.assertIn("$150.00 CAD", html)
        self.assertIn(
            "Prices: snapshot (snapshot from 2026-11-09T10:10:11Z, "
            "not live)", html)


if __name__ == "__main__":
    unittest.main()
