"""The complete, validated definition of a pipeline."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.connectors.models import SourceConfig
from src.loading.models import DestinationConfig
from src.transformations.engine import validate_steps
from src.validation.rules import parse_rule

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleConfig(BaseModel):
    """When a pipeline runs automatically."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["interval", "daily", "weekly", "cron"] = "daily"
    interval_minutes: int = Field(default=60, ge=1, le=60 * 24 * 31)
    time_of_day: str = "02:00"                      # daily / weekly
    days_of_week: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=lambda: ["mon"])
    cron: str = "0 2 * * *"
    timezone: str = "UTC"

    @field_validator("time_of_day")
    @classmethod
    def _time(cls, value: str) -> str:
        if not _TIME.match(value.strip()):
            raise ValueError("Time must be HH:MM in 24-hour format, e.g. 02:30.")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone '{value}'.") from exc
        return value

    @model_validator(mode="after")
    def _cron(self) -> "ScheduleConfig":
        if self.type == "cron":
            from apscheduler.triggers.cron import CronTrigger

            try:
                CronTrigger.from_crontab(self.cron.strip(), timezone=self.timezone)
            except ValueError as exc:
                raise ValueError(f"Invalid cron expression: {exc}") from exc
            self.cron = self.cron.strip()
        if self.type == "weekly" and not self.days_of_week:
            raise ValueError("Choose at least one day of the week.")
        return self

    def describe(self) -> str:
        if self.type == "interval":
            minutes = self.interval_minutes
            if minutes % 1440 == 0:
                every = f"{minutes // 1440} day(s)"
            elif minutes % 60 == 0:
                every = f"{minutes // 60} hour(s)"
            else:
                every = f"{minutes} minute(s)"
            return f"Every {every}"
        if self.type == "daily":
            return f"Daily at {self.time_of_day} ({self.timezone})"
        if self.type == "weekly":
            return f"Weekly on {', '.join(d.capitalize() for d in self.days_of_week)} at {self.time_of_day} ({self.timezone})"
        return f"Cron '{self.cron}' ({self.timezone})"


class PipelineConfig(BaseModel):
    """Source, transformations, validation rules, destination and schedule."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    source: SourceConfig
    transforms: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    destination: DestinationConfig
    schedule: ScheduleConfig | None = None
    schedule_enabled: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("The pipeline needs a name.")
        return value

    @field_validator("transforms")
    @classmethod
    def _steps(cls, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from src.utils.exceptions import TransformationError

        try:
            validate_steps(steps)
        except TransformationError as exc:
            raise ValueError(exc.message) from exc
        return steps

    @field_validator("rules")
    @classmethod
    def _rules(cls, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [parse_rule(r).model_dump(mode="json") for r in rules]

    @model_validator(mode="after")
    def _schedule(self) -> "PipelineConfig":
        if self.schedule_enabled and self.schedule is None:
            raise ValueError("Scheduling is enabled but no schedule is configured.")
        return self
