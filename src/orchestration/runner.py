"""Pipeline execution: Extract → Profile → Transform → Validate → Load & verify.

A run is reported as **success only when the load was verified** (row counts checked
inside the destination transaction). Any error marks the run as failed with the step
and a redacted message; nothing is partially loaded because every load is
transactional or atomic. Runs of the same pipeline never overlap (database lock).
"""

from __future__ import annotations

import gzip
import json
import shutil
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from src.connectors.factory import effective_policy, make_api_extractor_factory
from src.extraction.extractor import ExtractResult, dataframe_from_api_records, extract_source
from src.loading.loader import load_data
from src.loading.models import LoadStrategy
from src.orchestration.pipeline_config import PipelineConfig
from src.security.redaction import redact_text
from src.security.secrets import SecretBox
from src.security.url_guard import UrlPolicy
from src.storage.models import utcnow
from src.storage.repository import Database, to_config
from src.transformations.engine import apply_transformations
from src.utils.config import Settings
from src.utils.exceptions import (
    DataFlowError,
    ExtractionError,
    LoadError,
    NotFoundError,
    PipelineLockedError,
    ValidationFailedError,
)
from src.utils.logging_setup import RunLogger, get_logger
from src.validation.profiler import profile_dataframe
from src.validation.rules import validate_dataframe

STEPS: list[tuple[str, str]] = [
    ("extract", "Extract"),
    ("profile", "Profile data quality"),
    ("transform", "Transform"),
    ("validate", "Validate"),
    ("load", "Load & verify"),
]
CACHED_EXTRACT = "extract_records.json.gz"
REJECTED_FILE = "rejected_rows.csv"

ProgressCallback = Callable[[str, str, str], None]   # (step key, status, message)
_log = get_logger("runner")


@dataclass
class StepState:
    key: str
    label: str
    status: str = "pending"          # pending | running | success | failed | skipped
    message: str = ""
    duration_seconds: float | None = None


@dataclass
class RunOutcome:
    run_id: int | None
    pipeline_id: int
    pipeline_name: str
    status: str
    duration_seconds: float
    extracted: int = 0
    transformed: int = 0
    rejected: int = 0
    loaded: int = 0
    error_step: str | None = None
    error_message: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


def build_url_policy(settings: Settings, db: Database) -> UrlPolicy:
    """Environment settings combined with the allow-list saved on the Settings page."""
    return effective_policy(
        settings,
        extra_domains=db.get_setting("allowed_domains", []) or [],
        allow_http=db.get_setting("allow_http", None),
    )


def run_artifacts_dir(settings: Settings, run_id: int) -> Path:
    return settings.runs_dir / f"run_{run_id}"


def cached_extract_path(settings: Settings, run_id: int) -> Path:
    return run_artifacts_dir(settings, run_id) / CACHED_EXTRACT


def rejected_rows_path(settings: Settings, run_id: int) -> Path:
    return run_artifacts_dir(settings, run_id) / REJECTED_FILE


class PipelineRunner:
    """Executes saved pipelines and records every run."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        secret_box: SecretBox,
        *,
        policy: UrlPolicy | None = None,
        http_session: requests.Session | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.secret_box = secret_box
        self._policy = policy
        self._http_session = http_session
        self._sleep = sleep

    # -- public ---------------------------------------------------------------------
    def run(
        self,
        pipeline_id: int,
        *,
        trigger: str = "manual",
        retry_of: int | None = None,
        use_cached_extract: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RunOutcome:
        """Run a pipeline. Raises PipelineLockedError if it is already running."""
        record = self.db.get_pipeline(pipeline_id)
        stale_after = timedelta(minutes=self.settings.lock_timeout_minutes)
        try:
            token = self.db.acquire_lock(pipeline_id, stale_after)
        except PipelineLockedError as exc:
            if trigger == "schedule":
                self.db.record_skipped_run(record, trigger, exc.message)
            raise

        run_id: int | None = None
        try:
            try:
                config = to_config(record)
                snapshot = config.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 - invalid saved configuration
                config, snapshot = None, {"error": redact_text(str(exc))}
            run_id = self.db.create_run(record, trigger, retry_of, snapshot)
            self.db.attach_run_to_lock(pipeline_id, token, run_id)
            return self._execute(record.id, record.name, run_id, config, token, trigger, retry_of, use_cached_extract, progress)
        finally:
            self.db.release_lock(pipeline_id, token)

    def retry(self, run_id: int, *, use_cached_extract: bool = False, progress: ProgressCallback | None = None) -> RunOutcome:
        """Re-run the pipeline of a previous run (optionally re-using its extracted data)."""
        previous = self.db.get_run(run_id)
        if previous.pipeline_id is None:
            raise NotFoundError("The pipeline of this run was deleted; it cannot be retried.")
        if use_cached_extract and not previous.has_cached_extract:
            raise ExtractionError("This run has no cached extract to re-use.", hint="Retry with a fresh extraction instead.")
        return self.run(previous.pipeline_id, trigger="retry", retry_of=run_id,
                        use_cached_extract=use_cached_extract, progress=progress)

    # -- internals ---------------------------------------------------------------------
    def _execute(
        self,
        pipeline_id: int,
        pipeline_name: str,
        run_id: int,
        config: PipelineConfig | None,
        token: str,
        trigger: str,
        retry_of: int | None,
        use_cached_extract: bool,
        progress: ProgressCallback | None,
    ) -> RunOutcome:
        started = time.perf_counter()
        logger = RunLogger(f"run#{run_id}")
        steps = [StepState(key, label) for key, label in STEPS]
        state = {s.key: s for s in steps}
        outcome = RunOutcome(run_id, pipeline_id, pipeline_name, "running", 0.0)
        reports: dict[str, Any] = {}
        current = "extract"
        step_started = time.perf_counter()
        artifacts = run_artifacts_dir(self.settings, run_id)

        def begin(key: str) -> None:
            nonlocal current, step_started
            current = key
            step_started = time.perf_counter()
            state[key].status = "running"
            logger.info(key, f"{state[key].label} started")
            if progress:
                progress(key, "running", "")

        def finish(key: str, message: str, status: str = "success") -> None:
            state[key].status = status
            state[key].message = redact_text(message)
            state[key].duration_seconds = round(time.perf_counter() - step_started, 3)
            logger.info(key, f"{state[key].label} {status}: {message} ({state[key].duration_seconds:.2f}s)")
            self.db.heartbeat(pipeline_id, token)
            self.db.update_run(run_id, steps=[asdict(s) for s in steps])
            if progress:
                progress(key, status, state[key].message)

        retry_note = f", retry of run #{retry_of}" if retry_of else ""
        logger.info("run", f"Run #{run_id} of pipeline '{pipeline_name}' started (trigger: {trigger}{retry_note})")
        try:
            if config is None:
                raise DataFlowError(
                    "The saved pipeline configuration is invalid.",
                    hint="Open the pipeline in Create Pipeline, fix it and save again.",
                )

            # 1. Extract -------------------------------------------------------------
            begin("extract")
            extracted = self._extract(config, logger, retry_of if use_cached_extract else None)
            df = extracted.dataframe
            for warning in extracted.warnings:
                logger.warning("extract", warning)
                outcome.warnings.append(warning)
            if extracted.records is not None:
                artifacts.mkdir(parents=True, exist_ok=True)
                with gzip.open(cached_extract_path(self.settings, run_id), "wt", encoding="utf-8") as handle:
                    json.dump(extracted.records, handle, default=str)
                self.db.update_run(run_id, has_cached_extract=True)
            outcome.extracted = len(df)
            details = ", ".join(f"{k}={v}" for k, v in extracted.details.items() if k not in ("encoding",))
            finish("extract", f"{len(df):,} records, {len(df.columns)} columns ({details})")

            # 2. Profile ---------------------------------------------------------------
            begin("profile")
            quality = profile_dataframe(df)
            reports["quality_report"] = quality.to_dict()
            for issue in quality.issues[:20]:
                logger.info("profile", issue)
            finish("profile", f"Quality score {quality.quality_score:.0f}/100, {quality.duplicate_rows} duplicate(s), "
                              f"{quality.missing_cells} missing cell(s), {quality.invalid_cells} invalid value(s)")

            # 3. Transform -----------------------------------------------------------
            begin("transform")
            transformed = apply_transformations(df, config.transforms)
            reports["transform_report"] = transformed.reports_as_dicts()
            for report in transformed.reports:
                logger.info("transform", f"Step {report.index + 1} {report.label}: {report.message} "
                                         f"({report.rows_before}→{report.rows_after} rows)")
                for warning in report.warnings:
                    logger.warning("transform", f"Step {report.index + 1}: {warning}")
            outcome.transformed = len(transformed.dataframe)
            finish("transform", f"{len(config.transforms)} step(s); {outcome.transformed:,} rows, "
                                f"{len(transformed.dataframe.columns)} columns")

            # 4. Validate ------------------------------------------------------------
            begin("validate")
            validation = validate_dataframe(transformed.dataframe, config.rules)
            reports["validation_report"] = validation.results_as_dicts()
            outcome.rejected = len(validation.rejected)
            for result in validation.results:
                logger.info("validate", f"{result.label} ({result.description}): {result.status} - {result.message}")
            if not validation.rejected.empty:
                artifacts.mkdir(parents=True, exist_ok=True)
                validation.rejected.to_csv(rejected_rows_path(self.settings, run_id), index=False, encoding="utf-8")
                self.db.update_run(run_id, has_rejected_file=True)
            if not validation.passed:
                raise ValidationFailedError(
                    "Validation failed: " + " | ".join(validation.failure_messages),
                    hint="No data was loaded. Fix the data/transformations or change the rule to 'reject rows'.",
                )
            valid = validation.valid
            reports["output_quality"] = profile_dataframe(valid).to_dict()
            finish("validate", f"{len(valid):,} valid row(s), {outcome.rejected:,} rejected")

            # 5. Load ------------------------------------------------------------------
            begin("load")
            destination = config.destination
            if valid.empty:
                if destination.strategy == LoadStrategy.REPLACE:
                    raise LoadError(
                        "There are no valid rows to load; the destination was NOT replaced with an empty dataset.",
                        hint="Check the source data, filters and validation rules.",
                    )
                finish("load", "Nothing to load (0 valid rows)", status="skipped")
                outcome.warnings.append("No rows were loaded because there were no valid rows.")
            else:
                logger.info("load", f"Loading into {destination.target_label()} using '{destination.strategy.value}'")
                result = load_data(valid, destination, self.settings, self.secret_box)
                reports["load_report"] = result.to_dict()
                if not result.verified:
                    raise LoadError("The destination could not be verified after loading.")
                outcome.loaded = result.rows_loaded
                outcome.rejected += result.rows_skipped
                for warning in result.warnings:
                    logger.warning("load", warning)
                    outcome.warnings.append(warning)
                finish("load", f"{result.rows_loaded:,} row(s) loaded - {result.verification}")
            outcome.status = "success"
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
            outcome.status = "failed"
            outcome.error_step = current
            if isinstance(exc, DataFlowError):
                message = exc.message + (f" ({exc.hint})" if exc.hint else "")
            else:
                message = f"Unexpected error: {type(exc).__name__}: {exc}"
                logger.error(current, redact_text("".join(traceback.format_exception(exc))))
            outcome.error_message = redact_text(message)
            step = state[current]
            step.status = "failed"
            step.message = outcome.error_message
            step.duration_seconds = round(time.perf_counter() - step_started, 3)
            logger.error(current, outcome.error_message)
            for s in steps:
                if s.status == "pending":
                    s.status = "skipped"
            if progress:
                progress(current, "failed", outcome.error_message)
        finally:
            outcome.duration_seconds = round(time.perf_counter() - started, 3)
            outcome.steps = [asdict(s) for s in steps]
            logger.info("run", f"Run finished with status {outcome.status.upper()} in {outcome.duration_seconds:.2f}s "
                               f"(extracted {outcome.extracted}, transformed {outcome.transformed}, "
                               f"rejected {outcome.rejected}, loaded {outcome.loaded})")
            self.db.update_run(
                run_id,
                status=outcome.status,
                finished_at=utcnow(),
                duration_seconds=outcome.duration_seconds,
                extracted_count=outcome.extracted,
                transformed_count=outcome.transformed,
                rejected_count=outcome.rejected,
                loaded_count=outcome.loaded,
                error_step=outcome.error_step,
                error_message=outcome.error_message,
                steps=outcome.steps,
                log_text=logger.text,
                **reports,
            )
            self._write_log_file(run_id, logger.text)
        return outcome

    def _extract(self, config: PipelineConfig, logger: RunLogger, cached_run_id: int | None) -> ExtractResult:
        if cached_run_id is not None:
            path = cached_extract_path(self.settings, cached_run_id)
            if not path.is_file():
                raise ExtractionError(f"The cached extract of run #{cached_run_id} is no longer available.")
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                records = json.load(handle)
            logger.info("extract", f"Re-using {len(records):,} records extracted by run #{cached_run_id} (no API call)")
            flatten = config.source.api.flatten_nested if config.source.api else True
            return ExtractResult(dataframe_from_api_records(records, flatten), records, {"cached_from_run": cached_run_id})
        policy = self._policy or build_url_policy(self.settings, self.db)
        factory = make_api_extractor_factory(
            self.settings, self.secret_box, policy, session=self._http_session, sleep=self._sleep,
            log=lambda message: logger.info("extract", message),
        )
        return extract_source(config.source, self.settings, api_extractor_factory=factory)

    def _write_log_file(self, run_id: int, text: str) -> None:
        try:
            self.settings.run_logs_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.run_logs_dir / f"run_{run_id}.log").write_text(text, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk problems are logged only
            _log.warning("Could not write the run log file: %s", exc)


def purge_history(db: Database, settings: Settings, days: int) -> int:
    """Delete run records (and their artifacts and log files) older than ``days``."""
    removed = db.delete_runs_older_than(days)
    for run_id in removed:
        shutil.rmtree(run_artifacts_dir(settings, run_id), ignore_errors=True)
        (settings.run_logs_dir / f"run_{run_id}.log").unlink(missing_ok=True)
    return len(removed)


def load_rejected_rows(settings: Settings, run_id: int) -> pd.DataFrame | None:
    path = rejected_rows_path(settings, run_id)
    return pd.read_csv(path) if path.is_file() else None
