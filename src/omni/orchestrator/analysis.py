"""Name-keyed analysis invocation: call an analysis by name, return its result.

Both planners and the fill dispatcher select only through
``registry.producing(claim_type)``, and ~110 of the 113 extracted analyses have
``produces=()`` (a Sharpe ratio, a risk score, an IC have no home in the closed
``claim_type`` enum). ``run.execute`` compounds it by calling
``capability.call(step.target)`` -- the objective's target string -- which fits
a builtin adapter's single-identifier signature but not
``sharpe_ratio(*, returns, ...)`` or ``overall_risk_score(*, market_score, ...)``.

This module is the name-keyed path D2 step 6 specified: given a capability
name, an entity and an audience, materialize the analysis's declared
``ArgumentSpec`` s from coverage, call its compute function, and return the
result plus its provenance and licence verdict. No persistence -- no claim, no
finding, no gap.

Only capabilities with declared ``ArgumentSpec`` s are callable here. Today
that is ``perception.divergence`` (claim-producing, ``_DECLARED_ANALYSES``) and
``market_risk.credit_risk`` (non-claim, ``_NON_CLAIM_ANALYSES``). A capability
without declared arguments in either map is refused with a reason, never
silently falling back to ``capability.call(target)`` -- the defect this path
exists to avoid.

Abstention is honest: if any argument's materialization returns ``Abstention``,
the compute function is not called and the shortfall is returned naming which
argument was short and by how much. This is the same contract ``fill_analysis``
follows, and it does not degrade into calling the function with a padded or
defaulted argument.

The licence verdict reuses ``resolve_derived_licence`` over the materialized
inputs -- the same rule that governs derived claims, not a new one.

A composite (``market_risk.overall_risk_score``) declares its arguments as
``AnalysisOutputSpec`` s instead of ``ArgumentSpec`` s: each names a sibling
capability whose output (its ``score``) feeds the composite's argument. The
sibling is resolved by running the same materialize-and-compute machinery
recursively, so abstention propagates (a sub-analysis short of ``min_obs``
abstains the whole composite) and licence composes to the most restrictive
input across all sub-analyses transitively -- through the same
``resolve_derived_licence``, not a second rule. Cycles and excessive depth are
refused with a clear error (``MAX_COMPOSITE_DEPTH``) rather than recursing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from neutron.error import bad_request, not_found

from omni.capability.arguments import (
    Abstention,
    AnalysisOutputSpec,
    ArgumentSpec,
    Materialized,
    materialize,
)
from omni.capability.derived import DERIVED
from omni.capability.registry import Registry
from omni.capabilities.macro import recession_probability
from omni.capabilities.risk import analyze_credit_risk, calculate_overall_risk_score
from omni.fill.derived import DerivedCapability
from omni.perception.divergence import resolve_derived_licence

# The deepest composite chain this module will resolve before refusing.
# ``overall_risk_score`` is depth 2 (composite -> sub-analysis -> claims); a
# chain of composites would be depth 3+. No legitimate graph exceeds a handful
# of levels, so 8 is generous headroom that still catches runaway recursion
# before Python's own stack limit. At the limit the call is REFUSED with a
# ``bad_request`` error, not an abstention: hitting it means the dependency
# graph is pathologically deep (or a cycle the set-based guard missed), and an
# abstention would disguise a structural defect as missing data.
MAX_COMPOSITE_DEPTH = 8

# The set of capabilities whose inputs are declared as ArgumentSpecs and whose
# compute function takes (pool, context, **materialized). Today this is the
# single derived capability that has been restated as a declaration; every
# future analysis migrated to ArgumentSpec declarations is added here. A
# capability not in this map is refused with a reason rather than silently
# called through capability.call(target).
_DECLARED_ANALYSES: dict[str, DerivedCapability] = {
    DERIVED.name: DERIVED,
}


@dataclass(frozen=True)
class DeclaredAnalysis:
    """A non-claim analysis assembled from coverage -- like ``DerivedCapability``
    but without the claim-writing contract.

    ``DerivedCapability`` (``fill/derived.py``) is claim-writing machinery: its
    ``compute`` returns a ``ClaimDraft | None`` and ``fill_analysis`` persists
    it through ``write_derived``. A credit-risk score is the opposite case -- a
    policy-banded read of two spreads with no natural claim type (like every
    other ``market_risk.*`` composite), so it never writes a claim. This narrower
    type carries the same declared-input contract (an ``ArgumentSpec`` tuple
    materialized through ``visible_claims``) but a ``compute`` that returns a
    plain ``dict | None``. Stretching ``DerivedCapability`` to cover it would
    loosen a claim-writing contract to admit a result that is never a claim.

    No ``gather`` escape hatch. The escape hatch in ``DerivedCapability`` exists
    for structures the declaration cannot describe (multi-entity panels,
    caller-built inputs). Credit risk reads two named scalar series, each fully
    pinned by its ``ArgumentSpec`` (``claim_type`` + ``key`` selects exactly one
    series); there is no panel or caller-built structure to escape to, so a
    ``gather`` here would be dead machinery.
    """

    name: str
    arguments: tuple[ArgumentSpec | AnalysisOutputSpec, ...]
    compute: Callable[..., dict | None]


# FRED publishes the ICE BofA US Corporate Index OAS as ``BAMLC0A0CM`` and the
# ICE BofA US High Yield Index OAS as ``BAMLH0A0HYM2`` -- both ``allowed``
# ``macro_series_point`` series from ``fred.series``. Both ids were confirmed
# against FRED before use: ``BAMLC0A0CM`` and ``BAMLH0A0HYM2`` resolve; the work
# order's ``BAMLH0A0HYM`` (no trailing 2) returns a FRED 404 -- the high-yield
# OAS id carries the 2.
#
# ``min_obs=1``: the function wants one current spread level (scalar), so the
# count floor is one observation -- fewer than one means no spread exists and
# the call abstains. ``ArgumentSpec`` has no date/freshness field, so the
# declaration cannot refuse a stale latest observation by age; the scalar shape
# takes the most recent observation by event_date, and whether that latest is
# fresh enough for a live read is the producer's responsibility, not something
# the declaration can enforce. A higher ``min_obs`` would not improve recency
# (five points from a series that stopped publishing still yield a stale
# spread), so it would be a misleading proxy rather than an honest guard.
_CREDIT_RISK_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="ig_spread",
        claim_type="macro_series_point",
        key="BAMLC0A0CM",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
    ArgumentSpec(
        name="hy_spread",
        claim_type="macro_series_point",
        key="BAMLH0A0HYM2",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
)


async def _compute_credit_risk(
    *, ig_spread: Materialized, hy_spread: Materialized
) -> dict | None:
    return analyze_credit_risk(
        ig_spread=ig_spread.value,
        hy_spread=hy_spread.value,
    )


# ----------------------------------------------------------- recession_probability
#
# The first composite within reach (HANDOFF §6.3): a recession probability
# assembled from two earned claim types -- yield_curve_signal (D10) and
# sahm_rule_signal (D14) -- each consumed as a plain ArgumentSpec over the claim,
# NOT via the analysis_output seam. A claim is durable, provenanced and
# re-derivable; a passed sibling value is none of those, which is why §6.3 and
# D12's own report both reach this call independently.
#
# The function's third input (lei_signals) has no producer today, so it is not
# declared as an ArgumentSpec: materializing a claim type that nothing writes
# would abstain and make the composite uncallable, defeating the point. Instead
# lei_signals is passed as an empty list -- the honest "zero LEI signals
# available" -- and recession_probability's `if lei_signals:` correctly omits
# the 0.3 LEI term, so the result is a truthful 2-of-3 composite (probability in
# [0, 0.7]). When an LEI producer exists, a third ArgumentSpec replaces the []
# and the composite strengthens to [0, 1.0] with no other change.
#
# Each ArgumentSpec reads the boolean out of the producer's JSONB value:
# yield_curve_signal carries {"is_inverted": bool, ...}, sahm_rule_signal
# {"triggered": bool, ...}. value_field follows the dotted path and _extract_scalar
# does float() at the end (True -> 1.0, False -> 0.0), cast back to bool here.
# shape="scalar" takes the latest signal; min_obs=1 abstains when no signal of a
# type exists yet (so the composite is honest about a missing input rather than
# reading a stale or absent one as "no recession").
_RECESSION_PROBABILITY_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="yield_curve_inverted",
        claim_type="yield_curve_signal",
        value_field="is_inverted",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
    ArgumentSpec(
        name="sahm_triggered",
        claim_type="sahm_rule_signal",
        value_field="triggered",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
)


async def _compute_recession_probability(
    *, yield_curve_inverted: Materialized, sahm_triggered: Materialized
) -> dict | None:
    return await recession_probability(
        yield_curve_inverted=bool(yield_curve_inverted.value),
        sahm_triggered=bool(sahm_triggered.value),
        lei_signals=[],
    )


# ----------------------------------------------------------- overall_risk_score
#
# calculate_overall_risk_score takes five keyword-only floats -- market_score,
# economic_score, sentiment_score, correlation_score, geopolitical_score -- each
# the output ("score") of a sibling market_risk.* capability, not a claim. The
# five AnalysisOutputSpecs below name those siblings; at resolution time each is
# run first (recursively through _resolve_sibling_output) and its score feeds
# the composite's argument.
#
# NONE of the five sub-analyses is declarable from claims through ArgumentSpec
# today, so the composite abstains. That is the expected outcome for this
# order, not a failure. Each is assessed below; the assessments are part of the
# deliverable.
#
#   market_score -> market_risk.breadth (analyze_market_breadth): BLOCKED.
#     Inputs are breadth statistics (advance_decline_ratio,
#     percent_above_50ma/200ma, new_highs, new_lows) -- cross-section
#     derivations over a universe of price_snapshots with no claim source.
#
#   economic_score -> market_risk.growth_risk (analyze_growth_risk): BLOCKED.
#     gdp_growth and unemployment are macro_series_point scalars from FRED, but
#     job_growth is a payroll-change count (compared against 50000) that has no
#     direct FRED series -- it is a diff of PAYEMS with a x1000 unit conversion
#     ArgumentSpec's transform vocabulary cannot express.
#
#   sentiment_score -> market_risk.sentiment: BLOCKED. No capability produces a
#     sentiment score. The v1 sentiment analyzer (_analyze_sentiment_risks)
#     was deliberately not ported (it fabricated 0.5 on missing input). The
#     name below does not resolve to any declared analysis, so the composite
#     abstains naming it.
#
#   correlation_score -> market_risk.correlation_risks (analyze_correlation_
#     risks): BLOCKED. Takes returns_data: dict[str, Sequence[float]] -- a
#     multi-symbol panel. ArgumentSpec describes single-series and two-entity
#     aligned shapes, not an arbitrary dict-of-series keyed by symbol.
#
#   geopolitical_score -> market_risk.geopolitical_risks (analyze_geopolitical
#     _risks): BLOCKED. Takes articles: Sequence[dict] where each dict needs
#     title and summary. ArgumentSpec extracts scalar floats, not dicts.
#     news_event claims carry {"title","url","feed"} -- no summary field, and
#     the value is a JSONB object, not a scalar _extract_scalar can pull.
_OVERALL_RISK_ARGUMENTS: tuple[AnalysisOutputSpec, ...] = (
    AnalysisOutputSpec(name="market_score", capability="market_risk.breadth"),
    AnalysisOutputSpec(name="economic_score", capability="market_risk.growth_risk"),
    AnalysisOutputSpec(name="sentiment_score", capability="market_risk.sentiment"),
    AnalysisOutputSpec(
        name="correlation_score", capability="market_risk.correlation_risks"
    ),
    AnalysisOutputSpec(
        name="geopolitical_score", capability="market_risk.geopolitical_risks"
    ),
)


async def _compute_overall_risk(
    *,
    market_score: Materialized,
    economic_score: Materialized,
    sentiment_score: Materialized,
    correlation_score: Materialized,
    geopolitical_score: Materialized,
) -> dict | None:
    return calculate_overall_risk_score(
        market_score=market_score.value,
        economic_score=economic_score.value,
        sentiment_score=sentiment_score.value,
        correlation_score=correlation_score.value,
        geopolitical_score=geopolitical_score.value,
    )


_NON_CLAIM_ANALYSES: dict[str, DeclaredAnalysis] = {
    "market_risk.credit_risk": DeclaredAnalysis(
        name="market_risk.credit_risk",
        arguments=_CREDIT_RISK_ARGUMENTS,
        compute=_compute_credit_risk,
    ),
    "market_risk.overall_risk_score": DeclaredAnalysis(
        name="market_risk.overall_risk_score",
        arguments=_OVERALL_RISK_ARGUMENTS,
        compute=_compute_overall_risk,
    ),
    "macro.recession_probability": DeclaredAnalysis(
        name="macro.recession_probability",
        arguments=_RECESSION_PROBABILITY_ARGUMENTS,
        compute=_compute_recession_probability,
    ),
}


@dataclass(frozen=True)
class AnalysisShortfall:
    """One reason the analysis could not run, naming the argument that fell short."""

    argument: str
    reason: str


@dataclass(frozen=True)
class AnalysisResult:
    """The outcome of a name-keyed analysis invocation.

    ``abstained`` is the honest-refusal case: one or more arguments could not
    be materialized (or the compute function itself returned None), and no
    result was produced. ``shortfalls`` carries the reasons.

    On success, ``result`` is the compute function's output (a ``ClaimDraft``
    for claim-producing capabilities, a plain ``dict`` for non-claim ones),
    ``evidence`` is the contributing claim ids, and ``redistributable`` /
    ``audience_user_id`` are the licence verdict from ``resolve_derived_licence``
    over the inputs.
    """

    capability: str
    abstained: bool
    result: object | None = None
    evidence: tuple[str, ...] = ()
    redistributable: str | None = None
    audience_user_id: UUID | None = None
    shortfalls: tuple[AnalysisShortfall, ...] = ()


def _lookup_declared(name: str):
    """Return ``(arguments, compute, is_claim_path)`` or ``None``.

    Checks both the claim-producing (``_DECLARED_ANALYSES``) and non-claim
    (``_NON_CLAIM_ANALYSES``) registries. Used by ``run_analysis`` at the top
    level and by ``_resolve_sibling_output`` for each sub-analysis.
    """
    derived = _DECLARED_ANALYSES.get(name)
    if derived is not None and derived.arguments is not None:
        return derived.arguments, derived.compute, True
    analysis = _NON_CLAIM_ANALYSES.get(name)
    if analysis is not None:
        return analysis.arguments, analysis.compute, False
    return None


async def _materialize_all(
    arguments: tuple[ArgumentSpec | AnalysisOutputSpec, ...],
    registry: Registry,
    pool,
    *,
    entity_id: UUID,
    audience: UUID | None,
    seen: frozenset[str],
) -> tuple[dict[str, Materialized], list[Abstention], list]:
    """Materialize each argument (claim-sourced or sibling-output).

    For ``AnalysisOutputSpec`` arguments the sibling is resolved recursively
    through ``_resolve_sibling_output``, which packs the sibling's transitive
    ``claim_ids`` and ``rows`` into the returned ``Materialized`` -- so the
    caller's licence resolution sees every input claim transitively, not just
    the top-level ones. Returns ``(values, abstentions, rows)``.
    """
    values: dict[str, Materialized] = {}
    abstentions: list[Abstention] = []
    all_rows: list = []
    for spec in arguments:
        if isinstance(spec, AnalysisOutputSpec):
            result = await _resolve_sibling_output(
                spec, registry, pool,
                entity_id=entity_id, audience=audience, seen=seen,
            )
        else:
            result = await materialize(
                spec, pool, entity_id=entity_id, audience=audience
            )
        if isinstance(result, Abstention):
            abstentions.append(result)
        else:
            values[spec.name] = result
            all_rows.extend(result.rows)
    return values, abstentions, all_rows


async def _resolve_sibling_output(
    spec: AnalysisOutputSpec,
    registry: Registry,
    pool,
    *,
    entity_id: UUID,
    audience: UUID | None,
    seen: frozenset[str],
) -> Materialized | Abstention:
    """Run a sibling capability and extract its score as a ``Materialized``.

    The sibling is resolved through the same ``_materialize_all`` machinery,
    so its own sub-analyses (if any) are resolved transitively. Three
    properties hold:

    - **Abstention propagates.** If the sibling or any of its inputs abstain,
      the composite abstains naming which sibling and why -- never a default.
    - **Licence composes transitively.** The returned ``Materialized`` carries
      every ``ProvenanceRow`` the sibling consumed, so the caller's
      ``resolve_derived_licence`` sees the full transitive input set.
    - **Cycles and depth are refused** (``bad_request``), not abstained: a
      cycle is a structural defect, and disguising it as missing data would
      hide the bug.
    """
    cap_name = spec.capability

    if cap_name in seen:
        chain = " -> ".join([*seen, cap_name])
        raise bad_request(f"composite cycle detected: {chain}")

    if len(seen) >= MAX_COMPOSITE_DEPTH:
        raise bad_request(
            f"composite depth limit ({MAX_COMPOSITE_DEPTH}) exceeded resolving "
            f"{cap_name}"
        )

    new_seen = seen | {cap_name}

    decl = _lookup_declared(cap_name)
    if decl is None:
        return Abstention(
            spec.name,
            f"{cap_name} is not a declared analysis; "
            f"cannot resolve as a sub-analysis",
        )

    sibling_args, sibling_compute, is_claim_path = decl
    values, abstentions, rows = await _materialize_all(
        sibling_args, registry, pool,
        entity_id=entity_id, audience=audience, seen=new_seen,
    )

    if abstentions:
        reasons = "; ".join(a.reason for a in abstentions)
        return Abstention(spec.name, f"{cap_name} abstained: {reasons}")

    if is_claim_path:
        sibling_cap = registry.get(cap_name)
        context = {
            "entity_id": entity_id,
            "audience_user_id": audience,
            "claim_type": (
                sibling_cap.produces[0]
                if sibling_cap and sibling_cap.produces
                else ""
            ),
            "key": "",
        }
        computed = await sibling_compute(pool, context, **values)
    else:
        computed = await sibling_compute(**values)

    if computed is None:
        return Abstention(
            spec.name, f"{cap_name} compute returned no result"
        )

    if isinstance(computed, dict):
        val = computed.get(spec.result_key)
    elif hasattr(computed, "value") and isinstance(computed.value, dict):
        val = computed.value.get(spec.result_key)
    else:
        val = None

    if val is None or not isinstance(val, (int, float)):
        return Abstention(
            spec.name,
            f"{cap_name} produced no numeric {spec.result_key!r}",
        )

    return Materialized(
        value=float(val),
        claim_ids=tuple(r.id for r in rows),
        rows=tuple(rows),
    )


async def run_analysis(
    registry: Registry,
    pool,
    *,
    name: str,
    entity_id: UUID,
    audience: UUID | None,
) -> AnalysisResult:
    """Look up a capability by name, materialize its arguments, call it.

    No persistence: the result and its provenance are returned, not written.
    Raises ``not_found`` for an unknown name and ``bad_request`` for a
    registered-but-not-invocable capability or one that declares no arguments.
    For composites (capabilities whose arguments include
    ``AnalysisOutputSpec``), each sibling is resolved recursively with
    cycle/depth guards.
    """
    cap = registry.get(name)
    if cap is None:
        raise not_found(f"No capability named {name!r}")
    if not cap.invocable:
        raise bad_request(
            f"Capability {name!r} is registered but not invocable "
            f"(callability={cap.callability.value}, maturity={cap.maturity.value})"
        )

    decl = _lookup_declared(name)
    if decl is None:
        raise bad_request(
            f"Capability {name!r} declares no arguments; it cannot be assembled "
            f"from coverage"
        )

    arguments, compute, is_claim_path = decl

    seen = frozenset({name})
    materialized, abstentions, all_rows = await _materialize_all(
        arguments, registry, pool,
        entity_id=entity_id, audience=audience, seen=seen,
    )

    if abstentions:
        return AnalysisResult(
            capability=name,
            abstained=True,
            shortfalls=tuple(
                AnalysisShortfall(a.argument, a.reason) for a in abstentions
            ),
        )

    if is_claim_path:
        context = {
            "entity_id": entity_id,
            "audience_user_id": audience,
            "claim_type": cap.produces[0] if cap.produces else "",
            "key": "",
        }
        computed = await compute(pool, context, **materialized)
    else:
        computed = await compute(**materialized)

    if computed is None:
        count = sum(len(m.claim_ids) for m in materialized.values())
        return AnalysisResult(
            capability=name,
            abstained=True,
            shortfalls=(
                AnalysisShortfall(
                    "_compute",
                    f"compute abstained: {count} input claim(s) but no result",
                ),
            ),
        )

    input_claim_ids: list[UUID] = list(
        dict.fromkeys(cid for m in materialized.values() for cid in m.claim_ids)
    )

    # D7 folded the dates/licence fields materialize() already reads into
    # Materialized.rows, so the licence set is read straight off what was just
    # materialized rather than re-read by id. For composites, each
    # AnalysisOutputSpec's Materialized carries the sibling's transitive rows,
    # so all_rows already sees every input claim transitively.
    # resolve_derived_licence is duck-typed against
    # .redistributable/.audience_user_id, which ProvenanceRow carries.
    licence_inputs = all_rows
    redistributable, audience_resolved = resolve_derived_licence(licence_inputs)

    return AnalysisResult(
        capability=name,
        abstained=False,
        result=computed,
        evidence=tuple(str(cid) for cid in input_claim_ids),
        redistributable=redistributable,
        audience_user_id=audience_resolved,
    )
