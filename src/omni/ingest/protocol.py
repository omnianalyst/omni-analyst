from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


class Unavailable(Exception):
    """A source could not answer.

    Raised, never returned as a value, and never substituted for with a
    default. The fill pipeline turns this into a `fill_attempt` row with
    outcome 'unfillable' and this exception's message as the reason.
    """


@dataclass(frozen=True)
class ClaimDraft:
    """A claim before it has an entity, an audience, or a licence class.

    Adapters produce drafts. They do not decide who may see the result or
    whether it is redistributable — that is resolved once, in the writer, from
    the adapter's `provider_key`. An adapter that made that decision itself
    would be one place the licence rule could be got wrong.
    """

    claim_type: str
    key: str
    value: dict[str, Any]
    event_date: datetime
    knowledge_date: datetime
    confidence: float
    unit: str | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")
        if self.knowledge_date < self.event_date:
            raise ValueError(
                f"knowledge_date {self.knowledge_date} precedes "
                f"event_date {self.event_date}"
            )


@runtime_checkable
class Adapter(Protocol):
    """An ingestion source.

    `provider_key` indexes the credential catalog, which decides the licence
    class of everything this adapter produces. `source` is what lands in the
    claim's provenance.

    Fetching is injectable rather than inherited: every adapter takes a
    `fetch_fn` so its parsing can be tested against a recorded payload with no
    network. That is the one property that made v1's warehouse layer the only
    tested part of its ingestion code.
    """

    source: str
    provider_key: str

    async def fetch(self, key: str) -> list[ClaimDraft]: ...


Fetcher = Callable[..., Awaitable[Any]]
