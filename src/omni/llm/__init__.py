"""The language-model tier: constrained completion, and nothing else.

A model here selects and phrases; it never produces a figure. Numbers enter a
request as `Measurement` values the caller measured and leave in the same form
or not at all. See `protocol.py` for the protocol, the value objects and the
reasoning, and `fake.py` for the deterministic double. Failure is signalled
with ingest's `Unavailable`, so a model outage is handled by the code that
already handles a source outage.
"""

from omni.ingest.protocol import Unavailable
from omni.llm.fake import FAKE_CLOCK, FakeLanguageModel, ScriptedResponse
from omni.llm.protocol import (
    ChoiceField,
    Completion,
    CompletionRequest,
    InvalidRequest,
    LanguageModel,
    Measurement,
    OutputField,
    ResponseSchema,
    TextField,
    TokenUsage,
    UnusableCompletion,
    numeric_tokens,
)

__all__ = [
    "FAKE_CLOCK",
    "ChoiceField",
    "Completion",
    "CompletionRequest",
    "FakeLanguageModel",
    "InvalidRequest",
    "LanguageModel",
    "Measurement",
    "OutputField",
    "ResponseSchema",
    "ScriptedResponse",
    "TextField",
    "TokenUsage",
    "Unavailable",
    "UnusableCompletion",
    "numeric_tokens",
]
