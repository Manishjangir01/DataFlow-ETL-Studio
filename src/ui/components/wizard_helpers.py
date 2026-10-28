"""Helpers shared by the wizard and the builder pages."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.transformations.engine import TransformResult, apply_transformations
from src.ui.state import WIZARD_STEPS

_TRANSFORM_CACHE = "transformed_cache"


def transformed_sample(raw: pd.DataFrame | None, steps: list[dict]) -> TransformResult | None:
    """Apply ``steps`` to ``raw`` once per change (cached in the session); None on error."""
    if raw is None:
        return None
    signature = (id(raw), json.dumps(steps, sort_keys=True, default=str))
    cached = st.session_state.get(_TRANSFORM_CACHE)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        result = apply_transformations(raw, steps)
    except Exception:  # noqa: BLE001 - the builder shows the error itself
        result = None
    st.session_state[_TRANSFORM_CACHE] = (signature, result)
    return result


def render_step_indicator(current: int) -> None:
    parts = []
    for i, name in enumerate(WIZARD_STEPS):
        css = "active" if i == current else ("done" if i < current else "")
        parts.append(f'<span class="{css}">{i + 1}. {name}</span>')
    st.markdown(f'<div class="df-step">{"".join(parts)}</div>', unsafe_allow_html=True)
