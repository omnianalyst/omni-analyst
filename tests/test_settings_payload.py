from omni.api.settings import (
    _provider_catalog_payload,
    _sanitized_venues,
    _venue_catalog_payload,
    build_router,
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


def test_generic_settings_write_is_absent_and_secret_mutations_are_narrow():
    routes = {
        (info["method"], info["path"])
        for info in build_router(object()).get_handler_info()
    }

    assert ("post", "/settings") not in routes
    assert ("post", "/settings/venue/{venue_key}/credentials") in routes
    assert ("delete", "/settings/venue/{venue_key}/credentials") in routes
