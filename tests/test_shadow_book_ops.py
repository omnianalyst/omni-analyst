from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4

import pandas as pd
import pytest

from omni.research.shadow_book import Decision, ShadowBookRefused

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shadow_book_score_ops", ROOT / "ops" / "shadow_book_score.py"
)
assert SPEC is not None and SPEC.loader is not None
shadow_book_score = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shadow_book_score
SPEC.loader.exec_module(shadow_book_score)


def _decision(effective_from: date, weights: dict[str, float]) -> Decision:
    return Decision(
        id=uuid4(),
        book="etf_equal_weight_sectors",
        rule_version="equal_weight/v1",
        decided_at=datetime(2026, 8, 13, 22, tzinfo=UTC),
        effective_from=effective_from,
        universe=("XLK", "XLE"),
        inputs={},
        weights=weights,
        cost_bps=Decimal(2),
        benchmark="SPY",
        note=None,
    )


async def test_scoring_pass_records_a_completed_period_from_stored_decisions(monkeypatch):
    first = _decision(date(2026, 8, 17), {"XLK": 1.0})
    current = _decision(date(2026, 8, 18), {"XLE": 1.0})
    following = _decision(date(2026, 8, 19), {"XLK": 0.5, "XLE": 0.5})
    panel = pd.DataFrame()
    score = SimpleNamespace(
        period_start=date(2026, 8, 18),
        period_end=date(2026, 8, 19),
        sessions=1,
        realised_return=0.0125,
        benchmark_return=-0.004,
        cost_charged=0.0004,
        turnover=2.0,
        limits={"source": "recorded marks"},
    )
    monkeypatch.setattr(
        shadow_book_score, "decisions_for", AsyncMock(return_value=[first, current, following])
    )
    monkeypatch.setattr(
        shadow_book_score, "unscored_decisions", AsyncMock(return_value=[current])
    )
    score_decision = Mock(return_value=score)
    record_outcome = AsyncMock()
    monkeypatch.setattr(shadow_book_score, "score_decision", score_decision)
    monkeypatch.setattr(shadow_book_score, "record_outcome", record_outcome)

    result = await shadow_book_score.score_book(
        "pool", current.book, panel, through=date(2026, 8, 19)
    )

    assert result == shadow_book_score.PassResult(scored=1, pending=0)
    score_decision.assert_called_once_with(
        current,
        panel,
        previous_weights=first.weights,
        period_end=following.effective_from,
    )
    record_outcome.assert_awaited_once_with(
        "pool",
        decision_id=current.id,
        period_start=date(2026, 8, 18),
        period_end=date(2026, 8, 19),
        sessions=1,
        realised_return=Decimal("0.0125"),
        benchmark_return=Decimal("-0.004"),
        cost_charged=Decimal("0.0004"),
        turnover=Decimal("2.0"),
        limits={"source": "recorded marks"},
    )


@pytest.mark.parametrize("unavailable", ["no following decision", "missing marks"])
async def test_unavailable_outcomes_remain_pending(monkeypatch, unavailable):
    decision = _decision(date(2026, 8, 18), {"XLK": 1.0})
    following = _decision(date(2026, 8, 19), {"XLE": 1.0})
    decisions = [decision] if unavailable == "no following decision" else [decision, following]
    monkeypatch.setattr(
        shadow_book_score, "decisions_for", AsyncMock(return_value=decisions)
    )
    monkeypatch.setattr(
        shadow_book_score, "unscored_decisions", AsyncMock(return_value=[decision])
    )
    score_decision = Mock(side_effect=ShadowBookRefused("missing marks"))
    record_outcome = AsyncMock()
    monkeypatch.setattr(shadow_book_score, "score_decision", score_decision)
    monkeypatch.setattr(shadow_book_score, "record_outcome", record_outcome)

    result = await shadow_book_score.score_book(
        "pool", decision.book, pd.DataFrame(), through=date(2026, 8, 19)
    )

    assert result == shadow_book_score.PassResult(scored=0, pending=1)
    record_outcome.assert_not_awaited()
    if unavailable == "no following decision":
        score_decision.assert_not_called()
    else:
        score_decision.assert_called_once()


async def test_production_main_loads_prices_and_invokes_every_book(monkeypatch):
    panel = pd.DataFrame(
        {"SPY": [100.0]}, index=pd.to_datetime([date(2026, 8, 19)])
    )
    client = SimpleNamespace(pool="pool", close=AsyncMock())
    connect = AsyncMock(return_value=client)
    load_panel = AsyncMock(return_value=(panel, "audience-id"))
    score_book = AsyncMock(return_value=shadow_book_score.PassResult(scored=1, pending=0))
    record_health = AsyncMock(return_value=0)
    monkeypatch.setattr(shadow_book_score, "connect", connect)
    monkeypatch.setattr(shadow_book_score, "load_panel", load_panel)
    monkeypatch.setattr(shadow_book_score, "score_book", score_book)
    monkeypatch.setattr(shadow_book_score, "record_loop_health", record_health)

    assert await shadow_book_score.main() == 0

    connect.assert_awaited_once_with(shadow_book_score.settings.database_url)
    load_panel.assert_awaited_once_with("pool", [*shadow_book_score.SECTORS, "SPY"])
    assert score_book.await_args_list == [
        call("pool", book, panel, through=date(2026, 8, 19))
        for book in shadow_book_score.RULES
    ]
    record_health.assert_awaited_once_with(
        "pool",
        loop_name="shadow_scoring",
        ok=True,
        result=f"scored 4, pending 0, across {len(shadow_book_score.RULES)} books",
        expected_interval_seconds=86_400.0,
    )
    client.close.assert_awaited_once()


def test_the_scorer_covers_every_book_the_recorder_writes():
    record_spec = importlib.util.spec_from_file_location(
        "shadow_book_record_ops", ROOT / "ops" / "shadow_book_record.py"
    )
    assert record_spec is not None and record_spec.loader is not None
    shadow_book_record = importlib.util.module_from_spec(record_spec)
    sys.modules[record_spec.name] = shadow_book_record
    record_spec.loader.exec_module(shadow_book_record)

    assert set(shadow_book_score.RULES) == set(shadow_book_record.RULES)
    assert "etf_tsmom_252" in shadow_book_score.RULES


_decay_spec = importlib.util.spec_from_file_location(
    "shadow_decay_ops", ROOT / "ops" / "shadow_decay.py"
)
assert _decay_spec is not None and _decay_spec.loader is not None
shadow_decay = importlib.util.module_from_spec(_decay_spec)
sys.modules[_decay_spec.name] = shadow_decay
_decay_spec.loader.exec_module(shadow_decay)


async def test_the_decay_pass_judges_every_book_and_records_health(monkeypatch):
    from omni.research.decay import EdgeEvaluation

    def _evaluation(book):
        return EdgeEvaluation(
            book=book,
            as_of=date(2026, 8, 18),
            promoted=book == "etf_tsmom_252",
            state="insufficient",
            scored_sessions=0,
            recent_sessions=0,
            mean_session_excess=None,
            decay_p=None,
            window_start=None,
            window_end=None,
            reason="not enough scored outcomes yet",
        )

    client = SimpleNamespace(pool="pool", close=AsyncMock())
    connect = AsyncMock(return_value=client)
    outcomes_for = AsyncMock(return_value=[])
    evaluate_edge = Mock(side_effect=lambda book, as_of, rows: _evaluation(book))
    record_edge_state = AsyncMock(return_value=True)
    latest = AsyncMock(return_value=[])
    record_health = AsyncMock(return_value=0)
    monkeypatch.setattr(shadow_decay, "connect", connect)
    monkeypatch.setattr(shadow_decay, "outcomes_for", outcomes_for)
    monkeypatch.setattr(shadow_decay, "evaluate_edge", evaluate_edge)
    monkeypatch.setattr(shadow_decay, "record_edge_state", record_edge_state)
    monkeypatch.setattr(shadow_decay, "latest_edge_states", latest)
    monkeypatch.setattr(shadow_decay, "record_loop_health", record_health)

    assert await shadow_decay.main() == 0

    assert [c.args[1] for c in outcomes_for.await_args_list] == list(shadow_decay.RULES)
    assert record_edge_state.await_count == len(shadow_decay.RULES)
    record_health.assert_awaited_once_with(
        "pool",
        loop_name="shadow_decay",
        ok=True,
        result=(
            f"evaluated {len(shadow_decay.RULES)} books, 0 promoted edge(s) decayed"
        ),
        expected_interval_seconds=86_400.0,
    )
    client.close.assert_awaited_once()


def _run_wrapper(tmp_path: Path, *, decision_exit: int, scoring_exit: int, decay_exit: int = 0):
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "shadow_book_record.py").write_text("decision\n")
    (ops / "shadow_book_score.py").write_text("scoring\n")
    (ops / "shadow_decay.py").write_text("decay\n")
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "read -r pass\n"
        "echo \"$pass ran\"\n"
        "if [[ $pass == decision ]]; then exit \"$DECISION_EXIT\"; fi\n"
        "if [[ $pass == scoring ]]; then exit \"$SCORING_EXIT\"; fi\n"
        "exit \"$DECAY_EXIT\"\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "OMNI_ROOT": str(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DECISION_EXIT": str(decision_exit),
        "SCORING_EXIT": str(scoring_exit),
        "DECAY_EXIT": str(decay_exit),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "shadow_book.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, (ops / "shadow_book.log").read_text()


def test_production_wrapper_runs_decision_then_scoring(tmp_path):
    result, log = _run_wrapper(tmp_path, decision_exit=0, scoring_exit=0)

    assert result.returncode == 0
    assert log.index("decision ran") < log.index("scoring ran")
    assert log.index("scoring ran") < log.index("decay ran")
    assert "shadow_book start at " in log
    assert "shadow_book decision start at " in log
    assert "shadow_book decision end exit 0 at " in log
    assert "shadow_book scoring start at " in log
    assert "shadow_book scoring end exit 0 at " in log
    assert "shadow_book decay start at " in log
    assert "shadow_book decay end exit 0 at " in log
    assert "shadow_book end exit 0 at " in log
    assert "failure" not in log


@pytest.mark.parametrize(
    ("decision_exit", "scoring_exit", "decay_exit", "expected"),
    [(11, 0, 0, 11), (0, 23, 0, 23), (0, 0, 7, 7), (11, 23, 7, 11)],
)
def test_production_wrapper_attempts_both_passes_and_exposes_failure(
    tmp_path, decision_exit, scoring_exit, decay_exit, expected
):
    result, log = _run_wrapper(
        tmp_path,
        decision_exit=decision_exit,
        scoring_exit=scoring_exit,
        decay_exit=decay_exit,
    )

    assert result.returncode == expected
    assert "decision ran" in log
    assert "scoring ran" in log
    assert "decay ran" in log
    for name, command_exit in (
        ("decision", decision_exit),
        ("scoring", scoring_exit),
        ("decay", decay_exit),
    ):
        if command_exit == 0:
            assert f"shadow_book {name} end exit 0" in log
        else:
            assert f"shadow_book {name} failure exit {command_exit}" in log
            assert f"shadow_book {name} end" not in log
    assert f"shadow_book failure exit {expected}" in log
    assert "shadow_book end" not in log
