"""The probability elicitation request, built from the LLM protocol's existing
field kinds.

`omni.llm.protocol` declares no numeric output type — `TextField` and
`ChoiceField` only. Extending it with a `ProbabilityField` is out of scope for
this work order and would violate the protocol's stated anti-fabrication
invariant. The fit-for-purpose path is a `ChoiceField` whose options are ten
probability bin centres; the model selects one and the probability is one we
wrote down.

The bin centres double as `calibration_with_benchmark`'s decile bucket edges,
so elicitation and calibration share a grain. The bucket a market lands in at
estimate time is the same bucket it lands in at report time — no resampling,
no mid-bin drift.

**No free-form probability.** The model may not write "around 0.7" or "~3:1
against": those answers fail `ChoiceField` validation inside `Completion` and
raise `UnusableCompletion`. The estimator never has to defend against a
creative probability string, because the protocol already did.
"""

from __future__ import annotations

from datetime import datetime
from typing import final

from omni.llm.protocol import (
    ChoiceField,
    CompletionRequest,
    Measurement,
    ResponseSchema,
)
from omni.polymarket.types import Document, MarketAtCutoff

PROBABILITY_BINS: tuple[str, ...] = (
    "0.05", "0.15", "0.25", "0.35", "0.45",
    "0.55", "0.65", "0.75", "0.85", "0.95",
)

UNCERTAIN_BIN: str = "0.5"

PROBABILITY_FIELD = "probability_bin"

_TASK_TEMPLATE = (
    "The market is currently pricing YES at {market_pct:.1f}%. Your task: "
    "estimate your OWN probability for YES, anchored on the market's price as "
    "your prior. The market has not resolved yet; you are standing at the "
    "cutoff moment named in the instructions.\n"
    "\n"
    "If your evidence is consistent with the market, your bin should be near "
    "{market_pct:.1f}%. If you have specific evidence the market is "
    "UNDERPRICING YES, choose a higher bin. If you have specific evidence the "
    "market is OVERPRICING YES, choose a lower bin. Absent evidence, default "
    "to the bin closest to {market_pct:.1f}% — not to {uncertain}."
)

_INSTRUCTIONS_TEMPLATE = (
    "Market question: {question}\n"
    "Category: {category}\n"
    "Cutoff (you are standing at this instant; later events are unknowable): {cutoff}\n"
    "Market YES price at cutoff: {market_price} (the prior you are anchored on)\n"
    "\n"
    "RULES:\n"
    "1. Reason ONLY from the documents in MEASURED VALUES and the supplied "
    "evidence. Do not recall the actual outcome from training.\n"
    "2. Choose one probability bin. Your bin is your degree of agreement or "
    "disagreement with the market's price.\n"
    "3. Output ONLY the probability bin. No rationale, no explanation, no "
    "other text — a single token from the choice list.\n"
)

_EVIDENCE_TEMPLATE = (
    "EVIDENCE (each item's `at` is when it became knowable; cutoff is {cutoff}):\n"
    "{items}"
)


def _format_evidence(docs: tuple[Document, ...], cutoff: datetime) -> str:
    if not docs:
        return "- none supplied"
    items = [
        f"- [{d.at.isoformat()}] source={d.source}: {d.text}" for d in docs
    ]
    return _EVIDENCE_TEMPLATE.format(cutoff=cutoff.isoformat(), items="\n".join(items))


def _instructions(snap: MarketAtCutoff) -> str:
    return _INSTRUCTIONS_TEMPLATE.format(
        question=snap.market.question,
        category=snap.market.category,
        cutoff=snap.cutoff.isoformat(),
        market_price=f"{snap.market_probability:.4f}",
        uncertain=UNCERTAIN_BIN,
    )


def _task(snap: MarketAtCutoff) -> str:
    """The task description, with the market's current price as an explicit
    prior. Substituting the percentage into the task itself (caller-written
    text, exempt from the restatement check) makes the anchor unmissable; the
    model sees the same number three times: in the task, in the instructions,
    and in the MEASURED VALUES block."""
    return _TASK_TEMPLATE.format(
        market_pct=snap.market_probability * 100.0,
        uncertain=UNCERTAIN_BIN,
    )


def build_request(snap: MarketAtCutoff) -> CompletionRequest:
    """Build the constrained request for one market at its cutoff.

    Output schema is a single `ChoiceField` for the probability bin. There is
    deliberately no `TextField` for rationale: a free-text field is an
    invitation for the model to mention figures (market IDs, dates, prices)
    that fail the protocol's restatement check. The calibration only needs
    the bin, so the schema asks for the bin alone — a field that cannot be
    used to smuggle in a fabricated number.

    The task is **anchored on the market's price**. The model is told the
    market's current YES price explicitly and asked whether it agrees or
    disagrees. This is the single highest-leverage elicitation change for LLM
    probability calibration (Tetlock-style anchoring-and-adjusting); without
    it the model defaults to the uncertain bin too often.

    `Measurement` carries the market's benchmark probability. The model cannot
    output it directly (the only field is a `ChoiceField`), but the request
    records it as a measured input for the calibration report's traceability.

    `max_output_tokens` is sized for `thinking=max`: the thinking budget
    consumes output tokens before the visible answer, so 8192 is the floor
    that survived empirical testing (2048 produced ~21% empty-completion
    exclusions on `thinking=max`; 8192 dropped the rate under 5%). For
    `thinking=none` or `auto` this is a generous ceiling, not a tight budget.
    """
    from decimal import Decimal

    benchmark_measurement = Measurement(
        name="market_yes_price_at_cutoff",
        value=Decimal(str(snap.market_probability)),
        unit="probability",
        source=f"polymarket:clob:{snap.market.yes_token_id or snap.market.condition_id}",
        as_of=snap.cutoff,
    )
    schema = ResponseSchema(
        fields=(
            ChoiceField(name=PROBABILITY_FIELD, options=PROBABILITY_BINS + (UNCERTAIN_BIN,)),
        )
    )
    return CompletionRequest(
        task=_task(snap),
        instructions=_instructions(snap) + "\n" + _format_evidence(snap.documents, snap.cutoff),
        schema=schema,
        measurements=(benchmark_measurement,),
        max_output_tokens=8192,
    )


@final
def parse_bin(raw_choice: str) -> float:
    """The choice the model returned, as a bin centre on [0, 1].

    `Completion` already validated `raw_choice` against the schema's options,
    so by the time we see it it is one of `PROBABILITY_BINS + (UNCERTAIN_BIN,)`
    exactly. We still coerce through `float` and re-check membership so a
    caller that bypassed `Completion` (a test, a replay) cannot smuggle a
    crafted string through.
    """
    if raw_choice not in PROBABILITY_BINS and raw_choice != UNCERTAIN_BIN:
        raise ValueError(
            f"choice {raw_choice!r} is not one of {PROBABILITY_BINS + (UNCERTAIN_BIN,)}; "
            f"a value outside this set was never a valid answer"
        )
    return float(raw_choice)
