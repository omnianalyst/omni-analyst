"""Position sizing: the arithmetic, and the refusals that keep it honest.

Three groups of assertions carry the weight.

**The zero.** A coin flip against a symmetric payoff has no edge, and the only
correct size for it is exactly zero. Asserted as an equality against
`Decimal(0)`, not as "small", because an implementation that returned a
thousandth of NAV for a no-edge trade would pass any tolerance test and would
be trading noise forever.

**The missing probability.** There is no default hit rate anywhere in the
module, and that is enforced structurally: the source is parsed and every
function carrying a `hit_rate` parameter is checked to have no default. A test
that only called `size(hit_rate=None)` would keep passing on the day someone
adds `hit_rate: Decimal = Decimal("0.5")` to a helper.

**The exact multiples.** Quarter Kelly is asserted to be exactly a quarter of
full Kelly and a doubled volatility to halve the size, with numbers chosen so
Decimal arithmetic is exact. `assert quarter < full` would pass for an
implementation that ignored `kelly_cap` and merely happened to round down.
"""

from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from omni.portfolio import sizing
from omni.portfolio.sizing import (
    DEFAULT_KELLY_FRACTION,
    kelly_fraction,
    payoff_ratio,
    size,
)

_SOURCE = Path(sizing.__file__).read_text()

_FOLDABLE_CALLS = {"Decimal", "float"}


def _fold(node: ast.AST) -> Decimal | None:
    """Evaluate a constant-only numeric expression, or None if it is not one.

    Covers the forms a probability can be written in -- a bare literal, a
    `Decimal(...)`/`float(...)` around one, and arithmetic between them -- so
    the 0.5 scan is not a substring match wearing a parser's clothes. Bare
    string constants are deliberately not folded: an error message containing a
    number is prose, not a value.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return None
        return Decimal(repr(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _fold(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _FOLDABLE_CALLS or len(node.args) != 1 or node.keywords:
            return None
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            try:
                return Decimal(arg.value)
            except InvalidOperation:
                return None
        return _fold(arg)
    if isinstance(node, ast.BinOp):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        except (InvalidOperation, ZeroDivisionError):
            return None
    return None


# entry 100, stop 90, target 120 -> b = 20 / 10 = 2.
# p = 0.6 -> f* = (0.6 * 2 - 0.4) / 2 = 0.4 of NAV at full Kelly.
LONG = {"entry": Decimal(100), "stop": Decimal(90), "target": Decimal(120)}
SHORT = {"entry": Decimal(100), "stop": Decimal(110), "target": Decimal(80)}
NAV = Decimal(100_000)
P = Decimal("0.6")


def _size(**overrides) -> Decimal:
    call = {
        "nav": NAV,
        "hit_rate": P,
        "max_position_pct_nav": Decimal(1),
        **LONG,
        **overrides,
    }
    return size(**call)


class TestPayoffRatio:
    def test_long_is_reward_over_risk(self):
        assert payoff_ratio(**LONG) == Decimal(2)

    def test_short_mirrors_the_long(self):
        assert payoff_ratio(**SHORT) == payoff_ratio(**LONG)

    def test_asymmetric_barriers(self):
        assert payoff_ratio(
            entry=Decimal(100), stop=Decimal(95), target=Decimal(115)
        ) == Decimal(3)

    def test_a_long_with_its_stop_above_entry_is_refused(self):
        with pytest.raises(ValueError, match="needs a stop below entry"):
            payoff_ratio(entry=Decimal(100), stop=Decimal(110), target=Decimal(120))

    def test_a_short_with_its_stop_below_entry_is_refused(self):
        with pytest.raises(ValueError, match="needs a stop above entry"):
            payoff_ratio(entry=Decimal(100), stop=Decimal(90), target=Decimal(80))

    def test_a_stop_at_entry_is_refused_rather_than_divided_by(self):
        with pytest.raises(ValueError, match="needs a stop below entry"):
            payoff_ratio(entry=Decimal(100), stop=Decimal(100), target=Decimal(120))

    def test_a_stop_within_tolerance_of_entry_is_refused(self):
        # 1e-17 away from entry: not equal to it, so an `== entry` guard misses
        # it and the division returns a payoff of 1e18.
        with pytest.raises(ValueError, match="needs a stop below entry"):
            payoff_ratio(
                entry=Decimal(100),
                stop=Decimal("99.99999999999999999"),
                target=Decimal(120),
            )

    def test_a_target_at_entry_has_no_direction(self):
        with pytest.raises(ValueError, match="no direction to size"):
            payoff_ratio(entry=Decimal(100), stop=Decimal(90), target=Decimal(100))

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(-1)])
    def test_non_positive_prices_are_refused(self, bad):
        with pytest.raises(ValueError, match="must be a positive price"):
            payoff_ratio(entry=bad, stop=Decimal(90), target=Decimal(120))
        with pytest.raises(ValueError, match="must be a positive price"):
            payoff_ratio(entry=Decimal(100), stop=bad, target=Decimal(120))

    def test_nan_entry_is_refused_by_its_own_check(self):
        # Decimal comparisons against NaN raise InvalidOperation rather than
        # returning False, so a refusal that relied on the range check would
        # surface as an ArithmeticError from somewhere unrelated.
        with pytest.raises(ValueError, match="not finite"):
            payoff_ratio(entry=Decimal("NaN"), stop=Decimal(90), target=Decimal(120))

    def test_infinite_target_is_refused(self):
        with pytest.raises(ValueError, match="not finite"):
            payoff_ratio(
                entry=Decimal(100), stop=Decimal(90), target=Decimal("Infinity")
            )

    def test_float_money_is_refused(self):
        with pytest.raises(TypeError, match="must be Decimal, not float"):
            payoff_ratio(entry=100.0, stop=Decimal(90), target=Decimal(120))


class TestKellyFraction:
    def test_the_formula(self):
        assert kelly_fraction(hit_rate=P, payoff=Decimal(2)) == Decimal("0.4")

    def test_a_coin_flip_at_even_money_sizes_to_exactly_zero(self):
        flip = kelly_fraction(hit_rate=Decimal(1) / Decimal(2), payoff=Decimal(1))
        assert flip == Decimal(0)

    def test_a_negative_edge_is_zero_and_never_a_short(self):
        # p = 0.3, b = 1 -> f* = -0.4. Returning the sign would have the caller
        # take the opposite side of a trade the analysis never claimed.
        result = kelly_fraction(hit_rate=Decimal("0.3"), payoff=Decimal(1))
        assert result == Decimal(0)

    def test_a_thin_negative_edge_is_still_zero(self):
        result = kelly_fraction(hit_rate=Decimal("0.4999"), payoff=Decimal(1))
        assert result == Decimal(0)

    def test_a_thin_positive_edge_survives(self):
        result = kelly_fraction(hit_rate=Decimal("0.5001"), payoff=Decimal(1))
        assert result == Decimal("0.0002")

    def test_certainty_stakes_everything(self):
        assert kelly_fraction(hit_rate=Decimal(1), payoff=Decimal(3)) == Decimal(1)

    def test_a_better_payoff_at_the_same_probability_sizes_larger(self):
        modest = kelly_fraction(hit_rate=P, payoff=Decimal(2))
        generous = kelly_fraction(hit_rate=P, payoff=Decimal(4))
        assert generous > modest

    def test_no_hit_rate_is_a_refusal_not_a_default(self):
        with pytest.raises(ValueError, match="hit_rate is required"):
            kelly_fraction(hit_rate=None, payoff=Decimal(2))

    def test_hit_rate_must_be_a_probability(self):
        with pytest.raises(ValueError, match="out of range"):
            kelly_fraction(hit_rate=Decimal("1.5"), payoff=Decimal(2))

    def test_nan_hit_rate_is_refused_by_its_own_check(self):
        with pytest.raises(ValueError, match="not finite"):
            kelly_fraction(hit_rate=Decimal("NaN"), payoff=Decimal(2))

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(-2)])
    def test_a_non_positive_payoff_is_refused_rather_than_divided_by(self, bad):
        with pytest.raises(ValueError, match="payoff must be positive"):
            kelly_fraction(hit_rate=P, payoff=bad)


class TestSize:
    def test_a_coin_flip_at_even_money_sizes_to_exactly_zero(self):
        quantity = _size(
            hit_rate=Decimal(1) / Decimal(2),
            entry=Decimal(100),
            stop=Decimal(90),
            target=Decimal(110),
        )
        assert quantity == Decimal(0)

    def test_a_negative_edge_sizes_to_zero_and_never_negative(self):
        quantity = _size(hit_rate=Decimal("0.2"))
        assert quantity == Decimal(0)

    def test_full_kelly_is_the_fraction_of_nav_the_formula_states(self):
        # f* = 0.4 -> 40,000 notional at an entry of 100 -> 400 units.
        assert _size(kelly_cap=Decimal(1)) == Decimal(400)

    def test_quarter_kelly_is_exactly_a_quarter_of_full_kelly(self):
        full = _size(kelly_cap=Decimal(1))
        quarter = _size(kelly_cap=Decimal("0.25"))
        assert quarter == Decimal(100)
        assert quarter * Decimal(4) == full

    def test_the_default_cap_is_quarter_kelly(self):
        assert DEFAULT_KELLY_FRACTION == Decimal("0.25")
        assert _size() == _size(kelly_cap=DEFAULT_KELLY_FRACTION)

    def test_the_result_is_a_quantity_not_a_notional(self):
        # Same barriers as a ratio, twice the price: the notional is unchanged
        # and the quantity halves.
        doubled = _size(entry=Decimal(200), stop=Decimal(180), target=Decimal(240))
        assert doubled == Decimal(50)
        assert doubled * Decimal(200) == _size() * Decimal(100)

    def test_a_short_sizes_like_its_mirrored_long(self):
        assert _size(**SHORT) == _size(**LONG)

    def test_size_scales_with_nav(self):
        assert _size(nav=Decimal(200_000)) == _size(nav=NAV) * Decimal(2)

    def test_the_nav_cap_binds_when_kelly_would_exceed_it(self):
        # Quarter Kelly wants 0.1 of NAV; the limit allows 0.05.
        clamped = _size(max_position_pct_nav=Decimal("0.05"))
        assert clamped == Decimal(50)
        assert clamped < _size(max_position_pct_nav=Decimal(1))

    def test_a_slack_cap_does_not_change_the_size(self):
        assert _size(max_position_pct_nav=Decimal("0.5")) == _size(
            max_position_pct_nav=Decimal(1)
        )

    def test_vol_targeting_reduces_size_as_volatility_rises(self):
        at_target = _size(volatility=Decimal("0.02"), vol_target=Decimal("0.02"))
        doubled = _size(volatility=Decimal("0.04"), vol_target=Decimal("0.02"))
        quadrupled = _size(volatility=Decimal("0.08"), vol_target=Decimal("0.02"))

        assert at_target == Decimal(100)
        assert doubled == Decimal(50)
        assert quadrupled == Decimal(25)
        assert at_target > doubled > quadrupled

    def test_vol_targeting_at_the_target_is_a_no_op(self):
        scaled = _size(volatility=Decimal("0.03"), vol_target=Decimal("0.03"))
        assert scaled == _size()

    def test_the_nav_cap_is_applied_after_vol_targeting(self):
        # Half the target volatility doubles the fraction to 0.2 of NAV; the
        # 0.15 limit is a ceiling on the position, not one more term.
        levered = _size(
            volatility=Decimal("0.01"),
            vol_target=Decimal("0.02"),
            max_position_pct_nav=Decimal("0.15"),
        )
        assert levered == Decimal(150)

    def test_vol_scaling_above_one_is_visible_when_the_cap_allows_it(self):
        levered = _size(
            volatility=Decimal("0.01"),
            vol_target=Decimal("0.02"),
            max_position_pct_nav=Decimal("0.5"),
        )
        assert levered == Decimal(200)

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal("-0.01")])
    def test_non_positive_volatility_is_refused(self, bad):
        with pytest.raises(ValueError, match="volatility must be positive"):
            _size(volatility=bad, vol_target=Decimal("0.02"))

    def test_a_volatility_below_tolerance_is_refused_not_divided_by(self):
        # Not zero, so an `== 0` guard misses it; dividing by it would multiply
        # the position by 2e16.
        with pytest.raises(ValueError, match="volatility must be positive"):
            _size(volatility=Decimal("1e-18"), vol_target=Decimal("0.02"))

    def test_nan_volatility_is_refused_by_its_own_check(self):
        with pytest.raises(ValueError, match="not finite"):
            _size(volatility=float("nan"), vol_target=Decimal("0.02"))

    def test_half_a_vol_target_is_refused(self):
        with pytest.raises(ValueError, match="both volatility and vol_target"):
            _size(volatility=Decimal("0.02"))
        with pytest.raises(ValueError, match="both volatility and vol_target"):
            _size(vol_target=Decimal("0.02"))

    def test_no_hit_rate_is_a_refusal_not_a_default(self):
        with pytest.raises(ValueError, match="hit_rate is required"):
            _size(hit_rate=None)

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"kelly_cap": Decimal(1)},
            {"volatility": Decimal("0.02"), "vol_target": Decimal("0.02")},
            {"max_position_pct_nav": Decimal("0.05")},
            dict(SHORT),
        ],
        ids=["plain", "full-kelly", "vol-targeted", "tight-cap", "short"],
    )
    def test_no_configuration_lets_a_missing_hit_rate_through(self, extra):
        # One passing `hit_rate=None` case says nothing about the others; the
        # rule is that *no* path through `size` returns a quantity without a
        # calibrated p, so every branch it has is asked the same question.
        with pytest.raises(ValueError, match="hit_rate is required"):
            _size(hit_rate=None, **extra)

    def test_size_refuses_a_missing_hit_rate_on_its_own(self, monkeypatch):
        """The refusal must be `size`'s, not one it happens to inherit.

        `size` delegates to `kelly_fraction`, which refuses `None` too, so
        deleting the guard inside `size` changes nothing observable today and
        every other test here stays green. It is still a hole: the rule is that
        no path through `size` yields a quantity without a calibrated p, and
        with its own guard gone that holds only while the collaborator keeps
        enforcing it. Standing in a `kelly_fraction` that hands back a fraction
        for `None` -- the exact substitution this module exists to refuse --
        makes the difference visible.
        """

        def substituting_kelly(*, hit_rate, payoff):
            return Decimal("0.4")

        monkeypatch.setattr(sizing, "kelly_fraction", substituting_kelly)

        # The stub is reached and really does produce a number, so a refusal
        # below can only have come from `size` itself.
        assert _size(kelly_cap=Decimal(1)) == Decimal(400)

        with pytest.raises(ValueError, match="hit_rate is required"):
            _size(hit_rate=None)

    def test_hit_rate_cannot_be_omitted(self):
        with pytest.raises(TypeError):
            size(
                nav=NAV,
                max_position_pct_nav=Decimal(1),
                **LONG,
            )

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(-1)])
    def test_non_positive_nav_is_refused(self, bad):
        with pytest.raises(ValueError, match="nav must be positive"):
            _size(nav=bad)

    def test_nan_nav_is_refused(self):
        with pytest.raises(ValueError, match="not finite"):
            _size(nav=Decimal("NaN"))

    def test_non_positive_entry_is_refused(self):
        with pytest.raises(ValueError, match="must be a positive price"):
            _size(entry=Decimal(0), stop=Decimal(-10), target=Decimal(20))

    def test_a_stop_on_the_wrong_side_is_refused(self):
        with pytest.raises(ValueError, match="needs a stop below entry"):
            _size(stop=Decimal(110))

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal("1.5"), Decimal(-1)])
    def test_kelly_cap_must_be_a_fraction_of_full_kelly(self, bad):
        with pytest.raises(ValueError, match="kelly_cap"):
            _size(kelly_cap=bad)

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(5), Decimal("-0.1")])
    def test_the_nav_cap_must_be_a_fraction_of_nav(self, bad):
        with pytest.raises(ValueError, match="max_position_pct_nav"):
            _size(max_position_pct_nav=bad)


class TestNoSubstitutedProbability:
    """The rule that cannot be left to a runtime test.

    `size(hit_rate=None)` raising says nothing about a default added later to
    a helper three functions down. The source itself is the assertion.
    """

    def test_no_function_gives_hit_rate_a_default(self):
        checked: list[str] = []
        for node in ast.walk(ast.parse(_SOURCE)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            positional = args.posonlyargs + args.args
            padding: list[ast.expr | None] = [None] * (
                len(positional) - len(args.defaults)
            )
            pairs = list(zip(positional, padding + list(args.defaults), strict=True))
            pairs += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
            for arg, default in pairs:
                if arg.arg != "hit_rate":
                    continue
                checked.append(node.name)
                assert default is None, f"{node.name} gives hit_rate a default"
        assert {"kelly_fraction", "size"} <= set(checked)

    def test_the_source_carries_no_substitute_probability(self):
        assert "0.5" not in _SOURCE

    def test_no_literal_in_the_module_evaluates_to_one_half(self):
        # The substring check above only catches a coin flip spelled `0.5`.
        # `.5`, `5e-1`, `Decimal("5e-1")`, `1 / 2` and `Decimal(1) / Decimal(2)`
        # all read as 0.5 at runtime and all slip past it, which is exactly how
        # a substituted probability would arrive if someone were routing around
        # the rule rather than breaking it by accident. This folds every numeric
        # literal and every constant-only arithmetic expression in the module
        # and refuses any that lands on one half.
        offenders = [
            (node.lineno, ast.unparse(node))
            for node in ast.walk(ast.parse(_SOURCE))
            if _fold(node) == Decimal("0.5")
        ]
        assert offenders == [], f"literals evaluating to 0.5: {offenders}"

    def test_the_one_half_scan_would_catch_a_disguised_default(self):
        # Proves the scan above is not vacuous: the same walk over a module
        # that hides a coin flip behind `Decimal("5e-1")` -- which contains no
        # "0.5" and is not an argument default, so neither of the other two
        # tests sees it -- reports it.
        disguised = 'from decimal import Decimal\n_FALLBACK = Decimal("5e-1")\n'
        assert "0.5" not in disguised
        found = [
            ast.unparse(node)
            for node in ast.walk(ast.parse(disguised))
            if _fold(node) == Decimal("0.5")
        ]
        assert found, "the 0.5 scan misses a disguised one half"
