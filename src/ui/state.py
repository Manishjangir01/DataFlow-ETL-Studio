"""Session-state helpers for the pipeline draft edited in the Create Pipeline wizard.

The draft is a plain dictionary of JSON-serialisable configuration (no secrets in
plaintext - secrets are encrypted as soon as they are entered). The extracted sample
DataFrame lives next to it so every page can preview transformations.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

DRAFT_KEY = "pipeline_draft"
RAW_DF_KEY = "draft_raw_df"
RAW_META_KEY = "draft_raw_meta"
WIZARD_STEP_KEY = "wizard_step"

WIZARD_STEPS = ["Source", "Preview", "Transform", "Validate", "Destination", "Review & Run"]


def new_draft() -> dict[str, Any]:
    return {
        "pipeline_id": None,
        "name": "",
        "description": "",
        "source": None,
        "transforms": [],
        "rules": [],
        "destination": None,
        "schedule": None,
        "schedule_enabled": False,
    }


def get_draft() -> dict[str, Any]:
    if DRAFT_KEY not in st.session_state:
        st.session_state[DRAFT_KEY] = new_draft()
    return st.session_state[DRAFT_KEY]


def reset_draft() -> None:
    st.session_state[DRAFT_KEY] = new_draft()
    st.session_state.pop(RAW_DF_KEY, None)
    st.session_state.pop(RAW_META_KEY, None)
    st.session_state[WIZARD_STEP_KEY] = 0
    for key in list(st.session_state.keys()):
        if str(key).startswith(("src_", "tf_", "rule_", "dest_", "sched_")):
            del st.session_state[key]


def set_raw_data(df: pd.DataFrame, meta: dict[str, Any] | None = None) -> None:
    """Store the extracted data. The original is never modified by later steps."""
    st.session_state[RAW_DF_KEY] = df
    st.session_state[RAW_META_KEY] = meta or {}


def get_raw_data() -> pd.DataFrame | None:
    return st.session_state.get(RAW_DF_KEY)


def get_raw_meta() -> dict[str, Any]:
    return st.session_state.get(RAW_META_KEY, {})


def get_wizard_step() -> int:
    return int(st.session_state.get(WIZARD_STEP_KEY, 0))


def go_to_step(index: int) -> None:
    st.session_state[WIZARD_STEP_KEY] = max(0, min(index, len(WIZARD_STEPS) - 1))
