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

import math
import os

from .solana import ATA_RENT_FALLBACK_LAMPORTS


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


def _env_float(raw, default: float, minimum: float | None = None) -> float:
    """A float env value that falls back to `default` when missing,
    unparseable, non-finite, or below `minimum` — a bad Railway
    var must never crash the app at startup (the quick-buy
    defaults are the fail-safe)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


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
        # $GATE mint for the Buy $GATE packages (2026-11-23): the
        # mint the package buy purchases, from the GATE_MINT env
        # var. Default is the devnet Gate mint. The mainnet $GATE
        # mint is only used on mainnet (see gate_fuel.gate_mint);
        # a mainnet mint is never bought while NETWORK=devnet.
        self.gate_mint: str = env.get(
            "GATE_MINT",
            "Eu4u5JxX3YvgxWRoB5eWBtor8E79Cru2Y96Nh61v8uLE").strip()
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
            10, int(env.get("SILENT_LOOP_INTERVAL_SECONDS", "30")))
        # Mainnet RPC for the top-10 wallet watch and the pre-buy
        # scam gates (tuning build 2026-11-15): the top-10 watch
        # used to read every mint through the devnet RPC, which
        # cannot serve mainnet mints at all. Mainnet mints are now
        # read through this URL; devnet watchlist mints still use
        # RPC_URL (the devnet RPC that can see them).
        self.mainnet_rpc_url: str = env.get(
            "MAINNET_RPC_URL", "https://api.mainnet-beta.solana.com")
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
        # True only when DEVNET_TRENCH_MINTS was set explicitly in
        # the environment (even to empty). The startup guardrail
        # (silent_loop.validate_loop_startup) refuses to start on
        # mainnet only when an operator EXPLICITLY configured devnet
        # mints while on mainnet — the built-in defaults are inert
        # on mainnet (active_mints returns [] there), so they alone
        # never block the loop.
        self.devnet_trench_mints_explicit: bool = (
            env.get("DEVNET_TRENCH_MINTS") is not None)
        # Mainnet executor fix (2026-11-22). Every value below is a
        # Remy default, awaiting James's call — see
        # ~/workspace/executor-fix-staging/MANIFEST.md.
        # Fallback RPC: tried after RPC_URL on a transport failure,
        # then the read skips. Mainnet default is a second public
        # endpoint; devnet has no second public endpoint configured.
        self.rpc_fallback_url: str = env.get(
            "RPC_FALLBACK_URL",
            "https://solana-rpc.publicnode.com"
            if self.network == "mainnet" else "").strip()
        # Priority-fee cap, micro-lamports per compute unit.
        self.priority_fee_cap_micro_lamports: int = int(env.get(
            "PRIORITY_FEE_CAP_MICRO_LAMPORTS", "50000"))
        # Exit slippage, basis points (5%). Entries stay at the
        # pump.fun / Jupiter 1% default.
        self.exit_slippage_bps: int = int(env.get(
            "EXIT_SLIPPAGE_BPS", "500"))
        # Bounded re-quote retries for SELLS only (re-read the
        # curve/quote, rebuild, resend). Buys are never retried.
        self.sell_requote_attempts: int = max(
            1, int(env.get("SELL_REQUOTE_ATTEMPTS", "3")))
        # Per-trade Jupiter fee budget, SOL: a swap whose maximum
        # fee (from its compute-budget instructions) exceeds this
        # refuses instead of signing.
        self.jupiter_max_fee_sol: float = float(env.get(
            "JUPITER_MAX_FEE_SOL", "0.01"))
        # Price-validation gate (report §3): the stream tick
        # (decision price) vs a fresh on-chain read of the same
        # curve must agree within this % before a real buy.
        self.two_source_tolerance_pct: float = float(env.get(
            "TWO_SOURCE_TOLERANCE_PCT", "5"))
        # The old SOL-price cross-check (Jupiter vs CoinGecko) is
        # gone with the single-price source (2026-11-25): SOL has
        # one source, so there is nothing to cross-check against.
        # The SOL_PRICE_DISAGREE_PCT env var is ignored now.
        # A decision price older than this (seconds) is stale: new
        # buys refuse on it. Exits may still decide on it, but the
        # executor re-quotes before selling.
        self.decision_price_max_age_seconds: float = float(env.get(
            "DECISION_PRICE_MAX_AGE_SECONDS", "30"))
        # Buy-flow gas reserve (James, 2026-11-26 ~21:45, threshold
        # revised to USD 2026-11-26 ~21:48 when the multi-currency
        # picker was deferred — USD is the only buy currency for
        # now): a buy under $5 USD holds back 0.0012 SOL; a buy of
        # $5 USD or more holds back 0.012 SOL. Open question, asked
        # 2026-11-26 ~21:39: what happens when the real exit fee
        # comes in GREATER than the reserve held back — no top-up
        # or auto-raise behaviour exists until he answers; a sell
        # that fails for fees says so plainly in the log.
        self.user_buy_reserve_threshold_usd: float = float(env.get(
            "USER_BUY_RESERVE_THRESHOLD_USD", "5"))
        self.user_buy_reserve_small_sol: float = float(env.get(
            "USER_BUY_RESERVE_SMALL_SOL", "0.0012"))
        self.user_buy_reserve_large_sol: float = float(env.get(
            "USER_BUY_RESERVE_LARGE_SOL", "0.012"))

        # Trenches quick buy (buy-flow batch 6, James 2026-11-27
        # ~07:32-07:41, cap settled ~08:45): one-tap Buy 1 / Buy 2
        # buttons on each Trenches badge, both amounts settable by
        # the user in the buy popup. Defaults 0.02 / 0.05 SOL worth
        # of the coin. Priority fee if needed to enter or exit fast,
        # CAPPED at 0.001 SOL max per trade — the cap is a per-trade
        # SOL budget, not a per-compute-unit price: the executor
        # converts it to a unit price against the 200,000-unit
        # default budget (0.001 SOL / 200,000 CU = 5,000
        # micro-lamports per CU), so a spiking fee market can never
        # push one quick trade past 0.001 SOL in priority fees.
        self.quick_buy1_sol: float = _env_float(
            env.get("QUICK_BUY1_SOL"), 0.02, minimum=0.001)
        self.quick_buy2_sol: float = _env_float(
            env.get("QUICK_BUY2_SOL"), 0.05, minimum=0.001)
        self.quick_trade_priority_fee_max_sol: float = _env_float(
            env.get("QUICK_TRADE_PRIORITY_FEE_MAX_SOL"), 0.001,
            minimum=0.000001)

        # Pending-trade horizon: a sent signature with no receipt
        # after this long is finally marked failed (seconds).
        self.pending_trade_horizon_seconds: float = float(env.get(
            "PENDING_TRADE_HORIZON_SECONDS", "600"))
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
        # Secure session cookies (only sent over HTTPS). Railway
        # serves HTTPS, so this is on by default; set
        # SESSION_COOKIE_SECURE=0 only for plain-HTTP local dev.
        self.session_cookie_secure: bool = env.get(
            "SESSION_COOKIE_SECURE", "1") == "1"
        # Helius live-price stream (free-tier WSS): subscribe to
        # the bonding-curve accounts of held + watched mints and
        # price them from on-chain virtual reserves — ground truth,
        # ~400ms old (see tradingbot/streaming.py). Env-only, never
        # hardcoded. Unset = streaming OFF; the app behaves exactly
        # as today (DexScreener 5s poll). James's step: a free
        # Helius account + HELIUS_API_KEY on Railway. HELIUS_STREAM_
        # ENABLED=0 also turns it off with a key set.
        self.helius_api_key: str = env.get("HELIUS_API_KEY", "").strip()
        # Trading RPC: when no explicit RPC_URL is set and the Helius
        # key is configured on mainnet, trade through Helius instead
        # of the free public endpoint — the public endpoint
        # rate-limits and drops connections, which is what surfaces
        # as "RPC error / fails to simulate" on sells and sends.
        # Same key the price stream already uses; no new cost. The
        # key lives only in this URL (env-only, never logged —
        # nothing logs cfg.rpc_url).
        if (not env.get("RPC_URL") and self.network == "mainnet"
                and self.helius_api_key):
            self.rpc_url = ("https://mainnet.helius-rpc.com/?api-key="
                            + self.helius_api_key)
        self.helius_stream_enabled: bool = env.get(
            "HELIUS_STREAM_ENABLED", "1") == "1"
        # Cap on streamed mints (held + browser-watched). The set
        # is small by design — stream what you hold, poll what you
        # discover — so 50 is far above the real set; it only bounds
        # a pathological subscription count.
        self.helius_max_stream_mints: int = max(
            1, int(env.get("HELIUS_MAX_STREAM_MINTS", "50")))

    def user_buy_gas_reserve_sol(self, amount_usd) -> float:
        """The SOL to hold back after a User buy, by the buy's size
        in USD (see the tiers set in __init__). An unparseable
        amount fails safe to the large tier — never to no reserve."""
        try:
            usd = float(amount_usd)
        except (TypeError, ValueError):
            return self.user_buy_reserve_large_sol
        if usd < self.user_buy_reserve_threshold_usd:
            return self.user_buy_reserve_small_sol
        return self.user_buy_reserve_large_sol

    def quick_buy_reserve_sol(self, amount_usd) -> float:
        """The SOL a Trenches quick buy must leave behind: the
        buy-size tier for its USD size PLUS the priority-fee cap
        (0.001 SOL). The fee comes on top of the buy, so the
        reserve the executor enforces already contains it — the
        wallet keeps the tier reserve intact after the fee is
        paid. A 0.02 SOL buy is the small tier (0.0012), so a
        quick buy leaves 0.0022 SOL behind before ATA rent."""
        return (self.user_buy_gas_reserve_sol(amount_usd)
                + self.quick_trade_priority_fee_max_sol)

    def pct_buy_amounts_sol(self, wallet_sol, sol_price_usd,
                            include_ata_rent=False) -> dict:
        """The SOL each quick-buy % button buys: {25: sol|None,
        50: ..., 75: ..., 100: ...} (buy-flow batch 4). Each button
        buys that % of the SOL in the wallet but ALWAYS leaves the
        gas reserve behind — 100% is the wallet minus the reserve,
        never the whole wallet. The reserve is the buy-size tier
        for the button's own USD size at the live SOL price (no
        live price fails safe to the large tier, exactly like
        user_buy_gas_reserve_sol), PLUS the token-account rent
        when include_ata_rent — the executor charges that rent on
        a first buy of a coin with no token account yet, so a
        button that forgot it would be refused at the edge (the
        executor's own comment: without the rent, a buy at the
        edge fails for insufficient funds). The tier is picked
        from the button's raw % amount before capping: capping can
        only shrink the buy, so the tier it picks is never smaller
        than the capped amount needs. Amounts are floored to 6
        decimals — never rounded up past the cap. None = the wallet
        cannot cover the reserve at all, so the button is
        disabled; a missing/unreadable wallet balance (None)
        disables every button. This is the ONE source of truth for
        the button math — the templates and the route never
        re-implement the tier; the server-side reserve refusal in
        the executors stays as the backstop."""
        pcts = (25, 50, 75, 100)
        if wallet_sol is None:
            return {pct: None for pct in pcts}
        try:
            wallet = float(wallet_sol)
        except (TypeError, ValueError):
            return {pct: None for pct in pcts}
        if wallet <= 0:
            return {pct: None for pct in pcts}
        rent_sol = (ATA_RENT_FALLBACK_LAMPORTS / 1_000_000_000
                    if include_ata_rent else 0.0)
        out = {}
        for pct in pcts:
            raw = wallet * pct / 100.0
            usd = (raw * sol_price_usd
                   if sol_price_usd else None)
            reserve = self.user_buy_gas_reserve_sol(usd) + rent_sol
            amount = min(raw, wallet - reserve)
            amount = math.floor(amount * 1_000_000) / 1_000_000
            out[pct] = amount if amount > 0 else None
        return out

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
