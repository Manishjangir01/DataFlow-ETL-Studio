"""Single entry point that extracts a DataFrame from any configured source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from src.connectors.models import FileSourceConfig, SourceConfig
from src.extraction.file_reader import FileReadOptions, read_file_bytes, resolve_upload
from src.extraction.normalize import records_to_dataframe
from src.utils.config import Settings


@dataclass
class ExtractResult:
    """Extracted data plus details for the run log."""

    dataframe: pd.DataFrame
    records: list[dict[str, Any]] | None = None   # raw API records (kept for cached retries)
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def file_read_options(cfg: FileSourceConfig) -> FileReadOptions:
    return FileReadOptions(
        delimiter=cfg.delimiter,
        encoding=cfg.encoding,
        read_all_as_text=cfg.read_all_as_text,
        records_path=cfg.records_path,
        flatten_nested=cfg.flatten_nested,
    )


def extract_file_source(cfg: FileSourceConfig, settings: Settings) -> ExtractResult:
    """Read a stored upload."""
    path = resolve_upload(cfg.stored_name, settings.upload_dir)
    data = path.read_bytes()
    result = read_file_bytes(
        data, cfg.file_format, file_read_options(cfg), max_bytes=settings.max_upload_mb * 1_048_576
    )
    details = {"file": cfg.original_name, "format": cfg.file_format, "bytes": len(data), **result.details}
    return ExtractResult(result.dataframe, None, details)


def dataframe_from_api_records(records: list[dict[str, Any]], flatten: bool) -> pd.DataFrame:
    return records_to_dataframe(records, flatten=flatten)


def extract_source(
    source: SourceConfig,
    settings: Settings,
    *,
    api_extractor_factory: Callable[..., Any] | None = None,
    max_records: int | None = None,
    max_pages: int | None = None,
) -> ExtractResult:
    """Extract data from ``source``.

    Args:
        api_extractor_factory: callable ``(api_config) -> extractor`` used for API sources
            (injected so the UI, the runner and tests can supply secrets and sessions).
        max_records / max_pages: optional lower limits for quick previews.
    """
    if source.kind == "file" and source.file is not None:
        result = extract_file_source(source.file, settings)
        if max_records is not None and len(result.dataframe) > max_records:
            result.dataframe = result.dataframe.head(max_records).reset_index(drop=True)
            result.warnings.append(f"Preview limited to the first {max_records:,} rows.")
        return result

    if source.kind == "api" and source.api is not None:
        if api_extractor_factory is None:
            raise ValueError("An API extractor factory is required for API sources.")
        extractor = api_extractor_factory(source.api)
        fetched = extractor.fetch(max_records=max_records, max_pages=max_pages)
        df = dataframe_from_api_records(fetched.records, source.api.flatten_nested)
        details = {
            "url": source.api.display_url(),
            "pages": fetched.pages_fetched,
            "requests": fetched.requests_made,
            "records_path": fetched.records_path,
            "truncated": fetched.truncated,
        }
        return ExtractResult(df, fetched.records, details, list(fetched.warnings))

    raise ValueError(f"Unsupported source kind '{source.kind}'.")
