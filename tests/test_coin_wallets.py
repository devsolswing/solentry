"""Per-coin wallets: the first-trade "Create wallet" step.

The button shows exactly while no row exists for (user, coin);
after the tap the row exists and the page shows the green-dot
indicator with the REAL address (first 4 + last 4). SOL is the
exception: it is the native coin of the app wallet itself, so
the step is already satisfied for SOL (the indicator shows the
app wallet immediately, no tap). A coin with no supported wallet
refuses plainly instead of faking one.
"""

import tempfile
import unittest
from unittest import mock

from tradingbot import coin_wallets as cw
from tradingbot import jupiter as jup
from helpers import make_client, signup

# A pump.fun mint address (valid base58, 44 chars) — a Solana
# asset, so it has wallet support.
MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"


class TestTruncate(unittest.TestCase):
    def test_first4_last4(self):
        self.assertEqual(
            cw.truncate_pubkey("Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78"),
            "Ab12…Qr78")

    def test_short_value_returned_as_is(self):
        self.assertEqual(cw.truncate_pubkey("Ab12"), "Ab12")

    def test_empty_is_empty(self):
        self.assertEqual(cw.truncate_pubkey(""), "")
        self.assertEqual(cw.truncate_pubkey(None), "")


class CoinWalletBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.pubkey = self.db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]

    def tearDown(self):
        self.tmp.cleanup()


class TestSupport(CoinWalletBase):
    def test_sol_supported(self):
        support = cw.wallet_support_for_coin("SOL")
        self.assertTrue(support["supported"])
        self.assertEqual(support["network"], "Solana")

    def test_wrapped_coins_supported_without_network(self):
        # The four verified wrapped coins resolve from the static
        # map — no token-API call.
        for symbol in ("BTC", "ETH", "XRP", "ZEC"):
            with self.subTest(symbol=symbol):
                support = cw.wallet_support_for_coin(symbol)
                self.assertTrue(support["supported"])
                self.assertEqual(support["network"], "Solana")

    def test_pumpfun_mint_supported(self):
        support = cw.wallet_support_for_coin(MINT)
        self.assertTrue(support["supported"])
        self.assertEqual(support["network"], "Solana")

    def test_coin_with_no_route_unsupported(self):
        # ADA has no Solana token: the token API finds nothing,
        # so there is no wallet for it — and the page must say
        # so plainly instead of faking one.
        with mock.patch.object(jup, "resolve_mint",
                               return_value=None):
            support = cw.wallet_support_for_coin(
                "ADA", coin_name="Cardano")
        self.assertFalse(support["supported"])
        self.assertEqual(support["reason"], cw.WALLET_NOT_AVAILABLE)


class TestRecord(CoinWalletBase):
    def test_create_records_row_and_truncated_is_real(self):
        result = cw.create_coin_wallet(
            self.db, self.user_id, "BTC", self.pubkey)
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(result["wallet_pubkey"], self.pubkey)
        self.assertEqual(result["truncated"],
                         cw.truncate_pubkey(self.pubkey))
        row = cw.get_coin_wallet(self.db, self.user_id, "BTC")
        self.assertIsNotNone(row)
        self.assertEqual(row["wallet_pubkey"], self.pubkey)
        self.assertEqual(row["network"], "Solana")

    def test_second_create_returns_same_row(self):
        first = cw.create_coin_wallet(
            self.db, self.user_id, "BTC", self.pubkey)
        second = cw.create_coin_wallet(
            self.db, self.user_id, "BTC", self.pubkey)
        self.assertTrue(second["ok"])
        self.assertFalse(second["created"])
        self.assertEqual(second["wallet_pubkey"],
                         first["wallet_pubkey"])

    def test_button_never_reappears_after_first_trade(self):
        state_before = cw.coin_wallet_state(
            self.db, self.user_id, "BTC", self.pubkey)
        self.assertFalse(state_before["recorded"])
        cw.create_coin_wallet(
            self.db, self.user_id, "BTC", self.pubkey)
        state_after = cw.coin_wallet_state(
            self.db, self.user_id, "BTC", self.pubkey)
        self.assertTrue(state_after["recorded"])
        self.assertEqual(state_after["truncated"],
                         cw.truncate_pubkey(self.pubkey))

    def test_sol_already_recorded_no_tap_needed(self):
        # SOL is the native coin of the app wallet itself: the
        # step is already satisfied, with no coin_wallets row.
        self.assertIsNone(
            cw.get_coin_wallet(self.db, self.user_id, "SOL"))
        state = cw.coin_wallet_state(
            self.db, self.user_id, "SOL", self.pubkey)
        self.assertTrue(state["supported"])
        self.assertTrue(state["recorded"])
        self.assertEqual(state["truncated"],
                         cw.truncate_pubkey(self.pubkey))

    def test_create_refuses_without_app_wallet(self):
        result = cw.create_coin_wallet(
            self.db, self.user_id, "BTC", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], cw.NO_APP_WALLET)

    def test_create_refuses_unsupported_coin(self):
        with mock.patch.object(jup, "resolve_mint",
                               return_value=None):
            result = cw.create_coin_wallet(
                self.db, self.user_id, "ADA", self.pubkey,
                coin_name="Cardano")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], cw.WALLET_NOT_AVAILABLE)

    def test_state_unsupported_coin(self):
        with mock.patch.object(jup, "resolve_mint",
                               return_value=None):
            state = cw.coin_wallet_state(
                self.db, self.user_id, "ADA", self.pubkey,
                coin_name="Cardano")
        self.assertFalse(state["supported"])
        self.assertFalse(state["recorded"])
        self.assertEqual(state["truncated"], "")


if __name__ == "__main__":
    unittest.main()
