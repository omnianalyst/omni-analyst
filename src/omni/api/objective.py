"""The agentic surface: state what you want to know, see the plan and the cost.

Three endpoints over the planner and executor:

* ``POST /objective/plan`` -- what the system would do, without doing it. The
  point is that a user can see the cost and the licence implications before
  agreeing to the run, so planning is free of side effects.
* ``POST /objective/run`` -- plan, execute, and record whatever could not be
  answered as demand. The demand distinction (a missing producer becomes
  demand; a licensing or budget rule does not) already lives in ``run.py``;
  this layer passes the resolved ``entity_id`` through and does not re-derive
  it.
* ``GET /capabilities`` -- the runnable surface, so a caller can discover it
  without reading code.

The router closes over the Neutron ``App`` (for ``app.db``) and over
``default_registry()``, so the endpoints see everything runnable rather than
only the built-in adapters. The audience header is ``X-User-Id``, the same
hint the coverage API uses; a ``shareable`` objective ignores it because the
answer is destined for the shared network, which forbids licensed inputs
regardless of who is asking.
"""

from __future__ import annotations

from uuid import UUID

from neutron import App, Router
from neutron.error import not_found
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.credentials.catalog import redistribution_for
from omni.orchestrator.analysis import AnalysisResult, run_analysis
from omni.orchestrator.planner import (
    Objective,
    Plan,
    Step,
    explain,
    plan,
)
from omni.orchestrator.run import execute
from omni.scheduler.worker import default_registry


class ObjectiveRequest(BaseModel):
    text: str
    target: str
    needs: list[str]
    entity_kind: str | None = None
    shareable: bool = False
    budget: float = 10.0


class AnalysisRequest(BaseModel):
    capability: str
    target: str
    entity_kind: str | None = None


def _audience(request: Request) -> UUID | None:
    """Who is asking, from a verified token — never from a header.

    This read X-User-Id, so any caller could name any user and read their
    licensed claims. The store's constraints were sound and the identity
    in front of them was a claim. An absent or invalid token is an
    anonymous caller, which means shared coverage only.
    """
    return resolve_audience_from_request(request)


def _step_tier(step: Step) -> str:
    # Matches the labels explain() uses, so the JSON and the human summary
    # agree on what "private" means.
    return "private" if step.touches_byo else "shared"


def _capability_tier(cap) -> str:
    if cap.provider_key:
        return redistribution_for(cap.provider_key)
    return "byo_only" if cap.touches_byo else "allowed"


def _step_to_dict(step: Step) -> dict:
    return {
        "capability": step.capability,
        "claim_type": step.claim_type,
        "target": step.target,
        "cost": step.cost,
        "licence_tier": _step_tier(step),
    }


def _shortfall_to_dict(shortfall) -> dict:
    return {
        "claim_type": shortfall.claim_type,
        "reason": shortfall.reason.value,
        "detail": shortfall.detail,
    }


def _plan_to_dict(p: Plan) -> dict:
    return {
        "objective": p.objective,
        "steps": [_step_to_dict(s) for s in p.steps],
        "shortfalls": [_shortfall_to_dict(s) for s in p.shortfalls],
        "cost": p.cost,
        "satisfiable": p.satisfiable,
        "partial": p.partial,
    }


def _analysis_to_dict(result: AnalysisResult) -> dict:
    body: dict = {
        "capability": result.capability,
        "abstained": result.abstained,
    }
    if result.abstained:
        body["shortfalls"] = [
            {"argument": s.argument, "reason": s.reason}
            for s in result.shortfalls
        ]
        body["evidence"] = []
        body["licence"] = None
        return body

    draft = result.result
    if isinstance(draft, dict):
        # A non-claim declared analysis (e.g. market_risk.credit_risk via
        # QF1): compute returns a plain, already JSON-serialisable dict rather
        # than a ClaimDraft, because it never writes a claim. Emit it as-is --
        # evidence/licence are populated identically for both result shapes,
        # since they live on AnalysisResult, not on the result object.
        body["result"] = draft
    else:
        body["result"] = {
            "claim_type": draft.claim_type,
            "key": draft.key,
            "value": draft.value,
            "unit": draft.unit,
            "evidence": draft.evidence,
            "event_date": draft.event_date.isoformat() if draft.event_date else None,
            "knowledge_date": (
                draft.knowledge_date.isoformat() if draft.knowledge_date else None
            ),
            "confidence": draft.confidence,
        }
    body["evidence"] = list(result.evidence)
    body["licence"] = {
        "redistributable": result.redistributable,
        "audience_user_id": (
            str(result.audience_user_id) if result.audience_user_id else None
        ),
    }
    return body


async def _resolve_entity_id(pool, target: str, entity_kind: str | None) -> UUID | None:
    if entity_kind is not None:
        row = await pool.fetchrow(
            "SELECT id FROM entity WHERE symbol = $1 AND kind = $2",
            target,
            entity_kind,
        )
    else:
        row = await pool.fetchrow(
            "SELECT id FROM entity WHERE symbol = $1", target
        )
    return row["id"] if row else None


def build_router(app: App) -> Router:
    # Built once: descriptors only, no network. The callables reference
    # settings at call time, so closing over the registry here is safe.
    registry = default_registry()
    router = Router()

    @router.post("/objective/plan")
    async def plan_objective(req: ObjectiveRequest, request: Request) -> dict:
        audience = None if req.shareable else _audience(request)
        objective = Objective(
            text=req.text,
            target=req.target,
            needs=tuple(req.needs),
            entity_kind=req.entity_kind,
            audience=audience,
            shareable=req.shareable,
            budget=req.budget,
        )
        p = plan(objective, registry)
        body = _plan_to_dict(p)
        body["summary"] = explain(p)
        return body

    @router.post("/objective/run")
    async def run_objective(req: ObjectiveRequest, request: Request) -> dict:
        entity_id = await _resolve_entity_id(
            app.db.pool, req.target, req.entity_kind
        )
        if entity_id is None:
            raise not_found(f"No entity with symbol '{req.target}'")
        audience = None if req.shareable else _audience(request)
        objective = Objective(
            text=req.text,
            target=req.target,
            needs=tuple(req.needs),
            entity_kind=req.entity_kind,
            audience=audience,
            shareable=req.shareable,
            budget=req.budget,
        )
        p = plan(objective, registry)
        # execute() records NO_PRODUCER shortfalls as demand internally; the
        # licensing/budget distinction is its responsibility, not ours.
        outcome = await execute(
            p, registry, pool=app.db.pool, entity_id=entity_id
        )
        body = _plan_to_dict(p)
        body["summary"] = explain(p)
        body["results"] = [
            {
                "capability": r.capability,
                "claim_type": r.claim_type,
                "ok": r.ok,
                "output": r.output,
                "error": r.error,
            }
            for r in outcome.results
        ]
        body["evidence"] = outcome.evidence
        body["answered"] = outcome.answered
        body["demand_raised"] = [str(d) for d in outcome.demand_raised]
        return body

    @router.post("/analysis/run", status_code=200)
    async def run_analysis_by_name(
        req: AnalysisRequest, request: Request
    ) -> dict:
        entity_id = await _resolve_entity_id(
            app.db.pool, req.target, req.entity_kind
        )
        if entity_id is None:
            raise not_found(f"No entity with symbol '{req.target}'")
        audience = _audience(request)
        result = await run_analysis(
            registry,
            app.db.pool,
            name=req.capability,
            entity_id=entity_id,
            audience=audience,
        )
        return _analysis_to_dict(result)

    @router.get("/capabilities")
    async def list_capabilities() -> dict:
        caps = []
        for name in sorted(registry._by_name.keys()):
            cap = registry._by_name[name]
            caps.append(
                {
                    "name": cap.name,
                    "description": cap.description,
                    "produces": list(cap.produces),
                    "consumes": list(cap.consumes),
                    "entity_kinds": list(cap.entity_kinds),
                    "licence_tier": _capability_tier(cap),
                    "cost": cap.cost,
                    "reliability": registry.reliability(cap.name),
                }
            )
        return {"capabilities": caps}

    return router
