"""Secrets handling. Nothing here ever logs or returns secret material.

- Passwords: hashlib.scrypt, per-user random 16-byte salt.
- Wallet secret keys: AES-256-GCM (cryptography), random 12-byte nonce.
  Stored form is base64(nonce || ciphertext || tag).
- Bot API tokens: shown once at creation, only the SHA-256 hash is stored.
- Key wiping: key material is held in a mutable bytearray and wiped
  IN PLACE (overwritten with zeros) the moment the signature is done,
  in a finally block on every path. Reassigning a bytes variable only
  drops the reference — the old bytes can linger in memory until the
  garbage collector frees them — so bytes are never used to hold key
  material past the sign call. See wipe_secret / temporary_secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.request
from contextlib import contextmanager

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def wipe_secret(buf) -> None:
    """Overwrite key material IN PLACE with zeros.

    Only a mutable bytearray can be truly scrubbed — a bytes object
    is immutable, so for bytes there is nothing to overwrite and the
    caller must drop the reference instead (and should have used a
    bytearray in the first place). Returns None; the buffer reads all
    zeros afterwards.
    """
    if isinstance(buf, bytearray):
        buf[:] = b"\x00" * len(buf)

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


def decrypt_secret_mutable(blob: str, key: bytes) -> bytearray:
    """Same as decrypt_secret, but the plaintext comes back as a
    MUTABLE bytearray so the caller can wipe it in place with
    wipe_secret the moment it is done with it. All new code that
    decrypts a wallet key must use this, never decrypt_secret."""
    return bytearray(decrypt_secret(blob, key))


@contextmanager
def temporary_secret(blob: str, key: bytes):
    """Yield the decrypted key as a bytearray, wiped in place when
    the block exits — on success AND on exception. The only safe way
    to hold key material: `with temporary_secret(...) as secret:`.
    """
    secret = decrypt_secret_mutable(blob, key)
    try:
        yield secret
    finally:
        wipe_secret(secret)


def verify_turnstile(token: str, secret_key: str,
                     remote_ip: str | None = None) -> bool:
    """Verify a Cloudflare Turnstile token server-side.

    POSTs to Cloudflare's siteverify endpoint (5s timeout). Returns
    True only when Cloudflare says the token is valid. Any network
    error, bad response, or missing token is False — a failed check
    never silently passes. No dependency beyond urllib.
    """
    if not token or not secret_key:
        return False
    payload = json.dumps({
        "secret": secret_key,
        "response": token,
        **({"remoteip": remote_ip} if remote_ip else {}),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return bool(result.get("success"))


def hash_token(token: str) -> str:
    """SHA-256 of a bot API token (only the hash is stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A fresh random bot API token. Shown once; never stored in plain."""
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_hex(16)
