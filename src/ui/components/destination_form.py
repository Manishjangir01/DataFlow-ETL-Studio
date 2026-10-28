"""Destination settings form: file / SQLite / PostgreSQL, strategy and safety checks."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st
from pydantic import ValidationError

from src.loading.loader import check_destination, destructive_warning, inspect_target, list_tables
from src.loading.models import (
    DESTINATION_LABELS,
    FILE_TYPES,
    STRATEGY_LABELS,
    DestinationConfig,
    DestinationType,
    LoadStrategy,
)
from src.security.secrets import SecretRef
from src.ui.common import show_error
from src.ui.services import Services
from src.utils.exceptions import ConfigurationError

INFO_KEY = "dest_target_info"


def _slug(text: str, default: str = "output") -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_").lower()
    return slug[:50] or default


def seed_destination_widgets(dest: dict[str, Any] | None, pipeline_name: str = "") -> None:
    cfg = DestinationConfig.model_validate(dest) if dest else None
    pg = cfg.postgres if cfg and cfg.postgres else None
    slug = _slug(pipeline_name)
    st.session_state.update(
        {
            "dest_type": cfg.type.value if cfg else DestinationType.SQLITE.value,
            "dest_name": cfg.name if cfg else "Destination",
            "dest_file_name": (cfg.file_name or "") if cfg else slug,
            "dest_sqlite_file": (cfg.sqlite_file or "warehouse.db") if cfg else "warehouse.db",
            "dest_table": (cfg.table or "") if cfg else slug,
            "dest_strategy": cfg.strategy.value if cfg else LoadStrategy.APPEND.value,
            "dest_keys": list(cfg.upsert_keys) if cfg else [],
            "dest_confirm_replace": cfg.confirm_replace if cfg else False,
            "dest_pg_mode": "env" if pg and pg.url_env else "fields",
            "dest_pg_url_env": (pg.url_env or "") if pg else "",
            "dest_pg_host": pg.host if pg else "localhost",
            "dest_pg_port": pg.port if pg else 5432,
            "dest_pg_database": pg.database if pg else "postgres",
            "dest_pg_username": pg.username if pg else "postgres",
            "dest_pg_password": "",
            "dest_pg_saved_password": pg.password.model_dump() if pg and pg.password else None,
            "dest_pg_sslmode": pg.sslmode if pg else "prefer",
            "dest_pg_schema": pg.db_schema if pg else "public",
        }
    )
    st.session_state.pop(INFO_KEY, None)


def build_destination(services: Services, *, encrypt_new_secret: bool = True) -> DestinationConfig:
    """Validate the form. A newly typed password is encrypted only when saving/testing."""
    s = st.session_state
    dtype = DestinationType(s["dest_type"])
    data: dict[str, Any] = {
        "name": s.get("dest_name") or "Destination",
        "type": dtype,
        "strategy": s["dest_strategy"],
        "confirm_replace": bool(s.get("dest_confirm_replace")),
        "upsert_keys": list(s.get("dest_keys") or []) if s["dest_strategy"] == LoadStrategy.UPSERT.value else [],
    }
    if dtype in FILE_TYPES:
        data["file_name"] = (s.get("dest_file_name") or "").strip()
    else:
        data["table"] = (s.get("dest_table") or "").strip()
    if dtype == DestinationType.SQLITE:
        data["sqlite_file"] = s.get("dest_sqlite_file")
    if dtype == DestinationType.POSTGRESQL:
        if s.get("dest_pg_mode") == "env":
            data["postgres"] = {"url_env": s.get("dest_pg_url_env"), "db_schema": s.get("dest_pg_schema") or "public"}
            if not (s.get("dest_pg_url_env") or "").strip():
                raise ConfigurationError("Enter the name of the environment variable that holds the PostgreSQL URL.")
        else:
            typed = s.get("dest_pg_password") or ""
            if typed and encrypt_new_secret:
                ref = services.secret_box.encrypt(typed)
                s["dest_pg_saved_password"] = ref.model_dump()
                s["_dest_clear_password"] = True
            saved = s.get("dest_pg_saved_password")
            data["postgres"] = {
                "host": s.get("dest_pg_host"), "port": int(s.get("dest_pg_port") or 5432),
                "database": s.get("dest_pg_database"), "username": s.get("dest_pg_username"),
                "password": SecretRef.model_validate(saved) if saved else None,
                "sslmode": s.get("dest_pg_sslmode"), "db_schema": s.get("dest_pg_schema") or "public",
            }
    return DestinationConfig.model_validate(data)


def render_destination_form(services: Services, draft: dict, columns: list[str]) -> DestinationConfig | None:
    """Render the form; returns the saved destination (or None)."""
    if "dest_type" not in st.session_state:
        seed_destination_widgets(draft.get("destination"), draft.get("name", ""))
    if st.session_state.pop("_dest_clear_password", False):
        st.session_state["dest_pg_password"] = ""

    dtype = st.radio(
        "Destination type", [t.value for t in DestinationType], key="dest_type", horizontal=True,
        format_func=lambda v: DESTINATION_LABELS[DestinationType(v)],
    )
    dtype_enum = DestinationType(dtype)
    if dtype_enum in FILE_TYPES:
        st.text_input(f"Output file name (.{dtype})", key="dest_file_name",
                      help="Saved in the data/output folder of the project.")
    elif dtype_enum == DestinationType.SQLITE:
        c1, c2 = st.columns(2)
        c1.text_input("SQLite database file", key="dest_sqlite_file", help="Stored in data/databases, e.g. warehouse.db")
        c2.text_input("Destination table", key="dest_table", help="Letters, digits and underscores; it is created if missing.")
    else:
        mode = st.radio("Connection", ["fields", "env"], key="dest_pg_mode", horizontal=True,
                        format_func=lambda m: "Enter connection details" if m == "fields" else "Connection URL from an environment variable")
        if mode == "env":
            st.text_input("Environment variable name", key="dest_pg_url_env", placeholder="WAREHOUSE_PG_URL",
                          help="The variable holds e.g. postgresql+psycopg://user:password@host:5432/db")
        else:
            c1, c2, c3 = st.columns([3, 1, 2])
            c1.text_input("Host", key="dest_pg_host")
            c2.number_input("Port", key="dest_pg_port", min_value=1, max_value=65535)
            c3.text_input("Database", key="dest_pg_database")
            c4, c5, c6 = st.columns([2, 2, 1])
            c4.text_input("User name", key="dest_pg_username")
            saved = st.session_state.get("dest_pg_saved_password")
            c5.text_input("Password", key="dest_pg_password", type="password",
                          placeholder="•••••••• saved - leave empty to keep" if saved else "",
                          help="Encrypted immediately; never shown again or logged.")
            c6.selectbox("SSL mode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"], key="dest_pg_sslmode")
        c7, c8 = st.columns(2)
        c7.text_input("Schema", key="dest_pg_schema")
        c8.text_input("Destination table", key="dest_table")

    strategies = [s.value for s in LoadStrategy if not (dtype_enum in FILE_TYPES and s == LoadStrategy.UPSERT)]
    if st.session_state.get("dest_strategy") not in strategies:
        st.session_state["dest_strategy"] = LoadStrategy.APPEND.value
    strategy = st.radio("Loading strategy", strategies, key="dest_strategy",
                        format_func=lambda v: STRATEGY_LABELS[LoadStrategy(v)])
    if strategy == LoadStrategy.UPSERT.value:
        options = list(dict.fromkeys(columns + list(st.session_state.get("dest_keys") or [])))
        st.multiselect("Unique key column(s)", options, key="dest_keys",
                       help="Rows with the same key are updated; new keys are inserted. A unique index is created if needed.")

    info = st.session_state.get(INFO_KEY)
    if strategy == LoadStrategy.REPLACE.value:
        try:
            preview_dest = build_destination(services, encrypt_new_secret=False)
        except Exception:  # noqa: BLE001
            preview_dest = None
        warning = destructive_warning(preview_dest, info) if preview_dest else "Replace deletes all existing data in the target."
        if warning:
            st.warning(f"⚠️ {warning}")
        st.checkbox("I understand existing data will be replaced", key="dest_confirm_replace")

    b1, b2, b3, _ = st.columns([1.2, 1.2, 1.2, 2])
    save = b1.button("Save destination", type="primary", key="dest_save", icon=":material/save:")
    test = b2.button("Test connection", key="dest_test", icon=":material/lan:")
    check = b3.button("Check target", key="dest_check", icon=":material/table_view:")

    if save or test or check:
        try:
            dest = build_destination(services)
            if test:
                st.success(check_destination(dest, services.settings, services.secret_box))
            if check:
                target = inspect_target(dest, services.settings, services.secret_box)
                st.session_state[INFO_KEY] = target
                if target.exists:
                    st.info(f"**{target.location}** exists with {target.row_count:,} rows and columns: {', '.join(target.columns[:20])}")
                    missing = [c for c in columns if c not in target.columns]
                    if missing and strategy != LoadStrategy.REPLACE.value:
                        st.warning(f"These columns are not in the target and the load would fail: {', '.join(missing)}")
                else:
                    st.info(target.message)
                tables = list_tables(dest, services.settings, services.secret_box)
                if tables:
                    st.caption("Existing tables: " + ", ".join(tables[:30]))
            if save:
                if dest.strategy == LoadStrategy.REPLACE and not dest.confirm_replace:
                    raise ConfigurationError("Please confirm that existing data may be replaced, or choose Append.")
                draft["destination"] = dest.model_dump(mode="json")
                st.success(f"Destination saved: {dest.target_label()} ({dest.strategy.value})")
        except (ValidationError, Exception) as exc:  # noqa: BLE001
            show_error(exc)

    saved_dest = draft.get("destination")
    if saved_dest and not (save or test or check):
        cfg = DestinationConfig.model_validate(saved_dest)
        st.caption(f"✓ Saved destination: {cfg.target_label()} · {cfg.strategy.value}")
    return DestinationConfig.model_validate(saved_dest) if saved_dest else None
