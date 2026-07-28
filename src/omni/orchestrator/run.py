"""Executing a plan, and turning what it could not do into demand.

The connection worth noticing: when the planner cannot answer something because
coverage is missing, that shortfall is written to the demand ledger. The gap
engine then raises it as a gap, the fill pipeline closes it, and the same
question succeeds next time.

So the system's failures are its own work queue. A question nobody can answer
today is the single best signal about what to fetch next, and it costs nothing
extra to capture — the planner already knows exactly what it lacked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from omni.capability.registry import Registry
from omni.demand.ledger import direct_attention
from omni.orchestrator.planner import Plan, Unsatisfiable


@dataclass
class StepResult:
    capability: str
    claim_type: str
    ok: bool
    output: object = None
    error: str | None = None


@dataclass
class Outcome:
    plan: Plan
    results: list[StepResult] = field(default_factory=list)
    demand_raised: list[UUID] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def evidence(self) -> list[object]:
        return [r.output for r in self.results if r.ok and r.output is not None]


async def record_shortfalls_as_demand(
    pool, plan: Plan, *, entity_id: UUID, weight: float = 1.0
) -> list[UUID]:
    """Write what the planner could not answer into the demand ledger.

    Only genuine coverage gaps. A shortfall caused by a licensing rule or by
    the budget is not a missing-data problem, and raising demand for it would
    ask the fill pipeline to fetch something it is either forbidden to use or
    was never asked to pay for.
    """
    raised: list[UUID] = []
    for shortfall in plan.shortfalls:
        if shortfall.reason is not Unsatisfiable.NO_PRODUCER:
            continue
        demand_id = await direct_attention(
            pool,
            entity_id=entity_id,
            claim_type=shortfall.claim_type,
            key=None,
            requested_by=plan.audience,
            weight=weight,
        )
        raised.append(demand_id)
    return raised


async def execute(
    plan: Plan,
    registry: Registry,
    *,
    pool=None,
    entity_id: UUID | None = None,
    **call_kwargs,
) -> Outcome:
    """Run a plan's steps, then record whatever it could not do as demand."""
    outcome = Outcome(plan=plan)

    for step in plan.steps:
        capability = registry.get(step.capability)
        if capability is None or capability.call is None:
            outcome.results.append(StepResult(
                step.capability, step.claim_type, ok=False,
                error="capability is not bound to an implementation",
            ))
            continue
        try:
            output = await capability.call(step.target, **call_kwargs)
        except Exception as exc:  # noqa: BLE001 - recorded per step, not swallowed
            outcome.results.append(StepResult(
                step.capability, step.claim_type, ok=False,
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        outcome.results.append(StepResult(
            step.capability, step.claim_type, ok=True, output=output,
        ))

    if pool is not None and entity_id is not None:
        outcome.demand_raised = await record_shortfalls_as_demand(
            pool, plan, entity_id=entity_id
        )
    return outcome
