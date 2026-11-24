"""$GATE trade packages (James, 2026-11-23) — real trades cost fuel.

Packages replace the holdings-funded model (2026-11-22, superseded —
held $GATE is never fuel now). One real trade — manual or silent-bot —
costs 1 trade credit, bought as a $GATE package at live market price.
Packages are bought ONLY in three sizes: 1M $GATE = 100 trades,
5M $GATE = 550 trades (500 + 50 free), 10M $GATE = 1450 trades
(1000 + 450 free). There are no custom amounts, and $GATE bought
OUTSIDE the app can NEVER be fuel: the package balance starts at 0
and grows only from packages bought inside the app.

Purchase flow: the user taps Buy, GATEkeeper buys $GATE ON-CHAIN
through the pump.fun program with the user's real-mode SOL while
$GATE is still bonding (the same venue decision as coin_buy: a
graduated coin goes through Jupiter instead, so the flow keeps
working if $GATE graduates later), then sends the purchased $GATE
straight to the dev wallet immediately, up front. Once that transfer
is confirmed safe on-chain, GATEkeeper credits the package's trade
count AUTOMATICALLY — no manual step. There is NO per-trade send.
Paper trading stays free: fuel applies to REAL trades only.

Package prices: the user always receives EXACTLY the package's
coins — 1M / 5M / 10M $GATE — and pays whatever the live quote says
in SOL for exactly that many coins at the pump.fun curve price. The
$3 / $15 / $30 CAD figures are the expected launch prices, shown
as reference only. The pricing rule lives in ONE place
(price_bundle_sol / bundle_send_base_units) so nothing else
computes a purchase amount. The Buy popup links straight to the
coin on pump.fun (pump.fun/coin/<mint>) so the user can see it
there too.

GATEkeeper is the bot that runs this: it executes package purchases,
tracks each user's fuel, signs through the existing signing-bot
infrastructure, and enforces the fuel gate. It is a row in the bots
table (mode real, never silent) so the pump.fun executor's own
guards (buy-size cap, bonding-curve checks) apply to its buys
exactly as they do to any real bot.

LEDGER TRUTH (James, 2026-11-13): fuel consumption is counted
ONLY from confirmed executed trades — never from mark prices,
estimates, or loop-seen values. The persisted ledger is the truth:

  * gate_fuel_purchases — one row per completed package purchase
    (credits granted).
  * gate_fuel_uses — one row per consumed credit, keyed by the
    confirmed on-chain signature (UNIQUE: one signature can never
    consume twice) and, for silent-bot trades, the trade_ledger
    trade_id (UNIQUE: one ledger row can never consume twice).
  * gate_fuel — a cached counter, used ONLY as the concurrency
    primitive for the atomic check-and-decrement. Every read
    recomputes the truth from purchases minus uses (reconcile_fuel);
    when the cache disagrees, the ledger wins and the discrepancy
    is written to the activity log. The number the user sees is
    always the ledger truth.

KEY SECURITY (same pattern as the signing bot): no API route, log,
or message ever carries key material. The wallet key is decrypted
into a mutable buffer ONLY for the single package -> dev-wallet
transfer, used for one signature, and wiped IN PLACE in a finally
block the moment signing is done — whether the send succeeds or
fails. After the send, no trace of the key remains in memory, logs,
or storage. A decrypted key is never stored and never sent to any
other party or service.

SELF-HEALING (deterministic, inside its rules only): GATEkeeper
reconciles fuel counts from the ledger (backfilling a use row for
any real trade_ledger row after the first purchase that has none);
a failed dev-wallet send is verified on-chain before any retry
(send_and_confirm re-checks the known signature — a reported
failure means the transaction did NOT land), retried ONCE on
transient RPC errors, and if it still fails the purchase fails
closed (no credits granted, user told plainly); a package purchase
is refused plainly when no live price is available (never bought
at a guessed price). Every corrective action is written to the
activity log (what, when, why). GATEkeeper NEVER changes its own
limits, rates, or rules, and never moves funds except the specified
package -> dev-wallet send (and the trade signing it already does).
Anything beyond its rules is flagged in the log, not improvised.

The $GATE mint comes from config only (GATE_MINT env var) — it is
NEVER hardcoded in this module except for the network defaults
below. The mainnet mint swaps in by config change only, no code
change. The dev wallet is the fee-recipient pubkey from config —
never hardcoded here either.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

from .config import Config
from .executor import execute_real_trade, is_curve_complete, send_and_confirm
from .jupiter_executor import execute_jupiter_swap
from .pumpfun import (TOKEN_BASE, bonding_curve_pda, buy_quote,
                      curve_progress_pct, global_pda, parse_bonding_curve,
                      parse_global, sol_for_tokens)
from .security import wipe_secret
from .signing_bot import ensure_signing_bot, is_valid_support_id
from .solana import (SolanaRpcClient, is_valid_address, lamports_to_sol,
                     rpc_client_for_url, sol_to_lamports)
from . import token_send as token_send_mod
from .wallet_keys import temporary_wallet_secret

# Devnet Gate mint and the mainnet $GATE mint (only ever used on
# mainnet — see gate_mint). The mint is resolved from config
# (GATE_MINT env) with these as the network defaults.
GATE_MINT_DEVNET = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_MINT_MAINNET = "3Tb3C76pfUrdTsbQZfwQv53acSq11shAnUQxGLiRpump"

# Buy $GATE presets: exact token amounts (whole $GATE). Kept for
# the curve quote endpoint, which prices each exact preset from
# the live bonding curve.
GATE_PRESETS = (1_000_000, 5_000_000, 10_000_000)

# Packages ONLY — no custom amounts. "trades" is what the package
# credits; "coins" is the package's coin amount; "cad_price" is the
# EXPECTED launch price in Canadian dollars (James, 2026-11-13
# ~15:20) — reference only. The user always receives exactly the
# package's coins and pays the live SOL quote for them; that is the
# ONE pricing rule in price_bundle_sol / bundle_send_base_units
# below, and nothing else in this module computes a purchase amount.
BUNDLES: tuple[dict, ...] = (
    {"id": "bundle_1m", "coins": 1_000_000, "trades": 100,
     "label": "1M $GATE", "badge": None, "cad_price": 3},
    {"id": "bundle_5m", "coins": 5_000_000, "trades": 550,
     "label": "5M $GATE", "badge": "Sale", "cad_price": 15},
    {"id": "bundle_10m", "coins": 10_000_000, "trades": 1450,
     "label": "10M $GATE", "badge": None, "cad_price": 30},
)
_BUNDLE_BY_ID = {b["id"]: b for b in BUNDLES}

# The buy prices the package at the live price plus this buffer, so a
# small adverse move between the quote and execution still fills
# the full package. The estimate shown to the user BEFORE confirming
# includes the buffer, and any extra $GATE the buffer buys stays in
# the user's own app wallet. If the balance after the buy is still
# short of the full package, the purchase fails closed (see below).
SLIPPAGE_BUFFER_PCT = 1.0

GATEKEEPER_BOT_NAME = "GATEkeeper"

# A dev-wallet send that fails with one of these reasons is a
# transient RPC failure and is retried ONCE (after the on-chain
# verify inside send_and_confirm proved the first attempt did NOT
# land). Any other reason — a node rejection, a refusal, a bad
# address — fails immediately with no retry: retrying it would fail
# the same way.
_TRANSIENT_SEND_REASONS = ("transaction not confirmed",
                           "transaction not sent")

# The $GATE logo: pump.fun metadata image for the mint when it
# resolves, else the SolEntry fox mascot — never a broken image.
# Fetched server-side from DexScreener (the feed's own source for
# mint data) and cached per process for 5 minutes; one mint only,
# never a bulk load.
_LOGO_CACHE_SECONDS = 300
_logo_cache: dict[str, tuple[str, float]] = {}
_FOX_LOGO_URL = "/static/fox-gold-480.jpg"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_activity(db: sqlite3.Connection, user_id: int, kind: str,
                  detail: str) -> None:
    """One activity row. Fuel is real-mode only, so rows are tagged
    mode='real'. The activity log is the audit trail for every
    corrective action GATEkeeper takes."""
    db.execute(
        "INSERT INTO activity (user_id, kind, detail, created_at, mode) "
        "VALUES (?, ?, ?, ?, 'real')",
        (user_id, kind, detail, _now()))
    db.commit()


# ------------------------------------------------------------------
# Mint resolution + the live curve quote (kept from the holdings
# model — the Buy $GATE quote and the package buy both need them).
# ------------------------------------------------------------------

def gate_mint(cfg) -> str:
    """The $GATE mint for this network, from config only (GATE_MINT
    env) with the network defaults. Mainnet: GATE_MINT unless it is
    the devnet mint, else the mainnet mint. Devnet: GATE_MINT
    unless it is the mainnet mint, else the devnet mint. The mint
    is never hardcoded at a call site."""
    mint = (cfg.gate_mint or "").strip()
    if cfg.network == "mainnet":
        if mint and mint != GATE_MINT_DEVNET:
            return mint
        return GATE_MINT_MAINNET
    if mint and mint != GATE_MINT_MAINNET:
        return mint
    return GATE_MINT_DEVNET


def effective_gate_mint(cfg) -> str | None:
    """The mint the Buy $GATE flow buys. Packages resolved the old
    buyability distinction: the package buy runs through the live
    executor on either network now, so this is gate_mint — kept
    as a name for the quote endpoint and older callers."""
    return gate_mint(cfg)


def held_gate_mint(cfg) -> str | None:
    """Kept as a name for older callers (the trades-left chip and
    the $GATE-first sort used to read the HELD mint). Under
    packages, held $GATE is never fuel — this is gate_mint, the
    package mint, and nothing reads a held balance from it."""
    return gate_mint(cfg)


def read_gate_curve(rpc, mint: str):
    """The live bonding curve + program fee_bps for `mint`, or
    (None, None) when either account cannot be read — the caller
    fails closed. Same read pattern as the executor."""
    try:
        global_info = rpc.get_account_info(global_pda()[0])
        if not global_info or not global_info.get("data"):
            return None, None
        fee_bps = int(parse_global(global_info["data"])
                      ["fee_basis_points"])
        curve_info = rpc.get_account_info(bonding_curve_pda(mint)[0])
        if not curve_info or not curve_info.get("data"):
            return None, None
        curve = parse_bonding_curve(curve_info["data"])
    except Exception:
        return None, None
    return curve, fee_bps


def gate_buy_quote_for_curve(curve, fee_bps: int, mint: str) -> dict:
    """Quote dict for a curve already read: the SOL cost of each
    exact preset token amount (curve integral, via sol_for_tokens)
    plus the curve progress. A preset too big for the curve
    reports sol=None individually, never a guessed price."""
    presets = []
    for tokens in GATE_PRESETS:
        lamports = sol_for_tokens(curve, fee_bps, tokens * TOKEN_BASE)
        presets.append({
            "tokens": tokens,
            "sol": (lamports_to_sol(lamports)
                    if lamports is not None else None),
        })
    return {"mint": mint, "presets": presets,
            "fee_bps": fee_bps,
            "curve_progress_pct": curve_progress_pct(curve)}


def gate_buy_quote(rpc, mint: str) -> dict | None:
    """Live quote for the Buy $GATE screen (curve read + quote).
    Returns None when the curve cannot be read or is complete
    (migrated) — the screen then shows "price unavailable" and
    disables buying."""
    curve, fee_bps = read_gate_curve(rpc, mint)
    if curve is None or curve.get("complete"):
        return None
    return gate_buy_quote_for_curve(curve, fee_bps, mint)


def tokens_out_for_sol(curve, fee_bps: int, sol: float) -> float | None:
    """Estimated whole $GATE out for `sol` SOL in, at the live
    curve (the custom-SOL estimate on the Buy $GATE screen).
    None for an invalid amount."""
    try:
        amount = float(sol)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return buy_quote(curve, fee_bps, sol_to_lamports(amount)) / TOKEN_BASE


# ------------------------------------------------------------------
# Packages + config
# ------------------------------------------------------------------

def bundles() -> list[dict]:
    """The three packages, as copies (callers must not mutate them)."""
    return [dict(b) for b in BUNDLES]


def get_bundle(bundle_id: str | None) -> dict | None:
    """One package by id, or None when it is not one of the three —
    there is no custom amount, so anything else is refused."""
    if not bundle_id:
        return None
    return _BUNDLE_BY_ID.get(str(bundle_id))


def quote_bundle_sol(price_usd_per_coin: float | None,
                     sol_price_usd: float | None,
                     coins: int) -> float | None:
    """SOL for exactly `coins` $GATE at the live prices, including
    the slippage buffer — or None when either price is missing. A
    package is NEVER priced from a guessed price: None refuses the
    buy. This is THE pricing rule — exact coin amounts at the live
    price (resolved by James 2026-11-13 ~15:20: the user always
    receives exactly the package's coins; the SOL paid is whatever
    the live quote says for exactly that many coins). The $3 / $15 /
    $30 CAD figures are expected launch prices, reference only."""
    if price_usd_per_coin is None or sol_price_usd is None:
        return None
    try:
        price = float(price_usd_per_coin)
        sol = float(sol_price_usd)
    except (TypeError, ValueError):
        return None
    if price <= 0 or sol <= 0 or coins <= 0:
        return None
    return coins * (price / sol) * (1.0 + SLIPPAGE_BUFFER_PCT / 100.0)


# ------------------------------------------------------------------
# Package pricing — THE ONE PLACE the purchase amount is decided.
# ------------------------------------------------------------------
# RESOLVED (James, 2026-11-13 ~15:20): exact coin amounts. The user
# always receives exactly 1M / 5M / 10M $GATE; the SOL paid is
# whatever the live quote says for exactly that many coins at the
# pump.fun curve price. The $3 / $15 / $30 CAD figures are the
# EXPECTED launch prices, shown as reference only — the SOL amount
# in the popup is the live quote. price_bundle_sol and
# bundle_send_base_units below are the only two functions that know
# the rule; the buy flow never computes a purchase amount itself.
def price_bundle_sol(bundle: dict, price_usd_per_coin: float | None,
                     sol_price_usd: float | None) -> float | None:
    """THE pricing rule: SOL the user pays for one package. One
    function — the buy flow, the quote endpoint, and the popup
    estimate all call this and nothing else. Returns None when a
    live price is missing, which refuses the buy."""
    return quote_bundle_sol(price_usd_per_coin, sol_price_usd,
                            bundle["coins"])


def bundle_send_base_units(bundle: dict, balance_base_units: int,
                           decimals: int) -> int | None:
    """THE send rule: how many base units of $GATE go to the dev
    wallet for one package purchase, from the on-chain balance after
    the buy. Always exactly the package amount — or None when the
    buy filled short of the full package, in which case the caller
    fails closed, credits nothing, and the bought $GATE stays in
    the user's app wallet."""
    bundle_units = int(bundle["coins"]) * (10 ** int(decimals))
    if int(balance_base_units) < bundle_units:
        return None
    return bundle_units


def pumpfun_url(mint: str) -> str:
    """Direct link to the coin on pump.fun — shown in the Buy $GATE
    popup so the user can see the coin there too."""
    return "https://pump.fun/coin/" + (mint or "").strip()


def get_gate_logo_url(mint: str, http_get=None) -> str:
    """The official $GATE logo: the coin's pump.fun metadata image
    (DexScreener info.imageUrl for the mint) once it resolves, else
    the SolEntry fox mascot. Never a broken image — any fetch
    failure falls back to the fox, and the template also falls back
    on an image error."""
    mint = (mint or "").strip()
    if not mint:
        return _FOX_LOGO_URL
    cached = _logo_cache.get(mint)
    if cached and time.time() - cached[1] < _LOGO_CACHE_SECONDS:
        return cached[0]
    url = None
    try:
        get = http_get or _http_get_json
        data = get(
            "https://api.dexscreener.com/tokens/v1/solana/" + mint)
        pairs = data if isinstance(data, list) else []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            image = (pair.get("info") or {}).get("imageUrl")
            if isinstance(image, str) and image.startswith("http"):
                url = image
                break
    except Exception:
        url = None
    result = url or _FOX_LOGO_URL
    _logo_cache[mint] = (result, time.time())
    return result


def _http_get_json(url: str, timeout: float = 3.0):
    """One small JSON GET (DexScreener). Short timeout: a logo must
    never slow a page render."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "SolEntry/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------
# The GATEkeeper bot row + the 8-digit support ID
# ------------------------------------------------------------------

def ensure_gatekeeper_bot(db: sqlite3.Connection,
                          user_id: int) -> dict:
    """The account's GATEkeeper bot row, creating it when it does
    not exist yet. Idempotent: calling it twice returns the same
    row. Mode real, never silent — GATEkeeper only ever acts when
    the user taps Buy; it never trades on its own. buy_size_pct 100:
    a package purchase IS the user's explicit instruction to spend
    the package's SOL amount, so the per-trade cap must not block
    it — the cap that protects the user here is the package price
    itself, shown before confirming. platform pump.fun: the only
    venue real execution is built against."""
    row = db.execute(
        "SELECT * FROM bots WHERE user_id = ? AND name = ?",
        (user_id, GATEKEEPER_BOT_NAME)).fetchone()
    if row is not None:
        return dict(row)
    import secrets
    db.execute(
        "INSERT INTO bots (user_id, name, strategy, coins, "
        "buy_size_pct, stop_loss_pct, mode, silent, max_open_trades, "
        "api_token_hash, platform) VALUES (?, ?, 'gatekeeper', "
        "'[\"SOL\"]', 100, 7, 'real', 0, 1, ?, 'pump.fun')",
        (user_id, GATEKEEPER_BOT_NAME, secrets.token_hex(16)))
    db.commit()
    row = db.execute(
        "SELECT * FROM bots WHERE user_id = ? AND name = ?",
        (user_id, GATEKEEPER_BOT_NAME)).fetchone()
    return dict(row)


def get_support_id(db: sqlite3.Connection, user_id: int) -> str:
    """The account's 8-digit support ID for tech support. REUSED
    from the signing bot (signing_bots.support_id): exactly 8
    digits, cryptographically random, UNIQUE across accounts, and
    IMMUTABLE — no route or code path can change it. GATEkeeper
    does not mint its own ID; it guarantees this one exists at the
    first fuel interaction (ensure_signing_bot creates it when the
    account has never traded) and surfaces it in the GATEkeeper
    card and the buy popup, so the user can quote it to support.
    The ID identifies the account for lookups only — it never
    exposes keys and never grants access by itself."""
    return ensure_signing_bot(db, user_id)["support_id"]


def lookup_user_by_support_id(db: sqlite3.Connection,
                              support_id) -> dict | None:
    """Support lookup: the account behind an 8-digit support ID,
    with its fuel summary. Read-only, and it returns NO key
    material — the ID identifies the account, it never grants
    access. Support actions still require the normal admin/auth
    checks; this is only the lookup. None for a malformed ID or
    an ID no account holds."""
    if not is_valid_support_id(support_id):
        return None
    row = db.execute(
        "SELECT u.id AS user_id, u.username AS username "
        "FROM signing_bots s JOIN users u ON u.id = s.user_id "
        "WHERE s.support_id = ?", (support_id,)).fetchone()
    if row is None:
        return None
    return {"user_id": row["user_id"], "username": row["username"],
            "support_id": support_id,
            "fuel": get_fuel(db, row["user_id"])}


# ------------------------------------------------------------------
# Fuel ledger — the truth is purchases minus uses
# ------------------------------------------------------------------

def _ensure_fuel_row(db: sqlite3.Connection, user_id: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO gate_fuel (user_id, trades_remaining, "
        "total_purchased, total_used) VALUES (?, 0, 0, 0)", (user_id,))
    db.commit()


def _first_purchase_at(db: sqlite3.Connection,
                       user_id: int) -> str | None:
    row = db.execute(
        "SELECT MIN(created_at) AS first_at FROM gate_fuel_purchases "
        "WHERE user_id = ? AND status = 'completed'",
        (user_id,)).fetchone()
    return row["first_at"] if row else None


def _overdrawn_depth_changed(db: sqlite3.Connection, user_id: int,
                             depth: int) -> bool:
    """True when this overdraw depth is new: the last logged
    overdrawn flag for this user names a different depth (or there
    is none). reconcile_fuel runs on every fuel read, so logging
    the same flag on every read flooded the activity log and pushed
    real trade history out of view. A parse failure fails open to
    logging — the anomaly is never silently dropped."""
    try:
        row = db.execute(
            "SELECT detail FROM activity WHERE user_id = ? "
            "AND kind = 'gate_fuel_overdrawn' ORDER BY id DESC "
            "LIMIT 1", (user_id,)).fetchone()
        if row is None:
            return True
        match = re.search(r"flag: (\d+) more", row["detail"])
        return match is None or int(match.group(1)) != depth
    except Exception:
        return True


def reconcile_fuel(db: sqlite3.Connection, user_id: int) -> dict:
    """Recompute the fuel balance from the persisted ledger and fix
    the cached counter when it drifted. The ledger wins, always.

    Backfill: a real trade_ledger row with no fuel-use row for its
    trade_id is a confirmed executed trade whose consume step never
    landed (a crash between execution and the consume write). It
    consumes one credit now, linked by trade_id and by the
    signature the ledger stored for the trade. Which rows count is
    decided by the ledger-rowid anchor set at the first purchase
    (gate_fuel.ledger_rowid_at_first_purchase): rows above the
    anchor are post-fuel, rows at or below it predate fuel and are
    never backfilled — fuel did not exist for them. A rowid anchor,
    not a timestamp: trade_ledger created_at is second-precision,
    so a pre-fuel trade can share the purchase's second. Without
    an anchor (a row predating the anchor column) the fallback is
    created_at strictly after the first purchase — erring in the
    user's favour. A truth below zero (more executed trades than
    purchased credits) is clamped for display and logged as an
    anomaly — flagged, never silently adjusted."""
    _ensure_fuel_row(db, user_id)
    first_at = _first_purchase_at(db, user_id)
    backfilled = 0
    if first_at is not None:
        anchor_row = db.execute(
            "SELECT ledger_rowid_at_first_purchase FROM gate_fuel "
            "WHERE user_id = ?", (user_id,)).fetchone()
        anchor = anchor_row[0] if anchor_row else None
        if anchor is not None:
            missing = db.execute(
                "SELECT trade_id, signature FROM trade_ledger "
                "WHERE user_id = ? "
                "AND mode = 'real' AND rowid > ? AND trade_id NOT IN "
                "(SELECT trade_id FROM gate_fuel_uses WHERE user_id = ? "
                "AND trade_id IS NOT NULL) "
                "AND trade_id NOT IN "
                "(SELECT trade_id FROM gate_fuel_sweep_trades "
                "WHERE user_id = ?)",
                (user_id, anchor, user_id, user_id)).fetchall()
        else:
            missing = db.execute(
                "SELECT trade_id, signature FROM trade_ledger "
                "WHERE user_id = ? "
                "AND mode = 'real' AND created_at > ? AND trade_id NOT IN "
                "(SELECT trade_id FROM gate_fuel_uses WHERE user_id = ? "
                "AND trade_id IS NOT NULL) "
                "AND trade_id NOT IN "
                "(SELECT trade_id FROM gate_fuel_sweep_trades "
                "WHERE user_id = ?)",
                (user_id, first_at, user_id, user_id)).fetchall()
        for trade_id, signature in missing:
            try:
                db.execute(
                    "INSERT INTO gate_fuel_uses (user_id, trade_id, "
                    "signature) VALUES (?, ?, ?)",
                    (user_id, trade_id, signature))
                backfilled += 1
            except sqlite3.IntegrityError:
                db.rollback()
                continue
        if backfilled:
            db.commit()
            _log_activity(
                db, user_id, "gate_fuel_reconcile",
                f"GATEkeeper reconciled fuel: {backfilled} confirmed "
                f"real trade(s) had no fuel-use row — counted from the "
                f"trade ledger.")
    purchased = db.execute(
        "SELECT COALESCE(SUM(trades), 0) AS total FROM "
        "gate_fuel_purchases WHERE user_id = ? AND status = 'completed'",
        (user_id,)).fetchone()["total"]
    used = db.execute(
        "SELECT COUNT(*) AS total FROM gate_fuel_uses WHERE user_id = ?",
        (user_id,)).fetchone()["total"]
    truth = int(purchased) - int(used)
    cached = db.execute(
        "SELECT trades_remaining, total_purchased, total_used "
        "FROM gate_fuel WHERE user_id = ?", (user_id,)).fetchone()
    if cached is None:
        _ensure_fuel_row(db, user_id)
        cached = {"trades_remaining": 0, "total_purchased": 0,
                  "total_used": 0}
    # The cached counter stores the CLAMPED truth (never below 0),
    # so compare against the clamped value: comparing the raw truth
    # (-12 when confirmed trades outran purchases) against a cached
    # 0 mismatched on EVERY read, logging a reconcile row each time
    # and flooding the activity log.
    if int(cached["trades_remaining"]) != max(0, truth) or \
            int(cached["total_purchased"]) != int(purchased) or \
            int(cached["total_used"]) != int(used):
        db.execute(
            "UPDATE gate_fuel SET trades_remaining = ?, "
            "total_purchased = ?, total_used = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (max(0, truth), int(purchased), int(used), user_id))
        db.commit()
        _log_activity(
            db, user_id, "gate_fuel_reconcile",
            f"GATEkeeper reconciled fuel: cached count "
            f"{cached['trades_remaining']} did not match the ledger "
            f"(purchased {purchased}, used {used}) — ledger wins, "
            f"balance set to {max(0, truth)}.")
    if truth < 0 and _overdrawn_depth_changed(db, user_id, abs(truth)):
        _log_activity(
            db, user_id, "gate_fuel_overdrawn",
            f"GATEkeeper flag: {abs(truth)} more confirmed real trade(s) "
            f"than purchased fuel credits — fuel is 0 until the user "
            f"refills. No credits were invented.")
    return {"trades_remaining": max(0, truth),
            "total_purchased": int(purchased),
            "total_used": int(used)}


def get_fuel(db: sqlite3.Connection, user_id: int) -> dict:
    """The fuel balance — always the ledger truth (reconciled on
    every read). {"trades_remaining", "total_purchased",
    "total_used"}. This is the number the countdown shows."""
    return reconcile_fuel(db, user_id)


def has_fuel(db: sqlite3.Connection, user_id: int) -> bool:
    """True when the ledger truth says at least one trade credit
    remains."""
    return get_fuel(db, user_id)["trades_remaining"] > 0


def open_real_positions(db: sqlite3.Connection, user_id: int) -> int:
    """How many open real bot positions this user holds (amount > 0).
    Manual holdings are not positions — the exit-all sweep only
    exits bot-managed positions (see run_exit_all_sweep)."""
    row = db.execute(
        "SELECT COUNT(*) AS n FROM bot_positions "
        "WHERE user_id = ? AND mode = 'real' AND amount > 0",
        (user_id,)).fetchone()
    return int(row["n"]) if row else 0


def exit_all_pending(db: sqlite3.Connection, user_id: int) -> bool:
    """True when the last-credit exit-all rule is live for this user
    (James, 2026-11-24): exactly 1 fuel credit left AND at least one
    open real bot position. That last credit is reserved to exit
    every open position in one sweep — no single trade may consume
    it while this is true (see the reserve checks in routes.py and
    silent_loop.py). With no open real positions there is nothing
    to protect, so the reserve does not bind: a user at 1 credit
    with no positions can still trade, instead of being stuck until
    they refill."""
    if get_fuel(db, user_id)["trades_remaining"] != 1:
        return False
    return open_real_positions(db, user_id) > 0


def record_sweep_trade(db: sqlite3.Connection, user_id: int,
                       sweep_id: str, trade_id: str) -> None:
    """Mark one trade_ledger row as part of an exit-all sweep, so
    reconcile_fuel's backfill never counts that sell as its own
    credit — the sweep costs exactly one credit total, recorded
    separately by consume_fuel. INSERT OR IGNORE: the same trade
    is never recorded twice."""
    if not trade_id:
        return
    db.execute(
        "INSERT OR IGNORE INTO gate_fuel_sweep_trades "
        "(trade_id, user_id, sweep_id) VALUES (?, ?, ?)",
        (trade_id, user_id, sweep_id))
    db.commit()


def consume_fuel(db: sqlite3.Connection, user_id: int,
                 trade_id: str | None, signature: str | None) -> bool:
    """Consume ONE credit for a confirmed executed trade. The
    signature is the proof of execution — a trade with no signature
    was never confirmed on-chain, so it consumes nothing and is
    flagged. The use row and the cached decrement happen in one
    transaction; the signature UNIQUE (and trade_id UNIQUE) make a
    double-count impossible, so calling this twice for the same
    trade is a harmless no-op that returns True. When the balance
    hits zero the fuel gate runs (bots off)."""
    if not signature:
        _log_activity(
            db, user_id, "gate_fuel_unrecorded",
            "GATEkeeper flag: a real trade executed with no signature "
            "to count against fuel — no credit was consumed, so the "
            "balance may read one high until the next reconciliation.")
        return False
    _ensure_fuel_row(db, user_id)
    before = get_fuel(db, user_id)
    try:
        db.execute(
            "INSERT INTO gate_fuel_uses (user_id, trade_id, signature) "
            "VALUES (?, ?, ?)", (user_id, trade_id, signature))
    except sqlite3.IntegrityError:
        db.rollback()
        row = db.execute(
            "SELECT id FROM gate_fuel_uses WHERE user_id = ? AND "
            "(signature = ? OR (trade_id IS NOT NULL AND trade_id = ?))",
            (user_id, signature, trade_id)).fetchone()
        if row is not None:
            return True
        raise
    db.execute(
        "UPDATE gate_fuel SET trades_remaining = "
        "MAX(0, trades_remaining - 1), total_used = total_used + 1, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (user_id,))
    db.commit()
    if before["trades_remaining"] <= 1:
        enforce_fuel_gate(db, user_id)
    return True


def credit_purchase(db: sqlite3.Connection, user_id: int, bundle: dict,
                    sol_spent: float, buy_signature: str | None,
                    send_signature: str,
                    coins_sent: int | None = None) -> None:
    """Credit a completed package purchase: the purchase row and the
    fuel grant, in one transaction — a purchase is never half
    credited. Called ONLY after the dev-wallet send is confirmed
    on-chain. `coins_sent` is the $GATE that actually went to the
    dev wallet — always the package amount (exact-coins rule); the
    row records the truth, not the package label. The first purchase
    also sets the ledger-rowid anchor (see reconcile_fuel): the
    highest trade_ledger rowid at this moment, so later backfills
    can never count a trade that predates fuel."""
    if coins_sent is None:
        coins_sent = int(bundle["coins"])
    _ensure_fuel_row(db, user_id)
    db.execute(
        "INSERT INTO gate_fuel_purchases (user_id, bundle_id, coins, "
        "trades, sol_spent, buy_signature, send_signature, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')",
        (user_id, bundle["id"], int(coins_sent), bundle["trades"],
         float(sol_spent), buy_signature, send_signature))
    anchor_row = db.execute(
        "SELECT ledger_rowid_at_first_purchase FROM gate_fuel "
        "WHERE user_id = ?", (user_id,)).fetchone()
    if anchor_row is None or anchor_row[0] is None:
        max_rowid = db.execute(
            "SELECT COALESCE(MAX(rowid), 0) AS m FROM trade_ledger "
            "WHERE user_id = ?", (user_id,)).fetchone()["m"]
        db.execute(
            "UPDATE gate_fuel SET ledger_rowid_at_first_purchase = ? "
            "WHERE user_id = ?", (int(max_rowid), user_id))
    db.execute(
        "UPDATE gate_fuel SET trades_remaining = trades_remaining + ?, "
        "total_purchased = total_purchased + ?, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (bundle["trades"], bundle["trades"], user_id))
    db.commit()
    _log_activity(
        db, user_id, "gate_fuel_purchase",
        f"GATEkeeper credited {bundle['trades']} trades "
        f"({bundle['label']} package, {int(coins_sent):,} $GATE sent to "
        f"the dev wallet). Send signature: {send_signature[:12]}...")


def redeem_promo_code(db: sqlite3.Connection, user_id: int,
                      code: str) -> dict | None:
    """Redeem a one-time promo code from the coupon box in
    Settings (James, 2026-11-24). Returns {"trades",
    "trades_remaining"} on success, None when the code is unknown
    or already used up — the caller shows the same generic error
    either way, so the response never hints at which codes exist.

    Two scopes (promo_codes.scope):
    - 'global_once': usable at most max_uses times IN TOTAL
      across all users (BetaGate1994: max_uses 1). Enforced by
      ONE conditional UPDATE (uses = uses + 1 WHERE code = ? AND
      uses < max_uses) — the rowcount decides.
    - 'per_account_once': every account can use it once ("Still
      on the gate": the signup code, 50 trades). Enforced by the
      UNIQUE(user_id, code) row in promo_redemptions — the
      INSERT fails when this account already redeemed it.
    Both guards run inside one BEGIN IMMEDIATE transaction, so
    two simultaneous redeems cannot both succeed: the second
    writer re-evaluates after the first commits and finds either
    uses == max_uses or the duplicate redemption row. No account
    can ever redeem the same code twice, whatever the scope.
    The match is EXACT (case-sensitive, no trimming, spaces
    significant): the code is BetaGate1994 as written — and
    "Still on the gate" is lowercase gate, no $ and no
    question mark.

    The grant lands in gate_fuel_purchases as a completed
    purchase with source 'promo' and promo_code set — visible
    promo credit, never disguised as a paid package — so the
    ledger (purchases minus uses) stays the truth, the
    trades-left chip reads it, and reconcile_fuel never flags it
    as overdraw. No SOL moves, no $GATE moves: promo credit is
    testing fuel only, strictly to test mainnet before using
    money to test."""
    if not code:
        return None
    _ensure_fuel_row(db, user_id)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT trades, scope, max_uses FROM promo_codes "
            "WHERE code = ?", (code,)).fetchone()
        if row is None:
            db.rollback()
            return None
        trades = int(row["trades"])
        scope = row["scope"]
        if scope == "global_once":
            cur = db.execute(
                "UPDATE promo_codes SET uses = uses + 1 "
                "WHERE code = ? AND uses < max_uses", (code,))
            if cur.rowcount != 1:
                db.rollback()
                return None
        elif scope == "per_account_once":
            pass  # the UNIQUE redemption row below is the guard
        else:
            db.rollback()
            return None
        try:
            db.execute(
                "INSERT INTO promo_redemptions (code, user_id, "
                "trades) VALUES (?, ?, ?)", (code, user_id, trades))
        except sqlite3.IntegrityError:
            db.rollback()
            return None
        if scope == "per_account_once":
            db.execute(
                "UPDATE promo_codes SET uses = uses + 1 "
                "WHERE code = ?", (code,))
        db.execute(
            "INSERT INTO gate_fuel_purchases (user_id, bundle_id, "
            "coins, trades, sol_spent, buy_signature, send_signature, "
            "status, source, promo_code) VALUES (?, 'promo', 0, ?, 0, "
            "NULL, ?, 'completed', 'promo', ?)",
            (user_id, trades, f"promo:{code}", code))
        anchor_row = db.execute(
            "SELECT ledger_rowid_at_first_purchase FROM gate_fuel "
            "WHERE user_id = ?", (user_id,)).fetchone()
        if anchor_row is None or anchor_row[0] is None:
            max_rowid = db.execute(
                "SELECT COALESCE(MAX(rowid), 0) AS m FROM trade_ledger "
                "WHERE user_id = ?", (user_id,)).fetchone()["m"]
            db.execute(
                "UPDATE gate_fuel SET ledger_rowid_at_first_purchase "
                "= ? WHERE user_id = ?", (int(max_rowid), user_id))
        db.execute(
            "UPDATE gate_fuel SET trades_remaining = trades_remaining "
            "+ ?, total_purchased = total_purchased + ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (trades, trades, user_id))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    scope_note = ("one-time code, now used"
                  if scope == "global_once" else
                  "once-per-account code, this account has used it")
    _log_activity(
        db, user_id, "gate_fuel_promo",
        f"GATEkeeper credited {trades} trades (promo code {code} — "
        f"{scope_note}).")
    return {"trades": trades,
            "trades_remaining": get_fuel(db, user_id)["trades_remaining"]}


def enforce_fuel_gate(db: sqlite3.Connection, user_id: int) -> bool:
    """The fuel gate: True when the user has fuel to trade. When
    the ledger truth is zero, ALL of the user's real bots are turned
    off (silent = 0) until they refill — paper bots are never
    touched, paper trading stays free. Only bots that actually
    transition 1 -> 0 are logged, so the log is not spammed one row
    per loop pass."""
    if has_fuel(db, user_id):
        return True
    turning_off = db.execute(
        "SELECT id, name FROM bots WHERE user_id = ? AND mode = 'real' "
        "AND silent = 1", (user_id,)).fetchall()
    if turning_off:
        db.execute(
            "UPDATE bots SET silent = 0 WHERE user_id = ? "
            "AND mode = 'real' AND silent = 1", (user_id,))
        db.commit()
        for bot in turning_off:
            _log_activity(
                db, user_id, "gate_fuel_bot_off",
                f"GATEkeeper turned off real bot '{bot['name']}' — out "
                f"of fuel (0 trades left). Buy $GATE to turn it back "
                f"on.")
    return False


def gatekeeper_status(db: sqlite3.Connection, user_id: int) -> dict:
    """Everything the GATEkeeper card and the countdown show: the
    ledger-truth fuel balance, the support ID to quote to tech
    support, and how many of the user's real bots are silent-on.
    No key material, ever."""
    fuel = get_fuel(db, user_id)
    support_id = get_support_id(db, user_id)
    bots_on = db.execute(
        "SELECT COUNT(*) AS n FROM bots WHERE user_id = ? "
        "AND mode = 'real' AND silent = 1", (user_id,)).fetchone()["n"]
    return {"trades_remaining": fuel["trades_remaining"],
            "total_purchased": fuel["total_purchased"],
            "total_used": fuel["total_used"],
            "support_id": support_id,
            "real_bots_on": int(bots_on)}


# ------------------------------------------------------------------
# Package purchase: buy $GATE, send the full package to the dev
# wallet, credit the trades. Fail-closed at every step.
# ------------------------------------------------------------------

def send_bundle_to_dev_wallet(db: sqlite3.Connection, cfg: Config,
                              user_id: int, wallet_pubkey: str,
                              mint: str, dev_wallet: str,
                              coins_base_units: int,
                              rpc_client=None) -> tuple[str | None,
                                                        str | None]:
    """Send exactly `coins_base_units` $GATE to the dev wallet.
    Returns (signature, None) when the transfer landed on-chain,
    else (None, plain reason).

    Verify-before-retry: send_and_confirm re-checks the known
    signature on-chain before reporting a failure, so a reported
    failure means the first attempt did NOT land — a retry cannot
    double-send. The retry happens ONCE, only on transient RPC
    errors, with a fresh blockhash (a new signature). Any other
    failure (node rejection, refusal, bad address) fails at once
    with no retry.

    KEY SECURITY: the wallet key is decrypted into a mutable buffer
    ONLY for this one signature and wiped IN PLACE in a finally
    block the moment signing is done — success or failure. The
    signed transaction is sent only after the wipe. No key material
    is ever logged, stored, or sent anywhere."""
    rpc = rpc_client if rpc_client is not None \
        else rpc_client_for_url(cfg, cfg.rpc_url)

    def _attempt() -> tuple[str | None, str | None, bool]:
        """One send attempt. Returns (signature, reason, retryable).
        A TokenSendRefusal (bad balance, bad address, down RPC at
        inspect time) is not retryable — it would fail the same way.
        """
        try:
            inspection = token_send_mod.inspect_token(
                rpc, wallet_pubkey, mint, dev_wallet)
        except token_send_mod.TokenSendRefusal as exc:
            return None, str(exc), False
        if inspection["balance"] < coins_base_units:
            return None, ("You hold less $GATE than the package now — "
                          "the buy did not fill the full package."), False
        required_sol = (token_send_mod.TX_FEE_LAMPORTS
                        + inspection["rent_lamports"])
        try:
            sol_balance = rpc.get_balance_lamports(wallet_pubkey)
        except Exception:
            return None, ("Could not check your SOL balance right now "
                          "— try again."), False
        if sol_balance < required_sol:
            return None, ("Not enough SOL for the network fee to send "
                          "the package."), False
        try:
            instructions = token_send_mod.build_token_send_instructions(
                inspection, coins_base_units)
        except token_send_mod.TokenSendRefusal as exc:
            return None, str(exc), False
        try:
            # The key lives in a mutable buffer for one signature
            # only, and is wiped IN PLACE the moment signing is
            # done — before the transaction is even sent. The
            # finally block guarantees the wipe on every path,
            # including a signing failure.
            with temporary_wallet_secret(
                    db, user_id, cfg.encryption_key) as secret:
                if secret is None:
                    raise ValueError("No app wallet.")
                seed = bytearray(secret[:32])
                try:
                    blockhash = rpc.get_latest_blockhash()
                    signed_b64, signature = \
                        token_send_mod.sign_token_send(
                            seed, wallet_pubkey, instructions, blockhash)
                finally:
                    wipe_secret(seed)
        except Exception as exc:
            return None, str(exc)[:200], False
        tx_sig, reason = send_and_confirm(rpc, signed_b64, signature)
        if tx_sig is not None:
            return tx_sig, None, False
        return None, (reason or "transaction not confirmed"), \
            (reason in _TRANSIENT_SEND_REASONS)

    sig, reason, retryable = _attempt()
    if sig is not None:
        return sig, None
    if retryable:
        _log_activity(
            db, user_id, "gate_fuel_send_retry",
            f"GATEkeeper retrying the dev-wallet send once: the first "
            f"attempt failed ({reason}) and the chain confirmed it did "
            f"not land — a retry cannot double-send.")
        sig, reason, _retryable = _attempt()
        if sig is not None:
            return sig, None
    return None, reason


def buy_gate_bundle(db: sqlite3.Connection, cfg: Config, user_id: int,
                    bundle_id: str | None,
                    price_usd_per_coin: float | None,
                    sol_price_usd: float | None,
                    real_executor=None,
                    rpc_client=None,
                    curve_complete=None,
                    jupiter_executor=None) -> dict:
    """Buy one package: swap SOL for $GATE ON-CHAIN, send the
    purchased $GATE straight to the dev wallet up front, and credit
    the package's trades. Returns {"ok": True, "bundle_id",
    "coins", "trades_credited", "trades_remaining", "buy_signature",
    "send_signature", "sol_spent"} or {"ok": False, "error": <plain
    reason>}.

    The amounts come ONLY from the isolated pricing rule
    (price_bundle_sol / bundle_send_base_units) — this function
    never computes a purchase amount itself. The rule is exact
    coin amounts at live market price (resolved by James
    2026-11-13 ~15:20): the user always receives exactly the
    package's coins; the SOL paid is whatever the live quote says.

    Venue — the SAME decision as coin_buy: a graduated coin trades
    through Jupiter, a still-bonding coin through the pump.fun
    executor (execute_real_trade). $GATE is still bonding, so the
    pump.fun executor is the live path; the Jupiter branch keeps
    the flow working if $GATE graduates later. An unreadable curve
    refuses plainly — the venue is never guessed. `curve_complete`
    is injectable for tests (a stubbed executor cannot read a real
    curve); in production it is always read live here.

    Fail-closed, in order: unknown package; dev wallet or mint not
    configured; no live price (never buy at a guessed price);
    no app wallet / not enough SOL; the venue cannot be read; the
    swap refused or unconfirmed; the buy filled short of what the
    active pricing rule requires (the bought $GATE stays in the
    user's app wallet — it is theirs, and a retry reuses it); the
    dev-wallet send failed after its one retry. In every failure
    case NO trades are credited and nothing is sent. The purchase
    itself is never a fuel-gated trade: buying fuel does not cost
    fuel."""
    bundle = get_bundle(bundle_id)
    if bundle is None:
        return {"ok": False,
                "error": "That package doesn't exist — pick 1M, 5M or "
                         "10M $GATE."}
    mint = gate_mint(cfg)
    dev_wallet = (cfg.fee_recipient_pubkey or "").strip()
    if not is_valid_address(mint):
        return {"ok": False,
                "error": "$GATE is not configured yet — the coin's "
                         "address has not been set. Nothing was bought."}
    if not is_valid_address(dev_wallet):
        return {"ok": False,
                "error": "The dev wallet is not set up yet — the "
                         "package cannot be sent. Nothing was bought."}
    # The support ID is guaranteed to exist from the first fuel
    # interaction, so the user can always quote it to support.
    get_support_id(db, user_id)
    # The SOL amount comes ONLY from the isolated pricing rule — this
    # function never computes it. None refuses the buy (no live
    # price, never a guessed one).
    amount_sol = price_bundle_sol(bundle, price_usd_per_coin,
                                  sol_price_usd)
    if amount_sol is None:
        return {"ok": False,
                "error": "No live $GATE price right now — the buy "
                         "needs a current price. Nothing was bought."}
    wallet = db.execute(
        "SELECT pubkey FROM wallets WHERE user_id = ?",
        (user_id,)).fetchone()
    if not wallet:
        return {"ok": False, "error": "No app wallet."}
    wallet_pubkey = wallet["pubkey"]
    rpc = rpc_client if rpc_client is not None \
        else rpc_client_for_url(cfg, cfg.rpc_url)
    try:
        balance_lamports = rpc.get_balance_lamports(wallet_pubkey)
    except Exception:
        return {"ok": False,
                "error": "Could not check your SOL balance right now — "
                         "try again."}
    if balance_lamports < int(amount_sol * 1_000_000_000):
        return {"ok": False,
                "error": "Not enough SOL in your app wallet for this "
                         "package at the live price."}
    bot = ensure_gatekeeper_bot(db, user_id)
    # Venue, same as coin_buy: graduated -> Jupiter, still bonding
    # -> the pump.fun executor. Unreadable -> refused plainly.
    if curve_complete is None:
        try:
            curve_complete = is_curve_complete(rpc, mint)
        except Exception:
            curve_complete = None
    if curve_complete is None:
        return {"ok": False,
                "error": "Bonding curve unavailable — the trade venue "
                         "could not be determined. Nothing was bought."}
    if curve_complete:
        jup_exec = jupiter_executor or execute_jupiter_swap
        result = jup_exec(
            bot, user_id, mint, "buy", amount_sol, db, cfg,
            decision_price_usd=price_usd_per_coin or 0.0)
    else:
        executor = real_executor or execute_real_trade
        # Straight through the pump.fun program with SOL — never a
        # middleman. $GATE is a pump.fun coin; the pump.fun executor
        # is the venue for it while it is still bonding, on devnet
        # and on mainnet alike (the executor fix lifted the
        # devnet-only gate).
        result = executor(bot, user_id, mint, "buy", amount_sol,
                          price_usd_per_coin or 0.0, db, cfg)
    if result.get("status") != "executed":
        return {"ok": False,
                "error": "No $GATE was bought — "
                         f"{result.get('reason', 'real execution failed')}. "
                         "Nothing was sent and no trades were credited."}
    buy_signature = result.get("signature")
    sol_spent = result.get("sol_spent")
    try:
        sol_spent = float(sol_spent) if sol_spent is not None \
            else amount_sol
    except (TypeError, ValueError):
        sol_spent = amount_sol
    # The buy must have filled the FULL package before anything is
    # sent: read the holding on-chain (never the app's table) and
    # compare in base units. A short fill fails closed — the bought
    # $GATE stays in the app wallet and a retry reuses it.
    try:
        inspection = token_send_mod.inspect_token(
            rpc, wallet_pubkey, mint, dev_wallet)
    except token_send_mod.TokenSendRefusal as exc:
        return {"ok": False,
                "error": f"The $GATE buy went through (signature "
                         f"{str(buy_signature)[:12]}...) but the holding "
                         f"could not be checked: {exc} No $GATE was sent "
                         f"and no trades were credited."}
    decimals = int(inspection["decimals"])
    # How much $GATE goes to the dev wallet comes ONLY from the
    # isolated send rule — never computed here. None fails closed.
    coins_base_units = bundle_send_base_units(
        bundle, inspection["balance"], decimals)
    if coins_base_units is None:
        short_error = ("The buy filled short of the full package "
                       "at the live price, so nothing was sent and "
                       "no trades were credited.")
        return {"ok": False,
                "error": short_error + " The $GATE you bought is "
                         "still in your app wallet — try the package "
                         "again and it will top it up."}
    send_sig, send_error = send_bundle_to_dev_wallet(
        db, cfg, user_id, wallet_pubkey, mint, dev_wallet,
        coins_base_units, rpc_client=rpc)
    if send_sig is None:
        _log_activity(
            db, user_id, "gate_fuel_purchase_failed",
            f"GATEkeeper could not send the {bundle['label']} package to "
            f"the dev wallet: {send_error} No trades were credited. The "
            f"$GATE you bought is still in your app wallet.")
        return {"ok": False,
                "error": f"The $GATE was bought but the send to the dev "
                         f"wallet failed: {send_error} No trades were "
                         f"credited. The $GATE is still in your app "
                         f"wallet — try again."}
    credit_purchase(db, user_id, bundle, sol_spent, buy_signature,
                    send_sig,
                    coins_sent=coins_base_units // (10 ** decimals))
    fuel = get_fuel(db, user_id)
    return {"ok": True,
            "bundle_id": bundle["id"],
            "coins": bundle["coins"],
            "coins_sent": coins_base_units // (10 ** decimals),
            "trades_credited": bundle["trades"],
            "trades_remaining": fuel["trades_remaining"],
            "buy_signature": buy_signature,
            "send_signature": send_sig,
            "sol_spent": sol_spent}
