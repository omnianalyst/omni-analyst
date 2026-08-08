"""A language model that generates nothing, deterministically.

The double a test asserts against has to be *less* capable than the provider,
never more. Two properties matter:

**It is not a bypass.** The fake builds a real `Completion`, so a scripted
answer that violates its schema raises exactly as a provider's would. A double
that returned its script unchecked would let a test prove the validation it was
written to prove, while the validation was off.

**It reads no clock and draws no randomness.** `created_at` is a constant the
caller may set, and the script is consumed in order. Two identical runs produce
identical objects, including provenance -- which is what makes a fingerprint
assertion meaningful rather than a snapshot of whatever the machine's clock
said.

Zero tokens and an unknown cost are the honest defaults here: the fake really
did consume nothing and really has no bill to report. That is a different
statement from a provider adapter defaulting a cost to zero, which this package
refuses.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from omni.llm.protocol import (
    Completion,
    CompletionRequest,
    TokenUsage,
)

# A fixed instant, not "now". Every completion the fake produces is dated here
# unless the caller says otherwise.
FAKE_CLOCK = datetime(2020, 1, 1, tzinfo=UTC)

NO_USAGE = TokenUsage(prompt_tokens=0, completion_tokens=0, cost_usd=None)


@dataclass(frozen=True)
class ScriptedResponse:
    """One answer the fake will hand back, in script order."""

    fields: dict[str, str] = field(default_factory=dict)
    usage: TokenUsage = NO_USAGE


class FakeLanguageModel:
    """A `LanguageModel` that returns scripted answers and records its prompts.

    `raises` makes every call fail with the given exception, so the refusal
    path is exercised with the same object under test. The request is recorded
    *before* the raise: a call that failed still happened, and a caller
    asserting "we asked, and were refused" needs to see what was asked.

    Running off the end of the script raises `RuntimeError`, not `Unavailable`.
    An exhausted script is a defect in the test, and dressing it as a provider
    outage would let a test exercise the refusal path while believing it was
    exercising the answer path.
    """

    def __init__(
        self,
        responses: Sequence[ScriptedResponse] = (),
        *,
        name: str = "fake",
        model: str = "fake-model",
        raises: Exception | None = None,
        created_at: datetime = FAKE_CLOCK,
    ) -> None:
        if raises is not None and not isinstance(raises, Exception):
            raise TypeError(f"raises must be an exception instance, got {raises!r}")
        self.name = name
        self.model = model
        self.requests: list[CompletionRequest] = []
        self._responses = list(responses)
        self._raises = raises
        self._created_at = created_at
        self._index = 0

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def prompts(self) -> list[str]:
        return [r.render() for r in self.requests]

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        if self._index >= len(self._responses):
            raise RuntimeError(
                f"FakeLanguageModel script exhausted: call "
                f"{self._index + 1} for task {request.task!r} has no scripted "
                f"response"
            )
        scripted = self._responses[self._index]
        self._index += 1
        return Completion(
            request=request,
            model=self.model,
            created_at=self._created_at,
            fields=dict(scripted.fields),
            usage=scripted.usage,
        )
