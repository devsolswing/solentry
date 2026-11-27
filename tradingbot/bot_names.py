"""Stored bot names carry an invisible mode suffix (James, 2026-11-26).

The same preset bot can run in paper and real at the same time, as
two separate bots. The two copies are told apart in the stored
record by a suffix on the name — paper "demobot", real "realsol"
("Middydemobot" / "Middyrealsol") — which is NEVER shown in the
app: every surface that renders a bot name strips it back to the
plain name ("Middy").

Rules (James's spec, confirmed 2026-11-26 ~21:44):
- Only preset / assistant-preset / builderBot-created bots get the
  suffix. A name typed into POST /bots/create (or the assistant's
  generic bot_create) is stored exactly as typed — the mode column
  already tells those apart.
- The suffix is appended directly to the stored base name, with no
  separator ("Middy" + "demobot"). Pit keeps its version tag first:
  "Pit Sweeper v1.0demobot".
- display_bot_name() strips one trailing "demobot" / "realsol".
  It never strips to an empty string, and it never touches a name
  that does not end in exactly one of those two suffixes.
- The backfill is idempotent: a name that already ends with its
  mode's suffix is left alone, and a name that is not a preset
  bot's name is never touched.
"""

from __future__ import annotations

import sqlite3

# Suffix by stored bot mode ('paper' / 'real'). Unknown modes get no
# suffix — never invent one.
MODE_SUFFIXES = {"paper": "demobot", "real": "realsol"}

# Base names of the six preset bots (see rules.py *_PRESET_NAME).
# Pit stores its version in the name ("Pit Sweeper v1.0"), so it
# matches by prefix + version number, not by exact equality.
PRESET_BASE_NAMES = (
    "Trenches",
    "Scanner",
    "Clyde",
    "Gnat",
    "Pit Sweeper",
    "Middy",
)


def mode_suffix(mode: str) -> str:
    """The stored-name suffix for a bot mode ("" for unknown modes)."""
    return MODE_SUFFIXES.get(mode or "", "")


def suffixed_name(base_name: str, mode: str) -> str:
    """A base name with its mode suffix appended ("Middy", "paper"
    -> "Middydemobot")."""
    return (base_name or "") + mode_suffix(mode)


def display_bot_name(name) -> str:
    """The name as the app shows it: the stored mode suffix, if
    any, stripped off. A name with no suffix comes back unchanged;
    a bare "demobot" / "realsol" is never stripped to empty."""
    if not name:
        return ""
    name = str(name)
    for suffix in ("demobot", "realsol"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def preset_base_name(name):
    """The preset base name for a stored name, or None.

    Strips the mode suffix first, then matches the six preset
    names. Pit stores its version in the name ("Pit Sweeper v1.0"),
    so any "Pit Sweeper v<digit>..." name matches and the whole
    versioned name is the base.
    """
    base = display_bot_name(name)
    if base in PRESET_BASE_NAMES:
        return base
    if base.startswith("Pit Sweeper v"):
        rest = base[len("Pit Sweeper v"):]
        if rest and rest[0].isdigit():
            return base
    return None


def backfill_bot_mode_suffixes(conn: sqlite3.Connection) -> int:
    """One-time, idempotent backfill: existing preset bots get their
    mode suffix, so no bot has to be recreated for this change.

    Only names that match a known preset base are touched, and only
    when the stored name does not already end with that mode's
    suffix. A bot whose mode was switched since creation is healed
    to its current mode's suffix. Returns the number of bots
    renamed.
    """
    count = 0
    for row in conn.execute("SELECT id, name, mode FROM bots").fetchall():
        base = preset_base_name(row["name"])
        if base is None:
            continue
        wanted = suffixed_name(base, row["mode"])
        if row["name"] == wanted:
            continue
        conn.execute("UPDATE bots SET name = ? WHERE id = ?",
                     (wanted, row["id"]))
        count += 1
    return count
