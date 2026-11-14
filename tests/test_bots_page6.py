"""Page 6 (Bots) — assistant + platform wallets DELETED, signer card.

Owner's spec (2026-11-11 ~19:25 PST, "data glitch" on Page 6):
- The AI assistant is GONE from Page 6: no robo logo, no yellow
  chat box, no red letters, no chatbox, no /api/assistant wiring
  on this page — it must look like it was never there. The
  /api/assistant endpoint itself still exists and still answers
  (test_assistant.py covers it); builderBot (Page 9) is untouched.
- The platform-wallet rotating selector is GONE from Page 6: no
  "Platform wallets" section, no side arrows, no wallet panels,
  no Create buttons, no wallet addresses on this page. Page 8
  itself (the platform wallets) is untouched, and the create
  route still works — it just no longer shows anything on Page 6.
- The bottom half is the Signer Bot card (see
  test_bots_signer_dashboard.py): a bot card like the ones above
  it, showing the Signer ID and the number of real transactions
  signed. No fee line: the trading fee was removed (James,
  2026-11-12), so the card shows no fee figure. Tapping it
  reveals the current tasks.
- Edit Bot is the same size as Add Bot (one shared sizing rule).
- App name + logo are bigger on the Bots page than the base sizes.
- A black crown-tipped banner sits underneath the logo. (The thin
  gold bar that used to cross the logo was deleted 2026-11-11 —
  it cut through the fox, so the logo now sits clean.)
- The fox logo has no CSS gold border: the image's baked-in gold
  circle is the only outline (a CSS border drew a second circle).
No SOL moves in these tests; real execution is a recording mock.
"""

import re
import unittest

from helpers import csrf
from test_ui_rebuild import UiBase


class TestBotsPage6(UiBase):
    def _css(self):
        return open("tradingbot/static/style.css").read()

    def test_lower_scrolling_section_gone(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertNotIn("page-scroll-panel", html)
        # The old lower scrolling section's Memecoin build card is
        # gone — and the preset window no longer lists Trenches at
        # all (owner, 2026-11-11: button-only Clyde window), so
        # "Memecoin build" appears nowhere on Page 6 now.
        self.assertNotIn("Memecoin build", html)
        self.assertNotIn('data-preset="trenches"', html)
        # The Trenches card/link in the page body is gone (the
        # page-id picker in the topbar still lists every page,
        # including Trenches — that is a different link).
        self.assertNotIn("trenches-link", html)
        # The Trenches page itself is untouched and still reachable
        # from home.
        self.assertIn('href="/trenches"', self._html("/wallet"))

    def test_assistant_gone_from_bots_page(self):
        # Owner, 2026-11-11 ~19:25 PST: the AI assistant is
        # deleted from Page 6 — no trace of it may remain: no
        # card, no robo logo, no chatbox, no wiring, no CSS, and
        # not even a mention in the empty-bots line.
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertNotIn("bots-assistant", html)
        self.assertNotIn("robo-logo", html)
        self.assertNotIn('id="bots-chat-form"', html)
        self.assertNotIn('id="bots-chat-input"', html)
        self.assertNotIn('id="bots-chat-log"', html)
        self.assertNotIn("/api/assistant", html)
        self.assertNotIn("ask the assistant", html)
        self.assertNotIn("Assistant", html)
        css = self._css()
        self.assertNotIn(".bots-assistant", css)
        self.assertNotIn(".robo-logo", css)
        # The generic .chat-log rule stays: builderBot (Page 9)
        # still uses it.
        self.assertIn(".chat-log", css)
        # The endpoint itself still answers (covered by
        # test_assistant.py) — only the Page 6 box is gone.

    def test_platform_wallet_selector_gone_from_bots_page(self):
        # Owner, 2026-11-11 ~19:25 PST: the platform-wallet
        # rotating selector is deleted from Page 6 — no section,
        # no arrows, no panels, no Create buttons, no addresses,
        # no CSS, no JS. Page 8 itself is untouched.
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertNotIn("bots-wallets", html)
        self.assertNotIn("Platform wallets", html)
        self.assertNotIn("wallet-rotator", html)
        self.assertNotIn("data-wallet-prev", html)
        self.assertNotIn("data-wallet-next", html)
        self.assertNotIn("data-wallet-panel", html)
        self.assertNotIn("data-wallet-counter", html)
        self.assertNotIn("wallet-counter", html)
        # No platform wallet create buttons or addresses on Page 6.
        self.assertNotIn("/platforms/pump.fun/create", html)
        self.assertNotIn("/platforms/phantom/create", html)
        self.assertNotIn("/platforms/pumpswap/create", html)
        self.assertNotIn("Wallet ready.", html)
        css = self._css()
        self.assertNotIn(".bots-wallets", css)
        self.assertNotIn(".wallet-rotator", css)
        self.assertNotIn(".wallet-panel", css)
        self.assertNotIn(".wallet-arrow", css)
        self.assertNotIn(".wallet-counter", css)
        js = open("tradingbot/static/ui.js").read()
        self.assertNotIn("data-wallet-rotator", js)
        self.assertNotIn("data-wallet-panel", js)

    def test_platform_wallet_create_route_is_gone(self):
        # The platform-wallet feature is deleted entirely in the
        # pump.fun-only rebuild (platforms.py gone, the
        # /platforms/<platform>/create route gone, the
        # platform_wallets table dropped): the create route 404s,
        # and nothing about platform wallets shows on Page 6.
        self._choose_mode("paper")
        resp = self.client.post(
            "/platforms/pump.fun/create",
            data={"csrf_token": csrf(self.client, "/bots"),
                  "return_to": "/bots"})
        self.assertEqual(resp.status_code, 404)
        html = self._html("/bots")
        self.assertNotIn("Wallet ready.", html)
        self.assertNotIn("Platform wallets", html)
        self.assertNotIn("/platforms/pump.fun/create", html)

    def test_edit_bot_same_size_as_add_bot(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("Add Bot", html)
        self.assertIn("Edit Bot", html)
        # Both buttons share one sizing class — the same rule sizes
        # them, so they are the same size.
        add = re.search(r'class="([^"]*action-btn[^"]*)"[^>]*>[^<]*Add Bot',
                        html)
        edit = re.search(r'class="([^"]*action-btn[^"]*)"[^>]*>Edit Bot',
                         html)
        self.assertIsNotNone(add, "Add Bot button not found")
        self.assertIsNotNone(edit, "Edit Bot button not found")
        self.assertIn("action-btn", add.group(1).split())
        self.assertIn("action-btn", edit.group(1).split())
        css = self._css()
        self.assertIn(".bot-actions .action-btn", css)

    def test_logo_and_name_bigger(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("fox-logo", html)
        css = self._css()
        # Bots-page logo is bigger than the base home logo
        # (base: clamp(56px, 10dvh, 96px) / clamp(64px, 12dvh, 110px)).
        m = re.search(r"\.bots-screen[^{]*\.fox-logo\s*\{[^}]*width:\s*clamp\((\d+)px",
                      css)
        self.assertIsNotNone(m, "no bots-page logo size rule")
        self.assertGreaterEqual(int(m.group(1)), 96)
        # Bots-page wordmark is bigger than the base (base: 32–44px).
        m = re.search(r"\.bots-screen[^{]*\.home-wordmark\s*\{[^}]*font-size:\s*clamp\((\d+)px",
                      css)
        self.assertIsNotNone(m, "no bots-page wordmark size rule")
        self.assertGreaterEqual(int(m.group(1)), 48)

    def test_crown_banner_no_gold_bar(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("bots-crown-banner", html)
        # The thin gold bar that cut through the logo was DELETED
        # (owner, 2026-11-11) — no bar element, no bar CSS.
        self.assertNotIn("bots-gold-bar", html)
        css = self._css()
        self.assertNotIn(".bots-gold-bar", css)
        self.assertIn(".bots-crown-banner", css)
        # Black banner, crown-tipped (clip-path points on its edge).
        self.assertRegex(css, r"\.bots-crown-banner\s*\{[^}]*#000")
        self.assertIn("clip-path", css)

    def test_fox_logo_has_exactly_one_gold_border(self):
        # The top logo is the yellow fox's face recolored red, with
        # NO baked-in gold circle — so the CSS border IS the single
        # yellow circle line (owner, 2026-11-11). The base .fox-logo
        # rule must set it, and no other .fox-logo rule (base,
        # .fox-auth, .home-screen, .bots-screen) may add a second
        # outline. border-radius: 50% stays (it clips the square
        # image into a circle).
        css = self._css()
        self.assertIn("border-radius: 50%", css)
        base = re.search(r"\.fox-logo\s*\{([^}]*)\}", css)
        self.assertIsNotNone(base, "no base .fox-logo rule")
        self.assertIn("border: 3px solid #ffb300", base.group(1))
        for m in re.finditer(r"([^{}]*\.fox-logo[^{}]*)\{([^}]*)\}", css):
            selector = m.group(1).strip()
            if selector == ".fox-logo":
                continue
            self.assertNotRegex(
                m.group(2), r"border\s*:",
                "second outline on %s" % selector)


if __name__ == "__main__":
    unittest.main()
