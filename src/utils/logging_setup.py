"""Logging configuration and the per-run logger.

All handlers carry :class:`~src.security.redaction.RedactingFilter`, so secrets are
masked before anything reaches the console, the rotating log file, or the run log
stored in the database.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.security.redaction import RedactingFilter, redact_text

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured_lock = threading.Lock()
_configured = False


def configure_logging(logs_dir: Path, level: int = logging.INFO, filename: str = "dataflow.log") -> None:
    """Configure the ``dataflow`` logger once per process (idempotent).

    Each process type uses its own file (the app ``dataflow.log``, the worker
    ``worker.log``) because Windows cannot rotate a file another process has open.
    """
    global _configured
    with _configured_lock:
        if _configured:
            return
        logs_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("dataflow")
        logger.setLevel(level)
        logger.propagate = False

        redactor = RedactingFilter()
        formatter = logging.Formatter(_LOG_FORMAT)

        file_handler = RotatingFileHandler(
            logs_dir / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(redactor)
        logger.addHandler(console)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child of the application logger, e.g. ``dataflow.extraction``."""
    return logging.getLogger(f"dataflow.{name}")


class RunLogger:
    """Collects the log lines of one pipeline run (redacted) and mirrors them to logging.

    The collected text is stored with the run record and offered as a download.
    """

    def __init__(self, run_label: str, extra_secrets: list[str] | None = None) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._secrets: list[str] = list(extra_secrets or [])
        self._logger = get_logger("run")
        self.run_label = run_label

    def add_secret(self, value: str | None) -> None:
        if value:
            self._secrets.append(value)

    def _emit(self, level: int, step: str, message: str) -> None:
        clean = redact_text(message, self._secrets)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}Z {logging.getLevelName(level):<7} [{step}] {clean}"
        with self._lock:
            self._lines.append(line)
        self._logger.log(level, "%s [%s] %s", self.run_label, step, clean)

    def info(self, step: str, message: str) -> None:
        self._emit(logging.INFO, step, message)

    def warning(self, step: str, message: str) -> None:
        self._emit(logging.WARNING, step, message)

    def error(self, step: str, message: str) -> None:
        self._emit(logging.ERROR, step, message)

    @property
    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines) + ("\n" if self._lines else "")

    @property
    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)
