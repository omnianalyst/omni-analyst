"""Governed display universe for Discover.

Membership is policy-driven rather than a hand-picked list. CoinGecko's live
market-cap census determines which registered crypto assets are eligible; this
module records explicit exclusions and reports anything the registry cannot
yet map safely to the display price feed.
"""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "2026-08-12.1"
CRYPTO_MARKET_CAP_LIMIT = 60
MIN_CRYPTO_OBSERVATIONS = 365

# CoinGecko id -> display metadata. Yahoo symbols with numeric suffixes avoid
# known ticker collisions (for example SUI and TAO).
CRYPTO_REGISTRY: dict[str, dict[str, str]] = {
    "bitcoin": {"symbol": "BTC", "name": "Bitcoin", "yf": "BTC-USD"},
    "ethereum": {"symbol": "ETH", "name": "Ethereum", "yf": "ETH-USD"},
    "binancecoin": {"symbol": "BNB", "name": "BNB", "yf": "BNB-USD"},
    "ripple": {"symbol": "XRP", "name": "XRP", "yf": "XRP-USD"},
    "solana": {"symbol": "SOL", "name": "Solana", "yf": "SOL-USD"},
    "tron": {"symbol": "TRX", "name": "TRON", "yf": "TRX-USD"},
    "hyperliquid": {"symbol": "HYPE", "name": "Hyperliquid", "yf": "HYPE32196-USD"},
    "dogecoin": {"symbol": "DOGE", "name": "Dogecoin", "yf": "DOGE-USD"},
    "leo-token": {"symbol": "LEO", "name": "LEO Token", "yf": "LEO-USD"},
    "zcash": {"symbol": "ZEC", "name": "Zcash", "yf": "ZEC-USD"},
    "monero": {"symbol": "XMR", "name": "Monero", "yf": "XMR-USD"},
    "cardano": {"symbol": "ADA", "name": "Cardano", "yf": "ADA-USD"},
    "chainlink": {"symbol": "LINK", "name": "Chainlink", "yf": "LINK-USD"},
    "whitebit": {"symbol": "WBT", "name": "WhiteBIT Coin", "yf": "WBT-USD"},
    "stellar": {"symbol": "XLM", "name": "Stellar", "yf": "XLM-USD"},
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
}


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
            included.append({**item, "registered_symbol": CRYPTO_REGISTRY[coin_id]["symbol"]})
        else:
            unmapped.append({**item, "reason": "no verified display-feed mapping"})
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
    "crypto_assets",
    "evaluate_crypto_census",
]
