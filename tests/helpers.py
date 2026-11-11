"""Shared test harness: temp DB, test secrets, csrf helpers."""

from __future__ import annotations

import os
import re

from tradingbot import create_app
from tradingbot.config import Config

TEST_SESSION_SECRET = "test-session-secret-" + "x" * 40
TEST_ENCRYPTION_KEY = "ab" * 32  # 64 hex chars -> 32 bytes
# Throwaway fee/donation recipient for tests ONLY: a valid base58
# 32-byte address with no real wallet behind it (the keypair was
# generated once to produce the address and discarded). Never a
# real address — no real wallet address ever appears in tests.
TEST_FEE_RECIPIENT = "4XWpRuKMH5uVGiXkvV5PSKSBi36dBQVSy9QJ59Kvda7Q"


def make_config(tmpdir: str) -> Config:
    env = {
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tmpdir, "test.db"),
        "PAPER_START_CASH": "100",
        "FEE_RECIPIENT_PUBKEY": TEST_FEE_RECIPIENT,
        # Tests only: the Security bot's codes are echoed in the
        # signup/recovery popup so the flow can be tested without
        # SMTP. Never set in production.
        "SECURITY_CODE_DEV_ECHO": "1",
    }
    return Config(env=env)


def make_client(tmpdir: str):
    app = create_app(make_config(tmpdir))
    app.testing = True
    return app, app.test_client()


def csrf(client, path: str) -> str:
    """Fetch a page and pull the csrf token out of its forms."""
    resp = client.get(path)
    html = resp.get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def signup(client, username: str, password: str = "password123",
           email: str | None = None):
    """Two-step signup: POST /signup (creates a pending signup and
    the Security bot's code, echoed in the popup under the test
    dev-echo flag), then POST /signup/verify-code with the code.
    Returns the final response (302 to /wallet on success, or the
    first response when the signup itself was refused)."""
    token = csrf(client, "/signup")
    resp = client.post("/signup", data={
        "csrf_token": token, "username": username, "password": password,
        "email": email or f"{username}@example.com",
    }, follow_redirects=False)
    if resp.status_code != 200:
        return resp
    html = resp.get_data(as_text=True)
    m = re.search(r"your code is (\d{4})", html)
    assert m, "no dev code in signup popup"
    token = csrf(client, "/signup")
    return client.post("/signup/verify-code", data={
        "csrf_token": token, "code": m.group(1),
    }, follow_redirects=False)


def dev_code_from_signup(resp) -> str:
    """The Security bot's code, echoed in the signup popup under
    the test dev-echo flag."""
    html = resp.get_data(as_text=True)
    m = re.search(r"your code is (\d{4})", html)
    assert m, "no dev code in signup popup"
    return m.group(1)


def login(client, username: str, password: str = "password123"):
    token = csrf(client, "/login")
    return client.post("/login", data={
        "csrf_token": token, "username": username, "password": password,
    }, follow_redirects=False)
