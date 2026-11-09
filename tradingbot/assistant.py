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

STRATEGIES = ("careful", "balanced", "bold")

CAPABILITIES = (
    "Here's what I can do for you:\n"
    "- \"show my balance\" — paper cash, holdings, and real SOL balance\n"
    "- \"show my wallet\" — your app wallet address and balance\n"
    "- \"show my bots\" — list your trading bots\n"
    "- \"create bot\" — start a new bot (I can take a name and strategy)\n"
    "- \"edit bot <name> strategy to bold\" — change a bot's settings\n"
    "- \"delete bot <name>\" — remove a bot (I ask first)\n"
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
