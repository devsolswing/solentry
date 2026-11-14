"""Trading Bot app factory. Wires config, DB, sessions, and routes."""

from __future__ import annotations

import os
import secrets
from datetime import timedelta

from flask import Flask, g

from .config import Config, load_dotenv
from .db import init_db


def create_app(config: Config | None = None) -> Flask:
    load_dotenv()
    cfg = config or Config()
    app = Flask(__name__, template_folder="templates",
                static_folder="static")
    app.config["SECRET_KEY"] = cfg.session_secret or os.urandom(32).hex()
    # Session cookies: HttpOnly (no JS access), SameSite=Lax (no
    # cross-site posting), Secure over HTTPS (Railway serves HTTPS).
    # Sessions live 12 hours — long enough for a day of trading,
    # short enough that a stolen cookie does not last forever.
    # Login/signup rotate the session (see _rotate_session), so a
    # pre-login session id is useless after sign-in.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = cfg.session_cookie_secure
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["TRADINGBOT_CONFIG"] = cfg
    # One shared connection per app instance (sqlite, single-process
    # dev/Railway hobby deploy; check_same_thread off for the Flask
    # dev server's threaded mode).
    conn = init_db(cfg.database_path)
    app.config["DB"] = conn

    # Cloudflare Turnstile is enforced only when both keys are set.
    # With either missing, warn once at startup and continue without
    # it — never lock James out for missing keys.
    if not cfg.turnstile_site_key or not cfg.turnstile_secret_key:
        app.logger.warning(
            "Turnstile not configured (TURNSTILE_SITE_KEY / "
            "TURNSTILE_SECRET_KEY missing) — login/signup run without "
            "a bot challenge. See the README security page for the "
            "free Cloudflare setup.")

    @app.before_request
    def _set_csp_nonce():
        # Fresh nonce per request, for the inline <script> blocks.
        # Templates use {{ csp_nonce }} on their script tags; the
        # CSP header below only allows those nonces (plus our own
        # static files and Cloudflare's challenge scripts).
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _inject_csp_nonce():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}

    @app.after_request
    def _security_headers(resp):
        # Content Security Policy: only our own files, the per-request
        # nonce for the two inline scripts, and Cloudflare's
        # Turnstile challenge (scripts + widget frame). No inline
        # event handlers, no other origins, no framing of this app.
        # style-src allows 'unsafe-inline' for one inline style in
        # settings.html; img-src allows data: for the hero image
        # plus the token-logo hosts (DexScreener, pump.fun's image
        # host, IPFS and Arweave — the only places a held coin's
        # official logo can come from) so home-page coin images can
        # load — images only, nothing else is widened.
        nonce = getattr(g, "csp_nonce", "")
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' "
            "https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://dd.dexscreener.com "
            "https://image.pump.fun https://ipfs.io "
            "https://arweave.net; font-src 'self'; "
            "connect-src 'self'; "
            "frame-src https://challenges.cloudflare.com; "
            "object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        resp.headers["Content-Security-Policy"] = csp
        # HSTS: browsers must only ever reach this app over HTTPS.
        resp.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains")
        # No framing, no MIME sniffing, no leaking the full URL.
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return resp

    from .livefeed import LiveFeed

    # One live-feed service for the whole app (validated ticks for
    # majors + watched mints). Created here, started only from run.py
    # — create_app never opens sockets, so tests stay offline.
    app.config["LIVE_FEED"] = LiveFeed(cfg)
    # Flask-Sock: the app's own WebSocket (/ws/live) runs on the same
    # server. The route itself is registered in register_routes.
    from flask_sock import Sock

    app.config["SOCK"] = Sock(app)

    from .routes import register_routes

    register_routes(app)
    return app
