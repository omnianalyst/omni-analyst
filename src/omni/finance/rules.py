"""Ranked transaction rules, ported from Actual Budget (MIT).

Source: packages/loot-core/src/server/rules/{condition,action,rule}.ts and
server/transactions/transaction-rules.ts. Copyright (c) James Long and
Actual Budget contributors, MIT license.

Kept from Actual: conditions AND within a rule, every matching rule
applies in rank order (later rules may override earlier ones), string
matching is case-insensitive on the normalised form, `isapprox` on
numbers is the default 10% threshold and on dates means same month,
inflow/outflow constrain the comparison to one sign, schedule
(recurring) conditions, and the full action set: set (with handlebars
template or formula options), set-split-amount, prepend-notes,
append-notes, link-schedule, delete-transaction.

Deltas from Actual, stated: HyperFormula (a full spreadsheet engine)
is replaced by a safe arithmetic evaluator covering the practical
formula space (+ - * / % with amount/today/date context and round/abs/
min/max). Formulas are written in dollars, as in Actual, and results
are stored as integer cents. Actual's `balanceOf` helper needs the
account-balance prefetch machinery and is not ported.

Condition shape (JSONB): {"field", "op", "value"} where field is one of
imported_payee, payee, notes, account, category, amount, date, cleared,
schedule. Date values are "YYYY-MM-DD" (isbetween takes a two-element
list). Schedule values are {"type": "recur", "schedule": {config}} --
see finance/schedules.py.

Action shape: {"op"?, "field", "value", "options"?} with op defaulting
to "set".
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from omni.finance.normalisation import normalised_string
from omni.finance import schedules as schedules_mod

APPROX_THRESHOLD = 0.10

STRING_FIELDS = {"imported_payee", "payee", "notes", "account", "category"}
STRING_OPS = {"is", "contains", "oneOf", "isNot"}
NUMBER_OPS = {"is", "isapprox", "isbetween", "gt", "gte", "lt", "lte"}
NUMBER_FIELDS = {"amount"}
DATE_OPS = {"is", "isapprox", "isbetween", "gt", "gte", "lt", "lte"}
DATE_FIELDS = {"date"}
SCHEDULE_OPS = {"is", "isapprox"}
SCHEDULE_FIELDS = {"schedule"}
BOOLEAN_OPS = {"is"}
BOOLEAN_FIELDS = {"cleared"}

ACTION_OPS = {
    "set",
    "set-split-amount",
    "link-schedule",
    "prepend-notes",
    "append-notes",
    "delete-transaction",
}
SET_FIELDS = {"category", "payee", "notes", "cleared", "date", "amount"}

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_ALLOWED_FORMULA_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Call, ast.keyword,
    ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.BoolOp, ast.And, ast.Or, ast.Not,
)
def _truthy(value) -> bool:
    return value not in (None, 0, "", False)


def _fn_if(args):
    return args[1] if _truthy(args[0]) else (args[2] if len(args) > 2 else 0)


def _fn_date(args):
    year, month, day = (int(a) for a in args[:3])
    return f"{year:04d}-{month:02d}-{day:02d}"


_ALLOWED_FORMULA_FUNCS = {
    "round": lambda *a: round(a[0], a[1] if len(a) > 1 else 0),
    "abs": lambda *a: abs(a[0]),
    "min": lambda *a: min(a),
    "max": lambda *a: max(a),
    "int": lambda *a: int(a[0]),
    "if": lambda *a: _fn_if(a),
    "_if": lambda *a: _fn_if(a),
    "_and": lambda *a: all(_truthy(x) for x in a),
    "_or": lambda *a: any(_truthy(x) for x in a),
    "_not": lambda *a: not _truthy(a[0]),
    "and": lambda *a: all(_truthy(x) for x in a),
    "or": lambda *a: any(_truthy(x) for x in a),
    "not": lambda *a: not _truthy(a[0]),
    "len": lambda *a: len(str(a[0])),
    "concat": lambda *a: "".join(str(x) for x in a),
    "day": lambda *a: int(str(a[0])[8:10]),
    "month": lambda *a: int(str(a[0])[5:7]),
    "year": lambda *a: int(str(a[0])[0:4]),
    "date": lambda *a: _fn_date(a),
}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    rank: int
    conditions: list[dict]
    actions: list[dict]
    enabled: bool = True


def _norm(value: str | None) -> str:
    return normalised_string(value) if value else ""


def _string_matches(op: str, expected: str | list[str], actual: str | None) -> bool:
    hay = _norm(actual)
    if op == "is":
        return hay == _norm(expected)
    if op == "contains":
        return _norm(expected) in hay
    if op == "oneOf":
        options = {_norm(v) for v in expected}
        return hay in options
    if op == "isNot":
        return hay != _norm(expected)
    raise RuleError(f"unsupported string operator {op!r}")


def _number_matches(op: str, expected, actual: int, *, direction: str | None) -> bool:
    if direction == "outflow":
        if actual >= 0:
            return False
        value = -actual
    elif direction == "inflow":
        if actual <= 0:
            return False
        value = actual
    else:
        value = actual
    if op == "is":
        return value == expected
    if op == "isapprox":
        return abs(value - expected) <= abs(expected) * APPROX_THRESHOLD
    if op == "isbetween":
        low, high = sorted((expected[0], expected[1]))
        return low <= value <= high
    if op == "gt":
        return value > expected
    if op == "gte":
        return value >= expected
    if op == "lt":
        return value < expected
    if op == "lte":
        return value <= expected
    raise RuleError(f"unsupported number operator {op!r}")


def _date_value(value) -> str:
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, dict):
        for key in ("date", "num1"):
            if value.get(key):
                return str(value[key])[:10]
    if isinstance(value, date):
        return value.isoformat()
    raise RuleError(f"unrecognised date value {value!r}")


def _date_matches(op: str, value, actual: str | None) -> bool:
    if not actual:
        return False
    day = actual[:10]
    if op == "is":
        return day == _date_value(value)
    if op == "isapprox":
        return day[:7] == _date_value(value)[:7]
    if op == "isbetween":
        pair = value if isinstance(value, list) else (value.get("num1"), value.get("num2"))
        low, high = sorted(_date_value(p)[:10] for p in pair)
        return low <= day <= high
    bound = _date_value(value)
    if op == "gt":
        return day > bound
    if op == "gte":
        return day >= bound
    if op == "lt":
        return day < bound
    if op == "lte":
        return day <= bound
    raise RuleError(f"unsupported date operator {op!r}")


def _schedule_matches(op: str, value, tx: dict) -> bool:
    if not isinstance(value, dict) or value.get("type") != "recur":
        raise RuleError('schedule condition value must be {"type": "recur", "schedule": {...}}')
    tx_day = str(tx.get("date") or "")[:10]
    if not tx_day:
        return False
    from datetime import date as date_cls, timedelta as timedelta_cls

    rec = schedules_mod.Recurrence.from_config(value.get("schedule") or {})
    day = date_cls.fromisoformat(tx_day)
    if op == "is":
        return bool(schedules_mod.occurrences_between(rec, day, day))
    lo = day - timedelta_cls(days=schedules_mod.POSTED_WINDOW_BEFORE)
    hi = day + timedelta_cls(days=schedules_mod.POSTED_WINDOW_AFTER)
    return bool(schedules_mod.occurrences_between(rec, lo, hi))


def condition_matches(condition: dict, tx: dict, *, names: dict[str, str]) -> bool:
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")
    options = condition.get("options") or {}
    if field in STRING_FIELDS:
        actual = names.get(field)
        return _string_matches(op, value, actual)
    if field in NUMBER_FIELDS:
        actual = tx.get("amount")
        if actual is None:
            return False
        return _number_matches(op, value, actual, direction=options.get("direction"))
    if field in DATE_FIELDS:
        return _date_matches(op, value, tx.get("date"))
    if field in SCHEDULE_FIELDS:
        return _schedule_matches(op, value, tx)
    if field in BOOLEAN_FIELDS:
        return bool(tx.get(field)) is bool(value)
    raise RuleError(f"unsupported condition field {field!r}")


def rule_applies(rule: Rule, tx: dict, *, names: dict[str, str]) -> bool:
    return all(condition_matches(c, tx, names=names) for c in rule.conditions)


def render_template(template: str, context: dict) -> str:
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(substitute, template)


def evaluate_formula(formula: str, context: dict) -> int | str:
    """Dollars in, cents out for numeric results; string results (dates,
    concat) pass through. Safe arithmetic on the tx context."""
    if not formula.startswith("="):
        raise RuleError("formula must start with =")
    # Spreadsheet-style keyword functions -- if/and/or/not are Python
    # keywords, so they are rewritten to aliases before parsing.
    expression = re.sub(r"\b(if|and|or|not)\s*\(", lambda m: f"_{m.group(1)}(", formula[1:])
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"invalid formula: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_FORMULA_NODES):
            raise RuleError(f"formula element not allowed: {type(node).__name__}")

    def resolver(name: str):
        if name in _ALLOWED_FORMULA_FUNCS:
            return _ALLOWED_FORMULA_FUNCS[name]
        value = context.get(name)
        if value is None:
            return 0
        if isinstance(value, (bool, int, float, str)):
            return value
        return 0

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    if not called <= set(_ALLOWED_FORMULA_FUNCS):
        raise RuleError(
            "formula may only call " + ", ".join(sorted(_ALLOWED_FORMULA_FUNCS))
        )

    # Calls resolve to the function table under a mangled key; bare names
    # resolve to the transaction context. Without this, a context field
    # named like a function (e.g. "date") would shadow the callable or
    # vice versa.
    class _MangleCalls(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FORMULA_FUNCS:
                node.func = ast.Name(id=f"__fn_{node.func.id}", ctx=ast.Load())
            return node

    tree = ast.fix_missing_locations(_MangleCalls().visit(tree))

    names_used = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    if "__builtins__" in names_used:
        raise RuleError("formula may not touch builtins")
    scope = {f"__fn_{name}": _ALLOWED_FORMULA_FUNCS[name] for name in called}
    for name in names_used:
        if name.startswith("__fn_"):
            continue
        value = context.get(name)
        if value is None:
            scope[name] = 0
        elif isinstance(value, (bool, int, float, str)):
            scope[name] = value
        else:
            scope[name] = 0
    try:
        result = eval(  # noqa: S307 - AST-whitelisted arithmetic only
            compile(tree, "<formula>", "eval"),
            {"__builtins__": {}},
            scope,
        )
    except ZeroDivisionError:
        raise RuleError("formula divided by zero") from None
    except Exception as exc:  # noqa: BLE001 - surfaced as a rule error
        raise RuleError(f"formula error: {exc}") from exc
    if isinstance(result, str):
        return result
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise RuleError("formula must produce a number or a string")
    return int(round(float(result) * 100))


def _formula_context(tx: dict, names: dict) -> dict:
    from omni.finance.normalisation import amount_to_cents

    today = date.today().isoformat()
    return {
        "amount": (tx.get("amount") or 0) / 100.0,
        "today": today,
        "date": tx.get("date") or today,
        "cleared": tx.get("cleared"),
        "payee": names.get("payee"),
        "notes": names.get("notes"),
        "account": names.get("account"),
        "category": names.get("category"),
        "imported_payee": names.get("imported_payee"),
    }


def apply_rules(
    rules: list[Rule],
    tx: dict,
    *,
    names: dict[str, str],
    resolve: dict[str, Any],
) -> tuple[dict, list[str]]:
    """Run every enabled rule in rank order. `names` maps the string
    fields of this transaction to their display values; `resolve` maps
    {"category": norm_name -> {"id", "valid"}, "payee": ...}. Returns the
    mutated tx and the ids of rules that applied."""
    applied: list[str] = []
    effective_names = {
        field: (names.get(field) if names.get(field) is not None else tx.get(field))
        for field in STRING_FIELDS
    }
    context = _formula_context(tx, effective_names)
    for rule in sorted((r for r in rules if r.enabled), key=lambda r: r.rank):
        if not rule.conditions:
            continue
        if not rule_applies(rule, tx, names=effective_names):
            continue
        applied.append(rule.id)
        for action in rule.actions:
            op = action.get("op", "set")
            field = action.get("field")
            value = action.get("value")
            options = action.get("options") or {}
            if op not in ACTION_OPS:
                raise RuleError(f"unsupported action op {op!r}")
            if op == "delete-transaction":
                tx["tombstone"] = True
            elif op == "link-schedule":
                tx["schedule_id"] = value
            elif op == "prepend-notes":
                tx["notes"] = (value + tx["notes"]) if tx.get("notes") else value
            elif op == "append-notes":
                tx["notes"] = (tx["notes"] + value) if tx.get("notes") else value
            elif op == "set-split-amount":
                if options.get("method") == "fixed-amount":
                    tx["amount"] = value
                elif options.get("method") == "formula":
                    tx["amount"] = evaluate_formula(options.get("formula", ""), context)
            elif op == "set":
                if field not in SET_FIELDS:
                    raise RuleError(f"unsupported action field {field!r}")
                if options.get("formula"):
                    value = evaluate_formula(options["formula"], context)
                elif options.get("template"):
                    value = render_template(options["template"], context)
                if field in ("category", "payee"):
                    key = "category_id" if field == "category" else "payee_id"
                    resolved = resolve.get(field, {}).get(_norm(value), {})
                    if not resolved.get("valid"):
                        continue
                    tx[key] = resolved["id"]
                elif field == "cleared":
                    tx["cleared"] = bool(value)
                elif field == "amount":
                    tx["amount"] = int(value)
                else:
                    tx[field] = value
    return tx, applied


def validate_rule_payload(conditions: list[dict], actions: list[dict]) -> None:
    if not conditions:
        raise RuleError("a rule needs at least one condition")
    if not actions:
        raise RuleError("a rule needs at least one action")
    for c in conditions:
        field = c.get("field")
        op = c.get("op")
        if field in STRING_FIELDS and op not in STRING_OPS:
            raise RuleError(f"unsupported string operator {op!r}")
        elif field in NUMBER_FIELDS and op not in NUMBER_OPS:
            raise RuleError(f"unsupported number operator {op!r}")
        elif field in DATE_FIELDS and op not in DATE_OPS:
            raise RuleError(f"unsupported date operator {op!r}")
        elif field in SCHEDULE_FIELDS and op not in SCHEDULE_OPS:
            raise RuleError(f"unsupported schedule operator {op!r}")
        elif field in BOOLEAN_FIELDS and op not in BOOLEAN_OPS:
            raise RuleError(f"unsupported boolean operator {op!r}")
        elif field not in STRING_FIELDS | NUMBER_FIELDS | DATE_FIELDS | SCHEDULE_FIELDS | BOOLEAN_FIELDS:
            raise RuleError(f"unsupported condition field {field!r}")
        if op == "oneOf" and not isinstance(c.get("value"), list):
            raise RuleError("oneOf requires a list value")
        if field in NUMBER_FIELDS and op == "isbetween" and (
            not isinstance(c.get("value"), list) or len(c["value"]) != 2
        ):
            raise RuleError("isbetween requires a two-value list")
        if field in SCHEDULE_FIELDS:
            value = c.get("value")
            if not isinstance(value, dict) or value.get("type") != "recur":
                raise RuleError('schedule condition needs {"type": "recur", "schedule": {...}}')
            schedules_mod.Recurrence.from_config(value.get("schedule") or {})
    for a in actions:
        op = a.get("op", "set")
        if op not in ACTION_OPS:
            raise RuleError(f"unsupported action op {op!r}")
        if op == "set" and a.get("field") not in SET_FIELDS:
            raise RuleError(f"unsupported action field {a.get('field')!r}")
        if (a.get("options") or {}).get("formula"):
            formula = a["options"]["formula"]
            if not isinstance(formula, str) or not formula.startswith("="):
                raise RuleError("formula must start with =")
