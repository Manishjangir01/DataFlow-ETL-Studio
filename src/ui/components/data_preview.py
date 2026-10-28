"""Raw data preview, schema detection, data quality report and column selection."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.ui.common import dataframe_for_display
from src.validation.profiler import QualityReport, profile_dataframe

PROFILE_CACHE_KEY = "draft_raw_profile"
PREVIEW_SELECTION_NOTE = "preview-selection"


def cached_profile(df: pd.DataFrame, cache_key: str = PROFILE_CACHE_KEY) -> QualityReport:
    """Profile ``df`` once per extraction (re-used across reruns)."""
    cached = st.session_state.get(cache_key)
    if cached and cached[0] == id(df):
        return cached[1]
    report = profile_dataframe(df)
    st.session_state[cache_key] = (id(df), report)
    return report


def render_quality_report(report: QualityReport | dict[str, Any], *, title: str = "Data quality report") -> None:
    """Render a quality report (object or the dict stored with a run)."""
    data = report.to_dict() if isinstance(report, QualityReport) else report
    st.subheader(title)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Quality score", f"{data['quality_score']:.0f} / 100")
    c2.metric("Duplicate rows", f"{data['duplicate_rows']:,}")
    c3.metric("Missing cells", f"{data['missing_cells']:,}", f"{data['missing_pct']}% of cells", delta_color="off")
    c4.metric("Invalid values", f"{data['invalid_cells']:,}")
    if data.get("sampled"):
        st.caption("Large dataset: column statistics are estimated from a 50,000 row sample.")

    issues = data.get("issues") or []
    if issues:
        with st.expander(f"⚠️ {len(issues)} issue(s) found", expanded=len(issues) <= 8):
            for issue in issues:
                st.markdown(f"- {issue}")
    else:
        st.success("No data quality issues detected.")

    columns = pd.DataFrame(
        [
            {
                "Column": c["name"],
                "Detected type": c["inferred_type"],
                "Missing": c["missing"],
                "Missing %": c["missing_pct"],
                "Unique": c["unique"],
                "Invalid": c["invalid"],
                "Invalid examples": ", ".join(c.get("invalid_examples") or []),
                "Min": c.get("min"),
                "Max": c.get("max"),
                "Sample values": ", ".join(c.get("sample_values") or []),
            }
            for c in data["columns"]
        ]
    )
    if not columns.empty:
        st.markdown("**Schema and column statistics**")
        st.dataframe(
            columns,
            hide_index=True,
            column_config={
                "Missing %": st.column_config.ProgressColumn("Missing %", min_value=0, max_value=100, format="%.1f%%"),
            },
        )


def render_raw_preview(df: pd.DataFrame, meta: dict | None = None) -> None:
    meta = meta or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Records", f"{len(df):,}")
    c2.metric("Columns", f"{len(df.columns):,}")
    origin = meta.get("file") or meta.get("url") or meta.get("origin", "-")
    c3.metric("Source", str(origin)[:40])
    for warning in meta.get("warnings") or []:
        st.warning(warning)
    if meta.get("records_path"):
        st.caption(f"Records taken from the field `{meta['records_path']}`.")
    st.markdown("**Sample of the raw data** (first 100 rows)")
    st.dataframe(dataframe_for_display(df, 100), hide_index=True)
    st.caption("The original extracted data is kept unchanged; transformations work on a copy.")


def current_column_selection(draft: dict, all_columns: list[str]) -> list[str]:
    for step in draft.get("transforms", []):
        if step.get("type") == "select_columns" and step.get("note") == PREVIEW_SELECTION_NOTE:
            return [c for c in step.get("columns", []) if c in all_columns]
    return list(all_columns)


def apply_column_selection(draft: dict, keep: list[str], all_columns: list[str]) -> None:
    """Store the kept columns as the first transformation step (or remove it)."""
    steps = [s for s in draft.get("transforms", []) if s.get("note") != PREVIEW_SELECTION_NOTE]
    if keep and len(keep) < len(all_columns):
        steps.insert(
            0,
            {"type": "select_columns", "columns": keep, "enabled": True, "note": PREVIEW_SELECTION_NOTE},
        )
    draft["transforms"] = steps


def render_column_selector(df: pd.DataFrame, draft: dict) -> None:
    all_columns = [str(c) for c in df.columns]
    st.subheader("Choose columns to keep")
    current = current_column_selection(draft, all_columns)
    with st.form("col_select_form", border=False):
        keep = st.multiselect(
            "Columns to keep (removed columns are dropped before other transformations)",
            all_columns,
            default=current,
            key="col_keep",
        )
        if st.form_submit_button("Apply column selection"):
            if not keep:
                st.error("Keep at least one column.")
            else:
                apply_column_selection(draft, keep, all_columns)
                removed = len(all_columns) - len(keep)
                st.success(f"Keeping {len(keep)} column(s), removing {removed}.")
