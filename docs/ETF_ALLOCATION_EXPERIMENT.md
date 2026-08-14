# ETF allocation — exploratory result

Run date: 2026-08-13 (America/Vancouver), on production data.

## Question

`docs/ETF_PORTFOLIO_EXPERIMENT.md` asked whether picking *constituents* inside a
sector beats the sector ETF, and answered no. This asks a different question:
given the eleven SPDR sector ETFs, does any way of **weighting** them beat simply
holding SPY?

## Design

Three rules, three rebalancing cadences, against SPY bought once and held. Every
path pays 2 basis points per unit of turnover, charged before the following
session's return — the same decision rule the constituent experiment used, so
the two sets of numbers can be read side by side.

- **equal_weight** — 1/11 across every sector.
- **top_measured** — the three highest `price_quality_scores`, equally weighted.
  Same scoring function the constituent experiment ranked with, reused rather
  than restated.
- **risk_balanced** — inverse annualised volatility over 63 sessions. Not risk
  parity: it ignores correlation, so contributions are only equal when the names
  are equally correlated, which sector ETFs are not.

Cadences: `static` decides once and never again; `quarterly` restates every 63
sessions; `threshold` restates only when drift from target reaches 0.10.

Panel: 2024-08-05 through 2026-08-12, 507 sessions, 11 sector ETFs plus SPY,
from one audience's Polygon claims. A 127-session warmup leaves 380 scored
sessions.

## Result

```
book            cadence         CAGR     vol  Sharpe    maxDD   turn  rebal   exCAGR  exSharpe
SPY buy & hold  static        17.46%  17.80%    1.03  -19.00%   1.00      1        —         —
equal_weight    static        12.37%  13.68%    0.95  -15.35%   1.00      1   -5.09%     -0.08
equal_weight    quarterly     12.58%  13.69%    0.97  -15.35%   1.15      7   -4.88%     -0.06
equal_weight    threshold     12.44%  13.68%    0.96  -15.35%   1.05      2   -5.02%     -0.07
top_measured    static         4.53%  17.79%    0.37  -18.58%   1.00      1  -12.93%     -0.66
top_measured    quarterly      5.86%  16.67%    0.46  -18.58%   4.69      7  -11.60%     -0.57
top_measured    threshold      4.53%  17.79%    0.37  -18.58%   1.00      1  -12.93%     -0.66
risk_balanced   static        11.60%  13.34%    0.92  -14.68%   1.00      1   -5.86%     -0.11
risk_balanced   quarterly     10.98%  13.38%    0.88  -14.68%   1.37      7   -6.48%     -0.15
risk_balanced   threshold     11.86%  13.36%    0.94  -14.68%   1.11      2   -5.60%     -0.09
```

**0 of 9 rule/cadence combinations beat SPY buy-and-hold on CAGR after costs.**
None beat it on Sharpe either.

## Interpretation

**The broad index wins, and it wins on the risk-adjusted number too.** The
standing preference that "ETFs are the preferred portfolio core" survives, and
gets sharper: it means the *broad* ETF, not a scheme for weighting sector ETFs.

**The two diversifying rules buy a real drawdown improvement and pay for it.**
Equal weight and risk-balanced both cut maximum drawdown by 3.7 to 4.3 points
and volatility by roughly 4 points, at a cost of about 5 points of CAGR. Sharpe
says the trade is not free: 0.88–0.97 against SPY's 1.03. Someone who wanted the
shallower drawdown could rationally take it, but they would be buying comfort,
not edge, and this project's rule is that a measurement is not a recommendation.

**The price-quality ranker fails again, and this is now the third independent
time.** It failed on constituents (3 of 9 sectors, median excess CAGR -2.70%),
it fails here on sectors (-11.60% to -12.93% excess), and the quarterly variant
burns 4.69x turnover to do it. Two of three cadences produced *identical*
results, because the selected three never drifted enough to trigger a
rebalance — so the loss is the selection, not the trading.

**Cadence barely matters, which is itself the finding.** The spread between
static, quarterly and threshold within any rule is under 0.9 points of CAGR.
Rebalancing policy is not where the answer was.

## What this is not

- **Not decision-grade, and not a gate.** Two years of daily history is one
  regime with no holdout. Nothing here moves capital.
- **Not a claim that costs are right.** 2 bps is the ETF spread assumption
  inherited from the constituent experiment. At any realistic higher cost the
  ranked rule loses by more; the diversifying rules barely move.
- **Not survivorship-biased in the way its predecessor was.** The constituent
  experiment applied today's index membership backward, hiding every company
  dropped from the index. All eleven SPDR sector ETFs existed across this whole
  window, so no membership is being reconstructed. Every *other* limitation the
  earlier experiment had, this one still has.

## What happens next

The forward shadow book (migration 058) began recording all three rules on
2026-08-13, effective 2026-08-17. That record — decisions written before their
outcomes exist, in a table that refuses UPDATE and DELETE — is the only evidence
that could ever upgrade a result on this page from exploratory to decision-grade,
and it cannot be backfilled. The backtest above is why none of these rules
deserves capital today; the shadow book is what would have to change that.
