"""Rate limiting, stored in the app database (not in memory) so a
restart does not clear the counters and the silent-loop thread and
the web process share the same limits.

One row per key: how many hits in the current window and when the
window started. A hit past the limit is refused — the caller gets
False and must NOT perform the action. Windows are in UTC seconds.

Keys are namespaced, e.g. "login-fail:ip:1.2.3.4",
"signup:ip:1.2.3.4", "send:user:7", "burst:ip:1.2.3.4".
"""

from __future__ import annotations

import time


def record_hit(db, key: str, max_hits: int,
               window_seconds: int) -> bool:
    """Record one hit for `key`. Returns True when the hit is within
    the limit (and was counted), False when the key is already over
    the limit (the hit is NOT counted, so the limit never drifts
    while an attacker keeps hammering).
    """
    now = int(time.time())
    row = db.execute(
        "SELECT hits, window_start FROM rate_limits WHERE key = ?",
        (key,)).fetchone()
    if row is None or now - int(row["window_start"]) >= window_seconds:
        db.execute(
            "INSERT INTO rate_limits (key, hits, window_start) "
            "VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET "
            "hits = 1, window_start = excluded.window_start",
            (key, now))
        db.commit()
        return True
    if int(row["hits"]) >= max_hits:
        return False
    db.execute(
        "UPDATE rate_limits SET hits = hits + 1 WHERE key = ?",
        (key,))
    db.commit()
    return True


def hits_in_window(db, key: str, window_seconds: int) -> int:
    """How many hits `key` has in its current window (0 when the
    window expired or the key was never seen). Read-only."""
    now = int(time.time())
    row = db.execute(
        "SELECT hits, window_start FROM rate_limits WHERE key = ?",
        (key,)).fetchone()
    if row is None or now - int(row["window_start"]) >= window_seconds:
        return 0
    return int(row["hits"])


def reset_key(db, key: str) -> None:
    """Clear a key (used by tests and after a successful login)."""
    db.execute("DELETE FROM rate_limits WHERE key = ?", (key,))
    db.commit()
