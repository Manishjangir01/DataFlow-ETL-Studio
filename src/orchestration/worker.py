"""Standalone scheduler worker.

Use it when the Streamlit app should not run schedules itself (e.g. Docker Compose,
or a server where the UI is restarted often):

    set DATAFLOW_EMBEDDED_SCHEDULER=false   (in .env)
    python -m src.orchestration.worker

Only run ONE worker (or the embedded scheduler) at a time.
"""

from __future__ import annotations

import signal
import threading
from datetime import timedelta

from src.orchestration.runner import PipelineRunner
from src.orchestration.scheduler import SchedulerService
from src.security.secrets import SecretBox, load_or_create_key
from src.storage.repository import Database
from src.utils.config import get_settings
from src.utils.logging_setup import configure_logging, get_logger


def main() -> int:
    settings = get_settings()
    configure_logging(settings.logs_dir, filename="worker.log")
    log = get_logger("worker")
    key, _origin = load_or_create_key(settings.secret_key, settings.config_dir)
    box = SecretBox(key)
    db = Database(settings.database_url)
    recovered = db.recover_interrupted_runs(timedelta(minutes=settings.lock_timeout_minutes))
    if recovered:
        log.warning("Marked interrupted runs as failed: %s", recovered)

    service = SchedulerService(settings, db, lambda: PipelineRunner(settings, db, box))
    stop = threading.Event()

    def handle_signal(signum, _frame) -> None:  # pragma: no cover - signal handling
        log.info("Received signal %s, shutting down", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_signal)

    service.start()
    log.info("Worker running with %d active schedule(s). Press Ctrl+C to stop.", service.active_job_count())
    try:
        while not stop.wait(1.0):
            pass
    finally:
        service.shutdown(wait=True)
        db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
