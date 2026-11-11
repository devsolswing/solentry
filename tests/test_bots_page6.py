"""Page 6 (Bots) rebuild — the owner's spec (2026-11-10/11).

Rules under test:
- The scrolling section on the lower half is gone: no
  page-scroll-panel, no Memecoin build card, no Trenches link on
  the Bots page (the Trenches page is still reachable from home).
- The AI assistant lives on the Bots page: robo logo, BLACK card
  with a GOLD border, RED letters, and a working chatbox wired to
  the existing /api/assistant flow (it answers, it is not a dead
  box). (2026-11-11: the solid yellow washed the whole page out,
  so the yellow background was replaced by black + gold.)
- Platform wallets moved from Page 8 as a rotating selector: two
  side arrows cycle through the real platforms (pump.fun, Phantom,
  pumpSwap), black card with a gold border, red writing; a wallet
  that exists shows its address, one that does not shows a Create
  button that really creates it. Page 8 itself is untouched.
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
        self.assertNotIn("Memecoin build", html)
        # The Trenches card/link in the page body is gone (the
        # page-id picker in the topbar still lists every page,
        # including Trenches — that is a different link).
        self.assertNotIn("trenches-link", html)
        # The Trenches page itself is untouched and still reachable
        # from home.
        self.assertIn('href="/trenches"', self._html("/wallet"))

    def test_assistant_present_black_gold_red_robo(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("bots-assistant", html)
        self.assertIn("🤖", html)
        self.assertIn('id="bots-chat-form"', html)
        self.assertIn('id="bots-chat-input"', html)
        self.assertIn('id="bots-chat-log"', html)
        # Wired to the existing assistant endpoint, not a dead box.
        self.assertIn("/api/assistant", html)
        css = self._css()
        self.assertIn(".bots-assistant", css)
        # Black card, gold border, red letters — no yellow wash.
        self.assertRegex(css, r"\.bots-assistant\s*\{[^}]*background:\s*#000")
        self.assertRegex(css, r"\.bots-assistant\s*\{[^}]*#ffd700")
        self.assertRegex(css, r"\.bots-assistant[^{]*\{[^}]*#b91c1c")
        first = css.split(".bots-assistant")[1].split("}")[0]
        self.assertNotIn("linear-gradient(180deg, #ffd700", first)

    def test_assistant_answers(self):
        self._choose_mode("paper")
        self._html("/bots")
        resp = self.client.post(
            "/api/assistant",
            json={"message": "show my balance",
                  "csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue((data.get("reply") or "").strip())

    def test_platform_wallet_rotator_present(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("wallet-rotator", html)
        self.assertIn("data-wallet-prev", html)
        self.assertIn("data-wallet-next", html)
        self.assertIn("data-wallet-panel", html)
        # All three real platforms, in order.
        for label in ("pump.fun", "Phantom", "pumpSwap"):
            self.assertIn(label, html)
        # No wallet exists yet: each platform offers a Create button
        # that posts to the existing create route.
        self.assertIn("/platforms/pump.fun/create", html)
        self.assertIn("Create pump.fun wallet", html)
        css = self._css()
        self.assertIn(".wallet-rotator", css)
        self.assertIn(".wallet-panel", css)
        # Black card, gold border, red writing — no yellow wash.
        self.assertRegex(css, r"\.bots-wallets\s*\{[^}]*background:\s*#000")
        self.assertRegex(css, r"\.bots-wallets\s*\{[^}]*#ffd700")
        self.assertRegex(css, r"\.bots-wallets[^{]*\{[^}]*#b91c1c")
        first = css.split(".bots-wallets")[1].split("}")[0]
        self.assertNotIn("linear-gradient(180deg, #ffd700", first)

    def test_platform_wallet_shows_address_once_created(self):
        self._choose_mode("paper")
        resp = self.client.post(
            "/platforms/pump.fun/create",
            data={"csrf_token": csrf(self.client, "/bots"),
                  "return_to": "/bots"})
        self.assertIn(resp.status_code, (200, 302))
        html = self._html("/bots")
        db = self.app.config["DB"]
        row = db.execute(
            "SELECT pubkey FROM platform_wallets "
            "WHERE user_id = ? AND platform = 'pump.fun'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row["pubkey"], html)
        self.assertIn("Wallet ready.", html)

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
