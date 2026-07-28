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

    #: Providers for which this operator holds a redistribution licence, so
    #: their data may enter shared coverage. Comma-separated provider keys.
    licensed_redistribution_providers: str = ""

    @property
    def licensed(self) -> tuple[str, ...]:
        return tuple(
            p.strip() for p in self.licensed_redistribution_providers.split(",")
            if p.strip()
        )


settings = Settings()
