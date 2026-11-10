"""Losing-buy analysis: how did the coins that ended in a loss get bought?

Pairs paper-trade legs (FIFO per coin) into completed buys, using ONLY
data the app actually recorded:
- buy time / coin / buy price / sell time / sell price / P&L / hold
  time come from paper_trades legs (arithmetic on recorded rows);
- the exit reason comes from the sell leg (recorded since the buy
  filter build) or, for older sells, from the nearest silent_sell /
  silent_stop_loss activity row naming the same coin;
- the bot comes from the buy leg (recorded since the buy filter
  build) or, for older buys, from the nearest silent_buy / paper_buy
  activity row naming the same coin;
- the buy-time signal values (m5 gain, 5-minute volume, buys vs
  sells, regime) come from the buy leg — recorded since the buy
  filter build. Older buys show them as unavailable, never guessed.

A buy that has not sold yet is listed as still holding. The summary
compares LOSING buys vs WINNING buys on the signals that were
actually recorded; averages over pairs with no recorded signal are
shown as unavailable, never as zero.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S")

# A trade leg and its activity row are written in the same request or
# loop tick, so they are seconds apart. 3 minutes of slack covers
# clock skew between the two writes without matching a different
# trade of the same coin.
_MATCH_WINDOW_SECONDS = 180

_EXIT_CATEGORIES = ("sell ladder", "trailing stop", "stop loss",
                    "rug sell", "top-10 dump")


def parse_time(value):
    """A recorded timestamp string -> aware datetime, or None."""
    if not value:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def normalize_exit_reason(raw) -> str:
    """A raw exit reason -> one of the five categories, or the raw
    text when it is something else (e.g. 'sell signal', 'manual
    sell'), or 'unavailable' when nothing was recorded."""
    if raw is None:
        return "unavailable"
    text = str(raw).strip()
    if not text:
        return "unavailable"
    low = text.lower()
    if "stop_loss" in low or "stop loss" in low:
        return "stop loss"
    if "trailing stop" in low:
        return "trailing stop"
    if "rug sell" in low:
        return "rug sell"
    if "sell ladder" in low:
        return "sell ladder"
    if "top-10" in low or "top 10" in low:
        return "top-10 dump"
    return text


def _bot_from_detail(detail: str):
    """Bot name from an activity detail, or None."""
    m = re.search(r"Silent bot '([^']+)'", detail or "")
    if m:
        return m.group(1)
    m = re.search(r"looked after by bot '([^']+)'", detail or "")
    if m:
        return m.group(1)
    return None


def _reason_from_detail(detail: str):
    """The reason in the last (...) group of an activity detail."""
    groups = re.findall(r"\(([^)]+)\)", detail or "")
    return groups[-1] if groups else None


def _activity_rows(db: sqlite3.Connection, user_id: int) -> list:
    rows = db.execute(
        "SELECT kind, detail, created_at FROM activity "
        "WHERE user_id = ? AND kind IN ('silent_buy', 'silent_sell', "
        "'silent_stop_loss', 'paper_buy', 'paper_sell') "
        "ORDER BY created_at ASC, id ASC",
        (user_id,)).fetchall()
    out = []
    for r in rows:
        when = parse_time(r["created_at"])
        out.append({"kind": r["kind"], "detail": r["detail"] or "",
                    "when": when})
    return out


def _match_activity(rows: list, coin: str, when, kinds) -> dict | None:
    """The closest activity row naming `coin`, within the match
    window of `when`, or None. `coin` is matched as a whole word in
    the detail text."""
    if when is None:
        return None
    best = None
    best_gap = None
    pattern = re.compile(r"\b" + re.escape(coin) + r"\b")
    for row in rows:
        if row["kind"] not in kinds:
            continue
        if row["when"] is None or not pattern.search(row["detail"]):
            continue
        gap = abs((row["when"] - when).total_seconds())
        if gap > _MATCH_WINDOW_SECONDS:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best


def _avg(values: list):
    """Mean of the non-None values, or None when there are none."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _side_summary(pairs: list) -> dict:
    return {
        "count": len(pairs),
        "avg_pnl_usd": _avg([p["pnl_usd"] for p in pairs]),
        "avg_m5_pct": _avg([p["m5_pct"] for p in pairs]),
        "avg_volume_5m_usd": _avg([p["volume_5m_usd"] for p in pairs]),
        "avg_hold_seconds": _avg([p["hold_seconds"] for p in pairs]),
        "regime": {
            "slow": sum(1 for p in pairs if p["regime"] == "slow"),
            "trending": sum(1 for p in pairs
                            if p["regime"] == "trending"),
            "unavailable": sum(
                1 for p in pairs if p["regime"] not in ("slow", "trending")),
        },
        "with_signals": sum(1 for p in pairs if p["m5_pct"] is not None),
    }


def analyze_paper_trades(db: sqlite3.Connection,
                         user_id: int) -> dict:
    """Pair a user's paper trades into completed buys + open buys,
    with a losing-vs-winning summary. Returns {"pairs", "open",
    "summary"}. Pairs are newest first."""
    bot_names = {r["id"]: r["name"] for r in db.execute(
        "SELECT id, name FROM bots WHERE user_id = ?", (user_id,))}
    legs = db.execute(
        "SELECT * FROM paper_trades WHERE user_id = ? "
        "ORDER BY created_at ASC, id ASC",
        (user_id,)).fetchall()
    activity = _activity_rows(db, user_id)

    queues: dict[str, list] = {}
    pairs = []
    open_buys = []
    for leg in legs:
        coin = leg["coin_id"]
        when = parse_time(leg["created_at"])
        if leg["side"] == "buy":
            queues.setdefault(coin, []).append({
                "buy_id": leg["id"],
                "coin": coin,
                "bot_id": leg["bot_id"] if "bot_id" in leg.keys() else None,
                "buy_at": leg["created_at"],
                "buy_when": when,
                "buy_price": float(leg["price"]),
                "amount": float(leg["amount"]),
                "remaining": float(leg["amount"]),
                "m5_pct": (float(leg["buy_m5_pct"])
                           if leg["buy_m5_pct"] is not None else None),
                "volume_5m_usd": (
                    float(leg["buy_volume_5m_usd"])
                    if leg["buy_volume_5m_usd"] is not None else None),
                "buys_5m": leg["buy_buys_5m"],
                "sells_5m": leg["buy_sells_5m"],
                "regime": leg["buy_regime"],
            })
            continue
        # Sell leg: consume the oldest open buys of this coin (FIFO).
        sell_amount = float(leg["amount"])
        sell_price = float(leg["price"])
        exit_reason = normalize_exit_reason(leg["exit_reason"])
        if exit_reason == "unavailable":
            match = _match_activity(
                activity, coin, when,
                ("silent_sell", "silent_stop_loss"))
            if match is not None:
                if match["kind"] == "silent_stop_loss":
                    exit_reason = "stop loss"
                else:
                    exit_reason = normalize_exit_reason(
                        _reason_from_detail(match["detail"]))
        for buy in list(queues.get(coin, [])):
            if sell_amount <= 0:
                break
            take = min(buy["remaining"], sell_amount)
            if take <= 0:
                continue
            buy["remaining"] -= take
            sell_amount -= take
            hold = None
            if when is not None and buy["buy_when"] is not None:
                hold = (when - buy["buy_when"]).total_seconds()
            bot = None
            if buy["bot_id"] is not None:
                bot = bot_names.get(buy["bot_id"])
            if bot is None:
                match = _match_activity(
                    activity, coin, buy["buy_when"],
                    ("silent_buy", "paper_buy"))
                if match is not None:
                    bot = _bot_from_detail(match["detail"])
                    if bot is None and match["kind"] == "paper_buy":
                        bot = "manual"
            pairs.append({
                "buy_id": buy["buy_id"],
                "coin": coin,
                "bot": bot if bot else "unavailable",
                "buy_at": buy["buy_at"],
                "buy_price": buy["buy_price"],
                "sell_at": leg["created_at"],
                "sell_price": sell_price,
                "amount": take,
                "pnl_usd": (sell_price - buy["buy_price"]) * take,
                "hold_seconds": hold,
                "exit_reason": exit_reason,
                "m5_pct": buy["m5_pct"],
                "volume_5m_usd": buy["volume_5m_usd"],
                "buys_5m": buy["buys_5m"],
                "sells_5m": buy["sells_5m"],
                "regime": buy["regime"],
            })
        queues[coin] = [b for b in queues.get(coin, [])
                        if b["remaining"] > 1e-12]
    for coin, buys in queues.items():
        for buy in buys:
            if buy["remaining"] <= 1e-12:
                continue
            bot = None
            if buy["bot_id"] is not None:
                bot = bot_names.get(buy["bot_id"])
            if bot is None:
                match = _match_activity(
                    activity, coin, buy["buy_when"],
                    ("silent_buy", "paper_buy"))
                if match is not None:
                    bot = _bot_from_detail(match["detail"])
                    if bot is None and match["kind"] == "paper_buy":
                        bot = "manual"
            open_buys.append({
                "coin": coin,
                "bot": bot if bot else "unavailable",
                "buy_at": buy["buy_at"],
                "buy_price": buy["buy_price"],
                "amount": buy["remaining"],
                "m5_pct": buy["m5_pct"],
                "volume_5m_usd": buy["volume_5m_usd"],
                "buys_5m": buy["buys_5m"],
                "sells_5m": buy["sells_5m"],
                "regime": buy["regime"],
            })
    pairs.sort(key=lambda p: (p["sell_at"], p["buy_id"]), reverse=True)
    losing = [p for p in pairs if p["pnl_usd"] < -1e-9]
    winning = [p for p in pairs if p["pnl_usd"] > 1e-9]
    even = [p for p in pairs if -1e-9 <= p["pnl_usd"] <= 1e-9]
    return {
        "pairs": pairs,
        "open": open_buys,
        "summary": {
            "losing": _side_summary(losing),
            "winning": _side_summary(winning),
            "even": len(even),
            "total": len(pairs),
        },
    }
