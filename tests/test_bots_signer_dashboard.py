"""Page 6 (Bots) — motto + Signer Bot card.

Owner's spec (2026-11-11 ~19:25 PST, "data glitch" on Page 6):
- The motto "Still on the $GATE?" (same large red italic style as
  Pages 1-2) stays on Page 6.
- The old signer-bot dashboard block is REPLACED by a Signer Bot
  card in the bottom half: a bot card like the ones above it
  (same .card bot-card classes, icon, bold name, stats lines,
  chevron), showing inside it the Signer ID and the number of
  real transactions signed (a running count starting at 1).
  No fee line: the trading fee was removed (James, 2026-11-12),
  so the card shows no fee figure at all. Tapping the card
  reveals the signing bot's current tasks.
- The numbers are real app data, never invented: the transaction
  count is trade_ledger rows with mode='real' only (paper rows
  are simulated, never signed, never counted). "—" when a
  figure cannot be read, and an honest empty state when there
  is nothing to count yet.
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


class TestSignerCard(UiBase):
    def _dashboard(self):
        self._choose_mode("paper")
        return self._html("/bots")

    def test_card_looks_like_a_bot_card(self):
        # Same classes as the bot cards above it: .card bot-card,
        # icon, bold name, stats lines, chevron. The card is a
        # <button> (like the Clyde card), not a link.
        html = self._dashboard()
        m = html[html.find('class="card bot-card signer-card"'):]
        m = m[:m.find("</button>")]
        self.assertIn('class="card bot-card signer-card"', m)
        self.assertIn("bot-ico", m)
        self.assertIn("bot-info", m)
        self.assertIn("<strong>Signer Bot</strong>", m)
        self.assertIn("bot-arrow", m)
        # Full-width in the bottom half: .bot-card's 92% carousel
        # slot is reset for this card, or it would stretch down
        # the page.
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".signer-card", css)
        self.assertRegex(css, r"\.signer-card\s*\{[^}]*flex:\s*0 0 auto")
        self.assertRegex(css, r"\.signer-card\s*\{[^}]*width:\s*100%")

    def test_support_id_shown_in_card(self):
        row = self.db.execute(
            "SELECT support_id FROM signing_bots WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "signup should create a signing bot")
        html = self._dashboard()
        self.assertIn("Signer ID", html)
        self.assertIn(row["support_id"], html)

    def test_current_tasks_shown_on_tap(self):
        # The tasks live in a panel hidden until the card is
        # tapped: the button carries the toggle wiring and the
        # panel starts hidden.
        bot = self._create_bot()
        self.db.execute(
            "INSERT INTO bot_proposals (user_id, bot_id, kind, coin_id, "
            "side, detail, status, created_at) VALUES (?, ?, 'signal', "
            "'SOL', 'buy', 'test signal', 'pending', "
            "'2026-11-11T06:30:00Z')",
            (self.user_id, bot["id"]))
        self.db.commit()
        html = self._dashboard()
        self.assertIn('data-signer-toggle', html)
        self.assertIn('aria-controls="signer-tasks-panel"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('id="signer-tasks-panel"', html)
        panel = html[html.find('id="signer-tasks-panel"'):]
        self.assertIn("hidden", panel[:80])
        self.assertIn("Current tasks", html)
        self.assertIn("SOL", html)
        self.assertIn("buy", html)
        # The time it was tasked, shown with the task.
        self.assertIn("2026-11-11 06:30", html)
        # The tap toggles the panel (JS, in the page script).
        self.assertIn("signerPanel.removeAttribute('hidden')", html)
        self.assertIn("signerPanel.setAttribute('hidden', '')", html)

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
        self.assertIn('data-signer-tx-count="2"', html)
        self.assertIn("Transactions signed: 2", html)

    def test_transaction_count_zero_when_none_signed(self):
        # No signed transactions yet: the honest empty state, never
        # a made-up number — the running count starts at 1 with
        # the first signed transaction.
        html = self._dashboard()
        self.assertIn("Transactions signed", html)
        self.assertIn("No signed transactions yet", html)
        self.assertIn('data-signer-tx-count="0"', html)

    def test_no_gas_fee_line_on_card(self):
        # The trading fee was removed (James, 2026-11-12): the card
        # shows Signer ID + signed-transaction count only, no fee
        # line and no fee figure of any kind.
        html = self._dashboard()
        self.assertIn("Signer Bot", html)
        self.assertIn("Transactions signed", html)
        self.assertNotIn("Gas fees paid", html)
        self.assertNotIn("fees_total_sol", html)


if __name__ == "__main__":
    unittest.main()
