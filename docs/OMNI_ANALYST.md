# Omni Analyst

**The operating document for this repository.** Current state, architecture,
invariants, deployment, and open work. Everything else in `docs/` and
`_orchestrator/` is either evidence, a deeper reference, or archived history.

Last measured: **2026-08-12**. Every number below was read from the live system
or the working tree on that date, not copied from an earlier document. Several
move every session — the re-measure commands are at the end. Prefer measurement
over this file, including when this file is confident.

---

## 1. What this is

A **demand-driven coverage network with agentic gap-filling**, built on Neutron
(Python tier) against the PostgreSQL wire protocol.

The system holds *coverage* — claims about entities, each carrying provenance,
freshness and confidence. Attention directed at an entity produces a gap between
demanded and actual coverage; the scheduler dispatches fills to close it. Reports
are views over coverage. Findings that clear a calibration-derived conviction
threshold surface unprompted with their evidence and a recorded prediction.

That is the architecture. **The thing that currently makes money is narrower:**
a delta-neutral funding-carry book on Hyperliquid. Forty-eight directional
research hypotheses have been tested and every one failed. The coverage network
is the apparatus; the carry book is the only measured edge running on it. Hold
both facts at once — the architecture is not vindicated by the carry book, and
the carry book is not diminished by the failed hypotheses.

### Product surface

Four pages, deliberately small. The backend is broad; the user funnel is narrow,
ranked, legible, and candid about what was measured.

| Route | Page | Shows |
|---|---|---|
| `/` | Portfolio | Trading NAV, carry APR, delta-neutrality, pair tags, positions, cycle history, cash. External read-only wallet balances shown **separately**, never added to NAV. |
| `/search` | Discover | Ranked stocks/ETFs, defensive assets, crypto, sector leaders, entity search, watchlists, alerts, explicit coverage audit. |
| `/system` | System | Engine health, loop status, demand, fills, provider state. |
| `/settings` | Settings | Venue toggles, data-provider configuration. Still more status screen than control centre — see §8 P1. |

Header carries brand and nav left; search (Cmd+K), the bulletin pin, and a gear
dropdown right.

### Standing design preferences

- Minimal and clean, but not empty or artificially limited.
- Rank the best measured choices within useful categories; show roughly 10-15
  candidates so they can be compared, not one mysterious winner.
- Stocks/ETFs, crypto and defensive assets need explicit organisation.
- Every metric states its horizon and meaning. No unexplained percentage.
- Keep the heavy processing in the background; surface the conclusions.
- **Never hide missing coverage.** The Monero omission is why the governed
  crypto census and the explicit coverage audit exist.
- ETFs are the preferred portfolio core unless a point-in-time, costed
  experiment proves a custom portfolio beats them. As of §6, none has.
- External wallets are read-only and non-custodial, always.

---

## 2. Live production state

Measured 2026-08-12 against `deployment-host` (the-host, Tailscale).

```
containers     omni-v2-api-1        running
               omni-v2-scheduler-1  running
               omni_postgres        running   (TimescaleDB)
               caddy                shared, outside the app compose project

migrations     55  (local tree and live DB agree)
host path      /home/user/omni-v2
public         http://app.omnianalyst.com   (Cloudflare terminates public TLS)
```

### The carry book

```
NAV            $210.12        cash $72.40 free
net exposure   0.00000        delta-neutral, confirmed by PCA
positions      ETH/USDC  spot  +0.0365   ETH/USDC:USDC  perp  -0.0365
               SOL/USDC  spot  +0.91     SOL/USDC:USDC  perp  -0.91
venue          Hyperliquid
next rebalance ~2026-09-22    (six-week hold, enforced by the runner)
```

Live since 2026-08-11. Twelve cycles logged in `carry_cycle`. The 04:00 UTC
cycle on 2026-08-12 fired and **correctly refused** — "the hold is 42 days and
the next is due 2026-09-22; rebalancing sooner is turnover the signal did not ask
for" — exiting 2 with the book untouched. Refusals are logged to
`ops/carry_cycle.log` but do not write a `carry_cycle` row, so an absence of rows
between rebalances is expected, not a stalled loop. Check the log, not the table.

The pair gate is also live and has bitten: an earlier cycle refused two pairs
with `the_two_legs_did_not_fill_as_one_unit`. That is why the book holds ETH and
SOL out of six tradeable candidates.

### Scheduled work

```
17 */6 * * *   ops/launch_sweep.sh
40 7   * * *   ops/nav_snapshot.sh
0  21  * * *   ops/carry_cycle.sh     21:00 America/Vancouver = 04:00/05:00 UTC
```

The carry runner independently refuses any cycle outside 03:00-07:00 UTC or
inside the six-week hold, so the cron time is a convenience, not the control.

### Data actually in the store

```
fundamental_metric    715,701     EDGAR, ~20 years
macro_series_point    294,905     FRED, 1947-01-01 onward
price_snapshot        264,155     polygon 257,675 / ccxt 6,480
funding_rate            6,480
holding                   505     SPY constituents
sector_score               66
regime_assessment           5     written daily; latest 2026-08-11
```

**This corrects two earlier documents.** `_orchestrator/STATUS.md` states there
is "no macro data ... not thin data, none at all", and the 2026-08-11 session
note lists FRED as "never ingested" with the regime and sector loops abstaining
every cycle. Both were true when written and are now false: FRED is ingested and
the macro loop is writing `regime_assessment` daily. Do not brief work off either
claim.

The crypto price spine on production is shallow (6,480 rows) relative to the
deep multi-year backfills used in research. Research depth and production depth
are different things; check which one a question needs.

---

## 3. What has been measured

The project's discipline is that a refusal counts as evidence. This table is the
short form; `_orchestrator/GATE_A_FINDINGS.md` is the append-only ledger with all
51 numbered findings and is authoritative on any detail.

```
method                    effective_n   gross         t        verdict
carry basket (capped)          ~2,853   +8.31 %/yr   +36.0    PAYS, net +7.80%/yr
  same, out of sample          ~2,300   +9.12 %/yr   +35.4    PAYS, net +8.76%/yr
trend.sma (crypto)              1,301   +23.64 bps    +1.93   indistinguishable
trend.sma (equities)              268   + 0.14 bps    +0.03   absent
carry.funding                     931   - 3.18 bps    -2.95   reliably negative
basis.crossvenue                  936   -16.90 bps   -31.31   reliably negative
```

**Every directional method fails; the only one that pays is the only one that is
not a prediction.** `carry.funding` and the carry basket read the *same* funding
claims — one turns them into a claim about price and loses, the other holds
delta-neutral and collects. The next strategies should be harvests, not
forecasts, and the harvest shape currently holds exactly one member.

### The carry edge, stated honestly

Cross-sectional funding carry: select the top-quartile funding payers, hold
delta-neutral (long spot, short perp), rebalance every six weeks with hysteresis
(enter on top-5, hold until it leaves top-15). Turnover is what kills carry
trades and very nearly killed this one — at daily rebalancing it nets -10.71%.

- **+7.80% net annualised on notional, t = +36.0**, in sample 2024-26.
- **+8.76% out of sample** on 2021-23, a period the parameters were never fitted
  to and which contains the 2022 bear market. Out-of-sample beating in-sample,
  with the configuration ranking preserved, is the signature of a real effect
  rather than a fitted one.
- **5.85% to 6.50% on capital** at 3x-5x perp margin. That is the honest
  headline, not +7.80%. The leverage that raises the return is the same dial
  that creates liquidation risk: 3x liquidates on a ~33% adverse move, 10x on
  ~10%, and crypto does 10% days. A liquidated perp leg leaves the book naked
  long spot — an outright directional position inside a strategy whose entire
  premise is asserting nothing about direction.
- **The venue matters more than the strategy.** +7.80% is a measurement of
  Binance. Measured paired — same asset, same day — Hyperliquid pays **+4.31%/yr
  more on 9 of the 9 assets both list**, charges 28 bps round trip against 40,
  has a $10 minimum against Binance's $50 BTC floor, and needs no KYC. Its
  six-name tradeable book measured **+11.50%/yr net at t +19.4**, and the paper
  loop returned 11.48% against it over ten cycles with zero halts.
- Tradeable universe is six names: BTC, ETH, SOL, HYPE, PENGU, PURR. WLD, BERA
  and TRUMP resolve on the venue but have no volume and **must be excluded by
  name** — symbol resolution will not keep them out.

### The ceiling

Three independent probes converged: cross-sectional momentum decayed from 220%/yr
(2019-21) to zero (2024-26); unconditional BTC funding from +30%/yr (2021) to
+2%/yr (2026). **The honest ceiling is high single digits to low teens on
notional, 8-11% on capital at 3x.** Widening the universe is actively harmful —
in the tail, high trailing funding predicts *negative* forward funding, so the
30-name limit is protection rather than a limitation.

Closed doors: carry-decay forecasting (failed holdout by 11pp), market making
(maker fee 1.50 bps exceeds the 0.15 bps spread), book-to-market (t=1.22 against
a 3.18 bar), Fear & Greed sentiment, YouTuber predictions, dated-futures
cash-and-carry, Deribit boxes, MVRV. Book-to-market is parked rather than dead:
the signal works and EDGAR is deep, but Polygon's free tier caps equity prices at
two years and a quarterly factor needs five. **Deep equity history is this
project's one non-free edge.**

### The research harness

`src/omni/research/` is where a hypothesis gets tested. `evaluate()` applies five
non-optional guards, each of which nearly let a false positive through on
2026-08-09: a self-test that plants a known edge and refuses to run until it
recovers it; a permutation null calibrated on the same cross-section the
statistic used (crypto's own null 95th percentile runs 2.0-2.3, not 1.96);
gross-and-net always reported, because subtracting near-deterministic costs
*inflates* |t| on a loser; rank-IC checked against portfolio return; and a sweep
of every rebalance offset.

`passed` requires the **most recent third**, never the full sample. Every
strategy this project has retired was significant full-sample. The significance
bar is `sqrt(2 ln N)` over an append-only global count of every test ever run,
floored at 2.5 — not a per-script number. **A test count not recorded on the day
is lost.**

---

## 4. Architecture and invariants

These are load-bearing. Violating one is a correctness bug, not a style issue.
`AGENTS.md` is authoritative and must be read before editing; this is the summary.

### Claims

Every claim carries `source`, `event_date` (when it happened), `knowledge_date`
(when it became knowable), `confidence`, `credential_owner` and
`redistributable`. Bitemporal always — a single `as_of` is not sufficient.
Ingestion is idempotent on
`(entity, claim_type, source, event_date, knowledge_date)`. Freshness is
first-class and visible: a stale network that looks covered is worse than an
empty one, because emptiness is honest.

### Redistribution — the rule most often broken by accident

`credential_owner` is **an access-control key, not metadata.** Providers fall
into three classes (`src/omni/credentials/catalog.py`):

- `allowed` — public-domain or redistributable. Enters shared coverage.
- `byo_only` — commercial terms forbid serving the data on. A claim fetched with
  one is visible **only to its credential owner**, fills only that user's gaps,
  and is never served to another user.
- `prohibited` — never written at all. Yahoo/yfinance is the one entry, and no
  purchased licence can promote it.

Serving one user's BYO-sourced data to another makes this deployment the
redistributor, which the terms forbid. The gap engine computes gaps **per
audience**, never globally. Every query path returning claims must filter on
this. If you are unsure whether a path is audience-scoped, it is not — make it
explicit. Read `src/omni/coverage/visibility.py` before changing any query.

### Point-in-time correctness

- Fundamentals join on `knowledge_date`, not fiscal `event_date`.
- A score at `t` executes no earlier than the following session.
- Current membership applied backward stays labelled exploratory.
- Missing delisting or ticker history is a **refusal**, not a zero return.
- Costs and turnover are explicit.
- A holdout or forward shadow period is required before capital moves.

The cost of getting this wrong is documented: ccxt stamps a bar with its *open*,
not its close, and `parse_ohlcv` set `knowledge_date = event_date` — one full day
of lookahead on a two-day horizon, applied to both signal and entry. Every crypto
figure measured before the fix inherited it.

### Predictions and the conviction gate

The ledger records `direction`, never `action` — analysis, not advice. Barriers
are fixed at write time, because falsifiability requires the threshold exist
before the outcome. Entry price is point-in-time. **Scoring is a separate pass;
the writer never sets outcome fields.** If the price path is unavailable the
prediction stays `pending` — never scored against a substituted path. Conviction
thresholds are **derived from calibration, never chosen**; a claim class with too
few resolved predictions cannot surface at all, and a quiet week is a healthy
outcome.

### The trading boundary

Adapters and credentials existing does not mean trading is enabled.

- Read-only is the default. Paper/live modes, walk-forward gates, reconciliation
  and risk gates stay explicit.
- The API service must not receive scheduler trading secrets.
- Hyperliquid must use an **agent wallet**, never a raw account wallet capable of
  withdrawal. A private key *is* the wallet and cannot have withdrawals disabled.
- Never broaden live-order authority as a side effect of Settings or UI work.
- `risk.check` is **not on the carry path.** The daily-loss kill switch and
  position caps are called from `trading/bridge.py`, the directional loop.
  `carry_loop` calls only `pretrade.evaluate_risk_alerts`, which records firings
  and cannot halt. What protects this book is the pair gate, the reconciler, the
  unwind — and the size. Nothing watches the book between cycles; that is a
  property of the six-week cadence, not a missing feature.

### The wallet boundary

Public addresses only. Never request or store seed phrases or private keys.
Never request a transaction signature for read-only tracking. No fabricated USD
total when pricing is incomplete. Browser address sharing is not cryptographic
ownership proof and the UI says so. External wallet holdings are **not** managed
trading NAV.

### Data honesty

A missing value is not zero. Provider failures stay visible in coverage and error
metadata. Rankings are measurements within a declared universe, not
recommendations. Never cache an empty provider result. Never fabricate: if a
source is unavailable the claim is not written and the fill records `unfillable`
with a reason. **A gap-filler that always produces something is how hallucinated
coverage enters the store.**

### Never compare a float to zero with `==`

This has produced fabricated output in five separate modules, written by five
different agents, each of whom applied the rule correctly elsewhere in the same
file. `np.std` of a constant 0.05 series returns ~1e-17, so `if variance == 0:
raise` never fires, and the function returns a confident number that means
nothing. Guard with a tolerance on a scale-consistent quantity (prefer the
standard deviation over the variance); use `np.ptp(series) == 0` for the exact
case; use one idiom per module; and give `NaN`/`inf` their own refusal, because
every comparison against `NaN` is false.

### The LLM seam, deliberately narrow

`omni.llm` is a protocol and a deterministic fake, not a Fylun client. **Numbers
must never originate from the model.** This is structural rather than advisory:
`ResponseSchema` offers `TextField` and `ChoiceField` and **no `NumberField`**,
every digit-bearing token in returned text must equal one of the request's
measurements, and a sign flip is refused — a direction inversion wearing a real
number is the most plausible and most wrong thing narration can emit.

`ui/src/lib/explain.ts` already narrates findings deterministically. Replacing it
with an LLM would reverse a decision already taken in code, and needs a product
call rather than a work order.

---

## 5. Repository and workspace layout

```
~/Documents/Code Projects/omni-analyst/     umbrella (local git, no remote)
  omni-analyst/                             THIS REPO — the active application
  omni-analyst-website/                     marketing site
  reference/omni-analyst-v1/                the original FastAPI + Next.js build
  reference/OpenPlanter/  reference/worldmonitor/   read-only upstream clones
  _census/                                  v1 assessment and rebuild material
  docs/                                     workspace-level history
```

Each product folder has its own remote and is committed from within that folder.
`reference/` is read-only; never mix its contents into product code.

This repo:

```
src/omni/      Python application — API, research, coverage, trading, scheduler
ui/            Preact/TypeScript Neutron UI
migrations/    NNN_name.sql, run by neutron.nucleus.migrate.Migrator
tests/         pytest, asyncio_mode=auto
ops/           operational and research scripts
docs/          this document, the ETF report, testing notes
_orchestrator/ work orders, agent reports, and the evidence ledger
```

Remote: `http://the-forgejo-host:49152/Tyler/omni-analyst.git`

Neutron is a sibling checkout. Framework defects go to
`Neutron/docs/ADOPTION_FINDINGS.md` — not here, and not in a comment.

### Git state

```
branch            feat/autotrade-phase0
ahead of main     202 commits
main ahead        0 commits
```

`main` is an ancestor and can likely be fast-forwarded, but **production is
running the feature branch, so `main` does not currently represent the
product.** Reconciling this is P0 in §8. Do not squash the accumulated commits;
they contain the ported backend, audits, fixes, UI, deployment work, Discover,
wallets, the ETF experiment and the bulletin.

---

## 6. Completed work

### Discover and market-universe governance

28 broad stock/ETF assets, 12 defensive/real assets, 34 ranked crypto assets, all
11 GICS sectors, top 15 measured companies per sector. Each carries risk tier,
volatility, drawdown, correlation/market role, trailing one-year return,
annualised five- and ten-year results where history permits, median complete
calendar-year return, and a balanced score.

The balanced score is category-relative: 35% durable growth, 25% consistency,
20% stability, 10% one-year return, 10% diversification, with components
reweighted when history is shorter. **It is a measurement, not a recommendation.**

Last live signed-in audit: 11/11 sectors, 463 companies qualifying, 74/74 broad
assets ranked, 34 crypto ranked. The Monero omission is resolved — XMR ranked
11th in crypto at the time of audit. Rankings are time-varying; never hardcode a
position.

### External wallets

User-scoped public-address storage. Phantom (Solana + Ethereum), MetaMask with
EIP-6963 discovery and injected fallback, Ledger via the Ledger Live receive-
address workflow verified on the Nano screen or through MetaMask, and manual
ETH/SOL/BTC entry. Ethereum native plus indexed ERC-20 balances; SOL, SPL and
Token-2022; Bitcoin per address. Refresh one or all, labels, copy, removal,
explicit coverage and error text, and **no fabricated aggregate USD total**.

### Header bulletin

Private notes and safe links in a compact dropdown next to Settings.
HTTP/HTTPS validation with active schemes such as `javascript:` rejected, links
opened with `noopener noreferrer`, account-based server persistence, count badge,
50-item limit. Migration 054.

### The strategy research record (System)

Added 2026-08-12. The research harness had tested 49 hypotheses and rejected all
49, and none of it reached the product — a search that hides its failures reads
either as no search at all or as an unbroken run of successes, and both are
wrong in the direction that flatters.

`GET /research/hypotheses` and a **Strategy research** section on System now
expose every recorded test: its name, data source, statistic count, best |t| on
the recent third, the bar it was judged against, and whether it cleared. The
summary carries the current bar, the FDR bar, and the best result so far.

The mechanism matters more than the panel:

- The JSONL registry at `_orchestrator/hypothesis_registry.jsonl` remains the
  **single writer**. Migration 055 adds `hypothesis_test` as a one-way mirror,
  because the image ships only `src/` and `migrations/` and a deployed API can
  never read that file.
- Two writers would give two different `N` for `sqrt(2 ln N)`, and the
  disagreement would be invisible because both numbers would look plausible.
  So the bar arithmetic was **extracted into `bar_for` / `fdr_bar_for`** and is
  now called by both the harness and the API. `test_reported_bar_equals_the_bar_the_harness_would_apply`
  pins that: reimplementing the formula in the API — counting tests instead of
  cells — yields 1.48 against the correct 2.79, and the test catches it.
- The mirror is idempotent on `(name, recorded_at)`, so a re-run is a no-op.
  Retesting the same hypothesis is a genuinely new row, not an overwrite.
- Nothing is ever deleted by a sync. An append-only record a sync can silently
  shrink is not append-only.

Publish after a research session, from the machine holding the registry:

```bash
uv run python ops/publish_research.py --dry-run
uv run python ops/publish_research.py
```

Verified end to end on 2026-08-12: 49 entries / 154 statistics mirrored, bar
3.174, FDR bar 2.542, nothing cleared. `STATUS.md` had recorded 47/152 at bar
3.170 — the bar rose with the two additional cells, which is the behaviour
`sqrt(2 ln N)` requires and a useful cross-check that the extraction preserved
the arithmetic.

### ETF versus constituent experiment

Full report: `docs/ETF_PORTFOLIO_EXPERIMENT.md`.

A reusable, costed harness comparing sector ETF, equal-weight constituents,
ranked top-N and an 80/20 hybrid. Decision at close `t`, costs and target weights
applied before the return at `t+1`. Turnover, ETF spread and annual expense
charged.

- Equal-weight constituent baskets beat their sector ETF on CAGR in 7/9 testable
  sectors and Sharpe in 7/9, median excess +1.40% — but improved maximum
  drawdown in only 3/9.
- The price-quality top-10 ranker beat only 3/9. Median excess CAGR **-2.70%**;
  a single enormous technology result distorts the mean.
- The 80/20 hybrid usually gave up a little CAGR (median -0.49%) while improving
  maximum drawdown in 8/9.

**The active stock ranker does not pass. ETFs remain the default core.** SPY, XLC
and XLF were refused rather than estimated, because a currently listed
constituent developed an unresolved historical price gap while held — refusing
preserves the rule that a missing mark cannot silently become a zero return.

This is **not decision-grade**: today's membership and sector links are applied
backward, so it is survivorship-biased. Do not present it in the UI as a
recommendation.

---

## 7. Building, deploying, running

### Local

```bash
docker compose up -d postgres
uv sync --extra dev
uv run pytest
uv run uvicorn omni.main:app --reload
```

Health, OpenAPI and docs are provided by Neutron at `/health`, `/openapi.json`
and `/docs`. Do not hand-write them.

### The Neutron wheel prerequisite

`pyproject.toml` declares `neutron-py` as an editable path dependency at
`../../Neutron/python`, outside the repo, so Docker cannot COPY it. Build the
wheel on the host first, from the repository root:

```bash
uv build --wheel --project ../../Neutron/python --out-dir vendor
docker compose -f docker-compose.prod.yml build
```

`vendor/` is operator-created and untracked, like `.env`. If that dependency
moves again, CI breaks the same way and the failure reads as a uv error rather
than a layout problem.

### Required configuration

`src/omni/config.py` is the only source of truth for variable names.

| Variable | Missing behaviour |
|---|---|
| `OMNI_JWT_SECRET` | App starts, but token issuance 500s and every caller is treated as anonymous. No default on purpose; minimum 32 characters. |
| `DATABASE_URL` | Defaults to the dev compose port; inside a container that fails on connect. Compose points it at the `postgres` service. |
| `POSTGRES_PASSWORD` | Prod compose refuses to start. |

Optional credentials (`FRED_API_KEY`, `SEC_USER_AGENT`, `POLYGON_API_KEY`,
`COINGECKO_API_KEY`, `ETHERSCAN_API_KEY`) all degrade the same way: the system
**declines to fetch** rather than failing, records `unfillable` with a named
reason, and leaves coverage empty. An operator reading empty coverage as breakage
should check the fill log, not the claim store.

`LICENSED_REDISTRIBUTION_PROVIDERS` promotes named `byo_only` providers into
shared coverage for this deployment. Only set it if you actually hold the
licence. In multi-tenant use, never set a shared `byo_only` key without one.

### Migrations

There is no migration container. The migrator runs inside the app lifespan on API
startup and in the scheduler's `__main__`, is idempotent, and records applied
versions in `_neutron_migrations`. It relies on a primary key rather than an
advisory lock, so **for the very first boot of a brand-new database, run a single
API replica** until healthy, then scale out.

### Current deployment caveat

Normal Docker builds on `deployment-host` have failed because the host cannot
reliably resolve container-registry DNS. Service containers were given explicit
public DNS in `docker-compose.prod.yml`, which fixed runtime provider access, but
the host-level pull problem may remain.

Recent deployments used an **offline local image patch**: rsync the changed
source/migrations/UI, create a temporary container from the existing local image,
`docker cp` into `/app`, `docker commit` back to `omni-api:latest` or
`omni-scheduler:latest`, recreate with `--no-build`. This kept production
working, but it does not label images with a Git SHA and is easy to make
non-reproducible. Restoring normal builds is P6.

### Shared Caddy

Public routing is the shared `caddy` container at `/deployments/caddy/Caddyfile`,
**not** the compose `edge` service. When adding a top-level API router, update
both the repo `Caddyfile` matcher and **both** Omni matchers in the shared config
(public domain and Tailscale/internal). Page routes must stay out of the `@api`
matcher so they serve the SPA; API paths use `/newpath/*`, not `/newpath`.

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Back up the shared config first — it routes unrelated services.

### Backups

The Postgres volume holds irreplaceable provenance: bitemporal claims, the
prediction ledger, calibration buckets. A single volume loss is total.

**Backups are running.** Verified 2026-08-12: root cron `0 3 * * *` runs
`/opt/omni-backup.sh`, writing daily gzipped dumps to `/opt/omni-backups`
(2026-08-11 dump: 134 MB, growing steadily from 56 MB on 08-07) and logging to
`/var/log/omni-backup.log`.

Two caveats, both open:

- **The script that runs is `/opt/omni-backup.sh`, not the repo's
  `ops/backup.sh`.** They are different files and only the repo copy is
  versioned. Reconcile them, or the documented behaviour is not the running
  behaviour.
- **The dumps are on the same box they protect.** `ops/backup.sh` supports
  off-boxing via `OMNI_RSYNC_TARGET`; whether the running script does, and
  whether it is set, is unverified. As it stands this protects against deletion
  and corruption but not the box dying.

**The restore has never been tested.** An untested backup is a hope, not a
recovery.

### Verification

```bash
ssh deployment-host 'cd /home/user/omni-v2 && docker compose -f docker-compose.prod.yml ps'
ssh deployment-host 'docker exec omni_postgres psql -U postgres -d omni_v2 -Atc "select max(version) from _neutron_migrations;"'
ssh deployment-host 'curl -fsS http://127.0.0.1:49153/health'
ssh deployment-host "curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: app.omnianalyst.com' http://127.0.0.1/bulletin"
```

Unauthenticated `/bulletin` and `/wallets` must return **401**, not 200 — a 200
means the request hit the SPA fallback instead of the API.

---

## 8. Ordered backlog

### P0 — reconcile Git and releases

Production runs a branch 202 commits ahead of `main`. Verify ancestry, run the
release gate, fast-forward or merge, push, and tag a release containing
migrations through 54. Record the exact deployed SHA rather than an unlabelled
local image, and name a rollback target.

**Done when** `main` contains every deployed migration and feature, production's
source commit is queryable without inference, and a rollback target is named.

### P1 — finish Settings as a real control centre

Current truth: `SettingsView.tsx` only calls `GET /settings/config`. Backend
venue-toggle and venue-status endpoints exist but the UI does not use them.
`refresh_venues` runs when those endpoints are called, not at startup or on a
scheduler cycle, despite comments implying otherwise. Production shows FRED and
Polygon configured; Hyperliquid, Questrade and IBKR unconfigured. Production
compose manages no IB Gateway container.

Add real enable/disable controls **only** for venues that can actually connect.
Show live connection status and last-checked time without leaking secrets.
Separate data sources, trading venues and external wallets. Decide whether
Questrade and IBKR stay in scope — if yes, build a secure encrypted credential
path and a managed Gateway; if no, remove the misleading controls. Reconcile
venue state on startup and on a bounded scheduler interval.

**Never let toggling a venue enable live trading by implication.**

**Done when** every visible control changes real state and reports its real
outcome, unavailable integrations do not look configurable, secrets never reach
the browser, and a restart preserves intended state.

### P2 — ETF allocation experiment and forward shadow book

More promising than picking individual stocks. Test fixed diversified allocation,
equal-weight sector ETFs, top-three measured sector ETFs, risk-balanced sector
ETFs, and a stocks/international/bonds/gold/crypto portfolio, under static,
quarterly and threshold rebalancing.

**Start the forward shadow book now.** Persist every decision, its score inputs,
target weights and timestamp *before* the following session. Never revise an old
decision with new data. Compare to fixed ETF baselines after explicit costs. Do
not move capital until a predeclared sample and gate pass.

For decision-grade historical tests, obtain dated ETF constituents and weights,
ticker and share-class history, delisting/merger/bankruptcy returns, reinvested
distributions, roughly ten years of data, and an untouched recent holdout.

### P3 — complete Discover coverage

Industry and sub-industry coverage is explicitly incomplete because verified GICS
industry metadata is not stored. The company universe is S&P 500 / current
large-cap US, not all US or global equities, and membership is static reference
data accessed 2026-08-05. The defensive universe is 12 representative assets.
Seven live top-60 CoinGecko assets were unmapped at the last audit (RAIN, CC,
WLFI, ASTER, M, MORPHO, SKY) — this list changes, so always rerun the live audit.
Signed-in users see audience-owned Polygon company claims; anonymous users cannot,
so anonymous sector coverage can be zero.

Store verified GICS metadata, add dated membership snapshots rather than
overwriting history, resolve or explicitly exclude each live top-census crypto
mapping, govern expansion into mid/small cap and international, and make
signed-out behaviour explicit. **Keep the coverage gate visible; never silently
shrink the comparison set.**

### P4 — portfolio and household view

The page correctly separates trading NAV from external wallet balances but can
communicate the hierarchy better. Stronger visual division; filters driven by
backend classification rather than a hardcoded frontend symbol set; allocation
targets versus actual; ETF/direct/wallet overlap and concentration; cost basis
only when a trustworthy source exists. **Never add wallet balances to trading
NAV.** A combined household number must be separately named and must disclose
incomplete price and chain coverage.

### P5 — wallet coverage

EVM tracking queries Ethereum mainnet only, not Base, Arbitrum, Optimism or
Polygon. Bitcoin is single-address; HD wallets rotate. Ledger is receive-address
or MetaMask-exposed, not Ledger Live account sync. No transaction history, NFTs,
cost basis or tax lots. ERC-20 symbols come from the public index and can be
spammy.

Add explicit chain selection and read-only adapters for major EVM chains, plus
spam filtering and honest pricing coverage. Account/xpub support only after a
privacy and security design — **never ask for a seed or private key.** Optional
ownership proof may request a harmless message signature, but do not call an
unverified address "verified" before real signature validation exists.

### P6 — operations and observability

Fix registry DNS and normal image builds. Live images are `omni-api:latest` and
`omni-scheduler:latest` with no SHA label; the only tagged rollback targets are
`omni-api:rollback-20260809` and `omni-scheduler:rollback-20260809`, four days
behind the running images. **A rollback today loses four days of deploys.**

Daily backups already run (see §7) — what is missing is a pre-migration backup,
a tested restore, and reconciling `/opt/omni-backup.sh` against the repo's
`ops/backup.sh`.

Add deployment smoke tests covering health, migration version, API routing and UI
asset hashes. Track provider latency, failure and coverage-gate regressions.
Alert on degraded loops and stale coverage. Show the release SHA and deploy time
in System. Record deployment history and rollback status.

### P7 — targeted UI refinement

Major redesign is done; avoid another broad visual rewrite without specific
evidence. Useful focused passes: real-device mobile check of the bulletin, wallet
form and ranking tables; entity detail spacing and empty states; System
explanations for degraded providers; Settings controls once P1 lands.

### Not blocked, not scheduled

- **Let the carry book run.** It needs six months before the track record means
  anything. Do not build more strategy on top of it.
- Wire Questrade (read-only; partner-gated for trades) and IBKR (needs
  `ib_async` in the container, a Gateway container, and credentials — paper
  first, no 2FA).
- Ingest more ETF holdings. SPY is in at 505 holdings; VTI and QQQ need
  Vanguard/Invesco sources, GLD/SLV a different URL format.
- Re-run `ops/hold_length_probe.py` once ~168 days of funding history exist
  (currently ~45).

### Blocked on evidence

CEX execution, signers, on-chain routing and equities (roughly 12 tasks) are each
a delivery mechanism for an edge that has been measured and found absent. This is
no longer "we don't know" — it is "we looked, on 7.5 years of correct data, and
the answer is no." What would change it: the `flow` family (the only other
historically backfillable producer, and what `convergence.multistream` needs
before it can fire at all — but ~2.7 hours per address-year, and only 8 of 30
assets are Ethereum-native); a longer horizon that amortises the round trip; or
corroboration across families.

`convergence.multistream` is built, tested, registered, and has fired **0 times
in 320 attempts**, because only the `price` family can currently vote.

---

## 9. Test baseline

```
backend suite   4,141 passed, 0 failed, 9 pre-existing warnings, 5m47s
UI suite          229 passed (19 files)
UI typecheck and production build   passing
```

Both were run in full on 2026-08-12. Over that session the backend went
4,103 -> 4,141 and the UI 196 -> 229, adding coverage for the research record,
the scanner guards, the entity profile and credential encryption.

The backend figure moves every session — the 2026-08-11 handoff recorded 4,092
in roughly seven minutes, and documents before that recorded 3,791 and 3,965.
Re-run it rather than quoting any of them.

Focused baselines: ETF/research/portfolio 136 passed, bulletin/auth/settings
regression 21 passed, ETF-specific 8 passed.

```bash
uv run ruff check <changed-python-files>
uv run pytest <relevant-tests> -q
uv run pytest -q                 # full, slow
cd ui && npm run typecheck && npm test && npm run build
```

Repository-wide `ruff check .` reports several pre-existing warnings in unrelated
ops and research tests. Lint the files you changed; do not rewrite unrelated work
to make a global invocation green.

### Testing rules

- **Assert behaviour, not shape.** `assert "data" in response.json()` proves
  almost nothing.
- **Cover the failure path deliberately.** Every feature needs a case where its
  dependency is unavailable and it fails honestly. This codebase's predecessor
  failed by silently substituting defaults.
- **Never weaken an assertion to make a test green.** Changing `assert x == 42`
  to `assert x is not None` is a failure, not a fix. Never delete a test;
  skipping with a named dependency is acceptable.
- **Prove your test discriminates.** Stub the implementation to something
  deliberately wrong, confirm the test fails, restore it, confirm it passes.
  Audits here have found an optimiser no test could distinguish from equal
  weight, a convexity function replaceable by `return time_to_maturity(...)`,
  vega tests passing for `vega = sigma`, and Monte Carlo tests passing for a fake
  that runs no simulation.
- **A mutation that does not compile proves nothing.** Verify the mutation ran.
- **Assert the invariant, not a snapshot.** `len(price_producers) == 2` breaks on
  a legitimate addition; "every price source is licensed" does not.

---

## 10. Conventions that keep being re-learned

- **A default that silently bounds a result is worse than an error.**
  `fetch_ohlcv` returns a page, and a page looks like an answer. Every downstream
  component did its job correctly on 500 candles and no component's job was to
  ask whether 500 was all of them. Seven years of history were free and never
  requested. When a measurement disappoints, check what bounded the input before
  concluding something about the world.
- **Measure the quantity you are about to act on, not the nearest one to hand.**
  A status line read "23 planner-reachable of ~130". 23 was the builtin adapter
  binder alone; the scheduler's `default_registry()` merges builtin, extracted
  and derived into **142**, all invocable and pinned by test. The work was
  complete before it was scheduled.
- **If a fact is knowable, state it in the work order.** Three agent runs burned
  their whole budget researching facts. Agents implement against stated facts
  well and discover them badly.
- **Cut shared-file seams before fanning out.** Package `__init__.py`, migration
  numbers, and `config.py` / `credentials/catalog.py` drift sets are
  orchestrator-only; the drift tests require provider, config field and adapter
  to land together, so they cannot be split across agents.
- **"It looks right now" was never evidence.** Six defects distorted the headline
  measurement, each found only after the previous version looked convincing. Four
  made a losing configuration look profitable, one made the question look
  unanswerable, one inflated everything measured before it.

---

## 11. Which document is authoritative for what

| File | Authoritative for | Not for |
|---|---|---|
| `docs/OMNI_ANALYST.md` (this) | Current state, invariants, deployment, backlog | Detailed evidence |
| `AGENTS.md` | The rules an agent must follow before editing | State |
| `_orchestrator/GATE_A_FINDINGS.md` | Every measurement and defect, numbered, append-only | Plans |
| `_orchestrator/GOING_LIVE.md` | Operator runbook: key, size, leverage, halts | Why the strategy works |
| `_orchestrator/REMEASURE_RUNBOOK.md` | How to re-run GATE A at depth | Results |
| `_orchestrator/TRADING_API_CONTRACT.md` | The frozen JSON shape API and UI share | Anything else |
| `_orchestrator/RESEARCH_AGENDA.md` | Ranked directions, with priors and traps | State |
| `docs/ETF_PORTFOLIO_EXPERIMENT.md` | The ETF-versus-constituent result | Live allocation |
| `docs/HISTORY.md` | v1 lineage, retired strategies, superseded docs | Anything current |
| `DEPLOY.md` | Detailed build and configuration reference | Live topology |

`GATE_A_FINDINGS.md` is append-only. When a finding overturns an earlier one, the
earlier stays and gets a superseded banner — deleting it would erase the
reasoning that made the correction necessary, which is usually the more useful
half.

---

## 12. Re-measure before trusting this file

```bash
git branch --show-current
git rev-list --count main..HEAD
ls migrations/*.sql | tail -1
uv run pytest -q | tail -1
uv run python -c "from omni.scheduler.registry import default_registry as d; print(len(d()))"

ssh deployment-host 'cd /home/user/omni-v2 && docker compose -f docker-compose.prod.yml ps'
ssh deployment-host 'docker exec omni_postgres psql -U postgres -d omni_v2 -Atc "select claim_type, count(*) from claim group by 1 order by 2 desc;"'
ssh deployment-host 'docker exec omni_postgres psql -U postgres -d omni_v2 -Atc "select nav, cash, net_exposure, taken_at from nav_snapshot order by taken_at desc limit 1;"'
```

---

## 13. Changes on 2026-08-12

Recorded here because several of them correct claims made elsewhere in this
repository's documentation.

### Deployed

- **Migration 055** (`hypothesis_test`) applied; live DB and tree both at 55.
- **Shared Caddy** carries `/research/*` in **both** Omni matchers. Backup at
  `/deployments/caddy/Caddyfile.bak-20260812-141540`.
- Rollback images re-tagged `omni-api:rollback-20260812` /
  `omni-scheduler:rollback-20260812` from the then-running build, replacing
  targets that were four days stale. Pre-migration dump at
  `/tmp/pre-055-20260812-143228.dump` on the host.
- The research record is published: 49 tests, 154 statistics, bar 3.174.

**A deployment trap worth keeping.** The offline image patch creates a
temporary container to `docker cp` into, and **`docker commit` persists that
container's `Cmd`** — so committing from `docker create <img> sleep 1` produced
an image whose command was `sleep 1`, and the API restart-looped. The fix is to
restore the command explicitly:

```bash
docker commit --change 'CMD ["uvicorn","omni.main:app","--host","0.0.0.0","--port","8000"]' "$cid" omni-api:latest
docker commit --change 'CMD ["python","-m","omni.scheduler"]' "$cid" omni-scheduler:latest
```

Patch from the **rollback** tag rather than from `:latest`, or a bad commit
compounds into the next one. This is another reason P6 (restoring normal
builds) is worth doing.

### Scanner corrections

- **Sharpe was being reported for cash equivalents.** The `ann_vol > 0` guard
  caught only the exactly-constant case; SGOV measured 0.205% annualised
  volatility and a real 4.2% return, and reported a **Sharpe of 20.6** (SHV
  19.3). Sharpe is now withheld below `MIN_SHARPE_VOLATILITY` (1.0% annualised).
- **The `high` risk tier is unreachable in the stocks category**, and that is a
  property of the universe rather than a filter. Measured over two years, the
  most volatile of the 28 ranked broad stock ETFs is XLK at 27.1% against a 30%
  cut; the only broad asset above it is SLV at 47.8%, which sits in defensive.
  `/scanner/market` now publishes `risk_census` per category so an unreached
  tier reads as an explicit zero rather than an absent row.

### Entity profile

`GET /entities/{id}/profile` and a rebuilt entity page. The page was a raw
coverage/claims/gaps dump; it now leads with price, trailing returns, risk tier,
volatility, drawdown, correlation, headline EDGAR fundamentals with the filing
each came from, and derived market cap and margins. Coverage and gaps moved
into a disclosure below.

Rules it enforces:

- Fundamentals are newest by **`knowledge_date`**, so a restatement supersedes
  the figure it corrected. Ordering on the fiscal date would keep showing the
  wrong one.
- A margin is refused when its numerator and denominator come from different
  filing periods.
- Everything the store cannot support is `null` with a named reason in
  `limits` — never a zero.

**A bug this caught:** asyncpg returns `jsonb` as a *string*, not a dict. The
first version of `_number` handled only the dict case, so every price silently
became `None` and an entity with hundreds of observations rendered as "no data
stored". Honest-looking and wrong.

### Venue credentials

Encrypted at rest via `omni.credentials.keyring` (Fernet). The key is generated
by Omni on first use into `/var/lib/omni/credential.key`, backed by the new
`omni_keys` volume mounted on **both** api and scheduler; `OMNI_CREDENTIAL_KEY`
overrides it.

- **It refuses to generate a key it cannot persist.** The api and scheduler had
  no persistent volume, and deploys replace containers — a key minted into a
  container filesystem would be lost on the next deploy and every credential
  under it would be unrecoverable. An ephemeral key fails later and silently,
  which is worse than no key.
- A stored value without the `enc:v1:` marker is **refused**, not read as
  plaintext. Accepting it would make an exposed row work as well as a protected
  one and remove any pressure to fix it.
- What this protects: a leaked `pg_dump` (backups run daily and can be rsynced
  off-box). What it does not protect: a compromised host. Nothing app-level
  does.

`refresh_venues()` previously ran **only** when a Settings endpoint was called,
so a venue enabled in the UI stayed disconnected until someone reloaded that
page, and a restart dropped every connection silently — while comments claimed
otherwise. The scheduler now reconciles at boot and every
`RECONCILE_INTERVAL_SECONDS` (360s), with every failure contained so a bad venue
cannot stop the coverage loops booting.

### Still open

- **Companies are not yet ranked in Discover.** The decision was taken to add
  the 505 ingested SPY constituents as a section structurally separate from the
  ETF core; it is not built. Until it is, the stocks category remains
  diversified funds only, and `high` stays empty for the reason above.
- Settings remains read-only in the UI. The encryption and reconciliation it
  needs now exist; the controls do not.
