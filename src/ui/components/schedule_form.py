"""Schedule settings (interval, daily, weekly or cron) with a next-runs preview."""

from __future__ import annotations

from typing import Any

import streamlit as st
from pydantic import ValidationError

from src.orchestration.pipeline_config import ScheduleConfig
from src.orchestration.scheduling import next_run_times
from src.ui.common import show_error

COMMON_TIMEZONES = [
    "UTC", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Asia/Dubai", "Europe/London", "Europe/Berlin",
    "Europe/Paris", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Australia/Sydney", "Africa/Johannesburg",
]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TYPES = {"interval": "Every N minutes / hours", "daily": "Daily at a time", "weekly": "Weekly on chosen days", "cron": "Cron expression (advanced)"}


def seed_schedule_widgets(schedule: dict[str, Any] | None, enabled: bool, default_tz: str) -> None:
    cfg = ScheduleConfig.model_validate(schedule) if schedule else ScheduleConfig(timezone=default_tz)
    st.session_state.update(
        {
            "sched_enabled": enabled,
            "sched_type": cfg.type,
            "sched_interval": cfg.interval_minutes,
            "sched_time": cfg.time_of_day,
            "sched_days": list(cfg.days_of_week),
            "sched_cron": cfg.cron,
            "sched_tz": cfg.timezone,
        }
    )


def build_schedule() -> ScheduleConfig:
    s = st.session_state
    return ScheduleConfig(
        type=s["sched_type"], interval_minutes=int(s["sched_interval"]), time_of_day=s["sched_time"],
        days_of_week=s["sched_days"] or ["mon"], cron=s["sched_cron"], timezone=s["sched_tz"],
    )


def render_schedule_form(draft: dict[str, Any], default_tz: str, key_prefix: str = "sched") -> None:
    """Edit ``draft['schedule']`` / ``draft['schedule_enabled']`` in place."""
    if "sched_type" not in st.session_state:
        seed_schedule_widgets(draft.get("schedule"), bool(draft.get("schedule_enabled")), default_tz)
    enabled = st.toggle("Run this pipeline automatically on a schedule", key="sched_enabled")
    if not enabled:
        draft["schedule_enabled"] = False
        st.caption("Manual runs only. You can add a schedule later.")
        return
    stype = st.radio("Schedule type", list(TYPES), key="sched_type", horizontal=True, format_func=TYPES.get)
    c1, c2 = st.columns(2)
    if stype == "interval":
        c1.number_input("Run every (minutes)", min_value=1, max_value=60 * 24 * 31, step=5, key="sched_interval",
                        help="60 = hourly, 1440 = daily")
    elif stype in ("daily", "weekly"):
        c1.text_input("Time (HH:MM, 24-hour)", key="sched_time")
        if stype == "weekly":
            st.multiselect("Days", DAYS, key="sched_days", format_func=str.capitalize)
    else:
        c1.text_input("Cron expression (minute hour day month weekday)", key="sched_cron",
                      help="Examples: '0 2 * * *' daily 02:00 · '*/30 * * * *' every 30 minutes · '0 8 * * mon-fri'")
    zones = COMMON_TIMEZONES if st.session_state.get("sched_tz") in COMMON_TIMEZONES else [st.session_state.get("sched_tz")] + COMMON_TIMEZONES
    c2.selectbox("Time zone", zones, key="sched_tz")
    try:
        schedule = build_schedule()
    except ValidationError as exc:
        show_error(exc)
        return
    draft["schedule"] = schedule.model_dump(mode="json")
    draft["schedule_enabled"] = True
    upcoming = ", ".join(t.strftime("%a %d %b %H:%M %Z") for t in next_run_times(schedule))
    st.caption(f"🗓️ {schedule.describe()} · next runs: {upcoming}")
