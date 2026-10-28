"""Running pipelines with live progress, and showing run results and details."""

from __future__ import annotations

import json
from datetime import timezone
from typing import Any

import pandas as pd
import streamlit as st

from src.orchestration.runner import STEPS, RunOutcome, load_rejected_rows
from src.security.redaction import redact_mapping, redact_text
from src.storage.models import RunRecord
from src.ui.common import human_count, human_duration, show_error, status_label
from src.ui.components.data_preview import render_quality_report
from src.ui.components.rules_builder import render_validation_results
from src.ui.services import Services
from src.utils.exceptions import DataFlowError, PipelineLockedError

_STEP_LABELS = dict(STEPS)


def run_with_progress(services: Services, pipeline_id: int, *, retry_of: int | None = None,
                      use_cached_extract: bool = False) -> RunOutcome | None:
    """Run (or retry) a pipeline while showing step-by-step progress."""
    runner = services.runner()
    with st.status("Running pipeline…", expanded=True) as status:
        lines: dict[str, Any] = {key: st.empty() for key, _ in STEPS}
        for key, label in STEPS:
            lines[key].markdown(f"⚪ {label}")

        def progress(key: str, state: str, message: str) -> None:
            icon = {"running": "🔵", "success": "🟢", "failed": "🔴", "skipped": "⚪"}.get(state, "⚪")
            suffix = f" - {message}" if message else ("…" if state == "running" else "")
            lines[key].markdown(f"{icon} **{_STEP_LABELS[key]}**{suffix}")

        try:
            if retry_of is not None:
                outcome = runner.retry(retry_of, use_cached_extract=use_cached_extract, progress=progress)
            else:
                outcome = runner.run(pipeline_id, trigger="manual", progress=progress)
        except PipelineLockedError as exc:
            status.update(label="Pipeline is already running", state="error")
            show_error(exc)
            return None
        except DataFlowError as exc:
            status.update(label="Could not start the run", state="error")
            show_error(exc)
            return None
        if outcome.succeeded:
            status.update(label=f"Run #{outcome.run_id} succeeded in {human_duration(outcome.duration_seconds)}",
                          state="complete", expanded=False)
        else:
            status.update(label=f"Run #{outcome.run_id} failed at '{_STEP_LABELS.get(outcome.error_step or '', outcome.error_step)}'",
                          state="error", expanded=True)
    st.session_state["selected_run_id"] = outcome.run_id
    return outcome


def render_counts(status: str, duration: float | None, extracted: int, transformed: int, rejected: int, loaded: int) -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Status", status_label(status))
    c2.metric("Duration", human_duration(duration))
    c3.metric("Extracted", human_count(extracted))
    c4.metric("Transformed", human_count(transformed))
    c5.metric("Rejected", human_count(rejected))
    c6.metric("Loaded", human_count(loaded))


def render_steps_table(steps: list[dict[str, Any]]) -> None:
    if not steps:
        return
    frame = pd.DataFrame(
        [
            {
                "Step": s.get("label"),
                "Status": status_label(s.get("status")),
                "Duration": human_duration(s.get("duration_seconds")),
                "Details": s.get("message", ""),
            }
            for s in steps
        ]
    )
    st.dataframe(frame, hide_index=True)


def render_outcome(outcome: RunOutcome) -> None:
    render_counts(outcome.status, outcome.duration_seconds, outcome.extracted, outcome.transformed, outcome.rejected, outcome.loaded)
    if outcome.error_message:
        st.error(f"**Failed at {_STEP_LABELS.get(outcome.error_step or '', outcome.error_step)}:** {outcome.error_message}")
    for warning in outcome.warnings:
        st.warning(warning)
    render_steps_table(outcome.steps)


def _local(ts) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def runs_frame(runs: list[RunRecord]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Run": r.id,
                "Pipeline": r.pipeline_name,
                "Status": status_label(r.status),
                "Trigger": r.trigger + (f" (#{r.retry_of})" if r.retry_of else ""),
                "Started": _local(r.started_at),
                "Duration": human_duration(r.duration_seconds),
                "Extracted": r.extracted_count,
                "Transformed": r.transformed_count,
                "Rejected": r.rejected_count,
                "Loaded": r.loaded_count,
                "Error": (r.error_message or "")[:120],
            }
            for r in runs
        ]
    )


def render_run_details(services: Services, run: RunRecord, *, allow_retry: bool = True) -> None:
    """Full details of a stored run: counts, steps, reports, log download and retry."""
    st.markdown(f"#### Run #{run.id} · {run.pipeline_name}")
    st.caption(f"Started {_local(run.started_at)} · finished {_local(run.finished_at)} · trigger: {run.trigger}"
               + (f" (retry of #{run.retry_of})" if run.retry_of else ""))
    render_counts(run.status, run.duration_seconds, run.extracted_count, run.transformed_count, run.rejected_count, run.loaded_count)
    if run.error_message:
        st.error(f"**Failed at {_STEP_LABELS.get(run.error_step or '', run.error_step)}:** {redact_text(run.error_message)}")

    if allow_retry and run.status in ("failed", "skipped") and run.pipeline_id is not None:
        c1, c2, _ = st.columns([1.3, 2.2, 3])
        if c1.button("Retry run", key=f"retry_{run.id}", icon=":material/replay:", type="primary"):
            outcome = run_with_progress(services, run.pipeline_id, retry_of=run.id)
            if outcome:
                render_outcome(outcome)
        if run.has_cached_extract and c2.button("Retry with the same extracted data", key=f"retry_cached_{run.id}",
                                                icon=":material/cached:", help="Skips the API call and re-uses this run's data"):
            outcome = run_with_progress(services, run.pipeline_id, retry_of=run.id, use_cached_extract=True)
            if outcome:
                render_outcome(outcome)

    tabs = st.tabs(["Steps", "Data quality", "Transformations", "Validation", "Load", "Log", "Configuration"])
    with tabs[0]:
        render_steps_table(run.steps or [])
    with tabs[1]:
        if run.quality_report:
            render_quality_report(run.quality_report, title="Extracted data")
        if run.output_quality:
            render_quality_report(run.output_quality, title="Loaded data (after transformations and validation)")
        if not run.quality_report:
            st.info("No quality report (the run stopped before profiling).")
    with tabs[2]:
        if run.transform_report:
            st.dataframe(pd.DataFrame([
                {"#": r["index"] + 1, "Step": r["label"], "Details": r["description"],
                 "Rows": f"{r['rows_before']:,} → {r['rows_after']:,}", "Result": r["message"],
                 "Warnings": "; ".join(r.get("warnings") or []), "ms": r.get("duration_ms")}
                for r in run.transform_report
            ]), hide_index=True)
        else:
            st.info("No transformation report.")
    with tabs[3]:
        if run.validation_report:
            render_validation_results(run.validation_report)
        else:
            st.info("No validation rules were evaluated.")
        if run.has_rejected_file:
            rejected = load_rejected_rows(services.settings, run.id)
            if rejected is not None:
                st.download_button("Download rejected rows (CSV)", rejected.to_csv(index=False).encode("utf-8"),
                                   file_name=f"run_{run.id}_rejected_rows.csv", mime="text/csv", key=f"dl_rej_{run.id}")
                st.dataframe(rejected.head(100), hide_index=True)
    with tabs[4]:
        if run.load_report:
            report = run.load_report
            st.markdown(
                f"**Target:** {report['target']}  \n**Strategy:** {report['strategy']}  \n"
                f"**Rows loaded:** {report['rows_loaded']:,} (inserted {report.get('rows_inserted')}, updated {report.get('rows_updated')})  \n"
                f"**Verified:** {'✅ ' + report['verification'] if report.get('verified') else '❌ not verified'}"
            )
            for warning in report.get("warnings") or []:
                st.warning(warning)
        else:
            st.info("Nothing was loaded in this run.")
    with tabs[5]:
        log_text = redact_text(run.log_text or "")
        st.download_button("Download log", log_text.encode("utf-8"), file_name=f"dataflow_run_{run.id}.log",
                           mime="text/plain", key=f"dl_log_{run.id}", icon=":material/download:")
        st.code(log_text or "(empty)", language="log")
    with tabs[6]:
        st.caption("Configuration used by this run. Secrets are stored encrypted and never shown.")
        st.code(json.dumps(redact_mapping(run.config_snapshot or {}), indent=2, default=str), language="json")
