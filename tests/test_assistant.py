"""Assistant: parsing + the guarded execution paths (no direct sends)."""

import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.assistant import parse_message

DEST = "11111111111111111111111111111111"


class FakeRpc:
    def __init__(self, url, http_post=None, timeout=20):
        pass

    def get_balance_lamports(self, pubkey):
        return 500_000_000  # 0.5 SOL

    def get_latest_blockhash(self):
        raise AssertionError("assistant must never send on-chain")

    def send_transaction(self, signed_b64):
        raise AssertionError("assistant must never send on-chain")

    def confirm_transaction(self, signature, attempts=5, delay_seconds=1.0):
        return False


class TestAssistantParsing(unittest.TestCase):
    def test_balance(self):
        self.assertEqual(parse_message("what is my balance")["intent"],
                         "balance")
        self.assertEqual(parse_message("Balance?")["intent"], "balance")

    def test_bots_list(self):
        self.assertEqual(parse_message("show my bots")["intent"], "bots_list")

    def test_wallet_show(self):
        self.assertEqual(parse_message("show my wallet")["intent"],
                         "wallet_show")

    def test_create_bot(self):
        p = parse_message("create bot named Luna bold")
        self.assertEqual(p["intent"], "bot_create")
        self.assertEqual(p["bot_name"], "Luna")
        self.assertEqual(p["strategy"], "bold")

    def test_edit_bot(self):
        p = parse_message("edit bot Luna strategy to careful")
        self.assertEqual(p["intent"], "bot_edit")
        self.assertEqual(p["bot_name"], "Luna")
        self.assertEqual(p["fields"]["strategy"], "careful")
        p = parse_message("edit bot Luna buy size 25% stop loss 5%")
        self.assertEqual(p["fields"]["buy_size_pct"], 25.0)
        self.assertEqual(p["fields"]["stop_loss_pct"], 5.0)

    def test_delete_bot(self):
        p = parse_message("delete bot Luna")
        self.assertEqual(p["intent"], "bot_delete")
        self.assertEqual(p["bot_name"], "Luna")

    def test_send(self):
        p = parse_message(f"send 0.5 SOL to {DEST}")
        self.assertEqual(p["intent"], "send")
        self.assertEqual(p["amount"], "0.5")
        self.assertEqual(p["token"], "SOL")
        self.assertEqual(p["destination"], DEST)

    def test_paper_help(self):
        p = parse_message("how does paper trading work")
        self.assertEqual(p["intent"], "paper_help")
        self.assertIn("fake money", p["reply"])

    def test_unknown_lists_capabilities(self):
        p = parse_message("what is the weather on mars")
        self.assertEqual(p["intent"], "unknown")
        self.assertIn("what I can do", p["reply"])

    def test_confirm_yes(self):
        self.assertEqual(parse_message("yes")["intent"], "confirm_yes")


class TestAssistantHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient", FakeRpc)
        self.rpc_patch.start()
        signup(self.client, "alice")

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _ask(self, message):
        token = csrf(self.client, "/dashboard")
        resp = self.client.post("/api/assistant", json={
            "message": message, "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_assistant_reports_balance(self):
        data = self._ask("what is my balance")
        self.assertIn("0.5000 SOL", data["reply"])
        self.assertIn("$100.00 CAD", data["reply"])

    def test_assistant_create_and_list_bots(self):
        data = self._ask("create bot named Luna bold")
        self.assertIn("Created bot", data["reply"])
        data = self._ask("show my bots")
        self.assertIn("Luna", data["reply"])
        self.assertIn("bold", data["reply"])

    def test_assistant_edit_bot(self):
        self._ask("create bot named Luna")
        data = self._ask("edit bot Luna strategy to careful")
        self.assertIn("updated", data["reply"])
        db = self.app.config["DB"]
        bot = db.execute("SELECT strategy FROM bots WHERE name = 'Luna'"
                         ).fetchone()
        self.assertEqual(bot["strategy"], "careful")

    def test_assistant_delete_requires_yes(self):
        self._ask("create bot named Luna")
        data = self._ask("delete bot Luna")
        self.assertIn("Are you sure", data["reply"])
        db = self.app.config["DB"]
        self.assertIsNotNone(db.execute(
            "SELECT id FROM bots WHERE name = 'Luna'").fetchone())
        # "no" cancels; bot survives.
        data = self._ask("no")
        self.assertIsNotNone(db.execute(
            "SELECT id FROM bots WHERE name = 'Luna'").fetchone())
        # Ask again, then "yes" deletes.
        self._ask("delete bot Luna")
        data = self._ask("yes")
        self.assertIn("deleted", data["reply"])
        self.assertIsNone(db.execute(
            "SELECT id FROM bots WHERE name = 'Luna'").fetchone())

    def test_assistant_send_only_creates_intent(self):
        data = self._ask(f"send 0.5 SOL to {DEST}")
        self.assertIn("Nothing has moved", data["reply"])
        self.assertIn("confirm_url", data)
        db = self.app.config["DB"]
        intent = db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["amount_lamports"], 500_000_000)
        self.assertIsNone(intent["signature"])

    def test_assistant_rejects_bad_send(self):
        data = self._ask("send 0.5 SOL to not-an-address")
        self.assertNotIn("confirm_url", data)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
