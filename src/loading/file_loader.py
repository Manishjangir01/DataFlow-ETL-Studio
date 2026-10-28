"""Write CSV and Parquet exports atomically into ``data/output``.

Files are written to a temporary file in the same folder and moved into place with
``os.replace`` only after the row count was verified, so readers never see a
half-written file and a failed run leaves the previous file untouched.
"""

from __future__ import annotations

import csv
import os
import shutil
import tempfile
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from src.loading.models import DestinationConfig, DestinationType, LoadStrategy
from src.loading.sql_loader import LoadResult, TargetInfo
from src.utils.config import Settings
from src.utils.exceptions import ConfigurationError, LoadError

_IN_USE_HINT = "Close the file if it is open in Excel or another program, then retry."


def output_path(dest: DestinationConfig, settings: Settings) -> Path:
    """Absolute path of the output file, guaranteed to be inside data/output."""
    base = settings.output_dir.resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = (base / (dest.file_name or "")).resolve()
    if path.parent != base:
        raise ConfigurationError("Output files must be written to the data/output folder.")
    return path


def _csv_rows(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def _csv_header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle), [])


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c) for c in out.columns]
    return out


def _temp_path(target: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=target.suffix + ".tmp", dir=target.parent)
    os.close(fd)
    return Path(name)


def _swap(temp: Path, target: Path) -> None:
    try:
        os.replace(temp, target)
    except PermissionError as exc:
        temp.unlink(missing_ok=True)
        raise LoadError(f"Cannot overwrite {target.name}: the file is locked.", hint=_IN_USE_HINT) from exc


def _parquet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Make mixed-type object columns writable by pyarrow (as text)."""
    out = df.copy()
    for column in out.columns:
        if out[column].dtype == object:
            types = {type(v) for v in out[column].dropna().head(5000)}
            if len(types) > 1 and not types <= {int, float}:
                out[column] = out[column].map(lambda v: v if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
    return out


def load_file(df: pd.DataFrame, dest: DestinationConfig, settings: Settings) -> LoadResult:
    """Write ``df`` to the configured CSV / Parquet file and verify the result."""
    if dest.strategy == LoadStrategy.UPSERT:
        raise LoadError("Upsert is not supported for file destinations.", hint="Use SQLite or PostgreSQL for upserts.")
    if dest.strategy == LoadStrategy.REPLACE and not dest.confirm_replace:
        raise LoadError(
            "The Replace strategy overwrites the existing file and needs confirmation.",
            hint="Tick 'I understand existing data will be replaced' in the destination settings.",
        )
    started = time.perf_counter()
    target = output_path(dest, settings)
    data = _prepare(df)
    exists = target.exists()
    append = dest.strategy == LoadStrategy.APPEND and exists
    temp = _temp_path(target)
    try:
        if dest.type == DestinationType.CSV:
            if append:
                header = _csv_header(target)
                if header != list(data.columns):
                    raise LoadError(
                        "The columns do not match the existing CSV file.",
                        hint=f"Existing: {', '.join(header)}. New: {', '.join(data.columns)}. Use Replace or a new file name.",
                    )
                before = _csv_rows(target)
                shutil.copyfile(target, temp)
                with open(temp, "rb+") as handle:  # make sure the file ends with a newline
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() > 0:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) not in (b"\n", b"\r"):
                            handle.write(b"\n")
                data.to_csv(temp, mode="a", header=False, index=False, encoding="utf-8", lineterminator="\n")
            else:
                before = 0
                data.to_csv(temp, index=False, encoding="utf-8", lineterminator="\n")
            after = _csv_rows(temp)
        else:
            if append:
                existing = pd.read_parquet(target)
                if set(existing.columns) != set(data.columns):
                    raise LoadError(
                        "The columns do not match the existing Parquet file.",
                        hint=f"Existing: {', '.join(existing.columns)}. Use Replace or a new file name.",
                    )
                before = len(existing)
                combined = pd.concat([existing, data[list(existing.columns)]], ignore_index=True)
            else:
                before = 0
                combined = data
            try:
                _parquet_safe(combined).to_parquet(temp, index=False)
            except Exception as exc:  # pyarrow raises many types
                raise LoadError(f"Could not write Parquet: {exc}", hint="Convert mixed-type columns with 'Change data types'.") from exc
            after = pq.ParquetFile(temp).metadata.num_rows

        if after - before != len(data):
            raise LoadError(f"Verification failed: expected {len(data)} new rows, file has {after - before}.")
        _swap(temp, target)
    except LoadError:
        temp.unlink(missing_ok=True)
        raise
    except PermissionError as exc:
        temp.unlink(missing_ok=True)
        raise LoadError(f"Permission denied writing {target.name}.", hint=_IN_USE_HINT) from exc
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise LoadError(f"Could not write {target.name}: {exc.strerror or exc}") from exc

    strategy = "append" if dest.strategy == LoadStrategy.APPEND else "replace"
    note = f"File now has {after:,} rows" + ("" if append else " (new file)" if not exists else " (replaced)")
    return LoadResult(
        dest.type.value, dest.target_label(), strategy, len(data), rows_inserted=len(data), rows_updated=0,
        verified=True, verification=note, duration_seconds=round(time.perf_counter() - started, 3),
    )


def inspect_file_target(dest: DestinationConfig, settings: Settings) -> TargetInfo:
    path = output_path(dest, settings)
    if not path.exists():
        return TargetInfo(False, None, [], dest.target_label(), "The file does not exist yet; it will be created.")
    if dest.type == DestinationType.CSV:
        return TargetInfo(True, _csv_rows(path), _csv_header(path), dest.target_label())
    meta = pq.ParquetFile(path)
    return TargetInfo(True, meta.metadata.num_rows, meta.schema_arrow.names, dest.target_label())


def preview_file_target(dest: DestinationConfig, settings: Settings, limit: int = 50) -> pd.DataFrame:
    path = output_path(dest, settings)
    if not path.exists():
        return pd.DataFrame()
    if dest.type == DestinationType.CSV:
        return pd.read_csv(path, nrows=limit)
    return pd.read_parquet(path).head(limit)
