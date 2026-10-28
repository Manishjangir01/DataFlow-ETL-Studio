"""Schedule → APScheduler trigger conversion and next-run previews."""

from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.orchestration.pipeline_config import ScheduleConfig


def build_trigger(schedule: ScheduleConfig) -> BaseTrigger:
    """Return the APScheduler trigger for a schedule."""
    if schedule.type == "interval":
        return IntervalTrigger(minutes=schedule.interval_minutes, timezone=schedule.timezone)
    hour, minute = (int(part) for part in schedule.time_of_day.split(":"))
    if schedule.type == "daily":
        return CronTrigger(hour=hour, minute=minute, timezone=schedule.timezone)
    if schedule.type == "weekly":
        return CronTrigger(day_of_week=",".join(schedule.days_of_week), hour=hour, minute=minute, timezone=schedule.timezone)
    return CronTrigger.from_crontab(schedule.cron, timezone=schedule.timezone)


def trigger_signature(schedule: ScheduleConfig) -> str:
    """Stable text used to detect schedule changes."""
    return schedule.model_dump_json()


def next_run_times(schedule: ScheduleConfig, count: int = 3, now: datetime | None = None) -> list[datetime]:
    trigger = build_trigger(schedule)
    current = now or datetime.now(timezone.utc)
    previous = None
    times: list[datetime] = []
    for _ in range(count):
        upcoming = trigger.get_next_fire_time(previous, current)
        if upcoming is None:
            break
        times.append(upcoming)
        previous = upcoming
        current = upcoming
    return times
