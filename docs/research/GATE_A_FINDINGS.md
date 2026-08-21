# GATE A findings — the first real numbers

Run 2026-08-08 against the live dev database, after seeding the crypto universe
and ingesting a year of real CoinGecko price history plus Binance funding and
open interest. Nothing here is synthetic.

## What was measured

| | |
|---|---|
| Coverage | 15,441 price points, 9 crypto assets, 2025-08-09 → 2026-08-08 |
| Also ingested | 600 funding rates, 180 open-interest points (6 assets, Binance) |
| Backfill | `trend.sma`, 45 non-overlapping cutoffs, 7-day horizon |
| Result | **424 predictions generated and resolved**, 41 abstained |

## The headline, and why the obvious reading is wrong

```
trend.sma / crypto_asset
  n = 424      hits = 145      hit rate = 34.2%      Wilson 95% [0.298, 0.388]
```

The interval sits entirely **below** a coin flip. Read as a hit rate, that is a
strategy that is reliably wrong, and the gate refuses it: `BELOW_HIT_RATE`.

That reading is wrong, and the arithmetic says so:

```
  mean payoff ratio (target / stop) = 4.32 : 1
  wins 145   losses 130   expiry 149

  GROSS expectancy                        +29.2 bps per trade
  net of CEX taker  (10bps/side)           +9.2 bps
  net of CEX maker  (2bps/side)           +25.2 bps
```

**The strategy is profitable.** A 34% hit rate at 4.32:1 is a good trade; the
same hit rate at 1:1 would be ruinous. Hit rate alone cannot tell those apart.

Two things drive the gap between the two readings:

1. **`trend.sma`'s barriers are asymmetric by construction.** Its stop IS the
   moving average — a level the model identifies, close to price — while its
   target is a volatility-scaled move further away. Losing more often than
   winning is the *designed* behaviour of a trend follower.
2. **149 of 424 expired without touching either barrier.** Those are breakeven,
   not losses, but the hit definition scores a directional call that expired as
   a miss. Excluding them, the barrier-resolved rate is 145/275 = **52.7%** —
   a coin flip with a 4.3:1 payoff.

## THE DEFECT — the gate scores the wrong quantity

`trading/policy.py` gates on `hit_rate >= target_hit_rate`, and
`conviction/gate.py` derives its surfacing threshold from realised hit rate.
Neither reads the barrier distances.

That is only valid when payoffs are roughly symmetric. It is not a tuning
problem — it is the wrong statistic:

- a 34% strategy at 4.3:1 earns +29 bps/trade and is **refused**
- a 55% strategy at 0.5:1 loses money and would be **accepted**

The plan (`AUTOTRADE_PLAN.md` GATE B) does say "positive expectancy net of the
venue's real cost model", so the intent was right; the implementation gates on
hit rate and never computes expectancy. `venue/costs.py::gross_expectancy_bps`
already exists and takes exactly the three arguments needed — hit rate, target
bps, stop bps — and nothing calls it from the gate path.

This is mine, from the plan, and it is the single most consequential thing found
in this run.

## The other six producers

```
carry.funding          n=8    UNCALIBRATED   funding history reaches only 2026-07-06;
                                             producer requires 8 consecutive settlements,
                                             so it abstained 52 of 60 times. Correct.
basis.crossvenue       n=0    UNCALIBRATED   needs the same asset at 2+ venues (ccxt)
oi.divergence          n=0    UNCALIBRATED   OI history is one snapshot deep
flow.exchange_reserve  n=0    UNCALIBRATED   needs an Etherscan key
onchain.smart_money    n=0    UNCALIBRATED   needs an Etherscan key
fundamentals.protocol  n=0    UNCALIBRATED   DefiLlama fees not yet ingested
```

Every one refused, none silently. That is the machinery working.

## What the run also proved incidentally

- **The licence rule held against the operator's own scripts.** Both ingestion
  scripts were refused by `writer.py` with `MissingCredentialOwner` until they
  named a credential owner — including for `binance`, which resolves to
  `byo_only` and not `allowed`, the classification corrected earlier the same
  day.
- **Adapters refuse rather than fabricate.** CoinGecko's demo tier returned
  HTTP 401 on 4 of 12 symbols under throttle; each raised `Unavailable` and
  wrote nothing. No partial or interpolated series entered the store.
- **The backfill's point-in-time guard is load-bearing and holds** (verified by
  mutation: letting the producer see past its cutoff fails 3 tests).

## Recommended next actions, in order

1. **Gate on expectancy, not hit rate.** Add barrier distances to the
   calibration read so `policy.eligible` can call
   `costs.gross_expectancy_bps` and compare net-of-cost expectancy against a
   threshold. Keep hit rate as a reported statistic; stop using it as the bar.
   Until this lands, GATE A cannot pass a correctly-designed trend strategy.
2. **Re-run this report afterwards.** `trend.sma` at +9.2 bps net taker /
   +25.2 net maker is the first candidate.
3. **Walk-forward `trend.sma`** over the 424 to check the edge is not fitted.
4. Only then consider Phase 4.

## CORRECTION — the interval above is too tight, and I over-claimed

Three properties of the sample, measured after the fact, weaken the +29.2 bps
result considerably. All three are now computed by `trading/expectancy.py` and
reported alongside the number rather than left to be noticed.

**Effective sample is 44, not 424.** The 424 predictions span only 44 distinct
horizon dates -- nine highly correlated crypto assets resolving on the same day
are close to one observation, not nine. Every confidence interval quoted above
assumed independence and is therefore far too tight. `effective_n` now reports
the distinct-horizon count, which assumes assets are *perfectly* correlated:
the truth sits between 44 and 424, and a gate should stand on the near side.

**35.1% of the P&L is assumed, not measured.** Those predictions expired without
touching a barrier. The ledger records that they expired; it does not record the
price they expired at, so their P&L is scored as zero because nothing else is
available. Over a third of the sample contributes a number nobody observed.
Fixing this properly needs an exit price on the prediction row -- a schema
change, and a genuine gap.

**The result is concentrated.** Per asset:

```
  ADA +114.3   DOT +97.9   ETH +45.2   XRP +40.5   BTC +39.3
  SOL  +32.1   BNB +17.9   LTC  +6.8   UNI -149.1
```

8 of 9 positive, but the pooled mean is carried by ADA and DOT, and one name is
deeply negative. `concentration` reports 25.7% of absolute contribution in the
top name -- computed on absolute P&L so a large winner against a large loser
does not read as diversity just because they cancel.

None of these makes the gate defect less real. They mean the specific number
that revealed it is a weaker piece of evidence than it first appeared, and
saying so is cheaper now than discovering it with capital committed.

## Caveat worth stating

424 predictions across 9 assets over one year at a 7-day horizon is one market
regime. The equity `trend.sma` result (73%, CI [62%, 82%]) came from a different
asset class over a longer span. Nothing here yet distinguishes "trend following
works on crypto" from "trend following worked on crypto in 2025-2026".


---

# UPDATE — measured expiries, and the number falls by half

After migration 044 recorded the price each prediction actually exited at, and
`expectancy.py` was wired to read it, the same backfill was re-run clean.

```
                       expiries assumed zero      expiries measured
n                              2,235                     1,811
gross expectancy             +87.1 bps                 +46.6 bps
net of taker                 +67.1 bps                 +26.6 bps
net of maker                 +83.1 bps                 +42.6 bps
assumed share                    29.8%                      0.0%
concentration                    10.8%                     14.2%
positive entities                27/29                     22/29
effective_n                         65                        65
```

**Scoring expiries as zero was inflating the result by 46%.** It was not the
neutral approximation it looked like: an expiry is only zero if price happened
to close exactly at entry, and on a trending book the expiries skew against the
call more often than not. Seven names moved from positive to negative once
their expiries were measured rather than assumed.

This is the clearest argument in the whole exercise for measuring rather than
assuming, and it was worth one migration and an afternoon to find out.

## Two process failures worth recording

**The first re-run silently doubled the sample.** Migration 044 had not been
applied to the live database, so the backfill generated and stamped 1,922
predictions and then died at resolution. The second run generated 1,922 more,
and `resolve_due_predictions` -- correctly -- swept both batches. The result was
3,622 resolved predictions, exactly twice the truth, and it did NOT show up as
duplicate `(entity, horizon_ends_at)` pairs because the script derives its
cutoffs from `datetime.now()` and the two runs differed by minutes.

The tell was arithmetic: 3,622 is exactly 2 x 1,811. Nothing in the pipeline
flagged it. A backfill run that partially completes leaves stamped, unresolved
predictions behind, and the next run does not notice them.

**The float-to-NUMERIC defect was upstream of every number quoted here.**
`record_prediction` passed raw floats into NUMERIC columns, so a barrier of 0.1
persisted as 0.1000000000000000055511151231257827. Every barrier distance, and
therefore every expectancy figure in the original version of this document,
carried digits nobody produced. Fixed at the write boundary with
`Decimal(str(f))`, which is the shortest repr that round-trips the float.

## Where that leaves the verdict

`trend.sma` on crypto: **+26.6 bps net of taker fees, 22 of 29 names positive,
concentration 14.2%, nothing assumed** -- on an effective sample of 65.

That is a real number rather than an artefact, and it is still only 65
independent observations across one market regime. It is enough to justify the
next measurement, not enough to justify capital.


---

# VERDICT — no established edge, at either horizon

The 7-day result was re-run at a 2-day horizon to raise the effective sample.
Both are now measured, with recorded exit prices and no assumed P&L.

```
horizon    n      effective_n    gross      net taker    positive
7 day    1,811         65      +46.6 bps    +26.6 bps     22/29
2 day    6,296        240       -2.0 bps    -22.0 bps     14/29
```

**The point estimate swings from +46.6 to -2.0 on a horizon change.** That is
the signature of noise, not of an edge that happens to prefer one holding
period. And the dispersion says why:

```
standard deviation per trade   510 bps
standard error at eff_n = 240   32.9 bps
95% CI on gross at 2 day        [-66.5, +62.6]
```

The interval straddles zero by a wide margin. Applying the same dispersion to
the 7-day sample -- 510 / sqrt(65) = 63 bps of standard error -- puts that
result's interval at roughly [-77, +170]. **It straddled zero too.** The +46.6
bps figure was never distinguishable from chance; it only looked like a result
because the interval was never computed on the effective sample.

`trend.sma` on crypto is **not established**. The gate agrees, unprompted:

```
paper   eligible=False   net=-22.0 bps   eff_n=240   BELOW_EXPECTANCY
micro   eligible=False   net=-22.0 bps   eff_n=240   BELOW_EXPECTANCY
```

## One more integration defect, found by disagreement

Running the gate and an independent measurement over the same ledger produced
**+10.5 bps and -22.0 bps**. Same rows, same `expectancy.compute`, opposite
sign.

`policy.py` was written while `ResolvedTrade` still scored every expiry as an
assumed zero. Migration 044 and the `exit_price` wiring landed in a different
work stream, and the gate's query kept selecting the pre-044 column set. It
scored expiries at zero, inflated pooled expectancy by roughly half, and refused
for `TOO_MUCH_ASSUMED` -- a plausible-looking refusal for the wrong reason.

Nothing failed. The only signal was two numbers that should have matched and
did not. `TestTheGateReadsTheRecordedExitPrice` now pins it, and removing the
column from the query fails two tests.

## What this exercise cost, and what it bought

Cost: roughly a day, one migration, and four measurement passes.

Bought: the knowledge that `trend.sma` on crypto has no demonstrated edge --
before Phases 4 through 8 built a CEX venue, a Turnkey signer, an on-chain
router and a live trading loop on top of it. That is ~17 tasks of execution
plumbing whose entire value was conditional on this number.

Three separate defects each moved the figure materially, and every one was found
after the previous version looked convincing:

    gate barred on hit rate         a profitable shape was refused outright
    expiries scored as zero         inflated pooled expectancy by 46%
    gate not reading exit_price     inflated it again, in the opposite direction
                                    from the refusal it produced

## What would change the verdict

Not more assets, and not more history at these horizons -- the interval is set
by a 510 bps per-trade dispersion against a mean near zero, so precision only
improves as sqrt(effective_n) and the effect size has to be real to find.

- **Parameter sweep.** One SMA window and one vol multiple have been tested. The
  producer has three constants and none has been varied.
- **Signal combination.** `signal_fusion.py`, `volatility.py`, `crossasset.py`
  and the convergence detector all exist and none writes a prediction. A single
  signal near a coin flip is exactly the input that stack is for.
- **The other six producers.** carry.funding, basis.crossvenue, oi.divergence,
  flow.exchange_reserve, onchain.smart_money and fundamentals.protocol have
  never been measured -- they need derivatives history, a second venue, and an
  Etherscan key respectively.

The infrastructure to answer any of those now exists and is honest. That is the
actual deliverable.


---

# SWEEP — the default configuration was the problem, and the gradient is the finding

Twelve configurations of `trend.sma` (SMA window x volatility multiple), swept
on the OLDER half of the cutoffs, with the winner then tested on the newer half
it had never seen. 16 assets, 2-day horizon, 100 in-sample and 100
out-of-sample distinct cutoffs.

```
              k=1.0      k=2.0      k=3.0        net bps, in-sample
  w= 10      -82.7     -116.1     -123.6
  w= 20      -38.8      -71.4      -82.7
  w= 50      +13.7      -29.3      -45.9        <- w=50 k=2.0 is the DEFAULT
  w=100      +26.9      -14.3      -34.8
```

## The gradient matters more than the winner

Every increase in window improves the result. Every increase in `target_k`
worsens it. Both dimensions are monotonic across all twelve cells, with no
inversions.

**Noise does not do that.** Twelve independent measurements scattering randomly
would not order themselves along two axes simultaneously. The gradient is
evidence of a real structural effect even though no single cell clears its own
error bar.

And it locates the earlier failure precisely: **the default configuration
(w=50, k=2.0) sits at -29.3 bps, in the middle of the losing region.** The
-22.0 bps measured before this sweep was not a verdict on trend following; it
was a verdict on one arbitrary parameter pair that had never been examined.

## The winner survived out-of-sample

```
  window=100, k=1.0
    in-sample      +26.9 bps net   (SE 45.8)
    out-of-sample  +22.8 bps net   (SE 38.6)   95% CI [-52.9, +98.4]
    positive 10/15 names, concentration 15.4%, nothing assumed
```

In-sample and out-of-sample agree to within 4 bps. A fitted parameter usually
collapses out of sample; this did not. That is the second piece of evidence
pointing the same way as the gradient.

**It is still inside the noise.** The interval straddles zero by a wide margin,
and one honest reading of this whole page is that a ~25 bps effect against a
~40 bps standard error simply cannot be settled with 100 independent
observations.

## What it would take to settle it

```
  to establish 25 bps at 95% confidence:  effective_n ~= 1,000
  at a 2-day horizon:                     ~5.4 years of distinct cutoffs
```

Five years of daily crypto history at a 2-day horizon, or a shorter horizon over
the same span, or a genuinely larger effect. More assets do NOT help --
`effective_n` counts distinct horizon dates, and correlated names resolving
together add rows without adding information.

## Revised verdict

Not "no edge". **A small effect that two independent lines of evidence support --
a monotonic parameter gradient and out-of-sample agreement -- and that the
available data cannot establish.**

The gate still refuses, and should: +22.8 bps with a lower bound of -52.9 is not
something to fund. But the correct next action is no longer "abandon trend
following"; it is "get five years of history, or combine this with signals that
are independent of it".

`signal_fusion.py` and the convergence detector are exactly the second option,
and neither has ever written a prediction.


---

# CONVERGENCE — built, correct, and inert on the coverage we can backfill

The sweep's conclusion was "combine signals independent of trend". The
corroboration producer now exists (`convergence.multistream`). Measured against
the live database it fires **0 times out of 320 attempts**, at
`min_families=3` and at `min_families=2`.

That is not a defect. It is a data fact, and the arithmetic is short:

```
claim types present for crypto_asset      family        can vote on direction?
  price_snapshot   30,441                 price         yes
  funding_rate        600                 derivatives   NO -- deliberately silent
  open_interest       180                 derivatives   NO -- deliberately silent
```

**One voting family exists.** `min_families=2` can never be met.

## Why `derivatives` is silent, and why that is right

The obvious move is to read persistently positive funding as crowded longs and
therefore a `down` call. `carry.py` -- the only producer here that reads funding
directionally -- says outright that *"the directional bet is NOT that price
falls"*: its `down` is a carry thesis, an assertion about collecting a payment
stream, not a price forecast. Borrowing it as one would be a reading nothing in
this repository supports.

`narrative` is silent for the same class of reason: `news_event` carries a
title, a url and a feed name, and `perception_news` carries headline *counts*.
Reading article volume as positive sentiment fabricates the sign.

A silent family cannot agree, so it cannot be counted as agreement. That rule is
what stops a "3-family convergence" shipping as a call from one signal with two
mute companions.

## What would actually unblock it

Two families can vote and neither is currently backfillable:

- **`flow`** needs `onchain_flow` with labelled addresses, which needs an
  Etherscan key. It IS historically backfillable once available -- block history
  goes back indefinitely -- so this is the path that makes convergence
  calibratable rather than merely live.
- **`microstructure`** needs `orderbook_snapshot` / `trade_tape`. Binance serves
  these keylessly, but a book is a SNAPSHOT: there is no historical order book to
  fetch, so this family can only ever accumulate forward. It would let
  convergence trade eventually and never let it be backfilled.

So the corroboration path is blocked on **one credential**, not on code. With an
Etherscan key: price + flow gives two voting families across the whole price
history, and the class can be backfilled and calibrated exactly as `trend.sma`
was.

## The honest summary of the whole exercise

`trend.sma` alone: a small effect (~25 bps at window=100, k=1.0) that two
independent lines of evidence support and 100 independent observations cannot
establish. Corroboration was the recommended next lever, and it is built,
tested, mutation-verified -- and cannot run until the on-chain family has data.

That is a better place to be than it sounds. The remaining blocker is a single
API key rather than a design question, and every guard that would have let a
weak signal through while looking convincing has now been found and closed.

---

# Finding 5 — the sample was never small; it was never requested

Dated 2026-08-08, and it revises the conclusion of everything above.

Every measurement in this document rests on `effective_n`, and every one of them
was starved by a missing function argument.

## What was measured

```
DB oldest crypto price_snapshot                       2025-03-27
ccxt fetch_ohlcv('BTC/USDT','1d')                     500 candles, oldest 2025-03-27
ccxt fetch_ohlcv('BTC/USDT','1d', since=2019-01-01,
                 limit=1000)                          1000 candles, oldest 2019-01-01
```

`CCXTAdapter._default_fetch` calls `fetch_ohlcv(symbol, self._timeframe)` with
no `since` and no `limit`. That returns the exchange's default page, which for
Binance is the most recent 500 candles. The database's oldest row matches the
default page's oldest candle **to the day**, on every symbol seeded from ccxt.

The history was not missing. It was never asked for.

## What this changes

The stated blocker was that settling a ~25 bps effect against a ~40 bps standard
error needs `effective_n` near 1,000, that a 2-day non-overlapping horizon yields
about 180 distinct cutoffs a year, and that 5.4 years of history therefore stood
between this project and an answer.

Binance serves daily candles from 2017. Paging from 2019 gives roughly 2,780
days, or about **1,390 non-overlapping 2-day cutoffs** — past the threshold, at
a cost of three requests per symbol.

The measurements above are not wrong. `-22.0 bps` at the default parameters and
`+22.8 bps` at window=100 were both computed correctly over the sample that
existed. What was wrong is the inference drawn from them: that the ambiguity was
a property of the market rather than of the fetch.

## Why it survived this long

Nothing failed. The adapter returned 500 valid candles, `parse_ohlcv` mapped
them correctly, the bitemporal pair was right, the claims wrote, the producers
ran, the backtest measured, and the gate refused on a number that was honestly
computed. Every component did its job on the data it was handed, and no
component's job was to ask whether that was all the data.

This is the fifth defect in this project whose signature is a plausible number
rather than an error, and the first where the number was too *pessimistic*. The
four before it each made a losing configuration look profitable; this one made
an answerable question look unanswerable. Both directions cost the same.

The general lesson is narrower than "check your data" and worth stating exactly:
**a default that silently bounds a result is more dangerous than an error,
because a bounded result is still a result.** `fetch_ohlcv` has no failure mode
for "you did not ask for enough" — it returns a page, and a page looks like an
answer.

## The correction to Finding 4

Finding 4 concluded that convergence was blocked on one credential and that the
corroboration path was the recommended next lever. That still holds for the
`flow` family. But it also implied `trend.sma` alone had been pushed as far as
the data allowed, and that was the part that was false. Deepening price history
is both cheaper than the on-chain backfill (roughly 90 requests against ~48,000
per address-year) and directly addresses the binding constraint.

Price depth first. On-chain flow second.

## What must not go wrong

`parse_ohlcv` sets `knowledge_date=event_date`, anchored to the candle's own
timestamp, which is correct: a bar becomes knowable when it closes. If deep
history were ever stamped with the fetch time instead, every backfilled bar
would read as known today, a replay at a 2021 cutoff would see 2026 prices, and
`trend.sma` would post a spectacular and entirely fabricated edge.

That failure would not raise. The suite would pass and the numbers would improve.
It is the single highest-consequence mutation in this codebase and it is now
tested for explicitly.

## Finding 5, confirmed arithmetically

Grouping the crypto price spine by source removes any doubt about the cause:

```
source      rows     entities   oldest        newest
ccxt        15,000   30         2023-04-30    2026-08-08
coingecko   15,441    9         2025-08-09    2026-08-08
```

**15,000 / 30 = exactly 500.00 rows per entity.** Not approximately, not on
average — every one of the thirty crypto assets holds precisely one default
page. A rate limit, a partial failure, or a genuine listing boundary would
produce a ragged distribution. A number that lands exactly on the page size for
every symbol without exception is the page size.

CoinGecko is the deeper source at 365 days but covers only 9 of the 30 assets,
so it cannot substitute. After deep paging, ccxt becomes both the deepest and
the widest source and the question of which to trust stops being interesting.

## The same class, elsewhere in the ingest tier

Audited every adapter for a bound that silently caps a result:

| adapter | bound | verdict |
|---|---|---|
| `exchanges.py` (ccxt) | none passed; exchange default 500 | **the defect** |
| `coingecko.py` | `days: Any = 30` default | shallow default, overridden to 365 in practice |
| `polygon.py` | `now - 730 days` | deliberate, matches the free tier |
| `fred.py` | `_VINTAGE_LOOKBACK_DAYS = 1825` | deliberate, 5 years |
| `derivatives.py`, `microstructure.py`, `defillama.py`, `edgar.py` | none | not window-bounded |

Only ccxt had an *implicit* bound — one imposed by the remote service because
nothing local specified otherwise. The others are explicit numbers someone
chose, which is the difference that matters: a chosen bound can be reviewed,
argued with, and found wrong. An inherited one is invisible until somebody
divides the row count by the entity count.

`coingecko.py`'s 30-day default is worth changing anyway. It is not currently
doing harm because the live configuration passes 365, but a default that shallow
is one omitted argument away from being the same finding again.

---

# Finding 6 — the bar timestamp is the OPEN, so every price carried a day of lookahead

Dated 2026-08-08, found while building the deep-history pager. This one runs the
opposite way from Finding 5: that made results look worse than they were, this
made them look better.

## What was measured

Three consecutive daily bars from Binance, taken live at 19:31 UTC:

```
2026-08-06T00:00:00Z  open=64665.24  close=64323.61
2026-08-07T00:00:00Z  open=64323.61  close=64923.19
2026-08-08T00:00:00Z  open=64923.20  close=65017.39

now         2026-08-08T19:31:28Z
ticker last 65017.39          <-- equals the newest bar's "close"
```

Two things follow, and each is conclusive on its own. Each bar's `open` equals
the previous bar's `close`, so the stamp names the start of the period. And the
newest bar's `close` is the live ticker, so that bar is the candle still
forming — its close does not exist yet.

A bar stamped `T` therefore covers `[T, T + 1d)`, and its close is knowable only
at `T + 1d`.

`parse_ohlcv` set `knowledge_date = event_date`.

## What that did

Producers read coverage filtered on `knowledge_date <= as_of`, and `trend.sma`
takes `entry = closes[-1]`. So a replay at cutoff `T` saw the bar stamped `T`,
whose close is the price at `T + 1d`, and entered there.

**One full day of lookahead on a two-day horizon.** Not a rounding error — half
the holding period, and it applied to the signal as well as the entry, because
the SMA was computed over a window whose most recent point was also from the
future.

Every crypto number in this document was measured over that data.

## Why it survived

The same reason as Finding 5: nothing failed. `knowledge_date <= as_of` was
enforced correctly, the bitemporal constraint `knowledge_date >= event_date`
held, the claims wrote, the producers ran. The system's entire point-in-time
apparatus worked exactly as designed on a value that was wrong before it got
there. A guard that checks `knowledge_date >= event_date` cannot notice that
`knowledge_date` is a day too early, because a day too early still satisfies it.

Worse, the codebase asserted the defect in three places and explained it well:
`test_event_date_equals_knowledge_date` carried the docstring "a crypto bar is
knowable the moment it closes: crypto trades continuously with no settlement
lag." That reasoning is correct. Crypto genuinely has no settlement lag. The
error was never about lag — it was about which end of the candle the timestamp
names, and the plausible explanation made the wrong line look considered.

**And the runbook written this same session to catch exactly this class of
problem required `knowledge_date = event_date` and would have certified it.**

## The fix

`knowledge_date = event_date + bar_duration`, with the duration resolved from
the timeframe when the adapter is constructed. An unrecognised timeframe raises
rather than defaulting to a day, because a wrong duration is silent everywhere
downstream.

`event_date` is unchanged — the bar still labels the period it describes — which
means the existing rows can be corrected in place rather than re-ingested. That
matters: the idempotency index includes `knowledge_date`, so re-ingesting would
write a second row per bar instead of fixing the first, and `_price_window` does
not filter on source, so a doubled series would silently halve the SMA window.
The `UPDATE` is in `REMEASURE_RUNBOOK.md` step 3a.

Three mutations proved: regressing to `knowledge_date = event_date` fails 4
tests, hard-coding a day instead of reading the duration fails 2, and defaulting
an unknown timeframe instead of raising fails 1.

CoinGecko is NOT affected and must not be "fixed" to match. `market_chart`
returns a price AT a timestamp — a tick, not a bar — so a tick at `T` genuinely
is knowable at `T`. Its `knowledge_date = event_date` is correct.

## What this means for every number above

Findings 1 through 4 measured `trend.sma` on data with a day of lookahead. The
defects they identified are still real and the fixes still correct — the gate
scoring hit rate instead of expectancy, expiries assumed at zero, the gate not
reading `exit_price` — but **every basis-point figure in this document is now
suspect and none should be quoted.**

The direction is predictable even if the size is not: lookahead inflates. So
`-22.0 bps` at the default parameters is likely worse than measured, and the
`+22.8 bps` out-of-sample winner has the most to lose, since a trend signal that
can see one day ahead on a two-day horizon is being handed a third of the answer.

That is not a reason for despair about the method. It is a reason no verdict
about it has yet been earned. Combined with Finding 5, the position is: the
question has never actually been asked on correct data at a sample size that
could answer it. P1.10 asks it for the first time.

---

# Finding 7 — the answer: no net edge, and now it is measured rather than assumed

Dated 2026-08-08. This is the first measurement in this document taken on
correct data at a sample size capable of settling the question.

## What was fixed first

1. Migration 045 applied to the live DB (it was lagging at 044 — the same
   desync that silently doubled the last backfill).
2. All 15,000 pre-existing ccxt price rows repaired in place for Finding 6
   (`knowledge_date = event_date + 1 day`). Corrected rather than re-ingested:
   the idempotency index includes `knowledge_date`, so re-ingesting writes a
   second row per bar, and `_price_window` does not filter on source, so a
   doubled series silently halves the SMA window.
3. Deep history paged from 2019: **69,567 rows, 30 assets, 2019-01-01 to
   today**, up from 15,000 at exactly 500 per asset. Zero bitemporal
   violations, zero duplicate `(entity, key, event_date)`.
4. All 27,451 crypto predictions deleted. Every one was generated from
   lookahead-contaminated prices, and predictions are derived data —
   regenerable from the spine. The 97 company predictions were left: they come
   from EDGAR and Polygon, and Polygon's adapter already handled bar-open
   timestamps correctly.

## The measurement

`trend.sma`, 2-day horizon, non-overlapping cutoffs from 2019-06-01 to
2026-07-14, all 30 crypto assets. 33,634 predictions generated, 33,147 resolved,
5,396 abstentions (assets not yet listed at the cutoff — correct behaviour).

```
n                33,147
effective_n       1,301      (distinct horizons — past the ~1,000 needed)
assumed_share       0.000    (every expiry carries a measured exit price)
concentration       0.102    (1/30 = 0.033 is perfectly even)
positive entities  26 / 30
```

Error taken across **horizon means**, not trades — thirty correlated assets
resolving on one day are one observation, not thirty:

```
gross           +23.64 bps    sd 441.64    se 12.24    t +1.93
95% CI          [-0.36, +47.64]
horizons with a positive mean   701/1301 = 0.539
```

Cost sensitivity, since costs are the entire question:

```
round trip   net       t        95% CI low
   20 bps   + 3.64   +0.30      -20.36     not established
   11 bps   +12.64   +1.03      -11.36     not established
    2 bps   +21.64   +1.77       -2.36     not established
    0 bps   +23.64   +1.93       -0.36     not established
```

## The verdict

**No net edge, and not even a gross one at conventional significance.** At zero
cost — a bound no venue offers — the lower bound is still below zero. There is
no cost structure that rescues this, because there is nothing to rescue: the
gross effect itself does not clear the bar.

The direction is consistent and weakly positive: 26 of 30 assets positive, 53.9%
of horizons positive, t = +1.93 (p ≈ 0.054). Something is probably there. It is
smaller than the noise at 1,301 independent observations, and smaller than the
cost of trading it.

## No parameter sweep at depth, deliberately

The shallow sweep's winner (window=100, k=1.0) is not being re-run and promoted.
With se = 12.24 bps, the best of twelve cells would need roughly +44 bps gross to
clear a 20 bps round trip with a lower bound above zero — nearly double the
observed effect, and selecting the maximum of twelve correlated cells is exactly
how noise gets promoted to a strategy. The prior sweep's whole spread was
narrower than one standard error here.

If a sweep is ever run at depth, its winner needs its own out-of-sample period
before any number from it is quoted. Picking a cell and reporting its in-sample
mean is not a measurement.

## What this is worth

This retires a line of work rather than leaving it open, which is the more
useful outcome of the two. Four sessions of defects each made a losing
configuration look profitable; two more made the question look unanswerable.
The question is now answered on 7.5 years of correctly-stamped data, and the
answer is no.

**GATE A refuses `trend.sma`, and for the first time the refusal is evidence
rather than the absence of it.** Phases 4, 5 and 7 stay blocked — not because
nothing has been measured, but because what was measured says don't.

What would change the answer: a different signal family (the `flow` producers,
once on-chain history is backfilled), a longer horizon where a fixed round-trip
cost is amortised over a larger move, or corroboration across families — which
`convergence.multistream` exists to test and still cannot, because only `price`
can currently vote.

## Finding 7, cross-checked against the gate itself

The measurement above was computed by a script. The gate is what actually
decides. Earlier today those two disagreed — the gate reported +10.5 bps while
an independent measurement of the same rows reported -22.0, opposite signs, with
nothing failing — so agreement is now checked rather than assumed.

`policy.eligible` on the same data, at PAPER and at MICRO:

```
eligible = False
detail   trend.sma/crypto_asset realised 24.0 bps per trade gross,
         4.0 bps net of 20 bps of round-trip cost, below the 5 bps minimum
net_expectancy_bps   4.02310917725974777987745655
effective_n          1301
```

Against the independent run: gross +24.02, net +4.02, `effective_n` 1,301.
**Identical to the digit**, and the refusal names the right quantity — below the
minimum expectancy, not below a hit rate.

That is the whole apparatus agreeing with itself for the first time: producer,
ledger, resolver, expectancy, and gate, on 7.5 years of correctly-stamped data.
The answer it agrees on is no.

## Finding 8 — the equities side was never actually backfilled

Measured while confirming Finding 7 generalises. `trend.sma` on companies:

```
n 96    effective_n 6    concentration 0.155    positive entities 11/16
gross +12.83 bps    net -7.17 bps
```

**`effective_n` is six.** Ninety-six predictions spanning six distinct horizon
dates. Nothing can be concluded from that in either direction, and the pooled
+12.83 gross is noise around a handful of days — the per-entity spread runs from
JNJ at +55.75 to META at -158.13 on six observations each, which is what pure
dispersion looks like.

Two things this is not. It is not evidence that trend following fails on
equities, and it is not evidence that it works. It is the absence of a
measurement, and it has been sitting behind a number that looked like one.

`fundamentals.dcf_valuation` — the producer this entity kind exists for — has
**zero** resolved predictions. The 97 company rows are all `trend.sma`.

The data to fix this is already here: Polygon holds 8,437 price rows over 730
days, which at a non-overlapping 2-day horizon is ~365 distinct cutoffs, sixty
times what was run. EDGAR fundamentals go back to 2006. The equities side has
never had the treatment the crypto side just received, and until it does, any
statement about Phase 7 rests on six days.

Tracked as P7.0, ahead of P7.1-7.3. It is cheap — no new ingestion, the prices
are already stored — and it is the only way the equities phases stop being a
guess.

## Finding 8, resolved — equities backfilled, and the answer is cleaner than crypto's

P7.0 run the same day it was found. No new ingestion: the Polygon bars were
already stored, only the calibration had never been run over them. 325
non-overlapping cutoffs from 2024-10-15 to 2026-07-25, 17 companies. The 149
pre-existing company predictions were deleted first, so this is one anchored run
rather than two disjoint ones pooled.

```
generated 5,461   resolved 4,508   abstained 64   unresolvable 953

n 4,508   effective_n 268   assumed_share 0.000   concentration 0.170
positive entities 9 / 17

gross  +0.14 bps    se 5.13    t +0.03    95% CI [-9.91, +10.19]

round trip   net       t        95% CI low
   20 bps   -19.86   -3.87      -29.91     not established
    2 bps   - 1.86   -0.36      -11.91     not established
    0 bps   + 0.14   +0.03       -9.91     not established
```

`effective_n` went from **6 to 268**, and the gross effect is `+0.14 bps` — not
small, not marginal, *absent*. `t = +0.03`. Nine of seventeen names positive is
a coin flip. Where crypto showed something weakly positive that costs consumed,
equities show nothing to consume.

The net figure at 20 bps is significantly negative (`t = -3.87`), but that is
not a finding about the market — it is the cost of trading a signal that carries
no information, measured precisely.

## What Findings 7 and 8 say together

```
                 effective_n    gross         t
crypto              1,301      +23.64 bps    +1.93
equities              268      + 0.14 bps    +0.03
```

`trend.sma` does not earn capital on either asset class. The crypto reading is
the more interesting of the two — a real-looking direction that never clears
significance and dies to costs — but the equities reading is the one that
settles the method: given a genuine effect, 268 independent observations would
show more than `+0.14`.

This is what the whole apparatus was built to produce, and it took six defects,
two data repairs and a 4.6x deepening of the price spine to get one honest
number out of it. **The number says no.**

Every phase downstream of GATE A stays unbuilt, and P7.1-7.3 are now blocked on
the same evidence as P4 and P5 rather than merely unstarted.

## A note on the two gross figures, because they differ slightly

Findings 7 and 8 each quote a gross expectancy twice, and the numbers are not
identical:

```
              per trade    per horizon
crypto         +24.02        +23.64
equities       + 0.52        + 0.14
```

Both are correct and they measure slightly different things. `expectancy.compute`
pools **per trade**, equal-weighted across all resolved predictions — that is
the number the gate reads, and it is the right one for "what did the average
trade earn." The standard-error calculation pools **per horizon date** first,
because that is the independent unit, and horizons carry different trade counts,
so the two weightings diverge wherever a busy horizon differs from a quiet one.

The per-horizon figure is the one quoted beside `se` and `t`, and it must be:
computing a mean one way and its error the other is how a confidence interval
comes to describe a quantity nobody reported. The gap is small in both cases
(0.4 bps and 0.4 bps) and changes no conclusion, but it is stated rather than
smoothed over, because "close enough" about which quantity a number refers to is
how the last six defects started.

---

# Finding 9 — funding carry, and the claim that kept it hidden

Dated 2026-08-08. The first thing measured in this project that pays.

## The claim that was wrong

`STATUS.md` recorded, as established fact, that `funding_rate` and
`open_interest` "only accumulate forward and are not backfillable from a public
endpoint", so `carry.funding` and `oi.divergence` "must simply run for a year".

`ccxt.binance.has['fetchFundingRateHistory']` is `True`. It pages to contract
inception. Seven years for three assets took minutes.

That single wrong sentence is why the strongest signal in this system sat
unmeasured while a t = +1.93 signal got a 4.6x data deepening and two full
re-measurements.

## What funding actually is

```
BTC 2019-09 -> 2026-08   n=7,574   +11.64% ann   t = +44.5   positive 85.6%
ETH 2019-11 -> 2026-08   n=7,340   +13.95% ann   t = +40.0   positive 86.2%
SOL 2020-09 -> 2026-08   n=6,541   + 0.13% ann   t =  +0.1   positive 71.4%
```

BTC by year: +7.5, +17.2, +30.6, +4.2, +7.9, +11.9, +5.1, +2.1 (2026 YTD).
Significant in every single year, and decaying hard.

SOL matters as much as BTC here: the phenomenon is not universal, so it is a
property of specific markets rather than of perpetual futures in general. Any
strategy has to select.

## Cross-sectional carry, no lookahead

Score on trailing 7 days of funding, earn the *next* period. 20 assets, 2,831
rebalances, 2024-01 to 2026-08:

```
all assets, equal weight   +4.87% ann   t +19.7
top-quartile               +8.74% ann   t +35.0
bottom-quartile            -2.15% ann   t  -6.1
top minus bottom          +10.89% ann   t +41.0
```

The bottom quartile being reliably *negative* is the part that makes this a
signal rather than a level: the ranking separates, in both directions.

## Turnover nearly kills it, and hysteresis saves it

Net of 30 bps round trip on turned-over notional:

```
every 8h   gross 8.74%   cost 29.19%   NET -20.46%
daily            8.59%         19.31%      -10.71%
weekly           8.17%          8.54%       -0.37%
every 3w         7.85%          3.11%       +4.74%
every 6w         7.32%          1.69%       +5.63%
```

A naive reading of the top-quartile number would have put a -20% strategy into
production. The gross figure is not the strategy; the gross figure minus what it
costs to keep chasing it is.

Adding a separate exit rank — enter on top-5, hold until the asset leaves
top-15 — removes the churn from assets oscillating across the boundary:

```
                       gross    cost      NET       t
6w  top5/top15   IS    7.79%   0.44%   +7.35%   +30.6   2024-01 -> 2026-08
6w  top5/top15   OOS   9.73%   0.63%   +9.10%   +26.5   2023-03 -> 2023-12
```

**Out of sample beats in sample, and the ordering of configurations is preserved
in every period at every cadence** — wider hysteresis always wins. That is what
a real effect looks like; a fitted one inverts somewhere.

## Why this does not fit the system

It is not a directional prediction, and every layer below a producer assumes one.
No `direction`, no barriers, no 2-day horizon; a *pair* held ~6 weeks, graded on
realised carry per unit time net of turnover. `carry.py` already abstains on
direction for exactly this reason and is right to. The producer is honest and
the machinery has no shape to receive what it says.

That is the build, and it is in `MONEY_PLAN.md`.

## The lesson, which is the same one as Findings 5 and 6

Three times now the binding constraint was a sentence nobody re-measured. "No
`.github/workflows/`" cost a work order. "The history does not exist" cost two
re-measurements of the wrong signal. "Funding is not backfillable" cost the
strategy that pays.

Every one was cheap to check and expensive to believe.

## Finding 9, cross-checked against the ingested claims

The carry result was computed from ccxt directly. Every downstream measurement
will read the claim store instead, so the two must agree — the same discipline
as the gate-versus-script check in Finding 7, which exists because those two once
disagreed by 32 bps with opposite signs and nothing failing.

After backfilling funding to inception (600 -> 192,490 claims across 30 assets),
the identical rules run against the database:

```
              universe   config              gross    cost      NET       t
scratch/API      20      6w top5/top15       7.79%   0.44%   +7.35%   +30.6
database         29      6w top7/top21       8.31%   0.51%   +7.80%   +36.0
```

Same shape throughout: wider hysteresis wins at every cadence, cost collapses as
cadence slows, gross barely moves. The database reading is slightly better
because the universe is larger — 29 assets to rank across rather than 20 — which
is the direction a cross-sectional signal should move when given more names.

The ingest landed correctly, and the strategy is now expressible against stored
claims rather than a live API call.

---

# Finding 10 — basis.crossvenue fires, and loses reliably

Dated 2026-08-08. The second producer measured properly, and the first one
measured to be *wrong* rather than merely absent.

The producer could never fire before today, because every asset had exactly one
price source. After the multi-venue backfill — 28 of 30 assets now carry three or
more independent venues — it fires on 21,627 of 28,080 attempts.

```
n 21,627   effective_n 936   assumed_share 0.000   concentration 0.073
positive entities 2 / 28

gross  -16.90 bps   sd 16.51   se 0.54   t = -31.31
95% CI [-17.95, -15.84]
net after 20 bps  -36.90 bps   t = -68.38
```

**Reliably negative, not noise.** A t of -31 is nearly the magnitude of the
carry signal's +36, pointing the other way. Two of twenty-eight assets positive
and a concentration of 0.073 means this is not one bad name dragging a pool — it
loses almost everywhere, almost always.

## What it actually found

The producer bets on convergence: when two venues disagree, it calls the gap to
close, targeting the other venue's price. Over a 2-day horizon that bet is
consistently wrong, which says cross-venue dislocations **persist or widen**
rather than converge at this horizon.

That is a real result about the market and it is worth having. It does not
license the inverse trade: the payoff geometry here is a near target (the other
venue's price, often very close) against a volatility-scaled stop, so a shape
this lopsided can lose on a near-coin-flip without the direction being
informative. Establishing that the reverse pays would need its own measurement
with its own barriers and its own out-of-sample period. **Inverting a losing
strategy is how a t = -31 becomes a t = +31 in a backtest and a loss in
production.**

## A defect in my own measurement script, found by this result

The verdict line printed "net indistinguishable from zero" for a t = -68 result.
The logic tested only `net - 1.96*se > 0` and fell through to the null case for
everything else, so it could not distinguish "no effect" from "a large negative
effect". Corrected to three outcomes. The numbers were right throughout; the
label was wrong, and a label that calls a reliably losing strategy
"indistinguishable from zero" is exactly the kind of thing that gets one built.

## Running tally

```
method              effective_n    gross         t        verdict
carry (x-sectional)     ~2,853    +8.31%/yr   +36.0     PAYS, net +7.80%/yr
trend.sma (crypto)       1,301    +23.64 bps   +1.93    indistinguishable
trend.sma (equities)       268    + 0.14 bps   +0.03    indistinguishable
basis.crossvenue           936    -16.90 bps  -31.31    RELIABLY NEGATIVE
```

Two of the four are now settled, and the settled ones point opposite ways.

---

# Finding 11 — M4: the shipped selector reproduces the carry result, and the gap is the design

Dated 2026-08-08. The +7.80% figure came from a scratch script that inlined the
selection rule. Every future decision runs through `conviction/crosssectional.py`
instead, so the two had to be compared — the same discipline that caught a gate
and a script disagreeing by 32 bps with opposite signs in Finding 7.

Driving the identical backtest through the shipped `select_basket`:

```
                            gross    cost      NET       t     avg basket
scratch, fixed basket 7     8.31%   0.51%   +7.80%   +36.0        7.0
module,  floating basket    7.73%   0.57%   +7.16%   +33.7       13.6
```

**The module reproduces the result, and the 0.64pp gap is fully explained.** It
is not a defect in either — it is a genuine difference in what the two select.

The scratch rule pinned the basket at K names: prefer the retained, fill the
rest with the top-ranked entrants. The module holds `top(enter_rank)` unioned
with every retained name still inside `top(exit_rank)`, so the basket floats
between `enter_rank` and `exit_rank` — here drifting to 13.6 names against 7.

Floating dilutes. Half the capital ends up in names ranked 8 through 21, which
by construction pay less than the top 7, so gross falls 8.31% -> 7.73%. That
costs more than the churn it saves (0.51% -> 0.57% is nearly a wash), which is
why the fixed-size rule wins on net.

Both are defensible and both are far from zero at t > 33. The fixed-size rule is
better on two counts that matter for a real book: it earns more, and it makes
per-name capital predictable, which the floating rule does not — a book that
cannot say how much it holds per name cannot size a position.

**Recorded rather than acted on.** Changing the module to cap the basket is a
one-line change to the selector and a rewrite of its hysteresis tests, and it
should be done as its own piece of work with its own out-of-sample check, not
folded into a measurement. What must not happen is quoting +7.80% for a system
that will actually earn +7.16%.

## Running tally

```
method               effective_n   gross         t        verdict
carry, fixed basket       ~2,853   +8.31%/yr   +36.0     PAYS, net +7.80%/yr
carry, as shipped         ~2,853   +7.73%/yr   +33.7     PAYS, net +7.16%/yr
trend.sma (crypto)         1,301   +23.64 bps   +1.93    indistinguishable
trend.sma (equities)         268   + 0.14 bps   +0.03    indistinguishable
basis.crossvenue             936   -16.90 bps  -31.31    RELIABLY NEGATIVE
```

---

# Finding 12 — the same signal pays as a harvest and loses as a prediction

Dated 2026-08-08. This is the architectural claim in `MONEY_PLAN.md` section 1,
measured rather than argued.

`carry.funding` is the registered producer that turns funding into a directional
triple-barrier call. It had 600 claims when last measured; it now has 192,490.
Re-run at depth, 936 non-overlapping cutoffs, 30 assets:

```
n 12,838   effective_n 931   assumed_share 0.000   concentration 0.083
positive entities 10 / 30

gross  -3.18 bps   sd 32.83   se 1.08   t = -2.95
95% CI [-5.29, -1.07]
net after 20 bps   -23.18 bps   t = -21.54
```

**Reliably negative.** Not noise, not thin data: `effective_n` 931, and the
gross confidence interval sits entirely below zero.

Against the cross-sectional carry basket, reading the **same funding claims**:

```
                                gross          t       verdict
carry.funding    (directional)  -3.18 bps    -2.95    LOSES
carry basket     (harvest)      +7.73 %/yr  +33.7     PAYS, net +7.16%/yr
```

## Why the same data goes both ways

`carry.funding` converts a funding observation into a claim about **price**:
funding is extreme, therefore price will move. That claim is wrong, slightly and
reliably, and then pays 20 bps of round trip to be wrong.

The carry basket asserts **nothing about price**. It holds the pair delta
neutral and collects the settlement. Funding is not a forecast of anything — it
is a payment for supplying leverage, and the way to earn a payment is to supply
the thing, not to predict the price of it.

`carry.py`'s own docstring already says funding is a carry thesis rather than a
price forecast, and abstains on direction wherever it can. It was right, and the
producer registry gave it nowhere to say so — the only shape available was a
directional call, so it made one, and the call loses.

## What this settles

The system's core assumption was that value comes from predicting price. Four
methods have now been measured properly and **every directional one fails**:

```
method                effective_n   gross          t        verdict
carry basket (harvest)     ~2,853   +7.73 %/yr   +33.7     PAYS
trend.sma (crypto)          1,301   +23.64 bps    +1.93    indistinguishable
trend.sma (equities)          268   + 0.14 bps    +0.03    indistinguishable
carry.funding                 931   - 3.18 bps    -2.95    RELIABLY NEGATIVE
basis.crossvenue              936   -16.90 bps   -31.31    RELIABLY NEGATIVE
```

One method pays. It is the only one that is not a prediction.

That is not an argument against prediction in general — the sample is five
methods on crypto at a two-day horizon, and it says nothing about longer horizons
or other asset classes. But it is decisive about where to spend next: **the
harvest shape is the one earning, and it currently has exactly one strategy in
it.** Basis, liquidity provision, and cross-venue funding spreads are all
harvests rather than forecasts, and none has been built.

## What must not be concluded

That `carry.funding` should be inverted. Same reasoning as Finding 10: its
payoff geometry is a volatility-scaled target against a model-grounded stop, and
a t of -2.95 on that shape is a small directional error amplified by costs, not
a reliable signal pointing backwards. Inverting a losing directional strategy is
how a backtest improves and a book does not.

## Finding 12, follow-up — the cross-venue funding spread is too small to harvest

Finding 12 says build harvests, not forecasts. The obvious next harvest is the
cross-venue funding spread: short the perp where funding is rich, long it where
funding is cheap, collect the difference. Finding 10's result even argues for it
— dislocations *persist* rather than converge, which is what a spread harvest
wants and a convergence bet does not.

Measured on BTC perps, 2026-05-10 to 2026-08-08:

```
pair              n     mean spread    se      t      sign persists
binance-bybit   143     +1.51 %/yr   0.34%   +4.4        0.563
binance-okx     143     +1.52 %/yr   0.32%   +4.7        0.521
bybit-okx       272     -0.42 %/yr   0.30%   -1.4        0.620
```

Real and statistically present, and **an order of magnitude too small**. The
trade needs a perp leg on each of two venues, so it pays two round trips rather
than one and posts margin twice, against 1.5%/yr of gross where the
single-venue basket earns 7.73%. Sign persistence of 0.52-0.56 means the spread
also flips often enough to force turnover, which is the cost this whole strategy
family is organised around avoiding.

Not built. Recorded so the next person does not build it either.

**Caveat, stated because the sample is thin:** one asset, three months, n=143 on
the two useful pairs. This is enough to rule the trade out on magnitude — the
gap to the basket is 5x, not 5% — and not enough to say anything about how the
spread behaves in a different regime. If perp funding is ever backfilled across
venues the way spot prices were in M2, this is cheap to re-check.

The remaining harvest candidates are untouched: spot-perp basis at calendar
expiries, and liquidity provision.

---

# Finding 13 — the capped basket, and an out-of-sample run through a bear market

Dated 2026-08-08. M3.3 closes the gap Finding 11 opened.

Capping the basket at `enter_rank` names -- retained keep their slots, the rest
filled from the top -- recovers the scratch measurement exactly:

```
same window (2024-01 -> 2026-08), same 29 assets, 6w cadence, exit top21
floating   gross 7.73%   cost 0.51%   NET +7.16%   t 33.7   avg size 13.6
capped     gross 8.31%   cost 0.51%   NET +7.80%   t 36.0   avg size  7.0
```

## The out-of-sample result

Funding is now backfilled to contract inception, so the strategy can be tested
on a period its parameters were never fitted to -- and that period contains the
2022 bear market:

```
                              cadence  exit    NET       t
in sample     2024-01 -> 2026-08   6w   top21  +7.80%  +36.0
out of sample 2021-05 -> 2023-12   6w   top19  +8.76%  +35.4
```

**Out of sample is better, and the configuration ordering is preserved** --
wider hysteresis wins at every cadence in both periods, and the cost/cadence
trade-off has the same shape. A fitted result inverts somewhere; this does not.

The level matters less than what it survived. Unconditional funding collapsed in
2022 -- BTC averaged +4.16% for the year and ETH +0.79%, against +30.61% and
+37.54% in 2021 (Finding 9). A strategy riding the market-wide funding level
would have earned roughly nothing that year. This one earned, because it is
selecting **who pays**, not collecting what everyone pays. That is the
difference between a carry trade and a beta.

## A confound in my own first run, caught and corrected

The first attempt at this comparison loosened the minimum-history filter
(`>2000` settlements to `>1500`) at the same time as capping the basket. That
admitted shorter-lived assets, which truncated the aligned window to 2025-09 and
produced 12.09% gross -- a number not comparable to anything, and flattering.
Re-run with one variable changed.

Two things changed at once is how a measurement stops measuring. The tell was
the date range in the output header, not the return.

## Running tally

```
method                effective_n   gross          t        verdict
carry basket (capped)      ~2,853   +8.31 %/yr   +36.0     PAYS, net +7.80%/yr
   same, out of sample     ~2,300   +9.12 %/yr   +35.4     PAYS, net +8.76%/yr
trend.sma (crypto)          1,301   +23.64 bps    +1.93    indistinguishable
trend.sma (equities)          268   + 0.14 bps    +0.03    indistinguishable
carry.funding                 931   - 3.18 bps    -2.95    RELIABLY NEGATIVE
basis.crossvenue              936   -16.90 bps   -31.31    RELIABLY NEGATIVE
```

---

# Finding 14 — the DCF producer covers 2 of 17 companies, and that is not a verdict

Dated 2026-08-08. `fundamentals.dcf_valuation` sits on the deepest data in the
system — 27,591 `fundamental_metric` claims back to 2006 — and had never been
calibrated once. Run at depth over 325 cutoffs and 17 companies:

```
generated 386   resolved 320   abstained 5,139   unresolvable 66

n 320   effective_n 268   concentration 0.782   positive entities 0 / 2
gross -3.90 bps   net after costs -23.90 bps
```

**Do not read that as a result.** `concentration` of 0.782 and a universe of
**two** names says what the numbers are worth: `expectancy.py` states outright
that a strategy working on two names is a position rather than an edge, and the
same applies to one losing on two names. The measurement is of AMZN (325
predictions) and GOOGL (61). Fifteen companies produced nothing at all.

## Why it abstains, which is the actual finding

Not thin data. MSFT holds **2,191** fundamental claims and 496 prices — more
fundamentals than AMZN — and produced zero predictions. Both companies store
both concepts the model needs:

```
AMZN   NetCashProvidedByUsedInOperatingActivities 155   PaymentsToAcquirePP&E  72
MSFT   NetCashProvidedByUsedInOperatingActivities 118   PaymentsToAcquirePP&E 162
```

The gate is in `coverage/fundamentals.py`: free cash flow is built from
`_latest_annual_flow`, and when no ANNUAL figure is found the field is popped and
the essentials check refuses. That reasoning is correct and should not be
changed — a quarterly operating cash flow substituted for an annual one
understates FCF roughly fourfold and would produce a confident, wrong valuation
rather than an abstention.

So the defect is upstream of the model: the annual figure is not being detected
for 15 of 17 companies whose concepts are present. Whether that is EDGAR frame
selection, a fiscal-year convention, or the annual-vs-quarterly discriminator
itself is the thing to establish, and it is a coverage question rather than a
strategy question.

## What this means for the tally

`fundamentals.dcf_valuation` is **UNMEASURED**, not negative. It joins nothing
else in that state — every other producer has either been measured or is
honestly starved of data. This one has the data and cannot reach it.

```
method                effective_n   gross          t        verdict
carry basket (capped)      ~2,853   +8.31 %/yr   +36.0     PAYS, net +7.80%/yr
   same, out of sample     ~2,300   +9.12 %/yr   +35.4     PAYS, net +8.76%/yr
trend.sma (crypto)          1,301   +23.64 bps    +1.93    indistinguishable
trend.sma (equities)          268   + 0.14 bps    +0.03    indistinguishable
carry.funding                 931   - 3.18 bps    -2.95    RELIABLY NEGATIVE
basis.crossvenue              936   -16.90 bps   -31.31    RELIABLY NEGATIVE
fundamentals.dcf                --            --      --   UNMEASURED (2/17 names)
```

Worth fixing because it is the only producer left whose data exists and is deep
— 20 years — and because a DCF is a valuation rather than a price forecast,
which puts it closer to the harvest family that is actually paying than to the
directional family that is not.

---

# Finding 15 — the claim model cannot represent "the same asset, a different instrument"

Dated 2026-08-08, found while closing gap 3 of M7.0 ("the carry loop cannot see
basis"). The gap is real; the fix is not an ingestion.

## What was measured

Perp OHLCV is available from the same adapter and pages identically to spot:

```
BTC/USDT:USDT   1,000 daily bars, 2019-09-08 onward, one request
basis (perp - spot), 750 aligned days:  mean +1.76 bps   sd 9.70   max |180| bps
```

A mean of under 2 bps is nothing; a maximum of **180 bps is material** against a
strategy earning 780 bps a year, and it lands exactly when volatility does.
Worth having. But it cannot simply be written.

## Why ingesting it would break something already measured

`basis.crossvenue` identifies a venue as `COALESCE(value->>'venue', c.source)`.
Perp bars are `price_snapshot` claims like any other, so there are two options
and both are wrong:

- **`venue = "binance"`** — perp bars merge with spot bars for the same entity
  and day. Two different prices claiming to be the same observation; the spot
  series is corrupted and `trend.sma`, `basis.crossvenue` and the carry loop all
  read a series that alternates between two instruments.
- **`venue = "binance-perp"`** — `basis.crossvenue` sees a new venue and begins
  computing **spot-versus-perp basis**, which is a cash-and-carry trade, mixed
  into its cross-venue dislocation signal. Finding 10's measurement of that
  producer would silently stop describing what it measured.

Neither is an ingestion decision. The claim model has `source` (who produced the
row) and an entity, and no way to say **"the same asset, a different
instrument"**. Spot BTC and perp BTC are one entity and two instruments, and
nothing in the schema carries that.

## What this actually needs

An instrument dimension, and a decision about where it lives — a field on the
claim, a distinct `claim_type`, or separate entities linked by a relation. Each
has consequences for the producers that already read `price_snapshot`, which is
why it is a schema decision rather than a backfill.

**Recorded, not built.** The carry loop's inability to see basis is a monitoring
gap rather than a correctness bug: it prices both legs off one series, which is
what guarantees the two legs are equal-sized and delta-neutral. What it cannot
do is *notice* when the basis moves against it, and MONEY_PLAN section 5 lists
basis divergence as a live risk.

For M7 at micro size the exposure is bounded and this is acceptable with the
risk stated. It is not acceptable at size, and it should be settled before the
book is large enough for 180 bps to matter in absolute terms.

## The general shape, which has now appeared twice

Finding 12 found that the producer registry had no shape for a carry harvest, so
`carry.py` made a directional call it did not believe. This finding is the same
class one layer down: the claim model has no shape for an instrument, so perp
prices would have to pretend to be spot prices or pretend to be another venue.

Both times the missing abstraction did not announce itself as missing. It
appeared as a component doing something slightly wrong for a reason that looked
like a local choice.

---

# Finding 16 — the backtest and the live book are two different code paths

Dated 2026-08-08. Not a defect. A gap in what has been checked, noticed while
confirming the perpetual accounting fix had not moved the measured return.

It had not: +7.80% at t = +36.0, unchanged to the basis point. That is the
**correct** outcome, and the reason is the point:

```
the backtest   sums funding rates over a selected basket
the live book  executes pair trades -> apply_fill -> cash
               accrues settlements  -> apply_funding -> cash
```

The backtest never touches portfolio accounting, so a change to portfolio
accounting cannot move it. Which means the backtest **cannot detect an
accounting defect**, and the accounting is what a live book actually runs on.

## What this changes

`MONEY_PLAN` states M6's gate as "paper P&L tracks predicted carry within its
own error bars". The loop is built, unit-tested and mutation-verified — and that
gate has never been run. M6 was marked complete on the artifact existing rather
than on the gate being met, which is the same error as quoting a number for a
configuration that was never measured.

Tracked as M6.1, and it blocks M7.

## Why it is worth the run rather than an argument

Finding 7 is the precedent. The eligibility gate and an independent measurement
of the same rows reported +10.5 bps and -22.0 bps — opposite signs, same data,
nothing failing, no test red. The two agreed only after one was fixed, and the
disagreement was the only thing that revealed the defect.

The same structure exists here and has not been exercised. Two implementations
of "what does this strategy earn", one of which is the one that will hold money.
If they agree, the execution path is validated end to end for the first time. If
they disagree, the disagreement is the finding, and it is far cheaper to have it
now than after a position exists.

Everything the run needs is in place: the loop (M6), the corrected perpetual
accounting (M7.0 gap 2), funding accrual (M3.1), the capped selector (M3.3), and
192,490 funding claims to inception.

---

# Finding 17 — the same defect on both sides of the same question

Dated 2026-08-08. Found because an agent stopped at a forbidden file instead of
routing around it, and reported what it hit.

Commit `70dd024` fixed the perpetual accounting in `portfolio.state`: opening a
perp moves cash by the fee only, not by the notional. `PaperVenue._debit_cash`
had the identical defect, untouched. It did not even receive `market_type`, so
it could not tell a perpetual from spot at all.

The two are one question -- "what did this fill do to cash" -- asked of two
components, and `portfolio.reconcile` compares their answers directly. So fixing
one side made them disagree by exactly the perpetual notional:

```
USD at paper: we carry 97997.59980000, the venue reports 99997.19960000,
a difference of 1999.59980000 against a tolerance of 0.01
```

1,999.60 is the two perpetual notionals. A reconcile-first loop halted on every
cycle after the first, and **the halt was correct** -- the books genuinely
disagreed.

## Why the agent's refusal was the valuable part

It had gap 4 implemented and failing, `paper_venue.py` on its forbidden list,
and three ways to make the suite green:

- widen the tolerance until 2,000 on a 100k book passes
- pass `local_balances=()` and skip the cash comparison
- subclass a corrected `PaperVenue` inside the test file

Every one produces a green suite over a loop that halts in the real M6 path.
The third is the worst, because it is indistinguishable from working code until
someone runs the thing for real. It reported the defect and stopped instead.

## What it says about the layering

`omni.venue` sits below `omni.portfolio` and may not import from it, so the
realised-P&L logic is now written twice. That is a real cost of the layering
and the right trade -- a venue that imported portfolio state would be a venue
that could not be swapped for a real exchange.

The duplication is made safe by asserting the **same worked figures as
literals** on both sides rather than deriving either from the other. A shared
helper would have prevented this defect and also prevented the tests from
catching a future one, because both sides would move together silently.

## The general shape, third occurrence

Finding 12: the producer registry had no shape for a carry harvest, so
`carry.py` emitted a directional call it did not believe.
Finding 15: the claim model had no shape for an instrument, so perp prices would
have had to impersonate spot or a venue.
This one: two components answer the same question and nothing structural keeps
them consistent, so one was fixed and the other silently was not.

Each time the missing invariant showed up as a component doing something
slightly wrong for a reason that looked local. **The reconciler is what caught
this one** -- it exists precisely to compare two answers that must agree, and
it did its job the first time a divergence was real rather than hypothetical.

---

# Finding 18 — the minimum viable book, and what a small one actually trades

Dated 2026-08-08, measured against Binance's live market metadata. The question
was whether $15-200 can run the carry basket.

## Exchange minimums are per symbol and differ by instrument

```
                 min cost (USDT)
spot  BTC/USDT          5
spot  ETH/USDT          5
perp  BTC/USDT:USDT    50
perp  ETH/USDT:USDT    20
perp  SOL/USDT:USDT     5
```

Most alts floor at $5 on both legs. **BTC and ETH do not**, and the perp leg is
the binding one.

## What that costs in capital

A pair is a spot leg plus a perp leg of equal quantity, so both legs must clear
their own floor. Taking the cheapest names:

```
basket   spot legs   perp notional   capital @3x   capital @5x
3 names       $15             $15        $20.00       $18.00
5 names       $25             $25        $33.33       $30.00
7 names       $35             $35        $46.67       $42.00
```

**$15 cannot run this.** The cheapest configuration that is still a
cross-section — three names — needs about $20, and three names is not a ranking.
**$200 runs a 7-name basket at roughly 4x the minimum**, which leaves room to
rebalance without every trade sitting on the floor.

## The part that is easy to miss

A book that can only place $5 orders **cannot hold BTC or ETH**, because their
perp floors are $50 and $20. So a $200 book does not trade the universe that was
backtested. It trades the subset of it that floors at $5.

That is not fatal -- Finding 9 measured selection working *across* the
cross-section, not because of any particular name, and the two largest assets
are the two least likely to be paying top-quartile funding anyway. But it is a
real difference between the measured strategy and the executed one, and it
should be stated rather than discovered from a rejected order.

It also means the strategy's behaviour changes with account size in a way
nothing else in this system does. At $50 it is a 7-name alt basket; at $5,000 it
is the basket that was actually measured. The gap closes as the book grows.

## A defect this surfaced

`Capabilities.min_notional` is a single scalar. It cannot express a per-symbol,
per-instrument floor, so any single value either refuses orders the venue would
accept or -- worse -- accepts orders the venue will reject. The rejection
arrives after the intent is recorded and, for a pair, after the other leg may
already have filled: the half-open position that `carry_loop` exists to prevent,
caused by our own pre-flight check being wrong.

The live adapter reads the real floor from
`exchange.markets[symbol]['limits']['cost']['min']` rather than trusting the
scalar. Whether `protocol.py` should carry a per-symbol minimum is a design
decision that has not been made.

---

# Finding 19 — five things the Venue protocol cannot say

Dated 2026-08-08. Found building `CCXTVenue`, the first adapter that talks to a
real exchange. `PaperVenue` satisfied the protocol comfortably because a paper
venue answers every question instantly and unambiguously. A real one does not,
and the gaps are all in that difference.

Reported rather than worked around. `protocol.py` was left untouched.

## 1. `min_notional` is a scalar; real minimums are per symbol and per instrument

```
spot BTC/USDT       5      perp BTC/USDT:USDT   50
spot ETH/USDT       5      perp ETH/USDT:USDT   20
                           perp SOL/USDT:USDT    5
```

Any single value is wrong in one of two directions, and the dangerous direction
is accepting an order the venue rejects: the rejection lands after the intent is
recorded and possibly after the other leg of a pair has filled.

Enforcement therefore reads the real floor per symbol, and the scalar is set to
the **largest** minimum in scope -- the only direction that cannot mislead a
caller. That has a cost: unscoped across all of Binance, the scalar would make a
$50-$200 carry book look ineligible to any router filtering on it. Callers must
pass a symbol scope. Properly, the protocol needs a per-symbol accessor.

## 2. `cancel(external_id)` has no symbol, and every CEX requires one

Worked around by remembering `external_id -> symbol` for orders this adapter
placed. **An adapter restarted mid-session cannot cancel its own resting
orders** -- the map is in memory. That is an operational risk rather than a
correctness one, and it is real: a process that dies holding resting orders
leaves them for a human to cancel by hand.

An unknown id raises rather than returning `False`, because `False` means
"nothing to cancel" and an unknown live order is precisely the opposite.

## 3. There is no way to say "accepted and resting"

`execute` must return a `Fill`, so a live unfilled order comes back as an empty
`Fill` carrying `external_id` and `raw['resting'] = True`. Raising would be
honest about the absence of a fill and would throw away the only handle on a
live order, which is worse.

## 4. `Fill.fee_paid` cannot be absent

When a venue confirms an execution but reports no fee, refusing the fill would
discard a real position. The charge is computed from the venue's published taker
rate and flagged `raw['fee_estimated']`. Never left at zero, which would
understate every cost measured against that trade. A maker rebate goes in
`raw['fee_rebate']` because `Fill` forbids a negative fee.

## 5. No lifecycle method

ccxt's async client holds a session that must be closed. `aclose()` exists
outside the protocol.

## The pattern, fourth occurrence

Finding 12: no producer shape for a harvest. Finding 15: no claim shape for an
instrument. Finding 17: no structural guarantee that two components answering
one question stay consistent. This one: a protocol written against the only
implementation that existed.

`PaperVenue` is not a bad model of a venue -- it is a model of a venue that
always answers. Every gap above is a place where a real exchange is *uncertain*:
minimums that vary, an order whose existence is unknown after a timeout, an
order that is accepted but not filled, a fee that is not reported yet.

**A protocol derived from a synchronous, always-answering implementation has no
vocabulary for uncertainty.** That is the general lesson, and it is why the
timeout path in `CCXTVenue` is the part of it worth reading.

---

# Finding 20 — the paper venue modelled a perpetual but not the payment for holding one

Dated 2026-08-08. Found by wiring reconciliation into the carry loop (M7.0 gap
4), which is one layer earlier than M6.1 would have found it.

Two cycles, and the second one halted:

```
the_venue_and_the_book_do_not_agree: USD at paper: we carry 97998.69980000,
the venue reports 97997.59980000, a difference of 1.10000000
```

**1.10 was exactly the first cycle's funding accrual.** The book credited the
settlement through `portfolio.state.apply_funding`; `PaperVenue` did not, because
it had no funding model at all. The divergence therefore compounds every cycle,
and a reconcile-first loop halts on the second rebalance and every one after it.

A venue that models a perpetual position but not the funding it pays is not
modelling a perpetual. Funding is not a detail of the position -- it is the
entire return on it, and this whole strategy exists to collect it.

## The fix, and why it is shaped this way

`PaperVenue.credit_funding(symbol, amount)` moves the venue's own cash, and the
carry loop calls it after applying the accrual to the book -- but only if the
venue exposes it:

```python
credit = getattr(venue, "credit_funding", None)
if credit is not None and accrual.outcome is FundingOutcome.ACCRUED:
    credit(symbol, accrual.amount)
```

Guarded by capability rather than by venue type, because **a live venue that
needed telling would be a venue whose exchange was not paying us.** A real
exchange settles funding on its own schedule and a caller learns of it by
reading `balances()`; there is nothing to tell it, and the absence of the method
is the correct signal.

`amount` is signed the way the book signs it, so a caller mirrors
`FundingAccrual.amount` without reinterpreting it. Reinterpreting the sign here
would make the venue and the book disagree about the direction of the only cash
flow this strategy earns -- and inverting it fails 4 tests.

## A surviving mutation in my own work, and what it means

Removing the divergence halt entirely left the whole suite green. Gap 4 wired
reconciliation in, and **nothing asserted that a divergence actually stops the
cycle** -- the code was right and the guard was untested, which is the same
state as not having it. Three tests now cover it, including the pair test that a
reconciler refusing everything would fail, and the mutation is dead.

That is the second time today a mutation has found an unasserted guard rather
than a wrong line. Both times the code was correct. A guard nothing tests is a
guard the next refactor deletes silently.

## What it says about the M6.1 gate

Finding 16 argued that the backtest cannot detect an accounting defect because
it never touches portfolio accounting. This is the same argument one level
further out: **the paper venue cannot detect a defect in what it does not
simulate.** It had no funding, so no amount of paper trading through it would
have shown a carry book whose venue never pays.

The reconciler caught it, again, because it is the one component whose entire
job is comparing two answers that must agree.

---

# Finding 21 — the loop trades tickers, and a venue does not list tickers

Dated 2026-08-08. The first real M6.1 run, and it earned the gate immediately.

Running the carry loop over 2024-2026 halted on the second rebalance:

```
cycle1 opened=7  venue_fills=14  venue_pos=14
        book_cash=92481.83   venue_cash=100000
venue balance keys: ['ALGO','APT','ARB','FIL','INJ','MKR','OP','USD']
```

Fourteen fills executed. The book paid 7,518 for them. **The venue's USD did not
move at all** -- instead it debited seven assets named `ALGO`, `APT`, `MKR` and
so on.

## The cause

`carry_loop` takes its symbol from `entity.symbol`, which is a bare ticker:
`MKR`. It passes that straight into `TradeIntent.symbol`. `PaperVenue`
determines what a fill settles in with
`symbol.partition("/")` -- so `BTC/USD` settles in `USD`, and `MKR` settles in
`MKR`.

**There is no mapping between the entity namespace and the venue namespace.**
An entity is an asset (`BTC`); a venue trades an instrument on a pair
(`BTC/USDT` spot, `BTC/USDT:USDT` perp). Nothing converts one to the other.

## Why this had to be found by running it

Every unit test passes. They construct symbols like `BTC/USD` directly, because
a test writing a symbol writes a *venue* symbol -- naturally, since it is
building a `TradeIntent`. Nothing in the suite ever takes a symbol from an
entity row and hands it to a venue, which is the only path that exists in
production.

`CCXTVenue` would not have failed quietly. Binance does not list `MKR`, so the
order is rejected and the pair half-opens on the leg that went first -- the
exact failure `carry_loop` was built to prevent, arriving through a channel it
cannot see.

## The deeper shape, and it is Finding 15 again

Spot and perp have **different venue symbols for one asset**: `BTC/USDT` and
`BTC/USDT:USDT`. So the mapping is not asset to symbol, it is
(asset, instrument) to symbol -- which is the instrument dimension M10 already
records the claim model as lacking, surfacing a second time one layer out.

A venue is the thing that knows its own symbol format, so the durable answer is
a venue-level resolver rather than string-building in the strategy. Tracked as
M11.

## What M6.1 is worth

Three accounting defects surfaced today at three layers -- portfolio, venue,
funding -- each invisible to the layer above. This is the fourth, and it is the
first that no amount of unit testing could have reached, because the defect is
in the *seam* between two components that each behave correctly alone.

The gate was worth holding. It has now paid for itself before a single real
order.

## Finding 21, resolved — and two names in the universe no longer exist

The resolver lives on the venue, because the venue is the only thing that knows
its own naming. Resolved from the exchange's **own market metadata**, matching
`base`, `quote` and the instrument flags rather than parsing the symbol string,
which is a rendering of those fields and not their definition.

**The first implementation was deterministic and stably wrong.** It took
whichever market sorted first, which against real Binance metadata returned
`BTC/ARS` for spot and `BTC/U:U` for the perpetual. Stable and useless is worse
than unstable, because it looks reliable. The quote asset is a genuine decision
-- liquidity, depeg risk, what the funding stream is denominated in -- so it is
stated on the venue and unresolvable without it:

```
BTC   spot=BTC/USDT   perp=BTC/USDT:USDT
SOL   spot=SOL/USDT   perp=SOL/USDT:USDT
unstated quote -> ValueError naming ARS, AUD, BRL, USDT and telling the caller to pick
```

`PaperVenue._quote_asset` now REFUSES a bare ticker rather than returning it,
which is how it came to hold balances in `MKR` and `APT` while its cash never
moved. A fill settling in an asset the account never agreed to trade should be
loud.

## Two of the thirty are delisted

Checking the resolver against the live universe:

```
tradeable as a pair (spot + perp)   28
only one leg                         0
delisted / not listed                2   MATIC, MKR
```

`MKR/USDT` and `MKR/USDT:USDT` both exist in Binance's metadata with
`active=False`. The resolver returns `None`, which is correct -- but the
**backtest traded both names**, because they have funding history across the
whole window and nothing in a historical replay knows a market has since closed.

That is a small overstatement in the measured return rather than a defect: the
strategy is cross-sectional and 28 names is still a cross-section. It is worth
stating because it is a general property of any backtest over a universe that
changes, and because a live book will silently hold 28 where the measurement
assumed 30. The direction is knowable but not the size; re-measuring on the 28
survivors would itself be survivorship bias in the other direction.

The honest summary: **the live universe is 28 of the 30 measured, and the two
missing names were tradeable for the whole period they were measured over.**

---

# Finding 22 — M6.1 passes, and the difference is the backtest being wrong

Dated 2026-08-08. The gate: "paper P&L tracks predicted carry within its own
error bars." Run over 2024-01 to 2026-07, 22 rebalances, the real loop against
the real paper venue with reconciliation, the order ledger and funding accrual
all live:

```
cycles 22   halted 0
cycle 1:  book cash 92481.83   venue cash 92481.83   agree
funding collected  +1589.58
NAV change         +1513.99  over 2.48y
                   +8.73%/yr on deployed notional

backtest, same window and config:  +7.80%/yr
backtest, 28 still-listed names:   +7.33%/yr
```

The book and the venue agree to the cent at every cycle, and nothing halted.

## The 0.93pp gap, chased down rather than waved at

First hypothesis: the loop cannot hold MATIC or MKR because they are delisted,
and the backtest held both. **Wrong, and wrong in the opposite direction** --
excluding them takes the backtest DOWN to +7.33%, so the delisted names were
contributors and the real gap is 1.40pp, not 0.93pp.

The actual cause is that **the two compute funding on different notionals**:

```
backtest   funding rate x CONSTANT notional      (the rate edge, cleanly)
loop       funding rate x MARK notional          (what an exchange actually pays)
```

`apply_funding` computes `-q * mark * rate`, and `q` was fixed when the pair
opened. So as the mark rises the same position pays funding on a larger
notional -- which is what a real venue does. Over this window the median asset
went **x3.52**, and the loop collected 18.5% more funding than the backtest,
which is the same order as the notional growth.

**The loop is right and the backtest is the approximation.** That is the good
outcome of this comparison and the opposite of what a disagreement usually
means.

## The property this exposes, which is worth knowing before sizing anything

The strategy is delta-neutral in P&L and **its income is not**. Carry is paid on
notional, notional tracks price, so the dollars earned scale with the market
level even though the position has no view on it. In a bear market the same
book earns fewer dollars of carry on the same seven names.

That does not make the return dependent on direction -- the rate is what it is,
and the percentage return on the deployed notional is roughly stable. It means
the DOLLAR income is levered to price, so a book sized off last year's carry
income will be sized wrong after a drawdown.

Neither number is "the" answer. `+7.80%` measures the rate edge with notional
held constant, which is the right way to ask whether the signal works.
`+8.73%` is what this book would have earned over this particular price path.
The first generalises; the second happened.

## What the gate was worth

Four defects surfaced at four layers before this run and one during it:
portfolio accounting, venue accounting, funding settlement, symbol namespace.
Every one was invisible to the layer above and would have arrived live as a
rejected order, a silently wrong balance, or a P&L that looked plausible.

M6.1 is now the first end-to-end evidence that the pieces agree, and the one
number that differed turned out to be the backtest simplifying rather than the
system erring.

# Finding 23 — "the markets are the same" is true of the asset and false of the payment

The question that prompted this: *aren't the markets the same, just a different
way to swap and list?* It is the right challenge, and the answer splits cleanly.

**Spot BTC is the same everywhere.** Prices are arbitraged tight because closing
a gap is one trade in one asset -- move the coin from the cheap venue to the
rich one. Finding 10 is the evidence: cross-venue *price* dislocations are small.

**Funding is not.** It is not a property of BTC. It is a parameter of each
venue's own perpetual contract, set by that venue's own long/short imbalance,
its own formula, and its own clock. Measured on the same asset over the same
window:

```
BTC, 2026-05-01 -> 2026-08-08
  binance       0.00003725 per 8h   x3/day    ->  +4.08 %/yr
  hyperliquid   0.00000691 per 1h   x24/day   ->  +6.05 %/yr
  difference                                      +1.97 pp/yr
```

Same coin, same days, **~48% relative difference in the only thing the strategy
earns**.

## Why the gap survives, and why that is self-consistent

Funding cannot be arbitraged the way spot can. Closing a funding gap requires
being long perp on one venue and short perp on the other -- two legs, two
venues, two round trips, margin posted twice. That is exactly the trade
Finding 12's follow-up measured at **+1.5%/yr gross**, and rejected as an order
of magnitude too small.

So the two results are the same result seen twice: **the funding spread persists
precisely because arbitraging it does not pay.** If it were cheap to close it
would already be closed, and the follow-up would have found nothing to reject.

## What this means for the +7.80%

The **strategy** is venue-agnostic and so is the code: positions are keyed
`(venue, symbol, market_type)`, cash is per venue, `funding_venue` is required
config, and `symbol_for` resolves per venue. Rank by trailing funding, hold
top-K delta-neutral, hysteresis on the exit rank -- none of that mentions a
venue.

The **number** is not. `+7.80%` is a measurement of Binance's contract, over
Binance's 30 names, at Binance's 8-hourly cadence and its fee schedule. Porting
the strategy is a config change; porting the number is a re-measurement.

## The trap in re-measuring, which is worth stating before the result

Hyperliquid settles 24x/day against Binance's 3x. Treating each settlement as an
observation hands Hyperliquid **eight times the sample for the same calendar
span**, inflating its t-statistic by roughly sqrt(8) for no reason but its
clock. Any comparison must normalise cadence -- aggregate both to daily -- or it
will report the venue being argued for as the more significant one by
construction.

Aggregation is a **sum** over the day, not a mean: a holder receives every
settlement, so the day's carry is their sum. Averaging would divide
Hyperliquid's return by 24 and Binance's by 3, which is not a normalisation but
a different and wrong quantity.

# Finding 24 — the venue effect is real, measured paired, and it favours the venue with no KYC

Finding 23 argued funding is a venue parameter. This measures how much, with the
confounds stripped out one at a time.

## The paired measurement, which is the one that settles it

Same asset, same day, same static hold. The only thing that differs is the
venue:

```
550 common days, 2025-02-06 -> 2026-08-09
asset      hyperliquid     binance    HL edge
BTC              7.65%       3.70%     +3.95
ETH              6.85%       3.18%     +3.67
SOL              2.08%      -0.70%     +2.78
PENGU            1.47%      -2.22%     +3.70
AVAX             4.90%      -0.34%     +5.25
WLD              4.83%      -1.78%     +6.61
ENA             -0.07%      -6.49%     +6.43
TRUMP          -17.81%     -18.18%     +0.37
BERA           -67.25%     -73.33%     +6.08

PAIRED DIFFERENCE                    +4.31 %/yr   t +11.9   9 of 9 positive
```

**Nine of nine.** Not asset mix, not window, not selection -- the same coin on
the same day pays more on Hyperliquid, every time. This is the strongest form of
the Finding 23 claim and it is now measured rather than argued.

## Three confounds that had to be removed first, and one that reversed

The raw Hyperliquid strategy number is **+14.40%/yr net, t +22.8** (12 assets,
6-week rebalance, exit rank 9) against Binance's +7.80%. Taken at face value
that is nearly double, and taking it at face value would have been wrong three
times over:

1. **Cadence.** Hyperliquid settles 24x/day against Binance's 3x. Counting
   settlements as observations inflates its t-statistic by ~sqrt(8) for no
   reason but its clock. Everything above is aggregated to daily, summed within
   the day because a holder receives every settlement.
2. **Window.** These 550 days are a *low* funding regime on Binance: the same
   strategy on the 9 overlapping names over this window nets **+1.11%**, against
   the +7.80% Binance earned over the longer 2024-2026 window. Comparing
   Hyperliquid-recent against Binance-historical would have flattered
   Hyperliquid by more than the venue effect is worth.
3. **Asset mix.** Hyperliquid lists HYPE (+19.17%/yr) and PURR (+28.34%/yr),
   which exist nowhere else and carry much of the headline. Strip them and the
   remaining majors average +5.65%.

The fourth was a **liquidity bar I applied at the wrong scale.** Hyperliquid
spot is thin -- AVAX trades $2,698/day against Binance's $5.1M, a factor of
1,900 -- and I first read that as disqualifying. It is not, at this size: $60
into a $105K/day book is 0.06% of volume. Worse, the concern was backwards. The
dead-spot names (BERA, TRUMP, WLD at $0) all pay *negative* funding, so a
long-spot/short-perp basket would never select them, while the top of the
ranking -- HYPE, ETH, BTC, SOL -- are the deepest spot books on the venue.

## What a book that can actually be held earns

Restricted to the six names with spot depth at this size, held statically with
no rebalancing:

```
BTC ETH SOL HYPE PENGU PURR, 601 days
  gross +11.68%/yr   t +19.4   entry cost 0.18%/yr   NET +11.50%/yr
```

Turnover is one entry, so the cost line nearly vanishes -- the opposite of the
Binance basket, where turnover was the thing that nearly killed it.

## The two facts that change the operator story

**Hyperliquid does not authenticate with a key and a secret.** ccxt reports
`requiredCredentials = {privateKey, walletAddress}`. `TradingCredentials` models
a CEX pair and cannot express this, so P4.2 has a real gap.

It is also a **worse security posture by default**, and the reason matters: the
Binance instruction was to disable withdrawals on the key, and *a raw private
key cannot have withdrawals disabled, because it is the wallet.* The equivalent
control is a Hyperliquid **agent wallet** -- approved to trade, unable to
withdraw. That is the only form in which this should ever hold a key, and it
preserves exactly the property the Binance advice was buying.

**Minimum order is $10 on every leg of every asset**, against Binance's $50 BTC
perp floor. A delta-neutral pair therefore needs $10 of spot plus $10 of perp
notional, and at 3x margin the perp leg costs $3.33 of capital:

```
per name    $10.00 spot + $3.33 margin  =  $13.33 capital
6 names                                 =  $80 minimum
```

So **$200 runs the full six-name basket with room to rebalance, and $100 runs
it**. Finding 18's conclusion that a $200 Binance book cannot hold BTC or ETH
does not apply here. **$15 still does not work** -- it buys one leg of one pair.

# Finding 25 — the selector had no venue filter, and it was invisible while one venue existed

Found while preparing H4, the Hyperliquid funding ingest, and it is a defect the
ingest would have activated rather than caused.

`carry_loop._settlements` filters funding by `funding_venue`. `crosssectional.
_funding_window` did not:

```sql
WHERE c.entity_id = ANY($1::uuid[])
  AND c.claim_type = 'funding_rate'
  AND c.knowledge_date <= $2          -- and nothing about which venue
  AND c.event_date >= $4
```

Every funding claim in the store is `binance:`-keyed today, so the missing
filter has never selected a wrong basket. The moment a second venue lands it
does, and nothing fails: the loop runs, the ingest reports success, and the
basket is quietly chosen on a number nobody computed.

## Why a distinct key was not enough

The docstring said `key` carries the venue "which keeps two venues' streams
distinct", and that was true of the thing it described -- `DISTINCT ON
(entity_id, key, event_date)` stops one venue's settlement being read as a
*restatement* of another's. Keeping streams distinct is not keeping them apart.
Both still land in one entity's window, and `_trailing_score` averages whatever
is in it.

The average has no unit. Hyperliquid settles hourly and Binance every eight
hours, so identical annual carry arrives as a per-settlement mean **eight times
smaller** on Hyperliquid. A blended mean ranks a name partly by which venues
happen to cover it. `_trailing_score` even names "a venue on a different
cadence" as a reason to take a mean rather than a sum -- correct for coverage
gaps *within* a venue, and no help at all across two.

## The worse half

The accrual path filters and the scoring path did not, so after H4 the book
would have **selected against one venue's funding and settled another's**. Not a
mis-ranking -- two strategies sharing a portfolio, with the P&L attributed to
whichever one the reader assumed.

## The fix, and the test that had to be written twice

`funding_venue` is now required on `select_carry_basket`, no default, for the
reason `as_of` has no clock default: the wrong one is silent.

The first test asserted only that another venue's settlements stay out of the
score. That test also passes when the filter matches *nothing* and every name
abstains, so a mirror test ranks the same six names on an inverted Hyperliquid
stream and asserts the basket flips. The mirror caught its own version of this:
seeded with only three of the six names it abstained on `universe_too_small`,
which is the filter working and the test being wrong.

Mutating the filter to a typed tautology fails exactly one test, the one written
for it.

# Finding 26 — the same omission as Finding 25, one layer over, in the mark

Found while building H4, and it is Finding 25's shape exactly: a query that
reads a venue-specific quantity without saying which venue.

`carry_loop._PRICE_AT` took the newest visible `price_snapshot` for an entity
and stopped there. BTC in the live store carries six:

```
okx           2,777      binance     2,777      (no venue named)  2,437
bybit         1,861      kraken        721      hyperliquid         553
```

So the mark was **whichever source published most recently** -- decided by
ingest timing, changing between cycles, stated nowhere. Checked live before the
fix, the price `_price_at` would have returned for BTC came from Hyperliquid,
purely because Hyperliquid's daily bar had landed and Binance's had not.

## Why this survived M6.1

Spot arbitrages tightly across venues, so the wrong source is wrong by basis
points and every number it produces looks right. That is the property that let
it pass a gate designed to catch exactly this class of error: **the failure is
not a bad price, it is a book valued against a venue it does not trade.** The
reconciler then compares that valuation to the venue's own and reports the
difference as a divergence -- a halt whose stated cause is a data-source
mismatch wearing the costume of a broken book.

It also predates today: the multi-venue price spine (M2) put three or more
sources on 28 of 30 assets, so this has been arbitrary since M2 landed. Adding
Hyperliquid prices did not cause it, it made it visible.

## The fix and its safe direction

`_price_at` now requires a `venue` and filters `value->>'venue'`. No default,
for the reason `funding_venue` has none: the wrong one is silent.

When the trading venue has not priced an asset the function returns `None` and
the cycle refuses that name -- `NO_MARK` when settling, `NO_REFERENCE_PRICE`
when sizing. Refusing is correct: the alternative is opening a pair sized off a
price the venue cannot quote, which is the naked-residual failure arriving
through the mark instead of through a rejected leg.

Verified this does not starve the existing book: every asset in the 30-name
Binance universe carries binance-keyed prices, and the only seven without them
are the Hyperliquid-natives added today, which Binance does not list.

## The pattern worth naming

Three times now, in three layers, the same omission: **funding accrual filtered
by venue and the selector did not (Finding 25); the mark did not (this one).**
Each was invisible while exactly one venue had data. The claim store is
multi-venue by design and every reader of it needs to say which venue it means
-- the ones that do not are not neutral, they are picking one by accident.

# Finding 27 — H5 passes, and the in-system number lands on the scratch one

The M6.1 gate, re-run on Hyperliquid. None of M6.1's result transfers: the venue
differs in settlement cadence (hourly against eight-hourly), quote asset (USDC
not USDT), symbol namespace, fee schedule and universe.

```
10 cycles, 2025-07-11 -> 2026-07-24, six-name universe, enter 2 / exit 4
cycle  as_of        held  in  out       funding
1      2025-07-11   2     2   0            0.00
2      2025-08-22   2     0   0           65.67
3      2025-10-03   2     1   1           42.27
4      2025-11-14   2     0   0           25.78
5      2025-12-26   2     0   0           18.16
6      2026-02-06   2     0   0           10.25
7      2026-03-20   2     0   0           19.42
8      2026-05-01   2     0   0           14.29
9      2026-06-12   2     0   0           25.45
10     2026-07-24   2     0   0           25.89

halts                  0
reconciled             OK      (book == venue, tolerance 0.01)
open positions         4       (two pairs, both legs each)
funding collected      247.18 USDC over 393 days
annualised on notional 11.48%
```

**11.48% in-system against 11.50% measured independently in H1.** Two code paths
that share no arithmetic -- one a scratch pandas walk over ccxt output, the other
the shipped selector, portfolio state, paper venue and reconciler -- agreeing to
two basis points.

That agreement is the whole point of the gate, and it is a stronger result than
M6.1's, where the loop and the backtest differed by 18.5% and the difference had
to be explained (constant notional against mark notional). Here the H1 figure was
a *static* hold, so it carries no notional drift to explain, and the loop
reproduces it.

## What it does not show

The window is 393 days, not the 601 H1 measured, because **Hyperliquid spot
launched much later than its perpetuals** -- BTC spot begins 2025-02-03 against
funding from 2023-05-12, and PENGU's spot begins 2025-07-10, which sets the
start. A carry book cannot be held before both legs exist.

One rotation in ten cycles, so hysteresis was exercised once. On a six-name
universe with `enter_rank=2` the top two are stable, which is a property of this
universe rather than evidence about the rule.

The paper venue does not distinguish spot from perpetual in its symbol -- both
legs carry `{asset}/USDC` and are kept apart by `market_type`. That is what M6.1
ran against too. Per-leg symbols are a `CCXTVenue` property (M12) and belong to
the live path, so this gate does not exercise them.

## The universe, corrected by the data

H1 called 19 assets pairable from ccxt's market list. The backfill showed that
**a listed spot market is not a trading one**: WLD, BERA and TRUMP returned no
OHLCV bars at all, matching the zero 24h spot volume measured in Finding 24. The
gate runs the six with real spot depth -- BTC, ETH, SOL, HYPE, PENGU, PURR --
which is the same set H1 measured at +11.50%.

# Finding 28 — 84 pre-registered tests, one survivor, and it decayed to zero

The question asked: is there directional accuracy anywhere in this universe
large enough that leverage or options on it would be sane?

Five signals had been tested before and four lost. Five is not evidence about
the space of signals, so this tested a battery in one pass: 21 cross-sectional
signals x 4 horizons = 84 tests, over 30 assets and 2,777 days.

## The bar, fixed before any result was seen

Testing N signals and keeping the best guarantees a winner from noise: the
expected maximum |t| under the null is about `sqrt(2 ln N)`. At 84 tests that is
**2.98**, and a conventional `|t| > 2` was expected to occur **3.9 times by
chance**. Anything below 2.98 was declared meaningless in advance.

## What cleared it

```
range_position_20d   h=1  t 6.81  net 132%/yr  hit 54.2%
range_position_20d   h=3  t 5.63  net 123%/yr  hit 56.0%
dist_from_sma20      h=1  t 4.04  net  89%/yr  hit 51.8%
momentum_5d          h=1  t 3.80  net  73%/yr  hit 50.1%
momentum_20d         h=1  t 3.71  net  83%/yr  hit 51.3%
breakout_20d         h=1  t 3.71  net  73%/yr  hit 53.5%
```

Every survivor is a cross-sectional momentum variant and they agree, which is
harder to obtain by chance than one isolated cell. This does **not** contradict
Finding 7's `trend.sma` result: that graded a per-asset directional forecast on
hit rate, this ranks assets against each other and holds long-strong /
short-weak. Different shape, different answer.

`momentum_5d` returning +73%/yr on a **50.1% hit rate** is the clearest
statement available that hit rate is the wrong metric -- the return is entirely
magnitude asymmetry.

## The attack it survived

```
trim 1% tails    t 5.39 -> 5.82     STRONGER trimmed, so not outlier-driven
trim 5% tails    t 5.39 -> 6.08
cost curve       survives to 100 bps round trip (25.9%/yr)
long-only        +73.7%/yr  t 4.48  both legs independently significant
short-only       +49.8%/yr  t 4.31
turnover         33% per rebalance at h=1
```

Every way a cross-sectional backtest usually lies, it survived.

## The attack that killed it

```
                      h=1                  h=3
2019-2021        t 4.12  220%/yr      t 4.36  247%/yr
2021-2024        t 3.17  102%/yr      t 2.02   68%/yr
2024-2026        t 1.68   48%/yr      t 0.23    6%/yr
```

**Monotonic decay to nothing.** Below the pre-registered bar at h=1 in the
recent period and indistinguishable from zero at h=3.

The in-sample number is not a lie -- the effect was real and enormous. It is
gone in the window anyone would trade, and a strategy is only worth its most
recent third.

## Why this is the same finding as the funding decay

Funding fell from +30%/yr in 2021 to +2%/yr in 2026. Cross-sectional momentum
fell from 220%/yr to 48%/yr over the same span. Two unrelated strategies, one
decay curve, one cause: **capital arrived and competed it away.**

That is the central fact of this business and it has now been measured twice in
this codebase rather than assumed.

## The consequence for leverage, which was the actual question

An edge at `t = 1.68` and decaying cannot carry leverage. `g(L) = L*mu -
(L*sigma)^2/2` -- leverage scales the drift linearly and the drag quadratically,
so applying it to a decayed edge buys the variance and none of the return. This
is the arithmetic reason "size up the winner" empties accounts.

Had the battery been reported without the sub-period split, the headline read
`t = 6.81, 132%/yr net, robust to 100 bps` -- an unambiguous green light.

## What it points at

The battery ran on 30 large-cap Binance majors: the most liquid, most covered,
most arbitraged instruments in the asset class. Decay there is exactly what
capacity predicts, and the corollary is testable -- **the same signals may
persist in assets too small for capital to bother with.** That needs an ingest
of 200-500 names rather than 30, and it is the one directional hypothesis in
this project that still has a prior behind it.

# Finding 29 — the 30-name universe was protection, not a limitation

The carry book ranks 30 assets seeded for being major. Binance lists ~400
pairable perpetuals. Finding 28 had just shown that directional edge dies where
capital concentrates, so the obvious inference was that the carry universe was
too narrow and the tail would pay better.

**It does not, and widening the universe is actively harmful.**

## Raw funding first, 397 pairable names over 120 days

```
            n     mean       median    top-decile   names with t>2
majors     48    + 1.20%     +1.97%      + 6.30%          30
tail      349    -10.32%     +2.09%      +10.61%         164
```

The median tail name pays the same as a major. The **mean is -10.32%**, because
a long left tail of crowded-short names pays longs rather than shorts. The top
decile is richer (10.61% against 6.30%) and there are five times as many
candidates clearing t>2, which is what made expansion look attractive.

A caution that belongs next to any "richest names" list from Binance: several
show absurd t-statistics with 100% sign persistence (VELODROME t=157, BEAMX
t=143). That is not edge. Binance's funding formula carries a base interest
component of 0.01% per 8h ~ **10.95%/yr**, and funding pins there whenever the
perpetual tracks spot. Those names are sitting on the structural default.

## The strategy test, which is the one that decides

370 common days, 2025-08-05 to 2026-08-09, six-week rebalance:

```
universe          names  enter     gross     cost       NET       t
majors only          44      2     5.10%    1.97%     3.14%    20.0
majors only          44      3     5.17%    1.61%     3.56%    21.4
majors only          44      5     4.45%    1.27%     3.18%    17.4
majors only          44      8     3.71%    0.79%     2.92%     9.7
all pairable        337      2     8.69%    1.51%     7.18%     5.0
all pairable        337      3     3.80%    1.41%     2.38%     1.4
all pairable        337      5   -19.24%    1.45%   -20.69%    -2.6
all pairable        337      8   -10.29%    1.25%   -11.54%    -2.2
```

The majors hold 2.9-3.6% at every basket size, t between 10 and 21. The wide
universe swings from +7.18% to **-20.69%** on nothing but how many names are
held. A result that flips sign with a parameter that should barely matter is not
an edge.

## The mechanism, which is the opposite of the hypothesis

**In the tail, high trailing funding predicts NEGATIVE forward funding.** High
funding means crowded longs. In a liquid major that state persists long enough
to harvest; in a speculative name it unwinds violently, and a selector that
ranks on trailing funding buys precisely the names about to flip against it.

Selecting harder over a wider tail therefore selects harder for reversal. The
30-name universe was never a ceiling on this strategy -- it was the thing
keeping it out of names where its own signal inverts.

## The decay, seen a third time

The majors pay **3.14-3.56% net** over this window against the **7.80%**
measured over 2024-2026. Funding fell from +30%/yr in 2021 to +2%/yr in 2026;
cross-sectional momentum fell from 220%/yr to zero over the same span; and the
carry book itself is now roughly half what it earned over the longer window.

Three unrelated measurements, one curve. The honest ceiling implied by
everything measured in this project is **high single digits to low teens on
notional**, which is 8-11% on capital at 3x -- not a multiple, and not something
leverage rescues.

## What it leaves

Market making is the one remaining candidate whose economics differ in kind
rather than degree: it is payment for a service rather than a predictive edge,
so its capacity grows with venue volume instead of shrinking with competition.
That is the only reason to expect it not to trace the same decay.

# Finding 30 — market making is dead at base fee tier, and fees explain everything else

Market making was the last candidate with a real prior: payment for a service
rather than a predictive edge, so its capacity should grow with venue volume
instead of shrinking with competition. Measured before building, and it does not
survive contact with the fee schedule.

**First, a correction to this document's own earlier framing.** HLP's documented
15-30% was quoted here as the expected return for building a market maker. That
was wrong: HLP is Hyperliquid's liquidator of last resort and a large share of
its return is liquidation profit, which no retail market maker can access.

## The measurement

```
base-tier fees: maker 1.50 bps, taker 4.50 bps
a maker round trip costs 3.00 bps before any adverse selection

symbol   spread    5-level bid depth    edge per side
BTC       0.15b    $1,824,431           -1.42b
ETH       0.52b    $  926,482           -1.24b
SOL       0.13b    $   70,873           -1.43b
HYPE      0.18b    $   11,017           -1.41b
ZEC       0.20b    $   34,878           -1.40b
AVAX      1.08b    $    8,273           -0.96b
PENGU     3.18b    $   19,536           +0.09b
BERA      4.49b    $      181           +0.75b
PURR     18.06b    $       77           +7.53b
```

Spreads on every liquid instrument are an order of magnitude tighter than the
maker fee. The three showing positive edge are not opportunities: PURR carries
**$77 of depth**, BERA $181, and PENGU's +0.09 bps against $20k of depth needs
~$55M of turnover to earn $100 a day -- before adverse selection, which on a
memecoin would exceed the entire edge.

## Why, and it is not a skill gap

Professional market makers reach volume tiers that pay maker **rebates**. They
face a negative fee and can quote a 0.15 bps spread profitably. A base-tier
participant pays +1.50 bps into the same spread. The barrier is the fee
schedule, and it is a barrier by design.

## The structural explanation this exposes, which covers every prior finding

**Fees set a floor on how small an edge can be monetised, and at retail tiers
that floor sits above most of the remaining signal.**

```
market making        needs > 3 bps of spread     liquid spreads are 0.15 bps
short-horizon        taker costs 4.5 bps         1-minute moves are 1-5 bps
momentum (F28)       decayed to ~0               against a 20 bps round trip
carry                6-week hold                 round trip amortises to ~0.4%/yr
```

Carry is not the best signal in this project. It is the only one whose **holding
period is long enough that fees stop mattering**. That is why it survived and
the others did not, and it reframes the search: the question is not "what
predicts returns" but "what edge is large enough, or slow enough, to clear the
cost floor".

## The lever this points at, which had not been considered

Every prior effort here went into better signal. The measurement says the
binding constraint is **cost**, so the highest-leverage change available is a
lower fee tier -- rebate-paying venues, volume tiers, or fee structures that pay
makers at low volume. That changes what is monetisable more than any model
improvement, and nothing in this project has examined it.

Richer data (L2 book, tick, order-flow imbalance) remains the largest available
data upgrade and is free to collect from Hyperliquid's websocket. But it pays
off only where fees do not dominate: long holds, venues with different cost
structures, or markets without professional coverage. Collecting it to predict
1-5 bps moves while paying 4.5 bps to act is arithmetic that cannot close.

# Finding 31 — the parallel sweep: six unfed inputs measured, four dead, one marginal

Context for why this happened at all: the claim store held only three claim
types while NINE producers were registered. Five producers had been built and
never given data, so the earlier conclusion that "directional prediction fails
here" was true only of the methods that could be tested. The search was
narrower than this document represented it as being.

Six independent agents, each measuring one unfed input against a bar
pre-registered before any result was seen, with a mandatory chronological
sub-period split. Nothing was written to `src/`.

## Results

```
source reconnaissance   3 usable datasets found, 5 dead ends verified
variance risk premium   DEAD   BTC recent third t 0.78, ETH t -1.02
CFTC positioning        DEAD   subsumed by trailing funding; 0 cells survive control
open interest           DEAD   0 of 72; the shipped producer is WRONG-SIGNED
positioning ratios      DEAD   0 of 32; max |t| 2.36 at ZERO cost
token unlocks           MARGINAL: clears the letter, fails the substance
protocol fundamentals   (running)
```

## Two factual corrections to this document

**Binance's 30-day cap is a REST limit, not a data limit.** It was cited here
twice as a likely blocker. Two agents independently disproved it: **Bybit serves
5.9 years of open interest** (and windowed queries return delisted symbols, so a
survivorship-corrected panel is possible), and **`data.binance.vision` publishes
5.9 years of the positioning metrics** the REST endpoint caps at 30 days.
Neither signal was unmeasurable. Both were simply never measured.

**`oi.divergence` (producers.py:85) should not ship.** Its best configuration is
wrong-signed (gross t -2.36 to -2.77), a spanning regression shows it is 45-87%
pure price reversal, and its residual alpha over the price leg is insignificant
(t -1.30 to -1.60). More history will not save it; 5.9 years now exist and the
answer is no.

## Four methodological findings that outlive the negative results

**1. Validate the harness on a known positive.** The OI agent ran this project's
funding carry through its own machinery -- same universe, PIT rule,
non-overlapping periods, thirds, costs -- and recovered net t 8.0-11.6 full
sample, 3.3-6.5 recent third, 9 of 9 configs clearing. A test that cannot find a
known edge proves nothing when it finds none.

**2. Crypto cross-sectional t-statistics run hot.** A permutation null (300
draws, shuffling which name owns which history) put the null's own 95th
percentile at **|t| 2.2-2.5, not 1.96**, because of the single dominant market
beta. Every "t > 2" result in crypto is measured against the wrong distribution.
The `sqrt(2 ln N)` bars used here happened to be adequate; that was closer to
luck than design.

**3. Subtracting costs INFLATES significance on a loser.** Eight OI configs
looked significant net -- all with negative returns. Costs are near
deterministic, so subtracting them moves the mean without touching the variance
and mechanically pumps |t|. A significant net result with an insignificant gross
result is an artifact, not a discovery.

**4. A significant information coefficient is not a tradeable edge.** Positioning
rank-IC reached t = -8.35, which reads as enormous. The decile table explains it:
`corr(decile, MEDIAN forward return) = +0.852` against `corr(decile, MEAN) =
-0.313`. The signal predicts the typical name while the fat right tail sits in
the other decile, so the portfolio earns zero. IC screens are the standard first
pass in quant research and this is how they mislead.

## The one that nearly survived

Token unlocks. Largest quintile (mean 16.5% of float), 30 days before the event:
**-9.28%, t -4.91 full sample; -9.2%, t -3.61 in the recent third** against a
pre-registered bar of 3.094. Dose-response inside the quintile is monotone
(-6.6 / -7.9 / -11.1 / -13.2% as size rises 6.5% -> 37.3%), which noise does not
produce. **Fees do not bind for the first time in this project**: breakeven is
917 bps per event against 40 bps actual, a 23x margin.

It fails on substance:

```
pre-registered statistic      -9.16%   t -3.61   clears
DiD vs same-date controls     -8.07%   t -3.29   clears
momentum-controlled           -7.17%   t -2.99   fails
contract-enforced (PIT-safe)  -5.67%   t -2.01   FAILS
age-matched controls          -5.50%   t -1.99   FAILS
token de-meaned               -3.78%   t -1.42   FAILS
tradeable, net of funding              t  2.80   FAILS
```

The two most trustworthy controls both fail. Contract-enforced cliffs are the
only subset immune to schedule revision (63 of 370 schedules carry explicit
revision language in DefiLlama's own notes), and token de-meaning at t 1.42
suggests much of the effect may be that particular tokens fell and happened to
have unlocks.

And the tail is the real objection: **-61% maximum drawdown, worst single event
-74%**, on a book carrying ~3.5 concurrent short positions.

## The decay, now observed in every premium measured

```
unconditional funding     +30%/yr (2021)      ->  +2%/yr (2026)
carry strategy            7.80% (2024-26)     ->  3.14-3.56% (last year)
cross-sectional momentum  220%/yr (2019-21)   ->  zero
variance risk premium     20 vol points       ->  4.65, not significant
unlock short, funding leg +0.55%  ->  -0.60%  ->  -2.06%
```

The unlock case is the clearest instance because the mechanism is visible in the
data: crowded shorts into scheduled unlocks now push funding negative, so the
short pays to hold. Gross decayed 27%, net more than halved, and the trend
extrapolates to the edge being fully consumed within a few years.

**A competing explanation is now on the table and it is testable.** The VRP agent
found that its entire edge lives in the top volatility tercile (+57.4%/yr,
t 3.51) and is negative in the bottom -- and current DVOL sits at the **2nd
percentile** of its sample. If every premium here loads on volatility, then
these strategies are not dead; they are being measured at the bottom of a
volatility cycle. That predicts they return when volatility does, and the test
is a regression of each strategy's return on contemporaneous realised vol.

# Finding 32 — the decay is time, not volatility, and the sweep is complete

Two results close out the parallel sweep begun in Finding 31.

## The regime hypothesis, proposed and refuted

Every premium measured in this project has decayed. Two explanations fit equally
well and imply opposite actions: CROWDING (capital arrived, irreversible, stop
looking) or REGIME (risk premia compress when volatility does, reversible, build
and wait). The VRP agent supplied evidence for the second -- its entire edge sat
in the top volatility tercile with current DVOL at the 2nd percentile.

Tested directly on the strategy that pays, 78 monthly observations, 2020-02 to
2026-07:

```
TEST 1 -- does the premium load on volatility?
  mean funding          slope 0.224   t 1.27   r2 0.048
  top-quintile funding  slope 0.466   t 1.87   r2 0.090     not significant

TEST 2 -- does a time trend survive controlling for volatility?
  mean funding      volatility t -0.11   time trend -0.42/month  t -3.25
  top-quintile      volatility t +0.00   time trend -0.80/month  t -4.24
```

**When time and volatility compete, volatility collapses to exactly zero and
time wins decisively.** The decay is a function of elapsed time, not of regime.

The tercile table looks like support for regime in isolation -- low-vol months
pay 13.48% on the top quintile against 31.56% and 34.47% in mid and high -- but
that is confounded, because the low-vol months ARE the recent months. One
observation argues against pure collinearity: mid-vol pays slightly more than
high-vol on mean funding (15.74% vs 14.37%), so volatility is not merely a proxy
for time. It still goes to zero.

**Consequence: the compression is permanent, not cyclical.** The capital that
arrived is not leaving when volatility returns, and the honest forecast for the
carry book is continued decay at roughly -0.80 percentage points per month on
the top-quintile premium. The linear extrapolation must break at the boundary --
funding cannot stay deeply negative without inverting the trade -- but the
direction is unambiguous over 78 months at t -4.24.

## Protocol fundamentals: dead, and the market-cap check is why

`fundamentals.protocol` joins `oi.divergence` as a producer built on an
intuition that does not survive measurement. 8 signals x 4 horizons, N=32,
bar 2.633. Two cells cleared the recent third; both are void.

**The market cap was not point-in-time.** It was computed as `price_t *
(mcap_today / price_today)` -- one constant supply from a current snapshot
applied to every historical date. Verified against real per-day CoinGecko market
caps on the 33 largest protocols: median supply 365 days ago was **0.846x**
today's, drift reached **2.72x** at the worst, and **67% drifted more than 10%
over a single year** against a backtest spanning 5.5.

The bias is not noise, it is directional and self-fulfilling: high-emission
tokens have their past market cap overstated, are therefore ranked expensive,
are therefore shorted -- and high-emission tokens underperform. The signal was
partly measuring its own contamination.

The surviving cell dies a second, independent death: its long leg is liquid
staking and lending against short DEXs and bridges. An LST's TVL is other
people's staked ETH; a bridge's is escrowed supply. Category-neutralised, its
recent-third t falls from 2.51 to 1.18. It was a sector label, not a valuation
ratio.

## A fifth methodological finding: grid-alignment fragility

The choice of rebalance START date is arbitrary and nobody reports it. Sweeping
all equally-valid offsets, the two clearing cells cleared at **13 of 30** and
**24 of 60** alignments, with median t below the bar. The reported statistic was
one lucky draw from thirty. Any cross-sectional backtest that reports a single
alignment is reporting a sample of one from a distribution it never examined.

## The complete tally

```
reconnaissance          3 usable datasets, 5 dead ends verified
variance risk premium   DEAD
CFTC positioning        DEAD    (byproduct: carry Sharpe 3.82, DD -1.62%)
open interest           DEAD    (producer is wrong-signed)
positioning ratios      DEAD
protocol fundamentals   DEAD    (market cap void, grid-fragile)
token unlocks           MARGINAL: clears the letter, fails every substantive control
```

Six candidates on inputs that had never been fed. Five dead, one marginal and
visibly decaying in its funding leg. Combined with the regime refutation above,
the search is now genuinely complete rather than merely exhausted-sounding: the
premia are real, they decayed with time rather than with volatility, and no
regime change is waiting to restore them.

# Finding 33 — a public strategy library, measured: twelve tested, zero survive

A library of 43 strategy ideas compiled from public posts was supplied. Triaged:
~14 required intraday data this project did not hold, ~8 were already retired
here, ~7 were position-sizing rules rather than entry signals, leaving ~14
genuinely testable. Twelve were measured. **None survives.**

Everything below ran through `omni.research.harness`, which computes its bar
from an append-only registry of every test this project has run. The bar rose
from 2.677 to 2.925 during the sweep, which is the mechanism working.

## Candlestick and wick patterns — 0 of 36

The specific question was whether wick patterns are "reliable above 50%".

```
signal                     h   gross%      t     3rd%   3rd t  align  null95
close_in_bar_range         1    95.3%   3.81    68.3%    2.31   100%   2.11
body_vs_trailing           1    92.9%   4.05    48.4%    1.60   100%   1.66
lower_wick_ratio           7     3.7%   0.15    63.3%    2.54     0%   1.78
hammer                     3   -44.5%  -2.01   -48.5%   -2.08     0%   2.02
```

Two read as strong winners at t 4.05 and 3.81, gross ~95%/yr, clearing 100% of
rebalance offsets -- and both die in the recent third. `lower_wick_ratio` at h=7
shows the alignment guard earning its place: a recent-third t of 2.54 that
clears at **zero of seven** valid offsets.

`hammer` at t 2.01 against a measured null 95th percentile of **2.02** is the
cleanest available demonstration that 1.96 is the wrong threshold here.

**A 50% hit rate is the wrong question.** `momentum_5d` returned +73%/yr at a
50.1% hit rate (Finding 28); the return was entirely magnitude asymmetry.

## The halving cycle — why n=4 can never be evidence

```
mean +2177%/cycle, 98,800x compounded    the headline
t 2.72 on df=3, critical value 3.182     fails the ORDINARY bar
95% CI on the mean                       [-370%, +4723%] -- contains zero
vs non-overlapping 1000-day BTC holds    Welch t 1.57, p 0.161
in market 77% of the time                so most of it is beta
```

**With n=4 the smallest attainable p-value is 0.5^4 = 0.0625 one-sided.** No
arrangement of four observations reaches p<0.05 without assuming normality; the
sample size caps the evidence before the data is examined.

Two traps demonstrated rather than described. Sweeping 36 (lead, lag) pairs over
the same four points produced **t = 4.44**, above the bar -- the maximum of 36
draws, not a finding. And a bootstrap reporting "0.000 of draws below zero" is
an arithmetic identity when all four observations are positive, not a confidence
statement.

## Grids — the number they hide

Percent of peak capital the ladder had to fund, 10 bps per fill, hourly bars:

```
             realised    open MTM   annualised   worst MAE
BTC 2%        +47.6%      -11.9%      +4.1%       -47.5%
ETH 2%        +67.4%      -19.6%      +5.3%       -52.9%
SOL 5%       +124.4%      -32.9%     +11.4%       -91.4%

buy-and-hold, identical windows:  BTC +45.7%/yr  ETH +42.2%  SOL +72.1%
```

**A grid earns 2-11%/yr for carrying inventory that was at one point down 45-92%
of the capital funding it.** Buy-and-hold beat every configuration. This also
explains the library's "+965%" and "+2,563%" claims: report realised
closed-trade profit, ignore open inventory, divide by deployed rather than
reserved capital.

**Ascending grids are relabelling.** Buy and sell counts are identical across
uniform, linear and geometric weighting (1,897 buys / 1,864 sells in every
case). A weighting that cannot change which trades occur cannot change the risk
profile, only the denominator.

## Allocation rules and rotation

200-SMA / 50-SMA / 90-day-return switches: block t 0.34-0.55 against a bar of
2.734. Sharpe rises slightly but **Sortino falls** -- they clip upside harder
than downside. Sweeping the lookback 20 to 300 gives t from -0.11 to +0.67, so
200 is not special. Year-end top-performer rotation returned **-10.8%/yr against
+54.1%** for equal weight.

Momentum + volume + breadth came closest: gross t 3.44 over a bar of 2.86, then
failed the recent third (2.15) and the alignment sweep (43%). Adding the volume
filter made momentum **worse** (3.44 to 1.01).

## Sessions are real, and the ICT family is still dead

The intraday agent expected crypto's 24/7 clock to be flat and was wrong:

```
within-day variance share x24, BTC/ETH/SOL pooled
hour  00    03    05    08    11    13    14    15    17    20
      1.26  0.73  0.65  0.81  0.78  1.35  1.64  1.47  1.15  1.03

trough 05:00 UTC   peak 14:00 UTC (US cash open)   ratio 2.5x pooled
BTC by year: 2022 2.76 -> 2023 3.66 -> 2024 3.57 -> 2025 3.93 -> 2026 3.89
```

Crypto's clock is imported from TradFi and the effect is **strengthening**.

The premise being real does not rescue the strategies. Opening Range Breakout --
the family's best case, fully mechanical, two parameters -- failed 54
pre-registered tests with **mean -0.186 R per trade**, then failed a further 126,
then failed an honest whole-clock sweep:

```
6 of 72 clear the GROSS bar, all at 01-02 UTC (Tokyo cash open)
best: gross 14.20 bps, t 4.53
net @9bps t 1.66   net @11bps t 1.02   0 of 72 survive
thirds, gross:  30.5 -> 8.8 -> 3.3 bps
```

**In the recent third the gross edge (3.3 bps) is below the fee-only floor
(9 bps).** And the claimed 73-84% win rates are outside the achievable
distribution: a random long with a symmetric bracket hits upper 0.477-0.507, the
no-information baseline, while cost moves break-even at 1:1 from 0.500 to
0.54-0.67.

## The one actionable positive from roughly 250 tests

The hour-of-day variance surface is not a signal, it is an **execution fact**.
Rebalancing the carry book at 03-07 UTC rather than 13-16 UTC roughly halves the
variance crossed on every leg. Free, permanent, no new risk, and it improves the
only strategy that works.

## Return distribution versus the cost floor, quantified

```
             median |r|   frac > 9bps @1m    @5m     @15m
BTC            3.22 bp      16.1% (9.8%)    41.3%   61.0%
ETH            4.31 bp      24.1% (17.5%)   51.7%   69.9%
SOL            6.35 bp      37.4% (23.0%)   65.4%   79.8%
```

The median BTC one-minute move is **36% of a single round trip**, and the
bracketed figures are the last twelve months, all lower. Perfect-foresight
maximum favourable excursion over 60 minutes is 48 bps on BTC, of which cost is
18.6%. This is the cost floor of Finding 30, measured directly on the tape.

# Finding 34 — the harness had a live float-compared-to-zero bug, in the module written to prevent exactly that

The research harness was committed with fourteen tests and a mutation check
proving its recent-third gate discriminates. The data-source layer built on top
of it then found five defects in it, one of them live and severe.

```python
t = mean / se if se > 0 else 0.0        # as committed
```

`np.std(ddof=1)` of a constant series returns **2.0992353026283886e-17**, not
`0.0`. So `se > 0` is always True, and:

```
r = np.full(60, 0.05)
_stat(r, 1) -> Leg(mean_ann_pct=1825.0, t=1.8449496068202412e+16, n=60)
```

**A spread with no variation at all produced |t| = 1.8e16 and a PASSING
verdict.** This is precisely the float-compared-to-zero failure `AGENTS.md`
forbids, sitting in the module whose entire purpose is to not lie.

## Why the mutation testing missed it

The mutations exercised the **guards** -- the recent-third gate, the alignment
sweep, the cost-inflation check -- and every one discriminated. None touched the
arithmetic underneath them. A guard can be perfectly tested while the statistic
it guards is nonsense, and that gap is the lesson: mutate the computation, not
only the control flow.

## The other four, all fixed

- **`_stat` fabricated `Leg(0.0, 0.0, n)` for n < 2**, making a measured zero
  indistinguishable from an unmeasurable one. Now raises.
- **`_periods` appended a substituted `0.0`** to the information coefficients
  whenever a period was degenerate. Those invented zeros shrink the variance and
  pull the mean toward zero, so the IC statistic was biased by however many
  degenerate periods a panel happened to contain -- a fabricated observation
  dressed as a measured one. Now skipped.
- **`_permutation_p95` permuted column labels without re-imposing the
  availability mask.** A short-lived name's scores landed on a long-lived name's
  prices, changing how many assets were rankable per period, so the null was
  measured on a different sample than the statistic it calibrates. Minor on a
  panel of survivors; material now that `BinanceArchiveSource` makes
  survivorship-corrected panels routine.
- Two unused imports.

Five new tests, nineteen on the harness total.

## The probe defect the source layer found in itself

Worth recording because it is the same shape as the truncation trap. Its first
ccxt probe used `since=0` to ask a venue for its earliest bar. **Bybit reads
`since=0` as "no since given" and answers with its NEWEST bar**, so BTC/USDT was
reported as having one day of history on a venue serving it since 2021-07-05.

One day is not a harmless understatement -- it is exactly what a new listing
looks like, and a `min_history_days` filter would have silently dropped the
asset. It now probes from Bitcoin genesis and raises if the answer lands within
one bar of the newest.

## The pattern across today

Four separate silent-truncation traps surfaced in one session: a rate limit
returning a short series (Finding 24), Binance's monthly kline aggregates
omitting five days the daily partition contains, `since=0` meaning "newest", and
DefiLlama's paywall being route-specific rather than data-specific. Every one
produces data that is well-formed, plausible, and short. **None raises.**

# Finding 35 — the oscillator family IS the momentum family with the sign flipped

Sixteen mean-reversion signals x four horizons, run through the harness.
**Zero of 64 pass, and 59 of 64 are wrong-signed** -- buying the oversold name
and shorting the overbought one loses 80-155 %/yr gross at h=1.

## The structural result, which is worth more than the verdict

```
williams_r_n_neg   ==  100 x (1 - range_position_n)     exact affine negation
below_sma_n        ==  -(dist_from_sma_n)               exact negation
inclusive 5-bar breakout == 1 - williams_r_5/100        agrees to 2.2e-16
```

`range_position_20d` (t 6.81) and `dist_from_sma20` (t 4.04) were this project's
two strongest retired momentum signals in Finding 28. **The strategy library's
oscillator family is that same family with the sign reversed.** Its full-sample
significance is a re-measurement, not new information, and its 59 negative cells
are roughly two or three effective tests rather than 59 confirmations.

The agent refused to run the inclusive-window breakout on those grounds --
testing a signal and its own negation and reporting whichever won is the dredge
the bar exists to punish.

## The disclosed non-survivor

The **inverse** of `williams_r_14_neg` at h=1 clears every gate: recent third
+96.0 %/yr at t +3.31, 100% of rebalance offsets, above the null p95 of 2.55.
It is not proposed and should not be, for three reasons stated at the time it
was found: it is the sign flip of a tested cell, it is a rediscovery of the
already-retired `range_position` family, and its thirds (+276 -> +94 -> +96)
show the same 3x collapse that retired momentum the first time. It is also
h=1 -- daily rebalancing, where 20 bps of turnover costs roughly 73 %/yr, so a
gross +96% is not a net strategy.

## The win-rate claim, tested directly

The library asserts 70-84% win rates. Long-quintile holds, RSI(2):

```
config                    h   trades   RAW win  RAW mean   EXCESS win  EXCESS mean
unfiltered                1   12784     50.6%    +0.07%      43.3%       -0.09%
unfiltered                14    908     47.9%    +1.55%      39.4%       -0.90%
above SMA200              1    3734     49.9%    -0.04%      41.5%       -0.20%
above SMA200              3    1241     47.4%    -0.11%      39.3%       -0.64%
```

**Observed win rate is 47-51%, never 70-84%.**

The 200-SMA filter claim is refuted in both halves: it does not raise the win
rate (50.6% -> 49.9%) and it **flips raw expectancy negative** at h=1 and h=3.
It costs expectancy and buys nothing.

And the RAW column is mostly beta -- it rises monotonically with horizon
(+0.07% -> +1.55%) purely because holding crypto longer captures more drift.
Subtract the cross-sectional mean and every cell is negative. The filter also
discards **1,627 of 2,776 rebalance dates**, so it is a market-timing overlay
wearing a stock-selection costume, timing exactly the bull phase where its raw
numbers look best.

## A harness gap found by use, now closed

In eight cells the rank IC and the portfolio were **both significant and
pointed in opposite directions**:

```
below_sma_10      h=1   ic_t +4.66   gross t -4.02
rsi_2_neg         h=1   ic_t +4.18   gross t -3.40
bollinger_z20_neg h=1   ic_t +2.03   gross t -6.29
```

The oversold score really does rank the median asset correctly, and the quintile
spread loses 85-150 %/yr anyway, because the fat right tail sits in the
overbought decile the signal shorts. This is the median-versus-mean trap from
the positioning study (Finding 31), reproduced independently on price-only
signals.

The original guard only fired when the portfolio was **insignificant**. A
significant portfolio pointing the other way is the stronger version of the same
lie and passed silently. Now named: *the sign that ranks is not the sign that
earns.* Two tests added, 21 on the harness.

## Untestable, which is not the same as null

`gap_fill` cannot be measured on a 24/7 venue. The "gap" is the UTC-boundary
print: standard deviation **0.057%** against a daily-return standard deviation
of 5.58%, i.e. **1.0% of daily volatility**, with |gap| > 0.5% on 0.098% of
asset-days. All four cells sit inside the permuted null. That is not evidence
that gaps do not fill -- it is evidence that the variable barely exists here.
Reporting it as a null would be a category error.

## Concurrency note on the registry

Three agents appended to the shared registry during this sweep, so the bar rose
from 2.72 to 3.12 mid-run and each row was judged against the bar in force at
that moment. Everything was re-judged at the final 3.115: still zero pass. The
count is correct and the mechanism is working; a per-cell bar is a timestamp,
not a property of the signal.

# Finding 36 — the fee lever is real, and calibrated to exclude retail

Finding 30 concluded that cost is the binding constraint and named the one
unexamined escape: a venue that PAYS makers at a volume tier a small account can
reach. That would move the floor for every strategy already killed, which is
worth more than any new signal. Probed, and closed.

Thirteen venues checked. Five publish tier tables; two show negative maker fees.

```
KuCoin maker ladder -- volume is BTC of 30-day turnover, not dollars
vol      0  -> +10.00 bps
vol     50  ->  +9.00
vol    200  ->  +7.00
vol    500  ->  +5.00
vol  1,000  ->  +3.00
vol  2,000  ->   0.00 bps     ~$240 million / month
vol 15,000  ->  -0.50 bps     ~$1.8 BILLION / month
kraken      ->   0.00 bps at $10,000,000
bitfinex    ->   0.00 bps at  $7,500,000
```

**The rebate exists and requires roughly $1.8 billion of monthly volume.** The
first tier that merely reaches zero needs ~$240 million. A $200 book is short by
about seven orders of magnitude.

KuCoin's own live endpoint confirms the base rate rather than ccxt's cached
table: `XBTUSDTM maker 0.0002` -- **+2 bps, a fee.** ccxt's tier data was read
first and briefly looked like a reachable rebate at "volume 15,000"; the
denomination is what settles it, and the denomination is BTC.

**This is the fee schedule working as designed.** Professional market makers are
not better at quoting than everyone else -- they are on the other side of a
volume gate, and the gate is calibrated so that only genuine institutional flow
crosses it. Finding 30's conclusion is therefore structural rather than an
artifact of venue choice: at retail size the cost floor cannot be escaped by
shopping for venues.

## Two leads from an outside session, verified and failed

An external research session (Grok, given `RESEARCH_DOSSIER.md`) supplied
specifics for the two practical blockers. Both were probed rather than trusted.

**ETF custody addresses -- not custody wallets.** Two addresses were supplied as
Fidelity and BlackRock custody:

```
bc1qnzauugj9fvda5my...  0.0 BTC now,  n_tx 2,  received 1,100 BTC
bc1qnzqfan6cnmp67wg...  0.0 BTC now,  n_tx 3,  received   300 BTC
```

Both are **empty**, with two and three transactions. IBIT custody holds hundreds
of thousands of BTC across thousands of transactions. These are transit hops.
The address-list blocker is unchanged, and the mechanism (timestamped balance
history from `blockchain.info/rawaddr`) remains verified and unused.

**Free CME history -- too thin.** Yahoo's `BTC=F` returns **452 daily bars over
8.7 years** against roughly 2,200 trading days in that span, about one bar in
five. Stooq serves a JavaScript challenge. Gap-fill on CME remains answerable in
principle and unanswerable with what is free so far.

## What this leaves

The cost floor is now confirmed from both directions: measured on the tape
(Finding 30, Finding 33) and confirmed unescapable by venue selection (here).
Every remaining open item is a forced-flow or capacity argument, not a cost
argument:

```
R1                   long-tail momentum, 200-500 names, data already verified
index rebalances     the one genuinely new idea from the outside session;
                     Russell/S&P is heavily arbitraged, crypto indices are not
ETF flows            blocked on an address list, not on a paywall
unlocks              forward-testable against a known calendar, zero lookahead
```

# Finding 37 — three ways the system could have been biased, built and measured

The question raised was whether the measurement system might itself be broken --
optimising toward rejection rather than toward truth. That is a fair challenge
to a project that has retired thirteen strategies in a day, and it is answerable
rather than arguable. Three plausible biases were identified, implemented, and
measured.

## The three, and why each was plausible

1. **The bar controls the wrong error rate.** `sqrt(2 ln N)` is family-wise
   control -- the probability of even ONE false positive across every test ever
   run. That is correct when hunting a single true effect among nulls and brutal
   when several real effects exist. Benjamini-Hochberg false-discovery control
   is the honest correction for a genuine search.
2. **Equal weighting was inherited, never decided.** Crypto volatilities differ
   several-fold, so an equal-weight basket is dominated by its wildest name and
   its t-statistic is largely a statement about that name.
3. **Every measurement tested ONE signal alone.** Real high-Sharpe books are
   portfolios of weak uncorrelated signals. An ensemble is a different object
   from any of its parts, and this project had never built one.

## Measured on real data, 30 assets, 2019-2026

```
signal       weighting       gross%       t      3rd%   3rd t  sharpe  align
momentum     equal            66.3%    2.79     15.4%    0.52    1.02   57%
momentum     inverse_vol      65.9%    2.80     13.6%    0.47    1.02   57%
carry        equal           -54.2%   -2.28    -89.9%   -4.25   -0.89    0%
carry        inverse_vol     -44.5%   -2.03   -100.9%   -4.45   -0.79   29%
blend        equal            43.9%    1.81    -33.6%   -1.24    0.71    0%
blend        inverse_vol      36.1%    1.56    -40.0%   -1.37    0.61    0%

bars in force:  strict 3.130    balanced / FDR 2.542
```

**None of the three changes a conclusion.**

- **Inverse-vol weighting is a wash**: Sharpe 1.02 to 1.02. Better weighting
  improves risk-adjusted return where there IS return; it cannot manufacture
  one.
- **The blend is worse than its best component**: 66.3% to 43.9%, Sharpe 1.02 to
  0.71. The components are genuinely independent (cross-family correlation
  **-0.013**), so a blend should have helped -- and did not, because
  carry-as-a-directional-score is reliably negative.
- **FDR is materially looser** (2.54 against 3.13) and changes nothing, because
  momentum's recent third is t = 0.52. Not close at any threshold.

That last point is the substantive answer to the challenge: the conclusions are
**not bar-dependent**. Relaxing the correction admits only results that then
fail a different guard.

## What the failed blend accidentally proved

The carry row is Finding 12 reproduced in a single line. Funding as a
**directional forecast** scores t -4.25 and is reliably negative. The same
funding as a **delta-neutral harvest** returns +11.5%/yr at Sharpe 3.8. Same
data, same assets, same window -- the SHAPE is the entire difference, and the
blend demonstrated it again by getting worse when the two shapes were mixed.

## What was kept anyway

`strictness` is now an explicit argument with three settings, and the default is
stated as a **choice about the operator's situation** rather than a property of
statistics: small capital and no track record make a funded false positive far
more expensive than a missed opportunity. Someone else's asymmetry may invert,
and now they can say so in an argument rather than by editing a property.

`weighting` and `combine()` are kept because the blind spots are now closed by
measurement rather than by assumption -- and because `combine()` returns the
component correlation matrix, which is the only thing that makes an ensemble
worth attempting. Blending two of this project's price signals would blend a
signal with itself: `williams_r_n` is exactly `100 x (1 - range_position_n)`.

Six new tests, 27 on the harness.

# Finding 38 — R1, long-tail momentum: dead, and the prediction came out backwards

Dated 2026-08-09. The last item on the previous session's ranked list, and the
only one that had both a mechanism and available data. Honest prior was ~20%.

**The claim.** Every momentum variant retired in this project died on the 30
most liquid, most arbitraged names. Capacity theory says an edge survives where
capital cannot fit, so if crowding is why they died, the same signal should live
in the tail.

**The panel.** 832 Binance USD-M perpetuals quoted in USDT, daily closes and
quote volumes from `data.binance.vision`, 2020-01-02 to 2026-08-01 -- 612,421
observations pulled in 232 seconds with zero errors. Delisted symbols recovered
(MATICUSDT, MKRUSDT, FTTUSDT and LUNAUSDT are all present), so the long leg is
not selected on having survived.

**The split, point-in-time.** Trailing 30-day median quote volume, shifted one
day so the decision cannot see the bar it is made inside.

```
                     names/day   ever   median trailing liquidity (USD/day)
head  = top 30              30    329   $207M - $337M
tail  = rest, >= $1M/day   139    763   $3.5M - $44M
```

A ~50x separation in liquidity, and the $1M floor is load-bearing: below it a
name cannot be traded at any size, so including it would fabricate the capacity
the hypothesis is about.

**The test.** 30-day trailing return, ranked cross-sectionally within each
group, quintile long-short, equal weight, horizons 7/14/30, 40 bps round trip --
double the 20 bps used on liquid names, because the tail's edge has to clear the
tail's spread. Strict bar 3.14. Six cells, recorded.

```
                 gross %/yr      t     recent third %/yr      t    verdict
tail   h=7             2.60   0.10                 -4.67  -0.10    fail
tail   h=14          -10.62  -0.39                -83.73  -1.28    fail
tail   h=30           26.32   0.90                -19.19  -0.45    fail
head   h=7            79.54   1.46                176.96   1.26    fail
head   h=14           43.80   0.74                160.98   1.11    fail
head   h=30           33.25   0.61                174.26   1.22    fail
```

**Largest |t| anywhere is 1.46 against a bar of 3.14.** Costs never enter the
argument: the gross statistic fails, so there is nothing for a cost assumption
to take away.

**The prediction did not merely fail to confirm, it inverted.** Capacity theory
predicts tail > head. The tail is worse than the head at every horizon, on the
full sample and on the recent third. Whatever kills momentum here, it is not
that the liquid names are too arbitraged -- the illiquid ones are worse.

Two things not to over-read:

- **The tail's rank IC is negative and nearly significant** (t -3.12 at h=7,
  -2.00 at h=14, against a 3.14 bar). That is reversal, not momentum, and it is
  the shape trap 4 describes: it orders the typical tail asset and earns nothing
  (portfolio t 0.10). It is also below the bar, so it is a direction to note, not
  a result.
- **The head's recent third is large and positive at all three horizons** (+161%
  to +177%/yr, t 1.11 to 1.26). This is ONE window measured three overlapping
  ways, not three confirmations -- the last third of the same 2020-2026 sample is
  roughly the same calendar period in each row, over the same 30 names. And all
  three head cells are UNCALIBRATED: the permutation null could not run on them
  at all, for the reason in Finding 39.

**The two facts from the previous session survive intact.** Costs set a floor
and the decay is elapsed time. Nothing here contradicts either, and the tail --
the one place capacity theory said to look -- is the worse half.

# Finding 39 — the permutation null's cross-section collapses to n^2/N on a universe-restricted signal

Dated 2026-08-09. Found while running Finding 38, in the guard whose whole job
is to stop crypto's hot null passing for significance.

`_permutation_p95` permutes column labels, so each asset keeps a real score
history against a real price history in the wrong pairing. It then re-imposes
the original availability mask -- added deliberately, because permuting labels
alone lands a short-lived name's scores on a long-lived name's prices and
changes how many assets are rankable each period.

**That re-imposition intersects two masks that are only the same mask when the
signal scores every column.** When the signal scores n of N columns, the permuted
values agree with the original availability on about n/N of them, so the null
ranks roughly `n^2/N` names against the statistic's `n`.

Measured on the Finding 38 panel, N = 832:

```
group   statistic ranks   null ranks   predicted n^2/N   days clearing MIN_ASSETS
tail                138           18              22.9      1,645 of 2,404
head                 30            1               1.1          0 of 2,404
```

**The head's guard did not fail loudly -- it returned NaN, and NaN fails every
comparison, so the absence read as a pass.** Three cells were reported with no
calibration against the market factor and nothing said so. The tail's guard did
run, on a cross-section of 18-32 names calibrating a statistic taken over 157.

**Why it survived every earlier test.** Every signal measured in this project
before Finding 38 scored the whole panel. With n = N the intersection is a no-op
and the mask is exactly the fix its comment claims. It only bites on a signal
that restricts its universe -- which is precisely what a capacity test is, and
what any regime, sector or liquidity-conditioned test will be.

**What was changed, and what deliberately was not.** `_permutation_p95` now
returns the cross-section it measured alongside the p95, and `evaluate` warns
when the null could not be measured at all, or when it ranked materially fewer
names than the statistic did. The shrink itself is NOT fixed, because fixing it
means choosing between two things the current design gets for free and cannot
both keep once availability varies by date:

- permuting labels globally preserves each asset's score autocorrelation as a
  block, and therefore the persistence of holdings across periods;
- permuting within each date preserves the cross-section size exactly, and
  re-randomises holdings every period, which makes the null's t-distribution
  TIGHTER and the guard more permissive.

A guard made quietly more permissive is the wrong repair to make at the end of a
session. The absence is now stated, which is the difference between a
measurement and a blank; the choice between null designs is left open and
deliberate.

**Neither this nor its fix changes Finding 38.** A |t| of 1.46 against a bar of
3.14 fails whatever the null's 95th percentile turns out to be.

The Finding 38 re-run after this change was made with `record=False`: it is the
same six cells reported properly, not a second search, and recording it again
would have inflated N with tests nobody ran.

# Finding 40 — the external dossier's top-ranked candidate loses to what is already running

Dated 2026-08-09. An outside research document (`MARKET_RESEARCH_MASTER_ROUND7`,
seven rounds, ~13,400 lines) ranks **Binance dated-futures cash-and-carry** as
its single GREEN-HISTORICAL candidate: "Build this first... the cleanest
candidate in the entire dossier for converting external research into a measured
project finding." Its Queue A puts it at #1 of 12.

It asks for a census first -- archive integrity gate, top-of-book
synchronisation, settlement mapping, three capital denominators, a four-point
cost curve. That is a substantial build. **This project's own doctrine says
measure before building**, so the basis was measured first, from data already
reachable through `BinanceArchiveSource`.

**Census, which is itself a finding.** 48 dated USDT contracts, 26 expiries,
2021-02 to 2026-08, 6,235 contract-days. Underlyings: **BTC and ETH only.**

**Annualised basis on spot notional, by year:**

```
        n    25%     50%     75%
2021   705   7.52   13.71   25.57
2022   764  -0.64    1.77    3.17
2023  1010   3.93    5.23    7.82
2024  1456   9.17   11.49   14.77
2025  1452   4.79    6.04    7.40
2026   848   1.79    2.55    3.54
```

**Net of cost, by holding period** (40 bps round trip amortised over the hold;
recent third is from 2024-10):

```
hold    gross   drag@40bps     net    recent gross   recent net
 15d    4.95%       9.73%    -4.78%        4.27%        -5.46%
 30d    5.95%       4.87%     1.08%        5.41%         0.55%
 45d    6.83%       3.24%     3.59%        4.88%         1.63%
 60d    5.80%       2.43%     3.37%        5.00%         2.56%
 90d    5.28%       1.62%     3.66%        4.45%         2.82%
```

**The term structure is flat, so no entry point rescues it.** The annualised
basis is 5-7% at every horizon; the only thing that varies is how badly the cost
amortises. The longest hold is the best one and it still nets **2.82%/yr in the
recent third against the 11.48%/yr the existing Hyperliquid funding carry
already earns** -- a shortfall of 8.7pp. Short holds are negative outright.

**And that is before the cash benchmark the dossier itself mandates.** The spot
leg gives up whatever stablecoin or T-bill yield it would have earned. Subtract
a 4-5%/yr benchmark and the net is at or below zero.

**Three structural facts the readiness ranking missed:**

1. **Two underlyings is not a cross-section.** 48 contracts resolve to BTC and
   ETH, with about four live at once. `harness.evaluate` floors at
   `MIN_ASSETS = 10`, so the project's measurement rig **cannot evaluate this
   family at all** -- it is a time-series carry wearing a cross-sectional name.
   The dossier's own H-A1.3 "cross-sectional basis harvest" is not constructible
   on this venue's data.
2. **It is not a second, uncorrelated harvest.** Section 8 of the same document
   says what would be interesting is "a second UNCORRELATED harvest." This is
   the same premium (compensation for leverage demand), on the same two assets,
   at the same venue, funded by the same collateral, as the strategy already
   running. A fixed maturity instead of an eight-hour reset is a different
   wrapper, not a different risk.
3. **The decay is the project's second governing fact, again.** 13.71% (2021) to
   2.55% (2026), and the newest expiries are the worst of all: ETHUSDT_260626
   at 1.60%/yr and ETHUSDT_260925 at 1.29%/yr. Finding 32 measured decay as
   elapsed time rather than volatility regime; this is that curve on a different
   instrument.

**Method and its limits.** Daily closes, not top-of-book. That is deliberately
optimistic -- a real entry buys spot at the ask and sells futures at the bid, so
executable basis is strictly below what is measured here. A family that fails at
the close fails harder at the touch. This is a screen of the same kind that
killed market making (Finding 30), not a registry hypothesis: no cross-sectional
statistic exists to compute, so **no cells were spent and the bar did not move.**

**Verdict: do not build the census.** The engineering the document requests
would produce a well-instrumented measurement of a number that is already known
to be a third of the incumbent and shrinking. If it is ever revisited, the
question worth asking is not "what is the basis" but "is dated basis minus perp
funding a spread worth trading" -- and both legs of that are on the same two
assets, so the capacity is whatever BTC and ETH alone will carry.

# Finding 41 — the dossier's remaining free gates, swept: two dead, one real but n=1

Dated 2026-08-09. Follows Finding 40. The external dossier's Queue B and C hold
candidates gated on named data checks rather than on economics. Three of those
gates need no subscription, no broker entitlement and no account, so they were
settled directly instead of being scheduled.

## 41.1 Deribit linear-USDC option boxes — DEAD on the live book

Round 7 rates this "YELLOW: paid-history or FORWARD -- run live/forward kill;
buy history only if wedge appears." That kill needs no history: a box's edge is
arithmetic on the current book.

A long box at K1 < K2 (long call K1, short put K1, short call K2, long put K2)
pays exactly K2 - K1 at expiry. Buying it is lending. Every leg is crossed --
ask on the longs, bid on the shorts -- and Deribit's per-leg fee is charged at
0.03% of the underlying, capped at 12.5% of premium, computed per leg because
the cap binds on the wings and not on the deep legs.

Best executable box, by underlying, across every expiry with two-sided quotes:

```
underlying   expiries   best gross   best net   vs 4.5% cash   vs incumbent
BTC_USDC            6       -1.42%     -1.51%        -6.01pp      -12.99pp
ETH_USDC            6       -0.92%     -1.02%        -5.52pp      -12.50pp
SOL_USDC            5       +1.12%     +0.09%        -4.41pp      -11.39pp
```

**Zero of 17 expiries lend above the cash benchmark, let alone the incumbent.**
The best box on the venue pays -1.5%/yr.

The term pattern is the project's first governing fact restated exactly. The
best rate is always the LONGEST expiry, because four crossed spreads are a fixed
cost amortised over the holding period:

```
BTC_USDC   3.4 days   -7,410%/yr
          11.4 days     -117%/yr
          18.4 days      -29%/yr
          46.4 days       -5.8%/yr
         137.4 days       -1.5%/yr
```

An edge must be LARGE or SLOW. A box is neither: its payoff is fixed by
construction, so the only lever is time, and the spread outruns it at every
horizon Deribit lists. **Do not buy Tardis history for this.** One snapshot
cannot prove wedges never appear, but the gap to clear is 13pp and the observed
best is 13pp the wrong way. That is not a near miss.

## 41.2 Fixed-ratio token migrations — the mechanism is real, the family is n=1

Round 7 asks for a 20-event executable-overlap audit, decisive condition
"converter-live + old-token-withdrawable overlap". Four migrations have both
legs in the Binance spot archive.

**Two of the four have NO overlap at all:**

```
MATIC -> POL   old ends 2024-09-10, new begins 2024-09-14   (+4 days)
FTM   -> S     old ends 2025-01-13, new begins 2025-01-17   (+4 days)
```

These were ticker renames in place. There is no window in which both legs are
buyable, so the trade the candidate describes cannot be put on. **Both are the
most recent migrations of this type**, which is the more important half of the
result: the structure is disappearing, not waiting to be harvested.

The ASI merger did leave a window. Deviation of the old token from its own
conversion value, negative meaning cheap:

```
                last 30d      last 14d      last 7d    |dev|>50bps (14d)
AGIX -> FET     -285 bps      -358 bps     -346 bps          100%
OCEAN -> FET    -206 bps      -302 bps     -294 bps          100%
```

It is not a data artifact. Quote volume over the final 20 days ran a median
$19.9M/day on AGIX and $8.1M/day on OCEAN against $65M/day on FET, so the prices
are executable at size. And **the implied ratio converges on the published one
as delisting approaches** -- AGIX runs 0.40976, 0.41837, 0.42298, 0.42935,
0.42914 into a published 0.433350. A wrong ratio would sit at a constant offset;
a monotone walk toward parity is a spread closing. The mechanism is legible:
a holder who wants out before delisting and does not want to run a withdrawal,
an on-chain conversion and a redeposit sells 3% below conversion value, and
that 3% is what the patient side collects.

**It is still not fundable, for a reason no amount of return fixes.** AGIX and
OCEAN are two legs of ONE merger, announced the same day and delisted the same
day. This is n=1, not n=2, and their correlation is 1. Trap 7 applies with full
force: n caps the evidence before the data is seen. Two of the four candidate
events had no tradeable window, so the forward event rate is worse than the
historical one.

Unverified and load-bearing if this is ever revisited: the conversion ratios were
not confirmed against a primary source in this session, and the converter-live
date is not established. The convergence above is strong internal evidence the
ratios are approximately right, and internal evidence is not a citation.

**Verdict: do not build the scanner. Log migrations as they are announced** --
the cost is a calendar entry, the events arrive a few times a year, and only a
forward sample can turn n=1 into a family. The first thing to check on each is
whether both legs trade at once, because twice in the last two years they did
not.

## 41.3 MVRV — measured, dead, and the last cross-sectional free source is closed

Coin Metrics Community, 125 assets exposing `CapMVRVCur`, 120 with at least 500
days, panel 3,142 x 120 from 2018-01-01, median 100 rankable names per day. The
best-conditioned cross-section this project has measured.

Each asset's MVRV z-scored against its OWN trailing 365 days (closed yesterday),
then ranked. Ranking raw MVRV across assets would compare bitcoin's realised-cap
structure to a two-year-old token's and measure holder-base age rather than
valuation. Score = -z: cheap against own history is a long, pre-registered.

```
horizon   gross %/yr      t     recent third %/yr      t    verdict
 14            -1.74   -0.09                18.02    0.72   fail
 30           -19.46   -1.14                 3.98    0.19   fail
 60           -17.38   -0.96                 6.65    0.37   fail
 90           -13.93   -0.75                10.53    0.43   fail
```

Largest |t| is 1.14 against a bar of 3.15, and no cell exceeds its own permuted
null. Four cells recorded; registry now 44 entries, 144 cells, bar 3.153.

**This panel is SURVIVORS ONLY** -- Coin Metrics covers what it follows today --
so the long leg is flattered by an unmeasurable amount. The caveat points toward
manufacturing an edge, and there was none to manufacture.

Finding 39's new warning fired correctly here on a real test: the null ranked a
median 85 names against the statistic's 101. A mild shrink rather than a
collapse, because this score covers most of the panel -- which is the behaviour
the fix was meant to expose.

## 41.4 What was deliberately not measured, and why

The dossier's other four untested free sources were skipped with reasons rather
than tested:

- **Coin Metrics exchange flows** (btc/eth) and **blockchain.info miner
  economics** (btc) -- one or two assets. A cross-section of two is not a
  cross-section, and `MIN_ASSETS` is 10. Both are also directional forecasts,
  the family that is 0-for-everything here.
- **Bitfinex margin size** -- positioning. Positioning already reached IC
  t -8.35 and earned zero; a different vendor for a dead family is not a new
  test.
- **DefiLlama stablecoin supply** -- a regime conditioner, not a signal. Its use
  is splitting an existing signal by regime, and Finding 32 measured the decay
  as elapsed time rather than regime.

Of the six sources the dossier listed as free, verified and untested, **five are
now closed**: quarterly futures (Finding 40), MVRV (above), and three retired on
structure. Only the Korea premium remains, and it is unexecutable for the same
reason it exists -- capital controls are what keep it open.

# Finding 42 — Bybit open interest: the depth claim is true, the delisted claim is false, and it fails silently

Dated 2026-08-09. The source inventory carries the line "Bybit /v5/market — open
interest, 5.9 years, delisted recoverable" under **free, verified, and USED**.
Half of it is right. The half that is wrong is the half that would decide whether
a cross-sectional open-interest study means anything.

**Depth: confirmed.** Paginating `/v5/market/open-interest` at `intervalTime=1d`
for BTCUSDT walks 11 pages to **2,197 daily rows reaching 2020-08-05** -- just
under six years, as claimed.

```
page  1  rows=200  oldest 2026-01-23
page  6  rows=200  oldest 2023-04-29
page 11  rows=197  oldest 2020-08-05      total 2,197
```

**Delisted recovery: false.** Every delisted symbol returns an empty list:

```
MATICUSDT   EMPTY   retCode=0  retMsg=OK
AGIXUSDT    EMPTY   retCode=0  retMsg=OK
FTMUSDT     EMPTY   retCode=0  retMsg=OK
LUNAUSDT    EMPTY   retCode=0  retMsg=OK
ETHUSDT     200 rows
SOLUSDT     200 rows
```

`/v5/market/instruments-info?category=linear` returns 800 instruments and
**every one carries `status: "Trading"`**. The venue's API exposes no delisted
state at all, and the successors are present where the predecessors are gone
(POLUSDT and SUSDT yes, MATICUSDT and FTMUSDT no). There is nothing to recover
because Bybit does not serve it.

**The failure mode is the one this project keeps meeting.** The request does not
404 and does not error. It returns `retCode: 0`, `retMsg: "OK"`, and an empty
list -- well-formed, plausible, and short. That is trap 6 again, a fifth
distinct instance: a caller that checks the status code and iterates the list
sees a symbol with no open interest rather than a symbol the venue will not
discuss.

**What this costs.** An open-interest panel built here is **survivors only**, and
the endpoint will never say so. Binance's archive is the opposite -- it keeps
delisted symbols and is what made Findings 38 and 40 possible -- so the two
sources are not interchangeable, and an OI study that ranks a cross-section
assembled from Bybit has a long leg selected on survival with no way to measure
the size of it from inside the panel.

**The 180-row open-interest gap is still worth closing**, and the honest way to
close it is `Survivorship.SURVIVORS_ONLY` stamped on the panel, or Binance's
archive `metrics` dataset instead, which `BinanceArchiveSource` already reaches
with `dataset="metrics"` and which does hold delisted names.

**And the inventory line should be corrected where it is read**, because it is
the kind of claim that gets acted on: an agent briefed off "delisted
recoverable" would build the survivorship-corrected study it names, get a clean
empty-free panel out of it, and never learn the universe had been quietly
filtered to whatever is still trading today.

# Finding 43 — the venue lever is exhausted: Hyperliquid is confirmed, paired, against five competitors

Dated 2026-08-09. The last untested lever on the only strategy that works.
Findings 23-24 established that funding is a parameter of each venue's contract
rather than a property of the asset, and that moving venue was worth +4.31%/yr --
the largest single improvement this project has found. That comparison used two
venues. Eight have verified keyless funding endpoints, so the question is whether
a third pays more still.

Method is Finding 24's, unchanged: **paired, same asset, same day, both venues
present**, each venue's own settlements summed over 180 days and annualised by
elapsed time. Cadence differs (Hyperliquid hourly, most others 8-hourly) and
that is fine -- summing one venue's own payments is not the venue-mixing
Finding 25 forbids.

**The unpaired ranking is wrong, and it is wrong in the direction that would have
cost money:**

```
unpaired, 180 days      paired vs hyperliquid
backpack      6.39%       -0.48pp   t  -1.30    3 of 8 assets
bitget        5.37%       -0.97pp   t  -2.87    4 of 8
hyperliquid   3.81%        baseline
okx           2.90%       -2.47pp   t -11.61    0 of 8
bybit         1.61%       -3.29pp   t -12.85    1 of 8
binance       1.10%       -2.71pp   t -17.49    0 of 8
```

Backpack and Bitget lead the unpaired column and **both lose on the pair**. The
gap is a listing-and-window artifact: venues differ in which assets they carry
and from when, so an unpaired mean ranks the most generous LISTING policy. This
is Finding 24's lesson reproduced independently on six venues, and it is the
whole reason the paired design is not optional.

**No venue beats Hyperliquid.** Backpack is a statistical tie (t -1.30, not
significant at any bar this project uses, 3 of 8 assets). Bitget is worse by
about a point. The three large CEXs lose by 2.5-3.3pp with |t| between 11.6 and
17.5, on 0, 0 and 1 of 8 assets respectively.

Gate and Paradex returned nothing under the symbol templates used here. That is
**not evidence they pay less** -- it is a symbol-format miss on this pass, and
saying otherwise would be the unpaired error in a different costume. Two venues
remain genuinely unmeasured.

**What this closes.** The rate is not improvable by venue selection: the best
available venue is the one already chosen, and it was chosen before this was
measured. Combined with Findings 38, 40 and 41 -- momentum dead in the tail,
dated basis a third of the incumbent, boxes negative, MVRV dead -- the honest
position is that **the rate is fixed by market structure at roughly what the
book already earns, and the base is the only free variable left.**

That was the closing sentence of the previous session, offered as a suspicion.
It has now been tested on the last lever that could have overturned it.

One number worth not over-reading: the Hyperliquid-over-Binance edge measures
+2.71pp here against +4.31pp in Finding 24. The windows differ, so this is not a
clean decay estimate and must not be quoted as one. It is consistent with
Finding 32's direction and nothing stronger.

# Finding 44 — the measured universe and the executable universe are not the same set

Dated 2026-08-10. Found while wiring the first live cycle, and it is the reason
that cycle did not run.

**The selector cannot see a spread.** `select_carry_basket` scores gross funding,
and `conviction` may not import `venue` -- a correct package boundary with an
incorrect consequence, because on a venue whose richest-funding names are also
its thinnest, the ranking it produces and the ranking that pays disagree.

Modelled round trip at $70 a leg, measured live against Hyperliquid:

```
        gross funding    round trip    six-week gross    net %/yr
SOL          8.41 %/yr      28.3 bps          97 bps        6.9
BTC          3.90 %/yr      28.4 bps          45 bps        1.4
ETH          7.46 %/yr      29.0 bps          86 bps        4.9
HYPE         6.08 %/yr      30.3 bps          70 bps        3.4
PENGU        5.5  %/yr      49.7 bps          63 bps        1.1
PURR        12.2  %/yr     212.8 bps         140 bps       -6.3
```

**PURR ranks first on the number the selector reads and loses money on the
number that pays.** Its spread was measured at 60.7 bps, then 40.5, then 212.8
within ninety minutes; the name is not tradeable at this size at any moment one
happens to look. The 28 bps floor on the liquid four is almost entirely fee --
four legs at ccxt's 7 bps default tier -- so the spread contributes nothing
there, which is exactly the separation the filter is for.

## The consequence, which is the finding

The +11.48%/yr that justifies this book was measured over **six** names. Two of
them cannot be executed. So the number was measured on a universe the operator
does not have, and the honest question is what the strategy earns on the set
that can actually be traded.

Simulated on 27,877 hourly Hyperliquid settlements, 2023-05 to 2026-08, top-2 by
trailing funding held six weeks, costs charged at the measured round trips:

```
universe                enter   costs   periods    %/yr      t
six (as measured)           2    net         28   16.73%   4.37
four (executable)           2    net         28   14.58%   5.93
```

**Removing the untradeable names costs 2.1pp of return and improves the
t-statistic from 4.37 to 5.93.** The names that could not be executed were also
the volatile ones, so the book gives up some carry and a great deal of variance.
The strategy survives its own correction.

Held counts show why: over the six-name universe the book sat in PURR for 13 of
28 periods -- the single most-held name was the one that cannot be traded.

**Limits, stated because the absolute numbers will otherwise be quoted.** This
averages realised funding across held names rather than running the full book
with marks, so it is not comparable to the paper gate's 11.48% and only the
six-versus-four contrast is a valid inference. It models full rebalancing with no
`exit_rank` hysteresis, which the live loop has and which should if anything
help. n = 28 periods, and HYPE/PENGU/PURR only exist from late 2024.

## What it changes

`enter_rank=2, exit_rank=3` satisfies the selector's own floor of
`max(exit_rank + 1, enter_rank * 2)` on a four-name universe. The shipped
default of `exit_rank=4` requires five, which is why the first live cycle
abstained with `universe_too_small` rather than trading -- the selector refusing
a degenerate cross-section, working exactly as written.

Three independent guards refused to trade tonight, each for a different reason:
the execution filter dropped PURR, the selector then abstained on the remainder,
and the reconciler passed so neither was masked by a book that disagreed with the
venue. None of them needed anyone to be watching.

# Finding 45 — the Korea premium is dead, and the free-data surface is now closed

Dated 2026-08-10. The last of the six sources the dossier listed as free,
verified and NOT YET TESTED. I had closed it earlier in the session on reasoning
-- an outsider cannot arbitrage the kimchi premium, because capital controls are
what create it -- and that reasoning answers whether the premium can be
CAPTURED, not whether it PREDICTS. Those are different questions and only one of
them was asked.

**Construction.** `premium = upbit_krw_close / binance_usd_close`, z-scored
against each coin's own trailing 90 days closed yesterday, then ranked
cross-sectionally. Raw premium would rank structural listing and liquidity
differences -- some coins simply trade dearer in Seoul, permanently -- while the
question is which coin is unusually dear today.

**No FX series is needed**, which is what made this free. KRW/USD multiplies every
coin identically and cancels in a cross-sectional rank.

234 assets listed on both venues, 170 with at least 400 days, median 92 rankable
names per day, 2023-04-29 to 2026-08-10.

**Sign pre-registered as +z**: a high premium means Korean buyers are paying up,
which is either demand spilling into the global book or a global price yet to
catch up. Both readings are continuation, so there was no coin-flip to hide
behind.

```
horizon   gross %/yr       t     recent third %/yr       t    verdict
   3          -23.05    -1.19              -10.84    -0.46    fail
   7          +36.20    +1.74              -25.77    -0.99    fail
  14          +13.28    +0.62              -14.47    -0.67    fail
  30          +13.50    +0.60              -17.44    -0.76    fail
```

Largest |t| is 1.74 against a bar of 3.16. No cell exceeds its own permuted
null. Every recent third is negative.

**Two guards earned their keep here.**

At h=3 the rank IC is significant at t -3.52 while the portfolio is flat. The
premium genuinely orders the typical asset and earns nothing, because the tail
sits in the other decile -- trap 4, and the exact shape that made positioning
data look like a discovery at IC t -8.35 while paying zero. Reporting the IC as
the result would have looked like a finding.

At h=7 the full sample reads +36.20%/yr and the middle third +90.92% at t 2.15,
against a recent third of -25.77%. That is the rule that killed five of six
candidates in the previous session, catching a sixth.

**What this closes.** All six of the dossier's free-and-untested sources are now
measured: quarterly futures (Finding 40), MVRV (Finding 41), three retired on
structure in Finding 41.4, and this. **The free-data surface is exhausted.**
Everything remaining needs a paid feed, a broker entitlement, or elapsed time --
none of which is a research question.

**And a correction to my own method.** Three of those six were closed on
structural reasoning rather than measurement. The reasoning was sound in each
case, and this one shows what it is worth: it took an hour to convert an
argument into a number, and the number is what belongs in an append-only file.
Reasoning narrows a search. It does not close a question.

Registry: 45 entries, 148 cells, bar 3.161.

# Finding 46 — the carry premium has decayed to a third of what justified the book

Dated 2026-08-10, by the health monitor on the day it was built, before the
first live cycle.

Finding 44 justified this book at **14.58%/yr net** on the four executable
names, measured over 28 six-week periods from 2023-05 to 2026-08. The monitor
compares what the universe pays NOW against that. It reads **degraded**, and it
reads degraded at every lookback:

```
lookback   gross %/yr   net %/yr   verdict
      7d         7.97       5.37   degraded
     14d         6.95       4.34   below floor
     30d         8.50       5.89   degraded
     45d         8.51       5.90   degraded
```

Consistency across windows rules out a quiet week. The quarterly history rules
out a regime dip:

```
2023-12   36.09      2025-03   19.79
2024-03   40.48      2025-06   14.47
2024-06   19.38      2025-09   16.19
2024-09   10.89      2025-12   10.85
2024-12   41.77      2026-03    7.15
                     2026-06    6.72
                     2026-09    9.59
```

**This is Finding 32's decay, arrived.** That finding measured the premium
falling with elapsed time rather than volatility regime -- volatility's
coefficient went to zero at t -0.11 while the time trend held at t -4.24 -- and
concluded it was crowding, which does not reverse. Two years later the top-2
basket pays a quarter of its 2024 peak.

**The justification and the present are not the same number.** 14.58%/yr was an
average over a window containing two quarters above 40%. Quoting it as the
book's expected return today would be quoting a regime that has ended.

## What it changes

At ~5.90%/yr net against ~4.50% for idle stablecoin, the book earns **1.4pp**
for taking exchange failure, perp liquidation and basis divergence risk. On
$211 that is about $3 a year of edge over doing nothing.

- **As an investment it no longer clears its own risk.** Funding this at 14.58%
  was defensible; funding it at 5.90% with the trend down is not, and scaling it
  would be worse.
- **As an execution test it still is.** Cycle one exists to prove the path with
  real money while the stakes are trivial, and $12 against $30 a year does not
  change that. Every defect found on 2026-08-09/10 would have cost more than
  either figure.

**Recommendation: run cycle one, do not scale the book**, and make any decision
about real capital against 5.90% rather than 14.58%.

## The monitor's own limits, stated

It compares a trailing rate to a multi-year average, which are different
timescales; a single 7-day reading flipping between healthy and degraded would
be noise, not signal. It is trustworthy here only because every window agrees
and the quarterly series corroborates. A future reader should check the windows
against each other before acting on one verdict.

It also assesses the EXECUTABLE universe. The first wiring passed all six listed
names, ranked PURR first at 12.3%/yr and reported HEALTHY -- Finding 44's error
committed inside the monitor built to catch it. On the four that survive the
execution filter, the same instant reads degraded.

# Finding 47 — trading faster is arithmetically harder, measured on 1-minute bars

Dated 2026-08-10. Recorded because "an experienced trader stacks several percent
a day" is the most persistent objection to every negative result in this file,
and it deserves a number rather than an argument.

87,840 one-minute BTCUSDT bars from the Binance archive, 2026-06-01 to
2026-07-31. Break-even accuracy is `0.5 * (1 + cost / E|move|)` -- the
directional hit rate at which expected profit equals expected cost.

```
horizon    mean |move|    break-even @ 9 bps    @ 20 bps
 1 min        0.0399%           impossible     impossible
 5 min        0.0880%           impossible     impossible
15 min        0.1523%                79.5%     impossible
 1 hour       0.3088%                64.6%         82.4%
 4 hour       0.6065%                57.4%         66.5%
 1 day        1.5710%                52.9%         56.4%
```

**The requirement rises as the horizon shortens.** At one minute the average
move is 4 bps against a 9 bps round trip: the position is under water before it
is a position, and no hit rate recovers it -- "impossible" above means break-even
exceeds 100%.

The mechanism is that **cost is fixed per trade while the move scales with the
square root of time.** Halve the holding period and the move falls ~30% while the
cost is unchanged, so cost-to-move worsens as 1/sqrt(t). More trades is not more
opportunity; it is the same opportunity divided by more fixed charges.

Measured directional accuracy in this project is 47-51%. The best documented
systematic records sit near 50-55%. Nothing at 64-100% exists.

**And the compounding forecloses it independently.** 2%/day is 155x a year; 1%/day
is 12.3x. A repeatable per-day edge of that size owns the market inside a decade,
which is the reduction that settles it without any measurement at all.

## What this does NOT close

Two things, stated because the arithmetic above is often over-applied:

1. **Conditional entry is untested here.** Every one of the ~400 hypotheses is
   ALWAYS-ON and cross-sectional: hold the top quintile, rebalance forever. A
   trader who waits for a setup and then sizes up is a different shape, and the
   break-even table applies to the trades taken rather than to every bar. The
   harness has never measured it.
2. **Leverage on a directional edge is untested.** The carry work capped out
   because a cash-and-carry cannot be levered past its notional yield -- the spot
   leg is fully funded. A directional position has no such ceiling.

The region where the arithmetic is survivable is therefore **fewer, larger,
conditional bets at daily-or-longer horizons, levered** -- accuracy bar 53-56%
rather than 65-100%. That is also the shape of the operator's own 11.7x over 18
months, which annualises to ~410%/yr, or ~0.65%/day: real, and achieved through
concentration and leverage in a rising market rather than a repeatable daily
edge.

# Finding 48 — conditional entry, first gate: it made a dead signal worse

Dated 2026-08-10. The first measurement of a shape this project had never tested.

Every one of the ~400 hypotheses here is ALWAYS-ON: hold the top quintile,
rebalance forever, trade every period the calendar offers. A discretionary
trader waits for a setup and then sizes up, and Finding 47 established that
daily-or-longer horizons are the one region where the cost arithmetic does not
foreclose the answer. So the harness gained the ability to report selectivity,
and the shape was tested.

**The signal is momentum, chosen because it is the most thoroughly dead thing in
this file** -- Finding 38 killed it in the liquid head and the illiquid tail on
one afternoon. If waiting rescues that, the technique generalises; if it does
not, the negative is worth more than rescuing something nobody had tested.

**The gate is cross-sectional dispersion**, pre-registered with a mechanism: when
every asset moves together there is nothing to rank and a long-short book trades
the market factor with extra steps. Trade only when the interquartile range of
the momentum score sits in the top tercile of its own trailing year, measured on
data closed yesterday.

```
                       h=14 gross      t     h=30 gross      t   traded/offered
always on                  +43.80   0.74        +33.25   0.61     167/171, 78/80
gated on dispersion        +12.77   0.17        -48.03  -1.05      57/171, 23/80
```

**Waiting made it worse at both horizons**, turning +33.25%/yr into -48.03% at
h=30 while abstaining on 71% of rebalances. Neither version comes near the 3.17
bar in any case.

## What this does and does not establish

It does NOT kill conditional entry. **One gate was tested, not the technique.**
The space of possible conditions is enormous, which is precisely the danger --
searching it until something passes is a multiplicity nobody counts.

That danger is now named in the harness. `Verdict.selectivity` reports the
abstention rate, and a new guard warns when a strategy trades under half the
rebalances offered:

> the choice of WHEN to trade is a search that no multiplicity correction here
> counts -- treat this |t| as weaker than the same |t| from an always-on strategy

That warning is the durable output of this finding. A future conditional result
that looks strong will carry it, and a reader will know the |t| is worth less
than its face value.

## A limitation that has now bitten twice

**None of these four cells are calibrated.** The permutation null could not be
measured on any of them, for Finding 39's reason: the liquid head is 30 names of
an 832-column panel, so the null's cross-section collapses to roughly n^2/N and
no draw clears `MIN_PERIODS`. Finding 39 chose to state the absence rather than
redesign the null, on the grounds that every available repair trades sample size
against block autocorrelation and makes the guard MORE permissive.

That choice is now costing real coverage: any test on a restricted universe --
which is what a liquidity screen, a sector, a regime or a gate all produce -- is
uncalibrated. It is the highest-value open item in the harness, and it should be
settled deliberately rather than in passing.

# Finding 49 — the permutation null is fixed, and the defect was wider than Finding 39 said

Dated 2026-08-10. Finding 39 found the null's cross-section collapsing to
roughly n^2/N and chose to STATE the absence rather than repair it, on the
grounds that every available repair traded sample size against block
autocorrelation and the sample-preserving version made the guard more
permissive. That was the right call to make in passing and the wrong one to
leave standing: it bit twice more (Findings 41, 48), and it blocks exactly the
region worth exploring -- liquidity screens, regimes, sectors, gates all produce
a restricted universe.

## The repair

The null must destroy the score-to-asset association while preserving everything
else: the per-date universe, the cross-sectional size, the market factor, and
the persistence that makes holdings sticky.

`_relabel` draws one fixed random ranking of columns per draw, and at every date
re-sorts that row's scored positions along it, reassigning the values. So:

- **membership is exact** -- targets come from the row's own scored positions, so
  the null ranks the same names on the same dates as the statistic;
- **persistence survives** -- the ranking is fixed across dates, so a score that
  was sticky on one asset is sticky on its replacement and the null's holdings
  turn over at the strategy's rate;
- **the market factor survives** -- prices are never touched.

What it gives up is one global permutation's block structure, where an asset
inherited another's entire history. Persistence is now reproduced by the fixed
rank rather than inherited. That is the trade, and it is the one that buys back
the sample.

## A/B against the old design, same data, same seed

```
case                    old p95   old n    new p95   new n   statistic n
full panel                 1.95      43       2.10     181           181
tail (~138 of 832)         2.11      32       2.03     157           157
head (30 of 832)            nan       1       1.97      30            30
```

**The new sample matches the statistic exactly in all three cases.** The head
gained a guard it never had.

**And the defect was not confined to restricted universes.** On the "full panel"
the old null ranked 43 names against the statistic's 181 -- because a momentum
score is NaN for its first 30 days and for every delisted name, so `available`
was never all-True and n^2/N applied there too (181^2/832 = 39, against the 43
observed). **Every permutation null this project has ever computed was measured
on a shrunken sample.**

**The fix is not weaker**, which was the risk that would have made it a bad
trade: the full panel got STRICTER (1.95 to 2.10), the tail moved 4% looser, and
the head went from absent to present.

## What it does not change

No recorded verdict flips. Every past |t| that faced a null was far below both
the old and the new p95 -- R1 at 1.46, MVRV at 1.14, the Korea premium at 1.74,
against new nulls near 2.0. The conclusions in Findings 38, 41 and 45 stand, and
Finding 48's four cells are now calibrated rather than blank: nulls of 1.85 to
2.25, every cell still failing against its own.

The module docstring's "2.2-2.5" is corrected to 2.0-2.3: the higher figure was
measured on the shrunken samples. `registry.bar()` floors at 2.5 regardless, so
nothing downstream moves.

## The lesson worth keeping

Finding 39 named the defect precisely, chose not to fix it, and gave a good
reason. The reason was sound and the decision still cost three findings' worth of
uncalibrated cells. **A guard that is known to be broken and left in place is
worse than one nobody has checked, because its silence is now trusted.**

# Finding 50 — the carry decay is not forecastable with a linear model, and an automated exit on it would have exited at the trough

Dated 2026-08-10. Not a registry cell: this is robustness/monitoring of the one
thing that works, not a directional hypothesis. `Registry.record` is not called.

Finding 32 measured the top-quintile funding premium decaying linearly with
elapsed time (−0.80 pp/month, t −4.24, volatility's coefficient → 0) on 78
monthly observations, 2020-02 to 2026-07. The question this answers: is that
trend stable enough on THIS book's actual basket — top-2 of the executable four
on Hyperliquid — to forecast the floor crossover, with the project's hard rule
that the recent third settles it.

The series was reconstructed point-in-time at weekly cadence (87 snapshots,
2024-12-15 → 2026-08-09, trailing 7-day funding, annualised), via the same
`carry_health.assess` the live monitor uses.

```
train fit (oldest 67%, n=58):   premium = 35.54 + (-2.372) * months    r^2 0.338
holdout (recent third, n=29):
   mean actual       7.22%
   mean predicted   -3.75%
   bias            +10.97 pp    (premium beat the trend by 11 points)
   rmse             12.76 pp
   verdict          REGIME BREAK
```

The linear model fit on the early window fails the recent third by eleven
percentage points. The early slope (−2.37 pp/month) is nearly three times
steeper than Finding 32's −0.80, and the reason is visible in the series: it is
dominated by HYPE's launch normalisation (78% at inception, December 2024,
collapsing toward the pack). That is a one-time listing effect, not steady-state
crowding decay, and once it worked through the premium stopped declining.

The full-series fit (slope −1.53, r² 0.383) "predicts" a floor crossover at
2026-04-13. Reality refuted it: the premium dipped to 1.0% on 2026-04-19 and
−0.04% on 2026-03-15, then bounced to 15.81% on 2026-06-07 and has held 5-11%
since. **An automated exit built on the linear trend would have fired at the
March-April trough and exited immediately before the bounce.** That is the worst
possible exit — it sells the book at the bottom of a noise swing wearing the
costume of a trend.

Two things to keep separate:

- The long-term drift is real. Finding 32 measured it on an independent,
  broader sample and it holds there. The premium is lower now than it was two
  years ago; that is not in dispute.
- It is not forecastable at the resolution an exit decision needs. The noise
  band (holdout RMSE 12.76 pp) is roughly twice the distance from the current
  premium to the floor. A model whose prediction error exceeds the quantity it
  is trying to bound cannot drive a timing decision, and forcing it to is how a
  real edge gets closed at its worst moment.

**This validates `carry_health`'s design.** The monitor reports a level and a
threshold and does not trade, halt or size. An actuating exit on the same data
would be a second controller disagreeing with the cycle's own guards on the
basis of a forecast that does not generalise. The right shape stays: a human
reads the monitor and decides; the code does not.

Door A of `RESEARCH_AGENDA.md` is closed. The decay-forecast was the cheapest of
the three open doors and the one whose underlying relationship was already
measured, and it does not yield a usable signal. That raises the prior against
the remaining two (on-chain flow, equities slow factors) modestly, in the way
that any honest negative result does.

# Finding 51 — Door B ran end-to-end but is gated on equity price depth, which is the project's one non-free edge

Dated 2026-08-10. Door B (book-to-market value factor, the Fama-French HML
construction chosen over earnings-yield because StockholdersEquity is a clean
point-in-time stock while quarterly NetIncomeLoss is cumulative YTD).

**The fundamentals wall is built.** A bulk EDGAR ingest
(`ops/ingest_edgar_bulk.py`) ran on prod for all 503 seeded companies: **715,701
fundamental claims across 500 companies, 2005-12-31 to 2026-07-04** (20 years).
This is the deepest data in the store -- deeper than the crypto price spine.

**The factor test ran correctly and underpowered.** `ops/equity_value_factor.py`
built a PIT-aware book-to-market panel via `merge_asof` on `knowledge_date`
(the filing date, not the fiscal period end -- the lookahead Finding 32 bled
on): 83,682 finite B/M cells across 216 companies with both fundamentals and
prices, through `evaluate()` at the true 3.18 bar. Verdict: **7 non-overlapping
periods, below the 20-period floor.** No statistic reported, because the window
cannot answer the question.

**The wall is price depth, not fundamentals.** Equity prices span only
2024-08-05 to 2026-08-07 (~2 years, 216 companies). The fundamentals span 20.
At a 63-day quarterly horizon, 2 years gives ~8 periods; the harness floor is
20. A quarterly factor needs ~5+ years of prices.

**The 2-year cap is Polygon's free tier, confirmed by probe.** Requesting 10
years of AAPL daily aggregates from Polygon with the configured free key
returned 499 rows starting exactly 2024-08-12 -- a hard cap, not the adapter's
`days=730` default (that default matches the cap, so it was never noticed).
`polygon.py:157`'s lookback is a code constant; the limit behind it is the
plan.

This is the project's **one genuinely non-free edge.** Every other data source
is free and wired: EDGAR (public), ccxt/Hyperliquid (free), Etherscan (free
tier, 5 calls/sec, backfill code ready). Deep equity price history is the
single input where the wired free source stops at 2 years. Deeper history needs
either a paid Polygon plan, or a free alternative not currently wired --
`yfinance` (free, ~decades, but Yahoo's terms are gray for a redistribution-
aware system and it carries no licence classification in the credential
catalog) or Alpha Vantage's free tier (~20 years but 25 requests/day, so the
216-company backfill is ~9 days of wall-clock).

**Door B is not dead; it is parked on a data decision.** The signal, the PIT
join and the harness path are proven on the 2-year sample. The moment a deeper
price source is wired, the same script re-runs unchanged and produces a real
verdict. The decision is whether deep equity history is worth a Polygon
subscription or a `yfinance` adapter, measured against Door B's already-low
prior (the most-crowded factor family in finance).

# Finding 52 — fast signals on Hyperliquid: nothing clears, and the one that comes closest is survivors-only

Dated 2026-08-12. Prompted by an operator question rather than the research
agenda: a trader claimed an automated system caught a 20x on PURR and a 2x on
FROG, and asked why this system did not fire.

**Three facts settled before measuring anything.** PURR's entire trough-to-peak
over 120 days is 2.50x against a window return of -20.9%; CASHCAT is 5.55x peak
against -15.5%. A 20x on PURR therefore requires roughly 8x leverage on a
perfectly timed round trip, which is a different claim from detection. PURR is
already in the six-name carry basket.

**FRONG is on a different chain, and that is the load-bearing fact.** Neither
FRONG nor FROG appears among Hyperliquid's 754 bases (nothing containing FRO,
FRG or RONG; the venue lists plenty of memecoins -- FARTCOIN, PEPE, WIF, BONK,
POPCAT, MOG, BRETT, GOAT, PNUT -- so this is not a category gap).

FRONG trades on **Robinhood Chain** at `0x6245e67affa44a23077f0ea7f981a8dc743a0c47`.
Measured 2026-08-12: 13 days old, $5.28M FDV, 13,442 holders, $5.56M 24h volume,
**-31.58% over seven days** against +28.03% on the day. The round trip this
finding describes is visible in the token's own week.

This system structurally cannot see it, and not for signal reasons: the stack
has ccxt (CEX) and an Ethereum-mainnet Etherscan adapter, and Robinhood Chain is
neither. There is no perp, so no funding and no second leg -- the instrument is
an outright directional long, which is the opposite of the one edge that pays.
And a $5.28M FDV cannot absorb size, which is the same capacity ceiling that
bounds everything else here rather than an escape from it. Reaching it would
mean a new chain pipeline, DEX routing and slippage modelling, a directional
risk framework, and unhedged custody. That is a different product, not a
missing feature.

**The depth trap, hit again.** ccxt's default `limit` returns 500 daily bars.
BTC returns 2,186 at `limit=5000`. This is Finding 5 exactly, on a second venue:
a measurement taken at the default would have been a fact about an argument.
Listing dates survive the deeper pull unchanged (PURR still starts 2024-11-04
reaching back to 2023), so they are real rather than a paging artefact — which
is what made the listing study possible.

**The panel.** 219 of 485 perps returned history; the rest rate-limited. 188
with >= 90 bars, of which **41 were tokenized equities, FX and commodities**
(XYZ-AMD, XYZ-COIN, XYZ-GME, XYZ-EUR, XYZ-BRENTOIL). Those were removed: they do
not share a return process with crypto, and leaving them in calibrates the
permutation null on a population that is not a tradeable universe. Final panel
147 crypto perps x 2,186 sessions, 2020-08-19 to 2026-08-13.

Removing them made the distribution **worse**, not better — they were propping
it up.

**The distribution, which is the actual answer.**

```
                          crypto only        with equities mixed in
up over window            38/147  (26%)      55/188  (29%)
median buy-and-hold          -47.1%              -28.2%
ran >=2x trough->peak           104                 118
  now below their start   88/104  (85%)      95/118  (81%)
  median dd from peak          -93.9%              -91.9%
  median buy-and-hold          -83.4%              -79.1%
```

104 names did what PURR and CASHCAT did. 88 are below where they started. The
median member of that group is down 93.9% from its peak. PURR and CASHCAT are
not the ones that got away; they are the modal outcome.

**The tests.** All signals shifted one bar, since ccxt stamps a bar with its
open (Finding 6). 20 bps round trip. Twelve cells, all recorded.

```
                        gross t    net t   recent-third t    bar   verdict
hl.momentum.1d (mixed)      --       --          +1.43      3.18   fail
hl.crypto.momentum.2d h=2  +2.74    +2.16        +1.20      3.19   fail
hl.crypto.momentum.3d h=1  +2.53    +1.67        +0.76      3.20   fail
hl.crypto.momentum.3d h=3  +2.07    +1.72        +2.14      3.20   fail
hl.crypto.volume.breakout  +0.76    +0.51        +2.04      3.20   fail
```

The gross figures are seductive — +90.59%/yr at t +2.74 on 2-day momentum at
h=2 — and they die in the recent third. That is the pattern every retired
strategy here has shown.

**Listing drift is the largest effect and must not be promoted.**

```
h=1    n=142   mean excess  +4.52%   median +0.03%   t +2.47
h=3    n=142   mean excess  +1.60%   median +0.76%   t +0.86
h=7    n=142   mean excess  +7.09%   median +0.32%   t +1.84
h=14   n=142   mean excess  +9.70%   median +2.51%   t +1.48
h=30   n=142   mean excess +12.43%   median +4.93%   t +2.50
```

Excess is over the contemporaneous equal-weight universe, so it is drift rather
than beta. It fails the 3.205 bar. Three reasons not to revisit it on this data:

1. **Mean +4.52% against median +0.03% at h=1.** The typical new listing does
   nothing. The mean is a few enormous winners. Capturing it means hitting the
   tail, which is the same lottery the distribution above describes.
2. **The sample is survivors-only.** The 142 listings are names still listed.
   A token that launched, died and was delisted is absent — and that is the
   outcome most correlated with being a new listing. The bias runs upward and
   cannot be sized from this endpoint. Same class as Finding 42 on Bybit.
3. Entry assumes a fill at the first daily close of a brand-new listing, which
   is where the book is thinnest.

**What would change it:** a listing feed that includes delisted tokens with
their terminal returns. Without that, this measurement cannot be made honest,
and its most favourable number is the one most contaminated.

**Verdict: no change.** The system did not miss these moves; it declines to take
directional positions in a universe where 85% of the doublers round-trip, and
the closest thing to a signal is tail-driven on a biased sample. Registry 50 ->
53 tests, 158 -> 170 cells, bar 3.182 -> 3.205.

# Finding 53 — reversal on Hyperliquid fails, and 8 of its 16 cells should not have been recorded

Dated 2026-08-12. The operator asked the simplest possible version of the
question: is there a way to ride the ups and downs automatically? Everything in
Finding 52 bought what went UP. This tests the opposite.

**The answer is no, and the shape of the no is useful.**

```
hl.crypto.stretch.20d (fade whatever is furthest from its 20d mean)
  h=1   gross -84.46%/yr  t -2.41  | net -99.90%/yr  t -2.84  | recent third -1.14
  h=2   gross -83.76%/yr  t -2.50  | net -94.39%/yr  t -2.82  | recent third -0.98
  h=3   gross -89.03%/yr  t -2.42  | net -97.58%/yr  t -2.65  | recent third -1.44
  h=7   gross -89.52%/yr  t -2.29  | net -94.62%/yr  t -2.42  | recent third -1.34
```

Fading the stretched loses at every horizon, consistently, at |t| ~2.4. So these
moves do not mean-revert. Combined with Finding 52 -- where momentum did not
clear either -- there is no simple side of this trade to be on. The swings are
neither reliably continuing nor reliably reverting at 1-7 days.

**A prior stated in advance and falsified.** The expectation, from Finding 38's
negatively-significant tail rank IC, was that reversal would order the
cross-section correctly and earn nothing (trap 4). It did not order it
correctly; it ordered it backwards.

## The methodological error, recorded because the registry cannot be edited

**`hl.crypto.reversal.3d` is the EXACT NEGATION of `hl.crypto.momentum.3d`.**
Verified to machine precision: the two signal matrices sum to zero everywhere.
A long-short quintile portfolio built on `-X` is arithmetically the inverse of
the one built on `X`, so this re-ran an existing test and recorded it as new.
The symmetry is visible in the output -- momentum.3d h=1 gross +87.48%/yr
t +2.53, reversal.3d h=1 gross -87.48%/yr t -2.53.

**`hl.crypto.violent_fade` never produced a measurement.** Scoring only names
whose 1-day move exceeded 20% leaves a MEDIAN OF ZERO qualifying names per day;
just 10 of 2,186 days had 10 or more. There is no cross-section to rank, so the
harness returned 0.00 for every cell. That is `cannot_answer`, not `fail`, and
it was recorded as four cells of fail.

**Consequence.** 8 of the 16 cells recorded on this date carry no information,
and the registry is append-only by design. The bar moved 3.205 -> 3.233 partly
on a duplicate and a non-measurement, so every subsequent hypothesis faces a
threshold slightly stricter than the evidence supports. Erring strict is the
safe direction, but it is still wrong.

**The rule this should have followed, for whoever tests next:**

1. Before recording, ask whether the signal is an affine transform of one
   already tested. Negation, and any strictly monotone rescaling, produce the
   same ranking and therefore the same (or exactly inverted) portfolio. It is
   not a new test.
2. A signal that leaves too few names to form the ranking buckets has not been
   measured. Check the per-period count of scorable names BEFORE calling
   `evaluate`, and report `cannot_answer` rather than recording cells.

Neither check exists in the harness. Adding a scorable-count floor to
`evaluate()` -- refusing rather than returning zeros -- would have caught the
second one automatically, and is worth doing.
