"""Runs an ordered list of transformation steps on a copy of the data."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import pandas as pd
from pydantic import ValidationError

from src.transformations.steps import STEP_LABELS, apply_step, describe_step, parse_step
from src.utils.exceptions import TransformationError


@dataclass
class StepReport:
    index: int
    type: str
    label: str
    description: str
    enabled: bool
    rows_before: int
    rows_after: int
    columns_after: int
    message: str
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class TransformResult:
    dataframe: pd.DataFrame
    reports: list[StepReport]

    @property
    def rows_removed(self) -> int:
        if not self.reports:
            return 0
        enabled = [r for r in self.reports if r.enabled]
        return enabled[0].rows_before - enabled[-1].rows_after if enabled else 0

    @property
    def warnings(self) -> list[str]:
        return [f"Step {r.index + 1} ({r.label}): {w}" for r in self.reports for w in r.warnings]

    def reports_as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(r) for r in self.reports]


def validate_steps(steps: Iterable[dict[str, Any]]) -> list[Any]:
    """Validate step configurations; raises TransformationError naming the bad step."""
    parsed = []
    for index, step in enumerate(steps):
        try:
            parsed.append(parse_step(step))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()) if not isinstance(p, int) or p != 0)
            label = STEP_LABELS.get(step.get("type", "") if isinstance(step, dict) else "", "step")
            raise TransformationError(
                f"Invalid settings ({where or 'value'}): {first.get('msg', 'invalid').removeprefix('Value error, ')}",
                step_index=index,
                step_type=label,
            ) from exc
    return parsed


def apply_transformations(df: pd.DataFrame, steps: Iterable[dict[str, Any]]) -> TransformResult:
    """Apply ``steps`` in order to a deep copy of ``df``.

    The input DataFrame is never modified, so the original extracted data stays
    available for previews and re-runs.

    Raises:
        TransformationError: with the step number, step type and column at fault.
    """
    parsed = validate_steps(steps)
    current = df.copy(deep=True)
    reports: list[StepReport] = []
    for index, step in enumerate(parsed):
        label = STEP_LABELS.get(step.type, step.type)
        if not step.enabled:
            reports.append(
                StepReport(index, step.type, label, describe_step(step), False, len(current), len(current),
                           len(current.columns), "Skipped (disabled)")
            )
            continue
        started = time.perf_counter()
        rows_before = len(current)
        outcome = apply_step(current, step, index)
        current = outcome.dataframe
        reports.append(
            StepReport(
                index=index,
                type=step.type,
                label=label,
                description=describe_step(step),
                enabled=True,
                rows_before=rows_before,
                rows_after=len(current),
                columns_after=len(current.columns),
                message=outcome.message,
                warnings=outcome.warnings,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
    return TransformResult(current, reports)
