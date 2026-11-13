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


def record_snapshot(db: sqlite3.Connection, user_id: int, cash_usd: float,
                    holdings_value_usd: float, real_sol: float | None,
                    now: str | None = None) -> None:
    """Insert one portfolio snapshot row. Never invents a value."""
    db.execute(
        "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
        "holdings_value_usd, total_value_usd, real_sol, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, float(cash_usd), float(holdings_value_usd),
         float(cash_usd) + float(holdings_value_usd), real_sol,
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
    ~$2.48 early-window baseline). Returns {"delta": ..., "pct": ...}
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
