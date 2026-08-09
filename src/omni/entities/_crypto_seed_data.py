# Static reference data for the crypto-universe seed.
#
# These are NOT measurements and NOT provider-sourced coverage -- they are the
# slowly-changing reference sets that define what the system scans on the crypto
# side: the assets the autonomous loops demand coverage for, the chains they
# live on, the protocols that govern them, and the sectors that group them.
# Seeding identity is honest; every price, market cap and TVL is still fetched
# live by the fill loop. Nothing here is a fabricated claim.
#
# `coingecko_id` is the one field a wrong value silently corrupts forever -- a
# mistyped id fetches a *different asset's* price series and attributes it to
# this entity, the exact misattribution the coverage store exists to prevent.
# Every coingecko_id below is copied from the verified symbol->id map in
# `omni.ingest.coingecko.SYMBOL_TO_ID` (itself harvested from v1's reviewed
# `direct_mappings`). They are reproduced as independent literals here rather
# than imported, so the drift test (`test_crypto_seed.py`) can catch the two
# references diverging; an import would make that test tautological. Each
# asset's symbol is the uppercased SYMBOL_TO_ID key, which is also what the
# CoinGecko adapter resolves by, so every seeded asset is genuinely fillable.
#
# `contract_address` is None for any asset whose address could not be sourced
# with certainty. A native coin has no contract (it IS the chain's gas asset),
# and an ERC-20 address transcribed from memory risks a one-hex-digit typo that
# points at another contract forever. The few non-None values are the canonical
# Ethereum mainnet ERC-20 contracts that have been immutable since deployment
# (USDT, USDC, DAI, LINK, UNI, WBTC, WETH, MKR) -- as stable a piece of identity
# as the asset's name. `None` is correct and honest everywhere else.
#
# `venue_symbols` carries the conventional Binance USDT spot pair symbol for the
# asset (reference identity -- how the asset is addressed at that venue), not a
# live-listing guarantee; a delisting is an availability matter for the fill
# loop, not identity. It is empty `{}` for the quote asset itself (USDT), for
# assets that trade on another exchange or have no spot pair, and where listing
# status is uncertain. Two assets trade under a pair symbol that differs from
# their entity symbol (NEM -> XEMUSDT, RUNES -> RUNEUSDT); those are spelled out
# explicitly rather than built from the convention.
#
# Sourced 2026-08-07 from CoinGecko (ids via the verified map), Etherscan
# (contract addresses), Binance (pair symbols), and DeFiLlama (protocol slugs;
# see the note above PROTOCOLS for the inclusion bar and for the protocols
# deliberately left out). Re-derive to refresh.

from __future__ import annotations

from dataclasses import dataclass

SECTORS: tuple[tuple[str, str], ...] = (
    # (symbol, display name) -- one entity per asset class; member_of_sector
    # edges point assets at these. The six the CryptoAsset.sector field draws
    # from, no more: a seventh would have nowhere to link.
    ("l1", "Layer 1"),
    ("l2", "Layer 2"),
    ("defi", "DeFi"),
    ("infra", "Infrastructure"),
    ("stablecoin", "Stablecoin"),
    ("meme", "Meme"),
)


@dataclass(frozen=True)
class Chain:
    slug: str  # link key an asset's `chain` field references
    name: str  # display name


# Every chain referenced by an asset's `chain` field is listed here, so no asset
# is left without an issued_on target. Adding an asset on a new chain without
# adding the chain here would silently drop its edge -- the CryptoAsset
# constructor guard and the consistency test in test_crypto_seed.py both catch
# that, so the failure is loud rather than a missing edge.
CHAINS: tuple[Chain, ...] = (
    Chain("bitcoin", "Bitcoin"),
    Chain("ethereum", "Ethereum"),
    Chain("binance-smart-chain", "BNB Smart Chain"),
    Chain("solana", "Solana"),
    Chain("ripple", "XRP Ledger"),
    Chain("cardano", "Cardano"),
    Chain("dogecoin", "Dogecoin"),
    Chain("tron", "TRON"),
    Chain("avalanche", "Avalanche"),
    Chain("polkadot", "Polkadot"),
    Chain("polygon", "Polygon"),
    Chain("litecoin", "Litecoin"),
    Chain("cosmos", "Cosmos"),
    Chain("stellar", "Stellar"),
    Chain("ethereum-classic", "Ethereum Classic"),
    Chain("monero", "Monero"),
    Chain("bitcoin-cash", "Bitcoin Cash"),
    Chain("algorand", "Algorand"),
    Chain("filecoin", "Filecoin"),
    Chain("fantom", "Fantom"),
    Chain("near", "NEAR Protocol"),
    Chain("aptos", "Aptos"),
    Chain("arbitrum", "Arbitrum"),
    Chain("optimism", "Optimism"),
    Chain("zeta", "ZetaChain"),
    Chain("internet-computer", "Internet Computer"),
    Chain("injective", "Injective"),
    Chain("stacks", "Stacks"),
    Chain("kava", "Kava"),
    Chain("thorchain", "THORChain"),
    Chain("mina", "Mina"),
    Chain("flow", "Flow"),
    Chain("elrond", "MultiversX"),
    Chain("tezos", "Tezos"),
    Chain("hedera", "Hedera"),
    Chain("theta", "Theta"),
    Chain("kusama", "Kusama"),
    Chain("dash", "Dash"),
    Chain("zilliqa", "Zilliqa"),
    Chain("neo", "NEO"),
    Chain("waves", "Waves"),
    Chain("qtum", "Qtum"),
    Chain("iota", "IOTA"),
    Chain("vechain", "VeChain"),
    Chain("icon", "ICON"),
    Chain("ontology", "Ontology"),
    Chain("zcash", "Zcash"),
    Chain("siacoin", "Siacoin"),
    Chain("nem", "NEM"),
    Chain("lisk", "Lisk"),
    Chain("ark", "ARK"),
    # Added for the Hyperliquid carry universe (Findings 23-25). HYPE and PURR
    # are native to Hyperliquid's own L1 and BERA to Berachain; mapping them to
    # an existing chain would be a wrong `issued_on` edge rather than a missing
    # one, and the deduction chain walks those edges.
    Chain("hyperliquid", "Hyperliquid"),
    Chain("berachain", "Berachain"),
)


@dataclass(frozen=True)
class Protocol:
    defillama_slug: str  # link key an asset's `defillama_slug` field references
    name: str
    chain: str  # a CHAINS slug; the chain the protocol originated on
    # The asset that governs this protocol, as a CRYPTO_ASSETS symbol, or None.
    # None means one of two honest things: the protocol has no governance token
    # (Liquity, Spark), or it has one that this universe does not carry (LDO,
    # CAKE, PENDLE, ...). It is never a placeholder for a token that exists and
    # is seeded -- the consistency test in test_crypto_seed.py pairs this field
    # with the asset's own `defillama_slug` in both directions, so a link
    # asserted on one side and missing on the other fails.
    governance_token: str | None


# Only protocols whose DeFiLlama slug is unambiguous are listed, so `governs`
# never points at the wrong protocol's fees and `fundamentals.protocol` never
# computes a real-looking P/F for another company's revenue.
#
# The bar is: the slug names *this* protocol on DeFiLlama and could not
# plausibly name a different one. A slug that has drifted (a protocol renamed,
# a version suffix added) 404s and the fill loop records `unfillable` -- an
# honest failure. A slug that resolves to *another* protocol is the one outcome
# nothing downstream can detect, so a name shared with anything else is
# disqualifying on its own.
#
# Deliberately absent, because the slug could not be settled without a lookup
# and a guessed one is worse than a gap: Jupiter (aggregator, perps and the
# parent are separate DeFiLlama entries and it is not clear which the plain
# slug reaches), Frax (a parent over frxETH/Fraxlend/Fraxswap whose plain slug
# is uncertain), Radiant and BENQI (both are split into per-market entries and
# the parent slug is not certain). They stay out until the slug is verified
# against DeFiLlama rather than recalled.
PROTOCOLS: tuple[Protocol, ...] = (
    # DEXes and aggregators
    Protocol("uniswap", "Uniswap", "ethereum", "UNI"),
    Protocol("curve-dex", "Curve DEX", "ethereum", "CRV"),
    Protocol("pancakeswap", "PancakeSwap", "binance-smart-chain", None),
    Protocol("balancer", "Balancer", "ethereum", "BAL"),
    Protocol("sushiswap", "SushiSwap", "ethereum", "SUSHI"),
    Protocol("raydium", "Raydium", "solana", None),
    Protocol("orca", "Orca", "solana", None),
    Protocol("loopring", "Loopring", "ethereum", "LRC"),
    # Lending and CDPs
    Protocol("aave", "Aave", "ethereum", "AAVE"),
    Protocol("compound-finance", "Compound Finance", "ethereum", "COMP"),
    Protocol("makerdao", "MakerDAO", "ethereum", "MKR"),
    Protocol("morpho", "Morpho", "ethereum", None),
    Protocol("spark", "Spark", "ethereum", None),
    Protocol("venus", "Venus", "binance-smart-chain", None),
    Protocol("liquity", "Liquity", "ethereum", None),
    # Liquid staking and restaking
    Protocol("lido", "Lido", "ethereum", None),
    Protocol("rocket-pool", "Rocket Pool", "ethereum", None),
    Protocol("jito", "Jito", "solana", "JTO"),
    Protocol("eigenlayer", "EigenLayer", "ethereum", None),
    # Yield
    Protocol("convex-finance", "Convex Finance", "ethereum", None),
    Protocol("yearn-finance", "Yearn Finance", "ethereum", None),
    Protocol("pendle", "Pendle", "ethereum", None),
    # Derivatives
    Protocol("gmx", "GMX", "arbitrum", None),
    Protocol("dydx", "dYdX", "ethereum", None),
    Protocol("synthetix", "Synthetix", "ethereum", "SNX"),
    Protocol("gains-network", "Gains Network", "arbitrum", None),
    # Synthetic dollars
    Protocol("ethena", "Ethena", "ethereum", None),
    # Bridges and cross-chain liquidity
    Protocol("stargate", "Stargate", "ethereum", None),
    Protocol("across", "Across", "ethereum", None),
    Protocol("hop-protocol", "Hop Protocol", "ethereum", None),
    Protocol("thorchain", "THORChain", "thorchain", "RUNES"),
    # Middleware and cover
    Protocol("instadapp", "Instadapp", "ethereum", None),
    Protocol("nexus-mutual", "Nexus Mutual", "ethereum", None),
)


@dataclass(frozen=True)
class CryptoAsset:
    symbol: str  # uppercased SYMBOL_TO_ID key, e.g. "BTC"
    name: str
    coingecko_id: str  # verified in coingecko.SYMBOL_TO_ID
    chain: str  # a CHAINS slug; the asset's native chain
    contract_address: str | None  # canonical ERC-20, or None if unsourced
    defillama_slug: str | None  # a PROTOCOLS slug, or None
    venue_symbols: dict[str, str]  # {"binance": "BTCUSDT", ...}
    sector: str  # one of the SECTORS symbols

    def __post_init__(self) -> None:
        # Validate at construction so a bad reference row fails loud, the moment
        # the module loads, rather than silently dropping an edge at seed time.
        # An unknown sector/chain/defillama_slug here means the data file and the
        # reference tuples have drifted apart.
        if self.sector not in {s for s, _ in SECTORS}:
            raise ValueError(f"unknown sector {self.sector!r} for {self.symbol}")
        if self.chain not in {c.slug for c in CHAINS}:
            raise ValueError(f"unknown chain {self.chain!r} for {self.symbol}")
        if self.defillama_slug is not None and self.defillama_slug not in {
            p.defillama_slug for p in PROTOCOLS
        }:
            raise ValueError(
                f"defillama_slug {self.defillama_slug!r} for {self.symbol} "
                f"has no matching PROTOCOLS entry"
            )


def _hl(sym: str) -> dict[str, str]:
    # Hyperliquid's ccxt unified symbol for the PERPETUAL, which is the leg that
    # pays funding. It settles in USDC rather than USDT, and the symbol carries
    # its own colon -- `split_part(key, ':', 1)` still yields the venue because
    # the venue prefix comes first.
    return {"hyperliquid": f"{sym}/USDC:USDC"}


def _b(sym: str) -> dict[str, str]:
    # The conventional Binance USDT pair for a spot-listed asset. Centralised
    # here so the convention lives in one place; assets that are not Binance-
    # listed or whose pair symbol differs pass a literal dict instead.
    return {"binance": sym + "USDT"}


# 91 assets, ordered roughly by market cap and prominence. Every coingecko_id
# is copied verbatim from omni.ingest.coingecko.SYMBOL_TO_ID; the drift test
# enforces they stay in lock-step.
CRYPTO_ASSETS: tuple[CryptoAsset, ...] = (
    CryptoAsset("BTC", "Bitcoin", "bitcoin", "bitcoin", None, None, _b("BTC"), "l1"),
    CryptoAsset("ETH", "Ethereum", "ethereum", "ethereum", None, None, _b("ETH"), "l1"),
    CryptoAsset(
        "USDT",
        "Tether",
        "tether",
        "ethereum",
        "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        None,
        {},
        "stablecoin",
    ),
    CryptoAsset(
        "USDC",
        "USD Coin",
        "usd-coin",
        "ethereum",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0CE3606eB48",
        None,
        _b("USDC"),
        "stablecoin",
    ),
    CryptoAsset(
        "DAI",
        "Dai",
        "dai",
        "ethereum",
        "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        None,
        _b("DAI"),
        "stablecoin",
    ),
    CryptoAsset("BNB", "BNB", "binancecoin", "binance-smart-chain", None, None, _b("BNB"), "l1"),
    CryptoAsset("SOL", "Solana", "solana", "solana", None, None, _b("SOL"), "l1"),
    CryptoAsset("XRP", "XRP", "ripple", "ripple", None, None, _b("XRP"), "l1"),
    CryptoAsset("ADA", "Cardano", "cardano", "cardano", None, None, _b("ADA"), "l1"),
    CryptoAsset("DOGE", "Dogecoin", "dogecoin", "dogecoin", None, None, _b("DOGE"), "meme"),
    CryptoAsset("TRX", "TRON", "tron", "tron", None, None, _b("TRX"), "l1"),
    CryptoAsset("AVAX", "Avalanche", "avalanche-2", "avalanche", None, None, _b("AVAX"), "l1"),
    CryptoAsset("SHIB", "Shiba Inu", "shiba-inu", "ethereum", None, None, _b("SHIB"), "meme"),
    CryptoAsset("DOT", "Polkadot", "polkadot", "polkadot", None, None, _b("DOT"), "l1"),
    CryptoAsset("MATIC", "Polygon", "matic-network", "polygon", None, None, _b("MATIC"), "l2"),
    CryptoAsset(
        "LINK",
        "Chainlink",
        "chainlink",
        "ethereum",
        "0x514910771AF9Ca656af840dff83E8264EcF986CA",
        None,
        _b("LINK"),
        "infra",
    ),
    CryptoAsset("LTC", "Litecoin", "litecoin", "litecoin", None, None, _b("LTC"), "l1"),
    CryptoAsset(
        "UNI",
        "Uniswap",
        "uniswap",
        "ethereum",
        "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
        "uniswap",
        _b("UNI"),
        "defi",
    ),
    CryptoAsset("ATOM", "Cosmos Hub", "cosmos", "cosmos", None, None, _b("ATOM"), "l1"),
    CryptoAsset("XLM", "Stellar", "stellar", "stellar", None, None, _b("XLM"), "l1"),
    CryptoAsset(
        "ETC",
        "Ethereum Classic",
        "ethereum-classic",
        "ethereum-classic",
        None,
        None,
        _b("ETC"),
        "l1",
    ),
    CryptoAsset("BCH", "Bitcoin Cash", "bitcoin-cash", "bitcoin-cash", None, None, _b("BCH"), "l1"),
    CryptoAsset("ALGO", "Algorand", "algorand", "algorand", None, None, _b("ALGO"), "l1"),
    CryptoAsset("FIL", "Filecoin", "filecoin", "filecoin", None, None, _b("FIL"), "l1"),
    CryptoAsset("FTM", "Fantom", "fantom", "fantom", None, None, _b("FTM"), "l1"),
    CryptoAsset("NEAR", "NEAR Protocol", "near", "near", None, None, _b("NEAR"), "l1"),
    CryptoAsset("APT", "Aptos", "aptos", "aptos", None, None, _b("APT"), "l1"),
    CryptoAsset("ARB", "Arbitrum", "arbitrum", "arbitrum", None, None, _b("ARB"), "l2"),
    CryptoAsset("OP", "Optimism", "optimism", "optimism", None, None, _b("OP"), "l2"),
    CryptoAsset(
        "WBTC",
        "Wrapped Bitcoin",
        "wrapped-bitcoin",
        "ethereum",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        None,
        {},
        "defi",
    ),
    CryptoAsset(
        "MKR",
        "Maker",
        "maker",
        "ethereum",
        "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
        "makerdao",
        _b("MKR"),
        "defi",
    ),
    CryptoAsset("LEO", "LEO Token", "leo-token", "ethereum", None, None, {}, "infra"),
    CryptoAsset(
        "ICP",
        "Internet Computer",
        "internet-computer",
        "internet-computer",
        None,
        None,
        _b("ICP"),
        "l1",
    ),
    CryptoAsset("APE", "ApeCoin", "apecoin", "ethereum", None, None, _b("APE"), "defi"),
    CryptoAsset("SAND", "The Sandbox", "the-sandbox", "ethereum", None, None, _b("SAND"), "defi"),
    CryptoAsset("MANA", "Decentraland", "decentraland", "ethereum", None, None, _b("MANA"), "defi"),
    CryptoAsset("GALA", "Gala", "gala", "ethereum", None, None, _b("GALA"), "defi"),
    CryptoAsset("AXS", "Axie Infinity", "axie-infinity", "ethereum", None, None, _b("AXS"), "defi"),
    CryptoAsset("AAVE", "Aave", "aave", "ethereum", None, "aave", _b("AAVE"), "defi"),
    CryptoAsset(
        "COMP",
        "Compound",
        "compound-governance-token",
        "ethereum",
        None,
        "compound-finance",
        _b("COMP"),
        "defi",
    ),
    CryptoAsset("SNX", "Synthetix", "havven", "ethereum", None, "synthetix", _b("SNX"), "defi"),
    CryptoAsset(
        "CRV", "Curve DAO", "curve-dao-token", "ethereum", None, "curve-dex", _b("CRV"), "defi"
    ),
    CryptoAsset("GRT", "The Graph", "the-graph", "ethereum", None, None, _b("GRT"), "infra"),
    CryptoAsset("1INCH", "1inch", "1inch", "ethereum", None, None, _b("1INCH"), "defi"),
    CryptoAsset("SUSHI", "SushiSwap", "sushi", "ethereum", None, "sushiswap", _b("SUSHI"), "defi"),
    CryptoAsset("OKB", "OKB", "okb", "ethereum", None, None, {}, "infra"),
    CryptoAsset(
        "BAT",
        "Basic Attention Token",
        "basic-attention-token",
        "ethereum",
        None,
        None,
        _b("BAT"),
        "infra",
    ),
    CryptoAsset("ZET", "ZetaChain", "zeta", "zeta", None, None, _b("ZET"), "l1"),
    CryptoAsset("RNDR", "Render", "render-token", "ethereum", None, None, _b("RNDR"), "infra"),
    CryptoAsset("IMX", "Immutable", "immutable-x", "ethereum", None, None, _b("IMX"), "infra"),
    CryptoAsset("INJ", "Injective", "injection", "injective", None, None, _b("INJ"), "l1"),
    CryptoAsset("STX", "Stacks", "blockstack", "stacks", None, None, _b("STX"), "l1"),
    CryptoAsset("KAVA", "Kava", "kava", "kava", None, None, _b("KAVA"), "l1"),
    CryptoAsset(
        "RUNES",
        "THORChain (RUNE)",
        "rune",
        "thorchain",
        None,
        "thorchain",
        {"binance": "RUNEUSDT"},
        "defi",
    ),
    CryptoAsset("MINA", "Mina", "mina-protocol", "mina", None, None, _b("MINA"), "l1"),
    CryptoAsset("FLOW", "Flow", "flow", "flow", None, None, _b("FLOW"), "l1"),
    CryptoAsset("EGLD", "MultiversX", "elrond-erd-2", "elrond", None, None, _b("EGLD"), "l1"),
    CryptoAsset("XTZ", "Tezos", "tezos", "tezos", None, None, _b("XTZ"), "l1"),
    CryptoAsset("HBAR", "Hedera", "hedera-hashgraph", "hedera", None, None, _b("HBAR"), "l1"),
    CryptoAsset("CHZ", "Chiliz", "chiliz", "ethereum", None, None, _b("CHZ"), "infra"),
    CryptoAsset(
        "ENS",
        "Ethereum Name Service",
        "ethereum-name-service",
        "ethereum",
        None,
        None,
        _b("ENS"),
        "infra",
    ),
    CryptoAsset("GMT", "STEPN", "stepn", "solana", None, None, _b("GMT"), "defi"),
    CryptoAsset("THETA", "Theta Network", "theta-token", "theta", None, None, _b("THETA"), "infra"),
    CryptoAsset("KSM", "Kusama", "kusama", "kusama", None, None, _b("KSM"), "l1"),
    CryptoAsset("DASH", "Dash", "dash", "dash", None, None, _b("DASH"), "l1"),
    CryptoAsset("ZIL", "Zilliqa", "zilliqa", "zilliqa", None, None, _b("ZIL"), "l1"),
    CryptoAsset("NEO", "NEO", "neo", "neo", None, None, _b("NEO"), "l1"),
    CryptoAsset("WAVES", "Waves", "waves", "waves", None, None, _b("WAVES"), "l1"),
    CryptoAsset("QTUM", "Qtum", "qtum", "qtum", None, None, _b("QTUM"), "l1"),
    CryptoAsset("IOTA", "IOTA", "iota", "iota", None, None, _b("IOTA"), "l1"),
    CryptoAsset("VET", "VeChain", "vechain", "vechain", None, None, _b("VET"), "l1"),
    CryptoAsset("ICX", "ICON", "icon", "icon", None, None, _b("ICX"), "l1"),
    CryptoAsset("ONT", "Ontology", "ontology", "ontology", None, None, _b("ONT"), "l1"),
    CryptoAsset("ZEC", "Zcash", "zcash", "zcash", None, None, _b("ZEC"), "l1"),
    CryptoAsset("SC", "Siacoin", "siacoin", "siacoin", None, None, _b("SC"), "infra"),
    CryptoAsset(
        "CUSDC", "Compound USDC (cUSDC)", "compound-usd-coin", "ethereum", None, None, {}, "defi"
    ),
    CryptoAsset("NEM", "NEM", "nem", "nem", None, None, {"binance": "XEMUSDT"}, "l1"),
    CryptoAsset("LSK", "Lisk", "lisk", "lisk", None, None, _b("LSK"), "l1"),
    CryptoAsset("ARK", "ARK", "ark", "ark", None, None, _b("ARK"), "l1"),
    CryptoAsset("LRC", "Loopring", "loopring", "ethereum", None, "loopring", _b("LRC"), "defi"),
    CryptoAsset("BAL", "Balancer", "balancer", "ethereum", None, "balancer", _b("BAL"), "defi"),
    CryptoAsset(
        "WETH",
        "Wrapped Ether",
        "weth",
        "ethereum",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        None,
        {},
        "defi",
    ),
    CryptoAsset("HT", "Huobi Token", "huobi-token", "ethereum", None, None, {}, "infra"),
    CryptoAsset("PEPE", "Pepe", "pepe", "ethereum", None, None, _b("PEPE"), "meme"),
    CryptoAsset("WIF", "dogwifhat", "dogwifhat", "solana", None, None, _b("WIF"), "meme"),
    CryptoAsset("BONK", "Bonk", "bonk", "solana", None, None, _b("BONK"), "meme"),
    CryptoAsset("FLOKI", "Floki", "floki", "ethereum", None, None, _b("FLOKI"), "meme"),
    CryptoAsset("BOME", "Book of Meme", "book-of-meme", "solana", None, None, _b("BOME"), "meme"),
    CryptoAsset(
        "JUP", "Jupiter", "jupiter-exchange-solana", "solana", None, None, _b("JUP"), "defi"
    ),
    CryptoAsset("PYTH", "Pyth Network", "pyth-network", "solana", None, None, _b("PYTH"), "infra"),
    CryptoAsset("JTO", "Jito", "jito-governance-token", "solana", None, "jito", _b("JTO"), "defi"),
    # The Hyperliquid carry universe (Findings 23-25): every asset pairable
    # there -- spot and perpetual both listed -- with at least 540 days of
    # funding history. `venue_symbols` carries the ccxt unified symbol for the
    # PERPETUAL leg, which is the instrument that pays funding; Hyperliquid
    # settles in USDC, not USDT, so `_b()` does not apply.
    #
    # coingecko ids verified against /coins/markets rather than assumed from the
    # ticker: HYPE and PURR both collide with namesakes (`hype-3`,
    # `purrcoin`), and TRUMP with three. The ones here are ranked 10 and 519 and
    # 114 by market cap, which is what identifies them.
    CryptoAsset("HYPE", "Hyperliquid", "hyperliquid", "hyperliquid", None, None, _hl("HYPE"), "l1"),
    CryptoAsset("PURR", "Purr", "purr-2", "hyperliquid", None, None, _hl("PURR"), "meme"),
    CryptoAsset(
        "PENGU", "Pudgy Penguins", "pudgy-penguins", "solana", None, None,
        _hl("PENGU") | _b("PENGU"), "meme",
    ),
    CryptoAsset(
        "WLD", "Worldcoin", "worldcoin-wld", "ethereum", None, None,
        _hl("WLD") | _b("WLD"), "infra",
    ),
    CryptoAsset(
        "ENA", "Ethena", "ethena", "ethereum", None, None,
        _hl("ENA") | _b("ENA"), "defi",
    ),
    CryptoAsset(
        "TRUMP", "Official Trump", "official-trump", "solana", None, None,
        _hl("TRUMP") | _b("TRUMP"), "meme",
    ),
    CryptoAsset(
        "BERA", "Berachain", "berachain-bera", "berachain", None, None,
        _hl("BERA") | _b("BERA"), "l1",
    ),
    CryptoAsset(
        "SPX", "SPX6900", "spx6900", "ethereum", None, None,
        _hl("SPX") | _b("SPX"), "meme",
    ),
)
