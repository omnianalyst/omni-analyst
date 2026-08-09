"""Trading credentials: a security boundary, and a refusal to be half-configured.

A data key raises a rate limit; a trading key can empty an account. These are
separate config fields so that difference is a type rather than a convention,
and both failure modes are caught at construction rather than by a rejected
order -- which for a delta-neutral pair means the first leg is refused and the
strategy reads as broken rather than unconfigured.
"""

import pytest

from omni.venue.credentials import (
    CredentialsMissing,
    TradingCredentials,
    trading_credentials,
)


class _Settings:
    """Only the fields the accessor reads, so a test states its own world."""

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class TestACompletePairIsRequired:
    def test_a_key_with_no_secret_is_refused(self):
        # ccxt signs every private request with the secret, so a key alone
        # authenticates nothing and every order comes back rejected.
        with pytest.raises(ValueError, match="both a key and a secret"):
            TradingCredentials(venue="binance", api_key="k", api_secret="")

    def test_a_secret_with_no_key_is_refused(self):
        with pytest.raises(ValueError, match="both a key and a secret"):
            TradingCredentials(venue="binance", api_key="", api_secret="s")

    def test_whitespace_is_not_a_credential(self):
        # An env var set to an empty string arrives as whitespace often enough
        # that treating it as present would authenticate with nothing.
        with pytest.raises(ValueError, match="both a key and a secret"):
            TradingCredentials(venue="binance", api_key="  ", api_secret="s")

    def test_a_complete_pair_is_accepted(self):
        creds = TradingCredentials(venue="binance", api_key="k", api_secret="s")

        assert creds.venue == "binance"
        assert creds.api_key == "k"


class TestAbsentIsAStateAndHalfPresentIsAMistake:
    def test_nothing_configured_raises_credentials_missing(self):
        """The ordinary state of a system nobody has given keys to. A caller may
        legitimately continue read-only, which is why it is its own exception."""
        settings = _Settings(binance_trade_api_key="", binance_trade_api_secret="")

        with pytest.raises(CredentialsMissing, match="no trading credentials"):
            trading_credentials(settings, "binance")

    def test_a_half_pair_names_which_half_is_missing(self):
        # Not CredentialsMissing: reporting "not configured" for a key that is
        # right there in the environment sends the operator looking in the
        # wrong place.
        settings = _Settings(binance_trade_api_key="k", binance_trade_api_secret="")

        with pytest.raises(ValueError, match="both a key and a secret"):
            trading_credentials(settings, "binance")

    def test_a_missing_field_entirely_is_still_missing_not_an_attribute_error(self):
        # A venue with no configured fields at all must read as unconfigured
        # rather than crashing on getattr.
        with pytest.raises(CredentialsMissing):
            trading_credentials(_Settings(), "kraken")

    def test_a_complete_pair_is_returned(self):
        settings = _Settings(binance_trade_api_key="k", binance_trade_api_secret="s")

        creds = trading_credentials(settings, "binance")

        assert creds == TradingCredentials(venue="binance", api_key="k", api_secret="s")


class TestCredentialsAreNotPermissionToTrade:
    def test_the_data_key_is_a_different_field_from_the_trading_key(self):
        """The security boundary, asserted rather than documented.

        Reusing one key for both means every process that reads a price holds
        the power to trade. A data key configured alone must leave the venue
        unable to trade.
        """
        from omni.config import Settings

        fields = set(Settings.model_fields)

        assert "binance_api_key" in fields
        assert "binance_trade_api_key" in fields
        assert "binance_trade_api_secret" in fields

    def test_a_data_key_alone_does_not_configure_trading(self):
        settings = _Settings(
            binance_api_key="data-key",
            binance_trade_api_key="",
            binance_trade_api_secret="",
        )

        with pytest.raises(CredentialsMissing):
            trading_credentials(settings, "binance")
