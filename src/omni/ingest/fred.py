"""FRED/ALFRED ingestion.

Ported from v1 `app/data/warehouse/economic.py`, which was the only ingestion
code in that codebase with real tests — because its fetch was injectable.

ALFRED is queried for every vintage rather than current values. Each
observation carries `date` (the period, so event_date) and `realtime_start`
(when that figure became public, so knowledge_date). Keeping both is what
lets a backtest see the first print rather than a revision published years
later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Iterable

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "fred"
PROVIDER_KEY = "fred"
CLAIM_TYPE = "macro_series_point"

ALFRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# The full vintage history. Without this ALFRED returns only current values
# and the point-in-time guarantee is gone.
ALL_VINTAGES = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31"}

ObsFetcher = Callable[[str], Awaitable[list[dict]]]


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=UTC)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    # ALFRED encodes "not available yet" as ".".
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_observations(
    observations: Iterable[dict],
    *,
    series_id: str,
    units: str | None = None,
) -> list[ClaimDraft]:
    """Flatten ALFRED observations into claim drafts.

    An observation whose value is "." still becomes a draft with a null value.
    "FRED knows of this period and has not published a figure" is coverage;
    dropping it would make the gap engine treat a known hole as a missing
    fetch and re-request it forever.
    """
    drafts: list[ClaimDraft] = []
    for obs in observations:
        event_date = _to_datetime(obs.get("date"))
        knowledge_date = _to_datetime(obs.get("realtime_start"))
        if event_date is None or knowledge_date is None:
            continue
        if knowledge_date < event_date:
            # ALFRED publishes some series with a realtime_start before the
            # period closes. The schema forbids knowing a fact before it
            # happened, so the period is the earliest defensible knowledge date.
            knowledge_date = event_date
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=series_id,
                value={"value": _to_float(obs.get("value"))},
                event_date=event_date,
                knowledge_date=knowledge_date,
                confidence=1.0,
                unit=units,
            )
        )
    return drafts


async def _fetch_alfred(
    series_id: str, *, api_key: str, vintages: bool = True
) -> list[dict]:
    """Fetch observations, optionally every vintage.

    `vintages=False` asks for current values only. That is the right request
    for a series that is never restated -- a volatility index or an exchange
    rate has one true value per day, and FRED's daily snapshots of it are not
    revisions. Asking for every vintage of such a series is both wrong and
    over the API's 2,000-vintage ceiling.
    """
    import httpx

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    if vintages:
        params.update(ALL_VINTAGES)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(ALFRED_URL, params=params)
        if response.status_code != 200:
            # Carry FRED's own explanation. A bare status code sends whoever
            # reads the fill_attempt row back to the API docs; the message
            # usually names the exact problem.
            detail = ""
            try:
                detail = response.json().get("error_message", "")
            except Exception:
                detail = response.text[:200]
            raise Unavailable(
                f"ALFRED returned HTTP {response.status_code} for {series_id}"
                + (f": {detail}" if detail else "")
            )
        return response.json().get("observations", [])


class FredAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: ObsFetcher | None = None,
        units: str | None = None,
        vintages: bool = True,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._units = units
        self._vintages = vintages

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._api_key:
                raise Unavailable("no FRED API key configured")

            async def fetch_fn(series_id: str) -> list[dict]:
                return await _fetch_alfred(
                    series_id, api_key=self._api_key, vintages=self._vintages
                )

        observations = await fetch_fn(key)
        return parse_observations(
            observations or [], series_id=key, units=self._units
        )
