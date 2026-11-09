"""Wallet crypto: keypair generation, encryption round-trip, Solana math."""

import unittest

from tradingbot.security import decrypt_secret, encrypt_secret
from tradingbot.solana import (base58_decode, base58_encode,
                               build_signed_transfer,
                               ed25519_publickey_from_seed,
                               generate_keypair, is_valid_address,
                               publickey_from_secret)


class TestWalletCrypto(unittest.TestCase):
    def test_keypair_round_trip_and_pubkey_match(self):
        seed, pubkey, secret = generate_keypair()
        self.assertEqual(len(seed), 32)
        self.assertEqual(len(secret), 64)
        self.assertTrue(is_valid_address(pubkey))
        self.assertEqual(
            base58_encode(publickey_from_secret(secret)), pubkey)
        self.assertEqual(
            base58_encode(ed25519_publickey_from_seed(seed)), pubkey)

    def test_rfc8032_vector(self):
        # RFC 8032 test vector 1: seed -> known public key.
        secret = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        self.assertEqual(
            base58_encode(publickey_from_secret(secret)),
            base58_encode(bytes.fromhex(
                "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")))

    def test_encrypt_decrypt_round_trip(self):
        key = bytes.fromhex("ab" * 32)
        _seed, _pub, secret = generate_keypair()
        blob = encrypt_secret(secret, key)
        self.assertEqual(decrypt_secret(blob, key), secret)

    def test_encrypted_blob_contains_no_plaintext(self):
        key = bytes.fromhex("ab" * 32)
        _seed, pubkey, secret = generate_keypair()
        blob = encrypt_secret(secret, key)
        # Neither the raw secret, its base58 form, nor the pubkey appear.
        self.assertNotIn(secret, blob.encode())
        self.assertNotIn(pubkey, blob)
        self.assertNotIn(base58_encode(secret), blob)

    def test_decrypt_with_wrong_key_fails(self):
        key = bytes.fromhex("ab" * 32)
        wrong = bytes.fromhex("cd" * 32)
        _seed, _pub, secret = generate_keypair()
        blob = encrypt_secret(secret, key)
        with self.assertRaises(ValueError):
            decrypt_secret(blob, wrong)

    def test_address_validation(self):
        self.assertTrue(is_valid_address(
            "11111111111111111111111111111111"))
        self.assertTrue(is_valid_address(
            "Bc7CCunk9qsBbpXS6wEmLqnDqQ9e1czYbt3DUrUGSDmW"))
        self.assertFalse(is_valid_address("not an address"))
        self.assertFalse(is_valid_address("1111111111111111111111111111111O"))
        self.assertFalse(is_valid_address(""))

    def test_transfer_message_structure(self):
        seed, pubkey, _secret = generate_keypair()
        _dseed, dest, _dsecret = generate_keypair()
        blockhash = "11111111111111111111111111111111"
        signed_b64, sig = build_signed_transfer(
            seed, pubkey, dest, 123456, blockhash)
        import base64
        raw = base64.b64decode(signed_b64)
        # 1 signature (compact-u16 count 1) + 64-byte signature + message.
        self.assertEqual(raw[0], 1)
        self.assertEqual(len(raw[1:65]), 64)
        message = raw[65:]
        # Header: 1 required sig, 0 readonly signed, 1 readonly unsigned.
        self.assertEqual(list(message[:3]), [1, 0, 1])
        # Account keys: fee payer, destination, system program.
        n_keys = message[3]
        self.assertEqual(n_keys, 3)
        keys = [message[4 + i * 32: 4 + (i + 1) * 32] for i in range(3)]
        self.assertEqual(base58_encode(keys[0]), pubkey)
        self.assertEqual(base58_encode(keys[1]), dest)
        # Instruction data ends with the transfer discriminator + lamports.
        self.assertIn(
            bytes([2, 0, 0, 0]) + (123456).to_bytes(8, "little"), message)
        # A signature is 64 bytes (not a 32-byte address).
        self.assertEqual(len(base58_decode(sig)), 64)

    def test_transfer_refuses_fee_payer_mismatch(self):
        seed, _pub, _secret = generate_keypair()
        with self.assertRaises(ValueError):
            build_signed_transfer(
                seed, "11111111111111111111111111111111",
                "11111111111111111111111111111111", 100,
                "11111111111111111111111111111111")


if __name__ == "__main__":
    unittest.main()
