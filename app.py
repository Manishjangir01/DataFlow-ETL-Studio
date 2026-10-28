"""DataFlow ETL Studio - Streamlit entry point.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="DataFlow ETL Studio",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

from src.ui.common import show_error  # noqa: E402  (imports after set_page_config)
from src.ui.services import get_services  # noqa: E402

try:
    services = get_services()
except Exception as exc:  # noqa: BLE001 - configuration problems are shown, not raised
    st.title("DataFlow ETL Studio")
    show_error(exc, "The application could not start")
    st.info("Check your .env file (see .env.example) and restart the app.")
    st.stop()

pages = {
    "": [
        st.Page("pages/overview.py", title="Overview", icon=":material/dashboard:", default=True),
        st.Page("pages/create_pipeline.py", title="Create Pipeline", icon=":material/add_circle:"),
    ],
    "Build": [
        st.Page("pages/data_sources.py", title="Data Sources", icon=":material/cloud_download:"),
        st.Page("pages/transformation_builder.py", title="Transformation Builder", icon=":material/tune:"),
        st.Page("pages/destinations.py", title="Destinations", icon=":material/database:"),
    ],
    "Operate": [
        st.Page("pages/pipeline_runs.py", title="Pipeline Runs", icon=":material/play_circle:"),
        st.Page("pages/data_quality.py", title="Data Quality Reports", icon=":material/fact_check:"),
    ],
    "Admin": [
        st.Page("pages/settings.py", title="Settings", icon=":material/settings:"),
    ],
}

navigation = st.navigation(pages)

with st.sidebar:
    st.markdown("### 🔄 DataFlow ETL Studio")
    settings = services.settings
    database = "SQLite" if settings.is_sqlite else "PostgreSQL"
    if services.scheduler is not None and services.scheduler.running:
        scheduler_state = f"🟢 running ({services.scheduler.active_job_count()} active)"
    elif settings.embedded_scheduler:
        scheduler_state = "🔴 not running"
    else:
        scheduler_state = "⚙️ external worker"
    st.caption(f"Metadata DB: {database}  \nScheduler: {scheduler_state}  \nTime zone: {settings.timezone}")

navigation.run()
