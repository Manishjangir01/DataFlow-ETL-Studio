"""Moving pipelines between the database and the wizard draft."""

from __future__ import annotations

import copy
from typing import Any

import streamlit as st

from src.connectors.models import SourceConfig
from src.extraction.extractor import extract_source
from src.orchestration.pipeline_config import PipelineConfig
from src.ui.services import Services
from src.ui.state import DRAFT_KEY, go_to_step, new_draft, reset_draft, set_raw_data

PREVIEW_ROW_LIMIT = 5000
PREVIEW_PAGE_LIMIT = 2


def _forget_widget_state() -> None:
    for key in list(st.session_state.keys()):
        if str(key).startswith(("src_", "tf_", "rule_", "dest_", "sched_", "pl_", "col_keep", "draft_raw_profile", "transformed_cache")):
            del st.session_state[key]


def load_pipeline_into_draft(services: Services, pipeline_id: int, *, step: int = 0) -> None:
    """Open a saved pipeline in the Create Pipeline wizard."""
    record = services.db.get_pipeline(pipeline_id)
    reset_draft()
    _forget_widget_state()
    draft = new_draft()
    draft.update(
        {
            "pipeline_id": record.id,
            "name": record.name,
            "description": record.description or "",
            "source": copy.deepcopy(record.source),
            "transforms": copy.deepcopy(record.transforms or []),
            "rules": copy.deepcopy(record.rules or []),
            "destination": copy.deepcopy(record.destination),
            "schedule": copy.deepcopy(record.schedule),
            "schedule_enabled": bool(record.schedule_enabled),
        }
    )
    st.session_state[DRAFT_KEY] = draft
    go_to_step(step)


def start_new_pipeline() -> None:
    reset_draft()
    _forget_widget_state()


def extract_preview(services: Services, source: dict[str, Any] | SourceConfig) -> tuple[Any, dict[str, Any]]:
    """Extract a preview sample (limited rows / pages) for a saved source."""
    config = source if isinstance(source, SourceConfig) else SourceConfig.model_validate(source)
    result = extract_source(
        config, services.settings, api_extractor_factory=services.api_extractor_factory(),
        max_records=PREVIEW_ROW_LIMIT, max_pages=PREVIEW_PAGE_LIMIT,
    )
    meta = {"origin": config.kind, **result.details, "warnings": result.warnings}
    if config.kind == "api":
        meta["warnings"] = list(result.warnings) + [
            f"Preview limited to {PREVIEW_PAGE_LIMIT} page(s) / {PREVIEW_ROW_LIMIT:,} records; full runs fetch everything."
        ]
    return result.dataframe, meta


def reload_draft_sample(services: Services, draft: dict[str, Any]) -> None:
    df, meta = extract_preview(services, draft["source"])
    set_raw_data(df, meta)


def draft_to_config(draft: dict[str, Any]) -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": draft.get("name", ""),
            "description": draft.get("description", ""),
            "source": draft.get("source"),
            "transforms": draft.get("transforms", []),
            "rules": draft.get("rules", []),
            "destination": draft.get("destination"),
            "schedule": draft.get("schedule"),
            "schedule_enabled": bool(draft.get("schedule_enabled")),
        }
    )
