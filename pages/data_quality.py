"""Data Quality Reports: per-run quality, validation results and trends."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.orchestration.runner import load_rejected_rows
from src.ui.common import page_header, pipeline_picker, status_label
from src.ui.components.data_preview import render_quality_report
from src.ui.components.rules_builder import render_validation_results
from src.ui.services import get_services

services = get_services()
db = services.db
page_header("Data Quality Reports", "Quality of the extracted and loaded data for every run.")

pipeline = pipeline_picker(services, key="dq_pipeline", include_all=True)
runs = [r for r in db.list_runs(pipeline.id if pipeline else None, limit=300) if r.quality_report]
if not runs:
    st.info("No quality reports yet. Reports are created whenever a pipeline run extracts data.")
    st.stop()

if pipeline is not None and len(runs) > 1:
    st.subheader("Quality trend")
    trend = pd.DataFrame(
        [
            {
                "run": f"#{r.id}",
                "Extracted data": r.quality_report["quality_score"],
                "Loaded data": (r.output_quality or {}).get("quality_score"),
            }
            for r in reversed(runs[:30])
        ]
    ).set_index("run")
    st.line_chart(trend, height=220)

labels = {r.id: f"#{r.id} · {r.pipeline_name} · {status_label(r.status)} · score {r.quality_report['quality_score']:.0f}" for r in runs}
run_id = st.selectbox("Run", list(labels), format_func=labels.get, key="dq_run")
run = next(r for r in runs if r.id == run_id)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Extracted", f"{run.extracted_count:,}")
c2.metric("After transformations", f"{run.transformed_count:,}")
c3.metric("Rejected by rules", f"{run.rejected_count:,}")
c4.metric("Loaded", f"{run.loaded_count:,}")

extracted_tab, loaded_tab, rules_tab = st.tabs(["Extracted data", "Loaded data", "Validation rules"])
with extracted_tab:
    render_quality_report(run.quality_report, title="Quality of the extracted data")
with loaded_tab:
    if run.output_quality:
        render_quality_report(run.output_quality, title="Quality of the data sent to the destination")
    else:
        st.info("This run stopped before the validated data was produced.")
with rules_tab:
    if run.validation_report:
        render_validation_results(run.validation_report, run.transformed_count - run.rejected_count, run.rejected_count)
    else:
        st.info("No validation rules were evaluated in this run.")
    if run.has_rejected_file:
        rejected = load_rejected_rows(services.settings, run.id)
        if rejected is not None:
            st.download_button("Download rejected rows (CSV)", rejected.to_csv(index=False).encode("utf-8"),
                               file_name=f"run_{run.id}_rejected_rows.csv", mime="text/csv", key="dq_dl_rejected")
            st.dataframe(rejected.head(200), hide_index=True)
