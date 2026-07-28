"""SEC EDGAR companyfacts ingestion.

Ported from v1 `app/data/warehouse/fundamentals.py`. Only the parsing and
fetch survive the port; v1's SQLAlchemy upsert/access layer is the writer's
job here, not the adapter's (a ClaimDraft is pre-entity by design).

EDGAR publishes the full as-reported history per concept under us-gaap. Each
XBRL fact carries `end` (the period it describes, so event_date) and `filed`
(when it became public, so knowledge_date). A 10-K/A restatement therefore
produces a second draft sharing event_date with a later knowledge_date --
exactly as a FRED revision does. Dropping either date collapses the
restatement into the original and silently serves hindsight to a backtest.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Sequence

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "sec_edgar"
PROVIDER_KEY = "sec_edgar"
CLAIM_TYPE = "fundamental_metric"

EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

# The us-gaap concepts the factor library needs (value, quality, investment).
# Lifted verbatim from v1 fundamentals.DEFAULT_CONCEPTS -- do not invent here.
DEFAULT_CONCEPTS = (
    "Assets",
    "StockholdersEquity",
    "Revenues",
    "NetIncomeLoss",
    "GrossProfit",
    "LiabilitiesAndStockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "Liabilities",
)

# SEC throttles at 10 requests/second and 403s a backfill. The live path
# spaces requests at least this far apart; injected fetchers bypass it so
# tests stay instant.
_MIN_REQUEST_INTERVAL = 0.1

FactsFetcher = Callable[[str], Awaitable[dict | None]]


async def _respect_rate_limit() -> None:
    # Module-level gate: at most one request starts per _MIN_REQUEST_INTERVAL.
    # The lock guards only the timing decision, not the network call, so a
    # slow response does not block the next caller beyond the interval.
    global _last_request_ts
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_ts
        remaining = _MIN_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        _last_request_ts = time.monotonic()


_rate_lock = asyncio.Lock()
_last_request_ts = 0.0


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_companyfacts(
    facts: dict,
    *,
    cik: str,
    concepts: Sequence[str] = DEFAULT_CONCEPTS,
) -> list[ClaimDraft]:
    """Flatten EDGAR companyfacts JSON into claim drafts.

    Only the requested ``concepts`` under us-gaap are kept. A fact missing
    ``end`` or ``filed`` is skipped -- guessing a date would fabricate
    provenance. ``unit`` is the XBRL unit key (USD, shares), preserved as-is.
    """
    drafts: list[ClaimDraft] = []
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for concept in concepts:
        node = usgaap.get(concept)
        if not node:
            continue
        for unit, entries in (node.get("units") or {}).items():
            for entry in entries:
                event_date = _to_datetime(entry.get("end"))
                knowledge_date = _to_datetime(entry.get("filed"))
                val = entry.get("val")
                if event_date is None or knowledge_date is None or val is None:
                    continue
                if knowledge_date < event_date:
                    # A fact filed before the period it reports is bad data.
                    # Skip it: ClaimDraft would raise, and one such row would
                    # otherwise abort the whole company's ingestion. Unlike
                    # ALFRED, where an early realtime_start is a known quirk
                    # worth clamping, there is no defensible filing date to
                    # infer here.
                    continue
                drafts.append(
                    ClaimDraft(
                        claim_type=CLAIM_TYPE,
                        key=concept,
                        value={"value": val},
                        event_date=event_date,
                        knowledge_date=knowledge_date,
                        confidence=1.0,
                        unit=unit,
                        evidence={
                            "cik": cik,
                            "form": entry.get("form"),
                            "fy": entry.get("fy"),
                            "fp": entry.get("fp"),
                        },
                    )
                )
    return drafts


async def _fetch_companyfacts(cik: str, *, user_agent: str) -> dict:
    import httpx

    cik10 = str(cik).zfill(10)
    url = EDGAR_FACTS_URL.format(cik10=cik10)
    headers = {"User-Agent": user_agent}
    await _respect_rate_limit()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise Unavailable(
                f"EDGAR companyfacts returned HTTP {response.status_code} "
                f"for CIK {cik10}"
            )
        return response.json()


class EdgarAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        fetch_fn: FactsFetcher | None = None,
        concepts: Sequence[str] = DEFAULT_CONCEPTS,
    ) -> None:
        self._user_agent = user_agent
        self._fetch_fn = fetch_fn
        self._concepts = concepts

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._user_agent:
                raise Unavailable("no SEC User-Agent configured")

            async def fetch_fn(cik: str) -> dict | None:
                return await _fetch_companyfacts(cik, user_agent=self._user_agent)

        facts = await fetch_fn(key)
        if facts is None:
            raise Unavailable(f"EDGAR returned no companyfacts for CIK {key}")
        return parse_companyfacts(
            facts, cik=key, concepts=self._concepts
        )
