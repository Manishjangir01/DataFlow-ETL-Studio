"""Phase 2 tests: schema detection and the data quality report."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.extraction.file_reader import read_file_bytes
from src.validation.profiler import count_duplicate_rows, profile_dataframe


def _column(report, name):
    return next(c for c in report.columns if c.name == name)


def test_profile_sample_customers(samples_dir):
    df = read_file_bytes((samples_dir / "customers.csv").read_bytes(), "csv").dataframe
    report = profile_dataframe(df)
    assert report.row_count == 21 and report.column_count == 9
    assert report.duplicate_rows == 1
    age = _column(report, "age")
    assert age.inferred_type == "integer" and age.invalid == 1 and "thirty" in age.invalid_examples
    assert age.missing == 1
    signup = _column(report, "signup_date")
    assert signup.inferred_type == "datetime" and "not a date" in signup.invalid_examples
    email = _column(report, "email")
    assert email.inferred_type == "email" and email.invalid == 1 and email.missing == 1
    assert _column(report, "lifetime_value").inferred_type == "float"
    assert 0 < report.quality_score < 100
    assert any("duplicate" in issue for issue in report.issues)


def test_profile_types_and_empty_columns():
    df = pd.DataFrame(
        {
            "flag": ["yes", "no", "yes", None],
            "n": [1.0, 2.0, np.nan, 4.0],
            "empty": [None, None, None, None],
            "when": pd.to_datetime(["2024-01-01", None, "2024-02-01", "2024-03-01"]),
            "const": ["a", "a", "a", "a"],
        }
    )
    report = profile_dataframe(df)
    assert _column(report, "flag").inferred_type == "boolean"
    assert _column(report, "n").inferred_type == "integer"
    assert _column(report, "empty").inferred_type == "empty"
    assert _column(report, "when").inferred_type == "datetime"
    assert any("completely empty" in i for i in report.issues)
    assert any("same value" in i for i in report.issues)
    frame = report.columns_frame()
    assert list(frame["Column"]) == list(df.columns)


def test_duplicates_with_unhashable_cells_and_empty_frame():
    df = pd.DataFrame({"a": [[1, 2], [1, 2], [3]], "b": [{"x": 1}, {"x": 1}, {}]})
    assert count_duplicate_rows(df) == 1
    report = profile_dataframe(pd.DataFrame())
    assert report.row_count == 0 and report.quality_score == 100.0
