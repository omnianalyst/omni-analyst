"""v2's own capabilities — the ones that actually run today.

`from_census` produces 428 descriptors from v1's surface, none of them bound to
an implementation; that file is an inventory and a migration backlog. This one
is the opposite: a small set, every entry executable, every entry tested.

Keeping them apart matters. A planner asking "what can I actually do right now"
must not have to filter a catalogue of things that merely exist.
"""

from __future__ import annotations

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.config import settings as default_settings
from omni.credentials.catalog import redistribution_for


def _byo(provider_key: str) -> bool:
    """Whether this provider's output can only ever be private coverage."""
    return redistribution_for(provider_key) != "allowed"


def _adapter(
    name: str,
    description: str,
    *,
    provider_key: str,
    produces: tuple[str, ...],
    factory,
    cost: float = 1.0,
    entity_kinds: tuple[str, ...] = (),
    credentials: dict | None = None,
) -> Capability:
    bound = dict(credentials or {})

    async def call(key: str, **kwargs):
        # Caller kwargs win, so a test can still inject a fetch_fn and reach
        # no network at all.
        return await factory(**{**bound, **kwargs}).fetch(key)

    return Capability(
        name=name,
        description=description,
        produces=produces,
        entity_kinds=entity_kinds,
        provider_key=provider_key,
        source=provider_key,
        touches_byo=_byo(provider_key),
        cost=cost,
        maturity=Maturity.WIRED,
        callability=Callability.YES,
        origin=f"omni.ingest.{name.split('.')[0]}",
        call=call,
    )


def build_builtin_registry(settings=None) -> Registry:
    from omni.ingest.coingecko import CoinGeckoAdapter
    from omni.ingest.defillama import DefiLlamaAdapter
    from omni.ingest.derivatives import DerivativesAdapter
    from omni.ingest.edgar import EdgarAdapter
    from omni.ingest.entity_news import EntityNewsAdapter
    from omni.ingest.exchanges import CCXTAdapter
    from omni.ingest.filings import FilingsAdapter
    from omni.ingest.fred import FredAdapter
    from omni.ingest.macro_perception import MacroPerceptionAdapter
    from omni.ingest.microstructure import MicrostructureAdapter
    from omni.ingest.news import NewsAdapter
    from omni.ingest.onchain import OnChainAdapter
    from omni.ingest.polygon import PolygonAdapter
    from omni.ingest.positioning import PositioningAdapter

    cfg = settings if settings is not None else default_settings
    registry = Registry()

    for cap in (
        _adapter(
            "fred.series",
            "Point-in-time macro series from FRED/ALFRED, every vintage, so a "
            "backtest sees the first print rather than a later revision.",
            provider_key="fred",
            produces=("macro_series_point",),
            factory=FredAdapter,
            credentials={"api_key": cfg.fred_api_key} if cfg.fred_api_key else None,
        ),
        _adapter(
            "fred.perception",
            "Market-implied sentiment and stress indices published by FRED: "
            "consumer sentiment, implied volatility, credit spreads, term "
            "spread. The only perception coverage that is redistributable.",
            provider_key="fred",
            produces=("perception_macro",),
            factory=MacroPerceptionAdapter,
            credentials={"api_key": cfg.fred_api_key} if cfg.fred_api_key else None,
        ),
        _adapter(
            "edgar.companyfacts",
            "As-reported fundamentals from SEC XBRL. A restatement is a second "
            "claim sharing the period, not an overwrite.",
            provider_key="sec_edgar",
            produces=("fundamental_metric",),
            entity_kinds=("company",),
            factory=EdgarAdapter,
            credentials={"user_agent": cfg.sec_user_agent} if cfg.sec_user_agent else None,
            cost=2.0,
        ),
        _adapter(
            "onchain.activity",
            "Exchange flows, protocol TVL and supply from Etherscan and "
            "DefiLlama. Crypto's redistributable layer, the counterpart to "
            "EDGAR for equities.",
            provider_key="etherscan",
            produces=("onchain_flow", "onchain_tvl", "onchain_supply"),
            entity_kinds=("crypto_asset",),
            factory=OnChainAdapter,
            credentials={"api_key": cfg.etherscan_api_key} if cfg.etherscan_api_key else None,
        ),
        _adapter(
            "polygon.aggregates",
            "Equity price bars. Licensed per operator, so output is private to "
            "the credential owner.",
            provider_key="polygon",
            produces=("price_snapshot",),
            entity_kinds=("company",),
            factory=PolygonAdapter,
            credentials={"api_key": cfg.polygon_api_key} if cfg.polygon_api_key else None,
        ),
        _adapter(
            "coingecko.market_chart",
            "Crypto price, market cap and volume. Licensed per operator.",
            provider_key="coingecko",
            produces=("price_snapshot",),
            entity_kinds=("crypto_asset",),
            factory=CoinGeckoAdapter,
            credentials={"api_key": cfg.coingecko_api_key} if cfg.coingecko_api_key else None,
        ),
        _adapter(
            "derivatives.binance",
            "Perpetual funding rate, open interest and liquidations. The crypto "
            "edge RESEARCH.md rates best risk-adjusted for a solo operator, and "
            "the three claim types the carry, basis and OI producers consume. "
            "Public endpoints, but venue terms restrict redistribution, so "
            "private to the credential owner.",
            provider_key="binance",
            produces=("funding_rate", "open_interest", "liquidation_event"),
            entity_kinds=("crypto_asset",),
            factory=DerivativesAdapter,
        ),
        _adapter(
            "defillama.fundamentals",
            "Protocol fees, revenue, stablecoin supply and chain TVL. Crypto's "
            "redistributable fundamentals tier -- the EDGAR counterpart, and "
            "the only crypto source here whose output accumulates as shared "
            "network coverage rather than being pinned to one operator.",
            provider_key="defillama",
            produces=(
                "protocol_fees",
                "protocol_revenue",
                "stablecoin_supply",
                "chain_tvl",
            ),
            entity_kinds=("protocol", "chain"),
            factory=DefiLlamaAdapter,
        ),
    ):
        registry.add(cap)

    # ccxt venues are registered per venue rather than once, because
    # `provider_key` carries the licence class and ccxt is a client library, not
    # a licensor. One registration would apply one venue's terms to all of them.
    for venue in ("binance", "coinbase", "kraken", "bybit", "okx"):
        registry.add(_adapter(
            f"exchanges.{venue}",
            f"Price bars from {venue} via ccxt. Multiple venues covering one "
            f"asset is what the cross-venue basis producer needs; a single "
            f"aggregate price cannot express where a print happened.",
            provider_key=venue,
            produces=("price_snapshot",),
            entity_kinds=("crypto_asset",),
            factory=CCXTAdapter,
            credentials={"venue": venue},
        ))
        registry.add(_adapter(
            f"microstructure.{venue}",
            f"Order book and trade tape from {venue}. Supplies "
            f"venue/costs.py with a measured spread instead of a configured "
            f"constant, which is what the router accepts or rejects strategies "
            f"on.",
            provider_key=venue,
            produces=("orderbook_snapshot", "trade_tape"),
            entity_kinds=("crypto_asset",),
            factory=MicrostructureAdapter,
            credentials={"venue": venue},
            cost=2.0,
        ))

    registry.add(_adapter(
        "rss.entity_sentiment",
        "Company-scoped perception: headlines attributed to a ticker and "
        "scored per day. The second side the divergence finding needs — "
        "market-wide perception cannot be compared against one company's "
        "fundamentals.",
        provider_key="rss",
        produces=("perception_news",),
        entity_kinds=("company",),
        factory=EntityNewsAdapter,
    ))
    registry.add(_adapter(
        "edgar.filings",
        "What a company has filed and when. Distinct from companyfacts, which "
        "covers what the filings said: absence is informative here, and only a "
        "record of filing events distinguishes a company that has not filed an "
        "8-K in two years from one that files monthly.",
        provider_key="sec_edgar",
        produces=("filing_event",),
        entity_kinds=("company",),
        factory=FilingsAdapter,
        credentials={"user_agent": cfg.sec_user_agent} if cfg.sec_user_agent else None,
    ))
    registry.add(_adapter(
        "rss.headlines",
        "Market headlines from public financial feeds. Titles and links only, "
        "never article text -- a headline and a URL are references, "
        "reproducing the body is republishing someone else's work.",
        provider_key="rss",
        produces=("news_event",),
        factory=NewsAdapter,
    ))
    registry.add(_adapter(
        "polygon.positioning",
        "What market participants are doing rather than saying: options skew, "
        "short interest and flow. Licensed per operator, so private-tier.",
        provider_key="polygon",
        produces=("perception_positioning",),
        entity_kinds=("company",),
        factory=PositioningAdapter,
        credentials={"api_key": cfg.polygon_api_key} if cfg.polygon_api_key else None,
    ))
    registry.add(_manipulation_capability())
    return registry


def _manipulation_capability() -> Capability:
    async def call(ohlcv):
        from omni.detect.manipulation import ManipulationAnalyzer

        return ManipulationAnalyzer().analyze(ohlcv)

    return Capability(
        name="detect.manipulation",
        description=(
            "Volume-anomaly, wash-trading and pump-and-dump detection over an "
            "OHLCV window. Confidences are percentile ranks against the "
            "symbol's own history, never fixed thresholds, and patterns it "
            "cannot compute report unsupported rather than a result."
        ),
        consumes=("price_snapshot",),
        produces=("manipulation_signal",),
        # Derived from price. Whether the result is shareable depends on the
        # licence of the bars it consumed, which the claim writer resolves from
        # the input claims -- not something this descriptor can know.
        touches_byo=True,
        provenance=(
            "Necessary-conditions signal computed without order-flow data; "
            "it cannot distinguish manipulation from legitimate volume shocks."
        ),
        cost=0.1,
        maturity=Maturity.WIRED,
        callability=Callability.YES,
        origin="omni.detect.manipulation",
        call=call,
    )
