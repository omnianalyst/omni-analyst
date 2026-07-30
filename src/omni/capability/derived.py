"""The derived capabilities that actually run today.

Sibling of ``builtin``: where that module binds ingestion adapters, this one
binds derivations -- claims computed from claims already in the store.
``perception.divergence`` is restated here as a **declaration**: two
``ArgumentSpec``s (``perception_macro`` and ``fundamental_metric``) plus the
existing ``compute_divergence``. ``fill_analysis`` materializes the arguments
through ``visible_claims`` (audience-scoped), abstains if either is short,
and calls the compute adapter.

The adapter (``compute_divergence_declared``) bridges the shape mismatch
between ``materialize``'s output and ``compute_divergence``'s input by building
``DivergenceInput`` lists from the provenance rows ``Materialized`` carries --
the dates and licence fields ``compute_divergence`` needs, read once through
``visible_claims`` during materialization rather than re-queried. See its
docstring for the detail.

``touches_byo=True`` is the safe-direction planning hint; the actual decision
is made per-fill by ``resolve_derived_licence`` over the materialized inputs.
``consumes`` is derived from the spec claim types, not hand-written.
"""

from __future__ import annotations

import json
from uuid import UUID

from omni.capabilities.macro import yield_curve_inversion
from omni.capability.arguments import ArgumentSpec, Materialized
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.visibility import visible_claims
from omni.fill.derived import DerivedCapability, fill_analysis
from omni.ingest.protocol import ClaimDraft, Unavailable
from omni.perception.divergence import (
    MIN_HISTORY,
    DivergenceInput,
    compute_divergence,
)

NAME = "perception.divergence"
PRODUCES = "perception_divergence"

# TODO(D9): neither spec sets ``key``. Divergence compares ONE perception
# series against ONE fundamental concept, yet a real entity carries several
# keys per claim_type (eight under ``fundamental_metric`` in the live DB); with
# no key, ``materialize`` blends them into one series and ``compute_divergence``
# emits a confident, wrong claim. The correct key is caller-supplied (which
# series pair to compare -- VIX vs Revenues, sentiment vs Earnings), not a
# fixed constant this capability can pick, so it is left unset pending a
# decision by whoever owns divergence. The single-key test fixtures (``vix``,
# ``Revenues``) are why this has never surfaced. The mechanism
# (``ArgumentSpec.key``) now exists; set it when the pairing is decided.
ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="perception_macro",
        claim_type="perception_macro",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
    ArgumentSpec(
        name="fundamental_metric",
        claim_type="fundamental_metric",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
)

CONSUMES = tuple(spec.claim_type for spec in ARGUMENTS)


def _scalar(raw) -> float | None:
    """Pull a float out of a claim's JSONB ``value``.

    asyncpg returns jsonb columns as text (no codec is set on the pool), so a
    string is decoded first. A null-valued observation -- FRED's "." for an
    unpublished period -- has no scalar to extract and is skipped: it is
    coverage of the period, not an input to a computation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    val = raw.get("value")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_inputs(rows) -> list[DivergenceInput]:
    out: list[DivergenceInput] = []
    for r in rows:
        val = _scalar(r["value"])
        if val is None:
            continue
        out.append(
            DivergenceInput(
                id=r["id"],
                event_date=r["event_date"],
                knowledge_date=r["knowledge_date"],
                value=val,
                redistributable=r["redistributable"],
                audience_user_id=r["audience_user_id"],
            )
        )
    return out


def _to_inputs_from_rows(rows) -> list[DivergenceInput]:
    """Build ``DivergenceInput`` lists from the provenance rows ``materialize``
    carried -- no re-read.

    Each row already carries the id, dates, post-transform value and licence
    fields ``compute_divergence`` needs. This is the single place the
    ``Materialized``-to-``DivergenceInput`` shape conversion happens, so a
    future analysis that hits the same mismatch puts its adapter here.
    """
    return [
        DivergenceInput(
            id=r.id,
            event_date=r.event_date,
            knowledge_date=r.knowledge_date,
            value=r.value,
            redistributable=r.redistributable,
            audience_user_id=r.audience_user_id,
        )
        for r in rows
    ]


async def compute_divergence_declared(
    pool,
    gap,
    *,
    perception_macro: Materialized,
    fundamental_metric: Materialized,
):
    """Declared-path compute: adapt ``Materialized`` to ``DivergenceInput`` lists.

    The per-observation provenance (id, dates, value, licence) is carried in
    ``Materialized.rows`` by ``materialize`` -- the post-transform, post-window
    set the value was actually computed from, read through ``visible_claims``
    (audience-scoped). This builds the ``DivergenceInput`` lists from those rows
    directly, so ``compute_divergence`` gets the same inputs the old re-read
    produced without a second query.
    """
    perc = _to_inputs_from_rows(perception_macro.rows)
    facts = _to_inputs_from_rows(fundamental_metric.rows)
    return compute_divergence(perc, facts)


async def gather_divergence_inputs(
    pool, gap
) -> tuple[list[DivergenceInput], list[DivergenceInput]]:
    """Load perception_macro and fundamental_metric claims visible to the gap's
    audience, as DivergenceInputs.

    The injected-gather escape hatch: kept as the template for a capability
    whose inputs the declaration cannot describe (multi-entity panels,
    caller-built structures). Divergence itself now uses the declared path.
    """
    audience: UUID | None = gap["audience_user_id"]
    entity_id = gap["entity_id"]

    perception_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[0]
    )
    fact_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[1]
    )
    return _to_inputs(perception_rows), _to_inputs(fact_rows)


def compute_divergence_claim(
    perception: list[DivergenceInput], facts: list[DivergenceInput]
):
    """Adapt ``compute_divergence`` to the escape-hatch fill path's
    ``(draft, ids) | None`` contract, declaring every input as an edge."""
    draft = compute_divergence(perception, facts)
    if draft is None:
        return None
    return draft, [c.id for c in (*perception, *facts)]


DERIVED = DerivedCapability(
    name=NAME,
    arguments=ARGUMENTS,
    compute=compute_divergence_declared,
)


# ----------------------------------------------------------- yield-curve signal
#
# The first earned claim type after perception_divergence (D2 step 5). A derived
# STATE -- "is the 2Y/10Y curve inverted right now" -- not a horizon-parameterized
# computation, so it earns a claim type: macro.recession_probability consumes the
# inversion flag, and the signal has a real freshness policy. The compute is the
# existing (unchanged) ``macro.yield_curve_inversion``; this declaration gives its
# output a durable home in the coverage store.
#
# v1 sources this from FRED ``DGS2``/``DGS10`` (confirmed in
# ../software/backend/app/workers/signal_fusion_tasks.py:349-350); those series
# ids are the ArgumentSpec keys (the exact purpose D9's ``key`` field exists for:
# two series sharing one claim_type, ``macro_series_point``, distinguished here).

YC_NAME = "macro.yield_curve_signal"
YC_PRODUCES = "yield_curve_signal"
YC_KEY = "2y_10y"

# ``yield_curve_inversion`` looks at ``common_dates[-252:]`` (one trading year of
# daily yields) and counts inversions over ``spreads[-90:]``. ``window`` passes
# the function its full intended lookback when that much history exists;
# ``min_obs`` is the floor below which the 90-day inverted count would be
# silently truncated -- a 45-day count wearing a "90d" label is the fabrication
# vector AGENTS.md warns against. 90, not 252: the headline output
# (``days_inverted_90d``, ``is_inverted``) is fully meaningful at 90 common
# dates; demanding a full trading year would abstain for any entity younger than
# that when the recession signal is already honest at ~four months of data.
YC_WINDOW = 252
YC_MIN_OBS = 90

YC_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="series_2y",
        claim_type="macro_series_point",
        key="DGS2",
        shape="series",
        transform="level",
        window=YC_WINDOW,
        min_obs=YC_MIN_OBS,
    ),
    ArgumentSpec(
        name="series_10y",
        claim_type="macro_series_point",
        key="DGS10",
        shape="series",
        transform="level",
        window=YC_WINDOW,
        min_obs=YC_MIN_OBS,
    ),
)

YC_CONSUMES = tuple(spec.claim_type for spec in YC_ARGUMENTS)


async def compute_yield_curve_signal_declared(
    pool, gap, *, series_2y: Materialized, series_10y: Materialized
):
    """Declared-path compute: adapt ``Materialized`` to the two
    ``dict[date, float]`` arguments ``yield_curve_inversion`` expects.

    ``yield_curve_inversion`` takes series shaped as dicts keyed by date, not
    the ordered ``Materialized`` shape ``materialize`` returns -- the same kind
    of shape mismatch ``compute_divergence_declared`` bridges for
    ``DivergenceInput`` lists. The adapter lives here, next to that one, not in
    ``arguments.py`` (per D5/D10): the shape conversion is specific to this one
    analysis function's signature.

    Returns a ``ClaimDraft`` of ``claim_type="yield_curve_signal"`` whose
    ``value`` is the durable state a consumer reads (spread, inversion flag,
    90-day count) and whose ``evidence`` carries the recent spread trajectory
    (dates serialized for JSONB) and the input claim ids. The bulky
    ``historical_spreads`` is evidence, not value: it is supporting detail
    reconstructable from the inputs, not the headline signal a consumer joins on.
    Abstains (``None``) when fewer than 90 common dates survive -- the function's
    ``days_inverted_90d`` is a count over ``spreads[-90:]`` and silently shrinks
    below that, so the per-series ``min_obs`` floor is backed by a check on the
    actual intersection.
    """
    d2y = {r.event_date: r.value for r in series_2y.rows}
    d10y = {r.event_date: r.value for r in series_10y.rows}

    common = set(d2y) & set(d10y)
    if len(common) < YC_MIN_OBS:
        return None

    try:
        result = await yield_curve_inversion(d2y, d10y)
    except Unavailable:
        return None

    last_event = max(common)
    latest_knowledge = max(
        r.knowledge_date for r in (*series_2y.rows, *series_10y.rows)
    )

    historical = [
        {
            "date": s["date"].isoformat(),
            "spread": s["spread"],
            "inverted": s["inverted"],
        }
        for s in result["historical_spreads"]
    ]

    return ClaimDraft(
        claim_type=YC_PRODUCES,
        key=YC_KEY,
        value={
            "current_spread": result["current_spread"],
            "is_inverted": result["is_inverted"],
            "days_inverted_90d": result["days_inverted_90d"],
        },
        event_date=last_event,
        knowledge_date=latest_knowledge,
        confidence=1.0,
        unit="percent",
        evidence={
            "series": ["DGS2", "DGS10"],
            "historical_spreads": historical,
            "input_claim_ids": [
                str(r.id) for r in (*series_2y.rows, *series_10y.rows)
            ],
        },
    )


YIELD_CURVE = DerivedCapability(
    name=YC_NAME,
    arguments=YC_ARGUMENTS,
    compute=compute_yield_curve_signal_declared,
)


def build_derived_registry() -> Registry:
    """Register the derived capabilities that are wired and tested today."""
    registry = Registry()

    async def call(pool, gap):
        return await fill_analysis(pool, gap, capability=DERIVED)

    async def call_yield_curve(pool, gap):
        return await fill_analysis(pool, gap, capability=YIELD_CURVE)

    registry.add(
        Capability(
            name=NAME,
            description=(
                "Cross-domain divergence: fundamentals improving while "
                "perception deteriorates (or the reverse). Computable only "
                "because perception and fundamentals live in one store."
            ),
            consumes=CONSUMES,
            produces=(PRODUCES,),
            touches_byo=True,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.perception.divergence",
            call=call,
        )
    )
    registry.add(
        Capability(
            name=YC_NAME,
            description=(
                "2Y/10Y treasury yield-curve inversion signal: current spread, "
                "inversion flag and 90-day days-inverted count, derived from "
                "DGS2/DGS10 macro_series_point claims."
            ),
            consumes=YC_CONSUMES,
            produces=(YC_PRODUCES,),
            touches_byo=False,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.capabilities.macro.yield_curve_inversion",
            call=call_yield_curve,
        )
    )
    return registry
