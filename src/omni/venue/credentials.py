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

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingCredentials:
    """A complete, usable pair. Cannot be constructed incomplete."""

    venue: str
    api_key: str
    api_secret: str

    def __post_init__(self) -> None:
        if not self.venue.strip():
            raise ValueError("venue must be named")
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ValueError(
                f"{self.venue} trading credentials must carry both a key and a "
                f"secret; ccxt signs every private request with the secret, so "
                f"a key alone authenticates nothing and every order is rejected"
            )


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
