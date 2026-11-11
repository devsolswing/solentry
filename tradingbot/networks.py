"""Networks: which chain a coin lives on, and the network this app sends on.

The app is Solana-only (devnet by default, mainnet only on explicit
config). The coin detail page covers two kinds of coins: price-list
symbols (SOL, BTC, ...) that can live on many different chains, and
pump.fun mints, which are Solana tokens by construction. A Send or
Receive that does not say which network it uses invites the user to
send a coin on the wrong chain — funds sent to a Solana address on
another network do not arrive. So every Send and Receive on the coin
detail page states the network in plain words, and a coin that is not
a Solana asset is refused plainly instead of being sent as SOL on the
wrong network. Nothing here guesses: a coin with no known network is
refused, never silently defaulted.
"""

from __future__ import annotations

from .prices import COINS
from .solana import is_valid_address

# Native chain of each known price-list coin. USDC is deliberately
# absent: it exists on many chains (Solana, Ethereum, and others),
# so no single network can be named for it — it is refused with the
# multi-network reason instead.
COIN_NETWORKS = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "DOGE": "Dogecoin",
    "PEPE": "Ethereum",
    "BONK": "Solana",
    "JUP": "Solana",
}

MULTI_CHAIN_COINS = {"USDC"}


def network_label(network: str) -> str:
    """Plain-words label for the network this app sends on."""
    return f"Solana ({(network or 'devnet').lower()})"


def coin_network(coin_id: str) -> dict:
    """Which chain a coin lives on.

    Returns {"network", "is_solana", "multi_chain", "known"}. A
    pump.fun mint is a Solana token by construction (its address
    only exists on Solana). A known symbol maps to its native
    chain. Anything else is unknown — never guessed.
    """
    if not coin_id:
        return {"network": None, "is_solana": False,
                "multi_chain": False, "known": False}
    upper = coin_id.upper()
    if upper in MULTI_CHAIN_COINS:
        return {"network": None, "is_solana": False,
                "multi_chain": True, "known": True}
    if upper in COIN_NETWORKS:
        net = COIN_NETWORKS[upper]
        return {"network": net, "is_solana": net == "Solana",
                "multi_chain": False, "known": True}
    if is_valid_address(coin_id):
        return {"network": "Solana", "is_solana": True,
                "multi_chain": False, "known": True}
    return {"network": None, "is_solana": False,
            "multi_chain": False, "known": False}


def coin_display_name(coin_id: str) -> str:
    """Name for messages: the known name, else the id itself."""
    upper = (coin_id or "").upper()
    if upper in COINS:
        return COINS[upper]["name"]
    return coin_id or "Unknown coin"


def send_refusal(coin_id: str, app_network: str = "devnet") -> str | None:
    """Plain refusal when a send from this coin's page is not
    possible, else None (the send may go ahead on Solana).

    The same sentence is shown in the coin detail Send/Receive
    windows (where the form is hidden) and flashed when a POST for
    that coin is refused server-side, so the reason is identical
    wherever the user meets it.
    """
    info = coin_network(coin_id)
    if info["is_solana"]:
        return None
    name = coin_display_name(coin_id)
    label = network_label(app_network)
    if info["multi_chain"]:
        return (
            f"{name} exists on many networks (Solana, Ethereum, and "
            f"others). SolEntry only sends on the {label} network, "
            f"so {name} can't be sent from here. Nothing was sent.")
    if info["known"]:
        return (
            f"{name} lives on the {info['network']} network. SolEntry "
            f"only sends on the {label} network, so {name} can't "
            "be sent from here. Nothing was sent.")
    return ("Unknown coin — the network could not be determined, so "
            "the send was refused rather than guessed. Nothing was "
            "sent.")
