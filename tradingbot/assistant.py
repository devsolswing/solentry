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

from .rules import WINDOW_METRICS, scanner_ruleset

STRATEGIES = ("careful", "balanced", "bold")

# "Never sell below what I paid" is NOT a rule the scanner follows:
# a bad coin is cut at the 7% stop loss instead, so one dead coin
# cannot ride to zero. The agent explains that and changes nothing.
NEVER_BELOW_COST_REPLY = (
    "The Scanner doesn't do that on purpose — it cuts a bad coin at "
    "7% below the buy price and takes the small loss instead. If it "
    "never sold below cost, a dead coin would ride all the way to "
    "zero, and one zero wipes out a lot of winners. The profit comes "
    "from the asymmetry: winners run to +25–50% when the market is "
    "slow and +26% to +10000% when it's trending, while losers are "
    "capped at about 7%. Nothing was changed — say \"hold the "
    "runners\" if you want the Scanner on the profit ladders."
)

CAPABILITIES = (
    "Here's what I can do for you:\n"
    "- \"show my balance\" — paper cash, holdings, and real SOL balance\n"
    "- \"show my wallet\" — your app wallet address and balance\n"
    "- \"show my bots\" — list your trading bots\n"
    "- \"create bot\" — start a new bot (I can take a name and strategy)\n"
    "- \"create the trenches bot\" — a memecoin bot that scans pump.fun "
    "and PumpSwap (see Memecoin build)\n"
    "- \"create the scanner bot\" — a sniper (∆scanner∆) that buys any "
    "coin gaining 5% in 3 minutes, scanning every 30 seconds, and "
    "sells everything if the top 10 wallets dump 10% (proceeds stay "
    "in SOL)\n"
    "- \"edit bot <name> strategy to bold\" — change a bot's settings\n"
    "- \"delete bot <name>\" — remove a bot (I ask first)\n"
    "- \"bot <name> scan pump.fun and pumpswap, buy when a coin gains "
    "5% in 5 minutes\" — set a bot's trading rules (I show them back "
    "and ask you to confirm first; the bot can never trade outside "
    "them or its limits)\n"
    "- \"snipe coins gaining 5% in 3 minutes\" — set up a scanner "
    "sniper the same way (I show the rules back and ask you to "
    "confirm first)\n"
    "- \"scanner take profit at 3% when the market is slow\" — the "
    "scanner's slow-market ladder: 25% at +3%, +10%, +25%, the rest "
    "at +50%\n"
    "- \"hold the runners\" — the scanner holds trending coins for "
    "the big gains (10% at +26%, +50%, +100%, +500%, +1000%, the "
    "rest at +10000% or on the trailing stop)\n"
    "- \"show rules for <bot>\" — what rules a bot follows right now\n"
    "- \"remove rules for <bot>\" — back to the standard signals\n"
    "- \"sell everything if the dev starts selling\" — add a rug sell "
    "rule to a bot: it dumps the whole position on a fast price drop "
    "(15% in a minute or 25% in 5 minutes). The feed cannot see the "
    "dev's wallet, so it triggers on the price falling, not on the "
    "dev selling. I show it back and ask you to confirm first\n"
    "- \"sell if the price drops 15% in a minute\" — set the rug sell "
    "drop and window yourself\n"
    "- \"turn on silent trading for <bot>\" — the bot trades on its own "
    "within your limits (I ask first); \"turn it off\" stops it at once\n"
    "- \"is silent trading on\" — which bots are silent right now\n"
    "- \"send 0.5 SOL to <address>\" — starts a send; you confirm before anything moves\n"
    "- \"how does paper trading work\" — plain explanation\n"
    "I only ever act on your own account, and I never move money without "
    "you confirming first."
)

PAPER_HELP = (
    "Paper trading is practice with fake money — you start with $50 CAD. "
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


def _extract_bot_name(raw: str):
    """Bot name from "bot <name>" up to the next keyword, or
    "for <name>"."""
    nm = re.search(
        r"\bbot\s+(?:named\s+)?([A-Za-z0-9_\- ]+?)"
        r"(?:\s+(?:scan|set|rules|buy|sell|hold|take|,\s)|$)", raw, re.IGNORECASE)
    if nm:
        return nm.group(1).strip()
    fm = re.search(r"\bfor\s+(?:bot\s+)?([A-Za-z0-9_\- ]+?)\s*$",
                   raw, re.IGNORECASE)
    if fm:
        return fm.group(1).strip()
    return None


def _parse_rug(msg: str, raw: str):
    """Parse a rug-sell instruction into (rug, bot_name, error).

    Returns (None, None, None) when the message carries no rug
    instruction; (rug, bot_name, None) when it does; (None, None,
    error) when the instruction names a window the data cannot
    support. A rug instruction is one of:
      * the dev selling: "sell everything if the dev starts
        selling" / "if the creator dumps" — the dev's wallet is NOT
        visible in the feed, so this maps to the price-based fast
        drop with the default thresholds.
      * a rug rule: "add a rug rule (to bot X)" / "rug protection".
      * a fast drop: "sell if the price drops 15% in a minute" /
        "sell everything if it falls 25% in 5 minutes".
    """
    drop = None
    window_seconds = None
    dm = re.search(
        r"(?:drop|drops|fall|falls|decline|declines|down)\s+"
        r"(?:by\s+)?([\d.]+)\s*%\s*(?:in|over|within)?\s*"
        r"(?:(?:(\d+)|a|an)\s+)?(min|mins|minute|minutes|hour|hours)\b",
        msg)
    if dm:
        drop = float(dm.group(1))
        n = int(dm.group(2)) if dm.group(2) else 1
        window_seconds = n * _WINDOW_WORDS[dm.group(3)] * 60
        if window_seconds not in (60, 300):
            return (None, None,
                    "A rug sell can only watch a 1-minute or a "
                    "5-minute drop — those are the windows the price "
                    "data supports. Nothing was changed; try again "
                    "with one of those windows.")
    dev_sell = re.search(
        r"\b(dev|developer|creator)\b[^.]{0,50}\b(sell|sells|selling|"
        r"dump|dumps|dumping|rug|rugs|pull|pulls|exit|exits)\b", msg)
    rug_word = re.search(
        r"(\brug\b[^.]{0,30}\b(sell|rule|protection|exit)\b"
        r"|\b(sell|add|set|turn on)\b[^.]{0,30}\brug\b)", msg)
    if drop is None and not dev_sell and not rug_word:
        return (None, None, None)
    rug = {"drop_pct": drop if drop is not None else 15.0,
           "window_seconds": window_seconds
           if window_seconds is not None else 60,
           "drop_5m_pct": 25.0}
    return (rug, _extract_bot_name(raw), None)


def _parse_top10(msg: str):
    """Parse a top-10 wallets dump instruction into a top10_rug rule.

    "sell if the top wallets dump" / "if the top 10 wallets drop 10%"
    — the top 10 holders' combined holdings falling means the big
    holders are selling. Returns {"drop_pct": ...} (10% default) or
    None when the message carries no top-wallets instruction.
    """
    if not re.search(r"\btop\s*(10|ten)?\s*wallets?\b", msg):
        return None
    dm = re.search(
        r"\btop\s*(?:10|ten)?\s*wallets?\b[^.]{0,80}?"
        r"(\d+(?:\.\d+)?)\s*%", msg)
    return {"drop_pct": float(dm.group(1)) if dm else 10.0}


def _parse_rules_command(msg: str, raw: str):
    """Parse a plain-language rules instruction into a partial rule set.

    Returns (partial_rules, bot_name) or ("unsupported", reply) when
    the instruction is a rules instruction the grammar cannot express,
    or None when the message is not a rules instruction at all. A
    partial rule set may leave trailing_stop_pct as None — the route
    fills it from the bot's own stop loss before validating.
    """
    # A rug instruction on its own ("sell everything if the dev
    # starts selling") carries no buy signal: it is a change to a
    # bot's existing rules, handled by the rules_rug intent — not a
    # fresh rule set.
    rug, rug_bot, rug_err = _parse_rug(msg, raw)
    if rug_err:
        return ("unsupported", rug_err)
    # Scanner profit rules: "scanner take profit at 3% when the
    # market is slow" / "hold the runners" — the scanner's profit
    # ladders (slow ladder + trending runner ladder). No buy signal
    # is named, so the full scanner preset is proposed: slow market
    # secures profit early, trending market holds the runners.
    if re.search(
            r"\b(take profit|take profits|secure (a )?profit|"
            r"hold (the )?runners?|hold (the )?bull ?runners?)\b",
            msg) and ("scanner" in msg or "snipe" in msg
                      or "sniper" in msg or "runner" in msg):
        return dict(scanner_ruleset()), _extract_bot_name(raw)
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
            if rug is not None:
                return ("rug_only", rug_bot, rug)
            return None
        window_minutes = int(buy2.group(1)) * _WINDOW_WORDS[buy2.group(2)]
        threshold = float(buy2.group(3))
    window_seconds = window_minutes * 60
    if window_seconds not in WINDOW_METRICS:
        return ("unsupported",
                "I can only use 3-minute (scanner), 5-minute, 1-hour, "
                "6-hour, or 24-hour windows — those are the windows the "
                "price feed provides, plus the 3-minute window the "
                "scanner tracks itself. Nothing was changed; try again "
                "with one of those windows.")
    is_scanner = window_seconds == 180

    # Bot name: "bot <name>" up to the next keyword, or "for <name>".
    bot_name = _extract_bot_name(raw)

    sources = []
    if "pump.fun" in msg or "pump fun" in msg:
        sources.append("pump.fun")
    if "pumpswap" in msg or "pump swap" in msg:
        sources.append("pumpswap")
    if "dexscreener" in msg:
        sources.append("dexscreener")
    if not sources and "scan" in msg:
        sources = ["pump.fun", "pumpswap"]
    if not sources and is_scanner:
        # A 3-minute gain is only tracked on the pump.fun/PumpSwap
        # feed, so a scanner defaults to those sources.
        sources = ["pump.fun", "pumpswap"]

    interval = 30 if is_scanner else 60
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
        # A scanner has no sell ladder: it exits on the rug sense,
        # the trailing stop and the stop loss.
        ladder = [] if is_scanner else [
            {"pct_of_position": 100.0, "profit_pct": 10.0}]

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
        "kind": "scanner" if is_scanner else (
            "trenches" if sources or window_seconds == 300 else "signals"),
        "scan_sources": sources,
        "scan_interval_seconds": interval,
        "buy": {"metric": WINDOW_METRICS[window_seconds],
                "window_seconds": window_seconds,
                "threshold_pct": threshold, "pick": "strongest"},
        "sell_ladder": ladder,
        "trailing_stop_pct": trailing,
        "rug_sell": rug,
        "top10_rug": _parse_top10(msg),
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

    # Scanner preset: "create a scanner bot" / "make a scanner bot
    # that sells if the top wallets dump" / "create a sniper bot".
    if "scanner" in msg and "bot" in msg and re.search(
            r"\b(create|make|new|add|start)\b", msg):
        return {"intent": "preset_create", "preset": "scanner", "reply": ""}
    if "snipe" in msg and re.search(
            r"\b(create|make|new|add|start)\b", msg):
        return {"intent": "preset_create", "preset": "scanner", "reply": ""}

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

    # "Never sell below what I paid": not a rule — the scanner cuts
    # a bad coin at the 7% stop loss instead. Explain, change
    # nothing. The route returns this reply as-is.
    if re.search(
            r"\bnever\s+sell\s+(below|at a loss|for less)\b|"
            r"\b(don't|do not|never)\s+sell\b[^.]{0,60}\b"
            r"(below (cost|what i paid|my (buy|entry) price)|at a loss)\b",
            msg):
        return {"intent": "never_below_cost",
                "reply": NEVER_BELOW_COST_REPLY}

    # Bot rules: "bot Luna scan pump.fun and pumpswap, buy when a coin
    # gains 5% in 5 minutes" / "set rules for bot Luna ...". The parser
    # only proposes — the route shows the rules back and asks for a
    # confirmation before anything is stored. A rug instruction on its
    # own ("sell everything if the dev starts selling") is a change
    # to a bot's existing rules, not a fresh rule set.
    parsed_rules = _parse_rules_command(msg, raw)
    if isinstance(parsed_rules, tuple) and len(parsed_rules) == 3 \
            and parsed_rules[0] == "rug_only":
        return {"intent": "rules_rug", "bot_name": parsed_rules[1],
                "rug": parsed_rules[2], "reply": ""}
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
