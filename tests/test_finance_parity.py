"""Parity pass: schedules engine, full action ops, templates/formulas,
rollover modes, reports, starting balances, search."""

from __future__ import annotations

from datetime import date

import pytest

from omni.finance.rules import (
    Rule,
    RuleError,
    apply_rules,
    evaluate_formula,
    render_template,
)
from omni.finance.schedules import (
    Recurrence,
    ScheduleError,
    next_occurrence,
    occurrence_is_posted,
    occurrences_between,
    schedule_status,
)

NAMES = {"imported_payee": None, "payee": None, "notes": None, "account": None, "category": None}


def test_monthly_day_of_month():
    rec = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfMonth", "value": 15}],
    })
    dates = occurrences_between(rec, date(2026, 1, 1), date(2026, 3, 31))
    assert dates == [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]


def test_monthly_nth_weekday_and_last_weekday():
    third_friday = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfWeek", "value": 4, "n": 3}],
    })
    assert occurrences_between(third_friday, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 16)]

    last_weekday = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfWeek", "value": 4, "n": -1}],
    })
    assert occurrences_between(last_weekday, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 30)]


def test_monthly_interval_skips_months():
    rec = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "interval": 2,
        "patterns": [{"type": "dayOfMonth", "value": 1}],
    })
    dates = occurrences_between(rec, date(2026, 1, 1), date(2026, 6, 30))
    assert dates == [date(2026, 1, 1), date(2026, 3, 1), date(2026, 5, 1)]


def test_day_31_skips_short_months():
    rec = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfMonth", "value": 31}],
    })
    dates = occurrences_between(rec, date(2026, 1, 1), date(2026, 4, 30))
    assert dates == [date(2026, 1, 31), date(2026, 3, 31)]


def test_skip_weekend_solve_modes():
    base = {
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfMonth", "value": 17}],
        "skipWeekend": True,
    }
    after = Recurrence.from_config({**base, "weekendSolveMode": "after"})
    before = Recurrence.from_config({**base, "weekendSolveMode": "before"})
    nearest = Recurrence.from_config({**base, "weekendSolveMode": "nearest"})

    # 2026-01-17 is a Saturday
    assert occurrences_between(after, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 19)]
    assert occurrences_between(before, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 16)]
    assert occurrences_between(nearest, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 16)] or \
        occurrences_between(nearest, date(2026, 1, 1), date(2026, 1, 31)) == [date(2026, 1, 19)]


def test_weekly_and_yearly():
    weekly = Recurrence.from_config({
        "frequency": "weekly",
        "start": "2026-01-05",
        "patterns": [{"type": "dayOfWeek", "value": 0}],
    })
    dates = occurrences_between(weekly, date(2026, 1, 1), date(2026, 1, 25))
    assert dates == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]

    yearly = Recurrence.from_config({
        "frequency": "yearly",
        "start": "2026-01-01",
        "month": 7,
        "day": 4,
    })
    assert occurrences_between(yearly, date(2026, 1, 1), date(2028, 1, 1)) == [
        date(2026, 7, 4),
        date(2027, 7, 4),
    ]


def test_next_occurrence_and_bad_config():
    rec = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfMonth", "value": 15}],
    })
    assert next_occurrence(rec, date(2026, 2, 20)) == date(2026, 3, 15)
    with pytest.raises(ScheduleError):
        Recurrence.from_config({"frequency": "fortnightly"})


def test_schedule_status_paid_missed_upcoming():
    rec = Recurrence.from_config({
        "frequency": "monthly",
        "start": "2026-01-01",
        "patterns": [{"type": "dayOfMonth", "value": 5}],
    })
    today = date(2026, 3, 10)
    # No posted tx near 2026-03-05 or 2026-02-05
    assert schedule_status(rec, today, [])["status"] == "missed"
    # Feb 5 posted -> next is Mar 5, also missed unless posted
    assert schedule_status(rec, today, [date(2026, 2, 5)])["status"] == "missed"
    assert schedule_status(rec, today, [date(2026, 2, 5), date(2026, 3, 6)])["status"] == "upcoming"
    assert occurrence_is_posted(date(2026, 3, 5), [date(2026, 3, 1)]) is True


def test_template_rendering():
    context = {"payee": "Kroger", "today": "2026-01-05"}
    assert render_template("{{payee}} on {{today}}", context) == "Kroger on 2026-01-05"
    assert render_template("{{missing}}", context) == ""


def test_formula_dollars_in_cents_out():
    assert evaluate_formula("=10*2", {}) == 2000
    assert evaluate_formula("=round(amount/2, 1)", {"amount": 12.35}) == 620
    with pytest.raises(RuleError):
        evaluate_formula("=1/0", {})
    with pytest.raises(RuleError):
        evaluate_formula("=__import__('os')", {})
    with pytest.raises(RuleError):
        evaluate_formula("10", {})


def _resolve(categories: dict[str, str]):
    return {
        "category": {
            k: {"id": v, "valid": True} for k, v in categories.items()
        },
        "payee": {},
    }


def test_full_action_ops_on_one_rule_set():
    note_rule = Rule(
        id="r-notes",
        rank=1,
        conditions=[{"field": "imported_payee", "op": "contains", "value": "kroger"}],
        actions=[
            {"op": "prepend-notes", "field": "notes", "value": "[grocery] "},
            {"op": "append-notes", "field": "notes", "value": " (auto)"},
        ],
    )
    tx, applied = apply_rules(
        [note_rule],
        {"amount": -1000, "date": "2026-01-05", "notes": "weekly run", "cleared": True},
        names={**NAMES, "imported_payee": "KROGER #42"},
        resolve=_resolve({}),
    )
    assert applied == ["r-notes"]
    assert tx["notes"] == "[grocery] weekly run (auto)"

    template_rule = Rule(
        id="r-tpl",
        rank=2,
        conditions=[{"field": "payee", "op": "is", "value": "kroger"}],
        actions=[{
            "op": "set",
            "field": "notes",
            "value": None,
            "options": {"template": "{{payee}} logged {{today}}"},
        }],
    )
    tx, applied = apply_rules(
        [template_rule],
        {"amount": -1000, "date": "2026-01-05", "cleared": True},
        names={**NAMES, "payee": "Kroger"},
        resolve=_resolve({}),
    )
    assert "Kroger logged 20" in tx["notes"]

    formula_rule = Rule(
        id="r-fx",
        rank=3,
        conditions=[{"field": "amount", "op": "lt", "value": 0}],
        actions=[{"op": "set", "field": "amount", "value": 0, "options": {"formula": "=25.50"}}],
    )
    tx, _ = apply_rules(
        [formula_rule],
        {"amount": -1000, "date": "2026-01-05"},
        names=NAMES,
        resolve=_resolve({}),
    )
    assert tx["amount"] == 2550


def test_delete_transaction_tombstone_and_link_schedule():
    killer = Rule(
        id="r-kill",
        rank=1,
        conditions=[{"field": "notes", "op": "contains", "value": "transfer fee"}],
        actions=[{"op": "delete-transaction"}],
    )
    tx, _ = apply_rules(
        [killer],
        {"amount": -500, "date": "2026-01-05", "notes": "monthly transfer fee"},
        names=NAMES,
        resolve=_resolve({}),
    )
    assert tx["tombstone"] is True

    linker = Rule(
        id="r-link",
        rank=1,
        conditions=[{"field": "payee", "op": "is", "value": "landlord"}],
        actions=[{"op": "link-schedule", "value": "sched-uuid-1"}],
    )
    tx, _ = apply_rules(
        [linker],
        {"amount": -120000, "date": "2026-01-01"},
        names={**NAMES, "payee": "Landlord"},
        resolve=_resolve({}),
    )
    assert tx["schedule_id"] == "sched-uuid-1"


def test_schedule_condition_matches_occurrences():
    rule = Rule(
        id="r-sched",
        rank=1,
        conditions=[{
            "field": "schedule",
            "op": "is",
            "value": {
                "type": "recur",
                "schedule": {
                    "frequency": "monthly",
                    "start": "2026-01-01",
                    "patterns": [{"type": "dayOfMonth", "value": 15}],
                },
            },
        }],
        actions=[{"field": "category", "value": "Rent"}],
    )
    resolve = _resolve({"rent": "rent-id"})
    tx, applied = apply_rules(
        [rule],
        {"amount": -120000, "date": "2026-02-15"},
        names=NAMES,
        resolve=resolve,
    )
    assert applied == ["r-sched"]
    assert tx["category_id"] == "rent-id"

    tx, applied = apply_rules(
        [rule],
        {"amount": -120000, "date": "2026-02-16"},
        names=NAMES,
        resolve=resolve,
    )
    assert applied == []
