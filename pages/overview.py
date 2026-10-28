"""Overview dashboard: metrics, recent activity and pipelines."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.connectors.models import SourceConfig
from src.loading.models import DestinationConfig
from src.orchestration.pipeline_config import ScheduleConfig
from src.ui.common import human_count, page_header, show_error, status_label
from src.ui.components.run_view import render_outcome, run_with_progress, runs_frame
from src.ui.drafts import load_pipeline_into_draft, start_new_pipeline
from src.ui.services import get_services

services = get_services()
db = services.db
page_header("Overview", "Your pipelines and how they have been running.")

if services.recovered_runs:
    st.warning(f"{len(services.recovered_runs)} run(s) were interrupted when the app last stopped and were marked as failed: "
               + ", ".join(f"#{r}" for r in services.recovered_runs) + ". You can retry them under Pipeline Runs.")

metrics = db.run_metrics()
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Pipelines", metrics["pipelines"], f"{metrics['scheduled']} scheduled", delta_color="off")
c2.metric("Total runs", human_count(metrics["total_runs"]))
c3.metric("Successful runs", human_count(metrics["successful_runs"]))
c4.metric("Failed runs", human_count(metrics["failed_runs"]))
c5.metric("Records processed", human_count(metrics["records_extracted"]))
c6.metric("Records loaded", human_count(metrics["records_loaded"]))

pipelines = db.list_pipelines()
if not pipelines:
    st.subheader("Get started in three steps")
    g1, g2, g3 = st.columns(3)
    with g1.container(border=True):
        st.markdown("**1. Pick a source**  \nUpload a CSV / JSON / Parquet file, use a bundled sample, or connect a REST API.")
    with g2.container(border=True):
        st.markdown("**2. Clean the data**  \nRemove duplicates, fix types and dates, filter rows and add validation rules - with a live preview.")
    with g3.container(border=True):
        st.markdown("**3. Load and schedule**  \nWrite to CSV, Parquet, SQLite or PostgreSQL, then run it on demand or on a schedule.")
    if st.button("Create your first pipeline", type="primary", icon=":material/add_circle:", key="ov_create"):
        start_new_pipeline()
        st.switch_page("pages/create_pipeline.py")
    st.stop()

left, right = st.columns([3, 2])
with left:
    st.subheader("Runs in the last 14 days")
    per_day = db.runs_per_day(14)
    per_day.index = pd.to_datetime(per_day.index).strftime("%d %b")
    chart = per_day.rename(columns={"success": "Successful", "failed": "Failed"})[["Failed", "Successful"]]
    st.bar_chart(chart, color=["#ef4444", "#22c55e"], height=260, stack=True)
with right:
    st.subheader("Recent activity")
    recent = db.list_runs(limit=8)
    if recent:
        frame = runs_frame(recent)[["Run", "Pipeline", "Status", "Started", "Loaded"]]
        st.dataframe(frame, hide_index=True, height=260)
    else:
        st.info("No runs yet. Run a pipeline below.")

st.subheader("Pipelines")
locked = db.running_pipeline_ids()
rows = []
for p in pipelines:
    last = db.last_run(p.id)
    try:
        source = SourceConfig.model_validate(p.source).summary()
        destination = DestinationConfig.model_validate(p.destination).target_label()
    except Exception:  # noqa: BLE001 - show broken configs instead of crashing
        source, destination = "⚠️ invalid", "⚠️ invalid"
    schedule = "-"
    if p.schedule:
        schedule = ScheduleConfig.model_validate(p.schedule).describe() + ("" if p.schedule_enabled else " (paused)")
    rows.append({
        "ID": p.id, "Name": p.name, "Source": source, "Destination": destination, "Schedule": schedule,
        "Last run": ("🔵 Running" if p.id in locked else status_label(last.status)) if last or p.id in locked else "Never run",
        "Last loaded": last.loaded_count if last else None,
    })
st.dataframe(pd.DataFrame(rows), hide_index=True)

options = {p.id: f"{p.name} (#{p.id})" for p in pipelines}
chosen = st.selectbox("Choose a pipeline", list(options), format_func=options.get, key="ov_pipeline")
b1, b2, b3, _ = st.columns([1, 1.2, 1, 3])
run_clicked = b1.button("Run now", type="primary", icon=":material/play_arrow:", key="ov_run")
edit_clicked = b2.button("Edit in wizard", icon=":material/edit:", key="ov_edit")
delete_clicked = b3.button("Delete", icon=":material/delete:", key="ov_delete")

if run_clicked:
    outcome = run_with_progress(services, chosen)
    if outcome:
        render_outcome(outcome)
if edit_clicked:
    load_pipeline_into_draft(services, chosen)
    st.switch_page("pages/create_pipeline.py")
if delete_clicked:
    st.session_state["ov_confirm_delete"] = chosen
if st.session_state.get("ov_confirm_delete") == chosen:
    with st.container(border=True):
        st.warning(f"Delete **{options[chosen]}**? Its run history is kept, but the pipeline and its schedule are removed. "
                   "Data already loaded into the destination is not touched.")
        d1, d2, _ = st.columns([1, 1, 4])
        if d1.button("Yes, delete", type="primary", key="ov_delete_yes"):
            try:
                db.delete_pipeline(chosen)
                if services.scheduler is not None:
                    services.scheduler.sync_now()
                st.session_state.pop("ov_confirm_delete", None)
                st.success("Pipeline deleted.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
        if d2.button("Cancel", key="ov_delete_no"):
            st.session_state.pop("ov_confirm_delete", None)
            st.rerun()
