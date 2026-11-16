"""Every hypothesis ever tested, and the significance bar that follows from it.

The bar for calling a result real is `sqrt(2 * ln N)` -- the expected maximum
|t| under the null across N tests. That is only honest if N counts **every test
ever run**, not the handful in the current script. A researcher who runs 84
tests today, 84 tomorrow, and applies `sqrt(2 * ln 84)` to both has quietly run
168 tests against a bar built for 84.

So N lives here, in an append-only file, and it only goes up. This is the piece
that makes the arithmetic honest, and it cannot be reconstructed after the fact:
a test count not recorded on the day is a test count lost.

Append-only for the same reason `GATE_A_FINDINGS.md` is: a hypothesis that was
tested and failed is evidence, and deleting it to make room for a retest is how
a search convinces itself it has looked less than it has.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "docs" / "research" / "hypothesis_registry.jsonl"

# The permutation null measured on crypto cross-sections puts the null's own
# 95th percentile at |t| 2.2-2.5 even for a SINGLE test, because one dominant
# market factor correlates every asset. A bar of 1.96 is wrong here before any
# multiplicity is considered.
NULL_FLOOR = 2.5


@dataclass(frozen=True)
class Entry:
    """One tested hypothesis. `cells` is how many statistics it produced."""

    name: str
    source: str
    cells: int
    verdict: str
    recorded_at: str
    detail: dict[str, Any]


# The two bar calculations live here as pure functions of the history, not as
# methods, because more than one reader needs them: `evaluate()` computes the
# bar from the JSONL file during a research run, and the API computes it from
# the mirrored table so the product can show the same number. A second
# implementation of this arithmetic would eventually disagree with the first,
# and the disagreement would be invisible -- both would look like a plausible
# threshold. One implementation, two callers.


def bar_for(*, total_cells: int, pending_cells: int) -> float:
    """The |t| a result must clear, given every test before it AND now.

    `pending_cells` is included because the test about to run is part of the
    search. Computing the bar from history alone would let each new test be
    judged as though it were the first.
    """
    n = max(1, max(0, total_cells) + max(0, pending_cells))
    return max(NULL_FLOOR, math.sqrt(2.0 * math.log(n)))


def _critical_t(p: float) -> float:
    """The |t| whose two-sided normal p-value is `p`."""
    if p >= 1.0:
        return 0.0
    return NormalDist().inv_cdf(1.0 - p / 2.0)


def fdr_bar_for(*, stats: Sequence[float], pending_cells: int, q: float = 0.10) -> float:
    """The Benjamini-Hochberg |t| a NEW test must clear, given the record.

    `bar_for` controls the FAMILY-WISE error rate -- the probability of even one
    false positive across every test ever run. That is the right target when
    hunting a single true effect among nulls, and it is brutal when you believe
    several real effects exist, because it treats the hundredth test as harshly
    as if it were the only one.

    FDR instead controls the expected PROPORTION of discoveries that are false.
    At q = 0.10 it accepts that roughly one in ten survivors is noise, in
    exchange for a materially lower bar.

    The arithmetic. With the recorded |t| sorted descending `t_1..t_n` and
    `m = n + pending` tests in the family, a candidate at value `x` takes rank
    `r(x) = 1 + #{t_i > x}` and is a BH discovery when its two-sided p-value
    satisfies `p(x) <= q * r(x) / m`. The bar is the smallest `x` for which that
    holds. Within one rank band the requirement is a fixed critical value, so
    the bar is the minimum over bands of `max(critical_r, t_r)` where that
    point lies inside the band.

    **It tightens as the record fills with nulls**, which is the property that
    makes it trustworthy: with nothing but nulls recorded the candidate ranks
    near the top and must clear roughly `p <= q / m` -- about |t| 3.7 against
    500 nulls. Recorded results that are themselves large lower the bar, as BH
    intends: it is adaptive to how many effects the record appears to hold. It
    is sign-blind, so a large NEGATIVE recorded t counts as an effect too.

    An earlier version returned the smallest recorded statistic that had itself
    passed, or the floor when none had -- so against any all-null history a new
    test only ever had to clear 2.5, however long the history was. That is the
    opposite of the behaviour described above, and it is why this is written
    from the candidate's side.

    `stats` are the observed t values, in any order, SIGNED where the sign was
    recorded. A negative one is a measured loser: for the family this bar
    judges -- "does it earn a positive return" -- it is a null, not an effect,
    so it is ranked at zero rather than by its magnitude. Ranking it by
    magnitude let two catastrophic strategies (t -26 and -17) lower the bar for
    everything tested after them. A value recorded as a magnitude with its sign
    lost is taken as given, which can only loosen the bar, never tighten it;
    that is stated rather than hidden. A non-finite statistic is refused rather
    than ranked. With none recorded it falls back to the null floor rather than
    inventing a threshold.
    """
    if not 0.0 < q < 1.0:
        raise ValueError(f"q is a false-discovery rate in (0, 1), got {q}")
    values = [float(t) for t in stats]
    if any(not math.isfinite(t) for t in values):
        raise ValueError("a recorded statistic is not finite; it cannot be ranked")
    ordered = sorted((max(0.0, t) for t in values), reverse=True)
    if not ordered:
        return NULL_FLOOR
    n = len(ordered)
    # The candidate is itself a member of the family, pending or not.
    m = n + max(1, pending_cells)
    best = math.inf
    for rank in range(1, n + 2):
        # Rank band: at least `t_rank` (so exactly rank-1 recorded sit strictly
        # above), strictly below `t_{rank-1}`.
        upper = math.inf if rank == 1 else ordered[rank - 2]
        lower = ordered[rank - 1] if rank <= n else 0.0
        x = max(_critical_t(q * rank / m), lower)
        if x < upper:
            best = min(best, x)
    # rank 1 is always feasible (its band is unbounded above), so `best` is finite.
    return max(NULL_FLOOR, best)


class Registry:
    """The running count of every test, and the bar it implies.

    `path` is a JSONL file so a partially written line can never corrupt the
    history before it -- the failure mode of a single JSON document that is
    rewritten on every append.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[Entry]:
        if not self.path.exists():
            return []
        out: list[Entry] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line is the one corruption an append-only
                # file can suffer (an interrupted write). Skipping it loses one
                # record; raising would make the whole history unreadable.
                continue
            out.append(Entry(**raw))
        return out

    def total_cells(self) -> int:
        """Every statistic this project has ever computed against the null."""
        return sum(e.cells for e in self.entries())

    def recorded_stats(self) -> list[float]:
        """Every per-test statistic recorded against the null, unordered.

        Signed as recorded: `fdr_bar_for` ranks a negative statistic as a null.
        """
        return [
            float(t)
            for e in self.entries()
            if (t := e.detail.get("best_recent_third_t")) is not None
        ]

    def fdr_bar(self, *, pending_cells: int, q: float = 0.10) -> float:
        """The Benjamini-Hochberg threshold over this history, as a |t|.

        Computed from the recorded per-test statistics rather than assumed, so
        it tightens as the history fills with nulls. See `fdr_bar_for`.
        """
        return fdr_bar_for(
            stats=self.recorded_stats(), pending_cells=pending_cells, q=q
        )

    def bar(self, *, pending_cells: int) -> float:
        """The |t| a result must clear, given this history AND the pending test.

        See `bar_for`. Never below `NULL_FLOOR`.
        """
        return bar_for(total_cells=self.total_cells(), pending_cells=pending_cells)

    def record(
        self,
        *,
        name: str,
        source: str,
        cells: int,
        verdict: str,
        detail: dict[str, Any] | None = None,
    ) -> Entry:
        if cells < 1:
            raise ValueError(
                f"{name}: a test that produced no statistics is not a test; "
                f"recording zero cells would understate the search"
            )
        entry = Entry(
            name=name,
            source=source,
            cells=int(cells),
            verdict=verdict,
            recorded_at=datetime.now(UTC).isoformat(),
            detail=detail or {},
        )
        line = json.dumps(asdict(entry), sort_keys=True)
        # Append with an explicit flush+fsync: the count is the one thing that
        # cannot be recovered from anywhere else if the process dies.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry
