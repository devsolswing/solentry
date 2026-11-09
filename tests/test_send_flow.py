"""Send flow: intent requires confirm; no-confirm means no send."""

import base64
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup

DEST = "11111111111111111111111111111111"


class FakeRpc:
    instances = []

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeRpc.instances.append(self)

    def get_balance_lamports(self, pubkey):
        return 0

    def get_recent_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        self.send_calls.append(signed_b64)
        return "fakesig1111111111111111111111111111111111111111"

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return True


class FailingRpc(FakeRpc):
    def get_recent_blockhash(self):
        raise RuntimeError("rpc down")


class TestSendFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        FakeRpc.instances.clear()
        signup(self.client, "alice")

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _create_intent(self, amount="0.5", dest=DEST):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": dest, "amount": amount, "token": "SOL"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        intent = db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1").fetchone()
        return intent

    def test_create_intent_does_not_send(self):
        intent = self._create_intent()
        self.assertEqual(intent["status"], "pending")
        self.assertIsNone(intent["signature"])
        self.assertEqual(sum(len(i.send_calls) for i in FakeRpc.instances), 0)

    def test_confirm_page_shows_destination_and_amount(self):
        intent = self._create_intent()
        html = self.client.get(
            f"/wallet/send/{intent['id']}/confirm").get_data(as_text=True)
        self.assertIn(DEST, html)
        self.assertIn("0.5000", html)

    def test_confirm_sends_once_and_records_signature(self):
        intent = self._create_intent()
        resp = self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertTrue(row["signature"].startswith("fakesig"))
        # Exactly one on-chain send, and its message pays the destination.
        all_calls = [c for i in FakeRpc.instances for c in i.send_calls]
        self.assertEqual(len(all_calls), 1)
        raw = base64.b64decode(all_calls[0])
        self.assertIn(bytes(32), raw)  # destination (all-zero) key present
        # Re-confirming does not send again.
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        all_calls = [c for i in FakeRpc.instances for c in i.send_calls]
        self.assertEqual(len(all_calls), 1)

    def test_rpc_failure_marks_intent_failed(self):
        with mock.patch.object(routes, "SolanaRpcClient", FailingRpc):
            intent = self._create_intent()
            self.client.post(
                f"/wallet/send/{intent['id']}/confirm",
                data={"csrf_token": csrf(
                    self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["signature"])

    def test_invalid_destination_rejected(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": "not-an-address", "amount": "0.5", "token": "SOL"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)

    def test_zero_amount_and_non_sol_rejected(self):
        for amount, token in (("0", "SOL"), ("-1", "SOL"), ("0.5", "USDC")):
            resp = self.client.post("/wallet/send", data={
                "csrf_token": csrf(self.client, "/wallet/send"),
                "destination": DEST, "amount": amount, "token": token})
            self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
