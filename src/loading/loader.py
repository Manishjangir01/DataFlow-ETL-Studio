"""Destination dispatcher used by the runner and the UI."""

from __future__ import annotations

import pandas as pd

from src.loading.file_loader import inspect_file_target, load_file, output_path, preview_file_target
from src.loading.models import DestinationConfig, LoadStrategy
from src.loading.sql_loader import (
    LoadResult,
    TargetInfo,
    inspect_sql_target,
    list_sql_tables,
    load_sql,
    preview_sql_target,
    check_sql_connection,
)
from src.security.secrets import SecretBox
from src.utils.config import Settings


def load_data(df: pd.DataFrame, dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> LoadResult:
    """Load ``df`` into ``dest`` and return the verified result."""
    if dest.is_file:
        return load_file(df, dest, settings)
    return load_sql(df, dest, settings, secret_box)


def inspect_target(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> TargetInfo:
    if dest.is_file:
        return inspect_file_target(dest, settings)
    return inspect_sql_target(dest, settings, secret_box)


def preview_target(dest: DestinationConfig, settings: Settings, secret_box: SecretBox, limit: int = 50) -> pd.DataFrame:
    if dest.is_file:
        return preview_file_target(dest, settings, limit)
    return preview_sql_target(dest, settings, secret_box, limit)


def check_destination(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> str:
    if dest.is_file:
        path = output_path(dest, settings)
        probe = path.parent / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return f"Output folder is writable: {path.parent}"
    return check_sql_connection(dest, settings, secret_box)


def list_tables(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> list[str]:
    return [] if dest.is_file else list_sql_tables(dest, settings, secret_box)


def destructive_warning(dest: DestinationConfig, info: TargetInfo | None) -> str | None:
    """Return a warning text when the configured load would delete existing data."""
    if dest.strategy != LoadStrategy.REPLACE:
        return None
    if info is not None and not info.exists:
        return None
    rows = f"{info.row_count:,} existing row(s)" if info and info.row_count is not None else "all existing data"
    return f"Replace will permanently delete {rows} in {dest.target_label()} before loading."
