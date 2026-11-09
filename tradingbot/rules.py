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
      "kind": "trenches" | "signals",
      "scan_sources": ["pump.fun", "pumpswap"],   # trenches only
      "scan_interval_seconds": 60,
      "buy": {"metric": "gain_5m_pct", "window_seconds": 300,
              "threshold_pct": 3.4, "pick": "strongest"},
      "sell_ladder": [{"pct_of_position": 10, "profit_pct": 3},
                      {"pct_of_position": 90, "profit_pct": 10}],
      "trailing_stop_pct": 7.0,
      "max_new_positions_per_scan": 1,
    }

  * kind "trenches": scan pump.fun / PumpSwap new tokens through the
    DexScreener feed (trenches.py). kind "signals": the bot's own coin
    list, evaluated on CoinGecko 24h moves.
  * buy.metric must match buy.window_seconds: 300 -> gain_5m_pct,
    3600 -> gain_1h_pct, 21600 -> gain_6h_pct, 86400 -> gain_24h_pct.
    Those are the windows the feeds actually provide.
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
RULE_KINDS = ("trenches", "signals")
RULE_SOURCES = ("pump.fun", "pumpswap", "dexscreener")

# Windows the feeds provide, in seconds -> buy-metric name.
WINDOW_METRICS = {
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
    "sell_ladder", "trailing_stop_pct", "max_new_positions_per_scan",
)

TRENCHES_CATEGORY = "Memecoin build"
TRENCHES_PRESET_NAME = "Trenches"

# James's spec, exactly: scan the trenches every minute; buy a coin
# gaining 3.4% in a 5-minute window (strongest gainer wins); sell 10%
# at +3% profit, the rest at +10%; sell all at 7% below the peak.
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
    "max_new_positions_per_scan": 1,
}


def trenches_ruleset() -> dict:
    """A fresh copy of the Trenches preset."""
    return copy.deepcopy(TRENCHES_RULES)


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
                     "and PumpSwap) or 'signals' (your own coin list).")
    interval = raw.get("scan_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) \
            or not 10 <= interval <= 3600:
        return _fail("Scan interval must be between 10 and 3600 seconds.")
    sources = raw.get("scan_sources", [])
    if kind == "trenches":
        if not isinstance(sources, list) or not sources:
            return _fail("A trenches rule set needs at least one scan "
                         "source (pump.fun or pumpswap).")
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
        return _fail("Buy window must be 5 minutes, 1 hour, 6 hours, or "
                     "24 hours — those are the windows the price feed "
                     "provides.")
    if buy.get("metric") != WINDOW_METRICS[window]:
        return _fail("Buy metric does not match its window.")
    threshold = buy.get("threshold_pct")
    if not _is_number(threshold) or not 0.1 <= float(threshold) <= 100:
        return _fail("Buy threshold must be between 0.1% and 100%.")
    if buy.get("pick", "strongest") != "strongest":
        return _fail("Only 'strongest gainer wins' is supported for "
                     "picking between coins.")
    ladder = raw.get("sell_ladder")
    if not isinstance(ladder, list) or not 1 <= len(ladder) <= 3:
        return _fail("A sell ladder needs 1 to 3 steps.")
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
    max_new = raw.get("max_new_positions_per_scan", 1)
    if not isinstance(max_new, int) or isinstance(max_new, bool) \
            or not 1 <= max_new <= 5:
        return _fail("New positions per scan must be between 1 and 5.")
    cleaned = {
        "version": RULE_VERSION,
        "kind": kind,
        "scan_sources": list(sources) if kind == "trenches" else [],
        "scan_interval_seconds": interval,
        "buy": {"metric": WINDOW_METRICS[window],
                "window_seconds": window,
                "threshold_pct": float(threshold),
                "pick": "strongest"},
        "sell_ladder": clean_ladder,
        "trailing_stop_pct": float(trailing),
        "max_new_positions_per_scan": max_new,
    }
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
    return {300: "5 minutes", 3600: "1 hour", 21600: "6 hours",
            86400: "24 hours"}.get(window_seconds,
                                   f"{window_seconds} seconds")


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
    else:
        lines.append(
            f"Watch your own coin list every "
            f"{rules['scan_interval_seconds']} seconds.")
    lines.append(
        f"Buy when a coin gains {buy['threshold_pct']}% in "
        f"{_window_words(buy['window_seconds'])}"
        f"{' (strongest gainer wins)' if rules['kind'] == 'trenches' else ''}.")
    ladder = "; ".join(
        f"sell {r['pct_of_position']:.0f}% at +{r['profit_pct']:.0f}% profit"
        for r in rules["sell_ladder"])
    lines.append(f"Sell ladder: {ladder}.")
    lines.append(
        f"Sell everything if the price falls "
        f"{rules['trailing_stop_pct']}% below the highest price since "
        f"holding.")
    lines.append(
        f"At most {rules['max_new_positions_per_scan']} new position(s) "
        f"per scan.")
    return "\n".join(lines)
