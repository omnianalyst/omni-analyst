"""Macro perception adapter — the shareable perception layer.

Sibling of fred.py and the one perception source that accumulates as a shared
network asset: FRED publishes the market's own fear, stress and positioning
indices, and FRED is `allowed`-class (catalog fallback `FALLBACK_ALLOWED`).
Entity-level sentiment (news, social) is `byo_only`, so it stays private to
whichever key fetched it. This adapter is therefore the only perception
coverage the gap engine can score against a shared audience.

Reuses `parse_observations` from omni.ingest.fred for the ALFRED contract —
every vintage becomes its own draft, a '.' observation is kept as a null value
rather than dropped, and knowledge_date never precedes event_date. That
function hardcodes its own claim_type ('macro_series_point'), so drafts are
re-stamped here as 'perception_macro' with the series' evidence attached. The
parsing logic is not duplicated; the re-stamp is documented in the report.
"""

from __future__ import annotations

from dataclasses import replace

from omni.ingest.fred import ObsFetcher, _fetch_alfred, parse_observations
from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "fred"
PROVIDER_KEY = "fred"
CLAIM_TYPE = "perception_macro"

PERCEPTION_SERIES: dict[str, str] = {
    "UMCSENT": (
        "University of Michigan Consumer Sentiment — survey-based index of "
        "consumer attitudes and buying expectations."
    ),
    "VIXCLS": (
        "CBOE Volatility Index (VIX) — implied volatility of S&P 500 options, "
        "the equity fear gauge."
    ),
    "BAMLH0A0HYM2": (
        "ICE BofA US High Yield Option-Adjusted Spread — high-yield credit "
        "spread, a risk-appetite proxy."
    ),
    "T10Y2Y": (
        "10-Year minus 2-Year Treasury Constant Maturity spread — the "
        "yield-curve term spread, a growth-expectation proxy."
    ),
    "STLFSI4": (
        "St. Louis Fed Financial Stress Index — broad composite of interest, "
        "yield and funding-market stress."
    ),
    "DTWEXBGS": (
        "Nominal Broad U.S. Dollar Index — trade-weighted dollar, a risk-off "
        "proxy."
    ),
}


class MacroPerceptionAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: ObsFetcher | None = None,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._api_key:
                raise Unavailable("no FRED API key configured")

            async def fetch_fn(series_id: str) -> list[dict]:
                return await _fetch_alfred(series_id, api_key=self._api_key)

        measures = PERCEPTION_SERIES.get(key)
        if measures is None:
            raise Unavailable(f"{key} is not a curated macro-perception series")
        evidence = {"series": key, "measures": measures}

        observations = await fetch_fn(key)
        drafts = parse_observations(observations or [], series_id=key)
        return [
            replace(d, claim_type=CLAIM_TYPE, evidence=evidence) for d in drafts
        ]
