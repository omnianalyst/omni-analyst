from omni.api.settings import (
    _body_contains_secrets,
    _provider_catalog_payload,
    _sanitized_venues,
    _venue_catalog_payload,
)


def test_provider_catalog_reports_configuration_without_returning_keys(monkeypatch):
    monkeypatch.setattr("omni.api.settings.settings.fred_api_key", "secret-value")

    fred = next(entry for entry in _provider_catalog_payload() if entry["key"] == "fred")

    assert fred["configured"] is True
    assert "secret-value" not in str(fred)


def test_venue_payload_never_returns_saved_credentials():
    saved = {
        "venues": {
            "questrade": {
                "enabled": True,
                "credentials": {"refresh_token": "secret-value"},
            }
        }
    }

    catalog = _venue_catalog_payload(saved)
    sanitized = _sanitized_venues(saved)

    questrade = next(entry for entry in catalog if entry["key"] == "questrade")
    assert questrade["configured"] is True
    assert questrade["configuration_source"] == "legacy"
    assert "secret-value" not in str(catalog)
    assert "secret-value" not in str(sanitized)
    assert "credentials" not in sanitized["questrade"]


def test_secret_bodies_are_distinguished_from_non_secret_controls():
    assert _body_contains_secrets({"providers": {"fred_api_key": "secret"}})
    assert _body_contains_secrets(
        {"venues": {"ibkr": {"credentials": {"password": "secret"}}}}
    )
    assert not _body_contains_secrets({"venues": {"ibkr": {"enabled": False}}})
