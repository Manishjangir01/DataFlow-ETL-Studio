"""Validation rules: required columns, null/unique checks, ranges, patterns and
business rules written as expressions.

Every rule produces a boolean "violation" mask. Depending on ``on_fail`` a violating
row is either **rejected** (kept out of the load and offered as a download) or the
whole run **fails** before anything is loaded.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Iterable, Literal, Union

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from src.transformations.expressions import evaluate_condition, parse_expression
from src.transformations.steps import convert_series
from src.utils.exceptions import ExpressionError, ValidationFailedError

REJECTION_COLUMN = "_rejection_reasons"
MAX_PATTERN_LENGTH = 300


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class _RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    enabled: bool = True
    on_fail: Literal["reject", "fail"] = "reject"
    description: str | None = Field(default=None, max_length=200)


class RequiredColumnsRule(_RuleBase):
    """The listed columns must exist (always stops the run when missing)."""

    type: Literal["required_columns"] = "required_columns"
    columns: list[str] = Field(min_length=1)
    on_fail: Literal["reject", "fail"] = "fail"


class NotNullRule(_RuleBase):
    type: Literal["not_null"] = "not_null"
    column: str


class UniqueRule(_RuleBase):
    type: Literal["unique"] = "unique"
    columns: list[str] = Field(min_length=1)


class RangeRule(_RuleBase):
    type: Literal["range"] = "range"
    column: str
    min: float | None = None
    max: float | None = None
    allow_null: bool = True

    @model_validator(mode="after")
    def _bounds(self) -> "RangeRule":
        if self.min is None and self.max is None:
            raise ValueError("Set a minimum, a maximum or both.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("The minimum is larger than the maximum.")
        return self


class AllowedValuesRule(_RuleBase):
    type: Literal["allowed_values"] = "allowed_values"
    column: str
    values: list[str] = Field(min_length=1)
    case_sensitive: bool = False
    allow_null: bool = True


class PatternRule(_RuleBase):
    type: Literal["pattern"] = "pattern"
    column: str
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN_LENGTH)
    allow_null: bool = True

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        return value


class DataTypeRule(_RuleBase):
    """Values must be convertible to the given type (catches 'invalid values')."""

    type: Literal["data_type"] = "data_type"
    column: str
    expected: Literal["integer", "float", "boolean", "datetime", "date"]


class ExpressionRule(_RuleBase):
    """Business rule: the expression must be true for every row."""

    type: Literal["expression"] = "expression"
    expression: str = Field(min_length=1)

    @field_validator("expression")
    @classmethod
    def _valid(cls, value: str) -> str:
        try:
            parse_expression(value)
        except ExpressionError as exc:
            raise ValueError(exc.message) from exc
        return value


class RowCountRule(_RuleBase):
    """Table-level rule: the number of rows must be within limits (fails the run)."""

    type: Literal["row_count"] = "row_count"
    min_rows: int | None = Field(default=1, ge=0)
    max_rows: int | None = Field(default=None, ge=0)
    on_fail: Literal["reject", "fail"] = "fail"


ValidationRule = Annotated[
    Union[
        RequiredColumnsRule, NotNullRule, UniqueRule, RangeRule, AllowedValuesRule, PatternRule,
        DataTypeRule, ExpressionRule, RowCountRule,
    ],
    Field(discriminator="type"),
]
RULE_ADAPTER: TypeAdapter[Any] = TypeAdapter(ValidationRule)

RULE_LABELS = {
    "required_columns": "Required columns exist",
    "not_null": "Value is not empty",
    "unique": "Values are unique",
    "range": "Number within range",
    "allowed_values": "Value in allowed list",
    "pattern": "Matches pattern (regex)",
    "data_type": "Valid data type",
    "expression": "Business rule (expression)",
    "row_count": "Row count within limits",
}


def parse_rule(data: dict[str, Any] | BaseModel) -> Any:
    return data if isinstance(data, BaseModel) else RULE_ADAPTER.validate_python(data)


def describe_rule(rule: Any) -> str:
    r = parse_rule(rule)
    if r.type == "required_columns":
        return f"Columns exist: {', '.join(r.columns)}"
    if r.type == "not_null":
        return f"{r.column} is not empty"
    if r.type == "unique":
        return f"{' + '.join(r.columns)} is unique"
    if r.type == "range":
        low = "-∞" if r.min is None else f"{r.min:g}"
        high = "∞" if r.max is None else f"{r.max:g}"
        return f"{low} ≤ {r.column} ≤ {high}"
    if r.type == "allowed_values":
        return f"{r.column} in [{', '.join(r.values[:8])}{'…' if len(r.values) > 8 else ''}]"
    if r.type == "pattern":
        return f"{r.column} matches /{r.pattern}/"
    if r.type == "data_type":
        return f"{r.column} is a valid {r.expected}"
    if r.type == "expression":
        return r.expression
    if r.type == "row_count":
        parts = []
        if r.min_rows is not None:
            parts.append(f"at least {r.min_rows}")
        if r.max_rows is not None:
            parts.append(f"at most {r.max_rows}")
        return "Row count " + " and ".join(parts or ["any"])
    return r.type


@dataclass
class RuleResult:
    rule_id: str
    type: str
    label: str
    description: str
    on_fail: str
    violations: int
    status: str            # passed | rejected | failed | error | skipped
    message: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    valid: pd.DataFrame
    rejected: pd.DataFrame
    results: list[RuleResult]

    @property
    def passed(self) -> bool:
        return not any(r.status in ("failed", "error") for r in self.results)

    @property
    def failure_messages(self) -> list[str]:
        return [f"{r.label} - {r.description}: {r.message}" for r in self.results if r.status in ("failed", "error")]

    def results_as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(r) for r in self.results]


def _present(series: pd.Series) -> pd.Series:
    mask = series.notna()
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        mask = mask & series.astype("string").str.strip().ne("").fillna(False)
    return mask.astype(bool)


def _violations(df: pd.DataFrame, rule: Any) -> pd.Series:
    """Return a boolean mask of violating rows for row-level rules."""
    t = rule.type
    if t == "not_null":
        return ~_present(df[rule.column])
    if t == "unique":
        from src.validation.profiler import hashable_frame

        return hashable_frame(df[rule.columns]).duplicated(keep=False)
    if t == "range":
        series = df[rule.column]
        present = _present(series)
        numbers = pd.to_numeric(series.where(present), errors="coerce")
        bad = present & numbers.isna()
        if rule.min is not None:
            bad = bad | (numbers < rule.min).fillna(False)
        if rule.max is not None:
            bad = bad | (numbers > rule.max).fillna(False)
        if not rule.allow_null:
            bad = bad | ~present
        return bad.astype(bool)
    if t == "allowed_values":
        series = df[rule.column]
        present = _present(series)
        text = series.map(lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
        allowed = rule.values if rule.case_sensitive else [v.lower() for v in rule.values]
        values = text if rule.case_sensitive else text.str.lower()
        bad = present & ~values.isin(allowed)
        if not rule.allow_null:
            bad = bad | ~present
        return bad.astype(bool)
    if t == "pattern":
        series = df[rule.column]
        present = _present(series)
        regex = re.compile(rule.pattern)
        matches = series.where(present, "").astype(str).map(lambda v: bool(regex.fullmatch(v)))
        bad = present & ~matches
        if not rule.allow_null:
            bad = bad | ~present
        return bad.astype(bool)
    if t == "data_type":
        _, invalid = convert_series(df[rule.column], rule.expected)
        return invalid.astype(bool)
    if t == "expression":
        return ~evaluate_condition(rule.expression, df)
    raise ValueError(f"Unsupported rule type {t}")


def _examples(df: pd.DataFrame, rule: Any, mask: pd.Series) -> list[str]:
    columns = [rule.column] if hasattr(rule, "column") else list(getattr(rule, "columns", []))
    columns = [c for c in columns if c in df.columns]
    subset = df.loc[mask].head(5)
    if columns:
        return [", ".join(f"{c}={row[c]!r}" for c in columns) for _, row in subset.iterrows()]
    return [f"row {i + 1}" for i in subset.index[:5]]


def validate_dataframe(df: pd.DataFrame, rules: Iterable[dict[str, Any]]) -> ValidationResult:
    """Apply ``rules`` and split ``df`` into valid and rejected rows."""
    parsed = [parse_rule(r) for r in rules]
    results: list[RuleResult] = []
    rejected_mask = pd.Series(False, index=df.index)
    reasons: dict[Any, list[str]] = {}

    for rule in parsed:
        label = RULE_LABELS.get(rule.type, rule.type)
        description = rule.description or describe_rule(rule)
        if not rule.enabled:
            results.append(RuleResult(rule.id, rule.type, label, description, rule.on_fail, 0, "skipped", "Disabled"))
            continue

        if rule.type == "required_columns":
            missing = [c for c in rule.columns if c not in df.columns]
            status = "failed" if missing else "passed"
            message = f"Missing column(s): {', '.join(missing)}" if missing else "All required columns present"
            results.append(RuleResult(rule.id, rule.type, label, description, "fail", len(missing), status, message))
            continue
        if rule.type == "row_count":
            n = len(df)
            too_few = rule.min_rows is not None and n < rule.min_rows
            too_many = rule.max_rows is not None and n > rule.max_rows
            status = "failed" if (too_few or too_many) else "passed"
            results.append(RuleResult(rule.id, rule.type, label, description, "fail", int(too_few or too_many), status, f"{n:,} row(s)"))
            continue

        needed = [rule.column] if hasattr(rule, "column") else list(getattr(rule, "columns", []))
        missing = [c for c in needed if c not in df.columns]
        if missing:
            results.append(
                RuleResult(rule.id, rule.type, label, description, rule.on_fail, 0, "error",
                           f"Column(s) not found: {', '.join(missing)}")
            )
            continue
        try:
            mask = _violations(df, rule).reindex(df.index, fill_value=False).astype(bool)
        except ExpressionError as exc:
            results.append(RuleResult(rule.id, rule.type, label, description, rule.on_fail, 0, "error", exc.message))
            continue

        count = int(mask.sum())
        if count == 0:
            results.append(RuleResult(rule.id, rule.type, label, description, rule.on_fail, 0, "passed", "No violations"))
            continue
        examples = _examples(df, rule, mask)
        if rule.on_fail == "fail":
            results.append(
                RuleResult(rule.id, rule.type, label, description, "fail", count, "failed",
                           f"{count:,} row(s) violate this rule", examples)
            )
        else:
            rejected_mask |= mask
            for idx in df.index[mask]:
                reasons.setdefault(idx, []).append(description)
            results.append(
                RuleResult(rule.id, rule.type, label, description, "reject", count, "rejected",
                           f"{count:,} row(s) rejected", examples)
            )

    valid = df.loc[~rejected_mask].reset_index(drop=True)
    rejected = df.loc[rejected_mask].copy()
    if not rejected.empty:
        rejected[REJECTION_COLUMN] = ["; ".join(reasons.get(i, [])) for i in rejected.index]
    rejected = rejected.reset_index(drop=True)
    return ValidationResult(valid=valid, rejected=rejected, results=results)


def raise_if_failed(result: ValidationResult) -> None:
    if not result.passed:
        raise ValidationFailedError(
            "Validation failed: " + " | ".join(result.failure_messages),
            hint="Fix the data or the transformations, or change the rule to 'reject rows'.",
        )
