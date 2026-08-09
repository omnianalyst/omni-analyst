"""One way to test a cross-sectional signal, with the traps built in.

Six agents measured six hypotheses in parallel and each rebuilt the same
machinery: panel alignment, non-overlapping periods, quintile portfolios,
t-statistics, sub-period splits. The measurement was perhaps a fifth of each
run. This is that fifth, written once.

The point is not throughput. It is that **five distinct ways a cross-sectional
backtest lies were discovered in a single day**, each of which nearly fooled a
careful agent, and every one of them is mechanical enough to check every time:

1. **The harness might not work.** A test that cannot find a planted edge proves
   nothing when it finds none. `self_test()` plants a known signal in synthetic
   data and refuses to run until it recovers it.
2. **Crypto's null runs hot.** One dominant market factor correlates every
   asset, so the null's own 95th percentile |t| is 2.2-2.5, not 1.96. A
   permutation null measures it per test rather than assuming.
3. **Subtracting costs inflates |t| on a loser.** Costs are near deterministic,
   so they shift the mean without touching the variance. A result significant
   NET but not GROSS is an artifact; both are always reported.
4. **A rank IC is not tradeability.** Positioning data reached IC t = -8.35 and
   earned nothing, because it predicted the median while the fat tail sat in the
   other decile. IC and portfolio return are computed separately and disagreement
   is flagged.
5. **The rebalance start offset is arbitrary.** Winners cleared at 13 of 30
   equally valid offsets with median below the bar. Every offset is swept; the
   reported statistic is the median, and the fraction clearing is reported next
   to it.

Plus the rule that killed five of six candidates: **a strategy is worth only its
most recent third.** The split is not optional and the recent third is reported
first.

Signals are plain functions of a price panel returning a score per asset per
date, higher meaning a better long. Everything else is fixed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import numpy as np
import pandas as pd

from omni.research.registry import Registry

Signal = Callable[[pd.DataFrame], pd.DataFrame]

MIN_ASSETS = 10
MIN_PERIODS = 20
PERMUTATION_DRAWS = 200


@dataclass(frozen=True)
class Leg:
    """One measured statistic: a mean, its t, and the sample it came from."""

    mean_ann_pct: float
    t: float
    n: int


@dataclass(frozen=True)
class Verdict:
    """What a hypothesis earned, with every guard's answer attached.

    `passed` is deliberately conservative: it requires the RECENT THIRD to clear
    the bar on a GROSS basis and to survive the alignment sweep at a majority of
    offsets. Full-sample significance is reported but never sufficient -- every
    strategy killed in this project was significant full-sample.
    """

    name: str
    horizon: int
    bar: float
    gross: Leg
    net: Leg
    thirds: tuple[Leg, ...]
    recent_third: Leg
    null_p95: float
    alignment_median_t: float
    alignment_clearing: float
    ic_t: float
    turnover: float
    cost_bps: float
    warnings: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            abs(self.recent_third.t) > self.bar
            and self.recent_third.mean_ann_pct > 0
            and abs(self.gross.t) > self.bar
            and self.alignment_clearing >= 0.5
        )

    def summary(self) -> str:
        flag = "PASS" if self.passed else "fail"
        return (
            f"{self.name:<28} h={self.horizon:<3} "
            f"gross {self.gross.mean_ann_pct:>8.2f}%/yr t {self.gross.t:>6.2f} | "
            f"recent third {self.recent_third.mean_ann_pct:>8.2f}%/yr "
            f"t {self.recent_third.t:>6.2f} | "
            f"align {self.alignment_clearing:>4.0%} | bar {self.bar:.2f} | {flag}"
        )


def _stat(returns: np.ndarray, horizon: int) -> Leg:
    """Mean, t and n for one series of period returns.

    Raises on fewer than two observations rather than returning a zero: a
    measured zero and an unmeasurable one must not be indistinguishable in the
    Verdict that follows.

    The degenerate-variance branch is not defensive padding. `np.std(ddof=1)` of
    a constant series returns **2.1e-17**, not `0.0`, so a `se > 0` test is
    always True and a spread with no variation at all produced `|t| = 1.8e16`
    and a passing verdict. Comparing a float to zero is exactly what the house
    rules forbid, and this is why: the quantity has to be judged on its own
    scale.
    """
    n = len(returns)
    if n < 2:
        raise ValueError(
            f"a statistic needs at least two observations, got {n}; returning a "
            f"zero here would make an unmeasured result look like a measured one"
        )
    mean = float(returns.mean())
    se = float(returns.std(ddof=1)) / math.sqrt(n)
    periods = 365.0 / horizon
    scale = max(abs(mean), float(np.abs(returns).max()))
    degenerate = not math.isfinite(se) or se <= scale * 1e-12
    return Leg(
        mean_ann_pct=mean * periods * 100.0,
        # No variation is no evidence, so the t is zero and the verdict fails.
        # That is the safe direction: the alternative divides by 2e-17.
        t=0.0 if degenerate else mean / se,
        n=n,
    )


def _periods(
    scores: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    horizon: int,
    offset: int,
    quantile: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Non-overlapping long-short returns, their costs, and the rank ICs.

    Long-short rather than long-only because crypto has one dominant factor: a
    long-only book measures market beta and calls it signal. The spread removes
    it, which is the only way what remains is attributable to the ranking.
    """
    dates = prices.index
    rets: list[float] = []
    costs: list[float] = []
    ics: list[float] = []
    held: set = set()

    for i in range(offset, len(dates) - horizon, horizon):
        d0, d1 = dates[i], dates[i + horizon]
        if d0 not in scores.index:
            continue
        s = scores.loc[d0].dropna()
        p0, p1 = prices.loc[d0], prices.loc[d1]
        usable = [a for a in s.index if pd.notna(p0.get(a)) and pd.notna(p1.get(a))]
        s = s[usable]
        if len(s) < MIN_ASSETS:
            continue
        fwd = (p1[usable] / p0[usable] - 1.0).astype(float)

        k = max(1, len(s) // quantile)
        ranked = s.sort_values(ascending=False)
        longs = list(ranked.index[:k])
        shorts = list(ranked.index[-k:])
        rets.append(float(fwd[longs].mean() - fwd[shorts].mean()))

        now = set(longs) | set(shorts)
        costs.append((len(now - held) / max(1, len(now))) if held else 1.0)
        held = now

        # A degenerate period is SKIPPED, not recorded as zero. Appending a
        # substituted 0.0 would shrink the variance and pull the mean toward
        # zero, so the IC statistic would be biased by however many degenerate
        # periods the panel happens to contain -- a fabricated observation
        # dressed as a measured one.
        if s.nunique() > 1 and fwd.nunique() > 1:
            ics.append(float(s.rank().corr(fwd.rank())))

    return np.array(rets), np.array(costs), np.array(ics)


def _permutation_p95(
    scores: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    horizon: int,
    quantile: int,
    draws: int,
    seed: int,
) -> float:
    """The null's own 95th percentile |t|, measured rather than assumed.

    Columns are permuted, so each asset keeps a real score history and a real
    price history -- they are simply the wrong pair. That preserves the market
    factor and the autocorrelation while destroying only the association under
    test, which is the thing that makes crypto's null run hot.
    """
    rng = np.random.default_rng(seed)
    cols = list(scores.columns)
    # Assets differ in when they existed, so permuting labels alone lands a
    # short-lived name's scores on a long-lived name's prices and changes how
    # many assets are rankable each period. The null would then be measured on a
    # different sample than the statistic it calibrates -- minor on a panel of
    # survivors, material once delisted names are present, which a
    # survivorship-corrected source makes routine. Re-imposing the original
    # availability mask keeps the sample fixed and permutes only the values.
    available = scores.notna()
    ts: list[float] = []
    for _ in range(draws):
        shuffled = scores.copy()
        shuffled.columns = list(rng.permutation(cols))
        shuffled = shuffled.reindex(columns=cols).where(available)
        r, _c, _i = _periods(
            shuffled, prices, horizon=horizon, offset=0, quantile=quantile
        )
        if len(r) >= MIN_PERIODS:
            ts.append(abs(_stat(r, horizon).t))
    return float(np.percentile(ts, 95)) if ts else float("nan")


def evaluate(
    *,
    name: str,
    source: str,
    signal: Signal,
    prices: pd.DataFrame,
    horizons: Sequence[int] = (1, 3, 7, 14),
    cost_bps: float = 20.0,
    quantile: int = 5,
    registry: Registry | None = None,
    permutation_draws: int = PERMUTATION_DRAWS,
    seed: int = 0,
    record: bool = True,
) -> list[Verdict]:
    """Measure one signal across horizons, with every guard applied.

    The bar comes from the registry and counts every test this project has ever
    run, including the ones about to happen. It is not a per-script number.
    """
    reg = registry if registry is not None else Registry()
    scores = signal(prices)
    if not isinstance(scores, pd.DataFrame):
        raise TypeError(f"{name}: signal must return a DataFrame of scores")
    scores = scores.reindex(index=prices.index, columns=prices.columns)

    bar = reg.bar(pending_cells=len(horizons))
    verdicts: list[Verdict] = []

    for horizon in horizons:
        warnings: list[str] = []
        rets, turn, ics = _periods(
            scores, prices, horizon=horizon, offset=0, quantile=quantile
        )
        if len(rets) < MIN_PERIODS:
            warnings.append(
                f"only {len(rets)} non-overlapping periods; below the {MIN_PERIODS} "
                f"floor, so no statistic is reported"
            )
            verdicts.append(
                Verdict(
                    name=name, horizon=horizon, bar=bar,
                    gross=Leg(0, 0, len(rets)), net=Leg(0, 0, len(rets)),
                    thirds=(), recent_third=Leg(0, 0, len(rets)),
                    null_p95=float("nan"), alignment_median_t=float("nan"),
                    alignment_clearing=0.0, ic_t=float("nan"),
                    turnover=float("nan"), cost_bps=cost_bps,
                    warnings=tuple(warnings),
                )
            )
            continue

        cost = turn * cost_bps / 10_000.0
        gross = _stat(rets, horizon)
        net = _stat(rets - cost, horizon)

        # Guard 3: costs shift the mean without touching the variance, so a
        # loser gets a bigger |t| when they are subtracted.
        if abs(net.t) > bar and abs(gross.t) <= bar:
            warnings.append(
                "significant NET but not GROSS: subtracting near-deterministic "
                "costs inflates |t| on a negative mean; this is an artifact"
            )

        # Guard 5: the rebalance start offset is arbitrary.
        align_ts = []
        for off in range(horizon):
            r, _c, _i = _periods(
                scores, prices, horizon=horizon, offset=off, quantile=quantile
            )
            if len(r) >= MIN_PERIODS:
                align_ts.append(_stat(r, horizon).t)
        align_median = float(np.median(align_ts)) if align_ts else float("nan")
        align_clear = (
            float(np.mean([abs(t) > bar for t in align_ts])) if align_ts else 0.0
        )
        if align_ts and align_clear < 0.5 and abs(gross.t) > bar:
            warnings.append(
                f"clears at only {align_clear:.0%} of {len(align_ts)} equally valid "
                f"rebalance offsets (median |t| {abs(align_median):.2f}); the "
                f"headline is one draw from a distribution"
            )

        # Guard 4: rank IC measures the median asset, the portfolio earns the mean.
        # `ics` can be shorter than the return series now that degenerate
        # periods are skipped rather than zero-filled.
        ic = _stat(ics, horizon) if len(ics) >= 2 else Leg(0.0, 0.0, len(ics))
        if abs(ic.t) > bar and abs(gross.t) <= bar:
            warnings.append(
                f"rank IC is significant (t {ic.t:.2f}) while the portfolio is not; "
                f"the signal orders the typical asset but the tail sits elsewhere, "
                f"so it does not monetise"
            )

        # The rule that killed five of six candidates.
        thirds: list[Leg] = []
        cut = len(rets) // 3
        if cut >= 2:
            for lo, hi in ((0, cut), (cut, 2 * cut), (2 * cut, len(rets))):
                thirds.append(_stat(rets[lo:hi], horizon))
        recent = thirds[-1] if thirds else gross
        if abs(gross.t) > bar and abs(recent.t) <= bar:
            warnings.append(
                "significant full-sample but not in the most recent third; every "
                "strategy retired in this project looked exactly like this"
            )

        null_p95 = _permutation_p95(
            scores, prices, horizon=horizon, quantile=quantile,
            draws=permutation_draws, seed=seed,
        )
        if not math.isnan(null_p95) and abs(gross.t) <= null_p95:
            warnings.append(
                f"|t| {abs(gross.t):.2f} does not exceed the permuted null's own "
                f"95th percentile ({null_p95:.2f}); indistinguishable from the "
                f"market factor"
            )

        verdicts.append(
            Verdict(
                name=name, horizon=horizon, bar=bar, gross=gross, net=net,
                thirds=tuple(thirds), recent_third=recent, null_p95=null_p95,
                alignment_median_t=align_median, alignment_clearing=align_clear,
                ic_t=ic.t, turnover=float(turn.mean()), cost_bps=cost_bps,
                warnings=tuple(warnings),
            )
        )

    if record:
        reg.record(
            name=name,
            source=source,
            cells=len(horizons),
            verdict="PASS" if any(v.passed for v in verdicts) else "fail",
            detail={
                "bar": bar,
                "best_recent_third_t": max(
                    (abs(v.recent_third.t) for v in verdicts), default=0.0
                ),
            },
        )
    return verdicts


def self_test(*, seed: int = 0) -> None:
    """Refuse to be trusted until the machinery recovers a planted edge.

    A test that cannot find an edge that is definitely there proves nothing when
    it reports none. This plants a signal in synthetic prices -- one asset group
    genuinely drifts up after a high score -- and asserts the harness finds it,
    then asserts it finds nothing in pure noise.
    """
    rng = np.random.default_rng(seed)
    n_days, n_assets = 900, 30
    dates = pd.date_range("2021-01-01", periods=n_days, freq="D")
    cols = [f"A{i:02d}" for i in range(n_assets)]

    planted = rng.normal(0, 1, size=(n_days, n_assets))
    rets = 0.02 * rng.normal(0, 1, size=(n_days, n_assets))
    rets[1:] += 0.004 * planted[:-1]          # yesterday's score pays today
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=cols)
    score_frame = pd.DataFrame(planted, index=dates, columns=cols)

    scratch = Registry(path=None)
    scratch.path = scratch.path.parent / ".selftest_registry.jsonl"
    if scratch.path.exists():
        scratch.path.unlink()

    found = evaluate(
        name="selftest.planted", source="synthetic",
        signal=lambda _p: score_frame, prices=prices, horizons=(1,),
        cost_bps=0.0, registry=scratch, permutation_draws=30, record=False,
    )[0]
    if abs(found.gross.t) < 4.0:
        raise AssertionError(
            f"harness failed to recover a planted edge (|t| {abs(found.gross.t):.2f} "
            f"< 4.0); every negative result it produces would be meaningless"
        )

    noise = pd.DataFrame(rng.normal(0, 1, size=(n_days, n_assets)), index=dates, columns=cols)
    empty = evaluate(
        name="selftest.noise", source="synthetic",
        signal=lambda _p: noise, prices=prices, horizons=(1,),
        cost_bps=0.0, registry=scratch, permutation_draws=30, record=False,
    )[0]
    if abs(empty.gross.t) > 3.0:
        raise AssertionError(
            f"harness found an edge in pure noise (|t| {abs(empty.gross.t):.2f}); "
            f"its positive results cannot be trusted"
        )
    if scratch.path.exists():
        scratch.path.unlink()
