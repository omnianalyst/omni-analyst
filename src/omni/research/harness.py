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
   asset, so the null's own 95th percentile |t| runs near 2.0-2.3 rather than
   1.96, and it is measured per test rather than assumed. It must be measured on
   the SAME cross-section the statistic used: an earlier design permuted labels
   across the panel and re-imposed availability, collapsing the null's sample to
   roughly n^2/N and returning NaN -- a guard that had silently not applied --
   on any signal that restricted its universe (Findings 39, 49).
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

# How hard it is to pass, made an explicit choice rather than a property of the
# code. The default is STRICT because the operator this was built for has small
# capital and no track record, so a false positive that gets funded and levered
# can end the experiment while a false negative only costs opportunity. That
# asymmetry is a fact about the situation, not about statistics, and somebody
# else's situation may invert it.
#
#   strict       family-wise error control, sqrt(2 ln N). Assumes you are
#                hunting ONE true effect among N nulls -- the strictest
#                correction there is.
#   balanced     false-discovery-rate control (Benjamini-Hochberg). Appropriate
#                when you believe SEVERAL real effects exist, and materially
#                looser. This is the honest default for a genuine search.
#   exploratory  a fixed floor at the measured crypto null. Gates nothing;
#                surfaces candidates for a second, stricter look.
STRICTNESS = ("strict", "balanced", "exploratory")

# Portfolio weighting. Equal-weight is the default because it is the assumption
# every prior measurement in this project used, so changing it silently would
# make new numbers incomparable with recorded ones.
WEIGHTINGS = ("equal", "inverse_vol", "signal")


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
    # How many rebalances the calendar offered, against how many the signal
    # actually took. A strategy that abstains most of the time is a
    # legitimate object -- it is what waiting for a setup looks like -- but
    # its statistic rests on the trades it took, and a reader who cannot see
    # both numbers cannot tell selectivity from a small sample.
    periods_offered: int = 0
    strictness: str = "strict"
    weighting: str = "equal"

    @property
    def passed(self) -> bool:
        """Deliberately conservative, and the degree is now a stated choice.

        `strict` demands all four conditions. `balanced` drops the full-sample
        requirement -- a strategy that works NOW and not historically is a
        legitimate object, and demanding both is how a regime change gets
        mistaken for an absent edge. `exploratory` gates on the recent third
        alone and exists to surface candidates for a second, stricter look, not
        to fund anything.
        """
        recent_ok = (
            abs(self.recent_third.t) > self.bar
            and self.recent_third.mean_ann_pct > 0
        )
        if self.strictness == "exploratory":
            return recent_ok
        if self.strictness == "balanced":
            return recent_ok and self.alignment_clearing >= 0.5
        return (
            recent_ok
            and abs(self.gross.t) > self.bar
            and self.alignment_clearing >= 0.5
        )

    @property
    def selectivity(self) -> float:
        """Fraction of offered rebalances the signal declined to trade."""
        if self.periods_offered <= 0:
            return 0.0
        return 1.0 - (self.gross.n / self.periods_offered)

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
    weighting: str = "equal",
    vol_window: int = 30,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Non-overlapping long-short returns, their costs, and the rank ICs.

    Long-short rather than long-only because crypto has one dominant factor: a
    long-only book measures market beta and calls it signal. The spread removes
    it, which is the only way what remains is attributable to the ranking.

    `weighting` was equal in every prior measurement here, and that was an
    inherited default rather than a decision. It matters: crypto asset
    volatilities differ by several times, so an equal-weight basket is dominated
    by whichever name happens to be wildest, and its measured t-statistic is
    mostly a statement about that name. `inverse_vol` equalises risk
    contribution instead of dollars, which is what almost every real book does
    and is the single cheapest available improvement to a Sharpe ratio.
    """
    if weighting not in WEIGHTINGS:
        raise ValueError(f"weighting must be one of {WEIGHTINGS}, got {weighting!r}")

    dates = prices.index
    offered = 0
    rets: list[float] = []
    costs: list[float] = []
    ics: list[float] = []
    held: set = set()
    # Trailing realised vol, known at the decision date -- never the forward vol
    # of the period being weighted, which would be lookahead disguised as risk
    # management.
    trailing_vol = (
        np.log(prices).diff().rolling(vol_window).std()
        if weighting == "inverse_vol"
        else None
    )

    def _weights(names: list[str], d0, scores: pd.Series) -> np.ndarray:
        if weighting == "equal" or not names:
            return np.full(len(names), 1.0 / max(1, len(names)))
        if weighting == "inverse_vol":
            v = trailing_vol.loc[d0, names].to_numpy(float)
            # A name with no measurable trailing vol gets the basket's median
            # rather than an infinite weight; dropping it would change the
            # universe between weighting schemes and make them incomparable.
            finite = v[np.isfinite(v) & (v > 0)]
            med = float(np.median(finite)) if finite.size else 1.0
            v = np.where(np.isfinite(v) & (v > 0), v, med)
            w = 1.0 / v
        else:  # signal-proportional, on the absolute score within the leg
            w = np.abs(scores[names].to_numpy(float))
            if not np.isfinite(w).all() or w.sum() <= 0:
                w = np.ones(len(names))
        total = w.sum()
        return w / total if total > 0 else np.full(len(names), 1.0 / len(names))

    for i in range(offset, len(dates) - horizon, horizon):
        offered += 1
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
        wl = _weights(longs, d0, s)
        ws = _weights(shorts, d0, s)
        rets.append(
            float(fwd[longs].to_numpy(float) @ wl - fwd[shorts].to_numpy(float) @ ws)
        )

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

    return np.array(rets), np.array(costs), np.array(ics), offered


def _relabel(values: np.ndarray, rank: np.ndarray) -> np.ndarray:
    """Move each row's scores onto different assets, keeping membership exact.

    One draw is a fixed random `rank` over columns. At every date the row's
    scored positions are re-sorted by that rank and the values reassigned along
    it, so an asset that ranks early consistently receives an early column's
    values. That is what preserves PERSISTENCE: a score that was sticky on one
    asset is sticky on its replacement, and the null's holdings turn over at the
    same rate the strategy's do.

    Two properties this has and a global label permutation does not:

    - **Membership is exact.** Targets are drawn from the row's own scored
      positions, so the null ranks the same number of names on the same dates as
      the statistic. The previous design permuted labels across the whole panel
      and then re-imposed availability, intersecting two masks into roughly
      n^2/N -- which returned NaN, and a silently absent guard, on any signal
      that restricted its universe.
    - **The market factor survives.** Prices are untouched; only the pairing
      moves.

    What it gives up is the block structure of a single global permutation,
    where an asset kept one other asset's entire history. Persistence is
    reproduced by the fixed rank rather than inherited, which is the trade that
    buys back the sample.
    """
    out = np.full_like(values, np.nan)
    for i in range(values.shape[0]):
        idx = np.flatnonzero(~np.isnan(values[i]))
        if idx.size == 0:
            continue
        out[i, idx[np.argsort(rank[idx], kind="stable")]] = values[i, idx]
    return out


def _permutation_p95(
    scores: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    horizon: int,
    quantile: int,
    draws: int,
    seed: int,
) -> tuple[float, float, float]:
    """The null's own 95th percentile |t|, and the cross-section it measured on.

    Crypto's null runs hot -- one dominant market factor correlates every asset,
    so the null's 95th percentile |t| is 2.2-2.5 rather than 1.96. Assuming 1.96
    here would pass results that are indistinguishable from beta.

    The permutation moves scores onto the wrong assets while leaving prices, the
    per-date universe and the sample size exactly as the statistic found them.
    See `_relabel` for why membership is preserved by construction rather than
    re-imposed afterwards -- the earlier design did the latter and collapsed the
    null's cross-section to roughly n^2/N on any universe-restricted signal,
    returning NaN and a guard that had silently not applied.

    The two counts are still returned, because a null measured on a different
    sample than the statistic is worth saying out loud even when the difference
    is small.
    """
    rng = np.random.default_rng(seed)
    values = scores.to_numpy(dtype=float, copy=True)
    priced = prices.notna()
    available = scores.notna()
    real_rankable = (available & priced).sum(axis=1)
    real_names = float(real_rankable[real_rankable > 0].median())

    ts: list[float] = []
    null_counts: list[float] = []
    for _ in range(draws):
        rank = rng.permutation(values.shape[1])
        shuffled = pd.DataFrame(
            _relabel(values, rank), index=scores.index, columns=scores.columns
        )
        rankable = (shuffled.notna() & priced).sum(axis=1)
        null_counts.append(float(rankable[rankable > 0].median()))
        r, _c, _i, _o = _periods(
            shuffled, prices, horizon=horizon, offset=0, quantile=quantile
        )
        if len(r) >= MIN_PERIODS:
            ts.append(abs(_stat(r, horizon).t))
    null_names = float(np.median(null_counts)) if null_counts else float("nan")
    p95 = float(np.percentile(ts, 95)) if ts else float("nan")
    return p95, null_names, real_names


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
    strictness: str = "strict",
    weighting: str = "equal",
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

    if strictness not in STRICTNESS:
        raise ValueError(f"strictness must be one of {STRICTNESS}, got {strictness!r}")
    bar = (
        reg.bar(pending_cells=len(horizons))
        if strictness == "strict"
        else reg.fdr_bar(pending_cells=len(horizons))
    )
    verdicts: list[Verdict] = []

    for horizon in horizons:
        warnings: list[str] = []
        rets, turn, ics, offered = _periods(
            scores, prices, horizon=horizon, offset=0, quantile=quantile,
            weighting=weighting,
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
                    strictness=strictness, weighting=weighting,
                    periods_offered=offered,
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
            r, _c, _i, _o = _periods(
                scores, prices, horizon=horizon, offset=off, quantile=quantile,
                weighting=weighting,
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
        elif abs(ic.t) > bar and abs(gross.t) > bar and ic.t * gross.t < 0:
            # Found by use: eight oscillator cells had a significant IC pointing
            # one way and a significant portfolio pointing the other. That is a
            # stronger version of the median-versus-mean trap, not a weaker one
            # -- the score genuinely orders the typical asset while the fat tail
            # sits in the decile being shorted -- and the branch above misses it
            # because it only looks at an INSIGNIFICANT portfolio.
            warnings.append(
                f"rank IC (t {ic.t:.2f}) and portfolio (t {gross.t:.2f}) point in "
                f"OPPOSITE directions, both significant; the score orders the "
                f"median asset one way while the tail pays the other, so the sign "
                f"that ranks is not the sign that earns"
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

        null_p95, null_names, real_names = _permutation_p95(
            scores, prices, horizon=horizon, quantile=quantile,
            draws=permutation_draws, seed=seed,
        )
        # Guard 2 is the one that most looks like it ran when it did not: a NaN
        # p95 fails every comparison below, so the absence reads as a pass.
        if math.isnan(null_p95):
            warnings.append(
                "the permutation null could not be measured -- no draw produced "
                f"{MIN_PERIODS} rankable periods -- so this result is NOT calibrated "
                f"against the market factor. A signal scoring a subset of the panel "
                f"shrinks the null's cross-section to roughly n^2/N; here that is a "
                f"median of {null_names:.0f} names against the {real_names:.0f} the "
                f"statistic ranked"
            )
        elif not math.isnan(real_names) and null_names < 0.9 * real_names:
            warnings.append(
                f"the permutation null ranked a median of {null_names:.0f} names "
                f"against the statistic's {real_names:.0f}, so its 95th percentile "
                f"({null_p95:.2f}) calibrates a smaller cross-section than the one "
                f"under test"
            )
        if not math.isnan(null_p95) and abs(gross.t) <= null_p95:
            warnings.append(
                f"|t| {abs(gross.t):.2f} does not exceed the permuted null's own "
                f"95th percentile ({null_p95:.2f}); indistinguishable from the "
                f"market factor"
            )

        # Guard 6: a selective strategy is a legitimate object and a dangerous
        # statistic. Waiting for a setup means the t rests on the trades taken,
        # and with few of them the bar it must clear is not the bar an always-on
        # strategy faces -- the same |t| bought with 22 trades instead of 200 is
        # a much weaker claim, because the search over WHICH periods to trade is
        # itself a search nobody counted.
        if offered > 0 and gross.n < offered * 0.5:
            warnings.append(
                f"traded {gross.n} of {offered} offered rebalances "
                f"({1 - gross.n / offered:.0%} abstention). A conditional entry is "
                f"a legitimate shape, but the choice of WHEN to trade is a search "
                f"that no multiplicity correction here counts -- treat this |t| as "
                f"weaker than the same |t| from an always-on strategy"
            )

        verdicts.append(
            Verdict(
                name=name, horizon=horizon, bar=bar, gross=gross, net=net,
                thirds=tuple(thirds), recent_third=recent, null_p95=null_p95,
                alignment_median_t=align_median, alignment_clearing=align_clear,
                ic_t=ic.t, turnover=float(turn.mean()), cost_bps=cost_bps,
                warnings=tuple(warnings),
                strictness=strictness, weighting=weighting,
                periods_offered=offered,
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


def combine(
    signals: dict[str, pd.DataFrame],
    *,
    prices: pd.DataFrame,
    weights: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Blend several signals into one score, and report how independent they are.

    Every measurement in this project tested ONE signal at a time, which is a
    real blind spot: a portfolio of weak uncorrelated signals is the standard
    way high-Sharpe books are actually built, and it is a different object from
    any of its parts.

    Each signal is cross-sectionally z-scored per date before blending, because
    the inputs are on wildly different scales -- a funding rate is 1e-4 and a
    momentum is 1e-1, so an unnormalised sum is whichever signal has the largest
    units wearing an ensemble's name.

    Returns the blended score AND the pairwise correlation of the components,
    because the correlation is the whole question. Combining two signals that
    correlate at 0.95 buys nothing but a longer name, and this project's price
    signals are near-duplicates of each other by construction -- `williams_r_n`
    is exactly `100 * (1 - range_position_n)`. Look at the matrix before
    believing the blend.
    """
    if not signals:
        raise ValueError("combine needs at least one signal")
    w = weights or {name: 1.0 for name in signals}
    missing = set(signals) - set(w)
    if missing:
        raise ValueError(f"no weight given for {sorted(missing)}")

    z_frames: dict[str, pd.DataFrame] = {}
    for name, frame in signals.items():
        f = frame.reindex(index=prices.index, columns=prices.columns).astype(float)
        mu = f.mean(axis=1)
        sd = f.std(axis=1)
        # A date where every asset scores the same carries no cross-sectional
        # information; it becomes NaN rather than a divide-by-dust.
        z_frames[name] = f.sub(mu, axis=0).div(sd.where(sd > 0), axis=0)

    total = float(sum(abs(v) for v in w.values()))
    if total <= 0:
        raise ValueError("weights sum to zero; the blend would be undefined")
    blended = sum(z_frames[n] * (w[n] / total) for n in signals)

    names = list(signals)
    corr = pd.DataFrame(index=names, columns=names, dtype=float)
    stacked = {n: z_frames[n].stack(future_stack=True) for n in names}
    for a in names:
        for b in names:
            pair = pd.concat([stacked[a], stacked[b]], axis=1).dropna()
            corr.loc[a, b] = (
                float(pair.iloc[:, 0].corr(pair.iloc[:, 1])) if len(pair) > 2 else float("nan")
            )
    return blended, corr
