"""Middy preset (2026-11-24, retuned 2026-11-25, filters retuned
2026-11-26): James's spec — mid-exposure paper-first sniper.
Buys just beat sells (any positive edge), turnover (5-minute
volume / market cap) at least 0.01, only coins under 10 minutes
old and under 75% of the bonding curve, buy 20% of balance per
trade, max 2 coins open, take profit sells ALL — small TP at +17%
on coins Middy is done with; higher window 25%-37% only while buys
beat sells — at 37% secure the profit immediately and move on),
trailing stop -8% from the peak, HARD stop 5.5% from entry, slow
coins out after 120 seconds, daily-loss breaker 20% of the
starting balance, 5-minute re-buy cooldown after a losing close,
NO BLIND TRADES — every entry confirms on a second scan first.
The card shows verified paper results; the bot details page shows
the site-wide win/loss aggregate across all Middy copies.

Every gate, exit, preset default, Page 6 button and cooldown is
tested here, on the same fake-RPC / fake-feed / fake-scan harness
as the Pit tests. No live network in these tests: the fake RPC
serves the bonding-curve account (at a configured progress) and
the Metaplex metadata account (with a configured URI) for the
test mint, plus the tuning-test top-10 / authority surface.

The honest map lives in rules.MIDDY_NOTES and the staging
MANIFEST: sub-second entries, creator/bundler/dev-history
checks and unique-wallet counts are NOT enforced — the app has
no data source for them. These tests assert the closest versions
that ARE enforced, and that the not-enforced rules are never
faked.
"""

import hashlib
import json
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tradingbot.routes as routes
import tradingbot.silent_loop as sl
from helpers import csrf, make_client, make_config, signup
from tradingbot.coin_meta import metadata_pda
from tradingbot.ledger import kind_wl_summary, record_trade
from tradingbot.pumpfun import bonding_curve_pda
from tradingbot.rules import (MIDDY_CATEGORY, MIDDY_NOTES, middy_ruleset,
                              preset_bots, summarize_rules,
                              validate_ruleset)
from tradingbot.silent_loop import (_after_sell, _middy_already_bought,
                                    _middy_buy_halt, _middy_buys_last_hour,
                                    _middy_entry_gates,
                                    _middy_hard_exit_reason,
                                    _middy_mid_hold, _middy_pace_halt,
                                    _middy_slow_reason,
                                    _middy_take_profit_reason, run_once)

# Valid base58 addresses (no "I"/"O"/"l"/"0") — the curve PDA
# derivation decodes the mint, so the tuning tests' MINTA ("I")
# cannot be used here.
MIDDY_MINT = "F111111111111111111111111111111111111111111"
MIDDY_MINT2 = "G111111111111111111111111111111111111111111"
MIDDY_MINT3 = "H111111111111111111111111111111111111111111"


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def _now_ms() -> int:
    """Current time as a ms epoch — the scan feed's pairCreatedAt unit."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _entry(mint, price, change_5m=2.0, change_1h=25.0,
           volume_m5=5000, volume_h1=12000, buys=140, sells=100,
           mcap=25000, age_minutes=5.0):
    """A realistic Middy candidate: +2% in 5 minutes, $25k mcap,
    volume rising (5x the hourly pace), buys 1.4x sells. Fresh
    by default: 5 minutes old, inside the 10-minute lifecycle
    window (James, 2026-11-26). Tests that need an old, missing,
    or future creation time override pair_created_at after the
    call, or pass age_minutes."""
    return {"mint": mint, "symbol": mint[:6], "name": mint,
            "price_usd": price, "change_5m_pct": change_5m,
            "change_1h_pct": change_1h, "change_6h_pct": None,
            "change_24h_pct": None, "liquidity_usd": 1000,
            "market_cap_usd": mcap,
            "dex_id": "pumpfun", "pair_address": "pair" + mint,
            "pair_created_at": _now_ms() - int(age_minutes * 60_000),
            "volume_m5_usd": volume_m5, "volume_h1_usd": volume_h1,
            "txns_m5_buys": buys, "txns_m5_sells": sells}


def _middy_entry(mint, price, devnet_watchlist=False, **over):
    """A Middy candidate: the base gaining entry plus (for devnet
    test coins) the watchlist flag."""
    entry = _entry(mint, price, **over)
    if devnet_watchlist:
        entry["devnet_watchlist"] = True
    return entry


def _curve_bytes(progress_pct):
    """49-byte bonding-curve account at `progress_pct`% filled:
    discriminator (8), virtual token reserves, virtual SOL (30),
    real token (0), real SOL (0), total supply, complete=False."""
    virtual = int(round((1.0 - progress_pct / 100.0)
                        * 1_073_000_000_000_000))
    return (b"\x00" * 8
            + struct.pack("<Q", virtual)
            + struct.pack("<Q", 30_000_000_000)
            + struct.pack("<Q", 0)
            + struct.pack("<Q", 0)
            + struct.pack("<Q", 1_000_000_000_000_000)
            + b"\x00")


def _borsh_str(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _metadata_bytes(uri="https://example.com/meta.json"):
    """Metaplex metadata account bytes: key (1), update authority
    (32), mint (32), name, symbol, uri as Borsh strings."""
    return (b"\x04" + b"\x00" * 32 + b"\x00" * 32
            + _borsh_str("Middy Coin") + _borsh_str("MID")
            + _borsh_str(uri))


class _FakeScan:
    def __init__(self, entries):
        self.entries = list(entries)

    def __call__(self, mints=None):
        prices = {e["mint"]: e for e in self.entries}
        return {"candidates": list(self.entries), "prices": prices,
                "source": "dexscreener", "as_of": "test", "error": None}


def _empty_prices():
    return {"prices": {}, "source": "snapshot", "as_of": "test"}


class _FakeRpc:
    """Solana RPC stand-in: the tuning-test top-10 / authority
    surface, plus the curve + metadata account reads the Middy
    gates need. `progress_pct` is the bonding-curve fill for the
    test mint; `uri` is the metadata URI it carries."""

    def __init__(self, mint, progress_pct=30.0,
                 uri="https://example.com/meta.json"):
        self.mint = mint
        self.progress_pct = progress_pct
        self.uri = uri
        # One fake serves every RPC read in these tests: the
        # curve account is served for all three test mints, at
        # the same progress. In production the RPC serves any
        # mint's curve account, so the fake must too — otherwise
        # a second or third candidate coin could never pass the
        # curve half of Middy's lifecycle window in a test.
        self.curve_addrs = {bonding_curve_pda(m)[0]
                            for m in (MIDDY_MINT, MIDDY_MINT2,
                                      MIDDY_MINT3)}
        self.curve_addr, _ = bonding_curve_pda(mint)
        self.meta_addr = metadata_pda(mint)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self

    def get_account_info(self, pubkey):
        if pubkey in self.curve_addrs:
            return {"data": _curve_bytes(self.progress_pct)}
        if pubkey == self.meta_addr:
            return {"data": _metadata_bytes(self.uri)}
        return None

    def get_token_largest_accounts(self, mint):
        total = 50_000_000
        share = total // 10
        return [{"address": f"acct{i}", "amount": str(share),
                 "decimals": 6, "uiAmount": 0.0} for i in range(10)]

    def get_token_supply(self, mint):
        return 1_000_000_000

    def get_mint_authorities(self, mint):
        return (False, False)

    def get_balance_lamports(self, pubkey):
        return 0


class _FakeFeed:
    """Live-feed stand-in: snapshot() returns the tick per mint."""

    def __init__(self, ticks=None):
        self.ticks = ticks or {}

    def snapshot(self, coin_ids=None):
        out = {}
        for cid in (coin_ids or list(self.ticks)):
            if cid in self.ticks:
                out[cid] = {"coin_id": cid, "price": self.ticks[cid],
                            "source": "test", "stale": False}
        return out


    def tick_age_seconds(self, coin_id, now=None):
        # The loop's SOL read: a tick the feed holds is fresh
        # (age 0.0); a coin with no tick has no age at all.
        return 0.0 if coin_id in self.ticks else None

    def live_price(self, coin_id, now=None):
        price = self.ticks.get(coin_id)
        return float(price) if price is not None else None

    def live_tick(self, coin_id, now=None):
        if coin_id not in self.ticks:
            return None
        return {"coin_id": coin_id, "price": self.ticks[coin_id],
                "source": "test", "stale": False}

    def change_over(self, coin_id, window_seconds, now=None):
        # The fake feed carries no history: no change figure,
        # never a guessed one.
        return None

    def sanitize_prices_data(self, data):
        return data

    def sanitize_trenches(self, data):
        return data

    def set_tick_listener(self, callback):
        self._listener = callback


class MiddyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        self.rpc_patch = mock.patch.object(routes, "SolanaRpcClient")
        self.rpc_patch.start()
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.cfg = self.app.config["TRADINGBOT_CONFIG"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]
        # A clean coin: top-10 5% of supply, authorities revoked,
        # metadata on file, curve 30% filled — passes every gate.
        self.fake_rpc = _FakeRpc(MIDDY_MINT)
        for mint in (MIDDY_MINT, MIDDY_MINT2, MIDDY_MINT3):
            self._seed_meta(mint)
        # The loop reads the top-10 / curve / metadata through the
        # injected rpc_factory, and the scam gate through the same
        # fake — one fake serves every RPC read in these tests.
        sl._SELL_IN_FLIGHT.clear()
        sl._TOP10_SKIP_LOGGED_AT.clear()
        with sl._TICK_DIRTY_LOCK:
            sl._TICK_DIRTY_MINTS.clear()
        sl._TICK_EVENT.clear()

    def tearDown(self):
        self.rpc_patch.stop()
        self.tmp.cleanup()

    def _seed_meta(self, mint, name="Middy Coin", symbol="MID"):
        self.db.execute(
            "INSERT INTO coin_meta (mint, name, symbol, image_url, "
            "source, fetched_at) VALUES (?, ?, ?, NULL, 'metaplex', ?) "
            "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
            "symbol = excluded.symbol",
            (mint, name, symbol, _now()))
        self.db.commit()

    def _insert_bot(self, rules, name="Middy", mode="paper",
                    silent=1, coins=None, buy_size=20, stop_loss=5.5,
                    max_open=2, category=MIDDY_CATEGORY,
                    bankroll_sol=0.20):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, strategy, coins, "
            "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
            "api_token_hash, category, rules_json, bankroll_sol) "
            "VALUES (?, ?, 'bold', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_id, name, json.dumps(coins or []), buy_size,
             stop_loss, mode, silent, max_open,
             hashlib.sha256(b"tok").hexdigest(), category,
             json.dumps(rules), bankroll_sol))
        self.db.commit()
        return cur.lastrowid

    def _insert_position(self, bot_id, coin, amount, avg_price,
                         peak=None, orig=None, stage=0, opened_at=None,
                         last_price=None, last_price_at=None):
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, amount, "
            "avg_price, mode, opened_at, peak_price, orig_amount, "
            "ladder_stage, last_price, last_price_at) "
            "VALUES (?, ?, ?, ?, ?, 'paper', ?, ?, ?, ?, ?, ?)",
            (self.user_id, bot_id, coin, amount, avg_price,
             opened_at or _now(),
             peak if peak is not None else avg_price,
             orig if orig is not None else amount, stage,
             last_price if last_price is not None else avg_price,
             last_price_at if last_price_at is not None else _now()))
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (?, ?, ?, ?)",
            (self.user_id, coin, amount, avg_price))
        self.db.commit()

    def _snapshot(self, total_value_usd, days_ago=1, real_sol=None):
        """A portfolio snapshot `days_ago` days before today — the
        day-start capital source for the breaker."""
        when = _now(-days_ago * 86400)
        self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
            "holdings_value_usd, total_value_usd, real_sol, created_at) "
            "VALUES (?, ?, 0.0, ?, ?, ?)",
            (self.user_id, total_value_usd, total_value_usd,
             real_sol, when))
        self.db.commit()

    def _reset_scan(self, bot_id):
        self.db.execute("UPDATE bots SET last_scan_at = '' WHERE id = ?",
                        (bot_id,))
        self.db.commit()

    def _position(self, bot_id, coin):
        return self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, coin)).fetchone()

    def _run(self, entries, feed=None, executor=None):
        # Single-price source: a scan entry survives the loop's
        # reprice only with a live feed tick at its price (the
        # chain cannot price these test mints). Seed ticks for
        # entries the feed has none for — never overwriting a tick
        # a test set deliberately — and never a SOL tick, so tests
        # asserting "no live SOL price" still see none.
        if feed is None:
            feed = _FakeFeed()
        for _e in entries:
            feed.ticks.setdefault(_e["mint"], _e["price_usd"])
        scan = _FakeScan(entries)
        return run_once(self.db, self.cfg, price_fetcher=_empty_prices,
                        trenches_fetcher=scan,
                        rpc_factory=self.fake_rpc,
                        real_executor=executor, feed=feed)

    def _buys(self, outcomes):
        return [a for o in outcomes for a in o["actions"]
                if a["action"] == "buy"]

    def _sells(self, outcomes):
        return [a for o in outcomes for a in o["actions"]
                if a["action"] == "sell"]

    def _insert_mark(self, bot_id, mint, price, when):
        self.db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, when))
        # 1-hour trend baseline for the momentum gate: the loop
        # fills change_1h_pct from the loop's own marks, so a
        # 5-minute baseline alone leaves the 1-hour trend unknown
        # and the gate fails closed. A mark already an hour old
        # needs no companion.
        try:
            _age = (datetime.now(timezone.utc)
                    - datetime.strptime(
                        when, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc)).total_seconds()
        except Exception:
            _age = 0
        if 300 <= _age < 3600:
            self.db.execute(
                "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
                "recorded_at) VALUES (?, ?, ?, ?)",
                (bot_id, mint, price, _now(-3700)))
        self.db.commit()

    def _buy_twice(self, bot_id, entry, feed=None):
        """Run two scans (2-scan confirmation) and return the
        outcomes of the second scan."""
        # The 5-minute gain now comes from the loop's own marks:
        # seed one 330s old at the price the entry's stated
        # change implies (the helper also seeds the 1-hour
        # companion the momentum gate needs).
        change = entry.get("change_5m_pct")
        if change is not None:
            # Encode the stated change plus 0.05pp (see Pit:
            # an exactly-encoded gain reads a float hair under
            # its threshold).
            self._insert_mark(
                bot_id, entry["mint"],
                entry["price_usd"]
                / (1.0 + (float(change) + 0.05) / 100.0),
                _now(-330))
        self._run([entry], feed=feed)
        self._reset_scan(bot_id)
        return self._run([entry], feed=feed)

    def _stub_executor(self):
        """A real executor that always 'executes': the loop's
        real-mode sells need an executor result, and the default
        would try a real swap. The stub stands in for the signing
        bot — no chain, no keys."""
        return mock.Mock(return_value={
            "status": "executed", "signature": "sig-middy-test",
            "coin_amount": 500.0, "sol_spent": 0.02,
            "sol_received": 0.02, "signing_bot": "test"})


class TestMiddyRuleset(MiddyBase):
    def test_ruleset_validates(self):
        cleaned, err = validate_ruleset(middy_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["kind"], "middy")

    def test_ruleset_carries_turnover_not_floors(self):
        # The flat floors are GONE (2026-11-26): the preset's
        # buy filter gates on the turnover ratio only. The
        # volume/mcap keys stay at 0.0 (off) on purpose — the
        # validator fills a missing key from the shared default
        # ($1,000 volume), so omitting them would silently
        # reintroduce a floor.
        filt = middy_ruleset()["buy_filter"]
        self.assertEqual(filt["min_turnover_ratio"], 0.01)
        self.assertEqual(filt["min_volume_5m_usd"], 0.0)
        self.assertEqual(filt["min_market_cap_usd"], 0.0)
        self.assertEqual(filt["max_market_cap_usd"], 0.0)
        cleaned, err = validate_ruleset(middy_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(
            cleaned["buy_filter"]["min_turnover_ratio"], 0.01)

    def test_turnover_ratio_out_of_range_rejected(self):
        rules = middy_ruleset()
        for bad in (-0.1, 10.1, "fast"):
            rules["buy_filter"]["min_turnover_ratio"] = bad
            _, err = validate_ruleset(rules)
            self.assertIsNotNone(err, bad)

    def test_old_stored_rules_without_turnover_load_as_no_gate(self):
        # A bot created before the turnover gate stores the old
        # floors and no min_turnover_ratio key — it loads as 0.0
        # (no gate), never as a guessed ratio, and its old floors
        # are preserved as stored, not rewritten. That is the
        # preset-propagation rule in action: an existing bot
        # keeps its old rules until it is deleted and recreated
        # from the new preset.
        rules = middy_ruleset()
        rules["buy_filter"]["min_volume_5m_usd"] = 175.0
        rules["buy_filter"]["min_market_cap_usd"] = 12000.0
        del rules["buy_filter"]["min_turnover_ratio"]
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err, err)
        self.assertEqual(
            cleaned["buy_filter"]["min_turnover_ratio"], 0.0)
        self.assertEqual(
            cleaned["buy_filter"]["min_volume_5m_usd"], 175.0)
        self.assertEqual(
            cleaned["buy_filter"]["min_market_cap_usd"], 12000.0)

    def test_preset_list_ends_with_middy(self):
        presets = preset_bots()
        self.assertEqual(presets[-1]["key"], "middy")
        self.assertEqual(presets[-1]["category"], MIDDY_CATEGORY)
        self.assertEqual(presets[-1]["endpoint"], "bot_preset_middy")

    def test_summary_is_honest_about_the_backstop(self):
        summary = summarize_rules(middy_ruleset())
        self.assertIn("Sell ladder: none in effect", summary)
        self.assertIn("Take profit sells ALL", summary)
        self.assertIn("120 seconds", summary)
        self.assertIn("20% of the starting balance", summary)
        self.assertIn("5 minutes", summary)
        # The flat floors are gone (2026-11-26): the summary
        # states the turnover gate, never a $ volume or mcap
        # floor that no longer applies.
        self.assertIn("turnover", summary)
        self.assertIn("0.01", summary)
        self.assertNotIn("$17,000 CAD", summary)
        self.assertNotIn("$12,000 USD", summary)
        self.assertNotIn("$175", summary)
        self.assertNotIn("Sell ladder: sell 100%", summary)

    def test_notes_name_the_not_enforced_rules(self):
        joined = "\n".join(MIDDY_NOTES)
        self.assertIn("NOT enforced", joined)
        self.assertIn("1 second", joined)
        self.assertIn("80%", joined)
        self.assertIn("paper first", joined.lower())
        # The old floors are gone — the notes say so (named only
        # as the removed floors), and name the turnover gate and
        # the Block-0 gap instead.
        self.assertNotIn("$12,000 USD", joined)
        self.assertNotIn("fixed-rate", joined)
        self.assertIn("are GONE", joined)
        self.assertIn("0.01", joined)
        self.assertIn("bundled", joined.lower())


class TestMiddyHardExits(MiddyBase):
    def test_hard_stop_at_minus_5_5_pct(self):
        self.assertEqual(
            _middy_hard_exit_reason(1.0, 1.0, 0.945), "stop_loss")

    def test_above_the_stop_does_not_fire(self):
        self.assertIsNone(_middy_hard_exit_reason(1.0, 1.0, 0.946))

    def test_trailing_stop_at_minus_8_pct_from_peak(self):
        self.assertEqual(
            _middy_hard_exit_reason(1.0, 1.10, 1.012),
            "trailing stop")

    def test_above_the_trail_does_not_fire(self):
        self.assertIsNone(_middy_hard_exit_reason(1.0, 1.10, 1.013))

    def test_rising_coin_never_exits(self):
        self.assertIsNone(_middy_hard_exit_reason(1.0, 1.05, 1.05))

    def test_no_breakeven_move(self):
        # A coin that ran to +5% and fell back to entry still
        # holds — Middy has no breakeven stop, only the -5.5%
        # hard stop and the -8% trail from the peak. (A coin that
        # ran to +20% and fell back to entry IS out — that is the
        # -8% trail from the peak, not a breakeven move.)
        self.assertIsNone(_middy_hard_exit_reason(1.0, 1.05, 1.00))
        self.assertEqual(
            _middy_hard_exit_reason(1.0, 1.20, 1.00), "trailing stop")
        self.assertEqual(
            _middy_hard_exit_reason(1.0, 1.20, 0.945), "stop_loss")


class TestMiddyEntryGates(MiddyBase):
    def test_clean_coin_passes(self):
        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_buys_equal_to_sells_fail(self):
        # A tie does NOT beat sells — buys must strictly exceed
        # sells (James, 2026-11-25: "Buys just beat sells").
        ok, why = _middy_entry_gates(
            self.db, self.cfg,
            _middy_entry(MIDDY_MINT, 0.01, buys=100, sells=100),
            self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("must beat sells", why)

    def test_buys_one_more_than_sells_pass(self):
        # Any positive edge passes: 101 buys vs 100 sells.
        ok, why = _middy_entry_gates(
            self.db, self.cfg,
            _middy_entry(MIDDY_MINT, 0.01, buys=101, sells=100),
            self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_buys_above_sells_pass(self):
        ok, why = _middy_entry_gates(
            self.db, self.cfg,
            _middy_entry(MIDDY_MINT, 0.01, buys=135, sells=100),
            self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_missing_txn_counts_fail_closed(self):
        entry = _middy_entry(MIDDY_MINT, 0.01)
        entry["txns_m5_buys"] = None
        ok, why = _middy_entry_gates(
            self.db, self.cfg, entry, self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("no 5-minute txn data", why)

    def test_devnet_watchlist_skips_the_gate(self):
        entry = _middy_entry(MIDDY_MINT, 0.01, devnet_watchlist=True)
        entry["txns_m5_buys"] = None
        entry["txns_m5_sells"] = None
        ok, why = _middy_entry_gates(
            self.db, self.cfg, entry, self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_lifecycle_fresh_coin_passes(self):
        # 5 minutes old, 30% curve (the fake RPC default) —
        # inside both halves of the lifecycle window.
        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_lifecycle_old_coin_rejected(self):
        # 11 minutes old — past the 10-minute window.
        ok, why = _middy_entry_gates(
            self.db, self.cfg,
            _middy_entry(MIDDY_MINT, 0.01, age_minutes=11.0),
            self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("older than 10 minutes", why)

    def test_lifecycle_at_10_minutes_rejected(self):
        # Boundary: exactly 10 minutes old is NOT under 10 minutes.
        ok, why = _middy_entry_gates(
            self.db, self.cfg,
            _middy_entry(MIDDY_MINT, 0.01, age_minutes=10.0),
            self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("older than 10 minutes", why)

    def test_lifecycle_missing_creation_time_fails_closed(self):
        entry = _middy_entry(MIDDY_MINT, 0.01)
        entry["pair_created_at"] = None
        ok, why = _middy_entry_gates(
            self.db, self.cfg, entry, self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("no pair creation time", why)

    def test_lifecycle_future_creation_time_rejected(self):
        entry = _middy_entry(MIDDY_MINT, 0.01)
        entry["pair_created_at"] = _now_ms() + 60_000
        ok, why = _middy_entry_gates(
            self.db, self.cfg, entry, self.fake_rpc, {})
        self.assertFalse(ok)
        self.assertIn("in the future", why)

    def test_lifecycle_seconds_epoch_creation_time_accepted(self):
        # A seconds-epoch creation time is normalized to ms, not
        # mistaken for a 1970 coin.
        entry = _middy_entry(MIDDY_MINT, 0.01)
        entry["pair_created_at"] = (_now_ms() - 300_000) // 1000
        ok, why = _middy_entry_gates(
            self.db, self.cfg, entry, self.fake_rpc, {})
        self.assertTrue(ok, why)

    def test_curve_above_75_pct_rejected(self):
        rpc = _FakeRpc(MIDDY_MINT, progress_pct=80.0)
        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            rpc, {})
        self.assertFalse(ok)
        self.assertIn("80.0% filled", why)

    def test_curve_at_75_pct_rejected(self):
        # Boundary: exactly 75% is NOT under 75%.
        rpc = _FakeRpc(MIDDY_MINT, progress_pct=75.0)
        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            rpc, {})
        self.assertFalse(ok)
        self.assertIn("75.0% filled", why)

    def test_curve_at_74_pct_passes(self):
        rpc = _FakeRpc(MIDDY_MINT, progress_pct=74.0)
        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            rpc, {})
        self.assertTrue(ok, why)

    def test_curve_unreadable_rejected_fail_closed(self):
        class _NoCurveRpc(_FakeRpc):
            def get_account_info(self, pubkey):
                if pubkey == self.curve_addr:
                    return None
                return super().get_account_info(pubkey)

        ok, why = _middy_entry_gates(
            self.db, self.cfg, _middy_entry(MIDDY_MINT, 0.01),
            _NoCurveRpc(MIDDY_MINT), {})
        self.assertFalse(ok)
        self.assertIn("curve", why)


class TestMiddyTakeProfit(MiddyBase):
    def _flow(self, buys=300, sells=100, change_5m=2.0):
        return {"txns_m5_buys": buys, "txns_m5_sells": sells,
                "change_5m_pct": change_5m}

    def test_mid_hold_when_buys_beat_sells(self):
        self.assertTrue(_middy_mid_hold(self._flow()))

    def test_mid_hold_fails_when_buys_do_not_beat_sells(self):
        # 100 buys vs 100 sells is a tie — a tie does not beat
        # sells. 90 vs 100 is below.
        self.assertFalse(_middy_mid_hold(self._flow(buys=100)))
        self.assertFalse(_middy_mid_hold(self._flow(buys=90)))

    def test_mid_hold_passes_when_falling_but_buys_beat_sells(self):
        # The still-gaining condition is REMOVED (James, 2026-11-25
        # "That one too"): a falling coin still holds while buys
        # beat sells — the hold test is buys > sells only.
        self.assertTrue(_middy_mid_hold(
            self._flow(buys=300, change_5m=-1.0)))

    def test_mid_hold_fails_on_missing_data(self):
        self.assertFalse(_middy_mid_hold({}))
        self.assertFalse(_middy_mid_hold(
            {"txns_m5_buys": None, "txns_m5_sells": 100}))
        self.assertFalse(_middy_mid_hold(
            {"txns_m5_buys": 300, "txns_m5_sells": None}))

    def test_mid_hold_ignores_missing_change(self):
        # The 5-minute change is no longer part of the hold test,
        # so a missing change no longer fails it.
        self.assertTrue(_middy_mid_hold(
            {"txns_m5_buys": 300, "txns_m5_sells": 100}))
        self.assertTrue(_middy_mid_hold(
            {"txns_m5_buys": 300, "txns_m5_sells": 100,
             "change_5m_pct": None}))

    def test_below_17_pct_never_sells(self):
        self.assertIsNone(_middy_take_profit_reason(
            self._flow(buys=100), 1.0, 1.16, 1.16))

    def test_plus_18_pct_sells_when_hold_test_fails(self):
        reason = _middy_take_profit_reason(
            self._flow(buys=100), 1.0, 1.18, 1.18)
        self.assertIsNotNone(reason)
        self.assertIn("small take-profit", reason)

    def test_plus_18_pct_holds_when_hold_test_passes(self):
        self.assertIsNone(_middy_take_profit_reason(
            self._flow(), 1.0, 1.18, 1.18))

    def test_plus_30_pct_holds_in_the_higher_window(self):
        # +30% is inside the higher window (25%-37%): with buys
        # beating sells, it holds for more.
        self.assertIsNone(_middy_take_profit_reason(
            self._flow(), 1.0, 1.30, 1.30))

    def test_plus_30_pct_sells_when_hold_test_fails(self):
        reason = _middy_take_profit_reason(
            self._flow(buys=100), 1.0, 1.30, 1.30)
        self.assertIsNotNone(reason)
        self.assertIn("higher window", reason)

    def test_37_pct_is_the_ceiling_even_with_a_strong_flow(self):
        # +37% with buys beating sells: the
        # ceiling sells anyway — secure the profit immediately
        # and move on, never hold past it.
        reason = _middy_take_profit_reason(
            self._flow(), 1.0, 1.37, 1.37)
        self.assertIsNotNone(reason)
        self.assertIn("ceiling", reason)

    def test_past_55_pct_always_sells_no_moon_hold(self):
        # The old +26-55% hold and past-+55% "bitcoin Jr." hold
        # are REMOVED (James, 2026-11-24 ~12:35): +60% sells even
        # with the strongest flow — the ceiling is absolute.
        reason = _middy_take_profit_reason(
            self._flow(), 1.0, 1.60, 1.60)
        self.assertIsNotNone(reason)
        self.assertIn("ceiling", reason)


class TestMiddySlow(MiddyBase):
    def test_flat_for_120s_fires(self):
        self.assertIsNotNone(_middy_slow_reason(
            0.0, {"change_5m_pct": 0.2}, 130.0))

    def test_under_120s_never_fires(self):
        self.assertIsNone(_middy_slow_reason(
            0.0, {"change_5m_pct": 0.2}, 60.0))

    def test_moving_coin_never_fires(self):
        self.assertIsNone(_middy_slow_reason(
            0.0, {"change_5m_pct": 2.0}, 130.0))
        self.assertIsNone(_middy_slow_reason(
            0.0, {"change_5m_pct": -2.0}, 130.0))

    def test_missing_data_never_fires(self):
        self.assertIsNone(_middy_slow_reason(0.0, {}, 130.0))
        self.assertIsNone(_middy_slow_reason(
            None, {"change_5m_pct": 0.2}, 130.0))


class TestMiddyBuyHalt(MiddyBase):
    def _bot_row(self, bot_id):
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()

    def test_down_20_pct_of_starting_balance_halts(self):
        # Day-start capital $100, realized -$25 today: past -20%
        # ($20) — no new buys until tomorrow.
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._snapshot(100.0)
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-25.0, mode="paper",
                     now=_now())
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, reason = _middy_buy_halt(
            self.db, self._bot_row(bot_id), now_ts, None,
            _empty_prices())
        self.assertTrue(halted)
        self.assertIn("daily-loss breaker", reason)
        self.assertIn("20%", reason)

    def test_down_less_than_20_pct_does_not_halt(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._snapshot(100.0)
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-10.0, mode="paper",
                     now=_now())
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, _reason = _middy_buy_halt(
            self.db, self._bot_row(bot_id), now_ts, None,
            _empty_prices())
        self.assertFalse(halted)

    def test_no_snapshot_cannot_trip_the_breaker(self):
        # No day-start capital on file: the breaker cannot be
        # evaluated and does NOT trip — documented fail-open.
        bot_id = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-25.0, mode="paper",
                     now=_now())
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, _reason = _middy_buy_halt(
            self.db, self._bot_row(bot_id), now_ts, None,
            _empty_prices())
        self.assertFalse(halted)

    def test_breaker_halts_paper_buys_too(self):
        # Unlike Pit (real only), Middy's breaker applies to
        # paper as well — the paper test measures the rule.
        bot_id = self._insert_bot(rules=middy_ruleset(), mode="paper")
        self._snapshot(100.0)
        record_trade(self.db, self.user_id, bot_id, "L0", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-25.0, mode="paper",
                     now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("daily-loss breaker" in a["reason"] for a in skips),
            skips)


class TestMiddyBuyPace(MiddyBase):
    def _bot_row(self, bot_id):
        return self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()

    def _record_buys(self, bot_id, count, offset_seconds=0):
        for i in range(count):
            record_trade(self.db, self.user_id, bot_id, f"B{i}",
                         "buy", 1.0, 0.01, 0.01, mode="paper",
                         now=_now(offset_seconds))

    def test_no_buys_in_the_last_hour_does_not_halt(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, _reason = _middy_pace_halt(
            self.db, self._bot_row(bot_id), now_ts)
        self.assertFalse(halted)

    def test_six_buys_in_the_last_hour_halts(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._record_buys(bot_id, 6)
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, reason = _middy_pace_halt(
            self.db, self._bot_row(bot_id), now_ts)
        self.assertTrue(halted)
        self.assertIn("buy pace", reason)
        self.assertIn("6 buys", reason)

    def test_buys_older_than_an_hour_do_not_count(self):
        # 6 buys, but 2 of them over an hour ago: only 4 in the
        # rolling window — no halt.
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._record_buys(bot_id, 2, offset_seconds=-3700)
        self._record_buys(bot_id, 4)
        now_ts = datetime.now(timezone.utc).timestamp()
        halted, _reason = _middy_pace_halt(
            self.db, self._bot_row(bot_id), now_ts)
        self.assertFalse(halted)

    def test_pace_halts_new_buys(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._record_buys(bot_id, 6)
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        skips = [a for o in outcomes for a in o["actions"]
                 if a["action"] == "skip"]
        self.assertTrue(
            any("buy pace" in a["reason"] for a in skips), skips)

    def test_five_buys_in_the_last_hour_still_buys(self):
        # The pace is a cap, never a quota: 5 in the window means
        # the 6th buy is still allowed — and the bot does NOT have
        # to hold all six.
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._record_buys(bot_id, 5)
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_pace_halts_buys_but_not_exits(self):
        # A pace-halted bot still stops out of an open position.
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._record_buys(bot_id, 6)
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01,
                              last_price=0.0095)
        outcomes = self._run([_middy_entry(MIDDY_MINT, 0.0094,
                                           change_5m=-6.0)])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("stop_loss", sells[0]["reason"])


class TestMiddyMinimum(MiddyBase):
    def _miss_rows(self):
        return self.db.execute(
            "SELECT * FROM activity WHERE user_id = ? "
            "AND kind = 'silent_breaker' AND detail LIKE ?",
            (self.user_id, "%middy minimum missed%")).fetchall()

    def test_missed_hour_is_logged_not_forced(self):
        # Fresh bot, no buys in the last hour, open capacity: the
        # miss is logged once — and no buy is forced.
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._run(
            [_middy_entry(MIDDY_MINT, 0.0106, change_5m=0.5)],
            feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        rows = self._miss_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("nothing was forced", rows[0]["detail"])

    def test_no_miss_logged_when_a_buy_happened(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        for i in range(1):
            record_trade(self.db, self.user_id, bot_id, "B0",
                         "buy", 1.0, 0.01, 0.01, mode="paper",
                         now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        self._run(
            [_middy_entry(MIDDY_MINT, 0.0106, change_5m=0.5)],
            feed=feed)
        self.assertEqual(self._miss_rows(), [])

    def test_no_miss_logged_when_max_open_reached(self):
        # 2 open positions and no buys: the bot could not buy
        # anyway, so no missed-hour log.
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01)
        self._insert_position(bot_id, MIDDY_MINT2, 100.0, 0.01)
        feed = _FakeFeed({"SOL": 71.50})
        self._run(
            [_middy_entry(MIDDY_MINT3, 0.0106, change_5m=0.5)],
            feed=feed)
        self.assertEqual(self._miss_rows(), [])

    def test_miss_logged_at_most_once_per_hour(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        entry = _middy_entry(MIDDY_MINT, 0.0106, change_5m=0.5)
        self._run([entry], feed=feed)
        self._reset_scan(bot_id)
        self._run([entry], feed=feed)
        self.assertEqual(len(self._miss_rows()), 1)

    def test_buys_last_hour_counts_the_rolling_window(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        for i in range(2):
            record_trade(self.db, self.user_id, bot_id, f"B{i}",
                         "buy", 1.0, 0.01, 0.01, mode="paper",
                         now=_now())
        record_trade(self.db, self.user_id, bot_id, "BOLD",
                     "buy", 1.0, 0.01, 0.01, mode="paper",
                     now=_now(-3700))
        now_ts = datetime.now(timezone.utc).timestamp()
        self.assertEqual(
            _middy_buys_last_hour(self.db, bot_id, now_ts), 2)


class TestMiddyNeverRebuy(MiddyBase):
    def test_not_bought_before_returns_false(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self.assertFalse(
            _middy_already_bought(self.db, bot_id, MIDDY_MINT))

    def test_bought_once_returns_true(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, bot_id, MIDDY_MINT,
                     "buy", 1.0, 0.01, 0.01, mode="paper", now=_now())
        self.assertTrue(
            _middy_already_bought(self.db, bot_id, MIDDY_MINT))

    def test_never_buys_the_same_coin_twice(self):
        # Buy the coin once, close the position, then present it
        # again as a perfect candidate: no second buy, ever.
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        self._buy_twice(bot_id, _middy_entry(MIDDY_MINT, 0.0106),
                        feed=feed)
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))
        self.db.execute(
            "DELETE FROM bot_positions WHERE bot_id = ? AND coin_id = ?",
            (bot_id, MIDDY_MINT))
        self.db.commit()
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))
        row = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ? AND kind = "
            "'silent_skip' AND detail LIKE '%never rebuy%' "
            "ORDER BY id DESC LIMIT 1",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "no never-rebuy skip was logged")

    def test_profitable_previous_buy_still_never_rebuys(self):
        # Even a winning last trade does not earn a second buy:
        # the ledger holds a buy and a profitable sell, the coin
        # is done.
        bot_id = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, bot_id, MIDDY_MINT,
                     "buy", 1.0, 0.01, 0.01, mode="paper", now=_now())
        record_trade(self.db, self.user_id, bot_id, MIDDY_MINT,
                     "sell", 1.0, 0.02, 0.02, pnl_usd=5.0,
                     mode="paper", now=_now())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])

    def test_another_bot_copy_can_buy_the_same_coin(self):
        # The rule is per bot copy, not per coin: a second Middy
        # copy may buy a coin the first copy already traded.
        bot1 = self._insert_bot(rules=middy_ruleset(), name="Middy")
        record_trade(self.db, self.user_id, bot1, MIDDY_MINT,
                     "buy", 1.0, 0.01, 0.01, mode="paper", now=_now())
        bot2 = self._insert_bot(rules=middy_ruleset(), name="Middy")
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot2, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        self.assertEqual(len(self._buys(outcomes)), 1)


class TestMiddyCooldown(MiddyBase):
    def test_losing_close_sets_a_5_minute_cooldown(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        bot = self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        now_ts = datetime.now(timezone.utc).timestamp()
        _after_sell(self.db, bot, MIDDY_MINT, 100.0, 100.0,
                    pnl_usd=-1.0)
        row = self.db.execute(
            "SELECT blocked_until FROM bot_buy_cooldowns "
            "WHERE bot_id = ? AND mint = ?",
            (bot_id, MIDDY_MINT)).fetchone()
        self.assertIsNotNone(row, "no cooldown was recorded")
        blocked = datetime.strptime(
            row["blocked_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(blocked - now_ts, 300.0, delta=5.0)

    def test_other_kinds_keep_the_30_minute_cooldown(self):
        bot_id = self._insert_bot(rules=middy_ruleset(),
                                  name="Not Middy", category="Clyde")
        bot = self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        now_ts = datetime.now(timezone.utc).timestamp()
        _after_sell(self.db, bot, MIDDY_MINT, 100.0, 100.0,
                    pnl_usd=-1.0)
        row = self.db.execute(
            "SELECT blocked_until FROM bot_buy_cooldowns "
            "WHERE bot_id = ? AND mint = ?",
            (bot_id, MIDDY_MINT)).fetchone()
        self.assertIsNotNone(row, "no cooldown was recorded")
        blocked = datetime.strptime(
            row["blocked_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(blocked - now_ts, 1800.0, delta=5.0)

    def test_winning_close_sets_no_cooldown(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        bot = self.db.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        _after_sell(self.db, bot, MIDDY_MINT, 100.0, 100.0,
                    pnl_usd=1.0)
        row = self.db.execute(
            "SELECT blocked_until FROM bot_buy_cooldowns "
            "WHERE bot_id = ? AND mint = ?",
            (bot_id, MIDDY_MINT)).fetchone()
        self.assertIsNone(row)


class TestMiddyIntegration(MiddyBase):
    def test_first_scan_only_confirms_no_buy(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._run([_middy_entry(MIDDY_MINT, 0.0106)])
        self.assertEqual(self._buys(outcomes), [])
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))

    def test_second_scan_buys_20_pct_of_bankroll(self):
        # Live SOL $71.50: 20% of the $100 bankroll = $20.00.
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106), feed=feed)
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0]["usd"], 20.0, places=2)
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))

    def test_below_1_pct_gain_never_buys(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106, change_5m=0.9))
        self.assertEqual(self._buys(outcomes), [])

    def test_low_mcap_no_longer_blocks_when_turnover_passes(self):
        # The flat floors are GONE (2026-11-26): a $11,000 coin
        # is no longer blocked by a mcap floor. Its turnover is
        # 5000 / 11000 = 0.454 — far above 0.01 — so it buys.
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106, mcap=11000))
        self.assertEqual(len(self._buys(outcomes)), 1)
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))

    def test_19k_mcap_buys_turnover_passes(self):
        # Firefly (2026-11-24): $19,260 USD was skipped by the old
        # $20,000 USD bar, but $19,260 USD is about $27,500 CAD —
        # above James's $20,000 CAD rule. Under the turnover gate
        # a $19,000 coin with $5,000 of 5-minute volume has
        # turnover 0.263 — above 0.01 — so it buys.
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106, mcap=19000))
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))

    def test_dominance_tie_never_buys(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106,
                                 buys=100, sells=100))
        self.assertEqual(self._buys(outcomes), [])

    def test_low_turnover_never_buys(self):
        # The turnover gate (2026-11-26) replaces the old $175
        # volume floor: $100 of 5-minute volume on a $25,000 coin
        # is turnover 0.004 — below 0.01 — so it never buys.
        # (volume_h1 is set so the momentum gate would pass —
        # only the turnover gate blocks this buy.)
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106, volume_m5=100,
                                 volume_h1=1000))
        self.assertEqual(self._buys(outcomes), [])

    def test_turnover_at_0_01_passes(self):
        # Exactly 0.01 turnover meets the gate: $250 of 5-minute
        # volume on a $25,000 coin. The momentum gate passes
        # (250 > 2000/12 hourly pace).
        bot_id = self._insert_bot(rules=middy_ruleset())
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT, 0.0106, volume_m5=250,
                                 volume_h1=2000))
        self.assertEqual(len(self._buys(outcomes)), 1)

    def test_max_2_open_blocks_a_third_buy(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        self._buy_twice(bot_id, _middy_entry(MIDDY_MINT, 0.0106),
                        feed=feed)
        self._buy_twice(bot_id, _middy_entry(MIDDY_MINT2, 0.0106),
                        feed=feed)
        outcomes = self._buy_twice(
            bot_id, _middy_entry(MIDDY_MINT3, 0.0106), feed=feed)
        self.assertEqual(self._buys(outcomes), [])

    def test_take_profit_sells_all_when_hold_test_fails(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01)
        # +18% with buys 100 vs sells 100: the hold test fails —
        # sell all at the small take-profit level (+17%).
        # The 5-minute change now comes from the loop's own marks.
        self._insert_mark(bot_id, MIDDY_MINT, 0.0118 / 1.1505, _now(-330))
        entry = _middy_entry(MIDDY_MINT, 0.0118, change_5m=15.0,
                             buys=100, sells=100)
        outcomes = self._run([entry])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("take profit", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))

    def test_take_profit_holds_when_hold_test_passes(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01)
        # +18% with buys 300 vs sells 100: the hold test passes
        # (buys beat sells) — hold for the mid level, even though
        # the small take-profit level (+17%) is reached.
        # The 5-minute change now comes from the loop's own marks.
        self._insert_mark(bot_id, MIDDY_MINT, 0.0118 / 1.1505, _now(-330))
        entry = _middy_entry(MIDDY_MINT, 0.0118, change_5m=15.0,
                             buys=300, sells=100)
        outcomes = self._run([entry])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))

    def test_ceiling_sells_even_with_a_strong_flow(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01)
        # +37% with buys 300 vs sells 100: at the ceiling — secure
        # the profit immediately and move on. The ceiling is
        # absolute, no chasing higher.
        # The 5-minute change now comes from the loop's own marks.
        self._insert_mark(bot_id, MIDDY_MINT, 0.0137 / 1.1505, _now(-330))
        entry = _middy_entry(MIDDY_MINT, 0.0137, change_5m=15.0,
                             buys=300, sells=100)
        outcomes = self._run([entry])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("ceiling", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))

    def test_higher_window_holds_with_a_strong_flow(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01)
        # +30% with buys 300 vs sells 100: inside the higher
        # window (25%-37%) with the hold test passing — hold for
        # more.
        # The 5-minute change now comes from the loop's own marks.
        self._insert_mark(bot_id, MIDDY_MINT, 0.013 / 1.1505, _now(-330))
        entry = _middy_entry(MIDDY_MINT, 0.013, change_5m=15.0,
                             buys=300, sells=100)
        outcomes = self._run([entry])
        self.assertEqual(self._sells(outcomes), [])
        self.assertIsNotNone(self._position(bot_id, MIDDY_MINT))

    def test_slow_coin_sells_after_120s_flat(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01,
                              opened_at=_now(-130))
        # The 5-minute change now comes from the loop's own marks.
        self._insert_mark(bot_id, MIDDY_MINT, 0.01 / 1.0025, _now(-330))
        entry = _middy_entry(MIDDY_MINT, 0.01, change_5m=0.2)
        outcomes = self._run([entry])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("slow coin", sells[0]["reason"])
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))

    def test_hard_stop_sells_before_the_slow_rule(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01,
                              opened_at=_now(-130))
        # -6% and flat: the hard stop fires, not the slow rule.
        entry = _middy_entry(MIDDY_MINT, 0.0094, change_5m=0.0)
        outcomes = self._run([entry])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("stop_loss", sells[0]["reason"])

    def test_trailing_stop_sells_from_the_peak(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        self._insert_position(bot_id, MIDDY_MINT, 100.0, 0.01,
                              peak=0.011)
        # 0.0101 is -8.2% from the 0.011 peak.
        entry = _middy_entry(MIDDY_MINT, 0.0101, change_5m=1.0)
        outcomes = self._run([entry])
        sells = self._sells(outcomes)
        self.assertEqual(len(sells), 1)
        self.assertIn("trailing stop", sells[0]["reason"])


class TestMiddySelectivity(MiddyBase):
    def test_strongest_gainer_buys_first(self):
        # Two candidates, both passing every gate: at most one
        # buy per scan, and it is the strongest gainer — the one
        # the market is going crazy for.
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        strong = _middy_entry(MIDDY_MINT, 0.0106, change_5m=5.0)
        weak = _middy_entry(MIDDY_MINT2, 0.0106, change_5m=2.0)
        self._insert_mark(bot_id, MIDDY_MINT, 0.0106 / 1.0505, _now(-330))
        self._insert_mark(bot_id, MIDDY_MINT2, 0.0106 / 1.0205, _now(-330))
        self._run([strong, weak], feed=feed)
        self._reset_scan(bot_id)
        outcomes = self._run([strong, weak], feed=feed)
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], MIDDY_MINT)
        self.assertIsNone(self._position(bot_id, MIDDY_MINT2))

    def test_biggest_spike_with_weak_buys_is_skipped(self):
        # The strongest gainer has buys 100 vs sells 100 — no
        # buy dominance, no momentum — so it is filtered, and the
        # weaker coin with real buy pressure is bought instead.
        # Selectivity is the buy pressure, not the price spike.
        bot_id = self._insert_bot(rules=middy_ruleset())
        feed = _FakeFeed({"SOL": 71.50})
        spike = _middy_entry(MIDDY_MINT, 0.0106, change_5m=8.0,
                             buys=100, sells=100)
        real = _middy_entry(MIDDY_MINT2, 0.0106, change_5m=2.0)
        self._insert_mark(bot_id, MIDDY_MINT, 0.0106 / 1.0805, _now(-330))
        self._insert_mark(bot_id, MIDDY_MINT2, 0.0106 / 1.0205, _now(-330))
        self._run([spike, real], feed=feed)
        self._reset_scan(bot_id)
        outcomes = self._run([spike, real], feed=feed)
        buys = self._buys(outcomes)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["coin"], MIDDY_MINT2)
        self.assertIsNone(self._position(bot_id, MIDDY_MINT))


class TestMiddyWlSummary(MiddyBase):
    def test_site_wide_sums_every_middy_copy(self):
        middy1 = self._insert_bot(rules=middy_ruleset(), name="Middy")
        middy2 = self._insert_bot(rules=middy_ruleset(), name="Middy")
        clyde = self._insert_bot(rules=middy_ruleset(), name="Clyde",
                                 category="Clyde")
        record_trade(self.db, self.user_id, middy1, "A", "sell",
                     1.0, 90.0, 90.0, pnl_usd=5.0, mode="paper",
                     now=_now())
        record_trade(self.db, self.user_id, middy1, "B", "sell",
                     1.0, 90.0, 90.0, pnl_usd=-2.0, mode="paper",
                     now=_now())
        record_trade(self.db, self.user_id, middy2, "C", "sell",
                     1.0, 90.0, 90.0, pnl_usd=3.0, mode="paper",
                     now=_now())
        record_trade(self.db, self.user_id, clyde, "D", "sell",
                     1.0, 90.0, 90.0, pnl_usd=100.0, mode="paper",
                     now=_now())
        summary = kind_wl_summary(self.db, MIDDY_CATEGORY)
        self.assertEqual(summary["trades"], 3)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(summary["net_pnl_usd"], 6.0, places=2)
        self.assertEqual(summary["bots"], 2)

    def test_paper_filter_excludes_real_trades(self):
        middy = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, middy, "A", "sell",
                     1.0, 90.0, 90.0, pnl_usd=5.0, mode="paper",
                     now=_now())
        record_trade(self.db, self.user_id, middy, "B", "sell",
                     1.0, 90.0, 90.0, pnl_usd=50.0, mode="real",
                     now=_now())
        paper = kind_wl_summary(self.db, MIDDY_CATEGORY, mode="paper")
        self.assertEqual(paper["trades"], 1)
        self.assertAlmostEqual(paper["net_pnl_usd"], 5.0, places=2)
        real = kind_wl_summary(self.db, MIDDY_CATEGORY, mode="real")
        self.assertEqual(real["trades"], 1)
        self.assertAlmostEqual(real["net_pnl_usd"], 50.0, places=2)

    def test_empty_kind_is_zero_not_a_guess(self):
        summary = kind_wl_summary(self.db, MIDDY_CATEGORY)
        self.assertEqual(summary["trades"], 0)
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 0)
        self.assertIsNone(summary["win_rate"])
        self.assertEqual(summary["net_pnl_usd"], 0.0)
        self.assertEqual(summary["bots"], 0)

    def test_break_even_counts_as_neither(self):
        middy = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, middy, "A", "sell",
                     1.0, 90.0, 90.0, pnl_usd=0.0, mode="paper",
                     now=_now())
        summary = kind_wl_summary(self.db, MIDDY_CATEGORY)
        self.assertEqual(summary["trades"], 1)
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 0)
        self.assertIsNone(summary["win_rate"])


class TestMiddyRoutes(MiddyBase):
    def test_create_route_makes_a_paper_silent_off_bot(self):
        token = csrf(self.client, "/bots")
        resp = self.client.post(
            "/bots/preset/middy", data={"csrf_token": token},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE category = ?",
            (MIDDY_CATEGORY,)).fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["silent"], 0)
        self.assertEqual(bot["name"], "Middy")
        self.assertAlmostEqual(float(bot["buy_size_pct"]), 20.0)
        self.assertAlmostEqual(float(bot["stop_loss_pct"]), 5.5)
        self.assertEqual(bot["max_open_trades"], 2)
        self.assertEqual(bot["platform"], "pump.fun")
        rules, err = validate_ruleset(json.loads(bot["rules_json"]))
        self.assertIsNone(err, err)
        self.assertEqual(rules["kind"], "middy")

    def test_second_create_redirects_to_the_existing_bot(self):
        token = csrf(self.client, "/bots")
        self.client.post("/bots/preset/middy",
                         data={"csrf_token": token},
                         follow_redirects=False)
        token = csrf(self.client, "/bots")
        resp = self.client.post(
            "/bots/preset/middy", data={"csrf_token": token},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        count = self.db.execute(
            "SELECT COUNT(*) FROM bots WHERE category = ?",
            (MIDDY_CATEGORY,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_bots_page_shows_the_middy_card(self):
        resp = self.client.get("/bots")
        html = resp.get_data(as_text=True)
        self.assertIn("Middy", html)
        self.assertIn("No paper trades yet", html)

    def test_bots_page_shows_paper_results_once_they_exist(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, bot_id, "A", "sell",
                     1.0, 90.0, 90.0, pnl_usd=5.0, mode="paper",
                     now=_now())
        resp = self.client.get("/bots")
        html = resp.get_data(as_text=True)
        self.assertIn("Paper:", html)
        self.assertIn("1W", html)

    def test_bot_detail_shows_the_site_wide_aggregate(self):
        bot_id = self._insert_bot(rules=middy_ruleset())
        record_trade(self.db, self.user_id, bot_id, "A", "sell",
                     1.0, 90.0, 90.0, pnl_usd=5.0, mode="paper",
                     now=_now())
        resp = self.client.get(f"/bots/{bot_id}")
        html = resp.get_data(as_text=True)
        self.assertIn("All Middy bots site-wide", html)
        self.assertIn("1 wins", html)


if __name__ == "__main__":
    unittest.main()
