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
    WalletCredentials,
    trading_credentials,
    wallet_credentials,
)

ADDRESS = "0x" + "a1" * 20
PRIVATE_KEY = "0x" + "b2" * 32


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


class TestWalletCredentials:
    """A decentralised venue authenticates with a wallet, not a key pair.

    The interesting case is not the happy path. Both values are hex strings of
    similar shape, so swapping them is a mistake that gets made -- and the
    consequence is not only a rejected order but a private key sitting in the
    field that everything treats as public.
    """

    def test_a_complete_wallet_is_accepted(self):
        creds = WalletCredentials(
            venue="hyperliquid", wallet_address=ADDRESS, private_key=PRIVATE_KEY
        )
        assert creds.ccxt_options() == {
            "walletAddress": ADDRESS,
            "privateKey": PRIVATE_KEY,
        }

    def test_an_address_with_no_key_is_refused(self):
        with pytest.raises(ValueError, match="both a wallet address and a private key"):
            WalletCredentials(
                venue="hyperliquid", wallet_address=ADDRESS, private_key=""
            )

    def test_a_key_with_no_address_is_refused(self):
        with pytest.raises(ValueError, match="both a wallet address and a private key"):
            WalletCredentials(
                venue="hyperliquid", wallet_address="", private_key=PRIVATE_KEY
            )

    def test_swapped_values_are_named_as_swapped(self):
        # Not "malformed address": the operator needs to be told the two fields
        # are the wrong way round, because the fix is to swap them and the
        # generic message sends them looking for a bad value instead.
        with pytest.raises(ValueError, match="look swapped"):
            WalletCredentials(
                venue="hyperliquid",
                wallet_address=PRIVATE_KEY,
                private_key=ADDRESS,
            )

    def test_a_truncated_address_is_refused(self):
        with pytest.raises(ValueError, match="40 hex characters"):
            WalletCredentials(
                venue="hyperliquid",
                wallet_address="0xa1a1",
                private_key=PRIVATE_KEY,
            )

    def test_a_truncated_key_is_refused(self):
        with pytest.raises(ValueError, match="64 hex characters"):
            WalletCredentials(
                venue="hyperliquid", wallet_address=ADDRESS, private_key="0xb2b2"
            )

    def test_a_key_without_the_hex_prefix_is_accepted(self):
        # Wallets export it both ways and neither is wrong.
        creds = WalletCredentials(
            venue="hyperliquid", wallet_address=ADDRESS, private_key="b2" * 32
        )
        assert creds.private_key == "b2" * 32

    def test_the_private_key_is_not_in_the_repr(self):
        # A frozen dataclass prints every field, and this object appears in
        # tracebacks and log lines. The key must not travel with it.
        creds = WalletCredentials(
            venue="hyperliquid", wallet_address=ADDRESS, private_key=PRIVATE_KEY
        )
        assert PRIVATE_KEY not in repr(creds)
        assert ADDRESS in repr(creds)


class TestTheSecretDoesNotTravelInTheRepr:
    def test_the_api_secret_is_not_in_the_repr(self):
        creds = TradingCredentials(venue="binance", api_key="k", api_secret="topsecret")
        assert "topsecret" not in repr(creds)
        assert "k" in repr(creds)


class TestReadingAWalletFromSettings:
    def test_absent_is_a_state_and_names_the_agent_wallet(self):
        # The moment somebody is being told to paste a key is the moment to say
        # which kind of key it must be.
        with pytest.raises(CredentialsMissing, match="agent wallet"):
            wallet_credentials(_Settings(), "hyperliquid")

    def test_a_half_configured_wallet_is_a_mistake_not_an_absence(self):
        settings = _Settings(
            hyperliquid_wallet_address=ADDRESS, hyperliquid_private_key=""
        )
        with pytest.raises(ValueError, match="both a wallet address"):
            wallet_credentials(settings, "hyperliquid")

    def test_a_configured_wallet_is_returned(self):
        settings = _Settings(
            hyperliquid_wallet_address=ADDRESS, hyperliquid_private_key=PRIVATE_KEY
        )
        assert wallet_credentials(settings, "hyperliquid") == WalletCredentials(
            venue="hyperliquid", wallet_address=ADDRESS, private_key=PRIVATE_KEY
        )
