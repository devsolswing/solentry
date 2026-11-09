# Trading Bot — Railway-hosted multi-user trading app
#
# Public, small-community trading app. Separate from James's private
# live 5-bot engine (zealous-caring) and from SolSwing. Never mix those in.
#
# Stack: Python 3.12 + Flask + stdlib sqlite3 + cryptography (AES-256-GCM)
# + pure-Python Ed25519 Solana signing (pattern proven in the live engine's
# tx_builder.py; no trading logic copied from it).
#
# Why this stack: Flask is one pip install and Railway Nixpacks runs
# requirements.txt with zero native build steps; stdlib sqlite3 needs no
# native module (better-sqlite3 in Node would need a compiler at deploy);
# cryptography ships prebuilt wheels; the pure-Python Solana signing avoids
# a heavy web3 dependency entirely.

from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no extra dependency). Only sets vars not already set."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


class Config:
    """All secrets come from env vars. Nothing secret lives in code."""

    def __init__(self, env: dict | None = None):
        env = os.environ if env is None else env
        self.session_secret: str = env.get("SESSION_SECRET", "")
        self.encryption_key_raw: str = env.get("ENCRYPTION_KEY", "")
        self.network: str = (env.get("NETWORK", "devnet") or "devnet").lower()
        if self.network not in ("devnet", "mainnet"):
            raise ValueError("NETWORK must be devnet or mainnet")
        self.rpc_url: str = env.get(
            "RPC_URL",
            "https://api.devnet.solana.com"
            if self.network == "devnet"
            else "https://api.mainnet-beta.solana.com",
        )
        self.database_path: str = env.get("DATABASE_PATH", "tradingbot.db")
        self.railway_provisioning: bool = env.get("RAILWAY_PROVISIONING", "0") == "1"
        self.railway_token: str = env.get("RAILWAY_TOKEN", "")
        self.paper_start_cash: float = float(env.get("PAPER_START_CASH", "10000"))
        # Silent trading loop: runs as a daemon thread inside the web
        # process (the SQLite file lives on the web service's volume, so
        # a separate worker service could not see it). OFF by default —
        # set SILENT_LOOP_ENABLED=1 to run it. It only ever trades for
        # bots the user explicitly switched to silent.
        self.silent_loop_enabled: bool = env.get(
            "SILENT_LOOP_ENABLED", "0") == "1"
        self.silent_loop_interval_seconds: int = max(
            10, int(env.get("SILENT_LOOP_INTERVAL_SECONDS", "60")))
        # WebAuthn passkeys: RP identity comes from env vars only —
        # never hardcode the production domain. Dev defaults point at
        # a local server; Railway must set the real domain + origin.
        self.webauthn_rp_id: str = env.get("WEBAUTHN_RP_ID", "localhost")
        self.webauthn_rp_name: str = env.get("WEBAUTHN_RP_NAME", "SolEntry")
        self.webauthn_origin: str = env.get(
            "WEBAUTHN_ORIGIN", "http://localhost:5000")
        # Admin for /admin/donations. Empty = the first registered user.
        self.admin_username: str = env.get("ADMIN_USERNAME", "").strip()

    def require_secrets(self) -> None:
        """Fail fast at startup when required secrets are missing."""
        missing = []
        if not self.session_secret:
            missing.append("SESSION_SECRET")
        if not self.encryption_key_raw:
            missing.append("ENCRYPTION_KEY")
        if missing:
            raise RuntimeError(
                "Missing required env vars: " + ", ".join(missing)
                + ". See .env.example."
            )

    @property
    def encryption_key(self) -> bytes:
        """Resolve ENCRYPTION_KEY to 32 bytes.

        Accepts 64 hex chars, base64 of 32 bytes, or any other string
        (SHA-256 of it, deterministic). Generate with:
        python -c "import secrets; print(secrets.token_hex(32))"
        """
        raw = self.encryption_key_raw.strip()
        if len(raw) == 64:
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass
        import base64
        import hashlib

        try:
            decoded = base64.b64decode(raw, validate=True)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        return hashlib.sha256(raw.encode("utf-8")).digest()
