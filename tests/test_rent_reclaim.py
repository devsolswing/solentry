"""SOL rent reclaim: close empty token accounts, burn $0.00 holdings
to the incinerator, rent back to the wallet — only after the user
confirms the exact list. Never costs fuel, never blocked by the
fuel gate. Paper mode is a read-only preview. No live send runs in
these tests; the RPC is a fake.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.routes as routes
from helpers import csrf, make_client, signup
from tradingbot import gate_fuel as gate_fuel_mod
from tradingbot.rent_reclaim import (
    INCINERATOR,
    MAX_ACCOUNTS_PER_TX,
    ReclaimRefusal,
    build_reclaim_transactions,
    plan_reclaim,
    sign_reclaim,
)
from tradingbot.solana import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    base58_encode,
    build_close_token_account_instruction,
    find_associated_token_address,
)

WALLET = "4zg7rBoW6XV1o6VtFbqQD686pwgmRtpKHB7Ce1bQ7ze8"
MINT_ZERO = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
MINT_VALUABLE = base58_encode(bytes([7]) * 32)
MINT_EMPTY = base58_encode(bytes([9]) * 32)
MINT_UNPRICED = base58_encode(bytes([11]) * 32)
RENT = 2_039_280
SOL_BALANCE = 1_000_000_000  # 1 SOL
FAKESIG = "fakesig1111111111111111111111111111111111111111"


def _ata(wallet, mint, program=TOKEN_PROGRAM):
    return find_associated_token_address(wallet, mint, program)


class FakeReclaimRpc:
    """Serves the on-chain state for reclaim. `accounts` is the
    wallet's token accounts: [{account, mint, amount, decimals,
    lamports, program}]. `incinerator_atas` is the set of
    incinerator ATAs that already exist. Class-level knobs, reset
    in setUp so tests never leak state into each other."""

    instances = []
    accounts = []
    incinerator_atas = set()
    sol_balance = SOL_BALANCE
    confirm_result = True
    send_error = None

    def __init__(self, url, http_post=None, timeout=20):
        self.url = url
        self.send_calls = []
        FakeReclaimRpc.instances.append(self)

    def _call(self, method, params):
        if method != "getTokenAccountsByOwner":
            raise AssertionError(f"unexpected _call {method}")
        program = params[1]["programId"]
        value = []
        for acct in self.accounts:
            if acct["program"] != program:
                continue
            value.append({
                "pubkey": acct["account"],
                "account": {
                    "lamports": acct["lamports"],
                    "data": {
                        "parsed": {
                            "info": {
                                "mint": acct["mint"],
                                "tokenAmount": {
                                    "amount": str(acct["amount"]),
                                    "decimals": acct["decimals"],
                                    "uiAmount": acct["amount"] / (
                                        10 ** acct["decimals"]),
                                },
                            },
                            "type": "account",
                        },
                        "program": "spl-token",
                        "space": 165,
                    },
                },
            })
        return {"value": value}

    def get_account_info(self, pubkey):
        if pubkey in self.incinerator_atas:
            return {"data": b"", "lamports": RENT, "executable": False,
                    "owner": TOKEN_PROGRAM}
        return None

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

    def confirm_transaction(self, signature, attempts=15,
                            delay_seconds=2.0):
        return self.confirm_result

    def transaction_status(self, signature):
        return "confirmed"

    def get_transaction(self, signature):
        return {"meta": {"err": None}}


def _empty_account(mint=MINT_EMPTY, program=TOKEN_PROGRAM):
    return {"account": _ata(WALLET, mint, program), "mint": mint,
            "amount": 0, "decimals": 6, "lamports": RENT,
            "program": program}


def _holding(mint, amount, decimals=6, program=TOKEN_PROGRAM):
    return {"account": _ata(WALLET, mint, program), "mint": mint,
            "amount": amount, "decimals": decimals,
            "lamports": RENT, "program": program}


class TestCloseInstruction(unittest.TestCase):
    def test_close_instruction_layout(self):
        ins = build_close_token_account_instruction(
            "Src11111111111111111111111111111111111111111",
            "Dst11111111111111111111111111111111111111111",
            "Auth1111111111111111111111111111111111111111")
        self.assertEqual(ins["program_id"], TOKEN_PROGRAM)
        # SPL Token closeAccount: discriminator 9, no amount.
        self.assertEqual(ins["data"], bytes([9]))
        accounts = ins["accounts"]
        self.assertEqual(len(accounts), 3)
        self.assertTrue(accounts[0]["is_writable"])   # account
        self.assertFalse(accounts[0]["is_signer"])
        self.assertTrue(accounts[1]["is_writable"])   # destination
        self.assertFalse(accounts[1]["is_signer"])
        self.assertTrue(accounts[2]["is_signer"])     # authority
        self.assertFalse(accounts[2]["is_writable"])

    def test_close_uses_the_mints_token_program(self):
        ins = build_close_token_account_instruction(
            "Src11111111111111111111111111111111111111111",
            "Dst11111111111111111111111111111111111111111",
            "Auth1111111111111111111111111111111111111111",
            TOKEN_2022_PROGRAM)
        self.assertEqual(ins["program_id"], TOKEN_2022_PROGRAM)


class TestPlanReclaim(unittest.TestCase):
    def setUp(self):
        FakeReclaimRpc.accounts = []
        FakeReclaimRpc.incinerator_atas = set()
        self.rpc = FakeReclaimRpc("https://example.invalid")

    def test_empty_account_listed_for_close(self):
        FakeReclaimRpc.accounts = [_empty_account()]
        plan = plan_reclaim(self.rpc, WALLET, lambda m: 0.001)
        self.assertEqual(len(plan["empty"]), 1)
        self.assertEqual(plan["empty"][0]["kind"], "close")
        self.assertEqual(plan["empty"][0]["rent_lamports"], RENT)
        self.assertEqual(plan["burn"], [])
        self.assertEqual(plan["total_rent_lamports"], RENT)
        self.assertEqual(plan["tx_count"], 1)
        # One transaction's base fee is subtracted from the net.
        self.assertEqual(plan["net_lamports"], RENT - 5_000)

    def test_zero_value_holding_listed_for_burn(self):
        # 100 tokens at $0.0000001 = $0.00001 — displays as $0.00.
        FakeReclaimRpc.accounts = [
            _holding(MINT_ZERO, 100_000_000)]
        plan = plan_reclaim(self.rpc, WALLET,
                            lambda m: 0.0000001)
        self.assertEqual(len(plan["burn"]), 1)
        item = plan["burn"][0]
        self.assertEqual(item["kind"], "burn")
        self.assertEqual(item["amount"], 100_000_000)
        self.assertAlmostEqual(item["value_usd"], 0.00001)
        self.assertEqual(
            item["incinerator_ata"],
            _ata(INCINERATOR, MINT_ZERO))
        # The incinerator ATA does not exist yet: it is created and
        # its rent is subtracted, so the first burn nets ~0.
        self.assertTrue(item["create_incinerator_ata"])
        self.assertEqual(item["incinerator_ata_rent"], RENT)
        self.assertEqual(plan["ata_creation_lamports"], RENT)
        self.assertEqual(plan["net_lamports"], RENT - RENT - 5_000)

    def test_existing_incinerator_ata_costs_no_rent(self):
        FakeReclaimRpc.accounts = [
            _holding(MINT_ZERO, 100_000_000)]
        FakeReclaimRpc.incinerator_atas = {_ata(INCINERATOR, MINT_ZERO)}
        plan = plan_reclaim(self.rpc, WALLET,
                            lambda m: 0.0000001)
        item = plan["burn"][0]
        self.assertFalse(item["create_incinerator_ata"])
        self.assertEqual(item["incinerator_ata_rent"], 0)
        self.assertEqual(plan["net_lamports"], RENT - 5_000)

    def test_valuable_holding_never_listed(self):
        # 100 tokens at $0.002 = $0.20 — worth something, untouched.
        FakeReclaimRpc.accounts = [
            _holding(MINT_VALUABLE, 100_000_000)]
        plan = plan_reclaim(self.rpc, WALLET, lambda m: 0.002)
        self.assertEqual(plan["items"], [])
        self.assertEqual(plan["burn"], [])
        self.assertEqual(plan["empty"], [])

    def test_unpriced_holding_never_burned(self):
        # A holding the feed cannot price is skipped, never burned —
        # and shown separately so the user knows the list is
        # incomplete rather than clean.
        FakeReclaimRpc.accounts = [
            _holding(MINT_UNPRICED, 100_000_000)]
        plan = plan_reclaim(self.rpc, WALLET, lambda m: None)
        self.assertEqual(plan["items"], [])
        self.assertEqual(len(plan["skipped_unpriced"]), 1)
        self.assertEqual(plan["skipped_unpriced"][0]["mint"],
                         MINT_UNPRICED)

    def test_zero_price_is_not_a_price(self):
        FakeReclaimRpc.accounts = [
            _holding(MINT_ZERO, 100_000_000)]
        plan = plan_reclaim(self.rpc, WALLET, lambda m: 0.0)
        self.assertEqual(plan["items"], [])
        self.assertEqual(len(plan["skipped_unpriced"]), 1)

    def test_both_token_programs_listed(self):
        FakeReclaimRpc.accounts = [
            _empty_account(MINT_EMPTY, TOKEN_PROGRAM),
            _empty_account(MINT_ZERO, TOKEN_2022_PROGRAM)]
        plan = plan_reclaim(self.rpc, WALLET, lambda m: 0.001)
        self.assertEqual(len(plan["empty"]), 2)
        programs = {i["token_program"] for i in plan["empty"]}
        self.assertEqual(programs, {TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

    def test_rpc_failure_refuses(self):
        class DeadRpc:
            def _call(self, *a, **k):
                raise RuntimeError("down")

        with self.assertRaises(ReclaimRefusal):
            plan_reclaim(DeadRpc(), WALLET, lambda m: 0.001)


class TestBuildReclaimTransactions(unittest.TestCase):
    def test_close_only_item_is_one_close_to_the_wallet(self):
        item = {"kind": "close", "account": _ata(WALLET, MINT_EMPTY),
                "mint": MINT_EMPTY, "amount": 0, "decimals": 6,
                "rent_lamports": RENT, "token_program": TOKEN_PROGRAM,
                "incinerator_ata": None,
                "create_incinerator_ata": False,
                "incinerator_ata_rent": 0}
        chunks = build_reclaim_transactions([item], WALLET)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1)
        ins = chunks[0][0]
        self.assertEqual(ins["program_id"], TOKEN_PROGRAM)
        self.assertEqual(ins["data"], bytes([9]))
        # The reclaimed rent goes to the user's wallet.
        self.assertEqual(ins["accounts"][1]["pubkey"], WALLET)

    def test_burn_item_creates_ata_transfers_then_closes(self):
        inc_ata = _ata(INCINERATOR, MINT_ZERO)
        item = {"kind": "burn", "account": _ata(WALLET, MINT_ZERO),
                "mint": MINT_ZERO, "amount": 100_000_000,
                "decimals": 6, "rent_lamports": RENT,
                "token_program": TOKEN_PROGRAM,
                "incinerator_ata": inc_ata,
                "create_incinerator_ata": True,
                "incinerator_ata_rent": RENT}
        chunks = build_reclaim_transactions([item], WALLET)
        self.assertEqual(len(chunks[0]), 3)
        create, transfer, close = chunks[0]
        self.assertEqual(create["data"], b"")
        self.assertEqual(create["accounts"][1]["pubkey"], inc_ata)
        # The full balance goes to the incinerator.
        self.assertEqual(transfer["data"][0], 3)
        self.assertEqual(transfer["data"][1:],
                         (100_000_000).to_bytes(8, "little"))
        self.assertEqual(transfer["accounts"][1]["pubkey"], inc_ata)
        self.assertEqual(close["data"], bytes([9]))
        self.assertEqual(close["accounts"][1]["pubkey"], WALLET)

    def test_burn_item_with_existing_ata_skips_create(self):
        inc_ata = _ata(INCINERATOR, MINT_ZERO)
        item = {"kind": "burn", "account": _ata(WALLET, MINT_ZERO),
                "mint": MINT_ZERO, "amount": 100_000_000,
                "decimals": 6, "rent_lamports": RENT,
                "token_program": TOKEN_PROGRAM,
                "incinerator_ata": inc_ata,
                "create_incinerator_ata": False,
                "incinerator_ata_rent": 0}
        chunks = build_reclaim_transactions([item], WALLET)
        self.assertEqual(len(chunks[0]), 2)

    def test_chunks_split_past_the_per_tx_cap(self):
        items = []
        for i in range(MAX_ACCOUNTS_PER_TX + 1):
            mint = base58_encode(bytes([20 + i]) * 32)
            items.append(
                {"kind": "close", "account": _ata(WALLET, mint),
                 "mint": mint, "amount": 0, "decimals": 6,
                 "rent_lamports": RENT, "token_program": TOKEN_PROGRAM,
                 "incinerator_ata": None,
                 "create_incinerator_ata": False,
                 "incinerator_ata_rent": 0})
        chunks = build_reclaim_transactions(items, WALLET)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), MAX_ACCOUNTS_PER_TX)
        self.assertEqual(len(chunks[1]), 1)

    def test_sign_reclaim_refuses_fee_payer_mismatch(self):
        with self.assertRaises(ValueError):
            sign_reclaim(b"\x01" * 32,
                         "11111111111111111111111111111111", [],
                         "11111111111111111111111111111111")


class TestReclaimFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient",
                                           FakeReclaimRpc)
        self.rpc_patch.start()
        FakeReclaimRpc.instances.clear()
        FakeReclaimRpc.accounts = []
        FakeReclaimRpc.incinerator_atas = set()
        FakeReclaimRpc.sol_balance = SOL_BALANCE
        FakeReclaimRpc.confirm_result = True
        FakeReclaimRpc.send_error = None
        # Prices the preview uses (DexScreener shape): the zero
        # holding is worth $0.00, the valuable one is worth $0.20.
        self.prices = {MINT_ZERO: 0.0000001, MINT_VALUABLE: 0.002}
        self.price_patch = mock.patch.object(
            routes, "fetch_prices_for_mints",
            side_effect=lambda mints: {
                m: self.prices[m] for m in mints if m in self.prices})
        self.price_patch.start()
        signup(self.client, "alice")
        db = self.app.config["DB"]
        self.user_id = db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()["id"]

    def tearDown(self):
        self.price_patch.stop()
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _set_mode(self, mode):
        db = self.app.config["DB"]
        db.execute("UPDATE users SET active_mode = ? WHERE id = ?",
                   (mode, self.user_id))
        db.commit()

    def _wallet_pubkey(self):
        db = self.app.config["DB"]
        return db.execute(
            "SELECT pubkey FROM wallets WHERE user_id = ?",
            (self.user_id,)).fetchone()["pubkey"]

    def _fuel(self):
        return gate_fuel_mod.get_fuel(
            self.app.config["DB"], self.user_id)["trades_remaining"]

    def _sends(self):
        return sum(len(i.send_calls) for i in FakeReclaimRpc.instances)

    def _activity(self, kind):
        db = self.app.config["DB"]
        return db.execute(
            "SELECT * FROM activity WHERE user_id = ? AND kind = ?",
            (self.user_id, kind)).fetchall()

    def test_preview_lists_empty_and_zero_value(self):
        self._set_mode("devnet")
        wallet = self._wallet_pubkey()
        FakeReclaimRpc.accounts = [
            {"account": _ata(wallet, MINT_EMPTY), "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
            {"account": _ata(wallet, MINT_ZERO), "mint": MINT_ZERO,
             "amount": 100_000_000, "decimals": 6,
             "lamports": RENT, "program": TOKEN_PROGRAM},
            {"account": _ata(wallet, MINT_VALUABLE),
             "mint": MINT_VALUABLE, "amount": 100_000_000,
             "decimals": 6, "lamports": RENT, "program": TOKEN_PROGRAM},
        ]
        html = self.client.get("/wallet/reclaim").get_data(as_text=True)
        self.assertIn("Reclaim SOL rent", html)
        self.assertIn("Burn + close", html)
        self.assertIn("Close", html)
        self.assertIn(MINT_EMPTY[:8], html)
        self.assertIn(MINT_ZERO[:8], html)
        # The valuable holding is not on the list.
        self.assertNotIn(MINT_VALUABLE[:8], html)
        # Reclaim button present in real mode.
        self.assertIn("Reclaim", html)
        self.assertIn('name="csrf_token"', html)

    def test_preview_nothing_to_reclaim(self):
        self._set_mode("devnet")
        FakeReclaimRpc.accounts = []
        html = self.client.get("/wallet/reclaim").get_data(as_text=True)
        self.assertIn("Nothing to reclaim", html)

    def test_preview_paper_mode_is_preview_only(self):
        # Default mode after signup is paper.
        wallet = self._wallet_pubkey()
        FakeReclaimRpc.accounts = [
            {"account": _ata(wallet, MINT_EMPTY), "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
        ]
        html = self.client.get("/wallet/reclaim").get_data(as_text=True)
        self.assertIn("Preview only", html)
        self.assertIn(MINT_EMPTY[:8], html)
        # No confirm form in paper mode.
        self.assertNotIn("/wallet/reclaim/confirm", html)

    def test_confirm_refused_in_paper_mode(self):
        wallet = self._wallet_pubkey()
        FakeReclaimRpc.accounts = [
            {"account": _ata(wallet, MINT_EMPTY), "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
        ]
        resp = self.client.post("/wallet/reclaim/confirm", data={
            "csrf_token": csrf(self.client, "/settings"),
            "account": _ata(wallet, MINT_EMPTY), "amount": "0"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._sends(), 0)

    def test_confirm_executes_with_zero_fuel_and_fuel_untouched(self):
        self._set_mode("devnet")
        wallet = self._wallet_pubkey()
        FakeReclaimRpc.accounts = [
            {"account": _ata(wallet, MINT_EMPTY), "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
        ]
        # Fuel is 0 — reclaim is not a trade, so the fuel gate
        # must not block it and no fuel is consumed.
        self.assertEqual(self._fuel(), 0)
        resp = self.client.post("/wallet/reclaim/confirm", data={
            "csrf_token": csrf(self.client, "/settings"),
            "account": _ata(wallet, MINT_EMPTY), "amount": "0"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._sends(), 1)
        self.assertEqual(self._fuel(), 0)
        db = self.app.config["DB"]
        self.assertEqual(db.execute(
            "SELECT COUNT(*) c FROM gate_fuel_uses WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"], 0)
        rows = self._activity("rent_reclaim")
        self.assertEqual(len(rows), 1)
        self.assertIn("closed 1 token account", rows[0]["detail"])
        self.assertEqual(rows[0]["mode"], "real")

    def test_confirm_with_no_accounts_sends_nothing(self):
        self._set_mode("devnet")
        resp = self.client.post("/wallet/reclaim/confirm", data={
            "csrf_token": csrf(self.client, "/settings")})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._sends(), 0)

    def test_confirm_executes_only_confirmed_items(self):
        self._set_mode("devnet")
        wallet = self._wallet_pubkey()
        empty_ata = _ata(wallet, MINT_EMPTY)
        zero_ata = _ata(wallet, MINT_ZERO)
        FakeReclaimRpc.accounts = [
            {"account": empty_ata, "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
            {"account": zero_ata, "mint": MINT_ZERO,
             "amount": 100_000_000, "decimals": 6,
             "lamports": RENT, "program": TOKEN_PROGRAM},
        ]
        db = self.app.config["DB"]
        db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, MINT_ZERO, 100.0, 0.0000001))
        db.commit()
        # Confirm ONLY the burn item: the empty account is not
        # closed and its holding row (none) is untouched.
        resp = self.client.post("/wallet/reclaim/confirm", data={
            "csrf_token": csrf(self.client, "/settings"),
            "account": zero_ata, "amount": "100000000"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._sends(), 1)
        rows = self._activity("rent_reclaim")
        self.assertEqual(len(rows), 1)
        self.assertIn("closed 1 token account", rows[0]["detail"])
        self.assertIn("burned 1", rows[0]["detail"])
        # The burned holding leaves real_holdings.
        self.assertIsNone(db.execute(
            "SELECT * FROM real_holdings WHERE user_id = ? AND "
            "coin_id = ?", (self.user_id, MINT_ZERO)).fetchone())

    def test_confirm_skips_item_that_changed(self):
        self._set_mode("devnet")
        wallet = self._wallet_pubkey()
        empty_ata = _ata(wallet, MINT_EMPTY)
        FakeReclaimRpc.accounts = [
            {"account": empty_ata, "mint": MINT_EMPTY,
             "amount": 0, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
        ]
        # Between preview and confirm the account gains a balance:
        # it no longer qualifies, so nothing executes.
        FakeReclaimRpc.accounts = [
            {"account": empty_ata, "mint": MINT_EMPTY,
             "amount": 5_000_000, "decimals": 6, "lamports": RENT,
             "program": TOKEN_PROGRAM},
        ]
        self.prices[MINT_EMPTY] = 0.002  # now worth $0.01
        resp = self.client.post("/wallet/reclaim/confirm", data={
            "csrf_token": csrf(self.client, "/settings"),
            "account": empty_ata, "amount": "0"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._sends(), 0)
        self.assertEqual(len(self._activity("rent_reclaim")), 0)

    def test_wallet_details_links_reclaim(self):
        self._set_mode("devnet")
        html = self.client.get("/wallet/details").get_data(
            as_text=True)
        self.assertIn("Reclaim rent", html)
        self.assertIn("/wallet/reclaim", html)

if __name__ == "__main__":
    unittest.main()
