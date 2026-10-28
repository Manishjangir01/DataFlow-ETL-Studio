"""Persistence for pipelines, run history, locks and settings."""

from __future__ import annotations

import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.orchestration.pipeline_config import PipelineConfig
from src.storage.engine import make_engine
from src.storage.models import AppSetting, Base, PipelineLock, PipelineRecord, RunRecord, utcnow
from src.utils.exceptions import ConfigurationError, NotFoundError, PipelineLockedError

RUN_STATUSES = ("running", "success", "failed", "skipped")


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def process_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Database:
    """Wraps the metadata database engine and provides typed repository methods."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url, wal=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- Pipelines ---------------------------------------------------------------
    def list_pipelines(self) -> list[PipelineRecord]:
        with self.session() as s:
            return list(s.scalars(select(PipelineRecord).order_by(PipelineRecord.name)))

    def get_pipeline(self, pipeline_id: int) -> PipelineRecord:
        with self.session() as s:
            record = s.get(PipelineRecord, pipeline_id)
            if record is None:
                raise NotFoundError(f"Pipeline #{pipeline_id} does not exist.")
            return record

    def pipeline_config(self, pipeline_id: int) -> PipelineConfig:
        return to_config(self.get_pipeline(pipeline_id))

    def save_pipeline(self, config: PipelineConfig, pipeline_id: int | None = None) -> int:
        """Create a pipeline (``pipeline_id`` None) or update an existing one."""
        values = {
            "name": config.name,
            "description": config.description,
            "source": config.source.model_dump(mode="json"),
            "transforms": config.transforms,
            "rules": config.rules,
            "destination": config.destination.model_dump(mode="json"),
            "schedule": config.schedule.model_dump(mode="json") if config.schedule else None,
            "schedule_enabled": config.schedule_enabled,
        }
        try:
            with self.session() as s:
                if pipeline_id is None:
                    record = PipelineRecord(**values)
                    s.add(record)
                    s.flush()
                    return record.id
                record = s.get(PipelineRecord, pipeline_id)
                if record is None:
                    raise NotFoundError(f"Pipeline #{pipeline_id} does not exist.")
                for key, value in values.items():
                    setattr(record, key, value)
                record.updated_at = utcnow()
                return record.id
        except IntegrityError as exc:
            raise ConfigurationError(
                f"A pipeline named '{config.name}' already exists.", hint="Choose a different name."
            ) from exc

    def delete_pipeline(self, pipeline_id: int) -> None:
        with self.session() as s:
            s.execute(update(RunRecord).where(RunRecord.pipeline_id == pipeline_id).values(pipeline_id=None))
            s.execute(delete(PipelineLock).where(PipelineLock.pipeline_id == pipeline_id))
            s.execute(delete(PipelineRecord).where(PipelineRecord.id == pipeline_id))

    def set_schedule_enabled(self, pipeline_id: int, enabled: bool) -> None:
        with self.session() as s:
            record = s.get(PipelineRecord, pipeline_id)
            if record is None:
                raise NotFoundError(f"Pipeline #{pipeline_id} does not exist.")
            if enabled and not record.schedule:
                raise ConfigurationError("Configure a schedule before enabling it.")
            record.schedule_enabled = enabled
            record.updated_at = utcnow()

    # -- Runs ------------------------------------------------------------------------
    def create_run(self, pipeline: PipelineRecord, trigger: str, retry_of: int | None, snapshot: dict[str, Any]) -> int:
        with self.session() as s:
            run = RunRecord(
                pipeline_id=pipeline.id, pipeline_name=pipeline.name, status="running", trigger=trigger,
                retry_of=retry_of, config_snapshot=snapshot, steps=[],
            )
            s.add(run)
            s.flush()
            return run.id

    def record_skipped_run(self, pipeline: PipelineRecord, trigger: str, reason: str) -> int:
        now = utcnow()
        with self.session() as s:
            run = RunRecord(
                pipeline_id=pipeline.id, pipeline_name=pipeline.name, status="skipped", trigger=trigger,
                started_at=now, finished_at=now, duration_seconds=0.0, error_message=reason, steps=[],
                log_text=f"{now:%Y-%m-%d %H:%M:%S}Z INFO    [run] Skipped: {reason}\n",
            )
            s.add(run)
            s.flush()
            return run.id

    def update_run(self, run_id: int, **values: Any) -> None:
        with self.session() as s:
            s.execute(update(RunRecord).where(RunRecord.id == run_id).values(**values))

    def get_run(self, run_id: int) -> RunRecord:
        with self.session() as s:
            run = s.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError(f"Run #{run_id} does not exist.")
            return run

    def list_runs(self, pipeline_id: int | None = None, limit: int = 200, status: str | None = None) -> list[RunRecord]:
        with self.session() as s:
            query = select(RunRecord).order_by(RunRecord.id.desc()).limit(limit)
            if pipeline_id is not None:
                query = query.where(RunRecord.pipeline_id == pipeline_id)
            if status:
                query = query.where(RunRecord.status == status)
            return list(s.scalars(query))

    def last_run(self, pipeline_id: int) -> RunRecord | None:
        runs = self.list_runs(pipeline_id, limit=1)
        return runs[0] if runs else None

    def run_metrics(self) -> dict[str, int]:
        with self.session() as s:
            counts = dict(s.execute(select(RunRecord.status, func.count()).group_by(RunRecord.status)).all())
            sums = s.execute(
                select(
                    func.coalesce(func.sum(RunRecord.extracted_count), 0),
                    func.coalesce(func.sum(RunRecord.loaded_count), 0),
                    func.coalesce(func.sum(RunRecord.rejected_count), 0),
                ).where(RunRecord.status == "success")
            ).one()
            pipelines = s.execute(select(func.count()).select_from(PipelineRecord)).scalar_one()
            scheduled = s.execute(
                select(func.count()).select_from(PipelineRecord).where(PipelineRecord.schedule_enabled.is_(True))
            ).scalar_one()
        return {
            "pipelines": int(pipelines),
            "scheduled": int(scheduled),
            "total_runs": int(sum(counts.values())),
            "successful_runs": int(counts.get("success", 0)),
            "failed_runs": int(counts.get("failed", 0)),
            "running_runs": int(counts.get("running", 0)),
            "skipped_runs": int(counts.get("skipped", 0)),
            "records_extracted": int(sums[0]),
            "records_loaded": int(sums[1]),
            "records_rejected": int(sums[2]),
        }

    def runs_per_day(self, days: int = 14) -> pd.DataFrame:
        since = utcnow() - timedelta(days=days)
        with self.session() as s:
            rows = s.execute(
                select(RunRecord.started_at, RunRecord.status, RunRecord.loaded_count).where(RunRecord.started_at >= since)
            ).all()
        frame = pd.DataFrame(rows, columns=["started_at", "status", "loaded"])
        index = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=days, freq="D").date
        if frame.empty:
            return pd.DataFrame({"success": 0, "failed": 0}, index=pd.Index(index, name="day"))
        frame["day"] = pd.to_datetime(frame["started_at"], utc=True).dt.date
        pivot = frame.pivot_table(index="day", columns="status", values="loaded", aggfunc="count", fill_value=0)
        pivot = pivot.reindex(index, fill_value=0)
        for column in ("success", "failed"):
            if column not in pivot.columns:
                pivot[column] = 0
        pivot.index.name = "day"
        return pivot[["success", "failed"]]

    def delete_runs_older_than(self, days: int) -> list[int]:
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as s:
            ids = list(s.scalars(select(RunRecord.id).where(RunRecord.started_at < cutoff, RunRecord.status != "running")))
            if ids:
                s.execute(delete(RunRecord).where(RunRecord.id.in_(ids)))
        return ids

    # -- Locks -------------------------------------------------------------------------
    def acquire_lock(self, pipeline_id: int, stale_after: timedelta) -> str:
        """Take the pipeline's run lock or raise :class:`PipelineLockedError`.

        The lock row's primary key makes acquisition atomic across threads and
        processes. A lock whose heartbeat is older than ``stale_after`` (crashed run)
        is taken over with a compare-and-swap on its token.
        """
        for _ in range(3):
            token = uuid.uuid4().hex
            now = utcnow()
            try:
                with self.session() as s:
                    s.add(PipelineLock(pipeline_id=pipeline_id, token=token, owner=process_owner(),
                                       acquired_at=now, heartbeat_at=now))
                return token
            except IntegrityError:
                pass
            with self.session() as s:
                existing = s.get(PipelineLock, pipeline_id)
                if existing is None:
                    continue  # released in the meantime - try again
                if now - as_utc(existing.heartbeat_at) < stale_after:
                    started = as_utc(existing.acquired_at)
                    raise PipelineLockedError(
                        f"The pipeline is already running (run #{existing.run_id or '?'}, "
                        f"started {started:%Y-%m-%d %H:%M} UTC).",
                        hint="Overlapping runs of the same pipeline are not allowed. Wait for it to finish.",
                    )
                result = s.execute(
                    update(PipelineLock)
                    .where(PipelineLock.pipeline_id == pipeline_id, PipelineLock.token == existing.token)
                    .values(token=token, owner=process_owner(), acquired_at=now, heartbeat_at=now, run_id=None)
                )
                if result.rowcount == 1:
                    return token
        raise PipelineLockedError("The pipeline was started by another process at the same moment.")

    def attach_run_to_lock(self, pipeline_id: int, token: str, run_id: int) -> None:
        with self.session() as s:
            s.execute(update(PipelineLock).where(PipelineLock.pipeline_id == pipeline_id, PipelineLock.token == token)
                      .values(run_id=run_id, heartbeat_at=utcnow()))

    def heartbeat(self, pipeline_id: int, token: str) -> None:
        with self.session() as s:
            s.execute(update(PipelineLock).where(PipelineLock.pipeline_id == pipeline_id, PipelineLock.token == token)
                      .values(heartbeat_at=utcnow()))

    def release_lock(self, pipeline_id: int, token: str) -> None:
        with self.session() as s:
            s.execute(delete(PipelineLock).where(PipelineLock.pipeline_id == pipeline_id, PipelineLock.token == token))

    def running_pipeline_ids(self) -> set[int]:
        with self.session() as s:
            return set(s.scalars(select(PipelineLock.pipeline_id)))

    def recover_interrupted_runs(self, stale_after: timedelta) -> list[int]:
        """Mark runs left 'running' by a stopped process as failed and free their locks."""
        now = utcnow()
        recovered: list[int] = []
        with self.session() as s:
            locks = {lock.run_id: lock for lock in s.scalars(select(PipelineLock))}
            for run in s.scalars(select(RunRecord).where(RunRecord.status == "running")):
                lock = locks.get(run.id)
                alive = lock is not None and now - as_utc(lock.heartbeat_at) < stale_after
                if alive:
                    continue
                run.status = "failed"
                run.finished_at = now
                run.duration_seconds = (now - as_utc(run.started_at)).total_seconds()
                run.error_step = run.error_step or "interrupted"
                run.error_message = "The run was interrupted (the application stopped or crashed). Use Retry to run it again."
                run.log_text = (run.log_text or "") + f"{now:%Y-%m-%d %H:%M:%S}Z ERROR   [run] Run interrupted; marked as failed during recovery.\n"
                recovered.append(run.id)
                if lock is not None:
                    s.delete(lock)
            for lock in s.scalars(select(PipelineLock)):
                if now - as_utc(lock.heartbeat_at) >= stale_after:
                    s.delete(lock)
        return recovered

    # -- Settings ------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            record = s.get(AppSetting, key)
            return default if record is None else record.value

    def set_setting(self, key: str, value: Any) -> None:
        with self.session() as s:
            record = s.get(AppSetting, key)
            if record is None:
                s.add(AppSetting(key=key, value=value))
            else:
                record.value = value
                record.updated_at = utcnow()


def to_config(record: PipelineRecord) -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": record.name,
            "description": record.description or "",
            "source": record.source,
            "transforms": record.transforms or [],
            "rules": record.rules or [],
            "destination": record.destination,
            "schedule": record.schedule,
            "schedule_enabled": bool(record.schedule_enabled),
        }
    )
