"""builderBot: the conversational bot builder.

After pressing Create Bot, builderBot asks SIMPLE questions ONE AT A
TIME, in plain human language, explaining what each choice means and
why — then turns the answers into the bot's rules. The rules ARE the
code the bot runs: they are validated by the rules engine
(fail-closed) and executed by the silent loop. builderBot is
deterministic — a parser plus validated rules — NOT a general LLM,
and nothing in the UI claims otherwise.

Identity: one builderBot per user, named "builderBot", with a unique
immutable 8-digit ID (same pattern as the signing bot's support ID:
only INSERT exists, no update path).

SECURITY — builderBot NEVER has access to wallet keys:
  * This module never calls the wallet-key decryptors in
    security.py and never receives key material. Only the signing
    bot (executor.py) touches keys. The one decrypt call here reads
    back builderBot's OWN chat-state blob (which it encrypted
    itself) — never a wallet secret.
  * Everything builderBot stores (chat history, adjustment reasons)
    is encrypted at rest with the app's AES-256-GCM pattern via
    ENCRYPTION_KEY.
  * Every answer is run through the scrubber (scrubber.py) BEFORE
    storage or logging — anything shaped like a wallet key, seed
    phrase, password or personal info is deleted first.
  * Its working memory (the conversation) is wiped the moment the
    bot is created or the conversation is cancelled, and deleting a
    bot purges builderBot's stored adjustments and ledger rows for it.

Auto-adjustment: builderBot reviews each silent bot's trade ledger
after every loop iteration. A bot is LOSING when it has at least 5
closed trades and a win rate of 40% or less. A losing bot's rules
are tightened — ONLY tightened, never widened — one small step at a
time, at most once per 24 hours, in this priority order:
  1. stop loss down 1% (floor 3%)
  2. buy-signal threshold up 0.5% (cap 15%)
  3. buy size down 5% (floor 5%)
Every adjustment is logged with the reason (which trades triggered
it) and shown on the bot's page. The user can reverse any of them
by editing the bot.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from .ledger import bot_wl_summary
from .paper import reset_paper_balance
from .rules import (REGIME_DEFAULT, SLOW_LADDER, TRENDING_LADDER,
                    load_rules, summarize_rules, validate_ruleset)
from .scanner import BUY_FILTER_DEFAULT
from .scrubber import REDACTED, scrub_text
from .security import (decrypt_secret, encrypt_secret, hash_token,
                       new_token)
from .signing_bot import generate_support_id

BUILDERBOT_NAME = "builderBot"

# A bot is losing when it has this many closed trades and its win
# rate is at or below this fraction.
LOSING_MIN_TRADES = 5
LOSING_MAX_WIN_RATE = 0.40
# At most one adjustment per bot per 24 hours.
ADJUST_COOLDOWN_SECONDS = 24 * 3600
# Tightening steps (one per review, in priority order).
STOP_LOSS_STEP_PCT = 1.0
STOP_LOSS_FLOOR_PCT = 3.0
THRESHOLD_STEP_PCT = 0.5
THRESHOLD_CAP_PCT = 15.0
BUY_SIZE_STEP_PCT = 5.0
BUY_SIZE_FLOOR_PCT = 5.0

SIGNALS_DEFAULT_COINS = []


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


# --- identity ---------------------------------------------------------

def ensure_builderbot(db, user_id: int) -> dict:
    """The user's builderBot identity, creating it on first use.
    The 8-digit ID is immutable: only INSERT exists, no update path
    anywhere in this module."""
    row = db.execute(
        "SELECT user_id, builder_id, created_at FROM builderbots "
        "WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    for _attempt in range(5):
        builder_id = generate_support_id()
        try:
            db.execute(
                "INSERT INTO builderbots (user_id, builder_id, "
                "created_at) VALUES (?, ?, ?)",
                (user_id, builder_id, _now()))
            db.commit()
            return {"user_id": user_id, "builder_id": builder_id,
                    "created_at": _now()}
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a unique builderBot ID")


# --- conversation state (encrypted at rest) ---------------------------

def _save_state(db, user_id: int, enc_key: bytes, state: dict) -> None:
    blob = encrypt_secret(json.dumps(state).encode("utf-8"), enc_key)
    db.execute(
        "INSERT INTO builderbot_chats (user_id, encrypted_state, "
        "updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET encrypted_state = "
        "excluded.encrypted_state, updated_at = excluded.updated_at",
        (user_id, blob, _now()))
    db.commit()


def _load_state(db, user_id: int, enc_key: bytes):
    row = db.execute(
        "SELECT encrypted_state FROM builderbot_chats WHERE user_id = ?",
        (user_id,)).fetchone()
    if not row:
        return None
    try:
        raw = decrypt_secret(row["encrypted_state"], enc_key)
        state = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict) or "step" not in state:
        return None
    return state


def wipe_conversation(db, user_id: int) -> None:
    """Delete builderBot's working memory for this user — after the
    bot is created, after a cancel, or on demand."""
    db.execute("DELETE FROM builderbot_chats WHERE user_id = ?",
               (user_id,))
    db.commit()


# --- question parsing ---------------------------------------------------

def _parse_money(text: str):
    """$10,000 / 10000 / 10k / 1m / 0 / any / none -> dollars, or None
    when the text is not a money amount."""
    raw = text.strip().lower().replace("$", "").replace(",", "")
    if raw in ("any", "none", "no minimum", "no min", "0", "zero"):
        return 0.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", raw)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2) == "k":
        value *= 1_000
    elif match.group(2) == "m":
        value *= 1_000_000
    return value


def _parse_pct(text: str):
    """5 / 5% / five -> 5.0, or None."""
    raw = text.strip().lower().rstrip("%").strip()
    words = {"one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0,
             "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0,
             "nine": 9.0, "ten": 10.0, "fifteen": 15.0,
             "twenty": 20.0, "twenty-five": 25.0, "fifty": 50.0}
    if raw in words:
        return words[raw]
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_int(text: str):
    raw = text.strip().lower()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    if raw in words:
        return words[raw]
    try:
        value = int(raw)
    except ValueError:
        return None
    return value


def _parse_signal(text: str):
    """'5% in 5 minutes' / '5% in 3 min' / '10% in 1 hour' / '5%'
    -> (threshold_pct, window_seconds), or None. A bare % defaults
    to a 5-minute window."""
    raw = text.strip().lower()
    if raw in ("default", "standard", "skip"):
        return 5.0, 300
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*%?\s*(?:in\s+)?(\d+)?\s*"
        r"(min|mins|minute|minutes|hour|hours|hr|hrs|h|sec|secs|"
        r"second|seconds)?", raw)
    if not match:
        return None
    threshold = float(match.group(1))
    amount = match.group(2)
    unit = match.group(3)
    if amount is None or unit is None:
        return threshold, 300
    amount = int(amount)
    if unit.startswith("sec"):
        window = amount
    elif unit.startswith("min"):
        window = amount * 60
    elif unit in ("h", "hr", "hrs") or unit.startswith("hour"):
        window = amount * 3600
    else:
        window = 300
    if window not in (180, 300, 3600, 21600, 86400):
        return None
    return threshold, window


def _parse_ladder(text: str):
    """'default' / 'ladder' / 'all at +10%' / 'hold runners' /
    '25% at +3%, 25% at +10%' -> a sell ladder, or None."""
    raw = text.strip().lower()
    if ("default" in raw or raw in ("ladder", "standard", "skip", "yes")):
        return [dict(r) for r in SLOW_LADDER]
    if "runner" in raw or "hold" in raw:
        return [dict(r) for r in TRENDING_LADDER]
    if "all" in raw:
        match = re.search(r"\+?\s*(\d+(?:\.\d+)?)\s*%", raw)
        target = float(match.group(1)) if match else 10.0
        return [{"pct_of_position": 100, "profit_pct": target}]
    parts = re.findall(
        r"(\d+(?:\.\d+)?)\s*%\s*(?:at\s*)?\+?\s*(\d+(?:\.\d+)?)\s*%",
        raw)
    if not parts:
        return None
    ladder = [{"pct_of_position": float(p), "profit_pct": float(t)}
              for p, t in parts]
    total = sum(r["pct_of_position"] for r in ladder)
    if total > 100:
        return None
    if total < 100:
        ladder.append({"pct_of_position": 100 - total,
                       "profit_pct": ladder[-1]["profit_pct"] * 2})
    return ladder


def _parse_top10(text: str):
    """'yes' / 'both' / '21%' / '25%' / '21% only' / 'off' ->
    (concentration_pct or None, slow_climb_sell_pct). None
    concentration means no top-10 watch at all."""
    raw = text.strip().lower()
    if raw in ("off", "none", "no", "skip", "turn it off"):
        return None, 0.0
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw)
    concentration = float(match.group(1)) if match else 21.0
    if "only" in raw or "no slow" in raw or "no half" in raw:
        return concentration, 0.0
    return concentration, 50.0


def _parse_interval(text: str):
    raw = text.strip().lower()
    if raw in ("default", "standard", "skip"):
        return None  # caller fills the kind default
    match = re.search(r"(\d+)\s*(sec|secs|second|seconds|min|mins|"
                      r"minute|minutes|hour|hours|hr|hrs|h)?", raw)
    if not match:
        return -1
    amount = int(match.group(1))
    unit = match.group(2) or "min"
    if unit.startswith("sec"):
        return amount
    if unit.startswith("min"):
        return amount * 60
    return amount * 3600


def _parse_scan_interval_answer(text: str):
    """Scan-interval answer: (seconds, None) — None seconds means
    'use the default for the bot kind'. (None, error) when the
    answer is not usable."""
    value = _parse_interval(text)
    if value is None:
        return None, None
    if 10 <= value <= 3600:
        return value, None
    return None, ("Type something like \"30 seconds\", \"1 minute\" or "
                  "\"1 hour\" — between 10 seconds and 1 hour.")


def _parse_choice(text: str, options, default):
    raw = text.strip().lower()
    if raw in ("default", "standard", "skip", ""):
        return default
    for option in options:
        if option.lower() in raw or raw in option.lower():
            return option
    return None


# The questions, in order. Each step: the question, why it matters,
# tap-to-accept suggestions, and a parser returning
# (value, error_message_or_None).
STEPS = [
    {
        "key": "name",
        "question": "What should we call this bot?",
        "why": "A name helps you tell your bots apart on the Bots "
               "page — pick anything you like.",
        "suggestions": ["Luna", "Nova", "Moonshot"],
        "parse": lambda text: (
            (text.strip()[:40], None)
            if 1 <= len(text.strip()) <= 40
            else (None, "Give the bot a name — 1 to 40 characters. "
                        "Tap a suggestion or type your own.")),
    },
    {
        "key": "mode",
        "question": "Paper or real trading?",
        "why": "Paper trades with $100 of fake money — nothing real "
               "at risk, and a good way to see if the rules work. "
               "Real trades with devnet SOL (test money, no real "
               "value) and the signing bot signs them by itself.",
        "suggestions": ["Paper", "Real (devnet)"],
        "parse": lambda text: (
            ("paper" if _parse_choice(
                text, ["Paper", "Real (devnet)"], "Paper") == "Paper"
             else "real", None)
            if _parse_choice(text, ["Paper", "Real (devnet)"],
                             "Paper") is not None
            else (None, "Tap Paper or Real (devnet).")),
    },
    {
        "key": "market_cap_min",
        "question": "What is the smallest coin it may buy? "
                    "(Market-cap minimum — type $0 for any size.)",
        "why": "Market cap is the total value of all of a coin's "
               "tokens. A minimum filters out brand-new junk coins "
               "that can vanish overnight. $0 means any size counts.",
        "suggestions": ["$0 (any)", "$10,000", "$100,000",
                        "$1,000,000"],
        "parse": lambda text: (
            (_parse_money(text), None)
            if _parse_money(text) is not None
            and 0 <= _parse_money(text) <= 1000000000000
            else (None, "Type a dollar amount like $10,000, or $0 "
                        "for any size.")),
    },
    {
        "key": "buy_signal",
        "question": "When should it buy? Tell me a gain and a "
                    "window, like \"5% in 5 minutes\".",
        "why": "This is the buy signal — a coin jumping that much, "
               "that fast, is momentum: traders are piling in. A "
               "bigger gain means fewer, stronger buys; a smaller "
               "one means more, weaker buys. 5% in 3 minutes is the "
               "sniper pace; 5% in 5 minutes is the trenches pace.",
        "suggestions": ["5% in 5 minutes", "5% in 3 minutes",
                        "3.4% in 5 minutes", "10% in 1 hour"],
        "parse": lambda text: (
            (_parse_signal(text), None)
            if _parse_signal(text) is not None
            and 0.1 <= _parse_signal(text)[0] <= 100
            else (None, "Type something like \"5% in 5 minutes\" — a "
                        "gain % and a window (3 minutes, 5 minutes, "
                        "1 hour, 6 hours or 24 hours).")),
    },
    {
        "key": "buy_size",
        "question": "How much of your money should each buy use? "
                    "(Percent of your balance.)",
        "why": "Each buy spends this % of your balance. Smaller "
               "means one bad coin hurts less; bigger means a winner "
               "pays more. 20% is the usual middle ground.",
        "suggestions": ["10%", "20%", "25%"],
        "parse": lambda text: (
            (_parse_pct(text), None)
            if _parse_pct(text) is not None
            and 1 <= _parse_pct(text) <= 50
            else (None, "Type a percent between 1% and 50% — 20% is "
                        "the usual middle ground.")),
    },
    {
        "key": "max_open",
        "question": "How many coins can it hold at once?",
        "why": "A cap on open trades keeps the bot focused — and "
               "keeps a bad day from spreading across a dozen coins "
               "at once. 3 is the usual number.",
        "suggestions": ["1", "3", "5"],
        "parse": lambda text: (
            (_parse_int(text), None)
            if _parse_int(text) is not None
            and 1 <= _parse_int(text) <= 5
            else (None, "Type a whole number from 1 to 5.")),
    },
    {
        "key": "stop_loss",
        "question": "Stop loss: if a coin drops this far below what "
                    "you paid, sell it. How far?",
        "why": "The stop loss is the line that protects you: sell "
               "and take a small loss instead of watching a bad coin "
               "go to zero. 7% is the usual line — it caps what one "
               "bad coin can cost you.",
        "suggestions": ["5%", "7%", "10%"],
        "parse": lambda text: (
            (_parse_pct(text), None)
            if _parse_pct(text) is not None
            and 0.5 <= _parse_pct(text) <= 50
            else (None, "Type a percent between 0.5% and 50% — 7% is "
                        "the usual line.")),
    },
    {
        "key": "take_profit",
        "question": "When it wins, how should it take profit?",
        "why": "A sell ladder takes profit in slices as the price "
               "climbs — some early, some later — so you bank wins "
               "without selling the whole runner at once. Or sell it "
               "all at one target, or hold the runners for the big "
               "moves.",
        "suggestions": ["Default ladder", "Sell all at +10%",
                        "Hold runners"],
        "parse": lambda text: (
            (_parse_ladder(text), None)
            if _parse_ladder(text) is not None
            else (None, "Tap Default ladder, Sell all at +10%, or Hold "
                        "runners — or type your own steps like "
                        "\"25% at +3%, 25% at +10%\".")),
    },
    {
        "key": "trailing_stop",
        "question": "As a coin climbs, how far may it fall from its "
                    "highest price before selling? (Trailing stop %.)",
        "why": "The trailing stop locks in gains: if a coin runs up "
               "and then turns, you sell near the top instead of "
               "giving it all back. 7% gives it room to breathe; a "
               "tighter number banks profit sooner.",
        "suggestions": ["7%", "5%", "10%"],
        "parse": lambda text: (
            (_parse_pct(text), None)
            if _parse_pct(text) is not None
            and 0.5 <= _parse_pct(text) <= 50
            else (None, "Type a percent between 0.5% and 50% — 7% is "
                        "the usual number.")),
    },
    {
        "key": "top10",
        "question": "Top-10 wallets: if the 10 biggest holders own "
                    "21% of all tokens, sell everything immediately. "
                    "And if their share keeps climbing slowly toward "
                    "21%, sell half to lock profit. Keep both on?",
        "why": "The 10 biggest holders can dump on everyone else. "
               "I check their combined share every 15 seconds. 21% "
               "is the line where a small group owns a fifth of the "
               "supply — too much power in too few hands. A slow "
               "climb toward it is the early warning of a rug.",
        "suggestions": ["Yes, both", "21% sell-all only",
                        "Turn it off"],
        "parse": lambda text: (
            (_parse_top10(text), None)
            if _parse_top10(text) is not None
            else (None, "Tap Yes, both — or type a % like \"25%\" to "
                        "set your own line, or \"off\" to skip it.")),
    },
    {
        "key": "scan_interval",
        "question": "How often should it scan for new coins?",
        "why": "Every 30 seconds catches fast moves but checks more "
               "often; every minute is the usual pace for memecoins; "
               "every hour suits slow majors. Faster is not always "
               "better — it also means more chances to buy a "
               "false start.",
        "suggestions": ["30 seconds", "1 minute", "5 minutes",
                        "1 hour"],
        "parse": _parse_scan_interval_answer,
    },
]


def _question_text(step: dict) -> str:
    return f"{step['question']}\n{step['why']}"


def _public_state(state: dict) -> dict:
    """What the chatbox page shows: the message history, the current
    suggestions, and whether it is waiting on a confirmation."""
    step = state.get("step")
    if state.get("pending") == "paper_reset":
        suggestions = ["$100", "$500", "$1,000"]
    elif step == "confirm":
        suggestions = ["Yes", "No, start over"]
    elif isinstance(step, int) and 0 <= step < len(STEPS):
        suggestions = list(STEPS[step]["suggestions"])
    else:
        suggestions = []
    return {"messages": list(state.get("messages", [])),
            "suggestions": suggestions,
            "confirm": step == "confirm",
            "done": step == "done"}


# --- conversation flow --------------------------------------------------

def start_conversation(db, user_id: int, enc_key: bytes) -> dict:
    """Begin (or restart) a builderBot conversation. The old working
    memory is wiped first — one conversation at a time."""
    identity = ensure_builderbot(db, user_id)
    wipe_conversation(db, user_id)
    state = {"step": 0, "answers": {}, "messages": []}
    intro = (f"Hi, I'm {BUILDERBOT_NAME} (ID {identity['builder_id']}). "
             f"I'll ask you a few simple questions to build your "
             f"trading bot — one at a time. Tap a suggestion to "
             f"accept it, or type your own answer. I turn your "
             f"answers into the bot's rules, and those rules are the "
             f"code the bot runs. Never paste a wallet key, seed "
             f"phrase or password here — I delete anything that "
             f"looks like a secret, and I never have access to keys.")
    state["messages"].append({"role": "builderbot", "text": intro})
    state["messages"].append(
        {"role": "builderbot",
         "text": _question_text(STEPS[0])})
    _save_state(db, user_id, enc_key, state)
    return _public_state(state)


def _kind_for_window(window_seconds: int) -> str:
    if window_seconds == 180:
        return "scanner"
    if window_seconds == 300:
        return "trenches"
    return "signals"


def _window_metric(window_seconds: int) -> str:
    return {180: "gain_3m_pct", 300: "gain_5m_pct",
            3600: "gain_1h_pct", 21600: "gain_6h_pct",
            86400: "gain_24h_pct"}[window_seconds]


def _default_interval(kind: str) -> int:
    return {"scanner": 30, "trenches": 60, "signals": 3600}[kind]


def _compress_ladder(ladder, max_rungs: int):
    """Merge a ladder down to max_rungs by combining the trailing
    rungs into one (their %s add up, the profit target is the last
    rung's) — the economics stay the same: the same total sells, the
    same final target. Non-scanner ladders cap at 3 rungs; the
    4-rung default ladder compresses to 3 for them."""
    ladder = [dict(r) for r in ladder]
    while len(ladder) > max_rungs:
        last = ladder.pop()
        ladder[-1]["pct_of_position"] = (
            float(ladder[-1]["pct_of_position"])
            + float(last["pct_of_position"]))
        ladder[-1]["profit_pct"] = float(last["profit_pct"])
    return ladder


def _build_ruleset(answers: dict) -> dict:
    """The answers, as a validated rule set. Raises ValueError with a
    plain-words reason when the combination does not validate."""
    threshold, window = answers["buy_signal"]
    kind = _kind_for_window(window)
    interval = answers["scan_interval"]
    if interval is None:
        interval = _default_interval(kind)
    ladder = _compress_ladder(answers["take_profit"],
                              4 if kind == "scanner" else 3)
    rules = {
        "version": 1,
        "kind": kind,
        "scan_sources": (["pump.fun", "pumpswap"]
                         if kind in ("trenches", "scanner") else []),
        "scan_interval_seconds": int(interval),
        "buy": {"metric": _window_metric(window),
                "window_seconds": window,
                "threshold_pct": float(threshold),
                "pick": "strongest"},
        "sell_ladder": ladder,
        "trailing_stop_pct": float(answers["trailing_stop"]),
        "rug_sell": {"drop_pct": 15.0, "window_seconds": 60,
                     "drop_5m_pct": 25.0},
        "max_new_positions_per_scan": 1,
    }
    if kind in ("trenches", "scanner"):
        # The buy filter is a trenches/scanner rule only — a signals
        # bot watches its own coin list and takes no filter.
        buy_filter = dict(BUY_FILTER_DEFAULT)
        buy_filter["min_market_cap_usd"] = float(answers["market_cap_min"])
        rules["buy_filter"] = buy_filter
    concentration, slow_climb = answers["top10"]
    if concentration is not None and kind in ("trenches", "scanner"):
        # The top-10 watch is a memecoin rule (trenches/scanner
        # only) — a signals bot watching its own coin list gets none.
        rules["top10_rug"] = {
            "drop_pct": 10.0,
            "concentration_pct": float(concentration),
            "slow_climb_sell_pct": float(slow_climb),
            "check_interval_seconds": 15.0,
        }
    if kind == "scanner":
        rules["trending_ladder"] = [dict(r) for r in TRENDING_LADDER]
        rules["regime"] = dict(REGIME_DEFAULT)
    cleaned, error = validate_ruleset(
        rules, bot={"stop_loss_pct": float(answers["stop_loss"])})
    if error is not None:
        raise ValueError(error)
    return cleaned


def _window_words_short(window_seconds: int) -> str:
    if window_seconds < 60:
        return f"{window_seconds} seconds"
    if window_seconds < 3600:
        return f"{window_seconds // 60} minutes"
    if window_seconds == 3600:
        return "1 hour"
    return f"{window_seconds // 3600} hours"


def _confirmation_text(answers: dict, rules: dict) -> str:
    mode_word = ("Paper (fake money)" if answers["mode"] == "paper"
                 else "Real — devnet SOL")
    mcap = float(answers["market_cap_min"])
    mcap_word = ("any size" if mcap <= 0
                 else f"${mcap:,.0f} or bigger")
    threshold, window = answers["buy_signal"]
    interval = answers["scan_interval"]
    if interval is None:
        interval = _default_interval(_kind_for_window(window))
    lines = [
        "Here is the bot I would build — check it over:",
        f"• Name: {answers['name']}",
        f"• Mode: {mode_word}",
        f"• Smallest coin: {mcap_word}",
        f"• Buy when a coin gains {threshold}% in "
        f"{_window_words_short(window)}",
        f"• Each buy uses {answers['buy_size']}% of your balance, "
        f"at most {answers['max_open']} coins at once",
        f"• Stop loss {answers['stop_loss']}%, trailing stop "
        f"{answers['trailing_stop']}%",
        f"• Scan every {_window_words_short(int(interval))}",
        "",
        "Rules in full:",
        summarize_rules(rules),
        "",
        "Reply \"yes\" to create it, or \"no\" to start over.",
    ]
    return "\n".join(lines)


def _fmt_cad(amount: float) -> str:
    """$500 / $1,000 / $1,250.50 — whole dollars without decimals."""
    if float(amount) == int(amount):
        return f"${int(amount):,}"
    return f"${float(amount):,.2f}"


def _answer_paper_reset(db, user_id: int, enc_key: bytes, state: dict,
                        cleaned: str) -> dict:
    """One turn of the 'Add cad' paper-reset flow. `state` already
    carries pending='paper_reset' and the user's message is already
    appended. Returns the public state (plus paper_reset on success,
    so the route can log it). The reset is paper only: it touches
    only this user's paper tables, and the bot win/loss counts
    (trade_ledger) are never touched."""
    text = cleaned.strip()
    if text.lower() == "cancel":
        # Back out of the reset — the bot-building conversation is
        # untouched, and the next message answers its question.
        state.pop("pending", None)
        step = state.get("step")
        if isinstance(step, int) and 0 <= step < len(STEPS):
            state["messages"].append(
                {"role": "builderbot",
                 "text": "No problem — nothing was changed. "
                         + _question_text(STEPS[step])})
        elif step == "confirm":
            state["messages"].append(
                {"role": "builderbot",
                 "text": "No problem — nothing was changed. Reply "
                         "\"yes\" to create the bot, or \"no\" to "
                         "start over."})
        else:
            state["messages"].append(
                {"role": "builderbot",
                 "text": "No problem — nothing was changed."})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    amount = _parse_money(text)
    if amount is None and text.lstrip().startswith("-"):
        # A negative number is a number below zero, not "not a
        # number" — _parse_money has no minus sign, so parse the
        # magnitude and negate it to get the right rejection.
        magnitude = _parse_money(text.lstrip()[1:])
        if magnitude is not None:
            amount = -magnitude
    if amount is None:
        state["messages"].append(
            {"role": "builderbot",
             "text": "That doesn't look like a number — type an "
                     "amount like $500, or 1000. Nothing was "
                     "changed. How much would you like to start "
                     "with?"})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    if amount <= 0:
        state["messages"].append(
            {"role": "builderbot",
             "text": "The amount has to be more than $0 — type a "
                     "positive amount like $500. Nothing was "
                     "changed. How much would you like to start "
                     "with?"})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    reset_paper_balance(db, user_id, amount)
    state.pop("pending", None)
    state["messages"].append(
        {"role": "builderbot",
         "text": f"Done — your paper balance is now "
                 f"{_fmt_cad(amount)} CAD. Your paper positions, "
                 f"holdings, and P&L history are reset to a fresh "
                 f"start. Your bots' win/loss counts are unchanged."})
    _save_state(db, user_id, enc_key, state)
    result = _public_state(state)
    result["paper_reset"] = amount
    return result


def answer(db, user_id: int, enc_key: bytes, raw_text: str) -> dict:
    """One chatbox turn. Returns the public state (messages,
    suggestions, confirm/done flags) plus, on completion,
    {"created": {...}} or {"cancelled": True} or {"error": ...}."""
    state = _load_state(db, user_id, enc_key)
    if state is None:
        return {"error": "No conversation is open — press Create Bot "
                         "to start one.", "messages": [],
                "suggestions": [], "confirm": False, "done": False}
    cleaned, redactions = scrub_text(raw_text)
    if redactions and cleaned.strip() in (REDACTED, ""):
        state["messages"].append(
            {"role": "builderbot",
             "text": "I removed something that looked like a secret "
                     "(a key, seed phrase or password) — I never "
                     "store those, and I never have access to keys. "
                     "Please answer without pasting keys or personal "
                     "info."})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    # Only the scrubbed text is ever stored or logged.
    state["messages"].append({"role": "user", "text": cleaned})

    # "Add cad" command: reset the paper balance to a fresh start.
    # A command, not an answer to the current bot-building
    # question — it works at any step, and the pending flag carries
    # the ask across turns. Paper only, never real.
    if state.get("pending") == "paper_reset":
        return _answer_paper_reset(db, user_id, enc_key, state, cleaned)
    if cleaned.strip().lower() == "add cad":
        state["pending"] = "paper_reset"
        state["messages"].append(
            {"role": "builderbot",
             "text": "How much would you like to start with?"})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)

    step = state["step"]
    if step == "confirm":
        verdict = cleaned.strip().lower()
        if verdict in ("yes", "y", "yes please", "create it",
                       "go ahead", "ok", "okay", "confirm"):
            try:
                bot_id, summary = create_bot_from_answers(
                    db, user_id, state["answers"])
            except ValueError as exc:
                state["messages"].append(
                    {"role": "builderbot", "text": str(exc)})
                _save_state(db, user_id, enc_key, state)
                return _public_state(state)
            wipe_conversation(db, user_id)
            result = _public_state({"step": "done", "messages":
                                    state["messages"] + [
                                        {"role": "builderbot",
                                         "text": summary}]})
            result["created"] = {"bot_id": bot_id}
            return result
        if verdict in ("no", "n", "cancel", "start over", "nope"):
            wipe_conversation(db, user_id)
            result = _public_state({"step": "done", "messages":
                                    state["messages"] + [
                                        {"role": "builderbot",
                                         "text": "No problem — I wiped "
                                                 "that conversation. "
                                                 "Press Create Bot to "
                                                 "start over."}]})
            result["cancelled"] = True
            return result
        state["messages"].append(
            {"role": "builderbot",
             "text": "Reply \"yes\" to create the bot, or \"no\" to "
                     "start over."})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)

    step_def = STEPS[step]
    value, error = step_def["parse"](cleaned)
    if error is not None:
        state["messages"].append({"role": "builderbot", "text": error})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    state["answers"][step_def["key"]] = value
    next_step = step + 1
    if next_step >= len(STEPS):
        # All questions answered — show the whole bot back and ask
        # for a yes before anything is created.
        try:
            rules = _build_ruleset(state["answers"])
        except ValueError as exc:
            state["messages"].append(
                {"role": "builderbot",
                 "text": f"Those answers do not fit together: {exc} "
                         f"Let's start over — press Create Bot again."})
            wipe_conversation(db, user_id)
            result = _public_state({"step": "done", "messages":
                                    state["messages"]})
            result["error"] = str(exc)
            return result
        state["step"] = "confirm"
        state["messages"].append(
            {"role": "builderbot",
             "text": _confirmation_text(state["answers"], rules)})
        _save_state(db, user_id, enc_key, state)
        return _public_state(state)
    state["step"] = next_step
    state["messages"].append(
        {"role": "builderbot",
         "text": _question_text(STEPS[next_step])})
    _save_state(db, user_id, enc_key, state)
    return _public_state(state)


def create_bot_from_answers(db, user_id: int, answers: dict):
    """Create the bot a finished conversation describes. Returns
    (bot_id, plain-words summary). Raises ValueError with a
    plain-words reason when it cannot (e.g. a Scanner bot already
    exists — one per user)."""
    rules = _build_ruleset(answers)
    kind = rules["kind"]
    if kind == "scanner":
        existing = db.execute(
            "SELECT id FROM bots WHERE user_id = ? AND category = "
            "'scanner'", (user_id,)).fetchone()
        if existing:
            raise ValueError(
                "You already have a Scanner bot — one per user. "
                "Edit that one instead of creating another.")
    category = {"scanner": "scanner",
                "trenches": "Memecoin build"}.get(kind, "")
    coins = (list(SIGNALS_DEFAULT_COINS) if kind == "signals" else [])
    # Every bot gets its own API token, hashed at rest — the plain
    # token is shown once, in the creation summary below, and never
    # stored or shown again (same as the create form).
    token = new_token()
    cursor = db.execute(
        "INSERT INTO bots (user_id, name, coins, strategy, category, "
        "buy_size_pct, stop_loss_pct, max_open_trades, platform, mode, "
        "silent, rules_json, api_token_hash, created_at) VALUES "
        "(?, ?, ?, 'balanced', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (user_id, answers["name"], json.dumps(coins), category,
         float(answers["buy_size"]), float(answers["stop_loss"]),
         int(answers["max_open"]), "pump.fun", answers["mode"],
         json.dumps(rules), hash_token(token), _now()))
    bot_id = cursor.lastrowid
    db.commit()
    summary = (f"Done — your bot \"{answers['name']}\" is created. "
               f"{summarize_rules(rules)} It starts with silent "
               f"trading OFF: switch it on from the bot's page when "
               f"you are ready. Its API token, shown once — save it "
               f"if you need it: {token}")
    return bot_id, summary


# --- auto-adjustment ------------------------------------------------------

def list_adjustments(db, bot_id: int) -> list:
    """Adjustment log for one bot, newest first. The reason is stored
    encrypted (see the bot detail route, which decrypts it for
    display) — this module never decrypts it."""
    rows = db.execute(
        "SELECT * FROM builderbot_adjustments WHERE bot_id = ? "
        "ORDER BY id DESC LIMIT 20", (bot_id,)).fetchall()
    return [dict(row) for row in rows]


def review_bot(db, bot, enc_key: bytes):
    """Review one bot's trade ledger and tighten its rules when it is
    losing — at most one small step, at most once per 24 hours.

    Losing = at least 5 closed trades and a win rate of 40% or less.
    Tighten-only, in priority order: stop loss down 1% (floor 3%),
    then buy-signal threshold up 0.5% (cap 15%), then buy size down
    5% (floor 5%). Returns the adjustment dict, or None when nothing
    changed. Never raises — a review failure must not break the loop.
    """
    try:
        return _review_bot(db, bot, enc_key)
    except Exception:
        return None


def _review_bot(db, bot, enc_key: bytes):
    summary = bot_wl_summary(db, bot["id"])
    if summary["trades"] < LOSING_MIN_TRADES:
        return None
    win_rate = summary["win_rate"]
    if win_rate is None or win_rate > LOSING_MAX_WIN_RATE:
        return None
    last = db.execute(
        "SELECT created_at FROM builderbot_adjustments WHERE bot_id = ? "
        "ORDER BY id DESC LIMIT 1", (bot["id"],)).fetchone()
    if last:
        last_ts = _parse_time(last["created_at"])
        if last_ts is not None and (
                datetime.now(timezone.utc).timestamp() - last_ts
                < ADJUST_COOLDOWN_SECONDS):
            return None
    reason = (f"{summary['losses']} losses in the last "
              f"{summary['trades']} closed trades (win rate "
              f"{win_rate:.0%}, net "
              f"${summary['net_pnl_usd']:.2f}) — tightening one step.")
    stop_loss = float(bot["stop_loss_pct"])
    if stop_loss > STOP_LOSS_FLOOR_PCT:
        new_value = max(STOP_LOSS_FLOOR_PCT, stop_loss - STOP_LOSS_STEP_PCT)
        db.execute("UPDATE bots SET stop_loss_pct = ? WHERE id = ?",
                   (new_value, bot["id"]))
        field = "stop_loss_pct"
        old_value = stop_loss
    else:
        rules, error = load_rules(bot)
        threshold = None
        if rules is not None and error is None:
            threshold = float(rules["buy"]["threshold_pct"])
        buy_size = float(bot["buy_size_pct"])
        if threshold is not None and threshold < THRESHOLD_CAP_PCT:
            new_value = min(THRESHOLD_CAP_PCT,
                            threshold + THRESHOLD_STEP_PCT)
            rules["buy"]["threshold_pct"] = new_value
            cleaned, error = validate_ruleset(rules, bot=bot)
            if error is not None:
                return None
            db.execute("UPDATE bots SET rules_json = ? WHERE id = ?",
                       (json.dumps(cleaned), bot["id"]))
            field = "buy.threshold_pct"
            old_value = threshold
        elif buy_size > BUY_SIZE_FLOOR_PCT:
            new_value = max(BUY_SIZE_FLOOR_PCT,
                            buy_size - BUY_SIZE_STEP_PCT)
            db.execute("UPDATE bots SET buy_size_pct = ? WHERE id = ?",
                       (new_value, bot["id"]))
            field = "buy_size_pct"
            old_value = buy_size
        else:
            return None
    db.execute(
        "INSERT INTO builderbot_adjustments (bot_id, user_id, field, "
        "old_value, new_value, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bot["id"], bot["user_id"], field, old_value, new_value,
         encrypt_secret(reason.encode("utf-8"), enc_key), _now()))
    db.commit()
    db.execute(
        "INSERT INTO activity (user_id, kind, detail, created_at, mode) "
        "VALUES (?, 'builderbot_adjust', ?, ?, ?)",
        (bot["user_id"],
         f"builderBot tightened '{bot['name']}': {field} "
         f"{old_value} → {new_value}. {reason}",
         _now(), bot["mode"]))
    db.commit()
    return {"field": field, "old_value": old_value,
            "new_value": new_value, "reason": reason}


def purge_bot_data(db, bot_id: int, user_id: int) -> None:
    """Delete builderBot's stored data for a deleted bot: its
    adjustment log and its trade-ledger rows. Called from the shared
    bot-delete helper."""
    db.execute("DELETE FROM builderbot_adjustments WHERE bot_id = ?",
               (bot_id,))
    db.execute("DELETE FROM trade_ledger WHERE bot_id = ?",
               (bot_id,))
    db.commit()
