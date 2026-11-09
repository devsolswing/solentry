"""Shared test harness: temp DB, test secrets, csrf helpers."""

from __future__ import annotations

import os
import re

from tradingbot import create_app
from tradingbot.config import Config

TEST_SESSION_SECRET = "test-session-secret-" + "x" * 40
TEST_ENCRYPTION_KEY = "ab" * 32  # 64 hex chars -> 32 bytes


def make_config(tmpdir: str) -> Config:
    env = {
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NETWORK": "devnet",
        "DATABASE_PATH": os.path.join(tmpdir, "test.db"),
        "PAPER_START_CASH": "50",
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


def signup(client, username: str, password: str = "password123"):
    token = csrf(client, "/signup")
    return client.post("/signup", data={
        "csrf_token": token, "username": username, "password": password,
    }, follow_redirects=False)


def login(client, username: str, password: str = "password123"):
    token = csrf(client, "/login")
    return client.post("/login", data={
        "csrf_token": token, "username": username, "password": password,
    }, follow_redirects=False)
