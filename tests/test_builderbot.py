"""builderBot: chatbox builder, identity, scrubber, ledger, W/L,
auto-adjustment, top-10 concentration triggers, 15-second cadence."""

import json
import re
import tempfile
import unittest

from helpers import csrf, make_client, signup
from tradingbot import builderbot as bb
from tradingbot.config import Config
from tradingbot.db import init_db
from tradingbot.ledger import (bot_wl_summary, record_trade,
                               recent_trades)
from tradingbot.paper import paper_cash
from tradingbot.rules import validate_ruleset
from tradingbot.scanner import (BUY_FILTER_DEFAULT,
                                check_top10_concentration,
                                passes_buy_filter, top10_share)
from tradingbot.scrubber import REDACTED, scrub_text
from tradingbot.security import decrypt_secret
from tradingbot.silent_loop import top10_watch_once

KEYPAIR_JSON = "[" + ", ".join(str(i % 256) for i in range(64)) + "]"
LONG_BASE58 = "5" * 88  # 64-byte secret as base58
HEX_SECRET = "ab" * 64
SEED_PHRASE = ("abandon ability able about above absent absorb abstract "
               "absurd abuse access accident")
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"  # a normal 44-char Solana mint: never redacted


def _enc_key():
    return bytes.fromhex("ab" * 32)


class TestScrubber(unittest.TestCase):
    def test_keypair_json_redacted(self):
        cleaned, n = scrub_text(f"my key is {KEYPAIR_JSON} please use it")
        self.assertNotIn("0, 1, 2", cleaned)
        self.assertIn(REDACTED, cleaned)
        self.assertEqual(n, 1)

    def test_long_base58_redacted(self):
        cleaned, n = scrub_text(f"secret {LONG_BASE58}")
        self.assertNotIn(LONG_BASE58, cleaned)
        self.assertEqual(n, 1)

    def test_hex_secret_redacted(self):
        cleaned, _n = scrub_text(f"here {HEX_SECRET}")
        self.assertNotIn(HEX_SECRET, cleaned)

    def test_pem_block_redacted(self):
        pem = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B"
               "AQEFAASC\n-----END PRIVATE KEY-----")
        cleaned, _n = scrub_text(pem)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0B", cleaned)
        self.assertNotIn("BEGIN PRIVATE KEY", cleaned)

    def test_seed_phrase_alone_redacted(self):
        cleaned, n = scrub_text(SEED_PHRASE)
        self.assertEqual(cleaned, REDACTED)
        self.assertEqual(n, 1)

    def test_labelled_password_redacted(self):
        cleaned, _n = scrub_text("my password: hunter2 please")
        self.assertNotIn("hunter2", cleaned)

    def test_email_and_phone_redacted(self):
        cleaned, _n = scrub_text(
            "reach me at jjgordon123@hotmail.com or 236-622-1628")
        self.assertNotIn("jjgordon123@hotmail.com", cleaned)
        self.assertNotIn("236-622-1628", cleaned)

    def test_normal_answers_untouched(self):
        for text in ("5% in 5 minutes", "Luna", "$10,000", MINT,
                     "2026-11-09", "pump.fun", "yes, both", "7%"):
            cleaned, n = scrub_text(text)
            self.assertEqual(cleaned, text, text)
            self.assertEqual(n, 0, text)

    def test_scrub_is_idempotent(self):
        cleaned, _n = scrub_text(f"key {KEYPAIR_JSON}")
        cleaned2, n2 = scrub_text(cleaned)
        self.assertEqual(cleaned, cleaned2)
        self.assertEqual(n2, 0)


class TestNoKeyAccess(unittest.TestCase):
    def test_builderbot_never_touches_wallet_keys(self):
        # builderBot never calls the wallet-key decryptors, never
        # reads a wallet secret column, and never queries a wallet
        # table. The ONLY decrypt call in the module is decrypt_secret
        # on its OWN chat-state blob (which it encrypted itself) in
        # _load_state — it never receives or decrypts wallet key
        # material; only the signing bot (executor) touches keys.
        with open("tradingbot/builderbot.py") as f:
            source = f.read()
        self.assertNotIn("decrypt_secret_mutable", source)
        self.assertNotIn("temporary_secret", source)
        self.assertNotIn("encrypted_secret", source)
        self.assertNotIn("FROM wallets", source)
        self.assertNotIn("platform_wallets", source)
        self.assertEqual(source.count("decrypt_secret("), 1)

    def test_scrubber_and_ledger_decrypt_nothing(self):
        for module in ("scrubber.py", "ledger.py"):
            with open(f"tradingbot/{module}") as f:
                source = f.read()
            self.assertNotIn("decrypt_secret", source, module)
            self.assertNotIn("temporary_secret", source, module)
            self.assertNotIn("encrypted_secret", source, module)

    def test_builderbot_identity_has_no_update_path(self):
        with open("tradingbot/builderbot.py") as f:
            source = f.read()
        self.assertNotIn("UPDATE builderbots", source)
        self.assertIn("INSERT INTO builderbots", source)


class BuilderBotBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_db(f"{self.tmp.name}/test.db")
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt, "
            "created_at) VALUES ('alice', 'h', 's', "
            "'2026-01-01T00:00:00Z')")
        self.db.execute(
            "INSERT INTO paper_portfolio (user_id, cash_usd, updated_at) "
            "VALUES (1, 100, '2026-01-01T00:00:00Z')")
        self.db.commit()
        self.user_id = 1
        self.enc_key = _enc_key()

    def tearDown(self):
        self.tmp.cleanup()


class TestIdentity(BuilderBotBase):
    def test_eight_digit_id(self):
        identity = bb.ensure_builderbot(self.db, self.user_id)
        self.assertTrue(re.fullmatch(r"[1-9]\d{7}",
                                     identity["builder_id"]))

    def test_idempotent(self):
        first = bb.ensure_builderbot(self.db, self.user_id)
        second = bb.ensure_builderbot(self.db, self.user_id)
        self.assertEqual(first["builder_id"], second["builder_id"])

    def test_unique_per_user(self):
        self.db.execute(
            "INSERT INTO users (username, password_hash, salt, "
            "created_at) VALUES ('bob', 'h', 's', '2026-01-01T00:00:00Z')")
        self.db.commit()
        first = bb.ensure_builderbot(self.db, 1)
        second = bb.ensure_builderbot(self.db, 2)
        self.assertNotEqual(first["builder_id"], second["builder_id"])


class TestConversation(BuilderBotBase):
    ANSWERS = ["Luna", "pump.fun", "Paper", "$10,000", "5% in 5 minutes",
               "20%", "3", "7%", "Default ladder", "7%", "Yes, both",
               "1 minute"]

    def _run_conversation(self, answers=None):
        state = bb.start_conversation(self.db, self.user_id, self.enc_key)
        self.assertEqual(len(state["messages"]), 2)
        result = None
        for answer in (answers or self.ANSWERS):
            result = bb.answer(self.db, self.user_id, self.enc_key,
                               answer)
        return result

    def test_full_flow_creates_trenches_bot(self):
        result = self._run_conversation()
        # The last answer shows the confirmation, not the bot yet.
        self.assertTrue(result["confirm"])
        self.assertIn("Luna", result["messages"][-1]["text"])
        result = bb.answer(self.db, self.user_id, self.enc_key, "yes")
        self.assertIn("created", result)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(bot["mode"], "paper")
        self.assertEqual(bot["platform"], "pump.fun")
        rules = json.loads(bot["rules_json"])
        self.assertEqual(rules["kind"], "trenches")
        self.assertEqual(rules["buy"]["threshold_pct"], 5.0)
        self.assertEqual(rules["buy"]["window_seconds"], 300)
        self.assertEqual(rules["buy_filter"]["min_market_cap_usd"],
                         10000.0)
        self.assertEqual(rules["top10_rug"]["concentration_pct"], 21.0)
        self.assertEqual(rules["top10_rug"]["slow_climb_sell_pct"], 50.0)
        self.assertEqual(rules["top10_rug"]["check_interval_seconds"],
                         15.0)
        self.assertEqual(rules["scan_interval_seconds"], 60)
        # Working memory is wiped the moment the bot is created.
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM builderbot_chats WHERE user_id = ?",
            (self.user_id,)).fetchone())

    def test_scanner_flow(self):
        answers = list(self.ANSWERS)
        answers[4] = "5% in 3 minutes"
        answers[11] = "30 seconds"
        self._run_conversation(answers)
        result = bb.answer(self.db, self.user_id, self.enc_key, "yes")
        self.assertIn("created", result)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        rules = json.loads(bot["rules_json"])
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["scan_interval_seconds"], 30)

    def test_invalid_answer_asks_again(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Luna")
        bb.answer(self.db, self.user_id, self.enc_key, "pump.fun")
        bb.answer(self.db, self.user_id, self.enc_key, "Paper")
        bb.answer(self.db, self.user_id, self.enc_key, "$0")
        bb.answer(self.db, self.user_id, self.enc_key, "5% in 5 minutes")
        bb.answer(self.db, self.user_id, self.enc_key, "20%")
        bb.answer(self.db, self.user_id, self.enc_key, "3")
        result = bb.answer(self.db, self.user_id, self.enc_key, "banana")
        self.assertFalse(result["confirm"])
        self.assertIn("percent", result["messages"][-1]["text"])
        # The bad answer did not advance the conversation.
        state = bb._load_state(self.db, self.user_id, self.enc_key)
        self.assertEqual(state["answers"].get("stop_loss"), None)

    def test_no_at_confirm_cancels_and_wipes(self):
        self._run_conversation()
        result = bb.answer(self.db, self.user_id, self.enc_key, "no")
        self.assertTrue(result.get("cancelled"))
        self.assertIsNone(self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone())
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM builderbot_chats WHERE user_id = ?",
            (self.user_id,)).fetchone())

    def test_chat_state_encrypted_at_rest(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Luna")
        row = self.db.execute(
            "SELECT encrypted_state FROM builderbot_chats "
            "WHERE user_id = ?", (self.user_id,)).fetchone()
        self.assertNotIn("Luna", row["encrypted_state"])
        state = bb._load_state(self.db, self.user_id, self.enc_key)
        self.assertEqual(state["answers"]["name"], "Luna")

    def test_pasted_keypair_never_reaches_storage(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        result = bb.answer(self.db, self.user_id, self.enc_key,
                           KEYPAIR_JSON)
        # A bare keypair is not a name: refused, and the key is gone.
        self.assertIn("secret", result["messages"][-1]["text"])
        state = bb._load_state(self.db, self.user_id, self.enc_key)
        blob = json.dumps(state)
        self.assertNotIn(KEYPAIR_JSON, blob)
        self.assertNotIn("0, 1, 2, 3", blob)
        activity = self.db.execute(
            "SELECT detail FROM activity WHERE user_id = ?",
            (self.user_id,)).fetchall()
        for row in activity:
            self.assertNotIn(KEYPAIR_JSON, row["detail"])

    def test_second_scanner_refused(self):
        self.test_scanner_flow()  # first scanner bot exists now
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        answers = list(self.ANSWERS)
        answers[0] = "Sniper2"
        answers[4] = "5% in 3 minutes"
        answers[11] = "30 seconds"
        for answer in answers:
            result = bb.answer(self.db, self.user_id, self.enc_key, answer)
        result = bb.answer(self.db, self.user_id, self.enc_key, "yes")
        self.assertIn("already have a Scanner bot",
                      result["messages"][-1]["text"])
        self.assertIsNone(self.db.execute(
            "SELECT * FROM bots WHERE name = 'Sniper2'").fetchone())


class TestRules(unittest.TestCase):
    def test_market_cap_filter(self):
        entry = {"volume_m5_usd": 5000.0, "txns_m5_buys": 10,
                 "txns_m5_sells": 2, "change_5m_pct": 6.0,
                 "market_cap_usd": 50000.0}
        cfg = dict(BUY_FILTER_DEFAULT)
        cfg["min_market_cap_usd"] = 100000.0
        ok, why = passes_buy_filter(entry, cfg, 6.0)
        self.assertFalse(ok)
        self.assertIn("market cap", why)
        cfg["min_market_cap_usd"] = 10000.0
        self.assertTrue(passes_buy_filter(entry, cfg, 6.0)[0])
        # No market-cap data + a minimum set: fail-closed.
        entry2 = dict(entry)
        entry2.pop("market_cap_usd")
        self.assertFalse(passes_buy_filter(entry2, cfg, 6.0)[0])
        # No minimum: a coin with no market-cap data still passes.
        cfg["min_market_cap_usd"] = 0.0
        self.assertTrue(passes_buy_filter(entry2, cfg, 6.0)[0])

    def test_top10_keys_validate(self):
        base = {"version": 1, "kind": "scanner",
                "scan_sources": ["pump.fun"],
                "scan_interval_seconds": 30,
                "buy": {"metric": "gain_3m_pct", "window_seconds": 180,
                        "threshold_pct": 5.0, "pick": "strongest"},
                "sell_ladder": [{"pct_of_position": 25,
                                 "profit_pct": 3},
                                {"pct_of_position": 25,
                                 "profit_pct": 10},
                                {"pct_of_position": 25,
                                 "profit_pct": 25},
                                {"pct_of_position": 25,
                                 "profit_pct": 50}],
                "trailing_stop_pct": 7.0,
                "top10_rug": {"drop_pct": 10.0,
                              "concentration_pct": 21.0,
                              "slow_climb_sell_pct": 50.0,
                              "check_interval_seconds": 15.0},
                "buy_filter": dict(BUY_FILTER_DEFAULT)}
        cleaned, err = validate_ruleset(base)
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["top10_rug"]["concentration_pct"], 21.0)
        bad = json.loads(json.dumps(base))
        bad["top10_rug"]["concentration_pct"] = 150.0
        _cleaned, err = validate_ruleset(bad)
        self.assertIsNotNone(err)
        bad = json.loads(json.dumps(base))
        bad["top10_rug"]["check_interval_seconds"] = 5
        _cleaned, err = validate_ruleset(bad)
        self.assertIsNotNone(err)
        bad = json.loads(json.dumps(base))
        bad["top10_rug"]["slow_climb_sell_pct"] = 0.0
        cleaned, err = validate_ruleset(bad)
        self.assertIsNone(err, err)  # 0 means the slow climb is off
        bad = json.loads(json.dumps(base))
        bad["buy_filter"]["min_market_cap_usd"] = -5
        _cleaned, err = validate_ruleset(bad)
        self.assertIsNotNone(err)


class FakeTop10Client:
    """getTokenLargestAccounts + getTokenSupply with settable data."""

    def __init__(self, top10_total, supply):
        self.top10_total = top10_total
        self.supply = supply
        self.calls = 0

    def get_token_largest_accounts(self, mint):
        self.calls += 1
        return [{"amount": str(self.top10_total)}]

    def get_token_supply(self, mint):
        self.calls += 1
        return self.supply


class TestConcentration(BuilderBotBase):
    def _bot(self):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, coins, strategy, category, "
            "buy_size_pct, stop_loss_pct, max_open_trades, platform, "
            "mode, silent, rules_json, api_token_hash, created_at) "
            "VALUES "
            "(1, 'Scan', '[]', 'balanced', 'scanner', 20, 7, 3, "
            "'pump.fun', 'paper', 1, ?, 'x', '2026-01-01T00:00:00Z')",
            (json.dumps({"version": 1, "kind": "scanner",
                         "scan_sources": ["pump.fun"],
                         "scan_interval_seconds": 30,
                         "buy": {"metric": "gain_3m_pct",
                                 "window_seconds": 180,
                                 "threshold_pct": 5.0,
                                 "pick": "strongest"},
                         "sell_ladder": [{"pct_of_position": 100,
                                          "profit_pct": 10}],
                         "trailing_stop_pct": 7.0,
                         "top10_rug": {"drop_pct": 10.0,
                                       "concentration_pct": 21.0,
                                       "slow_climb_sell_pct": 50.0,
                                       "check_interval_seconds": 15.0},
                         "buy_filter": dict(BUY_FILTER_DEFAULT)}),))
        self.db.commit()
        return cur.lastrowid

    def test_share_at_21_sells_all(self):
        bot_id = self._bot()
        # First check only records the baseline.
        action, _r = check_top10_concentration(
            self.db, bot_id, MINT, 100_000, 1_000_000, 21.0, 50.0,
            "2026-01-01T00:00:00Z")
        self.assertIsNone(action)
        # 210,000 of 1,000,000 = 21% exactly.
        action, reason = check_top10_concentration(
            self.db, bot_id, MINT, 210_000, 1_000_000, 21.0, 50.0,
            "2026-01-01T00:00:15Z")
        self.assertEqual(action, "sell_all")
        self.assertIn("21.0%", reason)

    def test_slow_climb_sells_half_once(self):
        bot_id = self._bot()
        check_top10_concentration(self.db, bot_id, MINT, 100_000,
                                  1_000_000, 21.0, 50.0,
                                  "2026-01-01T00:00:00Z")
        # 12% -> 13% -> 14%: three consecutive rises above 10%.
        for i, total in enumerate((120_000, 130_000, 140_000)):
            action, reason = check_top10_concentration(
                self.db, bot_id, MINT, total, 1_000_000, 21.0, 50.0,
                f"2026-01-01T00:00:{15 * (i + 1):02d}Z")
        self.assertEqual(action, "sell_half")
        self.assertIn("50%", reason)
        # The streak reset: a fourth rise does not fire again.
        action, _r = check_top10_concentration(
            self.db, bot_id, MINT, 150_000, 1_000_000, 21.0, 50.0,
            "2026-01-01T00:01:00Z")
        self.assertIsNone(action)

    def test_non_rise_resets_streak(self):
        bot_id = self._bot()
        check_top10_concentration(self.db, bot_id, MINT, 100_000,
                                  1_000_000, 21.0, 50.0,
                                  "2026-01-01T00:00:00Z")
        check_top10_concentration(self.db, bot_id, MINT, 120_000,
                                  1_000_000, 21.0, 50.0,
                                  "2026-01-01T00:00:15Z")
        check_top10_concentration(self.db, bot_id, MINT, 130_000,
                                  1_000_000, 21.0, 50.0,
                                  "2026-01-01T00:00:30Z")
        # Flat: streak resets, so the next rise is only streak 1.
        action, _r = check_top10_concentration(
            self.db, bot_id, MINT, 130_000, 1_000_000, 21.0, 50.0,
            "2026-01-01T00:00:45Z")
        self.assertIsNone(action)
        action, _r = check_top10_concentration(
            self.db, bot_id, MINT, 140_000, 1_000_000, 21.0, 50.0,
            "2026-01-01T00:01:00Z")
        self.assertIsNone(action)

    def test_below_10_never_fires_slow_climb(self):
        bot_id = self._bot()
        check_top10_concentration(self.db, bot_id, MINT, 10_000,
                                  1_000_000, 21.0, 50.0,
                                  "2026-01-01T00:00:00Z")
        action = None
        for i, total in enumerate((20_000, 30_000, 40_000, 50_000)):
            action, _r = check_top10_concentration(
                self.db, bot_id, MINT, total, 1_000_000, 21.0, 50.0,
                f"2026-01-01T00:00:{15 * (i + 1):02d}Z")
        self.assertIsNone(action)

    def test_no_supply_never_guesses(self):
        bot_id = self._bot()
        action, reason = check_top10_concentration(
            self.db, bot_id, MINT, 500_000, None, 21.0, 50.0,
            "2026-01-01T00:00:00Z")
        self.assertIsNone(action)
        self.assertIsNone(reason)

    def test_top10_share_helper(self):
        client = FakeTop10Client(210_000, 1_000_000)
        total, supply, share = top10_share(client, MINT)
        self.assertEqual(total, 210_000)
        self.assertEqual(supply, 1_000_000)
        self.assertAlmostEqual(share, 21.0)


class TestLedger(BuilderBotBase):
    def test_trade_ids_unique_eight_digits(self):
        ids = set()
        for _ in range(20):
            ids.add(record_trade(self.db, 1, None, "SOL", "buy", 1.0,
                                 86.0, 86.0, mode="paper",
                                 now="2026-01-01T00:00:00Z"))
        self.assertEqual(len(ids), 20)
        for trade_id in ids:
            self.assertTrue(re.fullmatch(r"[1-9]\d{7}", trade_id))

    def test_wl_summary_math(self):
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, coins, strategy, "
            "category, buy_size_pct, stop_loss_pct, max_open_trades, "
            "platform, mode, silent, rules_json, api_token_hash, "
            "created_at) VALUES "
            "(1, 'Luna', '[]', 'balanced', '', 20, 7, 3, 'pump.fun', "
            "'paper', 0, '', 'x', '2026-01-01T00:00:00Z')")
        self.db.commit()
        bot_id = cur.lastrowid
        # 2 wins, 1 loss, 1 break-even (neither).
        record_trade(self.db, 1, bot_id, "A", "sell", 1.0, 110.0, 110.0,
                     pnl_usd=10.0, mode="paper",
                     now="2026-01-01T00:00:00Z")
        record_trade(self.db, 1, bot_id, "B", "sell", 1.0, 120.0, 120.0,
                     pnl_usd=20.0, mode="paper",
                     now="2026-01-01T00:01:00Z")
        record_trade(self.db, 1, bot_id, "C", "sell", 1.0, 90.0, 90.0,
                     pnl_usd=-10.0, mode="paper",
                     now="2026-01-01T00:02:00Z")
        record_trade(self.db, 1, bot_id, "D", "sell", 1.0, 100.0, 100.0,
                     pnl_usd=0.0, mode="paper",
                     now="2026-01-01T00:03:00Z")
        record_trade(self.db, 1, bot_id, "E", "buy", 1.0, 100.0, 100.0,
                     mode="paper", now="2026-01-01T00:04:00Z")
        summary = bot_wl_summary(self.db, bot_id)
        self.assertEqual(summary["trades"], 4)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3)
        self.assertAlmostEqual(summary["net_pnl_usd"], 20.0)
        trades = recent_trades(self.db, bot_id)
        self.assertEqual(len(trades), 5)
        self.assertEqual(trades[0]["side"], "buy")


class TestAutoAdjust(BuilderBotBase):
    def _bot(self, stop_loss=7.0, buy_size=20.0, threshold=5.0):
        rules = {"version": 1, "kind": "trenches",
                 "scan_sources": ["pump.fun"],
                 "scan_interval_seconds": 60,
                 "buy": {"metric": "gain_5m_pct", "window_seconds": 300,
                         "threshold_pct": threshold, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 10,
                                  "profit_pct": 3},
                                 {"pct_of_position": 90,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0,
                 "buy_filter": dict(BUY_FILTER_DEFAULT)}
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, coins, strategy, category, "
            "buy_size_pct, stop_loss_pct, max_open_trades, platform, "
            "mode, silent, rules_json, api_token_hash, created_at) "
            "VALUES "
            "(1, 'Loser', '[]', 'balanced', 'Memecoin build', ?, ?, 3, "
            "'pump.fun', 'paper', 1, ?, 'x', '2026-01-01T00:00:00Z')",
            (buy_size, stop_loss, json.dumps(rules)))
        self.db.commit()
        return cur.lastrowid

    def _losing_ledger(self, bot_id, losses=6, wins=2):
        for i in range(losses):
            record_trade(self.db, 1, bot_id, f"L{i}", "sell", 1.0, 90.0,
                         90.0, pnl_usd=-10.0, mode="paper",
                         now=f"2026-01-01T00:{i:02d}:00Z")
        for i in range(wins):
            record_trade(self.db, 1, bot_id, f"W{i}", "sell", 1.0, 110.0,
                         110.0, pnl_usd=10.0, mode="paper",
                         now=f"2026-01-01T00:{10 + i:02d}:00Z")

    def test_losing_bot_tightens_stop_loss(self):
        bot_id = self._bot()
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        adj = bb.review_bot(self.db, bot, self.enc_key)
        self.assertIsNotNone(adj)
        self.assertEqual(adj["field"], "stop_loss_pct")
        self.assertEqual(adj["old_value"], 7.0)
        self.assertEqual(adj["new_value"], 6.0)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertEqual(bot["stop_loss_pct"], 6.0)
        # Buy size never widened, threshold never lowered.
        self.assertEqual(bot["buy_size_pct"], 20.0)
        # The reason is encrypted at rest, readable with the key.
        row = self.db.execute(
            "SELECT reason FROM builderbot_adjustments WHERE bot_id = ?",
            (bot_id,)).fetchone()
        self.assertNotIn("losses", row["reason"])
        plain = decrypt_secret(row["reason"], self.enc_key).decode()
        self.assertIn("6 losses", plain)

    def test_cooldown_blocks_second_adjustment(self):
        bot_id = self._bot()
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNotNone(bb.review_bot(self.db, bot, self.enc_key))
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNone(bb.review_bot(self.db, bot, self.enc_key))

    def test_winning_bot_untouched(self):
        bot_id = self._bot()
        for i in range(6):
            record_trade(self.db, 1, bot_id, f"W{i}", "sell", 1.0, 110.0,
                         110.0, pnl_usd=10.0, mode="paper",
                         now=f"2026-01-01T00:{i:02d}:00Z")
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNone(bb.review_bot(self.db, bot, self.enc_key))

    def test_too_few_trades_untouched(self):
        bot_id = self._bot()
        self._losing_ledger(bot_id, losses=3, wins=0)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNone(bb.review_bot(self.db, bot, self.enc_key))

    def test_falls_through_to_threshold_then_size(self):
        # Stop loss already at the floor: the threshold rises instead.
        bot_id = self._bot(stop_loss=3.0)
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        adj = bb.review_bot(self.db, bot, self.enc_key)
        self.assertEqual(adj["field"], "buy.threshold_pct")
        self.assertEqual(adj["new_value"], 5.5)
        # Threshold at the cap + stop loss at the floor: size shrinks.
        bot_id2 = self._bot(stop_loss=3.0, threshold=15.0)
        self._losing_ledger(bot_id2)
        bot2 = self.db.execute("SELECT * FROM bots WHERE id = ?",
                               (bot_id2,)).fetchone()
        adj2 = bb.review_bot(self.db, bot2, self.enc_key)
        self.assertEqual(adj2["field"], "buy_size_pct")
        self.assertEqual(adj2["new_value"], 15.0)

    def test_nothing_left_to_tighten(self):
        bot_id = self._bot(stop_loss=3.0, buy_size=5.0, threshold=15.0)
        self._losing_ledger(bot_id)
        bot = self.db.execute("SELECT * FROM bots WHERE id = ?",
                              (bot_id,)).fetchone()
        self.assertIsNone(bb.review_bot(self.db, bot, self.enc_key))


class TestWatchCadence(BuilderBotBase):
    def _setup_scanner_position(self):
        rules = {"version": 1, "kind": "scanner",
                 "scan_sources": ["pump.fun"],
                 "scan_interval_seconds": 30,
                 "buy": {"metric": "gain_3m_pct", "window_seconds": 180,
                         "threshold_pct": 5.0, "pick": "strongest"},
                 "sell_ladder": [{"pct_of_position": 100,
                                  "profit_pct": 10}],
                 "trailing_stop_pct": 7.0,
                 "top10_rug": {"drop_pct": 10.0,
                               "concentration_pct": 21.0,
                               "slow_climb_sell_pct": 50.0,
                               "check_interval_seconds": 15.0},
                 "buy_filter": dict(BUY_FILTER_DEFAULT)}
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, coins, strategy, category, "
            "buy_size_pct, stop_loss_pct, max_open_trades, platform, "
            "mode, silent, rules_json, api_token_hash, created_at) "
            "VALUES "
            "(1, 'Scan', '[]', 'balanced', 'scanner', 20, 7, 3, "
            "'pump.fun', 'paper', 1, ?, 'x', '2026-01-01T00:00:00Z')",
            (json.dumps(rules),))
        bot_id = cur.lastrowid
        # Paper holdings + position so a sell can actually happen.
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (1, ?, 100.0, 1.0)", (MINT,))
        self.db.execute(
            "INSERT INTO bot_positions (user_id, bot_id, coin_id, "
            "amount, avg_price, mode, opened_at, peak_price, "
            "orig_amount, ladder_stage) VALUES "
            "(1, ?, ?, 100.0, 1.0, 'paper', '2026-01-01T00:00:00Z', "
            "1.0, 100.0, 0)", (bot_id, MINT))
        self.db.commit()
        return bot_id

    def test_second_pass_inside_15s_skips(self):
        self._setup_scanner_position()
        client = FakeTop10Client(100_000, 1_000_000)
        cfg = Config(env={"SESSION_SECRET": "x" * 40,
                          "ENCRYPTION_KEY": "ab" * 32,
                          "NETWORK": "devnet",
                          "DATABASE_PATH": f"{self.tmp.name}/test.db"})
        prices = {MINT: {"price_usd": 1.0}}
        top10_watch_once(self.db, cfg, lambda url: client,
                         price_fetcher=lambda mints: prices)
        first_calls = client.calls
        self.assertGreater(first_calls, 0)
        top10_watch_once(self.db, cfg, lambda url: client,
                         price_fetcher=lambda mints: prices)
        self.assertEqual(client.calls, first_calls)

    def test_concentration_sell_all_fires_in_watch(self):
        bot_id = self._setup_scanner_position()
        # Seed a baseline below 21% first.
        self.db.execute(
            "INSERT INTO scanner_top10 (bot_id, mint, top10_total, "
            "recorded_at, share_pct, rise_streak, last_checked_at) "
            "VALUES (?, ?, 100000, '2026-01-01T00:00:00Z', 10.0, 0, "
            "'2000-01-01T00:00:00Z')", (bot_id, MINT))
        self.db.commit()
        client = FakeTop10Client(300_000, 1_000_000)  # 30% of supply
        cfg = Config(env={"SESSION_SECRET": "x" * 40,
                          "ENCRYPTION_KEY": "ab" * 32,
                          "NETWORK": "devnet",
                          "DATABASE_PATH": f"{self.tmp.name}/test.db"})
        prices = {MINT: {"price_usd": 1.0}}
        actions = top10_watch_once(self.db, cfg, lambda url: client,
                                   price_fetcher=lambda mints: prices)
        self.assertTrue(any(a["action"] == "sell" for a in actions))
        self.assertIsNone(self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())
        # The sell is in the ledger with an exit reason.
        rows = self.db.execute(
            "SELECT * FROM trade_ledger WHERE bot_id = ?",
            (bot_id,)).fetchall()
        self.assertTrue(any("top-10" in (r["exit_reason"] or "")
                            for r in rows))

    def test_rpc_failure_skips_without_selling(self):
        bot_id = self._setup_scanner_position()

        class FailingClient:
            def get_token_largest_accounts(self, mint):
                raise RuntimeError("rpc down")

            def get_token_supply(self, mint):
                raise RuntimeError("rpc down")

        cfg = Config(env={"SESSION_SECRET": "x" * 40,
                          "ENCRYPTION_KEY": "ab" * 32,
                          "NETWORK": "devnet",
                          "DATABASE_PATH": f"{self.tmp.name}/test.db"})
        actions = top10_watch_once(
            self.db, cfg, lambda url: FailingClient(),
            price_fetcher=lambda mints: {MINT: {"price_usd": 1.0}})
        self.assertEqual(actions, [])
        self.assertIsNotNone(self.db.execute(
            "SELECT * FROM bot_positions WHERE bot_id = ?",
            (bot_id,)).fetchone())


class TestBuilderBotHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ask(self, message):
        token = csrf(self.client, "/dashboard")
        resp = self.client.post("/api/builderbot", json={
            "message": message, "csrf_token": token})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_create_page_is_chatbox(self):
        html = self.client.get("/bots/create").get_data(as_text=True)
        self.assertIn("builderBot", html)
        self.assertIn("ID", html)
        self.assertIn("What should we call this bot?", html)
        self.assertNotIn('name="category"', html)
        self.assertNotIn('name="strategy"', html)
        self.assertNotIn("Coins to watch", html)

    def test_full_chatbox_flow_over_http(self):
        self.client.get("/bots/create")
        for answer in TestConversation.ANSWERS:
            data = self._ask(answer)
        self.assertTrue(data["confirm"])
        data = self._ask("yes")
        self.assertIn("created", data)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        self.assertIsNotNone(bot)
        self.assertEqual(json.loads(bot["rules_json"])["kind"],
                         "trenches")

    def test_full_chatbox_flow_asks_each_question_in_order(self):
        """Regression: all 12 questions are asked, in order, each
        with its own suggestions — and the created bot stores the
        stop loss the user actually answered. A double-sent
        suggestion used to answer the NEXT question too, so the
        stop-loss question was skipped (see
        test_chatbox_sends_each_suggestion_once)."""
        self.client.get("/bots/create")
        for i, answer in enumerate(TestConversation.ANSWERS):
            data = self._ask(answer)
            if i < len(TestConversation.ANSWERS) - 1:
                next_step = bb.STEPS[i + 1]
                self.assertEqual(
                    data["messages"][-1]["text"],
                    bb._question_text(next_step))
                self.assertEqual(data["suggestions"],
                                 next_step["suggestions"])
            else:
                self.assertTrue(data["confirm"])
            if i == 6:  # just answered max_open — stop loss is next
                self.assertTrue(
                    data["messages"][-1]["text"].startswith(
                        "Stop loss"))
                self.assertEqual(data["suggestions"],
                                 ["5%", "7%", "10%"])
        data = self._ask("yes")
        self.assertIn("created", data)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        self.assertIsNotNone(bot)
        # The stop loss the user actually answered (7%), not a value
        # leaked from another question.
        self.assertEqual(bot["stop_loss_pct"], 7.0)

    def test_chatbox_sends_each_suggestion_once(self):
        """Regression for the skipped stop-loss question: the
        chatbox used to wire TWO click handlers onto every
        suggestion button (one direct, one delegated on the
        container), so one tap sent the answer twice and the
        duplicate was parsed as the answer to the next question.
        There must be exactly one handler, plus an in-flight guard
        so a double tap can never send twice."""
        html = self.client.get("/bots/create").get_data(as_text=True)
        self.assertNotIn("b.addEventListener('click'", html)
        self.assertIn("suggBox.addEventListener('click'", html)
        self.assertIn("let sending = false;", html)
        self.assertIn("if (!text || sending) return;", html)

    def test_keypair_paste_refused_and_never_stored(self):
        self.client.get("/bots/create")
        data = self._ask(KEYPAIR_JSON)
        self.assertIn("secret", data["messages"][-1]["text"])
        row = self.db.execute(
            "SELECT encrypted_state FROM builderbot_chats "
            "WHERE user_id = 1").fetchone()
        self.assertNotIn("123", row["encrypted_state"])

    def test_edit_form_has_no_pickers(self):
        self.client.get("/bots/create")
        for answer in TestConversation.ANSWERS:
            self._ask(answer)
        self._ask("yes")
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        html = self.client.get(
            f"/bots/{bot['id']}/edit").get_data(as_text=True)
        self.assertNotIn('name="category"', html)
        self.assertNotIn('name="strategy"', html)
        self.assertIn("builderBot", html)

    def test_create_post_ignores_strategy_and_category(self):
        resp = self.client.post("/bots/create", data={
            "csrf_token": csrf(self.client, "/bots/create"),
            "name": "Plain", "strategy": "bold", "category": "scanner",
            "coins": "SOL", "buy_size_pct": "20", "stop_loss_pct": "7",
            "mode": "paper"})
        self.assertEqual(resp.status_code, 200)
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Plain'").fetchone()
        self.assertEqual(bot["strategy"], "balanced")
        self.assertEqual(bot["category"], "")
        self.assertEqual(bot["rules_json"], "")

    def test_detail_shows_wl_and_ledger(self):
        self.client.get("/bots/create")
        for answer in TestConversation.ANSWERS:
            self._ask(answer)
        self._ask("yes")
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        record_trade(self.db, 1, bot["id"], "SOL", "sell", 1.0, 110.0,
                     110.0, pnl_usd=10.0, mode="paper",
                     now="2026-01-01T00:00:00Z")
        html = self.client.get(
            f"/bots/{bot['id']}").get_data(as_text=True)
        self.assertIn("Win / loss record", html)
        self.assertIn("1 wins", html)
        self.assertIn("Rule adjustments", html)

    def test_delete_purges_builderbot_data(self):
        self.client.get("/bots/create")
        for answer in TestConversation.ANSWERS:
            self._ask(answer)
        self._ask("yes")
        bot = self.db.execute(
            "SELECT * FROM bots WHERE name = 'Luna'").fetchone()
        record_trade(self.db, 1, bot["id"], "SOL", "sell", 1.0, 90.0,
                     90.0, pnl_usd=-10.0, mode="paper",
                     now="2026-01-01T00:00:00Z")
        self.db.execute(
            "INSERT INTO builderbot_adjustments (bot_id, user_id, "
            "field, old_value, new_value, reason, created_at) VALUES "
            "(?, 1, 'stop_loss_pct', 7.0, 6.0, 'x', "
            "'2026-01-01T00:00:00Z')", (bot["id"],))
        self.db.commit()
        resp = self.client.post(f"/bots/{bot['id']}/delete", data={
            "csrf_token": csrf(self.client, f"/bots/{bot['id']}/edit")})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM trade_ledger WHERE bot_id = ?",
            (bot["id"],)).fetchone()["c"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) c FROM builderbot_adjustments "
            "WHERE bot_id = ?", (bot["id"],)).fetchone()["c"], 0)


class TestPaperReset(BuilderBotBase):
    """builderBot 'Add cad': reset the paper balance to any amount.

    The command works from the builderBot chatbox: "Add cad" asks
    "How much would you like to start with?", a positive number
    becomes the new paper balance, and the paper positions,
    holdings and P&L history reset to a fresh start. ONLY the bot
    win/loss counts stay (they live in trade_ledger, never
    touched). Paper only — real mode is never touched.
    """

    def _seed_paper(self):
        # A paper holding + paper trade history to be cleared, and a
        # bot with a win and a loss in the ledger to be kept.
        self.db.execute(
            "INSERT INTO paper_holdings (user_id, coin_id, amount, "
            "avg_price) VALUES (1, 'SOL', 2.0, 10.0)")
        self.db.execute(
            "INSERT INTO paper_trades (user_id, coin_id, side, amount, "
            "price, total_usd, created_at) VALUES (1, 'SOL', 'buy', "
            "1.0, 10.0, 10.0, '2026-01-01T00:00:00Z')")
        cur = self.db.execute(
            "INSERT INTO bots (user_id, name, coins, strategy, "
            "category, buy_size_pct, stop_loss_pct, max_open_trades, "
            "platform, mode, silent, rules_json, api_token_hash, "
            "created_at) VALUES "
            "(1, 'Luna', '[]', 'balanced', '', 20, 7, 3, 'pump.fun', "
            "'paper', 0, '', 'x', '2026-01-01T00:00:00Z')")
        self.db.commit()
        bot_id = cur.lastrowid
        record_trade(self.db, 1, bot_id, "A", "sell", 1.0, 110.0,
                     110.0, pnl_usd=10.0, mode="paper",
                     now="2026-01-01T00:00:00Z")
        record_trade(self.db, 1, bot_id, "B", "sell", 1.0, 90.0,
                     90.0, pnl_usd=-10.0, mode="paper",
                     now="2026-01-01T00:01:00Z")
        # A real-mode ledger row: real is never touched.
        record_trade(self.db, 1, bot_id, "C", "sell", 1.0, 110.0,
                     110.0, pnl_usd=10.0, mode="real",
                     now="2026-01-01T00:02:00Z")
        return bot_id

    def _last_bot_text(self, result):
        return result["messages"][-1]["text"]

    def test_add_cad_asks_how_much(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        result = bb.answer(self.db, self.user_id, self.enc_key,
                           "Add cad")
        self.assertEqual(self._last_bot_text(result),
                         "How much would you like to start with?")
        # The balance is untouched until a number is given.
        self.assertEqual(paper_cash(self.db, 1), 100.0)

    def test_add_cad_case_insensitive(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        result = bb.answer(self.db, self.user_id, self.enc_key,
                           "ADD CAD")
        self.assertEqual(self._last_bot_text(result),
                         "How much would you like to start with?")

    def test_numeric_answer_resets_paper_keeps_wl(self):
        bot_id = self._seed_paper()
        before = bot_wl_summary(self.db, bot_id)
        self.assertEqual((before["wins"], before["losses"]), (2, 1))
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Add cad")
        result = bb.answer(self.db, self.user_id, self.enc_key, "500")
        self.assertIn("$500 CAD", self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 500.0)
        # Positions, holdings and P&L history are gone.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM paper_holdings WHERE user_id = 1"
            ).fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE user_id = 1"
            ).fetchone()[0], 0)
        # ONLY the bot win/loss counts stay — same wins, same
        # losses — and the real-mode row is untouched too.
        after = bot_wl_summary(self.db, bot_id)
        self.assertEqual((after["wins"], after["losses"]), (2, 1))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE user_id = 1 "
            "AND mode = 'real'").fetchone()[0], 1)
        self.assertEqual(result.get("paper_reset"), 500.0)

    def test_dollar_amount_and_k_suffix(self):
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Add cad")
        result = bb.answer(self.db, self.user_id, self.enc_key, "$1k")
        self.assertIn("$1,000 CAD", self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 1000.0)

    def test_non_numeric_rejected_without_reset(self):
        self._seed_paper()
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Add cad")
        result = bb.answer(self.db, self.user_id, self.enc_key,
                           "banana")
        self.assertIn("doesn't look like a number",
                      self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 100.0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM paper_holdings WHERE user_id = 1"
            ).fetchone()[0], 1)
        self.assertNotIn("paper_reset", result)

    def test_zero_and_negative_rejected_without_reset(self):
        self._seed_paper()
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Add cad")
        result = bb.answer(self.db, self.user_id, self.enc_key, "0")
        self.assertIn("more than $0", self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 100.0)
        # Still in the ask — a valid number after the rejection works.
        result = bb.answer(self.db, self.user_id, self.enc_key, "-50")
        self.assertIn("more than $0", self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 100.0)
        result = bb.answer(self.db, self.user_id, self.enc_key, "250")
        self.assertIn("$250 CAD", self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 250.0)

    def test_cancel_backs_out_without_reset(self):
        self._seed_paper()
        bb.start_conversation(self.db, self.user_id, self.enc_key)
        bb.answer(self.db, self.user_id, self.enc_key, "Add cad")
        result = bb.answer(self.db, self.user_id, self.enc_key,
                           "cancel")
        self.assertIn("nothing was changed",
                      self._last_bot_text(result))
        self.assertEqual(paper_cash(self.db, 1), 100.0)
        # The bot-building conversation is untouched — the next
        # message answers the first question again.
        result = bb.answer(self.db, self.user_id, self.enc_key, "Luna")
        self.assertNotIn("paper_reset", result)


if __name__ == "__main__":
    unittest.main()
