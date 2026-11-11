"""Candlestick (OHLC) charts: building candles from price points and
rendering them as SVG.

One module serves both charts in the app:
  * the coin detail chart (Page 5) — candles from CoinGecko's OHLC
    endpoint for known coins, or from the live feed's recorded ticks
    for pump.fun mints;
  * the homepage portfolio chart (Page 3) — candles from the recorded
    portfolio snapshots only.

A candle is a real bucket of real points: open is the first price in
the bucket, close the last, high/low the extremes. A bucket with a
single point is a valid candle (open = high = low = close) — it is a
real recorded value, not an invented one. Empty input builds no
candles at all: the page says the candles are still building (or
there is no data yet) instead of drawing a made-up chart. A chart
needs at least two candles to draw; one candle is a dot, not a
chart.

The line ("regular") view is drawn from the SAME candles — a
polyline through the closes — so the two views can never disagree
about the price.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# --- candle intervals -----------------------------------------------------
# Homepage portfolio chart: snapshots are recorded about once a
# minute, so the interval is chosen per range to give a readable
# number of candles (roughly 12-60). "all" uses 1-day candles unless
# the span is over a year, when it uses 1-week candles.
HOME_INTERVALS = {
    "1h": 300, "1d": 3600, "1w": 21600, "1mo": 86400, "1y": 604800,
    "all": 86400,
}
HOME_INTERVALS_LONG_SPAN = 604800
# Coin detail page, pump.fun mints: candles are built only from the
# live feed's recorded ticks, so short ranges get short candles (a
# 1-minute candle fills from the 5-second mint poll).
MINT_INTERVALS = {
    "24h": 60, "1d": 60, "1w": 900, "1m": 3600, "1y": 86400, "all": 86400,
}

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _FMT).replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def normalize_points(points: list) -> list:
    """Price points to {"ts": ms_since_epoch, "price": float},
    oldest first. Accepts CoinGecko-style {"ts", "price"} and
    snapshot-style {"created_at", "total_value_usd"} rows. A point
    with no usable time or price is dropped, never guessed; the
    result is sorted oldest first."""
    out = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        ts = p.get("ts")
        if ts is None:
            ts = _parse_ts(p.get("created_at"))
            if ts is not None:
                ts = int(ts * 1000)
        try:
            price = float(p.get("price", p.get("total_value_usd")))
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if ts is None or not math.isfinite(price):
            continue
        out.append({"ts": ts, "price": price})
    out.sort(key=lambda item: item["ts"])
    return out


def interval_for_home(range_key: str, span_seconds: float | None = None
                      ) -> int:
    """Candle interval (seconds) for the homepage portfolio chart."""
    if range_key == "all" and span_seconds is not None \
            and span_seconds > 365 * 86400:
        return HOME_INTERVALS_LONG_SPAN
    return HOME_INTERVALS.get(range_key, 86400)


def interval_for_mint(range_key: str) -> int:
    """Candle interval (seconds) for a mint's live-tick candles."""
    return MINT_INTERVALS.get(range_key, 60)


def build_candles(points: list, interval_seconds: int) -> list:
    """OHLC candles from price points: [{"ts", "open", "high",
    "low", "close"}], oldest first. `points` is
    {"ts": ms, "price": float} (or snapshot rows — see
    normalize_points). `interval_seconds` buckets the points by
    their timestamp; a point exactly on a bucket edge starts the new
    bucket. A bucket with one point is a real candle
    (open = high = low = close). Empty input returns []."""
    interval_seconds = int(interval_seconds)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    ticks = normalize_points(points)
    if not ticks:
        return []
    interval_ms = interval_seconds * 1000
    candles = []
    current = None
    for tick in ticks:
        bucket = (tick["ts"] // interval_ms) * interval_ms
        if current is None or current["ts"] != bucket:
            current = {"ts": bucket, "open": tick["price"],
                       "high": tick["price"], "low": tick["price"],
                       "close": tick["price"]}
            candles.append(current)
        else:
            current["high"] = max(current["high"], tick["price"])
            current["low"] = min(current["low"], tick["price"])
            current["close"] = tick["price"]
    return candles


def interval_label(interval_seconds: int) -> str:
    """Plain-words label for a candle interval ("1 minute",
    "30 minutes", "4 hours", "4 days")."""
    seconds = int(interval_seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour" + ("s" if hours != 1 else "")
    days = seconds // 86400
    return f"{days} day" + ("s" if days != 1 else "")


# --- SVG rendering --------------------------------------------------------
# Same frame as pnl.chart_svg (640x200, black, gold border) so the
# candle and line views sit in the same box. Up candles are the
# app's green, down candles its red — data colours only.

def _frame(candles: list, width: int, height: int, unit: str):
    """(t0, t1, v0, v1, pad_x, pad_y, plot_w, plot_h, slot_w) for the
    shared candle/line geometry, or None when there is nothing to
    draw (fewer than two candles)."""
    if len(candles) < 2:
        return None
    t0 = candles[0]["ts"]
    t1 = candles[-1]["ts"]
    values = []
    for c in candles:
        values.extend((c["high"], c["low"]))
    v0, v1 = min(values), max(values)
    span_t = (t1 - t0) or 1
    span_v = (v1 - v0) or (abs(v1) * 0.001 or 1.0)
    pad_x, pad_y = 8, 12
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y
    slot_w = plot_w / len(candles)
    return (t0, t1, v0, v1, pad_x, pad_y, plot_w, plot_h, slot_w,
            span_t, span_v)


def candles_svg(candles: list, width: int = 640, height: int = 200,
                unit: str = "$") -> str:
    """A candlestick SVG from OHLC candles, oldest first. Empty
    string when there are fewer than two candles — the page says so
    instead of drawing a one-candle chart."""
    frame = _frame(candles, width, height, unit)
    if frame is None:
        return ""
    (t0, _t1, v0, v1, pad_x, pad_y, _plot_w, plot_h, slot_w,
     span_t, span_v) = frame

    def _y(v):
        return pad_y + (1 - (v - v0) / span_v) * plot_h

    body_w = max(2.0, slot_w * 0.6)
    parts = []
    for i, c in enumerate(candles):
        cx = pad_x + (i + 0.5) * slot_w
        up = c["close"] >= c["open"]
        colour = "#4ade80" if up else "#f87171"
        y_high = _y(c["high"])
        y_low = _y(c["low"])
        y_open = _y(c["open"])
        y_close = _y(c["close"])
        top = min(y_open, y_close)
        bottom = max(y_open, y_close)
        if bottom - top < 1.0:
            bottom = top + 1.0  # a flat candle still draws a body
        parts.append(
            f'<line x1="{cx:.1f}" y1="{y_high:.1f}" x2="{cx:.1f}" '
            f'y2="{y_low:.1f}" stroke="{colour}" stroke-width="1.5"/>'
            f'<rect x="{cx - body_w / 2:.1f}" y="{top:.1f}" '
            f'width="{body_w:.1f}" height="{bottom - top:.1f}" '
            f'fill="{colour}"/>')
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="Candlestick chart" '
        f'style="background:#000;border:1px solid #ffb300;'
        f'border-radius:8px">'
        + "".join(parts) +
        f'<text x="{pad_x}" y="{pad_y - 2}" fill="#aaa" font-size="11">'
        f'{unit}{v1:,.2f}</text>'
        f'<text x="{pad_x}" y="{height - 2}" fill="#aaa" font-size="11">'
        f'{unit}{v0:,.2f}</text>'
        f'</svg>')


def line_svg(candles: list, width: int = 640, height: int = 200,
             unit: str = "$") -> str:
    """The "regular" line chart from the SAME candles — a polyline
    through the closes. Empty string when there are fewer than two
    candles, exactly like the candle view."""
    frame = _frame(candles, width, height, unit)
    if frame is None:
        return ""
    (t0, _t1, v0, v1, pad_x, pad_y, _plot_w, plot_h, slot_w,
     span_t, span_v) = frame

    def _xy(i, v):
        x = pad_x + (i + 0.5) * slot_w
        y = pad_y + (1 - (v - v0) / span_v) * plot_h
        return f"{x:.1f},{y:.1f}"

    closes = [c["close"] for c in candles]
    line = " ".join(_xy(i, v) for i, v in enumerate(closes))
    first = _xy(0, closes[0])
    last = _xy(len(closes) - 1, closes[-1])
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="Price chart" '
        f'style="background:#000;border:1px solid #ffb300;'
        f'border-radius:8px">'
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
