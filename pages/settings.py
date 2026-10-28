"""Settings: environment, API security, scheduler and maintenance."""

from __future__ import annotations

import platform
import sys

import pandas as pd
import streamlit as st
from sqlalchemy.engine import make_url

from src.orchestration.runner import purge_history
from src.security.url_guard import BLOCKED_HOSTNAMES
from src.ui.common import page_header, show_error
from src.ui.services import get_services
from src.utils.config import PROJECT_ROOT

services = get_services()
settings = services.settings
db = services.db
page_header("Settings", "Configuration, security and maintenance.")

env_tab, security_tab, scheduler_tab, maintenance_tab = st.tabs(["Environment", "API security", "Scheduler", "Maintenance"])

with env_tab:
    try:
        db_display = make_url(settings.database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        db_display = "(invalid URL)"
    rows = [
        ("Metadata database", db_display),
        ("Data folder", str(settings.data_dir)),
        ("Logs folder", str(settings.logs_dir)),
        ("Default time zone", settings.timezone),
        ("Max upload size", f"{settings.max_upload_mb} MB"),
        ("Max API response size", f"{settings.max_response_mb} MB"),
        ("Stale run lock timeout", f"{settings.lock_timeout_minutes} minutes"),
        ("Python", f"{sys.version.split()[0]} on {platform.system()} {platform.release()}"),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Setting", "Value"]), hide_index=True)
    st.markdown("**Secret encryption key**")
    if services.key_origin == "environment":
        st.success("Loaded from DATAFLOW_SECRET_KEY. Saved API keys, tokens and passwords are encrypted with it.")
    else:
        st.warning(
            "The key was generated automatically and stored in `config/secret.key` (git-ignored). "
            "Back it up - without it saved secrets cannot be decrypted. For production set DATAFLOW_SECRET_KEY instead."
        )
    st.caption("Settings come from environment variables / the .env file. Edit .env and restart the app to change them.")

with security_tab:
    st.markdown("**Allowed API domains**")
    st.caption("When the list is empty, any public internet domain may be called. When it has entries, only those domains "
               "(and their sub-domains) are allowed. Domains from DATAFLOW_ALLOWED_DOMAINS always apply.")
    if settings.allowed_domains:
        st.caption("From the environment: " + ", ".join(settings.allowed_domains))
    saved_domains = db.get_setting("allowed_domains", []) or []
    text = st.text_area("Additional allowed domains (one per line)", value="\n".join(saved_domains), key="set_domains", height=110)
    allow_http_saved = db.get_setting("allow_http", None)
    allow_http = st.toggle("Allow plain http:// API URLs (not recommended)",
                           value=settings.allow_http if allow_http_saved is None else bool(allow_http_saved), key="set_http")
    if st.button("Save API security settings", key="set_save_security", type="primary", icon=":material/save:"):
        domains = sorted({d.strip().lower().lstrip(".") for d in text.splitlines() if d.strip()})
        invalid = [d for d in domains if " " in d or "/" in d or ":" in d]
        if invalid:
            st.error("Enter bare domain names such as api.example.com (no http://, paths or ports): " + ", ".join(invalid))
        else:
            db.set_setting("allowed_domains", domains)
            db.set_setting("allow_http", allow_http)
            st.success("Saved. New API requests use these rules immediately.")
    with st.expander("What is always blocked"):
        st.markdown(
            "- Loopback and localhost (127.0.0.0/8, ::1, `localhost`)\n"
            "- Private networks (10/8, 172.16/12, 192.168/16, fc00::/7) and carrier-grade NAT (100.64/10)\n"
            "- Link-local and cloud metadata endpoints (169.254.0.0/16, fe80::/10, metadata.google.internal)\n"
            "- Multicast, reserved and unspecified addresses, and `.local` / `.internal` host names\n"
            "- URLs with embedded credentials, redirects or pagination links to another host\n\n"
            "Addresses are checked after DNS resolution **and** again when the connection is opened, "
            "so DNS-rebinding tricks are blocked as well."
        )
        st.caption("Blocked host names: " + ", ".join(sorted(BLOCKED_HOSTNAMES)))

with scheduler_tab:
    scheduler = services.scheduler
    if settings.embedded_scheduler:
        if scheduler is not None and scheduler.running:
            st.success("The scheduler is running inside this app (embedded mode).")
        else:
            st.error("The embedded scheduler is not running. Check the logs for errors.")
    else:
        st.info("Embedded scheduler disabled (DATAFLOW_EMBEDDED_SCHEDULER=false). Start the worker with "
                "`python -m src.orchestration.worker`; it picks up schedule changes within 30 seconds.")
    if scheduler is not None:
        jobs = scheduler.jobs_overview()
        if jobs:
            st.dataframe(pd.DataFrame(jobs), hide_index=True)
        else:
            st.caption("No active schedules.")
    st.markdown("#### Limitations of the in-process scheduler")
    st.markdown(
        "- Schedules only fire **while the app (or worker) process is running** - nothing runs when the computer sleeps or the app is closed. "
        "Runs missed by more than 5 minutes are skipped; missed runs are coalesced into one.\n"
        "- It runs on a single machine. Run **either** the embedded scheduler **or** one worker, not several copies.\n"
        "- Overlapping runs of the same pipeline are prevented by a database lock, so a slow run makes the next "
        "scheduled run be recorded as *skipped*.\n"
        "- Long runs execute in a background thread of the same process; very large datasets are limited by its memory.\n"
        "- There is no built-in alerting, dependency management between pipelines or distributed execution."
    )
    st.markdown("#### Moving to Apache Airflow")
    st.markdown(
        "Every pipeline can be run from the command line, which makes orchestration tools easy to plug in:\n\n"
        "```bash\npython -m src.cli run --pipeline-id 3\n```\n"
        "The command exits with code 0 on success and 1 on failure, and records the run in the same history. "
        "An example Airflow DAG that calls it is in `examples/airflow_dag.py`: install Airflow on a Linux host or container, "
        "copy the DAG into the `dags/` folder, set `DATAFLOW_HOME` to this project, and turn off the embedded scheduler."
    )
    example = PROJECT_ROOT / "examples" / "airflow_dag.py"
    if example.is_file():
        with st.expander("examples/airflow_dag.py"):
            st.code(example.read_text(encoding="utf-8"), language="python")

with maintenance_tab:
    st.markdown("**Delete old run history**")
    st.caption("Removes run records, their logs and cached data. Pipelines and loaded data are not affected.")
    days = st.number_input("Delete runs older than (days)", min_value=1, max_value=3650, value=90, key="set_purge_days")
    confirm = st.checkbox("I understand this permanently deletes run history", key="set_purge_confirm")
    if st.button("Delete old runs", key="set_purge", disabled=not confirm, icon=":material/delete_sweep:"):
        try:
            removed = purge_history(db, settings, int(days))
            st.success(f"Deleted {removed} run(s).")
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
