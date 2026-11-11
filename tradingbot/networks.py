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
# multi-network reason instead (see MULTI_CHAIN_NETWORKS below for
# the verified list, used by search).
#
# Chains verified 2026-11-11: the native L1s are the coin itself,
# the ERC-20s are Ethereum tokens (PEPE, LINK, UNI, AAVE, ONDO),
# the Solana natives are Solana tokens (BONK, JUP, WIF, POPCAT),
# and RENDER is Ethereum — its CoinGecko asset_platform_id is
# "ethereum" (checked live; WIF/POPCAT returned "solana").
COIN_NETWORKS = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "DOGE": "Dogecoin",
    "PEPE": "Ethereum",
    "BONK": "Solana",
    "JUP": "Solana",
    "ZEC": "Zcash",
    "XRP": "XRP Ledger",
    "ADA": "Cardano",
    "LTC": "Litecoin",
    "DOT": "Polkadot",
    "LINK": "Ethereum",
    "AVAX": "Avalanche",
    "ATOM": "Cosmos",
    "XLM": "Stellar",
    "ALGO": "Algorand",
    "NEAR": "NEAR Protocol",
    "UNI": "Ethereum",
    "AAVE": "Ethereum",
    "XTZ": "Tezos",
    "ETC": "Ethereum Classic",
    "BCH": "Bitcoin Cash",
    "FIL": "Filecoin",
    "HBAR": "Hedera",
    "SUI": "Sui",
    "APT": "Aptos",
    "SEI": "Sei",
    "INJ": "Injective",
    "TIA": "Celestia",
    "ONDO": "Ethereum",
    "RENDER": "Ethereum",
    "TAO": "Bittensor",
    "WIF": "Solana",
    "POPCAT": "Solana",
}

MULTI_CHAIN_COINS = {"USDC"}

# Every network a multi-chain coin REALLY exists on — only networks
# verified for that coin, never guessed. USDC: CoinGecko's USDC
# platforms list returned 36 chains on 2026-11-11; the list below
# is the major networks with real native/canonical USDC presence
# (all 21 appear in that verified list). Obscure/new chains from
# the full list where the app cannot verify real USDC presence
# are left out rather than listed on a guess. Solana first (the
# app's home chain), then alphabetical.
MULTI_CHAIN_NETWORKS = {
    "USDC": ["Solana", "Algorand", "Aptos", "Arbitrum", "Avalanche",
             "Base", "Celo", "Cronos", "Ethereum", "Hedera",
             "Injective", "NEAR Protocol", "Optimism", "Polkadot",
             "Polygon", "Sei", "Starknet", "Stellar", "Sui",
             "XRP Ledger", "zkSync"],
}


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


def coin_networks(coin_id: str) -> list:
    """Every verified network a coin exists on, as a list.

    A single-chain coin returns its one native chain; a
    multi-chain coin returns every verified network from
    MULTI_CHAIN_NETWORKS; a pump.fun mint returns ["Solana"];
    an unknown coin returns [] — never guessed.
    """
    if not coin_id:
        return []
    upper = coin_id.upper()
    if upper in MULTI_CHAIN_NETWORKS:
        return list(MULTI_CHAIN_NETWORKS[upper])
    info = coin_network(coin_id)
    if info["network"] is not None:
        return [info["network"]]
    return []


# Common wrapped-token symbol prefixes on Solana: wBTC, wETH, wZEC,
# wXRP (Portal/Wormhole style) and soBTC / soETH (old Wormhole
# style). A "$" prefix (e.g. "$WIF") is stripped first.
_WRAPPED_SYMBOL_PREFIXES = ("W", "SO")


def _symbol_names_foreign_coin(symbol: str | None) -> bool:
    """True when a Solana token's symbol names a coin whose native
    chain is NOT Solana — the bare symbol or with a wrapped prefix.

    Such a token is a stand-in for that coin on Solana, never the
    native coin itself, so search labels it Wrapped. A symbol
    naming a Solana-native coin (BONK, WIF, SOL, ...) is not
    foreign and returns False; an unknown symbol returns False —
    the token is shown as a plain Solana token, not mislabeled.
    """
    if not symbol:
        return False
    s = symbol.upper().strip()
    if s.startswith("$"):
        s = s[1:]
    candidates = {s}
    for prefix in _WRAPPED_SYMBOL_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix):
            candidates.add(s[len(prefix):])
    for cand in candidates:
        net = COIN_NETWORKS.get(cand)
        if net is not None and net != "Solana":
            return True
    return False


def search_result_network(coin_id: str, is_mint: bool,
                          symbol: str | None = None) -> dict:
    """Network info for one search result row.

    Returns {"networks": [...], "wrapped": bool}. A known coin
    lists its native chain — every verified chain for a
    multi-chain coin. A mint lists Solana, and is marked wrapped
    when its symbol names a coin whose native chain is elsewhere
    (a Solana stand-in for that coin, never the native coin).
    An unknown coin lists nothing: the row says "Network unknown"
    rather than inventing a network.
    """
    if is_mint:
        return {"networks": ["Solana"],
                "wrapped": _symbol_names_foreign_coin(symbol)}
    return {"networks": coin_networks(coin_id), "wrapped": False}


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
