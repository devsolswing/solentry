"""Helius live-price stream: the ONE price stream for the whole app.

Why: DexScreener has no public price/trade websocket — its API is
REST only, and the app's 5-second poll is already the fastest honest
use of it (see ~/workspace/mainnet-trading-research/REPORT.md §1).
For a held coin, the most accurate live price is not a third-party
quote at all: it is the bonding curve's own virtual reserves, read
from the chain. Helius's free-tier WebSocket lets the app subscribe
to a curve account and hear every state change (~1 per slot,
~400ms), then derive the price from the reserves — ground truth,
no intermediary price to be wrong. The same ground truth covers a
graduated coin: its Raydium pool's vault balances, subscribed the
same way (see _find_pool).

ONE STREAM (2026-11-25, "Do we need two streams?" — no): this is
the only price stream in the app. The DexScreener 5-second poll is
deleted — nothing reads DexScreener for prices any more — and every
price the app shows or decides on is a tick from this stream (or a
direct Helius RPC read of the same curve/pool account, same math,
for a coin the stream has not subscribed yet). A coin this stream
cannot tick (no curve, no known pool) has NO price: pages say
"no data" and the bot skips it.

Design (stream what you hold, pull what you discover):
- One daemon thread, one Helius connection. It subscribes (standard
  Solana `accountSubscribe`) to the bonding-curve accounts of HELD
  mints (bot positions + paper holdings) and browser-WATCHED mints
  only — never the full scanner set. Discovery candidates are
  priced on demand by the silent loop with direct Helius RPC reads
  of the same curve accounts (same math, same source).
- Every curve update is priced from the virtual reserves with the
  same math the executor's buy_quote reads
  (`pumpfun.curve_spot_price_sol` x the feed's live SOL price),
  then fed through the EXISTING `LiveFeed.ingest` validation — the
  50% single-tick jump guard, the 3-agreeing-tick re-base, the
  out-of-order guard. The stream never bypasses ingest, and it
  never decides or executes anything: it only supplies prices.
- Graduated coins: a curve update that parses as complete moves
  the mint to its Raydium pool — the stream finds the pool through
  Helius alone (getProgramAccounts on the Raydium AMM program,
  memcmp on the pool's baseMint), subscribes to the pool's base +
  quote vaults, and prices from the vault balances. A graduated
  coin with no discoverable pool has no price ("no data").
- Stale refresh (2026-11-25): a quiet coin with no trades stops
  ticking, and its last tick goes stale — stale reads as "no data".
  The stream re-reads a stale subscribed mint's curve (or pool
  vaults) straight from the chain on its own periodic wake, so
  "no data" is brief: the re-read carries the current reserves
  (unchanged when nothing traded) and the price is back. Bounded
  to one re-read per mint per STALE_REFRESH_SECONDS.
- Reconnect with jittered backoff + a heartbeat ping + a watchdog
  (45s of no data closes the socket and reconnects). On reconnect
  it resubscribes and backfills from direct chain reads, so no mint
  is left showing a stale price as live.

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

from .prices import get_sol_price_usd
from .pumpfun import (bonding_curve_pda, curve_spot_price_sol,
                      parse_bonding_curve)
from .solana import base58_encode

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

# Raydium AMM v4 program: the pool account for a graduated coin.
# The stream finds the pool through Helius alone (getProgramAccounts
# with a memcmp filter on the pool's baseMint) and then subscribes
# to the pool's base + quote vaults. Offsets are the AMM v4 state
# layout: 24 u64 fields (192 bytes), 4 u128 + 2 u64 swap fields (80
# bytes), then baseVault @272, quoteVault @304, baseMint @336,
# quoteMint @368 (each a 32-byte pubkey). The layout is NOT verified
# against a live pool in this build — a wrong offset fails safe: no
# pool is found and the coin shows "no data", never a wrong price.
# Vault balances are SPL token accounts: the amount is a u64 LE at
# offset 64.
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_POOL_BASE_VAULT_OFFSET = 272
RAYDIUM_POOL_QUOTE_VAULT_OFFSET = 304
RAYDIUM_POOL_BASE_MINT_OFFSET = 336
RAYDIUM_POOL_BASE_DECIMALS_OFFSET = 32
RAYDIUM_POOL_QUOTE_DECIMALS_OFFSET = 40
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
# Pump.fun graduations pair the token against wrapped SOL.
WSOL_MINT = "So11111111111111111111111111111111111111112"

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
# Stale-tick refresh: a subscribed mint whose feed tick is stale
# (or missing) is re-read from the chain at most this often, so
# "no data" is brief without hammering the RPC.
STALE_REFRESH_SECONDS = 30.0
# The SOL reference for the USD conversion: SOL refreshes from
# Jupiter every 60s, so the feed's 30s staleness cut would drop
# half the stream's updates. SOL moves slowly — a tick up to 90s
# old bounds the conversion error to SOL's move over ~90s — and
# older than that the stream stops ingesting rather than guessing.
SOL_MAX_AGE_SECONDS = 90.0
WS_CONNECT_TIMEOUT = 10
WS_RECV_TIMEOUT = 5
# Fallback SOL fetch: when the feed's SOL tick is stale or missing,
# the chain-read fallback fetches SOL directly from Jupiter with
# this short timeout, so a down Jupiter cannot stall a poll (the
# prices.py fetch is cached 60s, so this fires at most once a
# minute per process).
SOL_FALLBACK_FETCH_TIMEOUT = 5.0
# Repeat "last-known-good SOL rate" warnings are throttled to one
# per interval per stream instance, so a long Jupiter outage does
# not flood the logs.
SOL_FALLBACK_LOG_INTERVAL = 60.0


def sol_rate_for_conversion(feed, now: float | None = None):
    """(price_usd, source, age_seconds) for converting a
    SOL-denominated chain price to USD, or None when no rate
    exists at all. The fallback's SOL rate is decoupled from the
    feed's SOL tick: a fresh feed tick (<= SOL_MAX_AGE_SECONDS)
    wins ("feed"); else one direct Jupiter fetch ("jupiter",
    cached 60s inside prices.py, 5s timeout); else the feed's
    last-known-good SOL tick at any age ("last-good", labeled with
    its age — a stale rate, never presented as fresh). Never
    raises; None only when there is no feed tick, the Jupiter
    fetch failed, and no last-known-good tick exists. Callers log
    the non-"feed" sources — a conversion at a stale rate must
    always be visible in the logs, never silent."""
    now = time.time() if now is None else now
    if feed is not None:
        try:
            age = feed.tick_age_seconds("SOL", now=now)
        except Exception:
            age = None
        if age is not None and age <= SOL_MAX_AGE_SECONDS:
            try:
                tick = feed.snapshot(["SOL"]).get("SOL")
                price = float(tick["price"]) if tick else None
            except Exception:
                price = None
            if price is not None and math.isfinite(price) \
                    and price > 0:
                return (price, "feed", age)
    try:
        price = get_sol_price_usd(timeout=SOL_FALLBACK_FETCH_TIMEOUT)
    except Exception:
        price = None
    if price is not None and math.isfinite(price) and price > 0:
        # Fetched (or cached within 60s) just now — a live rate.
        return (price, "jupiter", 0.0)
    if feed is not None:
        try:
            age = feed.tick_age_seconds("SOL", now=now)
            tick = feed.snapshot(["SOL"]).get("SOL")
            price = float(tick["price"]) if tick else None
        except Exception:
            age, price = None, None
        if age is not None and price is not None \
                and math.isfinite(price) and price > 0:
            return (price, "last-good", age)
    return None


def _token_account_amount(data: bytes) -> int | None:
    """The token balance (base units) of an SPL token account, or
    None when the data is too short to parse. The amount is a u64
    little-endian at offset 64 (mint 0-32, owner 32-64)."""
    if len(data) < TOKEN_ACCOUNT_AMOUNT_OFFSET + 8:
        return None
    try:
        return int.from_bytes(
            data[TOKEN_ACCOUNT_AMOUNT_OFFSET:
                 TOKEN_ACCOUNT_AMOUNT_OFFSET + 8], "little")
    except Exception:
        return None


def find_raydium_pool(client, mint: str) -> dict | None:
    """A graduated mint's Raydium pool, through Helius alone:
    getProgramAccounts on the AMM program with a memcmp filter on
    the pool's baseMint. Returns {"pool", "base_vault",
    "quote_vault", "base_decimals", "quote_decimals"}, or None when
    no pool is found, the RPC fails, or the data does not parse.
    None means "no data" for this coin — never a guessed pool.
    Takes the first pool quoted in wSOL (a pump.fun graduation
    creates one pool per mint); a mint with several pools prices
    from the first. The AMM v4 layout offsets are NOT verified
    against a live pool in this build — a wrong offset fails safe
    (no pool found, never a wrong price)."""
    try:
        accounts = client.get_program_accounts(
            RAYDIUM_AMM_PROGRAM,
            [{"memcmp": {"offset": RAYDIUM_POOL_BASE_MINT_OFFSET,
                         "bytes": mint}}])
    except Exception as exc:
        log.warning("[helius-stream] pool lookup failed for %s: %s",
                    mint, exc)
        return None
    for entry in accounts or []:
        data = (entry.get("account") or {}).get("data") or b""
        if len(data) < RAYDIUM_POOL_BASE_MINT_OFFSET + 64:
            continue
        try:
            base_mint = base58_encode(
                data[RAYDIUM_POOL_BASE_MINT_OFFSET:
                     RAYDIUM_POOL_BASE_MINT_OFFSET + 32])
            quote_mint = base58_encode(
                data[RAYDIUM_POOL_BASE_MINT_OFFSET + 32:
                     RAYDIUM_POOL_BASE_MINT_OFFSET + 64])
            base_vault = base58_encode(
                data[RAYDIUM_POOL_BASE_VAULT_OFFSET:
                     RAYDIUM_POOL_BASE_VAULT_OFFSET + 32])
            quote_vault = base58_encode(
                data[RAYDIUM_POOL_QUOTE_VAULT_OFFSET:
                     RAYDIUM_POOL_QUOTE_VAULT_OFFSET + 32])
            base_decimals = int.from_bytes(
                data[RAYDIUM_POOL_BASE_DECIMALS_OFFSET:
                     RAYDIUM_POOL_BASE_DECIMALS_OFFSET + 8],
                "little")
            quote_decimals = int.from_bytes(
                data[RAYDIUM_POOL_QUOTE_DECIMALS_OFFSET:
                     RAYDIUM_POOL_QUOTE_DECIMALS_OFFSET + 8],
                "little")
        except Exception:
            continue
        if base_mint != mint or quote_mint != WSOL_MINT:
            continue
        if not base_vault or not quote_vault:
            continue
        if not (0 <= base_decimals <= 18
                and 0 <= quote_decimals <= 18):
            continue
        return {"pool": (entry.get("pubkey") or ""),
                "base_vault": base_vault,
                "quote_vault": quote_vault,
                "base_decimals": base_decimals,
                "quote_decimals": quote_decimals}
    return None


def pool_price_sol(pool: dict, vault_amounts: dict) -> float | None:
    """SOL per whole token from a pool's vault balances, or None
    when either vault has not been seen yet or the balances are
    degenerate. Vault amounts are base units; the pool's own
    decimals scale them."""
    if pool is None:
        return None
    base = vault_amounts.get(pool["base_vault"])
    quote = vault_amounts.get(pool["quote_vault"])
    if base is None or quote is None:
        return None
    try:
        base_tokens = float(base) / (10 ** pool["base_decimals"])
        quote_sol = float(quote) / (10 ** pool["quote_decimals"])
    except (TypeError, ValueError, OverflowError):
        return None
    if base_tokens <= 0 or quote_sol <= 0:
        return None
    price = quote_sol / base_tokens
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def chain_price_usd_for_mints(client, mints,
                              sol_price_usd) -> dict:
    """{mint: price_usd} for mints priced straight from the chain —
    the single source's pull twin of the Helius stream. Bonding
    curve first (one batched getMultipleAccounts for all curve
    PDAs); a mint whose curve is complete, missing or unreadable
    is tried against its Raydium pool (pool discovery + one
    batched vault read per such mint). Mints no source can price
    are absent from the result — never a stale, guessed or
    second-source number. sol_price_usd None (or unusable) means
    {}: no USD conversion without a live SOL rate."""
    out: dict = {}
    if not mints or sol_price_usd is None:
        return out
    try:
        sol_price_usd = float(sol_price_usd)
    except (TypeError, ValueError):
        return out
    if not (math.isfinite(sol_price_usd) and sol_price_usd > 0):
        return out
    mints = [m for m in dict.fromkeys(mints) if m]
    curve_addr: dict = {}
    addrs: list = []
    for mint in mints:
        try:
            addr, _ = bonding_curve_pda(mint)
        except Exception:
            continue
        curve_addr[mint] = addr
        addrs.append(addr)
    if not addrs:
        return out
    try:
        infos = client.get_multiple_accounts(addrs)
    except Exception as exc:
        log.warning("[helius-stream] batched curve read failed: %s",
                    exc)
        return out
    need_pool: list = []
    for mint, addr in curve_addr.items():
        info = infos.get(addr)
        if info is None:
            need_pool.append(mint)
            continue
        try:
            curve = parse_bonding_curve(info.get("data") or b"")
        except ValueError:
            need_pool.append(mint)
            continue
        if curve.get("complete"):
            need_pool.append(mint)
            continue
        price_sol = curve_spot_price_sol(curve)
        if price_sol is None:
            need_pool.append(mint)
            continue
        price = price_sol * sol_price_usd
        if math.isfinite(price) and price > 0:
            out[mint] = price
        else:
            need_pool.append(mint)
    # Graduated mints: pool discovery + one batched vault read each.
    for mint in need_pool:
        pool = find_raydium_pool(client, mint)
        if pool is None:
            continue
        try:
            vault_infos = client.get_multiple_accounts(
                [pool["base_vault"], pool["quote_vault"]])
        except Exception:
            continue
        amounts: dict = {}
        for vault, info in (vault_infos or {}).items():
            if info is None:
                continue
            amount = _token_account_amount(info.get("data") or b"")
            if amount is not None:
                amounts[vault] = amount
        price_sol = pool_price_sol(pool, amounts)
        if price_sol is None:
            continue
        price = price_sol * sol_price_usd
        if math.isfinite(price) and price > 0:
            out[mint] = price
    return out


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
        # Timer thread for the server-driven stale refresh (fix 4):
        # _refresh_stale_mints runs on its own clock, independent
        # of the websocket recv loop.
        self._refresh_thread = None
        self._ws = None
        # mint -> curve account address: the desired subscription
        # set (held + watched mints with a live, incomplete curve).
        self._subs: dict = {}
        # mint -> pool info for graduated mints: {"pool", "base_vault",
        # "quote_vault", "base_decimals", "quote_decimals"}.
        self._pools: dict = {}
        # vault address -> (amount in base units, decimals): the last
        # seen balance per pool vault, so a price can be recomputed
        # when either vault updates.
        self._vault_amounts: dict = {}
        # subscription id -> (mint, vault address) for pool-vault
        # subscriptions (curve subscriptions live in _sub_id).
        self._vault_sub_id: dict = {}
        # mint -> {vault address: subscription id}, for pool vaults.
        self._mint_vault_sub_id: dict = {}
        # mint -> last stale-refresh epoch (bounds re-reads).
        self._last_stale_refresh: dict = {}
        # Last "last-known-good SOL rate" warning epoch, per source
        # (see _log_last_good_sol): bounds repeat warnings to one
        # per SOL_FALLBACK_LOG_INTERVAL.
        self._last_sol_fallback_log: dict = {}
        # Per-connection state, reset on every (re)connect.
        self._pending_req: dict = {}  # jsonrpc id -> ("curve", mint)
        # or ("vault", mint, vault address)
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
        # Server-driven stale refresh (fix 4): its own timer
        # thread, so a dead websocket can no longer put the
        # refresh to sleep with it.
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, daemon=True,
            name="helius-refresh")
        self._refresh_thread.start()
        log.info("[helius-stream] started (Helius %s WSS, "
                 "held + watched mints)", self._network)

    def _refresh_loop(self) -> None:
        """Server-driven stale refresh, on its own timer: every
        STALE_REFRESH_SECONDS re-read subscribed mints whose feed
        tick is stale or missing, straight from the chain —
        independent of the websocket recv loop, so a 45s watchdog
        kill + up-to-60s reconnect backoff can no longer leave
        the refresh asleep for half the stall. The refresh needs
        only the RPC client and the feed, never a live socket:
        when the socket is down, _refresh_mints drops/pools
        graduated mints locally and the next (re)connect
        subscribes them. Never kills the thread; a failed pass
        is logged and the next tick retries."""
        while not self._stop.is_set():
            self._stop.wait(STALE_REFRESH_SECONDS)
            if self._stop.is_set():
                break
            try:
                self._refresh_stale_mints()
            except Exception as exc:  # never kill the refresh thread
                log.warning("[helius-stream] timer refresh failed: "
                            "%s", exc)

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
        unreadable curve is never subscribed — the pool is watched
        instead, or the coin has no price ("no data"). One REST
        read per new mint, on subscribe."""
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
        mints. New mints get a curve check first; a mint whose
        curve is complete (graduated) gets its Raydium pool
        instead; a mint with neither has no price ("no data").
        Mints that are no longer held or watched are unsubscribed."""
        desired = self._desired_mints()
        current = set(self._subs) | set(self._pools)
        for mint in sorted(desired - current):
            if self._curve_is_live(mint):
                curve_addr, _ = bonding_curve_pda(mint)
                self._subs[mint] = curve_addr
                if ws is not None:
                    try:
                        self._send_subscribe(ws, mint)
                    except Exception as exc:
                        log.warning("[helius-stream] subscribe failed "
                                    "for %s: %s", mint, exc)
                        self._subs.pop(mint, None)
            elif not self._ensure_pool(mint, ws=ws):
                log.info("[helius-stream] %s has no live curve and "
                         "no discoverable pool — no price", mint)
        for mint in sorted(current - desired):
            self._drop_mint(mint, "no longer held/watched", ws=ws)

    def _find_pool(self, mint: str) -> dict | None:
        """Find a graduated mint's Raydium pool through Helius
        alone (see find_raydium_pool). None means "no data" for
        this coin — never a guessed pool."""
        try:
            client = self._rpc_client()
        except Exception as exc:
            log.warning("[helius-stream] pool lookup failed for %s: "
                        "%s", mint, exc)
            return None
        return find_raydium_pool(client, mint)

    def _ensure_pool(self, mint: str, ws=None) -> bool:
        """Subscribe a graduated mint to its Raydium pool vaults.
        Returns True when the mint is now pool-subscribed."""
        if mint in self._pools:
            return True
        pool = self._find_pool(mint)
        if pool is None:
            return False
        self._pools[mint] = pool
        if ws is not None:
            try:
                self._send_subscribe_pool(ws, mint)
            except Exception as exc:
                log.warning("[helius-stream] pool subscribe failed "
                            "for %s: %s", mint, exc)
                self._pools.pop(mint, None)
                return False
        return True

    def _send_subscribe_pool(self, ws, mint: str) -> None:
        """Subscribe to a pool's base + quote vault accounts."""
        pool = self._pools[mint]
        for vault in (pool["base_vault"], pool["quote_vault"]):
            req_id = self._next_req_id
            self._next_req_id += 1
            ws.send(json.dumps(
                {"jsonrpc": "2.0", "id": req_id,
                 "method": "accountSubscribe",
                 "params": [vault,
                            {"encoding": SUBSCRIBE_ENCODING,
                             "commitment": SUBSCRIBE_COMMITMENT}]}))
            self._pending_req[req_id] = ("vault", mint, vault)

    def _drop_mint(self, mint: str, reason: str, ws=None) -> None:
        """Stop streaming a mint: unsubscribe when connected and
        forget it, so a later refresh can re-add it if it is held
        or watched again. Drops the curve subscription and the
        pool-vault subscriptions alike."""
        self._subs.pop(mint, None)
        self._last_slot.pop(mint, None)
        self._last_stale_refresh.pop(mint, None)
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
        pool = self._pools.pop(mint, None)
        if pool is not None:
            vault_subs = self._mint_vault_sub_id.pop(mint, {})
            for vault in (pool["base_vault"], pool["quote_vault"]):
                self._vault_amounts.pop(vault, None)
                self._last_slot.pop(vault, None)
                vault_sub = vault_subs.pop(vault, None)
                if vault_sub is not None:
                    self._vault_sub_id.pop(vault_sub, None)
                    if ws is not None:
                        req_id = self._next_req_id
                        self._next_req_id += 1
                        try:
                            ws.send(json.dumps(
                                {"jsonrpc": "2.0", "id": req_id,
                                 "method": "accountUnsubscribe",
                                 "params": [vault_sub]}))
                        except Exception as exc:
                            log.warning(
                                "[helius-stream] vault unsubscribe "
                                "failed for %s: %s", mint, exc)
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
        self._pending_req[req_id] = ("curve", mint)

    def _connect(self):
        url = self._wss_url()
        if url is None:
            raise RuntimeError("no Helius API key or unknown network")
        ws = self._ws_factory(url, timeout=WS_CONNECT_TIMEOUT)
        self._ws = ws
        self._pending_req = {}
        self._sub_id = {}
        self._mint_sub_id = {}
        self._vault_sub_id = {}
        self._mint_vault_sub_id = {}
        for mint in sorted(self._subs):
            self._send_subscribe(ws, mint)
        for mint in sorted(self._pools):
            self._send_subscribe_pool(ws, mint)
        self._last_message_at = self._time()
        log.info("[helius-stream] connected (%d curve + %d pool "
                 "subscriptions)",
                 len(self._subs), len(self._pools))
        return ws

    def _backfill(self) -> None:
        """After (re)connect, refresh the streamed mints from
        direct chain reads so no mint is left showing a stale price
        as live while the stream catches up. Never kills the
        thread."""
        mints = sorted(set(self._subs) | set(self._pools))
        if not mints:
            return
        try:
            self._refresh_mints(mints, force=True)
        except Exception as exc:
            log.warning("[helius-stream] backfill failed: %s", exc)

    def _log_last_good_sol(self, mint: str, sol_price: float,
                           age: float, now: float) -> None:
        """Throttled warning (one per SOL_FALLBACK_LOG_INTERVAL)
        when a price converts at the last-known-good SOL rate:
        the feed's SOL tick is stale and the Jupiter fetch failed,
        so the conversion carries SOL's whole move since that
        tick. The label is the point — a stale rate must be
        visible in the logs, never silent."""
        last = self._last_sol_fallback_log.get("last-good", 0.0)
        if now - last < SOL_FALLBACK_LOG_INTERVAL:
            return
        self._last_sol_fallback_log["last-good"] = now
        log.warning("[helius-stream] converting %s at "
                    "last-known-good SOL rate $%.2f, %.0fs old "
                    "(feed SOL tick stale, Jupiter fetch failed)",
                    mint, sol_price, age)

    def _ingest_sol_price(self, mint: str, price_sol: float,
                          now: float) -> bool:
        """Convert a SOL-denominated price and ingest it. Returns
        True when a tick was ingested. The SOL rate is decoupled
        from the feed's SOL tick (see sol_rate_for_conversion): a
        fresh feed tick wins; else a direct Jupiter fetch; else
        the last-known-good tick, labeled stale in the logs. Only
        when no rate exists at all is the update skipped — and
        that skip is logged, never silent. Never guessed: a
        missing rate is no price, not a converted one."""
        rate = sol_rate_for_conversion(self._feed, now=now)
        if rate is None:
            log.warning("[helius-stream] skipping %s: no SOL rate "
                        "for conversion (feed SOL tick missing/"
                        "stale, Jupiter fetch failed, no "
                        "last-known-good)", mint)
            return False
        sol_price, source, age = rate
        if source == "last-good":
            self._log_last_good_sol(mint, sol_price, age, now)
        price_usd = price_sol * sol_price
        if not math.isfinite(price_usd) or price_usd <= 0:
            return False
        return self._feed.ingest(mint, price_usd, SOURCE, now=now)

    def _pool_price_sol(self, mint: str) -> float | None:
        """SOL per whole token from a graduated mint's pool vault
        balances, or None when either vault has not been seen yet
        or the balances are degenerate (see pool_price_sol)."""
        pool = self._pools.get(mint)
        if pool is None:
            return None
        return pool_price_sol(pool, self._vault_amounts)

    def _ingest_pool_price(self, mint: str, now: float) -> bool:
        """Ingest a graduated mint's current pool price. Returns
        True when a tick was ingested, False when the price cannot
        be computed yet (a vault not seen) — the coin stays
        unpriced rather than showing a partial price."""
        price_sol = self._pool_price_sol(mint)
        if price_sol is None:
            return False
        return self._ingest_sol_price(mint, price_sol, now)

    def _refresh_mints(self, mints, now: float | None = None,
                       force: bool = False) -> None:
        """Re-read the given subscribed mints straight from the
        chain (bonding curves via one batched read, pool vaults
        the same way) and ingest the current price. Used after
        (re)connect (backfill, force=True) and for mints whose
        feed tick is stale or missing (force=False, bounded to one
        re-read per mint per STALE_REFRESH_SECONDS). A quiet coin's
        reserves do not move, so the re-read carries the same price
        with a fresh timestamp — "no data" is brief, never stuck.
        Never kills the thread."""
        now = self._time() if now is None else now
        todo = []
        for mint in sorted(mints):
            if not force:
                last = self._last_stale_refresh.get(mint, 0.0)
                if now - last < STALE_REFRESH_SECONDS:
                    continue
                if self._feed.live_tick(mint, now=now) is not None:
                    continue  # fresh already — nothing to refresh
            todo.append(mint)
        if not todo:
            return
        for mint in todo:
            self._last_stale_refresh[mint] = now
        addrs: list = []
        addr_kind: dict = {}
        for mint in todo:
            if mint in self._subs:
                addr = self._subs[mint]
                addrs.append(addr)
                addr_kind[addr] = ("curve", mint)
            pool = self._pools.get(mint)
            if pool is not None:
                for vault in (pool["base_vault"],
                              pool["quote_vault"]):
                    addrs.append(vault)
                    addr_kind[vault] = ("vault", mint)
        if not addrs:
            return
        try:
            client = self._rpc_client()
            infos = client.get_multiple_accounts(addrs)
        except Exception as exc:
            log.warning("[helius-stream] stale refresh read failed: "
                        "%s", exc)
            return
        for addr, info in infos.items():
            kind = addr_kind.get(addr)
            if info is None or kind is None:
                continue
            data = info.get("data") or b""
            if kind[0] == "curve":
                try:
                    curve = parse_bonding_curve(data)
                except ValueError:
                    continue
                if curve.get("complete"):
                    # Graduated between passes — move to the pool.
                    self._drop_mint(kind[1], "graduated",
                                    ws=self._ws)
                    if self._ensure_pool(kind[1], ws=self._ws):
                        log.info("[helius-stream] %s graduated — "
                                 "watching Raydium pool", kind[1])
                    continue
                price_sol = curve_spot_price_sol(curve)
                if price_sol is None:
                    continue
                self._ingest_sol_price(kind[1], price_sol, now)
            else:
                amount = _token_account_amount(data)
                if amount is None:
                    continue
                self._vault_amounts[addr] = amount
                self._ingest_pool_price(kind[1], now)

    def _refresh_stale_mints(self, now: float | None = None) -> None:
        """Re-read every subscribed mint whose feed tick is stale
        or missing (bounded per mint — see _refresh_mints). Called
        on the stream's periodic wake, so a removed price is
        replaced by a fresh tick as soon as one can be read. Never
        kills the thread."""
        now = self._time() if now is None else now
        mints = sorted(set(self._subs) | set(self._pools))
        if not mints:
            return
        try:
            self._refresh_mints(mints, now=now)
        except Exception as exc:
            log.warning("[helius-stream] stale refresh failed: %s",
                        exc)

    def _run(self) -> None:
        backoff = RECONNECT_BASE_SECONDS
        while not self._stop.is_set():
            ws = None
            try:
                self._refresh_subscriptions()
                if not self._subs and not self._pools:
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
            # Subscription confirmation: record which mint (and
            # which vault, for pool subscriptions) this id belongs
            # to.
            pending = self._pending_req.pop(msg.get("id"), None)
            if pending is None:
                return
            if pending[0] == "curve":
                mint = pending[1]
                self._sub_id[result] = mint
                self._mint_sub_id[mint] = result
            elif pending[0] == "vault":
                _kind, mint, vault = pending
                self._vault_sub_id[result] = (mint, vault)
                self._mint_vault_sub_id.setdefault(
                    mint, {})[vault] = result
            return
        if "error" in msg:
            log.warning("[helius-stream] error message: %s",
                        msg.get("error"))

    def _handle_account_notification(self, params: dict,
                                     now: float | None = None) -> bool:
        """One account update -> one validated price. Returns True
        when a tick was ingested. Every failure path returns False
        and leaves the last good price standing — never a guess,
        never a partial price."""
        now = self._time() if now is None else now
        sub = params.get("subscription")
        mint = self._sub_id.get(sub)
        if mint is not None:
            return self._handle_curve_notification(mint, params,
                                                   now=now)
        vault = self._vault_sub_id.get(sub)
        if vault is not None:
            return self._handle_vault_notification(vault[0], vault[1],
                                                   params, now=now)
        return False

    def _handle_curve_notification(self, mint: str, params: dict,
                                   now: float) -> bool:
        """One bonding-curve account update -> one validated price."""
        result = params.get("result") or {}
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
            # Graduated: the curve no longer prices the coin — move
            # to its Raydium pool. No pool found = no price
            # ("no data"), never a second-source number.
            self._drop_mint(mint, "graduated", ws=self._ws)
            if self._ensure_pool(mint, ws=self._ws):
                log.info("[helius-stream] %s graduated — watching "
                         "Raydium pool", mint)
            return False
        price_sol = curve_spot_price_sol(curve)
        if price_sol is None:
            return False
        return self._ingest_sol_price(mint, price_sol, now)

    def _handle_vault_notification(self, mint: str, vault: str,
                                   params: dict, now: float) -> bool:
        """One Raydium pool-vault update -> one validated price for
        a graduated mint. The price recomputes from BOTH vaults'
        last seen balances; a vault never seen yet means no price
        (never a partial price from one vault alone)."""
        result = params.get("result") or {}
        slot = (result.get("context") or {}).get("slot")
        if isinstance(slot, int):
            last_slot = self._last_slot.get(vault)
            if last_slot is not None and slot <= last_slot:
                return False
            self._last_slot[vault] = slot
        value = result.get("value") or {}
        data = value.get("data") or []
        raw = data[0] if data else None
        if not isinstance(raw, str):
            return False
        try:
            account_bytes = base64.b64decode(raw, validate=True)
        except Exception:
            return False
        amount = _token_account_amount(account_bytes)
        if amount is None:
            return False
        self._vault_amounts[vault] = amount
        return self._ingest_pool_price(mint, now)


def start_helius_stream(app):
    """Start the Helius stream for the app's live feed. Called
    from run.py only — create_app never starts threads, so tests
    never open sockets. Returns the HeliusStream, or None when
    streaming is off (no HELIUS_API_KEY): the app then has NO coin
    prices at all — pages show "no data" and the bot skips — until
    a key is set. That is the fail-closed single-source rule: no
    key, no second source to fall back to, never a stale number
    presented as live."""
    cfg = app.config["TRADINGBOT_CONFIG"]
    feed = app.config["LIVE_FEED"]
    stream = HeliusStream(feed, cfg)
    if not stream.enabled:
        log.warning("[helius-stream] HELIUS_API_KEY is not set — "
                    "live price streaming is OFF; the app has no "
                    "coin prices until a key is set (free Helius "
                    "account). Pages show 'no data' and the bot "
                    "skips — never a stale or second-source price.")
        return None
    stream.start()
    return stream
