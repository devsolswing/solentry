"""Networks: which chain a coin lives on, and the network this app sends on.

The app is Solana-only (devnet by default, mainnet only on explicit
config) and trades pump.fun coins only. Every coin in this app is a
pump.fun mint — a Solana token by construction, its address existing
only on Solana. There is no major-coin list any more, so there is no
per-coin native-chain table: anything that is not a Solana mint
address is not a coin in this app at all.

A Send or Receive that does not say which network it uses invites
the user to send a coin on the wrong chain — funds sent to a Solana
address on another network do not arrive. So every Send and Receive
on the coin detail page states the network in plain words, and a
coin that is not a Solana asset is refused plainly instead of being
sent as SOL on the wrong network. Nothing here guesses: a coin with
no known network is refused, never silently defaulted.
"""

from __future__ import annotations

from .solana import is_valid_address


def network_label(network: str) -> str:
    """Plain-words label for the network this app sends on."""
    return f"Solana ({(network or 'devnet').lower()})"


def coin_network(coin_id: str) -> dict:
    """Which chain a coin lives on.

    Returns {"network", "is_solana", "multi_chain", "known"}. A
    pump.fun mint is a Solana token by construction (its address
    only exists on Solana), and SOL is the network's native
    currency. Anything else is unknown — never guessed, never
    defaulted to a chain. (SOL is Solana-native but NOT a listed
    coin: the app never lists or trades it, it is only the unit of
    account and the currency a send moves.)
    """
    if not coin_id:
        return {"network": None, "is_solana": False,
                "multi_chain": False, "known": False}
    if coin_id == "SOL":
        return {"network": "Solana", "is_solana": True,
                "multi_chain": False, "known": True}
    if is_valid_address(coin_id):
        return {"network": "Solana", "is_solana": True,
                "multi_chain": False, "known": True}
    return {"network": None, "is_solana": False,
            "multi_chain": False, "known": False}


def coin_display_name(coin_id: str) -> str:
    """Name for messages: the id itself, or "Unknown coin" when
    there is nothing to show. Never a guessed name."""
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
    return (f"{name} is not a Solana token, so it can't be sent "
            f"from SolEntry — this app only sends on the {label} "
            f"network. Nothing was sent.")
