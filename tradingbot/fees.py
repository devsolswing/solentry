"""Trading fees: 3.65% on every trade, paid to ONE recipient wallet.

The fee (James, 2026-11-10):
  * 3.65% of every trade, going forward only — past trades are never
    charged, and the fee total starts at zero for everyone at deploy.
  * The fee is taken in SOL for pump.fun trades. Paper trades charge
    the same 3.65% in the paper ledger (deducted from the trade
    value) and record their SOL equivalent at the trade-time SOL
    price; a paper fee recorded without a usable SOL price is
    converted at the live price when displayed, never at a guessed
    rate.
  * Paper fees are NEVER counted in the displayed "fees paid"
    balances (James, 2026-11-10): the per-user total and the
    app-wide total count only REAL fees — rows from executed real
    trades (mode='real', with a transaction signature). Paper
    money is play money, and counting its SOL equivalent as SOL
    actually paid inflated both totals. Paper fee rows stay
    recorded in the ledger (mode='paper') for the paper P&L, but
    the SOL totals and the fees dropdown show real fees only.
  * The fee goes to ONE wallet for the whole app — the recipient in
    the FEE_RECIPIENT_PUBKEY env var (James's own wallet, set in
    Railway). There is no app-generated Treasury wallet any more:
    the generated wallet is retired (its row is purged at startup
    by db._purge_generated_fee_wallets) and must never receive
    anything. The recipient address is never shown on any page
    and never logged.
  * Real (devnet) trades: the fee travels in the SAME transaction
    as the trade itself — TWO System-program transfers, appended
    after the pump.fun buy/sell instruction (see THE SPLIT
    below). Either the whole transaction confirms (trade + both
    transfers) or neither happens, so a fee is never taken from
    a trade that did not execute, and never taken twice for one
    trade (fee rows are unique per trade signature).
  * THE SPLIT (James, 2026-11-10/11): the 3.65% fee is a MOCK for
    the real mainnet total — pump.fun's fee (~1.9%) + SolEntry's
    fee (1.7% of the trade). On DEVNET the fee is collected as
    two transfers in the same transaction as the trade: (a) 1.7%
    of the trade to the treasury — James's own wallet, the
    FEE_RECIPIENT_PUBKEY (there is NO separate treasury wallet);
    (b) the rest, 1.95% of the trade, BURNED to the Solana
    incinerator (1nc1nerator11111111111111111111111111111111), a
    well-known unspendable address. Exact integer math: treasury
    = amount x 170 // 10000, fee = amount x 365 // 10000, burn =
    fee - treasury, so nothing leaks to rounding.
  * THE BURN IS DEVNET-ONLY (James, 2026-11-11): the burn
    transfer is gated on the app network being devnet
    (Config.network). On mainnet there is NO burn — the rest is
    taken by pump.fun via the program instead. The mainnet
    collection (and the mainnet fee — 1.9% to the treasury, per
    James 2026-11-11) is a launch-time change, NOT part of this
    build — but this build cannot burn on mainnet:
    fee_split_lamports returns a zero burn for any network other
    than devnet, so on mainnet the burn transfer is never built.
  * THE DISPLAY shows three separate numbers, per user and
    app-wide (home dropdown, /api/fees): (a) SolEntry's fee =
    1.7% of the trade — the treasury share, recorded fee x
    170/365 for rows recorded before the split; (b) pump.fun's
    fee = the ACTUAL program fee paid on each trade, derived
    from the trade's amounts and the live global fee_bps
    (pumpfun.buy_program_fee / sell_program_fee) and recorded
    per trade — never James's ~1.9% estimate, never hardcoded;
    (c) the burned amount = 1.95% of the trade. The split is
    recorded per trade (treasury / burn / pump_fee, in SOL and
    in lamports) so the display sums real per-trade numbers.
  * The signing bot's destination restriction (executor.py) keeps
    exactly TWO narrow exceptions for this: a System-program
    instruction is allowed only when it is one of the two fee
    transfers — the treasury transfer to the fee recipient for
    exactly the treasury lamports, or the burn transfer to the
    incinerator for exactly the burn lamports, each appearing
    exactly once. Any other transfer, destination, or amount is
    still refused.
  * Fail-closed: when FEE_RECIPIENT_PUBKEY is unset, empty, or not
    a valid base58 32-byte pubkey, real trades refuse and the
    donate flow refuses — never a fallback wallet, never a guessed
    address.

Mainnet settlement rule (James, 2026-11-10): when the app launches
on mainnet, after selling a coin the proceeds are converted to SOL
and held as SOL — never cash, never USDC. On devnet this is already
how sells settle: pump.fun pays SOL to the seller's wallet, so
there is no cash leg to convert. MAINNET_SETTLEMENT is the rule's
single home in code; the README's "Trading fees" section states it
in plain words.
"""

from __future__ import annotations

from .solana import is_valid_address

# 3.65% — basis points, so fee math is exact integer math in
# lamports (amount * 365 // 10000, rounded down).
#
# MAINNET MODEL (James, 2026-11-11 — IMPLEMENTED for Jupiter
# swaps in jupiter_executor.py, behind the JUPITER_REAL_SWAPS
# gate): on mainnet the app fee is 1.9% of the trade, ALL of it
# to the Treasury dev wallet (FEE_RECIPIENT_PUBKEY — the address
# James already supplied; no new wallet). Pump.fun's fee is
# separate (taken by pump.fun via the program, accurately
# accounted in the display). No burn on mainnet — the mainnet
# fee travels as its own System transfer to the treasury (the
# Jupiter swap transaction is built by Jupiter, so the fee
# cannot ride inside it; it is sent right after the swap
# confirms, from the unspent fee portion, and recorded once by
# swap signature). mainnet_fee_split_lamports is the single
# home of that split.
FEE_BPS = 365
FEE_PCT = 3.65

# Mainnet Jupiter fee: 1.9% of the trade, ALL to the treasury.
MAINNET_FEE_BPS = 190
MAINNET_FEE_PCT = 1.9

# Treasury share of the fee, for the DISPLAYED balances only
# (James, 2026-11-10): the treasury's share is 1.7% OF THE TRADE,
# not 1.7% of the fee. The fee itself stays 3.65% of the trade and
# is still collected in full, in the same transaction, to
# FEE_RECIPIENT_PUBKEY; only the fees-paid balances DISPLAY the
# treasury's share: recorded fee × 170/365 (so a 1 SOL trade shows
# 0.017 SOL, a 0.2 SOL trade shows 0.0034 SOL). The factor lives in
# fee_row_sol below, the one function every display reads through —
# the per-user total, the app-wide total, /api/fees, and each row
# in the dropdown all show the same share and cannot disagree.
# The ledger rows themselves stay recorded at the full 3.65% fee.
TREASURY_SHARE_BPS_OF_TRADE = 170

# Burn share of the trade: 1.95% (3.65% total minus 1.7%
# treasury) — the rest of the fee, burned on devnet only.
BURN_SHARE_BPS_OF_TRADE = 195

# The Solana incinerator: a well-known unspendable address. The
# burn transfer goes here and only here — never a user-supplied
# or configured address, never a wallet anyone controls. Verified
# as a valid base58 32-byte address before use.
INCINERATOR_PUBKEY = "1nc1nerator11111111111111111111111111111111"

# Mainnet settlement: sells convert to SOL and stay in SOL.
MAINNET_SETTLEMENT = "SOL"


def treasury_lamports(amount_lamports: int) -> int:
    """1.7% of a lamport amount, integer math, rounded down — the
    treasury's (James's wallet's) share of the trade. Zero or
    negative amounts have no treasury share."""
    if amount_lamports is None or amount_lamports <= 0:
        return 0
    return int(amount_lamports) * TREASURY_SHARE_BPS_OF_TRADE // 10000


def burn_lamports(amount_lamports: int, network: str) -> int:
    """1.95% of a lamport amount — the share BURNED to the
    incinerator. DEVNET ONLY (James, 2026-11-11): any network
    other than devnet returns 0, so this build can never burn on
    mainnet. Exact: fee minus treasury, so on devnet treasury +
    burn is always exactly the fee — nothing leaks to rounding.
    On a non-devnet network the rest of the fee is simply not
    transferred (it stays in the wallet); the mainnet collection
    is a launch-time change, not this build."""
    if (network or "devnet").lower() != "devnet":
        return 0
    return fee_lamports(amount_lamports) \
        - treasury_lamports(amount_lamports)


def fee_split_lamports(amount_lamports: int, network: str) -> dict:
    """The 3.65% fee split for one trade amount, in lamports:
    {"fee_lamports", "treasury_lamports", "burn_lamports"}.
    Treasury is 1.7% of the trade; burn is the rest (1.95%) and
    is devnet-only — see burn_lamports. `network` is the app's
    existing network config (Config.network), never a second
    hardcoded network name."""
    fee = fee_lamports(amount_lamports)
    treasury = treasury_lamports(amount_lamports)
    burn = burn_lamports(amount_lamports, network)
    return {"fee_lamports": fee, "treasury_lamports": treasury,
            "burn_lamports": burn}


def fee_lamports(amount_lamports: int) -> int:
    """3.65% of a lamport amount, integer math, rounded down. Zero
    or negative amounts have no fee."""
    if amount_lamports is None or amount_lamports <= 0:
        return 0
    return int(amount_lamports) * FEE_BPS // 10000


def mainnet_fee_lamports(amount_lamports: int) -> int:
    """1.9% of a lamport amount, integer math, rounded down — the
    MAINNET Jupiter fee (James, 2026-11-11). Zero or negative
    amounts have no fee."""
    if amount_lamports is None or amount_lamports <= 0:
        return 0
    return int(amount_lamports) * MAINNET_FEE_BPS // 10000


def mainnet_fee_split_lamports(amount_lamports: int) -> dict:
    """The mainnet Jupiter fee split for one trade amount, in
    lamports: {"fee_lamports", "treasury_lamports",
    "burn_lamports"}. The fee is 1.9% of the trade, ALL of it to
    the treasury (FEE_RECIPIENT_PUBKEY); the burn is ALWAYS zero
    on mainnet — there is no burn on mainnet, ever. This is the
    single home of the mainnet split; the Jupiter executor
    (jupiter_executor.py) reads it, and the display records the
    treasury share from the fee row (see fee_row_split)."""
    fee = mainnet_fee_lamports(amount_lamports)
    return {"fee_lamports": fee, "treasury_lamports": fee,
            "burn_lamports": 0}


def fee_usd(amount_usd: float) -> float:
    """3.65% of a USD/paper amount. Zero or negative amounts have
    no fee."""
    if amount_usd is None or amount_usd <= 0:
        return 0.0
    return float(amount_usd) * FEE_BPS / 10000.0


def fee_sol_from_usd(fee_usd_amount: float, sol_price_usd) -> float | None:
    """A paper fee's SOL equivalent at the SOL price of the trade, or
    None when no usable SOL price was available — the display layer
    converts at the live price instead, never a guessed rate."""
    if fee_usd_amount is None or fee_usd_amount <= 0:
        return None
    try:
        price = float(sol_price_usd)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return fee_usd_amount / price


def get_fee_recipient_pubkey(cfg) -> str:
    """Return the fee/donation recipient pubkey from the
    FEE_RECIPIENT_PUBKEY env var (Config.fee_recipient_pubkey).

    Raises ValueError when the var is unset, empty, or not a valid
    base58 32-byte Solana address — the caller must fail closed
    (real trades refuse, donations refuse), never fall back to a
    generated wallet or a guessed address. No wallet is ever
    created here; the recipient is configured, not generated."""
    raw = (cfg.fee_recipient_pubkey or "").strip()
    if not raw or not is_valid_address(raw):
        raise ValueError(
            "FEE_RECIPIENT_PUBKEY is not set or not a valid wallet "
            "address")
    return raw


def record_fee(db, user_id: int, bot_id: int | None, coin: str,
               side: str, mode: str, fee_sol: float | None,
               fee_usd: float | None = None,
               fee_lamports: int | None = None,
               signature: str | None = None,
               trade_id: str | None = None,
               treasury_sol: float | None = None,
               burn_sol: float | None = None,
               pump_fee_sol: float | None = None,
               treasury_lamports: int | None = None,
               burn_lamports: int | None = None,
               pump_fee_lamports: int | None = None,
               now: str | None = None) -> bool:
    """Insert one fee row. Returns True when inserted, False when a
    row with the same signature already exists — a real trade
    recorded twice is one fee, never two. `signature` is None for
    paper trades (no on-chain transaction). The split kwargs
    record the fee's three parts per trade (treasury / burned /
    pump.fun program fee, in SOL and in lamports) so the display
    sums real per-trade numbers; they are None for paper trades
    and for rows recorded before the split existed."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.execute(
        "INSERT OR IGNORE INTO trade_fees (user_id, bot_id, coin, side, "
        "mode, fee_sol, fee_usd, fee_lamports, signature, trade_id, "
        "treasury_sol, burn_sol, pump_fee_sol, treasury_lamports, "
        "burn_lamports, pump_fee_lamports, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, bot_id, coin, side, mode, fee_sol, fee_usd,
         fee_lamports, signature, trade_id, treasury_sol, burn_sol,
         pump_fee_sol, treasury_lamports, burn_lamports,
         pump_fee_lamports, now))
    db.commit()
    return cur.rowcount == 1


def _col(row, name):
    """One column from a fee row, whether the row is a dict (from
    user_fees) or a sqlite3.Row (from a totals query) — None when
    the column is missing or NULL, never a raise."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def fee_row_split(row, sol_price_usd) -> dict:
    """The three DISPLAYED numbers for one fee row, in SOL:
    {"treasury_sol", "burn_sol", "pump_fee_sol"}.
      * treasury: the recorded split when present, else the
        recorded fee scaled to the treasury's share (x 170/365)
        — rows recorded before the split still display
        correctly. None when the row has no fee amount at all.
      * burn: the recorded burned amount when present, else 0.0
        — rows recorded before the split were collected in full
        to the wallet, so nothing was burned for them.
      * pump_fee: the recorded program fee when present, else
        None — unknown for rows recorded before the split, never
        guessed, and left out of the pump totals.
    Every display reads through here — the per-user totals, the
    app-wide totals, /api/fees, and each dropdown row — so they
    cannot disagree."""
    full = None
    if _col(row, "fee_sol") is not None:
        full = float(_col(row, "fee_sol"))
    elif _col(row, "fee_usd") is not None:
        full = fee_sol_from_usd(float(_col(row, "fee_usd")),
                                sol_price_usd)
    if _col(row, "treasury_sol") is not None:
        treasury = float(_col(row, "treasury_sol"))
    elif full is None:
        treasury = None
    else:
        treasury = full * TREASURY_SHARE_BPS_OF_TRADE / FEE_BPS
    if _col(row, "burn_sol") is not None:
        burn = float(_col(row, "burn_sol"))
    else:
        burn = 0.0
    pump = (_col(row, "pump_fee_sol")
            if _col(row, "pump_fee_sol") is not None else None)
    if pump is not None:
        pump = float(pump)
    return {"treasury_sol": treasury, "burn_sol": burn,
            "pump_fee_sol": pump}


def fee_row_sol(row, sol_price_usd) -> float | None:
    """The DISPLAYED (treasury-share) SOL for one fee row — the
    treasury number from fee_row_split. The recorded fee itself
    is the full 3.65% and stays that way in the ledger; only what
    is shown is the share."""
    return fee_row_split(row, sol_price_usd)["treasury_sol"]


def user_fees(db, user_id: int, limit: int = 50,
              mode: str | None = None) -> list:
    """The user's fee payments, newest first. With mode="real" only
    fees from executed real trades are returned — that is what the
    home-page "Fees paid" dropdown shows: paper fees are recorded
    in the ledger but are never SOL paid to the app. With mode=None
    (the default) every row is returned, which is what the paper
    ledger and the recording tests use."""
    if mode is None:
        rows = db.execute(
            "SELECT id, user_id, bot_id, coin, side, mode, fee_sol, "
            "fee_usd, fee_lamports, signature, trade_id, treasury_sol, "
            "burn_sol, pump_fee_sol, treasury_lamports, burn_lamports, "
            "pump_fee_lamports, created_at "
            "FROM trade_fees WHERE user_id = ? ORDER BY id DESC "
            "LIMIT ?", (user_id, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT id, user_id, bot_id, coin, side, mode, fee_sol, "
            "fee_usd, fee_lamports, signature, trade_id, treasury_sol, "
            "burn_sol, pump_fee_sol, treasury_lamports, burn_lamports, "
            "pump_fee_lamports, created_at "
            "FROM trade_fees WHERE user_id = ? AND mode = ? "
            "ORDER BY id DESC LIMIT ?", (user_id, mode, limit)).fetchall()
    return [dict(r) for r in rows]


def _totals(rows, sol_price_usd) -> dict:
    """Sum fee rows into the three displayed numbers, in SOL:
    {"treasury_sol", "burn_sol", "pump_fee_sol"}. A row with no
    usable fee amount contributes nothing to the treasury total;
    the burn is 0.0 for rows recorded before the split; the pump
    total sums only rows with a recorded program fee — unknown
    pump fees are left out, never guessed."""
    treasury = 0.0
    burn = 0.0
    pump = 0.0
    for row in rows:
        split = fee_row_split(row, sol_price_usd)
        if split["treasury_sol"] is not None:
            treasury += split["treasury_sol"]
        burn += split["burn_sol"]
        if split["pump_fee_sol"] is not None:
            pump += split["pump_fee_sol"]
    return {"treasury_sol": treasury, "burn_sol": burn,
            "pump_fee_sol": pump}


def user_fee_totals(db, user_id: int, sol_price_usd=None) -> dict:
    """The user's three displayed fee numbers, in SOL —
    {"treasury_sol", "burn_sol", "pump_fee_sol"} — REAL fees only
    (mode='real', rows from executed real trades). Paper fees are
    play money: recorded in the ledger but never counted here, so
    the totals are shares of SOL actually paid, never a paper SOL
    equivalent. A real fee recorded without a SOL price is
    converted at the given live SOL price — without a price it is
    left out of the treasury total rather than guessed."""
    rows = db.execute(
        "SELECT fee_sol, fee_usd, treasury_sol, burn_sol, pump_fee_sol "
        "FROM trade_fees WHERE user_id = ? AND mode = 'real'",
        (user_id,)).fetchall()
    return _totals(rows, sol_price_usd)


def app_fee_totals(db, sol_price_usd=None) -> dict:
    """The app-wide three displayed fee numbers, in SOL —
    {"treasury_sol", "burn_sol", "pump_fee_sol"} — summed over
    every user's REAL fees (mode='real'). Paper fees never count
    here, so play-money fees cannot inflate the totals. It starts
    at zero at deploy (no back-charging) and counts only fees
    actually recorded. Same conversion rule as the per-user
    totals: a fee recorded without a SOL price is converted at
    the given live SOL price, and left out without one, never
    guessed."""
    rows = db.execute(
        "SELECT fee_sol, fee_usd, treasury_sol, burn_sol, pump_fee_sol "
        "FROM trade_fees WHERE mode = 'real'").fetchall()
    return _totals(rows, sol_price_usd)


def user_fee_total_sol(db, user_id: int, sol_price_usd=None) -> float:
    """The user's total fees paid, in SOL, AS DISPLAYED — the
    treasury's share (1.7% of the trade). REAL fees only; see
    user_fee_totals for the three-number breakdown."""
    return user_fee_totals(db, user_id, sol_price_usd)["treasury_sol"]


def app_fee_total_sol(db, sol_price_usd=None) -> float:
    """The app-wide total of fees collected from every user, in
    SOL, AS DISPLAYED — the treasury's share (1.7% of the trade).
    REAL fees only; see app_fee_totals for the three-number
    breakdown."""
    return app_fee_totals(db, sol_price_usd)["treasury_sol"]
