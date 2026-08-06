"""SEC filing events.

EDGAR's submissions endpoint lists what a company has filed and when. That is
a different fact from what the filing *said* — `edgar.companyfacts` covers the
numbers, this covers the act of filing.

Worth having separately because absence is informative. A company that has not
filed an 8-K in two years and one that files monthly are different, and only a
record of filing events can tell them apart.

The bitemporal mapping is unusually clean here: `reportDate` is the period the
filing concerns (event_date) and `filingDate` is when it became public
(knowledge_date). The lag between them is the disclosure delay, which is itself
a signal — and it is only visible because both are kept.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "sec_edgar"
PROVIDER_KEY = "sec_edgar"
CLAIM_TYPE = "filing_event"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

SubmissionsFetcher = Callable[[str], Awaitable[dict | None]]


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_submissions(payload: dict, *, cik: str) -> list[ClaimDraft]:
    """Flatten EDGAR's recent-filings block into claim drafts.

    The block is column-oriented — parallel arrays of form, filingDate,
    reportDate and accessionNumber, one entry per index — not a list of
    objects. Zipping them by position is the only correct read, and a short
    array means the record is incomplete rather than empty.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filed = recent.get("filingDate") or []
    reported = recent.get("reportDate") or []
    accessions = recent.get("accessionNumber") or []

    company = payload.get("name")
    drafts: list[ClaimDraft] = []

    for i, form in enumerate(forms):
        if i >= len(filed) or i >= len(accessions):
            # Ragged arrays mean a truncated response; stop rather than pair
            # a form with another filing's date.
            break

        knowledge_date = _to_datetime(filed[i])
        if knowledge_date is None:
            continue

        # A filing with no reportDate covers no prior period -- an 8-K about
        # an event that day, say. The filing date is then both axes.
        event_date = _to_datetime(reported[i] if i < len(reported) else None)
        if event_date is None or event_date > knowledge_date:
            event_date = knowledge_date

        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=accessions[i],
                value={
                    "form": form,
                    "accession": accessions[i],
                    "cik": cik,
                },
                event_date=event_date,
                knowledge_date=knowledge_date,
                confidence=1.0,
                evidence={"company": company} if company else None,
            )
        )
    return drafts


async def _fetch_submissions(cik: str, *, user_agent: str) -> dict | None:
    import httpx

    url = SUBMISSIONS_URL.format(cik10=cik.zfill(10))
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await get_json(client, url, headers={"User-Agent": user_agent})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise Unavailable(
                f"EDGAR submissions returned HTTP {response.status_code} for CIK {cik}"
            )
        return response.json()


class FilingsAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        fetch_fn: SubmissionsFetcher | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._user_agent:
                raise Unavailable("no SEC User-Agent configured")

            async def fetch_fn(cik: str) -> dict | None:
                return await _fetch_submissions(cik, user_agent=self._user_agent)

        payload = await fetch_fn(key)
        if payload is None:
            raise Unavailable(f"EDGAR has no submissions record for CIK {key}")
        return parse_submissions(payload, cik=key)
