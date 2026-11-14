"""coin_meta: official name + ticker + logo for a pump.fun mint.

The primary source is the on-chain Metaplex metadata account
(what pump.fun wrote at creation): name, symbol, and the metadata
URI whose JSON carries the official image. DexScreener's best
pump.fun / PumpSwap pair fills whatever the chain did not —
usually the image. Image URLs are allowlisted (dd.dexscreener.com,
image.pump.fun, ipfs.io, arweave.net) and HEAD-verified before
they are stored, so a held coin never renders a broken image.
Storage is the `coin_meta` table, one row per mint; a "nothing
found" row is retried at most once an hour.
"""

import sqlite3
import struct
import tempfile
import unittest
from unittest import mock

import tradingbot.coin_meta as cm

GATE_MINT = "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE"
GATE_PDA = "CoP4GFWfgG7pP9swAy83zA6Ljw8eCcpT4ZrohcJJzt27"
MINT = "Grad1111111111111111111111111111111111111111"


def _metadata_bytes(name, symbol, uri):
    """A synthetic metadata account: key + authority + mint +
    Borsh name/symbol/uri strings."""
    out = bytearray(b"\x04" + b"\x00" * 32 + b"\x00" * 32)
    for text in (name, symbol, uri):
        raw = text.encode("utf-8")
        out += struct.pack("<I", len(raw)) + raw
    return bytes(out)


def _head_ok(url):
    return 200, "image/png"


def _head_not_image(url):
    return 200, "text/html"


class TestMetadataPda(unittest.TestCase):
    def test_gate_pda_matches_chain(self):
        # Verified live on devnet 2026-11-13: Gate's metadata
        # account is this PDA.
        self.assertEqual(cm.metadata_pda(GATE_MINT), GATE_PDA)

    def test_parse_metadata_account(self):
        data = _metadata_bytes(
            "Gate", "GATE", "https://example.com/gate.json")
        self.assertEqual(cm.parse_metadata_account(data), {
            "name": "Gate", "symbol": "GATE",
            "uri": "https://example.com/gate.json"})

    def test_parse_rejects_short_bytes(self):
        self.assertIsNone(cm.parse_metadata_account(b"\x04\x00\x00"))
        self.assertIsNone(cm.parse_metadata_account(b""))


class TestImageUrlAllowlist(unittest.TestCase):
    def test_allowlisted_hosts_pass(self):
        for url in ("https://dd.dexscreener.com/ds-data/tokens/x.png",
                    "https://image.pump.fun/x.png",
                    "https://ipfs.io/ipfs/QmX",
                    "https://arweave.net/abc"):
            self.assertEqual(cm._https_image_url(url), url, url)

    def test_ipfs_and_ar_rewritten(self):
        self.assertEqual(
            cm._https_image_url("ipfs://QmX/image.png"),
            "https://ipfs.io/ipfs/QmX/image.png")
        self.assertEqual(
            cm._https_image_url("ar://abc/image.png"),
            "https://arweave.net/abc/image.png")

    def test_off_allowlist_dropped(self):
        for url in ("http://image.pump.fun/x.png",
                    "https://assets.coingecko.com/x.png",
                    "https://evil.example.com/x.png",
                    "data:image/png;base64,AAA", None, ""):
            self.assertIsNone(cm._https_image_url(url), url)

    def test_verified_requires_image_content_type(self):
        url = "https://image.pump.fun/x.png"
        self.assertEqual(
            cm._verified_image_url(url, http_head=_head_ok), url)
        self.assertIsNone(
            cm._verified_image_url(url, http_head=_head_not_image))
        self.assertIsNone(
            cm._verified_image_url(
                url, http_head=lambda u: (404, "image/png")))
        self.assertIsNone(
            cm._verified_image_url(
                url, http_head=lambda u: (_ for _ in ()).throw(
                    Exception("down"))))


class TestFromMetaplex(unittest.TestCase):
    def _fake_client(self, data):
        class FakeClient:
            def __init__(self, url):
                self.url = url

            def get_account_info(self, pubkey):
                return {"data": data} if data is not None else None

        return FakeClient

    def test_name_symbol_and_logo_from_chain(self):
        data = _metadata_bytes(
            "Gate", "GATE", "https://example.com/gate.json")
        fake_get = lambda url: {"image":
                                "https://image.pump.fun/gate.png"}
        with mock.patch.object(cm, "SolanaRpcClient",
                              self._fake_client(data)):
            meta = cm._from_metaplex(
                GATE_MINT, "https://example.invalid",
                http_get=fake_get, http_head=_head_ok)
        self.assertEqual(meta["name"], "Gate")
        self.assertEqual(meta["symbol"], "GATE")
        self.assertEqual(meta["image_url"],
                         "https://image.pump.fun/gate.png")

    def test_dead_uri_still_yields_name_and_symbol(self):
        data = _metadata_bytes(
            "Gate", "GATE", "https://example.com/dead.json")
        with mock.patch.object(cm, "SolanaRpcClient",
                              self._fake_client(data)):
            meta = cm._from_metaplex(
                GATE_MINT, "https://example.invalid",
                http_get=lambda url: (_ for _ in ()).throw(
                    Exception("down")), http_head=_head_ok)
        self.assertEqual(meta["name"], "Gate")
        self.assertEqual(meta["symbol"], "GATE")
        self.assertIsNone(meta["image_url"])

    def test_no_metadata_account_is_none(self):
        with mock.patch.object(cm, "SolanaRpcClient",
                              self._fake_client(None)):
            self.assertIsNone(cm._from_metaplex(
                GATE_MINT, "https://example.invalid"))

    def test_no_rpc_url_is_none(self):
        self.assertIsNone(cm._from_metaplex(GATE_MINT, None))


class TestFromDexscreener(unittest.TestCase):
    def _pair(self, image=None):
        pair = {
            "chainId": "solana", "dexId": "pumpfun",
            "baseToken": {"address": MINT, "name": "Test Coin",
                          "symbol": "TEST"},
            "priceUsd": "0.002", "liquidity": {"usd": 50000},
            "info": {}}
        if image:
            pair["info"]["imageUrl"] = image
        return pair

    def test_name_symbol_image_from_pair(self):
        payload = {"pairs": [self._pair(
            "https://dd.dexscreener.com/ds-data/tokens/t.png")]}
        meta = cm._from_dexscreener(
            MINT, http_get=lambda url: payload, http_head=_head_ok)
        self.assertEqual(meta["name"], "Test Coin")
        self.assertEqual(meta["symbol"], "TEST")
        self.assertEqual(
            meta["image_url"],
            "https://dd.dexscreener.com/ds-data/tokens/t.png")

    def test_no_pair_is_none(self):
        meta = cm._from_dexscreener(
            MINT, http_get=lambda url: {"pairs": []})
        self.assertIsNone(meta)

    def test_non_pumpfun_pair_is_none(self):
        pair = self._pair()
        pair["dexId"] = "meteora"
        meta = cm._from_dexscreener(
            MINT, http_get=lambda url: {"pairs": [pair]})
        self.assertIsNone(meta)


class TestFetchAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(f"{self.tmp.name}/t.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS coin_meta ("
            "mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, "
            "image_url TEXT, source TEXT, fetched_at TEXT)")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_fetch_combines_chain_and_dex(self):
        # Chain names it but has no logo; DexScreener supplies
        # the image. Neither source is trusted for what the other
        # already gave.
        with mock.patch.object(
                cm, "_from_metaplex",
                return_value={"name": "Test Coin", "symbol": "TEST",
                              "image_url": None}), \
                mock.patch.object(
                    cm, "_from_dexscreener",
                    return_value={"name": "Test Coin", "symbol": "TEST",
                                  "image_url":
                                  "https://image.pump.fun/t.png"}):
            meta = cm.fetch_coin_meta(MINT)
        self.assertEqual(meta["name"], "Test Coin")
        self.assertEqual(meta["symbol"], "TEST")
        self.assertEqual(meta["image_url"],
                         "https://image.pump.fun/t.png")
        self.assertEqual(meta["source"], "metaplex+dexscreener")

    def test_fetch_none_when_neither_source_names_it(self):
        with mock.patch.object(cm, "_from_metaplex",
                               return_value=None), \
                mock.patch.object(cm, "_from_dexscreener",
                                  return_value=None):
            self.assertIsNone(cm.fetch_coin_meta(MINT))

    def test_fetch_invalid_mint_is_none(self):
        self.assertIsNone(cm.fetch_coin_meta("NOT-A-MINT"))

    def test_save_and_get_round_trip(self):
        cm.save_coin_meta(self.db, MINT, {
            "name": "Test Coin", "symbol": "TEST",
            "image_url": "https://image.pump.fun/t.png",
            "source": "metaplex"})
        stored = cm.get_coin_meta(self.db, MINT)
        self.assertEqual(stored["name"], "Test Coin")
        self.assertEqual(stored["symbol"], "TEST")
        self.assertEqual(stored["image_url"],
                         "https://image.pump.fun/t.png")
        self.assertEqual(stored["source"], "metaplex")
        self.assertEqual(cm.stored_display(self.db, MINT),
                         ("Test Coin", "TEST",
                          "https://image.pump.fun/t.png"))

    def test_ensure_fetches_once_then_returns_stored(self):
        calls = []

        def fake_fetch(mint, rpc_url=None, http_get=None,
                       http_head=None):
            calls.append(mint)
            return {"name": "Test Coin", "symbol": "TEST",
                    "image_url": "https://image.pump.fun/t.png",
                    "source": "metaplex"}

        with mock.patch.object(cm, "fetch_coin_meta",
                               side_effect=fake_fetch):
            first = cm.ensure_coin_meta(self.db, MINT)
            second = cm.ensure_coin_meta(self.db, MINT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["name"], "Test Coin")
        self.assertEqual(second["name"], "Test Coin")

    def test_ensure_negative_row_is_not_refetched_within_hour(self):
        calls = []

        def fake_fetch(mint, rpc_url=None, http_get=None,
                       http_head=None):
            calls.append(mint)
            return None

        with mock.patch.object(cm, "fetch_coin_meta",
                               side_effect=fake_fetch):
            first = cm.ensure_coin_meta(self.db, MINT)
            second = cm.ensure_coin_meta(self.db, MINT)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(first)
        self.assertEqual(second["source"], "none")
        self.assertEqual(cm.stored_display(self.db, MINT),
                         (None, None, None))

    def test_ensure_invalid_mint_is_none(self):
        self.assertIsNone(cm.ensure_coin_meta(self.db, "NOPE"))


if __name__ == "__main__":
    unittest.main()
