"""The ETF comparison must stay costed, delayed, and honestly labelled."""

import numpy as np
import pandas as pd
import pytest

from omni.research.etf_replication import price_quality_scores, run_experiment


def _panel(sessions: int = 320) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=sessions)
    t = np.arange(sessions, dtype=float)
    assets = {
        "A": 100 * np.exp(0.0010 * t + 0.008 * np.sin(t / 9)),
        "B": 100 * np.exp(0.0008 * t + 0.006 * np.sin(t / 11)),
        "C": 100 * np.exp(0.0005 * t + 0.012 * np.sin(t / 7)),
        "D": 100 * np.exp(0.0002 * t + 0.018 * np.sin(t / 5)),
    }
    frame = pd.DataFrame(assets, index=dates)
    frame["ETF"] = frame.mean(axis=1)
    return frame


def _run(panel=None, **overrides):
    params = {
        "prices": _panel() if panel is None else panel,
        "etf_symbol": "ETF",
        "constituents": ["A", "B", "C", "D"],
        "membership_mode": "current_membership_preview",
        "top_n": 2,
        "warmup_sessions": 126,
        "rebalance_sessions": 21,
    }
    params.update(overrides)
    return run_experiment(**params)


def test_current_membership_preview_cannot_look_like_point_in_time():
    result = _run()
    assert result.membership_mode == "current_membership_preview"
    assert any("survivorship bias" in warning for warning in result.warnings)


def test_every_strategy_starts_after_the_first_decision():
    panel = _panel()
    result = _run(panel)
    first_decision = result.strategies["ranked_top_n"].targets.index[0]
    for strategy in result.strategies.values():
        assert strategy.returns.index.min() > first_decision
        assert strategy.metrics.sessions == len(strategy.returns)


def test_future_prices_cannot_change_the_first_target():
    panel = _panel()
    baseline = _run(panel)
    first_decision = baseline.strategies["ranked_top_n"].targets.index[0]
    changed = panel.copy()
    changed.loc[changed.index > first_decision, "D"] *= 50
    rerun = _run(changed)
    first = baseline.strategies["ranked_top_n"].targets.loc[first_decision]
    repeated = rerun.strategies["ranked_top_n"].targets.loc[first_decision]
    assert first[first > 0].to_dict() == repeated[repeated > 0].to_dict()


def test_more_expensive_execution_reduces_custom_terminal_value():
    cheap = _run(constituent_cost_bps=0)
    costly = _run(constituent_cost_bps=100)
    for name in ("equal_weight", "ranked_top_n", "hybrid"):
        assert costly.strategies[name].values.iloc[-1] < cheap.strategies[name].values.iloc[-1]
        assert costly.strategies[name].metrics.modelled_cost_pct > 0


def test_hybrid_targets_are_fully_invested_and_keep_the_stated_etf_core():
    result = _run(hybrid_active_weight=0.20)
    targets = result.strategies["hybrid"].targets
    assert np.allclose(targets.sum(axis=1), 1.0)
    assert np.allclose(targets["ETF"], 0.80)


def test_price_quality_score_refuses_short_history():
    assert price_quality_scores(_panel(100)).empty


def test_etf_calendar_and_short_constituent_gaps_are_handled_without_fake_liquidation():
    panel = _panel()
    panel.loc[panel.index[20], "ETF"] = np.nan
    panel.loc[panel.index[200], "A"] = np.nan
    result = _run(panel)
    assert result.strategies["etf"].metrics.sessions > 0
    assert any("price gaps" in warning for warning in result.warnings)


def test_invalid_membership_mode_is_refused():
    with pytest.raises(ValueError, match="membership_mode"):
        _run(membership_mode="pretend_history")
