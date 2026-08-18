"""Governed display universe for Discover.

Membership is policy-driven rather than a hand-picked list. CoinGecko's live
market-cap census determines which registered crypto assets are eligible; this
module records explicit exclusions and reports anything the registry cannot
yet map safely to the display price feed.
"""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "2026-08-18.1"
CRYPTO_MARKET_CAP_LIMIT = 60
MIN_CRYPTO_OBSERVATIONS = 365

# CoinGecko id -> display metadata. Yahoo symbols with numeric suffixes avoid
# known ticker collisions (for example SUI and TAO).
CRYPTO_REGISTRY: dict[str, dict[str, str]] = {
    "bitcoin": {"symbol": "BTC", "name": "Bitcoin", "yf": "BTC-USD"},
    "ethereum": {"symbol": "ETH", "name": "Ethereum", "yf": "ETH-USD"},
    "ripple": {"symbol": "XRP", "name": "XRP", "yf": "XRP-USD"},
    "solana": {"symbol": "SOL", "name": "Solana", "yf": "SOL-USD"},
    "tron": {"symbol": "TRX", "name": "TRON", "yf": "TRX-USD"},
    "hyperliquid": {"symbol": "HYPE", "name": "Hyperliquid", "yf": "HYPE32196-USD"},
    "dogecoin": {"symbol": "DOGE", "name": "Dogecoin", "yf": "DOGE-USD"},
    "leo-token": {"symbol": "LEO", "name": "LEO Token", "yf": "LEO-USD"},
    "zcash": {"symbol": "ZEC", "name": "Zcash", "yf": "ZEC-USD"},
    "monero": {"symbol": "XMR", "name": "Monero", "yf": "XMR-USD"},
    "cardano": {"symbol": "ADA", "name": "Cardano", "yf": "ADA-USD"},
    "whitebit": {"symbol": "WBT", "name": "WhiteBIT Coin", "yf": "WBT-USD"},
    "bitcoin-cash": {"symbol": "BCH", "name": "Bitcoin Cash", "yf": "BCH-USD"},
    "the-open-network": {"symbol": "TON", "name": "Toncoin", "yf": "TON-USD"},
    "litecoin": {"symbol": "LTC", "name": "Litecoin", "yf": "LTC-USD"},
    "hedera-hashgraph": {"symbol": "HBAR", "name": "Hedera", "yf": "HBAR-USD"},
    "sui": {"symbol": "SUI", "name": "Sui", "yf": "SUI20947-USD"},
    "avalanche-2": {"symbol": "AVAX", "name": "Avalanche", "yf": "AVAX-USD"},
    "shiba-inu": {"symbol": "SHIB", "name": "Shiba Inu", "yf": "SHIB-USD"},
    "uniswap": {"symbol": "UNI", "name": "Uniswap", "yf": "UNI7083-USD"},
    "crypto-com-chain": {"symbol": "CRO", "name": "Cronos", "yf": "CRO-USD"},
    "near": {"symbol": "NEAR", "name": "NEAR Protocol", "yf": "NEAR-USD"},
    "okb": {"symbol": "OKB", "name": "OKB", "yf": "OKB-USD"},
    "bittensor": {"symbol": "TAO", "name": "Bittensor", "yf": "TAO22974-USD"},
    "htx-dao": {"symbol": "HTX", "name": "HTX DAO", "yf": "HTX-USD"},
    "ondo-finance": {"symbol": "ONDO", "name": "Ondo", "yf": "ONDO-USD"},
    "mantle": {"symbol": "MNT", "name": "Mantle", "yf": "MNT27075-USD"},
    "aave": {"symbol": "AAVE", "name": "Aave", "yf": "AAVE-USD"},
    "polkadot": {"symbol": "DOT", "name": "Polkadot", "yf": "DOT-USD"},
    "internet-computer": {"symbol": "ICP", "name": "Internet Computer", "yf": "ICP-USD"},
    "worldcoin-wld": {"symbol": "WLD", "name": "Worldcoin", "yf": "WLD-USD"},
}

EXCLUDED_CRYPTO_IDS: dict[str, str] = {
    "tether": "stablecoin",
    "usd-coin": "stablecoin",
    "usds": "stablecoin",
    "dai": "stablecoin",
    "usd1-wlfi": "stablecoin",
    "ethena-usde": "stablecoin",
    "global-dollar": "stablecoin",
    "paypal-usd": "stablecoin",
    "ripple-usd": "stablecoin",
    "usdd": "stablecoin",
    "falcon-finance": "stablecoin",
    "bfusd": "stablecoin",
    "united-stables": "stablecoin",
    "figure-heloc": "tokenized credit",
    "hashnote-usyc": "tokenized cash",
    "blackrock-usd-institutional-digital-liquidity-fund": "tokenized cash",
    "ondo-us-dollar-yield": "tokenized cash",
    "tether-gold": "tokenized gold; gold is represented separately",
    "pax-gold": "tokenized gold; gold is represented separately",
    # Operator policy 2026-08-18, after the rotation research closed: the
    # measured crypto section is one factor at different amplitudes, so
    # membership follows distinct user-facing roles, not breadth. These four
    # carried either a redundant role or none. Dated and revisable by a later
    # policy version, unlike the asset-class exclusions above.
    "binancecoin": (
        "operator policy 2026-08-18: exchange-token role carries no distinct "
        "choice for a user; redundant beta"
    ),
    "chainlink": (
        "operator policy 2026-08-18: comparative underperformance since 2021 "
        "and no distinct role on the ladder"
    ),
    "stellar": (
        "operator policy 2026-08-18: redundant with XRP -- same payments "
        "role, shallower history"
    ),
    "algorand": (
        "operator policy 2026-08-18: weakest name of the 2026-08-18 rotation "
        "batch; never registered, excluded so a future top-60 entry cannot "
        "surface it silently"
    ),
}

# Live census members that cannot be mapped safely, with what was measured.
#
# Deliberately NOT `EXCLUDED_CRYPTO_IDS`. That dict means "this does not belong
# in a ranking of digital assets" -- a stablecoin is excluded whatever the data
# says, and always will be. These are assets that *should* rank and cannot,
# because no display-feed symbol for them has been verified. Filing them as
# exclusions would retire an open coverage gap by renaming it, and they would
# drop out of the `unmapped` list that is the only place the gap is visible.
#
# The reasons are recorded so the next audit starts from a measurement instead
# of repeating one. Each was taken on 2026-08-13 by pulling the obvious
# `<SYMBOL>-USD` ticker from the display feed and comparing its last close to
# the same asset's live CoinGecko price. **Every one of the seven is a different
# asset.** Two of them are the dangerous shape: M-USD carries 1,515 daily
# observations and SKY-USD carries 3,200, so both clear the 365-observation gate
# comfortably and would have been ranked -- under the right name, with another
# coin's entire price history. The registry's numeric suffixes (HYPE32196-USD,
# SUI20947-USD, TAO22974-USD) exist for exactly this collision, and a mapping
# added without the price check is how one gets past it.
UNVERIFIED_CRYPTO_IDS: dict[str, str] = {
    "rain": (
        "RAIN-USD on the display feed is a different asset: 1 observation, last "
        "close $0.000016 against CoinGecko's $0.0123 (measured 2026-08-13)"
    ),
    "canton-network": (
        "CC-USD on the display feed is a different asset: 792 observations, last "
        "close $0.180 against CoinGecko's $0.0972 (measured 2026-08-13)"
    ),
    "world-liberty-financial": (
        "WLFI-USD on the display feed is a different asset: 323 observations, "
        "last close $2.7e-13 against CoinGecko's $0.0550 (measured 2026-08-13)"
    ),
    "aster-2": (
        "ASTER-USD on the display feed is a different asset: 1 observation, last "
        "close $0.00108 against CoinGecko's $0.602 (measured 2026-08-13)"
    ),
    "memecore": (
        "M-USD on the display feed is a different asset: 1,515 observations, "
        "last close $0.000251 against CoinGecko's $1.10 (measured 2026-08-13). "
        "It clears the observation gate, so only the price check catches it"
    ),
    "morpho": (
        "MORPHO-USD on the display feed is a different asset: 90 observations, "
        "last close $0.00181 against CoinGecko's $1.96 (measured 2026-08-13)"
    ),
    "sky": (
        "SKY-USD on the display feed is a different asset: 3,200 observations, "
        "last close $0.0141 against CoinGecko's $0.0533 (measured 2026-08-13). "
        "It clears the observation gate, so only the price check catches it"
    ),
}

UNMAPPED_WITHOUT_A_MEASUREMENT = "no verified display-feed mapping"


def crypto_assets() -> list[dict[str, str]]:
    """Return every safely mapped candidate; live rank filtering happens later."""
    return [
        {**metadata, "asset_class": "crypto", "area": "Digital assets", "coin_id": coin_id}
        for coin_id, metadata in CRYPTO_REGISTRY.items()
    ]


def evaluate_crypto_census(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify the live top-N census into registered, excluded, and unmapped."""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for row in rows:
        rank = row.get("market_cap_rank")
        if not isinstance(rank, int) or rank > CRYPTO_MARKET_CAP_LIMIT:
            continue
        coin_id = str(row.get("id", ""))
        item = {
            "rank": rank,
            "symbol": str(row.get("symbol", "")).upper(),
            "name": str(row.get("name", "")),
            "coin_id": coin_id,
        }
        if coin_id in EXCLUDED_CRYPTO_IDS:
            excluded.append({**item, "reason": EXCLUDED_CRYPTO_IDS[coin_id]})
        elif coin_id in CRYPTO_REGISTRY:
            included.append({
                **item,
                "registered_symbol": CRYPTO_REGISTRY[coin_id]["symbol"],
                # Carried so the display feed's latest close can be cross-checked
                # against the live price it claims to track. Without it (registry
                # fallback), the check degrades to not running rather than to
                # trusting the display feed alone.
                "price": row.get("current_price"),
            })
        else:
            # An unmapped asset stays unmapped whether or not anyone has looked
            # into why. The recorded finding replaces the generic sentence, and
            # its absence is itself informative: this one has not been measured
            # yet.
            unmapped.append(
                {
                    **item,
                    "reason": UNVERIFIED_CRYPTO_IDS.get(
                        coin_id, UNMAPPED_WITHOUT_A_MEASUREMENT
                    ),
                    "measured": coin_id in UNVERIFIED_CRYPTO_IDS,
                }
            )
    return {
        "policy_version": POLICY_VERSION,
        "market_cap_limit": CRYPTO_MARKET_CAP_LIMIT,
        "included": sorted(included, key=lambda item: item["rank"]),
        "excluded": sorted(excluded, key=lambda item: item["rank"]),
        "unmapped": sorted(unmapped, key=lambda item: item["rank"]),
    }


__all__ = [
    "CRYPTO_MARKET_CAP_LIMIT",
    "CRYPTO_REGISTRY",
    "EXCLUDED_CRYPTO_IDS",
    "MIN_CRYPTO_OBSERVATIONS",
    "POLICY_VERSION",
    "UNMAPPED_WITHOUT_A_MEASUREMENT",
    "UNVERIFIED_CRYPTO_IDS",
    "crypto_assets",
    "evaluate_crypto_census",
]
