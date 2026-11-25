"""Single price source: the Helius stream is the ONE price source
for every price in the app (James, 2026-11-25).

These tests prove the rules he set, at the level each one lives:

(a) decision price, home price and buy/sell price all come from
    the same stream tick — one source, one number.
(b) a real buy is NOT refused by the two-source agreement or the
    SOL cross-check when the stream is live.
(c) when the stream has no tick for a coin, the price is
    unavailable (never a stale/fake price) and the buy refuses
    plainly with that reason.
(d) a data page's displayed price refreshes on the 4-second
    cadence from the stream.
(e) the displayed price matches the latest stream tick exactly,
    and a stale tick is not presented as live.
(f) a stale tick renders as "no data" (not the stale number) and
    the bot treats a stale tick as no price.
(g) a stale price is removed ("no data") and the next fresh tick
    replaces it.
(h) a doubtful/absurd tick is rejected (never displayed, never
    decided on).
(i) holdings refresh every 5 seconds from the stream and drop a
    zero-balance coin; only current coins are shown.
(j) momentum comes from the loop's own recorded price marks, never
    the scan's DexScreener change figures — a fresh coin cannot
    show a 5-minute gain until the loop has watched it ~5 minutes.
(k) graduated coins price from their Raydium pool via Helius; a
    coin no source can price is "no data", never a DexScreener
    number.

No existing assertion is weakened here: the reworked tests for
the deleted second source live in the files they belonged to.
"""

import base64
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
import tradingbot.silent_loop as sl
from helpers import make_client, signup
from tradingbot.config import Config
from tradingbot.livefeed import LiveFeed
from tradingbot.price_gate import check_entry_price
from tradingbot.pumpfun import parse_bonding_curve
from tradingbot.rules import scanner_ruleset
from tradingbot.silent_loop import (
    _chain_price_usd, _reprice_trenches_data, run_once)
from tradingbot.solana import base58_decode
from tradingbot.streaming import (
    HeliusStream, chain_price_usd_for_mints, curve_spot_price_sol,
    find_raydium_pool, pool_price_sol)

GATE_MAINNET = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"
SIGMINT = "B111111111111111111111111111111111111111111"
SOL_PRICE = 100.0
WSOL = "So11111111111111111111111111111111111111112"


def _curve_bytes(vt, vs, complete=False):
    """Bonding-curve account bytes, the layout parse_bonding_curve
    reads (same builder as test_helius_stream)."""
    data = b"\x00" * 8
    for value in (vt, vs, 0, 0, 10 ** 15):
        data += int(value).to_bytes(8, "little")
    data += b"\x01" if complete else b"\x00"
    return data


def _live_curve():
    return _curve_bytes(1_073_000_000_000_000, 30_000_000_000)


def _curve_price_usd():
    return curve_spot_price_sol(parse_bonding_curve(_live_curve())) \
        * SOL_PRICE


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _cfg():
    return Config(env={"NETWORK": "mainnet", "HELIUS_API_KEY": "k",
                       "SESSION_SECRET": "x", "ENCRYPTION_KEY": "y"})


class _ChainRpc:
    """Helius RPC stand-in: one canned curve for any address, plus
    the scam-gate reads a scanner bot needs."""

    def __init__(self, curve_bytes=None, totals=None, supplies=None,
                 authorities=None):
        self._curve_bytes = curve_bytes
        self.totals = totals or {}
        self.supplies = supplies or {}
        self.authorities = authorities or {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self

    def get_account_info(self, pubkey):
        if self._curve_bytes is None:
            return None
        return {"data": self._curve_bytes, "lamports": 1000,
                "executable": False, "owner": "pump"}

    def get_multiple_accounts(self, pubkeys):
        return {p: self.get_account_info(p) for p in pubkeys}

    def get_program_accounts(self, program_id, filters=None):
        return []

    def get_token_largest_accounts(self, mint):
        if mint not in self.totals:
            raise RuntimeError("mint not served")
        total = self.totals[mint]
        share = total // 10
        return [{"address": f"acct{i}", "amount": str(share),
                 "decimals": 6, "uiAmount": 0.0} for i in range(10)]

    def get_token_supply(self, mint):
        if mint not in self.supplies:
            raise RuntimeError("mint not served")
        return self.supplies[mint]

    def get_mint_authorities(self, mint):
        if mint not in self.authorities:
            raise RuntimeError("mint not served")
        return self.authorities[mint]

    def get_balance_lamports(self, pubkey):
        return 0


class _PoolRpc:
    """Helius RPC stand-in for a GRADUATED mint: the curve is
    complete, one Raydium pool exists, and its vaults hold
    1,000,000 base tokens against 50 SOL."""

    POOL = "E111111111111111111111111111111111111111111"
    VAULT_BASE = "C111111111111111111111111111111111111111111"
    VAULT_QUOTE = "D111111111111111111111111111111111111111111"

    def __init__(self, mint):
        self._mint = mint

    def __call__(self, url):
        return self

    def get_account_info(self, pubkey):
        return {"data": _curve_bytes(1, 1, complete=True),
                "lamports": 1000, "executable": False, "owner": "pump"}

    def get_program_accounts(self, program_id, filters=None):
        data = bytearray(400)
        data[32:40] = (6).to_bytes(8, "little")   # base decimals
        data[40:48] = (9).to_bytes(8, "little")   # quote decimals
        data[272:304] = base58_decode(self.VAULT_BASE)
        data[304:336] = base58_decode(self.VAULT_QUOTE)
        data[336:368] = base58_decode(self._mint)
        data[368:400] = base58_decode(WSOL)
        return [{"pubkey": self.POOL,
                 "account": {"data": bytes(data), "lamports": 1000,
                             "owner": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"}}]

    def get_multiple_accounts(self, pubkeys):
        out = {}
        for p in pubkeys:
            data = bytearray(165)
            if p == self.VAULT_BASE:
                data[64:72] = (1_000_000_000_000).to_bytes(8, "little")
            elif p == self.VAULT_QUOTE:
                data[64:72] = (50_000_000_000).to_bytes(8, "little")
            else:
                continue
            out[p] = {"data": bytes(data), "lamports": 1000,
                      "executable": False, "owner": "token"}
        return out


class _EmptyRpc:
    """An RPC that can price nothing — every read comes back empty."""

    def __call__(self, url):
        return self

    def get_account_info(self, pubkey):
        return None

    def get_multiple_accounts(self, pubkeys):
        return {p: None for p in pubkeys}

    def get_program_accounts(self, program_id, filters=None):
        return []


class SingleSourceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", SOL_PRICE, "jupiter")
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()


class TestOneSourceOneNumber(SingleSourceBase):
    def test_decision_home_and_trade_prices_are_the_same_stream_tick(self):
        """(a) The scan says $0.0031 with a +6% 5-minute gain; the
        stream tick says $0.0025. After the loop boundary the
        decision price IS the tick — the home price, the trade
        price and the decision price are one number."""
        tick = 0.0025
        self.feed.ingest(SIGMINT, tick, "helius-stream")
        entry = {"mint": SIGMINT, "symbol": "SIG",
                 "name": "Sig", "price_usd": 0.0031,
                 "change_5m_pct": 6.0, "change_1h_pct": 25.0,
                 "change_6h_pct": 10.0, "change_24h_pct": 40.0,
                 "volume_m5_usd": 5000, "volume_h1_usd": 12000,
                 "txns_m5_buys": 40, "txns_m5_sells": 10}
        data = {"candidates": [entry], "prices": {SIGMINT: entry},
                "source": "dexscreener", "as_of": "test", "error": None}
        rpc = _ChainRpc(_live_curve())
        _reprice_trenches_data(data, self.feed, self.cfg, rpc)
        entry = data["candidates"][0]
        self.assertEqual(entry["price_usd"], tick)
        for key in ("change_5m_pct", "change_1h_pct",
                    "change_6h_pct", "change_24h_pct"):
            self.assertIsNone(entry[key],
                              f"DexScreener {key} must not survive")
        # The same tick, read three ways: the feed, the loop's
        # chain-price read, and the page's tick endpoint.
        self.assertEqual(self.feed.live_price(SIGMINT), tick)
        self.assertEqual(
            _chain_price_usd([SIGMINT], self.feed, self.cfg, rpc),
            {SIGMINT: tick})
        data = self.client.get(
            f"/api/coins/{SIGMINT}/tick").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], tick)
        self.assertEqual(data["source"], "helius-stream")

    def test_real_buy_not_refused_by_removed_cross_checks(self):
        """(b) Stream live, decision price == on-chain price, one
        SOL price: the gate passes, and its details carry no
        two-source disagreement and no SOL cross-check at all."""
        cfg = _cfg()
        price = _curve_price_usd()
        curve = parse_bonding_curve(_live_curve())
        ok, reason, details = check_entry_price(
            _ChainRpc(_live_curve()), cfg, GATE_MAINNET, price,
            decision_price_age_seconds=2.0,
            sol_price_primary=SOL_PRICE, curve=curve)
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)
        self.assertNotIn("sol_cross_check", details)
        self.assertNotIn("disagree", str(details))

    def test_no_tick_means_no_price_and_buy_refuses_plainly(self):
        """(c) The stream has no tick and the chain cannot price
        the mint: there is no price anywhere, and the buy refuses
        with the plain no-price reason — never a stale or fake
        number."""
        empty = _EmptyRpc()
        self.assertIsNone(self.feed.live_price(SIGMINT))
        self.assertEqual(
            _chain_price_usd([SIGMINT], self.feed, self.cfg, empty),
            {})
        data = {"candidates": [{"mint": SIGMINT, "price_usd": 0.0031,
                                "change_5m_pct": 6.0}],
                "prices": {SIGMINT: {"price_usd": 0.0031}},
                "source": "dexscreener", "as_of": "test", "error": None}
        _reprice_trenches_data(data, self.feed, self.cfg, empty)
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["prices"], {})
        data = self.client.get(
            f"/api/coins/{SIGMINT}/tick").get_json()
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")
        ok, reason, _details = check_entry_price(
            empty, _cfg(), GATE_MAINNET, None,
            decision_price_age_seconds=None,
            sol_price_primary=SOL_PRICE)
        self.assertFalse(ok)
        self.assertEqual(reason, "no decision price for this buy")


class TestExactLivePrice(SingleSourceBase):
    def test_displayed_price_matches_latest_stream_tick(self):
        """(e) The displayed price IS the latest tick, exactly —
        and when a newer tick lands, the display moves with it."""
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream")
        self.assertEqual(self.feed.live_price(SIGMINT), 0.0025)
        resp = self.client.get(f"/api/coins/{SIGMINT}/tick")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 0.0025)
        self.feed.ingest(SIGMINT, 0.0026, "helius-stream")
        self.assertEqual(self.feed.live_price(SIGMINT), 0.0026)
        data = self.client.get(
            f"/api/coins/{SIGMINT}/tick").get_json()
        self.assertEqual(data["price"], 0.0026)

    def test_stale_tick_is_not_presented_as_live(self):
        """(e) A tick older than the 30s bound is never a live
        price: the feed read is None and the tick endpoint says
        so."""
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream",
                         now=time.time() - 31)
        self.assertIsNone(self.feed.live_price(SIGMINT))
        data = self.client.get(
            f"/api/coins/{SIGMINT}/tick").get_json()
        self.assertFalse(data["ok"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["error"], "no data")

    def test_stale_tick_is_no_data_and_bot_skips(self):
        """(f) A stale tick renders as "no data" on the page and
        the bot treats it as no price: the scan entry is dropped
        from the decision set."""
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream",
                         now=time.time() - 60)
        data = self.client.get(
            f"/api/coins/{SIGMINT}/tick").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no data")
        self.assertEqual(
            _chain_price_usd([SIGMINT], self.feed, self.cfg,
                             _EmptyRpc()), {})
        scan = {"candidates": [{"mint": SIGMINT, "price_usd": 0.0025,
                                "change_5m_pct": 6.0}],
                "prices": {SIGMINT: {"price_usd": 0.0025}},
                "source": "dexscreener", "as_of": "test", "error": None}
        _reprice_trenches_data(scan, self.feed, self.cfg, _EmptyRpc())
        self.assertEqual(scan["candidates"], [])

    def test_stale_price_removed_then_fresh_tick_replaces_it(self):
        """(g) A stale price is removed and the stream's own
        re-read puts a fresh price back; the next live tick then
        replaces it again — "no data" is brief, not stuck."""
        t0 = time.time()
        self.feed.ingest("SOL", SOL_PRICE, "jupiter", now=t0)
        stream = HeliusStream(
            self.feed, _cfg(),
            rpc_factory=lambda url: _ChainRpc(_live_curve()))
        stream._subs[GATE_MAINNET] = "curveaddr"
        # No tick yet: the stale refresh re-reads the chain.
        stream._refresh_stale_mints(now=t0 + 31)
        self.assertAlmostEqual(
            self.feed.live_price(GATE_MAINNET, now=t0 + 31),
            _curve_price_usd(), places=9)
        # The tick goes stale again; a fresh tick replaces it.
        # (The fresh tick is near the curve price — a tick 1000x
        # off it would be rejected by the jump guard, as test (h)
        # proves.)
        self.assertIsNone(
            self.feed.live_price(GATE_MAINNET, now=t0 + 62))
        fresh = _curve_price_usd() * 1.037
        self.feed.ingest(GATE_MAINNET, fresh, "helius-stream",
                         now=t0 + 62)
        self.assertEqual(
            self.feed.live_price(GATE_MAINNET, now=t0 + 62), fresh)

    def test_absurd_tick_rejected(self):
        """(h) A 1000x jump tick is rejected by the feed's jump
        guard — never displayed, never decided on — and the gate
        refuses a decision price that far from the on-chain read."""
        self.feed.ingest(SIGMINT, 0.002, "helius-stream")
        self.assertFalse(
            self.feed.ingest(SIGMINT, 2.0, "helius-stream"))
        self.assertEqual(self.feed.live_price(SIGMINT), 0.002)
        cfg = _cfg()
        curve = parse_bonding_curve(_live_curve())
        onchain = _curve_price_usd()
        ok, reason, _details = check_entry_price(
            _ChainRpc(_live_curve()), cfg, GATE_MAINNET,
            onchain * 1000.0, decision_price_age_seconds=1.0,
            sol_price_primary=SOL_PRICE, curve=curve)
        self.assertFalse(ok)
        self.assertIn("moved away", reason)


class TestGraduatedCoins(unittest.TestCase):
    def test_graduated_coin_prices_from_raydium_pool_via_helius(self):
        """(k) A graduated coin has no curve — its price comes
        from the Raydium pool, read through Helius: 50 SOL of
        quote against 1,000,000 base tokens at $100 SOL."""
        rpc = _PoolRpc(SIGMINT)
        prices = chain_price_usd_for_mints(rpc, [SIGMINT], SOL_PRICE)
        self.assertAlmostEqual(prices[SIGMINT], 0.005, places=9)
        pool = find_raydium_pool(rpc, SIGMINT)
        self.assertIsNotNone(pool)
        self.assertEqual(pool_price_sol(
            pool, {rpc.VAULT_BASE: 1_000_000_000_000,
                   rpc.VAULT_QUOTE: 50_000_000_000}), 0.00005)

    def test_graduated_coin_with_no_pool_is_no_data(self):
        """(k) A graduated coin with no discoverable pool is "no
        data" — never a DexScreener number, never a guess."""
        rpc = _ChainRpc(_curve_bytes(1, 1, complete=True))
        self.assertEqual(
            chain_price_usd_for_mints(rpc, [SIGMINT], SOL_PRICE), {})


class TestMomentumFromMarks(unittest.TestCase):
    """(j) Momentum comes from the loop's own marks. Setup mirrors
    test_bot_tuning's scanner tests; the one difference is the
    mint is a real base58 address so the chain read can price it,
    and the scan entry's DexScreener change figures are the ones
    the reprice must null."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        self.feed = self.app.config["LIVE_FEED"]
        self.feed.ingest("SOL", SOL_PRICE, "jupiter")
        self.rpc = _ChainRpc(
            _live_curve(),
            totals={SIGMINT: 50_000_000},
            supplies={SIGMINT: 1_000_000_000},
            authorities={SIGMINT: (False, False)})
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, ?, ?, NULL, 'metaplex', ?) "
            "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
            "symbol = excluded.symbol",
            (SIGMINT, "Sig", "SIG", _now()))
        self.db.commit()

    def tearDown(self):
        self.tmp.cleanup()
        sl._SELL_IN_FLIGHT.clear()
        sl._TOP10_SKIP_LOGGED_AT.clear()
        with sl._TICK_DIRTY_LOCK:
            sl._TICK_DIRTY_MINTS.clear()
        sl._TICK_EVENT.clear()

    def _insert_bot(self):
        import hashlib
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, rules_json) VALUES (?, ?, 'bold', ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (self.user_id, "Tuned", json.dumps([]), 20, 7, "paper",
             1, 3, hashlib.sha256(b"tok").hexdigest(),
             json.dumps(scanner_ruleset())))
        self.db.commit()
        return cur.lastrowid

    def _insert_mark(self, bot_id, mint, price, when):
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, when))
        self.db.commit()

    def _reset_scan(self, bot_id):
        self.db.execute("UPDATE bots SET last_scan_at = '' WHERE id = ?",
                        (bot_id,))
        self.db.commit()

    def _scan(self):
        entries = [{"mint": SIGMINT, "symbol": "SIG", "name": "Sig",
                    "price_usd": _curve_price_usd(),
                    "change_5m_pct": 6.0, "change_1h_pct": 25.0,
                    "change_6h_pct": None, "change_24h_pct": None,
                    "liquidity_usd": 1000, "market_cap_usd": None,
                    "dex_id": "pumpfun", "pair_address": "pair",
                    "pair_created_at": 0, "volume_m5_usd": 5000,
                    "volume_h1_usd": 12000, "txns_m5_buys": 40,
                    "txns_m5_sells": 10}]

        def fetch(mints=None):
            prices = {e["mint"]: e for e in entries}
            return {"candidates": list(entries), "prices": prices,
                    "source": "dexscreener", "as_of": "test",
                    "error": None}

        return fetch

    def _run(self):
        return run_once(self.db, self.cfg,
                        price_fetcher=lambda: {"prices": {},
                                               "source": "snapshot",
                                               "as_of": "test"},
                        trenches_fetcher=self._scan(),
                        rpc_factory=self.rpc, feed=self.feed)

    def _buys(self, outcomes):
        return [a for o in outcomes for a in o["actions"]
                if a["action"] == "buy"]

    def test_fresh_coin_with_no_marks_never_buys(self):
        """The scan claims +6% in 5 minutes, but the loop has no
        marks for the coin — no 5-minute gain to judge — so two
        scans in a row buy nothing. A fresh coin cannot be bought
        on first sighting for a 5m-gain rule."""
        bot_id = self._insert_bot()
        self._run()
        self._reset_scan(bot_id)
        outcomes = self._run()
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND "
            "coin_id = ?", (bot_id, SIGMINT)).fetchone())

    def test_watched_coin_with_marks_buys_after_confirmation(self):
        """The same scan, but the loop has watched the coin: a
        mark 5.5 minutes ago 10% below the chain price and a mark
        an hour ago 25% below it. Two scans later the bot buys —
        the gain came from the marks, not the scan's figures."""
        bot_id = self._insert_bot()
        now_price = _curve_price_usd()
        self._insert_mark(bot_id, SIGMINT, now_price / 1.10,
                          _now(-330))
        self._insert_mark(bot_id, SIGMINT, now_price / 1.25,
                          _now(-3600))
        self._run()
        self._reset_scan(bot_id)
        outcomes = self._run()
        self.assertEqual(len(self._buys(outcomes)), 1)
        self.assertIsNotNone(self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND "
            "coin_id = ?", (bot_id, SIGMINT)).fetchone())


class TestCadenceAndHoldings(SingleSourceBase):
    def test_data_pages_refresh_every_four_seconds(self):
        """(d) The 4-second cadence: the REST fallback poll and the
        $GATE quote timer both run at 4000ms, and /api/live serves
        the current stream tick."""
        with open("tradingbot/static/live.js",
                  encoding="utf-8") as f:
            js = f.read()
        self.assertIn("setInterval(fetchSnapshot, 4000)", js)
        with open("tradingbot/templates/_gate_buy.html",
                  encoding="utf-8") as f:
            gate_js = f.read()
        self.assertIn("setInterval(refreshQuotes, 4000)", gate_js)
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream")
        resp = self.client.get("/api/live")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        ticks = data.get("ticks") or data.get("prices") or {}
        self.assertIn(SIGMINT, ticks)
        price = ticks[SIGMINT]
        if isinstance(price, dict):
            price = price.get("price")
        self.assertEqual(price, 0.0025)

    def test_holdings_refresh_every_five_seconds_from_the_stream(self):
        """(i) The held-coins list polls every 5 seconds, and a
        re-price from a new stream tick shows on the next read."""
        with open("tradingbot/static/live.js",
                  encoding="utf-8") as f:
            js = f.read()
        self.assertIn("SPARK_POLL_MS = 5000", js)
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.01)",
            (self.user_id, SIGMINT))
        self.db.commit()
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream")
        rows, _stale = routes._coin_rows(
            self.db, self.user_id, "paper",
            {"prices": {"SOL": {"price": SOL_PRICE}},
             "source": "live", "as_of": "test"},
            feed=self.feed, app=self.app)
        row = next(r for r in rows if r["coin_id"] == SIGMINT)
        self.assertEqual(row["price"], 0.0025)
        self.feed.ingest(SIGMINT, 0.0026, "helius-stream")
        rows, _stale = routes._coin_rows(
            self.db, self.user_id, "paper",
            {"prices": {"SOL": {"price": SOL_PRICE}},
             "source": "live", "as_of": "test"},
            feed=self.feed, app=self.app)
        row = next(r for r in rows if r["coin_id"] == SIGMINT)
        self.assertEqual(row["price"], 0.0026)

    def test_zero_balance_coin_drops_off_the_holdings_list(self):
        """(i) Only current coins: a coin whose balance hits 0
        drops off the list on the next refresh, and no exited or
        stale coin is ever shown."""
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.01)",
            (self.user_id, SIGMINT))
        self.db.commit()
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream")
        rows, _stale = routes._coin_rows(
            self.db, self.user_id, "paper",
            {"prices": {"SOL": {"price": SOL_PRICE}},
             "source": "live", "as_of": "test"},
            feed=self.feed, app=self.app)
        self.assertIn(SIGMINT, [r["coin_id"] for r in rows])
        self.db.execute(
            "UPDATE paper_holdings SET amount = 0 WHERE user_id = ? "
            "AND coin_id = ?", (self.user_id, SIGMINT))
        self.db.commit()
        rows, _stale = routes._coin_rows(
            self.db, self.user_id, "paper",
            {"prices": {"SOL": {"price": SOL_PRICE}},
             "source": "live", "as_of": "test"},
            feed=self.feed, app=self.app)
        self.assertNotIn(SIGMINT, [r["coin_id"] for r in rows])


class TestSnapshotPriceMap(SingleSourceBase):
    def test_snapshot_prices_holdings_from_the_stream(self):
        """The P&L snapshot values a holding at the stream tick —
        not at cost, not at a second source's number."""
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.01)",
            (self.user_id, SIGMINT))
        self.db.commit()
        self.feed.ingest(SIGMINT, 0.0025, "helius-stream")
        price_map = routes._snapshot_price_map(self.app, self.user_id)
        self.assertEqual(price_map[SIGMINT], 0.0025)
        self.assertEqual(price_map["SOL"], SOL_PRICE)

    def test_snapshot_leaves_unpriced_holding_at_cost(self):
        """A holding no source can price is left out of the map,
        so the snapshot values it at cost — never $0, never a
        guess."""
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, 100.0, 0.01)",
            (self.user_id, SIGMINT))
        self.db.commit()
        with mock.patch.object(routes, "_chain_prices_for_mints",
                               return_value={}):
            price_map = routes._snapshot_price_map(
                self.app, self.user_id)
        self.assertNotIn(SIGMINT, price_map)


if __name__ == "__main__":
    unittest.main()
