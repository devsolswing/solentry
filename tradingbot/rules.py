"""Per-bot trading rules: structured, validated, fail-closed.

A rule set is plain JSON stored on the bot row (bots.rules_json). The
silent loop only ever trades inside a bot's set limits — buy size %,
stop loss %, max open trades, balance — and those limits live ON THE
BOT, never in the rule set: any rule set that tries to carry a limit
field is rejected outright, so a rule can never widen a limit.

Rule sets come from two places: the in-app assistant's deterministic
parser (assistant.py) and built-in presets (TRENCHES_RULES below).
Both go through validate_ruleset() before anything is stored, and the
loop re-validates on every evaluation — an unparseable rule set means
NO trades, never a guess.

Schema (version 1):
    {
      "version": 1,
      "kind": "trenches" | "signals" | "scanner",
      "scan_sources": ["pump.fun", "pumpswap"],   # trenches/scanner only
      "scan_interval_seconds": 60,
      "buy": {"metric": "gain_5m_pct", "window_seconds": 300,
              "threshold_pct": 3.4, "pick": "strongest"},
      "sell_ladder": [{"pct_of_position": 10, "profit_pct": 3},
                      {"pct_of_position": 90, "profit_pct": 10}],
      "trailing_stop_pct": 7.0,
      "rug_sell": {"drop_pct": 15.0, "window_seconds": 60,
                   "drop_5m_pct": 25.0},
      "top10_rug": {"drop_pct": 10.0},
      "trending_ladder": [{"pct_of_position": 10, "profit_pct": 26}],
      "regime": {"volume_surge_x": 2.0, "require_buy_dominance": True,
                 "require_h1_gain": True},
      "max_new_positions_per_scan": 1,
    }

  * rug_sell is the fast-drop exit: sell ALL immediately when the
    price collapses — either a drop of drop_pct (default 15%) since
    the last recorded price mark, where the mark is at most
    window_seconds old (60 or 300), or a fall of drop_5m_pct
    (default 25%) in the feed's 5-minute change. It is checked
    FIRST in the position evaluation, before the stop loss, trailing
    stop and sell ladder, so it fires on the same scan that sees the
    collapse. It is PRICE-based: the feed (DexScreener) cannot see
    the token creator's wallet, so a dev selling shows up only as
    the price falling — the rule triggers on that fall, never on a
    faked "dev sold" signal.

  * kind "trenches": scan pump.fun / PumpSwap new tokens through the
    DexScreener feed (trenches.py). kind "signals": the bot's own coin
    list — pump.fun mint addresses, evaluated on the trenches feed's
    24h moves. kind "scanner": the
    ∆scanner∆ sniper — scan pump.fun / PumpSwap every 30 seconds for
    gainers, buy a coin gaining 5% in 3 minutes (strongest gainer
    wins), tracked from price marks the loop records itself
    (scanner.py), because the feed only provides 5-minute changes.
    Its sell_ladder is the SLOW-market ladder (25% at +5%, +10%,
    +25%, the rest at +50%) and trending_ladder is the
    TRENDING-market ladder (10% at +26%, +50%, +100%, +500%,
    +1000%, the rest at +10000%) — the regime (see below) picks
    which ladder a position follows on each scan. The 7% trailing
    stop from the peak stays as the runner's profit lock, and the
    stop loss (7% below the buy price, the bot's own limit) and the
    rug sell still fire regardless of cost — a bad coin is cut at a
    small loss. Its top10_rug watches the top 10 holders of every
    held coin through Solana RPC getTokenLargestAccounts: a 10%
    drop in their combined holdings from the peak since holding
    sells everything, proceeds in SOL.
  * regime (scanner only): what counts as a TRENDING market, judged
    from the DexScreener feed — the app has NO social-media feed.
    Trending needs the 5-minute volume at least volume_surge_x
    times the average 5-minute pace of the last hour, buys
    outnumbering sells in the last 5 minutes, and a positive
    1-hour change (each requirement can be switched off). Any
    missing data means SLOW — the slow ladder takes profits early
    rather than holding on a guess.
  * buy.metric must match buy.window_seconds: 180 -> gain_3m_pct
    (scanner only, computed from recorded marks), 300 -> gain_5m_pct,
    3600 -> gain_1h_pct, 21600 -> gain_6h_pct, 86400 -> gain_24h_pct.
    Those are the windows the feeds actually provide, plus the
    3-minute window the scanner computes itself.
  * sell_ladder rungs are % of the ORIGINAL position amount, executed
    in order as profit rises; rung profit thresholds must strictly
    increase and the rung sizes must sum to <= 100. A scanner's
    sell_ladder is its slow-market ladder (up to 4 rungs) and
    trending_ladder its trending-market ladder (up to 6 rungs,
    profit targets up to +10000%); the loop tracks how much of the
    position is already sold and fires the next rung of whichever
    ladder the regime picks, so a regime flip mid-position never
    re-sells a slice or loses track.
  * trailing_stop_pct sells ALL when the price falls that far below
    the highest price since the position opened. The bot's own stop
    loss % is still enforced separately as a hard stop.
"""

from __future__ import annotations

import copy
import json

from .scanner import (BUY_FILTER_DEFAULT, BUY_FILTER_KEYS, REGIME_DEFAULT,
                      SLOW_LADDER, TRENDING_LADDER)

RULE_VERSION = 1
RULE_KINDS = ("trenches", "signals", "scanner", "gnat")
RULE_SOURCES = ("pump.fun", "pumpswap", "dexscreener")

# Windows the feeds provide, in seconds -> buy-metric name. The
# 3-minute window is the scanner's own: it is computed from price
# marks the loop records (scanner.py), not from the feed, which only
# provides 5-minute changes.
WINDOW_METRICS = {
    180: "gain_3m_pct",
    300: "gain_5m_pct",
    3600: "gain_1h_pct",
    21600: "gain_6h_pct",
    86400: "gain_24h_pct",
}

# Fields that are limits and therefore live on the bot row, never in a
# rule set. Any rule set carrying one is rejected outright.
LIMIT_FIELDS = (
    "buy_size_pct", "max_open_trades", "stop_loss_pct", "balance_cap",
    "max_buy_usd", "buy_size_usd", "max_position_usd",
)

_TOP_LEVEL_KEYS = (
    "version", "kind", "scan_sources", "scan_interval_seconds", "buy",
    "sell_ladder", "trailing_stop_pct", "rug_sell", "top10_rug",
    "trending_ladder", "regime", "buy_filter",
    "max_new_positions_per_scan",
)

# Rug-sell defaults (see the module docstring): sell everything on a
# 15% drop in about a minute, or a 25% fall in 5 minutes. The windows
# are the only ones the data supports: 60 seconds is one scan apart
# (the price-mark comparison) and 300 seconds is the feed's 5-minute
# change.
RUG_SELL_DEFAULT = {"drop_pct": 15.0, "window_seconds": 60,
                    "drop_5m_pct": 25.0}
RUG_SELL_KEYS = ("drop_pct", "window_seconds", "drop_5m_pct")
RUG_WINDOWS = (60, 300)

# Top-10 wallets rug sense (scanner and trenches — the memecoin
# kinds): sell everything when the top 10 holders' combined holdings
# drop this far from the peak since holding (default 10%, James's
# number); sell everything when their combined share of TOTAL token
# supply reaches concentration_pct (default 21%); sell
# slow_climb_sell_pct (default 50%) when that share keeps rising
# toward the concentration level over consecutive 15-second checks
# (the slow-climb early rug warning). The check runs every
# check_interval_seconds (default 15) per held coin.
TOP10_RUG_DEFAULT = {"drop_pct": 10.0, "concentration_pct": 21.0,
                     "slow_climb_sell_pct": 50.0,
                     "check_interval_seconds": 15.0}
TOP10_RUG_KEYS = ("drop_pct", "concentration_pct", "slow_climb_sell_pct",
                  "check_interval_seconds")

TRENCHES_CATEGORY = "Memecoin build"
TRENCHES_PRESET_NAME = "Trenches"

# James's spec, exactly: scan the trenches every minute; buy a coin
# gaining 3.4% in a 5-minute window (strongest gainer wins); sell 10%
# at +3% profit, the rest at +10%; sell all at 7% below the peak.
# Rug sell on by default: sell everything on a 15% drop in about a
# minute, or a 25% fall in 5 minutes — the fast-drop exit that gets
# out before a dev rug finishes. No market-cap filter: a trending
# coin qualifies at any market cap.
TRENCHES_RULES = {
    "version": 1,
    "kind": "trenches",
    "scan_sources": ["pump.fun", "pumpswap"],
    "scan_interval_seconds": 60,
    "buy": {"metric": "gain_5m_pct", "window_seconds": 300,
            "threshold_pct": 3.4, "pick": "strongest"},
    "sell_ladder": [{"pct_of_position": 10, "profit_pct": 3},
                    {"pct_of_position": 90, "profit_pct": 10}],
    "trailing_stop_pct": 7.0,
    "rug_sell": dict(RUG_SELL_DEFAULT),
    "buy_filter": dict(BUY_FILTER_DEFAULT),
    "max_new_positions_per_scan": 1,
}


def trenches_ruleset() -> dict:
    """A fresh copy of the Trenches preset."""
    return copy.deepcopy(TRENCHES_RULES)


# --- ∆scanner∆ sniper preset -------------------------------------------

SCANNER_CATEGORY = "scanner"
SCANNER_DISPLAY = "∆scanner∆"
SCANNER_PRESET_NAME = "Scanner"
SCANNER_SOURCES = ("pump.fun", "pumpswap")
SCANNER_INTERVAL_SECONDS = 30
SCANNER_BUY_GAIN_PCT = 5.0
SCANNER_BUY_WINDOW_SECONDS = 180
SCANNER_MAX_NEW_PER_SCAN = 1
SCANNER_TOP10_DROP_PCT = 10.0
# A scanner's profit ladders (James's spec): the slow-market ladder
# secures a profit early (25% at +5%, +10%, +25%, the rest at
# +50%); the trending-market ladder holds the runners (10% at
# +26%, +50%, +100%, +500%, +1000%, the rest at +10000% or on the
# trailing stop). The regime picks which ladder a position follows
# on each scan. The stop loss (7% below the buy price, the bot's
# own limit) and the rug sell still fire regardless of cost — a bad
# coin is cut at a small loss.
SCANNER_TRAILING_STOP_PCT = 7.0
REGIME_KEYS = ("volume_surge_x", "require_buy_dominance",
               "require_h1_gain")

# James's spec, exactly: buy any coin gaining 5% in 3 minutes; scan
# every 30 seconds for gainers and keep track of improving coins
# (the strongest 3-minute gainer wins); watch the top 10 wallets of
# every held coin closely and sell everything if their combined
# holdings drop 10% — the pump.fun sell pays out in SOL, so the
# proceeds stay in SOL. The 3-minute gain is computed from price
# marks the loop records itself (scanner.py) because the DexScreener
# feed only provides 5-minute changes. Profit: slow market — 25% at
# +5%, +10%, +25%, the rest at +50%; trending market — 10% at
# +26%, +50%, +100%, +500%, +1000%, the rest at +10000% or on the
# 7% trailing stop. A bad coin is cut at the 7% stop loss.
SCANNER_RULES = {
    "version": 1,
    "kind": "scanner",
    "scan_sources": list(SCANNER_SOURCES),
    "scan_interval_seconds": SCANNER_INTERVAL_SECONDS,
    "buy": {"metric": "gain_3m_pct",
            "window_seconds": SCANNER_BUY_WINDOW_SECONDS,
            "threshold_pct": SCANNER_BUY_GAIN_PCT,
            "pick": "strongest"},
    "sell_ladder": [dict(r) for r in SLOW_LADDER],
    "trailing_stop_pct": SCANNER_TRAILING_STOP_PCT,
    "rug_sell": dict(RUG_SELL_DEFAULT),
    "top10_rug": {"drop_pct": SCANNER_TOP10_DROP_PCT,
                  "concentration_pct": 21.0,
                  "slow_climb_sell_pct": 50.0,
                  "check_interval_seconds": 15.0},
    "trending_ladder": [dict(r) for r in TRENDING_LADDER],
    "regime": dict(REGIME_DEFAULT),
    # Tuning build 2026-11-15: the 5-minute volume floor is $5,000
    # (was $1,000) — thin coins are trash-filtered before any buy.
    # The default filter (BUY_FILTER_DEFAULT, $1,000) is untouched,
    # so the Trenches preset keeps its own floor.
    "buy_filter": {"min_volume_5m_usd": 5000.0,
                   "require_buy_dominance": True,
                   "min_signal_pct": SCANNER_BUY_GAIN_PCT,
                   "min_market_cap_usd": 0.0},
    "max_new_positions_per_scan": SCANNER_MAX_NEW_PER_SCAN,
}


def scanner_ruleset() -> dict:
    """A fresh copy of the ∆scanner∆ preset."""
    return copy.deepcopy(SCANNER_RULES)


# --- Clyde preset ------------------------------------------------------

CLYDE_CATEGORY = "Clyde"
CLYDE_PRESET_NAME = "Clyde"
CLYDE_SCAN_INTERVAL_SECONDS = 30
CLYDE_BUY_THRESHOLD_PCT = 5.0
CLYDE_STOP_LOSS_PCT = 4.0
CLYDE_TOP10_CONCENTRATION_PCT = 12.0
CLYDE_MIN_MARKET_CAP = 45000.0
CLYDE_ZSCORE_STD = 2.5

# Clyde's spec, exactly as the owner gave it (2026-11-11): scan the
# pump.fun trenches every 30 seconds via DexScreener; hard minimum
# market cap $45,000 CAD before any capital deployment; the top ten
# holding wallets must command less than 12% of total circulating
# supply, instantly blacklisting any pool failing this; entry needs
# an exponential velocity shift in liquidity-pool injections plus a
# volume-weighted Z-score where buy pressure beats the trailing
# rolling average by at least 2.5 standard deviations; a hard 4%
# stop loss on every position; take profit 10% to 30% on standard
# baseline plays, 31% to 1000% on confirmed structural runners
# with extreme volume acceleration; systematically swap assets by
# evicting underperforming coins for higher-velocity trench runners,
# regardless of the position's current P&L.
#
# How each rule maps to the engine (kind "scanner" — the only kind
# with a 30-second scan and two profit ladders, baseline + runners):
#   * scan every 30s -> scan_interval_seconds 30.
#   * min market cap $45,000 CAD -> buy_filter min_market_cap_usd
#     45000. The engine's field is USD, so storing 45000 there is a
#     STRICTER bar than $45,000 CAD (about $33,000 USD) — a coin
#     under $45,000 CAD can never pass. The CAD origin is stated in
#     CLYDE_NOTES so the unit is never hidden.
#   * top-10 < 12% -> top10_rug concentration_pct 12.0: the engine
#     sells everything when a held coin's top-10 share reaches 12%,
#     and pre-screens the same bar before buying (tuning build
#     2026-11-15).
#   * 4% stop loss -> the bot row's stop_loss_pct (a limit, never in
#     the rule set) AND trailing_stop_pct 4.0.
#   * take profit baseline 10-30% -> sell_ladder (25% at +10%, 25%
#     at +20%, 50% at +30%); runners 31-1000% -> trending_ladder
#     (10% at +31%, +100%, +500%, 70% at +1000%).
#   * Z-score entry and velocity swap have NO engine field —
#     they are recorded in CLYDE_NOTES and shown on the preset
#     list, never pretended to be enforced. The engine's closest
#     available entry signal is the scanner's 5% gain in 3 minutes
#     (buy threshold 5.0, and the buy filter's min_signal_pct 5.0),
#     gated on the four-part momentum check and a second-scan
#     confirmation. The pre-buy top-10 blacklist IS enforced now
#     (tuning build 2026-11-15): the loop reads the top-10 share
#     through the mainnet RPC before buying and rejects any coin
#     at or over 12%, and rejects the buy outright when the RPC
#     cannot serve the mint.
CLYDE_RULES = {
    "version": 1,
    "kind": "scanner",
    "scan_sources": ["pump.fun", "pumpswap"],
    "scan_interval_seconds": CLYDE_SCAN_INTERVAL_SECONDS,
    "buy": {"metric": "gain_3m_pct",
            "window_seconds": SCANNER_BUY_WINDOW_SECONDS,
            "threshold_pct": CLYDE_BUY_THRESHOLD_PCT,
            "pick": "strongest"},
    "sell_ladder": [{"pct_of_position": 25, "profit_pct": 10},
                    {"pct_of_position": 25, "profit_pct": 20},
                    {"pct_of_position": 50, "profit_pct": 30}],
    "trailing_stop_pct": CLYDE_STOP_LOSS_PCT,
    "rug_sell": dict(RUG_SELL_DEFAULT),
    "top10_rug": {"drop_pct": 10.0,
                  "concentration_pct": CLYDE_TOP10_CONCENTRATION_PCT,
                  "slow_climb_sell_pct": 0.0,
                  "check_interval_seconds": 15.0},
    "trending_ladder": [{"pct_of_position": 10, "profit_pct": 31},
                        {"pct_of_position": 10, "profit_pct": 100},
                        {"pct_of_position": 10, "profit_pct": 500},
                        {"pct_of_position": 70, "profit_pct": 1000}],
    "regime": dict(REGIME_DEFAULT),
    # Tuning build 2026-11-15: the 5-minute volume floor is $5,000
    # (was $1,000) — thin coins are trash-filtered before any buy.
    "buy_filter": {"min_volume_5m_usd": 5000.0,
                   "require_buy_dominance": True,
                   "min_signal_pct": CLYDE_BUY_THRESHOLD_PCT,
                   "min_market_cap_usd": CLYDE_MIN_MARKET_CAP},
    "max_new_positions_per_scan": 1,
}


def clyde_ruleset() -> dict:
    """A fresh copy of the Clyde preset."""
    return copy.deepcopy(CLYDE_RULES)


# Rules Clyde states that the engine cannot enforce as written.
# Shown on the preset list, in plain words, next to the enforced
# rules — never dropped, never pretended to be enforced.
CLYDE_NOTES = [
    "Entry — Z-score: buy pressure must beat the trailing rolling "
    "average by at least 2.5 standard deviations (volume-weighted "
    "Z-score), with an exponential velocity shift in "
    "liquidity-pool injections. The engine has no Z-score field, "
    "so this is recorded here and the engine enforces the closest "
    "available signal instead: a coin gaining 5% in 3 minutes that "
    "also passes the four-part momentum gate (5-minute gain, "
    "rising volume, buyers outnumbering sellers, positive 1-hour "
    "trend) and confirms on a second scan.",
    "Market cap — $45,000 CAD minimum. The engine's market-cap "
    "field is in USD, so the preset stores $45,000 in that field — "
    "a stricter bar than $45,000 CAD, so a coin under $45,000 CAD "
    "can never pass.",
    "Top-10 — the top 10 wallets must hold less than 12% of "
    "supply. The engine enforces this twice: it pre-screens "
    "concentration before buying (a coin at or over 12% is never "
    "bought) and it sells everything when a held coin's top-10 "
    "share reaches 12%. A coin the RPC cannot serve is not bought "
    "at all — unverified is rejected, never guessed.",
    "Portfolio swap — evict underperforming coins for "
    "higher-velocity trench runners, regardless of the "
    "position's current P&L. The engine has no velocity-swap "
    "rule, so this is recorded here — the silent loop does not "
    "auto-swap positions. Underperformers exit only on the stop "
    "loss, trailing stop, rug sell, or the ladders.",
    "Runners — a position follows the runner ladder (31% to "
    "1000%) when the market regime is trending: 5-minute volume "
    "at least 2x the hourly pace, buys outnumbering sells, and a "
    "positive 1-hour change. That is the engine's closest proxy "
    "for 'extreme volume acceleration'.",
]


GNAT_CATEGORY = "Gnat"
GNAT_PRESET_NAME = "Gnat"
GNAT_SCAN_INTERVAL_SECONDS = 30
# James's spec values (2026-11-21) — REMY DEFAULTS awaiting his call.
# They are his suggested numbers, not verified rules: label them so
# everywhere they surface, and never present them as his final word.
GNAT_BUY_THRESHOLD_PCT = 5.0
GNAT_BUY_WINDOW_SECONDS = 180
GNAT_STOP_LOSS_PCT = 18.0
GNAT_BUY_SIZE_PCT = 18
# Fixed size: 0.25 SOL per trade (James's spec). The app's buy model
# is %-of-capital, so the loop converts 0.25 SOL at the live SOL
# price when it can, and falls back to GNAT_BUY_SIZE_PCT of the
# paper bankroll when no live SOL price exists (see silent_loop).
GNAT_BUY_SIZE_SOL = 0.25
GNAT_MAX_OPEN_TRADES = 3
# Curve band: buy only while the bonding curve is 12-65% filled.
GNAT_CURVE_MIN_PCT = 12.0
GNAT_CURVE_MAX_PCT = 65.0
# Buy-pressure gates (closest version — see GNAT_NOTES): the 5-minute
# DexScreener txn bucket stands in for the spec's 30-second window.
GNAT_MIN_BUYERS_5M = 15
GNAT_MIN_BUY_SELL_RATIO = 2.5
# Social links required at entry (spec left TRUE/FALSE open —
# default TRUE, James's call).
GNAT_REQUIRE_SOCIALS = True
# Take-profit tiers: Tier 1 +40% sell 35% (stop on the rest moves to
# breakeven +2%); Tier 2 +100% sell another 35% of the ORIGINAL
# position; the remaining 30% rides the trailing stop.
GNAT_TIER1_PROFIT_PCT = 40.0
GNAT_TIER1_SELL_PCT = 35.0
GNAT_BREAKEVEN_PLUS_PCT = 2.0
GNAT_TIER2_PROFIT_PCT = 100.0
GNAT_TIER2_SELL_PCT = 35.0
# Trailing stop on the moonbag: 18% while +100% to +250%, 12%
# above +250%.
GNAT_TRAIL_LOW_PCT = 18.0
GNAT_TRAIL_HIGH_PCT = 12.0
GNAT_TRAIL_TIGHTEN_AT_PCT = 250.0
# Momentum exhaustion (closest version): peak at least +25% and the
# 5-minute change at or below -5% exits the rest of the position.
GNAT_EXHAUST_MIN_PEAK_PCT = 25.0
GNAT_EXHAUST_DROP_5M_PCT = 5.0
# Time decay: not at least +15% within 90 seconds of entry → exit
# 100%.
GNAT_TIME_DECAY_SECONDS = 90
GNAT_TIME_DECAY_MIN_GAIN_PCT = 15.0
# Circuit breakers: halt all new entries for the rest of the day on
# 2.0 SOL of realized losses, or after 4 consecutive losing trades.
GNAT_MAX_DAILY_LOSS_SOL = 2.0
GNAT_MAX_CONSEC_LOSSES = 4

GNAT_RULES = {
    "version": 1,
    "kind": "gnat",
    "scan_sources": ["pump.fun", "pumpswap"],
    "scan_interval_seconds": GNAT_SCAN_INTERVAL_SECONDS,
    "buy": {"metric": "gain_3m_pct",
            "window_seconds": GNAT_BUY_WINDOW_SECONDS,
            "threshold_pct": GNAT_BUY_THRESHOLD_PCT,
            "pick": "strongest"},
    # TP tiers as a plain sell ladder: 35% at +40%, then 35% of the
    # ORIGINAL position at +100%. The loop sells ladder rungs as a
    # fraction of the original amount (not the remaining amount),
    # so these two rungs land exactly as specced; the remaining
    # 30% rides the trailing stop below.
    "sell_ladder": [{"pct_of_position": GNAT_TIER1_SELL_PCT,
                     "profit_pct": GNAT_TIER1_PROFIT_PCT},
                    {"pct_of_position": GNAT_TIER2_SELL_PCT,
                     "profit_pct": GNAT_TIER2_PROFIT_PCT}],
    "trailing_stop_pct": GNAT_TRAIL_LOW_PCT,
    "rug_sell": dict(RUG_SELL_DEFAULT),
    "top10_rug": {"drop_pct": 10.0,
                  "concentration_pct": 25.0,
                  "slow_climb_sell_pct": 0.0,
                  "check_interval_seconds": 15.0},
    "buy_filter": {"min_volume_5m_usd": 5000.0,
                   "require_buy_dominance": True,
                   "min_signal_pct": GNAT_BUY_THRESHOLD_PCT,
                   "min_market_cap_usd": 0.0},
    "max_new_positions_per_scan": 1,
}


def gnat_ruleset() -> dict:
    """A fresh copy of the Gnat preset."""
    return copy.deepcopy(GNAT_RULES)


# Rules Gnat states that the engine cannot enforce as written.
# Shown on the preset list, in plain words, next to the enforced
# rules — never dropped, never pretended to be enforced.
GNAT_NOTES = [
    "Entry — net SOL inflow: at least 5 SOL of net positive inflow "
    "across at least 15 unique buyers within 30 seconds. The app "
    "has no sub-5-minute flow data and no wallet identities, so "
    "the engine enforces the closest version instead: the coin "
    "must show at least $5,000 of 5-minute volume, at least 15 buy "
    "transactions in the 5-minute bucket, and a buy-to-sell "
    "transaction ratio of at least 2.5:1 — counts, not wallets, "
    "over 5 minutes, not 30 seconds.",
    "Entry — bonding curve: buy only while the curve is 12-65% "
    "filled, read straight from the on-chain bonding-curve "
    "account (virtual token reserves vs the 1,073,000,000,000,000 "
    "initial). A curve the RPC cannot read is never bought.",
    "Entry — metadata and socials: the coin must have a valid "
    "metadata URI (the on-chain Metaplex metadata account must "
    "carry a non-empty URI) and at least one published social or "
    "website link (counted from the DexScreener pair info). The "
    "app cannot check whether those links are live — presence "
    "only, never verified active. REQUIRE_SOCIALS defaults to "
    "TRUE; James decides whether it stays on.",
    "Anti-rug — creator: reject if the creator holds more than 5% "
    "or has a rapid-dump history. NOT ENFORCED — the app has no "
    "creator data source (no creator wallet, no launch history). "
    "Recorded here, never faked. The price-based rug sell and the "
    "top-10 watch are the live protection instead.",
    "Anti-rug — top-10: reject if the top 10 non-curve holders "
    "control more than 25%. ENFORCED twice: the pre-buy scam gate "
    "rejects a coin at or over 25% concentration, and the top-10 "
    "watch sells everything when a held coin's share reaches 25%.",
    "Momentum exhaustion: exit when the rolling 10-second net SOL "
    "flow flips negative by more than 4 SOL after being up at "
    "least +25%. The app has no 10-second flow data, so the "
    "engine enforces the closest version: when the peak is at "
    "least +25% and the 5-minute change is at or below -5%, the "
    "rest of the position exits in full.",
    "Emergency exits — creator sells any tokens, one sell removes "
    "more than 8% of the curve's virtual SOL, or buy velocity is "
    "zero for 20 seconds while in drawdown: NOT ENFORCED — the "
    "app has no wallet tracking and no per-second transaction "
    "stream. Covered by the price-based rug sell, the top-10 "
    "watch, the -18% hard stop and the 90-second time decay.",
    "Sizing: 0.25 SOL per trade. The app's buy model is "
    "%-of-capital, so the loop converts 0.25 SOL at the live SOL "
    "price when it can, and falls back to 18% of the paper "
    "bankroll when no live SOL price exists — the fallback is "
    "labelled in the log, never silent.",
    "Slippage stays at the app's 1% — the spec's 12%/20% is "
    "James's call, not built. Jito tips are not built (they cost "
    "SOL per trade) — also James's call.",
    "Stops are non-negotiable: the -18% hard stop, the breakeven "
    "+2% after +40%, the 18%/12% trailing tiers, the 90-second "
    "time decay and the circuit breakers are James's spec. The "
    "builderBot auto-tighten does NOT rewrite a Gnat bot's rules.",
]


def preset_bots() -> list:
    """The preset bots, in list order — Clyde first, then the
    existing presets. Each entry carries a fresh copy of its rule
    set, its plain-words notes, and the Flask endpoint that
    creates it. The list is data, so a new preset is one more
    entry here plus its create route."""
    return [
        {"key": "clyde",
         "name": CLYDE_PRESET_NAME,
         "tagline": "Trench sniper — 30-second scans, strict 12% "
                    "top-10 cap, 4% stop loss.",
         "category": CLYDE_CATEGORY,
         "rules": clyde_ruleset(),
         "notes": list(CLYDE_NOTES),
         "endpoint": "bot_preset_clyde"},
        {"key": "trenches",
         "name": TRENCHES_PRESET_NAME,
         "tagline": "Memecoin build — scan the trenches every "
                    "minute, buy the strongest 5-minute gainer.",
         "category": TRENCHES_CATEGORY,
         "rules": trenches_ruleset(),
         "notes": [],
         "endpoint": "bot_preset_trenches"},
        {"key": "scanner",
         "name": SCANNER_PRESET_NAME,
         "tagline": "∆scanner∆ sniper — buy any coin gaining 5% in "
                    "3 minutes.",
         "category": SCANNER_CATEGORY,
         "rules": scanner_ruleset(),
         "notes": [],
         "endpoint": "bot_preset_scanner"},
        {"key": "gnat",
         "name": GNAT_PRESET_NAME,
         "tagline": "Curve + buy-pressure sniper — 12-65% curve, "
                    "0.25 SOL a trade, tiered take-profit.",
         "category": GNAT_CATEGORY,
         "rules": gnat_ruleset(),
         "notes": list(GNAT_NOTES),
         "endpoint": "bot_preset_gnat"},
    ]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fail(message: str):
    return None, message


def _validate_ladder(ladder, label: str, max_rungs: int,
                     max_profit: float):
    """Validate one sell ladder. Returns (cleaned, None) or
    (None, error). Rungs are % of the original position, profit
    targets strictly increasing, sizes summing to at most 100."""
    if not isinstance(ladder, list) or not 1 <= len(ladder) <= max_rungs:
        return None, (f"A {label} ladder needs 1 to {max_rungs} "
                      f"step{'s' if max_rungs > 1 else ''}.")
    total_pct = 0
    prev_profit = None
    clean = []
    for rung in ladder:
        if not isinstance(rung, dict) \
                or set(rung) != {"pct_of_position", "profit_pct"}:
            return None, ("Each sell step needs exactly a % of the "
                          "position and a % profit.")
        pct = rung["pct_of_position"]
        profit = rung["profit_pct"]
        if not _is_number(pct) or not 1 <= float(pct) <= 100:
            return None, ("Each sell step must sell between 1% and 100% "
                          "of the position.")
        if not _is_number(profit) or not 0.1 <= float(profit) <= max_profit:
            return None, (f"Each sell step's profit target must be "
                          f"between 0.1% and {max_profit:.0f}%.")
        if prev_profit is not None and float(profit) <= prev_profit:
            return None, ("Sell steps must rise in profit — a later step "
                          "cannot trigger at a lower profit than an "
                          "earlier one.")
        prev_profit = float(profit)
        total_pct += float(pct)
        clean.append({"pct_of_position": float(pct),
                      "profit_pct": float(profit)})
    if total_pct > 100:
        return None, ("Sell steps add up to more than 100% of the "
                      "position — they cannot sell more than you hold.")
    return clean, None


def validate_ruleset(raw, bot=None):
    """Validate a parsed rule set. Returns (cleaned, None) or (None, err).

    `bot` is the bot row the rules would apply to (a sqlite Row or a
    dict); it is only used to fill the trailing-stop default from the
    bot's own stop loss. The cleaned dict is a normalized copy with
    defaults filled in. Nothing is written here.
    """
    if not isinstance(raw, dict):
        return _fail("Rules must be a set of named settings, not a list "
                     "or a single value.")
    for key in raw:
        if key in LIMIT_FIELDS:
            return _fail(f"'{key}' is a bot limit, not a rule — limits "
                         "live on the bot and a rule set cannot change "
                         "them. Set it on the bot itself.")
        if key not in _TOP_LEVEL_KEYS:
            return _fail(f"Unknown rule '{key}' — I don't know what that "
                         "means, so I changed nothing.")
    if raw.get("version") != RULE_VERSION:
        return _fail("Rules must be version 1.")
    kind = raw.get("kind")
    if kind not in RULE_KINDS:
        return _fail("The rule kind must be 'trenches' (scan pump.fun "
                     "and PumpSwap), 'scanner' (snipe gainers in "
                     "3 minutes), 'gnat' (curve + buy-pressure sniper) "
                     "or 'signals' (your own coin list).")
    interval = raw.get("scan_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) \
            or not 10 <= interval <= 3600:
        return _fail("Scan interval must be between 10 and 3600 seconds.")
    sources = raw.get("scan_sources", [])
    if kind in ("trenches", "scanner", "gnat"):
        if not isinstance(sources, list) or not sources:
            return _fail("A trenches, scanner or gnat rule set needs at "
                         "least one scan source (pump.fun or "
                         "pumpswap).")
        for s in sources:
            if s not in RULE_SOURCES:
                return _fail(f"Unknown scan source '{s}'.")
    else:
        if sources:
            return _fail("A signals rule set does not scan sources — "
                         "it watches your own coin list.")
    buy = raw.get("buy")
    if not isinstance(buy, dict):
        return _fail("A buy signal is required (for example: buy when a "
                     "coin gains 5% in 5 minutes).")
    for key in buy:
        if key in LIMIT_FIELDS:
            return _fail(f"'{key}' is a bot limit, not a rule.")
        if key not in ("metric", "window_seconds", "threshold_pct", "pick"):
            return _fail(f"Unknown buy setting '{key}'.")
    window = buy.get("window_seconds")
    if window not in WINDOW_METRICS:
        return _fail("Buy window must be 3 minutes (scanner and gnat "
                     "only), 5 minutes, 1 hour, 6 hours, or 24 hours — "
                     "those are the windows the price feed provides, "
                     "plus the 3-minute window the scanner tracks "
                     "itself.")
    if kind in ("scanner", "gnat") \
            and window != SCANNER_BUY_WINDOW_SECONDS:
        return _fail("A scanner or gnat buys on a 3-minute gain — "
                     "that is the only window the scanner tracks.")
    if buy.get("metric") != WINDOW_METRICS[window]:
        return _fail("Buy metric does not match its window.")
    threshold = buy.get("threshold_pct")
    if not _is_number(threshold) or not 0.1 <= float(threshold) <= 100:
        return _fail("Buy threshold must be between 0.1% and 100%.")
    if buy.get("pick", "strongest") != "strongest":
        return _fail("Only 'strongest gainer wins' is supported for "
                     "picking between coins.")
    ladder = raw.get("sell_ladder")
    if kind == "scanner" and (ladder is None or ladder == []):
        # A scanner with no slow ladder gets the default one — the
        # slow ladder is the scanner's profit-taking, not an
        # optional extra. (Stored rules from before the ladders
        # existed load as the new defaults on the next evaluation.)
        clean_ladder = [dict(r) for r in SLOW_LADDER]
    else:
        max_rungs = 4 if kind == "scanner" else 3
        clean_ladder, ladder_err = _validate_ladder(
            ladder, "sell", max_rungs, 1000.0)
        if ladder_err is not None:
            return _fail(ladder_err)
    trending = raw.get("trending_ladder")
    clean_trending = None
    if trending is not None:
        if kind != "scanner":
            return _fail("The trending profit ladder is a scanner rule — "
                         "it only applies to scanner bots.")
        clean_trending, trend_err = _validate_ladder(
            trending, "trending", 6, 10000.0)
        if trend_err is not None:
            return _fail(trend_err)
    if kind == "scanner" and clean_trending is None:
        clean_trending = [dict(r) for r in TRENDING_LADDER]
    regime = raw.get("regime")
    clean_regime = None
    if regime is not None:
        if kind != "scanner":
            return _fail("The market regime is a scanner rule — it only "
                         "applies to scanner bots.")
        if not isinstance(regime, dict):
            return _fail("The market regime must be a set of settings "
                         "(volume surge, buy dominance, 1-hour gain).")
        for key in regime:
            if key in LIMIT_FIELDS:
                return _fail(f"'{key}' is a bot limit, not a rule.")
            if key not in REGIME_KEYS:
                return _fail(f"Unknown market regime setting '{key}'.")
        surge = regime.get("volume_surge_x",
                           REGIME_DEFAULT["volume_surge_x"])
        if not _is_number(surge) or not 1.0 <= float(surge) <= 10.0:
            return _fail("The volume surge must be between 1x and 10x "
                         "the average 5-minute pace of the last hour.")
        for flag in ("require_buy_dominance", "require_h1_gain"):
            if not isinstance(regime.get(flag, True), bool):
                return _fail(f"'{flag}' must be true or false.")
        clean_regime = {
            "volume_surge_x": float(surge),
            "require_buy_dominance": bool(
                regime.get("require_buy_dominance", True)),
            "require_h1_gain": bool(
                regime.get("require_h1_gain", True)),
        }
    if kind == "scanner" and clean_regime is None:
        clean_regime = dict(REGIME_DEFAULT)
    trailing = raw.get("trailing_stop_pct")
    if trailing is None:
        trailing = float(bot["stop_loss_pct"]) if bot is not None else 7.0
    if not _is_number(trailing) or not 0.5 <= float(trailing) <= 50:
        return _fail("Trailing stop must be between 0.5% and 50% below "
                     "the highest price since holding.")
    rug = raw.get("rug_sell")
    clean_rug = None
    if rug is not None:
        if not isinstance(rug, dict):
            return _fail("The rug sell rule must be a set of settings "
                         "(drop %, window, 5-minute drop %).")
        for key in rug:
            if key in LIMIT_FIELDS:
                return _fail(f"'{key}' is a bot limit, not a rule.")
            if key not in RUG_SELL_KEYS:
                return _fail(f"Unknown rug sell setting '{key}'.")
        drop = rug.get("drop_pct", RUG_SELL_DEFAULT["drop_pct"])
        if not _is_number(drop) or not 1 <= float(drop) <= 90:
            return _fail("The rug sell drop must be between 1% and 90%.")
        rug_window = rug.get("window_seconds",
                             RUG_SELL_DEFAULT["window_seconds"])
        if not isinstance(rug_window, int) \
                or isinstance(rug_window, bool) \
                or rug_window not in RUG_WINDOWS:
            return _fail("The rug sell window must be 1 minute or "
                         "5 minutes — those are the windows the price "
                         "data supports.")
        drop_5m = rug.get("drop_5m_pct", RUG_SELL_DEFAULT["drop_5m_pct"])
        if not _is_number(drop_5m) or not 1 <= float(drop_5m) <= 90:
            return _fail("The rug sell 5-minute drop must be between "
                         "1% and 90%.")
        clean_rug = {"drop_pct": float(drop), "window_seconds": rug_window,
                     "drop_5m_pct": float(drop_5m)}
    top10 = raw.get("top10_rug")
    clean_top10 = None
    if top10 is not None:
        if kind not in ("scanner", "trenches", "gnat"):
            return _fail("The top-10 wallets watch is a memecoin rule — "
                         "it only applies to scanner, trenches and gnat "
                         "bots.")
        if not isinstance(top10, dict):
            return _fail("The top-10 wallets watch must be a set of "
                         "settings (drop %).")
        for key in top10:
            if key in LIMIT_FIELDS:
                return _fail(f"'{key}' is a bot limit, not a rule.")
            if key not in TOP10_RUG_KEYS:
                return _fail(f"Unknown top-10 wallets setting '{key}'.")
        top_drop = top10.get("drop_pct", TOP10_RUG_DEFAULT["drop_pct"])
        if not _is_number(top_drop) or not 1 <= float(top_drop) <= 90:
            return _fail("The top-10 wallets drop must be between 1% "
                         "and 90%.")
        concentration = top10.get(
            "concentration_pct", TOP10_RUG_DEFAULT["concentration_pct"])
        if not _is_number(concentration) \
                or not 1 <= float(concentration) <= 100:
            return _fail("The top-10 concentration level must be "
                         "between 1% and 100% of supply.")
        slow_climb = top10.get(
            "slow_climb_sell_pct",
            TOP10_RUG_DEFAULT["slow_climb_sell_pct"])
        if not _is_number(slow_climb) or not 0 <= float(slow_climb) <= 100:
            return _fail("The top-10 slow-climb sell must be between "
                         "0% (off) and 100% of the position.")
        check_interval = top10.get(
            "check_interval_seconds",
            TOP10_RUG_DEFAULT["check_interval_seconds"])
        if not _is_number(check_interval) \
                or not 10 <= float(check_interval) <= 300:
            return _fail("The top-10 check interval must be between "
                         "10 and 300 seconds.")
        clean_top10 = {"drop_pct": float(top_drop),
                       "concentration_pct": float(concentration),
                       "slow_climb_sell_pct": float(slow_climb),
                       "check_interval_seconds": float(check_interval)}
    buy_filter = raw.get("buy_filter")
    clean_filter = None
    if buy_filter is not None:
        if kind not in ("trenches", "scanner", "gnat"):
            return _fail("The buy filter is a trenches/scanner/gnat "
                         "rule — it only applies to bots that scan the "
                         "feed.")
        if not isinstance(buy_filter, dict):
            return _fail("The buy filter must be a set of settings "
                         "(minimum 5-minute volume, buy dominance, "
                         "minimum signal %).")
        for key in buy_filter:
            if key in LIMIT_FIELDS:
                return _fail(f"'{key}' is a bot limit, not a rule.")
            if key not in BUY_FILTER_KEYS:
                return _fail(f"Unknown buy filter setting '{key}'.")
        min_vol = buy_filter.get(
            "min_volume_5m_usd",
            BUY_FILTER_DEFAULT["min_volume_5m_usd"])
        if not _is_number(min_vol) or not 0 <= float(min_vol) <= 1000000:
            return _fail("The buy filter's minimum 5-minute volume must "
                         "be between $0 and $1,000,000.")
        if not isinstance(buy_filter.get("require_buy_dominance", True),
                          bool):
            return _fail("'require_buy_dominance' must be true or false.")
        min_signal = buy_filter.get(
            "min_signal_pct", BUY_FILTER_DEFAULT["min_signal_pct"])
        if not _is_number(min_signal) or not 0.1 <= float(min_signal) <= 100:
            return _fail("The buy filter's minimum signal must be "
                         "between 0.1% and 100%.")
        min_mcap = buy_filter.get(
            "min_market_cap_usd", BUY_FILTER_DEFAULT["min_market_cap_usd"])
        if not _is_number(min_mcap) \
                or not 0 <= float(min_mcap) <= 1000000000000:
            return _fail("The buy filter's minimum market cap must be "
                         "between $0 and $1,000,000,000,000.")
        clean_filter = {
            "min_volume_5m_usd": float(min_vol),
            "require_buy_dominance": bool(
                buy_filter.get("require_buy_dominance", True)),
            "min_signal_pct": float(min_signal),
            "min_market_cap_usd": float(min_mcap),
        }
    if kind in ("trenches", "scanner", "gnat") and clean_filter is None:
        # Stored rules from before the filter existed load as the new
        # defaults on the next evaluation — the filter applies to the
        # Scanner, Trenches and Gnat bots already running.
        clean_filter = dict(BUY_FILTER_DEFAULT)
    max_new = raw.get("max_new_positions_per_scan", 1)
    if not isinstance(max_new, int) or isinstance(max_new, bool) \
            or not 1 <= max_new <= 5:
        return _fail("New positions per scan must be between 1 and 5.")
    cleaned = {
        "version": RULE_VERSION,
        "kind": kind,
        "scan_sources": list(sources)
        if kind in ("trenches", "scanner", "gnat") else [],
        "scan_interval_seconds": interval,
        "buy": {"metric": WINDOW_METRICS[window],
                "window_seconds": window,
                "threshold_pct": float(threshold),
                "pick": "strongest"},
        "sell_ladder": clean_ladder,
        "trailing_stop_pct": float(trailing),
        "rug_sell": clean_rug,
        "max_new_positions_per_scan": max_new,
    }
    if kind == "scanner" or clean_top10 is not None:
        cleaned["top10_rug"] = clean_top10
    if kind in ("trenches", "scanner", "gnat"):
        cleaned["buy_filter"] = clean_filter
    if kind == "scanner":
        cleaned["trending_ladder"] = clean_trending
        cleaned["regime"] = clean_regime
    return cleaned, None


def load_rules(bot):
    """Load a bot's rule set. Returns (rules, error).

    (None, None) means the bot has no custom rules — the loop uses the
    standard v1 signals. (None, error) means the stored rules are
    broken: the loop must make NO trades for that bot.
    """
    raw = None
    try:
        raw = bot["rules_json"]
    except (KeyError, TypeError, IndexError):
        raw = None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None, "stored rules are not valid JSON"
    return validate_ruleset(parsed, bot=bot)


def _window_words(window_seconds: int) -> str:
    return {180: "3 minutes", 300: "5 minutes", 3600: "1 hour",
            21600: "6 hours", 86400: "24 hours"}.get(
                window_seconds, f"{window_seconds} seconds")


def summarize_rules(rules) -> str:
    """Plain-words summary of a rule set, for the UI and the agent."""
    if not rules:
        return "No custom rules — standard signals apply."
    buy = rules["buy"]
    lines = []
    if rules["kind"] == "trenches":
        sources = " and ".join(rules["scan_sources"])
        lines.append(
            f"Scan {sources} every {rules['scan_interval_seconds']} "
            f"seconds for new tokens.")
    elif rules["kind"] == "scanner":
        sources = " and ".join(rules["scan_sources"])
        lines.append(
            f"Scan {sources} every {rules['scan_interval_seconds']} "
            f"seconds for gainers, tracking improving coins.")
    elif rules["kind"] == "gnat":
        sources = " and ".join(rules["scan_sources"])
        lines.append(
            f"Scan {sources} every {rules['scan_interval_seconds']} "
            f"seconds for new tokens, checking the bonding curve "
            f"and buy pressure.")
    else:
        lines.append(
            f"Watch your own coin list every "
            f"{rules['scan_interval_seconds']} seconds.")
    lines.append(
        f"Buy when a coin gains {buy['threshold_pct']}% in "
        f"{_window_words(buy['window_seconds'])}"
        f"{' (strongest gainer wins)' if rules['kind'] in ('trenches', 'scanner', 'gnat') else ''}"
        f"{' — the 3-minute gain is tracked from recorded prices, because the feed only gives 5-minute changes' if rules['kind'] in ('scanner', 'gnat') else ''}.")
    if rules["kind"] == "scanner":
        slow = "; ".join(
            f"sell {r['pct_of_position']:.0f}% at +{r['profit_pct']:.0f}% profit"
            for r in rules["sell_ladder"])
        lines.append(f"Slow market — sell ladder: {slow}.")
        trending = rules.get("trending_ladder") or []
        if trending:
            runner = "; ".join(
                f"sell {r['pct_of_position']:.0f}% at "
                f"+{r['profit_pct']:.0f}% profit"
                for r in trending)
            lines.append(
                f"Trending market — hold the runners: {runner}.")
        regime = rules.get("regime")
        if regime:
            lines.append(
                f"Market regime: trending when the 5-minute volume is "
                f"{regime['volume_surge_x']:.0f}x the average 5-minute "
                f"pace of the last hour, buys outnumber sells in the "
                f"last 5 minutes, and the 1-hour change is positive — "
                f"otherwise slow. (There is no social-media feed: "
                f"trending is judged from the price feed's volume and "
                f"buyers, never guessed.)")
    elif rules["sell_ladder"]:
        ladder = "; ".join(
            f"sell {r['pct_of_position']:.0f}% at +{r['profit_pct']:.0f}% profit"
            for r in rules["sell_ladder"])
        lines.append(f"Sell ladder: {ladder}.")
    else:
        lines.append(
            "Sell ladder: none — exits on the rug sense, trailing "
            "stop and stop loss only.")
    lines.append(
        f"Sell everything if the price falls "
        f"{rules['trailing_stop_pct']}% below the highest price since "
        f"holding.")
    if rules["kind"] == "gnat":
        lines.append(
            "Take profit in tiers: sell 35% at +40% profit and move "
            "the stop on the rest to breakeven +2%; sell another 35% "
            "of the original position at +100% (2x); the remaining "
            "30% rides a trailing stop of 18% up to +250%, then 12% "
            "above +250%.")
        lines.append(
            "Time decay: if the coin is not at least +15% within "
            "90 seconds of buying, sell everything. Circuit "
            "breakers: no new buys for the rest of the day after "
            "2.0 SOL of realized losses, or after 4 losing trades "
            "in a row.")
    rug = rules.get("rug_sell")
    if rug:
        window_words = _window_words(rug["window_seconds"])
        lines.append(
            f"Rug sell: sell everything if the price drops "
            f"{rug['drop_pct']:.0f}% in {window_words}, or falls "
            f"{rug['drop_5m_pct']:.0f}% in 5 minutes. This is "
            f"price-based — the feed cannot see the dev's wallet, so "
            f"it triggers on the price falling, not on the dev "
            f"selling.")
    top10 = rules.get("top10_rug")
    if top10:
        slow = top10.get("slow_climb_sell_pct", 50)
        concentration = top10.get("concentration_pct", 21)
        check_every = top10.get("check_interval_seconds", 15)
        slow_word = ""
        if slow:
            slow_word = (
                f" If that share keeps climbing slowly toward "
                f"{concentration:.0f}% (rising for 3 checks in a row), "
                f"sell {slow:.0f}% to lock profit as an early rug "
                f"warning.")
        lines.append(
            f"Top-10 wallets, checked every {check_every:.0f} seconds: "
            f"sell everything if the top 10 holders' combined holdings "
            f"drop {top10['drop_pct']:.0f}% from the peak since holding "
            f"— the big holders dumping — or if their share of all "
            f"tokens reaches {concentration:.0f}%.{slow_word} "
            f"The proceeds stay in SOL.")
    buy_filter = rules.get("buy_filter")
    if buy_filter and rules["kind"] in ("trenches", "scanner", "gnat"):
        mcap = buy_filter.get("min_market_cap_usd", 0.0)
        lines.append(
            f"Buy filter — only buy a real signal: at least "
            f"${buy_filter['min_volume_5m_usd']:,.0f} of volume in the "
            f"last 5 minutes"
            f"{', buys outnumbering sells in the last 5 minutes' if buy_filter['require_buy_dominance'] else ''}, "
            f"and a signal of at least {buy_filter['min_signal_pct']:.1f}%"
            f"{f', and a market cap of at least ${mcap:,.0f}' if mcap else ''}. "
            f"A coin missing any of that is skipped, never bought on a guess.")
    lines.append(
        f"At most {rules['max_new_positions_per_scan']} new position(s) "
        f"per scan.")
    return "\n".join(lines)
