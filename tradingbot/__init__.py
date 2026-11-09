"""Trading Bot app factory. Wires config, DB, sessions, and routes."""

from __future__ import annotations

import os

from flask import Flask

from .config import Config, load_dotenv
from .db import init_db


def create_app(config: Config | None = None) -> Flask:
    load_dotenv()
    cfg = config or Config()
    app = Flask(__name__, template_folder="templates",
                static_folder="static")
    app.config["SECRET_KEY"] = cfg.session_secret or os.urandom(32).hex()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["TRADINGBOT_CONFIG"] = cfg
    # One shared connection per app instance (sqlite, single-process
    # dev/Railway hobby deploy; check_same_thread off for the Flask
    # dev server's threaded mode).
    conn = init_db(cfg.database_path)
    app.config["DB"] = conn

    from .routes import register_routes

    register_routes(app)
    return app
