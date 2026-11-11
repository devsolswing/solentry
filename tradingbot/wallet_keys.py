"""Per-user wallet-key layer: the password + email second level.

The wallet key (the app wallet's private key) is encrypted in two
levels:

  * Level 1 — the server key (ENCRYPTION_KEY, as before). The
    signing bot needs this to sign the user's real trades while
    they are offline, so the server must be able to unlock the
    wallet without the user present. The server key stays the
    root of trust for offline signing — a custodial app cannot
    sign silently without it.
  * Level 2 — the user key, derived from BOTH the password AND
    the email together (scrypt, domain-separated, per-user salt).
    The email is a required second factor: the user key cannot be
    derived without the account's email, and the password hash
    cannot be verified without it either (see
    security.hash_account_password).

How it fits together (wallets.enc_version):
  * v1 (legacy): the wallet secret is encrypted with the server
    key directly. Accounts created before this layer keep working
    — they are upgraded transparently at their next login (the
    password is available then, so the user key can be derived
    and the wallet re-encrypted).
  * v2: the wallet secret is encrypted with the user key, and
    the user key itself is stored wrapped with the server key
    (users.wrapped_user_key). The signing bot unwraps the user
    key with the server key, decrypts the wallet, signs, and
    wipes BOTH the user key and the wallet secret from memory in
    finally blocks — after every transaction, nothing lingers.

Key material is always held in mutable bytearrays and wiped IN
PLACE with wipe_secret the moment it is done being used, on every
path. No user key, wallet secret, or seed is ever logged.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager

from .security import (decrypt_secret, decrypt_secret_mutable,
                       encrypt_secret, wipe_secret)

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def derive_user_key(password: str, email: str, salt_hex: str) -> bytearray:
    """The per-user key, derived from password AND email together.

    The scrypt input is domain-separated ("user-key") so it can
    never collide with the password hash, even for the same
    password+email. The email is lowercased (emails are
    case-insensitive). Returns a mutable bytearray so the caller
    can wipe it in place with wipe_secret when done. Raises
    ValueError on a malformed salt.
    """
    salt = bytes.fromhex(salt_hex)
    material = ("user-key\x00" + password + "\x00"
                + (email or "").strip().lower()).encode("utf-8")
    return bytearray(hashlib.scrypt(
        material, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_SCRYPT_DKLEN))


def new_user_key_salt() -> str:
    """A fresh random 16-byte salt (hex) for a user key."""
    return os.urandom(16).hex()


def wrap_user_key(user_key: bytes, server_key: bytes) -> str:
    """Encrypt a user key with the server key for storage."""
    return encrypt_secret(user_key, server_key)


def unwrap_user_key(wrapped: str, server_key: bytes) -> bytearray:
    """Decrypt a wrapped user key as a mutable bytearray (wipe it
    with wipe_secret when done). Raises ValueError on bad input."""
    return decrypt_secret_mutable(wrapped, server_key)


def _decrypt_with_user_key(blob: str, user_key: bytes) -> bytearray:
    return decrypt_secret_mutable(blob, user_key)


def decrypt_wallet_secret(db, user_id: int, server_key: bytes):
    """Return the user's app-wallet secret as a mutable bytearray,
    or None when there is no wallet or it cannot be decrypted.

    v1 wallets decrypt with the server key (legacy). v2 wallets
    decrypt by unwrapping the user key with the server key and
    then decrypting with the user key — the user key is wiped in
    place the moment the wallet secret is out. The caller owns
    the returned bytearray and must wipe it with wipe_secret.
    """
    row = db.execute(
        "SELECT encrypted_secret, enc_version FROM wallets "
        "WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    enc_version = int(row["enc_version"] or 1)
    if enc_version == 1:
        try:
            return decrypt_secret_mutable(row["encrypted_secret"],
                                          server_key)
        except ValueError:
            return None
    # v2: unwrap the user key, then decrypt with it.
    user_row = db.execute(
        "SELECT wrapped_user_key FROM users WHERE id = ?",
        (user_id,)).fetchone()
    if not user_row or not user_row["wrapped_user_key"]:
        return None
    user_key = None
    try:
        user_key = unwrap_user_key(user_row["wrapped_user_key"], server_key)
        return _decrypt_with_user_key(row["encrypted_secret"],
                                      bytes(user_key))
    except ValueError:
        return None
    finally:
        if user_key is not None:
            wipe_secret(user_key)


@contextmanager
def temporary_wallet_secret(db, user_id: int, server_key: bytes):
    """Yield the decrypted app-wallet secret as a bytearray, wiped
    in place when the block exits — on success AND on exception.
    The only safe way to hold the wallet secret in a route: use
    this instead of decrypting the blob directly."""
    secret = decrypt_wallet_secret(db, user_id, server_key)
    try:
        yield secret
    finally:
        if secret is not None:
            wipe_secret(secret)


def rewrap_wallet_to_user_key(db, user_id: int, server_key: bytes,
                              user_key: bytes) -> bool:
    """Re-encrypt the user's app wallet with a (new) user key and
    store the user key wrapped with the server key. Used when a
    password changes (login upgrade, forgot-password reset,
    Recover Account reset) so the wallet key always follows the
    current password+email.

    Returns True when a wallet was re-encrypted, False when the
    user has no wallet. The old secret and any intermediate key
    material are wiped in place. The caller owns `user_key` (a
    plain bytes object here) and must wipe its own copy if it
    holds one in a bytearray.
    """
    secret = decrypt_wallet_secret(db, user_id, server_key)
    if secret is None:
        return False
    try:
        blob = encrypt_secret(bytes(secret), user_key)
    finally:
        wipe_secret(secret)
    wrapped = wrap_user_key(user_key, server_key)
    db.execute(
        "UPDATE wallets SET encrypted_secret = ?, enc_version = 2 "
        "WHERE user_id = ?", (blob, user_id))
    db.execute(
        "UPDATE users SET wrapped_user_key = ? WHERE id = ?",
        (wrapped, user_id))
    db.commit()
    return True
