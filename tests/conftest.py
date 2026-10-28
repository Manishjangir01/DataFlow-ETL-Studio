"""Shared pytest fixtures.

Every test gets an isolated data folder, logs folder and SQLite metadata database in a
temporary directory, so the suite never touches real data or the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.security.redaction import clear_registered_secrets
from src.utils.config import Settings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = PROJECT_ROOT / "data" / "samples"


@pytest.fixture(autouse=True)
def _reset_secret_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def settings_env(tmp_path: Path) -> dict[str, str]:
    """Environment variables pointing every folder at a temporary directory."""
    return {
        "DATAFLOW_DATA_DIR": str(tmp_path / "data"),
        "DATAFLOW_LOGS_DIR": str(tmp_path / "logs"),
        "DATAFLOW_CONFIG_DIR": str(tmp_path / "config"),
        "DATAFLOW_DATABASE_URL": "sqlite:///" + (tmp_path / "meta.db").as_posix(),
        "DATAFLOW_SECRET_KEY": Fernet.generate_key().decode(),
        "DATAFLOW_TIMEZONE": "UTC",
        "DATAFLOW_EMBEDDED_SCHEDULER": "false",
        # Explicit defaults so a developer's own .env cannot change test behaviour.
        "DATAFLOW_ALLOWED_DOMAINS": "",
        "DATAFLOW_ALLOW_HTTP": "false",
        "DATAFLOW_USE_ENV_PROXY": "false",
        "DATAFLOW_MAX_UPLOAD_MB": "200",
        "DATAFLOW_MAX_RESPONSE_MB": "50",
        "DATAFLOW_LOCK_TIMEOUT_MINUTES": "120",
    }


@pytest.fixture
def settings(settings_env: dict[str, str]) -> Settings:
    result = load_settings(settings_env)
    result.ensure_directories()
    return result


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES_DIR
