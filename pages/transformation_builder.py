"""Transformation Builder: edit a pipeline's transformations against live sample data."""

from __future__ import annotations

import copy

import streamlit as st

from src.storage.repository import to_config
from src.transformations.steps import STEP_LABELS
from src.ui.common import page_header, pipeline_picker, show_error
from src.ui.components.transform_builder import STEP_HELP, render_transform_builder
from src.ui.drafts import extract_preview
from src.ui.services import get_services
from src.ui.state import get_draft, get_raw_data

services = get_services()
db = services.db
page_header("Transformation Builder", "Design cleaning steps with an instant preview. The original data is never changed.")

draft = get_draft()
has_draft = get_raw_data() is not None and draft.get("source") is not None
modes = (["draft"] if has_draft else []) + (["saved"] if db.list_pipelines() else [])
if not modes:
    st.info("Create a pipeline (or extract data in Create Pipeline) to start building transformations.")
    st.page_link("pages/create_pipeline.py", label="Create Pipeline", icon=":material/add_circle:")
    with st.expander("Available transformations"):
        for key, label in STEP_LABELS.items():
            st.markdown(f"- **{label}** - {STEP_HELP.get(key, '')}")
    st.stop()

mode = st.radio("Work on", modes, horizontal=True, key="tb_mode",
                format_func=lambda m: "The pipeline being created" if m == "draft" else "A saved pipeline")

if mode == "draft":
    st.caption(f"Changes apply to the draft **{draft.get('name') or 'new pipeline'}** in Create Pipeline.")
    render_transform_builder(draft["transforms"], get_raw_data(), prefix="tf")
    st.stop()

pipeline = pipeline_picker(services, key="tb_pipeline")
if pipeline is None:
    st.stop()
steps_key, raw_key = f"tb_steps_{pipeline.id}", f"tb_raw_{pipeline.id}"
if steps_key not in st.session_state:
    st.session_state[steps_key] = copy.deepcopy(pipeline.transforms or [])
steps = st.session_state[steps_key]

c1, c2, c3, _ = st.columns([1.4, 1.4, 1.2, 2])
if c1.button("Load sample data", key="tb_load", icon=":material/download:"):
    try:
        with st.spinner("Extracting a preview sample..."):
            df, _meta = extract_preview(services, pipeline.source)
        st.session_state[raw_key] = df
    except Exception as exc:  # noqa: BLE001
        show_error(exc, "Could not load sample data")
dirty = steps != (pipeline.transforms or [])
if c2.button("Save transformations", key="tb_save", type="primary", icon=":material/save:", disabled=not dirty):
    try:
        config = to_config(pipeline).model_copy(update={"transforms": copy.deepcopy(steps)})
        type(config).model_validate(config.model_dump())  # re-validate the steps
        db.save_pipeline(config, pipeline.id)
        st.success("Transformations saved. The next run uses them.")
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        show_error(exc, "Could not save")
if c3.button("Discard changes", key="tb_discard", disabled=not dirty):
    st.session_state[steps_key] = copy.deepcopy(pipeline.transforms or [])
    st.rerun()
if dirty:
    st.caption("✏️ Unsaved changes")

raw = st.session_state.get(raw_key)
if raw is None:
    st.info("Click **Load sample data** to preview the transformations on real data.")
render_transform_builder(steps, raw, prefix=f"tb{pipeline.id}")
