"""Phase 5 tests: persistence, pipeline execution, history, logs, locks and recovery."""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta

import pytest
import responses
from sqlalchemy import text

from src.connectors.api_connector import build_session
from src.extraction.file_reader import save_upload
from src.loading.loader import inspect_target
from src.loading.models import DestinationConfig
from src.orchestration import runner as runner_module
from src.orchestration.pipeline_config import PipelineConfig
from src.orchestration.runner import PipelineRunner, load_rejected_rows, purge_history
from src.security.secrets import SecretBox
from src.security.url_guard import UrlPolicy
from src.storage.models import RunRecord, utcnow
from src.storage.repository import Database
from src.utils.exceptions import ConfigurationError, PipelineLockedError

API_URL = "https://api.example.com/v1/orders"
POLICY = UrlPolicy(resolver=lambda host, port: ["93.184.216.34"])


@pytest.fixture
def db(settings):
    database = Database(settings.database_url)
    yield database
    database.dispose()


@pytest.fixture
def box(settings) -> SecretBox:
    return SecretBox(settings.secret_key)


@pytest.fixture
def runner(settings, db, box) -> PipelineRunner:
    return PipelineRunner(settings, db, box, policy=POLICY, http_session=build_session(), sleep=lambda s: None)


def file_pipeline(settings, samples_dir, **overrides) -> PipelineConfig:
    stored = save_upload((samples_dir / "customers.csv").read_bytes(), "customers.csv", settings.upload_dir)
    data = {
        "name": "Customers to SQLite",
        "source": {"name": "customers", "kind": "file",
                   "file": {"stored_name": stored, "original_name": "customers.csv", "file_format": "csv"}},
        "transforms": [
            {"type": "trim_whitespace"},
            {"type": "remove_duplicates"},
            {"type": "text_case", "columns": ["country"], "case": "title"},
        ],
        "rules": [{"type": "not_null", "column": "email"}],
        "destination": {"type": "sqlite", "sqlite_file": "wh.db", "table": "customers", "strategy": "append"},
    }
    data.update(overrides)
    return PipelineConfig.model_validate(data)


def api_pipeline(box: SecretBox, **overrides) -> PipelineConfig:
    data = {
        "name": "Orders API",
        "source": {"name": "orders", "kind": "api", "api": {
            "url": API_URL, "authorized": True, "records_path": "data",
            "auth": {"type": "bearer", "secret": box.encrypt("tok_LIVE_abcdef123456").model_dump()},
        }},
        "transforms": [{"type": "standardize_column_names"}],
        "rules": [],
        "destination": {"type": "sqlite", "sqlite_file": "wh.db", "table": "orders", "strategy": "replace"},
    }
    data.update(overrides)
    return PipelineConfig.model_validate(data)


def table_count(settings, box, table: str) -> int | None:
    info = inspect_target(DestinationConfig(type="sqlite", sqlite_file="wh.db", table=table), settings, box)
    return info.row_count if info.exists else None


# --- Persistence ---------------------------------------------------------------------

def test_save_load_update_and_unique_names(db, settings, samples_dir):
    config = file_pipeline(settings, samples_dir)
    pipeline_id = db.save_pipeline(config)
    loaded = db.pipeline_config(pipeline_id)
    assert loaded == config
    db.save_pipeline(config.model_copy(update={"description": "updated"}), pipeline_id)
    assert db.get_pipeline(pipeline_id).description == "updated"
    with pytest.raises(ConfigurationError):
        db.save_pipeline(config)            # same name
    assert [p.name for p in db.list_pipelines()] == ["Customers to SQLite"]


def test_invalid_pipeline_configs_are_rejected(settings, samples_dir):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="step 1"):
        file_pipeline(settings, samples_dir, transforms=[{"type": "select_columns", "columns": []}])
    with pytest.raises(ValidationError, match="no schedule"):
        file_pipeline(settings, samples_dir, schedule_enabled=True)


# --- Execution ------------------------------------------------------------------------

def test_successful_run_records_counts_reports_and_logs(db, runner, settings, samples_dir, box):
    pipeline_id = db.save_pipeline(file_pipeline(settings, samples_dir))
    events = []
    outcome = runner.run(pipeline_id, progress=lambda key, status, msg: events.append((key, status)))
    assert outcome.succeeded, outcome.error_message
    assert (outcome.extracted, outcome.transformed, outcome.rejected, outcome.loaded) == (21, 20, 1, 19)
    assert table_count(settings, box, "customers") == 19
    assert ("load", "success") in events and events[0] == ("extract", "running")

    run = db.get_run(outcome.run_id)
    assert run.status == "success" and run.duration_seconds > 0
    assert [s["status"] for s in run.steps] == ["success"] * 5
    assert run.quality_report["row_count"] == 21 and run.output_quality["row_count"] == 19
    assert run.validation_report[0]["violations"] == 1
    assert run.load_report["verified"] is True
    assert "Run finished with status SUCCESS" in run.log_text
    assert (settings.run_logs_dir / f"run_{run.id}.log").read_text(encoding="utf-8") == run.log_text
    rejected = load_rejected_rows(settings, run.id)
    assert len(rejected) == 1 and "_rejection_reasons" in rejected.columns

    metrics = db.run_metrics()
    assert metrics["total_runs"] == 1 and metrics["successful_runs"] == 1 and metrics["records_loaded"] == 19
    assert int(db.runs_per_day(7)["success"].sum()) == 1


def test_validation_failure_stops_before_loading(db, runner, settings, samples_dir, box):
    config = file_pipeline(settings, samples_dir, rules=[{"type": "range", "column": "lifetime_value", "min": 0, "on_fail": "fail"}])
    outcome = runner.run(db.save_pipeline(config))
    assert outcome.status == "failed" and outcome.error_step == "validate"
    assert outcome.loaded == 0 and "No data was loaded" in outcome.error_message
    assert table_count(settings, box, "customers") is None
    steps = {s["key"]: s["status"] for s in outcome.steps}
    assert steps == {"extract": "success", "profile": "success", "transform": "success", "validate": "failed", "load": "skipped"}


def test_transform_error_reports_step(db, runner, settings, samples_dir):
    config = file_pipeline(settings, samples_dir, transforms=[{"type": "text_case", "columns": ["missing"], "case": "lower"}])
    outcome = runner.run(db.save_pipeline(config))
    assert outcome.error_step == "transform" and "missing" in outcome.error_message and "step 1" in outcome.error_message


def test_load_failure_leaves_destination_unchanged(db, runner, settings, samples_dir, box):
    pipeline_id = db.save_pipeline(file_pipeline(settings, samples_dir))
    assert runner.run(pipeline_id).succeeded
    changed = file_pipeline(settings, samples_dir, name="Customers v2",
                            transforms=[{"type": "calculated_column", "name": "extra", "expression": "age"}])
    outcome = runner.run(db.save_pipeline(changed))
    assert outcome.status == "failed" and outcome.error_step == "load" and "extra" in outcome.error_message
    assert table_count(settings, box, "customers") == 19


def test_replace_with_no_valid_rows_is_refused(db, runner, settings, samples_dir, box):
    config = file_pipeline(settings, samples_dir, name="empty", transforms=[
        {"type": "filter_rows", "conditions": [{"column": "country", "operator": "==", "value": "Atlantis"}]}],
        destination={"type": "sqlite", "sqlite_file": "wh.db", "table": "customers", "strategy": "replace", "confirm_replace": True})
    outcome = runner.run(db.save_pipeline(config))
    assert outcome.status == "failed" and "NOT replaced" in outcome.error_message


# --- API runs, secrets and recovery -----------------------------------------------------

@responses.activate
def test_api_run_never_logs_secrets_and_retry_uses_cached_extract(db, runner, settings, box):
    responses.get(API_URL, json={"data": [{"Order ID": i, "Total": i * 10} for i in range(1, 6)]})
    # Replace without confirmation fails at the load step (after extraction).
    pipeline_id = db.save_pipeline(api_pipeline(box))
    first = runner.run(pipeline_id)
    assert first.status == "failed" and first.error_step == "load" and first.extracted == 5
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok_LIVE_abcdef123456"

    run = db.get_run(first.run_id)
    assert run.has_cached_extract
    for text_blob in (run.log_text, str(run.config_snapshot), run.error_message or ""):
        assert "tok_LIVE_abcdef123456" not in text_blob

    # Recovery: fix the configuration and retry from the cached extract - no new API call.
    fixed = api_pipeline(box, destination={"type": "sqlite", "sqlite_file": "wh.db", "table": "orders",
                                           "strategy": "replace", "confirm_replace": True})
    db.save_pipeline(fixed, pipeline_id)
    second = runner.retry(first.run_id, use_cached_extract=True)
    assert second.succeeded and second.loaded == 5
    assert len(responses.calls) == 1
    retry_run = db.get_run(second.run_id)
    assert retry_run.trigger == "retry" and retry_run.retry_of == first.run_id
    assert "Re-using 5 records extracted by run" in retry_run.log_text
    assert table_count(settings, box, "orders") == 5


@responses.activate
def test_api_errors_mark_run_failed(db, runner, box):
    responses.get(API_URL, status=500)
    responses.get(API_URL, status=500)
    responses.get(API_URL, status=500)
    responses.get(API_URL, status=500)
    outcome = runner.run(db.save_pipeline(api_pipeline(box)))
    assert outcome.status == "failed" and outcome.error_step == "extract" and "HTTP 500" in outcome.error_message


# --- Overlap prevention and interrupted runs ----------------------------------------------

def test_overlapping_runs_are_prevented(db, runner, settings, samples_dir, monkeypatch):
    pipeline_id = db.save_pipeline(file_pipeline(settings, samples_dir))
    original = runner_module.extract_source
    started = threading.Event()

    def slow_extract(*args, **kwargs):
        started.set()
        time.sleep(1.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "extract_source", slow_extract)
    results = {}
    thread = threading.Thread(target=lambda: results.setdefault("first", runner.run(pipeline_id)))
    thread.start()
    assert started.wait(5)
    with pytest.raises(PipelineLockedError):
        runner.run(pipeline_id)
    with pytest.raises(PipelineLockedError):
        runner.run(pipeline_id, trigger="schedule")
    thread.join(10)
    assert results["first"].succeeded
    statuses = sorted(r.status for r in db.list_runs(pipeline_id))
    assert statuses == ["skipped", "success"]
    assert db.running_pipeline_ids() == set()      # lock released
    assert runner.run(pipeline_id).succeeded        # can run again


def test_stale_lock_is_taken_over_and_interrupted_runs_recovered(db, settings, samples_dir):
    pipeline_id = db.save_pipeline(file_pipeline(settings, samples_dir))
    token = db.acquire_lock(pipeline_id, timedelta(minutes=5))
    with pytest.raises(PipelineLockedError):
        db.acquire_lock(pipeline_id, timedelta(minutes=5))
    record = db.get_pipeline(pipeline_id)
    run_id = db.create_run(record, "manual", None, {})
    db.attach_run_to_lock(pipeline_id, token, run_id)
    # Simulate a crashed process: the heartbeat is old.
    from sqlalchemy import update

    from src.storage.models import PipelineLock

    with db.session() as s:
        s.execute(update(PipelineLock).values(heartbeat_at=utcnow() - timedelta(hours=5)))
    assert db.recover_interrupted_runs(timedelta(minutes=5)) == [run_id]
    run = db.get_run(run_id)
    assert run.status == "failed" and "interrupted" in run.error_message
    assert db.running_pipeline_ids() == set()
    new_token = db.acquire_lock(pipeline_id, timedelta(minutes=5))
    assert new_token != token


def test_purge_history_removes_old_runs_and_artifacts(db, runner, settings, samples_dir):
    pipeline_id = db.save_pipeline(file_pipeline(settings, samples_dir))
    outcome = runner.run(pipeline_id)
    with db.session() as s:
        s.get(RunRecord, outcome.run_id).started_at = utcnow() - timedelta(days=40)
    assert purge_history(db, settings, days=30) == 1
    assert not (settings.run_logs_dir / f"run_{outcome.run_id}.log").exists()
    assert db.list_runs() == []


def test_settings_store(db):
    assert db.get_setting("allowed_domains", []) == []
    db.set_setting("allowed_domains", ["api.example.com"])
    assert db.get_setting("allowed_domains") == ["api.example.com"]


def test_readme_walkthrough_pipeline(db, runner, settings, samples_dir, box):
    """The exact pipeline described in README 'Build and run your first pipeline'."""
    config = file_pipeline(
        settings, samples_dir, name="README walkthrough",
        transforms=[
            {"type": "trim_whitespace"},
            {"type": "remove_duplicates"},
            {"type": "text_case", "columns": ["country"], "case": "title"},
            {"type": "cast_types", "casts": {"age": "integer", "lifetime_value": "float"}},
            {"type": "parse_dates", "columns": ["signup_date"], "output": "date"},
            {"type": "calculated_column", "name": "value_band", "expression": '"high" if lifetime_value >= 1000 else "normal"'},
        ],
        rules=[
            {"type": "not_null", "column": "email"},
            {"type": "pattern", "column": "email", "pattern": r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}"},
            {"type": "range", "column": "lifetime_value", "min": 0},
        ],
        destination={"type": "sqlite", "sqlite_file": "wh.db", "table": "customers", "strategy": "upsert",
                     "upsert_keys": ["customer_id"]},
    )
    pipeline_id = db.save_pipeline(config)
    first = runner.run(pipeline_id)
    assert first.succeeded, first.error_message
    assert (first.extracted, first.transformed, first.rejected, first.loaded) == (21, 20, 3, 17)
    second = runner.run(pipeline_id)                       # idempotent upsert
    assert second.succeeded and db.get_run(second.run_id).load_report["rows_updated"] == 17
    assert table_count(settings, box, "customers") == 17


@pytest.mark.skipif(not os.environ.get("DATAFLOW_TEST_POSTGRES_URL"), reason="DATAFLOW_TEST_POSTGRES_URL not set")
def test_postgres_metadata_and_destination(settings, samples_dir, box, monkeypatch):
    """Full run with PostgreSQL as both the metadata database and the destination."""
    from sqlalchemy import create_engine

    url = os.environ["DATAFLOW_TEST_POSTGRES_URL"]
    schema_engine = create_engine(url)
    with schema_engine.begin() as conn:
        for table in ("pipeline_runs", "pipeline_locks", "app_settings", "pipelines", "pg_customers"):
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    database = Database(url)
    try:
        pg_runner = PipelineRunner(settings, database, box, policy=POLICY)
        config = file_pipeline(settings, samples_dir, destination={
            "type": "postgresql", "table": "pg_customers", "strategy": "upsert", "upsert_keys": ["customer_id"],
            "postgres": {"url_env": "DATAFLOW_TEST_POSTGRES_URL"}})
        pipeline_id = database.save_pipeline(config)
        outcome = pg_runner.run(pipeline_id)
        assert outcome.succeeded, outcome.error_message
        assert outcome.loaded == 19
        assert database.run_metrics()["records_loaded"] == 19
        with pytest.raises(PipelineLockedError):
            token = database.acquire_lock(pipeline_id, timedelta(minutes=5))
            database.acquire_lock(pipeline_id, timedelta(minutes=5))
        database.release_lock(pipeline_id, token)
    finally:
        database.dispose()
        with schema_engine.begin() as conn:
            for table in ("pipeline_runs", "pipeline_locks", "app_settings", "pipelines", "pg_customers"):
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        schema_engine.dispose()
