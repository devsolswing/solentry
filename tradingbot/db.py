"""SQLite storage. Every query is scoped by user_id at the call site.

Schema:
  users, wallets (encrypted secret), paper_portfolio, paper_holdings,
  paper_trades, bots, bot_proposals, send_intents, activity,
  login_attempts (rate limiting), passkey_credentials (WebAuthn),
  webauthn_challenges (single-use ceremony challenges).
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id)
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

-- Project donation wallet: ONE wallet for the whole project, generated
-- once at first use, encrypted like user wallets. It belongs to the
-- project — never to any user's personal wallet, and never to James's
-- personal Phantom wallet.
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
)


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
    conn.commit()
    return conn
