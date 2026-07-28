"""Catalog of data/AI/broker providers that accept operator credentials.

Single source of truth for the Provider settings UI and the credential
resolver. Each entry maps a stable `provider_key` to its display metadata and
the Settings attribute used for the environment-variable fallback.

Some no-key public sources (frankfurter/ECB, DefiLlama) are omitted because
they need no credentials at all.

iex_cloud is deliberately absent: IEX Cloud retired its API in 2024 and the
iexcloud.io domain has since lapsed. Adding a catalog entry or Settings field
for a dead service would only mislead operators into configuring a key that can
never work.
"""

from typing import Any

# category buckets used by the settings UI
CATEGORY_MARKET_DATA = "market_data"
CATEGORY_CRYPTO = "crypto"
CATEGORY_NEWS = "news"
CATEGORY_AI = "ai"
CATEGORY_BLOCKCHAIN = "blockchain"

# --- Server-credential fallback policy -------------------------------------
#
# Whether this deployment's own credential may serve a user who has not
# supplied one. This is a licensing question for data providers and a cost
# question for AI providers, so the two are distinguished by category, not by
# this flag alone.
#
#   FALLBACK_ALLOWED   Public-domain / permissively redistributable data, or
#                      inference where the only constraint is our own bill.
#   FALLBACK_BYO_ONLY  Commercial data whose free/starter terms forbid serving
#                      the data on to third parties. Using the deployment key
#                      here would make us the redistributor. Operators who have
#                      actually bought a redistribution tier can opt in per
#                      provider via settings.licensed_redistribution_providers.
#   FALLBACK_PROHIBITED Access itself breaches the provider's terms in a
#                      commercial product, with or without a key.
FALLBACK_ALLOWED = "allowed"
FALLBACK_BYO_ONLY = "byo_only"
FALLBACK_PROHIBITED = "prohibited"

PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    # --- Market data (equities / fundamentals / economics) ---
    "alpha_vantage": {
        "label": "Alpha Vantage",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "alpha_vantage_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "polygon": {
        "label": "Polygon.io",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "polygon_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "fmp": {
        "label": "Financial Modeling Prep",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "fmp_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "fred": {
        "label": "FRED (St. Louis Fed)",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "fred_api_key",
        "key_required": False,  # works without a key; higher limits with one
        "fallback": FALLBACK_ALLOWED,
    },
    "finnhub": {
        "label": "Finnhub",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "finnhub_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "twelve_data": {
        "label": "Twelve Data",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "twelve_data_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "trading_economics": {
        "label": "Trading Economics",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "trading_economics_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "quandl": {
        # Now Nasdaq Data Link; the quandl key/alias still works for backward compat.
        "label": "Quandl (Nasdaq Data Link)",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "quandl_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "world_bank": {
        # Public-domain open data. No key exists, so settings_field is empty and
        # the resolver returns None — the provider registers keyless, which is
        # correct for a free, unrestricted source.
        "label": "World Bank Open Data",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "sec_edgar": {
        # v2: keyless public source v1 omitted. SEC EDGAR filings / full-text
        # search: public-domain regulatory data, no key, redistributable.
        # Adapters still need its redistribution class.
        "label": "SEC EDGAR",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "frankfurter": {
        # v2: keyless public source v1 omitted. ECB reference exchange rates via
        # the Frankfurter API: free, no key, no redistribution restriction.
        "label": "Frankfurter (ECB exchange rates)",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    # --- Crypto ---
    "coingecko": {
        "label": "CoinGecko",
        "category": CATEGORY_CRYPTO,
        "settings_field": "coingecko_api_key",
        "key_required": False,  # demo tier works; paid for commercial limits
        "fallback": FALLBACK_BYO_ONLY,
    },
    "binance": {
        "label": "Binance",
        "category": CATEGORY_CRYPTO,
        "settings_field": "binance_api_key",
        "key_required": False,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "coinmarketcap": {
        "label": "CoinMarketCap",
        "category": CATEGORY_CRYPTO,
        "settings_field": "coinmarketcap_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "messari": {
        "label": "Messari",
        "category": CATEGORY_CRYPTO,
        "settings_field": "messari_api_key",
        "key_required": False,  # some endpoints work keyless; key raises limits
        "fallback": FALLBACK_BYO_ONLY,
    },
    "defillama": {
        # v2: keyless public source v1 omitted. DefiLlama TVL: public, keyless,
        # permissively redistributable.
        "label": "DefiLlama",
        "category": CATEGORY_CRYPTO,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    # --- News ---
    "news_api": {
        "label": "NewsAPI",
        "category": CATEGORY_NEWS,
        "settings_field": "news_api_key",
        "key_required": True,
        "fallback": FALLBACK_BYO_ONLY,
    },
    # --- AI ---
    "fylun": {
        "label": "Fylun (unified AI gateway)",
        "category": CATEGORY_AI,
        "settings_field": "fylun_api_key",
        "key_required": False,  # one key covers all AI; replaces deepseek/glm/openai
        "fallback": FALLBACK_ALLOWED,
    },
    "deepseek": {
        "label": "DeepSeek (direct)",
        "category": CATEGORY_AI,
        "settings_field": "deepseek_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "glm": {
        "label": "GLM / Zhipu (direct)",
        "category": CATEGORY_AI,
        "settings_field": "glm_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "openai": {
        "label": "OpenAI (direct)",
        "category": CATEGORY_AI,
        "settings_field": "openai_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "anthropic": {
        "label": "Anthropic (direct)",
        "category": CATEGORY_AI,
        "settings_field": "anthropic_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "groq": {
        "label": "Groq",
        "category": CATEGORY_AI,
        "settings_field": "groq_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "xai": {
        "label": "xAI (Grok)",
        "category": CATEGORY_AI,
        "settings_field": "xai_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    # --- Keyless / prohibited ---
    "yahoo": {
        # Listed so the fallback policy can see it, NOT because it takes a key.
        # yfinance is an unofficial scraper of Yahoo Finance endpoints; Yahoo's
        # terms restrict use to personal, non-commercial purposes. That applies
        # with or without a credential, so no key setting can make it
        # compliant. It is currently the DEFAULT provider for stock historical
        # and commodity data and the whole fallback tier, so it cannot simply be
        # switched off — removing it is its own piece of work.
        "label": "Yahoo Finance (yfinance)",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_PROHIBITED,
    },
    # --- Blockchain ---
    "alchemy": {
        "label": "Alchemy",
        "category": CATEGORY_BLOCKCHAIN,
        "settings_field": "alchemy_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "etherscan": {
        "label": "Etherscan",
        "category": CATEGORY_BLOCKCHAIN,
        "settings_field": "etherscan_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
}


def redistribution_for(provider_key: str, licensed=()) -> str:
    """Redistribution class a claim from ``provider_key`` must carry.

    Returns one of the three ``redistribution`` enum values defined in
    migrations/001_core_schema.sql: ``allowed``, ``byo_only`` or
    ``prohibited``. Callers write this straight onto a claim's
    ``redistributable`` column.

    An unknown ``provider_key`` raises rather than defaulting. v1's catalog
    defaulted unvetted sources to ``byo_only``; v2 cannot, because an
    unclassified source must never be silently served — and a quiet
    ``byo_only`` still lets a private claim into the store under a user's
    audience. The lookup is the single point at which "has a human vetted this
    provider's licence?" gets a yes, so it must fail loud when the answer is no.

    ``licensed`` is the operator's declared set of providers for which they
    hold a redistribution licence (settings.licensed_redistribution_providers,
    split on comma by the resolver). A ``byo_only`` provider named in it is
    promoted to ``allowed``. A ``prohibited`` provider is never promotable: its
    terms bind regardless of what the operator has bought.
    """
    entry = PROVIDER_CATALOG.get(provider_key)
    if entry is None:
        raise KeyError(provider_key)
    fallback = entry["fallback"]
    if fallback == FALLBACK_PROHIBITED:
        return FALLBACK_PROHIBITED
    if fallback == FALLBACK_ALLOWED:
        return FALLBACK_ALLOWED
    if provider_key in set(licensed or ()):
        return FALLBACK_ALLOWED
    return FALLBACK_BYO_ONLY
