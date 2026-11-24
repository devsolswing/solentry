"""Pit Sweeper shared brain (2026-11-23): the versioned preset
record, the missed-coins log, and the top-5 watch list.

James's order: one brain for every Pit bot. Rule changes apply
across the board in real time — when the brain publishes a new
version, every Pit bot adopts it on its next loop pass. The
version is the name-tag number (1.0, 1.1, 1.2, ...); every change
is logged with a number in the changelog, in order; any whole
update can be scrapped to downgrade to a previous version.

The audit's safety bar (10+ missed coins, canary user, 24-hour
soak, human approval before a widening ships) is recorded in the
build MANIFEST as a flagged risk James accepted — it is NOT what
ships. What ships is his order: 2 missed coins above the mcap
cap raise the cap. Missed coins that later rug are logged as
negative evidence in the changelog entry, never counted as a
reason to widen on their own.

Nothing here ever touches a wallet key, a signing key, or a
trade. The loop calls this module; this module never calls the
loop. Fail-closed: any failure here leaves the current version
in place and the bots keep trading on it.
"""

import json
import math
import re
from datetime import datetime, timezone

from .rules import (PIT_MISSED_COINS_TO_WIDEN, PIT_MISSED_RUG_DROP_PCT,
                    PIT_PRESET_NAME, PIT_PRESET_VERSION_INITIAL,
                    pit_ruleset, validate_ruleset)

PIT_PRESET_KEY = "pit"
PIT_WATCHLIST_SIZE = 5
PIT_SCRAP_VOTES_TO_DIE = 2


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value):
    """Parse a stored timestamp in either SQLite format
    ('YYYY-MM-DD HH:MM:SS') or ISO format ('...T...Z'). None when
    unparsable — a bad timestamp never decides a version change."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _bump_version(version: str) -> str | None:
    """'1.0' -> '1.1', '1.9' -> '1.10'. None when unparsable."""
    try:
        major, minor = version.split(".")
        return f"{int(major)}.{int(minor) + 1}"
    except (ValueError, AttributeError):
        return None


def _next_changelog_number(db) -> int:
    """Next changelog entry number: one past the highest numbered
    entry across every Pit version row."""
    highest = 0
    try:
        rows = db.execute(
            "SELECT change_log FROM preset_versions "
            "WHERE preset_key = ?", (PIT_PRESET_KEY,)).fetchall()
    except Exception:
        return 1
    for row in rows:
        for line in (row["change_log"] or "").splitlines():
            m = re.match(r"(\d+)\.\s", line.strip())
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def ensure_pit_version_1_0(db) -> str:
    """Seed the v1.0 version row (the complete first build) if no
    Pit version exists yet. Returns the active version string."""
    active = current_pit_version(db)
    if active is not None:
        return active["version"]
    rules = pit_ruleset()
    number = _next_changelog_number(db)
    changelog = (
        f"{number}. Pit Sweeper v1.0 — first complete build: top-5 "
        f"watch list, 6.3% in 5 minutes, 0.05 SOL per trade from a "
        f"0.20 SOL bankroll cap, sell all at +12%, stop loss 4.9%, "
        f"breakeven +6%, pullback -2% in 5 minutes, flat-dead, "
        f"10-minute cap, daily-loss breaker, curve band 15-65%, "
        f"buys at least 2x sells.")
    db.execute(
        "INSERT INTO preset_versions (preset_key, version, rules_json, "
        "change_log, source, active, created_at) "
        "VALUES (?, ?, ?, ?, 'seed', 1, ?)",
        (PIT_PRESET_KEY, PIT_PRESET_VERSION_INITIAL,
         json.dumps(rules), changelog, _now()))
    db.commit()
    return PIT_PRESET_VERSION_INITIAL


def current_pit_version(db):
    """The active Pit version row (dict) or None."""
    try:
        row = db.execute(
            "SELECT * FROM preset_versions WHERE preset_key = ? "
            "AND active = 1 ORDER BY id DESC LIMIT 1",
            (PIT_PRESET_KEY,)).fetchone()
    except Exception:
        return None
    return dict(row) if row is not None else None


def pit_version_rules(db):
    """The active Pit version's rules dict, or None when there is
    no active version."""
    active = current_pit_version(db)
    if active is None:
        return None
    try:
        return json.loads(active["rules_json"])
    except (ValueError, TypeError):
        return None


def publish_pit_version(db, rules: dict, change_entries: list,
                        source: str) -> str:
    """Publish a new Pit version: the next +0.1 number, the given
    rules (validated first), and the given changelog entries
    (numbered here, in order). The new version becomes active and
    every Pit bot adopts it on its next loop pass. Returns the new
    version string. Raises ValueError on invalid rules — the caller
    must not publish a broken version."""
    cleaned, error = validate_ruleset(rules)
    if error is not None:
        raise ValueError(f"cannot publish invalid pit rules: {error}")
    active = current_pit_version(db)
    if active is None:
        base = PIT_PRESET_VERSION_INITIAL
    else:
        base = _bump_version(active["version"])
        if base is None:
            raise ValueError(
                f"cannot bump unparsable version {active['version']!r}")
    number = _next_changelog_number(db)
    lines = []
    for entry in change_entries:
        lines.append(f"{number}. {entry}")
        number += 1
    db.execute("UPDATE preset_versions SET active = 0 "
               "WHERE preset_key = ?", (PIT_PRESET_KEY,))
    db.execute(
        "INSERT INTO preset_versions (preset_key, version, rules_json, "
        "change_log, source, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (PIT_PRESET_KEY, base, json.dumps(cleaned), "\n".join(lines),
         source, _now()))
    db.commit()
    return base


def rollback_pit_version(db, target_version: str) -> tuple[bool, str]:
    """Scrap the current update and downgrade every Pit bot to
    `target_version`: the target row becomes active, the newer
    rows stay as history. The rollback itself is logged as a
    numbered changelog entry on the target row, so the downgrade
    can be pinpointed like any other change. Returns (ok, note)."""
    try:
        target = db.execute(
            "SELECT * FROM preset_versions WHERE preset_key = ? "
            "AND version = ?", (PIT_PRESET_KEY, target_version)).fetchone()
    except Exception:
        return False, "rollback failed — version store unreadable"
    if target is None:
        return False, f"no Pit version {target_version} to roll back to"
    active = current_pit_version(db)
    from_version = active["version"] if active else "none"
    if active is not None and active["version"] == target_version:
        return False, f"Pit is already on v{target_version}"
    number = _next_changelog_number(db)
    entry = (f"{number}. Rolled back to v{target_version} from "
             f"v{from_version} — the v{from_version} update was "
             f"scrapped in full.")
    old_log = target["change_log"] or ""
    new_log = (old_log + "\n" + entry) if old_log else entry
    db.execute("UPDATE preset_versions SET active = 0 "
               "WHERE preset_key = ?", (PIT_PRESET_KEY,))
    db.execute(
        "UPDATE preset_versions SET active = 1, change_log = ? "
        "WHERE preset_key = ? AND version = ?",
        (new_log, PIT_PRESET_KEY, target_version))
    db.commit()
    return True, (f"Pit rolled back to v{target_version} from "
                  f"v{from_version}")


def maybe_adopt_pit_version(db, bot):
    """Adopt the active Pit version for one bot, if it runs an
    older one: its stored rules, its preset_version and its name
    tag ('Pit Sweeper vX.Y') are all refreshed from the active
    version. Returns the re-fetched bot row when adoption happened,
    else the bot row unchanged. A bot with no readable pit rules
    is left alone — adoption never writes a broken rule set."""
    try:
        raw = bot["rules_json"]
    except (KeyError, TypeError, IndexError):
        return bot
    if not raw or (isinstance(raw, str) and not raw.strip()):
        return bot
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return bot
    if not isinstance(parsed, dict) or parsed.get("kind") != "pit":
        return bot
    active = current_pit_version(db)
    if active is None:
        return bot
    try:
        current = bot["preset_version"]
    except (KeyError, TypeError, IndexError):
        current = ""
    if current == active["version"]:
        return bot
    db.execute(
        "UPDATE bots SET rules_json = ?, preset_version = ?, "
        "name = ? WHERE id = ?",
        (active["rules_json"], active["version"],
         f"{PIT_PRESET_NAME} v{active['version']}", bot["id"]))
    db.commit()
    fresh = db.execute("SELECT * FROM bots WHERE id = ?",
                       (bot["id"],)).fetchone()
    return fresh if fresh is not None else bot


def record_pit_missed_coin(db, mint: str, mcap_usd: float,
                           gain_5m: float) -> None:
    """Log a missed coin: it gained 6.3% or more in 5 minutes but
    sat above the mcap cap, so the bot could not buy it. One row
    per mint, updated on every sighting (peak gain, last seen).
    Never raises — a logging failure must never break a scan."""
    try:
        db.execute(
            "INSERT INTO pit_missed_coins (mint, mcap_usd, "
            "first_seen_at, last_seen_at, peak_gain_5m, status) "
            "VALUES (?, ?, ?, ?, ?, 'missed') "
            "ON CONFLICT(mint) DO UPDATE SET "
            "mcap_usd = excluded.mcap_usd, "
            "last_seen_at = excluded.last_seen_at, "
            "peak_gain_5m = MAX(peak_gain_5m, excluded.peak_gain_5m)",
            (mint, float(mcap_usd), _now(), _now(), float(gain_5m)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def pit_review_missed_coins(db, price_map: dict | None) -> str:
    """The shared brain's missed-coin review, run once per loop
    pass after the evaluations. Missed coins still in the feed
    whose 5-minute change has since fallen 25% or more are marked
    rugged (negative evidence). When at least 2 missed coins have
    been recorded since the active version was published, a new
    version is published with the mcap cap raised to the highest
    missed mcap, rounded up to the next $5,000 — James's order
    (2 missed coins -> raise the cap). Returns a note when a
    version was published, else ''."""
    active = current_pit_version(db)
    if active is None:
        return ""
    price_map = price_map or {}
    # Mark rugged missed coins from the live feed.
    try:
        missed = db.execute(
            "SELECT mint FROM pit_missed_coins "
            "WHERE status = 'missed'").fetchall()
    except Exception:
        return ""
    for row in missed:
        entry = price_map.get(row["mint"]) or {}
        change = entry.get("change_5m_pct")
        try:
            change = float(change) if change is not None else None
        except (TypeError, ValueError):
            change = None
        if change is not None and change <= -PIT_MISSED_RUG_DROP_PCT:
            try:
                db.execute(
                    "UPDATE pit_missed_coins SET status = 'rugged' "
                    "WHERE mint = ?", (row["mint"],))
            except Exception:
                pass
    try:
        db.commit()
    except Exception:
        pass
    # Qualifying misses since the active version was published.
    since = _parse_ts(active["created_at"])
    try:
        rows = db.execute(
            "SELECT mint, mcap_usd, first_seen_at FROM "
            "pit_missed_coins WHERE status = 'missed'").fetchall()
    except Exception:
        return ""
    qualifying = []
    for row in rows:
        first = _parse_ts(row["first_seen_at"])
        if since is None or first is None or first >= since:
            qualifying.append(row)
    if len(qualifying) < PIT_MISSED_COINS_TO_WIDEN:
        return ""
    top_mcap = max(float(r["mcap_usd"]) for r in qualifying)
    new_cap = math.ceil(top_mcap / 5000.0) * 5000.0
    try:
        rules = json.loads(active["rules_json"])
    except (ValueError, TypeError):
        return ""
    old_cap = float(rules["buy_filter"]["max_market_cap_usd"])
    if new_cap <= old_cap:
        return ""
    try:
        rugged = db.execute(
            "SELECT COUNT(*) FROM pit_missed_coins "
            "WHERE status = 'rugged'").fetchone()[0]
    except Exception:
        rugged = 0
    rules["buy_filter"]["max_market_cap_usd"] = new_cap
    missed_desc = ", ".join(
        f"{r['mint'][:6]} ${float(r['mcap_usd']):,.0f}"
        for r in qualifying)
    entries = [
        f"Max market cap ${old_cap:,.0f} -> ${new_cap:,.0f} after "
        f"{len(qualifying)} missed coins above the cap "
        f"({missed_desc})",
    ]
    if rugged:
        entries.append(
            f"Negative evidence logged: {rugged} missed coin(s) "
            f"later rugged — counted, not used to widen on their "
            f"own.")
    try:
        version = publish_pit_version(db, rules, entries, "auto-widen")
    except ValueError as exc:
        return f"pit auto-widen refused: {exc}"
    return (f"pit shared brain: published v{version} — max market "
            f"cap ${old_cap:,.0f} -> ${new_cap:,.0f} after "
            f"{len(qualifying)} missed coins")


def update_pit_watchlist(db, bot_id: int, movers: list,
                         price_map: dict, pit_settings: dict,
                         now_str: str) -> None:
    """Refresh one bot's top-5 watch list from this scan's movers
    (top 5 by 5-minute gain, strongest first). Movers are upserted
    with their rank and a clean scrap count. An active watchlist
    coin that shows resistance (5-minute change at or below the
    -2% pullback, or flat under 0.5%) or vanishes from the feed
    earns a scrap vote; at 2 consecutive votes it is dead and the
    next mover takes its place. A coin that is healthy again has
    its votes cleared. Never raises — a watchlist failure must
    never break a scan."""
    pullback = float(pit_settings.get("pullback_pct", 2.0))
    flat = float(pit_settings.get("flat_pct", 0.5))
    try:
        mover_mints = set()
        for rank, entry in enumerate(movers[:PIT_WATCHLIST_SIZE], 1):
            mint = entry.get("mint")
            if not mint:
                continue
            mover_mints.add(mint)
            db.execute(
                "INSERT INTO pit_watchlist (bot_id, mint, rank, "
                "first_seen_at, last_seen_at, scrap_votes, status, "
                "dead_reason) VALUES (?, ?, ?, ?, ?, 0, 'active', '') "
                "ON CONFLICT(bot_id, mint) DO UPDATE SET rank = "
                "excluded.rank, last_seen_at = excluded.last_seen_at, "
                "scrap_votes = 0, status = 'active', dead_reason = ''",
                (bot_id, mint, rank, now_str, now_str))
        active = db.execute(
            "SELECT mint, scrap_votes FROM pit_watchlist "
            "WHERE bot_id = ? AND status = 'active'",
            (bot_id,)).fetchall()
        for row in active:
            mint = row["mint"]
            if mint in mover_mints:
                continue
            entry = (price_map or {}).get(mint)
            votes = int(row["scrap_votes"] or 0)
            reason = ""
            if entry is None:
                votes += 1
                reason = "vanished from feed"
            else:
                change = entry.get("change_5m_pct")
                try:
                    change = float(change) if change is not None else None
                except (TypeError, ValueError):
                    change = None
                if change is None:
                    votes += 1
                    reason = "no 5-minute change data"
                elif change <= -pullback:
                    votes += 1
                    reason = (f"resistance: 5-minute change "
                              f"{change:.1f}%")
                elif abs(change) <= flat:
                    votes += 1
                    reason = (f"flat: 5-minute change {change:.1f}% — "
                              f"dead")
                else:
                    votes = 0
            if votes >= PIT_SCRAP_VOTES_TO_DIE:
                db.execute(
                    "UPDATE pit_watchlist SET status = 'dead', "
                    "scrap_votes = ?, dead_reason = ?, "
                    "last_seen_at = ? WHERE bot_id = ? AND mint = ?",
                    (votes, reason, now_str, bot_id, mint))
            else:
                db.execute(
                    "UPDATE pit_watchlist SET scrap_votes = ?, "
                    "last_seen_at = ? WHERE bot_id = ? AND mint = ?",
                    (votes, now_str, bot_id, mint))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def pit_changelog(db) -> list:
    """The full numbered Pit changelog, in order — every version's
    entries, oldest first."""
    try:
        rows = db.execute(
            "SELECT change_log FROM preset_versions "
            "WHERE preset_key = ? ORDER BY id",
            (PIT_PRESET_KEY,)).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        for line in (row["change_log"] or "").splitlines():
            if line.strip():
                out.append(line)
    # Numbered in order: a rollback appends to an OLD version's
    # row, so row order is not entry order — sort by the entry
    # number so the changelog always reads 1, 2, 3, ...
    def _entry_number(line):
        m = re.match(r"(\d+)\.\s", line.strip())
        return int(m.group(1)) if m else 10 ** 9
    return sorted(out, key=_entry_number)


def log_pit_price_gate_refusal(db, user_id: int, bot_id: int,
                               mint: str, reason: str, mode: str) -> None:
    """Record a price-gate refusal for a Pit bot in the activity
    log, so the refusal rate can be measured on paper: the gate
    refuses buys exactly when the entry signal is strongest, and
    a rate nobody measures is a gate nobody can tune. Never
    raises."""
    try:
        db.execute(
            "INSERT INTO activity (user_id, kind, detail, created_at, "
            "mode) VALUES (?, 'pit_price_gate_refusal', ?, ?, ?)",
            (user_id,
             f"Pit bot {bot_id}: price gate refused {mint[:12]} — "
             f"{reason[:160]}", _now(), mode))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
