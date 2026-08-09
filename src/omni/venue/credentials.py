"""Trading credentials, and the refusal to be half-configured.

A data key raises a rate limit. A trading key can empty an account. This module
exists so the difference is a type rather than a convention, and so the two
failure modes that would otherwise be found by a rejected order are found at
construction instead:

- **A key with no secret.** ccxt signs every private request with the secret, so
  a key alone authenticates nothing. Every order comes back rejected, which
  reads as an exchange problem rather than a configuration one, and for a
  delta-neutral pair it means the first leg is refused and the strategy looks
  broken rather than unconfigured.
- **A secret with no key.** Same outcome, and more alarming: a secret sitting in
  an environment it is not being used from is a secret with no reason to be
  there.

**Nothing here enables trading.** `CCXTVenue` defaults to `READ_ONLY` and stays
there until a caller passes `TradingMode.LIVE` explicitly; credentials being
present is necessary and never sufficient. The two are separate switches on
purpose -- a deployment can hold live credentials and still not trade, which is
the state a paper run against real market data wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEX64 = re.compile(r"\A(0x)?[0-9a-fA-F]{64}\Z")
_ADDRESS = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True)
class TradingCredentials:
    """A complete, usable pair. Cannot be constructed incomplete."""

    venue: str
    api_key: str
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.venue.strip():
            raise ValueError("venue must be named")
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ValueError(
                f"{self.venue} trading credentials must carry both a key and a "
                f"secret; ccxt signs every private request with the secret, so "
                f"a key alone authenticates nothing and every order is rejected"
            )

    def ccxt_options(self) -> dict[str, str]:
        return {"apiKey": self.api_key, "secret": self.api_secret}


@dataclass(frozen=True)
class WalletCredentials:
    """What a decentralised venue authenticates with, which is not a key pair.

    Hyperliquid reports `requiredCredentials = {privateKey, walletAddress}`, so
    `TradingCredentials` cannot express it. The difference is not cosmetic: the
    advice for a CEX key is to disable withdrawals on it, and **a raw private
    key cannot have withdrawals disabled, because it is the wallet.** The
    equivalent control is an *agent wallet* -- approved to trade on behalf of an
    account and unable to move funds out of it -- which is the only form this
    should ever hold. `CredentialsMissing` says so, because the moment somebody
    is pasting a key is the moment the distinction is worth stating.

    Both values are hex, which makes swapping them a mistake that gets made.
    Left undetected it authenticates nothing and reads as an exchange problem;
    worse, it puts a private key in the field that gets logged as public.
    """

    venue: str
    wallet_address: str
    private_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.venue.strip():
            raise ValueError("venue must be named")
        address = self.wallet_address.strip()
        key = self.private_key.strip()
        if not address or not key:
            raise ValueError(
                f"{self.venue} wallet credentials must carry both a wallet "
                f"address and a private key; one without the other signs "
                f"nothing and every order is rejected"
            )
        if _HEX64.match(address) and _ADDRESS.match(key):
            raise ValueError(
                f"{self.venue} wallet address and private key look swapped: the "
                f"address field holds 64 hex characters and the key field holds "
                f"an address. Left as is this authenticates nothing, and it puts "
                f"a private key in the field that is logged as public"
            )
        if not _ADDRESS.match(address):
            raise ValueError(
                f"{self.venue} wallet address must be 0x followed by 40 hex "
                f"characters, got {len(address)} characters"
            )
        if not _HEX64.match(key):
            raise ValueError(
                f"{self.venue} private key must be 64 hex characters, "
                f"optionally 0x-prefixed, got {len(key)}"
            )

    def ccxt_options(self) -> dict[str, str]:
        return {
            "walletAddress": self.wallet_address,
            "privateKey": self.private_key,
        }


class CredentialsMissing(Exception):
    """No trading credentials are configured for this venue.

    Distinct from a malformed pair, and distinct from a refusal to trade. This
    is the ordinary state of a system that has not been given keys, and a
    caller may legitimately continue read-only.
    """


def trading_credentials(settings, venue: str) -> TradingCredentials:
    """The configured pair for a venue, or `CredentialsMissing`.

    Raises rather than returning `None` because the alternative is an optional
    that every call site must remember to check, and the one that forgets builds
    a venue with `apiKey=None` and discovers it at the first order. A half pair
    raises too, with a different message: absent is a state, half-present is a
    mistake.
    """
    key = getattr(settings, f"{venue}_trade_api_key", "") or ""
    secret = getattr(settings, f"{venue}_trade_api_secret", "") or ""
    if not key.strip() and not secret.strip():
        raise CredentialsMissing(
            f"no trading credentials configured for {venue}. Set "
            f"{venue.upper()}_TRADE_API_KEY and {venue.upper()}_TRADE_API_SECRET "
            f"to a key created for trading only, with withdrawals disabled"
        )
    # A half pair falls through to TradingCredentials, which names which half is
    # missing rather than reporting "not configured" for a key that is right
    # there in the environment.
    return TradingCredentials(venue=venue, api_key=key, api_secret=secret)


def wallet_credentials(settings, venue: str) -> WalletCredentials:
    """The configured wallet for a venue, or `CredentialsMissing`.

    Same contract as `trading_credentials`: absent is a state, half-present is a
    mistake, and neither enables trading on its own.
    """
    address = getattr(settings, f"{venue}_wallet_address", "") or ""
    key = getattr(settings, f"{venue}_private_key", "") or ""
    if not address.strip() and not key.strip():
        raise CredentialsMissing(
            f"no wallet credentials configured for {venue}. Set "
            f"{venue.upper()}_WALLET_ADDRESS and {venue.upper()}_PRIVATE_KEY. "
            f"Use an agent wallet approved for trading, never an account key: "
            f"a private key cannot have withdrawals disabled because it is the "
            f"wallet"
        )
    return WalletCredentials(venue=venue, wallet_address=address, private_key=key)
