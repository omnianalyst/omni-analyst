from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5434/omni_v2"
    debug: bool = False

    # Ingestion credentials, supplied by the operator of this deployment.
    # Which of these are set decides what the background can actually fetch;
    # an unset one produces an honest refusal naming itself, never a guess.
    fred_api_key: str = ""
    polygon_api_key: str = ""
    coingecko_api_key: str = ""
    etherscan_api_key: str = ""

    # Exchange venues. All optional: every adapter in this wave reads public
    # endpoints, and a key only raises the rate limit. An absent key is not an
    # Unavailable here -- it is the normal case.
    binance_api_key: str = ""
    coinbase_api_key: str = ""
    kraken_api_key: str = ""
    bybit_api_key: str = ""
    okx_api_key: str = ""

    # TRADING credentials, deliberately separate fields from the data keys
    # above -- not a tidiness preference, a security boundary.
    #
    # A data key raises a rate limit. A trading key can empty the account. They
    # are different powers and they should be different keys ON THE EXCHANGE
    # too: create a second API key for this, with withdrawals DISABLED and an
    # IP allowlist, and leave the read-only key read-only. Reusing one key for
    # both means every process that reads a price holds the power to trade.
    #
    # Both halves are required to trade: ccxt signs with the secret, so a key
    # without one authenticates nothing and every order is rejected. `venue.py`
    # refuses a half-configured pair rather than discovering it at the first
    # order.
    #
    # Empty is the correct default and means "cannot trade", which is the state
    # `CCXTVenue` is already in by default (TradingMode.READ_ONLY). Nothing here
    # enables trading on its own.
    binance_trade_api_key: str = ""
    binance_trade_api_secret: str = ""

    # Hyperliquid authenticates with a wallet, not a key pair. Use an AGENT
    # wallet: approved to trade on behalf of the account, unable to withdraw
    # from it. The advice above -- disable withdrawals on the key -- has no
    # equivalent for a raw private key, because a private key IS the wallet.
    hyperliquid_wallet_address: str = ""
    hyperliquid_private_key: str = ""

    # SEC requires a User-Agent identifying the operator, and rejects requests
    # without one. It is not a secret and not a key -- EDGAR is free.
    # Format: "Organisation contact@example.com".
    sec_user_agent: str = ""

    # HS256 signing key for auth tokens. Read os.environ FIRST (so tests that
    # setdefault the env still match their own tokens), then fall back to .env
    # here so local/dev and deploy without an explicit export still work.
    omni_jwt_secret: str = ""

    #: Providers for which this operator holds a redistribution licence, so
    #: their data may enter shared coverage. Comma-separated provider keys.
    licensed_redistribution_providers: str = ""

    #: The conviction gate's bar: a prediction is surfaced only if its
    #: confidence bucket has historically resolved right at least this often.
    #: Derived-threshold-friendly -- raising it surfaces only higher-conviction
    #: calls and silences weak methods; lowering it surfaces more (noisier).
    #: 0.6 is lenient (3-in-5); 0.7 is a defensible "high conviction" bar.
    target_hit_rate: float = Field(default=0.6, ge=0.0, le=1.0, allow_inf_nan=False)

    #: Auth token lifetime in seconds. 3600 (1h) is conservative; for a solo
    #: operator on a private deployment, a longer session (e.g. 604800 = 7d)
    #: is more usable. Env-configurable so it's tunable without code changes.
    token_expires_in: int = 3600

    @property
    def licensed(self) -> tuple[str, ...]:
        return tuple(
            p.strip() for p in self.licensed_redistribution_providers.split(",")
            if p.strip()
        )


settings = Settings()
