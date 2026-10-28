"""REST API source form with 'Test connection' and full extraction."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st
from pydantic import ValidationError

from src.connectors.models import (
    AUTH_LABELS,
    PAGINATION_LABELS,
    ApiSourceConfig,
    AuthConfig,
    AuthType,
    PaginationConfig,
    PaginationType,
    SourceConfig,
)
from src.extraction.extractor import extract_source
from src.security.secrets import SecretRef
from src.ui.common import dataframe_for_display, show_error
from src.ui.components.key_value_editor import render_key_value_editor, reset_key_value_editor
from src.ui.services import Services
from src.ui.state import set_raw_data
from src.utils.exceptions import ConfigurationError

PROBE_KEY = "src_api_probe"
_PAGINATION_TYPES = [t.value for t in PaginationType]
_AUTH_TYPES = [t.value for t in AuthType]


def seed_api_widgets(api: dict[str, Any] | None) -> None:
    """Fill the form widgets from a saved API configuration (or defaults)."""
    cfg = ApiSourceConfig.model_validate(api) if api else None
    pagination = cfg.pagination if cfg else PaginationConfig()
    auth = cfg.auth if cfg else AuthConfig()
    st.session_state.update(
        {
            "src_api_url": cfg.url if cfg else "",
            "src_api_method": cfg.method if cfg else "GET",
            "src_api_auth_type": auth.type.value,
            "src_api_key_name": auth.api_key_name,
            "src_api_key_location": auth.api_key_location,
            "src_api_secret_mode": "env" if auth.secret and auth.secret.kind == "env" else "encrypted",
            "src_api_secret_env": auth.secret.value if auth.secret and auth.secret.kind == "env" else "",
            "src_api_secret": "",
            "src_api_saved_secret": auth.secret.model_dump() if auth.secret and auth.secret.kind == "encrypted" else None,
            "src_api_body": json.dumps(cfg.json_body, indent=2) if cfg and cfg.json_body is not None else "",
            "src_api_pagination": pagination.type.value,
            "src_api_page_param": pagination.page_param,
            "src_api_start_page": pagination.start_page,
            "src_api_size_param": pagination.size_param or "",
            "src_api_page_size": pagination.page_size,
            "src_api_offset_param": pagination.offset_param,
            "src_api_limit_param": pagination.limit_param,
            "src_api_cursor_param": pagination.cursor_param,
            "src_api_cursor_path": pagination.cursor_path,
            "src_api_next_link_path": pagination.next_link_path or "",
            "src_api_max_pages": pagination.max_pages,
            "src_api_stop_empty": pagination.stop_on_empty_page,
            "src_api_timeout": float(cfg.timeout_seconds) if cfg else 30.0,
            "src_api_max_records": cfg.max_records if cfg else 10_000,
            "src_api_max_retries": cfg.max_retries if cfg else 3,
            "src_api_records_path": (cfg.records_path or "") if cfg else "",
            "src_api_flatten": cfg.flatten_nested if cfg else True,
            "src_api_authorized": cfg.authorized if cfg else False,
        }
    )
    reset_key_value_editor("src_api_headers", cfg.headers if cfg else {"Accept": "application/json"})
    reset_key_value_editor("src_api_params", cfg.query_params if cfg else {})
    st.session_state.pop(PROBE_KEY, None)


def _ensure_defaults(draft: dict) -> None:
    """Seed the widgets (again) from the draft when they are not in the session.

    Streamlit forgets widget values when a widget is hidden (e.g. on another wizard
    step), so returning to the source step restores them from the saved draft.
    """
    if "src_api_url" not in st.session_state:
        source = draft.get("source") or {}
        seed_api_widgets(source.get("api") if source.get("kind") == "api" else None)


def _secret_from_widgets(services: Services, auth_type: AuthType) -> SecretRef | None:
    if auth_type == AuthType.NONE:
        return None
    if st.session_state.get("src_api_secret_mode") == "env":
        name = (st.session_state.get("src_api_secret_env") or "").strip()
        return SecretRef(kind="env", value=name) if name else None
    typed = st.session_state.get("src_api_secret") or ""
    if typed:
        ref = services.secret_box.encrypt(typed)
        # Keep only the ciphertext; the plaintext input is cleared on the next run.
        st.session_state["src_api_saved_secret"] = ref.model_dump()
        st.session_state["_src_api_clear_secret"] = True
        return ref
    saved = st.session_state.get("src_api_saved_secret")
    return SecretRef.model_validate(saved) if saved else None


def build_api_config(services: Services, headers: dict[str, str], params: dict[str, str]) -> ApiSourceConfig:
    """Validate the form and return the API configuration."""
    s = st.session_state
    auth_type = AuthType(s["src_api_auth_type"])
    body_text = (s.get("src_api_body") or "").strip()
    body = None
    if body_text and s["src_api_method"] == "POST":
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"The JSON request body is invalid: {exc.msg} (line {exc.lineno}).") from exc
    auth = AuthConfig(
        type=auth_type,
        api_key_name=s.get("src_api_key_name") or "X-API-Key",
        api_key_location=s.get("src_api_key_location") or "header",
        secret=_secret_from_widgets(services, auth_type),
    )
    if auth_type != AuthType.NONE and auth.secret is None:
        raise ConfigurationError(
            f"{AUTH_LABELS[auth_type]} authentication is selected but no secret was entered.",
            hint="Type the key/token, or choose an environment variable that holds it.",
        )
    pagination = PaginationConfig(
        type=PaginationType(s["src_api_pagination"]),
        page_param=s["src_api_page_param"],
        start_page=int(s["src_api_start_page"]),
        size_param=s.get("src_api_size_param") or None,
        page_size=int(s["src_api_page_size"]),
        offset_param=s["src_api_offset_param"],
        limit_param=s["src_api_limit_param"],
        cursor_param=s["src_api_cursor_param"],
        cursor_path=s["src_api_cursor_path"],
        next_link_path=s.get("src_api_next_link_path") or None,
        max_pages=int(s["src_api_max_pages"]),
        stop_on_empty_page=bool(s["src_api_stop_empty"]),
    )
    return ApiSourceConfig(
        url=s["src_api_url"],
        method=s["src_api_method"],
        auth=auth,
        headers=headers,
        query_params=params,
        json_body=body,
        pagination=pagination,
        timeout_seconds=float(s["src_api_timeout"]),
        max_records=int(s["src_api_max_records"]),
        max_retries=int(s["src_api_max_retries"]),
        records_path=s.get("src_api_records_path") or None,
        flatten_nested=bool(s["src_api_flatten"]),
        authorized=bool(s["src_api_authorized"]),
    )


def _render_form(services: Services) -> tuple[dict[str, str], dict[str, str]]:
    if st.session_state.pop("_src_api_clear_secret", False):
        st.session_state["src_api_secret"] = ""

    c1, c2 = st.columns([4, 1])
    c1.text_input("API endpoint URL", key="src_api_url", placeholder="https://api.example.com/v1/orders")
    c2.selectbox("HTTP method", ["GET", "POST"], key="src_api_method")

    with st.container(border=True):
        st.markdown("**Authentication**")
        auth_type = st.radio(
            "Authentication type", _AUTH_TYPES, key="src_api_auth_type", horizontal=True,
            format_func=lambda t: AUTH_LABELS[AuthType(t)], label_visibility="collapsed",
        )
        if auth_type != AuthType.NONE.value:
            if auth_type == AuthType.API_KEY.value:
                k1, k2 = st.columns(2)
                k1.text_input("Key name", key="src_api_key_name", help="Header or parameter name, e.g. X-API-Key or appid")
                k2.selectbox("Send the key in", ["header", "query"], key="src_api_key_location")
            st.radio(
                "Secret source", ["encrypted", "env"], key="src_api_secret_mode", horizontal=True,
                format_func=lambda m: "Enter it here (stored encrypted)" if m == "encrypted" else "Environment variable",
            )
            if st.session_state.get("src_api_secret_mode") == "env":
                st.text_input("Environment variable name", key="src_api_secret_env", placeholder="MY_API_TOKEN")
            else:
                saved = st.session_state.get("src_api_saved_secret")
                st.text_input(
                    "API key / token", key="src_api_secret", type="password",
                    placeholder="•••••••• saved - leave empty to keep it" if saved else "Paste the secret",
                    help="Encrypted immediately and never shown again or written to logs.",
                )
                if saved:
                    st.caption("🔒 A secret is saved (encrypted).")

    left, right = st.columns(2)
    with left:
        headers = render_key_value_editor(
            "Request headers", "src_api_headers",
            help="Credentials are not allowed here - use Authentication.",
        )
    with right:
        params = render_key_value_editor("Query parameters", "src_api_params")

    if st.session_state.get("src_api_method") == "POST":
        st.text_area("JSON request body (optional)", key="src_api_body", height=120, placeholder='{"filter": "active"}')

    with st.expander("Pagination", expanded=st.session_state.get("src_api_pagination") != PaginationType.NONE.value):
        ptype = st.selectbox(
            "Pagination type", _PAGINATION_TYPES, key="src_api_pagination",
            format_func=lambda t: PAGINATION_LABELS[PaginationType(t)],
        )
        if ptype == PaginationType.PAGE_NUMBER.value:
            p1, p2, p3, p4 = st.columns(4)
            p1.text_input("Page parameter", key="src_api_page_param")
            p2.number_input("First page", key="src_api_start_page", min_value=0, step=1)
            p3.text_input("Page size parameter", key="src_api_size_param", help="Leave empty if the API has a fixed page size")
            p4.number_input("Page size", key="src_api_page_size", min_value=1, max_value=10_000, step=10)
        elif ptype == PaginationType.OFFSET_LIMIT.value:
            p1, p2, p3 = st.columns(3)
            p1.text_input("Offset parameter", key="src_api_offset_param")
            p2.text_input("Limit parameter", key="src_api_limit_param")
            p3.number_input("Page size", key="src_api_page_size", min_value=1, max_value=10_000, step=10)
        elif ptype == PaginationType.CURSOR.value:
            p1, p2 = st.columns(2)
            p1.text_input("Cursor parameter to send", key="src_api_cursor_param")
            p2.text_input("Response field with the next cursor", key="src_api_cursor_path", help="Dot path, e.g. meta.next_cursor")
        elif ptype == PaginationType.NEXT_LINK.value:
            st.text_input(
                "Response field with the next page URL", key="src_api_next_link_path",
                help="Dot path such as links.next. Leave empty to use the HTTP Link header (rel=\"next\").",
            )
        if ptype != PaginationType.NONE.value:
            m1, m2 = st.columns(2)
            m1.number_input("Maximum pages per run", key="src_api_max_pages", min_value=1, max_value=10_000)
            m2.checkbox("Stop when a page is empty", key="src_api_stop_empty")

    with st.expander("Response and limits"):
        r1, r2, r3 = st.columns(3)
        r1.number_input("Request timeout (seconds)", key="src_api_timeout", min_value=1.0, max_value=300.0, step=5.0)
        r2.number_input("Maximum records per run", key="src_api_max_records", min_value=1, max_value=1_000_000, step=1000)
        r3.number_input("Retries on errors / rate limits", key="src_api_max_retries", min_value=0, max_value=10)
        st.text_input(
            "Field containing the records", key="src_api_records_path",
            placeholder="Leave empty to auto-detect, e.g. data.items",
            help="Use 'Test connection' to list the fields that contain records.",
        )
        st.checkbox("Flatten nested objects into columns", key="src_api_flatten")

    st.checkbox(
        "I confirm I am authorized to access this API and its data.", key="src_api_authorized",
    )
    return headers, params


def _apply_records_path() -> None:
    picked = st.session_state.get("src_api_path_pick")
    if picked is not None:
        st.session_state["src_api_records_path"] = picked


def _render_probe() -> None:
    probe = st.session_state.get(PROBE_KEY)
    if not probe:
        return
    with st.container(border=True):
        st.markdown(
            f"✅ **HTTP {probe['status']}** in {probe['elapsed_ms']} ms · {probe['content_type'] or 'unknown type'} · "
            f"**{probe['count']} records** on the first page"
        )
        paths = probe["paths"]
        if paths:
            labels = {path: label for label, path in paths}
            current = st.session_state.get("src_api_records_path") or ""
            options = list(labels)
            st.selectbox(
                "Fields that contain records", options,
                index=options.index(current) if current in options else 0,
                format_func=lambda p: labels[p], key="src_api_path_pick", on_change=_apply_records_path,
                help="Selecting a field sets 'Field containing the records'.",
            )
        if probe["sample"]:
            st.dataframe(dataframe_for_display(probe["sample_df"], 20), hide_index=True)
        with st.expander("Raw response (first 5,000 characters)"):
            st.code(probe["raw"], language="json")


def render_api_source(services: Services, draft: dict, source_name: str) -> bool:
    """Render the API form. Returns True when data was extracted on this run."""
    _ensure_defaults(draft)
    headers, params = _render_form(services)

    b1, b2, _ = st.columns([1, 1, 3])
    test_clicked = b1.button("Test connection", key="src_api_test", icon=":material/network_check:")
    extract_clicked = b2.button("Extract data", key="src_api_extract", type="primary", icon=":material/download:")

    if test_clicked or extract_clicked:
        try:
            config = build_api_config(services, headers, params)
            if not config.authorized:
                raise ConfigurationError("Please confirm that you are authorized to access this API.")
            factory = services.api_extractor_factory()
            if test_clicked:
                with st.spinner("Calling the API..."):
                    probe = factory(config).probe()
                from src.extraction.normalize import records_to_dataframe

                st.session_state[PROBE_KEY] = {
                    "status": probe.status_code,
                    "elapsed_ms": probe.elapsed_ms,
                    "content_type": probe.content_type,
                    "count": probe.record_count,
                    "paths": probe.record_paths,
                    "sample": bool(probe.sample_records),
                    "sample_df": records_to_dataframe(probe.sample_records, flatten=config.flatten_nested),
                    "raw": json.dumps(probe.payload, indent=2, default=str)[:5000],
                }
            else:
                source = SourceConfig(name=source_name or "API source", kind="api", api=config)
                with st.spinner("Extracting all pages..."):
                    result = extract_source(source, services.settings, api_extractor_factory=factory)
                if result.dataframe.empty:
                    st.warning("The API responded but returned no records. Check 'Field containing the records'.")
                    return False
                draft["source"] = source.model_dump(mode="json")
                if not draft.get("name"):
                    draft["name"] = f"{source.name} pipeline"
                set_raw_data(result.dataframe, {"origin": "api", **result.details, "warnings": result.warnings})
                return True
        except (ValidationError, Exception) as exc:  # noqa: BLE001 - shown to the user
            show_error(exc, "API request failed" if not isinstance(exc, (ValidationError, ConfigurationError)) else None)
    _render_probe()
    return False
