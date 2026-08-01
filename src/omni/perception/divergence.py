"""Perception/fundamentals divergence -- the first derived claim type.

Fundamentals improving while perception deteriorates (or the reverse) is the
finding a good analyst hunts for and no single-domain dashboard surfaces. It is
only computable because perception and fundamentals live in one store.

The rolling-z-score detection is delegated to the existing engine
``omni.perception.dynamics.SentimentDynamicsModel._analyze_sentiment_divergence``
(ported from v1). This module adapts claim series into the DataFrames that
engine expects, classifies the direction under this work order's convention,
and writes the resulting claim with its provenance edges in one transaction.

The licence rule (migration 002): a divergence derived from a ``byo_only``
series is itself ``byo_only`` and scoped to that series' owner. It is resolved
from the inputs' own ``redistributable`` / ``audience_user_id`` -- never from a
caller's guess -- because relying on the database to reject a bad write still
leaves the bug in any path that bypasses ``claim_input``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pandas as pd

from omni.ingest.protocol import ClaimDraft
from omni.perception.dynamics import SentimentDynamicsModel

CLAIM_TYPE = "perception_divergence"
KEY = "perception_vs_fundamentals"
SOURCE = "internal"

DEFAULT_WINDOW = 20

# The dynamics engine's significance band is
# ``divergence.rolling(window * 3).std() * 2``. The z-scores need ``window``
# aligned observations to be defined, and the band needs ``3 * window`` more
# points of divergence history. Below this floor the band is undefined, the
# engine cannot honestly report a divergence, and we return None rather than
# emit a low-confidence guess.
MIN_HISTORY = 4 * DEFAULT_WINDOW

# A divergence_score is measured in standardized units (a difference of two
# rolling z-scores). Four units of separation is treated as full confidence;
# below that, confidence scales linearly with the magnitude. Derived from the
# score, not hardcoded: a larger standardized gap is more certainly a
# divergence.
_CONFIDENCE_FULL = 4.0

_COLUMN = "v"


@dataclass(frozen=True)
class DivergenceInput:
    """A scalar observation pulled out of a claim for divergence maths.

    The caller extracts the scalar from the claim's JSONB ``value`` and brings
    the claim's own licence fields along, so the derived claim's audience can
    be resolved from inputs rather than guessed.
    """

    id: UUID
    event_date: datetime
    knowledge_date: datetime
    value: float
    redistributable: str
    audience_user_id: UUID | None


class ConflictingAudience(Exception):
    """byo_only inputs private to different users cannot blend into one claim."""


class ProhibitedInput(Exception):
    """A prohibited claim reached derivation. The schema forbids this; the
    input was constructed inconsistently."""


def resolve_derived_licence(
    inputs: list[DivergenceInput],
) -> tuple[str, UUID | None]:
    """Derive the licence class and audience from the inputs themselves.

    Returns ``(redistributable, audience_user_id)`` matching migration 001's
    CHECK: an allowed claim has no audience; a byo_only claim must carry one.

    The most restrictive input wins. One byo_only input among several allowed
    ones makes the whole derivation private to that input's owner, because the
    restricted content reached the result even if the raw data did not move.
    """

    for c in inputs:
        if c.redistributable == "prohibited":
            raise ProhibitedInput(
                "a prohibited claim cannot be an input to a derivation"
            )

    byo_owners = {
        c.audience_user_id
        for c in inputs
        if c.redistributable == "byo_only"
    }
    if len(byo_owners) > 1:
        raise ConflictingAudience(
            "cannot derive a divergence from byo_only inputs private to "
            f"different users: {sorted(str(u) for u in byo_owners)}"
        )
    if byo_owners:
        owner = next(iter(byo_owners))
        if owner is None:
            raise ConflictingAudience(
                "a byo_only input has no audience_user_id; the schema forbids "
                "this but the input was constructed inconsistently"
            )
        return "byo_only", owner
    return "allowed", None


def _to_frame(claims: list[DivergenceInput]) -> pd.DataFrame:
    """Observations indexed by event_date, deduplicated by averaging.

    The dynamics engine iterates columns and matches them by name between its
    two DataFrame arguments, so both frames use one shared column label.
    """

    if not claims:
        return pd.DataFrame(columns=[_COLUMN])
    df = pd.DataFrame(
        {
            "event_date": [c.event_date for c in claims],
            _COLUMN: [c.value for c in claims],
        }
    )
    df = df.dropna(subset=[_COLUMN])
    df = df.groupby("event_date", as_index=False)[_COLUMN].mean()
    return df.set_index("event_date").sort_index()[[_COLUMN]]


def _recompute_z(series: pd.Series, window: int) -> float:
    """The last point's rolling z-score, for evidence.

    The dynamics engine computes these internally but does not surface them.
    Reproduced with the same rolling formula so the evidence is checkable
    against the reported divergence_score (which is their difference).
    """

    mean = series.rolling(window).mean().iloc[-1]
    std = series.rolling(window).std().iloc[-1]
    if pd.isna(std) or std == 0:
        return 0.0
    return float((series.iloc[-1] - mean) / std)


def compute_divergence(
    perception_claims: list[DivergenceInput],
    fact_claims: list[DivergenceInput],
    *,
    window: int = DEFAULT_WINDOW,
) -> ClaimDraft | None:
    """Derive a ``perception_divergence`` draft, or None when undecided.

    Returns None when there is too little aligned history to form the
    dynamics engine's significance band, or when the engine finds no active
    divergence. Both are honest abstentions: a quiet week is a healthy
    outcome, and a guessed divergence is how fabricated coverage enters the
    store.
    """

    if not perception_claims or not fact_claims:
        return None

    perception = _to_frame(perception_claims)
    facts = _to_frame(fact_claims)

    min_history = 4 * window
    common = perception.index.intersection(facts.index)
    if len(common) < min_history:
        return None

    perception = perception.loc[common]
    facts = facts.loc[common]

    # The existing rolling-z-score divergence engine. Its signature expects
    # DataFrames with matching column names; both frames carry the single
    # shared label _COLUMN so the per-column loop runs once.
    result = SentimentDynamicsModel()._analyze_sentiment_divergence(
        perception, facts, window=window
    )

    active = result.get("active_divergences") or []
    if not active:
        return None

    finding = active[0]
    score = float(finding["divergence_score"])

    # Direction follows the level convention in the work order: perception
    # below fundamentals (score < 0) is bullish -- reality has improved beyond
    # what the market prices; perception above fundamentals (score > 0) is
    # bearish. The dynamics engine's own `type` uses an opposite,
    # momentum-based convention (sentiment improving + price falling), so it
    # is deliberately not used here; see W6.md for the adaptation note.
    if score > 0:
        direction = "bearish"
    elif score < 0:
        direction = "bullish"
    else:
        return None

    perception_z = _recompute_z(perception[_COLUMN], window)
    fact_z = _recompute_z(facts[_COLUMN], window)

    last_event = common[-1]
    if isinstance(last_event, pd.Timestamp):
        last_event_dt = last_event.to_pydatetime()
    else:
        last_event_dt = last_event

    # A derived claim cannot be knowable before the last thing it derives from.
    latest_knowledge = max(
        c.knowledge_date for c in (*perception_claims, *fact_claims)
    )

    all_inputs = (*perception_claims, *fact_claims)

    confidence = min(1.0, abs(score) / _CONFIDENCE_FULL)

    return ClaimDraft(
        claim_type=CLAIM_TYPE,
        key=KEY,
        value={"direction": direction, "score": round(score, 4)},
        event_date=last_event_dt,
        knowledge_date=latest_knowledge,
        confidence=round(confidence, 4),
        unit="z",
        evidence={
            "window": window,
            "perception_z": round(perception_z, 4),
            "fact_z": round(fact_z, 4),
            "direction": direction,
            "input_claim_ids": [str(c.id) for c in all_inputs],
        },
    )


_INSERT_DERIVED = """
INSERT INTO claim (entity_id, claim_type, key, value, unit, evidence, source,
                   event_date, knowledge_date, confidence, credential_owner,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6::jsonb,$7,$8,$9,$10,NULL,
        $11::redistribution,$12,$13::claim_derivation)
RETURNING id
"""

_INSERT_EDGE = "INSERT INTO claim_input (claim_id, input_id) VALUES ($1, $2)"


async def write_derived(
    pool,
    draft: ClaimDraft,
    *,
    entity_id: UUID,
    input_claim_ids: list[UUID],
    audience_user_id: UUID | None,
    redistributable: str,
    source: str = SOURCE,
) -> UUID:
    """Persist a derived claim and its ``claim_input`` edges.

    Both writes share one transaction. Migration 002's deferred constraint
    trigger rejects a ``derived`` claim with no declared inputs at commit, so
    splitting them would leave the derived claim un-committable.

    ``redistributable`` and ``audience_user_id`` must be the output of
    ``resolve_derived_licence`` applied to the same inputs -- not a guess.
    The per-edge trigger is the safety net if they are wrong.

    ``claim_type``, ``key``, ``unit``, ``value`` and ``evidence`` travel on
    ``draft``; ``source`` is the one field that is not on the draft, so it is
    a parameter (defaulting to the module's ``SOURCE`` for divergence).
    """

    async with pool.acquire() as conn, conn.transaction():
        claim_id = await conn.fetchval(
            _INSERT_DERIVED,
            entity_id,
            draft.claim_type,
            draft.key,
            json.dumps(draft.value),
            draft.unit,
            json.dumps(draft.evidence),
            source,
            draft.event_date,
            draft.knowledge_date,
            draft.confidence,
            redistributable,
            audience_user_id,
            "derived",
        )
        for input_id in input_claim_ids:
            await conn.execute(_INSERT_EDGE, claim_id, input_id)
    return claim_id
