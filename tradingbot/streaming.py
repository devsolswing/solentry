"""Helius live-price stream: bonding-curve prices straight off the
chain for the coins the app holds.

Why: DexScreener has no public price/trade websocket — its API is
REST only, and the app's 5-second poll is already the fastest honest
use of it (see ~/workspace/mainnet-trading-research/REPORT.md §1).
For a held coin, the most accurate live price is not a third-party
quote at all: it is the bonding curve's own virtual reserves, read
from the chain. Helius's free-tier WebSocket lets the app subscribe
to a curve account and hear every state change (~1 per slot,
~400ms), then derive the price from the reserves — ground truth,
no intermediary price to be wrong.

Design (stream what you hold, poll what you discover):
- One daemon thread, one Helius connection. It subscribes (standard
  Solana `accountSubscribe`) to the bonding-curve accounts of HELD
  mints (bot positions + paper holdings) and browser-WATCHED mints
  only — never the full scanner set. The DexScreener 5s poll stays
  as discovery + fallback for everything else.
- Every curve update is priced from the virtual reserves with the
  same math the executor's buy_quote reads
  (`pumpfun.curve_spot_price_sol` x the feed's live SOL price),
  then fed through the EXISTING `LiveFeed.ingest` validation — the
  50% single-tick jump guard, the 3-agreeing-tick re-base, the
  out-of-order guard. The stream never bypasses ingest, and it
  never decides or executes anything: it only supplies prices.
- Reconnect with jittered backoff + a heartbeat ping + a watchdog
  (45s of no data closes the socket and reconnects). On reconnect
  it resubscribes and backfills from the REST poll, so no mint is
  left showing a stale price as live.
- Graduated coins drop out by themselves: a curve update that
  parses as complete unsubscribes the mint (the Raydium/Jupiter
  price takes over via the poll).

Helius endpoints (official docs):
- Mainnet: wss://mainnet.helius-rpc.com/?api-key=<api-key>
- Devnet:  wss://devnet.helius-rpc.com/?api-key=<api-key>
- Docs: https://www.helius.dev/docs/api-reference/rpc/websocket/accountsubscribe
  and https://www.helius.dev/docs/rpc/websocket/quickstart
  (accountSubscribe, encoding base64, commitment confirmed; the
  10-minute inactivity timer is why the 30s ping exists).

Config: HELIUS_API_KEY from env only, never hardcoded. Unset =
streaming OFF and the app behaves exactly as today (DexScreener
5s poll) — fail-closed, no breakage, a warning at startup. The
key itself is James's step (free Helius account). The stream is
started from run.py only — create_app never starts threads, so
tests never open sockets.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import random
import threading
import time

import websocket

from .pumpfun import (bonding_curve_pda, curve_spot_price_sol,
                      parse_bonding_curve)

log = logging.getLogger("tradingbot.streaming")

# Helius WSS endpoints (official docs, see module docstring). The
# API key goes in the query string, exactly as Helius documents.
HELIUS_WSS_URLS = {
    "mainnet": "wss://mainnet.helius-rpc.com/?api-key=",
    "devnet": "wss://devnet.helius-rpc.com/?api-key=",
}
# Source label on every tick the stream ingests, so /api/live and
# the logs show where a price came from.
SOURCE = "helius-stream"
# accountSubscribe parameters: base64 is the reliable encoding for
# a pump.fun curve account (jsonParsed has no parser for it and
# falls back to binary anyway); confirmed is faster than finalized
# and still network-confirmed.
SUBSCRIBE_ENCODING = "base64"
SUBSCRIBE_COMMITMENT = "confirmed"

# --- defaults (Remy defaults, James's call — see MANIFEST) --------
# Ping every 30s: Helius's 10-minute inactivity timer is why the
# ping exists at all; 30s is the stricter of the two values in
# Helius's own docs (quickstart: 30s; method page: every minute).
HEARTBEAT_SECONDS = 30.0
# No data message for this long = the socket is dead or the feed
# is silent: close and reconnect. A quiet mint with no trades
# trips this too — a bounded reconnect + resubscribe + backfill,
# not an error — and the 5s poll covers the gap.
WATCHDOG_SILENCE_SECONDS = 45.0
# Reconnect backoff: start at 1s, double to a 60s cap, jittered so
# a fleet of restarts does not hammer Helius in lockstep.
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
# How often the subscription set is re-derived (held + watched
# mints change as bots trade and browsers come and go).
SUBSCRIPTION_REFRESH_SECONDS = 60.0
# The SOL reference for the USD conversion: SOL refreshes from
# Jupiter every 60s, so the feed's 30s staleness cut would drop
# half the stream's updates. SOL moves slowly — a tick up to 90s
# old bounds the conversion error to SOL's move over ~90s — and
# older than that the stream stops ingesting rather than guessing.
SOL_MAX_AGE_SECONDS = 90.0
WS_CONNECT_TIMEOUT = 10
WS_RECV_TIMEOUT = 5


class HeliusStream:
    """One Helius WebSocket connection streaming bonding-curve
    prices for held + watched mints into a LiveFeed."""

    def __init__(self, feed, config, ws_factory=None,
                 rpc_factory=None, sleep=None, time_fn=None):
        self._feed = feed
        self._config = config
        self._api_key = (getattr(config, "helius_api_key", "")
                         or "").strip()
        self._stream_flag = getattr(config, "helius_stream_enabled",
                                    True)
        self._network = (getattr(config, "network", "devnet")
                         or "devnet").lower()
        self._max_mints = max(
            1, int(getattr(config, "helius_max_stream_mints", 50)))
        # Injectable seams: the tests drive the whole loop with a
        # fake socket and a fake RPC, never a real connection.
        self._ws_factory = ws_factory or websocket.create_connection
        self._rpc_factory = rpc_factory
        self._sleep = sleep or time.sleep
        self._time = time_fn or time.time
        self._stop = threading.Event()
        self._started = False
        self._thread = None
        self._ws = None
        # mint -> curve account address: the desired subscription
        # set (held + watched mints with a live, incomplete curve).
        self._subs: dict = {}
        # Per-connection state, reset on every (re)connect.
        self._pending_req: dict = {}  # jsonrpc id -> mint
        self._sub_id: dict = {}  # subscription id -> mint
        self._mint_sub_id: dict = {}  # mint -> subscription id
        self._last_slot: dict = {}  # mint -> last processed slot
        self._last_message_at = 0.0
        self._next_req_id = 1

    # --- lifecycle ------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Streaming is on only when a key is set and the flag
        allows it. No key = off, and nothing else changes."""
        return bool(self._api_key) and bool(self._stream_flag)

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="helius-stream")
        self._thread.start()
        log.info("[helius-stream] started (Helius %s WSS, "
                 "held + watched mints)", self._network)

    def stop(self) -> None:
        self._stop.set()
        self._started = False
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # --- subscription set ----------------------------------------------
    def _wss_url(self) -> str | None:
        if not self._api_key:
            return None
        base = HELIUS_WSS_URLS.get(self._network)
        if base is None:
            return None
        return base + self._api_key

    def _desired_mints(self) -> set:
        """Held mints (bot positions + paper holdings) plus
        browser-watched mints — the stream-what-you-hold set.
        Capped, deterministically, so a pathological set cannot
        open unbounded subscriptions."""
        mints = set(self._feed.watched_mints())
        try:
            mints |= set(self._feed._held_mints_from_db())
        except Exception as exc:
            log.warning("[helius-stream] held-mints read failed: %s",
                        exc)
        if len(mints) > self._max_mints:
            log.warning("[helius-stream] %d mints wanted, capping "
                        "stream subscriptions at %d",
                        len(mints), self._max_mints)
            mints = set(sorted(mints)[:self._max_mints])
        return mints

    def _rpc_client(self):
        if self._rpc_factory is not None:
            return self._rpc_factory(self._config.rpc_url)
        from .solana import SolanaRpcClient
        return SolanaRpcClient(self._config.rpc_url)

    def _curve_is_live(self, mint: str) -> bool:
        """True only when the mint's bonding curve exists, parses,
        and is incomplete. A graduated coin (complete) or an
        unreadable curve is never subscribed — the poll prices
        those instead. One REST read per new mint, on subscribe."""
        try:
            curve_addr, _ = bonding_curve_pda(mint)
            client = self._rpc_client()
            info = client.get_account_info(curve_addr)
        except Exception as exc:
            log.warning("[helius-stream] curve check failed for %s: "
                        "%s", mint, exc)
            return False
        if not info or not info.get("data"):
            return False
        try:
            curve = parse_bonding_curve(info["data"])
        except ValueError:
            return False
        return not curve.get("complete")

    def _refresh_subscriptions(self, ws=None) -> None:
        """Re-derive the subscription set from held + watched
        mints. New mints get a curve check first; mints that are
        no longer held or watched are unsubscribed."""
        desired = self._desired_mints()
        current = set(self._subs)
        for mint in sorted(desired - current):
            if not self._curve_is_live(mint):
                continue
            curve_addr, _ = bonding_curve_pda(mint)
            self._subs[mint] = curve_addr
            if ws is not None:
                try:
                    self._send_subscribe(ws, mint)
                except Exception as exc:
                    log.warning("[helius-stream] subscribe failed "
                                "for %s: %s", mint, exc)
                    self._subs.pop(mint, None)
        for mint in sorted(current - desired):
            self._drop_mint(mint, "no longer held/watched", ws=ws)

    def _drop_mint(self, mint: str, reason: str, ws=None) -> None:
        """Stop streaming a mint: unsubscribe when connected and
        forget it, so a later refresh can re-add it if it is held
        or watched again."""
        self._subs.pop(mint, None)
        self._last_slot.pop(mint, None)
        sub_id = self._mint_sub_id.pop(mint, None)
        if sub_id is not None:
            self._sub_id.pop(sub_id, None)
        if ws is not None and sub_id is not None:
            req_id = self._next_req_id
            self._next_req_id += 1
            try:
                ws.send(json.dumps(
                    {"jsonrpc": "2.0", "id": req_id,
                     "method": "accountUnsubscribe",
                     "params": [sub_id]}))
            except Exception as exc:
                log.warning("[helius-stream] unsubscribe failed for "
                            "%s: %s", mint, exc)
        log.info("[helius-stream] dropped %s (%s)", mint, reason)

    # --- connection ------------------------------------------------------
    def _send_subscribe(self, ws, mint: str) -> None:
        curve_addr = self._subs[mint]
        req_id = self._next_req_id
        self._next_req_id += 1
        ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": req_id,
             "method": "accountSubscribe",
             "params": [curve_addr,
                        {"encoding": SUBSCRIBE_ENCODING,
                         "commitment": SUBSCRIBE_COMMITMENT}]}))
        self._pending_req[req_id] = mint

    def _connect(self):
        url = self._wss_url()
        if url is None:
            raise RuntimeError("no Helius API key or unknown network")
        ws = self._ws_factory(url, timeout=WS_CONNECT_TIMEOUT)
        self._ws = ws
        self._pending_req = {}
        self._sub_id = {}
        self._mint_sub_id = {}
        for mint in sorted(self._subs):
            self._send_subscribe(ws, mint)
        self._last_message_at = self._time()
        log.info("[helius-stream] connected (%d subscriptions)",
                 len(self._subs))
        return ws

    def _backfill(self) -> None:
        """After (re)connect, refresh the streamed mints from REST
        so no mint is left showing a stale price as live while the
        stream catches up. The 5s DexScreener poller covers the
        same mints within one poll anyway; this just closes the
        gap at once. Never kills the thread."""
        mints = sorted(self._subs)
        if not mints:
            return
        try:
            self._feed._poll_dexscreener(mints)
        except Exception as exc:
            log.warning("[helius-stream] backfill failed: %s", exc)

    def _run(self) -> None:
        backoff = RECONNECT_BASE_SECONDS
        while not self._stop.is_set():
            ws = None
            try:
                self._refresh_subscriptions()
                if not self._subs:
                    # Nothing to stream: idle until the set changes.
                    self._sleep(SUBSCRIPTION_REFRESH_SECONDS)
                    continue
                ws = self._connect()
                self._backfill()
                backoff = RECONNECT_BASE_SECONDS
                self._recv_loop(ws)
            except Exception as exc:
                log.warning("[helius-stream] connection error: %s",
                            exc)
            finally:
                self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            delay = backoff * (0.5 + random.random())
            self._sleep(delay)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    def _recv_loop(self, ws) -> None:
        """Read messages until the socket dies, the watchdog fires,
        or the stream stops. A dead socket raises into _run, which
        reconnects with backoff."""
        try:
            ws.settimeout(WS_RECV_TIMEOUT)
        except Exception:
            pass
        now = self._time()
        next_ping_at = now + HEARTBEAT_SECONDS
        next_refresh_at = now + SUBSCRIPTION_REFRESH_SECONDS
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                now = self._time()
                if now >= next_ping_at:
                    try:
                        ws.ping()
                    except Exception as exc:
                        raise ConnectionError(
                            "ping failed: %s" % exc)
                    next_ping_at = now + HEARTBEAT_SECONDS
                if now - self._last_message_at > \
                        WATCHDOG_SILENCE_SECONDS:
                    raise ConnectionError(
                        "no stream data for %.0fs"
                        % WATCHDOG_SILENCE_SECONDS)
                if now >= next_refresh_at:
                    self._refresh_subscriptions(ws)
                    next_refresh_at = now + SUBSCRIPTION_REFRESH_SECONDS
                continue
            except websocket.WebSocketConnectionClosedException as exc:
                raise ConnectionError("socket closed: %s" % exc)
            now = self._time()
            self._last_message_at = now
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            self._handle_message(raw, now=now)
            if now >= next_refresh_at:
                self._refresh_subscriptions(ws)
                next_refresh_at = now + SUBSCRIPTION_REFRESH_SECONDS

    # --- messages ---------------------------------------------------------
    def _handle_message(self, raw, now: float | None = None) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("[helius-stream] unparseable message")
            return
        if not isinstance(msg, dict):
            return
        if msg.get("method") == "accountNotification":
            self._handle_account_notification(
                msg.get("params") or {}, now=now)
            return
        result = msg.get("result")
        if isinstance(result, int):
            # Subscription confirmation: record which mint this
            # subscription id belongs to.
            mint = self._pending_req.pop(msg.get("id"), None)
            if mint is not None:
                self._sub_id[result] = mint
                self._mint_sub_id[mint] = result
            return
        if "error" in msg:
            log.warning("[helius-stream] error message: %s",
                        msg.get("error"))

    def _handle_account_notification(self, params: dict,
                                     now: float | None = None) -> bool:
        """One curve-account update -> one validated price. Returns
        True when a tick was ingested. Every failure path returns
        False and leaves the last good price standing — never a
        guess, never a partial price."""
        now = self._time() if now is None else now
        result = params.get("result") or {}
        mint = self._sub_id.get(params.get("subscription"))
        if mint is None:
            return False
        slot = (result.get("context") or {}).get("slot")
        if isinstance(slot, int):
            last_slot = self._last_slot.get(mint)
            if last_slot is not None and slot <= last_slot:
                # Replay or out-of-order delivery: the newer slot
                # already set the price — never rewind it.
                return False
            self._last_slot[mint] = slot
        value = result.get("value") or {}
        data = value.get("data") or []
        raw = data[0] if data else None
        if not isinstance(raw, str):
            return False
        try:
            account_bytes = base64.b64decode(raw, validate=True)
        except Exception:
            return False
        try:
            curve = parse_bonding_curve(account_bytes)
        except ValueError:
            return False
        if curve.get("complete"):
            # Graduated: the curve no longer prices the coin — the
            # Raydium/Jupiter price takes over via the poll.
            self._drop_mint(mint, "graduated", ws=self._ws)
            return False
        price_sol = curve_spot_price_sol(curve)
        if price_sol is None:
            return False
        # USD needs the feed's live SOL price. SOL refreshes from
        # Jupiter every 60s, so a tick up to SOL_MAX_AGE_SECONDS
        # old is accepted (see the constant's note); older than
        # that, or no SOL tick at all, the update is skipped.
        sol_age = self._feed.tick_age_seconds("SOL", now=now)
        if sol_age is None or sol_age > SOL_MAX_AGE_SECONDS:
            return False
        sol = self._feed.snapshot(["SOL"]).get("SOL")
        if sol is None:
            return False
        try:
            sol_price = float(sol["price"])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(sol_price) or sol_price <= 0:
            return False
        price_usd = price_sol * sol_price
        if not math.isfinite(price_usd) or price_usd <= 0:
            return False
        return self._feed.ingest(mint, price_usd, SOURCE, now=now)


def start_helius_stream(app):
    """Start the Helius stream for the app's live feed. Called
    from run.py only — create_app never starts threads, so tests
    never open sockets. Returns the HeliusStream, or None when
    streaming is off (no HELIUS_API_KEY): the app then runs on
    the DexScreener poll exactly as before."""
    cfg = app.config["TRADINGBOT_CONFIG"]
    feed = app.config["LIVE_FEED"]
    stream = HeliusStream(feed, cfg)
    if not stream.enabled:
        log.warning("[helius-stream] HELIUS_API_KEY is not set — "
                    "live curve streaming is OFF; prices come from "
                    "the DexScreener poll as before. Set "
                    "HELIUS_API_KEY (free Helius account) to "
                    "enable it.")
        return None
    stream.start()
    return stream
