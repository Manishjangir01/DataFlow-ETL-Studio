"""ORM models for the application metadata database (SQLite or PostgreSQL)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class PipelineRecord(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    transforms: Mapped[list[Any]] = mapped_column(JSON, default=list)
    rules: Mapped[list[Any]] = mapped_column(JSON, default=list)
    destination: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunRecord(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_runs_pipeline_started", "pipeline_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id", ondelete="SET NULL"), nullable=True)
    pipeline_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # running | success | failed | skipped
    trigger: Mapped[str] = mapped_column(String(20), default="manual")         # manual | schedule | retry | cli
    retry_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    extracted_count: Mapped[int] = mapped_column(Integer, default=0)
    transformed_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    loaded_count: Mapped[int] = mapped_column(Integer, default=0)
    error_step: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    transform_report: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    validation_report: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    load_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    has_cached_extract: Mapped[bool] = mapped_column(Boolean, default=False)
    has_rejected_file: Mapped[bool] = mapped_column(Boolean, default=False)
    log_text: Mapped[str] = mapped_column(Text, default="")


class PipelineLock(Base):
    """One row per running pipeline; the primary key makes the lock atomic."""

    __tablename__ = "pipeline_locks"

    pipeline_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
