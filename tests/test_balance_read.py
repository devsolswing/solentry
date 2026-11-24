"""Balance-read reliability (2026-11-24).

Rules under test — the mainnet freeze: Home amounts froze while prices
kept moving, because the holdings chain read used a bare RPC client
(no fallback) and any failure silently fell back to stale DB amounts,
while prices come from DexScreener, a different source.
- Every balance read goes through a fallback client
  (rpc_client_for_url / rpc_client_for_config / the silent loop's
  default rpc_factory).
- fetch_chain_token_balances reports whether the chain was reached:
  (balances, ok); ok is False only when BOTH program reads failed.
- _reconciled_real_holdings returns (rows, chain_ok); a failed read
  falls back to DB rows AND reports chain_ok False.
- _coin_rows returns (rows, balances_stale); stale only in real mode
  on a failed chain read.
- The real-mode header total is SOL + holdings at the rows' own
  amounts and prices: portfolio_sol == real_sol +
  sum(held * price) / sol_price.
- The executor sell-path balance read treats a missing token account
  as 0 held ("insufficient token balance"), not as an unavailable
  balance; a genuinely down RPC still reports "token balance
  unavailable".
No SOL moves in these tests; sends are never confirmed.
"""

import os
import tempfile
import unittest
from unittest import mock

import tradingbot.home_holdings as home_holdings
import tradingbot.routes as routes
import tradingbot.silent_loop as silent_loop
from helpers import (TEST_ENCRYPTION_KEY, TEST_SESSION_SECRET,
                     make_client, signup)
from tradingbot.config import Config
from tradingbot.executor import execute_real_trade
from tradingbot.solana import (RpcError, held_token_balance,
                               rpc_client_for_config, rpc_client_for_url)
from test_executor import (CURVE_PRICE_USD, TEST_SOL_PRICE_USD,
                           ExecutorBase, FakePumpRpc, make_curve)

MINT = "11111111111111111111111111111111"
SPL_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _token_accounts(amount_ui, decimals=6):
    """getTokenAccountsByOwner value list holding `amount_ui` of MINT."""
    return {"value": [{
        "account": {"data": {"parsed": {"info": {
            "mint": MINT,
            "tokenAmount": {
                "amount": str(int(amount_ui * (10 ** decimals))),
                "decimals": decimals,
                "uiAmount": float(amount_ui)}}}}}}]}


class ChainRpc:
    """Fake RPC for getTokenAccountsByOwner, per token program."""

    def __init__(self, spl=None, token_2022=None, fail=False):
        self._results = {
            SPL_PROGRAM: spl, TOKEN_2022_PROGRAM: token_2022}
        self._fail = fail

    def _call(self, method, params):
        if self._fail:
            raise RuntimeError("rpc down")
        program_id = (params[1] or {}).get("programId")
        result = self._results.get(program_id)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {"value": []}


def _cfg_with_fallback():
    env = {
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "mainnet",
        "DATABASE_PATH": os.path.join(
            tempfile.gettempdir(), "balance-read-test.db"),
        "RPC_URL": "https://primary.example",
        "RPC_FALLBACK_URL": "https://fallback.example",
    }
    return Config(env=env)


class TestRpcClientForUrl(unittest.TestCase):
    def test_for_url_carries_the_fallback(self):
        cfg = _cfg_with_fallback()
        client = rpc_client_for_url(cfg, "https://primary.example")
        self.assertEqual(client.rpc_url, "https://primary.example")
        self.assertEqual(client.fallback_rpc_url,
                         "https://fallback.example")

    def test_for_config_uses_primary_plus_fallback(self):
        cfg = _cfg_with_fallback()
        client = rpc_client_for_config(cfg)
        self.assertEqual(client.rpc_url, "https://primary.example")
        self.assertEqual(client.fallback_rpc_url,
                         "https://fallback.example")

    def test_no_fallback_when_unconfigured(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(
                tempfile.gettempdir(), "balance-read-test.db"),
        })
        client = rpc_client_for_url(cfg, cfg.rpc_url)
        self.assertIsNone(client.fallback_rpc_url)


class TestFetchChainTokenBalancesChecked(unittest.TestCase):
    def test_both_programs_fail_is_not_ok(self):
        rpc = ChainRpc(fail=True)
        balances, ok = home_holdings.fetch_chain_token_balances(
            rpc, "WALLET")
        self.assertEqual(balances, [])
        self.assertFalse(ok)

    def test_partial_read_is_ok(self):
        rpc = ChainRpc(spl=_token_accounts(10.0),
                       token_2022=RuntimeError("2022 down"))
        balances, ok = home_holdings.fetch_chain_token_balances(
            rpc, "WALLET")
        self.assertTrue(ok)
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0]["ui_amount"], 10.0)

    def test_success_is_ok(self):
        rpc = ChainRpc(spl=_token_accounts(10.0),
                       token_2022=_token_accounts(2.5))
        balances, ok = home_holdings.fetch_chain_token_balances(
            rpc, "WALLET")
        self.assertTrue(ok)
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0]["ui_amount"], 12.5)


class TestHeldTokenBalance(unittest.TestCase):
    def test_missing_account_is_zero_not_an_error(self):
        class Rpc:
            def get_token_account(self, pubkey):
                return None
        self.assertEqual(held_token_balance(Rpc(), "ATA"), 0)

    def test_present_account_returns_its_amount(self):
        class Rpc:
            def get_token_account(self, pubkey):
                return {"amount": 123, "decimals": 6}
        self.assertEqual(held_token_balance(Rpc(), "ATA"), 123)

    def test_rpc_failure_still_raises(self):
        class Rpc:
            def get_token_account(self, pubkey):
                raise RpcError("rpc request failed")
        with self.assertRaises(RpcError):
            held_token_balance(Rpc(), "ATA")

    def test_client_without_get_token_account_uses_the_old_read(self):
        class Rpc:
            def get_token_account_balance(self, pubkey):
                return 77
        self.assertEqual(held_token_balance(Rpc(), "ATA"), 77)


class _RealModeBase(unittest.TestCase):
    """make_client app + a real-mode user holding MINT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.patches = [
            mock.patch.object(
                routes, "get_prices",
                return_value={"prices": {
                    "SOL": {"price": 100.0, "change_24h": 0.0,
                            "change_1h": None}},
                    "source": "live", "as_of": "test"}),
            mock.patch.object(
                routes, "fetch_trenches_scan",
                return_value={"candidates": [], "prices": {},
                              "source": "dexscreener", "as_of": "test",
                              "error": None}),
            mock.patch.object(
                routes, "fetch_prices_for_mints",
                return_value={MINT: 2.0}),
            mock.patch.object(
                routes, "fetch_mint_1h_changes", return_value={}),
            mock.patch.object(
                routes, "fetch_mint_identities", return_value={}),
            mock.patch.object(
                routes, "ensure_coin_meta", return_value=None),
            mock.patch.object(
                home_holdings, "ensure_coin_meta", return_value=None),
            mock.patch.object(
                home_holdings, "_default_get",
                return_value={"pairs": []}),
            mock.patch.object(
                home_holdings, "_default_head",
                return_value=(404, "text/html")),
        ]
        for p in self.patches:
            p.start()
        home_holdings.clear_image_cache()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.db.execute(
            "UPDATE users SET active_mode = 'devnet' WHERE id = ?",
            (self.user_id,))
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 10.0, 1.0)",
            (self.user_id, MINT))
        self.db.commit()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        home_holdings.clear_image_cache()
        self.tmp.cleanup()

    def _chain_rpc(self, amount_ui=20.0, fail=False):
        return ChainRpc(spl=_token_accounts(amount_ui), fail=fail)


class TestReconciledHoldings(_RealModeBase):
    def test_chain_amount_wins_when_the_read_succeeds(self):
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._chain_rpc(amount_ui=20.0)):
            rows, chain_ok = routes._reconciled_real_holdings(
                self.app, self.db, self.user_id)
        self.assertTrue(chain_ok)
        self.assertEqual(rows[0]["amount"], 20.0)

    def test_failed_read_falls_back_to_db_and_reports_it(self):
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._chain_rpc(fail=True)):
            rows, chain_ok = routes._reconciled_real_holdings(
                self.app, self.db, self.user_id)
        self.assertFalse(chain_ok)
        self.assertEqual(rows[0]["amount"], 10.0)


class TestCoinRowsStale(_RealModeBase):
    def test_chain_ok_is_not_stale(self):
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._chain_rpc(amount_ui=20.0)):
            rows, stale = routes._coin_rows(
                self.db, self.user_id, "devnet",
                {"prices": {"SOL": {"price": 100.0}},
                 "source": "live", "as_of": "test"},
                feed=None, app=self.app)
        self.assertFalse(stale)
        self.assertEqual(rows[0]["held"], 20.0)

    def test_chain_down_is_stale_with_db_amounts(self):
        with mock.patch.object(
                routes, "rpc_client_for_url",
                return_value=self._chain_rpc(fail=True)):
            rows, stale = routes._coin_rows(
                self.db, self.user_id, "devnet",
                {"prices": {"SOL": {"price": 100.0}},
                 "source": "live", "as_of": "test"},
                feed=None, app=self.app)
        self.assertTrue(stale)
        self.assertEqual(rows[0]["held"], 10.0)

    def test_paper_mode_is_never_stale(self):
        rows, stale = routes._coin_rows(
            self.db, self.user_id, "paper",
            {"prices": {"SOL": {"price": 100.0}},
             "source": "live", "as_of": "test"},
            feed=None, app=self.app)
        self.assertFalse(stale)


class TestDashboardTotal(_RealModeBase):
    def _wallet_html(self, chain_amount_ui=20.0, chain_fail=False):
        rpc = self._chain_rpc(amount_ui=chain_amount_ui,
                              fail=chain_fail)
        with mock.patch.object(
                routes, "rpc_client_for_url", return_value=rpc), \
                mock.patch.object(
                    routes, "_real_balance", return_value=0.5):
            return self.client.get("/wallet")

    def test_total_is_sol_plus_holdings_at_row_prices(self):
        # Chain holds 20 MINT at $2.00 = $40 = 0.40 SOL at $100/SOL;
        # SOL balance 0.50 -> total 0.9000 SOL, $90.00.
        resp = self._wallet_html(chain_amount_ui=20.0)
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("0.9000 SOL", html)
        self.assertIn("90.00", html)
        self.assertNotIn("last saved ones", html)

    def test_stale_read_shows_the_banner_and_db_amounts(self):
        # Chain down: the rows fall back to the DB's 10 MINT
        # ($20 = 0.20 SOL), total 0.7000 SOL — and the page says
        # the amounts are the last saved ones, not live.
        resp = self._wallet_html(chain_fail=True)
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("0.7000 SOL", html)
        self.assertIn("last saved ones", html)

    def test_dashboard_page_shows_the_banner_too(self):
        rpc = self._chain_rpc(fail=True)
        with mock.patch.object(
                routes, "rpc_client_for_url", return_value=rpc), \
                mock.patch.object(
                    routes, "_real_balance", return_value=0.5):
            resp = self.client.get("/dashboard")
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("last saved ones", html)


class TestSilentLoopDefaultFactory(unittest.TestCase):
    def test_factory_builds_a_fallback_client(self):
        cfg = _cfg_with_fallback()
        factory = silent_loop._default_rpc_factory(cfg)
        client = factory("https://primary.example")
        self.assertEqual(client.rpc_url, "https://primary.example")
        self.assertEqual(client.fallback_rpc_url,
                         "https://fallback.example")

    def test_factory_without_a_fallback_builds_bare(self):
        cfg = Config(env={
            "SESSION_SECRET": TEST_SESSION_SECRET,
            "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "NETWORK": "devnet",
            "DATABASE_PATH": os.path.join(
                tempfile.gettempdir(), "balance-read-test.db"),
        })
        client = silent_loop._default_rpc_factory(cfg)(
            "https://api.devnet.solana.com")
        self.assertIsNone(client.fallback_rpc_url)


class MissingAtaRpc(FakePumpRpc):
    """Real-RPC behaviour: getTokenAccountBalance errors when the
    account does not exist; get_token_account answers None."""

    def get_token_account_balance(self, pubkey):
        if pubkey not in self._ata_balances:
            raise RpcError(
                "rpc error response: getTokenAccountBalance: "
                "-32602 could not find account")
        return self._ata_balances[pubkey]

    def get_token_account(self, pubkey):
        if pubkey not in self._ata_balances:
            return None
        return {"amount": self._ata_balances[pubkey], "decimals": 6}


class DownRpc(MissingAtaRpc):
    """Both the balance read and the account read fail: the RPC
    itself is down, not the account missing."""

    def get_token_account(self, pubkey):
        raise RpcError("rpc request failed")


class TestExecutorMissingAta(ExecutorBase):
    def test_sell_with_no_token_account_is_insufficient_not_unavailable(
            self):
        # No ATA for the mint: the wallet holds 0, so the sell
        # refuses as insufficient — it must not claim the balance
        # is unavailable when the chain answered.
        rpc = MissingAtaRpc(self.pubkey, self.mint,
                            curve=make_curve())
        result = execute_real_trade(
            self._bot(), self.user_id, self.mint, "sell", 1.0, 100.0,
            self.db, self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("insufficient token balance", result["reason"])
        self.assertNotIn("unavailable", result["reason"])
        self.assertEqual(rpc.sent, [])

    def test_sell_with_rpc_down_is_still_unavailable(self):
        rpc = DownRpc(self.pubkey, self.mint, curve=make_curve())
        result = execute_real_trade(
            self._bot(), self.user_id, self.mint, "sell", 1.0, 100.0,
            self.db, self.cfg, rpc_client=rpc)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("token balance unavailable", result["reason"])
        self.assertEqual(rpc.sent, [])


if __name__ == "__main__":
    unittest.main()
