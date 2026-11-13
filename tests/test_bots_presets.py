"""Preset bots on Page 6 (Bots) — the owner's spec (2026-11-11).

- A Preset button sits in the bot-actions row (same action-btn
  style as Add Bot / Edit Bot) and swaps the contents of the ONE
  Active Bots carousel: the header becomes "Presets", the active
  bot cards hide and the Clyde card shows in their spot, same
  layout. After 6 seconds with no input from the user it swaps
  back to Active Bots on its own — there is no manual switch
  back, and any input (tap, scroll, key) restarts the 6-second
  timer. There is NO separate preset window or preset list
  element anywhere on the page.
- The preset view is a BUTTON ONLY (owner, 2026-11-11 ~10:31
  PST, structure corrected ~10:43 PST): one Clyde button, no
  description, no rules, no notes. It looks identical to an
  active bot card (🔥, Clyde, gold Preset pill, its stats line)
  and POSTs to /bots/preset/clyde, which creates Clyde (or goes
  to the existing one) and lands on the bot details page.
- Clyde (owner's exact rules): scan the pump.fun trenches every
  30 seconds; hard minimum market cap $45,000 CAD; top-10 wallets
  under 12% of supply; entry is a Z-score (buy pressure 2.5
  standard deviations over the trailing average) plus a velocity
  shift; hard 4% stop loss; take profit 10-30% baseline,
  31-1000% for confirmed runners; swap underperformers for
  higher-velocity runners regardless of P&L.
- The engine has no Z-score field, no velocity-swap rule, and no
  pre-buy top-10 screen — those live in the preset data
  (rules.py), never pretended to be enforced. The engine
  enforces the closest available signals: 5% gain in
  3 minutes for entry, the 12% top-10 share as a sell on held
  coins, and the market-cap bar in its USD field (stricter than
  $45,000 CAD).
No SOL moves in these tests; real execution is a recording mock.
"""

import json
import re
import unittest

from helpers import csrf
from test_ui_rebuild import UiBase
from tradingbot.rules import (clyde_ruleset, preset_bots,
                              validate_ruleset)


class TestBotsPresets(UiBase):
    def test_preset_button_present_same_style_as_add_bot(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn("Presets", html)
        self.assertIn("data-presets-toggle", html)
        # The button controls the one carousel list.
        self.assertIn('aria-controls="active-bots-list"', html)
        self.assertNotIn('aria-controls="preset-list"', html)
        # Same sizing class as Add Bot / Edit Bot.
        m = re.search(r'class="([^"]*action-btn[^"]*)"[^>]*>Presets',
                      html)
        self.assertIsNotNone(m, "Presets button not found")
        self.assertIn("action-btn", m.group(1).split())

    def test_clyde_card_hidden_by_default_in_carousel(self):
        """One list, contents swap (owner, 2026-11-11 ~10:43 PST):
        the Clyde card is a carousel item inside #active-bots-list,
        hidden until the Presets button opens it. There is NO
        separate preset list element anywhere on the page."""
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertNotIn('id="preset-list"', html)
        self.assertNotIn("preset-list", html)
        self.assertNotIn("bots-presets", html)
        # The Clyde card lives INSIDE the one carousel, after the
        # active bot cards and before the signer card.
        carousel = html.find('id="active-bots-list"')
        self.assertGreater(carousel, -1, "carousel not found")
        clyde = html.find("data-preset-item", carousel)
        self.assertGreater(clyde, -1, "Clyde card not in carousel")
        self.assertLess(clyde, html.find('id="signer-card"', carousel))
        # Hidden until the Presets button opens it.
        m = re.search(r'<form[^>]*data-preset-item[^>]*>', html)
        self.assertIsNotNone(m, "Clyde form not found")
        self.assertIn("hidden", m.group(0))
        # Items hide via [hidden] — .bot-card is display:flex,
        # which beats the UA [hidden] rule, so the carousel needs
        # the override.
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".bot-carousel [hidden]", css)

    def test_preset_swap_single_list_same_carousel(self):
        """Presets swap the contents of the ONE carousel (owner,
        2026-11-11 ~10:43 PST): the header carries both titles,
        the active bot cards and the Clyde card sit in the same
        list — no second list, no separate preset section."""
        self._choose_mode("paper")
        html = self._html("/bots")
        # One section holds the head + the one carousel.
        self.assertIn('id="bots-section"', html)
        section = html[html.find('id="bots-section"'):]
        self.assertIn('id="active-bots-list"', section)
        self.assertNotIn("preset-list", section)
        # Bot cards and the Clyde card are items of that list.
        self.assertIn("data-active-item", section)
        self.assertIn("data-preset-item", section)
        # Header: "Active Bots" shown, "Presets" hidden until swap.
        m = re.search(r'<span data-active-title>([^<]*)</span>', html)
        self.assertIsNotNone(m, "active title not found")
        self.assertIn("Active Bots", m.group(1))
        m = re.search(r'<span data-presets-title([^>]*)>([^<]*)</span>',
                      html)
        self.assertIsNotNone(m, "presets title not found")
        self.assertIn("hidden", m.group(1))
        self.assertIn("Presets", m.group(2))
        # Carousel arrows hide while Presets is showing.
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".bots-section.showing-presets", css)

    def test_preset_idle_timer_auto_revert_no_manual_toggle(self):
        """After 6 seconds with no input Presets swaps back to
        Active Bots on its own; any input restarts the timer; the
        Preset button is NOT a manual toggle back."""
        self._choose_mode("paper")
        html = self._html("/bots")
        script = html[html.find("PRESETS_IDLE_MS"):]
        # 6-second idle timeout that restores Active Bots.
        self.assertIn("PRESETS_IDLE_MS = 6000", html)
        self.assertIn("setTimeout(showActiveBots, PRESETS_IDLE_MS)",
                      html)
        # The restore shows the active bot cards + title and hides
        # the Clyde card again (one list, contents swap).
        restore = script[script.find("function showActiveBots"):]
        restore = restore[:restore.find("function armPresetsIdleTimer")]
        self.assertIn("setCarouselItems(false)", restore)
        self.assertIn("activeTitle.removeAttribute('hidden')", restore)
        self.assertIn("presetsTitle.setAttribute('hidden', '')", restore)
        swap = script[script.find("function setCarouselItems"):]
        swap = swap[:swap.find("function presetsShowing")]
        # Active items unhide, preset items hide.
        self.assertIn("querySelectorAll('[data-active-item]')", swap)
        self.assertIn("querySelectorAll('[data-preset-item]')", swap)
        self.assertIn("removeAttribute('hidden')", swap)
        self.assertIn("setAttribute('hidden', '')", swap)
        # The timer only arms while Presets is showing.
        self.assertIn("function presetsShowing()", html)
        self.assertIn("if (!presetsShowing()) return;", html)
        # Input restarts the timer: tap, touch, key, scroll, click.
        for ev in ("pointerdown", "touchstart", "keydown", "scroll",
                   "click"):
            self.assertIn("'%s'" % ev, html)
        self.assertIn("armPresetsIdleTimer", html)
        # The Preset button only ever SHOWS presets — pressing it
        # again re-arms the timer; it never hides the Clyde card.
        self.assertIn("presetsToggle.addEventListener('click', "
                      "showPresets)", html)
        show = script[script.find("function showPresets"):]
        show = show[:show.find("if (presetsToggle")]
        self.assertIn("setCarouselItems(true)", show)
        # showPresets never hides the preset item itself.
        self.assertNotIn("presetList.setAttribute", show)
        self.assertNotIn("presetList", show)

    def test_preset_window_is_clyde_button_only(self):
        """Owner (2026-11-11): the preset view is a BUTTON ONLY —
        no description, no rules, no notes. One Clyde button, a
        carousel item like an active bot card (🔥, Clyde, gold
        Preset pill, its stats line), that POSTs to create Clyde."""
        self._choose_mode("paper")
        html = self._html("/bots")
        # No separate preset list element anywhere.
        self.assertNotIn("preset-list", html)
        # Scope to the one carousel (the words "Trenches" and
        # "Scanner" must not appear in the window at all).
        start = html.find('id="active-bots-list"')
        end = html.find('id="signer-card"', start)
        window = html[start:end]
        # The Clyde card is a carousel item, hidden by default.
        self.assertIn("data-preset-item", window)
        m = re.search(r'<form[^>]*data-preset-item[^>]*>', window)
        self.assertIsNotNone(m, "Clyde form not found")
        self.assertIn("hidden", m.group(0))
        # One POST form, to the Clyde create route.
        self.assertEqual(window.count("<form"), 1)
        self.assertIn('action="/bots/preset/clyde"', window)
        # The button IS the card: same classes as a bot card.
        m = re.search(r'<button class="([^"]*)" type="submit"',
                      window)
        self.assertIsNotNone(m, "Clyde button not found")
        self.assertIn("card", m.group(1).split())
        self.assertIn("bot-card", m.group(1).split())
        # Identical to the Moonshot card: 🔥 icon, name, chevron.
        self.assertIn("🔥", window)
        self.assertIn("<strong>Clyde</strong>", window)
        self.assertIn("bot-arrow", window)
        # Gold Preset pill — never "Active" / "Silent ON": no bot
        # exists yet.
        self.assertIn('badge preset">Preset<', window)
        self.assertNotIn("Silent ON", window)
        self.assertNotIn("● Active", window)
        # Clyde's stats line, same format as a bot card.
        self.assertIn("bold · Buy 20% · Stop 4% · Max 3", window)
        # No bot-yet lines, no description of any kind.
        self.assertNotIn("ID:", window)
        self.assertNotIn("Created:", window)
        self.assertNotIn("preset-rules", window)
        self.assertNotIn("preset-note", window)
        self.assertNotIn("preset-card", window)
        self.assertNotIn("data-preset=", window)
        # No other preset in the window at all.
        self.assertNotIn("Trenches", window)
        self.assertNotIn("Scanner", window)
        self.assertNotIn("/bots/preset/trenches", window)
        self.assertNotIn("/bots/preset/scanner", window)
        # None of the rules text survives in the window.
        self.assertNotIn("30 seconds", window)
        self.assertNotIn("$45,000", window)
        self.assertNotIn("2.5 standard deviations", window)
        self.assertNotIn("velocity-swap", window)

    def test_clyde_card_same_carousel_slot_as_bot_card(self):
        """The Clyde card takes the same carousel slot as a bot
        card (no outer wrapper, no second card border): the form
        is a 92% carousel item like .bot-card, the button fills
        it, and the button carries the card styling (same .card
        + .bot-card as an active bot card)."""
        self._choose_mode("paper")
        self._html("/bots")
        css = open("tradingbot/static/style.css").read()
        # No preset-window styling left at all.
        self.assertNotIn("bots-presets", css)
        self.assertNotIn("preset-list", css)
        # The Clyde form is a carousel item: same 92% slot and
        # snap as a bot card.
        self.assertRegex(
            css,
            r"#active-bots-list form\[data-preset-item\]\s*\{[^}]*"
            r"flex:\s*0 0 92%")
        self.assertRegex(
            css,
            r"#active-bots-list form\[data-preset-item\]\s*\{[^}]*"
            r"scroll-snap-align:\s*start")
        # Items hide via [hidden] — .bot-card is display:flex,
        # which beats the UA [hidden] rule.
        self.assertIn(".bot-carousel [hidden]", css)
        # The <button> UA chrome is reset so it renders like the
        # <a class="card bot-card">.
        self.assertIn("button.bot-card", css)
        self.assertRegex(css,
                         r"button\.bot-card\s*\{[^}]*font:\s*inherit")
        self.assertRegex(css,
                         r"button\.bot-card\s*\{[^}]*text-align:\s*left")
        self.assertRegex(css,
                         r"button\.bot-card\s*\{[^}]*cursor:\s*pointer")
        # Gold Preset pill.
        self.assertRegex(css, r"\.badge\.preset\s*\{[^}]*#ffd700")
        # The button still gets the card look from the shared
        # classes (gold border, dark background).
        self.assertRegex(css, r"\.card\s*\{[^}]*#ffb300")

    def test_clyde_ruleset_validates(self):
        cleaned, err = validate_ruleset(clyde_ruleset())
        self.assertIsNone(err, err)
        self.assertEqual(cleaned["kind"], "scanner")
        self.assertEqual(cleaned["scan_interval_seconds"], 30)
        self.assertEqual(cleaned["top10_rug"]["concentration_pct"], 12.0)
        self.assertEqual(cleaned["buy_filter"]["min_market_cap_usd"],
                         45000.0)
        self.assertEqual(cleaned["trailing_stop_pct"], 4.0)
        # Baseline ladder stays inside 10-30%.
        self.assertEqual(
            [r["profit_pct"] for r in cleaned["sell_ladder"]],
            [10.0, 20.0, 30.0])
        # Runner ladder stays inside 31-1000%.
        self.assertEqual(
            [r["profit_pct"] for r in cleaned["trending_ladder"]],
            [31.0, 100.0, 500.0, 1000.0])

    def test_preset_bots_order(self):
        names = [p["name"] for p in preset_bots()]
        self.assertEqual(names, ["Clyde", "Trenches", "Scanner"])

    def test_create_clyde_from_preset(self):
        self._choose_mode("paper")
        resp = self.client.post(
            "/bots/preset/clyde",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        row = self.db.execute(
            "SELECT * FROM bots WHERE user_id = ? AND name = 'Clyde'",
            (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "Clyde bot was not created")
        # Clyde's hard numbers, on the bot and in its rules.
        self.assertEqual(row["stop_loss_pct"], 4.0)
        self.assertEqual(row["mode"], "paper")
        self.assertEqual(row["silent"], 0)
        self.assertEqual(row["platform"], "pump.fun")
        rules = json.loads(row["rules_json"])
        self.assertEqual(rules["kind"], "scanner")
        self.assertEqual(rules["scan_interval_seconds"], 30)
        self.assertEqual(rules["top10_rug"]["concentration_pct"], 12.0)
        self.assertEqual(rules["buy_filter"]["min_market_cap_usd"],
                         45000.0)
        self.assertEqual(rules["trailing_stop_pct"], 4.0)
        self.assertEqual(
            [r["profit_pct"] for r in rules["sell_ladder"]],
            [10.0, 20.0, 30.0])
        self.assertEqual(
            [r["profit_pct"] for r in rules["trending_ladder"]],
            [31.0, 100.0, 500.0, 1000.0])
        # The created bot's rules validate — the loop can run them.
        cleaned, err = validate_ruleset(rules)
        self.assertIsNone(err, err)
        # Lands on the new bot's detail page.
        self.assertIn("/bots/", resp.headers.get("Location", ""))

    def test_create_clyde_twice_goes_to_existing(self):
        self._choose_mode("paper")
        self.client.post(
            "/bots/preset/clyde",
            data={"csrf_token": csrf(self.client, "/bots")})
        resp = self.client.post(
            "/bots/preset/clyde",
            data={"csrf_token": csrf(self.client, "/bots")})
        self.assertEqual(resp.status_code, 302)
        rows = self.db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND name = 'Clyde'",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1, "duplicate Clyde bot created")


if __name__ == "__main__":
    unittest.main()
