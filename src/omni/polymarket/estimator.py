"""Run the elicitation request against a `LanguageModel`, return an estimation.

The estimator owns one decision: how a chosen bin becomes a `(direction,
confidence)` pair. That mapping lives in one function, examined by one test
class, so a convention change cannot drift between call sites.

Convention:

    bin >= 0.5  ->  direction = "up"    confidence = bin
    bin <  0.5  ->  direction = "down"  confidence = 1.0 - bin

So a 0.65 bin reads "YES at 0.65"; a 0.25 bin reads "NO at 0.75" (1.0 - 0.25).
Confidence always lies in [0.5, 0.95] — never below 0.5 because a sub-half
answer inverts into a >-half answer in the opposite direction. A model that
cannot tell either way picks 0.5; the resulting estimation is `up` with
`confidence = 0.5`, which the calibration bucket at [0.5, 0.6) handles
correctly.
"""

from __future__ import annotations

import uuid

from omni.llm.protocol import LanguageModel
from omni.polymarket.elicitation import PROBABILITY_FIELD, build_request, parse_bin
from omni.polymarket.types import Estimation, MarketAtCutoff


def _direction_and_confidence(bin_value: float) -> tuple[str, float]:
    if bin_value >= 0.5:
        return "up", bin_value
    return "down", 1.0 - bin_value


async def estimate(
    model: LanguageModel,
    snap: MarketAtCutoff,
) -> Estimation:
    """One LLM call -> one `Estimation`, or raise.

    `LanguageModel.complete` raises `Unavailable` on provider outage and
    `UnusableCompletion` on a schema-violating answer. Both propagate: an
    estimator that swallowed them would let a failed call read as a 0.5
    answer, which is precisely the "ignorance default" being reserved for
    genuine uncertainty.
    """
    request = build_request(snap)
    completion = await model.complete(request)
    raw_choice = completion.fields[PROBABILITY_FIELD]
    bin_value = parse_bin(raw_choice)
    direction, confidence = _direction_and_confidence(bin_value)
    return Estimation(
        chosen_bin=bin_value,
        direction=direction,
        confidence=confidence,
        raw_choice=raw_choice,
        market_id=snap.market.condition_id,
        cutoff=snap.cutoff,
    )


def estimation_id(est: Estimation) -> str:
    """A deterministic-ish id for the pure calibration function's `id` slot.

    `calibration_with_benchmark` reads `str(p.id)` to join predictions to
    benchmarks. The id has no semantic role here (Stage A has no DB), but the
    join key still has to be unique per estimation: two markets with the same
    id would alias in the benchmark map and one benchmark would be applied to
    the other. `market_id + cutoff + uuid4` is unique because `uuid4` is.
    """
    return f"{est.market_id}:{int(est.cutoff.timestamp())}:{uuid.uuid4().hex}"
