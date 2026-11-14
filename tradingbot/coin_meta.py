"""Coin identity + logos for held pump.fun coins: real official name,
ticker, and logo — fetched once per coin the user holds, stored in
the DB, never bulk-loaded.

Sources, in order (first hit wins per field):
1. On-chain Metaplex metadata — pump.fun writes this at creation, so
   it is the coin's official name/symbol/URI on devnet AND mainnet.
   The metadata account lives at the standard PDA
   ["metadata", metadata-program, mint]; its URI JSON carries the
   `image` (the official logo). The pump.fun frontend API is NOT
   used: it sits behind a Cloudflare challenge and cannot be read
   server-side (verified 2026-11-13). A dead URI (Gate's metadata
   file is gone from its host) simply yields no image.
2. DexScreener's best pump.fun / PumpSwap pair for the mint — its
   base token name/symbol and `info.imageUrl` (the same official
   image as indexed). Used when the chain has no metadata, or when
   the metadata URI gives no image.

Only coins the user holds are ever fetched: on purchase (paper and
real) and lazily on render of a held row that has no stored meta.
A coin with no logo shows no image — never a broken image, never a
placeholder box. Stored image URLs are allowlisted to the hosts the
CSP permits (see IMAGE_HOSTS); anything else is dropped, not stored.

A stored row with source "none" means nothing was found; it is
retried at most once an hour so a permanently unknown coin does not
trigger a fetch on every page render.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .solana import (SolanaRpcClient, base58_decode,
                     find_program_address, is_valid_address)
from .trenches import TOKENS_URL, _best_pair, _default_get

METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# Image hosts the CSP allows (see __init__.py) — a stored logo URL
# must be on one of these, or it is dropped, never rendered.
IMAGE_HOSTS = ("dd.dexscreener.com", "image.pump.fun", "ipfs.io",
               "arweave.net")

# A "nothing found" row is retried at most once an hour.
_NEGATIVE_TTL_SECONDS = 3600


def metadata_pda(mint: str) -> str:
    """The Metaplex metadata account address for a mint."""
    addr, _bump = find_program_address(
        [b"metadata", base58_decode(METADATA_PROGRAM),
         base58_decode(mint)],
        METADATA_PROGRAM)
    return addr


def parse_metadata_account(data: bytes) -> dict | None:
    """{name, symbol, uri} from a metadata account's raw bytes, or
    None when the bytes are not a metadata account. Layout: key (1),
    update authority (32), mint (32), then Borsh strings name,
    symbol, uri."""
    if not data or len(data) < 1 + 32 + 32 + 12:
        return None
    off = 1 + 32 + 32

    def _read_str(buf: bytes, pos: int):
        if pos + 4 > len(buf):
            return None, pos
        size = int.from_bytes(buf[pos:pos + 4], "little")
        if size < 0 or pos + 4 + size > len(buf):
            return None, pos
        text = buf[pos + 4:pos + 4 + size].decode(
            "utf-8", "replace").rstrip("\x00").strip()
        return text or None, pos + 4 + size

    name, off = _read_str(data, off)
    symbol, off = _read_str(data, off)
    uri, _off = _read_str(data, off)
    if name is None and symbol is None and uri is None:
        return None
    return {"name": name, "symbol": symbol, "uri": uri}


def _https_image_url(url) -> str | None:
    """A usable https image URL, or None. ipfs:// and ar:// URIs are
    rewritten to their https gateways; anything off the allowlist
    is dropped."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("ipfs://"):
        url = "https://ipfs.io/ipfs/" + url[len("ipfs://"):]
    elif url.startswith("ar://"):
        url = "https://arweave.net/" + url[len("ar://"):]
    if not url.startswith("https://"):
        return None
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in IMAGE_HOSTS:
        return None
    return url


def _default_head(url: str):
    """HEAD a URL: (status, content_type) or (None, None)."""
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "SolEntry-coin-meta"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.headers.get("Content-Type") or ""
    except Exception:
        return None, None


def _verified_image_url(url, http_head=None) -> str | None:
    """Return `url` only if it serves an image (HEAD 200 + image/*
    content type); otherwise None. Never raises."""
    url = _https_image_url(url)
    if url is None:
        return None
    head = http_head or _default_head
    try:
        status, ctype = head(url)
    except Exception:
        return None
    if status != 200:
        return None
    if not (ctype or "").lower().startswith("image/"):
        return None
    return url


def _fetch_metadata_json(uri: str, http_get=None) -> dict | None:
    """The metadata JSON behind a URI: {name, symbol, image} (any
    field may be missing). ipfs:// / ar:// URIs are rewritten to
    https gateways. Never raises."""
    if not uri or not isinstance(uri, str):
        return None
    uri = uri.strip()
    if uri.startswith("ipfs://"):
        uri = "https://ipfs.io/ipfs/" + uri[len("ipfs://"):]
    elif uri.startswith("ar://"):
        uri = "https://arweave.net/" + uri[len("ar://"):]
    if not uri.startswith("https://"):
        return None
    get = http_get or _default_get
    try:
        raw = get(uri)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return {"name": raw.get("name") or None,
            "symbol": raw.get("symbol") or None,
            "image": raw.get("image") or None}


def _from_metaplex(mint: str, rpc_url: str | None,
                   http_get=None, http_head=None) -> dict | None:
    """{name, symbol, image_url} from the on-chain metadata account,
    or None when the mint has no metadata account. A metadata
    account with a dead URI still yields its name/symbol — only the
    image is missing."""
    if not rpc_url:
        return None
    try:
        client = SolanaRpcClient(rpc_url)
        info = client.get_account_info(metadata_pda(mint))
    except Exception:
        return None
    if not info or not info.get("data"):
        return None
    raw = info["data"]
    if not isinstance(raw, (bytes, bytearray)):
        return None
    parsed = parse_metadata_account(bytes(raw))
    if parsed is None:
        return None
    name = parsed.get("name")
    symbol = parsed.get("symbol")
    image_url = None
    if parsed.get("uri"):
        meta_json = _fetch_metadata_json(parsed["uri"],
                                         http_get=http_get)
        if meta_json:
            name = name or meta_json.get("name")
            symbol = symbol or meta_json.get("symbol")
            image_url = _verified_image_url(meta_json.get("image"),
                                            http_head=http_head)
    if name is None and symbol is None and image_url is None:
        return None
    return {"name": name, "symbol": symbol, "image_url": image_url}


def _from_dexscreener(mint: str, http_get=None,
                      http_head=None) -> dict | None:
    """{name, symbol, image_url} from DexScreener's best pump.fun /
    PumpSwap pair for the mint, or None when it has no such pair."""
    get = http_get or _default_get
    try:
        data = get(TOKENS_URL.format(mint))
    except Exception:
        return None
    best = _best_pair(data.get("pairs") or [])
    if best is None:
        return None
    base = best.get("baseToken") or {}
    name = base.get("name") or None
    symbol = base.get("symbol") or None
    image_url = _verified_image_url(
        (best.get("info") or {}).get("imageUrl"),
        http_head=http_head)
    if name is None and symbol is None and image_url is None:
        return None
    return {"name": name, "symbol": symbol, "image_url": image_url}


def fetch_coin_meta(mint: str, rpc_url: str | None = None,
                    http_get=None, http_head=None) -> dict | None:
    """{name, symbol, image_url, source} for a mint, or None when
    neither source names it. On-chain Metaplex metadata first
    (the official record pump.fun wrote at creation), DexScreener
    second (fills whatever the chain did not — usually the image).
    Never raises; any failure in either source falls through to
    the next, and a total failure returns None."""
    if not mint or not is_valid_address(mint):
        return None
    meta = None
    source = None
    chain = _from_metaplex(mint, rpc_url, http_get=http_get,
                           http_head=http_head)
    if chain is not None:
        meta = dict(chain)
        source = "metaplex"
    dex = _from_dexscreener(mint, http_get=http_get,
                            http_head=http_head)
    if dex is not None:
        if meta is None:
            meta = dict(dex)
            source = "dexscreener"
        else:
            meta["name"] = meta.get("name") or dex.get("name")
            meta["symbol"] = meta.get("symbol") or dex.get("symbol")
            meta["image_url"] = (meta.get("image_url")
                                 or dex.get("image_url"))
            if meta.get("image_url") and dex.get("image_url"):
                source = "metaplex+dexscreener"
    if meta is None:
        return None
    if (meta.get("name") is None and meta.get("symbol") is None
            and meta.get("image_url") is None):
        return None
    meta["source"] = source
    return meta


# --- storage (coin_meta table: one row per mint) -------------------------

def get_coin_meta(db, mint: str) -> dict | None:
    """The stored row for a mint, or None. Never raises."""
    try:
        row = db.execute(
            "SELECT mint, name, symbol, image_url, source, fetched_at "
            "FROM coin_meta WHERE mint = ?", (mint,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {"mint": row["mint"], "name": row["name"],
            "symbol": row["symbol"], "image_url": row["image_url"],
            "source": row["source"], "fetched_at": row["fetched_at"]}


def save_coin_meta(db, mint: str, meta: dict | None) -> None:
    """Store a fetched result (or a "nothing found" row when meta
    is None, so the miss is not re-fetched on every render).
    Never raises; a failed write is simply lost."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if meta is None:
            db.execute(
                "INSERT INTO coin_meta (mint, name, symbol, image_url, "
                "source, fetched_at) VALUES (?, NULL, NULL, NULL, "
                "'none', ?) ON CONFLICT(mint) DO UPDATE SET "
                "fetched_at = excluded.fetched_at",
                (mint, now))
        else:
            db.execute(
                "INSERT INTO coin_meta (mint, name, symbol, image_url, "
                "source, fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mint) DO UPDATE SET name = excluded.name, "
                "symbol = excluded.symbol, "
                "image_url = excluded.image_url, "
                "source = excluded.source, "
                "fetched_at = excluded.fetched_at",
                (mint, meta.get("name"), meta.get("symbol"),
                 meta.get("image_url"), meta.get("source") or "unknown",
                 now))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _negative_fresh(stored: dict) -> bool:
    """True when a stored "nothing found" row is still within its
    retry window."""
    if stored.get("source") != "none":
        return False
    try:
        when = datetime.strptime(stored["fetched_at"],
                                 "%Y-%m-%dT%H:%M:%SZ").replace(
                                     tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return age < _NEGATIVE_TTL_SECONDS


def ensure_coin_meta(db, mint: str, rpc_url: str | None = None,
                     http_get=None, http_head=None) -> dict | None:
    """The stored meta for a mint, fetching + storing it when the
    DB has nothing (or a stale "nothing found" row). Best effort:
    a fetch failure stores the miss and returns the stored row (or
    None). Never raises — a purchase or a page render must never
    fail because a logo could not be fetched."""
    if not mint or not is_valid_address(mint):
        return None
    try:
        stored = get_coin_meta(db, mint)
    except Exception:
        stored = None
    if stored is not None:
        if stored.get("source") != "none" or _negative_fresh(stored):
            return stored
    try:
        meta = fetch_coin_meta(mint, rpc_url=rpc_url,
                               http_get=http_get, http_head=http_head)
    except Exception:
        meta = None
    try:
        save_coin_meta(db, mint, meta)
    except Exception:
        pass
    if meta is not None:
        return {"mint": mint, "name": meta.get("name"),
                "symbol": meta.get("symbol"),
                "image_url": meta.get("image_url"),
                "source": meta.get("source")}
    return stored


def stored_display(db, mint: str) -> tuple:
    """(name, symbol, image_url) for a mint from the DB only — no
    network. Each element is None when unknown. Callers render
    the ticker alone when the name is None, and no image when the
    URL is None."""
    stored = get_coin_meta(db, mint) or {}
    return (stored.get("name"), stored.get("symbol"),
            stored.get("image_url"))
