"""Analog windows: the honest version of the 1929-overlay.

The Trader-film overlay keyed on price-chart shapes -- unfalsifiable in a
10-year window. The honest analog keys on MACRO STATE: the same measured
inputs the regime assessment uses (yield curve inversion, Sahm trigger, LEI
direction, CPI momentum), projected back through FRED's long public history.

Everything here is descriptive and carries its own arithmetic. An analog
window names its dates, its similarity inputs, and the forward returns that
actually followed. Nothing predicts anything; step 4 (a gated
``analog.macro`` producer) exists only if this analysis accumulates and the
conviction gate later earns the right to speak.

Similarity is a plain weighted match over booleans/indicators -- identical in
spirit to the recession_probability weights, so an analog is "a month whose
gauges looked like now", judged by the same composition rules the system
already publishes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MacroState:
    """The gauges that define a moment. All are the regime assessment's own
    inputs, so 'similar' means similar by the system's own rules."""

    yield_curve_inverted: bool
    sahm_triggered: bool
    lei_negative: bool
    cpi_yoy: float | None  # None = not measurable that month; excluded from score


@dataclass(frozen=True)
class AnalogWindow:
    """One historical month judged similar, with what followed."""

    month: str  # YYYY-MM of the analog
    similarity: float  # 0..1, 1 = identical state
    matched: list[str]  # which gauges matched
    missed: list[str]  # which did not (named, not hidden)
    forward: dict[str, float | None]  # sleeve forward returns, 12m, percent


# Gauge weights mirror the recession-probability composition: curve 0.3,
# Sahm 0.4, LEI 0.3. CPI momentum is a tiebreaker worth 0.2 on top when both
# sides are measurable, capped at 1.0 -- inflation context refines, the three
# recession gauges decide.
_W_CURVE = 0.3
_W_SAHM = 0.4
_W_LEI = 0.3
_W_CPI = 0.2
_CPI_TOLERANCE = 1.0  # percentage points of YoY CPI considered "similar"


def similarity(now: MacroState, then: MacroState) -> tuple[float, list[str], list[str]]:
    """Weighted match between two months. Returns (score, matched, missed)."""
    matched: list[str] = []
    missed: list[str] = []
    score = 0.0
    for name, w, a, b in (
        ("yield_curve", _W_CURVE, now.yield_curve_inverted, then.yield_curve_inverted),
        ("sahm", _W_SAHM, now.sahm_triggered, then.sahm_triggered),
        ("lei", _W_LEI, now.lei_negative, then.lei_negative),
    ):
        if a == b:
            score += w
            matched.append(name)
        else:
            missed.append(name)
    if now.cpi_yoy is not None and then.cpi_yoy is not None:
        if abs(now.cpi_yoy - then.cpi_yoy) <= _CPI_TOLERANCE:
            score = min(1.0, score + _W_CPI)
            matched.append("cpi")
        else:
            missed.append("cpi")
    return score, matched, missed


def analog_windows(
    now: MacroState,
    history: list[tuple[str, MacroState]],
    forward_returns: dict[str, dict[str, float | None]],
    *,
    min_similarity: float = 0.7,
    limit: int = 8,
    current_month: str | None = None,
) -> list[AnalogWindow]:
    """The most similar historical months, with their forward sleeve returns.

    ``history`` is (YYYY-MM, state) rows. ``forward_returns`` maps YYYY-MM to
    each sleeve's following-12-month total return in percent; a missing
    forward value (the month is too recent, or a sleeve lacks data) stays
    None rather than being invented.

    ``current_month`` is excluded from the candidates: a window overlapping
    now has no forward period, and scoring the present against itself is a
    tautology, not an analog.
    """
    scored: list[tuple[float, str, MacroState, list[str], list[str]]] = []
    for month, state in history:
        if current_month is not None and month == current_month:
            continue
        score, matched, missed = similarity(now, state)
        if score >= min_similarity:
            scored.append((score, month, state, matched, missed))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [
        AnalogWindow(
            month=month,
            similarity=score,
            matched=matched,
            missed=missed,
            forward=forward_returns.get(month, {}),
        )
        for score, month, _state, matched, missed in scored[:limit]
    ]
