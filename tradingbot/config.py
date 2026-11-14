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


# DEVNET-TEST-ONLY (remove before mainnet): the pump.fun mints the
# silent loop watches as extra trenches candidates on devnet.
# Gate is James's own devnet test coin; AuditTest is a barely
# traded devnet coin made for testing. Both have live devnet
# bonding curves and no DexScreener data.
DEVNET_TRENCH_MINTS_DEFAULT = (
    "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE",  # Gate
    "3k6ZYa2Wh6P92VN2uGvKsmHiw2RQpcVgxpb447raf2kC",  # AuditTest
)


def _parse_devnet_trench_mints(env: dict) -> list[str]:
    """DEVNET_TRENCH_MINTS, comma-separated. Unset means the two
    built-in devnet test mints; set (even empty) means exactly
    what the operator listed."""
    raw = env.get("DEVNET_TRENCH_MINTS")
    if raw is None:
        return list(DEVNET_TRENCH_MINTS_DEFAULT)
    return [m.strip() for m in str(raw).split(",") if m.strip()]


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
        self.paper_start_cash: float = float(env.get("PAPER_START_CASH", "100"))
        # Silent trading loop: runs as a daemon thread inside the web
        # process (the SQLite file lives on the web service's volume, so
        # a separate worker service could not see it). OFF by default —
        # set SILENT_LOOP_ENABLED=1 to run it. It only ever trades for
        # bots the user explicitly switched to silent.
        self.silent_loop_enabled: bool = env.get(
            "SILENT_LOOP_ENABLED", "0") == "1"
        # Real Jupiter swaps (mainnet): OFF by default — set
        # JUPITER_REAL_SWAPS=1 on Railway to enable them. This is
        # James's explicit mainnet go-ahead (2026-11-11): the flag
        # is admin-only (a Railway env var) and NOTHING in the
        # user-facing UI can flip it on. With the flag off, any
        # real/mainnet Jupiter swap attempt refuses plainly. The
        # executor also requires NETWORK=mainnet — Jupiter has no
        # devnet liquidity, so a mainnet swap on a devnet RPC is
        # refused too. No real money moves unless both hold.
        self.jupiter_real_swaps: bool = env.get(
            "JUPITER_REAL_SWAPS", "0") == "1"
        # Live price feed: DexScreener poll for pump.fun mints
        # (Raydium pairs for graduated coins, bonding-curve prices
        # for devnet watchlist mints), Jupiter for the SOL price.
        # Runs as daemon threads inside the web process (started
        # from run.py, never from create_app). ON by default — set
        # LIVE_FEED_ENABLED=0 to kill it; the app keeps serving from
        # the existing REST feeds.
        self.live_feed_enabled: bool = env.get(
            "LIVE_FEED_ENABLED", "1") == "1"
        self.silent_loop_interval_seconds: int = max(
            10, int(env.get("SILENT_LOOP_INTERVAL_SECONDS", "60")))
        # DEVNET-TEST-ONLY watchlist for the silent loop (devnet
        # proof): pump.fun mints the loop watches as extra
        # trenches candidates, priced from their devnet bonding
        # curves (DexScreener has no data for them — it returns
        # pairs:null for both). Comma-separated override via
        # DEVNET_TRENCH_MINTS; empty means no watchlist. Active
        # ONLY when NETWORK == devnet — see
        # tradingbot/devnet_watchlist.py. REMOVE BEFORE MAINNET:
        # these are devnet test tokens and must never be
        # candidates on mainnet.
        self.devnet_trench_mints: list[str] = _parse_devnet_trench_mints(
            env)
        # WebAuthn passkeys: RP identity comes from env vars only —
        # never hardcode the production domain. Dev defaults point at
        # a local server; Railway must set the real domain + origin.
        self.webauthn_rp_id: str = env.get("WEBAUTHN_RP_ID", "localhost")
        self.webauthn_rp_name: str = env.get("WEBAUTHN_RP_NAME", "SolEntry")
        self.webauthn_origin: str = env.get(
            "WEBAUTHN_ORIGIN", "http://localhost:5000")
        # Admin for /admin/donations. Empty = the first registered user.
        self.admin_username: str = env.get("ADMIN_USERNAME", "").strip()
        # Donation + future bot-rental recipient: the ONE wallet
        # that receives login-screen donations (James's own wallet,
        # set in Railway), reserved for bot-rental payments when
        # that system is built later. The trading fee was REMOVED
        # (James, 2026-11-12) — no trade reads this var any more.
        # Fail-closed: when unset, empty, or not a valid base58
        # 32-byte address, the donate flow refuses — never fall
        # back to a generated wallet, never a guessed address.
        self.fee_recipient_pubkey: str = env.get(
            "FEE_RECIPIENT_PUBKEY", "").strip()
        # Cloudflare Turnstile (bot challenge on login/signup). Both
        # keys must be set for the challenge to be enforced; with
        # either missing the app logs a warning at startup and
        # continues without it (never lock James out for missing
        # keys). Free setup is documented in the README security
        # page. Never commit real keys — env vars only.
        self.turnstile_site_key: str = env.get(
            "TURNSTILE_SITE_KEY", "").strip()
        self.turnstile_secret_key: str = env.get(
            "TURNSTILE_SECRET_KEY", "").strip()
        # Per-wallet daily send cap (SOL). A send that would take
        # today's total over this is refused plainly. Default 5 SOL
        # is far above James's $20 CAD-scale testing and stops a
        # compromised session draining a wallet in one day.
        self.daily_send_limit_sol: float = float(
            env.get("DAILY_SEND_LIMIT_SOL", "5"))
        # Outbound email for the Security bot ONLY (see mailer.py):
        # the 4-digit signup confirmation code and the 4-digit
        # account-recovery code are the only emails the app ever
        # sends. Unset = email is not configured and the
        # signup/recovery flows say so plainly instead of
        # pretending a code was sent. Set on Railway (a Gmail app
        # password works): SMTP_HOST=smtp.gmail.com, SMTP_PORT=587,
        # SMTP_USER=<gmail address>, SMTP_PASSWORD=<16-char app
        # password>, SMTP_FROM=<gmail address>. Never commit real
        # credentials — env vars only.
        self.smtp_host: str = env.get("SMTP_HOST", "").strip()
        self.smtp_port: int = int(env.get("SMTP_PORT", "587") or 587)
        self.smtp_user: str = env.get("SMTP_USER", "").strip()
        self.smtp_password: str = env.get("SMTP_PASSWORD", "")
        self.smtp_from: str = env.get("SMTP_FROM", "").strip()
        self.smtp_use_tls: bool = env.get("SMTP_USE_TLS", "1") == "1"
        # Dev/test fallback for the Security bot's codes: when on,
        # issue_code returns the code in its result so the signup /
        # recovery flows can be tested without SMTP. TESTS ONLY —
        # never set in production (Railway must not have it).
        self.security_code_dev_echo: bool = env.get(
            "SECURITY_CODE_DEV_ECHO", "0") == "1"
        # Secure session cookies (only sent over HTTPS). Railway
        # serves HTTPS, so this is on by default; set
        # SESSION_COOKIE_SECURE=0 only for plain-HTTP local dev.
        self.session_cookie_secure: bool = env.get(
            "SESSION_COOKIE_SECURE", "1") == "1"

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
