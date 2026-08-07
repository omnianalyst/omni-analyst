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
    target_hit_rate: float = 0.6

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
