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
    list, evaluated on CoinGecko 24h moves. kind "scanner": the
    ∆scanner∆ sniper — scan pump.fun / PumpSwap every 30 seconds for
    gainers, buy a coin gaining 5% in 3 minutes (strongest gainer
    wins), tracked from price marks the loop records itself
    (scanner.py), because the feed only provides 5-minute changes.
    A scanner has no sell ladder by default — it exits on the rug
    sense, the trailing stop and the stop loss. Its top10_rug watches
    the top 10 holders of every held coin through Solana RPC
    getTokenLargestAccounts: a 10% drop in their combined holdings
    from the peak since holding sells everything, proceeds in SOL.
  * buy.metric must match buy.window_seconds: 180 -> gain_3m_pct
    (scanner only, computed from recorded marks), 300 -> gain_5m_pct,
    3600 -> gain_1h_pct, 21600 -> gain_6h_pct, 86400 -> gain_24h_pct.
    Those are the windows the feeds actually provide, plus the
    3-minute window the scanner computes itself.
  * sell_ladder rungs are % of the ORIGINAL position amount, executed
    in order as profit rises; rung profit thresholds must strictly
    increase and the rung sizes must sum to <= 100.
  * trailing_stop_pct sells ALL when the price falls that far below
    the highest price since the position opened. The bot's own stop
    loss % is still enforced separately as a hard stop.
"""

from __future__ import annotations

import copy
import json

RULE_VERSION = 1
RULE_KINDS = ("trenches", "signals", "scanner")
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

# Top-10 wallets rug sense (scanner only): sell everything when the
# top 10 holders' combined holdings drop this far from the peak since
# holding. The default 10% is James's number.
TOP10_RUG_DEFAULT = {"drop_pct": 10.0}
TOP10_RUG_KEYS = ("drop_pct",)

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
# A scanner has no sell ladder: James's snipe only names the buy and
# the rug exits. It exits on the top-10 dump sense, the fast-drop rug
# sell, the trailing stop and the stop loss.
SCANNER_TRAILING_STOP_PCT = 7.0

# James's spec, exactly: buy any coin gaining 5% in 3 minutes; scan
# every 30 seconds for gainers and keep track of improving coins
# (the strongest 3-minute gainer wins); watch the top 10 wallets of
# every held coin closely and sell everything if their combined
# holdings drop 10% — the pump.fun sell pays out in SOL, so the
# proceeds stay in SOL. The 3-minute gain is computed from price
# marks the loop records itself (scanner.py) because the DexScreener
# feed only provides 5-minute changes.
SCANNER_RULES = {
    "version": 1,
    "kind": "scanner",
    "scan_sources": list(SCANNER_SOURCES),
    "scan_interval_seconds": SCANNER_INTERVAL_SECONDS,
    "buy": {"metric": "gain_3m_pct",
            "window_seconds": SCANNER_BUY_WINDOW_SECONDS,
            "threshold_pct": SCANNER_BUY_GAIN_PCT,
            "pick": "strongest"},
    "sell_ladder": [],
    "trailing_stop_pct": SCANNER_TRAILING_STOP_PCT,
    "rug_sell": dict(RUG_SELL_DEFAULT),
    "top10_rug": {"drop_pct": SCANNER_TOP10_DROP_PCT},
    "max_new_positions_per_scan": SCANNER_MAX_NEW_PER_SCAN,
}


def scanner_ruleset() -> dict:
    """A fresh copy of the ∆scanner∆ preset."""
    return copy.deepcopy(SCANNER_RULES)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fail(message: str):
    return None, message


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
                     "3 minutes) or 'signals' (your own coin list).")
    interval = raw.get("scan_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) \
            or not 10 <= interval <= 3600:
        return _fail("Scan interval must be between 10 and 3600 seconds.")
    sources = raw.get("scan_sources", [])
    if kind in ("trenches", "scanner"):
        if not isinstance(sources, list) or not sources:
            return _fail("A trenches or scanner rule set needs at "
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
        return _fail("Buy window must be 3 minutes (scanner only), "
                     "5 minutes, 1 hour, 6 hours, or 24 hours — those "
                     "are the windows the price feed provides, plus "
                     "the 3-minute window the scanner tracks itself.")
    if kind == "scanner" and window != SCANNER_BUY_WINDOW_SECONDS:
        return _fail("A scanner buys on a 3-minute gain — that is the "
                     "only window the scanner tracks.")
    if buy.get("metric") != WINDOW_METRICS[window]:
        return _fail("Buy metric does not match its window.")
    threshold = buy.get("threshold_pct")
    if not _is_number(threshold) or not 0.1 <= float(threshold) <= 100:
        return _fail("Buy threshold must be between 0.1% and 100%.")
    if buy.get("pick", "strongest") != "strongest":
        return _fail("Only 'strongest gainer wins' is supported for "
                     "picking between coins.")
    ladder = raw.get("sell_ladder")
    min_rungs = 0 if kind == "scanner" else 1
    if not isinstance(ladder, list) or not min_rungs <= len(ladder) <= 3:
        return _fail("A sell ladder needs 1 to 3 steps (a scanner may "
                     "have none — it exits on the rug sense, trailing "
                     "stop and stop loss).")
    total_pct = 0
    prev_profit = None
    clean_ladder = []
    for rung in ladder:
        if not isinstance(rung, dict) \
                or set(rung) != {"pct_of_position", "profit_pct"}:
            return _fail("Each sell step needs exactly a % of the "
                         "position and a % profit.")
        pct = rung["pct_of_position"]
        profit = rung["profit_pct"]
        if not _is_number(pct) or not 1 <= float(pct) <= 100:
            return _fail("Each sell step must sell between 1% and 100% "
                         "of the position.")
        if not _is_number(profit) or not 0.1 <= float(profit) <= 1000:
            return _fail("Each sell step's profit target must be between "
                         "0.1% and 1000%.")
        if prev_profit is not None and float(profit) <= prev_profit:
            return _fail("Sell steps must rise in profit — a later step "
                         "cannot trigger at a lower profit than an "
                         "earlier one.")
        prev_profit = float(profit)
        total_pct += float(pct)
        clean_ladder.append({"pct_of_position": float(pct),
                             "profit_pct": float(profit)})
    if total_pct > 100:
        return _fail("Sell steps add up to more than 100% of the "
                     "position — they cannot sell more than you hold.")
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
        if kind != "scanner":
            return _fail("The top-10 wallets watch is a scanner rule — "
                         "it only applies to scanner bots.")
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
        clean_top10 = {"drop_pct": float(top_drop)}
    max_new = raw.get("max_new_positions_per_scan", 1)
    if not isinstance(max_new, int) or isinstance(max_new, bool) \
            or not 1 <= max_new <= 5:
        return _fail("New positions per scan must be between 1 and 5.")
    cleaned = {
        "version": RULE_VERSION,
        "kind": kind,
        "scan_sources": list(sources)
        if kind in ("trenches", "scanner") else [],
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
    else:
        lines.append(
            f"Watch your own coin list every "
            f"{rules['scan_interval_seconds']} seconds.")
    lines.append(
        f"Buy when a coin gains {buy['threshold_pct']}% in "
        f"{_window_words(buy['window_seconds'])}"
        f"{' (strongest gainer wins)' if rules['kind'] in ('trenches', 'scanner') else ''}"
        f"{' — the 3-minute gain is tracked from recorded prices, because the feed only gives 5-minute changes' if rules['kind'] == 'scanner' else ''}.")
    if rules["sell_ladder"]:
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
        lines.append(
            f"Top-10 wallets: sell everything if the top 10 holders' "
            f"combined holdings drop {top10['drop_pct']:.0f}% from "
            f"the peak since holding — the big holders dumping. "
            f"The proceeds stay in SOL.")
    lines.append(
        f"At most {rules['max_new_positions_per_scan']} new position(s) "
        f"per scan.")
    return "\n".join(lines)
