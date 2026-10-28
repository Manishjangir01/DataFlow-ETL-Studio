"""Application settings loaded from environment variables (and an optional .env file).

Settings are validated once at start-up so configuration mistakes are reported with a
clear message instead of surfacing later as obscure runtime errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from src.utils.exceptions import ConfigurationError

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False if value else default
    raise ConfigurationError(f"{name} must be true or false, got '{raw}'.")


def _parse_int(name: str, raw: str | None, default: int, minimum: int, maximum: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number, got '{raw}'.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def _resolve_dir(raw: str | None, default: Path) -> Path:
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _normalise_database_url(url: str) -> str:
    """Validate the metadata database URL and make relative SQLite paths absolute."""
    url = url.strip()
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        raw_path = url[len("sqlite:///"):]
        if raw_path and raw_path != ":memory:" and not Path(raw_path).is_absolute():
            return "sqlite:///" + (PROJECT_ROOT / raw_path).resolve().as_posix()
        return url
    if url.startswith("sqlite://"):
        return url
    if url.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        if url.startswith("postgresql://"):
            # Use the psycopg 3 driver that ships with the project.
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    raise ConfigurationError(
        "DATAFLOW_DATABASE_URL must start with sqlite:/// or postgresql+psycopg://",
        hint="See .env.example for working examples.",
    )


@dataclass(frozen=True)
class Settings:
    """Validated application settings."""

    database_url: str
    data_dir: Path
    logs_dir: Path
    config_dir: Path
    secret_key: str | None
    timezone: str
    embedded_scheduler: bool
    allow_http: bool
    allowed_domains: tuple[str, ...]
    use_env_proxy: bool
    max_upload_mb: int
    max_response_mb: int
    lock_timeout_minutes: int
    extra: Mapping[str, str] = field(default_factory=dict)

    # Derived folders -------------------------------------------------------------
    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def databases_dir(self) -> Path:
        return self.data_dir / "databases"

    @property
    def samples_dir(self) -> Path:
        """Bundled sample datasets (always read from the project folder)."""
        return PROJECT_ROOT / "data" / "samples"

    @property
    def run_logs_dir(self) -> Path:
        return self.logs_dir / "runs"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_directories(self) -> None:
        """Create every folder the application writes to."""
        for folder in (
            self.data_dir,
            self.upload_dir,
            self.output_dir,
            self.runs_dir,
            self.databases_dir,
            self.logs_dir,
            self.run_logs_dir,
            self.config_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)


def load_settings(env: Mapping[str, str] | None = None, *, load_env_file: bool = True) -> Settings:
    """Build :class:`Settings` from ``env`` (defaults to ``os.environ``).

    Raises:
        ConfigurationError: when a value is missing or invalid.
    """
    if env is None:
        if load_env_file:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        env = os.environ

    data_dir = _resolve_dir(env.get("DATAFLOW_DATA_DIR"), PROJECT_ROOT / "data")
    logs_dir = _resolve_dir(env.get("DATAFLOW_LOGS_DIR"), PROJECT_ROOT / "logs")
    config_dir = _resolve_dir(env.get("DATAFLOW_CONFIG_DIR"), PROJECT_ROOT / "config")

    default_db = "sqlite:///" + (data_dir / "dataflow.db").resolve().as_posix()
    database_url = _normalise_database_url(env.get("DATAFLOW_DATABASE_URL") or default_db)

    timezone = (env.get("DATAFLOW_TIMEZONE") or "UTC").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"DATAFLOW_TIMEZONE '{timezone}' is not a valid IANA time zone.",
            hint="Examples: UTC, Asia/Kolkata, Europe/London. On Windows install 'tzdata'.",
        ) from exc

    domains_raw = env.get("DATAFLOW_ALLOWED_DOMAINS") or ""
    allowed_domains = tuple(
        sorted({d.strip().lower().lstrip(".") for d in domains_raw.split(",") if d.strip()})
    )

    secret_key = (env.get("DATAFLOW_SECRET_KEY") or "").strip() or None

    return Settings(
        database_url=database_url,
        data_dir=data_dir,
        logs_dir=logs_dir,
        config_dir=config_dir,
        secret_key=secret_key,
        timezone=timezone,
        embedded_scheduler=_parse_bool(
            "DATAFLOW_EMBEDDED_SCHEDULER", env.get("DATAFLOW_EMBEDDED_SCHEDULER"), True
        ),
        allow_http=_parse_bool("DATAFLOW_ALLOW_HTTP", env.get("DATAFLOW_ALLOW_HTTP"), False),
        allowed_domains=allowed_domains,
        use_env_proxy=_parse_bool(
            "DATAFLOW_USE_ENV_PROXY", env.get("DATAFLOW_USE_ENV_PROXY"), False
        ),
        max_upload_mb=_parse_int(
            "DATAFLOW_MAX_UPLOAD_MB", env.get("DATAFLOW_MAX_UPLOAD_MB"), 200, 1, 2048
        ),
        max_response_mb=_parse_int(
            "DATAFLOW_MAX_RESPONSE_MB", env.get("DATAFLOW_MAX_RESPONSE_MB"), 50, 1, 1024
        ),
        lock_timeout_minutes=_parse_int(
            "DATAFLOW_LOCK_TIMEOUT_MINUTES", env.get("DATAFLOW_LOCK_TIMEOUT_MINUTES"), 120, 1, 10080
        ),
        extra={k: v for k, v in env.items() if k.startswith("DATAFLOW_")},
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached)."""
    settings = load_settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests)."""
    get_settings.cache_clear()
