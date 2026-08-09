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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "_orchestrator" / "hypothesis_registry.jsonl"


@dataclass(frozen=True)
class Entry:
    """One tested hypothesis. `cells` is how many statistics it produced."""

    name: str
    source: str
    cells: int
    verdict: str
    recorded_at: str
    detail: dict[str, Any]


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

    def fdr_bar(self, *, pending_cells: int, q: float = 0.10) -> float:
        """The Benjamini-Hochberg threshold, expressed as a |t|.

        `bar()` controls the FAMILY-WISE error rate -- the probability of even
        one false positive across every test ever run. That is the right target
        when hunting a single true effect among nulls, and it is brutal when you
        believe several real effects exist, because it treats the hundredth test
        as harshly as if it were the only one.

        FDR instead controls the expected PROPORTION of discoveries that are
        false. At q = 0.10 it accepts that roughly one in ten survivors is
        noise, in exchange for a materially lower bar. For a genuine search
        across many families that is the honest correction.

        Computed from the recorded per-test statistics rather than assumed, so
        it tightens as the history fills with nulls -- which is the behaviour
        that makes it trustworthy. With no recorded statistics it falls back to
        the crypto null floor rather than inventing a threshold.
        """
        from math import erfc, sqrt

        stats = sorted(
            (abs(float(t)) for e in self.entries()
             if (t := e.detail.get("best_recent_third_t")) is not None),
            reverse=True,
        )
        if not stats:
            return 2.5
        m = len(stats) + max(0, pending_cells)
        # Two-sided normal p-value for each observed |t|, largest t first.
        threshold = None
        for rank, t in enumerate(stats, start=1):
            p = erfc(t / sqrt(2.0))
            if p <= q * rank / m:
                threshold = t
            else:
                break
        return max(2.5, threshold if threshold is not None else 2.5)

    def bar(self, *, pending_cells: int) -> float:
        """The |t| a result must clear, given everything tested before AND now.

        Includes `pending_cells` because the test about to run is part of the
        search. Computing the bar from history alone would let each new test be
        judged as though it were the first.

        Never below 2.5. The permutation null measured on crypto cross-sections
        puts the null's own 95th percentile at |t| 2.2-2.5 even for a SINGLE
        test, because one dominant market factor correlates every asset. A bar
        of 1.96 is wrong here before any multiplicity is considered.
        """
        n = max(1, self.total_cells() + max(0, pending_cells))
        return max(2.5, math.sqrt(2.0 * math.log(n)))

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
