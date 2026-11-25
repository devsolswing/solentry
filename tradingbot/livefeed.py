"""Live price feed: one service that subscribes to upstream live sources,
validates every tick, and serves the app's own WebSocket.

Pump.fun-only. ONE price pipeline (single-price source, 2026-11-25):
every coin price in the app comes from the Helius stream — on-chain
bonding-curve reads for bonding coins (accountSubscribe on the curve
account, ~1 tick per slot, ~400ms) and Raydium pool-vault reads for
graduated coins — ingested here through `ingest` and served from this
store. There is exactly one other upstream: Jupiter lite-api v3 for
the SOL price every 60s (the unit of account only — never a listed
or buyable coin). That is the whole price system: Helius for coins,
Jupiter for SOL, this store as the single validated source.

GONE (2026-11-25, "Do we need two streams?" — no): the DexScreener
5-second REST poll was the second stream and it is deleted — nothing
reads DexScreener for prices any more. The devnet curve poll is
deleted too: the Helius stream covers devnet curves the same way
it covers mainnet ones. DexScreener remains only the discovery
index (which tokens are new) and activity metadata (volume, txns,
mcap) in the trenches scan — never a price. A coin the stream
cannot tick (no curve, no known pool) has NO price here: the
pages say "no data" and the bot skips it, never a stale or
second-source number.

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
applies. A tick older than STALE_AFTER_SECONDS (30s) is stale: it is
NO price at all — pages show "no data" and the bot skips — never
shown as live, never decided on. Accuracy first: a wrong number is
worse than no number.

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
from collections import deque

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
# ONE price pipeline (2026-11-25): the Helius stream ingests coin
# ticks (bonding curves + Raydium vaults); Jupiter ingests the SOL
# price. No DexScreener poll, no devnet curve poll — both deleted.
JUPITER_CHECK_SECONDS = 60.0
JUPITER_SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3?ids={}"


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
        # Optional tick listener: set by the silent loop so a tick
        # that crosses a stop can trigger the sell path at once
        # (tick-driven exits, tuning build 2026-11-15). The
        # listener is called AFTER an accepted tick is stored, OUTSIDE
        # the store lock, and never raises into the feed — a listener
        # failure is logged and the tick still stands. Unset by
        # default: the feed works exactly as before without it.
        self._tick_listener = None
        # coin_id -> deque of (epoch_seconds, price): every ACCEPTED
        # tick, oldest first, capped at the last ~10,000 per coin
        # (about an hour at stream cadence for a busy coin — enough
        # for the 1-hour change to come from the stream's own
        # history). Rejected ticks never enter it. This is the
        # recorded history the candlestick chart builds mint candles
        # from — it is in-memory only, so a fresh deploy starts with
        # no history and the page says the candles are still building.
        self._history: dict = {}
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
            if last is not None and now < last["as_of"]:
                # Out-of-order delivery: a tick stamped older than
                # the stored one never overwrites it. A streamed
                # price replayed after a fresher REST tick, or a
                # slow poll arriving after a fresher stream tick,
                # is dropped instead of rewinding the price.
                log.warning(
                    "[live-feed] rejected out-of-order tick %s: "
                    "stamped %.3f, stored tick is %.3f; last good "
                    "stands", coin_id, now, last["as_of"])
                return False
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
            history = self._history.get(coin_id)
            if history is None:
                history = deque(maxlen=10000)
                self._history[coin_id] = history
            history.append((now, price))
            accepted = True
        listener = self._tick_listener
        if accepted and listener is not None:
            # Outside the lock, never raising into the feed: the
            # listener (the silent loop's fast-exit path) gets the
            # tick the moment it lands.
            try:
                listener(coin_id, price, now)
            except Exception as exc:  # never kill the feed thread
                log.warning("[live-feed] tick listener failed for %s: %s",
                            coin_id, exc)
        return accepted

    def set_tick_listener(self, callback) -> None:
        """Register (or clear, with None) the tick listener: called
        with (coin_id, price, now_epoch) for every ACCEPTED tick.
        Rejected ticks never reach it. The callback must be fast
        and must never raise — it runs on the feed's poller thread,
        so it should hand the tick to a worker, not sell inline."""
        self._tick_listener = callback

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

    def history(self, coin_id: str) -> list:
        """Recorded (epoch_seconds, price) ticks for a coin, oldest
        first. Only accepted ticks are recorded; a coin nobody has
        watched yet returns []."""
        with self._lock:
            ticks = self._history.get(coin_id)
            return list(ticks) if ticks else []

    def is_stale(self, coin_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            tick = self._ticks.get(coin_id)
        if tick is None:
            return True
        return (now - tick["as_of"]) > STALE_AFTER_SECONDS

    def tick_age_seconds(self, coin_id: str,
                         now: float | None = None) -> float | None:
        """Age in seconds of the stored tick for a coin, or None
        when there is no tick. The Helius stream uses it for its
        SOL reference: SOL refreshes from Jupiter every 60s, so
        the feed's 30s staleness cut would drop half the stream's
        updates — the stream allows a SOL tick up to its own
        (longer) age bound instead."""
        now = time.time() if now is None else now
        with self._lock:
            tick = self._ticks.get(coin_id)
        if tick is None:
            return None
        return now - tick["as_of"]

    def live_tick(self, coin_id: str,
                  now: float | None = None) -> dict | None:
        """The stored tick for a coin when it is FRESH, else None.
        A stale tick is no price at all — pages show "no data" and
        the bot skips — never shown as live, never decided on.
        This is the one verified-price read: every price shown or
        decided on in the app goes through here (or `live_price`
        below)."""
        now = time.time() if now is None else now
        with self._lock:
            tick = self._ticks.get(coin_id)
        if tick is None:
            return None
        if (now - tick["as_of"]) > STALE_AFTER_SECONDS:
            return None
        return dict(tick)

    def live_price(self, coin_id: str,
                   now: float | None = None) -> float | None:
        """The fresh price for a coin, or None when there is no
        fresh tick. Never a stale number, never a guess."""
        tick = self.live_tick(coin_id, now=now)
        if tick is None:
            return None
        try:
            price = float(tick["price"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        return price

    def change_over(self, coin_id: str, window_seconds: float,
                    now: float | None = None) -> float | None:
        """% change between the latest tick and the tick closest to
        (now - window) in the recorded history, or None when the
        history does not reach that far back. Computed from the
        stream's own ticks — never a second source's change figure.
        A coin the stream has watched for less than the window has
        no change figure yet ("—" on the page), never a guessed one."""
        now = time.time() if now is None else now
        with self._lock:
            points = self._history.get(coin_id)
            points = list(points) if points else []
        if len(points) < 2:
            return None
        latest_ts, latest_price = points[-1]
        if (now - latest_ts) > STALE_AFTER_SECONDS:
            return None
        target = now - float(window_seconds)
        old_price = None
        for ts, price in points:
            if ts <= target:
                old_price = price
            else:
                break
        if old_price is None or old_price <= 0 or latest_price <= 0:
            return None
        return (latest_price / old_price - 1.0) * 100.0

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
        """Start the Jupiter SOL check as a daemon thread.
        Idempotent. Coin ticks come only from the Helius stream
        (started separately from run.py) — there is no poller any
        more: the DexScreener 5s poll and the devnet curve poll
        were deleted with the single-price source (2026-11-25).
        A thread that dies logs and reconnects on its own
        schedule; a dead upstream never stops the app — the last
        good ticks stand, marked stale, and stale reads as no
        price until a fresh tick lands."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        thread = threading.Thread(target=self._jupiter_loop,
                                  daemon=True, name="livefeed-jupiter")
        thread.start()
        self._threads.append(thread)
        log.info("[live-feed] started (jupiter SOL check; coin "
                 "ticks come from the Helius stream)")

    def stop(self) -> None:
        self._stop.set()
        self._started = False

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
        # Jupiter price v3 returns a flat {mint: {...}} map — no "data"
        # wrapper (verified live 2026-11-16: {"So11...": {"usdPrice": ...}}).
        # Accept the wrapped shape too, so either response parses.
        entry = raw.get(JUPITER_SOL_MINT) or \
            (raw.get("data") or {}).get(JUPITER_SOL_MINT) or {}
        try:
            price = float(entry.get("usdPrice"))
        except (TypeError, ValueError):
            return
        if not math.isfinite(price) or price <= 0:
            return
        # Jupiter IS the SOL source now (the unit of account) — there
        # is no Kraken feed left to cross-check against.
        change = entry.get("priceChange24h")
        self.ingest("SOL", price, "jupiter", change_24h=change)

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
