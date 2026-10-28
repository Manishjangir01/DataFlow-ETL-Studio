"""Create Pipeline wizard.

Configure Source → Extract → Preview → Transform → Validate → Configure Destination →
Review, save and run (Load) → View results.
"""

from __future__ import annotations

import streamlit as st
from pydantic import ValidationError

from src.connectors.models import SourceConfig
from src.loading.models import DestinationConfig, LoadStrategy
from src.transformations.steps import describe_step
from src.ui.common import page_header, show_error
from src.ui.components.api_source import render_api_source
from src.ui.components.data_preview import cached_profile, render_column_selector, render_quality_report, render_raw_preview
from src.ui.components.destination_form import render_destination_form
from src.ui.components.file_source import render_file_source
from src.ui.components.rules_builder import render_rules_builder
from src.ui.components.run_view import render_outcome, run_with_progress
from src.ui.components.schedule_form import render_schedule_form
from src.ui.components.transform_builder import render_transform_builder
from src.ui.components.wizard_helpers import render_step_indicator, transformed_sample
from src.ui.drafts import draft_to_config, reload_draft_sample, start_new_pipeline
from src.ui.services import get_services
from src.ui.state import WIZARD_STEPS, get_draft, get_raw_data, get_raw_meta, get_wizard_step, go_to_step
from src.validation.rules import describe_rule

services = get_services()
draft = get_draft()
step = get_wizard_step()
raw = get_raw_data()

page_header("Create Pipeline", "Build an ETL pipeline step by step - no code required.")

top_left, top_right = st.columns([4, 1])
if draft.get("pipeline_id"):
    top_left.info(f"Editing saved pipeline **{draft.get('name')}** (#{draft['pipeline_id']}). Saving updates it.")
if top_right.button("Start a new pipeline", key="wiz_new", icon=":material/restart_alt:"):
    start_new_pipeline()
    st.rerun()

render_step_indicator(step)

if step > 0 and raw is None and draft.get("source"):
    st.warning("No sample data is loaded for this pipeline yet.")
    if st.button("Load a preview sample from the source", key="wiz_reload", icon=":material/download:"):
        try:
            with st.spinner("Extracting a preview sample..."):
                reload_draft_sample(services, draft)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc, "Could not extract the sample")

# ---------------------------------------------------------------------------------
if step == 0:
    st.subheader("1. Configure the source")
    saved_source = draft.get("source") or {}
    if "src_kind" not in st.session_state and saved_source:
        st.session_state["src_kind"] = saved_source.get("kind", "file")
    if "src_name" not in st.session_state and saved_source:
        st.session_state["src_name"] = saved_source.get("name", "")
    source_name = st.text_input("Source name", key="src_name", placeholder="e.g. Customers export")
    kind = st.radio("Source type", ["file", "api"], key="src_kind", horizontal=True,
                    format_func=lambda k: "Upload a file (CSV, JSON, Parquet)" if k == "file" else "REST API")
    extracted = render_file_source(services, draft, source_name) if kind == "file" else render_api_source(services, draft, source_name)
    if extracted:
        go_to_step(1)
        st.rerun()
    if raw is not None:
        st.success(f"Data extracted: {len(raw):,} records. Continue to the preview, or extract again.")

elif step == 1:
    st.subheader("2. Preview and data quality")
    if raw is not None:
        render_raw_preview(raw, get_raw_meta())
        render_quality_report(cached_profile(raw))
        render_column_selector(raw, draft)

elif step == 2:
    st.subheader("3. Transform")
    st.caption("Steps run top to bottom on a copy of the extracted data. The preview updates after every change.")
    render_transform_builder(draft["transforms"], raw, prefix="tf")

elif step == 3:
    st.subheader("4. Validate")
    st.caption("Rules run after the transformations. Rejected rows are kept out of the destination and can be downloaded.")
    result = transformed_sample(raw, draft["transforms"])
    if raw is not None and result is None:
        st.error("The transformations currently fail - go back to Transform to fix them.")
    render_rules_builder(draft["rules"], result.dataframe if result else None, prefix="rule")

elif step == 4:
    st.subheader("5. Configure the destination")
    result = transformed_sample(raw, draft["transforms"])
    columns = [str(c) for c in result.dataframe.columns] if result else []
    render_destination_form(services, draft, columns)

elif step == 5:
    st.subheader("6. Review, save and run")
    if "pl_name" not in st.session_state:
        st.session_state["pl_name"] = draft.get("name", "")
        st.session_state["pl_desc"] = draft.get("description", "")
    draft["name"] = st.text_input("Pipeline name", key="pl_name", max_chars=120)
    draft["description"] = st.text_area("Description (optional)", key="pl_desc", height=80)

    c1, c2, c3, c4 = st.columns(4)
    with c1.container(border=True):
        st.markdown("**Source**")
        if draft.get("source"):
            source = SourceConfig.model_validate(draft["source"])
            st.caption(f"{source.name}  \n{source.summary()}")
        else:
            st.caption("❌ Not configured")
    with c2.container(border=True):
        st.markdown(f"**Transformations ({len(draft['transforms'])})**")
        for i, s in enumerate(draft["transforms"][:8]):
            try:
                st.caption(f"{i + 1}. {describe_step(s)}")
            except ValidationError:
                st.caption(f"{i + 1}. ⚠️ invalid")
    with c3.container(border=True):
        st.markdown(f"**Validation rules ({len(draft['rules'])})**")
        for r in draft["rules"][:8]:
            st.caption(f"- {r.get('description') or describe_rule(r)}")
    with c4.container(border=True):
        st.markdown("**Destination**")
        if draft.get("destination"):
            dest = DestinationConfig.model_validate(draft["destination"])
            st.caption(f"{dest.target_label()}  \nStrategy: {dest.strategy.value}")
            if dest.strategy == LoadStrategy.REPLACE:
                st.caption("⚠️ Replaces existing data on every run")
        else:
            st.caption("❌ Not configured - go back to step 5 and save it")

    st.markdown("#### Schedule (optional)")
    render_schedule_form(draft, services.settings.timezone)

    config = None
    try:
        config = draft_to_config(draft)
    except ValidationError as exc:
        show_error(exc, "The pipeline is not complete yet")

    b1, b2, _ = st.columns([1.2, 1.6, 3])
    save = b1.button("Save pipeline", key="pl_save", icon=":material/save:", disabled=config is None)
    save_run = b2.button("Save and run now", key="pl_save_run", type="primary", icon=":material/play_arrow:", disabled=config is None)
    if (save or save_run) and config is not None:
        pipeline_id = None
        try:
            pipeline_id = services.db.save_pipeline(config, draft.get("pipeline_id"))
            draft["pipeline_id"] = pipeline_id
            if services.scheduler is not None:
                services.scheduler.sync_now()
            scheduled = f" Scheduled: {config.schedule.describe()}." if config.schedule_enabled and config.schedule else ""
            st.success(f"Pipeline **{config.name}** saved (#{pipeline_id}).{scheduled}")
        except Exception as exc:  # noqa: BLE001
            show_error(exc, "Could not save the pipeline")
        if save_run and pipeline_id:
            outcome = run_with_progress(services, pipeline_id)
            if outcome:
                render_outcome(outcome)
                st.page_link("pages/pipeline_runs.py", label="Open the run history and logs", icon=":material/history:")

# --- Navigation ---------------------------------------------------------------------
st.divider()
back, _, forward = st.columns([1, 4, 1])
if step > 0 and back.button("← Back", key="wiz_back"):
    go_to_step(step - 1)
    st.rerun()
can_continue = raw is not None or (step >= 1 and draft.get("source") is not None)
if step < len(WIZARD_STEPS) - 1 and forward.button("Next →", key="wiz_next", type="primary", disabled=not can_continue):
    go_to_step(step + 1)
    st.rerun()
