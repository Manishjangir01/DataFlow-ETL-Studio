"""Data Sources: the sources of all pipelines, with testing and editing."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.connectors.models import AUTH_LABELS, PAGINATION_LABELS, SourceConfig
from src.ui.common import page_header, pipeline_picker, show_error
from src.ui.components.data_preview import cached_profile, render_quality_report, render_raw_preview
from src.ui.drafts import extract_preview, load_pipeline_into_draft, start_new_pipeline
from src.ui.services import get_services

services = get_services()
db = services.db
page_header("Data Sources", "Files and REST APIs your pipelines read from.")

c1, c2 = st.columns([1, 4])
if c1.button("Add a new source", type="primary", icon=":material/add:", key="ds_new"):
    start_new_pipeline()
    st.switch_page("pages/create_pipeline.py")
policy = services.url_policy()
with c2:
    domains = ", ".join(policy.allowed_domains) if policy.allowed_domains else "any public domain"
    st.caption(f"🔒 API sources may call: **{domains}** · https only{' (http allowed)' if policy.allow_http else ''} · "
               "localhost, private networks and cloud metadata addresses are always blocked.")

pipelines = db.list_pipelines()
if not pipelines:
    st.info("No sources yet - create a pipeline to add one.")
    st.stop()

rows = []
for p in pipelines:
    try:
        source = SourceConfig.model_validate(p.source)
    except Exception:  # noqa: BLE001
        rows.append({"Pipeline": p.name, "Source": "⚠️ invalid configuration"})
        continue
    if source.kind == "api" and source.api:
        api = source.api
        auth = AUTH_LABELS[api.auth.type]
        if api.auth.secret:
            auth += f" ({api.auth.secret.describe()})"
        rows.append({
            "Pipeline": p.name, "Source": source.name, "Type": "REST API", "Location": f"{api.method} {api.display_url()}",
            "Authentication": auth, "Pagination": PAGINATION_LABELS[api.pagination.type].split(" (")[0],
            "Records field": api.records_path or "auto", "Limit": f"{api.max_records:,} records",
        })
    elif source.file:
        rows.append({
            "Pipeline": p.name, "Source": source.name, "Type": f"{source.file.file_format.upper()} file",
            "Location": source.file.original_name, "Authentication": "-", "Pagination": "-",
            "Records field": source.file.records_path or "-", "Limit": "-",
        })
st.dataframe(pd.DataFrame(rows), hide_index=True)

st.subheader("Test a source")
pipeline = pipeline_picker(services, key="ds_pipeline")
if pipeline is None:
    st.stop()
b1, b2, _ = st.columns([1.3, 1.3, 3])
if b1.button("Test source (preview)", key="ds_test", icon=":material/network_check:"):
    try:
        with st.spinner("Extracting a preview sample..."):
            df, meta = extract_preview(services, pipeline.source)
        st.session_state["ds_preview"] = (pipeline.id, df, meta)
    except Exception as exc:  # noqa: BLE001
        show_error(exc, "The source test failed")
        st.session_state.pop("ds_preview", None)
if b2.button("Edit source in wizard", key="ds_edit", icon=":material/edit:"):
    load_pipeline_into_draft(services, pipeline.id, step=0)
    st.switch_page("pages/create_pipeline.py")

preview = st.session_state.get("ds_preview")
if preview and preview[0] == pipeline.id:
    st.success("✅ The source responded and returned data.")
    render_raw_preview(preview[1], preview[2])
    render_quality_report(cached_profile(preview[1], cache_key="ds_profile"))
