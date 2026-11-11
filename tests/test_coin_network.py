"""Page 5 (coin detail) Send/Receive must account for the network.

Every Send and Receive on the coin detail page states the network it
will use (Solana, devnet) in plain words; a coin that is not a Solana
asset is refused plainly — no address shown, no send form — and the
server refuses a crafted POST for it too. The confirm step states
the network alongside destination, amount and token.

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
# Wrapped SOL mint: a real, valid Solana address (decodes to 32 bytes).
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
        "source": "snapshot", "as_of": "test",
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
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25), "USDC": (1.0, 0.0),
                "DOGE": (0.07, 3.0), "PEPE": (0.000005, -1.0),
                "BONK": (0.000003, 8.0), "JUP": (0.23, -0.5)}))
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
        signup(self.client, "alice")
        resp = self.client.post("/choose-mode", data={
            "csrf_token": csrf(self.client, "/wallet"), "mode": "paper"})
        self.assertEqual(resp.status_code, 302)
        self.db = self.app.config["DB"]

    def tearDown(self):
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

    def test_sol_is_solana(self):
        info = coin_network("SOL")
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertIsNone(send_refusal("SOL"))

    def test_mint_is_solana(self):
        info = coin_network("So11111111111111111111111111111111111111112")
        self.assertEqual(info["network"], "Solana")
        self.assertTrue(info["is_solana"])
        self.assertIsNone(
            send_refusal("So11111111111111111111111111111111111111112"))

    def test_bitcoin_is_not_solana(self):
        info = coin_network("BTC")
        self.assertEqual(info["network"], "Bitcoin")
        self.assertFalse(info["is_solana"])
        refusal = send_refusal("BTC")
        self.assertIn("Bitcoin", refusal)
        self.assertIn("Solana (devnet)", refusal)
        self.assertIn("Nothing was sent", refusal)

    def test_usdc_is_multi_chain(self):
        info = coin_network("USDC")
        self.assertTrue(info["multi_chain"])
        self.assertFalse(info["is_solana"])
        refusal = send_refusal("USDC")
        self.assertIn("many networks", refusal)
        self.assertIn("Solana (devnet)", refusal)

    def test_unknown_coin_is_refused_never_guessed(self):
        info = coin_network("NOPE")
        self.assertFalse(info["known"])
        self.assertFalse(info["is_solana"])
        self.assertIn("could not be determined", send_refusal("NOPE"))


class TestCoinDetailNetwork(CoinNetworkBase):
    def test_sol_send_window_shows_network_and_sends_sol(self):
        html = self._html("/coins/SOL/detail")
        send = self._overlay(html, "coin-send-overlay")
        self.assertIn("Solana (devnet)", send)
        self.assertIn("Review send", send)
        self.assertIn('name="return_to" value="coin:SOL"', send)

    def test_sol_receive_window_shows_network_and_address(self):
        html = self._html("/coins/SOL/detail")
        receive = self._overlay(html, "coin-receive-overlay")
        self.assertIn("Solana (devnet)", receive)
        self.assertIn("Only send on the Solana network", receive)
        self.assertIn("Copy address", receive)

    def test_btc_send_window_refuses_plainly_no_form(self):
        html = self._html("/coins/BTC/detail")
        send = self._overlay(html, "coin-send-overlay")
        self.assertIn("Can't send Bitcoin from here", send)
        self.assertIn("Bitcoin lives on the Bitcoin network", send)
        self.assertIn("Solana (devnet)", send)
        self.assertNotIn("Review send", send)
        self.assertNotIn("<form", send)

    def test_btc_receive_window_refuses_plainly_no_address(self):
        html = self._html("/coins/BTC/detail")
        receive = self._overlay(html, "coin-receive-overlay")
        self.assertIn("Can't receive Bitcoin here", receive)
        self.assertIn("Bitcoin lives on the Bitcoin network", receive)
        self.assertNotIn("Copy address", receive)
        self.assertNotIn('class="addr"', receive)

    def test_usdc_windows_refuse_multi_chain(self):
        html = self._html("/coins/USDC/detail")
        send = self._overlay(html, "coin-send-overlay")
        self.assertIn("many networks", send)
        self.assertNotIn("Review send", send)
        receive = self._overlay(html, "coin-receive-overlay")
        self.assertIn("many networks", receive)
        self.assertNotIn("Copy address", receive)

    def test_mint_windows_show_solana_network(self):
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.002}):
            html = self._html(f"/coins/{MINT}/detail")
        send = self._overlay(html, "coin-send-overlay")
        self.assertIn("Solana (devnet)", send)
        self.assertIn("Review send", send)
        # The send is the mint token now, not SOL — the window
        # says so, and the form posts the mint as the token.
        self.assertIn("this sends So11…", send)
        self.assertIn(f'name="token" value="{MINT}"', send)
        self.assertNotIn("SolEntry sends SOL only", send)

    def test_server_refuses_btc_send_post(self):
        before = self._intent_count()
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "SOL",
            "return_to": "coin:BTC"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bitcoin lives on the Bitcoin network", html)
        self.assertIn("Nothing was sent", html)
        self.assertEqual(self._intent_count(), before)

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
