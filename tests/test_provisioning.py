"""Railway provisioning gate: OFF unless flag AND token are both set."""

import unittest

from tradingbot.config import Config
from tradingbot.railway_provision import (ProvisioningDisabled,
                                          is_enabled,
                                          provision_bot_service)


def _config(flag: str, token: str) -> Config:
    return Config(env={
        "SESSION_SECRET": "x" * 40, "ENCRYPTION_KEY": "ab" * 32,
        "NETWORK": "devnet", "DATABASE_PATH": ":memory:",
        "RAILWAY_PROVISIONING": flag, "RAILWAY_TOKEN": token})


class TestProvisioningGate(unittest.TestCase):
    def test_off_without_flag(self):
        self.assertFalse(is_enabled(_config("0", "tok123")))
        with self.assertRaises(ProvisioningDisabled):
            provision_bot_service(_config("0", "tok123"), "p", "e", "svc", {})

    def test_off_without_token(self):
        self.assertFalse(is_enabled(_config("1", "")))
        with self.assertRaises(ProvisioningDisabled):
            provision_bot_service(_config("1", ""), "p", "e", "svc", {})

    def test_on_with_flag_and_token_uses_mock(self):
        calls = []

        def fake_http(query, variables):
            calls.append((query, variables))
            if "ServiceCreate" in query:
                return {"serviceCreate": {"id": "svc-123", "name": "bot-1"}}
            return {"variableCollectionUpsert": True}

        result = provision_bot_service(
            _config("1", "tok123"), "proj-1", "env-1", "bot-1",
            {"NETWORK": "devnet", "BOT_NAME": "Luna"},
            http_post=fake_http)
        self.assertEqual(result["service_id"], "svc-123")
        self.assertEqual(result["service_name"], "bot-1")
        # Only NAMES come back — values never appear in the result.
        self.assertEqual(result["variables_set"], ["BOT_NAME", "NETWORK"])
        self.assertNotIn("devnet", str(result))
        self.assertNotIn("Luna", str(result))
        self.assertEqual(len(calls), 2)

    def test_service_create_failure_raises(self):
        def fake_http(query, variables):
            return {"serviceCreate": {}}

        with self.assertRaises(RuntimeError):
            provision_bot_service(
                _config("1", "tok123"), "p", "e", "svc",
                {"NETWORK": "devnet"}, http_post=fake_http)


if __name__ == "__main__":
    unittest.main()
