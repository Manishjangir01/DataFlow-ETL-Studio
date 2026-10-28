"""Editable key/value table used for request headers and query parameters."""

from __future__ import annotations

import pandas as pd
import streamlit as st


def render_key_value_editor(label: str, key: str, initial: dict[str, str] | None = None, help: str | None = None) -> dict[str, str]:
    """Show an editable two-column table and return its non-empty rows as a dict."""
    seed_key = f"{key}__seed"
    if seed_key not in st.session_state:
        rows = [{"Name": k, "Value": v} for k, v in (initial or {}).items()]
        st.session_state[seed_key] = pd.DataFrame(rows or [{"Name": "", "Value": ""}], columns=["Name", "Value"])
    st.markdown(f"**{label}**")
    if help:
        st.caption(help)
    edited = st.data_editor(
        st.session_state[seed_key],
        key=key,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "Name": st.column_config.TextColumn("Name", max_chars=128),
            "Value": st.column_config.TextColumn("Value", max_chars=2048),
        },
    )
    result: dict[str, str] = {}
    for _, row in edited.iterrows():
        name = str(row.get("Name") or "").strip()
        if name:
            value = row.get("Value")
            result[name] = "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
    return result


def reset_key_value_editor(key: str, values: dict[str, str] | None) -> None:
    """Re-seed an editor (used when loading a saved pipeline into the form)."""
    rows = [{"Name": k, "Value": v} for k, v in (values or {}).items()]
    st.session_state[f"{key}__seed"] = pd.DataFrame(rows or [{"Name": "", "Value": ""}], columns=["Name", "Value"])
    st.session_state.pop(key, None)
