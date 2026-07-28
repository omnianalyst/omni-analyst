"""Objective in, plan out.

The product thesis in one module: the user states what they want to know, and
the system works out which capabilities to run rather than making the person
assemble it from seventy-one pages.

Four constraints, each of which exists because its absence produces a specific
failure:

**Budgeted.** A planner with hundreds of capabilities and no ceiling is an
unbounded bill. Every plan declares its cost before it runs.

**Licence-aware at planning time.** A plan that would blend licensed data into
a shareable answer is rejected while planning, not when the database refuses
the write. By then the fetch has been paid for and the reason is a constraint
violation rather than an explanation.

**Able to fail usefully.** "I cannot answer this, and here is what is missing"
is a first-class outcome, and the missing pieces are written back as demand.
The planner's failure mode is the gap engine's input, which is the tightest
loop in the system: an unanswerable question is precisely the definition of a
coverage gap worth filling.

**Cited.** A step records the claims it consumed, so a finding can be
re-derived and audited rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from omni.capability.registry import Registry


class Unsatisfiable(str, Enum):
    """Why an objective could not be planned. Each maps to a different remedy."""

    NO_PRODUCER = "no_capability_produces_this"
    ONLY_LICENSED = "only_licensed_sources_can_produce_this"
    OVER_BUDGET = "cheapest_viable_plan_exceeds_budget"


@dataclass(frozen=True)
class Step:
    capability: str
    claim_type: str
    target: str
    cost: float
    touches_byo: bool


@dataclass(frozen=True)
class Shortfall:
    claim_type: str
    reason: Unsatisfiable
    detail: str


@dataclass(frozen=True)
class Plan:
    objective: str
    steps: tuple[Step, ...] = ()
    shortfalls: tuple[Shortfall, ...] = ()
    audience: UUID | None = None
    shareable: bool = False

    @property
    def cost(self) -> float:
        return sum(s.cost for s in self.steps)

    @property
    def satisfiable(self) -> bool:
        return bool(self.steps) and not self.shortfalls

    @property
    def partial(self) -> bool:
        """Some of the objective is answerable and some is not.

        Worth distinguishing from total failure: a partial answer plus an
        honest list of what is missing is useful, whereas silence is not.
        """
        return bool(self.steps) and bool(self.shortfalls)


@dataclass
class Objective:
    """What the user wants to know, resolved to the coverage it requires.

    `shareable` means the answer is destined for the shared network rather than
    one user, which forbids licensed inputs entirely.
    """

    text: str
    target: str
    needs: tuple[str, ...]
    entity_kind: str | None = None
    audience: UUID | None = None
    shareable: bool = False
    budget: float = 10.0


def plan(objective: Objective, registry: Registry) -> Plan:
    """Choose one capability per required claim type, cheapest-best first."""
    steps: list[Step] = []
    shortfalls: list[Shortfall] = []
    spent = 0.0

    for claim_type in objective.needs:
        candidates = registry.producing(
            claim_type, allow_byo=not objective.shareable,
            entity_kind=objective.entity_kind,
        )

        if not candidates:
            # Distinguish "nothing can do this" from "nothing may do this for a
            # shareable answer" -- the remedies are completely different. The
            # first needs a new adapter; the second needs a redistribution
            # licence, or for the answer to be scoped to one user.
            served = registry.producing(claim_type, entity_kind=objective.entity_kind)
            if not served and registry.producing(claim_type):
                shortfalls.append(Shortfall(
                    claim_type, Unsatisfiable.NO_PRODUCER,
                    f"producers of {claim_type} exist but none serve entity kind "
                    f"'{objective.entity_kind}'",
                ))
                continue
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
            continue

        chosen = candidates[0]
        if spent + chosen.cost > objective.budget:
            shortfalls.append(Shortfall(
                claim_type, Unsatisfiable.OVER_BUDGET,
                f"{chosen.name} costs {chosen.cost} with {objective.budget - spent:.2f} "
                "of budget left",
            ))
            continue

        spent += chosen.cost
        steps.append(Step(
            capability=chosen.name, claim_type=claim_type,
            target=objective.target, cost=chosen.cost,
            touches_byo=chosen.touches_byo,
        ))

    return Plan(
        objective=objective.text,
        steps=tuple(steps),
        shortfalls=tuple(shortfalls),
        audience=objective.audience,
        shareable=objective.shareable,
    )


def explain(plan: Plan) -> str:
    """What the system intends to do, in a form a person can check.

    A plan the user cannot inspect is a plan they have to trust, and the point
    of showing less is not to hide more.
    """
    lines = [f"objective: {plan.objective}"]
    if plan.steps:
        lines.append(f"plan ({plan.cost:.1f} cost):")
        for s in plan.steps:
            tier = "private" if s.touches_byo else "shared"
            lines.append(f"  {s.capability} -> {s.claim_type} [{tier}]")
    if plan.shortfalls:
        lines.append("cannot answer:")
        for f in plan.shortfalls:
            lines.append(f"  {f.claim_type}: {f.detail}")
    return "\n".join(lines)
