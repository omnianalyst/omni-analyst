# Deploying Omni Analyst v2

Three processes, because they have genuinely different lifecycles:

1. **api** - `uvicorn omni.main:app`. Stateless, restartable, scales
   horizontally. Serves JSON only; it does not serve the `ui/` front end, which
   the image deliberately omits.
2. **scheduler** - `python -m omni.scheduler`. The background sweep and fill
   loops. Run **exactly one**. Its fill workers lease gaps with `SKIP LOCKED`, so
   a second instance would not corrupt the gap table - it would simply double
   the data-provider API spend to produce the same coverage. `docker-compose.prod.yml`
   hard-caps it at `replicas: 1`.
3. **postgres** - `timescale/timescaledb:2.17.2-pg17`, matching the dev compose
   image. The single source of truth.

`docker-compose.prod.yml` defines all three. `Dockerfile` builds the API image,
`Dockerfile.scheduler` builds the scheduler image. They share an identical
builder stage.

---

## Build prerequisites (read this first)

The app depends on **Neutron** as an *editable local path*
(`pyproject.toml` -> `[tool.uv.sources] neutron-py = { path = "../../Neutron/python", editable = true }`).
Neutron is a local framework that is not published to any registry, and the
path `../../Neutron/python` lives outside this repository - so it cannot be
`COPY`-ed into a build whose context is the repo root (Docker forbids paths
above the context), and `pyproject.toml` cannot be changed to point at a
registry without a source edit.

The operator therefore builds the Neutron wheel on the host, once, before
building the images. From the **repository root** (so that `../../Neutron/python`
resolves to the checked-out framework):

```bash
uv build --wheel --project ../../Neutron/python --out-dir vendor
```

This writes `vendor/neutron_py-0.1.0-py3-none-any.whl` (~100 KB). The
Dockerfiles find it there by default. If the Neutron version differs, pass its
filename explicitly:

```bash
docker build -f Dockerfile          --build-arg NEUTRON_WHEEL=vendor/<your-wheel>.whl -t omni-api .
docker build -f Dockerfile.scheduler --build-arg NEUTRON_WHEEL=vendor/<your-wheel>.whl -t omni-scheduler .
```

`vendor/` is operator-created, like `.env`. It is not tracked. Add it to
`.gitignore` if you keep the wheel around.

## Building and running

```bash
# 1. produce the Neutron wheel (once, and after any Neutron change)
uv build --wheel --project ../../Neutron/python --out-dir vendor

# 2. build both images
docker compose -f docker-compose.prod.yml build

# 3. set the two required secrets, then bring the stack up
export POSTGRES_PASSWORD='...'
export OMNI_JWT_SECRET='...'        # >= 32 characters
docker compose -f docker-compose.prod.yml up -d
```

Compose reads a `.env` file in this directory automatically, so you can put the
two required values (and the optional credentials) there instead of exporting
them. `.env` is gitignored and stays that way.

Health: once the API is up, `GET /health` returns 200. `/openapi.json` and
`/docs` describe the surface (provided by Neutron - do not hand-write them).

## Migrations

There is **no separate migration container**. The migrator runs inside the app
lifespan on API startup (`omni.main`), and the scheduler's `__main__` runs it
too. The migrator is idempotent: it records applied versions in
`_neutron_migrations` and skips them. Adding a third, standalone migration
container would only create a racer.

The migrator relies on `_neutron_migrations.version PRIMARY KEY` rather than an
advisory lock, so two migrators hitting a **fresh** database simultaneously can
clash. The compose file removes that window by starting the scheduler only after
the API is *healthy* - and `/health` answers only once the lifespan (which
includes migrations) has completed, so the schema is already in place by the
time the scheduler starts. **For the very first boot of a brand-new database,
run a single API replica** until it is healthy, then scale out.

---

## Configuration

The only source of truth for variable names is `src/omni/config.py` (pydantic
`Settings`) and `src/omni/auth/__init__.py`. Nothing below is invented.

### Required

| Variable | Default | When missing / wrong |
|---|---|---|
| `OMNI_JWT_SECRET` | none | The app **starts fine**, but any endpoint that must *issue* a token raises `500 "OMNI_JWT_SECRET is not configured"`; incoming `Bearer` tokens cannot be verified, so every caller is treated as anonymous (shared network only). There is no default **on purpose**: a signing key shipped in source would not be a signing key. Must be at least 32 characters. `JWT_SECRET` is accepted as an alias. |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5434/omni_v2` | That default is the **dev** compose port. Inside a container `localhost` is the container itself, which has no Postgres, so startup fails on connect. Point it at the `postgres` service - the compose file does this for you (`postgresql://...@postgres:5432/...`). |
| `POSTGRES_PASSWORD` | none (prod) | Compose refuses to start (`POSTGRES_PASSWORD is required`). The dev compose defaults it to `postgres`; prod must not. |

### Optional application settings

Every optional credential below has the same shape: when it is absent the system
**declines to fetch** rather than failing. The fill attempt is recorded as
`unfillable` with a named reason, and coverage for that area simply stays empty.
An operator reading empty coverage as breakage should check the fill log for the
reason, not the claim store for a value.

| Variable | Default | What degrades without it |
|---|---|---|
| `DEBUG` | `false` | Safe to leave unset. |
| `FRED_API_KEY` | `""` | Macro indices and the **shareable** perception layer (consumer sentiment, VIX, credit spreads) stop filling; attempts raise `Unavailable "no FRED API key configured"` and record `unfillable`. FRED is `allowed`-class, so a key is the only thing keeping this shared coverage live. |
| `SEC_USER_AGENT` | `""` | Fundamentals (EDGAR companyfacts) and filings stop filling; attempts raise `Unavailable "no SEC User-Agent configured"`. Not a secret - EDGAR is free and public-domain, it just requires an identifying `User-Agent` of the form `Organisation contact@example.com`, which EDGAR rejects outright without one. |
| `POLYGON_API_KEY` | `""` | Polygon fills raise `Unavailable "no Polygon API key configured"`. (Polygon is `byo_only`; see below.) |
| `COINGECKO_API_KEY` | `""` | Works on the demo tier without a key; degrades to `Unavailable` only on an HTTP 429 throttle. |
| `ETHERSCAN_API_KEY` | `""` | Etherscan on-chain routes (flows, supply) raise `Unavailable`; the Alchemy and DefiLlama routes are unaffected (both `allowed`). |
| `LICENSED_REDISTRIBUTION_PROVIDERS` | `""` | All `byo_only` providers stay private. Set this to promote named providers into shared coverage (see below). Comma-separated provider keys, e.g. `polygon,coingecko`. |

### Infrastructure (compose)

`POSTGRES_USER` (default `postgres`), `POSTGRES_DB` (default `omni_v2`), and
`API_PORT` (default `8000`, the host port published for the API) are optional
convenience variables consumed only by `docker-compose.prod.yml`.

---

## Redistribution - read before configuring a shared key

`credential_owner` is an **access-control key, not metadata**. Data providers
fall into three classes (see `src/omni/credentials/catalog.py`):

- `allowed` - public-domain / redistributable. Enters **shared** coverage; no
  owner.
- `byo_only` - commercial terms forbid serving the data on to third parties.
  A claim fetched with one is visible **only to its credential owner**. It fills
  *that user's* gaps; it does **not** count toward shared coverage and is never
  served to another user.
- `prohibited` - never written at all.

Serving one user's BYO-sourced data to another makes *this deployment* the
redistributor, which the provider's terms forbid. Every query path filters on
this, and the gap engine computes gaps **per audience**, never globally.

**`byo_only` providers** (commercial terms restrict redistribution):

| Provider key | Category | Env-configurable today |
|---|---|---|
| `alpha_vantage` | market data | no (catalog-only) |
| `polygon` | market data | `POLYGON_API_KEY` |
| `fmp` | market data | no (catalog-only) |
| `finnhub` | market data | no (catalog-only) |
| `twelve_data` | market data | no (catalog-only) |
| `trading_economics` | market data | no (catalog-only) |
| `quandl` | market data | no (catalog-only) |
| `coingecko` | crypto | `COINGECKO_API_KEY` |
| `binance` | crypto | no (catalog-only) |
| `coinmarketcap` | crypto | no (catalog-only) |
| `messari` | crypto | no (catalog-only) |
| `news_api` | news | no (catalog-only) |

Only `polygon` and `coingecko` have a `Settings` field today, so only they can
be configured via environment. The rest are catalog entries without env wiring;
adding a shared key for one of them needs the corresponding `Settings` field
added first.

If this operator has actually purchased a redistribution licence for a
`byo_only` provider, name it in `LICENSED_REDISTRIBUTION_PROVIDERS` and that
provider's claims are promoted to `allowed` for *this deployment*. A
`prohibited` provider (Yahoo Finance via yfinance, the one `prohibited` entry)
can never be promoted - its terms bind regardless of what you have bought.

For a **multi-tenant** deployment: do not set a single shared
`byo_only`-class key unless you hold a redistribution licence for it. Without
one, each user must supply their own key, and the claims they fetch stay private
to them.


## Verified

Built and run on 2026-07-28, podman 5.x on macOS.

    podman build -f Dockerfile -t omni-v2-api:test .   ->  677 MB
    GET /health  ->  {"status":"ok","nucleus":"connected","version":"0.1.0"}

Confirmed in the running container: non-root (`omni`), no `ui/`, no `tests/`.

Two things this build surfaced that inspection had not:

**HEALTHCHECK is dropped under podman.** The Dockerfile declares one and podman
warns `HEALTHCHECK is not supported for OCI image format and will be ignored.
Must use docker format`. Build with `--format docker` if you want it honoured,
or rely on the orchestrator's own probe against `/health`. Do not assume the
container self-reports health.

**On macOS, `--network host` shares the VM's network, not the Mac's.** Publish
a port instead, and reach host services at `host.containers.internal`.

## Backups

The Postgres volume is the source of truth for irreplaceable provenance
(bitemporal claims, the prediction ledger, calibration buckets). A single
volume loss is total. `ops/backup.sh` takes a custom-format `pg_dump` to
`/opt/omni-backups` with a rolling 14-day window, and ships it off-box when
`OMNI_RSYNC_TARGET` is set.

Run nightly from the host (the stack's postgres publishes on 5434):

```
17 4 * * *  OMNI_RSYNC_TARGET=tyler@<other-node>:/opt/omni-backups  /path/to/app-v2/ops/backup.sh >> /var/log/omni-backup.log 2>&1
```

Without `OMNI_RSYNC_TARGET` the backup protects against deletion and corruption
but not the box dying -- set it to a second Proxmox node's address for site
resilience. **Test the restore** before relying on it: stop the stack,
`pg_restore --clean --if-exists -d omni_v2 <file>.dump` into the postgres
container, then bring the stack back up. An untested backup is a hope, not a
recovery.
