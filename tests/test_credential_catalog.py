"""Tests for the v2 credential catalog and the redistribution lookup.

The behaviour under test is a licensing boundary: which ``redistribution`` enum
value a claim from a given source must carry, and therefore whether it may
enter the shared coverage network or only the fetching user's private view
(``allowed`` -> shared, ``byo_only`` -> audience-scoped, ``prohibited`` -> not
writable at all per migrations/001_core_schema.sql).

Unknown sources must raise, not default. v1's catalog defaulted unvetted
sources to ``byo_only``; v2 rejects them loud, because a quiet default is how
unvetted data enters the store.

The ``wired`` field (TestWiredField, TestWiredProvidersHaveConfigFields) is the
implementation-honesty layer: the catalog lists ~27 providers for licensing
classification, but only 6 have adapters today. ``wired`` tells the Settings UI
which entries are real integrations vs forward-looking placeholders.
"""
import pytest

from omni.config import Settings
from omni.credentials.catalog import (
    _WIRED_PROVIDERS,
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

    def test_okx_is_byo_only_even_though_public_market_data_is_keyless(self):
        assert PROVIDER_CATALOG["okx"]["key_required"] is False
        assert redistribution_for("okx") == FALLBACK_BYO_ONLY

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
            redistribution_for("coingecko", licensed=["polygon"])
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
            "polygon",
            "coingecko",
            "binance",
            "coinbase",
            "kraken",
            "bybit",
            "okx",
            "hyperliquid",
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


# -- Wired vs catalog-only ----------------------------------------------------
#
# The catalog lists providers that have no adapter yet (AI providers, extra
# market-data feeds) because the licensing resolver must classify them. The
# `wired` field separates "classified for licensing" from "has a working
# integration." The Settings UI renders only wired providers; a reader who sees
# an unwired entry knows it is a placeholder, not a feature.


class TestWiredField:
    def test_every_entry_has_a_wired_flag(self):
        for key, entry in PROVIDER_CATALOG.items():
            assert "wired" in entry, f"{key} missing 'wired' field"

    def test_ai_providers_are_not_wired(self):
        for key, entry in PROVIDER_CATALOG.items():
            if entry["category"] == "ai":
                assert entry["wired"] is False, (
                    f"{key} is marked wired but no LLM client exists"
                )

    def test_wired_providers_are_the_expected_six(self):
        for pk in ("fred", "polygon", "sec_edgar", "coingecko", "etherscan", "rss"):
            assert PROVIDER_CATALOG[pk]["wired"] is True


class TestWiredProvidersHaveConfigFields:
    """A wired provider's settings_field must exist in Settings, or it must be
    keyless. A wired entry that references a nonexistent settings field is the
    exact dishonesty this test prevents."""

    def test_wired_providers_have_real_settings_fields(self):
        field_names = set(Settings.model_fields.keys())
        for pk in _WIRED_PROVIDERS:
            entry = PROVIDER_CATALOG[pk]
            sf = entry["settings_field"]
            if not sf:
                assert not entry["key_required"], (
                    f"{pk} is wired and key_required but has no settings_field"
                )
                continue
            assert sf in field_names, (
                f"{pk} is wired but settings_field {sf!r} does not exist in "
                f"Settings. Add it to config.py or mark the provider unwired."
            )

    def test_unwired_providers_settings_fields_dont_exist(self):
        field_names = set(Settings.model_fields.keys())
        for pk, entry in PROVIDER_CATALOG.items():
            if entry["wired"]:
                continue
            sf = entry["settings_field"]
            if sf:
                assert sf not in field_names, (
                    f"{pk} is unwired but its settings_field {sf!r} exists in "
                    f"Settings -- the operator can set a key that does nothing"
                )


class TestWiredMatchesBuiltinRegistry:
    """The wired set must match the adapters actually registered in builtin.py.
    If someone adds an adapter but forgets to add the provider to
    _WIRED_PROVIDERS, this test fails."""

    def test_every_builtin_adapter_provider_is_marked_wired(self):
        from omni.capability.builtin import build_builtin_registry

        registry = build_builtin_registry(settings=Settings())
        builtin_providers = set()
        for name in registry._by_name:
            cap = registry.get(name)
            if cap.provider_key:
                builtin_providers.add(cap.provider_key)

        for pk in builtin_providers:
            assert pk in _WIRED_PROVIDERS, (
                f"{pk} has an adapter in builtin.py but is not in "
                f"_WIRED_PROVIDERS. Add it to credentials/catalog.py."
            )
