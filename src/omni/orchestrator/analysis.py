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
that is ``perception.divergence`` alone (the one analysis with a declaration).
A capability without declared arguments is refused with a reason, never
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

from dataclasses import dataclass
from uuid import UUID

from neutron.error import bad_request, not_found

from omni.capability.arguments import Abstention, Materialized, materialize
from omni.capability.derived import DERIVED
from omni.capability.registry import Registry
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
    for declared-path capabilities), ``evidence`` is the contributing claim ids,
    and ``redistributable`` / ``audience_user_id`` are the licence verdict from
    ``resolve_derived_licence`` over the inputs.
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

    declared = _DECLARED_ANALYSES.get(name)
    if declared is None or declared.arguments is None:
        raise bad_request(
            f"Capability {name!r} declares no arguments; it cannot be assembled "
            f"from coverage"
        )

    materialized: dict[str, Materialized] = {}
    abstentions: list[Abstention] = []
    for spec in declared.arguments:
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

    context = {
        "entity_id": entity_id,
        "audience_user_id": audience,
        "claim_type": cap.produces[0] if cap.produces else "",
        "key": "",
    }

    computed = await declared.compute(pool, context, **materialized)

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
