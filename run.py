"""Entry point: python run.py (Railway start command)."""

import os

from tradingbot import create_app
from tradingbot.config import Config

if __name__ == "__main__":
    cfg = Config()
    cfg.require_secrets()
    app = create_app(cfg)
    if cfg.silent_loop_enabled:
        from tradingbot.silent_loop import start_background_loop

        start_background_loop(app)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
