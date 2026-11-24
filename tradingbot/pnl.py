"""P&L snapshots and the P&L dashboard's data.

The chart is built ONLY from recorded snapshot points — history is
never invented. A snapshot is recorded on every silent-loop
evaluation (one per user, per iteration) and after every manual
paper trade. A range with no recorded points says "no data yet".

Valuation is honest about what it knows: a holding with no current
price is valued at its average cost and flagged price-unavailable,
never guessed. The on-chain SOL balance is recorded when the RPC
answers; otherwise it is NULL and shown as "unavailable".
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

RANGES = {
    "all": ("All-time", None),
    "1y": ("1 year", 365 * 86400),
    "1mo": ("1 month", 30 * 86400),
    "1w": ("1 week", 7 * 86400),
    "1d": ("1 day", 86400),
    "1h": ("1 hour", 3600),
}

# Short labels for the homepage chart's duration row (1h / 1d / 1w /
# 1m / 1y / All) — same ranges as RANGES, short enough for one row
# on a phone.
HOME_RANGE_LABELS = {
    "1h": "1h", "1d": "1d", "1w": "1w", "1mo": "1m", "1y": "1y",
    "all": "All",
}

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_FMT)


def _parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _FMT).replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


# --- glitch-snapshot guard -------------------------------------------------
# A single wild snapshot (the $709,599.60 All-time-chart point) used to
# poison both the home headline and the chart: change_24h picked
# whatever snapshot was nearest the 24-hour mark, and chart_svg scaled
# to the min/max of all points, so one bad row flattened everything.
# The live feed already rejects absurd single ticks before the silent
# loop records a snapshot, but a bad price can still arrive through
# the manual-trade path (no feed check there) or a first tick with no
# reference yet — so the read path must not trust any single point.
# The rule walks the series with a running reference (the last
# accepted value, mirroring the feed's bad-tick rule): a point within
# GLITCH_FACTOR of the reference is accepted; a point that jumps away
# is a genuine step only when the next two points confirm the new
# level (three agreeing points, like the feed's 3-tick re-base) —
# otherwise it is a spike-and-revert and is dropped. The first point
# has no history, so it is judged against the level that follows it:
# when the next two points agree with each other at a level more than
# GLITCH_FACTOR away, the first point is the odd one out and is
# dropped (this also covers a glitch landing just inside a ranged
# window, where it becomes the window's first point). A jump with
# fewer than two points after it cannot be judged yet and is kept.
# A wrong level sustained for 3+ snapshots is treated as genuine,
# matching the feed. Dropping a point never invents history — the
# page shows the remaining recorded points, or "no data yet". Known
# cost: a genuine step of more than GLITCH_FACTOR immediately after
# the first point drops that first point; the baseline then falls
# back to the next point (or "—"), which is the safe error — a
# missing figure, never a poisoned one.
GLITCH_FACTOR = 10.0


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _absurd_flags(values: list, factor: float = GLITCH_FACTOR) -> set:
    """Indices of single-point spikes in a time-ordered value list.

    A value is flagged when it jumps more than `factor` away from the
    running reference and the next two points do not confirm the new
    level, when the first value disagrees by more than `factor` with
    the next two points (which agree with each other), or when the
    value itself is not a finite number. A jump with fewer than two
    points after it cannot be judged yet and is never flagged.
    """
    flags = set()
    for i, value in enumerate(values):
        if not _is_finite_number(value):
            flags.add(i)
    pts = [(i, float(v)) for i, v in enumerate(values)
           if _is_finite_number(v)]
    if not pts:
        return flags
    ref = pts[0][1]
    start = 1
    if len(pts) >= 3:
        _i1, nxt = pts[1]
        _i2, nxt2 = pts[2]
        if (nxt <= factor * nxt2 and nxt >= nxt2 / factor
                and (ref > factor * nxt or ref < nxt / factor)):
            # First point is the odd one out against the level that
            # follows it (also covers a glitch just inside a ranged
            # window, where it is the window's first point).
            flags.add(pts[0][0])
            ref = nxt
            start = 2
    k = start
    while k < len(pts):
        idx, v = pts[k]
        if v > factor * ref or v < ref / factor:
            followers = [w for _, w in pts[k + 1:k + 3]]
            if len(followers) < 2:
                # Tail of the series: cannot judge yet, keep.
                k += 1
                continue
            if all(w <= factor * v and w >= v / factor
                   for w in followers):
                # Genuine step: the new level persists, re-base.
                ref = v
                k += 1
                continue
            flags.add(idx)
            k += 1
            continue
        ref = v
        k += 1
    return flags


def sane_points(points: list, factor: float = GLITCH_FACTOR) -> list:
    """Recorded points with single-point spikes removed, order kept.

    `points` are {"created_at", "total_value_usd"} dicts (the
    chart_points / chart_points_real shape). The raw rows are
    untouched — this is a read-time filter only, so displaying the
    history never rewrites it.
    """
    values = []
    for p in points or []:
        try:
            values.append(float(p["total_value_usd"]))
        except (KeyError, TypeError, ValueError):
            values.append(None)
    flags = _absurd_flags(values, factor)
    return [p for i, p in enumerate(points or []) if i not in flags]


def purge_absurd_snapshots(db: sqlite3.Connection, user_id: int | None = None,
                           factor: float = GLITCH_FACTOR,
                           dry_run: bool = True) -> list:
    """One-time cleanup for glitch rows already recorded.

    Returns the flagged rows as [{"id", "user_id", "created_at",
    "total_value_usd"}]. With dry_run=True (the default) nothing is
    deleted. With dry_run=False the flagged rows are deleted and the
    change committed. Rows are judged per user, oldest first, by the
    same running-reference rule as sane_points — a genuine step change
    is never flagged, so the cleanup cannot delete real history.
    """
    if user_id is None:
        users = [r["user_id"] for r in db.execute(
            "SELECT DISTINCT user_id FROM portfolio_snapshots"
            ).fetchall()]
    else:
        users = [user_id]
    flagged = []
    for uid in users:
        rows = db.execute(
            "SELECT id, created_at, total_value_usd "
            "FROM portfolio_snapshots WHERE user_id = ? "
            "ORDER BY created_at ASC, id ASC", (uid,)).fetchall()
        values = [r["total_value_usd"] for r in rows]
        for i in sorted(_absurd_flags(values, factor)):
            flagged.append({"id": rows[i]["id"], "user_id": uid,
                            "created_at": rows[i]["created_at"],
                            "total_value_usd": float(
                                rows[i]["total_value_usd"])})
    if not dry_run and flagged:
        db.executemany(
            "DELETE FROM portfolio_snapshots WHERE id = ?",
            [(f["id"],) for f in flagged])
        db.commit()
    return flagged


def record_snapshot(db: sqlite3.Connection, user_id: int, cash_usd: float,
                    holdings_value_usd: float, real_sol: float | None,
                    now: str | None = None) -> None:
    """Insert one portfolio snapshot row. Never invents a value. A
    non-finite total (inf/nan from a bad price) is refused — both
    callers already tolerate a failed snapshot, and a poisoned row
    must never reach the table."""
    cash = float(cash_usd)
    holdings = float(holdings_value_usd)
    if not (math.isfinite(cash) and math.isfinite(holdings)
            and math.isfinite(cash + holdings)):
        raise ValueError("refusing non-finite snapshot total")
    db.execute(
        "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
        "holdings_value_usd, total_value_usd, real_sol, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, cash, holdings, cash + holdings, real_sol,
         now or _now()))
    db.commit()


def current_holdings(db: sqlite3.Connection, user_id: int,
                     prices: dict) -> list:
    """Per-coin holdings with live value and unrealised P&L.

    `prices` maps coin_id/mint -> current USD price. A holding with
    no current price is valued at its average cost, with price None
    and pnl None, so the page can say "price unavailable" instead of
    guessing.
    """
    rows = db.execute(
        "SELECT coin_id, amount, avg_price FROM paper_holdings "
        "WHERE user_id = ? ORDER BY coin_id", (user_id,)).fetchall()
    out = []
    for r in rows:
        raw_price = prices.get(r["coin_id"])
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None
        # A price of 0 or less is not a price: the holding is valued
        # at its average cost with pnl None, so a dead quote can
        # never value it at $0 or invent a loss.
        if price is not None and price <= 0:
            price = None
        amount = float(r["amount"])
        avg = float(r["avg_price"])
        if price is not None:
            value = amount * price
            pnl = (price - avg) * amount
        else:
            value = amount * avg
            pnl = None
        out.append({"coin_id": r["coin_id"], "amount": amount,
                    "avg_price": avg, "price": price, "value": value,
                    "pnl": pnl})
    return out


def portfolio_totals(db: sqlite3.Connection, user_id: int,
                     prices: dict) -> dict:
    """Cash + holdings value + per-coin rows for one user."""
    row = db.execute(
        "SELECT cash_usd FROM paper_portfolio WHERE user_id = ?",
        (user_id,)).fetchone()
    cash = float(row["cash_usd"]) if row else 0.0
    holdings = current_holdings(db, user_id, prices)
    holdings_value = sum(h["value"] for h in holdings)
    return {"cash_usd": cash, "holdings": holdings,
            "holdings_value_usd": holdings_value,
            "total_value_usd": cash + holdings_value}


def chart_points(db: sqlite3.Connection, user_id: int,
                 range_key: str) -> tuple[list, bool]:
    """Snapshot points for a range, oldest first.

    Returns (points, has_data). Points are ONLY rows actually
    recorded — an empty range returns ([], False) and the page says
    "no data yet".
    """
    _label, seconds = RANGES.get(range_key, RANGES["all"])
    if seconds is None:
        rows = db.execute(
            "SELECT created_at, total_value_usd FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY created_at ASC, id ASC",
            (user_id,)).fetchall()
    else:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=seconds)).strftime(_FMT)
        rows = db.execute(
            "SELECT created_at, total_value_usd FROM portfolio_snapshots "
            "WHERE user_id = ? AND created_at >= ? "
            "ORDER BY created_at ASC, id ASC",
            (user_id, cutoff)).fetchall()
    points = [{"created_at": r["created_at"],
               "total_value_usd": float(r["total_value_usd"])}
              for r in rows]
    return points, len(points) > 0


def chart_points_real(db: sqlite3.Connection, user_id: int,
                      range_key: str) -> tuple[list, bool]:
    """Snapshot points for the REAL mode chart: the recorded on-chain
    wallet SOL balance over time. Points are ONLY rows actually
    recorded with a real_sol value — an empty range returns ([], False)
    and the page says "no data yet". The values are SOL, not USD."""
    _label, seconds = RANGES.get(range_key, RANGES["all"])
    if seconds is None:
        rows = db.execute(
            "SELECT created_at, real_sol FROM portfolio_snapshots "
            "WHERE user_id = ? AND real_sol IS NOT NULL "
            "ORDER BY created_at ASC, id ASC",
            (user_id,)).fetchall()
    else:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=seconds)).strftime(_FMT)
        rows = db.execute(
            "SELECT created_at, real_sol FROM portfolio_snapshots "
            "WHERE user_id = ? AND real_sol IS NOT NULL "
            "AND created_at >= ? ORDER BY created_at ASC, id ASC",
            (user_id, cutoff)).fetchall()
    points = [{"created_at": r["created_at"],
               "total_value_usd": float(r["real_sol"])}
              for r in rows]
    return points, len(points) > 0


def change_24h(db: sqlite3.Connection, user_id: int,
               current_total: float | None,
               real: bool = False) -> dict | None:
    """Change over the last 24 hours from recorded snapshots only.

    Compares the live `current_total` against the snapshot NEAREST
    to 24 hours ago — not the oldest snapshot in the window, which
    may be an account-start or thin-data moment and would explode
    the percentage (the +25,548% home-screen figure came from a
    ~$2.48 early-window baseline). Single-point glitch snapshots
    are filtered out first (sane_points): one wild row nearest the
    mark used to poison the headline the same way (a $709,599.60
    row would read as −99.96%). Returns {"delta": ..., "pct": ...}
    or None when there is no usable baseline: no snapshot within 3
    hours of the 24-hour mark, a zero/negative baseline, or no
    current total. Never invents a figure — a missing baseline means
    the page shows "—".
    """
    if current_total is None:
        return None
    if real:
        points, _has = chart_points_real(db, user_id, "1d")
    else:
        points, _has = chart_points(db, user_id, "1d")
    points = sane_points(points)
    if not points:
        return None
    target = (datetime.now(timezone.utc)
              - timedelta(hours=24)).timestamp()
    best = None
    best_gap = None
    for p in points:
        ts = _parse_ts(p["created_at"])
        if ts is None:
            continue
        gap = abs(ts - target)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = p
    # The baseline must actually be ~24h old: a nearest point more
    # than 3 hours from the mark means the account has no real
    # 24-hour history yet, and any % would be measured over the
    # wrong window.
    if best is None or best_gap is None or best_gap > 3 * 3600:
        return None
    baseline = float(best["total_value_usd"])
    if baseline <= 0:
        return None
    delta = float(current_total) - baseline
    return {"delta": delta, "pct": delta / baseline * 100.0}


def chart_svg(points: list, width: int = 640, height: int = 200,
              unit: str = "$") -> str:
    """A plain SVG line chart from recorded points. Empty string when
    there are fewer than two points (the page says so instead).
    `unit` is the axis label prefix ("$" for USD, "SOL " for the
    real-mode wallet chart)."""
    if len(points) < 2:
        return ""
    times = [_parse_ts(p["created_at"]) for p in points]
    values = [p["total_value_usd"] for p in points]
    if any(t is None for t in times):
        return ""
    t0, t1 = min(times), max(times)
    v0, v1 = min(values), max(values)
    span_t = (t1 - t0) or 1.0
    span_v = (v1 - v0) or 1.0
    pad_x, pad_y = 8, 12
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y

    def _xy(t, v):
        x = pad_x + (t - t0) / span_t * plot_w
        y = pad_y + (1 - (v - v0) / span_v) * plot_h
        return f"{x:.1f},{y:.1f}"

    line = " ".join(_xy(t, v) for t, v in zip(times, values))
    first = _xy(times[0], values[0])
    last = _xy(times[-1], values[-1])
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="P&amp;L chart" '
        f'style="background:#000;border:1px solid #ffb300;border-radius:8px">'
        f'<polyline points="{line}" fill="none" stroke="#ffd700" '
        f'stroke-width="2"/>'
        f'<circle cx="{first.split(",")[0]}" cy="{first.split(",")[1]}" '
        f'r="3" fill="#ffd700"/>'
        f'<circle cx="{last.split(",")[0]}" cy="{last.split(",")[1]}" '
        f'r="3" fill="#ffd700"/>'
        f'<text x="{pad_x}" y="{pad_y - 2}" fill="#aaa" font-size="11">'
        f'{unit}{v1:,.2f}</text>'
        f'<text x="{pad_x}" y="{height - 2}" fill="#aaa" font-size="11">'
        f'{unit}{v0:,.2f}</text>'
        f'</svg>')


def _main(argv=None) -> int:
    """One-time cleanup CLI (dry-run by default — deletes nothing):

    python -m tradingbot.pnl --db PATH [--user ID] [--apply]

    Lists the glitch snapshot rows the neighbour rule flags; --apply
    deletes them. Run from the repo root; the DB path is the live
    database file.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="List (or --apply: delete) glitch snapshot rows.")
    parser.add_argument("--db", required=True,
                        help="path to the SQLite database file")
    parser.add_argument("--user", type=int, default=None,
                        help="only this user id (default: all users)")
    parser.add_argument("--apply", action="store_true",
                        help="delete the flagged rows (default: dry-run)")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        flagged = purge_absurd_snapshots(
            conn, user_id=args.user, dry_run=not args.apply)
    finally:
        conn.close()
    for f in flagged:
        print(f"user {f['user_id']} id {f['id']} "
              f"{f['created_at']} total {f['total_value_usd']:,.2f}")
    print(f"{len(flagged)} glitch row(s) "
          f"{'deleted' if args.apply else 'flagged (dry-run, nothing deleted)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
