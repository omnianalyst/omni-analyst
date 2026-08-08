# Public reference data for the country-universe seed.
#
# These are NOT measurements and NOT provider-sourced coverage -- they are the
# slowly-changing reference sets that define the geopolitical dimension the
# system scans: which sovereigns exist, the region bucket each sits in, the
# single-country ETF that tracks its equity market, and its currency's pair
# versus USD. Seeding identity is honest; every price, spread and macro series
# is still fetched live by the fill loop. Nothing here is a fabricated claim.
#
# WHY THE ETF AND THE FX PAIR ARE PART OF IDENTITY
#
# The conviction gate refuses to surface a claim class until 10 predictions have
# resolved. Sovereign horizons are long: at 90 days, ten non-overlapping
# resolutions is roughly 900 days before the first finding could surface. A
# country entity is therefore only useful to this system if it carries something
# that resolves on a short clock, and the two such things are the ETF that
# tracks its market and its FX pair -- both are daily price series the existing
# trend producer can already write triple-barrier predictions against.
#
# `is_predictable` makes that explicit rather than implied. A country with
# neither is still seeded (it is real, and macro or news claims may attach to
# it) but nothing downstream should attempt a call it cannot score.
#
# `etf_symbol` is the field a wrong value silently corrupts forever -- a
# mistyped ticker fetches a *different country's* price series and attributes it
# to this entity, the exact misattribution the coverage store exists to prevent.
# So the rule here is accuracy over count: only well-established single-country
# funds are named (the iShares MSCI single-country series, plus the Global X /
# VanEck equivalents where iShares has none). Where a fund was closed, delisted
# or could not be recalled with certainty, `etf_symbol` is None. Ten of the
# countries below carry None for that reason -- Russia (its US-listed fund was
# closed after the 2022 sanctions), Czechia, Hungary, Romania, Ukraine, Kuwait,
# Egypt, Nigeria, Kenya and Pakistan (no fund, or one whose current listing
# status is uncertain). None is correct and honest; a guess is not. The US uses
# SPY, which is the broad-market fund tracking that country rather than an MSCI
# single-country wrapper, and is unambiguous.
#
# `etf_symbol` is unique across countries by construction: two countries sharing
# a ticker would mean one country's predictions were made from another's prices.
# `fx_pair` is deliberately NOT unique -- the eleven euro-area members below all
# carry EURUSD, because they genuinely share a currency, and a shared sovereign
# currency is a fact about the world rather than a data error.
#
# FX PAIR CONVENTION -- one convention, applied without exception
#
# `fx_pair` is written `<CCY>USD` and means USD PER ONE UNIT OF THE LOCAL
# CURRENCY. The country's own currency is always the base; USD is always the
# quote. So a rise in the series always means the local currency strengthened
# against the dollar, for every country, with no per-country sign rule to get
# wrong.
#
# For EUR, GBP, AUD and NZD this coincides with the interbank convention. For
# everything else -- JPY, CHF, CAD, MXN, the Scandies, and the EM currencies --
# the interbank convention quotes the inverse (USDJPY is JPY per USD). A
# consumer fetching a provider that only publishes the inverse must invert the
# series before using it; taking USDJPY as JPYUSD would flip the direction of
# every prediction on Japan.
#
# Pegged and heavily managed pairs are still recorded, because the peg is a fact
# about the currency and not a reason to omit its identity: HKD, DKK, SAR, AED
# and QAR are pegged, so their series is real but near-flat. Every one of those
# countries also carries an ETF, so none of them depends on the pegged pair to
# be predictable. Where a currency has no single well-known USD quote -- ARS
# (multiple official and parallel rates), RUB (post-2022), VND, PKR, UAH, RON,
# KWD, EGP, NGN, KES -- `fx_pair` is None.
#
# `region` is a coarse navigation bucket for the member_of_region edge, not a
# geopolitical statement. Turkey and Russia sit in `europe` and Egypt in
# `africa` because that is where their equity markets are grouped; nothing
# downstream should read meaning into the bucket beyond "roughly near".
#
# Sourced 2026-08-07 from ISO 3166-1 (iso2/iso3), ISO 4217 (currency), and the
# issuers' published single-country fund lists (iShares MSCI series, Global X,
# VanEck). Re-derive to refresh; a fund closure is the most likely drift.

from __future__ import annotations

from dataclasses import dataclass

REGIONS: tuple[tuple[str, str], ...] = (
    # (slug, display name) -- one entity per region; member_of_region edges
    # point countries at these. The six the Country.region field draws from, no
    # more: a seventh would have nowhere to link.
    ("north_america", "North America"),
    ("latin_america", "Latin America"),
    ("europe", "Europe"),
    ("middle_east", "Middle East"),
    ("africa", "Africa"),
    ("asia_pacific", "Asia-Pacific"),
)


@dataclass(frozen=True)
class Country:
    iso2: str  # ISO 3166-1 alpha-2, e.g. "DE"
    iso3: str  # ISO 3166-1 alpha-3, e.g. "DEU"
    name: str
    region: str  # a REGIONS slug
    etf_symbol: str | None  # single-country ETF, or None if not confidently known
    fx_pair: str | None  # <CCY>USD, USD per one unit of the local currency
    currency: str | None  # ISO 4217

    def __post_init__(self) -> None:
        # Validate the cross-reference at construction so a bad reference row
        # fails loud, the moment the module loads, rather than silently dropping
        # an edge at seed time. An unknown region here means the data file and
        # the REGIONS tuple have drifted apart.
        if self.region not in {slug for slug, _ in REGIONS}:
            raise ValueError(f"unknown region {self.region!r} for {self.iso2}")

    @property
    def is_predictable(self) -> bool:
        """Whether this country carries a price series that resolves on a short clock.

        The ETF and the FX pair are the only two short-horizon targets a country
        has. Without either, no prediction written against this entity could be
        scored, so nothing downstream should write one.
        """
        return self.etf_symbol is not None or self.fx_pair is not None


COUNTRIES: tuple[Country, ...] = (
    # -- North America ------------------------------------------------------
    Country("US", "USA", "United States", "north_america", "SPY", None, "USD"),
    Country("CA", "CAN", "Canada", "north_america", "EWC", "CADUSD", "CAD"),
    Country("MX", "MEX", "Mexico", "north_america", "EWW", "MXNUSD", "MXN"),
    # -- Latin America ------------------------------------------------------
    Country("BR", "BRA", "Brazil", "latin_america", "EWZ", "BRLUSD", "BRL"),
    # Argentina: ARS has official and parallel rates that diverge, so no single
    # USD quote is the pair. ETF-only, still predictable.
    Country("AR", "ARG", "Argentina", "latin_america", "ARGT", None, "ARS"),
    Country("CL", "CHL", "Chile", "latin_america", "ECH", "CLPUSD", "CLP"),
    Country("CO", "COL", "Colombia", "latin_america", "GXG", "COPUSD", "COP"),
    Country("PE", "PER", "Peru", "latin_america", "EPU", "PENUSD", "PEN"),
    # -- Europe -------------------------------------------------------------
    Country("DE", "DEU", "Germany", "europe", "EWG", "EURUSD", "EUR"),
    Country("FR", "FRA", "France", "europe", "EWQ", "EURUSD", "EUR"),
    Country("IT", "ITA", "Italy", "europe", "EWI", "EURUSD", "EUR"),
    Country("ES", "ESP", "Spain", "europe", "EWP", "EURUSD", "EUR"),
    Country("NL", "NLD", "Netherlands", "europe", "EWN", "EURUSD", "EUR"),
    Country("BE", "BEL", "Belgium", "europe", "EWK", "EURUSD", "EUR"),
    Country("AT", "AUT", "Austria", "europe", "EWO", "EURUSD", "EUR"),
    Country("IE", "IRL", "Ireland", "europe", "EIRL", "EURUSD", "EUR"),
    Country("PT", "PRT", "Portugal", "europe", "PGAL", "EURUSD", "EUR"),
    Country("FI", "FIN", "Finland", "europe", "EFNL", "EURUSD", "EUR"),
    Country("GR", "GRC", "Greece", "europe", "GREK", "EURUSD", "EUR"),
    Country("GB", "GBR", "United Kingdom", "europe", "EWU", "GBPUSD", "GBP"),
    Country("CH", "CHE", "Switzerland", "europe", "EWL", "CHFUSD", "CHF"),
    Country("SE", "SWE", "Sweden", "europe", "EWD", "SEKUSD", "SEK"),
    Country("NO", "NOR", "Norway", "europe", "ENOR", "NOKUSD", "NOK"),
    Country("DK", "DNK", "Denmark", "europe", "EDEN", "DKKUSD", "DKK"),
    Country("PL", "POL", "Poland", "europe", "EPOL", "PLNUSD", "PLN"),
    # Czechia and Hungary: no single-country fund recalled with confidence, but
    # both currencies have a standard USD quote. FX-only, still predictable.
    Country("CZ", "CZE", "Czechia", "europe", None, "CZKUSD", "CZK"),
    Country("HU", "HUN", "Hungary", "europe", None, "HUFUSD", "HUF"),
    Country("TR", "TUR", "Turkey", "europe", "TUR", "TRYUSD", "TRY"),
    # Russia, Ukraine, Romania: seeded as real sovereigns, but neither a fund
    # nor a usable USD quote. Not predictable.
    Country("RU", "RUS", "Russia", "europe", None, None, "RUB"),
    Country("UA", "UKR", "Ukraine", "europe", None, None, "UAH"),
    Country("RO", "ROU", "Romania", "europe", None, None, "RON"),
    # -- Middle East --------------------------------------------------------
    Country("IL", "ISR", "Israel", "middle_east", "EIS", "ILSUSD", "ILS"),
    Country("SA", "SAU", "Saudi Arabia", "middle_east", "KSA", "SARUSD", "SAR"),
    Country("AE", "ARE", "United Arab Emirates", "middle_east", "UAE", "AEDUSD", "AED"),
    Country("QA", "QAT", "Qatar", "middle_east", "QAT", "QARUSD", "QAR"),
    Country("KW", "KWT", "Kuwait", "middle_east", None, None, "KWD"),
    # -- Africa -------------------------------------------------------------
    Country("ZA", "ZAF", "South Africa", "africa", "EZA", "ZARUSD", "ZAR"),
    Country("EG", "EGY", "Egypt", "africa", None, None, "EGP"),
    Country("NG", "NGA", "Nigeria", "africa", None, None, "NGN"),
    Country("KE", "KEN", "Kenya", "africa", None, None, "KES"),
    # -- Asia-Pacific -------------------------------------------------------
    Country("JP", "JPN", "Japan", "asia_pacific", "EWJ", "JPYUSD", "JPY"),
    Country("CN", "CHN", "China", "asia_pacific", "FXI", "CNYUSD", "CNY"),
    Country("HK", "HKG", "Hong Kong", "asia_pacific", "EWH", "HKDUSD", "HKD"),
    Country("TW", "TWN", "Taiwan", "asia_pacific", "EWT", "TWDUSD", "TWD"),
    Country("KR", "KOR", "South Korea", "asia_pacific", "EWY", "KRWUSD", "KRW"),
    Country("IN", "IND", "India", "asia_pacific", "INDA", "INRUSD", "INR"),
    Country("ID", "IDN", "Indonesia", "asia_pacific", "EIDO", "IDRUSD", "IDR"),
    Country("TH", "THA", "Thailand", "asia_pacific", "THD", "THBUSD", "THB"),
    Country("MY", "MYS", "Malaysia", "asia_pacific", "EWM", "MYRUSD", "MYR"),
    Country("PH", "PHL", "Philippines", "asia_pacific", "EPHE", "PHPUSD", "PHP"),
    Country("SG", "SGP", "Singapore", "asia_pacific", "EWS", "SGDUSD", "SGD"),
    # Vietnam: VND has no freely-quoted USD cross. ETF-only, still predictable.
    Country("VN", "VNM", "Vietnam", "asia_pacific", "VNM", None, "VND"),
    Country("AU", "AUS", "Australia", "asia_pacific", "EWA", "AUDUSD", "AUD"),
    Country("NZ", "NZL", "New Zealand", "asia_pacific", "ENZL", "NZDUSD", "NZD"),
    Country("PK", "PAK", "Pakistan", "asia_pacific", None, None, "PKR"),
)
