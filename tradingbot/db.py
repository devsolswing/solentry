"""SQLite storage. Every query is scoped by user_id at the call site.

Schema:
  users, wallets (encrypted secret), coin_wallets (one row per
  user+coin, written on the first-trade "Create wallet" tap),
  paper_portfolio, paper_holdings,
  paper_trades, bots, bot_proposals, send_intents, activity,
  login_attempts (rate limiting), passkey_credentials (WebAuthn),
  webauthn_challenges (single-use ceremony challenges), trade_ledger,
  treasury_wallet (retired 2026-11-10: rows purged at startup by
  _purge_generated_fee_wallets, never written), trade_fees
  (one row per 3.65% fee charged).
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    recovery_question TEXT NOT NULL DEFAULT '',
    recovery_answer_hash TEXT NOT NULL DEFAULT '',
    recovery_answer_salt TEXT NOT NULL DEFAULT '',
    recovery_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    pubkey TEXT NOT NULL,
    encrypted_secret TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Per-coin wallets (first-trade wallet step, 2026-11-11): one row
-- per (user, coin), written when the user taps "Create wallet" for
-- that coin. The wallet recorded is the user's existing Solana app
-- wallet — no new keys are generated here. The row existing is what
-- makes the button disappear for that coin forever.
CREATE TABLE IF NOT EXISTS coin_wallets (
    user_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    wallet_pubkey TEXT NOT NULL,
    network TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, coin_id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS paper_portfolio (
    user_id INTEGER PRIMARY KEY,
    cash_usd REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS paper_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    amount REAL NOT NULL,
    avg_price REAL NOT NULL,
    UNIQUE (user_id, coin_id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    total_usd REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL,
    coins TEXT NOT NULL DEFAULT '["SOL"]',
    buy_size_pct REAL NOT NULL DEFAULT 20,
    stop_loss_pct REAL NOT NULL DEFAULT 7,
    mode TEXT NOT NULL DEFAULT 'paper',
    silent INTEGER NOT NULL DEFAULT 0,
    max_open_trades INTEGER NOT NULL DEFAULT 3,
    api_token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Per-bot open positions for silent trading. The paper portfolio /
-- holdings tables are per-user; this table is per-bot so the loop can
-- enforce each bot's own stop loss and max-open-trades limits.
-- mode is 'paper' or 'real' (copied from the bot at open time).
CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    amount REAL NOT NULL,
    avg_price REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (bot_id, coin_id),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

CREATE TABLE IF NOT EXISTS bot_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    coin_id TEXT NOT NULL,
    side TEXT NOT NULL,
    amount_sol REAL,
    destination TEXT,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

CREATE TABLE IF NOT EXISTS send_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    destination TEXT NOT NULL,
    amount_lamports INTEGER NOT NULL,
    token TEXT NOT NULL DEFAULT 'SOL',
    status TEXT NOT NULL DEFAULT 'pending',
    signature TEXT,
    error TEXT,
    mint TEXT NOT NULL DEFAULT '',
    amount_base_units INTEGER NOT NULL DEFAULT 0,
    decimals INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Real holdings from manual (popup) real trades: pump.fun mints the
-- user bought by hand on devnet. Bot-managed real positions live in
-- bot_positions (mode='real'); these are the manual ones, so the two
-- never double-count. avg_price is the USD cost basis at buy time.
CREATE TABLE IF NOT EXISTS real_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    amount REAL NOT NULL,
    avg_price REAL NOT NULL,
    UNIQUE (user_id, coin_id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Coin identity + logos, one row per mint (pump.fun-only app): the
-- coin's real official name + ticker and its official logo URL,
-- fetched once (on purchase, or lazily on render of a held row) from
-- the on-chain Metaplex metadata and/or DexScreener — only for coins
-- the user holds, never bulk-loaded. A row with source 'none' means
-- nothing was found; it is retried at most once an hour.
CREATE TABLE IF NOT EXISTS coin_meta (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    image_url TEXT,
    source TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS login_attempts (
    username TEXT PRIMARY KEY,
    fail_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

-- Passkeys (WebAuthn): one row per registered credential. The
-- public key is stored base64url-encoded; the private key never
-- leaves the user's device. sign_count is updated on every login
-- (a mismatch means a possible cloned credential and fails the
-- ceremony).
CREATE TABLE IF NOT EXISTS passkey_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Single-use WebAuthn challenges (registration + login). A challenge
-- is issued by the server, lives 5 minutes, and is marked used the
-- moment it is presented — replaying it always fails.
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    challenge TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Rate limiting counters (rate_limit.py). One row per key: hits in
-- the current window and when the window started. Stored in the DB
-- so a restart does not clear the limits.
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    hits INTEGER NOT NULL,
    window_start INTEGER NOT NULL
);

-- Login events for anomaly detection: every login attempt, success
-- or failure, with the client IP and device string. A burst of
-- failures from IPs the account has never logged in from is the
-- signal that locks the account.
CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-account dedicated signing bot: the bot that signs the
-- account's real trades. support_id is exactly 8 digits, unique
-- across accounts, and IMMUTABLE — the only writes to this table
-- are INSERTs (signup / backfill). No route, form, or code path
-- anywhere in this app can change it.
CREATE TABLE IF NOT EXISTS signing_bots (
    user_id INTEGER PRIMARY KEY,
    support_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Project donation wallet: RETIRED 2026-11-10. All donations go
-- only to the recipient in FEE_RECIPIENT_PUBKEY; the generated
-- wallet's row is purged at startup and nothing ever writes here.
-- The table stays (empty) so old databases keep their shape.
CREATE TABLE IF NOT EXISTS donation_wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pubkey TEXT NOT NULL,
    encrypted_secret TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Confirmed donations (donor-paid, Phantom-signed). This table is also
-- the admin notification record: each row is one notification for James.
CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_address TEXT NOT NULL,
    amount_lamports INTEGER NOT NULL,
    signature TEXT NOT NULL UNIQUE,
    network TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- P&L snapshots: one row per user per silent-loop evaluation and per
-- manual paper trade. The P&L chart is built ONLY from these recorded
-- rows — history is never invented. real_sol is the app wallet's
-- on-chain SOL balance when the RPC answered, else NULL (shown as
-- "unavailable", never guessed).
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    cash_usd REAL NOT NULL,
    holdings_value_usd REAL NOT NULL,
    total_value_usd REAL NOT NULL,
    real_sol REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- ∆scanner∆ price marks: one price per candidate mint per scan, so
-- the scanner can compute a 3-minute gain (the feed only provides
-- 5-minute changes). Marks older than 10 minutes are pruned on
-- every scan. No mark at least 3 minutes old means no 3-minute
-- gain — the coin is not bought, never guessed.
CREATE TABLE IF NOT EXISTS scanner_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    price_usd REAL NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);
CREATE INDEX IF NOT EXISTS idx_scanner_marks_bot_mint
    ON scanner_marks (bot_id, mint, recorded_at);

-- ∆scanner∆ top-10 wallets watch: the combined holdings of a held
-- coin's 10 largest token accounts, tracked per bot per mint. The
-- baseline is the first observation after the position opens,
-- tracked upward to the peak; a drop of 10% from that peak is the
-- dump signal. Rows are cleared when the position closes.
CREATE TABLE IF NOT EXISTS scanner_top10 (
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    top10_total INTEGER NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (bot_id, mint),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

CREATE TABLE IF NOT EXISTS trade_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    bot_id INTEGER,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    total_usd REAL NOT NULL,
    pnl_usd REAL,
    win INTEGER,
    exit_reason TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'paper',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- The app's former SolEntry Treasury wallet: RETIRED 2026-11-10.
-- The trading fee was REMOVED 2026-11-12, so no trade writes
-- here and no trade reads FEE_RECIPIENT_PUBKEY any more; the
-- generated wallet's row is purged at startup and nothing ever
-- writes here. The table stays (empty) so old databases keep
-- their shape.
CREATE TABLE IF NOT EXISTS treasury_wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pubkey TEXT NOT NULL,
    encrypted_secret TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per fee charged, from when the trading fee existed
-- (removed 2026-11-12, James): 3.65% of a trade. fee_sol is the
-- SOL the fee was worth (real trades: exact lamports in the same
-- tx; paper trades: the USD fee converted at the trade-time SOL
-- price, or NULL when no SOL price was available). signature is
-- unique and NULL for paper trades, so a real trade recorded
-- twice is one fee, never two. HISTORY ONLY: no new rows are
-- recorded — the table is kept so past trades keep their record.
CREATE TABLE IF NOT EXISTS trade_fees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    fee_sol REAL,
    fee_usd REAL,
    fee_lamports INTEGER,
    signature TEXT UNIQUE,
    trade_id TEXT,
    treasury_sol REAL,
    burn_sol REAL,
    pump_fee_sol REAL,
    treasury_lamports INTEGER,
    burn_lamports INTEGER,
    pump_fee_lamports INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- builderBot's identity: one per user, unique immutable 8-digit ID
-- (same pattern as the signing bot's support ID — only INSERT
-- exists, no update path anywhere).
CREATE TABLE IF NOT EXISTS builderbots (
    user_id INTEGER PRIMARY KEY,
    builder_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- builderBot's working memory for one in-progress conversation:
-- the whole state (step, answers, message history) is encrypted
-- with AES-256-GCM (ENCRYPTION_KEY) and wiped the moment the bot
-- is created or the conversation is cancelled. Only one active
-- conversation per user.
CREATE TABLE IF NOT EXISTS builderbot_chats (
    user_id INTEGER PRIMARY KEY,
    encrypted_state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Every rule adjustment builderBot makes to a losing bot: what
-- changed, from what to what, and which trades triggered it.
CREATE TABLE IF NOT EXISTS builderbot_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots (id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Security bot email codes (signup confirmation + account
-- recovery). Only the SHA-256 hash of (per-code salt + code) is
-- stored — the 4-digit code itself is never stored and never
-- logged. Single-use (used=1 after a correct entry or after 5
-- wrong tries), 10-minute expiry. At most one live code per
-- purpose per email (issuing a new one voids the old).
CREATE TABLE IF NOT EXISTS email_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,
    user_id INTEGER,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    code_salt TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Signups waiting on their email confirmation code. The account
-- is NOT created until the code is entered correctly — this row
-- holds the validated signup (password hash bound to the email,
-- recovery question/answer) plus the user key wrapped with the
-- server key, so the wallet can be created at verification time
-- without ever storing the plaintext password. Rows older than
-- 30 minutes are dead (the code itself expires in 10).
CREATE TABLE IF NOT EXISTS pending_signups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    pw_version INTEGER NOT NULL DEFAULT 2,
    email TEXT NOT NULL,
    recovery_question TEXT NOT NULL DEFAULT '',
    recovery_answer_hash TEXT NOT NULL DEFAULT '',
    recovery_answer_salt TEXT NOT NULL DEFAULT '',
    recovery_hint TEXT NOT NULL DEFAULT '',
    user_key_salt TEXT NOT NULL,
    wrapped_user_key TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the silent-loop thread (own connection) write while the
    # web process reads, without locking the whole database file.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added after the first release. init_db applies them to
# databases created before they existed.
_MIGRATIONS = (
    ("bots", "mode", "TEXT NOT NULL DEFAULT 'paper'"),
    ("bots", "silent", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "max_open_trades", "INTEGER NOT NULL DEFAULT 3"),
    ("users", "recovery_question", "TEXT NOT NULL DEFAULT ''"),
    # Email collected at signup (used by the forgot-username flow:
    # email + security answer reveals the username). '' for accounts
    # created before the field existed.
    ("users", "email", "TEXT NOT NULL DEFAULT ''"),
    ("users", "recovery_answer_hash", "TEXT NOT NULL DEFAULT ''"),
    ("users", "recovery_answer_salt", "TEXT NOT NULL DEFAULT ''"),
    ("users", "recovery_hint", "TEXT NOT NULL DEFAULT ''"),
    # Per-bot trading rules (rules.py): validated JSON, or '' for the
    # standard v1 signals. category groups bots, e.g. "Memecoin build".
    # last_scan_at is when the rules engine last scanned for this bot.
    ("bots", "rules_json", "TEXT NOT NULL DEFAULT ''"),
    ("bots", "category", "TEXT NOT NULL DEFAULT ''"),
    ("bots", "last_scan_at", "TEXT NOT NULL DEFAULT ''"),
    # Per-position rule state: the highest price since the position
    # opened (trailing stop), the original amount (sell-ladder rungs
    # are % of it), and how many ladder rungs already sold.
    ("bot_positions", "peak_price", "REAL"),
    ("bot_positions", "orig_amount", "REAL"),
    ("bot_positions", "ladder_stage", "INTEGER NOT NULL DEFAULT 0"),
    # The last price the loop saw for a position, and when (rug sell:
    # the fast-drop exit compares the current price against this mark,
    # so a collapse between two scans is caught on the next one).
    ("bot_positions", "last_price", "REAL"),
    ("bot_positions", "last_price_at", "TEXT NOT NULL DEFAULT ''"),
    # The user's pump.fun trading wallet: pump.fun has no separate
    # accounts, so trading on pump.fun uses the linked Solana wallet —
    # the per-user app wallet, recorded here at signup.
    ("users", "pumpfun_wallet", "TEXT NOT NULL DEFAULT ''"),
    # Which balance the header shows: 'paper' (paper portfolio value)
    # or 'devnet' (app wallet's on-chain SOL). Flipped by the header
    # switch; the buy form defaults to it.
    ("users", "active_mode", "TEXT NOT NULL DEFAULT 'paper'"),
    # Which platform a bot trades on: always 'pump.fun' now —
    # the app trades pump.fun coins only. '' is a bot from before
    # platforms existed — it is treated as pump.fun wherever the
    # platform matters.
    ("bots", "platform", "TEXT NOT NULL DEFAULT ''"),
    # Which mode an activity row belongs to: 'paper', 'real', or ''
    # for account events (signup, recovery, passkey) that belong to
    # neither trading mode and are shown in both. Trading rows are
    # always tagged, so paper and real activity never mix on a page.
    ("activity", "mode", "TEXT NOT NULL DEFAULT ''"),
    # Audit trail: the client IP and device string behind each
    # activity row, where available. Empty for rows the silent loop
    # writes (no HTTP request) and for rows written before this
    # column existed.
    ("activity", "ip", "TEXT NOT NULL DEFAULT ''"),
    ("activity", "device", "TEXT NOT NULL DEFAULT ''"),
    # Paper-trade provenance (losing-buy analysis + buy filter):
    # which bot bought the coin, the signal values at buy time, and
    # why a sell exited. NULL for rows written before these columns
    # existed — the analysis section on the P&L page shows those as unavailable, never
    # guessed. Manual buys/sells have no bot and no signal.
    ("paper_trades", "bot_id", "INTEGER"),
    ("paper_trades", "buy_m5_pct", "REAL"),
    ("paper_trades", "buy_volume_5m_usd", "REAL"),
    ("paper_trades", "buy_buys_5m", "INTEGER"),
    ("paper_trades", "buy_sells_5m", "INTEGER"),
    ("paper_trades", "buy_regime", "TEXT"),
    ("paper_trades", "exit_reason", "TEXT"),
    # Top-10 concentration watch (builderBot): the top-10 share of
    # total supply at the last check, how many consecutive checks it
    # has risen, and when the check ran (15-second cadence gating).
    ("scanner_top10", "share_pct", "REAL"),
    ("scanner_top10", "rise_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("scanner_top10", "last_checked_at", "TEXT NOT NULL DEFAULT ''"),
    # When the user last answered the Demo / Real mode chooser
    # (UTC timestamp, or '' when never answered since sign-in). The
    # chooser shows once after sign-in; a sign-in clears this, an
    # answer sets it. Remembering it in the database (not just the
    # session cookie) means a dropped session flag alone can never
    # make the chooser reappear mid-session — the user can still
    # switch modes any time from the HUD.
    ("users", "mode_chosen_at", "TEXT NOT NULL DEFAULT ''"),
    # Fee split (James, 2026-11-10/11) — HISTORY ONLY: the per-trade
    # treasury / burned / pump.fun-program-fee shares, in SOL and in
    # lamports, for rows recorded while the trading fee existed.
    # The fee was removed 2026-11-12; no new rows are recorded.
    # NULL for paper trades and for rows recorded before the split
    # existed.
    ("trade_fees", "treasury_sol", "REAL"),
    ("trade_fees", "burn_sol", "REAL"),
    ("trade_fees", "pump_fee_sol", "REAL"),
    ("trade_fees", "treasury_lamports", "INTEGER"),
    ("trade_fees", "burn_lamports", "INTEGER"),
    ("trade_fees", "pump_fee_lamports", "INTEGER"),
    # Token sends (general SPL send): the mint, the amount in the
    # mint's base units, and the mint's decimals (so the amount can
    # be rendered back in token units). '' / 0 / 0 for SOL sends
    # and for rows recorded before token sends existed.
    ("send_intents", "mint", "TEXT NOT NULL DEFAULT ''"),
    ("send_intents", "amount_base_units", "INTEGER NOT NULL DEFAULT 0"),
    ("send_intents", "decimals", "INTEGER NOT NULL DEFAULT 0"),
    # Password + email second level (Security bot build, 2026-11-11):
    # pw_version 1 = legacy password-only hash, 2 = password bound
    # to the account email (hash_account_password). user_key_salt +
    # wrapped_user_key hold the per-user key (derived from
    # password+email) wrapped with the server key, so the signing
    # bot can unlock the wallet offline. wallets.enc_version 1 =
    # server-key-only (legacy), 2 = user-key encrypted. Legacy rows
    # upgrade transparently at the next login, when the password
    # is available to derive the user key.
    ("users", "pw_version", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "user_key_salt", "TEXT NOT NULL DEFAULT ''"),
    ("users", "wrapped_user_key", "TEXT NOT NULL DEFAULT ''"),
    ("wallets", "enc_version", "INTEGER NOT NULL DEFAULT 1"),
)


def _backfill_pumpfun_wallets(conn: sqlite3.Connection) -> None:
    """One-time, idempotent backfill: users created before the
    pumpfun_wallet column existed get their app wallet linked."""
    conn.execute(
        "UPDATE users SET pumpfun_wallet = "
        "(SELECT pubkey FROM wallets WHERE wallets.user_id = users.id) "
        "WHERE pumpfun_wallet = '' AND EXISTS "
        "(SELECT 1 FROM wallets WHERE wallets.user_id = users.id)")


def _backfill_builderbots(conn: sqlite3.Connection) -> None:
    """One-time, idempotent backfill: users created before builderBot
    existed get one (with a fresh 8-digit ID)."""
    from .builderbot import ensure_builderbot

    user_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM users").fetchall()]
    for user_id in user_ids:
        ensure_builderbot(conn, user_id)


def _backfill_signing_bots(conn: sqlite3.Connection) -> None:
    """One-time, idempotent backfill: users created before signing
    bots existed get one (with a fresh support ID)."""
    from .signing_bot import ensure_signing_bot

    user_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM users").fetchall()]
    for user_id in user_ids:
        ensure_signing_bot(conn, user_id)


def _reset_paper_50_cad(conn: sqlite3.Connection) -> None:
    """One-time reset (2026-11-09): paper trading restarts at $50 CAD.

    Every existing paper account is reset to $50 cash and its paper
    positions are cleared — the old $10,000-scale holdings do not fit
    a $50 account. Runs once: a marker row in meta() records it, so
    later restarts never touch paper balances again (a user who
    trades down to $40 stays at $40). Paper trade history and P&L
    snapshots are kept as records of what happened; only the live
    balance and open positions are reset. Real SOL is never touched.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'paper_reset_50_cad'"
    ).fetchone()
    if row:
        return
    conn.execute("UPDATE paper_portfolio SET cash_usd = 50")
    conn.execute("DELETE FROM paper_holdings")
    conn.execute(
        "INSERT INTO meta (key, value) "
        "VALUES ('paper_reset_50_cad', '2026-11-09')")


def _reset_paper_100_cad(conn: sqlite3.Connection) -> None:
    """One-time reset (2026-11-09): paper trading restarts at $100 CAD.

    Same shape as the $50 reset above, new marker: every existing
    paper account is reset to $100 cash and its paper positions are
    cleared — the old balances (many traded down toward $0) do not
    fit a $100 account. Runs once: a marker row in meta() records
    it, so later restarts never touch paper balances again (a user
    who trades down to $40 stays at $40). Paper trade history and
    P&L snapshots are kept as records of what happened; only the
    live balance and open positions are reset. Real SOL is never
    touched.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'paper_reset_100_cad'"
    ).fetchone()
    if row:
        return
    conn.execute("UPDATE paper_portfolio SET cash_usd = 100")
    conn.execute("DELETE FROM paper_holdings")
    conn.execute(
        "INSERT INTO meta (key, value) "
        "VALUES ('paper_reset_100_cad', '2026-11-09')")


def _purge_generated_fee_wallets(conn: sqlite3.Connection) -> None:
    """One-time purge (2026-11-10): the app-generated Treasury and
    donation wallets are retired — ALL donations go only to the
    recipient in FEE_RECIPIENT_PUBKEY (the trading fee was removed
    2026-11-12, so no trade uses it any more). Any treasury_wallet
    or donation_wallet row is deleted so neither wallet can ever
    receive again. Runs once: a marker row in meta() records it, so
    later restarts never touch these tables again. On a fresh
    database there is nothing to purge. The tables stay (empty) —
    nothing in the app writes to them any more."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'purge_generated_fee_wallets'"
    ).fetchone()
    if row:
        return
    conn.execute("DELETE FROM treasury_wallet")
    conn.execute("DELETE FROM donation_wallet")
    conn.execute(
        "INSERT INTO meta (key, value) "
        "VALUES ('purge_generated_fee_wallets', '2026-11-10')")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    _backfill_pumpfun_wallets(conn)
    _backfill_signing_bots(conn)
    _backfill_builderbots(conn)
    _reset_paper_50_cad(conn)
    _reset_paper_100_cad(conn)
    _purge_generated_fee_wallets(conn)
    conn.commit()
    return conn
