"""Multi-step planning: capabilities that need other capabilities first.

`planner.plan` picks one capability per claim type and stops. That is enough
when every need is directly fetchable, and useless the moment something is
derived — a manipulation signal needs price bars, a divergence needs both a
perception series and a fundamental one. Asking for the derived thing alone
produced "no capability produces this" even when everything required to build
it was available.

This resolves the dependency graph instead: for each need, if a capability
declares `consumes`, plan those inputs first, recursively, and order the whole
thing so nothing runs before what it depends on.

Two properties worth stating because they are easy to lose:

**Cycles terminate.** A capability chain that loops is a bug in the registry,
not a reason to hang, so it is detected and reported as a shortfall naming the
cycle.

**Depth is bounded.** An unbounded resolver on a rich registry can plan
hundreds of steps from one innocuous objective, which is a bill rather than an
answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.capability.registry import Registry
from omni.orchestrator.planner import (
    Objective,
    Plan,
    Shortfall,
    Step,
    Unsatisfiable,
)

MAX_DEPTH = 4


@dataclass(frozen=True)
class Resolution:
    """One claim type resolved to a capability, with its own inputs first."""

    claim_type: str
    capability: str
    cost: float
    touches_byo: bool
    depth: int
    consumes: tuple[str, ...] = ()


def _resolve(
    claim_type: str,
    registry: Registry,
    objective: Objective,
    *,
    seen: tuple[str, ...],
    depth: int,
    out: list[Resolution],
    shortfalls: list[Shortfall],
) -> bool:
    """Depth-first resolve one claim type and everything it consumes."""
    if claim_type in seen:
        shortfalls.append(Shortfall(
            claim_type, Unsatisfiable.NO_PRODUCER,
            f"capability cycle: {' -> '.join((*seen, claim_type))}",
        ))
        return False

    if depth > MAX_DEPTH:
        shortfalls.append(Shortfall(
            claim_type, Unsatisfiable.NO_PRODUCER,
            f"dependency chain deeper than {MAX_DEPTH} for {claim_type}",
        ))
        return False

    if any(r.claim_type == claim_type for r in out):
        return True  # already planned by another branch

    candidates = registry.producing(
        claim_type,
        allow_byo=not objective.shareable,
        entity_kind=objective.entity_kind,
    )
    if not candidates:
        served = registry.producing(claim_type, entity_kind=objective.entity_kind)
        if objective.shareable and served:
            shortfalls.append(Shortfall(
                claim_type, Unsatisfiable.ONLY_LICENSED,
                f"every producer of {claim_type} is licensed per operator, "
                "so it cannot enter a shareable answer",
            ))
        else:
            shortfalls.append(Shortfall(
                claim_type, Unsatisfiable.NO_PRODUCER,
                f"no invocable capability produces {claim_type}",
            ))
        return False

    chosen = candidates[0]

    # Inputs first. If any input cannot be resolved, this capability cannot
    # run either -- planning it anyway would schedule a step guaranteed to
    # fail, and the honest answer is that the whole branch is unavailable.
    for required in chosen.consumes:
        ok = _resolve(
            required, registry, objective,
            seen=(*seen, claim_type), depth=depth + 1,
            out=out, shortfalls=shortfalls,
        )
        if not ok:
            shortfalls.append(Shortfall(
                claim_type, Unsatisfiable.NO_PRODUCER,
                f"{chosen.name} needs {required}, which could not be planned",
            ))
            return False

    out.append(Resolution(
        claim_type=claim_type, capability=chosen.name, cost=chosen.cost,
        touches_byo=chosen.touches_byo, depth=depth,
        consumes=tuple(chosen.consumes),
    ))
    return True


def plan_chain(objective: Objective, registry: Registry) -> Plan:
    """Plan an objective, resolving each need's dependencies before it.

    Steps come back in execution order: an input always precedes the thing
    that consumes it.
    """
    resolutions: list[Resolution] = []
    shortfalls: list[Shortfall] = []

    for claim_type in objective.needs:
        _resolve(
            claim_type, registry, objective,
            seen=(), depth=0, out=resolutions, shortfalls=shortfalls,
        )

    # Budget is applied after resolution, not during. Truncating mid-chain
    # would leave a capability scheduled without the input it consumes, which
    # is worse than refusing the tail cleanly.
    steps: list[Step] = []
    dropped: set[str] = set()
    spent = 0.0
    for r in resolutions:
        # Anything downstream of a dropped step goes too. Skipping only the
        # expensive step would schedule a capability without the input it
        # consumes -- which is the exact failure this ordering exists to
        # prevent, and it survived the first implementation of it.
        missing = [c for c in r.consumes if c in dropped]
        if missing:
            dropped.add(r.claim_type)
            shortfalls.append(Shortfall(
                r.claim_type, Unsatisfiable.OVER_BUDGET,
                f"{r.capability} needs {', '.join(missing)}, dropped for budget",
            ))
            continue
        if spent + r.cost > objective.budget:
            dropped.add(r.claim_type)
            shortfalls.append(Shortfall(
                r.claim_type, Unsatisfiable.OVER_BUDGET,
                f"{r.capability} costs {r.cost} with "
                f"{objective.budget - spent:.2f} of budget left",
            ))
            continue
        spent += r.cost
        steps.append(Step(
            capability=r.capability, claim_type=r.claim_type,
            target=objective.target, cost=r.cost, touches_byo=r.touches_byo,
        ))

    return Plan(
        objective=objective.text,
        steps=tuple(steps),
        shortfalls=tuple(shortfalls),
        audience=objective.audience,
        shareable=objective.shareable,
    )
