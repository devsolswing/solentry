"""Price-signal evaluation, shared by the bot suggestion route and the
silent-trading loop so both use exactly one rule set.

Rules (the existing v1 rules — no new trading rules invented here):
  * 24h change >= +2%  -> buy signal
  * 24h change <= -5%  -> sell signal
  * otherwise          -> no signal
The strategy (careful / balanced / bold) is a risk label in v1; it does
not change these thresholds.
"""

from __future__ import annotations

BUY_CHANGE_PCT = 2.0
SELL_CHANGE_PCT = -5.0


def signal_for_coin(coin: str, change_24h: float | None,
                    strategy: str) -> tuple[str | None, str]:
    """Return (side, detail) for one coin, or (None, detail)."""
    if change_24h is None:
        return None, f"No 24h change data for {coin}."
    if change_24h >= BUY_CHANGE_PCT:
        return "buy", (f"{coin} is up {change_24h:.1f}% in 24h — "
                       f"{strategy} strategy suggests considering a buy.")
    if change_24h <= SELL_CHANGE_PCT:
        return "sell", (f"{coin} is down {change_24h:.1f}% in 24h — "
                        f"{strategy} strategy suggests considering a sell.")
    return None, f"{coin} moved {change_24h:.1f}% in 24h — no signal."
