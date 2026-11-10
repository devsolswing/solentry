"""Scrubber ("spoofer") for builderBot: deletes personal information
and wallet-key material from any text builderBot sees or stores,
BEFORE it is stored or logged.

What it catches (known patterns):
  * PEM private-key blocks (-----BEGIN ... PRIVATE KEY----- ...).
  * Solana / generic keypair JSON arrays — a JSON array of 64 bytes
    (0-255), and any long array of small ints that looks like one.
  * Long base58 strings (>= 80 chars) — a 64-byte secret key encodes
    to 87-88 base58 chars. Normal Solana addresses are 32-44 chars
    and are NOT touched, and neither are coin mints.
  * 64-byte hex secrets (128 hex chars).
  * Extended private keys (xprv...).
  * A message that is ONLY a 12- or 24-word lowercase seed phrase.
  * Labelled secrets: password=..., private key: ..., seed phrase:
    ..., secret=..., api key: ... and friends.
  * Email addresses and phone-number shapes.

Honest limits: this catches KNOWN patterns. It cannot prove an
unknown secret format was caught — a secret in a format this file
does not know about (a custom encoding, a split-across-messages
key, an image) will pass through. That is why builderBot also never
receives key material by design (it never calls the decrypt
functions and is never handed a wallet), and why its working memory
is wiped after each bot is created. The scrubber is a second layer,
not a guarantee.
"""

from __future__ import annotations

import re

REDACTED = "[removed]"

# -----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*"
    r"PRIVATE KEY-----", re.DOTALL)

# A JSON array of 64 ints 0-255 (a Solana keypair), tolerating
# whitespace and newlines. A shorter array (>= 32 ints) is caught
# too — no honest chat answer is a long array of small integers.
_KEYPAIR_RE = re.compile(
    r"\[\s*(?:\d{1,3}\s*,\s*){31,}\d{1,3}\s*\]")

# Long base58 strings: 64-byte secrets encode to 87-88 chars. The
# threshold (80) keeps normal 32-44 char addresses and mints safe.
_BASE58_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{80,}\b")

# 64-byte secret as hex (128 hex chars), word-bounded.
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{128}\b")

# Extended private keys.
_XPRV_RE = re.compile(r"\bxprv[1-9A-HJ-NP-Za-km-z]{100,}\b")

# Labelled secrets: "password: hunter2", "private key = [...]" etc.
# The value runs to the end of the line / a comma / a quote.
_LABELLED_RE = re.compile(
    r"(?i)\b(password|passwd|private[\s_-]?key|secret[\s_-]?key|"
    r"seed[\s_-]?phrase|recovery[\s_-]?phrase|mnemonic|api[\s_-]?key|"
    r"auth[\s_-]?token|access[\s_-]?token)\b\s*[:=]\s*"
    r"[^\s,;\"'}\]]+")

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Phone-ish: an optional + then 10+ digits with separators, or a
# bare 10-15 digit run. Short numbers (amounts, percentages, years)
# are untouched.
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")

_WORD_RE = re.compile(r"^[a-z]{3,8}$")


def _is_seed_phrase(text: str) -> bool:
    """True when the whole message is exactly a 12- or 24-word
    lowercase phrase — the shape of a pasted seed phrase. A normal
    sentence has punctuation, capitals, numbers or a different word
    count, so it does not match."""
    words = text.strip().split()
    if len(words) not in (12, 24):
        return False
    return all(_WORD_RE.match(w) for w in words)


def _scrub_phones(text: str) -> tuple[str, int]:
    """Redact phone-number shapes: at least 10 digits in the run,
    so dates (2026-11-09, 8 digits) and amounts are untouched."""
    count = 0

    def _check(match):
        nonlocal count
        digits = sum(ch.isdigit() for ch in match.group(0))
        if digits >= 10:
            count += 1
            return REDACTED
        return match.group(0)

    return _PHONE_RE.sub(_check, text), count


def scrub_text(text: str) -> tuple[str, int]:
    """Return (cleaned_text, number_of_redactions). Idempotent: a
    second pass finds nothing new to redact. Never raises — on any
    surprise it returns the input with the secret-shaped parts
    replaced by the marker."""
    if not isinstance(text, str):
        text = str(text)
    redactions = 0

    def _sub(pattern, value):
        nonlocal redactions
        cleaned, n = pattern.subn(REDACTED, value)
        redactions += n
        return cleaned

    text = _sub(_PEM_RE, text)
    text = _sub(_KEYPAIR_RE, text)
    text = _sub(_XPRV_RE, text)
    text = _sub(_BASE58_RE, text)
    text = _sub(_HEX_RE, text)
    text = _sub(_LABELLED_RE, text)
    text = _sub(_EMAIL_RE, text)
    text, n_phones = _scrub_phones(text)
    redactions += n_phones
    if _is_seed_phrase(text):
        redactions += 1
        text = REDACTED
    return text, redactions


def scrub(text: str) -> str:
    """scrub_text without the count — for call sites that only need
    the cleaned value."""
    cleaned, _n = scrub_text(text)
    return cleaned


def looks_like_secret(text: str) -> bool:
    """True when the text contains any redaction pattern. Used by
    builderBot to refuse a paste outright instead of storing it."""
    _cleaned, n = scrub_text(text)
    return n > 0
