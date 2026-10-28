"""Phase 3 tests: transformation steps, safe expressions and reproducibility."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.extraction.file_reader import read_file_bytes
from src.transformations.engine import apply_transformations
from src.transformations.expressions import evaluate, evaluate_condition
from src.transformations.steps import convert_series, standardize_name
from src.utils.exceptions import ExpressionError, TransformationError


@pytest.fixture
def customers(samples_dir) -> pd.DataFrame:
    return read_file_bytes((samples_dir / "customers.csv").read_bytes(), "csv").dataframe


def run(df, *steps):
    return apply_transformations(df, list(steps))


# --- Nulls and duplicates ------------------------------------------------------------

def test_remove_duplicates_all_columns_and_subset():
    df = pd.DataFrame({"id": [1, 1, 2, 2], "v": ["a", "a", "b", "c"]})
    assert len(run(df, {"type": "remove_duplicates"}).dataframe) == 3
    out = run(df, {"type": "remove_duplicates", "subset": ["id"], "keep": "last"}).dataframe
    assert out["v"].tolist() == ["a", "c"]


def test_handle_missing_strategies():
    df = pd.DataFrame({"a": [1.0, None, 3.0, None], "b": ["x", " ", None, "y"]})
    assert len(run(df, {"type": "handle_missing", "strategy": "drop_rows"}).dataframe) == 1
    assert len(run(df, {"type": "handle_missing", "strategy": "drop_rows", "how": "all"}).dataframe) == 3
    filled = run(df, {"type": "handle_missing", "strategy": "fill_value", "columns": ["b"], "fill_value": "unknown"}).dataframe
    assert filled["b"].tolist() == ["x", "unknown", "unknown", "y"]
    mean = run(df, {"type": "handle_missing", "strategy": "fill_mean", "columns": ["a"]}).dataframe
    assert mean["a"].tolist() == [1.0, 2.0, 3.0, 2.0]
    median_num = run(df, {"type": "handle_missing", "strategy": "fill_value", "columns": ["a"], "fill_value": "0"}).dataframe
    assert median_num["a"].tolist() == [1.0, 0.0, 3.0, 0.0]
    ffill = run(df, {"type": "handle_missing", "strategy": "fill_forward", "columns": ["a"]}).dataframe
    assert ffill["a"].tolist() == [1.0, 1.0, 3.0, 3.0]
    mode = run(pd.DataFrame({"c": ["k", "k", None, "z"]}), {"type": "handle_missing", "strategy": "fill_mode"}).dataframe
    assert mode["c"].tolist() == ["k", "k", "k", "z"]


def test_fill_value_required():
    with pytest.raises(TransformationError) as info:
        run(pd.DataFrame({"a": [None]}), {"type": "handle_missing", "strategy": "fill_value"})
    assert "step 1" in str(info.value)


# --- Type conversion ----------------------------------------------------------------

def test_cast_types_set_null_and_fail(customers):
    result = run(customers, {"type": "cast_types", "casts": {"age": "integer", "lifetime_value": "float"}})
    assert str(result.dataframe["age"].dtype) == "Int64"
    assert result.dataframe["age"].isna().sum() == 2   # one blank + 'thirty'
    assert any("thirty" in w for w in result.warnings)
    with pytest.raises(TransformationError) as info:
        run(customers, {"type": "cast_types", "casts": {"age": "integer"}, "on_error": "fail"})
    assert info.value.column == "age" and "'thirty'" in info.value.message


def test_convert_series_variants():
    ints, bad = convert_series(pd.Series(["1", "2.5", "x", None, " "]), "integer")
    assert ints.tolist()[:1] == [1] and bad.tolist() == [False, True, True, False, False]
    bools, bad = convert_series(pd.Series(["Yes", "no", "maybe", None]), "boolean")
    assert bools.tolist()[:2] == [True, False] and bad.tolist() == [False, False, True, False]
    dates, bad = convert_series(pd.Series(["2024-01-02", "02/03/2024", "nope"]), "date")
    assert dates.iloc[0] == dt.date(2024, 1, 2) and bad.tolist() == [False, False, True]
    text, _ = convert_series(pd.Series([1, None]), "string")
    assert text.iloc[0] == "1" and pd.isna(text.iloc[1])


def test_parse_dates_formats(customers):
    out = run(customers, {"type": "parse_dates", "columns": ["signup_date"], "output": "string", "output_format": "%d-%m-%Y"})
    df = out.dataframe
    assert df.loc[0, "signup_date"] == "15-01-2024"
    assert df.loc[1, "signup_date"] == "15-02-2024"   # 15/02/2024 parsed via mixed formats
    assert pd.isna(df.loc[5, "signup_date"])           # 'not a date'
    with pytest.raises(TransformationError):
        run(customers, {"type": "parse_dates", "columns": ["signup_date"], "on_error": "fail"})
    explicit = run(pd.DataFrame({"d": ["03/04/2024"]}), {"type": "parse_dates", "columns": ["d"], "input_format": "%d/%m/%Y"})
    assert explicit.dataframe.loc[0, "d"] == dt.date(2024, 4, 3)


# --- Text -----------------------------------------------------------------------------

def test_trim_case_replace(customers):
    out = run(
        customers,
        {"type": "trim_whitespace"},
        {"type": "text_case", "columns": ["first_name", "country", "status"], "case": "title"},
        {"type": "text_case", "columns": ["last_name"], "case": "sentence"},
        {"type": "replace_values", "column": "country", "mapping": {"Usa": "United States", "Uk": "United Kingdom"}},
    ).dataframe
    assert out.loc[0, "first_name"] == "Alice"
    assert out.loc[1, "last_name"] == "Smith"
    assert out.loc[4, "country"] == "United States"
    assert out.loc[1, "status"] == "Active"
    collapsed = run(pd.DataFrame({"x": ["a   b", "  ", 5]}), {"type": "trim_whitespace", "collapse_inner_spaces": True}).dataframe
    assert collapsed["x"].tolist()[0] == "a b" and collapsed["x"].iloc[1] is None and collapsed["x"].iloc[2] == 5


def test_rename_select_drop_and_standardize():
    df = pd.DataFrame({"Order Date": [1], "customerID": [2], "x": [3]})
    out = run(
        df,
        {"type": "standardize_column_names"},
        {"type": "rename_columns", "mapping": {"x": "amount"}},
        {"type": "select_columns", "columns": ["amount", "order_date"]},
    ).dataframe
    assert list(out.columns) == ["amount", "order_date"]
    assert standardize_name("customerID") == "customer_id" and standardize_name("2024 Sales") == "col_2024_sales"
    assert list(run(df, {"type": "drop_columns", "columns": ["x"]}).dataframe.columns) == ["Order Date", "customerID"]
    with pytest.raises(TransformationError) as info:
        run(df, {"type": "rename_columns", "mapping": {"x": "customerID"}})
    assert "duplicate" in info.value.message


def test_missing_column_error_names_step_and_column():
    with pytest.raises(TransformationError) as info:
        run(pd.DataFrame({"a": [1]}), {"type": "rename_columns", "mapping": {"a": "b"}}, {"type": "text_case", "columns": ["a"]})
    err = info.value
    assert err.step_index == 1 and err.column == "a" and "step 2" in str(err)


def test_invalid_step_settings_are_reported():
    with pytest.raises(TransformationError) as info:
        run(pd.DataFrame({"a": [1]}), {"type": "select_columns", "columns": []})
    assert info.value.step_index == 0


# --- Filters and calculated columns ---------------------------------------------------

def test_filter_conditions_and_expression(customers):
    base = run(customers, {"type": "trim_whitespace"}, {"type": "text_case", "columns": ["country"], "case": "lower"}).dataframe
    india = run(base, {"type": "filter_rows", "conditions": [{"column": "country", "operator": "==", "value": "india"}]}).dataframe
    assert len(india) == 6
    rich = run(base, {"type": "filter_rows", "conditions": [
        {"column": "lifetime_value", "operator": ">=", "value": "1000"},
        {"column": "status", "operator": "in", "value": "active, ACTIVE"},
    ]}).dataframe
    assert set(rich["customer_id"]) == {1001, 1005, 1009, 1013, 1017}
    either = run(base, {"type": "filter_rows", "match": "any", "conditions": [
        {"column": "email", "operator": "is_null"}, {"column": "age", "operator": "is_null"}]}).dataframe
    assert len(either) == 2
    expr = run(base, {"type": "filter_rows", "mode": "expression", "expression": "country in ['usa', 'canada'] and lifetime_value > 500"}).dataframe
    assert set(expr["customer_id"]) == {1002, 1005, 1013, 1015, 1019}
    contains = run(base, {"type": "filter_rows", "conditions": [{"column": "email", "operator": "contains", "value": "@EXAMPLE"}]}).dataframe
    assert len(contains) == 19


def test_calculated_columns():
    df = pd.DataFrame({"price": [10.0, 5.0], "qty": [2, 0], "first": ["a", None], "last": ["b", "c"], "d": ["2024-01-01", "2024-03-01"]})
    out = run(
        df,
        {"type": "calculated_column", "name": "total", "expression": "round(price * qty * 1.18, 2)"},
        {"type": "calculated_column", "name": "full", "expression": "concat(first, ' ', last)"},
        {"type": "calculated_column", "name": "size", "expression": "'big' if total > 10 else 'small'"},
        {"type": "calculated_column", "name": "per_unit", "expression": "price / qty"},
        {"type": "calculated_column", "name": "yr", "expression": "year(d)"},
        {"type": "calculated_column", "name": "label", "expression": "coalesce(first, 'n/a') + '-' + last"},
    ).dataframe
    assert out["total"].tolist() == [23.6, 0.0]
    assert out["full"].tolist() == ["a b", " c"]
    assert out["size"].tolist() == ["big", "small"]
    assert out.loc[0, "per_unit"] == 5.0 and pd.isna(out.loc[1, "per_unit"])
    assert out["yr"].tolist() == [2024, 2024]
    assert out["label"].tolist() == ["a-b", "n/a-c"]
    with pytest.raises(TransformationError):
        run(df, {"type": "calculated_column", "name": "price", "expression": "qty"})
    assert run(df, {"type": "calculated_column", "name": "price", "expression": "qty", "overwrite": True}).dataframe["price"].tolist() == [2, 0]


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hi')",
        "price.__class__",
        "[x for x in price]",
        "lambda: 1",
        "open('secrets.txt')",
        "price[0]",
        "f'{price}'",
        "price ** 1000",
        "(price := 5)",
    ],
)
def test_expressions_reject_unsafe_code(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression, pd.DataFrame({"price": [1.0]}))


def test_expression_backticks_and_errors():
    df = pd.DataFrame({"order total": [5, 15], "status": ["a", "b"]})
    assert evaluate_condition("`order total` > 10", df).tolist() == [False, True]
    assert evaluate_condition("not (status == 'a')", df).tolist() == [False, True]
    assert evaluate_condition("5 < `order total` <= 15", df).tolist() == [False, True]
    with pytest.raises(ExpressionError) as info:
        evaluate("missing_col + 1", df)
    assert "Unknown column" in info.value.message
    with pytest.raises(ExpressionError):
        evaluate("status +", df)
    with pytest.raises(ExpressionError):
        evaluate("unknown_fn(status)", df)


# --- Reproducibility and immutability --------------------------------------------------

def test_original_data_unchanged_and_runs_reproducible(customers):
    snapshot = customers.copy(deep=True)
    steps = [
        {"type": "trim_whitespace"},
        {"type": "remove_duplicates"},
        {"type": "cast_types", "casts": {"age": "integer"}},
        {"type": "handle_missing", "strategy": "fill_value", "columns": ["email"], "fill_value": "unknown@example.com"},
        {"type": "calculated_column", "name": "value_band", "expression": "'high' if lifetime_value >= 1000 else 'normal'"},
        {"type": "text_case", "columns": ["country"], "case": "upper", "enabled": False},
    ]
    first = apply_transformations(customers, steps)
    second = apply_transformations(customers, steps)
    pd.testing.assert_frame_equal(customers, snapshot)
    pd.testing.assert_frame_equal(first.dataframe, second.dataframe)
    assert len(first.dataframe) == 20 and first.rows_removed == 1
    assert first.reports[-1].message == "Skipped (disabled)"
    assert first.dataframe["country"].iloc[0] == "india"


def test_numpy_free_object_values_survive():
    df = pd.DataFrame({"v": [np.int64(1), "2"]})
    out = run(df, {"type": "cast_types", "casts": {"v": "integer"}}).dataframe
    assert out["v"].tolist() == [1, 2]
