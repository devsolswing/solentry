"""Preset bots on Page 6 (Bots) — the owner's spec (2026-11-11).

- A Preset button sits in the bot-actions row (same action-btn
  style as Add Bot / Edit Bot) and opens the preset list.
- The list is Clyde first, then Trenches, then Scanner. Each
  preset shows its name, its rules in plain words, and a Create
  button that really creates the bot.
- Clyde (owner's exact rules): scan the pump.fun trenches every
  30 seconds; hard minimum market cap $45,000 CAD; top-10 wallets
  under 12% of supply; entry is a Z-score (buy pressure 2.5
  standard deviations over the trailing average) plus a velocity
  shift; hard 4% stop loss; take profit 10-30% baseline,
  31-1000% for confirmed runners; swap underperformers for
  higher-velocity runners regardless of P&L.
- The engine has no Z-score field, no velocity-swap rule, and no
  pre-buy top-10 screen — those are recorded in the preset and
  stated plainly on the list, never pretended to be enforced.
  The engine enforces the closest available signals: 5% gain in
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
        self.assertIn('aria-controls="preset-list"', html)
        # Same sizing class as Add Bot / Edit Bot.
        m = re.search(r'class="([^"]*action-btn[^"]*)"[^>]*>Presets',
                      html)
        self.assertIsNotNone(m, "Presets button not found")
        self.assertIn("action-btn", m.group(1).split())

    def test_preset_list_hidden_by_default(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        self.assertIn('id="preset-list"', html)
        # Hidden until the Presets button opens it.
        m = re.search(r'<div[^>]*id="preset-list"[^>]*>', html)
        self.assertIsNotNone(m, "preset list not found")
        self.assertIn("hidden", m.group(0))
        css = open("tradingbot/static/style.css").read()
        self.assertIn(".bots-presets", css)
        self.assertIn(".bots-presets[hidden]", css)

    def test_clyde_listed_first_with_rules(self):
        self._choose_mode("paper")
        html = self._html("/bots")
        # Clyde first, then the existing presets — scoped to the
        # preset list itself (the word "Trenches" also appears in
        # the assistant's hint text higher on the page).
        clyde = html.find('data-preset="clyde"')
        trenches = html.find('data-preset="trenches"')
        scanner = html.find('data-preset="scanner"')
        self.assertGreater(clyde, -1, "Clyde not listed")
        self.assertGreater(trenches, -1, "Trenches not listed")
        self.assertGreater(scanner, -1, "Scanner not listed")
        self.assertLess(clyde, trenches)
        self.assertLess(trenches, scanner)
        # Clyde's enforced rules, in plain words.
        self.assertIn("30 seconds", html)
        self.assertIn("$45,000", html)
        self.assertIn("12%", html)
        self.assertIn("4.0%", html)
        # Take-profit brackets: baseline 10-30%, runners 31-1000%.
        self.assertIn("+10% profit", html)
        self.assertIn("+30% profit", html)
        self.assertIn("+31% profit", html)
        self.assertIn("+1000% profit", html)
        # The rules the engine cannot enforce as written are stated
        # plainly, not dropped: Z-score entry, CAD market cap,
        # pre-buy top-10 screen, velocity swap.
        self.assertIn("2.5 standard deviations", html)
        self.assertIn("CAD", html)
        self.assertIn("does not pre-screen", html)
        self.assertIn("velocity-swap", html)
        # Create buttons post to the preset routes.
        self.assertIn("/bots/preset/clyde", html)
        self.assertIn("/bots/preset/trenches", html)
        self.assertIn("/bots/preset/scanner", html)

    def test_preset_list_theme_black_gold_red(self):
        self._choose_mode("paper")
        self._html("/bots")
        css = open("tradingbot/static/style.css").read()
        # Black card, gold border, red letters — the page stays
        # toned down (no yellow wash).
        self.assertRegex(css, r"\.bots-presets\s*\{[^}]*background:\s*#000")
        self.assertRegex(css, r"\.bots-presets\s*\{[^}]*#ffd700")
        self.assertRegex(css, r"\.bots-presets[^{]*\{[^}]*#b91c1c")

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
