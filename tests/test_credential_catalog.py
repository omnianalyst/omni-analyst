"""Tests for the v2 credential catalog and the redistribution lookup.

The behaviour under test is a licensing boundary: which ``redistribution`` enum
value a claim from a given source must carry, and therefore whether it may
enter the shared coverage network or only the fetching user's private view
(``allowed`` -> shared, ``byo_only`` -> audience-scoped, ``prohibited`` -> not
writable at all per migrations/001_core_schema.sql).

Unknown sources must raise, not default. v1's catalog defaulted unvetted
sources to ``byo_only``; v2 rejects them loud, because a quiet default is how
unvetted data enters the store.
"""
import pytest

from omni.credentials.catalog import (
    FALLBACK_ALLOWED,
    FALLBACK_BYO_ONLY,
    FALLBACK_PROHIBITED,
    PROVIDER_CATALOG,
    redistribution_for,
)

# The three values migrations/001_core_schema.sql allows on the
# `redistributable` column. Catalog entries may carry only these.
SCHEMA_ENUM_VALUES = {FALLBACK_ALLOWED, FALLBACK_BYO_ONLY, FALLBACK_PROHIBITED}


class TestRedistributionFor:
    def test_fred_is_allowed(self):
        assert redistribution_for("fred") == FALLBACK_ALLOWED

    def test_polygon_is_byo_only(self):
        assert redistribution_for("polygon") == FALLBACK_BYO_ONLY

    def test_yahoo_is_prohibited(self):
        assert redistribution_for("yahoo") == FALLBACK_PROHIBITED

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            redistribution_for("some_unvetted_provider")

    def test_unknown_provider_never_defaults(self):
        # Distinct from v1, which returned byo_only for unknown keys. v2 must
        # reject every unvetted key, including the easy-to-miss ones.
        for bad in ("iex_cloud", "", "unknown", "FRED", "Polygon", "polygon "):
            with pytest.raises(KeyError):
                redistribution_for(bad)

    def test_byo_only_promoted_by_licence(self):
        assert redistribution_for("polygon", licensed=["polygon"]) == FALLBACK_ALLOWED

    def test_licence_promotes_only_the_named_provider(self):
        assert redistribution_for("polygon", licensed=["polygon"]) == FALLBACK_ALLOWED
        assert (
            redistribution_for("alpha_vantage", licensed=["polygon"])
            == FALLBACK_BYO_ONLY
        )

    def test_prohibited_is_never_promotable(self):
        assert (
            redistribution_for("yahoo", licensed=["yahoo"]) == FALLBACK_PROHIBITED
        )

    def test_licensed_accepts_iterables_and_defaults_to_byo_only(self):
        assert redistribution_for("polygon", licensed=("polygon",)) == FALLBACK_ALLOWED
        assert redistribution_for("polygon", licensed=()) == FALLBACK_BYO_ONLY
        assert redistribution_for("polygon") == FALLBACK_BYO_ONLY

    def test_allowed_provider_is_unchanged_by_licence(self):
        # An already-allowed provider is allowed regardless; licence is a
        # promotion from byo_only only.
        assert redistribution_for("fred", licensed=()) == FALLBACK_ALLOWED
        assert redistribution_for("fred", licensed=["fred"]) == FALLBACK_ALLOWED

    @pytest.mark.parametrize(
        "provider",
        [
            "alpha_vantage",
            "fmp",
            "finnhub",
            "twelve_data",
            "trading_economics",
            "quandl",
            "coingecko",
            "binance",
            "coinmarketcap",
            "messari",
            "news_api",
        ],
    )
    def test_commercial_providers_are_byo_only(self, provider):
        assert redistribution_for(provider) == FALLBACK_BYO_ONLY

    def test_every_catalog_lookup_returns_a_schema_enum(self):
        # Iterate the catalog; do not hardcode a provider list. Every value
        # returned must be one of the three redistribution enum values, or the
        # schema CHECK on `claim.redistributable` will reject the insert.
        assert PROVIDER_CATALOG, "catalog is empty — port failed"
        for provider_key in PROVIDER_CATALOG:
            value = redistribution_for(provider_key)
            assert value in SCHEMA_ENUM_VALUES, (
                f"{provider_key!r} returned {value!r}, not a schema enum value"
            )


class TestKeylessAdditions:
    """v2 adds the keyless public sources v1 omitted (its docstring said so)
    because adapters still need their redistribution class."""

    @pytest.mark.parametrize("provider", ["sec_edgar", "frankfurter", "defillama"])
    def test_keyless_public_source_is_allowed(self, provider):
        assert redistribution_for(provider) == FALLBACK_ALLOWED

    @pytest.mark.parametrize("provider", ["sec_edgar", "frankfurter", "defillama"])
    def test_keyless_public_source_registered_without_a_key(self, provider):
        entry = PROVIDER_CATALOG[provider]
        assert entry["key_required"] is False
        assert entry["settings_field"] == ""
        assert entry["fallback"] == FALLBACK_ALLOWED


class TestIexCloudDeliberatelyAbsent:
    def test_iex_cloud_has_no_entry(self):
        assert "iex_cloud" not in PROVIDER_CATALOG

    def test_iex_cloud_lookup_raises(self):
        with pytest.raises(KeyError):
            redistribution_for("iex_cloud")
