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

Market regime (profit ladders): the app has NO social-media feed, so
"trending + community active" is judged from the DexScreener feed
alone. TRENDING needs all three at once — a 5-minute volume surge
(volume.m5 at least 2x the average 5-minute pace of the last hour),
more buys than sells in the last 5 minutes, and a positive 1-hour
price change. Anything else is SLOW. Missing data means SLOW
(fail-closed: the slow ladder takes profits early rather than
holding on a guess). A coin whose volume is ALL in the last
5 minutes (h1 volume zero) counts as a surge — the crowd just
arrived; that biases very fresh tokens toward TRENDING, which is
the safe direction for a sniper (runners hold).

Profit ladders (scanner only, % of the original position):
  * SLOW: sell 25% at +3%, 25% at +10%, 25% at +25%, the rest at
    +50% — secure a profit early and often.
  * TRENDING (bullrunners hold): sell 10% at +26%, 10% at +50%,
    10% at +100%, 10% at +500%, 10% at +1000%, the rest at
    +10000% or on the 7% trailing stop, whichever comes first.
Rungs are tracked by cumulative fraction sold (derived from the
position's amount vs its original amount), so the regime can flip
between scans without losing track: a rung fires only when its
profit target is met AND its cumulative fraction is above what is
already sold. The stop loss (7% below the buy price) and the rug
sell still fire regardless of cost — a bad coin is cut at a small
loss, which is what lets the big winners pay for the losers even
when fewer than half the trades win.
"""

from __future__ import annotations

from datetime import datetime, timezone

MARK_WINDOW_SECONDS = 180
MARK_KEEP_SECONDS = 600

# --- Scanner profit ladders (James's spec) ---------------------------
# Rungs are % of the ORIGINAL position, executed in order as profit
# rises. SLOW takes profits early; TRENDING holds the runners for
# the big gains. The last rung of each ladder sells the rest (100%
# cumulative), so a position always fully exits through the ladder
# once its top target is reached.
SLOW_LADDER = [
    {"pct_of_position": 25, "profit_pct": 3},
    {"pct_of_position": 25, "profit_pct": 10},
    {"pct_of_position": 25, "profit_pct": 25},
    {"pct_of_position": 25, "profit_pct": 50},
]
TRENDING_LADDER = [
    {"pct_of_position": 10, "profit_pct": 26},
    {"pct_of_position": 10, "profit_pct": 50},
    {"pct_of_position": 10, "profit_pct": 100},
    {"pct_of_position": 10, "profit_pct": 500},
    {"pct_of_position": 10, "profit_pct": 1000},
    {"pct_of_position": 50, "profit_pct": 10000},
]

# Regime detection defaults: 5-minute volume at least 2x the average
# 5-minute pace of the last hour, buys outnumbering sells in the
# last 5 minutes, and a positive 1-hour change — all three, or SLOW.
REGIME_DEFAULT = {"volume_surge_x": 2.0, "require_buy_dominance": True,
                  "require_h1_gain": True}

# Buy filter defaults: a buy needs a REAL signal, not just a gain
# number. A coin qualifies only when all three hold:
#   * at least $1,000 of volume in the last 5 minutes — a real
#     crowd is moving money, not a $50 blip that still "surges"
#     2x against a dead hour;
#   * more buys than sells in the last 5 minutes — buyers are
#     actually in control, not sellers exiting into the pump;
#   * the signal itself is at least 5% — the same 5% the scanner
#     already treats as a real move (its buy trigger). The Trenches
#     buy threshold is 3.4%, so the filter raises the effective bar
#     to 5% unless the bot's rule keys tune it down.
# These are rule keys, not constants: each Scanner / Trenches bot
# carries its own buy_filter, tunable through the assistant
# ("set the buy filter for bot X ..."), and the losing-buy analysis
# page is where the numbers get tuned to what the trades show.
BUY_FILTER_DEFAULT = {"min_volume_5m_usd": 1000.0,
                      "require_buy_dominance": True,
                      "min_signal_pct": 5.0,
                      "min_market_cap_usd": 0.0}
BUY_FILTER_KEYS = ("min_volume_5m_usd", "require_buy_dominance",
                   "min_signal_pct", "min_market_cap_usd")


def _gte(a: float, b: float) -> bool:
    """a >= b with a 1e-9 relative tolerance — prices are floats."""
    return a >= b or abs(a - b) <= 1e-9 * max(1.0, abs(b))


def passes_buy_filter(entry, filter_cfg=None,
                      signal_pct=None,
                      devnet_watchlist_active=False) -> tuple[bool, str]:
    """Does a feed entry pass the low-performer buy filter?

    `signal_pct` is the gain that triggered the buy (3-minute gain
    for the scanner, 5-minute gain for Trenches). Returns
    (True, "") or (False, reason). Fail-closed: any missing or
    unusable input rejects the buy — a coin with no volume data,
    no buy/sell counts, or no signal is never bought on a guess.

    DEVNET-TEST-ONLY carve-out (remove before mainnet): an entry
    flagged `devnet_watchlist` — a devnet mint priced from its
    bonding curve, which has no DexScreener data at all — takes a
    different path, and ONLY when the caller confirms the
    watchlist is active (`devnet_watchlist_active`, which the
    silent loop sets from NETWORK == "devnet"). With the flag
    off, a flagged entry falls through to the normal filter and
    is rejected for its missing volume data. With it on, the two
    DexScreener-only checks are replaced: the 5-minute-volume
    check becomes a bonding-curve liquidity check (real SOL must
    be sitting in the curve), and the buy/sell-count check is
    skipped (no on-chain equivalent exists from one curve
    snapshot). The signal-strength and market-cap checks still
    apply in full — the signal is still the real gate.
    """
    cfg = filter_cfg or BUY_FILTER_DEFAULT
    for key in cfg:
        if key not in BUY_FILTER_KEYS:
            return False, f"Unknown buy filter setting '{key}'"
    if entry.get("devnet_watchlist"):
        if devnet_watchlist_active:
            return _passes_devnet_watchlist_filter(entry, cfg, signal_pct)
        # Watchlist flag with the watchlist inactive (any network
        # other than devnet): no carve-out — the normal filter
        # below rejects it for its missing DexScreener data.
    try:
        min_vol = float(cfg.get("min_volume_5m_usd",
                                BUY_FILTER_DEFAULT["min_volume_5m_usd"]))
        min_signal = float(cfg.get("min_signal_pct",
                                   BUY_FILTER_DEFAULT["min_signal_pct"]))
    except (TypeError, ValueError):
        return False, "buy filter is misconfigured"
    vol_m5 = entry.get("volume_m5_usd")
    if vol_m5 is None:
        return False, "no 5-minute volume data"
    try:
        vol_m5 = float(vol_m5)
    except (TypeError, ValueError):
        return False, "no 5-minute volume data"
    if not _gte(vol_m5, min_vol):
        return False, (f"5-minute volume ${vol_m5:,.0f} is below the "
                       f"${min_vol:,.0f} minimum")
    if cfg.get("require_buy_dominance", True):
        buys = entry.get("txns_m5_buys")
        sells = entry.get("txns_m5_sells")
        if buys is None or sells is None:
            return False, "no 5-minute buy/sell counts"
        try:
            if int(buys) <= int(sells):
                return False, (f"buys ({int(buys)}) do not outnumber "
                               f"sells ({int(sells)}) in 5 minutes")
        except (TypeError, ValueError):
            return False, "no 5-minute buy/sell counts"
    if signal_pct is None:
        # No explicit signal given (direct callers): fall back to
        # the entry's 5-minute gain. The loop always passes the gain
        # that triggered the buy (3-minute for the scanner, 5-minute
        # for Trenches), so this only matters for standalone use.
        signal_pct = entry.get("change_5m_pct")
    if signal_pct is None:
        return False, "no signal strength"
    try:
        signal_pct = float(signal_pct)
    except (TypeError, ValueError):
        return False, "no signal strength"
    if not _gte(signal_pct, min_signal):
        return False, (f"signal {signal_pct:.1f}% is below the "
                       f"{min_signal:.1f}% minimum")
    try:
        min_mcap = float(cfg.get("min_market_cap_usd",
                                 BUY_FILTER_DEFAULT["min_market_cap_usd"]))
    except (TypeError, ValueError):
        return False, "buy filter is misconfigured"
    if min_mcap > 0:
        # A market-cap minimum is set: the coin must report a market
        # cap at or above it. No market-cap data means no buy —
        # fail-closed, never a guess at the coin's size.
        mcap = entry.get("market_cap_usd")
        if mcap is None:
            return False, "no market cap data"
        try:
            mcap = float(mcap)
        except (TypeError, ValueError):
            return False, "no market cap data"
        if not _gte(mcap, min_mcap):
            return False, (f"market cap ${mcap:,.0f} is below the "
                           f"${min_mcap:,.0f} minimum")
    return True, ""


def _passes_devnet_watchlist_filter(entry, cfg, signal_pct):
    """Buy-filter carve-out for a devnet watchlist mint (see
    passes_buy_filter). DEVNET-TEST-ONLY — remove before mainnet.

    The entry has no DexScreener data by construction, so the
    volume and buy/sell-count checks cannot apply. In their
    place: the bonding curve must hold real SOL (curve_real_sol
    > 0 — the curve-derived equivalent of "real money is in this
    market"); a missing or zero figure rejects the buy. The
    signal-strength check (same minimum as the normal filter)
    and the market-cap check (against the curve-derived market
    cap the watchlist entry carries) still apply in full.
    """
    try:
        min_signal = float(cfg.get("min_signal_pct",
                                   BUY_FILTER_DEFAULT["min_signal_pct"]))
    except (TypeError, ValueError):
        return False, "buy filter is misconfigured"
    curve_sol = entry.get("curve_real_sol")
    if curve_sol is None:
        return False, "no bonding-curve liquidity data"
    try:
        curve_sol = float(curve_sol)
    except (TypeError, ValueError):
        return False, "no bonding-curve liquidity data"
    if not curve_sol > 0:
        return False, "bonding curve holds no real SOL"
    if signal_pct is None:
        # No explicit signal given: fall back to the entry's
        # 5-minute gain. The loop always passes the gain that
        # triggered the buy, so this only matters for standalone
        # use — and a watchlist entry has no feed 5-minute gain,
        # so it rejects, never a guess.
        signal_pct = entry.get("change_5m_pct")
    if signal_pct is None:
        return False, "no signal strength"
    try:
        signal_pct = float(signal_pct)
    except (TypeError, ValueError):
        return False, "no signal strength"
    if not _gte(signal_pct, min_signal):
        return False, (f"signal {signal_pct:.1f}% is below the "
                       f"{min_signal:.1f}% minimum")
    try:
        min_mcap = float(cfg.get("min_market_cap_usd",
                                 BUY_FILTER_DEFAULT["min_market_cap_usd"]))
    except (TypeError, ValueError):
        return False, "buy filter is misconfigured"
    if min_mcap > 0:
        mcap = entry.get("market_cap_usd")
        if mcap is None:
            return False, "no market cap data"
        try:
            mcap = float(mcap)
        except (TypeError, ValueError):
            return False, "no market cap data"
        if not _gte(mcap, min_mcap):
            return False, (f"market cap ${mcap:,.0f} is below the "
                           f"${min_mcap:,.0f} minimum")
    return True, ""


def detect_regime(entry, regime=None) -> str:
    """'trending' or 'slow' for a feed entry.

    TRENDING needs all three (when required by `regime`): the
    5-minute volume at least volume_surge_x times the average
    5-minute volume of the last hour (volume.h1 / 12), more buys
    than sells in the last 5 minutes, and a positive 1-hour price
    change. Any missing or unusable input means SLOW — never guess.
    A coin with volume only in the last 5 minutes (h1 volume zero)
    counts as a surge: the crowd just arrived.
    """
    cfg = regime or REGIME_DEFAULT
    try:
        surge_x = float(cfg.get("volume_surge_x", 2.0))
    except (TypeError, ValueError):
        return "slow"
    vol_m5 = entry.get("volume_m5_usd")
    vol_h1 = entry.get("volume_h1_usd")
    if vol_m5 is None or vol_h1 is None:
        return "slow"
    try:
        vol_m5 = float(vol_m5)
        vol_h1 = float(vol_h1)
    except (TypeError, ValueError):
        return "slow"
    if vol_m5 <= 0:
        return "slow"
    if vol_h1 <= 0:
        surge = True
    else:
        surge = vol_m5 >= surge_x * (vol_h1 / 12.0)
    if not surge:
        return "slow"
    if cfg.get("require_buy_dominance", True):
        buys = entry.get("txns_m5_buys")
        sells = entry.get("txns_m5_sells")
        if buys is None or sells is None:
            return "slow"
        try:
            if int(buys) <= int(sells):
                return "slow"
        except (TypeError, ValueError):
            return "slow"
    if cfg.get("require_h1_gain", True):
        change_1h = entry.get("change_1h_pct")
        if change_1h is None:
            return "slow"
        try:
            if float(change_1h) <= 0:
                return "slow"
        except (TypeError, ValueError):
            return "slow"
    return "trending"


def next_ladder_rung(ladder, sold_fraction: float, profit_pct: float):
    """Next ladder rung to fire, or None.

    `ladder` is a list of {pct_of_position, profit_pct} rungs.
    `sold_fraction` is the fraction of the original position already
    sold (0.0-1.0); `profit_pct` is the current profit vs the buy
    price. A rung fires when its profit target is met AND its
    cumulative fraction exceeds what is already sold — so the regime
    can flip between scans and the ladder still converges: already
    sold slices are never re-sold, and the first unmet rung fires.
    Returns (sell_fraction_of_original, profit_target) or None.
    """
    try:
        sold = float(sold_fraction)
        profit = float(profit_pct)
    except (TypeError, ValueError):
        return None
    cumulative = 0.0
    for rung in ladder or []:
        try:
            pct = float(rung["pct_of_position"])
            target = float(rung["profit_pct"])
        except (TypeError, ValueError, KeyError, AttributeError):
            return None
        cumulative += pct / 100.0
        if _gte(profit, target) and cumulative > sold + 1e-9:
            # Rounded: 0.1 + 0.1 + 0.1 sums to 0.30000000000000004
            # in floats, and the sell amount must be exact.
            return (round(cumulative - sold, 9), target)
    return None


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


def gain_window_pct(db, bot_id: int, mint: str, price_now: float,
                    now_ts: float, window_seconds: int):
    """% gain over the last `window_seconds`, from recorded marks.

    Uses the latest mark at least `window_seconds` old. None when
    the coin has not been tracked that long yet — no gain to judge,
    so no buy. This is how a coin with no feed-provided change
    (a devnet watchlist mint, priced from its bonding curve) gets
    its buy signal: the loop's own marks, never a guess.
    """
    cutoff = _fmt(now_ts - window_seconds)
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


def gain_3m_pct(db, bot_id: int, mint: str, price_now: float,
                now_ts: float):
    """% gain over the last 3 minutes, from recorded marks.

    Uses the latest mark at least 3 minutes old. None when the coin
    has not been tracked that long yet — no gain to judge, so no buy.
    """
    return gain_window_pct(db, bot_id, mint, price_now, now_ts,
                           MARK_WINDOW_SECONDS)


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


def top10_share(client, mint: str):
    """Top-10 share of total supply for a mint: (top10_total,
    total_supply, share_pct). (None, None, None) when the RPC cannot
    serve the mint or the supply — the caller skips the check for
    that round, never guesses."""
    total = top10_total(client, mint)
    if total is None:
        return None, None, None
    try:
        supply = int(client.get_token_supply(mint))
    except Exception:
        return None, None, None
    if supply <= 0:
        return None, None, None
    return total, supply, total / supply * 100.0


def check_top10_concentration(db, bot_id: int, mint: str,
                              top10_total_units: int, total_supply: int,
                              concentration_pct: float,
                              slow_climb_sell_pct: float,
                              now_str: str):
    """Top-10 concentration watch: (action, reason).

    `action` is None, "sell_all" or "sell_half". Exact definitions:
      * share = top-10 combined holdings / total token supply * 100.
      * SELL ALL when share reaches concentration_pct (default 21%):
        a small group holding a fifth of the supply can dump on
        everyone, so the position exits immediately.
      * SLOW CLIMB: share RISING over 3 CONSECUTIVE checks — each
        check higher than the previous one — while share is at least
        10% (a real concentration, not noise) and still below
        concentration_pct: SELL HALF (slow_climb_sell_pct of the
        position, default 50%) as an early rug warning, to lock
        profit while there is still a market to sell into. It fires
        once per climb: the streak resets to 0 after it fires, so it
        cannot sell the same position down repeatedly on one climb.
      * Any check where share does NOT rise resets the streak to 0.
      * total_supply None or <= 0 means the share cannot be
        computed — (None, None), never a guess.
    """
    if total_supply is None or int(total_supply) <= 0:
        return None, None
    share = int(top10_total_units) / int(total_supply) * 100.0
    row = db.execute(
        "SELECT top10_total, share_pct, rise_streak FROM scanner_top10 "
        "WHERE bot_id = ? AND mint = ?", (bot_id, mint)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO scanner_top10 (bot_id, mint, top10_total, "
            "recorded_at, share_pct, rise_streak, last_checked_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (bot_id, mint, int(top10_total_units), now_str, share,
             now_str))
        db.commit()
        return None, None
    prev_share = row["share_pct"]
    streak = int(row["rise_streak"] or 0)
    if prev_share is not None and share > float(prev_share):
        streak += 1
    else:
        streak = 0
    db.execute(
        "UPDATE scanner_top10 SET top10_total = ?, recorded_at = ?, "
        "share_pct = ?, rise_streak = ?, last_checked_at = ? "
        "WHERE bot_id = ? AND mint = ?",
        (int(top10_total_units), now_str, share, streak, now_str,
         bot_id, mint))
    db.commit()
    if share >= float(concentration_pct):
        return "sell_all", (f"top-10 wallets hold {share:.1f}% of "
                            f"supply (at or above "
                            f"{float(concentration_pct):.0f}%) — "
                            f"selling everything")
    if float(slow_climb_sell_pct) > 0 and share >= 10.0 and streak >= 3:
        # Fire once per climb: reset the streak so the same slow
        # climb cannot sell the position down check after check.
        db.execute(
            "UPDATE scanner_top10 SET rise_streak = 0 "
            "WHERE bot_id = ? AND mint = ?", (bot_id, mint))
        db.commit()
        return "sell_half", (f"top-10 share climbing slowly: "
                             f"{share:.1f}% of supply and rising for "
                             f"3 checks — selling "
                             f"{float(slow_climb_sell_pct):.0f}% to lock "
                             f"profit")
    return None, None


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
