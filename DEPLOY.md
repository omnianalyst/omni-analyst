# Deploying Omni Analyst v2

Three processes, because they have genuinely different lifecycles:

1. **api** - `uvicorn omni.main:app`. Stateless, restartable, scales
   horizontally. Serves JSON only; it does not serve the `ui/` front end, which
   the image deliberately omits.
2. **scheduler** - `python -m omni.scheduler`. The background sweep and fill
   loops. Run **exactly one**. Singleton ownership is enforced in the app:
   the scheduler takes a PostgreSQL advisory lock at startup and refuses to
   run while another instance holds it (`omni/scheduler/singleton.py`), so a
   second instance -- manual or mis-orchestrated -- exits loudly instead of
   doubling the data-provider API spend for the same coverage. The compose
   file's `replicas: 1` is orchestration intent; the database lock is the
   guarantee.
3. **postgres** - `timescale/timescaledb:2.17.2-pg17`, matching the dev compose
   image. The single source of truth.

`docker-compose.prod.yml` defines all three. `Dockerfile` builds the API image,
`Dockerfile.scheduler` builds the scheduler image. They share an identical
builder stage.

---

## Building and running

Install Git, Docker with Compose, uv, Python 3.11 or newer, and Node.js 22 or newer. From the
repository root, run:

```bash
./ops/start_stack.sh
```

The script installs the exact `neutron-framework` release pinned in `uv.lock`,
generates missing secrets in the gitignored `.env`, builds the UI and both images,
then starts Postgres and the API. The scheduler is temporarily excluded while
its memory use is investigated; do not start it on the shared host until the
reviewed restart plan is complete.
Commit changes to the application and deployment files before running it so
the image's Omni revision matches the built source.
The API and scheduler images record the Omni revision and Neutron package version. The images
serve JSON only; serve `ui/dist` through the bundled Caddy configuration or
another reverse proxy.

The secret initializer keeps existing values and sets `.env` to owner-only
permissions. Compose reads it automatically. Optional provider credentials can
be added later. No separate Neutron checkout or wheel build is needed.

Health: once the API is up, `GET /health` returns 200. `/openapi.json` and
`/docs` describe the surface (provided by Neutron - do not hand-write them).

The current public `app.omnianalyst.com` route enters a Cloudflare Tunnel on
deploy-home2. Its Caddy `http://app.omnianalyst.com` block forwards over
Tailscale to infra-home `100.108.123.49:8080`, where the Omni UI and API are
served. The route was restored on 2026-09-28 after a same-URL 308 redirect
loop; both the public UI and `/health` returned 200 afterward. The public site
at `omnianalyst.com` is a separate static deployment.

## Migrations

There is **no separate migration container**. The migrator runs inside the app
lifespan on API startup (`omni.main`), and the scheduler's `__main__` runs it
too. The migrator is idempotent: it records applied versions in
`_neutron_migrations` and skips them. Adding a third, standalone migration
container would only create a racer.

The migrator serialises concurrent runs with a transaction-scoped advisory
lock (`pg_advisory_xact_lock` around the whole run: lock, read versions,
apply), so two migrators hitting a **fresh** database simultaneously serialise
-- the second waits, re-reads the versions table, and finds nothing left to
do. A regression test (`tests/test_migration_lock.py`) launches two migrators
concurrently against a blank database on every run.

---

## Configuration

The only source of truth for variable names is `src/omni/config.py` (pydantic
`Settings`) and `src/omni/auth/__init__.py`. Nothing below is invented.

### Required

| Variable | Default | When missing / wrong |
|---|---|---|
| `OMNI_JWT_SECRET` | none | The app **refuses to start**: startup fails fast with `OMNI_JWT_SECRET is not configured` (or a too-short rejection below 32 characters). Previously a missing key silently downgraded every authenticated request to anonymous -- an infrastructure fault impersonating an unauthenticated caller -- which served shared-data responses while every caller looked logged-out. There is no default **on purpose**: a signing key shipped in source would not be a signing key. `JWT_SECRET` is accepted as an alias. |
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
| `OMNI_TRUSTED_PROXIES` | `""` (none) | Login throttling sees the reverse proxy's address instead of the real client, so everyone behind the proxy shares one throttle bucket. Set it to the proxy's address or CIDR (comma-separated for several, e.g. `10.0.0.0/8`) and `X-Forwarded-For` is honoured **only** from those peers -- a direct client cannot spoof it. A malformed entry is a loud startup-path error, never a silent narrowing. |
| `SCHEDULER_HEARTBEAT_MAX_AGE` | `900` | How stale the scheduler's heartbeat file may be before the container healthcheck fails (sweeps run every 300s; the default tolerates one missed cycle). |
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
| `polygon` | market data | `POLYGON_API_KEY` |
| `coingecko` | crypto | `COINGECKO_API_KEY` |
| `binance` | crypto | no (keyless public endpoints) |

Catalog entries for providers without adapters were removed 2026-08-21; the
catalog now lists only live sources. Users can also paste their own keys for
`polygon`, `fred`, `etherscan` and `coingecko` in Settings (encrypted at
rest, used for their fetches, deployment env as fallback).

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

### Moving machines

Settings has a **Download backup** button (operator account): one click
produces the custom-format dump. **That dump alone is not a complete
backup**: saved bank/provider/venue credentials are Fernet-encrypted under a
key that lives outside PostgreSQL (the `omni_keys` volume or
`OMNI_CREDENTIAL_KEY`). Restoring the dump onto a host with a different or
missing key leaves every stored credential undecryptable. The nightly
`ops/backup.sh` pairs every dump with `<name>.key.age` -- the same key,
encrypted to an offline `age` recipient -- and moving machines needs both
halves.

1. On the old machine: run `ops/backup.sh` (it takes the dump AND the
   encrypted key; the Settings button takes only the dump), or use Settings
   -> Download backup and separately export the key.
2. Install the stack on the new machine (this document), bring it up once
   with a fresh database, then stop the api and scheduler.
3. Restore the dump into the new postgres container:

   ```
   docker cp omni-backup-YYYYMMDD.dump omni_postgres:/tmp/restore.dump
   docker exec omni_postgres dropdb -U postgres omni_v2
   docker exec omni_postgres createdb -U postgres omni_v2
   docker exec omni_postgres pg_restore -U postgres -d omni_v2 /tmp/restore.dump
   ```

4. Restore the credential key onto the new machine, from the `.key.age`
   archive, with the offline identity that matches the backup recipient:

   ```
   age --decrypt -i /path/to/offline-identity omni-backup-YYYYMMDD.key.age |
     docker compose -f docker-compose.prod.yml run --rm --no-deps -T --user root api \
       sh -eu -c 'umask 077; mkdir -p /var/lib/omni; cat > /var/lib/omni/credential.key; chown -R 10001:10001 /var/lib/omni; chmod 700 /var/lib/omni; chmod 600 /var/lib/omni/credential.key'
   ```

   The file is named `credential.key` with mode 600 under `/var/lib/omni`
   (owner 10001) -- the same location and modes the application itself uses.

   Then start the stack. Restore is deliberately not a UI button: it writes
   over a live database, and a browser-upload path to that action is a
   self-destruct button wearing a friendly label.

One licensing note: the dump contains every claim fetched under your keys,
including byo_only data licensed to you. Moving YOUR dump to YOUR new machine
is personal use. Publishing or sharing the file is redistribution -- don't.
