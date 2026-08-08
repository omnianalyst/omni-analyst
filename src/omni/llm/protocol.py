"""The language-model seam -- and the one thing a model may never do here.

A language model is the largest fabrication surface this system will ever
have. Every other source can be wrong; a model is wrong *fluently*, and fluent
wrongness is indistinguishable from coverage right up until a prediction is
written against it. The rules below are structural rather than advisory,
because the two consumers this seam exists for -- the hypothesis loop and
finding narration -- are both under constant pressure to ask a model for "just
the number".

**There is no numeric output type.** A `ResponseSchema` is assembled from
exactly two field kinds: `TextField` (phrasing) and `ChoiceField` (selection
from options the caller wrote). There is deliberately no `NumberField`, so
there is no API through which a figure can be requested. A caller that needs a
number in the output passes it in as a `Measurement` -- a `Decimal` with a unit
and a named source -- and the model may only restate what it was handed.

**Restatement is enforced, not requested.** Every digit-bearing token in a
returned `TextField` must equal, as a `Decimal`, one of the measurements the
request carried. "revenue rose 43%" is refused when nothing measured 43. So is
"fell 3.2%" when the measurement is -3.2: a sign flip is the single most
damaging thing a narrator can do to a direction, and allowing an absolute-value
match to pass would license exactly that. The strictness costs a caller one
extra `Measurement` when it wants "Q3" in a sentence, which is the correct side
to err on.

**Failure raises.** `Unavailable` -- ingest's, not a second one with the same
job, so the fill pipeline's existing `unfillable` handling already covers a
model outage without knowing this package exists. An answer that does not
satisfy its schema raises `UnusableCompletion`, a subclass of it, and that
happens inside `Completion.__post_init__`: there is no sequence of calls that
yields a `Completion` object which was not validated, and no field whose
absence degrades to a blank string. Empty text is not an answer.

**The answer carries where it came from.** `Completion.provenance()` names the
model, the sha256 of the exact rendered prompt, the timestamp, the token usage
and the measurements that were in scope, and stamps `origin` as
`language_model` so a writer downstream can refuse to store any of it as a
measured claim.

Cost is `Decimal`, and may be `None`. Netting an LLM bill against edge measured
in basis points is a subtraction between two small numbers; binary float error
accumulated across a hypothesis loop lands in the number that decides whether a
strategy is worth running. `None` means the provider did not report a cost --
an adapter that substituted zero would make an unmetered budget look free,
which is the same defect as a fabricated price with a different unit.

This module declares no implementation. `fake.py` provides the deterministic
double. A concrete provider adapter is deferred until its wire protocol is
known; nothing here assumes one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from omni.ingest.protocol import Unavailable


class UnusableCompletion(Unavailable):
    """The model answered, but the answer is not one this system can use.

    A subclass of `Unavailable` on purpose. Every caller that already handles a
    source outage handles this too, without a second except clause someone can
    forget to write -- and forgetting it is what would leave a schema-violating
    answer propagating as if it were text a human approved.

    Distinct from `InvalidRequest`, which is raised before anything is sent:
    there the instruction we wrote is incoherent, here the provider's reply is.
    """


class InvalidRequest(Exception):
    """A request could not be constructed as stated.

    Raised at construction, so an incoherent request cannot reach a provider
    and cannot be billed for. Not an `Unavailable`: nothing is down, the
    caller's own instruction is malformed.
    """


# Every maximal digit-bearing run, sign and thousands separators included.
# Deliberately greedy about what counts as a number: a token this misses is a
# figure the model got to invent.
_NUMERIC_TOKEN = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def numeric_tokens(text: str) -> tuple[str, ...]:
    return tuple(_NUMERIC_TOKEN.findall(text))


def _as_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Measurement:
    """A figure the caller measured, handed to the model as an input.

    The only way a number reaches generated text. `value` must be a `Decimal`
    and not a float, for the same reason every price in `venue/protocol.py` is:
    the rendered form is what the model is allowed to echo, and 0.1 rendered
    from binary float is not the number the caller measured.

    `source` is required rather than optional because the completion's
    provenance carries it: a narrated sentence containing a figure must be
    traceable to the claim the figure came from, and a measurement that cannot
    say where it came from should not be put in front of a model at all.
    """

    name: str
    value: Decimal
    unit: str
    source: str
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidRequest("a measurement must be named")
        if not isinstance(self.value, Decimal):
            raise InvalidRequest(
                f"measurement {self.name!r} must carry a Decimal, got "
                f"{type(self.value).__name__}; a float here is a different "
                f"number than the one that was measured"
            )
        if not self.unit.strip():
            raise InvalidRequest(f"measurement {self.name!r} must carry a unit")
        if not self.source.strip():
            raise InvalidRequest(
                f"measurement {self.name!r} must name its source; an untraceable "
                f"figure must not be put in front of a model"
            )

    def render(self) -> str:
        stamp = f", as of {self.as_of.isoformat()}" if self.as_of is not None else ""
        return f"{self.name} = {self.value} {self.unit} (source: {self.source}{stamp})"


@dataclass(frozen=True)
class TextField:
    """A field the model may phrase freely, within a length the caller sets.

    Free phrasing, never free figures: `Completion` checks every numeric token
    in the returned value against the request's measurements.
    """

    name: str
    max_chars: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidRequest("an output field must be named")
        if self.max_chars <= 0:
            raise InvalidRequest(
                f"field {self.name!r} needs a positive max_chars, got {self.max_chars}"
            )

    def render(self) -> str:
        return f"{self.name}: free text, at most {self.max_chars} characters"


@dataclass(frozen=True)
class ChoiceField:
    """A field the model may only select from options the caller wrote.

    Two options minimum. A one-option choice is not a selection -- the caller
    already knows the answer, and asking for it buys a token bill and the
    illusion that a model agreed with something.
    """

    name: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidRequest("an output field must be named")
        if len(self.options) < 2:
            raise InvalidRequest(
                f"field {self.name!r} offers {len(self.options)} option(s); a "
                f"choice with fewer than two is not a selection"
            )
        if len(set(self.options)) != len(self.options):
            raise InvalidRequest(f"field {self.name!r} has duplicate options")
        if any(not option.strip() for option in self.options):
            raise InvalidRequest(f"field {self.name!r} has a blank option")

    def render(self) -> str:
        return f"{self.name}: choose exactly one of: {' | '.join(self.options)}"


OutputField = TextField | ChoiceField


@dataclass(frozen=True)
class ResponseSchema:
    """The shape of an answer this system can use.

    Note what cannot be expressed: there is no field kind that yields a number.
    That absence is the whole point of the type -- a consumer tempted to ask a
    model for a figure finds no way to write the request down.
    """

    fields: tuple[OutputField, ...]

    def __post_init__(self) -> None:
        if not self.fields:
            raise InvalidRequest("a response schema needs at least one field")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise InvalidRequest(f"duplicate field names in schema: {names}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field(self, name: str) -> OutputField:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def render(self) -> str:
        return "\n".join(f"- {f.render()}" for f in self.fields)


@dataclass(frozen=True)
class CompletionRequest:
    """One constrained call: what to do, what is known, what may come back.

    `measurements` is the entire numeric vocabulary of the answer. Anything not
    in it cannot legally appear in generated text, so a caller widens the
    vocabulary by measuring something, never by loosening a check.
    """

    task: str
    instructions: str
    schema: ResponseSchema
    measurements: tuple[Measurement, ...] = ()
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise InvalidRequest("a request must name its task")
        if not self.instructions.strip():
            raise InvalidRequest(f"task {self.task!r} carries no instructions")
        if self.max_output_tokens <= 0:
            raise InvalidRequest(
                f"max_output_tokens must be positive, got {self.max_output_tokens}"
            )
        names = [m.name for m in self.measurements]
        if len(set(names)) != len(names):
            raise InvalidRequest(f"duplicate measurement names: {names}")

    @property
    def allowed_numbers(self) -> frozenset[Decimal]:
        return frozenset(m.value for m in self.measurements)

    def render(self) -> str:
        """The exact prompt an adapter sends. Deterministic, order-preserving.

        Deterministic because `fingerprint` hashes it and that hash is the
        provenance record: two runs that produced different text must not be
        able to claim the same prompt.
        """
        measured = (
            "\n".join(f"- {m.render()}" for m in self.measurements)
            or "- none supplied"
        )
        return (
            f"TASK: {self.task}\n"
            f"\n"
            f"INSTRUCTIONS:\n"
            f"{self.instructions}\n"
            f"\n"
            f"MEASURED VALUES. These are the only figures that may appear in your\n"
            f"answer. Do not compute, convert, round, or estimate a new one.\n"
            f"{measured}\n"
            f"\n"
            f"ANSWER WITH EXACTLY THESE FIELDS, AND NOTHING ELSE:\n"
            f"{self.schema.render()}\n"
        )

    @property
    def fingerprint(self) -> str:
        return sha256(self.render().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenUsage:
    """What a call consumed, and what it cost -- or that the cost is unknown.

    `cost_usd` is `None` when the provider did not report one. Zero would be a
    claim that the call was free, and a budget netted against basis points of
    edge cannot tell a free call apart from an unmeasured one unless the type
    can say "unknown".
    """

    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError(
                f"token counts must not be negative: prompt={self.prompt_tokens} "
                f"completion={self.completion_tokens}"
            )
        if self.cost_usd is None:
            return
        if not isinstance(self.cost_usd, Decimal):
            raise TypeError(
                f"cost_usd must be a Decimal or None, got "
                f"{type(self.cost_usd).__name__}; this figure is netted against "
                f"edge in basis points"
            )
        if self.cost_usd < 0:
            raise ValueError(f"cost_usd must not be negative: {self.cost_usd}")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    """A validated answer, or no object at all.

    Every check lives in `__post_init__`, so holding one of these is proof the
    answer satisfied its schema and introduced no figure. A provider adapter
    cannot opt out of that by constructing the object differently, and neither
    can the test double.
    """

    request: CompletionRequest
    model: str
    created_at: datetime
    fields: dict[str, str]
    usage: TokenUsage

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise UnusableCompletion(
                f"task {self.request.task!r}: the answer does not say which model "
                f"produced it, so nothing written from it could be traced"
            )
        if self.created_at.tzinfo is None:
            raise UnusableCompletion(
                f"task {self.request.task!r}: created_at is naive; a completion "
                f"dated in an unstated zone cannot be ordered against a claim"
            )
        self._validate_fields()

    def _validate_fields(self) -> None:
        expected = set(self.request.schema.names)
        got = set(self.fields)
        if missing := sorted(expected - got):
            raise UnusableCompletion(
                f"task {self.request.task!r}: answer is missing {missing}; a "
                f"missing field is a refusal, not an empty string"
            )
        if extra := sorted(got - expected):
            raise UnusableCompletion(
                f"task {self.request.task!r}: answer carries unrequested fields "
                f"{extra}"
            )
        for spec in self.request.schema.fields:
            value = self.fields[spec.name]
            if not isinstance(value, str):
                raise UnusableCompletion(
                    f"task {self.request.task!r}: field {spec.name!r} is "
                    f"{type(value).__name__}, not text"
                )
            if isinstance(spec, ChoiceField):
                self._validate_choice(spec, value)
            else:
                self._validate_text(spec, value)

    def _validate_choice(self, spec: ChoiceField, value: str) -> None:
        if value not in spec.options:
            raise UnusableCompletion(
                f"task {self.request.task!r}: field {spec.name!r} answered "
                f"{value!r}, which is not one of {list(spec.options)}"
            )

    def _validate_text(self, spec: TextField, value: str) -> None:
        if not value.strip():
            raise UnusableCompletion(
                f"task {self.request.task!r}: field {spec.name!r} is empty; an "
                f"empty answer must refuse, not read as a short one"
            )
        if len(value) > spec.max_chars:
            raise UnusableCompletion(
                f"task {self.request.task!r}: field {spec.name!r} is "
                f"{len(value)} characters, over the {spec.max_chars} asked for"
            )
        allowed = self.request.allowed_numbers
        for token in numeric_tokens(value):
            number = _as_decimal(token)
            if number is None or number not in allowed:
                raise UnusableCompletion(
                    f"task {self.request.task!r}: field {spec.name!r} states the "
                    f"figure {token!r}, which no measurement in this request "
                    f"carries; a number in generated text must be one that was "
                    f"measured and passed in"
                )

    def provenance(self) -> dict[str, Any]:
        """Everything needed to trace a sentence back to what produced it.

        `origin` is stamped so a writer downstream can refuse this on sight:
        generated text is evidence about phrasing, never a measurement.
        """
        return {
            "origin": "language_model",
            "model": self.model,
            "task": self.request.task,
            "prompt_sha256": self.request.fingerprint,
            "created_at": self.created_at.isoformat(),
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "cost_usd": None if self.usage.cost_usd is None else str(self.usage.cost_usd),
            "measurements": [
                {
                    "name": m.name,
                    "value": str(m.value),
                    "unit": m.unit,
                    "source": m.source,
                }
                for m in self.request.measurements
            ],
        }


@runtime_checkable
class LanguageModel(Protocol):
    """Something that answers a constrained request, or refuses.

    One method, and no free-form chat method. What this system needs from a
    model is a filled-in schema; an open `chat(prompt) -> str` would be the
    hole every rule in this module is written to close, because its return type
    cannot be checked against anything.

    `complete` raises `Unavailable` when the provider cannot answer and
    `UnusableCompletion` when it answered something unusable. It never returns
    a partial, empty, or defaulted `Completion` -- the type cannot represent
    one.
    """

    name: str

    async def complete(self, request: CompletionRequest) -> Completion: ...
