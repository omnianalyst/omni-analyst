"""Formula function subset: logic, date, and string functions on the
AST evaluator, matching how Actual formulas are used in practice."""

from __future__ import annotations

import pytest

from omni.finance.rules import RuleError, apply_rules, evaluate_formula

NAMES = {"imported_payee": None, "payee": None, "notes": None, "account": None, "category": None}
CONTEXT = {"amount": -120.50, "today": "2026-01-05", "date": "2026-01-04"}


def test_if_and_or_not():
    assert evaluate_formula("=if(amount < 0, 10, 20)", CONTEXT) == 1000
    assert evaluate_formula("=if(amount > 0, 10, 20)", CONTEXT) == 2000
    assert evaluate_formula("=if(and(amount < 0, 1), 5, 6)", CONTEXT) == 500
    assert evaluate_formula("=if(or(0, 0), 5, 6)", CONTEXT) == 600
    assert evaluate_formula("=if(not(0), 1, 2)", CONTEXT) == 100


def test_date_functions_and_cents_semantics():
    # Actual converts every numeric formula result from dollars to cents;
    # we keep that exactly, so year() yields 2026 dollars-as-cents.
    assert evaluate_formula("=year(today)", CONTEXT) == 202600
    assert evaluate_formula("=month(date)", CONTEXT) == 100
    assert evaluate_formula("=day(date)", CONTEXT) == 400
    assert evaluate_formula('=date(2026, 3, 15)', CONTEXT) == "2026-03-15"


def test_concat_and_len():
    assert evaluate_formula('=concat("a", 1, "b")', CONTEXT) == "a1b"
    assert evaluate_formula('=len("kroger")', CONTEXT) == 600


def test_formula_still_refuses_exotic_calls():
    with pytest.raises(RuleError):
        evaluate_formula("=eval('1')", CONTEXT)
    with pytest.raises(RuleError):
        evaluate_formula("=getattr(1, 'x')", CONTEXT)


def test_date_formula_sets_notes_field():
    from omni.finance.rules import Rule

    rule = Rule(
        id="r1",
        rank=1,
        conditions=[{"field": "amount", "op": "lt", "value": 0}],
        actions=[{
            "op": "set",
            "field": "notes",
            "value": None,
            "options": {"formula": '=concat("spent on ", today)'},
        }],
    )
    tx, applied = apply_rules(
        [rule],
        {"amount": -1000, "date": "2026-01-05"},
        names=NAMES,
        resolve={"category": {}, "payee": {}},
    )
    assert applied == ["r1"]
    assert tx["notes"].startswith("spent on 20")
