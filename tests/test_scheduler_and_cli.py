"""Phase 6 tests: schedules, the APScheduler service, the worker loop and the CLI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from cryptography.fernet import Fernet
from pydantic import ValidationError

from src import cli
from src.extraction.file_reader import save_upload
from src.orchestration.pipeline_config import PipelineConfig, ScheduleConfig
from src.orchestration.runner import PipelineRunner
from src.orchestration.scheduler import SchedulerService
from src.orchestration.scheduling import build_trigger, next_run_times
from src.security.secrets import SecretBox
from src.storage.repository import Database
from src.utils.config import reset_settings_cache


# --- Schedule definitions ------------------------------------------------------------

def test_schedule_types_and_descriptions():
    interval = ScheduleConfig(type="interval", interval_minutes=90)
    assert isinstance(build_trigger(interval), IntervalTrigger) and interval.describe() == "Every 90 minute(s)"
    assert ScheduleConfig(type="interval", interval_minutes=120).describe() == "Every 2 hour(s)"
    daily = ScheduleConfig(type="daily", time_of_day="06:30", timezone="Asia/Kolkata")
    assert isinstance(build_trigger(daily), CronTrigger) and "06:30" in daily.describe()
    weekly = ScheduleConfig(type="weekly", days_of_week=["mon", "fri"], time_of_day="08:00")
    start = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)   # a Monday after 08:00
    times = next_run_times(weekly, 3, now=start)
    assert [t.strftime("%a %H:%M") for t in times] == ["Fri 08:00", "Mon 08:00", "Fri 08:00"]
    cron = ScheduleConfig(type="cron", cron="*/15 * * * *")
    minutes = [t.minute for t in next_run_times(cron, 4, now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))]
    assert minutes == [15, 30, 45, 0]


@pytest.mark.parametrize(
    "data",
    [
        {"type": "cron", "cron": "not a cron"},
        {"type": "daily", "time_of_day": "25:00"},
        {"type": "daily", "timezone": "Mars/Olympus"},
        {"type": "interval", "interval_minutes": 0},
        {"type": "weekly", "days_of_week": []},
    ],
)
def test_invalid_schedules(data):
    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate(data)


# --- Scheduler service -----------------------------------------------------------------

@pytest.fixture
def stack(settings, samples_dir):
    db = Database(settings.database_url)
    box = SecretBox(settings.secret_key)
    stored = save_upload((samples_dir / "customers.csv").read_bytes(), "customers.csv", settings.upload_dir)

    def make(name: str, schedule: dict | None, enabled: bool) -> int:
        config = PipelineConfig.model_validate({
            "name": name,
            "source": {"name": "c", "kind": "file", "file": {"stored_name": stored, "original_name": "c.csv", "file_format": "csv"}},
            "destination": {"type": "csv", "file_name": f"{name}.csv", "strategy": "append"},
            "schedule": schedule, "schedule_enabled": enabled,
        })
        return db.save_pipeline(config)

    service = SchedulerService(settings, db, lambda: PipelineRunner(settings, db, box), sync_seconds=3600)
    yield db, service, make
    service.shutdown(wait=True)
    db.dispose()


def wait_for(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_sync_adds_updates_pauses_and_removes_jobs(stack):
    db, service, make = stack
    hourly = {"type": "interval", "interval_minutes": 60}
    a = make("alpha", hourly, True)
    b = make("beta", hourly, False)
    service.start()
    assert service.active_job_count() == 1 and service.next_run_time(a) is not None
    assert service.next_run_time(b) is None

    db.set_schedule_enabled(b, True)
    assert service.sync_now()["added"] == [b]
    config = db.pipeline_config(a).model_copy(update={"schedule": ScheduleConfig(type="daily", time_of_day="03:00")})
    db.save_pipeline(config, a)
    assert service.sync_now()["updated"] == [a]
    assert service.sync_now() == {"added": [], "updated": [], "removed": []}   # idempotent

    db.set_schedule_enabled(a, False)                                      # pause
    assert service.sync_now()["removed"] == [a]
    db.delete_pipeline(b)
    assert service.sync_now()["removed"] == [b]
    assert service.active_job_count() == 0 and service.jobs_overview() == []


def test_scheduled_job_really_runs_the_pipeline(stack):
    db, service, make = stack
    pipeline_id = make("gamma", {"type": "interval", "interval_minutes": 60}, True)
    service.start()
    job = service._scheduler.get_job(f"pipeline-{pipeline_id}")
    job.modify(next_run_time=datetime.now(timezone.utc))            # fire now
    assert wait_for(lambda: any(r.status == "success" for r in db.list_runs(pipeline_id)))
    run = db.list_runs(pipeline_id)[0]
    assert run.trigger == "schedule" and run.loaded_count == 21


def test_scheduled_run_is_skipped_while_pipeline_is_running(stack):
    db, service, make = stack
    pipeline_id = make("delta", {"type": "interval", "interval_minutes": 60}, True)
    token = db.acquire_lock(pipeline_id, timedelta(minutes=5))
    service._run_scheduled(pipeline_id)
    runs = db.list_runs(pipeline_id)
    assert [r.status for r in runs] == ["skipped"] and "already running" in runs[0].error_message
    db.release_lock(pipeline_id, token)


# --- CLI ---------------------------------------------------------------------------------

@pytest.fixture
def cli_env(settings_env, monkeypatch):
    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_cli_generate_key_init_list_run_and_runs(cli_env, settings, samples_dir, capsys):
    assert cli.main(["generate-key"]) == 0
    Fernet(capsys.readouterr().out.strip().encode())   # a valid key
    assert cli.main(["init-db"]) == 0
    assert cli.main(["list"]) == 0
    assert "No pipelines" in capsys.readouterr().out

    db = Database(settings.database_url)
    stored = save_upload((samples_dir / "customers.csv").read_bytes(), "customers.csv", settings.upload_dir)
    pipeline_id = db.save_pipeline(PipelineConfig.model_validate({
        "name": "cli pipeline",
        "source": {"name": "c", "kind": "file", "file": {"stored_name": stored, "original_name": "c.csv", "file_format": "csv"}},
        "destination": {"type": "parquet", "file_name": "cli_out", "strategy": "append"},
    }))
    db.dispose()
    assert cli.main(["run", "--pipeline-id", str(pipeline_id)]) == 0
    assert "SUCCESS" in capsys.readouterr().out
    assert cli.main(["runs"]) == 0
    assert "cli pipeline" in capsys.readouterr().out
    assert cli.main(["run", "--pipeline-id", "999"]) == 2
    assert cli.main(["purge", "--days", "30"]) == 0
