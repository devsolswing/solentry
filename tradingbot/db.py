"""SQLite storage. Every query is scoped by user_id at the call site.

Schema:
  users, wallets (encrypted secret),
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
    -- NULL for a usernameless (discoverable-credential) login
    -- challenge: no user is known until the authenticator
    -- answers. A NULL user never violates the FK below.
    user_id INTEGER,
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

-- Tuning build 2026-11-15: 2-scan entry confirmation. One row per
-- (bot, mint) that passed the buy threshold + buy filter + momentum
-- gate on a scan but has not confirmed yet. A buy happens only when
-- the signal is still passing on a LATER scan; a faded signal never
-- buys. Rows older than twice the bot's scan interval (at least 10
-- minutes) are pruned on every evaluation.
CREATE TABLE IF NOT EXISTS scanner_signal_confirmations (
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    first_gain_pct REAL,
    PRIMARY KEY (bot_id, mint),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- Tuning build 2026-11-15: re-buy cooldown. One row per (bot,
-- mint) that closed at a loss — the bot may not buy that coin again
-- until blocked_until (30 minutes after the loss). A losing coin
-- is never re-bought on the next scan.
CREATE TABLE IF NOT EXISTS bot_buy_cooldowns (
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    blocked_until TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (bot_id, mint),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- Tuning build 2026-11-15: signal-outcome log. One row per entry
-- the four-part signal gate (or, for signals-kind bots, the 24h
-- signal) approved, recording the signal's features at entry; the
-- trade's outcome is filled in when the position fully closes.
-- LOGGING ONLY — the bot never changes its own rules from past
-- outcomes (that is the auto-tighten doom-loop pattern). Any
-- adaptation is a human decision from this data. Features that
-- do not exist for a bot kind are NULL (signals bots have no
-- volume/buyer/1h data), never guessed.
CREATE TABLE IF NOT EXISTS bot_signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    gain_pct REAL,
    gain_window TEXT NOT NULL DEFAULT '',
    volume_ratio REAL,
    buyer_ratio REAL,
    trend_1h_pct REAL,
    outcome_pnl_usd REAL,
    outcome_win INTEGER,
    outcome_at TEXT,
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);
CREATE INDEX IF NOT EXISTS idx_bot_signal_log_bot_mint
    ON bot_signal_log (bot_id, mint, outcome_at);

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
    -- Friction logging (2026-11-15): the full on-chain signature
    -- and the executed SOL (spent on buys, received on sells) for
    -- real trades, so round-trip friction can be measured from the
    -- ledger alone. NULL for paper trades and for rows recorded
    -- before these columns existed.
    signature TEXT,
    sol_amount REAL,
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

-- $GATE trade packages (James, 2026-11-23): real trades cost 1
-- trade credit, bought as a $GATE package inside the app (1M = 100
-- trades, 5M = 550, 10M = 1450). The purchased $GATE goes straight
-- to the dev wallet and the package's trades are credited
-- automatically once that transfer confirms on-chain. $GATE bought
-- OUTSIDE the app is never fuel; the package balance starts at 0
-- and grows only from packages bought in the app. The LEDGER is
-- the truth — the cached counter below is only a concurrency
-- primitive, and every read recomputes from purchases minus uses
-- (see gate_fuel.py).
-- gate_fuel: cached counter per user (reconciled against the ledger
-- on every read; the ledger wins when they disagree).
-- ledger_rowid_at_first_purchase anchors the backfill: the highest
-- trade_ledger rowid at the moment of the user's first completed
-- package purchase. Trades at or below it predate fuel and are never
-- backfilled; trades above it are post-fuel. A rowid anchor, not a
-- timestamp — trade_ledger created_at is second-precision, so a
-- pre-fuel trade can share the purchase's second.
-- gate_fuel_purchases: one row per completed package purchase
-- (credits granted). status is 'completed' — a purchase is only
-- recorded after the dev-wallet send is confirmed on-chain.
-- gate_fuel_uses: one row per consumed credit, keyed by the
-- confirmed on-chain signature (UNIQUE: one signature can never
-- consume twice) and, for silent-bot trades, the trade_ledger
-- trade_id (UNIQUE: one ledger row can never consume twice).
-- signature is NULL only for rows backfilled from the trade ledger
-- (the ledger never stored the signature); trade_id is NULL only
-- for manual trades (which write no ledger row).
-- gate_fuel_enrollment is RETIRED (2026-11-23): the holdings-funded
-- model is gone. The table stays so old databases keep their shape;
-- nothing reads it any more. Holdings-era gate_fuel_uses rows (the
-- tokens column) stay as history — the migration in
-- _migrate_gate_fuel_uses rebuilds the table keeping them, and the
-- package balance starts from gate_fuel_purchases only.
CREATE TABLE IF NOT EXISTS gate_fuel (
    user_id INTEGER PRIMARY KEY,
    trades_remaining INTEGER NOT NULL DEFAULT 0 CHECK (trades_remaining >= 0),
    total_purchased INTEGER NOT NULL DEFAULT 0,
    total_used INTEGER NOT NULL DEFAULT 0,
    ledger_rowid_at_first_purchase INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE TABLE IF NOT EXISTS gate_fuel_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bundle_id TEXT NOT NULL,
    coins INTEGER NOT NULL,
    trades INTEGER NOT NULL,
    sol_spent REAL NOT NULL,
    buy_signature TEXT,
    send_signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    error TEXT,
    -- source is 'package' for a paid $GATE package, 'promo' for a
    -- one-time promo code grant (James, 2026-11-24). promo_code
    -- names the code that granted it. A promo grant is visible
    -- promo credit in the ledger — never disguised as a paid
    -- package — and its send_signature is the literal
    -- 'promo:<code>', never a real on-chain signature.
    source TEXT NOT NULL DEFAULT 'package',
    promo_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE TABLE IF NOT EXISTS gate_fuel_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    trade_id TEXT UNIQUE,
    signature TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);
-- gate_fuel_sweep_trades: trade_ledger rows that belong to a
-- last-credit exit-all sweep (James, 2026-11-24). The sweep sells
-- every open real position for ONE credit total, so reconcile_fuel's
-- backfill must NOT count each sweep sell as its own credit: a
-- trade_id listed here is excluded from backfill. Without this,
-- the backfill would consume one credit per swept position on the
-- next fuel read, on top of the sweep's single credit.
CREATE TABLE IF NOT EXISTS gate_fuel_sweep_trades (
    trade_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    sweep_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE TABLE IF NOT EXISTS gate_fuel_enrollment (
    user_id INTEGER PRIMARY KEY,
    enrolled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Promo / one-time codes (James, 2026-11-24): a coupon code
-- entered in Settings grants fuel credits. A code is usable at
-- most max_uses times IN TOTAL across all users — BetaGate1994
-- has max_uses 1, so the first person to enter it gets the
-- trades and the code is dead after that. One-time codes only:
-- that is the anti-bypass rule, so a single user cannot mint
-- fuel by making multiple accounts. promo_redemptions is the
-- audit trail (who redeemed which code, when, for how many
-- trades). Codes are matched EXACTLY (case-sensitive).
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    trades INTEGER NOT NULL CHECK (trades > 0),
    -- scope is the rule: 'global_once' means the code is usable
    -- at most max_uses times IN TOTAL across all users
    -- (BetaGate1994: max_uses 1); 'per_account_once' means every
    -- account can use it once ("Still on the gate": the signup
    -- code, 50 trades — max_uses NULL, no global cap, the
    -- per-user UNIQUE row in promo_redemptions is the guard).
    scope TEXT NOT NULL DEFAULT 'global_once'
        CHECK (scope IN ('global_once', 'per_account_once')),
    max_uses INTEGER CHECK (max_uses IS NULL OR max_uses >= 1),
    uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    trades INTEGER NOT NULL,
    redeemed_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- No account can ever redeem the same code twice, whatever
    -- the scope: this UNIQUE row is the per-user guard (and the
    -- atomicity backstop for simultaneous redeems).
    UNIQUE (user_id, code),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Pending trades (mainnet executor fix, 2026-11-22): every sent
-- real-trade signature, recorded BEFORE the send (the signature is
-- known once the transaction is signed locally). The loop re-checks
-- unconfirmed rows via getTransaction each pass before a trade is
-- finally marked failed — a transaction that landed after the ~30s
-- confirmation window is never reported as failed. status is
-- 'pending', 'confirmed' or 'failed'.
CREATE TABLE IF NOT EXISTS pending_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER,
    coin_id TEXT NOT NULL,
    side TEXT NOT NULL,
    signature TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- Persistent sell claims (mainnet executor fix, 2026-11-22): one
-- open claim per (bot, position) while a real sell is in flight.
-- Unlike the in-memory _SELL_IN_FLIGHT guard, this survives a
-- process restart: before any re-sell, the loop reconciles the
-- claim against the chain (via the pending-trades signature) so a
-- ladder rung can never double-sell after a restart between an
-- on-chain fill and the position close. status is 'open',
-- 'confirmed' or 'released'.
CREATE TABLE IF NOT EXISTS sell_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    coin_id TEXT NOT NULL,
    amount REAL NOT NULL,
    signature TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- Pit Sweeper shared brain (2026-11-23): the preset's versioned
-- rule sets. One row per published version; exactly one row per
-- preset is active at a time (active = 1) — every Pit bot runs the
-- active version's rules_json, adopted on its next loop pass. The
-- version is the name-tag number (1.0, 1.1, 1.2, ...); change_log
-- is the numbered changelog for that version, in order, so any
-- change can be pinpointed. Rollback = mark an older version
-- active again — the newer rows stay as history, nothing is
-- deleted. source is 'seed' (v1.0), 'auto-widen' (missed coins),
-- 'rollback', or 'james'.
CREATE TABLE IF NOT EXISTS preset_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_key TEXT NOT NULL,
    version TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    change_log TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (preset_key, version)
);

-- Pit Sweeper top-5 watch list (2026-11-23): per-bot memory of
-- the movers it watches. rank is the current top-5 rank (NULL
-- when the coin is not in the top 5 this scan); scrap_votes
-- counts consecutive scans where the coin showed resistance
-- (5-minute change at or below -2%, or flat under 0.5%) or
-- vanished from the feed — at 2 votes the coin is dead and the
-- next mover takes its place. status is 'active' or 'dead'.
CREATE TABLE IF NOT EXISTS pit_watchlist (
    bot_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    rank INTEGER,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    scrap_votes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    dead_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (bot_id, mint),
    FOREIGN KEY (bot_id) REFERENCES bots (id)
);

-- Pit Sweeper missed coins (2026-11-23): coins that gained 6.3%
-- or more in 5 minutes but sat ABOVE the mcap cap, so the bot
-- could not buy them. Global (the shared brain learns from every
-- user's misses, not one bot's). After 2 missed coins since the
-- current version was published, the shared brain raises the cap
-- to catch the next one earlier. status is 'missed' or 'rugged'
-- (a missed coin whose 5-minute change later fell 25% or more —
-- negative evidence, logged in the changelog, never counted as a
-- reason to widen on its own).
CREATE TABLE IF NOT EXISTS pit_missed_coins (
    mint TEXT PRIMARY KEY,
    mcap_usd REAL NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    peak_gain_5m REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'missed'
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
    # `users.email` is KEPT on purpose (owner, 2026-11-20): no
    # email is collected at signup any more, but older accounts
    # had their password hash and wallet key derived from
    # password+email, so their stored email must stay — blanking
    # it would lock them out and make their wallets undecryptable.
    # New accounts store ''. The column is never dropped.
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
    # Promo-code grants land in gate_fuel_purchases marked as
    # promo (James, 2026-11-24): source + promo_code name the
    # grant, so it is never mistaken for a paid package.
    ("gate_fuel_purchases", "source",
     "TEXT NOT NULL DEFAULT 'package'"),
    ("gate_fuel_purchases", "promo_code", "TEXT"),
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
    # Friction logging (2026-11-15): the full on-chain signature
    # and the executed SOL (spent on buys, received on sells) for
    # real trades, so round-trip friction can be measured from the
    # ledger alone. NULL for paper trades and for rows recorded
    # before these columns existed — the migration only adds the
    # columns, it never touches existing rows.
    ("trade_ledger", "signature", "TEXT"),
    ("trade_ledger", "sol_amount", "REAL"),
    # Pit Sweeper (2026-11-23): the preset version a bot currently
    # runs (its name-tag number, e.g. '1.0'), the per-bot bankroll
    # cap in SOL (0.20 for Pit — the loop never sizes from the
    # whole wallet balance), the preset version each ledger row ran
    # under (per-version win rate / P&L), and the per-position
    # exit-vote state for Pit's pullback / flat-dead exits (2
    # consecutive ticks before the exit fires — the debounce that
    # keeps one bad bucket from scrapping a position).
    ("bots", "preset_version", "TEXT NOT NULL DEFAULT ''"),
    ("bots", "bankroll_sol", "REAL"),
    ("trade_ledger", "preset_version", "TEXT"),
    ("bot_positions", "pit_exit_votes", "INTEGER NOT NULL DEFAULT 0"),
    ("bot_positions", "pit_exit_kind", "TEXT NOT NULL DEFAULT ''"),
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


def _migrate_gate_fuel_uses(conn: sqlite3.Connection) -> None:
    """Rebuild gate_fuel_uses from the holdings-era shape to the
    package shape (2026-11-23). The holdings-era table is
    (signature TEXT NOT NULL UNIQUE, tokens REAL NOT NULL) with no
    trade_id; the package table is (trade_id TEXT UNIQUE,
    signature TEXT UNIQUE) with the tokens column kept but no
    longer written. SQLite cannot ADD a UNIQUE column, so the
    table is rebuilt: old rows are copied across (id, user,
    signature, created_at — and their tokens values, kept as
    history), trade_id stays NULL on them, and the package
    balance starts from gate_fuel_purchases only — old rows never
    convert to credits. Idempotent: a table that already has
    trade_id is left alone. Fresh databases get the package shape
    straight from SCHEMA and never enter this branch."""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(gate_fuel_uses)").fetchall()}
    if not cols or "trade_id" in cols:
        return
    conn.execute(
        "CREATE TABLE gate_fuel_uses_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL, "
        "trade_id TEXT UNIQUE, "
        "signature TEXT UNIQUE, "
        "tokens REAL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "FOREIGN KEY (user_id) REFERENCES users (id))")
    conn.execute(
        "INSERT INTO gate_fuel_uses_new "
        "(id, user_id, trade_id, signature, tokens, created_at) "
        "SELECT id, user_id, NULL, signature, tokens, created_at "
        "FROM gate_fuel_uses")
    conn.execute("DROP TABLE gate_fuel_uses")
    conn.execute(
        "ALTER TABLE gate_fuel_uses_new RENAME TO gate_fuel_uses")


def _migrate_webauthn_challenges_user_nullable(
        conn: sqlite3.Connection) -> None:
    """Let webauthn_challenges.user_id be NULL (2026-11-25): a
    one-tap passkey sign-in issues its challenge before any user
    is known, so the challenge row carries no user. SQLite cannot
    relax a NOT NULL column in place, so the table is rebuilt:
    every existing row is copied across with its user_id intact
    (username-first challenges keep working exactly as before).
    Idempotent: a table whose user_id is already nullable is left
    alone. Fresh databases get the nullable shape straight from
    SCHEMA and never enter this branch. No credential rows are
    touched — passkey_credentials is unchanged."""
    info = conn.execute(
        "PRAGMA table_info(webauthn_challenges)").fetchall()
    if not info:
        return
    user_col = [r for r in info if r["name"] == "user_id"]
    if not user_col or not user_col[0]["notnull"]:
        return
    conn.execute(
        "CREATE TABLE webauthn_challenges_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER, "
        "purpose TEXT NOT NULL, "
        "challenge TEXT NOT NULL UNIQUE, "
        "expires_at TEXT NOT NULL, "
        "used INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "FOREIGN KEY (user_id) REFERENCES users (id))")
    conn.execute(
        "INSERT INTO webauthn_challenges_new "
        "(id, user_id, purpose, challenge, expires_at, used, created_at) "
        "SELECT id, user_id, purpose, challenge, expires_at, used, created_at "
        "FROM webauthn_challenges")
    conn.execute("DROP TABLE webauthn_challenges")
    conn.execute(
        "ALTER TABLE webauthn_challenges_new RENAME TO webauthn_challenges")


def _seed_promo_codes(conn: sqlite3.Connection) -> None:
    """Seed the first one-time promo codes (James, 2026-11-24):
    BetaGate1994 grants 1000 trades, usable ONCE IN TOTAL
    (global_once, max_uses 1); "Still on the gate" — the signup
    code, the motto phrase itself — grants 50 trades, usable ONCE
    PER ACCOUNT (per_account_once, no global cap). Idempotent
    (INSERT OR IGNORE): re-running init_db never resets the uses
    count or re-creates a spent code. Matching is exact
    (case-sensitive, spaces significant) — "Still on the gate"
    is lowercase gate, no $ and no question mark."""
    conn.execute(
        "INSERT OR IGNORE INTO promo_codes (code, trades, scope, "
        "max_uses) VALUES ('BetaGate1994', 1000, 'global_once', 1)")
    conn.execute(
        "INSERT OR IGNORE INTO promo_codes (code, trades, scope, "
        "max_uses) VALUES ('Still on the gate', 50, "
        "'per_account_once', NULL)")


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
    _migrate_gate_fuel_uses(conn)
    _migrate_webauthn_challenges_user_nullable(conn)
    _seed_promo_codes(conn)
    _backfill_pumpfun_wallets(conn)
    _backfill_signing_bots(conn)
    _backfill_builderbots(conn)
    # Bot modes (2026-11-26): existing preset bots get their invisible
    # mode suffix ("Middydemobot" / "Middyrealsol") so the two copies
    # of a preset are told apart without recreating anything.
    # Idempotent — already-suffixed and non-preset names are untouched.
    from .bot_names import backfill_bot_mode_suffixes

    backfill_bot_mode_suffixes(conn)
    _reset_paper_50_cad(conn)
    _reset_paper_100_cad(conn)
    _purge_generated_fee_wallets(conn)
    conn.commit()
    return conn
