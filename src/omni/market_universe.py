"""Governed display universe for Discover.

Membership is policy-driven rather than a hand-picked list. CoinGecko's live
market-cap census determines which registered crypto assets are eligible; this
module records explicit exclusions and reports anything the registry cannot
yet map safely to the display price feed.
"""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "2026-08-18.2"
CRYPTO_MARKET_CAP_LIMIT = 60
MIN_CRYPTO_OBSERVATIONS = 365

# CoinGecko id -> display metadata. Yahoo symbols with numeric suffixes avoid
# known ticker collisions (for example SUI and TAO).
#
# The 2026-08-18.2 ladder: the operator's seven names, one per distinct
# user-facing role -- BTC (core), ETH (smart-contract platform), SOL (top
# alt platform, also a carry-book name), XRP (payments), DOGE (meme), XMR
# (privacy), HBAR (enterprise L1, the ladder's highest-vol rung). Chosen the
# day the rotation research closed: with per-tier alpha measured at zero,
# breadth implies a choice precision that does not exist. Everything removed
# from this registry keeps ingesting in the background through the entity
# seeds; it stops being *ranked* on Discover, which is a display decision,
# not a coverage decision.
CRYPTO_REGISTRY: dict[str, dict[str, str]] = {
    "bitcoin": {"symbol": "BTC", "name": "Bitcoin", "yf": "BTC-USD"},
    "ethereum": {"symbol": "ETH", "name": "Ethereum", "yf": "ETH-USD"},
    "ripple": {"symbol": "XRP", "name": "XRP", "yf": "XRP-USD"},
    "solana": {"symbol": "SOL", "name": "Solana", "yf": "SOL-USD"},
    "dogecoin": {"symbol": "DOGE", "name": "Dogecoin", "yf": "DOGE-USD"},
    "monero": {"symbol": "XMR", "name": "Monero", "yf": "XMR-USD"},
    "hedera-hashgraph": {"symbol": "HBAR", "name": "Hedera", "yf": "HBAR-USD"},
}

# The reason every off-ladder id carries: display removal, not coverage
# removal. Stated once so no entry implies its data stopped flowing.
_OFF_LADDER = (
    "operator policy 2026-08-18.2: not on the seven-name display ladder; "
    "still ingested in the background, not ranked on Discover"
)

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
    # Operator policy 2026-08-18.2, the seven-name ladder: everything not on
    # the ladder leaves the DISPLAY universe, not the ingestion universe.
    # Entity seeds keep these ingesting in the background; a later policy
    # version can restore any of them to ranking by moving the id back into
    # CRYPTO_REGISTRY, which is the only place display membership lives.
    "tron": _OFF_LADDER,
    "hyperliquid": _OFF_LADDER,
    "leo-token": _OFF_LADDER,
    "zcash": _OFF_LADDER,
    "cardano": _OFF_LADDER,
    "whitebit": _OFF_LADDER,
    "bitcoin-cash": _OFF_LADDER,
    "the-open-network": _OFF_LADDER,
    "litecoin": _OFF_LADDER,
    "sui": _OFF_LADDER,
    "avalanche-2": _OFF_LADDER,
    "shiba-inu": _OFF_LADDER,
    "uniswap": _OFF_LADDER,
    "crypto-com-chain": _OFF_LADDER,
    "near": _OFF_LADDER,
    "okb": _OFF_LADDER,
    "bittensor": _OFF_LADDER,
    "htx-dao": _OFF_LADDER,
    "ondo-finance": _OFF_LADDER,
    "mantle": _OFF_LADDER,
    "aave": _OFF_LADDER,
    "polkadot": _OFF_LADDER,
    "internet-computer": _OFF_LADDER,
    "worldcoin-wld": _OFF_LADDER,
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
