"""Process-wide services for the Streamlit app (created once and cached)."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

import streamlit as st

from src.connectors.factory import make_api_extractor_factory
from src.orchestration.runner import PipelineRunner, build_url_policy
from src.security.secrets import SecretBox, load_or_create_key
from src.security.url_guard import UrlPolicy
from src.storage.repository import Database
from src.utils.config import Settings, get_settings
from src.utils.logging_setup import configure_logging, get_logger

_log = get_logger("app")


@dataclass
class Services:
    settings: Settings
    secret_box: SecretBox
    key_origin: str
    db: Database
    scheduler: Any = None
    recovered_runs: list[int] = field(default_factory=list)

    def url_policy(self) -> UrlPolicy:
        return build_url_policy(self.settings, self.db)

    def api_extractor_factory(self, log: Callable[[str], None] | None = None):
        return make_api_extractor_factory(self.settings, self.secret_box, self.url_policy(), log=log)

    def runner(self) -> PipelineRunner:
        return PipelineRunner(self.settings, self.db, self.secret_box)


@st.cache_resource(show_spinner=False)
def get_services() -> Services:
    settings = get_settings()
    configure_logging(settings.logs_dir)
    key, origin = load_or_create_key(settings.secret_key, settings.config_dir)
    db = Database(settings.database_url)
    recovered = db.recover_interrupted_runs(timedelta(minutes=settings.lock_timeout_minutes))
    if recovered:
        _log.warning("Marked interrupted runs as failed: %s", recovered)
    services = Services(settings=settings, secret_box=SecretBox(key), key_origin=origin, db=db, recovered_runs=recovered)
    if settings.embedded_scheduler:
        from src.orchestration.scheduler import SchedulerService

        scheduler = SchedulerService(settings, db, services.runner)
        try:
            scheduler.start()
            services.scheduler = scheduler
            atexit.register(scheduler.shutdown)
        except Exception:  # noqa: BLE001 - the UI still works without schedules
            _log.exception("The embedded scheduler could not start")
    _log.info("DataFlow ETL Studio started (database: %s)", "SQLite" if settings.is_sqlite else "PostgreSQL")
    return services
