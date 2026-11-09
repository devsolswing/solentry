"""In-app assistant: a deterministic command parser + knowledge base.

This is NOT a language model and the UI labels it as the app assistant.
It understands fixed command shapes for the logged-in user's own bots,
wallet, and sends. It NEVER executes a send or a delete directly:
  * "send ..." creates a send intent and returns the confirm step.
  * "delete bot ..." asks for confirmation; only a follow-up "yes"
    (with a pending delete in the session) performs it.
All actions are scoped to the logged-in user by the route layer.
"""

from __future__ import annotations

import re

from .rules import WINDOW_METRICS

STRATEGIES = ("careful", "balanced", "bold")

CAPABILITIES = (
    "Here's what I can do for you:\n"
    "- \"show my balance\" — paper cash, holdings, and real SOL balance\n"
    "- \"show my wallet\" — your app wallet address and balance\n"
    "- \"show my bots\" — list your trading bots\n"
    "- \"create bot\" — start a new bot (I can take a name and strategy)\n"
    "- \"create the trenches bot\" — a memecoin bot that scans pump.fun "
    "and PumpSwap (see Memecoin build)\n"
    "- \"edit bot <name> strategy to bold\" — change a bot's settings\n"
    "- \"delete bot <name>\" — remove a bot (I ask first)\n"
    "- \"bot <name> scan pump.fun and pumpswap, buy when a coin gains "
    "5% in 5 minutes\" — set a bot's trading rules (I show them back "
    "and ask you to confirm first; the bot can never trade outside "
    "them or its limits)\n"
    "- \"show rules for <bot>\" — what rules a bot follows right now\n"
    "- \"remove rules for <bot>\" — back to the standard signals\n"
    "- \"turn on silent trading for <bot>\" — the bot trades on its own "
    "within your limits (I ask first); \"turn it off\" stops it at once\n"
    "- \"is silent trading on\" — which bots are silent right now\n"
    "- \"send 0.5 SOL to <address>\" — starts a send; you confirm before anything moves\n"
    "- \"how does paper trading work\" — plain explanation\n"
    "I only ever act on your own account, and I never move money without "
    "you confirming first."
)

PAPER_HELP = (
    "Paper trading is practice with fake money — you start with $10,000. "
    "Buy and sell coins at the current price and your portfolio tracks "
    "gains and losses, but no real money ever moves. It is the safe way to "
    "try a strategy before using real SOL. Switch to Real mode on your "
    "dashboard when you are ready — and even there, the bot only suggests "
    "trades; you approve each one before anything moves. If you turn on "
    "silent trading for a bot, it trades on its own within your limits "
    "instead — but only after you confirm, and you can turn it off any time."
)

SILENT_HELP = (
    "Silent trading means a bot trades on its own, in the background, "
    "with no pop-up and no per-trade approval. It only runs within the "
    "bot's own limits — buy size %, stop loss %, max open trades — and "
    "never more than your balance. Every trade and every skipped trade "
    "is written to your activity log. Silent is off by default for every "
    "bot; you turn it on per bot and I always ask you to confirm first. "
    "Say \"turn off silent trading for <bot>\" any time to stop it."
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_WINDOW_WORDS = {"min": 1, "mins": 1, "minute": 1, "minutes": 1,
                 "hour": 60, "hours": 60, "h": 60,
                 "day": 1440, "days": 1440}


def _parse_rules_command(msg: str, raw: str):
    """Parse a plain-language rules instruction into a partial rule set.

    Returns (partial_rules, bot_name) or ("unsupported", reply) when
    the instruction is a rules instruction the grammar cannot express,
    or None when the message is not a rules instruction at all. A
    partial rule set may leave trailing_stop_pct as None — the route
    fills it from the bot's own stop loss before validating.
    """
    # Buy pattern, two shapes:
    #   "gains 5% in 5 minutes" / "up 3.4% in 5 min"
    #   "gaining in a 5 min window by 5%"
    buy = re.search(
        r"(?:gain|gains|gaining|up|rise|rises|rising|increase|increases)"
        r"s?\s+(?:by\s+)?([\d.]+)\s*%\s*(?:in|over|within)?\s*(?:a\s+|an\s+)?"
        r"(\d+)\s*(min|mins|minute|minutes|hour|hours|h|day|days)\b", msg)
    if buy:
        threshold = float(buy.group(1))
        window_minutes = int(buy.group(2)) * _WINDOW_WORDS[buy.group(3)]
    else:
        buy2 = re.search(
            r"(?:in|over|within)\s*(?:a\s+|an\s+)?(\d+)\s*"
            r"(min|mins|minute|minutes|hour|hours|h|day|days)\s*"
            r"(?:window\s*)?(?:by\s+)?([\d.]+)\s*%", msg)
        if not buy2:
            return None
        window_minutes = int(buy2.group(1)) * _WINDOW_WORDS[buy2.group(2)]
        threshold = float(buy2.group(3))
    window_seconds = window_minutes * 60
    if window_seconds not in WINDOW_METRICS:
        return ("unsupported",
                "I can only use 5-minute, 1-hour, 6-hour, or 24-hour "
                "windows — those are the windows the price feed "
                "provides. Nothing was changed; try again with one of "
                "those windows.")

    # Bot name: "bot <name>" up to the next keyword, or "for <name>".
    bot_name = None
    nm = re.search(
        r"\bbot\s+(?:named\s+)?([A-Za-z0-9_\- ]+?)"
        r"(?:\s+(?:scan|set|rules|buy|sell|,\s)|$)", raw, re.IGNORECASE)
    if nm:
        bot_name = nm.group(1).strip()
    else:
        fm = re.search(r"\bfor\s+(?:bot\s+)?([A-Za-z0-9_\- ]+?)\s*$",
                       raw, re.IGNORECASE)
        if fm:
            bot_name = fm.group(1).strip()

    sources = []
    if "pump.fun" in msg or "pump fun" in msg:
        sources.append("pump.fun")
    if "pumpswap" in msg or "pump swap" in msg:
        sources.append("pumpswap")
    if "dexscreener" in msg:
        sources.append("dexscreener")
    if not sources and "scan" in msg:
        sources = ["pump.fun", "pumpswap"]

    interval = 60
    im = re.search(
        r"every\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours)\b",
        msg)
    if im:
        n = int(im.group(1))
        unit = im.group(2)
        interval = n * (1 if unit.startswith("second")
                        else 60 if unit.startswith("minute") else 3600)
    elif "every minute" in msg:
        interval = 60
    elif "every hour" in msg:
        interval = 3600

    # Sell ladder: "sell 10% at 3% profit" / "90% at 10%" / "the 90 at 10%".
    ladder = []
    for m in re.finditer(
            r"(\d+(?:\.\d+)?)\s*%?\s*(?:of\s+(?:(?:the|total|position|"
            r"coins?|holding|rest|remaining)\s+)*)?at\s*\+?\s*"
            r"(\d+(?:\.\d+)?)\s*%\s*(?:profit)?", msg):
        ladder.append({"pct_of_position": float(m.group(1)),
                       "profit_pct": float(m.group(2))})
    if not ladder:
        ladder = [{"pct_of_position": 100.0, "profit_pct": 10.0}]

    # Trailing stop: "sell all if it declines 7% of highest value" /
    # "sell all if it falls 7% below the highest price" / "trailing
    # stop 7%".
    trailing = None
    tm = re.search(
        r"(?:sell all|sell everything)\s+if[^.]*?"
        r"(?:drop|drops|fall|falls|decline|declines|down)\s+(?:by\s+)?"
        r"([\d.]+)\s*%\s*(?:of\s+)?(?:below\s+|under\s+|from\s+)?"
        r"(?:the\s+)?(?:highest|peak|high)", msg)
    if tm:
        trailing = float(tm.group(1))
    else:
        tm2 = re.search(r"trailing stop\s+(?:of\s+)?([\d.]+)\s*%", msg)
        if tm2:
            trailing = float(tm2.group(1))

    partial = {
        "version": 1,
        "kind": "trenches" if sources or window_seconds == 300 else "signals",
        "scan_sources": sources,
        "scan_interval_seconds": interval,
        "buy": {"metric": WINDOW_METRICS[window_seconds],
                "window_seconds": window_seconds,
                "threshold_pct": threshold, "pick": "strongest"},
        "sell_ladder": ladder,
        "trailing_stop_pct": trailing,
        "max_new_positions_per_scan": 1,
    }
    return partial, bot_name


def parse_message(text: str) -> dict:
    """Parse a user message into an intent dict.

    Always returns a dict with at least {"intent": ..., "reply": ...}.
    The route layer decides what to execute; the parser never touches
    the database or the chain. Matching is case-insensitive, but bot
    names keep the user's own capitalisation.
    """
    msg = _clean(text)
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not msg:
        return {"intent": "help", "reply": CAPABILITIES}

    # Confirmation of a pending action (delete bot).
    if msg in ("yes", "confirm", "yeah", "yes please", "yes delete it"):
        return {"intent": "confirm_yes", "reply": ""}

    if msg in ("no", "cancel", "never mind", "nevermind"):
        return {"intent": "cancel", "reply": ""}

    # Send: "send 0.5 SOL to <address>"
    m = re.search(
        r"send\s+([\d.]+)\s+([a-z]+)\s+to\s+([A-Za-z0-9]{32,44})", msg)
    if m:
        return {"intent": "send",
                "amount": m.group(1), "token": m.group(2).upper(),
                "destination": m.group(3), "reply": ""}

    # Delete bot: "delete bot <name>"
    m = re.match(r"delete bot\s+(.+)$", raw, re.IGNORECASE)
    if m:
        return {"intent": "bot_delete", "bot_name": m.group(1).strip(),
                "reply": ""}

    # Edit bot: "edit bot <name> strategy to bold" / "buy size 20%" /
    # "stop loss 7%"
    m = re.match(r"edit bot\s+(.+)$", raw, re.IGNORECASE)
    if m:
        rest = m.group(1).strip()
        fields: dict = {}
        sm = re.search(r"strategy\s+(?:to\s+)?(careful|balanced|bold)",
                       rest, re.IGNORECASE)
        if sm:
            fields["strategy"] = sm.group(1).lower()
        bm = re.search(r"buy size\s+(?:to\s+)?([\d.]+)\s*%?", rest,
                       re.IGNORECASE)
        if bm:
            fields["buy_size_pct"] = float(bm.group(1))
        lm = re.search(r"stop loss\s+(?:to\s+)?([\d.]+)\s*%?", rest,
                       re.IGNORECASE)
        if lm:
            fields["stop_loss_pct"] = float(lm.group(1))
        # Bot name = text before the first field keyword.
        name = re.split(
            r"\s+(?:strategy|buy size|stop loss)\b", rest, maxsplit=1,
            flags=re.IGNORECASE)[0].strip()
        return {"intent": "bot_edit", "bot_name": name, "fields": fields,
                "reply": ""}

    # Trenches preset: "create the trenches bot" / "make a trenches bot".
    if "trenches" in msg and "bot" in msg and re.search(
            r"\b(create|make|new|add|start)\b", msg):
        return {"intent": "preset_create", "preset": "trenches", "reply": ""}

    # Create bot: "create bot", "create bot named Luna", "create bot bold"
    if msg.startswith("create bot") or msg.startswith("make bot") \
            or msg.startswith("new bot"):
        name = None
        nm = re.search(
            r"named\s+([\w\- ]+?)(?:\s+(?:strategy|coins|buy|careful|balanced|bold)|$)",
            raw, re.IGNORECASE)
        if nm:
            name = nm.group(1).strip()
        strategy = None
        for s in STRATEGIES:
            if re.search(rf"\b{s}\b", msg):
                strategy = s
                break
        return {"intent": "bot_create", "bot_name": name,
                "strategy": strategy, "reply": ""}

    # Bot rules: "bot Luna scan pump.fun and pumpswap, buy when a coin
    # gains 5% in 5 minutes" / "set rules for bot Luna ...". The parser
    # only proposes — the route shows the rules back and asks for a
    # confirmation before anything is stored.
    parsed_rules = _parse_rules_command(msg, raw)
    if isinstance(parsed_rules, tuple) and len(parsed_rules) == 2 \
            and parsed_rules[0] == "unsupported":
        return {"intent": "rules_unsupported",
                "reply": parsed_rules[1]}
    if parsed_rules:
        partial, bot_name = parsed_rules
        return {"intent": "rules_propose", "bot_name": bot_name,
                "rules": partial, "reply": ""}

    # Show rules: "show rules for Luna" / "what are Luna's rules".
    if "rules" in msg and re.search(
            r"\b(show|what|list|see|tell)\b", msg):
        name = None
        fm = re.search(r"\bfor\s+(?:bot\s+)?(.+?)\s*$", raw, re.IGNORECASE)
        if fm:
            name = fm.group(1).strip()
        return {"intent": "rules_show", "bot_name": name, "reply": ""}

    # Clear rules: "remove rules for Luna" / "reset bot Luna rules".
    if "rules" in msg and re.search(
            r"\b(remove|clear|reset|delete)\b", msg):
        name = None
        fm = re.search(r"\bfor\s+(?:bot\s+)?(.+?)\s*$", raw, re.IGNORECASE)
        if fm:
            name = fm.group(1).strip()
        if not name:
            bm = re.search(r"\bbot\s+(.+?)\s+rules\b", raw, re.IGNORECASE)
            if bm:
                name = bm.group(1).strip()
        return {"intent": "rules_clear", "bot_name": name, "reply": ""}

    # Silent trading: "turn on silent trading for <name>",
    # "turn off silent trading for <name>", "is silent trading on".
    if "silent" in msg:
        name = None
        nm = re.search(r"\bfor\s+(.+?)\s*$", raw, re.IGNORECASE)
        if nm:
            name = nm.group(1).strip()
        if re.search(r"\b(turn on|enable|start|switch on)\b", msg):
            return {"intent": "silent_on", "bot_name": name, "reply": ""}
        if re.search(r"\b(turn off|disable|stop|switch off)\b", msg):
            return {"intent": "silent_off", "bot_name": name, "reply": ""}
        if re.search(r"\b(on|off|enabled|status)\b", msg):
            return {"intent": "silent_status", "bot_name": name, "reply": ""}
        return {"intent": "silent_help", "reply": SILENT_HELP}

    # Show bots.
    if "bot" in msg and any(w in msg for w in ("show", "list", "my", "see")):
        return {"intent": "bots_list", "reply": ""}

    # Balance.
    if "balance" in msg:
        return {"intent": "balance", "reply": ""}

    # Wallet.
    if "wallet" in msg:
        return {"intent": "wallet_show", "reply": ""}

    # Paper trading help.
    if "paper" in msg:
        return {"intent": "paper_help", "reply": PAPER_HELP}

    # Help.
    if msg in ("help", "what can you do", "commands", "hi", "hello", "hey"):
        return {"intent": "help", "reply": CAPABILITIES}

    return {"intent": "unknown", "reply":
            "I don't understand that one. " + CAPABILITIES}
