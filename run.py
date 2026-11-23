"""Entry point: python run.py (Railway start command)."""

import os

from tradingbot import create_app
from tradingbot.config import Config

if __name__ == "__main__":
    cfg = Config()
    cfg.require_secrets()
    app = create_app(cfg)
    if cfg.live_feed_enabled:
        from tradingbot.livefeed import start_feed

        start_feed(app)
        # Helius curve stream (free WSS): live prices for held +
        # watched mints straight off the chain. No key = off, and
        # the app runs on the DexScreener poll exactly as before.
        from tradingbot.streaming import start_helius_stream

        start_helius_stream(app)
    if cfg.silent_loop_enabled:
        from tradingbot.silent_loop import start_background_loop

        start_background_loop(app)
    # threaded=True: a WebSocket client holds its connection open, so
    # the server must serve other requests on other threads.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            threaded=True)
