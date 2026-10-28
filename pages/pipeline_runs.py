"""Pipeline Runs: run on demand, manage schedules, browse history, logs and retries."""

from __future__ import annotations

import streamlit as st

from src.orchestration.pipeline_config import ScheduleConfig
from src.orchestration.scheduling import next_run_times
from src.storage.repository import to_config
from src.ui.common import page_header, pipeline_picker, show_error, status_label
from src.ui.components.run_view import render_outcome, render_run_details, run_with_progress, runs_frame
from src.ui.components.schedule_form import render_schedule_form, seed_schedule_widgets
from src.ui.services import get_services

services = get_services()
db = services.db
page_header("Pipeline Runs", "Run pipelines, manage schedules and inspect every execution.")

if not db.list_pipelines():
    st.info("There are no pipelines yet. Create one first.")
    st.page_link("pages/create_pipeline.py", label="Create Pipeline", icon=":material/add_circle:")
    st.stop()

pipeline = pipeline_picker(services, key="runs_pipeline", include_all=True)

if pipeline is not None:
    with st.container(border=True):
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"### {pipeline.name}")
            if pipeline.description:
                st.caption(pipeline.description)
            running = pipeline.id in db.running_pipeline_ids()
            if running:
                st.info("🔵 This pipeline is running right now. Overlapping runs are blocked.")
            if st.button("Run now", type="primary", icon=":material/play_arrow:", key="runs_run_now", disabled=running):
                outcome = run_with_progress(services, pipeline.id)
                if outcome:
                    render_outcome(outcome)
        with right:
            st.markdown("**Schedule**")
            if pipeline.schedule:
                schedule = ScheduleConfig.model_validate(pipeline.schedule)
                state = "active" if pipeline.schedule_enabled else "paused"
                st.markdown(f"{schedule.describe()} · **{state}**")
                if pipeline.schedule_enabled:
                    next_times = next_run_times(schedule, 1)
                    job_next = services.scheduler.next_run_time(pipeline.id) if services.scheduler else None
                    shown = job_next or (next_times[0] if next_times else None)
                    if shown:
                        st.caption(f"Next run: {shown.strftime('%a %d %b %Y %H:%M %Z')}")
                    if services.scheduler is None and not services.settings.embedded_scheduler:
                        st.caption("Runs are executed by the separate scheduler worker.")
                toggle_label = "Pause schedule" if pipeline.schedule_enabled else "Resume schedule"
                toggle_icon = ":material/pause:" if pipeline.schedule_enabled else ":material/play_circle:"
                if st.button(toggle_label, key="runs_toggle_schedule", icon=toggle_icon):
                    try:
                        db.set_schedule_enabled(pipeline.id, not pipeline.schedule_enabled)
                        if services.scheduler is not None:
                            services.scheduler.sync_now()
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        show_error(exc)
            else:
                st.caption("No schedule - manual runs only.")
            with st.expander("Change schedule"):
                draft = {"schedule": pipeline.schedule, "schedule_enabled": pipeline.schedule_enabled}
                marker = f"sched_loaded_for_{pipeline.id}"
                if not st.session_state.get(marker):
                    for key in [k for k in st.session_state if str(k).startswith("sched_")]:
                        del st.session_state[key]
                    seed_schedule_widgets(pipeline.schedule, bool(pipeline.schedule_enabled), services.settings.timezone)
                    st.session_state[marker] = True
                render_schedule_form(draft, services.settings.timezone)
                if st.button("Save schedule", key="runs_save_schedule", icon=":material/save:"):
                    try:
                        config = to_config(pipeline).model_copy(
                            update={"schedule": ScheduleConfig.model_validate(draft["schedule"]) if draft.get("schedule") else None,
                                    "schedule_enabled": bool(draft.get("schedule_enabled"))}
                        )
                        db.save_pipeline(config, pipeline.id)
                        if services.scheduler is not None:
                            services.scheduler.sync_now()
                        st.session_state.pop(marker, None)
                        st.success("Schedule saved.")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        show_error(exc)

st.subheader("Execution history")
f1, f2 = st.columns([1, 3])
status_filter = f1.selectbox("Status", ["all", "success", "failed", "running", "skipped"], key="runs_status",
                             format_func=lambda s: "All statuses" if s == "all" else status_label(s))
runs = db.list_runs(pipeline.id if pipeline else None, limit=300, status=None if status_filter == "all" else status_filter)
if not runs:
    st.info("No runs match. Use 'Run now' to execute the pipeline.")
    st.stop()

st.dataframe(runs_frame(runs), hide_index=True, height=min(420, 38 + 35 * len(runs)))

run_ids = [r.id for r in runs]
preferred = st.session_state.get("selected_run_id")
if st.session_state.get("runs_selected") not in run_ids:
    st.session_state["runs_selected"] = preferred if preferred in run_ids else run_ids[0]
labels = {r.id: f"#{r.id} · {r.pipeline_name} · {r.status}" for r in runs}
selected = st.selectbox("Show details for run", run_ids, key="runs_selected", format_func=labels.get)
st.divider()
render_run_details(services, db.get_run(selected))
