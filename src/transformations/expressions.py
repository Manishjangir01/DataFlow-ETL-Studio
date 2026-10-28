"""Safe, vectorised expression evaluator for filters, calculated columns and rules.

Expressions use a small, Python-like syntax that is parsed with :mod:`ast` and checked
against a whitelist - nothing is passed to ``eval``, ``exec`` or ``DataFrame.query``.

Examples::

    price * quantity
    round(amount * 1.18, 2)
    status == "active" and age >= 18
    country in ["IN", "US"] or `order total` > 1000
    concat(first_name, " ", last_name)
    "adult" if age >= 18 else "minor"

Column names that are not valid identifiers are written in backticks.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.utils.exceptions import ExpressionError

MAX_EXPRESSION_LENGTH = 1000
MAX_NODES = 300
_BACKTICK = re.compile(r"`([^`]+)`")
_LITERAL_NAMES = {"true": True, "false": False, "null": None, "none": None, "True": True, "False": False, "None": None}


def _as_series(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series([value] * len(index), index=index, dtype=object if isinstance(value, str) or value is None else None)


def _num(value: Any, index: pd.Index) -> pd.Series:
    return pd.to_numeric(_as_series(value, index), errors="coerce")


def _text(value: Any, index: pd.Index) -> pd.Series:
    series = _as_series(value, index)
    return series.astype("string")


def _date(value: Any, index: pd.Index) -> pd.Series:
    import warnings

    series = _as_series(value, index)
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(series, errors="coerce", format="mixed")


def _bool(value: Any, index: pd.Index) -> pd.Series:
    series = _as_series(value, index)
    return series.fillna(False).astype(bool)


def _coalesce(index: pd.Index, *values: Any) -> pd.Series:
    result = _as_series(values[0], index).copy() if values else pd.Series(index=index, dtype=object)
    for value in values[1:]:
        result = result.where(result.notna(), _as_series(value, index))
    return result


def _concat(index: pd.Index, *values: Any) -> pd.Series:
    parts = [_text(v, index).fillna("") for v in values]
    result = parts[0] if parts else pd.Series("", index=index, dtype="string")
    for part in parts[1:]:
        result = result + part
    return result


# name -> (min args, max args, implementation(index, *args))
FUNCTIONS: dict[str, tuple[int, int, Callable[..., Any]]] = {
    "abs": (1, 1, lambda i, x: _num(x, i).abs()),
    "round": (1, 2, lambda i, x, n=0: _num(x, i).round(int(n))),
    "floor": (1, 1, lambda i, x: np.floor(_num(x, i))),
    "ceil": (1, 1, lambda i, x: np.ceil(_num(x, i))),
    "lower": (1, 1, lambda i, x: _text(x, i).str.lower()),
    "upper": (1, 1, lambda i, x: _text(x, i).str.upper()),
    "title": (1, 1, lambda i, x: _text(x, i).str.title()),
    "trim": (1, 1, lambda i, x: _text(x, i).str.strip()),
    "len": (1, 1, lambda i, x: _text(x, i).str.len()),
    "contains": (2, 2, lambda i, x, s: _text(x, i).str.contains(str(s), regex=False, na=False)),
    "startswith": (2, 2, lambda i, x, s: _text(x, i).str.startswith(str(s), na=False)),
    "endswith": (2, 2, lambda i, x, s: _text(x, i).str.endswith(str(s), na=False)),
    "replace": (3, 3, lambda i, x, a, b: _text(x, i).str.replace(str(a), str(b), regex=False)),
    "concat": (1, 20, _concat),
    "coalesce": (2, 20, _coalesce),
    "is_null": (1, 1, lambda i, x: _as_series(x, i).isna()),
    "not_null": (1, 1, lambda i, x: _as_series(x, i).notna()),
    "to_number": (1, 1, lambda i, x: _num(x, i)),
    "to_text": (1, 1, lambda i, x: _text(x, i)),
    "to_date": (1, 1, lambda i, x: _date(x, i)),
    "year": (1, 1, lambda i, x: _date(x, i).dt.year),
    "month": (1, 1, lambda i, x: _date(x, i).dt.month),
    "day": (1, 1, lambda i, x: _date(x, i).dt.day),
    "weekday": (1, 1, lambda i, x: _date(x, i).dt.day_name()),
    "days_between": (2, 2, lambda i, a, b: (_date(b, i) - _date(a, i)).dt.days),
    "today": (0, 0, lambda i: _as_series(pd.Timestamp.today().normalize(), i)),
    "min_of": (2, 20, lambda i, *xs: pd.concat([_num(x, i) for x in xs], axis=1).min(axis=1)),
    "max_of": (2, 20, lambda i, *xs: pd.concat([_num(x, i) for x in xs], axis=1).max(axis=1)),
}

FUNCTION_HELP = {
    "abs(x)": "absolute value", "round(x, digits)": "round a number", "floor(x) / ceil(x)": "round down / up",
    "lower(x) / upper(x) / title(x)": "change text case", "trim(x)": "remove surrounding spaces",
    "len(x)": "text length", "contains(x, 'abc')": "text contains", "startswith / endswith": "text starts / ends with",
    "replace(x, 'a', 'b')": "replace text", "concat(a, ' ', b)": "join text", "coalesce(a, b, ...)": "first non-empty value",
    "is_null(x) / not_null(x)": "missing checks", "to_number(x) / to_text(x) / to_date(x)": "conversions",
    "year(d) / month(d) / day(d) / weekday(d)": "date parts", "days_between(a, b)": "days from a to b",
    "today()": "today's date", "min_of(a, b) / max_of(a, b)": "row-wise min / max",
}

_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CMP_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}


class _Compiler:
    def __init__(self, df: pd.DataFrame, aliases: dict[str, str]) -> None:
        self.df = df
        self.index = df.index
        self.aliases = aliases

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expressions.")
        return method(node)

    def _eval_Expression(self, node: ast.Expression) -> Any:
        return self.eval(node.body)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ExpressionError(f"Unsupported value {node.value!r}.")

    def _eval_Name(self, node: ast.Name) -> Any:
        name = self.aliases.get(node.id, node.id)
        if name in self.df.columns:
            return self.df[name]
        if node.id in _LITERAL_NAMES:
            return _LITERAL_NAMES[node.id]
        available = ", ".join(map(str, list(self.df.columns)[:15]))
        raise ExpressionError(f"Unknown column '{name}'. Available columns: {available}.")

    def _eval_List(self, node: ast.List) -> list[Any]:
        values = [self.eval(e) for e in node.elts]
        if any(isinstance(v, pd.Series) for v in values):
            raise ExpressionError("Lists may only contain fixed values, e.g. [\"A\", \"B\"].")
        return values

    _eval_Tuple = _eval_List

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.eval(node.operand)
        if isinstance(node.op, ast.Not):
            return ~_bool(operand, self.index)
        if isinstance(node.op, ast.USub):
            return -_num(operand, self.index) if isinstance(operand, pd.Series) else -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ExpressionError("Unsupported unary operator.")

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError("Only + - * / // % ** are allowed.")
        left, right = self.eval(node.left), self.eval(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > 64:
            raise ExpressionError("Exponents larger than 64 are not allowed.")
        if isinstance(node.op, ast.Add) and (_is_text(left) or _is_text(right)):
            return _concat(self.index, left, right)
        left_n = _num(left, self.index) if isinstance(left, pd.Series) else left
        right_n = _num(right, self.index) if isinstance(right, pd.Series) else right
        try:
            result = op(left_n, right_n)
        except ZeroDivisionError:
            return pd.Series(np.nan, index=self.index)
        except TypeError as exc:
            raise ExpressionError(f"Cannot apply arithmetic to these values: {exc}") from exc
        if isinstance(result, pd.Series):
            result = result.replace([np.inf, -np.inf], np.nan)
        return result

    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [_bool(self.eval(v), self.index) for v in node.values]
        result = values[0]
        for value in values[1:]:
            result = (result & value) if isinstance(node.op, ast.And) else (result | value)
        return result

    def _eval_Compare(self, node: ast.Compare) -> Any:
        left = self.eval(node.left)
        result = pd.Series(True, index=self.index)
        for op_node, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            if isinstance(op_node, (ast.In, ast.NotIn)):
                if not isinstance(right, list):
                    raise ExpressionError("'in' must be followed by a list, e.g. status in [\"A\", \"B\"].")
                matched = _as_series(left, self.index).isin(right)
                part = ~matched if isinstance(op_node, ast.NotIn) else matched
            else:
                op = _CMP_OPS.get(type(op_node))
                if op is None:
                    raise ExpressionError("Unsupported comparison operator.")
                part = _compare(op, left, right, self.index)
            result = result & part
            left = right
        return result

    def _eval_IfExp(self, node: ast.IfExp) -> Any:
        condition = _bool(self.eval(node.test), self.index)
        when_true = _as_series(self.eval(node.body), self.index)
        when_false = _as_series(self.eval(node.orelse), self.index)
        return when_true.where(condition, when_false)

    def _eval_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ExpressionError("Only simple function calls like round(x, 2) are allowed.")
        name = node.func.id
        if name not in FUNCTIONS:
            raise ExpressionError(f"Unknown function '{name}'. Available: {', '.join(sorted(FUNCTIONS))}.")
        low, high, impl = FUNCTIONS[name]
        args = [self.eval(a) for a in node.args]
        if not low <= len(args) <= high:
            raise ExpressionError(f"{name}() takes {low}-{high} argument(s), got {len(args)}.")
        try:
            return impl(self.index, *args)
        except ExpressionError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface pandas errors in plain words
            raise ExpressionError(f"{name}() failed: {exc}") from exc


def _is_text(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, pd.Series):
        if pd.api.types.is_string_dtype(value) and not pd.api.types.is_numeric_dtype(value):
            sample = value.dropna().head(20)
            return not sample.empty and all(isinstance(v, str) for v in sample)
    return False


def _compare(op: Callable[[Any, Any], Any], left: Any, right: Any, index: pd.Index) -> pd.Series:
    left_s = _as_series(left, index)
    if right is None:
        if op is operator.eq:
            return left_s.isna()
        if op is operator.ne:
            return left_s.notna()
        raise ExpressionError("Use is_null(x) or not_null(x) to compare with null.")
    right_v = right
    # Compare numbers numerically when the column holds numbers stored as text.
    if isinstance(right, (int, float)) and not isinstance(right, bool) and not pd.api.types.is_numeric_dtype(left_s):
        left_s = pd.to_numeric(left_s, errors="coerce")
    if isinstance(right, pd.Series) and pd.api.types.is_numeric_dtype(left_s) and not pd.api.types.is_numeric_dtype(right):
        right_v = pd.to_numeric(right, errors="coerce")
    if isinstance(right, str) and pd.api.types.is_datetime64_any_dtype(left_s):
        right_v = pd.Timestamp(right)
        if left_s.dt.tz is not None and right_v.tzinfo is None:
            right_v = right_v.tz_localize(left_s.dt.tz)
    try:
        result = op(left_s, right_v)
    except TypeError as exc:
        raise ExpressionError(f"Cannot compare these values: {exc}") from exc
    return pd.Series(result, index=index).fillna(False).astype(bool)


def _prepare(expression: str) -> tuple[str, dict[str, str]]:
    if not expression or not expression.strip():
        raise ExpressionError("The expression is empty.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"Expressions are limited to {MAX_EXPRESSION_LENGTH} characters.")
    aliases: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        alias = f"__col_{len(aliases)}__"
        aliases[alias] = match.group(1)
        return alias

    return _BACKTICK.sub(replace, expression.strip()), aliases


def parse_expression(expression: str) -> tuple[ast.Expression, dict[str, str]]:
    """Parse and structurally validate an expression (no data needed)."""
    source, aliases = _prepare(expression)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Syntax error in expression at position {exc.offset}: {exc.msg}.") from exc
    count = sum(1 for _ in ast.walk(tree))
    if count > MAX_NODES:
        raise ExpressionError("The expression is too complex.")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Lambda, ast.comprehension, ast.Starred, ast.NamedExpr, ast.Dict, ast.Set, ast.JoinedStr)):
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expressions.")
        if isinstance(node, ast.Name) and node.id.startswith("__") and node.id not in aliases:
            raise ExpressionError("Names starting with '__' are not allowed.")
    return tree, aliases


def evaluate(expression: str, df: pd.DataFrame) -> pd.Series:
    """Evaluate ``expression`` for every row of ``df`` and return a Series."""
    tree, aliases = parse_expression(expression)
    result = _Compiler(df, aliases).eval(tree)
    return _as_series(result, df.index)


def evaluate_condition(expression: str, df: pd.DataFrame) -> pd.Series:
    """Evaluate a boolean expression; missing results count as False."""
    result = evaluate(expression, df)
    if result.dtype != bool:
        try:
            result = result.astype("boolean").fillna(False).astype(bool)
        except (TypeError, ValueError) as exc:
            raise ExpressionError("The condition must produce true/false values (use ==, >, and, or ...).") from exc
    return result
