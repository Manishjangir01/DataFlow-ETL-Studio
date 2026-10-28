"""Read CSV, JSON (including JSON Lines) and Parquet files into DataFrames.

Uploaded files are stored under ``data/uploads`` so that saved and scheduled pipelines
can re-read them later. Every path is resolved and checked to stay inside that folder.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.extraction.normalize import extract_records, make_cells_scalar, records_to_dataframe
from src.utils.exceptions import FileReadError, SecurityError

FileFormat = Literal["csv", "json", "parquet"]

_EXTENSION_FORMATS: dict[str, FileFormat] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".parquet": "parquet",
    ".pq": "parquet",
}
_FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class FileReadOptions:
    """Options for parsing a file."""

    delimiter: str | None = None          # None = auto-detect
    encoding: str = "utf-8"
    read_all_as_text: bool = False        # keep values such as 00123 unchanged
    records_path: str | None = None       # JSON only
    flatten_nested: bool = True           # JSON only


@dataclass
class FileReadResult:
    dataframe: pd.DataFrame
    file_format: FileFormat
    details: dict = field(default_factory=dict)


def detect_format(filename: str) -> FileFormat:
    """Infer the file format from the extension."""
    suffix = Path(filename).suffix.lower()
    if suffix not in _EXTENSION_FORMATS:
        allowed = ", ".join(sorted(_EXTENSION_FORMATS))
        raise FileReadError(f"Unsupported file type '{suffix or filename}'. Supported: {allowed}.")
    return _EXTENSION_FORMATS[suffix]


def _decode(data: bytes, encoding: str) -> tuple[str, str]:
    tried = []
    for candidate in (encoding, *_FALLBACK_ENCODINGS):
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            return data.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    raise FileReadError(f"Could not decode the file with encodings: {', '.join(tried)}.")


def _sniff_delimiter(text: str) -> str:
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample else ""
        counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def read_csv_bytes(data: bytes, options: FileReadOptions) -> FileReadResult:
    text, used_encoding = _decode(data, options.encoding)
    if not text.strip():
        raise FileReadError("The CSV file is empty.")
    delimiter = options.delimiter or _sniff_delimiter(text)
    try:
        with warnings.catch_warnings():
            # Rows with more fields than the header must fail, not be silently cut.
            warnings.simplefilter("error", pd.errors.ParserWarning)
            df = pd.read_csv(
                io.StringIO(text),
                sep=delimiter,
                dtype=str if options.read_all_as_text else None,
                keep_default_na=not options.read_all_as_text,
                index_col=False,
                on_bad_lines="error",
            )
    except (pd.errors.ParserError, pd.errors.ParserWarning) as exc:
        raise FileReadError(
            f"The CSV file is malformed: {exc}",
            hint="Check the delimiter and that every row has the same number of fields.",
        ) from exc
    except pd.errors.EmptyDataError as exc:
        raise FileReadError("The CSV file has no columns.") from exc
    df.columns = [str(c).strip() for c in df.columns]
    return FileReadResult(df, "csv", {"delimiter": delimiter, "encoding": used_encoding})


def read_json_bytes(data: bytes, options: FileReadOptions) -> FileReadResult:
    text, used_encoding = _decode(data, options.encoding)
    if not text.strip():
        raise FileReadError("The JSON file is empty.")
    layout = "json"
    try:
        document = json.loads(text)
    except json.JSONDecodeError as first_error:
        # Try JSON Lines (one object per line).
        document = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                document.append(json.loads(line))
            except json.JSONDecodeError:
                raise FileReadError(
                    f"Invalid JSON: {first_error.msg} at line {first_error.lineno}, "
                    f"column {first_error.colno}.",
                    hint="The file is neither a JSON document nor JSON Lines.",
                ) from first_error
        layout = "jsonl"
    records = extract_records(document, options.records_path)
    df = records_to_dataframe(records, flatten=options.flatten_nested)
    if options.read_all_as_text and not df.empty:
        df = df.astype("string").astype(object).where(df.notna(), None)
    return FileReadResult(df, "json", {"layout": layout, "encoding": used_encoding})


def read_parquet_bytes(data: bytes, options: FileReadOptions) -> FileReadResult:
    try:
        df = pd.read_parquet(io.BytesIO(data))
    except Exception as exc:  # pyarrow raises several exception types
        raise FileReadError(f"Could not read the Parquet file: {exc}") from exc
    df.columns = [str(c) for c in df.columns]
    df = make_cells_scalar(df)
    if options.read_all_as_text and not df.empty:
        df = df.astype("string").astype(object).where(df.notna(), None)
    return FileReadResult(df, "parquet", {})


def read_file_bytes(
    data: bytes,
    file_format: FileFormat,
    options: FileReadOptions | None = None,
    *,
    max_bytes: int | None = None,
) -> FileReadResult:
    """Parse file content into a DataFrame.

    Raises:
        FileReadError: when the file is too large, empty or malformed.
    """
    options = options or FileReadOptions()
    if max_bytes is not None and len(data) > max_bytes:
        raise FileReadError(
            f"The file is {len(data) / 1_048_576:.1f} MB, larger than the "
            f"{max_bytes / 1_048_576:.0f} MB limit.",
            hint="Increase DATAFLOW_MAX_UPLOAD_MB if needed.",
        )
    readers = {"csv": read_csv_bytes, "json": read_json_bytes, "parquet": read_parquet_bytes}
    if file_format not in readers:
        raise FileReadError(f"Unsupported file format '{file_format}'.")
    result = readers[file_format](data, options)
    result.details["rows"] = len(result.dataframe)
    result.details["columns"] = len(result.dataframe.columns)
    return result


# ---------------------------------------------------------------------------------
# Upload storage
# ---------------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    """Return a file name containing only safe characters (no folders)."""
    base = Path(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


def save_upload(data: bytes, original_name: str, upload_dir: Path) -> str:
    """Store an uploaded file and return its path relative to ``upload_dir``."""
    detect_format(original_name)  # reject unsupported types early
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{safe_filename(original_name)}"
    target = upload_dir / stored_name
    target.write_bytes(data)
    return stored_name


def resolve_upload(relative_name: str, upload_dir: Path) -> Path:
    """Return the absolute path of a stored upload, refusing paths outside the folder."""
    base = upload_dir.resolve()
    candidate = (base / relative_name).resolve()
    if base not in candidate.parents and candidate != base:
        raise SecurityError("The file path points outside the uploads folder.")
    if not candidate.is_file():
        raise FileReadError(
            f"The uploaded file '{relative_name}' no longer exists.",
            hint="Upload the file again in the pipeline's source settings.",
        )
    return candidate
