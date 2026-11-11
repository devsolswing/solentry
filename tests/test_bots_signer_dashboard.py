"""Page 6 (Bots) — motto, toned-down yellow, signer-bot dashboard.

Owner's report (2026-11-11 ~06:38 PST):
- Page 6 has NO motto and is completely yellowed out: the assistant
  box and the platform-wallet selector are solid yellow and together
  make the whole page a yellow sheet. The motto "Still on the $GATE?"
  (same large red italic style as Pages 1-2) belongs on Page 6, and
  the two yellow sections become compact black cards with gold
  borders (red letters kept) so the page reads black/gold/red like
  the rest of the app. The assistant stays wired to /api/assistant
  and the wallet selector keeps working (two arrows, Page 8
  untouched).
- The bottom half is the signer bot: a status dashboard showing the
  user's own Support ID, the signing bot's current tasks (each with
  the time it was tasked), and the number of transactions the bot
  has signed — all from real app data, never invented; "—" when a
  figure cannot be read, and an honest empty state when there are
  no tasks.
No SOL moves in these tests; real execution is a recording mock.
"""

import unittest

from helpers import csrf  # noqa: F401  (kept for symmetry with siblings)
from test_ui_rebuild import UiBase
from tradingbot.ledger import record_trade


class TestBotsMotto(UiBase):
    def test_motto_on_bots_page(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("Still on", html)
        self.assertIn("$GATE?", html)
        # Same shared motto style as Pages 1-2 (large red italic).
        self.assertIn("fox-tagline", html)
        self.assertIn("bots-motto", html)

    def test_motto_is_red_italic_in_css(self):
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".bots-motto", css)
        self.assertRegex(css, r"\.fox-tagline\s*\{[^}]*#ff2d20")
        self.assertRegex(css, r"\.fox-tagline\s*\{[^}]*italic")


class TestBotsTonedDown(UiBase):
    def _css(self):
        return open("tradingbot/static/style.css").read()

    def test_assistant_is_black_card_gold_border_red_letters(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("bots-assistant", html)
        # Still wired to the existing assistant flow, not a dead box.
        self.assertIn("/api/assistant", html)
        css = self._css()
        # Black background, gold border — NOT a solid yellow sheet.
        self.assertRegex(css, r"\.bots-assistant\s*\{[^}]*background:\s*#000")
        self.assertRegex(css, r"\.bots-assistant\s*\{[^}]*#ffd700")
        # Red letters kept.
        self.assertRegex(css, r"\.bots-assistant\s*\{[^}]*#b91c1c")
        # No yellow gradient wash on the card.
        m = css.split(".bots-assistant")[1].split("}")[0]
        self.assertNotIn("linear-gradient(180deg, #ffd700", m)

    def test_wallets_is_black_card_gold_border_red_letters(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("bots-wallets", html)
        self.assertIn("wallet-rotator", html)
        css = self._css()
        self.assertRegex(css, r"\.bots-wallets\s*\{[^}]*background:\s*#000")
        self.assertRegex(css, r"\.bots-wallets\s*\{[^}]*#ffd700")
        self.assertRegex(css, r"\.bots-wallets\s*\{[^}]*#b91c1c")
        m = css.split(".bots-wallets")[1].split("}")[0]
        self.assertNotIn("linear-gradient(180deg, #ffd700", m)

    def test_assistant_is_compact(self):
        # The chat log is capped and scrolls internally instead of
        # stretching the card down the whole page.
        css = self._css()
        self.assertRegex(
            css, r"\.bots-assistant\s+\.chat-log\s*\{[^}]*max-height:\s*\d+px")


class TestSignerDashboard(UiBase):
    def _dashboard(self):
        self._choose_mode("paper")
        return self._html("/bots")

    def test_support_id_shown(self):
        row = self.db.execute(
            "SELECT support_id FROM signing_bots WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "signup should create a signing bot")
        html = self._dashboard()
        self.assertIn("Support ID", html)
        self.assertIn(row["support_id"], html)

    def test_current_tasks_listed_with_time_tasked(self):
        bot = self._create_bot()
        self.db.execute(
            "INSERT INTO bot_proposals (user_id, bot_id, kind, coin_id, "
            "side, detail, status, created_at) VALUES (?, ?, 'signal', "
            "'SOL', 'buy', 'test signal', 'pending', "
            "'2026-11-11T06:30:00Z')",
            (self.user_id, bot["id"]))
        self.db.commit()
        html = self._dashboard()
        self.assertIn("Current tasks", html)
        self.assertIn("SOL", html)
        self.assertIn("buy", html)
        # The time it was tasked, shown with the task.
        self.assertIn("2026-11-11 06:30", html)

    def test_empty_tasks_state_is_honest(self):
        html = self._dashboard()
        self.assertIn("Current tasks", html)
        self.assertIn("No tasks right now.", html)

    def test_transaction_count_counts_real_signed_trades_only(self):
        bot = self._create_bot()
        record_trade(self.db, self.user_id, bot["id"], "SOL", "buy",
                     1.0, 10.0, 10.0, mode="real")
        record_trade(self.db, self.user_id, bot["id"], "SOL", "sell",
                     1.0, 11.0, 11.0, mode="real")
        # A paper trade is simulated, never signed — it must not count.
        record_trade(self.db, self.user_id, bot["id"], "SOL", "buy",
                     1.0, 10.0, 10.0, mode="paper")
        html = self._dashboard()
        self.assertIn("Transactions signed", html)
        self.assertIn('data-signer-tx-count="2">2<', html)

    def test_transaction_count_zero_when_none_signed(self):
        html = self._dashboard()
        self.assertIn("Transactions signed", html)
        self.assertIn('data-signer-tx-count="0">0<', html)


if __name__ == "__main__":
    unittest.main()
