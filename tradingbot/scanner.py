"""∆scanner∆ sniper feed: 3-minute gains from recorded price marks,
and the top-10 wallets watch via Solana RPC.

DexScreener only provides 5-minute price changes, so a "5% gain in
3 minutes" signal cannot come from the feed: the loop records a price
mark for every candidate mint on every scan (scanner_marks) and
computes the 3-minute gain from the mark closest to 3 minutes ago. A
coin with no mark at least 3 minutes old has no 3-minute gain yet —
it is not bought, never guessed.

Top-10 wallets: Solana RPC getTokenLargestAccounts for the mint
(public, no key). The baseline is the top-10 combined holdings the
first time a held coin is watched, tracked upward to the peak since
the position opened; a drop of 10% from that peak means the big
holders are dumping — sell everything. If the RPC cannot serve a
mint, that coin's watch is skipped for that scan and logged — never
faked.
"""

from __future__ import annotations

from datetime import datetime, timezone

MARK_WINDOW_SECONDS = 180
MARK_KEEP_SECONDS = 600


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def record_marks(db, bot_id: int, entries, now_str: str) -> None:
    """One price mark per mint, then prune marks older than 10 minutes."""
    for entry in entries or []:
        mint = entry.get("mint")
        price = entry.get("price_usd")
        if not mint or price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        db.execute(
            "INSERT INTO scanner_marks (bot_id, mint, price_usd, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, price, now_str))
    cutoff = _fmt(datetime.now(timezone.utc).timestamp()
                  - MARK_KEEP_SECONDS)
    db.execute("DELETE FROM scanner_marks WHERE bot_id = ? AND "
               "recorded_at < ?", (bot_id, cutoff))
    db.commit()


def gain_3m_pct(db, bot_id: int, mint: str, price_now: float,
                now_ts: float):
    """% gain over the last 3 minutes, from recorded marks.

    Uses the latest mark at least 3 minutes old. None when the coin
    has not been tracked that long yet — no gain to judge, so no buy.
    """
    cutoff = _fmt(now_ts - MARK_WINDOW_SECONDS)
    row = db.execute(
        "SELECT price_usd FROM scanner_marks WHERE bot_id = ? AND "
        "mint = ? AND recorded_at <= ? ORDER BY recorded_at DESC "
        "LIMIT 1",
        (bot_id, mint, cutoff)).fetchone()
    if not row or not row["price_usd"]:
        return None
    old = float(row["price_usd"])
    if old <= 0:
        return None
    return (float(price_now) - old) / old * 100.0


def top10_total(client, mint: str):
    """Combined holdings of the mint's 10 largest accounts (base
    units). None when the RPC cannot serve the mint — the caller
    skips the watch for that scan, never guesses."""
    try:
        accounts = client.get_token_largest_accounts(mint)
    except Exception:
        return None
    if not isinstance(accounts, list) or not accounts:
        return None
    total = 0
    for acct in accounts[:10]:
        try:
            total += int(acct.get("amount") or 0)
        except (TypeError, ValueError):
            return None
    return total if total > 0 else None


def check_top10_rug(db, bot_id: int, mint: str, current_total: int,
                    drop_pct: float, now_str: str):
    """Top-10 watch: (fired, reason).

    The baseline is the top-10 total the first time the position is
    watched, tracked upward to the peak since the position opened.
    A drop of drop_pct or more from that peak means the big holders
    are dumping — sell everything. Otherwise the baseline is updated
    to the new peak and nothing fires.
    """
    row = db.execute(
        "SELECT top10_total FROM scanner_top10 WHERE bot_id = ? AND "
        "mint = ?", (bot_id, mint)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO scanner_top10 (bot_id, mint, top10_total, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (bot_id, mint, int(current_total), now_str))
        db.commit()
        return False, None
    baseline = int(row["top10_total"])
    if baseline <= 0 or int(current_total) >= baseline:
        db.execute("UPDATE scanner_top10 SET top10_total = ?, "
                   "recorded_at = ? WHERE bot_id = ? AND mint = ?",
                   (int(current_total), now_str, bot_id, mint))
        db.commit()
        return False, None
    drop = (baseline - int(current_total)) / baseline * 100.0
    if drop >= float(drop_pct):
        return True, (f"top-10 wallets dumped: holdings down "
                      f"{drop:.1f}%")
    return False, None


def clear_watches(db, bot_id: int, mint: str) -> None:
    """Forget a closed position's marks and top-10 baseline."""
    db.execute("DELETE FROM scanner_marks WHERE bot_id = ? AND mint = ?",
               (bot_id, mint))
    db.execute("DELETE FROM scanner_top10 WHERE bot_id = ? AND mint = ?",
               (bot_id, mint))
    db.commit()
