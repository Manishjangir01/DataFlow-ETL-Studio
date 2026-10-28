"""In-process scheduling with APScheduler.

Schedules live in the database (the ``pipelines`` table). The scheduler re-reads them
every ``sync_seconds`` (and immediately when the UI calls :meth:`sync_now`), so the
same code works embedded in the Streamlit app or in the standalone worker process.

Limitations (also shown on the Settings page): jobs only fire while the process runs,
it is single-machine, and missed runs older than ``misfire_grace_time`` are skipped.
For production-scale orchestration run pipelines from Airflow via ``python -m src.cli run``.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from src.orchestration.pipeline_config import ScheduleConfig
from src.orchestration.runner import PipelineRunner
from src.orchestration.scheduling import build_trigger, trigger_signature
from src.storage.repository import Database
from src.utils.config import Settings
from src.utils.exceptions import NotFoundError, PipelineLockedError
from src.utils.logging_setup import get_logger

JOB_PREFIX = "pipeline-"
SYNC_JOB_ID = "dataflow-sync-schedules"
_log = get_logger("scheduler")


class SchedulerService:
    """Keeps APScheduler jobs in sync with the pipelines' saved schedules."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        runner_factory: Callable[[], PipelineRunner],
        *,
        sync_seconds: int = 30,
        misfire_grace_seconds: int = 300,
    ) -> None:
        self.settings = settings
        self.db = db
        self._runner_factory = runner_factory
        self._sync_seconds = sync_seconds
        self._signatures: dict[int, str] = {}
        self._names: dict[int, str] = {}
        self._lock = threading.RLock()
        self._scheduler = BackgroundScheduler(
            timezone=settings.timezone,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": misfire_grace_seconds},
        )

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        if self._scheduler.running:
            return
        self._scheduler.start()
        self._scheduler.add_job(self.sync_now, "interval", seconds=self._sync_seconds, id=SYNC_JOB_ID,
                                name="Sync schedules from the database", replace_existing=True)
        self.sync_now()
        _log.info("Scheduler started (time zone %s)", self.settings.timezone)

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            _log.info("Scheduler stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- synchronisation --------------------------------------------------------------
    def sync_now(self) -> dict[str, list[int]]:
        """Add, update or remove jobs so they match the database."""
        changes: dict[str, list[int]] = {"added": [], "updated": [], "removed": []}
        with self._lock:
            try:
                pipelines = self.db.list_pipelines()
            except Exception as exc:  # noqa: BLE001 - the DB may be briefly unavailable
                _log.error("Could not read schedules: %s", exc)
                return changes
            wanted: dict[int, ScheduleConfig] = {}
            for pipeline in pipelines:
                if not (pipeline.schedule_enabled and pipeline.schedule):
                    continue
                try:
                    wanted[pipeline.id] = ScheduleConfig.model_validate(pipeline.schedule)
                    self._names[pipeline.id] = pipeline.name
                except Exception as exc:  # noqa: BLE001
                    _log.error("Pipeline #%s has an invalid schedule and was not scheduled: %s", pipeline.id, exc)

            for job in self._scheduler.get_jobs():
                if not job.id.startswith(JOB_PREFIX):
                    continue
                pipeline_id = int(job.id[len(JOB_PREFIX):])
                if pipeline_id not in wanted:
                    job.remove()
                    self._signatures.pop(pipeline_id, None)
                    changes["removed"].append(pipeline_id)

            for pipeline_id, schedule in wanted.items():
                signature = trigger_signature(schedule)
                job_id = f"{JOB_PREFIX}{pipeline_id}"
                existing = self._scheduler.get_job(job_id)
                if existing is not None and self._signatures.get(pipeline_id) == signature:
                    continue
                self._scheduler.add_job(
                    self._run_scheduled, trigger=build_trigger(schedule), id=job_id, args=[pipeline_id],
                    name=self._names.get(pipeline_id, job_id), replace_existing=True,
                )
                self._signatures[pipeline_id] = signature
                changes["updated" if existing is not None else "added"].append(pipeline_id)
        if any(changes.values()):
            _log.info("Schedules synchronised: %s", changes)
        return changes

    # -- execution --------------------------------------------------------------------
    def _run_scheduled(self, pipeline_id: int) -> None:
        try:
            outcome = self._runner_factory().run(pipeline_id, trigger="schedule")
            _log.info("Scheduled run #%s of pipeline #%s finished: %s", outcome.run_id, pipeline_id, outcome.status)
        except PipelineLockedError as exc:
            _log.warning("Scheduled run of pipeline #%s skipped: %s", pipeline_id, exc.message)
        except NotFoundError:
            _log.warning("Pipeline #%s no longer exists; removing its schedule", pipeline_id)
            self.sync_now()
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler thread
            _log.exception("Scheduled run of pipeline #%s crashed", pipeline_id)

    # -- information --------------------------------------------------------------------
    def next_run_time(self, pipeline_id: int) -> datetime | None:
        job = self._scheduler.get_job(f"{JOB_PREFIX}{pipeline_id}")
        return getattr(job, "next_run_time", None) if job else None

    def active_job_count(self) -> int:
        return sum(1 for job in self._scheduler.get_jobs() if job.id.startswith(JOB_PREFIX))

    def jobs_overview(self) -> list[dict[str, Any]]:
        rows = []
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(JOB_PREFIX):
                continue
            next_time = getattr(job, "next_run_time", None)
            rows.append({
                "Pipeline": job.name,
                "Trigger": str(job.trigger),
                "Next run": next_time.strftime("%Y-%m-%d %H:%M %Z") if next_time else "paused",
            })
        return rows
