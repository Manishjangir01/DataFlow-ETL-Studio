"""Schema detection and data quality profiling.

For each column the profiler reports the pandas dtype, a *probable* semantic type
(integer, float, boolean, datetime, string, ...), missing values, distinct values and
**invalid values** - entries that do not match the probable type, e.g. ``"thirty"`` in a
mostly numeric ``age`` column or ``"not a date"`` in a date column.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

_BOOL_TOKENS = {"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"}
_TYPE_THRESHOLD = 0.8          # share of values that must convert to call a type "probable"
_SAMPLE_LIMIT = 50_000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_HINT = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{4}-\d{2}-\d{2}T")
_NAME_HINTS_EMAIL = ("email", "e_mail", "mail")


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    inferred_type: str
    non_null: int
    missing: int
    missing_pct: float
    unique: int
    invalid: int
    invalid_examples: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    min: str | None = None
    max: str | None = None


@dataclass
class QualityReport:
    row_count: int
    column_count: int
    duplicate_rows: int
    missing_cells: int
    missing_pct: float
    invalid_cells: int
    quality_score: float
    columns: list[ColumnProfile]
    issues: list[str]
    sampled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def columns_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "Column": c.name,
                    "Detected type": c.inferred_type,
                    "Pandas dtype": c.dtype,
                    "Missing": c.missing,
                    "Missing %": c.missing_pct,
                    "Unique": c.unique,
                    "Invalid": c.invalid,
                    "Invalid examples": ", ".join(c.invalid_examples),
                    "Sample values": ", ".join(c.sample_values),
                }
                for c in self.columns
            ]
        )


def hashable_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame whose cells can be hashed (needed for duplicate detection)."""
    result = df
    for column in df.columns:
        if df[column].dtype == object:
            series = df[column]
            if series.map(lambda v: isinstance(v, (list, dict, set, np.ndarray))).any():
                if result is df:
                    result = df.copy()
                result[column] = series.map(lambda v: repr(v) if isinstance(v, (list, dict, set, np.ndarray)) else v)
    return result


def count_duplicate_rows(df: pd.DataFrame, subset: list[str] | None = None) -> int:
    if df.empty:
        return 0
    return int(hashable_frame(df).duplicated(subset=subset or None).sum())


def _to_datetime(values: pd.Series) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(values, errors="coerce", utc=True)
        missing = parsed.isna() & values.notna()
        if missing.any():
            retry = pd.to_datetime(values[missing], errors="coerce", utc=True, format="mixed")
            parsed = parsed.copy()
            parsed[missing] = retry
    return parsed


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _infer_text_column(name: str, values: pd.Series) -> tuple[str, pd.Series | None]:
    """Infer the probable type of a text column; returns (type, invalid_mask_on_values)."""
    text = values.astype(str).str.strip()
    non_empty = text[text != ""]
    if non_empty.empty:
        return "string", None
    lowered = non_empty.str.lower()
    if lowered.isin(_BOOL_TOKENS).mean() >= 0.98 and lowered.nunique() <= 4 and not lowered.isin({"0", "1"}).all():
        return "boolean", ~lowered.isin(_BOOL_TOKENS)
    numeric = pd.to_numeric(non_empty, errors="coerce")
    numeric_share = numeric.notna().mean()
    if numeric_share >= _TYPE_THRESHOLD:
        valid = numeric.dropna()
        integral = bool((valid == valid.round()).all()) if not valid.empty else False
        return ("integer" if integral else "float"), numeric.isna()
    if non_empty.str.contains(_DATE_HINT).mean() >= 0.5:
        parsed = _to_datetime(non_empty)
        if parsed.notna().mean() >= _TYPE_THRESHOLD:
            return "datetime", parsed.isna()
    if any(hint in name.lower() for hint in _NAME_HINTS_EMAIL):
        matches = non_empty.str.match(_EMAIL)
        if matches.mean() >= 0.5:
            return "email", ~matches
    return "string", None


def profile_column(name: str, series: pd.Series) -> ColumnProfile:
    total = len(series)
    non_null_values = series.dropna()
    if series.dtype == object:
        non_null_values = non_null_values[non_null_values.astype(str).str.strip() != ""]
    missing = total - len(non_null_values)
    try:
        unique = int(non_null_values.nunique())
    except TypeError:
        unique = int(non_null_values.astype(str).nunique())

    invalid_mask = None
    inferred: str
    if pd.api.types.is_bool_dtype(series):
        inferred = "boolean"
    elif pd.api.types.is_integer_dtype(series):
        inferred = "integer"
    elif pd.api.types.is_float_dtype(series):
        valid = non_null_values
        inferred = "integer" if not valid.empty and bool((valid == valid.round()).all()) else "float"
        if np.isinf(valid.astype(float)).any():
            invalid_mask = np.isinf(valid.astype(float))
    elif pd.api.types.is_datetime64_any_dtype(series):
        inferred = "datetime"
    elif non_null_values.empty:
        inferred = "empty"
    else:
        inferred, invalid_mask = _infer_text_column(name, non_null_values)

    invalid = int(invalid_mask.sum()) if invalid_mask is not None else 0
    invalid_examples: list[str] = []
    if invalid:
        examples = non_null_values[invalid_mask.reindex(non_null_values.index, fill_value=False)] if isinstance(invalid_mask, pd.Series) else non_null_values[invalid_mask]
        invalid_examples = [_short(v) for v in pd.unique(examples.astype(str))[:5]]

    sample_values = [_short(v) for v in pd.unique(non_null_values.astype(str))[:5]]
    minimum = maximum = None
    if inferred in ("integer", "float") and not non_null_values.empty:
        numbers = pd.to_numeric(non_null_values, errors="coerce").dropna()
        if not numbers.empty:
            minimum, maximum = _short(numbers.min()), _short(numbers.max())
    elif inferred == "datetime" and not non_null_values.empty:
        dates = non_null_values if pd.api.types.is_datetime64_any_dtype(non_null_values) else _to_datetime(non_null_values)
        dates = dates.dropna()
        if not dates.empty:
            minimum, maximum = _short(dates.min()), _short(dates.max())

    return ColumnProfile(
        name=str(name),
        dtype=str(series.dtype),
        inferred_type=inferred,
        non_null=len(non_null_values),
        missing=missing,
        missing_pct=round(100.0 * missing / total, 2) if total else 0.0,
        unique=unique,
        invalid=invalid,
        invalid_examples=invalid_examples,
        sample_values=sample_values,
        min=minimum,
        max=maximum,
    )


def profile_dataframe(df: pd.DataFrame) -> QualityReport:
    """Build a data quality report for ``df`` (large frames are sampled)."""
    rows, cols = df.shape
    sampled = rows > _SAMPLE_LIMIT
    work = df.sample(_SAMPLE_LIMIT, random_state=7) if sampled else df
    profiles = [profile_column(str(c), work[c]) for c in work.columns]
    scale = rows / len(work) if len(work) else 1.0

    duplicates = count_duplicate_rows(df)
    missing_cells = int(round(sum(p.missing for p in profiles) * scale))
    invalid_cells = int(round(sum(p.invalid for p in profiles) * scale))
    total_cells = rows * cols
    missing_ratio = missing_cells / total_cells if total_cells else 0.0
    invalid_ratio = invalid_cells / total_cells if total_cells else 0.0
    duplicate_ratio = duplicates / rows if rows else 0.0
    score = 100.0 * (1 - min(1.0, 0.45 * missing_ratio * 4 + 0.3 * duplicate_ratio * 4 + 0.25 * invalid_ratio * 4))

    issues: list[str] = []
    if rows == 0:
        issues.append("The dataset has no rows.")
    if duplicates:
        issues.append(f"{duplicates:,} duplicate row(s) found.")
    for p in profiles:
        if p.missing_pct == 100.0 and rows:
            issues.append(f"Column '{p.name}' is completely empty.")
        elif p.missing:
            issues.append(f"Column '{p.name}' has {p.missing:,} missing value(s) ({p.missing_pct}%).")
        if p.invalid:
            examples = ", ".join(p.invalid_examples[:3])
            issues.append(
                f"Column '{p.name}' looks like {p.inferred_type} but has {p.invalid:,} invalid value(s): {examples}."
            )
        if p.unique == 1 and rows > 1 and p.missing == 0:
            issues.append(f"Column '{p.name}' has the same value in every row.")

    return QualityReport(
        row_count=rows,
        column_count=cols,
        duplicate_rows=duplicates,
        missing_cells=missing_cells,
        missing_pct=round(100.0 * missing_ratio, 2),
        invalid_cells=invalid_cells,
        quality_score=round(max(0.0, score), 1),
        columns=profiles,
        issues=issues,
        sampled=sampled,
    )
