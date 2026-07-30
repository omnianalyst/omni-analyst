"""Consensus comparison and trend as pure capabilities.

Extracted from two inline handlers in v1
`app/api/v1/endpoints/consensus.py`, both graded ``needs-extraction`` because
the analysis lived in the handler rather than a service:

- ``GET /consensus/compare`` (consensus.py:648-719) ran ``ConsensusEngine.analyze``
  per symbol, then ranked the results by composite score, built a
  factor-by-symbol comparison, and generated comparative insights inline.
- ``GET /consensus/historical/{symbol}`` (consensus.py:562-624) read a Redis
  sorted set of past consensus snapshots and computed trend statistics
  (score change, min/max/avg, current score and signal) inline.

The consensus *engine* itself is not ported -- it is an async,
framework-tangled service that fetches per-factor data through
``market_data_service`` and holds no portable maths. These capabilities take
its already-computed results as plain dicts and perform the comparative /
trend analysis that was inline in the handlers.

Where v1 substituted defaults on missing input -- ``/historical`` returned a
full ``trends`` dict of zeros plus a ``"hold"`` signal and a guidance message
when no history existed, presenting a flat trend where none was measurable --
this module raises ``Unavailable`` from ``omni.ingest.protocol`` instead. A
symbol with no consensus history has an unknown trend, not a zero one; a
compare call with no analyses has no ranking, not an empty one.

Input shape (the caller owns assembling these from whatever produced them):

- ``compare_consensus`` takes ``{symbol: analysis}`` where each ``analysis`` is
  a plain dict with ``composite_score`` (required), optional ``signal`` /
  ``confidence``, and optional ``factor_scores`` mapping a factor key to
  ``{"score": float, "signal": str}``. This is the dict form of v1's
  ``Consensus`` dataclass; the engine is not a dependency.
- ``consensus_trend`` takes the chronologically-ordered list of stored
  consensus snapshots (oldest first, matching the Redis sorted-set order v1
  iterated), each requiring a ``composite_score``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omni.ingest.protocol import Unavailable


def compare_consensus(
    analyses: Mapping[str, Mapping[str, Any]],
    *,
    factor_leader_threshold: float = 0.5,
) -> dict[str, Any]:
    """Rank per-symbol consensus analyses and surface comparative insights.

    Mirrors the comparative section of v1's ``/consensus/compare`` handler:
    rank by ``composite_score`` descending, build a factor-by-symbol
    comparison, and generate human-readable insights (the overall leader, plus
    the leader of each factor whose winning score clears
    ``factor_leader_threshold`` -- 0.5 in v1).

    ``analyses`` maps each symbol to its analysis dict. At least one is
    required: v1 raised HTTP 500 ("Failed to analyze any symbols") when none
    resolved, and an empty comparison has no ranking. ``composite_score`` is
    the ranking key and is required on every analysis; a missing score is a
    gap in the input, not a zero to rank.

    The factor set compared is the union of factors present across the
    analyses (v1 iterated a ``FactorType`` enum; the enum is a framework
    dependency and is dropped -- the factors are whatever the analyses
    actually carry). Factor keys are humanised with ``_`` -> space in the
    insight text only, matching v1's ``factor.value.replace('_', ' ')``.
    """
    if not analyses:
        raise Unavailable("no analyses; cannot rank a comparison")

    for symbol, analysis in analyses.items():
        if "composite_score" not in analysis:
            raise Unavailable(f"analysis for {symbol!r} has no composite_score; cannot rank")

    ranked = sorted(analyses.items(), key=lambda kv: kv[1]["composite_score"], reverse=True)

    rankings = {
        symbol: {
            "rank": idx + 1,
            "score": analysis["composite_score"],
            "signal": analysis.get("signal"),
            "confidence": analysis.get("confidence"),
        }
        for idx, (symbol, analysis) in enumerate(ranked)
    }

    factors: list[str] = []
    seen: set[str] = set()
    for analysis in analyses.values():
        for factor in analysis.get("factor_scores", {}):
            if factor not in seen:
                seen.add(factor)
                factors.append(factor)
    factors.sort()

    factor_comparison: dict[str, dict[str, Any]] = {}
    for factor in factors:
        per_symbol: dict[str, Any] = {}
        for symbol, analysis in analyses.items():
            fs = analysis.get("factor_scores", {}).get(factor)
            if fs is not None:
                per_symbol[symbol] = {
                    "score": fs.get("score"),
                    "signal": fs.get("signal"),
                }
        factor_comparison[factor] = per_symbol

    insights: list[str] = []
    best_overall = ranked[0][0]
    insights.append(f"{best_overall} shows the strongest overall consensus score")

    for factor in factors:
        scores: dict[str, float] = {}
        for symbol, analysis in analyses.items():
            fs = analysis.get("factor_scores", {}).get(factor)
            if fs is not None and fs.get("score") is not None:
                scores[symbol] = fs["score"]
        if scores:
            leader, leader_score = max(scores.items(), key=lambda kv: kv[1])
            if leader_score > factor_leader_threshold:
                insights.append(f"{leader} leads in {factor.replace('_', ' ')}")

    return {
        "symbols": list(analyses.keys()),
        "rankings": rankings,
        "factor_comparison": factor_comparison,
        "insights": insights,
    }


def consensus_trend(history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Trend statistics over a chronologically-ordered consensus history.

    Mirrors the trend computation in v1's ``/consensus/historical/{symbol}``:
    from the stored snapshot series it derives the score change over the
    window and the min / max / average / current score, plus the most recent
    signal. ``history`` is oldest-first (the order v1 read from the Redis
    sorted set), so the current reading is ``history[-1]``.

    Each snapshot must carry ``composite_score`` -- it is the only field the
    trend is computed from, and a snapshot without it is not a consensus
    observation. v1 returned a full zero-filled ``trends`` dict (and a
    ``"hold"`` signal) when history was absent, which reported a flat trend
    over data that did not exist; that path raises ``Unavailable`` here.

    With a single observation the change is 0.0 (one point cannot move) and
    min == max == avg == current == that point -- an honest degenerate
    summary, not a fabricated one. ``current_signal`` is the last snapshot's
    ``signal`` when present, else ``None`` (unknown), never a synthesised
    ``"hold"``.
    """
    history = list(history)
    if not history:
        raise Unavailable("no consensus history; trend is unknown")

    for i, snapshot in enumerate(history):
        if "composite_score" not in snapshot:
            raise Unavailable(f"history snapshot {i} has no composite_score; cannot compute trend")

    scores = [snapshot["composite_score"] for snapshot in history]
    score_change = scores[-1] - scores[0] if len(scores) > 1 else 0.0

    return {
        "data_points": len(scores),
        "score_change": round(score_change, 3),
        "min_score": round(min(scores), 3),
        "max_score": round(max(scores), 3),
        "avg_score": round(sum(scores) / len(scores), 3),
        "current_score": round(scores[-1], 3),
        "current_signal": history[-1].get("signal"),
    }
