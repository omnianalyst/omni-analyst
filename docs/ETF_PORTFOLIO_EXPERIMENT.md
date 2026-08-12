# ETF versus constituent portfolios — exploratory result

Run date: 2026-08-11 (America/Vancouver)

## Question

Is it better to buy a sector ETF, hold its constituent companies directly, or
use Omni's measurements to select a smaller basket from those constituents?

## Design

Four long-only paths were replayed on the production adjusted-close store:

1. the sector ETF;
2. an equal-weight basket of eligible current sector constituents;
3. the ten highest price-quality constituents, equal weighted; and
4. an 80% ETF / 20% ranked-basket hybrid.

The score uses only information available at each decision close: six- and
three-month momentum, trailing volatility, drawdown, and positive one-month
window consistency. Targets take effect on the following session. Portfolios
rebalance every 21 sessions after a 126-session warmup. The main run charges 20
basis points per unit of constituent turnover, 2 basis points to enter an ETF,
and a 10 basis point annual ETF expense assumption.

Price panel: 2024-08-05 through 2026-08-11, 506 sessions and 515 symbols.

## Main top-10 result

| ETF | ETF CAGR | Equal-weight excess | Ranked excess | Hybrid excess | Ranked turnover |
|---|---:|---:|---:|---:|---:|
| XLB | 14.35% | +3.58% | +4.47% | +0.93% | 4.86x |
| XLE | 26.19% | +2.09% | -4.56% | -0.85% | 5.31x |
| XLI | 22.20% | -3.85% | -13.72% | -2.59% | 8.97x |
| XLK | 45.30% | +15.82% | +57.66% | +10.87% | 6.79x |
| XLP | 1.43% | +1.40% | +2.56% | +0.56% | 5.38x |
| XLRE | 2.54% | -3.51% | -2.70% | -0.49% | 4.75x |
| XLU | 7.94% | +1.78% | -2.70% | -0.48% | 5.58x |
| XLV | 8.46% | +0.90% | -9.81% | -1.85% | 7.54x |
| XLY | 11.17% | +1.30% | -4.92% | -0.84% | 5.57x |

SPY, XLC, and XLF were refused rather than estimated because a currently
listed constituent developed an unresolved historical ticker/price gap while
held. Refusing preserves the rule that a missing mark cannot silently become a
zero return or a costless liquidation.

## Sensitivity

| Selection | Cost | CAGR wins | Median excess CAGR | Sharpe wins | Drawdown improvements |
|---|---:|---:|---:|---:|---:|
| Top 5 | 20 bps | 4/9 | -2.01% | 4/9 | 3/9 |
| Top 10 | 5 bps | 3/9 | -2.20% | 3/9 | 5/9 |
| Top 10 | 20 bps | 3/9 | -2.70% | 3/9 | 4/9 |
| Top 10 | 50 bps | 3/9 | -3.94% | 3/9 | 3/9 |
| Top 15 | 20 bps | 4/9 | -0.76% | 4/9 | 3/9 |

At 20 bps, equal weight beat the ETF's CAGR and Sharpe in 7/9 sectors, with a
median +1.40% CAGR, but improved maximum drawdown in only 3/9. The 80/20 hybrid
usually gave up a small amount of CAGR (top-10 median -0.49%) while improving
maximum drawdown in 8/9 sectors.

## Interpretation

The active ranker does not pass. Its positive mean is dominated by technology;
the median sector loses to its ETF, the conclusion worsens with costs, and the
sample covers only about eighteen post-warmup months. That is concentration,
not broad evidence of selection skill.

Equal weighting is the more credible research candidate. Its result is broad
across sectors and less turnover-sensitive, but its worse drawdown outcome
means it is not an automatic improvement over buying the ETF.

The hybrid is useful as a risk-control candidate, not yet as a return enhancer.

## Limits and next gate

This is **not decision-grade** because today's S&P 500 membership and current
GICS sector links are applied backward. Delisted and removed constituents are
absent, creating survivorship bias. Historical ETF weights are also absent, so
the constituent path is equal weight rather than a true ETF replication.

Before capital allocation, obtain dated constituent/weight snapshots, resolve
ticker histories and delisting returns, repeat on at least ten years, preserve
an untouched recent period, and then run a forward shadow portfolio. Until
those gates pass, the ETF remains the default core and the custom baskets remain
research candidates.

## Reproduction

```bash
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/etf_portfolio_experiment.py
```

Change `--top-n`, `--cost-bps`, and `--hybrid-weight` for sensitivity runs.
