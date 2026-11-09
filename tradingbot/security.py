"""Secrets handling. Nothing here ever logs or returns secret material.

- Passwords: hashlib.scrypt, per-user random 16-byte salt.
- Wallet secret keys: AES-256-GCM (cryptography), random 12-byte nonce.
  Stored form is base64(nonce || ciphertext || tag).
- Bot API tokens: shown once at creation, only the SHA-256 hash is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# scrypt cost parameters (OWASP-recommended interactive values).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for a new password."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time password check. Returns False on any malformed input."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(digest, expected)


def encrypt_secret(plaintext: bytes, key: bytes) -> str:
    """AES-256-GCM encrypt. Returns base64(nonce || ciphertext+tag)."""
    if len(key) != 32:
        raise ValueError("encryption key must be 32 bytes")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(blob: str, key: bytes) -> bytes:
    """Reverse of encrypt_secret. Raises ValueError on bad blob/key."""
    if len(key) != 32:
        raise ValueError("encryption key must be 32 bytes")
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception as exc:
        raise ValueError("bad encrypted blob") from exc
    if len(raw) < 12 + 16:
        raise ValueError("bad encrypted blob")
    nonce, ciphertext = raw[:12], raw[12:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("decryption failed") from exc


def hash_token(token: str) -> str:
    """SHA-256 of a bot API token (only the hash is stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A fresh random bot API token. Shown once; never stored in plain."""
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_hex(16)
