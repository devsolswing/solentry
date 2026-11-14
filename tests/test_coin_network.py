"""Page 5 (coin detail) Send/Receive must account for the network.

Every Send and Receive on the coin detail page states the network
it will use (Solana, devnet) in plain words; a coin that is not a
Solana asset is refused plainly — no address shown, no send form —
and the server refuses a crafted POST for it too. The confirm step
states the network alongside destination, amount and token.

Major coins have no detail page any more (SOL, BTC, ETH, ZEC,
USDC, … all 404): the app trades pump.fun mints only, and SOL is
the unit of account, never a listed coin. Only a pump.fun mint
gets Send/Receive windows.

No SOL moves in these tests; the send RPC is a recording mock.
"""

import re
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.networks import (
    coin_network, network_label, send_refusal)

DEST = "11111111111111111111111111111111"
# A real, valid Solana address (decodes to 32 bytes).
MINT = "So11111111111111111111111111111111111111112"


class FakeRpc:
    instances = []

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return 0

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


def make_prices(changes):
    return {
        "prices": {c: {"price": p, "change_24h": ch}
                   for c, (p, ch) in changes.items()},
        "source": "live", "as_of": "test",
    }


class CoinNetworkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({"SOL": (10.0, 5.25)}))
        self.prices_patch.start()
        self.chart_patch = mock.patch.object(
            routes, "fetch_market_chart",
            return_value={"points": [], "error": "no chart history"})
        self.chart_patch.start()
        self.flow_patch = mock.patch.object(
            routes, "fetch_trade_flow", return_value=None)
        self.flow_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        # No live metadata lookups in these tests: the mint under
        # test has no stored name, so it renders "Unknown coin".
        self.meta_patch = mock.patch.object(
            routes, "ensure_coin_meta", return_value=None)
        self.meta_patch.start()
        signup(self.client, "alice")
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.assertEqual(resp.status_code, 302)
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.meta_patch.stop()
        self.trenches_patch.stop()
        self.flow_patch.stop()
        self.chart_patch.stop()
        self.prices_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _html(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)

    def _overlay(self, html, overlay_id):
        """The HTML inside one action overlay, for scoped assertions."""
        m = re.search(
            r'id="%s".*?<div class="coin-popup">(.*?)</div>\s*</div>'
            % re.escape(overlay_id), html, re.S)
        self.assertIsNotNone(m, f"overlay {overlay_id} missing")
        return m.group(1)

    def _intent_count(self):
        return self.db.execute(
            "SELECT COUNT(*) c FROM send_intents").fetchone()["c"]


class TestCoinNetworkUnit(unittest.TestCase):
    def test_label(self):
        self.assertEqual(network_label("devnet"), "Solana (devnet)")
        self.assertEqual(network_label("mainnet"), "Solana (mainnet)")

    def test_mint_is_solana(self):
        info = coin_network(MINT)
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertFalse(info["multi_chain"])
        self.assertTrue(info["known"])
        self.assertIsNone(send_refusal(MINT))

    def test_sol_is_native_solana(self):
        # SOL is the network's native currency: a SOL send IS a
        # Solana send, so there is no refusal.
        info = coin_network("SOL")
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertIsNone(send_refusal("SOL"))

    def test_major_symbol_is_not_a_coin(self):
        # A major symbol is not a mint address: it is not a coin in
        # this app at all, never guessed onto a chain.
        for symbol in ("BTC", "ETH", "ZEC", "USDC", "ADA"):
            info = coin_network(symbol)
            self.assertFalse(info["known"], symbol)
            self.assertFalse(info["is_solana"], symbol)
            refusal = send_refusal(symbol)
            self.assertIn("not a Solana token", refusal)
            self.assertIn("Solana (devnet)", refusal)
            self.assertIn("Nothing was sent", refusal)

    def test_unknown_coin_is_refused_never_guessed(self):
        info = coin_network("NOPE")
        self.assertFalse(info["known"])
        self.assertFalse(info["is_solana"])
        refusal = send_refusal("NOPE")
        self.assertIn("not a Solana token", refusal)
        self.assertIn("Nothing was sent", refusal)


class TestCoinDetailNetwork(CoinNetworkBase):
    def test_major_detail_pages_are_gone(self):
        # SOL, BTC, ETH, ZEC, USDC: no detail page any more. The
        # app trades pump.fun mints only; SOL is the unit of
        # account, never a listed coin.
        for coin_id in ("SOL", "BTC", "ETH", "ZEC", "USDC", "ADA"):
            resp = self.client.get(f"/coins/{coin_id}/detail")
            self.assertEqual(resp.status_code, 404, coin_id)

    def test_mint_send_window_shows_network_and_sends_token(self):
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.002}):
            html = self._html(f"/coins/{MINT}/detail")
        send = self._overlay(html, "coin-send-overlay")
        self.assertIn("Solana (devnet)", send)
        self.assertIn("Review send", send)
        # The send is the mint token now, not SOL — the window
        # says so, and the form posts the mint as the token.
        self.assertIn(f'name="token" value="{MINT}"', send)
        self.assertNotIn("SolEntry sends SOL only", send)

    def test_mint_receive_window_shows_network_and_address(self):
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.002}):
            html = self._html(f"/coins/{MINT}/detail")
        receive = self._overlay(html, "coin-receive-overlay")
        self.assertIn("Solana (devnet)", receive)
        self.assertIn("Only send on the Solana network", receive)
        self.assertIn("Copy address", receive)

    def test_stale_major_return_to_falls_back_to_wallet_send(self):
        # return_to=coin:BTC names a page that no longer exists, so
        # it is not a coin page any more and the send falls back to
        # the plain wallet send: the token here IS SOL (a Solana
        # send to a valid destination), so the send itself goes
        # ahead — there is no non-Solana coin left to refuse.
        before = self._intent_count()
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL",
            "return_to": "coin:BTC"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._intent_count(), before + 1)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(intent["status"], "pending")

    def test_sol_send_creates_intent_and_confirm_states_network(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL",
            "return_to": "coin:SOL"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(intent)
        self.assertEqual(intent["status"], "pending")
        html = self._html(
            f"/wallet/send/{intent['id']}/confirm?return_to=coin:SOL")
        # Confirm step: network alongside destination, amount, token.
        self.assertIn("Solana (devnet)", html)
        self.assertIn(DEST, html)
        self.assertIn("0.5000", html)
        self.assertIn("SOL", html)

    def test_plain_wallet_confirm_states_network_too(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL"})
        self.assertEqual(resp.status_code, 302)
        intent = self.db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        html = self._html(f"/wallet/send/{intent['id']}/confirm")
        self.assertIn("Solana (devnet)", html)


if __name__ == "__main__":
    unittest.main()
