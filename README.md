# Omni Analyst

A demand-driven coverage network with agentic gap-filling. The system holds
*coverage* — claims about entities, each with provenance, freshness and
confidence. You point it at what you care about; it computes the gap between
demanded and actual coverage and works to close it. Reports are views over
coverage, not the product.

Two properties are load-bearing and deliberately non-negotiable:

- **Nothing is fabricated.** If a source is unavailable or unconfigured, the
  gap stays visible and the fill attempt records why it refused. An empty
  network is honest; a full-looking one that guessed is not.
- **Predictions are falsifiable.** Direction (never action), barriers fixed at
  write time, point-in-time entry prices, scoring as a separate pass, and a
  conviction gate derived from measured calibration — never chosen.

Bring your own keys. The system is honest about what it cannot see: unfillable
gaps are a feature, because they are the truthful state of your coverage.

## Running it

```bash
docker compose up -d postgres
uv sync --extra dev
uv run uvicorn omni.main:app --reload
```

See `DEPLOY.md` for the production stack (API, scheduler, Timescale Postgres,
Caddy edge) and the full configuration reference. The only source of truth for
settings is `src/omni/config.py`.

## Footprint

The deployment profile decides how much the system collects:

| | `solo` (default) | `full` |
|---|---|---|
| Price history / backfill depth | 365 days | 730 days |
| Standing sector demand | top 4 sectors | all 11 sectors |
| Target box | 2-4 GB VPS | shared 8GB+ box |

Measured on the reference deployment (shared 12GB box, 500-entity universe,
730-day depth) before and after the 2026-08 performance pass:

- Postgres CPU: 100-140% constant → ~0.9 core average
- Postgres RAM: 3.1 GB → ~0.9 GB
- Total stack footprint: ~3.7 GB → ~1.6 GB

The wins came from eliminating waste (unindexed calibration reads, live
aggregate views, default-for-dedicated-hardware Postgres sizing, duplicate
backfill re-walks), not from cutting correctness machinery. That is the
constraint this project holds itself to: a small footprint as a property of
correctness, never in tension with it.

Postgres sizing guidance for a solo box: `shared_buffers` 256-512MB,
`maintenance_work_mem` 128MB, `max_connections` 20-30. The docker-compose
comment block shows the reasoning template — size for the box you actually
run, not the image defaults, which assume dedicated hardware.

### The honest trade-off

Shallower backfill means fewer resolved predictions per claim class, which
means noisier calibration buckets. Noisier calibration raises the conviction
bar, so a `solo` deployment surfaces **fewer** findings than a `full` one.
That refusal is correct behavior: a gate that surfaces everything regardless
of its measured accuracy is the thing this system exists not to be. If a solo
box goes quiet, check the calibration record before assuming breakage.

### Providers

| Provider | Access | Notes |
|---|---|---|
| FRED | free key | macro series; redistributable |
| DefiLlama, ccxt (Binance et al.) | keyless | crypto prices; public endpoints |
| Polygon | bring your own key | equity prices; per-user data, never shared |
| SEC EDGAR | free, identifying User-Agent | fundamentals, insider filings |
| Etherscan | free key | on-chain routes |

Every provider is classified by redistribution terms
(`src/omni/credentials/catalog.py`): data fetched with a `byo_only` key is
visible only to the key's owner, fills that owner's gaps, and never enters
shared coverage. Serving one user's keyed data to another would make the
deployment the redistributor, which the provider terms forbid.

## Architecture

Built on [Neutron](https://github.com/neutron-build/neutron) (Python tier)
against the PostgreSQL wire protocol. `AGENTS.md` states the invariants;
`docs/OMNI_ANALYST.md` the current architecture; `docs/NEXT_SESSION.md` the
working state.
