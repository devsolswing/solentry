"""Home-holdings wiring (2026-11-11).

Rules under test — the home_holdings.py helper is now wired into
the home route (routes._coin_rows):
- Each home row carries its real token image (image_url) and a
  sparkline SVG (sparkline_svg) after enrichment. A row with no
  image or no price history keeps the ticker-initial icon / no
  sparkline — never a fake.
- In real (devnet) mode, holdings are reconciled with the wallet's
  on-chain token balances, so a coin the chain holds but the DB
  never recorded (Gate, whose six buys were recorded as failed)
  appears on Home with the chain amount, avg_price None and P&L
  None — never guessed. A chain failure or partial read never
  hides a DB-recorded holding.
- The Content-Security-Policy img-src allows the two token-image
  hosts (DexScreener + CoinGecko) so the images can load; no other
  directive is widened.
No SOL moves in these tests; sends are never confirmed. The Gate
mint address below is a public token address, not a secret.
"""

import tempfile
import unittest
from unittest import mock

import tradingbot.charts as charts
import tradingbot.home_holdings as home_holdings
import tradingbot.routes as routes
from helpers import csrf, login, make_client, signup

GATE_MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_UI_AMOUNT = 550340693.346715
GATE_BASE_UNITS = "550340693346715"
MINT = "11111111111111111111111111111111"
DEX_IMG = ("https://dd.dexscreener.com/ds-data/tokens/solana/"
           "11111111111111111111111111111111.png")
CG_IMG = "https://assets.coingecko.com/coins/images/1/large/solana.png"


def make_prices(changes):
    """changes: {coin: (price, change_24h)} or
    (price, change_24h, change_1h)"""
    out = {}
    for c, tup in changes.items():
        price, ch24 = tup[0], tup[1]
        ch1h = tup[2] if len(tup) > 2 else None
        out[c] = {"price": price, "change_24h": ch24,
                  "change_1h": ch1h}
    return {"prices": out, "source": "snapshot", "as_of": "test"}


class FakeFeed:
    """A live feed stand-in: recorded ticks + accept-all prices."""

    def __init__(self, history=None):
        self._history = history or {}

    def history(self, coin_id):
        return self._history.get(coin_id, [])

    def would_accept(self, coin_id, price):
        return True


class FakeRpc:
    """A Solana RPC stand-in returning canned token accounts."""

    def __init__(self, url, gate=True, raise_on_call=False):
        self.url = url
        self._gate = gate
        self._raise = raise_on_call

    def _call(self, method, params):
        if self._raise:
            raise Exception("rpc down")
        if method != "getTokenAccountsByOwner":
            return {}
        program_id = params[1].get("programId")
        # Gate is a legacy SPL token: only the SPL program lists it.
        # Returning it for both programs would double the amount.
        if (self._gate and program_id ==
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
            return {"value": [{
                "account": {"data": {"parsed": {"info": {
                    "mint": GATE_MINT,
                    "tokenAmount": {
                        "amount": GATE_BASE_UNITS, "decimals": 6,
                        "uiAmount": GATE_UI_AMOUNT}}}}}}]}
        return {"value": []}


def _fake_home_get(url):
    """DexScreener pairs for mints, CoinGecko image for majors."""
    if "dexscreener.com" in url:
        return {"pairs": [{
            "chainId": "solana", "dexId": "pumpfun",
            "priceUsd": "0.00001", "liquidity": {"usd": 1000},
            "info": {"imageUrl": DEX_IMG}}]}
    if "api.coingecko.com/api/v3/coins/" in url:
        return {"image": {"large": CG_IMG}}
    return {}


def _fake_charts_get(url):
    """CoinGecko market_chart: three rising prices."""
    return {"prices": [[1700000000000, 9.0],
                       [1700003600000, 10.0],
                       [1700007200000, 11.0]]}


class WiringBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.prices_patch = mock.patch.object(
            routes, "get_prices",
            return_value=make_prices({
                "BTC": (74000.0, -2.5), "ETH": (2100.0, 1.0),
                "SOL": (10.0, 5.25, 2.5), "USDC": (1.0, 0.0)}))
        self.prices_patch.start()
        self.trenches_patch = mock.patch.object(
            routes, "fetch_trenches_scan",
            return_value={"candidates": [], "prices": {},
                          "source": "dexscreener", "as_of": "test",
                          "error": None})
        self.trenches_patch.start()
        self.mint_prices_patch = mock.patch.object(
            routes, "fetch_prices_for_mints", return_value={})
        self.mint_prices_patch.start()
        self.mint_1h_patch = mock.patch.object(
            routes, "fetch_mint_1h_changes", return_value={})
        self.mint_1h_patch.start()
        self.mint_id_patch = mock.patch.object(
            routes, "fetch_mint_identities", return_value={})
        self.mint_id_patch.start()
        home_holdings.clear_image_cache()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.mint_id_patch.stop()
        self.mint_1h_patch.stop()
        self.mint_prices_patch.stop()
        self.trenches_patch.stop()
        self.prices_patch.stop()
        home_holdings.clear_image_cache()
        self.tmp.cleanup()

    def _hold_paper(self, coin_id, amount, avg_price):
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, coin_id, amount, avg_price))
        self.db.commit()

    def _hold_real(self, coin_id, amount, avg_price):
        self.db.execute(
            "INSERT INTO real_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, coin_id, amount, avg_price))
        self.db.commit()

    def _enrichment_patches(self):
        """Patch the HTTP layer so enrichment resolves deterministically."""
        return (
            mock.patch.object(home_holdings, "_default_get",
                              side_effect=_fake_home_get),
            mock.patch.object(home_holdings, "_default_head",
                              return_value=(200, "image/png")),
            mock.patch.object(charts, "_default_get",
                              side_effect=_fake_charts_get),
        )


class TestHomeRowsEnriched(WiringBase):
    def test_mint_row_carries_image_and_sparkline(self):
        self._hold_paper(MINT, 100.0, 0.01)
        feed = FakeFeed({MINT: [(1700000000, 0.00001),
                                (1700003600, 0.00002),
                                (1700007200, 0.000015)]})
        patches = self._enrichment_patches()
        for p in patches:
            p.start()
        try:
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25, 2.5)}), feed=feed)
        finally:
            for p in patches:
                p.stop()
        by_id = {r["coin_id"]: r for r in rows}
        self.assertEqual(by_id[MINT]["image_url"], DEX_IMG)
        svg = by_id[MINT].get("sparkline_svg") or ""
        self.assertTrue(svg.startswith("<svg"),
                        "mint sparkline should be an SVG")
        self.assertIn("holdings-spark-svg", svg)

    def test_major_row_carries_image_and_sparkline(self):
        self._hold_paper("SOL", 2.0, 5.0)
        feed = FakeFeed()
        patches = self._enrichment_patches()
        for p in patches:
            p.start()
        try:
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25, 2.5)}), feed=feed)
        finally:
            for p in patches:
                p.stop()
        by_id = {r["coin_id"]: r for r in rows}
        self.assertEqual(by_id["SOL"]["image_url"], CG_IMG)
        svg = by_id["SOL"].get("sparkline_svg") or ""
        self.assertTrue(svg.startswith("<svg"),
                        "major sparkline should be an SVG")

    def test_row_without_image_or_history_has_neither_key(self):
        self._hold_paper(MINT, 100.0, 0.01)
        feed = FakeFeed()  # no history for the mint
        with mock.patch.object(
                home_holdings, "_default_get",
                return_value={"pairs": []}), mock.patch.object(
                home_holdings, "_default_head",
                return_value=(404, "text/html")):
            rows = routes._coin_rows(
                self.db, self.user_id, "paper",
                make_prices({"SOL": (10.0, 5.25, 2.5)}), feed=feed)
        by_id = {r["coin_id"]: r for r in rows}
        self.assertNotIn("image_url", by_id[MINT])
        self.assertNotIn("sparkline_svg", by_id[MINT])


class TestGateReconciliation(WiringBase):
    def _set_devnet(self):
        self.db.execute(
            "UPDATE users SET active_mode = 'devnet' WHERE id = ?",
            (self.user_id,))
        self.db.commit()

    def test_gate_appears_from_chain_with_no_db_row(self):
        """Gate's six buys were recorded as failed, so no
        real_holdings row exists — the chain balance alone must
        make it appear on Home, with no invented cost basis."""
        self._set_devnet()
        feed = FakeFeed({GATE_MINT: [(1700000000, 0.00001),
                                     (1700003600, 0.00002)]})
        with mock.patch.object(
                routes, "fetch_mint_identities",
                return_value={GATE_MINT: {"name": "Gate",
                                          "symbol": "GATE"}}), \
                mock.patch.object(
                    routes, "fetch_prices_for_mints",
                    return_value={GATE_MINT: 0.00001}), \
                mock.patch.object(routes, "SolanaRpcClient", FakeRpc):
            rows = routes._coin_rows(
                self.db, self.user_id, "devnet",
                make_prices({"SOL": (10.0, 5.25, 2.5)}),
                feed=feed, app=self.app)
        by_id = {r["coin_id"]: r for r in rows}
        self.assertIn(GATE_MINT, by_id,
                      "Gate must appear from the chain balance")
        gate = by_id[GATE_MINT]
        self.assertAlmostEqual(gate["held"], GATE_UI_AMOUNT, places=4)
        self.assertIsNone(gate["avg_price"])
        self.assertIsNone(gate["pnl"])
        self.assertTrue(gate["is_mint"])
        self.assertEqual(gate["ticker"], "GATE")
        self.assertEqual(gate["name"], "Gate")

    def test_chain_failure_keeps_db_holdings(self):
        """A dead RPC must never hide a recorded holding."""
        self._set_devnet()
        self._hold_real(MINT, 50.0, 0.02)
        feed = FakeFeed()
        with mock.patch.object(
                routes, "SolanaRpcClient",
                lambda url: FakeRpc(url, raise_on_call=True)):
            rows = routes._coin_rows(
                self.db, self.user_id, "devnet",
                make_prices({"SOL": (10.0, 5.25, 2.5)}),
                feed=feed, app=self.app)
        by_id = {r["coin_id"]: r for r in rows}
        self.assertIn(MINT, by_id)
        self.assertAlmostEqual(by_id[MINT]["held"], 50.0)

    def test_partial_chain_read_keeps_db_only_rows(self):
        """A chain read that lists no accounts keeps DB rows as-is."""
        self._set_devnet()
        self._hold_real(MINT, 50.0, 0.02)
        feed = FakeFeed()
        with mock.patch.object(
                routes, "SolanaRpcClient",
                lambda url: FakeRpc(url, gate=False)):
            rows = routes._coin_rows(
                self.db, self.user_id, "devnet",
                make_prices({"SOL": (10.0, 5.25, 2.5)}),
                feed=feed, app=self.app)
        by_id = {r["coin_id"]: r for r in rows}
        self.assertIn(MINT, by_id)
        self.assertNotIn(GATE_MINT, by_id)

    def test_gate_shows_on_home_page(self):
        """End to end: /wallet in real mode lists Gate with its
        real image and sparkline."""
        self._set_devnet()
        feed = FakeFeed({GATE_MINT: [(1700000000, 0.00001),
                                     (1700003600, 0.00002)]})
        self.app.config["LIVE_FEED"] = feed
        patches = self._enrichment_patches()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(
                    routes, "fetch_mint_identities",
                    return_value={GATE_MINT: {"name": "Gate",
                                              "symbol": "GATE"}}), \
                    mock.patch.object(
                        routes, "fetch_prices_for_mints",
                        return_value={GATE_MINT: 0.00001}), \
                    mock.patch.object(routes, "SolanaRpcClient",
                                      FakeRpc):
                resp = self.client.get("/wallet")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Gate", html)
        self.assertIn(GATE_MINT, html)
        self.assertIn("holdings-spark-svg", html)


class TestCspImageHosts(WiringBase):
    def test_img_src_allows_dexscreener_and_coingecko(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        csp = resp.headers.get("Content-Security-Policy") or ""
        img_src = ""
        for directive in csp.split(";"):
            if directive.strip().startswith("img-src"):
                img_src = directive
        self.assertIn("https://dd.dexscreener.com", img_src)
        self.assertIn("https://assets.coingecko.com", img_src)

    def test_no_other_directive_is_widened(self):
        resp = self.client.get("/login")
        csp = resp.headers.get("Content-Security-Policy") or ""
        for directive in csp.split(";"):
            name = directive.strip().split(" ")[0]
            if name in ("script-src", "connect-src", "style-src",
                        "frame-src", "font-src"):
                self.assertNotIn("dexscreener", directive)
                self.assertNotIn("coingecko", directive)
        self.assertIn("connect-src 'self'", csp)


if __name__ == "__main__":
    unittest.main()
