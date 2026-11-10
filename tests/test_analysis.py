"""Losing-buy analysis: FIFO pairing of paper trades, exit reasons,
bot attribution, buy-time signals, and the losing-vs-winning
summary. Only recorded data is used — anything never recorded shows
as unavailable, never guessed."""

import json
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import login, make_client, signup
from tradingbot.analysis import (analyze_paper_trades, normalize_exit_reason,
                                 parse_time)


class TestNormalizeExitReason(unittest.TestCase):
    def test_each_category(self):
        self.assertEqual(normalize_exit_reason("stop_loss"), "stop loss")
        self.assertEqual(normalize_exit_reason("stop loss"), "stop loss")
        self.assertEqual(normalize_exit_reason("trailing stop"),
                         "trailing stop")
        self.assertEqual(normalize_exit_reason(
            "rug sell: price fell 16.2% in 60s"), "rug sell")
        self.assertEqual(normalize_exit_reason(
            "sell ladder: 25% at +3% profit"), "sell ladder")
        self.assertEqual(normalize_exit_reason(
            "sell ladder (trending): 10% at +26% profit"), "sell ladder")
        self.assertEqual(normalize_exit_reason(
            "top-10 wallets dumped: holdings down 11.0%"),
            "top-10 dump")

    def test_other_reasons_pass_through(self):
        self.assertEqual(normalize_exit_reason("manual sell"),
                         "manual sell")
        self.assertEqual(normalize_exit_reason("sell signal"),
                         "sell signal")

    def test_missing_is_unavailable(self):
        self.assertEqual(normalize_exit_reason(None), "unavailable")
        self.assertEqual(normalize_exit_reason(""), "unavailable")
        self.assertEqual(normalize_exit_reason("   "), "unavailable")


class TestParseTime(unittest.TestCase):
    def test_both_formats(self):
        self.assertIsNotNone(parse_time("2026-11-09T10:00:00Z"))
        self.assertIsNotNone(parse_time("2026-11-09 10:00:00"))
        self.assertEqual(parse_time("2026-11-09T10:00:00Z"),
                         parse_time("2026-11-09 10:00:00"))

    def test_bad_values_are_none(self):
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time("not a time"))


class AnalysisBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        fake = self.rpc_patch.start()
        fake.return_value.get_balance_lamports.return_value = 0
        signup(self.client, "alice")
        login(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _trade(self, coin, side, amount, price, when, bot_id=None,
               m5=None, volume=None, buys=None, sells=None,
               regime=None, exit_reason=None):
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, amount, "
            "price, total_usd, created_at, bot_id, buy_m5_pct, "
            "buy_volume_5m_usd, buy_buys_5m, buy_sells_5m, buy_regime, "
            "exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (self.user_id, coin, side, amount, price, amount * price,
             when, bot_id, m5, volume, buys, sells, regime, exit_reason))
        self.db.commit()

    def _activity(self, kind, detail, when):
        self.db.execute(
            "INSERT INTO activity (user_id, kind, detail, created_at, "
            "mode) VALUES (?, ?, ?, ?, 'paper')",
            (self.user_id, kind, detail, when))
        self.db.commit()

    def _insert_bot(self, name="Luna"):
        import hashlib
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash) VALUES (?, ?, 'bold', '[]', 20, 7, 'paper', "
            "0, 3, ?)",
            (self.user_id, name,
             hashlib.sha256(b"tok").hexdigest()))
        self.db.commit()
        return cur.lastrowid


class TestPairing(AnalysisBase):
    def test_fifo_pairs_split_sells_and_tracks_hold_time(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z",
                    m5=6.0, volume=5000, buys=40, sells=10,
                    regime="trending")
        self._trade("MINTA", "buy", 10, 2.00, "2026-11-09T10:05:00Z",
                    m5=7.0, volume=6000, buys=30, sells=5,
                    regime="trending")
        self._trade("MINTA", "sell", 15, 3.00, "2026-11-09T10:10:00Z",
                    exit_reason="sell ladder: 25% at +3% profit")
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(len(data["pairs"]), 2)
        # Newest first: the second buy's 5 units sold first in the
        # list order (pairs are sorted by sell time, then buy id —
        # same sell, so the later buy comes first).
        self.assertAlmostEqual(data["pairs"][0]["pnl_usd"], 5.0)   # 5 x (3-2)
        self.assertAlmostEqual(data["pairs"][1]["pnl_usd"], 20.0)  # 10 x (3-1)
        self.assertEqual(data["pairs"][1]["hold_seconds"], 600.0)
        self.assertEqual(data["pairs"][1]["exit_reason"], "sell ladder")
        self.assertAlmostEqual(data["pairs"][1]["m5_pct"], 6.0)
        self.assertAlmostEqual(data["pairs"][1]["volume_5m_usd"], 5000.0)
        self.assertEqual(data["pairs"][1]["regime"], "trending")
        # 5 units of the second buy are still holding.
        self.assertEqual(len(data["open"]), 1)
        self.assertEqual(data["open"][0]["coin"], "MINTA")
        self.assertAlmostEqual(data["open"][0]["amount"], 5.0)

    def test_exit_reason_from_leg_beats_activity(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 0.90, "2026-11-09T10:01:00Z",
                    exit_reason="stop_loss")
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(data["pairs"][0]["exit_reason"], "stop loss")

    def test_exit_reason_from_activity_for_old_sells(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 1.10, "2026-11-09T10:01:00Z")
        self._activity(
            "silent_sell",
            "Silent bot 'Luna' sold 10.0000 MINTA for $11.00 "
            "(trailing stop).",
            "2026-11-09T10:01:05Z")
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(data["pairs"][0]["exit_reason"], "trailing stop")

    def test_exit_reason_unavailable_with_no_record(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 1.10, "2026-11-09T10:01:00Z")
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(data["pairs"][0]["exit_reason"], "unavailable")

    def test_bot_from_leg_and_from_activity(self):
        bot_id = self._insert_bot("Luna")
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z",
                    bot_id=bot_id)
        self._trade("MINTA", "sell", 10, 1.10, "2026-11-09T10:01:00Z")
        self._trade("MINTB", "buy", 10, 1.00, "2026-11-09T10:02:00Z")
        self._trade("MINTB", "sell", 10, 1.10, "2026-11-09T10:03:00Z")
        self._activity(
            "silent_buy",
            "Silent bot 'Luna' bought 10.0000 MINTB for $10.00 (paper).",
            "2026-11-09T10:02:01Z")
        data = analyze_paper_trades(self.db, self.user_id)
        by_coin = {p["coin"]: p for p in data["pairs"]}
        self.assertEqual(by_coin["MINTA"]["bot"], "Luna")
        self.assertEqual(by_coin["MINTB"]["bot"], "Luna")

    def test_manual_buy_shows_manual(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 1.10, "2026-11-09T10:01:00Z")
        self._activity(
            "paper_buy", "Bought 10.0000 MINTA for $10.00 (paper, "
            "price source: snapshot).", "2026-11-09T10:00:02Z")
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(data["pairs"][0]["bot"], "manual")

    def test_signals_unavailable_for_old_trades(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 0.50, "2026-11-09T10:01:00Z")
        data = analyze_paper_trades(self.db, self.user_id)
        pair = data["pairs"][0]
        self.assertIsNone(pair["m5_pct"])
        self.assertIsNone(pair["volume_5m_usd"])
        self.assertIsNone(pair["buys_5m"])
        self.assertIsNone(pair["regime"])
        self.assertEqual(data["summary"]["losing"]["with_signals"], 0)


class TestSummary(AnalysisBase):
    def test_losing_vs_winning_summary(self):
        # Losing buy: weak signal, sellers in control, slow.
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z",
                    m5=4.0, volume=800, buys=5, sells=20, regime="slow")
        self._trade("MINTA", "sell", 10, 0.50, "2026-11-09T10:05:00Z",
                    exit_reason="stop_loss")
        # Winning buy: strong signal, buyers in control, trending.
        self._trade("MINTB", "buy", 10, 1.00, "2026-11-09T10:10:00Z",
                    m5=8.0, volume=9000, buys=50, sells=5,
                    regime="trending")
        self._trade("MINTB", "sell", 10, 2.00, "2026-11-09T10:15:00Z",
                    exit_reason="sell ladder: 25% at +3% profit")
        # Even buy: no gain, no loss.
        self._trade("MINTC", "buy", 10, 1.00, "2026-11-09T10:20:00Z")
        self._trade("MINTC", "sell", 10, 1.00, "2026-11-09T10:25:00Z")
        data = analyze_paper_trades(self.db, self.user_id)
        summary = data["summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["even"], 1)
        losing = summary["losing"]
        self.assertEqual(losing["count"], 1)
        self.assertAlmostEqual(losing["avg_pnl_usd"], -5.0)
        self.assertAlmostEqual(losing["avg_m5_pct"], 4.0)
        self.assertAlmostEqual(losing["avg_volume_5m_usd"], 800.0)
        self.assertEqual(losing["regime"], {"slow": 1, "trending": 0,
                                            "unavailable": 0})
        winning = summary["winning"]
        self.assertEqual(winning["count"], 1)
        self.assertAlmostEqual(winning["avg_pnl_usd"], 10.0)
        self.assertAlmostEqual(winning["avg_m5_pct"], 8.0)
        self.assertAlmostEqual(winning["avg_volume_5m_usd"], 9000.0)
        self.assertEqual(winning["regime"], {"slow": 0, "trending": 1,
                                             "unavailable": 0})

    def test_empty_history_summary_is_zeroes(self):
        data = analyze_paper_trades(self.db, self.user_id)
        self.assertEqual(data["pairs"], [])
        self.assertEqual(data["open"], [])
        self.assertEqual(data["summary"]["total"], 0)
        self.assertEqual(data["summary"]["losing"]["count"], 0)
        self.assertIsNone(data["summary"]["losing"]["avg_m5_pct"])


class TestAnalysisPage(AnalysisBase):
    def test_pnl_page_shows_analysis_section(self):
        self._trade("MINTA", "buy", 10, 1.00, "2026-11-09T10:00:00Z")
        self._trade("MINTA", "sell", 10, 0.50, "2026-11-09T10:05:00Z",
                    exit_reason="stop_loss")
        resp = self.client.get("/pnl")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Losing-buy analysis", html)
        self.assertIn("MINTA", html)
        self.assertIn("stop loss", html)
        self.assertIn("unavailable", html)
        self.assertIn("Losing buys (1)", html)
        self.assertIn("Winning buys (0)", html)

    def test_no_separate_analysis_route(self):
        # The analysis lives on /pnl as a section — there is no
        # separate page or route for it.
        resp = self.client.get("/pnl/analysis")
        self.assertEqual(resp.status_code, 404)
        html = self.client.get("/pnl").get_data(as_text=True)
        self.assertNotIn("/pnl/analysis", html)

    def test_pnl_requires_login(self):
        self.client.get("/logout")
        resp = self.client.get("/pnl")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
