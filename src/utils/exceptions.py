"""Exception hierarchy for DataFlow ETL Studio.

Every error raised deliberately by the application derives from :class:`DataFlowError`
so the UI and the pipeline runner can show a clear, user-facing message without
leaking stack traces (or secrets) to the screen.
"""

from __future__ import annotations


class DataFlowError(Exception):
    """Base class for all expected application errors."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} ({self.hint})" if self.hint else self.message


class ConfigurationError(DataFlowError):
    """Invalid application or pipeline configuration."""


class SecurityError(DataFlowError):
    """A request or configuration was blocked by a security policy."""


class SecretError(SecurityError):
    """A secret could not be stored, found or decrypted."""


class ExtractionError(DataFlowError):
    """Data could not be extracted from a source."""

    def __init__(
        self, message: str, *, hint: str | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code


class FileReadError(ExtractionError):
    """An uploaded or stored file could not be parsed."""


class TransformationError(DataFlowError):
    """A transformation step failed.

    Carries the step number, step type and affected column so the user can find
    the problem quickly.
    """

    def __init__(
        self,
        message: str,
        *,
        step_index: int | None = None,
        step_type: str | None = None,
        column: str | None = None,
        hint: str | None = None,
    ) -> None:
        location = []
        if step_index is not None:
            location.append(f"step {step_index + 1}")
        if step_type:
            location.append(step_type)
        if column:
            location.append(f"column '{column}'")
        prefix = f"[{' / '.join(location)}] " if location else ""
        super().__init__(prefix + message, hint=hint)
        self.step_index = step_index
        self.step_type = step_type
        self.column = column


class ExpressionError(DataFlowError):
    """An expression (filter, calculated column, business rule) is invalid."""


class ValidationFailedError(DataFlowError):
    """Data failed a validation rule configured to stop the pipeline."""


class LoadError(DataFlowError):
    """Data could not be loaded into the destination."""


class PipelineLockedError(DataFlowError):
    """The pipeline is already running; overlapping runs are not allowed."""


class NotFoundError(DataFlowError):
    """A requested pipeline, run or file does not exist."""
