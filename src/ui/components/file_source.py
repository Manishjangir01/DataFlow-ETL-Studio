"""File upload source form (CSV, JSON, JSON Lines, Parquet)."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.connectors.models import FileSourceConfig, SourceConfig
from src.extraction.extractor import extract_file_source, file_read_options
from src.extraction.file_reader import detect_format, read_file_bytes, save_upload
from src.extraction.normalize import find_record_paths
from src.ui.common import show_error
from src.ui.services import Services
from src.ui.state import set_raw_data

_DELIMITERS = {"Auto-detect": None, "Comma ( , )": ",", "Semicolon ( ; )": ";", "Tab": "\t", "Pipe ( | )": "|"}
_ENCODINGS = ["utf-8", "utf-8-sig", "cp1252", "latin-1", "utf-16"]
_UPLOAD_TYPES = ["csv", "tsv", "txt", "json", "jsonl", "ndjson", "parquet", "pq"]
_SUGGESTION_LIMIT = 20 * 1_048_576


def _record_path_choices(data: bytes) -> list[tuple[str, str]]:
    """Return (label, path) pairs for JSON fields that contain records."""
    if len(data) > _SUGGESTION_LIMIT:
        return []
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []  # JSON Lines or not JSON - records are the lines themselves
    return [(c.label, c.path) for c in find_record_paths(document)]


def _options_form(file_format: str, data: bytes | None, key: str) -> dict:
    with st.expander("File options", expanded=file_format == "json"):
        options: dict = {}
        if file_format == "csv":
            c1, c2 = st.columns(2)
            delimiter_label = c1.selectbox("Delimiter", list(_DELIMITERS), key=f"{key}_delim")
            options["delimiter"] = _DELIMITERS[delimiter_label]
            options["encoding"] = c2.selectbox(
                "Encoding", _ENCODINGS, key=f"{key}_enc",
                help="Auto-falls back to utf-8-sig, cp1252 and latin-1 if decoding fails.",
            )
        if file_format == "json":
            choices = _record_path_choices(data) if data else []
            if choices:
                labels = ["Auto-detect"] + [label for label, _ in choices]
                picked = st.selectbox(
                    "Field containing the records", labels, key=f"{key}_path_pick",
                    help="DataFlow found these lists of objects in the document.",
                )
                options["records_path"] = dict(choices).get(picked) if picked != "Auto-detect" else None
            else:
                typed = st.text_input(
                    "Field containing the records (optional)", key=f"{key}_path",
                    placeholder="e.g. data.items - leave empty to auto-detect",
                )
                options["records_path"] = typed.strip() or None
            options["flatten_nested"] = st.checkbox(
                "Flatten nested objects into columns (customer.address.city)", value=True,
                key=f"{key}_flatten",
            )
        options["read_all_as_text"] = st.checkbox(
            "Read every column as text (keeps values like 00123 unchanged)", key=f"{key}_text"
        )
    return options


def _extract(services: Services, draft: dict, data: bytes, name: str, source_name: str, options: dict) -> bool:
    try:
        file_format = detect_format(name)
        cfg_preview = FileSourceConfig(
            stored_name="pending", original_name=name, file_format=file_format, **options
        )
        result = read_file_bytes(
            data, file_format, file_read_options(cfg_preview),
            max_bytes=services.settings.max_upload_mb * 1_048_576,
        )
        if result.dataframe.empty:
            st.warning("The file was read successfully but contains no records.")
            return False
        stored = save_upload(data, name, services.settings.upload_dir)
        source = SourceConfig(
            name=source_name or Path(name).stem,
            kind="file",
            file=cfg_preview.model_copy(update={"stored_name": stored}),
        )
    except Exception as exc:  # noqa: BLE001 - every error is shown to the user
        show_error(exc, "Could not read the file")
        return False
    draft["source"] = source.model_dump(mode="json")
    if not draft.get("name"):
        draft["name"] = f"{source.name} pipeline"
    set_raw_data(result.dataframe, {"origin": "file", **result.details})
    return True


def _render_saved_file(services: Services, draft: dict) -> bool:
    """Offer to re-extract the file already stored for this pipeline."""
    source = draft.get("source") or {}
    if source.get("kind") != "file" or not source.get("file"):
        return False
    cfg = FileSourceConfig.model_validate(source["file"])
    with st.container(border=True):
        st.markdown(f"📄 Saved file: **{cfg.original_name}** ({cfg.file_format.upper()})")
        st.caption("Upload a new file below to replace it, or extract again from the saved copy.")
        if st.button("Extract from saved file", key="src_extract_saved", icon=":material/refresh:"):
            try:
                result = extract_file_source(cfg, services.settings)
            except Exception as exc:  # noqa: BLE001
                show_error(exc, "Could not read the saved file")
                return False
            set_raw_data(result.dataframe, {"origin": "file", **result.details})
            return True
    return False


def render_file_source(services: Services, draft: dict, source_name: str) -> bool:
    """Render the upload form. Returns True when data was extracted on this run."""
    if _render_saved_file(services, draft):
        return True
    upload_tab, sample_tab = st.tabs(["Upload a file", "Use a sample dataset"])

    with upload_tab:
        uploaded = st.file_uploader(
            "CSV, JSON, JSON Lines or Parquet",
            type=_UPLOAD_TYPES,
            key="src_file_upload",
            help=f"Maximum size: {services.settings.max_upload_mb} MB",
        )
        if uploaded is not None:
            try:
                file_format = detect_format(uploaded.name)
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
                return False
            data = uploaded.getvalue()
            st.caption(f"**{uploaded.name}** - {len(data) / 1024:,.1f} KB - {file_format.upper()}")
            options = _options_form(file_format, data, "src_up")
            if st.button("Extract data", type="primary", key="src_extract_upload"):
                return _extract(services, draft, data, uploaded.name, source_name, options)

    with sample_tab:
        samples = sorted(
            p for p in services.settings.samples_dir.glob("*") if p.suffix.lower() in {".csv", ".json", ".jsonl", ".parquet"}
        ) if services.settings.samples_dir.is_dir() else []
        if not samples:
            st.info("No sample datasets found in data/samples.")
        else:
            st.caption("Messy sample data to try every feature: duplicates, blanks, bad dates and nested JSON.")
            picked = st.selectbox("Sample dataset", [p.name for p in samples], key="src_sample_pick")
            path = services.settings.samples_dir / picked
            data = path.read_bytes()
            options = _options_form(detect_format(picked), data, "src_sample")
            if st.button("Load sample", type="primary", key="src_extract_sample"):
                return _extract(services, draft, data, picked, source_name or Path(picked).stem, options)
    return False
