"""Phase 3 tests: data quality validation rules (reject vs. fail)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.utils.exceptions import ValidationFailedError
from src.validation.rules import REJECTION_COLUMN, describe_rule, raise_if_failed, validate_dataframe


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [1, 2, 2, 4, 5],
            "email": ["a@x.com", "bad", None, "d@x.com", "e@x.com"],
            "amount": [10.0, -5.0, 20.0, 1000.0, None],
            "status": ["active", "Active", "closed", "unknown", "active"],
            "joined": ["2024-01-01", "2024-02-30", "2024-03-01", None, "2024-05-05"],
        }
    )


def by_id(result, rule_id):
    return next(r for r in result.results if r.rule_id == rule_id)


def test_reject_rules_split_rows(df):
    rules = [
        {"id": "nn", "type": "not_null", "column": "email"},
        {"id": "rx", "type": "pattern", "column": "email", "pattern": r"[^@\s]+@[^@\s]+\.[a-z]+"},
        {"id": "rg", "type": "range", "column": "amount", "min": 0, "max": 500},
        {"id": "av", "type": "allowed_values", "column": "status", "values": ["active", "closed"]},
        {"id": "dt", "type": "data_type", "column": "joined", "expected": "date"},
    ]
    result = validate_dataframe(df, rules)
    assert result.passed
    assert by_id(result, "nn").violations == 1
    assert by_id(result, "rx").violations == 1          # 'bad'; null allowed by pattern rule
    assert by_id(result, "rg").violations == 2          # -5 and 1000
    assert by_id(result, "av").violations == 1          # 'unknown' ('Active' passes case-insensitively)
    assert by_id(result, "dt").violations == 1          # 2024-02-30
    assert result.valid["id"].tolist() == [1, 5]
    assert len(result.rejected) == 3
    reasons = result.rejected[REJECTION_COLUMN].tolist()
    assert any("email is not empty" in r for r in reasons)
    assert any("0 ≤ amount ≤ 500" in r and "email matches" in r for r in reasons)


def test_unique_and_expression_rules(df):
    rules = [
        {"id": "u", "type": "unique", "columns": ["id"]},
        {"id": "e", "type": "expression", "expression": "amount >= 0 or is_null(amount)"},
    ]
    result = validate_dataframe(df, rules)
    assert by_id(result, "u").violations == 2
    assert by_id(result, "e").violations == 1
    assert set(result.valid["id"]) == {1, 4, 5}


def test_fail_rules_stop_pipeline(df):
    rules = [
        {"type": "required_columns", "columns": ["id", "customer_name"]},
        {"type": "range", "column": "amount", "min": 0, "on_fail": "fail"},
        {"type": "row_count", "min_rows": 10},
    ]
    result = validate_dataframe(df, rules)
    assert not result.passed
    assert [r.status for r in result.results] == ["failed", "failed", "failed"]
    with pytest.raises(ValidationFailedError) as info:
        raise_if_failed(result)
    assert "customer_name" in info.value.message


def test_rule_errors_and_disabled_rules(df):
    rules = [
        {"type": "not_null", "column": "nope"},
        {"type": "expression", "expression": "unknown_col > 1"},
        {"type": "not_null", "column": "email", "enabled": False},
    ]
    result = validate_dataframe(df, rules)
    assert [r.status for r in result.results] == ["error", "error", "skipped"]
    assert not result.passed


def test_rule_config_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        validate_dataframe(pd.DataFrame(), [{"type": "range", "column": "a"}])
    with pytest.raises(ValidationError):
        validate_dataframe(pd.DataFrame(), [{"type": "pattern", "column": "a", "pattern": "(unclosed"}])
    with pytest.raises(ValidationError):
        validate_dataframe(pd.DataFrame(), [{"type": "expression", "expression": "__import__('os')"}])
    assert describe_rule({"type": "range", "column": "a", "min": 1}) == "1 ≤ a ≤ ∞"
