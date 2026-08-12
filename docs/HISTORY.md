# Omni Analyst — history and decisions

**The record.** Why this codebase is shaped the way it is, what was tried and
retired, and where the superseded documents went.

Nothing here describes the current system. For that, read
`docs/OMNI_ANALYST.md`. Where this file and that one disagree, that one wins —
and the disagreement is probably that this file is describing something that has
since been replaced.

---

## 1. Lineage

There have been two implementations.

**v1** — FastAPI + Next.js, 57 routers, 586 graded features. Now at
`reference/omni-analyst-v1/` in the umbrella workspace. Frozen 2026-07-27 on
branch `fix/ibkr-silent-mock-broker`. **Reference only; do not extend or deploy
it.**

**v2** — this repository. A full rebuild on Neutron, not a port. Python tier for
ingestion, analytics, agents, the gap engine and the scheduler; Preact/TypeScript
for the surface, built natively rather than transplanted.

The umbrella workspace also holds `omni-analyst-website/` (marketing) and
read-only upstream clones under `reference/`.

### Naming history worth knowing

Earlier documents refer to the active app as `app-v2/`, and to sibling folders
`software/`, `website-astro/`, `website-omni-analyst/` and `website-neutron/`.
Those names are gone. The active repo is `omni-analyst/`; the old one is
`reference/omni-analyst-v1/`; the site is `omni-analyst-website/`. A document
using the old names predates 2026-08-08 and its facts should be re-checked, not
assumed.

---

## 2. The v1 census, and what it found

Before deciding what to rebuild, all 57 v1 routers were graded feature by
feature, with every grade cited to `file:line`. Evidence lives in
`_census/findings/W-01..W-20` in the umbrella workspace.

```
grade                count   share
wired                  252     43%     route -> real logic -> real source, passing test
stub                   140     24%     placeholder, hardcoded shape, or honest 501
unknown (untested)      99     17%
orphaned                51      9%     real logic nothing routes to
fabricated              44    7.5%     synthesised output presented as real analysis
```

The 44 fabrications are the reason this codebase's central rule is "never
fabricate data." They included market impact estimated from an invented
`1000 * (10 - i)` order-book ladder, a `whale_score` derived from a hardcoded
$2000 ETH price, `np.random.normal(0.0005, 0.015)` standing in for daily
returns, and hardcoded BTC and ETH prices. Thirty-two were remediated; the
pattern used — raise a named `*Unavailable` rather than substitute a default, and
return 503 — is the ancestor of v2's `raise Unavailable` discipline.

### The lesson that reversed the cut list

The first pass at deciding what to keep was drawn from router names plus census
grades, and reading what the routers actually contained reversed three of four
decisions. The error is worth naming, because it recurs:

> **The census measures what is built, not what is worth building.** A
> `0 wired / n stub` grade means *unimplemented*, not *unwanted*.

Reversed on that basis: `fraud_detection` turned out to be market-manipulation
analysis (wash trading, spoofing, pump-and-dump, layering) rather than payment
fraud — directly protective of inexperienced users. `prediction_markets` had
Polymarket already integrated and offered a natural benchmark for the calibration
ledger: "our model said 62%, the market said 71%, the outcome was yes" is a
richer story than hit rate alone. `analytics_advanced` was the macro synthesis
layer, not a wrapper. `insights` and `intelligence` were the human-facing output
layer.

Order execution was also reinstated after being called the clearest cut. The
product was never purely analytical — BYO broker credentials are the same posture
as BYO data credentials, applied to execution. What survived the correction was
the specific bug: `POST /trading/orders` fabricating `"status": "submitted"`
while working broker clients sat unused.

**The final cut was just `kyc` and `regulatory`** — they assert the obligations
of a regulated entity the project explicitly disclaims — with
`regulatory/position-limits/{symbol}` salvaged into risk. Everything else was
unfinished rather than unwanted.

---

## 3. The rebuild decisions

### Why a rebuild rather than a port

The v1 frontend was 73% `use client` across 71 pages, sized for a product the
census said should shrink drastically. Building the surface natively in Preact
sidestepped the `preact/compat` question entirely — 17 Radix packages,
framer-motion and React 19 never had to survive a transplant.

### Language

**Python + TypeScript. Go when measured. Mojo probably never.**

- **Mojo — no, and not close.** Mojo optimises CPU/GPU numerical kernels; this
  system is I/O bound. The work is: call a provider and wait, call a model and
  wait, write a claim and wait. The analytics are microseconds in numpy. Adopting
  it would buy roughly zero throughput and cost the entire Python ecosystem.
  Where it would earn its place later is one specific hot loop — millions of
  Monte Carlo paths, or self-hosted inference — as a targeted optimisation of a
  proven bottleneck, not a foundation choice.
- **Go — deliberately deferred.** Neutron Go is complete and is the right home
  for a scheduler needing predictable memory at scale. Starting there means two
  deployment units, two model definitions and a wire contract, all paid before
  any evidence the Python scheduler is a bottleneck. The Framework Contract is
  what makes deferring safe: both tiers speak the same protocol, so moving later
  is a contained migration. **The multi-language design is an argument for
  waiting, not for starting polyglot.**

### Data layer

The coverage network wants entities, claims, freshness history, semantic
similarity and relationships — which maps well onto Nucleus's multi-model pitch.
It was also the single biggest risk, because v1 ran on TimescaleDB hypertables
and Nucleus TimeSeries parity was unproven.

**Decision: build against the Postgres wire protocol, which Nucleus speaks.** Run
on Postgres + Timescale, keep Nucleus-specific models out of the core, and switch
when Nucleus proves parity on the point-in-time workload. This keeps the option
without betting the rebuild on it, and makes dogfooding Nucleus a deliberate
later step rather than an unavoidable dependency.

### Why a coverage network at all

Coverage is a **shared, accumulating asset**. Work done for one user's question
improves the network for everyone, so marginal cost per additional user asking
about the same entity approaches zero. That was the only structure found that
survives consumer-scale economics.

### What carried over from v1

| v1 asset | v2 role |
|---|---|
| 57 routers of analysis | the capability library agents call — internal, not product surface |
| Prediction ledger + triple-barrier | the falsifiable subset of the claim store |
| Calibration + market benchmark | tells the gap engine which pipelines to trust |
| `{source, as_of, is_simulated, credential_owner}` | already claim-shaped |
| Credential policy + catalog | ingestion licensing, unchanged |
| Manipulation detector | first native claim producer — already emits claim + evidence |
| Census + stub triage | tells the gap engine which capabilities actually work |

---

## 4. The prediction ledger, and why not paper trading

Paper trading measures a simulated execution engine. A ledger measures whether
the analysis is any good, which is what the product claims. Three reasons, all
evidenced in v1:

1. **Simulated fills flatter.** There was no real order-book depth, and market
   impact was being estimated from an invented ladder. Paper P&L on assumed fills
   inflates returns exactly where it matters — illiquid names, size, fast moves.
   The same fabrication class, wearing an equity curve.
2. **Calibration needs scored claims, not P&L.** "When this system says 70% it is
   right 70% of the time" cannot be derived from an equity curve. It falls
   straight out of a scored ledger.
3. **The pieces already existed.** 244 lines of López de Prado triple-barrier
   labelling (AFML §3) sat orphaned, imported by nothing but its own tests. It is
   the correct scorer for exactly this ledger.

The design decisions that came out of it are now v2 invariants: `direction` and
never `action`; barriers fixed at write time; entry price point-in-time; scoring
as a separate pass; provenance carrying `credential_owner`; and an unavailable
price path leaving the prediction `pending` rather than scored against a
substitute.

---

## 5. Strategy research — what was tried and retired

The full evidence is `_orchestrator/GATE_A_FINDINGS.md`, 51 numbered findings,
append-only. This is the shape of the arc.

**Forty-eight directional hypotheses were tested. All failed.** The headline
method, `trend.sma`, was retired only after six defects were found in the
measurement itself — and the order matters more than the count, because each was
found only after the previous version looked convincing:

1. the gate barred on hit rate, refusing a profitable *shape* outright;
2. expiries scored as assumed zero, inflating pooled expectancy by 46%;
3. the gate was not reading `exit_price`, inflating it again — in the opposite
   direction from the refusal it produced;
4. the default parameters sat in the middle of the losing region;
5. the price spine was one default page deep, so the sample could not answer the
   question at all;
6. every bar carried a day of lookahead, because ccxt stamps a bar with its open.

The first four made a losing configuration look profitable. The fifth made the
question look unanswerable. The sixth inflated everything measured before it.
**Building execution at any point in that sequence would have wired an exchange
to a number that was wrong in a different direction each time.**

The final reading: `trend.sma` at `effective_n` 1,301 on 7.5 years of correctly
stamped crypto data gives +23.64 bps gross, t +1.93, with the lower bound below
zero even at zero cost. On equities, over 268 independent horizons, +0.14 bps at
t +0.03 — given a genuine effect, 268 observations would show more than that.

Then the reversal: **the only thing that pays is the only one that is not a
prediction.** Cross-sectional funding carry nets +7.80% annualised at t +36.0,
and +8.76% out of sample on a period containing the 2022 bear market. It selects
who pays rather than collecting what everyone pays, which is why it survived a
year when unconditional funding collapsed. `carry.funding` and the carry basket
read the *same* claims — one turns them into a claim about price and loses, the
other holds delta-neutral and collects.

Also closed, each with a measurement rather than an opinion: carry-decay
forecasting (failed its holdout by 11pp — the premium bounced where the trend
predicted decline); market making (maker fee 1.50 bps against a 0.15 bps spread);
book-to-market (t 1.22 against a 3.18 bar, parked on Polygon's two-year price
cap rather than dead); Fear & Greed sentiment; YouTuber predictions; long-tail
momentum (and capacity theory's prediction inverted — the illiquid tail is worse
than the liquid head at every horizon); dated-futures cash-and-carry; Deribit
boxes; MVRV across 120 assets. Funding was measured paired across six venues and
none beats Hyperliquid — the unpaired ranking puts two above it and both lose on
the pair.

---

## 6. Market research and positioning

Compiled 2026-08-02 from eight parallel research rounds. Full document:
`../docs/research/RESEARCH.md` in the umbrella workspace. The durable
conclusions:

- **"LLM as writer, not knower" is the winning architecture.** Every credible
  project converges on deterministic code for math, model for synthesis, every
  number cited. This is why `ResponseSchema` has no `NumberField`.
- **Coverage breadth is the real market gap.** Human analysts cover ~20 names
  deeply; the long tail is uncovered. No competitor does demand-driven coverage.
- **BYOK is defensible, and only OpenBB shares it.** It avoids data-redistribution
  compliance entirely and means never becoming a data vendor.
- **The conviction gate plus prediction ledger is genuinely novel.** No competitor
  publishes their hit rate on the things they chose to surface.
- **Citation and provenance is the moat**, and adversarial bull/bear design
  measurably improves output quality.

Closest competitors and the distinction that holds: OpenBB (highest overlap —
agent-first, BYOK, data-agnostic — but interactive where this is autonomous);
Rogo (best funded, enterprise IB only, equity only, no BYOK); Hebbia (analyses
documents you have, rather than discovering gaps you need); Nansen (reactive and
crypto-only); FinceptTerminal (highest-starred, but the opposite philosophy —
personality-AI investment opinions where this refuses model opinions on
direction, and AGPL makes source study a contamination risk).

Note the tension the strategy research later exposed: the positioning above is
about an autonomous research product, while the only measured edge is a carry
harvest. Both are true. Do not let either one quietly rewrite the other.

---

## 7. UI history

The January 2026 v1 redesign — purple/blue palette, glass morphism, gradient
buttons, 45 pages cut to 25 — belongs to the Next.js application and describes
nothing in the current Preact surface. It is retained in the umbrella workspace
at `docs/ui/` for the record. Both files use emoji headings, which violates the
current standard; they are historical artifacts and are not a precedent.

The v2 surface went the other way: navigation reduced to Portfolio, Discover and
System with Settings behind a gear menu, the command control centred in the
header, the blue-box logo treatment removed, Discover reworked into ranked
category views with an explicit coverage audit, and Portfolio simplified to a
summary, holdings filters, a recorded NAV chart and recent activity.

---

## 8. Superseded documents

These were the state of the project when written. They are retained for their
reasoning, not their facts.

| Document | Superseded by | Specifically stale |
|---|---|---|
| `_orchestrator/STATUS.md` | `docs/OMNI_ANALYST.md` | "No macro data ... none at all" — FRED is ingested, 294,905 rows. Tests 3,791; migrations 049. |
| `_orchestrator/HANDOFF.md`, `HANDOFF_2026-08-09/10/11.md`, `HANDOFF_2026-08-11_END.md`, `HANDOFF_POLYMARKET_2026-08-11.md` | `docs/OMNI_ANALYST.md` | Five session notes across three days with three different test counts and three different migration counts. `HANDOFF_2026-08-11_END.md` also lists FRED as never ingested. |
| `_orchestrator/STATE.md` | `docs/OMNI_ANALYST.md` | Superseded by STATUS.md, which is itself now superseded. |
| `docs/HANDOFF_2026-08-11.md` | `docs/OMNI_ANALYST.md` | Accurate on product and boundaries; its Git and test figures have moved. |
| `../docs/archive/HANDOFF_TODO.md` | `docs/OMNI_ANALYST.md` | Already carries its own superseded banner. Claimed "no `.github/workflows/`" when CI existed, and a work order was briefed to build it twice. |
| `../docs/planning/IMPLEMENTATION_PLAN.md` | this file, §3 | An eight-phase plan for the v1 FastAPI codebase. |
| `../docs/planning/KNOWN_ISSUES.md` | nothing | v1 issue log; every item resolved or decided. References folder names that no longer exist. |
| `../_census/*` | this file, §2 | The v1 census and its work orders. Evidence, not instructions. |
| `../docs/ui/*` | this file, §7 | The v1 Next.js redesign. |
| `../docs/research/NEUTRON_FINDINGS.md` | `Neutron/docs/ADOPTION_FINDINGS.md` | Already a pointer. New findings go to the Neutron repo. |

**`AUTOTRADE_PLAN.md` is a special case.** Its architecture stands; its
*sequence* does not, because it assumed the edge would come from directional
prediction. `MONEY_PLAN.md` supersedes its ordering. Read the architecture there
and the ordering here.

**`GATE_A_FINDINGS.md` is never superseded.** It is append-only. When a finding
overturns an earlier one, the earlier stays with a banner, because the reasoning
that made the correction necessary is usually the more useful half.

---

## 9. The recurring failure this project keeps documenting

Every entry above is a variation on one thing: **a confident number that nobody
had checked the provenance of.**

A hardcoded $2000 ETH price inside a whale score. An order-book ladder invented
because real depth was licensed. `np.std` of a constant series returning 1e-17,
so the guard never fired and noise got divided by noise. Five hundred candles
that looked like a complete history. A bar stamped with its open, read as its
close. A registry count of 23 that was one binder rather than the whole. A status
line about CI that was false by the time it was acted on.

Each was written by someone competent who applied the correct rule elsewhere in
the same file. The defence is not care — care was already present. The defence is
structural: bitemporal claims, honest refusal instead of defaults, tolerances
instead of equality, `effective_n` instead of `n`, out-of-sample instead of
full-sample, and a documented preference for measuring over reading, **including
over the documents this project writes about itself.**
