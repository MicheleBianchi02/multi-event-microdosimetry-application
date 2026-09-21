"""
Safe arithmetic formula evaluation for the multi-measurement analysis.

A formula is a plain expression restricted to a whitelist of Python AST
nodes: the binary operators ``+ - * / **``, unary ``+``/``-``,
parentheses, numeric constants and variable names. Nothing else is
accepted — in particular no function calls, attribute access,
subscripts or comparisons — so a formula can never execute arbitrary
code. Function support is prepared (a name → callable table) but the
table ships empty; adding ``sqrt`` later is one dict entry.

Every variable evaluates to a ``(values, uncertainties)`` pair of numpy
arrays with one entry per measurement; constants carry zero
uncertainty. Uncertainties propagate to first order via automatic
differentiation: each node computes its partial derivative with
respect to every base variable, and the final uncertainty is
``σ_f = sqrt(Σ (∂f/∂v · σ_v)²)``. Unlike pairwise subexpression
combination, this treats a variable appearing twice in a formula
correctly — ``(yd / i) * i`` keeps exactly the uncertainty of ``yd``.

NaN and inf propagate silently (missing values, division by zero);
the caller counts and reports the excluded points.
"""

import ast
import difflib
from dataclasses import dataclass

import numpy as np

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)

# Reserved names that can never be used as variables (would shadow the
# Python constants parsed by ast); kept for clearer error messages.
_RESERVED_NAMES = ("True", "False", "None", "inf", "nan")

# Functions available inside formulas: name -> callable. Empty for now;
# registering one automatically enables the Call node for it.
FUNCTIONS = {}


class FormulaError(ValueError):
    """Raised when a formula cannot be parsed or evaluated."""


@dataclass
class FormulaResult:
    values: np.ndarray
    unc: np.ndarray


def referenced_names(expr: str) -> set:
    """Return the set of variable names used by the formula (no validation
    of unknown names — those are reported by validate()). Returns an empty
    set when the expression is not even parseable."""
    try:
        tree = ast.parse(_normalise(expr), mode="eval")
    except SyntaxError:
        return set()
    return {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }


def referenced_ids(expr: str) -> set:
    return referenced_names(expr)


def validate(expr: str, known_vars) -> list:
    """Check a formula against the known variable ids.

    Returns a list of human-readable problems (empty = valid). Never
    raises for user input; a bare syntax error is reported as a problem.
    """
    problems = []
    try:
        tree = ast.parse(_normalise(expr), mode="eval")
    except SyntaxError as exc:
        return [f"invalid syntax ({exc.msg})"]
    names = set()
    _check_node(tree.body, names, problems)
    known = set(known_vars)
    for name in sorted(names - known):
        hint = _suggest(name, known)
        problems.append(
            f"unknown variable '{name}'" + (f" — did you mean '{hint}'?" if hint else "")
        )
    return problems


def evaluate_formula(expr: str, variables) -> FormulaResult:
    """Evaluate a formula over the given variable arrays.

    Parameters
    ----------
    expr : str
        The formula text, e.g. ``"autocov_yd_ch1 / beam_current_nA"``.
    variables : mapping str -> (values, unc)
        One entry per variable name; values/unc are array-likes with one
        entry per measurement (scalars allowed).

    Raises
    ------
    FormulaError
        On invalid syntax, disallowed constructs or unknown variables.
    """
    problems = validate(expr, variables.keys())
    if problems:
        raise FormulaError("; ".join(problems))

    env = {
        name: (np.asarray(vals, dtype=float), np.asarray(uncs, dtype=float))
        for name, (vals, uncs) in variables.items()
    }
    tree = ast.parse(_normalise(expr), mode="eval")
    with np.errstate(all="ignore"):
        values, grads = _eval_node(tree.body, env)
    values = np.asarray(values, dtype=float)
    # σ_f² = Σ (∂f/∂v · σ_v)² over the variables actually referenced
    # (inf·0 → NaN warnings are expected for division-by-zero points;
    # those points are non-finite and excluded by the caller anyway)
    with np.errstate(invalid="ignore"):
        variance = np.zeros(np.shape(values), dtype=float)
        for name, deriv in grads.items():
            sigma = np.asarray(env[name][1], dtype=float)
            term = np.asarray(deriv, dtype=float) * sigma
            variance = variance + term**2  # broadcasts scalars up to values' shape
    return FormulaResult(values, np.sqrt(variance))


# --- internals -----------------------------------------------------------


def _normalise(expr: str) -> str:
    # Unicode minus and division signs from copy-pasted text
    return (
        expr.replace("−", "-")
        .replace("×", "*")
        .replace("÷", "/")
        .replace("·", "*")
    )


def _suggest(name: str, known) -> str:
    matches = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    return matches[0] if matches else ""


def _check_node(node, names, problems):
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            problems.append(
                f"operator '{_op_symbol(node.op)}' is not supported "
                "(allowed: + - * / **)"
            )
        _check_node(node.left, names, problems)
        _check_node(node.right, names, problems)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            problems.append("unary operator not supported (allowed: + -)")
        _check_node(node.operand, names, problems)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            problems.append(f"constant {node.value!r} is not a number")
    elif isinstance(node, ast.Name):
        if node.id in _RESERVED_NAMES:
            problems.append(f"'{node.id}' cannot be used as a variable")
        else:
            names.add(node.id)
    elif isinstance(node, ast.Call):
        fname = getattr(node.func, "id", None)
        if fname in FUNCTIONS:
            for arg in node.args:
                _check_node(arg, names, problems)
            if node.keywords:
                problems.append("keyword arguments are not supported")
        else:
            problems.append(
                f"function '{fname or '…'}' is not supported "
                "(only + - * / ** and parentheses are available)"
            )
    else:
        problems.append(
            f"'{type(node).__name__}' is not allowed in a formula "
            "(only numbers, variables, + - * / ** and parentheses)"
        )


_OP_SYMBOLS = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**",
    ast.BitXor: "^", ast.BitAnd: "&", ast.BitOr: "|", ast.Mod: "%",
    ast.FloorDiv: "//", ast.LShift: "<<", ast.RShift: ">>",
}


def _op_symbol(op) -> str:
    return _OP_SYMBOLS.get(type(op), type(op).__name__)


def _eval_node(node, env):
    """Evaluate a node to (values, grads) where grads maps each base
    variable name to ∂(node)/∂(variable) (sparse: only names the node
    depends on appear)."""
    if isinstance(node, ast.BinOp):
        lv, lg = _eval_node(node.left, env)
        rv, rg = _eval_node(node.right, env)
        return _apply_op(type(node.op), lv, lg, rv, rg)
    if isinstance(node, ast.UnaryOp):
        v, g = _eval_node(node.operand, env)
        if isinstance(node.op, ast.USub):
            return -v, {name: -d for name, d in g.items()}
        return v, g
    if isinstance(node, ast.Constant):
        return float(node.value), {}
    if isinstance(node, ast.Name):
        v, _ = env[node.id]
        return v, {node.id: np.ones_like(np.asarray(v, dtype=float))}
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[getattr(node.func, "id", "")]
        args = [_eval_node(a, env) for a in node.args]
        return fn(*args)
    raise FormulaError(
        f"'{type(node).__name__}' is not allowed in a formula"
    )


def _grad_add(g1, g2, sign=1.0):
    out = dict(g1)
    for name, d in g2.items():
        out[name] = out.get(name, 0.0) + sign * d
    return out


def _grad_scale(g, factor):
    return {name: d * factor for name, d in g.items()}


def _apply_op(op, lv, lg, rv, rg):
    if op is ast.Add:
        return lv + rv, _grad_add(lg, rg)
    if op is ast.Sub:
        return lv - rv, _grad_add(lg, rg, sign=-1.0)
    if op is ast.Mult:
        grads = _grad_add(_grad_scale(lg, rv), _grad_scale(rg, lv))
        return lv * rv, grads
    if op is ast.Div:
        grads = _grad_add(
            _grad_scale(lg, 1.0 / rv),          # ∂(l/r)/∂l = 1/r
            _grad_scale(rg, -lv / rv**2),       # ∂(l/r)/∂r = -l/r²
        )
        return lv / rv, grads
    if op is ast.Pow:
        val = lv**rv
        dl = rv * lv ** (rv - 1.0)              # ∂l^r/∂l
        dr = val * np.log(lv)                   # ∂l^r/∂r (NaN where invalid)
        grads = _grad_add(_grad_scale(lg, dl), _grad_scale(rg, dr))
        return val, grads
    raise FormulaError(f"operator '{_op_symbol(op)}' is not supported")
