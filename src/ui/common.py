"""Small Streamlit helpers shared by every page."""

from __future__ import annotations

import traceback
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.security.redaction import redact_text
from src.utils.exceptions import DataFlowError
from src.utils.logging_setup import get_logger

_log = get_logger("ui")

STATUS_ICONS = {
    "success": "🟢",
    "failed": "🔴",
    "running": "🔵",
    "skipped": "⚪",
    "pending": "⚪",
    "partial": "🟠",
    "cancelled": "⚫",
}

_CSS = """
<style>
div[data-testid="stMetric"] {
    background: var(--secondary-background-color, #f4f6fb);
    border-radius: 0.6rem;
    padding: 0.6rem 0.9rem;
}
.df-step { display:flex; gap:.35rem; flex-wrap:wrap; margin:.2rem 0 1rem 0; }
.df-step span { padding:.25rem .65rem; border-radius:999px; font-size:.82rem;
    background:#eef2ff; color:#3730a3; border:1px solid #e0e7ff; }
.df-step span.done { background:#ecfdf5; color:#065f46; border-color:#d1fae5; }
.df-step span.active { background:#2563eb; color:#fff; border-color:#2563eb; font-weight:600; }
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def page_header(title: str, caption: str | None = None) -> None:
    inject_css()
    st.title(title)
    if caption:
        st.caption(caption)


def status_label(status: str | None) -> str:
    status = (status or "pending").lower()
    return f"{STATUS_ICONS.get(status, '⚪')} {status.capitalize()}"


def format_validation_error(exc: ValidationError) -> str:
    """Turn a pydantic error into short beginner-friendly lines."""
    lines = []
    for error in exc.errors():
        location = " > ".join(str(part) for part in error.get("loc", ()) if part not in ("body",))
        message = error.get("msg", "invalid value").removeprefix("Value error, ")
        lines.append(f"- **{location or 'value'}**: {message}" if location else f"- {message}")
    return "\n".join(lines)


def show_error(exc: BaseException, context: str | None = None) -> None:
    """Display an exception safely (redacted, no stack trace for expected errors)."""
    prefix = f"{context}: " if context else ""
    if isinstance(exc, ValidationError):
        st.error(f"{prefix}Please fix the following:\n\n" + redact_text(format_validation_error(exc)))
    elif isinstance(exc, DataFlowError):
        message = redact_text(exc.message)
        hint = f"\n\n💡 {redact_text(exc.hint)}" if exc.hint else ""
        st.error(f"{prefix}{message}{hint}")
    else:
        _log.error("Unexpected UI error: %s", redact_text("".join(traceback.format_exception(exc))))
        st.error(f"{prefix}Unexpected error ({type(exc).__name__}): {redact_text(str(exc))}")


def dataframe_for_display(df: pd.DataFrame, max_rows: int = 200) -> pd.DataFrame:
    """Return a copy that renders reliably in st.dataframe (no mixed-type object columns)."""
    view = df.head(max_rows).copy()
    for column in view.columns:
        if view[column].dtype == object:
            view[column] = view[column].map(lambda v: v if v is None or isinstance(v, str) else str(v))
    return view


def human_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s"


def pipeline_picker(services, label: str = "Pipeline", key: str = "pipeline_pick", *, include_all: bool = False,
                    all_label: str = "All pipelines"):
    """Select box over saved pipelines. Returns the record, or None for 'all' / no pipelines."""
    pipelines = services.db.list_pipelines()
    if not pipelines:
        return None
    options: list = ([None] if include_all else []) + [p.id for p in pipelines]
    names = {p.id: f"{p.name} (#{p.id})" for p in pipelines}
    preferred = st.session_state.get("focus_pipeline_id")
    if key not in st.session_state and preferred in options:
        st.session_state[key] = preferred
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)
    chosen = st.selectbox(label, options, key=key, format_func=lambda v: all_label if v is None else names[v])
    if chosen is not None:
        st.session_state["focus_pipeline_id"] = chosen
    return next((p for p in pipelines if p.id == chosen), None)
