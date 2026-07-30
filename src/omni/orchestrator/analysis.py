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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from neutron.error import bad_request, not_found

from omni.capability.arguments import Abstention, ArgumentSpec, Materialized, materialize
from omni.capability.derived import DERIVED
from omni.capability.registry import Registry
from omni.capabilities.risk import analyze_credit_risk
from omni.fill.derived import DerivedCapability
from omni.perception.divergence import resolve_derived_licence

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
    arguments: tuple[ArgumentSpec, ...]
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


_NON_CLAIM_ANALYSES: dict[str, DeclaredAnalysis] = {
    "market_risk.credit_risk": DeclaredAnalysis(
        name="market_risk.credit_risk",
        arguments=_CREDIT_RISK_ARGUMENTS,
        compute=_compute_credit_risk,
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
    """
    cap = registry.get(name)
    if cap is None:
        raise not_found(f"No capability named {name!r}")
    if not cap.invocable:
        raise bad_request(
            f"Capability {name!r} is registered but not invocable "
            f"(callability={cap.callability.value}, maturity={cap.maturity.value})"
        )

    derived = _DECLARED_ANALYSES.get(name)
    analysis = _NON_CLAIM_ANALYSES.get(name)
    if (derived is None or derived.arguments is None) and analysis is None:
        raise bad_request(
            f"Capability {name!r} declares no arguments; it cannot be assembled "
            f"from coverage"
        )

    if derived is not None and derived.arguments is not None:
        arguments = derived.arguments
        is_claim_path = True
    else:
        arguments = analysis.arguments
        is_claim_path = False

    materialized: dict[str, Materialized] = {}
    abstentions: list[Abstention] = []
    for spec in arguments:
        result = await materialize(
            spec, pool, entity_id=entity_id, audience=audience
        )
        if isinstance(result, Abstention):
            abstentions.append(result)
        else:
            materialized[spec.name] = result

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
        computed = await derived.compute(pool, context, **materialized)
    else:
        computed = await analysis.compute(**materialized)

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
    # materialized rather than re-read by id -- the same fix D7 applied to
    # fill_analysis, adopted here so this path does not reintroduce the
    # re-read D7 deleted. resolve_derived_licence is duck-typed against
    # .redistributable/.audience_user_id, which ProvenanceRow carries.
    licence_inputs = [row for m in materialized.values() for row in m.rows]
    redistributable, audience_resolved = resolve_derived_licence(licence_inputs)

    return AnalysisResult(
        capability=name,
        abstained=False,
        result=computed,
        evidence=tuple(str(cid) for cid in input_claim_ids),
        redistributable=redistributable,
        audience_user_id=audience_resolved,
    )
