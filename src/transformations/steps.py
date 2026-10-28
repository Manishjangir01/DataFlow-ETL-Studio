"""Transformation step definitions (validated, JSON-serialisable and reproducible).

Each step is a pydantic model with a ``type`` discriminator, so a pipeline's list of
steps can be stored as JSON and replayed exactly. ``apply_step`` returns a *new*
DataFrame; the input is never modified.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Union

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from src.transformations.expressions import evaluate, evaluate_condition, parse_expression
from src.utils.exceptions import ExpressionError, TransformationError

TargetType = Literal["string", "integer", "float", "boolean", "datetime", "date"]
_TRUE_TOKENS = {"true", "t", "yes", "y", "1", "on"}
_FALSE_TOKENS = {"false", "f", "no", "n", "0", "off"}
_MAX_ERROR_EXAMPLES = 5


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    note: str | None = Field(default=None, max_length=200)


class SelectColumns(_StepBase):
    """Keep only these columns, in this order."""

    type: Literal["select_columns"] = "select_columns"
    columns: list[str] = Field(min_length=1)


class DropColumns(_StepBase):
    type: Literal["drop_columns"] = "drop_columns"
    columns: list[str] = Field(min_length=1)


class RenameColumns(_StepBase):
    type: Literal["rename_columns"] = "rename_columns"
    mapping: dict[str, str] = Field(min_length=1)

    @field_validator("mapping")
    @classmethod
    def _non_empty_names(cls, mapping: dict[str, str]) -> dict[str, str]:
        clean = {}
        for old, new in mapping.items():
            new = str(new).strip()
            if not new:
                raise ValueError(f"The new name for '{old}' is empty.")
            clean[str(old)] = new
        if len(set(clean.values())) != len(clean):
            raise ValueError("Two columns cannot be renamed to the same name.")
        return clean


class StandardizeColumnNames(_StepBase):
    """Make column names database friendly, e.g. 'Order Date' -> 'order_date'."""

    type: Literal["standardize_column_names"] = "standardize_column_names"
    style: Literal["snake_case", "lower"] = "snake_case"


class RemoveDuplicates(_StepBase):
    type: Literal["remove_duplicates"] = "remove_duplicates"
    subset: list[str] = Field(default_factory=list)   # empty = compare all columns
    keep: Literal["first", "last"] = "first"


class HandleMissing(_StepBase):
    type: Literal["handle_missing"] = "handle_missing"
    columns: list[str] = Field(default_factory=list)  # empty = all columns
    strategy: Literal[
        "drop_rows", "fill_value", "fill_mean", "fill_median", "fill_mode", "fill_forward", "fill_backward"
    ] = "drop_rows"
    fill_value: str | float | int | bool | None = None
    how: Literal["any", "all"] = "any"                 # for drop_rows
    treat_blank_as_missing: bool = True


class CastTypes(_StepBase):
    type: Literal["cast_types"] = "cast_types"
    casts: dict[str, TargetType] = Field(min_length=1)
    on_error: Literal["set_null", "fail"] = "set_null"


class ParseDates(_StepBase):
    type: Literal["parse_dates"] = "parse_dates"
    columns: list[str] = Field(min_length=1)
    input_format: str | None = None                   # e.g. %d/%m/%Y ; None = auto-detect
    dayfirst: bool = False
    output: Literal["datetime", "date", "string"] = "date"
    output_format: str = "%Y-%m-%d"
    on_error: Literal["set_null", "fail"] = "set_null"

    @field_validator("input_format", "output_format")
    @classmethod
    def _format_ok(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if len(value) > 64:
            raise ValueError("Date formats are limited to 64 characters.")
        return value


class TrimWhitespace(_StepBase):
    type: Literal["trim_whitespace"] = "trim_whitespace"
    columns: list[str] = Field(default_factory=list)  # empty = all text columns
    collapse_inner_spaces: bool = False
    blank_to_null: bool = True


class TextCase(_StepBase):
    type: Literal["text_case"] = "text_case"
    columns: list[str] = Field(min_length=1)
    case: Literal["lower", "upper", "title", "sentence"] = "lower"


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    operator: Literal[
        "==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "starts_with", "ends_with",
        "in", "not_in", "is_null", "not_null",
    ]
    value: Any = None


class FilterRows(_StepBase):
    """Keep only rows matching the conditions (or the expression)."""

    type: Literal["filter_rows"] = "filter_rows"
    mode: Literal["conditions", "expression"] = "conditions"
    conditions: list[Condition] = Field(default_factory=list)
    match: Literal["all", "any"] = "all"
    expression: str | None = None

    @field_validator("expression")
    @classmethod
    def _expression_ok(cls, value: str | None) -> str | None:
        if value:
            try:
                parse_expression(value)
            except ExpressionError as exc:
                raise ValueError(exc.message) from exc
        return value


class CalculatedColumn(_StepBase):
    type: Literal["calculated_column"] = "calculated_column"
    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1)
    overwrite: bool = False

    @field_validator("expression")
    @classmethod
    def _expression_ok(cls, value: str) -> str:
        try:
            parse_expression(value)
        except ExpressionError as exc:
            raise ValueError(exc.message) from exc
        return value


class ReplaceValues(_StepBase):
    type: Literal["replace_values"] = "replace_values"
    column: str
    mapping: dict[str, str] = Field(min_length=1)
    case_sensitive: bool = False


TransformStep = Annotated[
    Union[
        SelectColumns, DropColumns, RenameColumns, StandardizeColumnNames, RemoveDuplicates,
        HandleMissing, CastTypes, ParseDates, TrimWhitespace, TextCase, FilterRows,
        CalculatedColumn, ReplaceValues,
    ],
    Field(discriminator="type"),
]
STEP_ADAPTER: TypeAdapter[Any] = TypeAdapter(TransformStep)
STEP_LIST_ADAPTER: TypeAdapter[list[Any]] = TypeAdapter(list[TransformStep])

STEP_LABELS: dict[str, str] = {
    "remove_duplicates": "Remove duplicate rows",
    "handle_missing": "Handle missing values",
    "rename_columns": "Rename columns",
    "standardize_column_names": "Standardize column names",
    "cast_types": "Change data types",
    "parse_dates": "Parse and standardize dates",
    "trim_whitespace": "Trim whitespace",
    "text_case": "Standardize text case",
    "replace_values": "Replace values",
    "filter_rows": "Filter rows",
    "select_columns": "Select and reorder columns",
    "drop_columns": "Remove columns",
    "calculated_column": "Create calculated column",
}


def parse_step(data: dict[str, Any] | BaseModel) -> Any:
    """Validate a step dictionary (raises pydantic ``ValidationError``)."""
    return data if isinstance(data, BaseModel) else STEP_ADAPTER.validate_python(data)


def describe_step(step: Any) -> str:
    """One-line human description of a step."""
    s = parse_step(step)
    t = s.type
    if t == "select_columns":
        return f"Keep {len(s.columns)} column(s): {', '.join(s.columns[:6])}{'…' if len(s.columns) > 6 else ''}"
    if t == "drop_columns":
        return f"Remove {', '.join(s.columns)}"
    if t == "rename_columns":
        return ", ".join(f"{a} → {b}" for a, b in s.mapping.items())
    if t == "standardize_column_names":
        return f"Column names to {s.style}"
    if t == "remove_duplicates":
        return f"Compare {', '.join(s.subset) if s.subset else 'all columns'}, keep {s.keep}"
    if t == "handle_missing":
        target = ", ".join(s.columns) if s.columns else "all columns"
        extra = f" = {s.fill_value!r}" if s.strategy == "fill_value" else ""
        return f"{s.strategy.replace('_', ' ')}{extra} in {target}"
    if t == "cast_types":
        return ", ".join(f"{c} → {ty}" for c, ty in s.casts.items())
    if t == "parse_dates":
        return f"{', '.join(s.columns)} → {s.output}" + (f" ({s.output_format})" if s.output == "string" else "")
    if t == "trim_whitespace":
        return ", ".join(s.columns) if s.columns else "all text columns"
    if t == "text_case":
        return f"{s.case} case: {', '.join(s.columns)}"
    if t == "replace_values":
        return f"{s.column}: " + ", ".join(f"{a} → {b}" for a, b in list(s.mapping.items())[:4])
    if t == "filter_rows":
        if s.mode == "expression":
            return f"Keep rows where {s.expression}"
        joiner = " and " if s.match == "all" else " or "
        return "Keep rows where " + joiner.join(f"{c.column} {c.operator} {c.value!r}" for c in s.conditions)
    if t == "calculated_column":
        return f"{s.name} = {s.expression}"
    return t


# ---------------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------------

@dataclass
class StepOutcome:
    dataframe: pd.DataFrame
    message: str
    warnings: list[str] = field(default_factory=list)


def _require_columns(df: pd.DataFrame, columns: list[str], ctx: dict[str, Any]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        available = ", ".join(map(str, list(df.columns)[:20]))
        raise TransformationError(
            f"Column(s) not found: {', '.join(missing)}. Available: {available}.",
            column=missing[0],
            hint="An earlier step may have renamed or removed this column.",
            **ctx,
        )


def _blank_mask(series: pd.Series) -> pd.Series:
    mask = series.isna()
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        mask = mask | series.astype("string").str.strip().eq("").fillna(False)
    return mask


def _select(df, s: SelectColumns, ctx):
    _require_columns(df, s.columns, ctx)
    return StepOutcome(df[list(s.columns)].copy(), f"Kept {len(s.columns)} of {len(df.columns)} columns")


def _drop(df, s: DropColumns, ctx):
    _require_columns(df, s.columns, ctx)
    return StepOutcome(df.drop(columns=s.columns), f"Removed {len(s.columns)} column(s)")


def _rename(df, s: RenameColumns, ctx):
    _require_columns(df, list(s.mapping), ctx)
    result_columns = [s.mapping.get(c, c) for c in df.columns]
    duplicates = sorted({c for c in result_columns if result_columns.count(c) > 1})
    if duplicates:
        raise TransformationError(f"Renaming would create duplicate column names: {', '.join(duplicates)}.", **ctx)
    return StepOutcome(df.rename(columns=s.mapping), f"Renamed {len(s.mapping)} column(s)")


def standardize_name(name: str, style: str = "snake_case") -> str:
    text = str(name).strip()
    if style == "lower":
        return text.lower()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    if not text:
        text = "column"
    if text[0].isdigit():
        text = f"col_{text}"
    return text[:63]


def _standardize_names(df, s: StandardizeColumnNames, ctx):
    new_names: list[str] = []
    for column in df.columns:
        base = standardize_name(column, s.style)
        candidate, n = base, 2
        while candidate in new_names:
            candidate = f"{base}_{n}"
            n += 1
        new_names.append(candidate)
    changed = sum(1 for a, b in zip(df.columns, new_names) if a != b)
    result = df.copy()
    result.columns = new_names
    return StepOutcome(result, f"Standardized {changed} column name(s)")


def _dedupe(df, s: RemoveDuplicates, ctx):
    from src.validation.profiler import hashable_frame

    if s.subset:
        _require_columns(df, s.subset, ctx)
    mask = hashable_frame(df).duplicated(subset=s.subset or None, keep=s.keep)
    removed = int(mask.sum())
    return StepOutcome(df.loc[~mask].reset_index(drop=True), f"Removed {removed} duplicate row(s)")


def _missing(df, s: HandleMissing, ctx):
    columns = s.columns or list(df.columns)
    _require_columns(df, columns, ctx)
    result = df.copy()
    if s.treat_blank_as_missing:
        for c in columns:
            blank = _blank_mask(result[c]) & result[c].notna()
            if blank.any():
                result[c] = result[c].mask(blank, None)
    missing_before = int(result[columns].isna().sum().sum())
    if s.strategy == "drop_rows":
        mask = result[columns].isna()
        drop = mask.any(axis=1) if s.how == "any" else mask.all(axis=1)
        result = result.loc[~drop].reset_index(drop=True)
        return StepOutcome(result, f"Dropped {int(drop.sum())} row(s) with missing values")
    warnings_out: list[str] = []
    for c in columns:
        series = result[c]
        if not series.isna().any():
            continue
        if s.strategy == "fill_value":
            if s.fill_value is None:
                raise TransformationError("Enter a value to fill missing cells with.", column=c, **ctx)
            fill = s.fill_value
            if pd.api.types.is_numeric_dtype(series) and isinstance(fill, str):
                numeric = pd.to_numeric(pd.Series([fill]), errors="coerce").iloc[0]
                if pd.isna(numeric):
                    result[c] = series.astype(object).fillna(fill)
                    warnings_out.append(f"'{c}' is numeric; filling with text made it a text column.")
                    continue
                fill = numeric
            result[c] = series.fillna(fill) if series.dtype != object else series.where(series.notna(), fill)
        elif s.strategy in ("fill_mean", "fill_median"):
            numbers = pd.to_numeric(series, errors="coerce")
            if numbers.notna().sum() == 0:
                raise TransformationError("Mean/median needs a numeric column with some values.", column=c, **ctx)
            value = numbers.mean() if s.strategy == "fill_mean" else numbers.median()
            result[c] = numbers.fillna(value)
        elif s.strategy == "fill_mode":
            modes = series.dropna().mode()
            if not modes.empty:
                result[c] = series.where(series.notna(), modes.iloc[0])
        elif s.strategy == "fill_forward":
            result[c] = series.ffill()
        elif s.strategy == "fill_backward":
            result[c] = series.bfill()
    missing_after = int(result[columns].isna().sum().sum())
    return StepOutcome(result, f"Filled {missing_before - missing_after} missing value(s)", warnings_out)


def _to_bool(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    if pd.api.types.is_bool_dtype(series):
        return series.astype("boolean"), pd.Series(False, index=series.index)
    text = series.astype("string").str.strip().str.lower()
    result = pd.Series(pd.NA, index=series.index, dtype="boolean")
    result[text.isin(_TRUE_TOKENS).fillna(False)] = True
    result[text.isin(_FALSE_TOKENS).fillna(False)] = False
    invalid = series.notna() & result.isna() & text.ne("").fillna(False)
    return result, invalid


def _parse_datetime(series: pd.Series, fmt: str | None = None, dayfirst: bool = False) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if fmt:
            return pd.to_datetime(series, errors="coerce", format=fmt)
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=dayfirst)
        retry = parsed.isna() & series.notna()
        if retry.any():
            parsed = parsed.copy()
            parsed[retry] = pd.to_datetime(series[retry], errors="coerce", format="mixed", dayfirst=dayfirst)
        return parsed


def convert_series(series: pd.Series, target: str) -> tuple[pd.Series, pd.Series]:
    """Convert ``series`` to ``target``; returns (converted, invalid_mask)."""
    present = series.notna()
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        present = present & series.astype("string").str.strip().ne("").fillna(False)
    if target == "string":
        source = series
        if pd.api.types.is_float_dtype(series):
            valid = series.dropna()
            if not valid.empty and bool((valid == valid.round()).all()):
                source = series.astype("Int64")  # 1.0 -> "1" rather than "1.0"
        converted = source.astype("string")
        return converted, pd.Series(False, index=series.index)
    if target in ("integer", "float"):
        numbers = pd.to_numeric(series.where(present), errors="coerce")
        invalid = present & numbers.isna()
        if target == "integer":
            fractional = numbers.notna() & (numbers != numbers.round())
            invalid = invalid | fractional
            numbers = numbers.mask(fractional)
            return numbers.round().astype("Int64"), invalid
        return numbers.astype("float64"), invalid
    if target == "boolean":
        return _to_bool(series.where(present))
    if target in ("datetime", "date"):
        parsed = _parse_datetime(series.where(present))
        invalid = present & parsed.isna()
        if target == "date":
            return parsed.dt.date.where(parsed.notna(), None), invalid
        return parsed, invalid
    raise ValueError(f"Unknown target type '{target}'.")


def _examples(series: pd.Series, mask: pd.Series) -> str:
    values = pd.unique(series[mask].astype(str))[:_MAX_ERROR_EXAMPLES]
    return ", ".join(repr(v) for v in values)


def _cast(df, s: CastTypes, ctx):
    _require_columns(df, list(s.casts), ctx)
    result = df.copy()
    warnings_out: list[str] = []
    for column, target in s.casts.items():
        converted, invalid = convert_series(result[column], target)
        count = int(invalid.sum())
        if count and s.on_error == "fail":
            raise TransformationError(
                f"{count} value(s) cannot be converted to {target}: {_examples(result[column], invalid)}.",
                column=column,
                hint="Clean these values first, or choose 'set to empty' for invalid values.",
                **ctx,
            )
        if count:
            warnings_out.append(f"{column}: {count} invalid value(s) set to empty ({_examples(result[column], invalid)})")
        result[column] = converted
    return StepOutcome(result, f"Converted {len(s.casts)} column(s)", warnings_out)


def _dates(df, s: ParseDates, ctx):
    _require_columns(df, s.columns, ctx)
    result = df.copy()
    warnings_out: list[str] = []
    for column in s.columns:
        original = result[column]
        present = original.notna()
        if original.dtype == object or pd.api.types.is_string_dtype(original):
            present = present & original.astype("string").str.strip().ne("").fillna(False)
        try:
            parsed = _parse_datetime(original.where(present), s.input_format, s.dayfirst)
        except (ValueError, TypeError) as exc:
            raise TransformationError(f"Invalid date format '{s.input_format}': {exc}", column=column, **ctx) from exc
        invalid = present & parsed.isna()
        count = int(invalid.sum())
        if count and s.on_error == "fail":
            raise TransformationError(
                f"{count} value(s) are not valid dates: {_examples(original, invalid)}.", column=column, **ctx
            )
        if count:
            warnings_out.append(f"{column}: {count} invalid date(s) set to empty ({_examples(original, invalid)})")
        if s.output == "date":
            result[column] = parsed.dt.date.where(parsed.notna(), None)
        elif s.output == "string":
            try:
                result[column] = parsed.dt.strftime(s.output_format).where(parsed.notna(), None)
            except ValueError as exc:
                raise TransformationError(f"Invalid output format '{s.output_format}': {exc}", column=column, **ctx) from exc
        else:
            result[column] = parsed
    return StepOutcome(result, f"Parsed {len(s.columns)} date column(s)", warnings_out)


def _text_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if df[c].dtype == object or pd.api.types.is_string_dtype(df[c])]


def _trim(df, s: TrimWhitespace, ctx):
    columns = s.columns or _text_columns(df)
    _require_columns(df, columns, ctx)
    result = df.copy()
    changed = 0
    for column in columns:
        series = result[column]
        is_text = series.map(lambda v: isinstance(v, str)).astype(bool)
        if not is_text.any():
            continue
        values = series[is_text].astype(str).str.strip()
        if s.collapse_inner_spaces:
            values = values.str.replace(r"\s+", " ", regex=True)
        cleaned = series.astype(object).copy()
        cleaned[is_text] = values
        if s.blank_to_null:
            cleaned[is_text & cleaned.eq("")] = None
        changed += int((cleaned[is_text].fillna("\x00") != series[is_text]).sum())
        result[column] = cleaned
    return StepOutcome(result, f"Trimmed {changed} value(s) in {len(columns)} column(s)")


def _case(df, s: TextCase, ctx):
    _require_columns(df, s.columns, ctx)
    result = df.copy()
    for column in s.columns:
        series = result[column]
        is_text = series.map(lambda v: isinstance(v, str)).astype(bool)
        if not is_text.any():
            continue
        text = series[is_text].astype(str)
        if s.case == "lower":
            new = text.str.lower()
        elif s.case == "upper":
            new = text.str.upper()
        elif s.case == "title":
            new = text.str.title()
        else:
            new = text.str.lower().str.capitalize()
        updated = series.astype(object).copy()
        updated[is_text] = new
        result[column] = updated
    return StepOutcome(result, f"Changed text case of {len(s.columns)} column(s) to {s.case}")


def _replace(df, s: ReplaceValues, ctx):
    _require_columns(df, [s.column], ctx)
    result = df.copy()
    series = result[s.column]
    text = series.astype("string")
    replaced = 0
    for old, new in s.mapping.items():
        mask = (text == old) if s.case_sensitive else (text.str.lower() == str(old).lower())
        mask = mask.fillna(False).astype(bool)
        replaced += int(mask.sum())
        series = series.mask(mask, new)
    result[s.column] = series
    return StepOutcome(result, f"Replaced {replaced} value(s) in '{s.column}'")


def condition_mask(df: pd.DataFrame, condition: Condition, ctx: dict[str, Any]) -> pd.Series:
    _require_columns(df, [condition.column], ctx)
    series = df[condition.column]
    op, value = condition.operator, condition.value
    if op == "is_null":
        return _blank_mask(series)
    if op == "not_null":
        return ~_blank_mask(series)
    if op in ("contains", "not_contains", "starts_with", "ends_with"):
        text = series.astype("string")
        needle = "" if value is None else str(value)
        if op == "starts_with":
            mask = text.str.startswith(needle)
        elif op == "ends_with":
            mask = text.str.endswith(needle)
        else:
            mask = text.str.contains(needle, case=False, regex=False)
        mask = mask.fillna(False).astype(bool)
        return ~mask if op == "not_contains" else mask
    if op in ("in", "not_in"):
        values = value if isinstance(value, list) else [v.strip() for v in str(value or "").split(",") if v.strip()]
        text_values = {str(v) for v in values}
        mask = series.map(lambda v: (str(v) if not isinstance(v, float) or not v.is_integer() else str(int(v))) in text_values if pd.notna(v) else False)
        mask = mask.astype(bool)
        return ~mask if op == "not_in" else mask
    # Comparison operators: compare numerically when possible, then as dates, else as text.
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise TransformationError(f"A value is required for '{op}'.", column=condition.column, **ctx)
    comparators = {
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    }
    compare = comparators[op]
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.notna(number) and not isinstance(value, bool):
        left = pd.to_numeric(series, errors="coerce")
        if left.notna().any() or pd.api.types.is_numeric_dtype(series):
            return compare(left, float(number)).fillna(False).astype(bool)
    if pd.api.types.is_datetime64_any_dtype(series) or (series.map(lambda v: hasattr(v, "year")).any()):
        left = _parse_datetime(series)
        right = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(right):
            raise TransformationError(f"'{value}' is not a valid date.", column=condition.column, **ctx)
        if getattr(left.dt, "tz", None) is not None and right.tzinfo is None:
            right = right.tz_localize(left.dt.tz)
        return compare(left, right).fillna(False).astype(bool)
    return compare(series.astype("string"), str(value)).fillna(False).astype(bool)


def _filter(df, s: FilterRows, ctx):
    if s.mode == "expression":
        if not s.expression:
            raise TransformationError("Enter a filter expression.", **ctx)
        try:
            keep = evaluate_condition(s.expression, df)
        except ExpressionError as exc:
            raise TransformationError(exc.message, **ctx) from exc
    else:
        if not s.conditions:
            raise TransformationError("Add at least one condition.", **ctx)
        masks = [condition_mask(df, c, ctx) for c in s.conditions]
        keep = masks[0]
        for mask in masks[1:]:
            keep = (keep & mask) if s.match == "all" else (keep | mask)
    removed = int((~keep).sum())
    return StepOutcome(df.loc[keep].reset_index(drop=True), f"Kept {int(keep.sum())} row(s), removed {removed}")


def _calculated(df, s: CalculatedColumn, ctx):
    if s.name in df.columns and not s.overwrite:
        raise TransformationError(
            f"Column '{s.name}' already exists.", column=s.name,
            hint="Choose another name or tick 'overwrite'.", **ctx,
        )
    try:
        values = evaluate(s.expression, df)
    except ExpressionError as exc:
        raise TransformationError(exc.message, column=s.name, **ctx) from exc
    result = df.copy()
    result[s.name] = values.values
    nulls = int(pd.Series(values).isna().sum())
    warnings_out = [f"{nulls} row(s) produced an empty value."] if nulls and nulls < len(df) else []
    if nulls == len(df) and len(df):
        warnings_out = ["Every row produced an empty value - check the expression."]
    return StepOutcome(result, f"Created column '{s.name}'", warnings_out)


_IMPLEMENTATIONS: dict[str, Callable[[pd.DataFrame, Any, dict[str, Any]], StepOutcome]] = {
    "select_columns": _select,
    "drop_columns": _drop,
    "rename_columns": _rename,
    "standardize_column_names": _standardize_names,
    "remove_duplicates": _dedupe,
    "handle_missing": _missing,
    "cast_types": _cast,
    "parse_dates": _dates,
    "trim_whitespace": _trim,
    "text_case": _case,
    "filter_rows": _filter,
    "calculated_column": _calculated,
    "replace_values": _replace,
}


def apply_step(df: pd.DataFrame, step: Any, index: int) -> StepOutcome:
    """Apply one step; errors are raised as :class:`TransformationError`."""
    model = parse_step(step)
    ctx = {"step_index": index, "step_type": STEP_LABELS.get(model.type, model.type)}
    try:
        return _IMPLEMENTATIONS[model.type](df, model, ctx)
    except TransformationError:
        raise
    except Exception as exc:  # noqa: BLE001 - report unexpected pandas errors with context
        raise TransformationError(f"{type(exc).__name__}: {exc}", **ctx) from exc


__all__ = [
    "STEP_ADAPTER", "STEP_LABELS", "STEP_LIST_ADAPTER", "StepOutcome", "TransformStep", "apply_step",
    "convert_series", "describe_step", "parse_step", "standardize_name",
]
