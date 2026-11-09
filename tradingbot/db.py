"""SQLite storage. Every query is scoped by user_id at the call site.

Schema:
  users, wallets (encrypted secret), paper_portfolio, paper_holdings,
  paper_trades, bots, bot_proposals, send_intents, activity,
  login_attempts (rate limiting), verification_codes (phone/email
  linking and two-code recovery; salted SHA-256 hashes only).
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone_verified INTEGER NOT NULL DEFAULT 0,
    email_verified INTEGER NOT NULL DEFAULT 0,
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

-- Verification codes for phone/email linking and two-code account
-- recovery. Only a salted SHA-256 hash is stored — the plaintext code
-- never reaches this table. One live code per (user, purpose, contact);
-- purpose is 'link' or 'recover', contact is the phone (E.164) or the
-- email the code was sent to.
CREATE TABLE IF NOT EXISTS verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    contact TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, purpose, contact),
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
    ("users", "phone", "TEXT NOT NULL DEFAULT ''"),
    ("users", "email", "TEXT NOT NULL DEFAULT ''"),
    ("users", "phone_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "email_verified", "INTEGER NOT NULL DEFAULT 0"),
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
