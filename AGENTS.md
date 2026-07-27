# AGENTS.md — Omni Analyst v2

Read this before touching anything. If a work order contradicts this file, the
work order is wrong — say so in your report and change nothing.

## What this is

A **demand-driven coverage network with agentic gap-filling**. Not a dashboard,
not a report generator.

The system holds *coverage* — claims about entities, each with provenance,
freshness and confidence. Users direct attention at an entity; the system
computes the gap between demanded and actual coverage and dispatches agents to
close it. Reports are views over coverage. High-conviction findings are pushed
out unprompted, with their evidence and a recorded prediction.

Built on Neutron (Python tier) against the PostgreSQL wire protocol.

## Critical rules

1. **Never use emojis** in code, output, or docs. SVG/icon fonts only.
2. **Never fabricate data.** No hardcoded fallback values, no invented defaults,
   no placeholder numbers. If a source is unavailable, the claim is not written
   and the fill attempt records `unfillable` with a reason. A gap-filler that
   always produces something is how hallucinated coverage enters the store.
3. **No unsolicited comments, docstrings, or refactors.** Comment only when the
   reasoning is genuinely non-obvious.
4. **Don't add features** beyond the work order.
5. **Test before claiming done.** Run it. Paste the real output in your report.
6. **Stay inside your work order's file list.** Other agents are working in this
   repo concurrently. A diff touching files you do not own is rejected
   regardless of whether the tests pass.

## The invariants

These are load-bearing. Violating one is a correctness bug, not a style issue.

### Claims

- A claim carries `source`, `event_date` (when it happened), `knowledge_date`
  (when it became knowable), `confidence`, `credential_owner`, and
  `redistributable`. Bitemporal, always — a single `as_of` is not sufficient.
- Ingestion is idempotent on
  `(entity, claim_type, source, event_date, knowledge_date)`.
- Freshness is first-class and visible. A stale network that looks covered is
  worse than an empty one, because emptiness is honest.

### Redistribution — the rule most likely to be broken by accident

`credential_owner` is **an access-control key, not metadata.**

Data providers fall into three classes (see the credential catalog):
`allowed` (public-domain / redistributable), `byo_only` (commercial terms forbid
serving the data on to third parties), and `prohibited`.

- A claim fetched with a **`byo_only`** credential is visible **only to the
  credential owner**. It fills that user's gaps. It does **not** count toward
  shared coverage and must never be served to another user.
- Only `allowed`-class claims accumulate into the shared network.
- The gap engine therefore computes gaps **per audience**, never globally.

Serving one user's BYO-sourced data to another makes this deployment the
redistributor, which the provider terms forbid. Any query path that returns
claims must filter on this. If you are unsure whether a code path is
audience-scoped, it is not — make it explicit.

### Predictions and the conviction gate

- The ledger records `direction`, never `action`. This is analysis, not advice.
- Barriers are fixed at write time. Falsifiability requires the threshold exist
  before the outcome.
- Entry price is point-in-time.
- **Scoring is a separate pass.** The writer never sets outcome fields.
- If the price path is unavailable, the prediction stays `pending`. Never score
  against a substituted or approximated path.
- Conviction thresholds are **derived from calibration, never chosen**. A claim
  class with too few resolved predictions cannot be surfaced at all. A quiet
  week is a healthy outcome.

## Testing

- **Assert behaviour, not shape.** `assert "data" in response.json()` proves
  almost nothing.
- **Deliberately cover the failure path.** Every feature needs a case where its
  dependency is unavailable and it fails honestly. This codebase's predecessor
  failed by silently substituting defaults; do not reintroduce that.
- **Never weaken an assertion to make a test green.** Changing
  `assert x == 42` to `assert x is not None` is a failure, not a fix.
- Never delete a test. Skipping with a named dependency is acceptable.
- Run only your own test files unless asked otherwise.

## Layout

```
src/omni/          application code
migrations/        NNN_name.sql, run by neutron.nucleus.migrate.Migrator
tests/             pytest, asyncio_mode=auto
_orchestrator/     work orders, specs, agent reports and logs
```

## Running

```bash
docker compose up -d postgres
uv sync --extra dev
uv run pytest
uv run uvicorn omni.main:app --reload
```

Health, OpenAPI and docs are provided by Neutron at `/health`,
`/openapi.json` and `/docs` — do not hand-write them.

## Reporting Neutron defects

This project is Neutron's primary dogfood. When the framework is the problem,
record it in `Neutron/docs/ADOPTION_FINDINGS.md` — not here, and not in a
comment. Include the file, the line, and what you expected.
