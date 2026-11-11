"""Live price feed: one service that subscribes to upstream live sources,
validates every tick, and serves the app's own WebSocket.

Upstream sources (all free, no key):
- Kraken public WebSocket (wss://ws.kraken.com) — ticker for BTC, ETH,
  SOL in USD. The majors feed. Verified working from this server.
- DexScreener REST (api.dexscreener.com) — polled every 5s for the
  pump.fun mints the app is watching (held coins + coins a browser
  subscribed to). DexScreener has no public price stream, so polling
  at its fastest sensible rate is the only option there; 5s polling
  of at most a few dozen mints is far inside its 300 req/min limit.
- Jupiter lite-api v3 (lite-api.jup.ag, no key) — SOL cross-check every
  60s. If Kraken's SOL goes stale it becomes the SOL source; if both
  are fresh and disagree by >5% the disagreement is logged, never
  silently resolved.
- CoinGecko REST stays as the fallback behind get_prices() (60s cache,
  rate-limited — that is why it cannot be the live feed).

Left OUT, deliberately:
- Binance: its REST and WebSocket both return HTTP 451 from US IPs
  (geo-blocked), and Railway runs in US East. Documented, not retried.
- PumpPortal's WebSocket (wss://pumpportal.fun/api/data) streams
  pump.fun new-token and trade events and was verified working, but
  turning trade events into per-mint prices needs per-mint
  subscriptions and reconnect handling that would not be finished and
  tested in this pass. It is the candidate real-time mint source for a
  later step; the DexScreener poll covers mints until then.

Tick validation (the $709,599.60 lesson — one bad print once inflated
a portfolio by ~$706,000 for a minute): a tick is accepted only if its
price is finite and positive and within MAX_SINGLE_TICK_JUMP (50%) of
the last good price for that coin. A rejected tick is logged and the
last good price stands — a rejected tick never reaches a page, a P&L
total, a snapshot, or the silent loop. A genuine step change is not
lost forever: if three consecutive ticks agree at the new level (within
5% of each other), the reference re-bases to it — a real move persists,
a bad print reverts on the next tick. A coin with no last good price
yet accepts its first tick (nothing to compare against), and when the
feed has no reference at all the loop's sanitizer accepts prices too —
fail-open without a reference is safer than halting all trading
whenever the feed hiccups; the moment a reference exists, the bound
applies. A tick older than STALE_AFTER_SECONDS (30s) is stale: shown
with its age, never as fresh.

The feed runs as daemon threads inside the web process (started from
run.py, never from create_app, so tests never open sockets). Kill it
with LIVE_FEED_ENABLED=0; the app keeps serving from the existing
REST feeds.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import urllib.request

log = logging.getLogger("tradingbot.livefeed")

# --- tick validation ------------------------------------------------------
# A single tick jumping further than this versus the last good price is
# rejected as a bad print. 50% is far above any honest one-tick move on
# a 5-second poll or a Kraken ticker, and far below the 100,000x spike
# that once inflated a portfolio.
MAX_SINGLE_TICK_JUMP = 0.5
# Consecutive agreeing ticks at a new level re-base the reference, so a
# genuine step change is picked up within ~15s (3 DexScreener polls)
# instead of being rejected forever.
REBASE_CONFIRMATIONS = 3
REBASE_TOLERANCE = 0.05
# A tick older than this is stale — shown with its age, never as fresh.
STALE_AFTER_SECONDS = 30.0

# --- upstream sources -----------------------------------------------------
KRAKEN_WS_URL = "wss://ws.kraken.com"
KRAKEN_PAIRS = {"XBT/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL"}
DEXSCREENER_POLL_SECONDS = 5.0
HELD_MINTS_REFRESH_SECONDS = 60.0
JUPITER_CHECK_SECONDS = 60.0
JUPITER_SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3?ids={}"
JUPITER_DISAGREE_PCT = 0.05


def parse_kraken_ticker(raw: str):
    """(coin, last_price, change_24h_pct) from one Kraken ticker message,
    or None when the message is not a ticker for a pair we watch.

    Kraken ticker frames look like:
    [channel_id, {"c": ["last", "lot vol"], "o": "open 24h", ...},
     "ticker", "XBT/USD"]. 24h change is last vs the 24h open.
    """
    try:
        msg = json.loads(raw)
    except Exception:
        return None
    if not isinstance(msg, list) or len(msg) < 4 or msg[2] != "ticker":
        return None
    coin = KRAKEN_PAIRS.get(msg[3])
    if coin is None:
        return None
    data = msg[1] or {}
    try:
        last = float((data.get("c") or [None])[0])
        open24 = float(data.get("o"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(last) or last <= 0 or not math.isfinite(open24) \
            or open24 <= 0:
        return None
    return coin, last, (last / open24 - 1.0) * 100.0


class LiveFeed:
    """Thread-safe store of the last good tick per coin, fed by the
    upstream threads. The store is the single source of validated
    prices: browsers read it over /ws/live and /api/live, and the
    silent loop sanitizes its prices against it."""

    def __init__(self, config=None, db_path: str | None = None):
        self._config = config
        self._db_path = db_path or (
            getattr(config, "database_path", None) if config else None)
        self._lock = threading.Lock()
        # coin_id -> {"coin_id", "price", "source", "as_of" (epoch),
        #             "change_24h"?, "change_1h"?}
        self._ticks: dict = {}
        # coin_id -> {"price", "count"}: consecutive rejected ticks at
        # a new level, waiting to re-base the reference.
        self._pending: dict = {}
        # mint -> subscriber count (browsers watching it).
        self._mint_subs: dict = {}
        self._stop = threading.Event()
        self._started = False
        self._threads: list = []

    # --- validation -------------------------------------------------------
    def would_accept(self, coin_id: str, price) -> bool:
        """Would this price be accepted against the current reference?
        Read-only: it never stores the price or moves the reference.
        True when there is no reference yet (nothing to compare
        against) or the jump is within the bound."""
        try:
            price = float(price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or price <= 0:
            return False
        with self._lock:
            last = self._ticks.get(coin_id)
        if last is None:
            return True
        return abs(price / last["price"] - 1.0) <= MAX_SINGLE_TICK_JUMP

    def ingest(self, coin_id: str, price, source: str,
               change_24h=None, change_1h=None, now: float | None = None
               ) -> bool:
        """Validate one tick and store it when good. Returns True when
        accepted. A rejected tick is logged and the last good price
        stands. Changes are kept from the last good tick when the new
        tick does not carry them."""
        now = time.time() if now is None else now
        try:
            price = float(price)
        except (TypeError, ValueError):
            log.warning("[live-feed] rejected non-numeric tick %s: %r",
                        coin_id, price)
            return False
        if not math.isfinite(price) or price <= 0:
            log.warning("[live-feed] rejected bad tick %s: %r",
                        coin_id, price)
            return False
        with self._lock:
            last = self._ticks.get(coin_id)
            if last is not None:
                prev = last["price"]
                if abs(price / prev - 1.0) > MAX_SINGLE_TICK_JUMP:
                    pend = self._pending.get(coin_id)
                    if pend is not None and abs(
                            price / pend["price"] - 1.0
                            ) <= REBASE_TOLERANCE:
                        pend["count"] += 1
                    else:
                        pend = {"price": price, "count": 1}
                        self._pending[coin_id] = pend
                    if pend["count"] >= REBASE_CONFIRMATIONS:
                        log.warning(
                            "[live-feed] %s re-based: %.6g -> %.6g "
                            "after %d agreeing ticks",
                            coin_id, prev, price, pend["count"])
                        self._pending.pop(coin_id, None)
                    else:
                        log.warning(
                            "[live-feed] rejected absurd tick %s: %.6g "
                            "vs last good %.6g (%.0f%% jump); last good "
                            "stands", coin_id, price, prev,
                            abs(price / prev - 1.0) * 100.0)
                        return False
                else:
                    self._pending.pop(coin_id, None)
            entry = {"coin_id": coin_id, "price": price,
                     "source": source, "as_of": now}
            for key, value in (("change_24h", change_24h),
                               ("change_1h", change_1h)):
                if value is not None:
                    try:
                        entry[key] = float(value)
                    except (TypeError, ValueError):
                        pass
                elif last is not None and key in last:
                    entry[key] = last[key]
            self._ticks[coin_id] = entry
            return True

    # --- reads ------------------------------------------------------------
    def _tick_dict(self, tick: dict, now: float) -> dict:
        out = {"coin_id": tick["coin_id"], "price": tick["price"],
               "source": tick["source"],
               "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(tick["as_of"])),
               "stale": (now - tick["as_of"]) > STALE_AFTER_SECONDS}
        for key in ("change_24h", "change_1h"):
            if key in tick:
                out[key] = tick[key]
        return out

    def snapshot(self, coin_ids=None, now: float | None = None) -> dict:
        """Current validated ticks, {coin_id: tick dict}. coin_ids
        limits the result; None returns everything stored. A coin
        with no tick is left out — never guessed."""
        now = time.time() if now is None else now
        with self._lock:
            ids = (list(coin_ids) if coin_ids is not None
                   else list(self._ticks))
            ticks = {cid: self._ticks[cid] for cid in ids
                     if cid in self._ticks}
        return {cid: self._tick_dict(t, now) for cid, t in ticks.items()}

    def is_stale(self, coin_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            tick = self._ticks.get(coin_id)
        if tick is None:
            return True
        return (now - tick["as_of"]) > STALE_AFTER_SECONDS

    # --- mint subscriptions (what the DexScreener poller watches) --------
    def subscribe_mint(self, mint: str) -> None:
        with self._lock:
            self._mint_subs[mint] = self._mint_subs.get(mint, 0) + 1

    def unsubscribe_mint(self, mint: str) -> None:
        with self._lock:
            count = self._mint_subs.get(mint, 0) - 1
            if count <= 0:
                self._mint_subs.pop(mint, None)
            else:
                self._mint_subs[mint] = count

    def watched_mints(self) -> set:
        with self._lock:
            return set(self._mint_subs)

    # --- sanitizers (silent loop + page renders) --------------------------
    def sanitize_trenches(self, data: dict | None) -> dict | None:
        """Copy of a trenches scan with absurd mint prices dropped.
        The input is never mutated. A dropped mint reads as 'no price'
        downstream — the loop skips it and the page values it at cost —
        never as a trade signal or a balance."""
        if not data:
            return data
        prices = data.get("prices") or {}
        kept = {}
        dropped = []
        for mint, entry in prices.items():
            price = (entry or {}).get("price_usd")
            if price is None or self.would_accept(mint, price):
                kept[mint] = entry
            else:
                dropped.append(mint)
        if dropped:
            log.warning("[live-feed] dropped absurd trenches price(s) "
                        "from the loop/render: %s", ", ".join(dropped))
        out = dict(data)
        out["prices"] = kept
        candidates = data.get("candidates") or []
        out["candidates"] = [c for c in candidates
                             if (c or {}).get("mint") in kept]
        return out

    def sanitize_prices_data(self, data: dict | None) -> dict | None:
        """Copy of a get_prices() dict with absurd symbol prices
        dropped. Same contract as sanitize_trenches."""
        if not data:
            return data
        prices = data.get("prices") or {}
        kept = {}
        dropped = []
        for symbol, entry in prices.items():
            price = (entry or {}).get("price")
            if price is None or self.would_accept(symbol, price):
                kept[symbol] = entry
            else:
                dropped.append(symbol)
        if dropped:
            log.warning("[live-feed] dropped absurd price(s) from the "
                        "loop/render: %s", ", ".join(dropped))
        out = dict(data)
        out["prices"] = kept
        return out

    # --- upstream threads -------------------------------------------------
    def start(self) -> None:
        """Start the Kraken WebSocket, DexScreener poller and Jupiter
        cross-check as daemon threads. Idempotent. A thread that dies
        logs and reconnects on its own schedule; a dead upstream never
        stops the app — the last good ticks stand, marked stale."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        for name, target in (
                ("livefeed-kraken", self._kraken_loop),
                ("livefeed-dexscreener", self._dexscreener_loop),
                ("livefeed-jupiter", self._jupiter_loop)):
            thread = threading.Thread(target=target, daemon=True,
                                      name=name)
            thread.start()
            self._threads.append(thread)
        log.info("[live-feed] started (kraken ws + dexscreener poll + "
                 "jupiter check)")

    def stop(self) -> None:
        self._stop.set()
        self._started = False

    def _on_kraken_open(self, ws) -> None:
        ws.send(json.dumps({
            "event": "subscribe",
            "pair": list(KRAKEN_PAIRS),
            "subscription": {"name": "ticker"}}))
        log.info("[live-feed] kraken subscribed: %s",
                 ", ".join(KRAKEN_PAIRS))

    def _on_kraken_message(self, ws, raw: str) -> None:
        parsed = parse_kraken_ticker(raw)
        if parsed is None:
            return
        coin, last, change_24h = parsed
        self.ingest(coin, last, "kraken", change_24h=change_24h)

    def _kraken_loop(self) -> None:
        import websocket

        backoff = 5.0
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    KRAKEN_WS_URL,
                    on_message=self._on_kraken_message,
                    on_error=lambda w, e: log.warning(
                        "[live-feed] kraken error: %s", e),
                    on_close=lambda w, c, m: log.warning(
                        "[live-feed] kraken closed: %s %s", c, m),
                    on_open=self._on_kraken_open)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # never kill the feed thread
                log.warning("[live-feed] kraken loop error: %s", exc)
            if self._stop.is_set():
                break
            log.warning("[live-feed] kraken reconnecting in %.0fs",
                        backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _held_mints_from_db(self) -> set:
        """Mints the app already holds (bot positions + paper
        holdings), so the poller keeps pricing them even when no
        browser is watching. Symbols are not mints and are skipped.
        A DB failure returns an empty set — the poller keeps the
        browser subscriptions."""
        if not self._db_path:
            return set()
        try:
            from .solana import is_valid_address
            conn = sqlite3.connect(self._db_path, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT coin_id FROM bot_positions"
                    ).fetchall()
                rows += conn.execute(
                    "SELECT DISTINCT coin_id FROM paper_holdings"
                    ).fetchall()
            finally:
                conn.close()
            return {r[0] for r in rows
                    if r and r[0] and is_valid_address(r[0])}
        except Exception as exc:
            log.warning("[live-feed] held-mints db read failed: %s", exc)
            return set()

    def _poll_dexscreener(self, mints) -> None:
        from .trenches import (
            _CHUNK, TOKENS_URL, _best_pair, _default_get, _entry)
        for i in range(0, len(mints), _CHUNK):
            chunk = mints[i:i + _CHUNK]
            try:
                data = _default_get(TOKENS_URL.format(",".join(chunk)))
            except Exception as exc:
                log.warning("[live-feed] dexscreener fetch failed: %s",
                            exc)
                return
            by_mint: dict = {}
            for pair in data.get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = _best_pair(pairs)
                if best is None:
                    continue
                entry = _entry(mint, best)
                self.ingest(mint, entry["price_usd"], "dexscreener",
                            change_24h=entry.get("change_24h_pct"),
                            change_1h=entry.get("change_1h_pct"))

    def _dexscreener_loop(self) -> None:
        held_refresh_at = 0.0
        held: set = set()
        while not self._stop.is_set():
            try:
                now = time.time()
                if now >= held_refresh_at:
                    held = self._held_mints_from_db()
                    held_refresh_at = now + HELD_MINTS_REFRESH_SECONDS
                mints = sorted(self.watched_mints() | held)
                if mints:
                    self._poll_dexscreener(mints)
            except Exception as exc:  # never kill the feed thread
                log.warning("[live-feed] dexscreener poll error: %s", exc)
            self._stop.wait(DEXSCREENER_POLL_SECONDS)

    def _jupiter_check(self) -> None:
        try:
            req = urllib.request.Request(
                JUPITER_PRICE_URL.format(JUPITER_SOL_MINT),
                headers={"User-Agent": "trading-bot-app"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("[live-feed] jupiter fetch failed: %s", exc)
            return
        entry = (raw.get("data") or {}).get(JUPITER_SOL_MINT) or {}
        try:
            price = float(entry.get("usdPrice"))
        except (TypeError, ValueError):
            return
        if not math.isfinite(price) or price <= 0:
            return
        sol = self.snapshot(["SOL"]).get("SOL")
        if sol is None or sol["stale"]:
            # Kraken has no fresh SOL — Jupiter becomes the source.
            change = entry.get("priceChange24h")
            self.ingest("SOL", price, "jupiter", change_24h=change)
            return
        if abs(price / sol["price"] - 1.0) > JUPITER_DISAGREE_PCT:
            log.warning("[live-feed] SOL cross-check disagrees: kraken "
                        "%.4f vs jupiter %.4f", sol["price"], price)

    def _jupiter_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._jupiter_check()
            except Exception as exc:  # never kill the feed thread
                log.warning("[live-feed] jupiter check error: %s", exc)
            self._stop.wait(JUPITER_CHECK_SECONDS)


def start_feed(app) -> LiveFeed:
    """Start the app's live feed threads. Called from run.py only —
    create_app never starts threads, so tests never open sockets."""
    feed = app.config["LIVE_FEED"]
    feed.start()
    return feed
