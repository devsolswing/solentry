"""General SPL token sends: any token the app wallet holds, to any
Solana address — no per-token code.

The send transaction contains only the recipient's ATA creation
(when needed) plus the token transfer. The balance is read on-chain,
so a holding the app never recorded is still sendable (the Gate
regression: six buys landed on-chain but were recorded as failed,
so the app showed "You hold none of this coin" and could not send
it). No live send runs in these tests; the RPC is a fake.
"""

import base64
import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot.solana import (
    ASSOCIATED_TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    RpcError,
    SolanaRpcClient,
    base58_decode,
    build_create_associated_token_account_instruction,
    build_token_transfer_instruction,
    find_associated_token_address,
)
from tradingbot.token_send import (
    TokenSendRefusal,
    amount_to_base_units,
    build_token_send_instructions,
    format_token_amount,
    inspect_token,
    sign_token_send,
)

# Gate (the first real use) and its burn wallet: public devnet
# addresses, no keys. The fake RPC serves the on-chain state.
MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATEBURN = "EzhPuigYk8pU2Ci4HeA1WCaos8YTthcenCUQqszU8UxX"
DEST = GATEBURN
BALANCE = 550_340_693_467_150  # 550,340,693.46715 Gate, 6 decimals
DECIMALS = 6
RENT = 2_039_280
SOL_BALANCE = 665_216_462
FAKESIG = "fakesig1111111111111111111111111111111111111111"


class FakeTokenRpc:
    """Serves the on-chain state for one mint. get_token_account
    answers for the sender's ATA (the only token account the send
    flow reads); get_account_info answers for the mint and the
    destination's ATA (the only accounts it reads).

    The knobs are CLASS attributes: set them before a request to
    control the instance that request creates, and reset them in
    setUp so tests never leak state into each other."""
    instances = []
    mint_balance = BALANCE
    mint_decimals = DECIMALS
    sender_ata_exists = True
    mint_exists = True
    dest_ata_exists = False
    sol_balance = SOL_BALANCE
    confirm_result = True
    tx_status_result = "confirmed"
    # A preflight rejection to raise from send_transaction
    # (without recording a send), and a getTransaction receipt
    # to return — both None by default, so existing tests are
    # unaffected.
    send_error = None
    receipt = None

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeTokenRpc.instances.append(self)

    def get_account_info(self, pubkey):
        if pubkey == MINT:
            if not self.mint_exists:
                return None
            return {"data": b"", "lamports": 0, "executable": False,
                    "owner": TOKEN_PROGRAM}
        # Anything else read here is the destination's ATA.
        if self.dest_ata_exists:
            return {"data": b"", "lamports": RENT, "executable": False,
                    "owner": TOKEN_PROGRAM}
        return None

    def get_token_account(self, pubkey):
        if not self.sender_ata_exists:
            return None
        return {"amount": self.mint_balance,
                "decimals": self.mint_decimals}

    def get_minimum_balance_for_rent_exemption(self, data_size):
        return RENT

    def get_balance_lamports(self, pubkey):
        return self.sol_balance

    def get_latest_blockhash(self):
        return "11111111111111111111111111111111"

    def send_transaction(self, signed_b64):
        if self.send_error is not None:
            raise self.send_error
        self.send_calls.append(signed_b64)
        return FAKESIG

    def confirm_transaction(self, signature, attempts=15, delay_seconds=2.0):
        return self.confirm_result

    def transaction_status(self, signature):
        return self.tx_status_result

    def get_transaction(self, signature):
        return self.receipt


class TestTokenInstructions(unittest.TestCase):
    def test_transfer_instruction_layout(self):
        ins = build_token_transfer_instruction(
            "Src11111111111111111111111111111111111111111",
            "Dst11111111111111111111111111111111111111111",
            "Auth1111111111111111111111111111111111111111",
            55034069346715)
        self.assertEqual(ins["program_id"], TOKEN_PROGRAM)
        # SPL Token transfer: discriminator 3 + amount u64 LE.
        self.assertEqual(ins["data"][0], 3)
        self.assertEqual(ins["data"][1:],
                         (55034069346715).to_bytes(8, "little"))
        accounts = ins["accounts"]
        self.assertEqual(len(accounts), 3)
        self.assertTrue(accounts[0]["is_writable"])   # source
        self.assertFalse(accounts[0]["is_signer"])
        self.assertTrue(accounts[1]["is_writable"])   # destination
        self.assertFalse(accounts[1]["is_signer"])
        self.assertTrue(accounts[2]["is_signer"])     # authority
        self.assertFalse(accounts[2]["is_writable"])

    def test_create_ata_instruction_layout(self):
        ins = build_create_associated_token_account_instruction(
            "Pay11111111111111111111111111111111111111111",
            "Ata11111111111111111111111111111111111111111",
            "Own11111111111111111111111111111111111111111",
            MINT)
        self.assertEqual(ins["program_id"], ASSOCIATED_TOKEN_PROGRAM)
        self.assertEqual(ins["data"], b"")
        accounts = ins["accounts"]
        self.assertEqual(len(accounts), 7)
        # Funding account signs and pays; the ATA is writable.
        self.assertTrue(accounts[0]["is_signer"])
        self.assertTrue(accounts[0]["is_writable"])
        self.assertTrue(accounts[1]["is_writable"])
        self.assertFalse(accounts[1]["is_signer"])
        # Owner, mint, system program, token program, rent sysvar
        # are all readonly.
        for acct in accounts[2:]:
            self.assertFalse(acct["is_signer"])
            self.assertFalse(acct["is_writable"])
        self.assertEqual(accounts[3]["pubkey"], MINT)
        self.assertEqual(accounts[5]["pubkey"], TOKEN_PROGRAM)

    def test_create_ata_uses_the_mints_token_program(self):
        # A Token-2022 mint's ATA is created under the Token-2022
        # program, not the legacy one.
        ins = build_create_associated_token_account_instruction(
            "Pay11111111111111111111111111111111111111111",
            "Ata11111111111111111111111111111111111111111",
            "Own11111111111111111111111111111111111111111",
            MINT, TOKEN_2022_PROGRAM)
        self.assertEqual(ins["accounts"][5]["pubkey"], TOKEN_2022_PROGRAM)

    def test_amount_conversion_is_exact(self):
        self.assertEqual(amount_to_base_units("55034069.346715", 6),
                         55034069346715)
        self.assertEqual(amount_to_base_units("100", 6), 100_000_000)
        self.assertEqual(amount_to_base_units("0.000001", 6), 1)
        with self.assertRaises(TokenSendRefusal):
            amount_to_base_units("0.0000001", 6)  # 7 decimals > 6
        with self.assertRaises(TokenSendRefusal):
            amount_to_base_units("0", 6)
        with self.assertRaises(TokenSendRefusal):
            amount_to_base_units("not-a-number", 6)

    def test_format_token_amount(self):
        self.assertEqual(format_token_amount(55034069346715, 6),
                         "55034069.346715")
        self.assertEqual(format_token_amount(100_000_000, 6), "100")
        self.assertEqual(format_token_amount(500_000, 6), "0.5")

    def test_sign_token_send_refuses_fee_payer_mismatch(self):
        with self.assertRaises(ValueError):
            sign_token_send(b"\x01" * 32,
                            "11111111111111111111111111111111", [],
                            "11111111111111111111111111111111")


class TestInspectToken(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeTokenRpc("https://example.invalid")
        self.wallet = "4zg7rBoW6XV1o6VtFbqQD686pwgmRtpKHB7Ce1bQ7ze8"

    def test_fresh_destination_needs_ata_and_rent(self):
        plan = inspect_token(self.rpc, self.wallet, MINT, DEST)
        self.assertEqual(plan["token_program"], TOKEN_PROGRAM)
        self.assertTrue(plan["create_ata"])
        self.assertEqual(plan["rent_lamports"], RENT)
        self.assertEqual(plan["balance"], BALANCE)
        self.assertEqual(plan["decimals"], DECIMALS)
        self.assertEqual(
            plan["dest_ata"],
            find_associated_token_address(DEST, MINT, TOKEN_PROGRAM))
        self.assertEqual(
            plan["sender_ata"],
            find_associated_token_address(self.wallet, MINT,
                                          TOKEN_PROGRAM))

    def test_existing_destination_ata_needs_no_create(self):
        self.rpc.dest_ata_exists = True
        plan = inspect_token(self.rpc, self.wallet, MINT, DEST)
        self.assertFalse(plan["create_ata"])
        self.assertEqual(plan["rent_lamports"], 0)

    def test_invalid_addresses_refused(self):
        with self.assertRaises(TokenSendRefusal):
            inspect_token(self.rpc, self.wallet, "not-a-mint", DEST)
        with self.assertRaises(TokenSendRefusal):
            inspect_token(self.rpc, self.wallet, MINT, "not-an-address")

    def test_no_holding_refused(self):
        self.rpc.sender_ata_exists = False
        with self.assertRaises(TokenSendRefusal) as ctx:
            inspect_token(self.rpc, self.wallet, MINT, DEST)
        self.assertIn("hold none", str(ctx.exception))

    def test_missing_mint_refused(self):
        self.rpc.mint_exists = False
        with self.assertRaises(TokenSendRefusal) as ctx:
            inspect_token(self.rpc, self.wallet, MINT, DEST)
        self.assertIn("could not be found", str(ctx.exception))

    def test_build_instructions_only_ata_plus_transfer(self):
        plan = inspect_token(self.rpc, self.wallet, MINT, DEST)
        instructions = build_token_send_instructions(plan, 1000)
        self.assertEqual(len(instructions), 2)
        self.assertEqual(instructions[0]["program_id"],
                         ASSOCIATED_TOKEN_PROGRAM)
        self.assertEqual(instructions[1]["program_id"], TOKEN_PROGRAM)
        self.assertEqual(instructions[1]["data"][1:],
                         (1000).to_bytes(8, "little"))
        # No System-program transfer anywhere in the send.
        self.assertNotIn(
            "11111111111111111111111111111111",
            [i["program_id"] for i in instructions])

    def test_build_instructions_existing_ata_is_transfer_only(self):
        self.rpc.dest_ata_exists = True
        plan = inspect_token(self.rpc, self.wallet, MINT, DEST)
        instructions = build_token_send_instructions(plan, 1000)
        self.assertEqual(len(instructions), 1)
        self.assertEqual(instructions[0]["program_id"], TOKEN_PROGRAM)

    def test_build_instructions_over_balance_refused(self):
        plan = inspect_token(self.rpc, self.wallet, MINT, DEST)
        with self.assertRaises(TokenSendRefusal):
            build_token_send_instructions(plan, BALANCE + 1)


class TestTokenSendFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeTokenRpc)
        self.rpc_patch.start()
        FakeTokenRpc.instances.clear()
        # Reset the fake's class-level knobs — a previous test's
        # settings must never leak into this one.
        FakeTokenRpc.mint_balance = BALANCE
        FakeTokenRpc.mint_decimals = DECIMALS
        FakeTokenRpc.sender_ata_exists = True
        FakeTokenRpc.mint_exists = True
        FakeTokenRpc.dest_ata_exists = False
        FakeTokenRpc.sol_balance = SOL_BALANCE
        FakeTokenRpc.confirm_result = True
        FakeTokenRpc.tx_status_result = "confirmed"
        FakeTokenRpc.send_error = None
        FakeTokenRpc.receipt = None
        self.ticker_patch = mock.patch.object(
            routes, "fetch_mint_identities",
            return_value={MINT: {"name": "Gate", "symbol": "GATE"}})
        self.ticker_patch.start()
        signup(self.client, "alice")

    def tearDown(self):
        self.ticker_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _post_token_send(self, amount="55034069.346715", dest=DEST,
                         mint=MINT):
        return self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": dest, "amount": amount, "token": mint})

    def _intent(self):
        db = self.app.config["DB"]
        return db.execute(
            "SELECT * FROM send_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def test_token_send_creates_intent_with_mint_and_base_units(self):
        # The app never recorded this holding (no real_holdings
        # row) — the send still works, because the balance is read
        # on-chain. That is the Gate regression.
        db = self.app.config["DB"]
        self.assertEqual(db.execute(
            "SELECT COUNT(*) c FROM real_holdings").fetchone()["c"], 0)
        resp = self._post_token_send()
        self.assertEqual(resp.status_code, 302)
        intent = self._intent()
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["mint"], MINT)
        self.assertEqual(intent["amount_base_units"], 55034069346715)
        self.assertEqual(intent["decimals"], DECIMALS)
        self.assertEqual(intent["token"], "GATE")
        self.assertEqual(intent["amount_lamports"], 0)
        self.assertIsNone(intent["signature"])
        self.assertEqual(
            sum(len(i.send_calls) for i in FakeTokenRpc.instances), 0)

    def test_confirm_page_shows_token_amount_destination_and_fee(self):
        self._post_token_send()
        intent = self._intent()
        html = self.client.get(
            f"/wallet/send/{intent['id']}/confirm").get_data(as_text=True)
        self.assertIn(DEST, html)
        self.assertIn("55034069.346715", html)
        self.assertIn("GATE", html)
        self.assertIn("Solana (devnet)", html)
        # Fresh destination: the fee line includes the ATA rent.
        self.assertIn("token account rent", html)
        self.assertNotIn("55034069.346715 SOL", html)

    def test_confirm_sends_token_once_and_records_signature(self):
        self._post_token_send()
        intent = self._intent()
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
        all_calls = [c for i in FakeTokenRpc.instances
                     for c in i.send_calls]
        self.assertEqual(len(all_calls), 1)
        raw = base64.b64decode(all_calls[0])
        # Both programs are in the transaction: the ATA program
        # (create) and the Token program (transfer), plus the exact
        # transfer amount in base units.
        self.assertIn(base58_decode(ASSOCIATED_TOKEN_PROGRAM), raw)
        self.assertIn(base58_decode(TOKEN_PROGRAM), raw)
        self.assertIn((55034069346715).to_bytes(8, "little"), raw)
        # Re-confirming does not send again.
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        all_calls = [c for i in FakeTokenRpc.instances
                     for c in i.send_calls]
        self.assertEqual(len(all_calls), 1)

    def test_confirm_to_existing_ata_sends_transfer_only(self):
        FakeTokenRpc.dest_ata_exists = True
        self._post_token_send()
        intent = self._intent()
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        all_calls = [c for i in FakeTokenRpc.instances
                     for c in i.send_calls]
        self.assertEqual(len(all_calls), 1)
        raw = base64.b64decode(all_calls[0])
        self.assertIn(base58_decode(TOKEN_PROGRAM), raw)
        self.assertNotIn(base58_decode(ASSOCIATED_TOKEN_PROGRAM), raw)

    def test_invalid_destination_and_over_balance_refused(self):
        resp = self._post_token_send(dest="not-an-address")
        self.assertEqual(resp.status_code, 302)
        resp = self._post_token_send(amount="999999999.0")
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)

    def test_non_mint_token_still_rejected(self):
        resp = self.client.post("/wallet/send", data={
            "csrf_token": csrf(self.client, "/wallet/send"),
            "destination": DEST, "amount": "0.5", "token": "USDC"})
        self.assertEqual(resp.status_code, 302)
        db = self.app.config["DB"]
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM send_intents"
                                    ).fetchone()["c"], 0)

    def test_balance_drop_before_confirm_marks_intent_failed(self):
        self._post_token_send()
        intent = self._intent()
        FakeTokenRpc.mint_balance = 1000  # balance fell after creation
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["signature"])
        self.assertEqual(
            sum(len(i.send_calls) for i in FakeTokenRpc.instances), 0)

    def test_slow_confirmation_recheck_reports_landed_send(self):
        # The confirmation fix: a send the first checks miss is
        # re-checked on-chain before it is called a failure.
        FakeTokenRpc.confirm_result = False
        FakeTokenRpc.tx_status_result = "confirmed"
        self._post_token_send()
        intent = self._intent()
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertTrue(row["signature"].startswith("fakesig"))

    def test_receipt_fallback_reports_landed_send(self):
        # The status cache has dropped the signature entirely
        # (its window is only ~300 slots), but the receipt says
        # the transaction landed: the send is reported as sent,
        # not failed. This is the live Gate send: the app said
        # "transaction not confirmed" for a transaction that had
        # landed, because the status check itself was broken.
        FakeTokenRpc.confirm_result = False
        FakeTokenRpc.tx_status_result = None
        FakeTokenRpc.receipt = {"slot": 1, "meta": {"err": None}}
        self._post_token_send()
        intent = self._intent()
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertTrue(row["signature"].startswith("fakesig"))

    def test_receipt_showing_failure_reports_failed_send(self):
        # The status cache is empty and the receipt says the
        # transaction failed on-chain: the send is failed, and
        # no signature is recorded.
        FakeTokenRpc.confirm_result = False
        FakeTokenRpc.tx_status_result = None
        FakeTokenRpc.receipt = {"slot": 1, "meta": {"err":
                                                    {"InstructionError":
                                                     [0, "Custom"]}}}
        self._post_token_send()
        intent = self._intent()
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["signature"])
        self.assertIn("not confirmed", row["error"])

    def test_send_preflight_rejection_reports_rejected(self):
        # The node refuses the transaction in preflight: a real
        # failure, reported with the node's own error — and the
        # chain is not re-checked, because a rejected transaction
        # cannot have landed. Nothing is sent.
        FakeTokenRpc.send_error = RpcError(
            "rpc error response: sendTransaction: -32002 "
            "Transaction simulation failed: Blockhash not found")
        self._post_token_send()
        intent = self._intent()
        self.client.post(
            f"/wallet/send/{intent['id']}/confirm",
            data={"csrf_token": csrf(
                self.client, f"/wallet/send/{intent['id']}/confirm")})
        db = self.app.config["DB"]
        row = db.execute("SELECT * FROM send_intents WHERE id = ?",
                         (intent["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["signature"])
        self.assertIn("transaction rejected", row["error"])
        self.assertIn("simulation failed", row["error"])
        self.assertEqual(
            sum(len(i.send_calls) for i in FakeTokenRpc.instances), 0)

    def test_coin_page_send_window_offers_token_send(self):
        with mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 0.002}), mock.patch.object(
                routes, "_trenches_list",
                return_value=([{"mint": MINT, "name": "Gate",
                                "symbol": "GATE", "price_usd": 0.002,
                                "change_1h_pct": 0.0}], None)):
            html = self.client.get(f"/coins/{MINT}/detail").get_data(
                as_text=True)
        self.assertIn("Solana (devnet)", html)
        # The mint's send form posts the mint as the token, with
        # the amount in the token's units — not SOL.
        self.assertIn(f'name="token" value="{MINT}"', html)
        self.assertIn("Amount (GATE)", html)
        self.assertIn("this sends GATE", html)
        self.assertNotIn("SolEntry sends SOL only", html)

    def test_sol_coin_page_send_window_unchanged(self):
        html = self.client.get("/coins/SOL/detail").get_data(as_text=True)
        self.assertIn('name="token" value="SOL"', html)
        self.assertIn("Amount (SOL)", html)
        self.assertIn("Review send", html)


class TestRealStatusShape(unittest.TestCase):
    """The real RPC client's status check, against a real-shaped
    getSignatureStatuses response. The live 10% Gate send was
    reported as "transaction not confirmed" for a transaction
    that had landed, because transaction_status read result[0]
    on the {"context": ..., "value": [...]} shape — a KeyError
    on EVERY check, so no landed transaction was ever seen as
    confirmed. The fakes return a bare status string and never
    exercised the real shape, which is why the suite stayed
    green while the live path was broken."""

    def _client_with_result(self, result):
        def fake_post(url, payload):
            return {"jsonrpc": "2.0", "id": 1, "result": result}
        return SolanaRpcClient("https://example.invalid",
                               http_post=fake_post)

    def _status(self, confirmation_status, err=None):
        return {"slot": 1, "confirmations": 5, "err": err,
                "confirmationStatus": confirmation_status}

    def test_confirmed_reads_from_value_list(self):
        client = self._client_with_result(
            {"context": {"slot": 1},
             "value": [self._status("confirmed")]})
        self.assertEqual(client.transaction_status("sig"), "confirmed")

    def test_finalized_reads_from_value_list(self):
        client = self._client_with_result(
            {"context": {"slot": 1},
             "value": [self._status("finalized")]})
        self.assertEqual(client.transaction_status("sig"), "confirmed")

    def test_failed_reads_from_value_list(self):
        client = self._client_with_result(
            {"context": {"slot": 1},
             "value": [self._status(
                 "finalized",
                 err={"InstructionError": [0, "Custom"]})]})
        self.assertEqual(client.transaction_status("sig"), "failed")

    def test_null_entry_is_unknown_not_failed(self):
        # The signature has aged out of the status cache (its
        # window is only ~300 slots): unknown, never a failure.
        client = self._client_with_result(
            {"context": {"slot": 1}, "value": [None]})
        self.assertIsNone(client.transaction_status("sig"))

    def test_bare_list_tolerated_for_fakes(self):
        client = self._client_with_result([self._status("confirmed")])
        self.assertEqual(client.transaction_status("sig"), "confirmed")

    def test_rpc_error_is_unknown_not_failed(self):
        def failing_post(url, payload):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32603, "message": "oops"}}
        client = SolanaRpcClient("https://example.invalid",
                                 http_post=failing_post)
        self.assertIsNone(client.transaction_status("sig"))


if __name__ == "__main__":
    unittest.main()
