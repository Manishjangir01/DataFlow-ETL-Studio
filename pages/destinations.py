"""Destinations: test connections, inspect targets and browse loaded data."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import inspect, select, func, MetaData, Table

from src.loading.loader import check_destination, inspect_target, preview_target
from src.loading.models import DESTINATION_LABELS, DestinationConfig
from src.storage.engine import make_engine
from src.ui.common import dataframe_for_display, page_header, pipeline_picker, show_error
from src.ui.drafts import load_pipeline_into_draft
from src.ui.services import get_services

services = get_services()
settings = services.settings
db = services.db
page_header("Destinations", "Where your pipelines load data, and the data they produced.")

pipelines_tab, files_tab, sqlite_tab = st.tabs(["Pipeline destinations", "Output files", "SQLite databases"])

with pipelines_tab:
    pipelines = db.list_pipelines()
    if not pipelines:
        st.info("No pipelines yet.")
    else:
        rows = []
        for p in pipelines:
            try:
                d = DestinationConfig.model_validate(p.destination)
                rows.append({"Pipeline": p.name, "Type": DESTINATION_LABELS[d.type], "Target": d.target_label(),
                             "Strategy": d.strategy.value, "Upsert keys": ", ".join(d.upsert_keys) or "-"})
            except Exception:  # noqa: BLE001
                rows.append({"Pipeline": p.name, "Type": "⚠️ invalid configuration"})
        st.dataframe(pd.DataFrame(rows), hide_index=True)

        pipeline = pipeline_picker(services, key="dst_pipeline")
        if pipeline is not None:
            dest = DestinationConfig.model_validate(pipeline.destination)
            b1, b2, b3, _ = st.columns([1.2, 1.4, 1.4, 2])
            if b1.button("Test connection", key="dst_test", icon=":material/lan:"):
                try:
                    st.success(check_destination(dest, settings, services.secret_box))
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)
            if b2.button("Preview target data", key="dst_preview", icon=":material/table_view:"):
                try:
                    info = inspect_target(dest, settings, services.secret_box)
                    if not info.exists:
                        st.info(info.message)
                    else:
                        st.markdown(f"**{info.location}** · {info.row_count:,} rows · {len(info.columns)} columns")
                        st.dataframe(dataframe_for_display(preview_target(dest, settings, services.secret_box, 100)), hide_index=True)
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)
            if b3.button("Edit destination in wizard", key="dst_edit", icon=":material/edit:"):
                load_pipeline_into_draft(services, pipeline.id, step=0)
                st.switch_page("pages/create_pipeline.py")
            st.caption("Editing opens the pipeline in Create Pipeline; load a preview sample there, then go to step 5.")

with files_tab:
    files = sorted((p for p in settings.output_dir.glob("*") if p.is_file() and not p.name.startswith(".")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("No CSV or Parquet exports yet. They appear here after a run with a file destination.")
    for path in files:
        stat = path.stat()
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{path.name}** · {stat.st_size / 1024:,.1f} KB · modified {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M}")
            mime = "text/csv" if path.suffix == ".csv" else "application/octet-stream"
            c2.download_button("Download", path.read_bytes(), file_name=path.name, mime=mime, key=f"dl_out_{path.name}")
            with st.expander("Preview"):
                try:
                    frame = pd.read_csv(path, nrows=50) if path.suffix == ".csv" else pd.read_parquet(path).head(50)
                    st.dataframe(dataframe_for_display(frame), hide_index=True)
                except Exception as exc:  # noqa: BLE001
                    show_error(exc, "Cannot preview")

with sqlite_tab:
    databases = sorted(p for p in settings.databases_dir.glob("*") if p.suffix in {".db", ".sqlite", ".sqlite3"})
    if not databases:
        st.info("No SQLite databases yet. They are created in data/databases by SQLite destinations.")
    else:
        chosen = st.selectbox("Database file", [p.name for p in databases], key="dst_sqlite_file")
        engine = make_engine("sqlite:///" + (settings.databases_dir / chosen).resolve().as_posix())
        try:
            with engine.connect() as conn:
                tables = sorted(t for t in inspect(conn).get_table_names() if not t.startswith("_dfstage_"))
                if not tables:
                    st.info("This database has no tables yet.")
                else:
                    summary = []
                    for name in tables:
                        table = Table(name, MetaData(), autoload_with=conn)
                        summary.append({"Table": name, "Rows": conn.execute(select(func.count()).select_from(table)).scalar_one(),
                                        "Columns": len(table.columns)})
                    st.dataframe(pd.DataFrame(summary), hide_index=True)
                    table_name = st.selectbox("Preview table", tables, key="dst_sqlite_table")
                    table = Table(table_name, MetaData(), autoload_with=conn)
                    rows = conn.execute(select(table).limit(100)).mappings().all()
                    st.dataframe(dataframe_for_display(pd.DataFrame(rows, columns=[c.name for c in table.columns])), hide_index=True)
            st.download_button("Download database file", (settings.databases_dir / chosen).read_bytes(), file_name=chosen,
                               key="dst_sqlite_dl")
        except Exception as exc:  # noqa: BLE001
            show_error(exc, "Could not read the database")
        finally:
            engine.dispose()
