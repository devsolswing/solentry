"""Trade-routes fix (2026-11-24): the sells and sends James tried
are valid transactions — the app's public RPC was the failure.

A. Trading RPC: mainnet + HELIUS_API_KEY set + no explicit RPC_URL
   -> the Helius mainnet RPC. An explicit RPC_URL always wins.
B. Sell screen honesty: tiny prices show 8 decimals (a $0.000002
   coin must not read $0.0000); the sell form prefills at most the
   holding's SOL value and caps the amount there, with the expected
   return shown before submitting.
C. Send rent: Token-2022 ATAs are 170 bytes (see test_token_send).
"""
from __future__ import annotations

import unittest

from test_coins_page import GATE, CoinsPageBase
from tradingbot import format_usd_price
from tradingbot.config import Config
from tradingbot.routes import _sell_prefill_for_row

HELIUS_URL = ("https://mainnet.helius-rpc.com/?api-key=test-key-123")
PUBLIC_MAINNET = "https://api.mainnet-beta.solana.com"
PUBLIC_DEVNET = "https://api.devnet.solana.com"


class TestHeliusRpcConfig(unittest.TestCase):
    def test_mainnet_with_helius_key_uses_helius_rpc(self):
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key-123"})
        self.assertEqual(cfg.rpc_url, HELIUS_URL)

    def test_explicit_rpc_url_wins_over_helius(self):
        cfg = Config(env={"NETWORK": "mainnet",
                          "HELIUS_API_KEY": "test-key-123",
                          "RPC_URL": "https://my-node.example"})
        self.assertEqual(cfg.rpc_url, "https://my-node.example")

    def test_mainnet_without_key_keeps_public_default(self):
        cfg = Config(env={"NETWORK": "mainnet"})
        self.assertEqual(cfg.rpc_url, PUBLIC_MAINNET)

    def test_devnet_with_key_keeps_devnet_default(self):
        cfg = Config(env={"NETWORK": "devnet",
                          "HELIUS_API_KEY": "test-key-123"})
        self.assertEqual(cfg.rpc_url, PUBLIC_DEVNET)


class TestUsdPriceFormat(unittest.TestCase):
    def test_tiny_price_shows_8_decimals(self):
        self.assertEqual(format_usd_price(0.00000201), "0.00000201")
        self.assertEqual(format_usd_price(0.0000005), "0.00000050")

    def test_normal_price_shows_4_decimals(self):
        self.assertEqual(format_usd_price(0.5), "0.5000")
        self.assertEqual(format_usd_price(71.5), "71.5000")

    def test_none_renders_empty(self):
        self.assertEqual(format_usd_price(None), "")


class TestSellPrefill(unittest.TestCase):
    def test_small_holding_prefills_its_own_value(self):
        # 1,000,000 GATE at $0.00000201, SOL $71.50: worth $2.01,
        # about 0.0281 SOL — the prefill is the value, not 0.1.
        held, prefill = _sell_prefill_for_row(
            {"held": 1_000_000, "price": 0.00000201}, 71.50)
        self.assertAlmostEqual(held, 0.028111, places=6)
        self.assertAlmostEqual(prefill, 0.028111, places=6)

    def test_big_holding_prefill_capped_at_0_1(self):
        held, prefill = _sell_prefill_for_row(
            {"held": 20_000_000, "price": 0.0000005}, 10.0)
        self.assertAlmostEqual(held, 1.0, places=6)
        self.assertEqual(prefill, 0.1)

    def test_no_holding_or_no_price_gives_no_prefill(self):
        self.assertEqual(
            _sell_prefill_for_row({"held": 0, "price": 0.000002}, 71.5),
            (None, None))
        self.assertEqual(
            _sell_prefill_for_row({"held": 1000, "price": None}, 71.5),
            (None, None))
        self.assertEqual(
            _sell_prefill_for_row({"held": 1000, "price": 0.001}, None),
            (None, None))


class TestSellScreen(CoinsPageBase):
    """The coin detail page + the coins-page popup, at the mocked
    Gate price $0.00000050 and SOL $10: 1,000,000 GATE is worth
    0.05 SOL."""

    def _seed_gate(self, amount):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, 0.0000005)",
            (self.user_id, GATE, amount))
        self.db.commit()

    def test_detail_shows_tiny_price_and_capped_prefill(self):
        self._choose_mode("paper")
        self._seed_gate(1_000_000)
        html = self._html(f"/coins/{GATE}/detail")
        self.assertIn("$0.00000050", html)
        self.assertNotIn("$0.0000<", html)
        self.assertIn("worth \u2248 0.0500 SOL", html)
        self.assertIn('value="0.05"', html)
        self.assertIn('max="0.05"', html)
        self.assertIn("\u2248 1,000,000 GATE", html)

    def test_detail_caps_prefill_at_0_1_for_big_holding(self):
        self._choose_mode("paper")
        self._buy_gate()  # $10 buys ~19,801,980 GATE (paper fill
        # costs ~1% a leg) = 0.990099 SOL at SOL $10
        html = self._html(f"/coins/{GATE}/detail")
        self.assertIn('value="0.1"', html)
        self.assertIn('max="0.990099"', html)
        self.assertIn("\u2248 2,000,000 GATE", html)

    def test_popup_sell_form_prefills_the_same(self):
        self._choose_mode("paper")
        self._seed_gate(1_000_000)
        html = self._html(f"/coins/{GATE}")
        self.assertIn("$0.00000050", html)
        self.assertIn('value="0.05"', html)
        self.assertIn('max="0.05"', html)


if __name__ == "__main__":
    unittest.main()
