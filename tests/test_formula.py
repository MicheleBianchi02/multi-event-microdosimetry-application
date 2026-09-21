"""
Tests for the multi-analysis formula engine (core/formula.py): the AST
whitelist (no code execution, precise error messages), precedence,
NaN/inf propagation and first-order uncertainty propagation.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.formula import (  # noqa: E402
    FormulaError,
    evaluate_formula,
    referenced_names,
    validate,
)


def env(**kwargs):
    """Build a variables mapping {name: (values, unc)} from kwargs."""
    return {k: (np.asarray(v[0], dtype=float), np.asarray(v[1], dtype=float)) for k, v in kwargs.items()}


# --- validation -----------------------------------------------------------

def test_valid_formula_passes():
    problems = validate("autocov_yd_ch1 / beam_current_nA", {"autocov_yd_ch1", "beam_current_nA"})
    assert problems == []


def test_unknown_variable_suggests_close_match():
    problems = validate("autocov_yd_ch1", {"autocov_yb_ch1", "beam_current_nA"})
    assert len(problems) == 1
    assert "unknown variable 'autocov_yd_ch1'" in problems[0]
    assert "autocov_yb_ch1" in problems[0]


def test_syntax_error_reported_not_raised():
    problems = validate("a +* b", {"a", "b"})
    assert problems and "syntax" in problems[0].lower()


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",       # code execution
        "a and b",                # boolean logic
        "a < b",                  # comparison
        "a[0]",                   # subscript
        "a.b",                    # attribute access
        "sqrt(a)",                # functions not enabled yet
        "'text' + a",             # string constant
        "a ^ 2",                  # caret is XOR, not power
        "a; b",                   # two statements
        "lambda x: x",            # lambda
    ],
)
def test_forbidden_constructs_rejected(expr):
    problems = validate(expr, {"a", "b"})
    assert problems, f"{expr} should be rejected"


def test_caret_error_message_mentions_power_operator():
    problems = validate("a ^ 2", {"a"})
    assert any("'^'" in p and "**" in p for p in problems)


def test_unary_minus_and_constants_valid():
    assert validate("-a + 2.5", {"a"}) == []
    assert validate("-(a + b) * 1e-3", {"a", "b"}) == []


def test_unicode_operators_normalised():
    assert validate("a × b − 1", {"a", "b"}) == []
    assert validate("a ÷ 2", {"a"}) == []


# --- evaluation -----------------------------------------------------------

def test_precedence_and_parentheses():
    e = env(a=([2.0], [0.0]))
    assert evaluate_formula("a + 2 * 3", e).values[0] == pytest.approx(8.0)
    assert evaluate_formula("(a + 2) * 3", e).values[0] == pytest.approx(12.0)
    assert evaluate_formula("a ** 2 ** 3", e).values[0] == pytest.approx(256.0)  # right assoc
    assert evaluate_formula("-a + 4", e).values[0] == pytest.approx(2.0)


def test_evaluation_over_arrays():
    e = env(
        yd=([1.0, 2.0, 4.0], [0.1, 0.2, 0.4]),
        i=([2.0, 2.0, 4.0], [0.0, 0.0, 0.0]),
    )
    res = evaluate_formula("yd / i", e)
    assert res.values == pytest.approx([0.5, 1.0, 1.0])
    assert res.unc == pytest.approx([0.05, 0.1, 0.1])


def test_evaluate_unknown_variable_raises():
    with pytest.raises(FormulaError):
        evaluate_formula("nope + 1", env(a=([1.0], [0.0])))


def test_nan_and_division_by_zero_propagate_silently():
    e = env(a=([1.0, np.nan], [0.0, 0.0]), b=([0.0, 1.0], [0.0, 0.0]))
    res = evaluate_formula("a / b", e)
    assert np.isinf(res.values[0])
    assert np.isnan(res.values[1])


# --- uncertainty propagation ----------------------------------------------

def test_propagation_add_sub():
    e = env(a=([3.0], [1.0]), b=([4.0], [2.0]))
    assert evaluate_formula("a + b", e).unc[0] == pytest.approx(np.sqrt(5))
    assert evaluate_formula("a - b", e).unc[0] == pytest.approx(np.sqrt(5))


def test_propagation_mul():
    e = env(a=([3.0], [0.1]), b=([4.0], [0.5]))
    # sigma = sqrt((b*sa)^2 + (a*sb)^2) = sqrt(0.16 + 2.25)
    assert evaluate_formula("a * b", e).unc[0] == pytest.approx(np.sqrt(0.4**2 + 1.5**2))


def test_propagation_div():
    e = env(a=([10.0], [1.0]), b=([2.0], [0.1]))
    # f = 5; sigma = sqrt((1/2)^2 + (10*0.1/4)^2) = sqrt(0.25+0.0625)
    res = evaluate_formula("a / b", e)
    assert res.values[0] == pytest.approx(5.0)
    assert res.unc[0] == pytest.approx(np.sqrt(0.25 + 0.0625))


def test_propagation_pow_with_constant_exponent():
    e = env(a=([2.0], [0.1]))
    # f = a^2 = 4; df/da = 2a = 4 -> sigma = 0.4
    res = evaluate_formula("a ** 2", e)
    assert res.values[0] == pytest.approx(4.0)
    assert res.unc[0] == pytest.approx(0.4, rel=1e-6)


def test_propagation_exact_zero_for_pure_parameters():
    e = env(depth=([1.0, 2.0], [0.0, 0.0]), yd=([3.0, 6.0], [0.3, 0.6]))
    res = evaluate_formula("yd * depth", e)
    assert res.unc == pytest.approx([0.3, 1.2])


def test_nested_formula_propagation():
    # (yd/i) * i must return yd with yd's uncertainty
    e = env(yd=([3.0], [0.3]), i=([2.0], [0.2]))
    res = evaluate_formula("(yd / i) * i", e)
    assert res.values[0] == pytest.approx(3.0)
    assert res.unc[0] == pytest.approx(0.3, rel=1e-6)


# --- referenced names ------------------------------------------------------

def test_referenced_names():
    assert referenced_names("a + b * (a - c)") == {"a", "b", "c"}
    assert referenced_names("broken (((") == set()
