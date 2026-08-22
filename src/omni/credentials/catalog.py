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

# --- Wired providers ---------------------------------------------------------
#
# Every entry here is real: either an adapter exists in capability/builtin.py
# (wired) or the source is live through another path and needs its licence
# class on file (yahoo: prohibited, binding on the yfinance research path;
# world_bank, hyperliquid: keyless live sources). Catalog entries for
# never-built adapters were removed 2026-08-21 -- a Settings row or licence
# entry for a provider that cannot fetch anything is a promise the code does
# not keep. When you add an adapter to builtin.py, add the provider_key here
# and the settings field to config.py; test_wired_providers_have_adapters
# catches the drift.
_WIRED_PROVIDERS = frozenset({
    "fred",
    "sec_edgar",
    "polygon",
    "coingecko",
    "etherscan",
    "rss",
    # Wired by the Phase 1 crypto wave. Each has an adapter in `ingest/` that
    # the planner can reach through `capability/builtin.py`.
    "binance",
    "defillama",
    "coinbase",
    "kraken",
    "bybit",
    "okx",
})

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
    "polygon": {
        "label": "Polygon.io",
        "category": CATEGORY_MARKET_DATA,
        "settings_field": "polygon_api_key",
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
    # --- Exchange venues reached through ccxt ---
    #
    # One entry per venue rather than a single "ccxt" entry, because ccxt is a
    # client library and not a licensor: redistribution is governed by the venue
    # whose data was fetched. Collapsing them would apply one venue's terms to
    # all of them, which is the licence rule AGENTS.md calls the one most likely
    # to be broken by accident.
    #
    # All byo_only. Public and keyless is not the same as redistributable -- a
    # venue's terms restrict serving its market data on to third parties whether
    # or not a key was needed to fetch it.
    "coinbase": {
        "label": "Coinbase",
        "category": CATEGORY_CRYPTO,
        "settings_field": "coinbase_api_key",
        "key_required": False,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "kraken": {
        "label": "Kraken",
        "category": CATEGORY_CRYPTO,
        "settings_field": "kraken_api_key",
        "key_required": False,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "bybit": {
        "label": "Bybit",
        "category": CATEGORY_CRYPTO,
        "settings_field": "bybit_api_key",
        "key_required": False,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "okx": {
        # Its market-data endpoints are public and keyless, but the venue terms
        # still restrict serving that data on to third parties.
        "label": "OKX",
        "category": CATEGORY_CRYPTO,
        "settings_field": "okx_api_key",
        "key_required": False,
        "fallback": FALLBACK_BYO_ONLY,
    },
    "hyperliquid": {
        # Reads need no credential at all -- market data is served from a public
        # endpoint, and trading authenticates with a wallet rather than an API
        # key, so `settings_field` names no key to hold. byo_only regardless,
        # for the reason stated above: keyless is not redistributable.
        "label": "Hyperliquid",
        "category": CATEGORY_CRYPTO,
        "settings_field": "",
        "key_required": False,
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
    "rss": {
        # Public RSS/Atom feeds. No credential exists. The adapter stores only
        # the headline, the link and the feed name -- a citation, not a copy
        # of the article -- so nothing the provider's copyright controls is
        # republished. That makes it reference, not redistribution, the same
        # boundary the byo_only rule enforces for licensed data.
        "label": "RSS / Atom feeds",
        "category": CATEGORY_NEWS,
        "settings_field": "",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    # --- AI ---
    # --- AI (the Polymarket tier) ---
    # Only the two adapters that exist (polymarket/glm_adapter.py,
    # anthropic_adapter.py). The model selects and phrases; the protocol
    # forbids it producing a figure. Never-adaptered AI entries were removed
    # 2026-08-21 along with the rest of the aspirational catalog.
    "glm": {
        "label": "GLM / Zhipu (Polymarket phrasing)",
        "category": CATEGORY_AI,
        "settings_field": "glm_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
    "anthropic": {
        "label": "Anthropic (Polymarket phrasing)",
        "category": CATEGORY_AI,
        "settings_field": "anthropic_api_key",
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
    "etherscan": {
        "label": "Etherscan",
        "category": CATEGORY_BLOCKCHAIN,
        "settings_field": "etherscan_api_key",
        "key_required": False,
        "fallback": FALLBACK_ALLOWED,
    },
}

# Annotate each entry with its implementation status. `redistribution_for`
# works for every entry regardless; `wired` tells the Settings UI and any
# reader whether an adapter and config field actually exist today.
for _pk, _entry in PROVIDER_CATALOG.items():
    _entry["wired"] = _pk in _WIRED_PROVIDERS


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
