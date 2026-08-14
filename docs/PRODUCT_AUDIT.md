# Omni Analyst Product Audit

**Living engineering review queue.** This is the single place for evidence-backed
product, UX, integration, operational, and Neutron-adoption findings. It is not
a statement of live state; `docs/OMNI_ANALYST.md` remains the operating document.

Update a finding in place when its evidence or status changes. Do not create a
new session audit beside this file. Neutron framework defects belong in
`Neutron/docs/ADOPTION_FINDINGS.md`, with a link here where useful.

## How To Use This

- `P0` blocks exposing the affected capability to more users or relying on it
  with more capital.
- `P1` is a correctness, security, or trust problem that should be scheduled
  before polish work.
- `P2` is important product/operational debt, but does not invalidate a current
  user-visible claim by itself.
- A documented limitation is not repeated here unless this audit found a
  separate defect or a misleading product surface around it.

## Current Findings

### P0

#### OA-001 - Venue connections cross user boundaries

**Status: FIXED 2026-08-14.** Live adapters are now keyed by user and venue, and
every accessor requires the owner. Reconciliation is serialized per user,
visits every active configured account, removes stale owners, and process
shutdown closes every adapter even when one close fails. Direct and HTTP
two-user regressions prove one account cannot read, reuse, or disconnect another
account's venue.

`src/omni/venue/manager.py:43` holds connected venues in one process-global map
keyed only by venue name. `refresh_venues()` reuses that map for every user
(`:57-100`), and `GET /settings/venues/status` returns positions and balances of
every connected venue (`src/omni/api/settings.py:356-391`). A second account can
therefore see another account's Questrade/IBKR data or disconnect its venue.
Credentials themselves are user-scoped, so this defeats the boundary after
storage. No test exercises two users.

**Fix direction:** key active connections and status by user, scope all reads and
disconnects to the authenticated user, and add a two-user isolation test.

#### OA-002 - Public risk monitor reads BYO market data

**Status: FIXED 2026-08-14.** `/scanner/risk` now authenticates before any read,
uses the established owned-portfolio resolver, reads prices through
`visible_claims_cte`, and never serves a cached result from another audience or
an earlier book state. Regressions cover anonymous/inactive callers, foreign and
missing portfolios, another user's CCXT history, owner-visible history, selected
portfolios, and a flat book that changes after the first request.

`GET /scanner/risk` has no audience resolution
(`src/omni/api/risk_monitor.py:187-200`). Its price query reads all CCXT claims
without `visible_claims_cte` or an audience predicate (`:36-55`), although the
credential catalog classifies exchange data as `byo_only`. This publishes a
derived view of private market data and global portfolio state.

**Fix direction:** authenticate the endpoint, derive its portfolio and claims
from the caller's audience, and test unauthenticated and cross-user refusal.

#### OA-003 - Carry runner permits concurrent live cycles

`ops/carry_cycle.sh:11-15` has neither an OS nor database lock. The runner reads
the prior boundary, trades, and only then writes its cycle record
(`src/omni/trading/carry_runner.py:329-439`). The unique key includes the
caller's current instant, so overlapping invocations do not conflict
(`ops/cycle_one.py:86-99`, `migrations/048_carry_cycle.sql:72`). A manual rerun,
duplicate cron entry, or overlap can submit duplicate orders.

**Fix direction:** make cycle ownership an atomic DB lease keyed by venue before
any venue call; use `flock` in the cron wrapper as a second guard; test two
concurrent runners.

### P1

#### OA-004 - Risk verdict is not PCA-based or notional-aware

`_pca_risk()` calculates the PC projection and discards it
(`src/omni/api/risk_monitor.py:119-126`). Its verdict instead uses the sum of raw
asset quantities (`:123-136`), so `+1 BTC` and `-1 ETH` reads delta-neutral despite
different USD notionals and factor loadings. This contradicts the endpoint's PCA
claim and can understate live risk.

**Fix direction:** value legs at point-in-time prices, calculate and expose the
normalised PC exposure, and test a mismatched BTC/ETH pair as factor-exposed.

#### OA-005 - Questrade execution is both out of scope and malformed

The product documents Questrade as read-only, yet
`src/omni/venue/questrade_venue.py:144-156` can submit orders. Its order builder
uses `BuyAll`/`SellAll` rather than the provider's `Buy`/`Sell` sides (`:271-284`).
The existing fake accepts any payload, so its tests do not prove the wire
contract.

**Fix direction:** remove/disable execution until the product explicitly widens
scope, then use the documented order values and assert the real request body.

#### OA-006 - Questrade rotated tokens are lost after restart

The adapter adopts a refreshed token in memory
(`src/omni/venue/questrade_venue.py:232-241`), but it is never written back to

**Fix direction:** persist rotation through the encrypted credential store and
add a restart-shaped test.

#### OA-007 - IBKR is presented as connectable but cannot connect

The Settings catalog says the system manages an IB Gateway and the toggle says it
will start one (`src/omni/api/settings.py:86-96,352-354`), but production compose
has no Gateway service and `ib_async` is not installed. Separately, the manager
passes `username`, `password`, and `mode` to `IBKRVenue.connect()`
(`src/omni/venue/manager.py:145-156`), whose signature accepts none of them
(`src/omni/venue/ibkr_venue.py:85-92`), so it raises a caught `TypeError` even if
a Gateway is supplied.

**Fix direction:** remove the configuration surface until a paper-first Gateway
deployment exists, or ship the dependency/container and repair the contract as
one tested vertical slice.

#### OA-008 - Settings is not yet a truthful connection centre

Settings reports only whether provider keys exist in deployment configuration;
it has no user flow to add or rotate FRED, Polygon, CoinGecko, Etherscan, or SEC
credentials (`ui/src/components/SettingsView.tsx:77-104`). It labels saved
`enabled` state as `Connected` (`ui/src/components/VenueCard.tsx:67-90`) while
never reading the live status endpoint (`src/omni/api/settings.py:356-391`), and
does not show a last check or connection error. Hyperliquid remaining deployment
only is deliberate and correct because the API must not receive its trading key.

**Fix direction:** separate deployment-managed credentials from encrypted BYO
credentials, show live connection state/timestamp/error, and never represent
desired state as actual connectivity.

#### OA-009 - Settings renders IBKR mode as a free-text field

The API declares `mode` as a `select` with `paper` and `live`
(`src/omni/api/settings.py:91-96`), but `VenueField` excludes `select`
(`ui/src/lib/settings.ts:5-10`) and `VenueCard` renders it as
`<input type="select">` (`ui/src/components/VenueCard.tsx:132-161`). Browsers
treat that as an unconstrained text input, allowing values outside the contract.

**Fix direction:** model select fields and options in the TypeScript contract,
render `<select>`, validate required fields before submit, and add a component
test.

#### OA-010 - Polygon can record invalid bars as coverage

`parse_aggregates()` validates only a timestamp and writes all OHLCV fields
unchanged (`src/omni/ingest/polygon.py:112-137`). A missing, non-finite, or
nonnumeric close then satisfies coverage and suppresses refetching while price
consumers cannot use it.

**Fix direction:** refuse malformed and non-finite OHLCV data before drafting a
claim; add parser and fill-path failure tests.

#### OA-011 - Microstructure adapter conflicts with credential policy

The adapter says public OKX microstructure data is shared/allowed
(`src/omni/ingest/microstructure.py:17-25`) but identifies itself as `okx`
(`:269-298`), which the credential catalog marks `byo_only`. The writer then
requires an owner for shared demand and refuses the result. The advertised
shared-coverage path is therefore unfillable.

**Fix direction:** resolve the provider classification and adapter contract,
then test catalog-to-writer behaviour under shared and BYO demand.

#### OA-012 - Auth transitions can leave stale private UI visible

The layout reads authentication in a mount-only effect
(`ui/src/routes/_layout.tsx:80-116`), while login stores a token and
client-navigates (`ui/src/components/LoginView.tsx:48-51`). Header state can stay
signed out after login; sign-out clears the token without clearing rendered
private data or navigating (`_layout.tsx:120-123,211`).

**Fix direction:** make auth state reactive, clear sensitive route data on
sign-out, redirect to a public route, and cover login/logout in browser tests.

#### OA-013 - Conviction target accepts unsafe configuration values

`TARGET_HIT_RATE` is an unvalidated float (`src/omni/config.py:68-78`) passed to
the live scheduler (`src/omni/scheduler/__main__.py:52-55`). A negative value can
admit every calibrated bucket; `NaN` or a value above one silently suppresses all
findings. Trading policy already validates comparable probability inputs.

**Fix direction:** reject non-finite values outside `[0, 1]` during configuration
load and test startup failure for invalid input.

#### OA-014 - Cron wrappers hide failed operational runs

`ops/carry_cycle.sh:11-15` and `ops/shadow_book.sh:15-19` omit `set -e`; after a
failed `docker compose exec`, their final `echo` becomes the successful process
exit. The failure is only appended to a host log and produces neither a refusal
record nor a health signal.

**Fix direction:** preserve the command exit code, alert on it, and add an
execution/heartbeat record for each cron-driven task.

### P2

#### OA-015 - System health omits autonomous and cron work

The System view reflects core scheduler-loop health, not autonomous loops, venue
reconciliation, carry, NAV, shadow-book, or launch cron jobs
(`src/omni/scheduler/worker.py:399-414`,
`src/omni/autonomous/runner.py:241-256`, `docs/OMNI_ANALYST.md:119-129`). It can
therefore read healthy while a required process has stopped.

**Fix direction:** persist bounded heartbeats/results for every scheduled unit
and render the last successful run and error in System.

#### OA-016 - Alerts have no browser lifecycle controls

The backend supports update/pause and delete (`src/omni/api/alerts.py:173-215`),
(`ui/src/lib/alerts.ts:86-95`, `ui/src/components/AlertsView.tsx:145-177`). Users
cannot correct, pause, or remove an alert.

**Fix direction:** expose pause/resume, edit, and delete with explicit feedback
and a test for each lifecycle transition.

#### OA-017 - Empty states can conceal failed requests

A failed wallet request reaches “No external wallets tracked”
(`ui/src/components/WalletAccounts.tsx:87-98,262-279`), and a failed research
request removes that section (`ui/src/components/SystemView.tsx:58-68,175-266`).
Both turn unavailable data into an honest-looking absence.

**Fix direction:** preserve the error state in context and render a named
unavailable/retry state instead of an empty result.

#### OA-018 - Backup executable is not source-controlled

`ops/backup.sh` says it is the installed script, while the operating document
says production runs a different unversioned `/opt/omni-backup.sh` with
unverified off-box behaviour (`ops/backup.sh:18-29`,
`docs/OMNI_ANALYST.md:642-658`). The repository therefore cannot specify the
backup process it claims to protect the database with.

**Fix direction:** reconcile the host script with the versioned one, verify a
restore, set and test an off-box target, then update the operating document.

#### OA-019 - Deployed Neutron revision is not traceable

Tests use an editable sibling checkout (`pyproject.toml:56-57`), while images use
an untracked operator-built wheel (`Dockerfile:46-53`, `Dockerfile.scheduler:38-43`).
Deployment copies the wheel without recording the framework revision. A release
cannot prove which Neutron source was exercised or deployed.

**Fix direction:** record the Neutron commit/version in the wheel and image
metadata, expose it in deployment verification, and reject a stale wheel.

#### OA-020 - No Omni MCP surface exists

The product has no MCP client, server, route, or Settings configuration. Neutron
does provide an MCP server capability, but Omni neither imports nor mounts it
(`src/omni/main.py:3-69`, `Caddyfile:34-43`). This is a product gap, not a broken
integration; do not present it as available until its permissions, tool set, and
user setup flow are designed.

## Second-Pass Findings

### P0

#### OA-021 - Carry execution bypasses portfolio risk limits

The live carry path executes after reconciliation without `RiskLimits` or
`risk.check` (`src/omni/trading/carry_loop.py:890-1188`); its cron entry supplies
only `CarryConfig` (`ops/cycle_one.py:70-99`). Daily loss, gross notional, and
drawdown inputs therefore cannot stop a carry rebalance.

**Fix direction:** state and enforce a carry risk policy before venue calls; test
that a failed risk check produces no order.

#### OA-022 - Sector scores can mix private price histories across audiences

**Status: FIXED 2026-08-14.** The sector scan now uses one point-in-time boundary
and audience-scoped price histories carrying claim IDs and licence metadata.
Every target score declares the full target/peer/regime panel that determined
its trend and percentile, derives its licence and dates from those same inputs,
and scopes idempotency by audience. Mixed-owner, peer-taint, knowledge-cutoff,
complete-edge, output-date, audience-idempotency, and invalid-close regressions
all pass.

`src/omni/autonomous/reading.py:161-184` reads history without visibility
filtering. `sector.py:144` derives a score from the mixed window but records only
the newest claim as provenance (`:155-167,238-263`), allowing private historical
prices to influence another user's or shared output.

**Fix direction:** scope every input to one audience, record all material inputs,
and add mixed-audience history tests.

#### OA-023 - Initial account setup races under concurrent requests

**Status: FIXED 2026-08-14.** Migration 059 adds explicit `operator` and `member`
roles plus a partial unique index that permits exactly one operator. Existing
deployments promote the oldest account during migration. First setup now uses a
single conditional operator insert, with the database constraint arbitrating a
concurrent race. `test_concurrent_setup_creates_exactly_one_operator` runs two
setup requests together and proves one account and one operator remain. A
disposable 058-to-059 upgrade with two existing users promoted only the oldest
and rejected a second operator at the unique index.

`/auth/setup` counts users then creates one in separate statements
(`src/omni/api/auth.py:94-99`); the schema has no singleton-operator constraint
(`migrations/007_users.sql:13-21`). Two concurrent requests with different emails
can both observe an empty system and provision accounts.

**Fix direction:** enforce the setup gate transactionally/in schema and test two
concurrent setup attempts.

#### OA-024 - Anonymous objective execution spends credentials and writes demand

**Status: FIXED 2026-08-14.** `/objective/run` and `/analysis/run` now require an
active database-backed principal before entity lookup, planning, capability
execution, provider access, or demand writes. The anonymous regression replaces
the planner and executor with failing sentinels and proves neither is called and
the demand count is unchanged.

`POST /objective/run` requires no user (`src/omni/api/objective.py:194-215`),
executes capabilities (`src/omni/orchestrator/run.py:84-107`), and can persist
shortfalls. The planner can select BYO producers with no audience
(`src/omni/orchestrator/planner.py:111-114`), so unauthenticated callers can
spend deployment provider quota and write demand. Existing tests accept it.

**Fix direction:** require an authorized audience before planning/execution and
test that anonymous calls make no provider request or DB write.

### P1

#### OA-025 - Prediction resolution can look ahead through knowledge date

Prediction price paths are filtered by event time, not `knowledge_date`
(`src/omni/conviction/ledger.py:102-113`). The resolver's `now` limits due rows
only (`:414-432`), allowing delayed-available prices into historical outcomes.

**Fix direction:** bound marks by the resolution cutoff's knowledge date and add
a delayed-knowledge regression test.

#### OA-026 - Unmapped carry holdings can lose funding accrual permanently

Unknown held pairs raise only a transient refusal
(`src/omni/trading/carry_loop.py:956-969`); funding is queried only for mapped
IDs (`:1027-1034`) while the interval still settles (`:1183-1187`) and advances
the carry boundary (`src/omni/trading/carry_runner.py:433-439`).

**Fix direction:** do not advance the boundary while any held pair is unresolved.

#### OA-027 - The forward shadow book is never scored in production

`ops/shadow_book.sh:17-18` runs only the decision writer, which never calls
`unscored_decisions`, `score_decision`, or `record_outcome`
(`ops/shadow_book_record.py:84-116`). The forward record will contain decisions
without measurable outcomes.

**Fix direction:** schedule a separate point-in-time scoring pass and test its
production invocation.

#### OA-028 - Any user can read global operator telemetry and research history

**Status: FIXED 2026-08-14.** `/system/status` and `/research/hypotheses` now
require the database-derived `operator` role. Two-user HTTP regressions prove an
active member receives 403 while the operator retains access.

`/system/status` accepts any valid JWT and returns global operational data,
including errors (`src/omni/api/system.py:49-53,58-180`). `/research/hypotheses`
does likewise (`src/omni/api/research.py:27-33`) over a table with no owner
(`migrations/055_hypothesis_test.sql:20-29`).

**Fix direction:** explicitly make these operator-only or audience-scoped and
cover access with two-user tests.

#### OA-029 - Order ledger accepts and applies overfills

`record_fill()` accepts cumulative quantity over the intended order quantity
(`src/omni/portfolio/orders.py:432-475`); `trade_order` has no upper-bound
constraint (`migrations/034_order_ledger.sql:50-80`), and the trading loop applies
the entire fill to portfolio state (`src/omni/trading/loop.py:384-386`).

**Fix direction:** refuse/reconcile single and cumulative overfills before
position or cash mutation.

#### OA-030 - Deactivated users retain issued access tokens

**Status: FIXED 2026-08-14.** `ActivePrincipalMiddleware` resolves every bearer
token against the current `users.active` row before any route sees an audience.
A token issued before deactivation now resolves as anonymous immediately; the
regression proves `/auth/me` changes from 200 to 401 without reissuing the token.

JWT audience resolution checks token validity but not the user's current `active`
state (`src/omni/auth/__init__.py:57-81`). Deactivation prevents a new login but
not use of a previously issued token on private routes.

**Fix direction:** check active status or revoke tokens on every authenticated
request; test a pre-deactivation token is refused.

#### OA-031 - Removing one watchlist entry disables unrelated demand

`remove_entity()` deactivates every matching direct demand for a user/entity
(`src/omni/watchlist/lists.py:200-214`), so removal can withdraw another list's
demand, direct attention, or alert-created demand.

**Fix direction:** persist the entry-to-demand relation and withdraw only that
entry's demand.

#### OA-032 - Finding synthesis mutates an append-only record

Findings are claimed append-only (`src/omni/conviction/publish.py:110-117`), but
synthesis reads newest sector/regime evidence without an as-of bound
(`src/omni/autonomous/synthesis.py:37-70,107-137`) and updates the original
finding (`:149-153`). It can attach future evidence with no revision trail.

**Fix direction:** immutable, point-in-time enrichment revisions, or enrich only
before publish.

#### OA-033 - “Operator-only” registration has no operator boundary

**Status: FIXED 2026-08-14.** The first account is the sole operator and accounts
it creates default to `member`. `/auth/register` checks the role loaded from the
database on the current request; a two-user regression proves a member cannot
create a third account and no row is written.

Registration accepts any valid JWT (`src/omni/api/auth.py:116-133`) and the schema
has no role field. The first account can create another, which can create more.

**Fix direction:** model and enforce the operator role; test a non-operator is
denied user creation.

#### OA-034 - Generic Settings writes can erase encrypted credentials

`POST /settings` shallow-merges `venues` (`src/omni/api/settings.py:226-231`), so
an enabled-only update can replace and drop its encrypted credential object. Its
read-modify-write pattern also loses concurrent changes.

**Fix direction:** remove generic mutation or use narrow atomic/versioned writes;
test retention and concurrent updates.

#### OA-035 - ETF ratios can fabricate significance from float noise

`src/omni/research/etf_replication.py:195-205` divides Sharpe and information
ratio by any positive float deviation. Constant decimal-like returns can produce
tiny binary noise and arbitrary ratios; existing flat-return tests do not assert
the metrics.

**Fix direction:** use one scale-appropriate zero-volatility tolerance and test
constant/near-constant series.

#### OA-039 - Empty portfolios prevent the wallet surface from rendering

An authenticated account with no managed portfolio receives `404 No portfolio
for this account` from `GET /trading/portfolio`. Portfolio then renders its error
state, so `WalletAccounts` never mounts and a user cannot start read-only wallet
tracking from an empty account.

**Fix direction:** return an explicit empty managed-book state and keep the
independent external-wallet workflow available.

#### OA-040 - First-run Setup does not submit or surface validation failures

Same-origin Chromium validation found that mismatched/short passwords showed no
error and a valid submission issued no `POST /auth/setup`. This blocks browser
onboarding despite the API working when invoked directly.

**Fix direction:** repair client-side submission/error state and add a rendered
setup-flow test that asserts the request and resulting redirect.

### P2

#### OA-036 - Migration tests do not represent production upgrades

Fixtures create an empty database and apply all migrations
(`tests/conftest.py:43-76`). No test upgrades persisted claims, findings,
predictions, or credentials from an earlier schema. A data-incompatible migration
can pass CI and prevent the app from booting.

**Fix direction:** test selected historical schemas with representative data.

#### OA-037 - CI does not build or boot production images

The CI gate runs Python and UI checks but never builds the operator-created
Neutron wheel or either production image (`.github/workflows/gate.yml:44-92`).
Docker/runtime/migration-layout failures are discovered only at deployment.

**Fix direction:** build the wheel and images in CI, then boot health/migration
smoke containers.

#### OA-038 - UI tests do not exercise rendered user workflows

Vitest runs Node-only library tests (`ui/vitest.config.ts:3-8`), with no rendered
component or browser coverage. Auth, Settings, objectives, alerts, wallet errors,
keyboard paths, and mobile layouts can regress while typecheck/build stay green.

**Fix direction:** add DOM component tests and a small browser workflow suite.

#### OA-041 - Alerts produce hydration mismatches

Each desktop and mobile Alerts page load emitted three SSR hydration-mismatch
console errors in Chromium. The lifecycle API works, but hydration errors make
rendered state unreliable and mask later browser regressions.

**Fix direction:** identify server/client output divergence and add a browser
test that fails on console hydration errors.

#### OA-042 - The documented local UI/API dev arrangement is CORS-blocked

With UI at `:5173` and API at `:8000`, `OPTIONS /auth/login` returns 405 and the
browser blocks requests. Production is same-origin through Caddy, so this is a
local developer workflow defect rather than a demonstrated public outage.

**Fix direction:** proxy API requests through the UI dev server or configure
development CORS deliberately.

## Neutron Findings

`A-023` in `Neutron/docs/ADOPTION_FINDINGS.md` tracks the confirmed concurrent
first-boot migration race found through Omni's deployment model.

## Dynamic Validation

The following passes ran locally or against public unauthenticated production
routes without writing repository files, using credentials, submitting orders,
or touching production data.

### Two-User Isolation

A freshly migrated local database dynamically confirmed OA-001, OA-002, OA-023,
OA-024, OA-028, OA-030, OA-033, and OA-034. The probe demonstrated that Bob could
read Alice's connected venue data and disconnect it; anonymous risk and objective
requests returned 200/201 against Alice-owned state; concurrent setup created two
accounts; deactivation did not invalidate Alice's issued token; and a generic
Settings update removed stored credentials.

The focused existing suite still passed: 133 tests in 33.73s. That is evidence of
missing/discriminating coverage, not evidence against the reproduced defects.

### Provider And Trading Contracts

Offline/isolated probes confirmed OA-004 through OA-007, OA-010, OA-011, OA-013,
OA-021, OA-025 through OA-027, OA-029, and OA-035. Notable measured outcomes:

- `+1 BTC` and `-1 ETH` returned `delta_neutral` with PC1 share 1.0.
- A delayed-knowledge price resolved a prediction outcome.
- An 11-unit fill completed a 10-unit order.
- Near-constant returns produced a Sharpe ratio of `1.32e17`.
- IBKR manager-to-adapter connection raised `TypeError` before any Gateway call.

Existing provider and trading suites passed despite these probes: 69 provider
tests in 3.30s and 155 ledger/carry/shadow-book tests in 30.43s. Several encode
the defective result, named in the relevant findings.

### Browser And Mobile

Production Playwright Chromium checks confirmed unauthenticated `/settings`
redirects to login, invalid login reports a 401 error, and the login page has no
runtime error or horizontal overflow at 390px.

Locally, UI typecheck, 250 tests, and production build pass; the backend suite
passed 4,339 tests after starting the pre-existing local database. OA-008,
OA-009, OA-016, OA-017, and OA-038 remain confirmed. The documented dev-server
arrangement has a separate local CORS defect: UI `:5173` calls API `:8000`, but
`OPTIONS /auth/login` returns 405. Production is same-origin through Caddy, so
this has not been shown to affect production. An isolated same-origin Chromium
pass exercised Setup, login/logout, Settings, alerts, wallet validation, and
390px layouts. It confirmed OA-039 through OA-042; no real credentials, wallet
extensions, or physical devices were used.

### Deployment And Restore

Both production Dockerfiles built locally after the Neutron wheel prerequisite.
Compose validates when required secrets are provided. This confirms OA-037 is a
coverage gap rather than a present build failure. OA-003, OA-014, OA-018,
OA-019, OA-023, OA-036, and Neutron A-023 remain confirmed.

The restore drill passed on 2026-08-14. The 180 MB latest dump restored to a
disposable production-host database in 49 seconds, verified 55 migrations and
1,312,515 claims, then the 1.28 GB temporary database was dropped. Services
remained healthy throughout. The procedure, retained for the next drill, is:

```bash
cd /home/user/omni-v2
DUMP=/opt/omni-backups/<known-good>.dump
VERIFY_DB=omni_restore_verify_$(date -u +%Y%m%dT%H%M%SZ)

docker exec -i omni_postgres pg_restore --list < "$DUMP" >/dev/null
docker exec omni_postgres createdb -U postgres "$VERIFY_DB"
docker exec -i omni_postgres pg_restore --exit-on-error -U postgres -d "$VERIFY_DB" < "$DUMP"
docker exec omni_postgres psql -U postgres -d "$VERIFY_DB" -Atc \
  "SELECT max(version), count(*) FROM _neutron_migrations;
   SELECT to_regclass('public.claim'), to_regclass('public.prediction'), to_regclass('public.users');"
docker exec omni_postgres dropdb -U postgres "$VERIFY_DB"
```

Do not stop Postgres before a container-executed restore. For actual recovery,
stop writers (`api`, `scheduler`) rather than the database container.

## Next Passes

1. After fixes, turn every dynamically reproduced OA finding into a committed
regression test, then re-run it against a fresh isolated compose boot.
2. Repeat the disposable restore with a dump created after migrations 056-058,
and configure/test `OMNI_RSYNC_TARGET` against an independent host.
3. Validate repaired Questrade and IBKR paths with operator-owned credentials in
read-only/paper mode only. The current IBKR container and adapter must be fixed
before this is meaningful.
4. Run Safari, Chrome Android, and physical wallet-extension/hardware workflows
on real devices. Chromium emulation cannot verify those browser integrations.
