"""Pure-logic tests for the finance domain: normalisation (Actual MIT
port), rules engine, CSV parsing, cents handling."""

from __future__ import annotations

import pytest

from omni.finance.normalisation import (
    amount_to_cents,
    normalise_import_row,
    normalise_notes,
    normalise_payee_name,
    normalised_string,
    title_case,
)
from omni.finance.rules import (
    Rule,
    RuleError,
    apply_rules,
    validate_rule_payload,
)
from omni.finance.service import parse_csv


def test_normalised_string_strips_diacritics_the_actual_way():
    assert normalised_string("Łódź België Straße") == "lodz belgie strasse"
    assert normalised_string("Ølstykke") == "olstykke"
    assert normalised_string("Cœur") == "coeur"


def test_title_case_keeps_small_words_and_inner_caps():
    assert title_case("acme corp of texas") == "Acme Corp of Texas"
    assert title_case("the beginning of it") == "The Beginning of It"
    assert title_case("mcdonald's lunch") == "Mcdonald's Lunch"


def test_payee_name_trims_and_blanks_become_none():
    assert normalise_payee_name("  amazon  ") == "Amazon"
    assert normalise_payee_name("   ") is None
    assert normalise_payee_name(None) is None


def test_notes_escapes_hash_and_trims():
    assert normalise_notes(" bill #42 ") == "bill ##42"
    assert normalise_notes("") is None


def test_amount_to_cents_never_floats():
    assert amount_to_cents("12.34") == 1234
    assert amount_to_cents("-12.34") == -1234
    assert amount_to_cents("$1,234.56") == 123456
    assert amount_to_cents("12.3") == 1230
    assert amount_to_cents("12") == 1200
    assert amount_to_cents(0.29) == 29


def test_import_row_requires_the_truth():
    with pytest.raises(ValueError):
        normalise_import_row({"amount": "10.00"})
    with pytest.raises(ValueError):
        normalise_import_row({"date": "2026-01-01"})
    with pytest.raises(ValueError):
        normalise_import_row({"date": "2026-01-01", "payee_name": "X", "amount": "0"})
    row = normalise_import_row(
        {"date": "2026-01-01", "payee_name": " amazon ", "amount": "-12.34"}
    )
    assert row["amount"] == -1234
    assert row["payee_name"] == "Amazon"
    assert row["imported_payee"] == "Amazon"


def test_rule_contains_sets_category():
    rule = Rule(
        id="r1",
        rank=1,
        conditions=[{"field": "imported_payee", "op": "contains", "value": "amazon"}],
        actions=[{"field": "category", "value": "Shopping"}],
    )
    resolve = {"category": {"shopping": {"id": "cat-1", "valid": True}}, "payee": {}}
    tx, applied = apply_rules(
        [rule],
        {"amount": -1234, "category_id": None},
        names={"imported_payee": "Amazon Marketplace"},
        resolve=resolve,
    )
    assert applied == ["r1"]
    assert tx["category_id"] == "cat-1"


def test_rule_conditions_case_fold_and_rank_overrides():
    first = Rule(
        id="r1",
        rank=1,
        conditions=[{"field": "payee", "op": "is", "value": "kroger"}],
        actions=[{"field": "category", "value": "Groceries"}],
    )
    second = Rule(
        id="r2",
        rank=2,
        conditions=[{"field": "payee", "op": "is", "value": "KROGER"}],
        actions=[{"field": "category", "value": "Household"}],
    )
    resolve = {
        "category": {
            "groceries": {"id": "g", "valid": True},
            "household": {"id": "h", "valid": True},
        },
        "payee": {},
    }
    tx, applied = apply_rules(
        [second, first],
        {"amount": -1000},
        names={"payee": "Kroger"},
        resolve=resolve,
    )
    assert applied == ["r1", "r2"]
    assert tx["category_id"] == "h"


def test_rule_outflow_direction_and_approx():
    rule = Rule(
        id="r1",
        rank=1,
        conditions=[
            {
                "field": "amount",
                "op": "isapprox",
                "value": 5000,
                "options": {"direction": "outflow"},
            }
        ],
        actions=[{"field": "category", "value": "Rent"}],
    )
    resolve = {"category": {"rent": {"id": "rent", "valid": True}}, "payee": {}}
    names = {"imported_payee": None, "payee": None, "notes": None}

    _, applied = apply_rules([rule], {"amount": -4800}, names=names, resolve=resolve)
    assert applied == ["r1"]
    _, applied = apply_rules([rule], {"amount": 4800}, names=names, resolve=resolve)
    assert applied == []
    _, applied = apply_rules([rule], {"amount": -6000}, names=names, resolve=resolve)
    assert applied == []


def test_unresolvable_category_leaves_tx_untouched():
    rule = Rule(
        id="r1",
        rank=1,
        conditions=[{"field": "notes", "op": "is", "value": "x"}],
        actions=[{"field": "category", "value": "Does Not Exist"}],
    )
    tx, applied = apply_rules(
        [rule], {"amount": -100}, names={"notes": "x"}, resolve={"category": {}, "payee": {}}
    )
    assert applied == ["r1"]
    assert "category_id" not in tx


def test_rule_validation_refuses_junk():
    with pytest.raises(RuleError):
        validate_rule_payload([], [{"field": "category", "value": "X"}])
    with pytest.raises(RuleError):
        validate_rule_payload(
            [{"field": "imported_payee", "op": "gt", "value": "x"}], []
        )
    with pytest.raises(RuleError):
        validate_rule_payload(
            [{"field": "amount", "op": "oneOf", "value": [1]}],
            [{"field": "category", "value": "X"}],
        )


def test_parse_csv_maps_columns_and_dates():
    csv_text = (
        "Posted Date,Description,Debit,Credit,Notes\n"
        "01/15/2026,ACME CORP,12.34,,bill 1\n"
        "2026-01-16,OTHER CO,,5.00,payment\n"
    )
    rows = parse_csv(csv_text)
    assert rows[0]["date"] == "2026-01-15"
    assert rows[0]["payee_name"] == "ACME CORP"
    assert rows[0]["amount"] == "-12.34"
    assert rows[0]["notes"] == "bill 1"
    assert rows[1]["date"] == "2026-01-16"


def test_parse_csv_refuses_without_date_or_amount():
    with pytest.raises(ValueError):
        parse_csv("Description,Notes\nACME,x\n")


def test_parse_csv_rejects_unknown_dates():
    with pytest.raises(ValueError):
        parse_csv("Date,Amount\n15 janvier 2026,12.00\n")
